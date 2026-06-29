"""
Ghost Browser-Use Integration — AI-Native Browser Automation

Uses browser-use (browser-use.com) for LLM-powered browser automation with:
- Intelligent element detection and interaction
- Multi-step task planning
- Self-healing capabilities when page elements change
- Vision-based automation support

Complements the existing Playwright-based ghost_browser.py with AI-native control.

Requirements:
    pip install browser-use langchain-openai

Note: browser-use requires Playwright and an LLM API key to function.
"""

import logging
import json
import asyncio
import threading
import atexit
import sqlite3
from typing import Optional, Dict, Any, List
from pathlib import Path
from dataclasses import dataclass, field
from datetime import datetime

import platform
if platform.system() == "Darwin":
    try:
        import AppKit  # pyobjc-framework-Cocoa
        info = AppKit.NSBundle.mainBundle().infoDictionary()
        info["LSUIElement"] = "1"
    except Exception:
        pass

try:
    from browser_use import Agent, Browser
    BROWSER_USE_AVAILABLE = True
except ImportError:
    BROWSER_USE_AVAILABLE = False
    Agent = None
    Browser = None

log = logging.getLogger(__name__)


# ── Ghost LLM integration ─────────────────────────────────────────────

class _GhostChatModel:
    """browser-use BaseChatModel implementation that routes through Ghost's provider system.

    Works with ALL Ghost providers including openai-codex, anthropic,
    openrouter, ollama, etc. — Ghost's adapt_request/adapt_response handles
    every format natively.
    """

    model: str = ""
    _verified_api_keys: bool = True

    def __init__(self, engine, provider_name: str, model_id: str):
        self._engine = engine
        self.model = model_id
        self._provider_name = provider_name

    @property
    def provider(self) -> str:
        return self._provider_name

    @property
    def name(self) -> str:
        return self.model

    @property
    def model_name(self) -> str:
        return self.model

    @staticmethod
    def _serialize_messages(messages) -> list:
        """Convert browser-use message objects to OpenAI-format dicts."""
        oai = []
        for m in messages:
            role = getattr(m, "role", "user")
            content = getattr(m, "content", "")
            if isinstance(content, list):
                parts = []
                for part in content:
                    ptype = getattr(part, "type", "text")
                    if ptype == "text":
                        parts.append({"type": "text", "text": getattr(part, "text", "")})
                    elif ptype == "image_url":
                        img = getattr(part, "image_url", None)
                        if img:
                            parts.append({
                                "type": "image_url",
                                "image_url": {"url": getattr(img, "url", ""), "detail": getattr(img, "detail", "auto")},
                            })
                    elif ptype == "refusal":
                        parts.append({"type": "text", "text": f"[Refusal] {getattr(part, 'refusal', '')}"})
                content = parts

            entry = {"role": role, "content": content}

            tool_calls = getattr(m, "tool_calls", None)
            if tool_calls:
                entry["tool_calls"] = [
                    {
                        "id": tc.id,
                        "type": "function",
                        "function": {"name": tc.function.name, "arguments": tc.function.arguments},
                    }
                    for tc in tool_calls
                ]
            oai.append(entry)
        return oai

    async def ainvoke(self, messages, output_format=None, **kwargs):
        import asyncio
        loop = asyncio.get_event_loop()
        return await loop.run_in_executor(
            None, lambda: self._invoke_sync(messages, output_format)
        )

    def _invoke_sync(self, messages, output_format=None):
        from browser_use.llm.views import ChatInvokeCompletion, ChatInvokeUsage

        oai_messages = self._serialize_messages(messages)
        payload = {"messages": oai_messages}

        if output_format is not None:
            try:
                schema = output_format.model_json_schema()
                payload["response_format"] = {
                    "type": "json_schema",
                    "json_schema": {"name": "agent_output", "strict": True, "schema": schema},
                }
            except Exception:
                pass

        data, err = self._engine._call_llm(payload, timeout=120)

        if err:
            from browser_use.llm.exceptions import ModelProviderError
            raise ModelProviderError(message=str(err), model=self.name)

        usage_info = None
        if isinstance(data, dict):
            raw_usage = data.get("usage", {})
            if isinstance(raw_usage, dict) and raw_usage.get("total_tokens"):
                usage_info = ChatInvokeUsage(
                    prompt_tokens=raw_usage.get("prompt_tokens", 0),
                    prompt_cached_tokens=raw_usage.get("cached_tokens"),
                    prompt_cache_creation_tokens=None,
                    prompt_image_tokens=None,
                    completion_tokens=raw_usage.get("completion_tokens", 0),
                    total_tokens=raw_usage.get("total_tokens", 0),
                )

        choices = data.get("choices", []) if isinstance(data, dict) else []
        text = ""
        if choices:
            msg = choices[0].get("message", {})
            text = msg.get("content", "") or ""

        if output_format is not None and text:
            try:
                parsed = output_format.model_validate_json(text)
                return ChatInvokeCompletion(completion=parsed, usage=usage_info)
            except Exception:
                pass

        return ChatInvokeCompletion(completion=text, usage=usage_info)


