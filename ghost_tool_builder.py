"""
Ghost Tool Builder — discover, create, install, load, and manage ghost tools.

A "tool" is an isolated, LLM-callable capability that lives in ghost_tools/<name>/
and registers itself with Ghost's ToolRegistry via a register(api) entry point.

Tool directory layout:
    ghost_tools/<name>/
        TOOL.yaml          — manifest (name, version, deps, tools, hooks, settings)
        tool.py            — register(api) entry point
        vendor/            — optional: git-cloned library
        requirements.txt   — optional: pip dependencies
        data/              — runtime data (auto-created)
"""

import ast
import importlib
import importlib.util
import json
import logging
import shutil
import subprocess
import sys
import threading
import traceback
from collections import defaultdict
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Optional

_tool_progress = threading.local()


def set_tool_progress_callback(callback):
    """Set a thread-local callback for tool progress updates.

    callback(tool_id: str, message: str) is called by ToolAPI.log().
    """
    _tool_progress.callback = callback


def clear_tool_progress_callback():
    """Clear the thread-local progress callback."""
    _tool_progress.callback = None


log = logging.getLogger("quinely.tool_builder")

PROJECT_DIR = Path(__file__).resolve().parent
TOOLS_DIR = PROJECT_DIR / "ghost_tools"
TOOLS_DIR.mkdir(parents=True, exist_ok=True)
GHOST_HOME = Path.home() / ".ghost"

VALID_HOOK_EVENTS = frozenset({
    "on_boot", "on_shutdown",
    "on_chat_message", "on_tool_call",
    "on_tool_loop_complete", "on_tool_loop_error",
    "on_media_generated", "on_evolve_complete",
    "on_subagent_started", "on_subagent_completed", "on_subagent_failed",
})


# ═════════════════════════════════════════════════════════════════════
#  TOOL EVENT BUS
# ═════════════════════════════════════════════════════════════════════

class ToolEventBus:
    """Lightweight pub/sub for tool lifecycle hooks with source tracking."""

    def __init__(self):
        self._handlers: dict[str, list[tuple[str, Callable]]] = defaultdict(list)
        self._lock = threading.Lock()

    def subscribe(self, event: str, callback: Callable, source_id: str = ""):
        if event not in VALID_HOOK_EVENTS:
            log.warning(
                "Source '%s' tried to subscribe to unknown event '%s'. "
                "Valid events: %s. Hook will NOT be registered.",
                source_id, event, ", ".join(sorted(VALID_HOOK_EVENTS)),
            )
            return
        with self._lock:
            self._handlers[event].append((source_id, callback))

    def unsubscribe_all(self, source_id: str):
        """Remove all handlers registered by a given source."""
        with self._lock:
            for event in self._handlers:
                self._handlers[event] = [
                    (sid, cb) for sid, cb in self._handlers[event]
                    if sid != source_id
                ]

    def emit(self, event: str, **kwargs):
        with self._lock:
            handlers = list(self._handlers.get(event, []))
        for source_id, cb in handlers:
            try:
                cb(**kwargs)
            except Exception as e:
                log.warning("Hook %s failed for %s: %s", source_id, event, e)

    def get_subscribers(self, event: str) -> list[str]:
        with self._lock:
            return [sid for sid, _ in self._handlers.get(event, [])]

    def clear(self):
        with self._lock:
            self._handlers.clear()


# ═════════════════════════════════════════════════════════════════════
#  YAML LOADER
# ═════════════════════════════════════════════════════════════════════

def _load_yaml(path: Path) -> dict:
    """Load YAML with fallback to minimal parser if pyyaml unavailable."""
    text = path.read_text(encoding="utf-8")
    try:
        import yaml
        return yaml.safe_load(text) or {}
    except ImportError:
        pass
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        pass
    return _minimal_yaml_parse(text)


