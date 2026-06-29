"""
GHOST Canvas — visual output panel for the agent.

The agent can generate HTML/CSS/JS content and display it in a dedicated
panel inside the Chat page.  Content is stored in ~/.ghost/canvas/<session>/
and served by the dashboard.  Supports live-reload, JS injection, and
bidirectional messaging between the rendered content and the agent.
"""

import json
import logging
import os
import time
import uuid
from pathlib import Path
from threading import RLock

log = logging.getLogger("quinely.canvas")

CANVAS_ROOT = Path.home() / ".ghost" / "canvas"
CANVAS_ROOT.mkdir(parents=True, exist_ok=True)

_DEFAULT_SCAFFOLD = """\
<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Ghost Canvas</title>
<style>
  *, *::before, *::after { box-sizing: border-box; margin: 0; padding: 0; }
  body {
    font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, sans-serif;
    background: #0a0a0f;
    color: #e4e4e7;
    min-height: 100vh;
    display: flex;
    align-items: center;
    justify-content: center;
  }
  .canvas-ready {
    text-align: center;
    opacity: 0.4;
  }
  .canvas-ready h1 { font-size: 1.25rem; font-weight: 600; margin-bottom: 0.5rem; }
  .canvas-ready p { font-size: 0.75rem; color: #71717a; }
</style>
</head>
<body>
  <div class="canvas-ready">
    <h1>Canvas Ready</h1>
    <p>Ghost will render content here.</p>
  </div>
</body>
</html>
"""