def _resolve_ghost_llm():
    """Build a LangChain-compatible LLM using Ghost's own provider system.

    Returns (llm_object, description_string) or (None, error_string).
    Routes through ToolLoopEngine which handles ALL providers: openai-codex,
    openrouter, anthropic, ollama, google, etc.
    """
    try:
        from ghost import load_config
        from ghost_auth_profiles import AuthProfileStore
        from ghost_providers import get_provider

        cfg = load_config()
        primary_pid = cfg.get("primary_provider", "openrouter")
        provider_models = cfg.get("provider_models", {})
        model = provider_models.get(primary_pid, cfg.get("model", ""))

        api_key = cfg.get("api_key", "")
        fallback_models = cfg.get("fallback_models", [])

        auth = AuthProfileStore()

        provider = get_provider(primary_pid)
        provider_name = provider.name if provider else primary_pid
        display_model = model or (provider.default_model if provider else "?")
        desc = f"{provider_name}: {display_model}"

        from ghost_loop import ToolLoopEngine
        engine = ToolLoopEngine(
            api_key=api_key,
            model=model,
            fallback_models=fallback_models,
            auth_store=auth,
            provider_chain=_build_browser_provider_chain(cfg, auth),
        )

        llm = _GhostChatModel(engine, provider_name=provider_name, model_id=display_model)
        return llm, desc

    except Exception as exc:
        return None, f"Ghost LLM resolution failed: {exc}"


def _build_browser_provider_chain(cfg, auth):
    """Build a minimal provider chain for browser-use from Ghost's config."""
    from ghost_providers import PROVIDERS, get_provider

    primary_pid = cfg.get("primary_provider", "openrouter")
    provider_models = cfg.get("provider_models", {})
    model = cfg.get("model", "")
    fallback_models = cfg.get("fallback_models", [])
    chain = []
    seen = set()

    def _add(pid, mdl):
        key = (pid, mdl)
        if key not in seen:
            seen.add(key)
            chain.append(key)

    order = [primary_pid] + [pid for pid in PROVIDERS if pid != primary_pid]
    for pid in order:
        prov = get_provider(pid)
        if not prov:
            continue
        if prov.auth_type != "none" and not auth.is_provider_configured(pid):
            continue
        pm = provider_models.get(pid, "")
        if pid == primary_pid:
            _add(pid, pm or model or prov.default_model)
            for fm in fallback_models:
                _add(pid, fm)
        else:
            _add(pid, pm or prov.default_model)

    # Honor the fallback toggle: primary-only when disabled.
    if not cfg.get("fallback_enabled", True) and chain:
        return chain[:1]

    return chain


# ── shared asyncio event loop (one per process) ────────────────────────
_loop: Optional[asyncio.AbstractEventLoop] = None
_loop_thread: Optional[threading.Thread] = None
_loop_lock = threading.Lock()


def _get_shared_loop() -> asyncio.AbstractEventLoop:
    """Return (and lazily start) a long-lived background event loop.

    All browser-use async work is dispatched here so that Playwright
    connections survive across multiple calls.
    """
    global _loop, _loop_thread
    with _loop_lock:
        if _loop is not None and _loop.is_running():
            return _loop
        _loop = asyncio.new_event_loop()
        _loop_thread = threading.Thread(
            target=_loop.run_forever, daemon=True, name="browser-use-loop"
        )
        _loop_thread.start()
        atexit.register(_shutdown_loop)
        return _loop