def _minimal_yaml_parse(text: str) -> dict:
    """Bare-bones YAML-subset parser for TOOL.yaml when PyYAML is missing."""
    result: dict[str, Any] = {}
    current_key: str | None = None
    current_list: list | None = None

    for line in text.splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue

        if stripped.startswith("- ") and current_key and current_list is not None:
            val = stripped[2:].strip().strip('"').strip("'")
            current_list.append(val)
            continue

        if ":" in stripped:
            if current_key and current_list is not None:
                result[current_key] = current_list
                current_list = None

            key, _, val = stripped.partition(":")
            key = key.strip()
            val = val.strip().strip('"').strip("'")

            if not val:
                current_key = key
                current_list = []
            elif val.startswith("[") and val.endswith("]"):
                items = val[1:-1]
                result[key] = [
                    i.strip().strip('"').strip("'")
                    for i in items.split(",") if i.strip()
                ]
                current_key = None
                current_list = None
            elif val.lower() in ("true", "false"):
                result[key] = val.lower() == "true"
                current_key = None
                current_list = None
            else:
                try:
                    result[key] = int(val)
                except ValueError:
                    result[key] = val
                current_key = None
                current_list = None

    if current_key and current_list is not None:
        result[current_key] = current_list

    return result


# ═════════════════════════════════════════════════════════════════════
#  TOOL MANIFEST
# ═════════════════════════════════════════════════════════════════════

@dataclass
class ToolManifest:
    name: str
    version: str = "1.0.0"
    description: str = ""
    author: str = "ghost"
    category: str = "utility"
    deps: list[str] = field(default_factory=list)
    tools: list[str] = field(default_factory=list)
    hooks: list[str] = field(default_factory=list)
    settings: list[dict] = field(default_factory=list)
    enabled: bool = True

    @classmethod
    def from_yaml(cls, path: Path) -> "ToolManifest":
        data = _load_yaml(path)
        return cls(
            name=data.get("name", path.parent.name),
            version=str(data.get("version", "1.0.0")),
            description=data.get("description", ""),
            author=data.get("author", "ghost"),
            category=data.get("category", "utility"),
            deps=data.get("deps") or data.get("dependencies", []),
            tools=data.get("tools", []),
            hooks=data.get("hooks", []),
            settings=data.get("settings", []),
            enabled=data.get("enabled", True),
        )

    def to_dict(self) -> dict:
        return {
            "name": self.name,
            "version": self.version,
            "description": self.description,
            "author": self.author,
            "category": self.category,
            "deps": self.deps,
            "tools": self.tools,
            "hooks": self.hooks,
            "settings": self.settings,
            "enabled": self.enabled,
        }


# ═════════════════════════════════════════════════════════════════════
#  TOOL API  (exposed to each tool's register() function)
# ═════════════════════════════════════════════════════════════════════

