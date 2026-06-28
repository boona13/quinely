"""
ghost_mcp.py — Model Context Protocol (MCP) client.

Connects Ghost to external MCP servers (the 2026 standard for agent tool
servers — Linear, Sentry, Stripe, filesystem, Playwright, custom servers, etc.)
and bridges their tools into Ghost's own ToolRegistry so the LLM can call them
like any native tool.

Transport: MCP **stdio** — each JSON-RPC 2.0 message is a single newline-
delimited JSON object exchanged over the child process's stdin/stdout. This is
the most widely supported MCP transport and needs no extra dependencies (pure
stdlib subprocess + json + threading), keeping Ghost cross-platform.

Configuration lives in ``~/.ghost/mcp_servers.json`` (Claude/Cursor-compatible
shape)::

    {
      "mcpServers": {
        "filesystem": {
          "command": "npx",
          "args": ["-y", "@modelcontextprotocol/server-filesystem", "/path"],
          "env": {}
        }
      }
    }

A server may also set ``"disabled": true`` to skip it. Bridged tools are named
``mcp_<server>_<tool>`` to avoid collisions with native tools.
"""

import json
import logging
import os
import re
import subprocess
import threading
from pathlib import Path
from typing import Any, Dict, List, Optional

log = logging.getLogger("ghost.mcp")

CONFIG_PATH = Path.home() / ".ghost" / "mcp_servers.json"
PROTOCOL_VERSION = "2024-11-05"
DEFAULT_REQUEST_TIMEOUT = 30.0
TOOL_CALL_TIMEOUT = 120.0
MAX_RESULT_CHARS = 12000

_NAME_SANITIZE = re.compile(r"[^a-zA-Z0-9_]+")


def _safe_segment(name: str) -> str:
    return _NAME_SANITIZE.sub("_", (name or "").strip()).strip("_") or "x"