def _shutdown_loop() -> None:
    global _loop, _loop_thread
    if _loop and _loop.is_running():
        _loop.call_soon_threadsafe(_loop.stop)
    if _loop_thread:
        _loop_thread.join(timeout=5)
    _loop = None
    _loop_thread = None


def _run_async(coro) -> Any:
    """Submit *coro* to the shared loop and block until it completes."""
    loop = _get_shared_loop()
    future = asyncio.run_coroutine_threadsafe(coro, loop)
    return future.result()


# ── SQLite persistence for session metadata & history ──────────────────
_DB_DIR = Path.home() / ".ghost" / "browser_use"
_DB_PATH = _DB_DIR / "sessions.db"
_db_lock = threading.Lock()


def _get_db() -> sqlite3.Connection:
    _DB_DIR.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(_DB_PATH), check_same_thread=False)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("""
        CREATE TABLE IF NOT EXISTS sessions (
            id          TEXT PRIMARY KEY,
            url         TEXT NOT NULL,
            task        TEXT,
            status      TEXT NOT NULL DEFAULT 'idle',
            history     TEXT NOT NULL DEFAULT '[]',
            created_at  TEXT NOT NULL,
            error_message TEXT
        )
    """)
    conn.commit()
    return conn


_db: Optional[sqlite3.Connection] = None


def _conn() -> sqlite3.Connection:
    global _db
    with _db_lock:
        if _db is None:
            _db = _get_db()
        return _db


# ── live runtime objects (browser/agent — NOT serializable) ────────────
_runtime_lock = threading.Lock()
_runtime: Dict[str, Dict[str, Any]] = {}
_session_counter = 0


@dataclass
class BrowserUseSession:
    """Represents a browser-use session.

    Serializable fields are persisted to SQLite.
    Live objects (browser, agent) are held separately in _runtime.
    """
    id: str
    url: str
    task: Optional[str] = None
    status: str = "idle"
    history: List[Dict] = field(default_factory=list)
    created_at: str = field(default_factory=lambda: datetime.now().isoformat())
    error_message: Optional[str] = None


def _get_next_session_id() -> str:
    global _session_counter
    with _runtime_lock:
        _session_counter += 1
        return f"bu_{_session_counter:04d}"


# ── session CRUD (SQLite-backed) ──────────────────────────────────────

def get_session(session_id: str) -> Optional[BrowserUseSession]:
    """Retrieve a session by ID from the database."""
    row = _conn().execute(
        "SELECT id, url, task, status, history, created_at, error_message "
        "FROM sessions WHERE id = ?", (session_id,)
    ).fetchone()
    if not row:
        return None
    return BrowserUseSession(
        id=row[0], url=row[1], task=row[2], status=row[3],
        history=json.loads(row[4]), created_at=row[5], error_message=row[6],
    )


def save_session(session: BrowserUseSession) -> None:
    """Upsert session metadata into SQLite."""
    _conn().execute(
        "INSERT INTO sessions (id, url, task, status, history, created_at, error_message) "
        "VALUES (?, ?, ?, ?, ?, ?, ?) "
        "ON CONFLICT(id) DO UPDATE SET "
        "url=excluded.url, task=excluded.task, status=excluded.status, "
        "history=excluded.history, error_message=excluded.error_message",
        (session.id, session.url, session.task, session.status,
         json.dumps(session.history), session.created_at, session.error_message),
    )
    _conn().commit()


def _set_runtime(session_id: str, key: str, value: Any) -> None:
    with _runtime_lock:
        _runtime.setdefault(session_id, {})[key] = value


def _get_runtime(session_id: str, key: str) -> Any:
    with _runtime_lock:
        return _runtime.get(session_id, {}).get(key)