class ToolAPI:
    """API surface exposed to each tool's register() function.

    Deliberately minimal — no UI methods (register_page, register_route).
    Tools are LLM-callable capabilities only.
    """

    def __init__(self, tool_id: str, manifest: ToolManifest,
                 tool_registry, event_bus: ToolEventBus, config: dict,
                 memory_db=None):
        self.id = tool_id
        self.manifest = manifest
        self._tool_registry = tool_registry
        self._event_bus = event_bus
        self._config = config
        self._memory_db = memory_db
        self._registered_tools: list[str] = []
        self._registered_hooks: list[tuple[str, Callable]] = []
        self._registered_crons: list[dict] = []
        self._tool_dir = TOOLS_DIR / tool_id
        self._data_dir = TOOLS_DIR / tool_id / "data"
        self._data_dir.mkdir(parents=True, exist_ok=True)

    def register_tool(self, tool_def: dict):
        """Register an LLM-callable tool with Ghost's tool registry."""
        required = {"name", "description", "parameters", "execute"}
        missing = required - set(tool_def.keys())
        if missing:
            raise ValueError(f"Tool definition missing keys: {missing}")
        tool_def.setdefault("_tool_builder_id", self.id)
        self._tool_registry.register(tool_def)
        self._registered_tools.append(tool_def["name"])

    def register_hook(self, event: str, callback: Callable):
        """Subscribe to a lifecycle event."""
        self._event_bus.subscribe(event, callback, source_id=self.id)
        self._registered_hooks.append((event, callback))

    def register_cron(self, name: str, callback: Callable, schedule: str):
        """Register a scheduled task (cron expression)."""
        self._registered_crons.append({
            "name": f"{self.id}_{name}",
            "callback": callback,
            "schedule": schedule,
        })

    def register_setting(self, schema: dict):
        """Declare a configuration setting for this tool."""
        schema.setdefault("tool_id", self.id)
        self.manifest.settings.append(schema)

    def get_setting(self, key: str, default: Any = None) -> Any:
        """Read a tool-specific setting from Ghost config."""
        tool_settings = self._config.get("tool_settings", {}).get(self.id, {})
        return tool_settings.get(key, default)

    def set_setting(self, key: str, value: Any):
        """Write a tool-specific setting."""
        if "tool_settings" not in self._config:
            self._config["tool_settings"] = {}
        if self.id not in self._config["tool_settings"]:
            self._config["tool_settings"][self.id] = {}
        self._config["tool_settings"][self.id][key] = value

    def read_data(self, filename: str) -> Optional[str]:
        """Read from the tool's data directory."""
        path = self._data_dir / filename
        return path.read_text(encoding="utf-8") if path.exists() else None

    def write_data(self, filename: str, content: str):
        """Write to the tool's data directory."""
        (self._data_dir / filename).write_text(content, encoding="utf-8")

    def log(self, message: str):
        """Log with tool prefix and push to any active chat progress stream."""
        log.info("[tool:%s] %s", self.id, message)
        cb = getattr(_tool_progress, "callback", None)
        if cb:
            try:
                cb(self.id, message)
            except Exception:
                pass

    def memory_save(self, content: str, tags: list[str] | None = None,
                    type: str = "tool") -> bool:
        """Save content to Ghost's memory system."""
        if not self._memory_db:
            return False
        try:
            self._memory_db.save(
                content=content,
                type=type,
                source_preview=f"[tool:{self.id}] {content[:60]}",
                tools_used=self.id,
            )
            return True
        except Exception:
            log.warning("Tool %s: memory_save failed", self.id, exc_info=True)
            return False

    def memory_search(self, query: str, limit: int = 5) -> list[dict]:
        """Search Ghost's memory."""
        if not self._memory_db:
            return []
        try:
            results = self._memory_db.search(query, limit=limit)
            return [{"content": r.content, "type": r.type} for r in results]
        except Exception:
            log.warning("Tool %s: memory_search failed", self.id, exc_info=True)
            return []

    def llm_summarize(self, text: str, instruction: str = "Summarize concisely") -> str:
        """One-shot LLM call for summarization."""
        try:
            from ghost_llm import get_engine
            engine = get_engine()
            if engine:
                return engine.single_shot(
                    system_prompt=instruction,
                    user_message=text[:8000],
                )
        except Exception:
            pass
        return text[:500] + "..." if len(text) > 500 else text

    def channel_send(self, message: str):
        """Send a message via Ghost's channel system."""
        try:
            from ghost_channels import get_channel_manager
            cm = get_channel_manager()
            if cm:
                cm.send_all(message)
        except Exception:
            log.warning("Tool %s: channel_send failed", self.id, exc_info=True)


# ═════════════════════════════════════════════════════════════════════
#  TOOL INFO
# ═════════════════════════════════════════════════════════════════════

@dataclass
class ToolInfo:
    name: str
    path: str
    manifest: Optional[ToolManifest] = None
    enabled: bool = True
    loaded: bool = False
    tools: list = field(default_factory=list)
    crons: list = field(default_factory=list)
    error: Optional[str] = None

    def to_dict(self) -> dict:
        return {
            "name": self.name,
            "path": self.path,
            "manifest": self.manifest.to_dict() if self.manifest else None,
            "enabled": self.enabled,
            "loaded": self.loaded,
            "tools": self.tools,
            "crons": [c["name"] for c in self.crons],
            "error": self.error,
        }


# ═════════════════════════════════════════════════════════════════════
#  TOOL MANAGER
# ═════════════════════════════════════════════════════════════════════