class MCPClient:
    """A single MCP server connection over stdio JSON-RPC."""

    def __init__(self, name: str, command: str, args: List[str] = None,
                 env: Dict[str, str] = None, cwd: str = None):
        self.name = name
        self.command = command
        self.args = args or []
        self.env = env or {}
        self.cwd = cwd
        self.proc: Optional[subprocess.Popen] = None
        self.tools: List[Dict[str, Any]] = []
        self.server_info: Dict[str, Any] = {}
        self._id = 0
        self._id_lock = threading.Lock()
        self._pending: Dict[int, Dict[str, Any]] = {}
        self._pending_lock = threading.Lock()
        self._reader: Optional[threading.Thread] = None
        self._alive = False
        self._write_lock = threading.Lock()

    # ── lifecycle ────────────────────────────────────────────────
    def start(self) -> None:
        full_env = dict(os.environ)
        full_env.update({k: str(v) for k, v in self.env.items()})
        self.proc = subprocess.Popen(
            [self.command, *self.args],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            bufsize=1,  # line-buffered
            cwd=self.cwd,
            env=full_env,
        )
        self._alive = True
        self._reader = threading.Thread(target=self._read_loop, daemon=True,
                                        name=f"mcp-{self.name}-reader")
        self._reader.start()

    def connect(self) -> None:
        """Full startup: spawn, initialize handshake, and list tools."""
        self.start()
        init = self._request("initialize", {
            "protocolVersion": PROTOCOL_VERSION,
            "capabilities": {"tools": {}},
            "clientInfo": {"name": "ghost", "version": "1.0"},
        })
        self.server_info = (init or {}).get("serverInfo", {})
        # Per spec, notify the server that initialization is complete.
        self._notify("notifications/initialized", {})
        self._refresh_tools()

    def _refresh_tools(self) -> None:
        result = self._request("tools/list", {})
        self.tools = (result or {}).get("tools", []) or []

    def stop(self) -> None:
        self._alive = False
        if self.proc:
            try:
                self.proc.terminate()
                try:
                    self.proc.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    self.proc.kill()
            except Exception:
                pass
        self.proc = None

    # ── JSON-RPC plumbing ────────────────────────────────────────
    def _next_id(self) -> int:
        with self._id_lock:
            self._id += 1
            return self._id

    def _read_loop(self) -> None:
        stream = self.proc.stdout if self.proc else None
        if not stream:
            return
        for line in stream:
            line = line.strip()
            if not line:
                continue
            try:
                msg = json.loads(line)
            except json.JSONDecodeError:
                continue  # non-JSON log noise from the server
            mid = msg.get("id")
            if mid is None:
                continue  # notification/request from server — ignored (no sampling)
            with self._pending_lock:
                slot = self._pending.get(mid)
                if slot is not None:
                    slot["response"] = msg
                    slot["event"].set()
        self._alive = False

    def _send(self, payload: Dict[str, Any]) -> None:
        if not self.proc or not self.proc.stdin:
            raise RuntimeError(f"MCP server '{self.name}' is not running")
        data = json.dumps(payload) + "\n"
        with self._write_lock:
            self.proc.stdin.write(data)
            self.proc.stdin.flush()

    def _notify(self, method: str, params: Dict[str, Any]) -> None:
        self._send({"jsonrpc": "2.0", "method": method, "params": params})

    def _request(self, method: str, params: Dict[str, Any],
                 timeout: float = DEFAULT_REQUEST_TIMEOUT) -> Dict[str, Any]:
        mid = self._next_id()
        event = threading.Event()
        with self._pending_lock:
            self._pending[mid] = {"event": event, "response": None}
        self._send({"jsonrpc": "2.0", "id": mid, "method": method, "params": params})
        if not event.wait(timeout):
            with self._pending_lock:
                self._pending.pop(mid, None)
            raise TimeoutError(f"MCP '{self.name}' {method} timed out after {timeout}s")
        with self._pending_lock:
            slot = self._pending.pop(mid, None)
        resp = (slot or {}).get("response") or {}
        if "error" in resp:
            err = resp["error"]
            raise RuntimeError(f"MCP error {err.get('code')}: {err.get('message')}")
        return resp.get("result", {})

    # ── tool invocation ──────────────────────────────────────────
    def call_tool(self, tool_name: str, arguments: Dict[str, Any]) -> str:
        result = self._request(
            "tools/call",
            {"name": tool_name, "arguments": arguments or {}},
            timeout=TOOL_CALL_TIMEOUT,
        )
        return self._format_result(result)

    @staticmethod
    def _format_result(result: Dict[str, Any]) -> str:
        parts: List[str] = []
        for block in result.get("content", []) or []:
            btype = block.get("type")
            if btype == "text":
                parts.append(block.get("text", ""))
            elif btype == "image":
                parts.append(f"[image: {block.get('mimeType', 'image')}]")
            elif btype == "resource":
                res = block.get("resource", {})
                parts.append(res.get("text") or f"[resource: {res.get('uri', '')}]")
            else:
                parts.append(json.dumps(block))
        text = "\n".join(p for p in parts if p)
        if not text:
            text = json.dumps(result)[:MAX_RESULT_CHARS]
        if result.get("isError"):
            text = "ERROR from MCP tool: " + text
        if len(text) > MAX_RESULT_CHARS:
            text = text[:MAX_RESULT_CHARS] + f"\n... (truncated {len(text) - MAX_RESULT_CHARS} chars)"
        return text


