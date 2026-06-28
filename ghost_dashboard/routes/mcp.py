"""MCP API — manage external Model Context Protocol tool servers."""

import json
import logging
from pathlib import Path
from flask import Blueprint, jsonify, request

log = logging.getLogger(__name__)

bp = Blueprint("mcp", __name__)

CONFIG_PATH = Path.home() / ".ghost" / "mcp_servers.json"


def _get_daemon():
    try:
        from ghost_dashboard import get_daemon
        return get_daemon()
    except Exception:
        return None


def _read_config() -> dict:
    if not CONFIG_PATH.exists():
        return {"mcpServers": {}}
    try:
        data = json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
        if isinstance(data, dict) and "mcpServers" in data:
            return data
        if isinstance(data, dict):
            return {"mcpServers": data}
    except Exception as e:
        log.warning("Failed to read mcp config: %s", e)
    return {"mcpServers": {}}


def _write_config(cfg: dict) -> None:
    CONFIG_PATH.parent.mkdir(parents=True, exist_ok=True)
    CONFIG_PATH.write_text(json.dumps(cfg, indent=2), encoding="utf-8")


def _norm_args(args):
    """Accept a list, or a string (split on whitespace)."""
    if isinstance(args, list):
        return [str(a) for a in args]
    if isinstance(args, str):
        return args.split()
    return []


def _norm_env(env):
    if isinstance(env, dict):
        return {str(k): str(v) for k, v in env.items()}
    return {}


@bp.route("/api/mcp/status")
def mcp_status():
    daemon = _get_daemon()
    enabled = True
    if daemon is not None:
        try:
            enabled = bool(daemon.cfg.get("enable_mcp", True))
        except Exception:
            enabled = True

    manager = getattr(daemon, "mcp_manager", None) if daemon else None
    live = manager.status() if manager else {"connected": {}, "errors": {}}
    connected = live.get("connected", {})
    errors = live.get("errors", {})

    cfg = _read_config()
    servers = []
    for name, spec in (cfg.get("mcpServers", {}) or {}).items():
        if not isinstance(spec, dict):
            continue
        info = connected.get(name, {})
        servers.append({
            "name": name,
            "command": spec.get("command", ""),
            "args": spec.get("args", []),
            "env": spec.get("env", {}),
            "disabled": bool(spec.get("disabled", False)),
            "connected": name in connected,
            "tools": info.get("tool_names", []),
            "tool_count": info.get("tools", 0),
            "error": errors.get(name, ""),
        })

    return jsonify({
        "enabled": enabled,
        "available": manager is not None,
        "config_path": str(CONFIG_PATH),
        "servers": servers,
    })


@bp.route("/api/mcp/servers", methods=["POST"])
def save_server():
    data = request.get_json(silent=True) or {}
    name = (data.get("name") or "").strip()
    command = (data.get("command") or "").strip()
    if not name:
        return jsonify({"error": "name is required"}), 400
    if not command:
        return jsonify({"error": "command is required"}), 400

    cfg = _read_config()
    servers = cfg.setdefault("mcpServers", {})
    servers[name] = {
        "command": command,
        "args": _norm_args(data.get("args", [])),
        "env": _norm_env(data.get("env", {})),
        "disabled": bool(data.get("disabled", False)),
    }
    _write_config(cfg)
    return jsonify({"ok": True, "name": name,
                    "message": "Saved. Click Reconnect to apply."})


@bp.route("/api/mcp/servers/<name>/delete", methods=["POST"])
def delete_server(name):
    cfg = _read_config()
    servers = cfg.get("mcpServers", {})
    if name in servers:
        del servers[name]
        _write_config(cfg)
        return jsonify({"ok": True, "message": f"Removed '{name}'. Click Reconnect to apply."})
    return jsonify({"error": f"Server '{name}' not found"}), 404


@bp.route("/api/mcp/servers/<name>/toggle", methods=["POST"])
def toggle_server(name):
    cfg = _read_config()
    servers = cfg.get("mcpServers", {})
    if name not in servers or not isinstance(servers[name], dict):
        return jsonify({"error": f"Server '{name}' not found"}), 404
    servers[name]["disabled"] = not bool(servers[name].get("disabled", False))
    _write_config(cfg)
    return jsonify({"ok": True, "disabled": servers[name]["disabled"],
                    "message": "Updated. Click Reconnect to apply."})


@bp.route("/api/mcp/reload", methods=["POST"])
def reload_servers():
    daemon = _get_daemon()
    if daemon is None:
        return jsonify({"error": "Daemon not available"}), 503
    if not daemon.cfg.get("enable_mcp", True):
        return jsonify({"error": "MCP is disabled. Enable it in Configuration first."}), 400

    registry = getattr(daemon, "tool_registry", None)
    try:
        manager = getattr(daemon, "mcp_manager", None)
        if manager is None:
            from ghost_mcp import MCPManager, build_mcp_introspection_tools
            manager = MCPManager()
            daemon.mcp_manager = manager
            if registry is not None:
                for td in build_mcp_introspection_tools(manager):
                    registry.register(td)
        defs = manager.reload(registry)
        return jsonify({
            "ok": True,
            "connected": len(manager.clients),
            "tools_bridged": len(defs),
            "errors": manager.errors,
        })
    except Exception as e:
        log.warning("MCP reload failed: %s", e)
        return jsonify({"error": str(e)}), 500