def list_sessions() -> List[Dict[str, Any]]:
    """List all sessions (serializable fields only)."""
    rows = _conn().execute(
        "SELECT id, url, task, status, history, created_at, error_message "
        "FROM sessions ORDER BY created_at DESC"
    ).fetchall()
    result = []
    for r in rows:
        history = json.loads(r[4])
        result.append({
            "id": r[0], "url": r[1], "task": r[2],
            "status": r[3], "created_at": r[5],
            "history_count": len(history),
            "error_message": r[6],
        })
    return result


def delete_session(session_id: str) -> bool:
    """Delete a session, close its browser if running, and remove from DB."""
    browser = _get_runtime(session_id, "browser")
    if browser:
        try:
            _run_async(browser.stop())
        except Exception as exc:
            log.warning("Error closing browser for session %s: %s", session_id, exc)
    with _runtime_lock:
        _runtime.pop(session_id, None)
    cur = _conn().execute("DELETE FROM sessions WHERE id = ?", (session_id,))
    _conn().commit()
    return cur.rowcount > 0


# ── async task runner ─────────────────────────────────────────────────

async def _run_browser_task(
    session_id: str,
    task: str,
    start_url: str,
    api_key: Optional[str] = None,
    model: Optional[str] = None,
    headless: bool = True,
) -> Dict[str, Any]:
    """Run a browser-use task asynchronously on the shared event loop."""
    if not BROWSER_USE_AVAILABLE:
        return {"success": False, "error": "browser-use package not installed"}

    session = get_session(session_id)
    if not session:
        return {"success": False, "error": f"Session {session_id} not found"}

    session.status = "running"
    session.task = task
    save_session(session)

    try:
        browser = Browser(headless=headless)
        _set_runtime(session_id, "browser", browser)

        ghost_llm, desc = _resolve_ghost_llm()
        if not ghost_llm:
            return {"success": False, "error": f"No LLM available: {desc}"}
        llm = ghost_llm
        log.info("Browser-use using Ghost LLM: %s", desc)

        agent = Agent(task=task, llm=llm, browser=browser)
        _set_runtime(session_id, "agent", agent)

        result = await agent.run()

        history_entry = {
            "timestamp": datetime.now().isoformat(),
            "task": task,
            "start_url": start_url,
            "result": result.model_dump() if hasattr(result, "model_dump") else str(result),
            "success": True,
        }
        session = get_session(session_id) or session
        session.history.append(history_entry)
        session.status = "completed"
        save_session(session)

        return {"success": True, "result": history_entry["result"], "session_id": session_id}

    except Exception as exc:
        log.exception("Browser-use task failed for session %s", session_id)
        session = get_session(session_id) or session
        session.status = "error"
        session.error_message = str(exc)
        session.history.append({
            "timestamp": datetime.now().isoformat(),
            "task": task,
            "start_url": start_url,
            "error": str(exc),
            "success": False,
        })
        save_session(session)
        return {"success": False, "error": str(exc), "session_id": session_id}

    finally:
        browser = _get_runtime(session_id, "browser")
        if browser:
            try:
                await browser.stop()
            except Exception:
                pass
        with _runtime_lock:
            _runtime.pop(session_id, None)


# ── public tool functions ─────────────────────────────────────────────

def browser_use_create_session(url: str = "https://google.com", **kwargs) -> str:
    """Create a new browser-use automation session."""
    session_id = _get_next_session_id()
    session = BrowserUseSession(id=session_id, url=url, status="idle")
    save_session(session)
    log.info("Created browser-use session %s starting at %s", session_id, url)
    return session_id


def browser_use_run_task(
    session_id: str,
    task: str,
    api_key: Optional[str] = None,
    model: Optional[str] = None,
    headless: bool = True,
    **kwargs
) -> Dict[str, Any]:
    """Run an AI-powered browser task using browser-use."""
    if not BROWSER_USE_AVAILABLE:
        return {"success": False, "error": "browser-use not installed. Run: pip install browser-use"}

    session = get_session(session_id)
    if not session:
        return {"success": False, "error": f"Session {session_id} not found"}

    start_url = kwargs.get("url") or session.url

    try:
        return _run_async(_run_browser_task(
            session_id=session_id,
            task=task,
            start_url=start_url,
            api_key=api_key,
            model=model,
            headless=headless,
        ))
    except Exception as exc:
        log.exception("Failed to run browser-use task")
        return {"success": False, "error": str(exc)}