class CanvasEngine:
    """Manages canvas sessions, files, and state."""

    def __init__(self):
        self._lock = RLock()
        self._session_id: str | None = None
        self._visible: bool = False
        self._target: str | None = None
        self._last_update: float = 0
        self._pending_js: list[str] = []
        self._version: int = 0
        self._inbox: list[dict] = []

    @property
    def session_dir(self) -> Path | None:
        if not self._session_id:
            return None
        return CANVAS_ROOT / self._session_id

    def get_state(self) -> dict:
        with self._lock:
            return {
                "session_id": self._session_id,
                "visible": self._visible,
                "target": self._target,
                "version": self._version,
                "last_update": self._last_update,
                "pending_js": list(self._pending_js),
            }

    def pop_pending_js(self) -> list[str]:
        with self._lock:
            js = list(self._pending_js)
            self._pending_js.clear()
            return js

    def new_session(self) -> str:
        with self._lock:
            self._session_id = uuid.uuid4().hex[:12]
            d = CANVAS_ROOT / self._session_id
            d.mkdir(parents=True, exist_ok=True)
            (d / "index.html").write_text(_DEFAULT_SCAFFOLD, encoding="utf-8")
            self._target = f"/canvas/content/{self._session_id}/index.html"
            self._version += 1
            self._last_update = time.time()
            log.info("Canvas session created: %s", self._session_id)
            return self._session_id

    def _ensure_session(self):
        if not self._session_id:
            self.new_session()

    def write_file(self, file_path: str, content: str) -> str:
        with self._lock:
            self._ensure_session()
            p = (CANVAS_ROOT / self._session_id / file_path).resolve()
            if not p.is_relative_to(CANVAS_ROOT):
                return "Error: path escapes canvas root"
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_text(content, encoding="utf-8")
            self._version += 1
            self._last_update = time.time()
            log.info("Canvas file written: %s/%s (%d bytes)", self._session_id, file_path, len(content))
            return f"Written {file_path} ({len(content)} bytes)"

    def _resolve_target(self, target: str) -> str:
        """Turn a target into the canonical /canvas/content/... path, avoiding double-prefixing."""
        if target.startswith(("http://", "https://")):
            return target
        if target.startswith("/ghost-files/"):
            return target
        prefix = f"/canvas/content/{self._session_id}/"
        if target.startswith(prefix) or target.startswith("/canvas/content/"):
            return target
        ghost_home = str(Path.home() / ".ghost")
        if target.startswith(ghost_home) or target.startswith("~/.ghost/"):
            abs_path = Path(target).expanduser().resolve()
            try:
                rel = abs_path.relative_to(Path(ghost_home).resolve())
                return f"/ghost-files/{rel}"
            except ValueError:
                pass
        return f"{prefix}{target.lstrip('/')}"

    def present(self, target: str | None = None) -> dict:
        with self._lock:
            self._ensure_session()
            if target:
                self._target = self._resolve_target(target)
            elif not self._target:
                self._target = f"/canvas/content/{self._session_id}/index.html"
            self._visible = True
            self._version += 1
            self._last_update = time.time()
            return {"visible": True, "target": self._target, "session_id": self._session_id}

    def hide(self) -> dict:
        with self._lock:
            self._visible = False
            self._version += 1
            self._last_update = time.time()
            return {"visible": False}

    def navigate(self, target: str) -> dict:
        with self._lock:
            self._ensure_session()
            self._target = self._resolve_target(target)
            self._version += 1
            self._last_update = time.time()
            return {"target": self._target}

    def eval_js(self, code: str) -> str:
        with self._lock:
            self._pending_js.append(code)
            self._version += 1
            return "JS queued for execution"

    def receive_message(self, action: str, data: dict | None = None) -> str:
        """Receive a message from canvas content (A2UI)."""
        with self._lock:
            msg = {"action": action, "data": data or {}, "time": time.time()}
            self._inbox.append(msg)
            self._version += 1
            log.info("Canvas A2UI message received: %s", action)
            return "Message received"

    def pop_inbox(self) -> list[dict]:
        """Pop all pending A2UI messages from canvas content."""
        with self._lock:
            msgs = list(self._inbox)
            self._inbox.clear()
            return msgs

    def read_file(self, file_path: str) -> str:
        """Read a file from the current canvas session."""
        with self._lock:
            if not self._session_id:
                return ""
            p = (CANVAS_ROOT / self._session_id / file_path).resolve()
            if not p.is_relative_to(CANVAS_ROOT) or not p.is_file():
                return ""
            return p.read_text(encoding="utf-8")

    def screenshot(self, dashboard_port: int = 3333) -> str:
        """Capture a pixel-level screenshot of the current canvas via PinchTab."""
        with self._lock:
            if not self._session_id or not self._target:
                return ""
        target = self._target
        if target.startswith("/"):
            target = f"http://127.0.0.1:{dashboard_port}{target}"
        out_dir = Path.home() / ".ghost" / "screenshots"
        out_dir.mkdir(parents=True, exist_ok=True)
        out_path = out_dir / f"canvas_{self._session_id}_{int(time.time())}.png"
        try:
            import requests
            base = os.environ.get("PINCHTAB_URL", "http://localhost:9867")
            headers = {"Content-Type": "application/json"}
            r = requests.post(f"{base}/instances/start",
                              json={"mode": "headless"}, headers=headers, timeout=10)
            r.raise_for_status()
            inst_id = r.json()["id"]
            for _ in range(8):
                time.sleep(1)
                try:
                    ri = requests.get(f"{base}/instances", headers=headers, timeout=5)
                    for inst in ri.json():
                        if isinstance(inst, dict) and inst.get("id") == inst_id and inst.get("status") == "running":
                            break
                    else:
                        continue
                    break
                except Exception:
                    continue
            r = requests.post(f"{base}/instances/{inst_id}/tabs/open",
                              json={"url": target}, headers=headers, timeout=15)
            r.raise_for_status()
            tab_id = r.json()["tabId"]
            time.sleep(2)
            r = requests.get(f"{base}/tabs/{tab_id}/screenshot", headers=headers, timeout=10)
            if r.headers.get("content-type", "").startswith("image/"):
                out_path.write_bytes(r.content)
            else:
                import base64
                png_data = base64.b64decode(r.json().get("data", ""))
                out_path.write_bytes(png_data)
            requests.post(f"{base}/instances/{inst_id}/stop", headers=headers, timeout=5)
            log.info("Canvas screenshot saved: %s", out_path)
            return str(out_path)
        except Exception as e:
            log.warning("Canvas screenshot failed: %s", e)
            return ""

    def list_files(self) -> list[str]:
        with self._lock:
            if not self._session_id:
                return []
            d = CANVAS_ROOT / self._session_id
            if not d.exists():
                return []
            return [str(f.relative_to(d)) for f in d.rglob("*") if f.is_file()]