class MCPManager:
    """Loads MCP server configs, connects clients, and bridges their tools."""

    def __init__(self, config_path: Path = CONFIG_PATH):
        self.config_path = Path(config_path)
        self.clients: Dict[str, MCPClient] = {}
        self.errors: Dict[str, str] = {}

    def load_config(self) -> Dict[str, Any]:
        if not self.config_path.exists():
            return {}
        try:
            data = json.loads(self.config_path.read_text(encoding="utf-8"))
        except Exception as e:
            log.warning("Failed to parse %s: %s", self.config_path, e)
            return {}
        return data.get("mcpServers", data) if isinstance(data, dict) else {}

    def connect_all(self) -> List[Dict[str, Any]]:
        """Connect every configured server. Returns bridged tool definitions."""
        servers = self.load_config()
        tool_defs: List[Dict[str, Any]] = []
        for name, spec in servers.items():
            if not isinstance(spec, dict) or spec.get("disabled"):
                continue
            command = spec.get("command")
            if not command:
                self.errors[name] = "missing 'command'"
                continue
            try:
                client = MCPClient(
                    name=name,
                    command=command,
                    args=spec.get("args", []),
                    env=spec.get("env", {}),
                    cwd=spec.get("cwd"),
                )
                client.connect()
                self.clients[name] = client
                tool_defs.extend(self._bridge(client))
                log.info("MCP server '%s' connected: %d tools", name, len(client.tools))
            except Exception as e:
                self.errors[name] = str(e)
                log.warning("MCP server '%s' failed: %s", name, e)
        return tool_defs

    def _bridge(self, client: MCPClient) -> List[Dict[str, Any]]:
        defs: List[Dict[str, Any]] = []
        server_seg = _safe_segment(client.name)
        for tool in client.tools:
            raw_name = tool.get("name", "")
            if not raw_name:
                continue
            bridged_name = f"mcp_{server_seg}_{_safe_segment(raw_name)}"
            schema = tool.get("inputSchema") or {"type": "object", "properties": {}}
            desc = tool.get("description") or f"{raw_name} (via MCP server '{client.name}')"
            desc = f"[MCP:{client.name}] {desc}"

            def _make_exec(cl: MCPClient, tname: str):
                def _execute(**kwargs):
                    try:
                        return cl.call_tool(tname, kwargs)
                    except Exception as e:
                        return f"ERROR calling MCP tool '{tname}' on '{cl.name}': {e}"
                return _execute

            defs.append({
                "name": bridged_name,
                "description": desc[:1024],
                "parameters": schema,
                "execute": _make_exec(client, raw_name),
            })
        return defs

    def status(self) -> Dict[str, Any]:
        return {
            "connected": {
                name: {
                    "tools": len(c.tools),
                    "tool_names": [t.get("name") for t in c.tools],
                    "server_info": c.server_info,
                }
                for name, c in self.clients.items()
            },
            "errors": dict(self.errors),
            "config_path": str(self.config_path),
        }

    def reload(self, registry=None) -> List[Dict[str, Any]]:
        """Disconnect, reconnect from current config, and (optionally) re-register
        bridged tools into a live ToolRegistry. Returns the new tool defs."""
        if registry is not None:
            for name in list(registry.names()):
                if name.startswith("mcp_") and name != "mcp_status":
                    try:
                        registry.unregister(name)
                    except Exception:
                        pass
        self.shutdown()
        self.errors = {}
        defs = self.connect_all()
        if registry is not None:
            for d in defs:
                try:
                    registry.register(d)
                except Exception as e:
                    log.warning("Failed to register MCP tool %s: %s", d.get("name"), e)
        return defs

    def shutdown(self) -> None:
        for client in self.clients.values():
            try:
                client.stop()
            except Exception:
                pass
        self.clients.clear()


def build_mcp_introspection_tools(manager: "MCPManager") -> List[Dict[str, Any]]:
    """A tool letting the LLM inspect connected MCP servers."""
    def _mcp_status():
        st = manager.status()
        if not st["connected"] and not st["errors"]:
            return (
                "No MCP servers configured. Add servers to "
                f"{st['config_path']} (mcpServers map) and restart Ghost."
            )
        lines = []
        for name, info in st["connected"].items():
            lines.append(f"• {name}: {info['tools']} tools — {', '.join(info['tool_names'][:20])}")
        for name, err in st["errors"].items():
            lines.append(f"• {name}: FAILED — {err}")
        return "\n".join(lines)

    return [{
        "name": "mcp_status",
        "description": "List connected Model Context Protocol (MCP) servers and the tools they expose. MCP tools are callable directly as mcp_<server>_<tool>.",
        "parameters": {"type": "object", "properties": {}},
        "execute": lambda: _mcp_status(),
    }]