def browser_use_get_status(session_id: str, **kwargs) -> Dict[str, Any]:
    """Get the status and history of a browser-use session."""
    session = get_session(session_id)
    if not session:
        return {"success": False, "error": f"Session {session_id} not found"}

    return {
        "success": True,
        "session": {
            "id": session.id,
            "url": session.url,
            "task": session.task,
            "status": session.status,
            "created_at": session.created_at,
            "history": session.history,
            "error_message": session.error_message,
        }
    }


def browser_use_list_sessions(**kwargs) -> Dict[str, Any]:
    """List all active browser-use sessions."""
    sessions = list_sessions()
    return {
        "success": True,
        "sessions": sessions,
        "count": len(sessions),
        "browser_use_available": BROWSER_USE_AVAILABLE,
    }


def browser_use_close_session(session_id: str, **kwargs) -> Dict[str, Any]:
    """Close a browser-use session and release resources."""
    if delete_session(session_id):
        return {"success": True, "message": f"Session {session_id} closed"}
    return {"success": False, "error": f"Session {session_id} not found"}


def browser_use_navigate(session_id: str, url: str, **kwargs) -> Dict[str, Any]:
    """Navigate to a URL in an existing browser-use session."""
    if not BROWSER_USE_AVAILABLE:
        return {"success": False, "error": "browser-use not installed"}

    session = get_session(session_id)
    if not session:
        return {"success": False, "error": f"Session {session_id} not found"}

    try:
        browser = _get_runtime(session_id, "browser")
        if browser:
            async def _nav():
                page = await browser.get_current_page()
                await page.goto(url)
            _run_async(_nav())
            session.url = url
            save_session(session)
            return {"success": True, "message": f"Navigated to {url}"}
        else:
            session.url = url
            save_session(session)
            return {"success": True, "message": f"URL updated to {url} (browser not started)"}
    except Exception as exc:
        log.exception("Navigation failed")
        return {"success": False, "error": str(exc)}


def browser_use_get_html(session_id: str, **kwargs) -> Dict[str, Any]:
    """Get the current page HTML from a browser-use session."""
    if not BROWSER_USE_AVAILABLE:
        return {"success": False, "error": "browser-use not installed"}

    session = get_session(session_id)
    if not session:
        return {"success": False, "error": f"Session {session_id} not found"}

    try:
        browser = _get_runtime(session_id, "browser")
        if not browser:
            return {"success": False, "error": "Browser not started for this session"}

        async def _get_html():
            page = await browser.get_current_page()
            return await page.content()
        html = _run_async(_get_html())

        max_len = kwargs.get("max_length", 50000)
        if len(html) > max_len:
            html = html[:max_len] + f"\n... [truncated, total: {len(html)} chars]"

        return {"success": True, "html": html, "url": session.url}
    except Exception as exc:
        log.exception("Failed to get HTML")
        return {"success": False, "error": str(exc)}


def browser_use_screenshot(session_id: str, output_path: Optional[str] = None, **kwargs) -> Dict[str, Any]:
    """Take a screenshot from a browser-use session."""
    if not BROWSER_USE_AVAILABLE:
        return {"success": False, "error": "browser-use not installed"}

    session = get_session(session_id)
    if not session:
        return {"success": False, "error": f"Session {session_id} not found"}

    try:
        browser = _get_runtime(session_id, "browser")
        if not browser:
            return {"success": False, "error": "Browser not started for this session"}

        if not output_path:
            ts = datetime.now().strftime("%Y%m%d_%H%M%S")
            output_path = str(Path.home() / ".ghost" / "screenshots" / f"browser_use_{session_id}_{ts}.png")

        path = Path(output_path)
        path.parent.mkdir(parents=True, exist_ok=True)

        async def _screenshot():
            page = await browser.get_current_page()
            await page.screenshot(path=str(path), full_page=kwargs.get("full_page", True))
        _run_async(_screenshot())

        return {"success": True, "path": str(path), "url": session.url}
    except Exception as exc:
        log.exception("Screenshot failed")
        return {"success": False, "error": str(exc)}