_engine: CanvasEngine | None = None


def get_canvas_engine() -> CanvasEngine:
    global _engine
    if _engine is None:
        _engine = CanvasEngine()
    return _engine


def build_canvas_tools(cfg: dict | None = None) -> list[dict]:
    """Build canvas tools for the agent."""
    engine = get_canvas_engine()

    def execute_canvas(action: str, file_path: str = "", content: str = "",
                       target: str = "", js_code: str = "", **_kw) -> str:
        try:
            if action == "write":
                if not file_path or not content:
                    return "Error: file_path and content required for write"
                result = engine.write_file(file_path, content)
                engine.present(file_path)
                return result
            elif action == "present":
                r = engine.present(target or None)
                return json.dumps(r)
            elif action == "hide":
                r = engine.hide()
                return json.dumps(r)
            elif action == "navigate":
                if not target:
                    return "Error: target required for navigate"
                r = engine.navigate(target)
                return json.dumps(r)
            elif action == "eval_js":
                if not js_code:
                    return "Error: js_code required for eval_js"
                return engine.eval_js(js_code)
            elif action == "list_files":
                files = engine.list_files()
                return json.dumps(files) if files else "No files in current session"
            elif action == "new_session":
                sid = engine.new_session()
                return f"New canvas session: {sid}"
            elif action == "snapshot":
                img_path = engine.screenshot()
                if img_path:
                    return f"Screenshot saved: {img_path}"
                html = engine.read_file(target or "index.html")
                if not html:
                    return "No content to snapshot"
                if len(html) > 8000:
                    html = html[:8000] + "\n<!-- ... truncated ... -->"
                return f"Screenshot unavailable, returning HTML source:\n{html}"
            elif action == "read_inbox":
                msgs = engine.pop_inbox()
                if not msgs:
                    return "No messages from canvas"
                return json.dumps(msgs)
            else:
                return f"Unknown action: {action}. Use: write, present, hide, navigate, eval_js, list_files, new_session, snapshot, read_inbox"
        except Exception as e:
            return f"Canvas error: {e}"

    return [{
        "name": "canvas",
        "description": (
            "Display rich visual content (HTML/CSS/JS) in a Canvas panel next to the chat. "
            "Use this to show interactive demos, visualizations, dashboards, mini-apps, "
            "formatted reports, or any web content. The canvas panel appears beside the "
            "chat and auto-reloads when you update files.\n\n"
            "Actions:\n"
            "- write: Create/update a file in the canvas (auto-presents it)\n"
            "- present: Show the canvas panel (optionally with a target file/URL)\n"
            "- hide: Hide the canvas panel\n"
            "- navigate: Navigate to a different file or URL\n"
            "- eval_js: Execute JavaScript in the canvas\n"
            "- list_files: List files in the current canvas session\n"
            "- new_session: Start a fresh canvas session\n"
            "- snapshot: Capture a pixel-level screenshot of the canvas (falls back to HTML source)\n"
            "- read_inbox: Read messages sent from canvas content via ghostCanvas.send()"
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "action": {
                    "type": "string",
                    "enum": ["write", "present", "hide", "navigate", "eval_js", "list_files", "new_session", "snapshot", "read_inbox"],
                    "description": "The canvas action to perform",
                },
                "file_path": {
                    "type": "string",
                    "description": "File path within the canvas session (for write). e.g. 'index.html', 'style.css', 'app.js'",
                },
                "content": {
                    "type": "string",
                    "description": "File content to write (for write action)",
                },
                "target": {
                    "type": "string",
                    "description": "Target file path or URL (for present/navigate). e.g. 'index.html' or 'https://example.com'",
                },
                "js_code": {
                    "type": "string",
                    "description": "JavaScript code to execute in the canvas (for eval_js)",
                },
            },
            "required": ["action"],
        },
        "execute": execute_canvas,
    }]