class ToolManager:
    """Discover, load, and manage ghost tools."""

    def __init__(self, tool_registry, event_bus: ToolEventBus,
                 cfg: dict | None = None, memory_db=None):
        self.tool_registry = tool_registry
        self.event_bus = event_bus
        self.cfg = cfg or {}
        self.memory_db = memory_db
        self.tools: dict[str, ToolInfo] = {}
        self._disabled: set[str] = self._load_disabled()
        self._lock = threading.Lock()

    # ── Discover ──────────────────────────────────────────────────

    def discover_all(self):
        """Scan ghost_tools/ for TOOL.yaml or tool.py manifests."""
        if not TOOLS_DIR.is_dir():
            return

        for child in sorted(TOOLS_DIR.iterdir()):
            if not child.is_dir() or child.name.startswith((".", "_")):
                continue

            if (child / ".evolving").exists():
                log.info("Skipping %s — evolution in progress (.evolving marker)", child.name)
                continue

            manifest_path = child / "TOOL.yaml"
            entry_path = child / "tool.py"

            if not manifest_path.exists() and not entry_path.exists():
                continue

            try:
                manifest = ToolManifest.from_yaml(manifest_path) if manifest_path.exists() else ToolManifest(name=child.name)
            except Exception as e:
                log.warning("Failed to parse TOOL.yaml in %s: %s", child.name, e)
                self.tools[child.name] = ToolInfo(
                    name=child.name, path=str(child),
                    error=f"Manifest parse error: {e}",
                )
                continue

            info = ToolInfo(
                name=manifest.name,
                path=str(child),
                manifest=manifest,
                enabled=manifest.enabled and manifest.name not in self._disabled,
            )
            self.tools[manifest.name] = info

    # ── Load ──────────────────────────────────────────────────────

    def load_all(self) -> tuple[int, list[str]]:
        """Load and register all enabled tools. Returns (count, tool_names)."""
        tool_names = []
        loaded = 0
        failed = []

        for name, info in list(self.tools.items()):
            if not info.enabled:
                continue
            ok, names = self._load_tool(info)
            if ok:
                loaded += 1
                tool_names.extend(names)
            elif info.error:
                failed.append((name, info.error))

        if failed:
            for fname, ferr in failed:
                log.error(
                    "TOOL LOAD FAILED: '%s' — %s", fname,
                    ferr.split("\n")[0] if ferr else "unknown error",
                )

        return loaded, tool_names

    def _discover_and_load_one(self, name: str, tool_dir: Path) -> dict:
        """Discover and load a single tool by name. Used for hot-reload after creation."""
        manifest_path = tool_dir / "TOOL.yaml"
        entry_path = tool_dir / "tool.py"

        if not manifest_path.exists() and not entry_path.exists():
            return {"loaded": False, "error": "No TOOL.yaml or tool.py found"}

        try:
            manifest = (
                ToolManifest.from_yaml(manifest_path)
                if manifest_path.exists()
                else ToolManifest(name=name)
            )
        except Exception as e:
            err = f"Manifest parse error: {e}"
            self.tools[name] = ToolInfo(name=name, path=str(tool_dir), error=err)
            return {"loaded": False, "error": err}

        info = ToolInfo(
            name=manifest.name,
            path=str(tool_dir),
            manifest=manifest,
            enabled=manifest.enabled and manifest.name not in self._disabled,
        )
        self.tools[manifest.name] = info

        if not info.enabled:
            reason = "disabled in TOOL.yaml" if not manifest.enabled else "disabled by user"
            return {"loaded": False, "error": f"Tool is disabled ({reason})"}

        ok, names = self._load_tool(info)
        if ok:
            log.info("Hot-loaded tool '%s' → registered tools: %s", name, names)
            return {"loaded": True, "tools": names}
        return {"loaded": False, "error": info.error, "tools": []}

    def reload_tool(self, name: str) -> dict:
        """Hot-reload a tool at runtime (re-discover + re-load without restart).

        Unregisters old tool definitions first, then re-imports and re-registers.
        """
        info = self.tools.get(name)
        if info:
            for tool_name in info.tools:
                try:
                    self.tool_registry.unregister(tool_name)
                except Exception:
                    pass
            # Remove cached module so importlib re-reads the file
            mod_name = f"ghost_tool_{name}"
            import sys as _sys
            _sys.modules.pop(mod_name, None)

        tool_dir = TOOLS_DIR / name
        if not tool_dir.is_dir():
            return {"status": "error", "error": f"Tool directory not found: {tool_dir}"}

        result = self._discover_and_load_one(name, tool_dir)
        if result.get("loaded"):
            return {"status": "ok", "tools": result.get("tools", []),
                    "message": f"Tool '{name}' reloaded successfully"}
        return {"status": "error", "error": result.get("error", "Unknown error")}

    def _load_tool(self, info: ToolInfo) -> tuple[bool, list[str]]:
        """Import and call register(api) for a single tool."""
        tool_dir = Path(info.path)
        entry = tool_dir / "tool.py"
        if not entry.exists():
            info.error = "No tool.py found"
            return False, []

        if info.manifest and info.manifest.deps:
            self._install_deps(info.manifest.deps, info.name)

        vendor_dir = tool_dir / "vendor"
        if vendor_dir.is_dir() and str(vendor_dir) not in sys.path:
            sys.path.insert(0, str(vendor_dir))
        if str(tool_dir) not in sys.path:
            sys.path.insert(0, str(tool_dir))

        try:
            spec = importlib.util.spec_from_file_location(
                f"ghost_tool_{info.name}", str(entry),
            )
            mod = importlib.util.module_from_spec(spec)
            sys.modules[spec.name] = mod
            spec.loader.exec_module(mod)

            register_fn = getattr(mod, "register", None)
            if not register_fn:
                info.error = "tool.py missing register() function"
                return False, []

            api = ToolAPI(
                tool_id=info.name,
                manifest=info.manifest or ToolManifest(name=info.name),
                tool_registry=self.tool_registry,
                event_bus=self.event_bus,
                config=self.cfg,
                memory_db=self.memory_db,
            )
            register_fn(api)

            info.loaded = True
            info.tools = list(api._registered_tools)
            info.crons = list(api._registered_crons)
            info.error = None
            return True, info.tools

        except Exception as e:
            info.error = f"Load failed: {e}\n{traceback.format_exc()}"
            log.warning("Failed to load tool %s: %s", info.name, e)
            return False, []

    # ── Create ────────────────────────────────────────────────────

    def create_tool(self, name: str, description: str, code: str,
                    hooks: list[str] | None = None,
                    deps: list[str] | None = None,
                    overwrite: bool = False) -> dict:
        """Create a new tool folder with TOOL.yaml and tool.py, then auto-load it."""
        import re
        if not re.match(r"^[a-z][a-z0-9_]{1,48}$", name):
            return {"status": "error", "error": "Invalid tool name. Use lowercase, underscores, 2-50 chars."}

        tool_dir = TOOLS_DIR / name
        if tool_dir.exists() and not overwrite:
            return {"status": "error", "error": f"Tool '{name}' already exists. Set overwrite=true to replace it."}

        try:
            tool_dir.mkdir(parents=True, exist_ok=True)

            manifest = {
                "name": name,
                "version": "1.0.0",
                "description": description,
                "author": "ghost",
                "category": "utility",
            }
            if deps:
                manifest["deps"] = deps
            if hooks:
                manifest["hooks"] = hooks

            try:
                import yaml
                manifest_text = yaml.dump(manifest, default_flow_style=False, sort_keys=False)
            except ImportError:
                lines = []
                for k, v in manifest.items():
                    if isinstance(v, list):
                        lines.append(f"{k}:")
                        for item in v:
                            lines.append(f"  - {item}")
                    else:
                        lines.append(f"{k}: {v}")
                manifest_text = "\n".join(lines) + "\n"

            (tool_dir / "TOOL.yaml").write_text(manifest_text, encoding="utf-8")
            (tool_dir / "tool.py").write_text(code, encoding="utf-8")
            (tool_dir / "data").mkdir(exist_ok=True)

            # Auto-discover and load the new tool immediately
            load_result = self._discover_and_load_one(name, tool_dir)

            return {
                "status": "ok",
                "path": str(tool_dir),
                "message": f"Tool '{name}' created at {tool_dir}",
                "loaded": load_result.get("loaded", False),
                "registered_tools": load_result.get("tools", []),
                "load_error": load_result.get("error"),
            }

        except Exception as e:
            if tool_dir.exists():
                shutil.rmtree(tool_dir, ignore_errors=True)
            return {"status": "error", "error": str(e)}

    # ── Install from GitHub ───────────────────────────────────────

    def install_from_github(self, repo_url: str, subdir: str = "") -> dict:
        """Clone a GitHub repo into ghost_tools/<name>/vendor/ and create a wrapper."""
        import re
        match = re.search(r"github\.com[/:]([^/]+)/([^/.]+)", repo_url)
        if not match:
            return {"status": "error", "error": "Invalid GitHub URL"}

        repo_name = match.group(2).lower().replace("-", "_")
        tool_dir = TOOLS_DIR / repo_name
        vendor_dir = tool_dir / "vendor"

        if tool_dir.exists():
            return {"status": "error", "error": f"Tool '{repo_name}' already exists"}

        try:
            tool_dir.mkdir(parents=True)
            result = subprocess.run(
                ["git", "clone", "--depth", "1", repo_url, str(vendor_dir)],
                capture_output=True, text=True, timeout=120,
            )
            if result.returncode != 0:
                shutil.rmtree(tool_dir, ignore_errors=True)
                return {"status": "error", "error": f"git clone failed: {result.stderr[:500]}"}

            req_file = vendor_dir / "requirements.txt"
            if req_file.exists():
                shutil.copy2(req_file, tool_dir / "requirements.txt")

            manifest = {
                "name": repo_name,
                "version": "1.0.0",
                "description": f"Wrapper for {match.group(1)}/{match.group(2)}",
                "author": match.group(1),
                "category": "utility",
            }
            try:
                import yaml
                manifest_text = yaml.dump(manifest, default_flow_style=False, sort_keys=False)
            except ImportError:
                lines = [f"{k}: {v}" for k, v in manifest.items()]
                manifest_text = "\n".join(lines) + "\n"

            (tool_dir / "TOOL.yaml").write_text(manifest_text, encoding="utf-8")

            wrapper_code = (
                '"""Auto-generated wrapper for cloned repository."""\n'
                "import sys\n"
                "from pathlib import Path\n\n"
                "VENDOR_DIR = Path(__file__).parent / 'vendor'\n"
                "if str(VENDOR_DIR) not in sys.path:\n"
                "    sys.path.insert(0, str(VENDOR_DIR))\n\n\n"
                "def register(api):\n"
                '    """Register tools from the cloned repository.\n\n'
                "    TODO: Ghost should fill in the actual tool registrations\n"
                '    based on the repo\'s functionality.\n"""\n'
                f'    api.log("Wrapper for {repo_name} loaded from vendor/")\n'
            )
            (tool_dir / "tool.py").write_text(wrapper_code, encoding="utf-8")
            (tool_dir / "data").mkdir(exist_ok=True)

            return {
                "status": "ok",
                "path": str(tool_dir),
                "message": f"Cloned {repo_url} into {tool_dir}/vendor/. Edit tool.py to register tools.",
            }

        except Exception as e:
            if tool_dir.exists():
                shutil.rmtree(tool_dir, ignore_errors=True)
            return {"status": "error", "error": str(e)}

    # ── Uninstall ─────────────────────────────────────────────────

    def uninstall_tool(self, name: str) -> dict:
        """Remove a tool folder entirely."""
        info = self.tools.get(name)
        if not info:
            return {"status": "error", "error": f"Tool '{name}' not found"}

        tool_dir = Path(info.path)
        if not tool_dir.is_dir():
            self.tools.pop(name, None)
            return {"status": "ok", "message": f"Tool '{name}' already removed"}

        try:
            for tool_name in info.tools:
                try:
                    self.tool_registry.unregister(tool_name)
                except Exception:
                    pass
            shutil.rmtree(tool_dir)
            self.tools.pop(name, None)
            return {"status": "ok", "message": f"Tool '{name}' uninstalled"}
        except Exception as e:
            return {"status": "error", "error": str(e)}

    # ── List ──────────────────────────────────────────────────────

    def list_tools(self) -> list[dict]:
        """List all discovered tools with status."""
        return [info.to_dict() for info in self.tools.values()]

    def get_tool_names(self) -> list[str]:
        """Return names of all registered LLM tools from loaded tools."""
        names = []
        for info in self.tools.values():
            if info.loaded:
                names.extend(info.tools)
        return names

    # ── Validate ──────────────────────────────────────────────────

    def validate_tool(self, name: str) -> dict:
        """Validate a tool's syntax, imports, and registration."""
        tool_dir = TOOLS_DIR / name
        entry = tool_dir / "tool.py"
        issues: list[str] = []
        warnings: list[str] = []

        if not entry.exists():
            return {"valid": False, "issues": [f"No tool.py in {tool_dir}"]}

        source = entry.read_text(encoding="utf-8")

        try:
            tree = ast.parse(source, filename=str(entry))
        except SyntaxError as e:
            return {"valid": False, "issues": [f"Syntax error: {e}"]}

        has_register = any(
            isinstance(node, ast.FunctionDef) and node.name == "register"
            for node in ast.walk(tree)
        )
        if not has_register:
            issues.append("tool.py missing register() function")

        manifest_path = tool_dir / "TOOL.yaml"
        if manifest_path.exists():
            try:
                manifest = ToolManifest.from_yaml(manifest_path)
                for hook in manifest.hooks:
                    if hook not in VALID_HOOK_EVENTS:
                        warnings.append(f"Unknown hook event in manifest: {hook}")
            except Exception as e:
                issues.append(f"Invalid TOOL.yaml: {e}")

        return {
            "valid": len(issues) == 0,
            "issues": issues,
            "warnings": warnings,
        }

    # ── Enable / Disable ──────────────────────────────────────────

    def enable_tool(self, name: str, update_yaml: bool = False) -> dict:
        """Enable a tool. If update_yaml=True, also set enabled: true in TOOL.yaml."""
        info = self.tools.get(name)
        if not info:
            return {"status": "error", "error": f"Tool '{name}' not found"}
        self._disabled.discard(name)
        self._save_disabled()
        if update_yaml and info.manifest and not info.manifest.enabled:
            self._set_yaml_enabled(name, True)
            info.manifest.enabled = True
        info.enabled = True
        if not info.loaded:
            ok, names = self._load_tool(info)
            if ok:
                return {"status": "ok", "tools": names}
            return {"status": "error", "error": info.error}
        return {"status": "ok", "tools": info.tools}

    def _set_yaml_enabled(self, name: str, enabled: bool):
        """Update the enabled field in a tool's TOOL.yaml."""
        yaml_path = TOOLS_DIR / name / "TOOL.yaml"
        if not yaml_path.exists():
            return
        try:
            content = yaml_path.read_text(encoding="utf-8")
            old = "enabled: false" if enabled else "enabled: true"
            new = "enabled: true" if enabled else "enabled: false"
            if old in content:
                content = content.replace(old, new, 1)
                yaml_path.write_text(content, encoding="utf-8")
        except Exception as e:
            log.warning("Failed to update TOOL.yaml for %s: %s", name, e)

    def disable_tool(self, name: str) -> dict:
        info = self.tools.get(name)
        if not info:
            return {"status": "error", "error": f"Tool '{name}' not found"}
        self._disabled.add(name)
        self._save_disabled()
        info.enabled = False
        for tool_name in info.tools:
            try:
                self.tool_registry.unregister(tool_name)
            except Exception:
                pass
        info.loaded = False
        info.tools = []
        return {"status": "ok"}

    # ── Dependency installation ───────────────────────────────────

    def _install_deps(self, deps: list[str], tool_name: str):
        """Install pip dependencies for a tool."""
        if not deps:
            return
        try:
            subprocess.run(
                [sys.executable, "-m", "pip", "install", "--quiet"] + deps,
                capture_output=True, text=True, timeout=120,
            )
        except Exception as e:
            log.warning("Failed to install deps for tool %s: %s", tool_name, e)

    # ── Disabled state persistence ────────────────────────────────

    def _load_disabled(self) -> set[str]:
        path = GHOST_HOME / "disabled_tools.json"
        if path.exists():
            try:
                return set(json.loads(path.read_text(encoding="utf-8")))
            except Exception:
                pass
        return set()

    def _save_disabled(self):
        path = GHOST_HOME / "disabled_tools.json"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(sorted(self._disabled)), encoding="utf-8")