def build_browser_use_tools(cfg=None):
    """Build and return browser-use tool definitions."""
    return [
        {
            "name": "browser_use_create_session",
            "description": "Create a new AI-powered browser automation session using browser-use. Returns a session ID for use with other browser_use tools.",
            "parameters": {
                "type": "object",
                "properties": {
                    "url": {
                        "type": "string",
                        "description": "Starting URL for the session",
                        "default": "https://google.com"
                    }
                },
                "required": []
            },
            "execute": browser_use_create_session
        },
        {
            "name": "browser_use_run_task",
            "description": "Run an AI-powered browser task using browser-use. Uses Ghost's configured LLM by default. The LLM will control the browser to complete the described task. Examples: 'Find the price of iPhone 15 on Amazon', 'Fill out the contact form and submit it', 'Extract all article titles from the news page'.",
            "parameters": {
                "type": "object",
                "properties": {
                    "session_id": {
                        "type": "string",
                        "description": "Session ID from browser_use_create_session"
                    },
                    "task": {
                        "type": "string",
                        "description": "Natural language description of the task to perform"
                    },
                    "api_key": {
                        "type": "string",
                        "description": "Optional override API key (defaults to Ghost's configured provider)",
                        "default": None
                    },
                    "model": {
                        "type": "string",
                        "description": "Optional override model (defaults to Ghost's configured model)",
                        "default": None
                    },
                    "headless": {
                        "type": "boolean",
                        "description": "Run browser in headless mode (default: True)",
                        "default": True
                    }
                },
                "required": ["session_id", "task"]
            },
            "execute": browser_use_run_task
        },
        {
            "name": "browser_use_get_status",
            "description": "Get the status and history of a browser-use session.",
            "parameters": {
                "type": "object",
                "properties": {
                    "session_id": {
                        "type": "string",
                        "description": "Session ID to check"
                    }
                },
                "required": ["session_id"]
            },
            "execute": browser_use_get_status
        },
        {
            "name": "browser_use_list_sessions",
            "description": "List all active browser-use sessions with their status.",
            "parameters": {
                "type": "object",
                "properties": {},
                "required": []
            },
            "execute": browser_use_list_sessions
        },
        {
            "name": "browser_use_close_session",
            "description": "Close a browser-use session and release resources.",
            "parameters": {
                "type": "object",
                "properties": {
                    "session_id": {
                        "type": "string",
                        "description": "Session ID to close"
                    }
                },
                "required": ["session_id"]
            },
            "execute": browser_use_close_session
        },
        {
            "name": "browser_use_navigate",
            "description": "Navigate to a URL in an existing browser-use session.",
            "parameters": {
                "type": "object",
                "properties": {
                    "session_id": {
                        "type": "string",
                        "description": "Session ID"
                    },
                    "url": {
                        "type": "string",
                        "description": "URL to navigate to"
                    }
                },
                "required": ["session_id", "url"]
            },
            "execute": browser_use_navigate
        },
        {
            "name": "browser_use_get_html",
            "description": "Get the current page HTML content from a browser-use session.",
            "parameters": {
                "type": "object",
                "properties": {
                    "session_id": {
                        "type": "string",
                        "description": "Session ID"
                    },
                    "max_length": {
                        "type": "integer",
                        "description": "Maximum HTML length to return (default: 50000)",
                        "default": 50000
                    }
                },
                "required": ["session_id"]
            },
            "execute": browser_use_get_html
        },
        {
            "name": "browser_use_screenshot",
            "description": "Take a screenshot from a browser-use session and save it to a file.",
            "parameters": {
                "type": "object",
                "properties": {
                    "session_id": {
                        "type": "string",
                        "description": "Session ID"
                    },
                    "output_path": {
                        "type": "string",
                        "description": "Optional path to save screenshot (auto-generated if not provided)",
                        "default": None
                    },
                    "full_page": {
                        "type": "boolean",
                        "description": "Capture full page or just viewport (default: True)",
                        "default": True
                    }
                },
                "required": ["session_id"]
            },
            "execute": browser_use_screenshot
        },
    ]