# ═════════════════════════════════════════════════════════════════════
#  LLM TOOLS (for Ghost to manage its own tools)
# ═════════════════════════════════════════════════════════════════════

def build_tool_manager_tools(manager: ToolManager) -> list[dict]:
    """Build LLM-callable tools for Ghost to manage its tool ecosystem."""

    def execute_list(**_kw):
        return json.dumps({
            "status": "ok",
            "tools": manager.list_tools(),
        }, default=str)

    def execute_create(name: str = "", description: str = "", code: str = "",
                       hooks: list | None = None, deps: list | None = None,
                       overwrite: bool = False, **_kw):
        if not name or not code:
            return json.dumps({"status": "error", "error": "name and code required"})
        return json.dumps(manager.create_tool(name, description, code, hooks, deps, overwrite=overwrite), default=str)

    def execute_install_github(repo_url: str = "", subdir: str = "", **_kw):
        if not repo_url:
            return json.dumps({"status": "error", "error": "repo_url required"})
        return json.dumps(manager.install_from_github(repo_url, subdir), default=str)

    def execute_uninstall(name: str = "", **_kw):
        if not name:
            return json.dumps({"status": "error", "error": "name required"})
        return json.dumps(manager.uninstall_tool(name), default=str)

    def execute_validate(name: str = "", **_kw):
        if not name:
            return json.dumps({"status": "error", "error": "name required"})
        return json.dumps(manager.validate_tool(name), default=str)

    def execute_enable(name: str = "", **_kw):
        if not name:
            return json.dumps({"status": "error", "error": "name required"})
        return json.dumps(manager.enable_tool(name), default=str)

    def execute_disable(name: str = "", **_kw):
        if not name:
            return json.dumps({"status": "error", "error": "name required"})
        return json.dumps(manager.disable_tool(name), default=str)

    def execute_reload(name: str = "", **_kw):
        if not name:
            return json.dumps({"status": "error", "error": "name required"})
        return json.dumps(manager.reload_tool(name), default=str)

    def execute_reload_all(**_kw):
        manager.discover_all()
        count, names = manager.load_all()
        return json.dumps({
            "status": "ok",
            "loaded_count": count,
            "tool_names": names,
        }, default=str)

    return [
        {
            "name": "tools_list",
            "description": "List all installed ghost tools with their status, registered LLM tools, and errors.",
            "parameters": {"type": "object", "properties": {}},
            "execute": execute_list,
        },
        {
            "name": "tools_create",
            "description": (
                "Create a new ghost tool. Writes TOOL.yaml and tool.py to ghost_tools/<name>/. "
                "The code must define a register(api) function that calls api.register_tool(). "
                "Set overwrite=true to replace an existing tool."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "name": {"type": "string", "description": "Tool name (lowercase, underscores, 2-50 chars)"},
                    "description": {"type": "string", "description": "What the tool does"},
                    "code": {"type": "string", "description": "Full Python source for tool.py"},
                    "hooks": {"type": "array", "items": {"type": "string"}, "description": "Lifecycle hooks to subscribe to"},
                    "deps": {"type": "array", "items": {"type": "string"}, "description": "pip dependencies"},
                    "overwrite": {"type": "boolean", "description": "Replace existing tool if it exists (default false)"},
                },
                "required": ["name", "description", "code"],
            },
            "execute": execute_create,
        },
        {
            "name": "tools_install_github",
            "description": "Clone a GitHub repository and create a ghost tool wrapper around it.",
            "parameters": {
                "type": "object",
                "properties": {
                    "repo_url": {"type": "string", "description": "GitHub repository URL"},
                    "subdir": {"type": "string", "description": "Subdirectory within repo to use"},
                },
                "required": ["repo_url"],
            },
            "execute": execute_install_github,
        },
        {
            "name": "tools_uninstall",
            "description": "Remove an installed ghost tool.",
            "parameters": {
                "type": "object",
                "properties": {
                    "name": {"type": "string", "description": "Tool name to uninstall"},
                },
                "required": ["name"],
            },
            "execute": execute_uninstall,
        },
        {
            "name": "tools_validate",
            "description": "Validate a ghost tool's syntax, imports, and registration.",
            "parameters": {
                "type": "object",
                "properties": {
                    "name": {"type": "string", "description": "Tool name to validate"},
                },
                "required": ["name"],
            },
            "execute": execute_validate,
        },
        {
            "name": "tools_enable",
            "description": "Enable a disabled ghost tool.",
            "parameters": {
                "type": "object",
                "properties": {
                    "name": {"type": "string", "description": "Tool name to enable"},
                },
                "required": ["name"],
            },
            "execute": execute_enable,
        },
        {
            "name": "tools_disable",
            "description": "Disable an enabled ghost tool (unloads it).",
            "parameters": {
                "type": "object",
                "properties": {
                    "name": {"type": "string", "description": "Tool name to disable"},
                },
                "required": ["name"],
            },
            "execute": execute_disable,
        },
        {
            "name": "tools_reload",
            "description": "Hot-reload a single ghost tool at runtime (re-reads tool.py from disk and re-registers). Use after editing a tool or to fix a failed load.",
            "parameters": {
                "type": "object",
                "properties": {
                    "name": {"type": "string", "description": "Tool name to reload"},
                },
                "required": ["name"],
            },
            "execute": execute_reload,
        },
        {
            "name": "tools_reload_all",
            "description": "Re-discover and re-load ALL ghost tools from ghost_tools/. Picks up newly created tools without requiring a restart.",
            "parameters": {
                "type": "object",
                "properties": {},
            },
            "execute": execute_reload_all,
        },
    ]
