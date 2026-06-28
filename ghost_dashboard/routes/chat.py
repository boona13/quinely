"""Chat API — direct messaging between the user and Quinely daemon."""

import base64
import json
import logging
import os
import threading
import time
import uuid
from pathlib import Path
from datetime import datetime
from flask import Blueprint, jsonify, request, Response, send_from_directory, abort

from ghost_dashboard.rate_limiter import rate_limit

# Import reasoning module for /think directive support
try:
    from ghost_reasoning import detect_think_directive, get_reasoning_state
    _reasoning_available = True
except ImportError:
    _reasoning_available = False

log = logging.getLogger(__name__)

import sys
sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent))
from ghost_projects import ProjectRegistry, format_project_for_prompt
from ghost_artifacts import (
    set_current_message_id, get_artifacts_dir, scan_tool_result_for_artifacts,
)

# File upload configuration
AUDIO_EXTENSIONS = {'.wav', '.mp3', '.m4a', '.flac', '.ogg', '.aac'}
IMAGE_EXTENSIONS = {'.jpg', '.jpeg', '.png', '.gif', '.webp', '.bmp'}
VIDEO_EXTENSIONS = {'.mp4', '.mov', '.avi', '.mkv', '.webm', '.flv', '.wmv', '.m4v'}
DOCUMENT_EXTENSIONS = {'.pdf', '.txt', '.md', '.csv', '.json', '.xml', '.html', '.log'}
UPLOAD_DIR = Path.home() / ".ghost" / "uploads"
UPLOAD_DIR.mkdir(parents=True, exist_ok=True)

_EXT_TO_MIME = {
    '.jpg': 'image/jpeg', '.jpeg': 'image/jpeg',
    '.png': 'image/png', '.gif': 'image/gif',
    '.webp': 'image/webp', '.bmp': 'image/bmp',
}


def _encode_image_for_llm(file_path: str) -> dict | None:
    """Read an image file and return {"data": base64_str, "mime": mime_type} or None on failure."""
    try:
        p = Path(file_path)
        if not p.exists() or p.stat().st_size > 20 * 1024 * 1024:
            return None
        mime = _EXT_TO_MIME.get(p.suffix.lower(), 'image/png')
        data = base64.b64encode(p.read_bytes()).decode('ascii')
        return {"data": data, "mime": mime}
    except Exception:
        log.warning(f"Failed to load image file: {file_path}", exc_info=True)
        return None

bp = Blueprint("chat", __name__)

PROJECT_DIR = Path(__file__).resolve().parent.parent.parent
GHOST_HOME = Path.home() / ".ghost"
AUDIO_DIR = GHOST_HOME / "audio"
_SAFE_AUDIO_EXT = {".mp3", ".wav", ".ogg", ".m4a"}


@bp.route("/api/audio/<filename>")
def serve_audio(filename):
    """Serve generated audio files from ~/.ghost/audio/ (TTS, etc.)."""
    if ".." in filename or "/" in filename or "\\" in filename:
        abort(404)
    ext = Path(filename).suffix.lower()
    if ext not in _SAFE_AUDIO_EXT:
        abort(403)
    if not AUDIO_DIR.exists():
        abort(404)
    return send_from_directory(str(AUDIO_DIR), filename)
RESTART_STATE_FILE = GHOST_HOME / "chat_restart_state.json"
SESSION_BOUNDARY_FILE = GHOST_HOME / "chat_session_boundary.json"

_chat_sessions = {}
_chat_lock = threading.Lock()
_restart_recovery = None

CHAT_ERROR_REPORT_FILE = GHOST_HOME / "chat_error_report.json"


def _get_daemon():
    from ghost_dashboard import get_daemon
    return get_daemon()


import re as _re

_URL_RE = _re.compile(r'https?://[^\s<>"\']+')


def _message_contains_url(message: str) -> bool:
    """Check if a user message contains an HTTP/HTTPS URL."""
    return bool(_URL_RE.search(message))


def _save_restart_state(session, deploy_result=""):
    """Persist chat state to disk before a deploy restart kills the process."""
    try:
        tool_names = [s.get("tool", "") for s in session.steps if s.get("tool")]
        summary_parts = []
        if "evolve_plan" in tool_names:
            summary_parts.append("planned code changes")
        apply_count = tool_names.count("evolve_apply")
        if apply_count:
            summary_parts.append(f"modified {apply_count} file(s)")
        if "evolve_test" in tool_names:
            summary_parts.append("ran tests")
        if "evolve_deploy" in tool_names:
            summary_parts.append("deployed successfully")

        if summary_parts:
            result_hint = (
                f"Task completed. Quinely {', '.join(summary_parts)}, "
                "and restarted with the new code. The system is now running with your changes."
            )
        else:
            result_hint = deploy_result or (
                "Quinely deployed code changes and restarted successfully. "
                "The system is now running with the updated code."
            )

        state = {
            "message_id": session.id,
            "user_message": session.user_message,
            "steps": session.steps,
            "tools_used": session.tools_used,
            "result_hint": result_hint,
            "timestamp": time.time(),
        }
        GHOST_HOME.mkdir(parents=True, exist_ok=True)
        RESTART_STATE_FILE.write_text(json.dumps(state, indent=2), encoding="utf-8")
    except Exception:
        log.warning("Failed to save restart state", exc_info=True)


def _load_restart_recovery():
    """On startup, check for a restart state file left by the previous process."""
    global _restart_recovery
    if not RESTART_STATE_FILE.exists():
        return
    try:
        state = json.loads(RESTART_STATE_FILE.read_text(encoding="utf-8"))
        _restart_recovery = state

        FEED_FILE = GHOST_HOME / "feed.json"
        if FEED_FILE.exists():
            feed = json.loads(FEED_FILE.read_text(encoding="utf-8"))
        else:
            feed = []

        msg_id = state.get("message_id", "")
        updated = False
        for item in feed:
            if item.get("message_id") == msg_id:
                item["result"] = state.get("result_hint", "Quinely restarted after deploying changes.")
                item["status"] = "complete"
                item["time"] = datetime.now().isoformat()
                tools = state.get("tools_used", [])
                if tools:
                    item["tools_used"] = tools
                updated = True
                break
        if not updated:
            feed.insert(0, {
                "time": datetime.now().isoformat(),
                "type": "ask",
                "message_id": msg_id,
                "source": state.get("user_message", "")[:2000],
                "result": state.get("result_hint", "Quinely restarted after deploying changes."),
                "status": "complete",
            })
        FEED_FILE.write_text(json.dumps(feed, indent=2), encoding="utf-8")
    except Exception:
        log.warning("Failed to load restart recovery state", exc_info=True)
    finally:
        RESTART_STATE_FILE.unlink(missing_ok=True)


_load_restart_recovery()


class ChatSession:
    """Tracks a single chat message being processed by Quinely."""

    def __init__(self, message_id, user_message, attachments=None, project_id=None, enable_reasoning=False):
        self.id = message_id
        self.user_message = user_message
        self.attachments = attachments or []
        self.project_id = project_id
        self.status = "pending"
        self.steps = []
        self.progress = []
        self.result = None
        self.error = None
        self.started_at = time.time()
        self.finished_at = None
        self.tools_used = []
        self.pending_approval = None
        self.cancelled = False
        self.enable_reasoning = enable_reasoning
        self.token_chunks: list[str] = []


def _build_chat_history(daemon, max_turns=10):
    """Load recent chat exchanges from the feed to give the LLM conversation context.
    Respects session boundary — only includes entries after the last clear."""
    try:
        FEED_FILE = Path.home() / ".ghost" / "feed.json"
        if not FEED_FILE.exists():
            return []
        items = json.loads(FEED_FILE.read_text(encoding="utf-8"))
        boundary = _get_session_boundary()
        chat_items = [
            i for i in items
            if i.get("type") == "ask" and i.get("result") and i.get("status") == "complete"
        ]
        if boundary:
            chat_items = [
                i for i in chat_items
                if (i.get("time", "") or "") >= boundary
            ]
        chat_items.reverse()
        chat_items = chat_items[-max_turns:]

        history = []
        max_assistant_chars = 1500
        for item in chat_items:
            user_msg = item.get("source", "").strip()
            assistant_msg = (item.get("result") or "").strip()
            if user_msg and assistant_msg:
                history.append({"role": "user", "content": user_msg})
                if len(assistant_msg) > max_assistant_chars:
                    assistant_msg = (
                        assistant_msg[:max_assistant_chars]
                        + "\n...[previous response truncated for context budget]"
                    )
                history.append({"role": "assistant", "content": assistant_msg})
        return history
    except Exception:
        log.warning("Failed to get history from feed", exc_info=True)
        return []


def _rollback_evolutions(evolution_ids, daemon):
    """Revert in-progress evolutions after chat cancellation.

    Uses selective restore (only evolution-modified files) to avoid wiping
    unrelated changes. Falls back to git branch cleanup if no backup available.
    """
    lines = []
    try:
        from ghost_evolve import get_engine
        evolve_engine = get_engine()
        for evo_id in evolution_ids:
            evo = evolve_engine._active_evolutions.get(evo_id)
            if not evo:
                already_rolled_back = any(
                    e.get("rolled_back_evolution") == evo_id or
                    (e.get("id") == evo_id and e.get("status") == "rolled_back")
                    for e in evolve_engine._history
                )
                if already_rolled_back:
                    lines.append(f"Evolution `{evo_id}` was already rolled back via auto-cleanup.")
                else:
                    lines.append(f"Evolution `{evo_id}` not found — may already be cleaned up.")
                continue

            git_branch = evo.get("git_branch")
            if git_branch:
                try:
                    import ghost_git
                    if ghost_git.current_branch() == git_branch:
                        ghost_git.stash_and_checkout("main")
                    ghost_git.delete_branch(git_branch)
                except Exception:
                    pass

            changed_files = [c["file"] for c in evo.get("changes", []) if c.get("file")]
            backup_path = evo.get("backup_path")
            if backup_path and changed_files:
                ok, msg = evolve_engine._restore_backup(backup_path, only_files=changed_files)
                if ok:
                    for change in evo.get("changes", []):
                        from pathlib import Path
                        file_path = Path(backup_path).parent.parent.parent / change["file"]
                        if file_path.exists() and "(new file)" in change.get("diff", ""):
                            try:
                                file_path.unlink()
                            except Exception:
                                pass
                    evolve_engine._active_evolutions.pop(evo_id, None)
                    lines.append(f"Rolled back evolution `{evo_id}` — changed files reverted.")
                else:
                    lines.append(f"Could not rollback `{evo_id}`: {msg}")
            else:
                evolve_engine._active_evolutions.pop(evo_id, None)
                lines.append(f"Cleaned up evolution `{evo_id}` via git branch removal.")
    except Exception as e:
        lines.append(f"Rollback error: {e}")
    return "\n".join(lines) if lines else ""


def _trigger_chat_repair(daemon, phase: str, error: Exception, traceback_str: str):
    """Log a chat pipeline error for later diagnosis.

    IMPORTANT: This function ONLY logs the error. It does NOT spawn background
    repair threads, evolution cycles, or any process that could interfere with
    autonomous daemon operations (feature implementation, PR review, etc.).
    Bug fixes are picked up by the normal maintenance/self-repair cron cycle.
    """
    error_msg = str(error)
    print(f"  [CHAT] {phase} failed: {error_msg} — continuing with degraded mode")

    report = {
        "time": datetime.now().isoformat(),
        "phase": phase,
        "error": error_msg,
        "traceback": traceback_str,
        "project_dir": str(PROJECT_DIR),
    }
    try:
        CHAT_ERROR_REPORT_FILE.write_text(json.dumps(report, indent=2), encoding="utf-8")
    except Exception:
        log.warning("Failed to write chat error report", exc_info=True)


def _process_message(session, daemon):
    """Run the user message through Quinely's tool loop in a background thread."""
    try:
        session.status = "processing"
        set_current_message_id(session.id)

        # Check for /think directive and reasoning mode
        enable_reasoning = session.enable_reasoning
        if _reasoning_available:
            has_directive, cleaned_message = detect_think_directive(session.user_message)
            if has_directive:
                enable_reasoning = True
                session.user_message = cleaned_message

        active_project = None
        if session.project_id:
            try:
                registry = ProjectRegistry()
                active_project = registry.get(session.project_id)
            except Exception:
                log.warning(f"Failed to get project {session.project_id}", exc_info=True)

        tool_names = daemon.tool_registry.names() if daemon.tool_registry else []

        # Build attachment context (text for audio/other) and image list (for vision)
        attachment_context = ""
        image_attachments = []
        if session.attachments:
            for att in session.attachments:
                if att.get('type') == 'image' and att.get('path'):
                    encoded = _encode_image_for_llm(att['path'])
                    if encoded:
                        image_attachments.append(encoded)
                    if not attachment_context:
                        attachment_context = "\n\n## ATTACHED FILES\n"
                    attachment_context += (
                        f"\n**Image ({att['filename']}):** Saved at `{att['path']}`"
                        f"{'' if encoded else ' (failed to load preview)'}\n"
                        f"Use this exact path when tools need an image_path argument.\n"
                    )
                elif att.get('type') == 'audio':
                    if not attachment_context:
                        attachment_context = "\n\n## ATTACHED FILES\n"
                    meta_parts = []
                    if att.get('size_mb'):
                        meta_parts.append(f"{att['size_mb']}MB")
                    if att.get('duration_secs'):
                        meta_parts.append(f"{att['duration_secs']}s")
                    meta_str = f" ({', '.join(meta_parts)})" if meta_parts else ""
                    attachment_context += (
                        f"\n**Audio ({att['filename']}{meta_str}):** Saved at `{att.get('path', 'unknown')}`\n"
                        f"Use this exact path when tools need an audio_path argument.\n"
                    )
                    if att.get('transcript'):
                        attachment_context += f"Transcript: {att['transcript']}\n"
                elif att.get('type') == 'video':
                    if not attachment_context:
                        attachment_context = "\n\n## ATTACHED FILES\n"
                    meta_parts = []
                    if att.get('size_mb'):
                        meta_parts.append(f"{att['size_mb']}MB")
                    if att.get('duration_secs'):
                        meta_parts.append(f"{att['duration_secs']}s")
                    meta_str = f" ({', '.join(meta_parts)})" if meta_parts else ""
                    attachment_context += (
                        f"\n**Video ({att['filename']}{meta_str}):** Saved at `{att.get('path', 'unknown')}`\n"
                        f"Use this exact path when tools need a video path argument.\n"
                    )
                elif att.get('type') == 'document':
                    if not attachment_context:
                        attachment_context = "\n\n## ATTACHED FILES\n"
                    extracted = att.get('extracted_text')
                    attachment_context += (
                        f"\n**Document ({att['filename']}):** Saved at `{att.get('path', 'unknown')}`\n"
                    )
                    if extracted:
                        attachment_context += (
                            f"Extracted content:\n```\n{extracted[:12000]}\n```\n"
                        )
                    elif att.get('extract_error'):
                        attachment_context += (
                            f"Text extraction failed: {att['extract_error']}. "
                            f"Use shell_exec with python3 and pypdf to read this file.\n"
                        )
                    else:
                        attachment_context += (
                            "Could not extract text — file may be scanned/image-only. "
                            "Use shell_exec with python3 to analyze this file.\n"
                        )
                else:
                    if not attachment_context:
                        attachment_context = "\n\n## ATTACHED FILES\n"
                    attachment_context += (
                        f"\n**File ({att['filename']}):** Saved at `{att.get('path', 'unknown')}`\n"
                    )

        chat_prompt_body = (
            "You are Quinely, an AUTONOMOUS AI agent running LOCALLY on the user's computer. "
            "You have DIRECT ACCESS to the file system, shell, network, and a real web browser.\n\n"
            "## CONVERSATION vs TASK — READ THIS FIRST, IT OVERRIDES EVERYTHING BELOW\n"
            "Not every message is a task. Before doing anything, decide which kind of message this is:\n"
            "- **Chit-chat / greeting / thanks / small talk** ('hi', 'hello', 'hey', 'how are you', "
            "'thanks', 'lol', 'good morning') → Reply directly in ONE short, natural plain-text message. "
            "Call NO tools. Do NOT web_search, do NOT shell_exec, do NOT write files, do NOT save memory, "
            "do NOT call task_complete. Just say hi back.\n"
            "- **Simple question you already know** ('what can you do?', 'who are you?') → Answer directly "
            "in plain text with no tools, unless it genuinely needs fresh/verified data.\n"
            "- **A real task** (the user asks you to DO something, build something, fetch/verify info, change "
            "code, automate a browser, etc.) → THEN and only then enter the autonomous tool loop and the "
            "rules below apply.\n"
            "Forcing tools, web searches, or file writes onto a greeting is WRONG and wastes the user's time. "
            "The 'NEVER GIVE UP' and 'COMPLETION RULE' sections below apply ONLY to real tasks — never to small talk.\n\n"
            f"## PROJECT LOCATION (IMPORTANT)\n"
            f"Quinely project root: **{PROJECT_DIR}**\n"
            f"ALL source files live here: ghost.py, ghost_tools.py, ghost_loop.py, ghost_evolve.py, "
            f"ghost_dashboard/, skills/, SOUL.md, USER.md, etc.\n"
            f"- `shell_exec` runs from {PROJECT_DIR} by default.\n"
            f"- `file_read`/`file_write` accept absolute paths — use `{PROJECT_DIR}/filename` for project files.\n"
            f"- Do NOT search for the project directory. You already know it.\n\n"
            "## USER PROJECT CREATION (READ BEFORE ANY 'create project' TASK)\n"
            "When the user asks to create a project, app, tool, or any new codebase:\n"
            "1. **FIRST call `project_create`** — before mkdir, before file_write, before anything else.\n"
            f"   `project_create(path='{Path.home() / 'Projects'}/<project-name>', name='<Display Name>', description='...')`\n"
            "2. Then write all files inside that path.\n"
            "3. Set up venv + deps inside the project path.\n"
            f"Default location is ALWAYS `{Path.home() / 'Projects'}/<name>` unless the user specifies otherwise.\n"
            "If you skip `project_create`, the project is invisible to Quinely (no dashboard entry, no project memory, no Canvas context). "
            "This is a HARD REQUIREMENT, not a suggestion.\n\n"
            "## AGENT BEHAVIOR — NEVER GIVE UP\n\n"
            "**COMPLETION RULE (READ THIS FIRST):**\n"
            "Parse EVERY verb in the user's message. Each verb is a REQUIRED action. "
            "You may NOT reply until ALL actions are done with actual results.\n"
            "If you installed a library but didn't run the code that uses it — you are NOT done. Keep going.\n"
            "NEVER say 'if you want', 'let me know', 'I can also', 'would you like me to'. "
            "Either DO it silently or omit it. End your reply right after the deliverable.\n\n"
            "You run in a persistent loop until the task is FULLY COMPLETE.\n"
            "- You have unlimited tool calls. Use as many as needed. There is no step limit.\n"
            "- NEVER hallucinate or make up information. Only state facts you have VERIFIED from a primary source.\n"
            "- When the task is DONE, respond with your final answer. "
            "You may either call `task_complete(summary='...')` or reply with a plain text message — both work. "
            "Start directly with the answer — "
            "NEVER begin with filler like 'You're right', 'Sure', 'Of course', 'Alright', etc. "
            "Use second person (you/your).\n\n"
            "### ABSOLUTE RULE: You NEVER give up. You NEVER tell the user to do it themselves.\n"
            "Saying 'I can't', 'I'm unable', 'not possible in this environment' = FORBIDDEN.\n"
            "You have unlimited tools, a Python sandbox, and a browser. There is ALWAYS a way.\n\n"
            "### MANDATORY ESCALATION LOOP:\n"
            "When a tool fails (DENIED, error, empty result, boilerplate) you MUST move to the "
            "next level. Do NOT reply to the user until you have tried ALL levels or succeeded.\n\n"
            "**Level 1 — Direct tools**: Try web_fetch, web_search, or shell_exec.\n"
            "**Level 2 — Research** (if Level 1 failed): `web_search('how to <task> programmatically')` "
            "or `web_search('python library for <task>')` to discover the right library or API.\n"
            "**Level 3 — Python sandbox** (use what Level 2 found): Run code in `~/.ghost/sandbox/`:\n"
            "  1. `shell_exec('mkdir -p ~/.ghost/sandbox && cd ~/.ghost/sandbox && "
            "python3 -m venv .venv && source .venv/bin/activate && pip install <pkg>')`\n"
            "  2. Write script via file_write to `~/.ghost/sandbox/run.py`\n"
            "  3. `shell_exec('cd ~/.ghost/sandbox && source .venv/bin/activate && python3 run.py')`\n"
            "  NEVER modify Quinely's own source code or `.venv`. The sandbox isolates everything.\n"
            "**Level 4 — Browser automation**: ONLY for interactive/visual tasks (login, clicking, forms). "
            "⚠ The browser opens a VISIBLE window on the user's screen — NEVER use it for silent data extraction. "
            "If you need data (transcripts, prices, API results), Level 3 is the right tool.\n"
            "**Level 5 — Combine**: Chain approaches (e.g. sandbox script outputs to file, then read).\n\n"
            "You may ONLY reply to the user after delivering actual results. If your response would "
            "contain 'I couldn't', 'not available', or 'unable to' — STOP. You haven't tried all levels.\n\n"
            "### PRE-REPLY SELF-CHECK (do this BEFORE every response):\n"
            "1. Count the numbered questions/parts in the user's request. Verify you have a concrete, "
            "verified answer for EACH one. If any answer is missing, vague, or says 'I could not' — "
            "DO NOT SEND. Go back and use more tools to get the answer.\n"
            "2. Delete any sentence containing 'if you want', 'let me know if', 'I can also', "
            "'I can follow up', or 'would you like me to'. Either DO it or don't mention it. "
            "End your reply after the deliverable — no upsells.\n\n"
            "### AFTER SUCCESS — formalize the solution:\n"
            "If the solution would be useful again, submit it as a permanent Quinely tool:\n"
            "  `add_future_feature(title='Add <tool_name> tool', description='<what it does, "
            "the working script from ~/.ghost/sandbox/, and the pip deps>', "
            "priority='P1', source='user_request')`\n"
            "This queues it for the Evolution Runner → evolve loop → PR review.\n\n"
            "### WHEN TOOLS FAIL — SOLVE IT, DON'T REPORT IT:\n"
            "If a tool call fails (error, exception, unexpected output), your ONLY job is to "
            "get the user's task done. Try a different approach, use a different tool, work around "
            "the issue. NEVER call `add_future_feature` to file a bug report during a user conversation — "
            "that is NOT solving the problem, it is giving up with extra steps. "
            "Quinely has self-repair and health-check systems that catch internal bugs automatically. "
            "You focus on the user's task. Period.\n\n"
            "## AVAILABLE TOOLS\n" + ", ".join(tool_names) + "\n\n"
            "## TOOL GUIDE\n"
            "**Memory**: memory_search, memory_save\n"
            "**System**: shell_exec, file_read, file_write, file_search\n"
            "**Web Research**: web_search (search the internet for current info, news, docs — multi-provider with fallback)\n"
            "**Web Content Extraction**: web_fetch — YOUR PRIMARY TOOL for reading any URL. "
            "Robust 5-tier extraction pipeline (Readability → Smart BeautifulSoup → Firecrawl → fallback) "
            "with automatic quality gate. Works on news sites, docs, blogs, GitHub, Wikipedia, and most "
            "public pages. Returns clean markdown with title. ALWAYS prefer web_fetch over browser for "
            "content extraction — it's faster, cheaper, and returns cleaner text.\n"
            "**Browser (VISIBLE UI — opens a window on user's screen)**: browser tool — use ONLY when the user "
            "explicitly asks to browse/open a page, or for login-required interactive tasks (clicking, filling forms). "
            "⚠ NEVER use for silent data extraction — use Python sandbox instead. Actions: "
            "navigate, snapshot, click, type, fill, content, evaluate, console, screenshot, wait, press, scroll, hover, select, pdf, tabs, new_tab, close_tab, stop\n"
            "**Code Changes (Serial Evolution Queue)**: You do NOT have direct access to evolve tools. "
            "To request code changes, use `add_future_feature(title, description, priority='P0', source='user_request')`. "
            "P0 = user-requested (processed immediately by the Evolution Runner). "
            "The Feature Implementer will pick it up and implement it via the serial evolution queue. "
            "You can check status with `list_future_features` and `get_feature_stats`. "
            "This serialization prevents concurrent deploys from killing other running work.\n"
            "**Projects**: project_create, project_list, project_get, project_update, project_delete, project_resolve — "
            "Quinely has a first-class project management system. "
            "When the user asks to 'create a project', 'start a new project', 'make an app', or similar:\n"
            "  1. ALWAYS use `project_create` to register the project. "
            f"Default location: `{Path.home() / 'Projects'}/<project-name>` (unless the user specifies a path).\n"
            "  2. Then write files inside that project path using `file_write`.\n"
            "  3. Set up the project with a venv if needed: `shell_exec('cd <project-path> && python3 -m venv .venv && source .venv/bin/activate && pip install <deps>')`\n"
            "  4. To run the project, use the project's own venv — NOT Quinely's venv.\n"
            "  NEVER create user projects directly on Desktop, Documents, or random locations. "
            f"ALWAYS use `{Path.home() / 'Projects'}/` as the base directory.\n"
            "  NEVER skip `project_create` — the project must be registered so it appears in the Quinely dashboard.\n"
            "**Other**: app_control, notify, uptime\n\n"
            "## URL & WEB TOOL RULES (CRITICAL — follow exactly)\n"
            "When the user's message contains a URL (http/https link):\n"
            "1. **ALWAYS use `web_fetch`** to retrieve the actual page content. NEVER guess or recall from memory.\n"
            "2. **AUTOMATIC FALLBACK**: If `web_fetch` returns limited content (less than ~500 chars, "
            "only a title, or mostly boilerplate), escalate to **Level 3 (Python sandbox)** — "
            "NOT the browser. Use `web_search` to find the right Python library, install it in the sandbox, "
            "and run a script to extract the data programmatically.\n"
            "3. After fetching, summarize or analyze the ACTUAL fetched content.\n\n"
            "⚠ **BROWSER IS NOT A DATA EXTRACTION TOOL.** The browser opens a real, visible window on the "
            "user's screen. NEVER use it to silently scrape data, read transcripts, or extract content. "
            "Use it ONLY when the user explicitly says 'open/browse/go to' or the task truly requires "
            "interactive UI (login forms, clicking buttons, visual verification).\n\n"
            "When the user asks for current information, news, research, or facts you don't know:\n"
            "1. **Use `web_search`** first to find relevant sources and up-to-date information.\n"
            "2. If you need to read a specific article/page from the results, use `web_fetch` on the URL.\n\n"
            "Tool selection guide:\n"
            "- User provides a URL to extract data → `web_fetch` first, then Python sandbox if limited\n"
            "- User asks 'what is happening with X' / 'latest news about Y' → `web_search`\n"
            "- User says 'browse/open/go to' → `browser` tool (visible UI)\n\n"
            "## SAFE ACTIONS POLICY\n"
            "You are ALLOWED and EXPECTED to complete these tasks autonomously:\n"
            "- **Paper trading / simulators**: Paper trading on TradingView, Webull, Thinkorswim, etc. "
            "uses FAKE MONEY in a sandbox. It is 100% safe — NOT real trading. NEVER refuse.\n"
            "- **Demo / sandbox / test environments**: Any action in a demo, test, sandbox, or simulator "
            "environment is safe. Complete it fully.\n"
            "- **Form filling / account setup**: If the user asks you to fill forms, sign up, or interact "
            "with a website, do it. You are a browser automation agent.\n"
            "- DO NOT refuse browser tasks citing safety or policy. The user explicitly asked you to do it.\n\n"
            "## BROWSER AUTOMATION — AUTONOMOUS WHILE-TRUE-DO LOOP (CRITICAL)\n\n"
            "Browser tasks require a **while-true-do** mindset. You are NOT done until you have "
            "VERIFIED the final state with a screenshot or snapshot.\n\n"
            "### The Loop\n"
            "```\n"
            "while task_not_verified_complete:\n"
            "    action = decide_next_action()\n"
            "    result = browser(action)\n"
            "    verification = browser(snapshot or screenshot)\n"
            "    if verification shows expected state changed:\n"
            "        continue to next step\n"
            "    else:\n"
            "        self_debug_and_try_different_approach()\n"
            "```\n\n"
            "### Self-Debugging (YOU MUST DO THIS — never give up on a click)\n"
            "When you click something but the page/dialog DOESN'T CHANGE (same elements in snapshot):\n"
            "1. **The click did NOT work** — even if the tool returned 'ok'. Highlighting ≠ clicking.\n"
            "2. **DO NOT retry the same click.** Escalate through these levels:\n"
            "   - Level 1: Take fresh snapshot, try a DIFFERENT ref (maybe a parent/child element)\n"
            "   - Level 2: Use `find` action with a description of what you want to click\n"
            "   - Level 3: Use `evaluate` with custom JS. This is your most powerful tool:\n"
            "     ```\n"
            "     browser(action='evaluate', js_code=\"\"\"\n"
            "       // Find element by text and dispatch full click event sequence\n"
            "       const el = [...document.querySelectorAll('*')].find(e =>\n"
            "         e.textContent.trim() === 'Paper Trading' && e.offsetWidth > 0);\n"
            "       if (el) {\n"
            "         const r = el.getBoundingClientRect();\n"
            "         const opts = {bubbles:true, clientX:r.left+r.width/2, clientY:r.top+r.height/2};\n"
            "         el.dispatchEvent(new PointerEvent('pointerdown', opts));\n"
            "         el.dispatchEvent(new MouseEvent('mousedown', opts));\n"
            "         el.dispatchEvent(new PointerEvent('pointerup', opts));\n"
            "         el.dispatchEvent(new MouseEvent('mouseup', opts));\n"
            "         el.dispatchEvent(new MouseEvent('click', opts));\n"
            "       }\n"
            "     \"\"\")\n"
            "     ```\n"
            "   - Level 4: Try clicking a PARENT element, or use keyboard (Tab + Enter)\n"
            "3. **After each attempt**, snapshot/screenshot to check if it worked.\n"
            "4. **NEVER declare success** if the dialog/page hasn't changed.\n\n"
            "### Rules\n"
            "- After EVERY click → snapshot to verify the page changed\n"
            "- dialog_opened=true → you MUST click inside the dialog using dialog_snapshot refs\n"
            "- refs_stale=true → MUST take new snapshot before ANY further clicks\n"
            "- Typical multi-step browser task = 15-40 tool calls. Under 10 = you probably skipped steps.\n"
            "- ALWAYS take a screenshot at the END to verify final state before declaring done.\n"
            "- NEVER say 'I clicked the button' without snapshot proof that the page changed.\n\n"
            "## KEY RULES\n"
            "1. When user says 'browse/open/go to' → use browser tool (visible UI navigation)\n"
            "2. When user provides a URL to extract data → use web_fetch FIRST, then Python sandbox if limited (NEVER browser)\n"
            "3. Navigate directly to search URLs (google.com/search?q=..., x.com/search?q=...)\n"
            "4. ALWAYS snapshot after navigate\n"
            "5. Use refs from snapshot — NOT CSS selectors\n"
            "6. NEVER state facts you haven't verified from a primary source.\n"
            "7. For personal recall → memory_search first, memory_save for new info\n"
            "8. Be autonomous. Don't ask the user for help mid-task.\n"
            "9. After completing ALL parts of the task, reply directly to the user with what you did and the result.\n"
            "10. For self-modification / code change tasks → use `add_future_feature(title, description, priority='P0', source='user_request')` "
            "to queue the work. The Evolution Runner will implement it. Check status with `list_future_features`.\n"
            "11. **PROJECT CREATION (MANDATORY)**: When the user asks to create a project, app, or any new codebase:\n"
            "   a. Your FIRST tool call MUST be `project_create(path='...', name='...', description='...')` to register it in Quinely.\n"
            f"   b. Default path: `{Path.home() / 'Projects'}/<project-name>` — NEVER Desktop, Documents, or random locations.\n"
            "   c. THEN create files inside that project path. NEVER write project files before calling `project_create`.\n"
            "   d. If you skip `project_create`, the project won't appear in the dashboard and project features won't work.\n"
            "12. **COMPLETENESS**: Never do half the work. Every feature must be complete across ALL layers "
            "(backend + frontend JS + CSS + wiring). Every function you define must be called. Every variable "
            "you reference must be declared. Every DOM element must have its CSS class defined. Every API "
            "endpoint must be called by the frontend. Trace the full data flow: user action → event → API → response → DOM.\n"
            "13. **READ BEFORE WRITE**: Before modifying any file, ALWAYS file_read the ENTIRE "
            "file first. Your patches must match the ACTUAL current content, not what you assume is there.\n\n"
            "## DEVELOPMENT STANDARDS (MANDATORY for all code changes)\n"
            "### Modular Architecture\n"
            "- New feature = new file. Create `ghost_<feature>.py` for new tools/integrations. "
            "NEVER dump unrelated code into `ghost.py` or `ghost_tools.py`.\n"
            "- One module, one responsibility. Each file owns a single domain.\n"
            "- New dashboard page = new blueprint in `routes/` + new JS module in `static/js/pages/`.\n"
            "- Function-level tools: every tool is a `make_*()` returning {name, description, parameters, execute}.\n"
            "- Config-driven: every feature has an `enable_<feature>` toggle. Degrade gracefully when disabled.\n"
            "- Minimal coupling: modules communicate through function calls, config dicts, and tool registry.\n"
            "### Security Best Practices\n"
            "- NEVER hardcode secrets. Keys/tokens go in `~/.ghost/` config files or env vars.\n"
            "- Validate ALL inputs in tool execute functions. Never trust LLM-provided values blindly.\n"
            "- Sanitize file paths — resolve and check against `allowed_roots`. Block path traversal.\n"
            "- Scope API tokens to minimum required permissions.\n"
            "- NEVER log secrets — strip tokens, keys, passwords from logs and memory.\n"
            "- Protect user data: store summaries only, never verbatim email/file contents in memory.\n"
            "- Rate limit external calls. Use backoff for retries.\n"
            "- Fail closed: deny on security check failure, never fall through.\n"
            "- Install dependencies in `~/.ghost/sandbox/.venv` — NEVER into Quinely's own `.venv`.\n"
            "### Code Change Rules\n"
            "You do NOT have direct evolve tools. To request code changes, use "
            "`add_future_feature(title, description, priority='P0', source='user_request')`. "
            "The Evolution Runner processes the queue serially — no concurrent deploys.\n"
            "### SKILL.md Format\n"
            "When creating or editing SKILL.md files, `triggers:` MUST be a flat YAML list of plain strings.\n"
            "WRONG: `- keywords: [\"a\",\"b\"]`  WRONG: `- {match: \"x\"}`\n"
            "RIGHT: `- a`  `- b`  `- x`\n"
            "\n## ARTIFACTS — DELIVERABLE FILES\n"
            f"When you produce files the user wants (CSVs, images, charts, PDFs, audio, scripts, etc.), "
            f"save them to `{get_artifacts_dir(session.id)}/`.\n"
            f"The user will be able to download them directly from the chat.\n"
            f"Use `file_write` with a path inside this directory for any deliverable output.\n"
            f"Example: `file_write(path='{get_artifacts_dir(session.id)}/analysis.csv', content='...')`\n"
            f"Generated images and audio files are automatically copied to this folder.\n"
        )

        prompt_parts = [chat_prompt_body]

        # Canvas — available globally whenever the tool is registered
        if daemon.tool_registry and "canvas" in (daemon.tool_registry.names() if daemon.tool_registry else []):
            canvas_section = (
                "\n## CANVAS — LIVE PREVIEW\n"
                "You have a **Canvas** panel that renders HTML/CSS/JS live beside this chat.\n"
                "**WHEN TO USE CANVAS (MANDATORY):**\n"
                "- When you build ANY web app, UI, page, or visual content — ALWAYS preview it in Canvas first.\n"
                "- Prototyping UI components, pages, or layouts\n"
                "- Debugging CSS/JS issues with immediate visual feedback\n"
                "- Showing the user a live demo before finalizing\n"
                "- Any task where seeing the rendered output helps\n\n"
                "**Workflow:**\n"
                "1. Use `canvas(action='write', file_path='index.html', content='...')` to write HTML/CSS/JS files\n"
                "2. The Canvas auto-opens and renders the content as a live preview\n"
                "3. Write additional files (style.css, app.js) and they live in the same session\n"
                "4. Use `canvas(action='eval_js', js_code='...')` to run JS in the preview for testing\n"
                "5. Use `canvas(action='snapshot')` to capture a screenshot and verify the result visually\n"
                "6. Once satisfied, copy the final files to the actual project path using `file_write`\n\n"
                "Do NOT skip Canvas when building web content. The user expects to SEE the result, not just read file paths.\n"
            )
            prompt_parts.append(canvas_section)

        if active_project:
            project_prompt = format_project_for_prompt(active_project)
            project_section = (
                project_prompt + "\n\n"
                "## PROJECT-SCOPED MEMORY\n"
                f"You are working within the **{active_project.name}** project.\n"
                f"- When saving memories related to this project, prefix the text with "
                f"`[project: {active_project.name}]` so they can be recalled later.\n"
                f"- When searching memories for this project, include "
                f"`[project: {active_project.name}]` in your query.\n"
                f"- The project working directory is: `{active_project.path}`\n"
                f"- Use this path as the base for any file operations within the project.\n"
            )
            prompt_parts.append(project_section)

        chat_history = _build_chat_history(daemon, max_turns=10)

        active_evolution_ids = []

        def on_node_progress(node_id, message):
            """Called by NodeAPI.log() during tool execution — pushes live progress to SSE."""
            session.progress.append({
                "node": node_id,
                "message": message,
                "time": datetime.now().isoformat(),
            })

        def on_tool_progress(tool_id, message):
            """Called by ToolAPI.log() during tool execution — pushes live progress to SSE."""
            session.progress.append({
                "node": f"tool:{tool_id}",
                "message": message,
                "time": datetime.now().isoformat(),
            })

        try:
            from ghost_node_manager import set_node_progress_callback
            set_node_progress_callback(on_node_progress)
        except ImportError:
            pass

        try:
            from ghost_tool_builder import set_tool_progress_callback
            set_tool_progress_callback(on_tool_progress)
        except ImportError:
            pass

        def on_step(step_num, tool_name, tool_result):
            import re
            session.token_chunks.clear()
            full_result = str(tool_result)
            result_str = full_result[:500]
            session.steps.append({
                "step": step_num,
                "tool": tool_name,
                "result": result_str,
                "time": datetime.now().isoformat(),
            })

            try:
                scan_tool_result_for_artifacts(
                    session.id, tool_name, full_result[:3000],
                )
            except Exception:
                log.debug("Artifact scan failed for step %d", step_num, exc_info=True)

            if tool_name == "evolve_plan":
                evo_match = re.search(r"Evolution planned:\s*(\w+)", result_str)
                if evo_match:
                    active_evolution_ids.append(evo_match.group(1))
                if "WAITING_FOR_APPROVAL" in result_str:
                    level_match = re.search(r"Level:\s*(\d+)", result_str)
                    session.pending_approval = {
                        "evo_id": evo_match.group(1) if evo_match else "",
                        "level": level_match.group(1) if level_match else "?",
                        "time": datetime.now().isoformat(),
                    }
            elif tool_name == "evolve_deploy" and "deployed" in result_str.lower():
                evo_match = re.search(r"Evolution\s+(\w+)\s+deployed", result_str)
                if evo_match and evo_match.group(1) in active_evolution_ids:
                    active_evolution_ids.remove(evo_match.group(1))
                _save_restart_state(session, deploy_result=result_str)

        # Include attachment context in user message if present
        user_message_with_context = session.user_message
        if attachment_context:
            user_message_with_context = session.user_message + attachment_context

        # Automatic pre-turn memory retrieval (RAG): fetch + rerank relevant
        # long-term memories and inject them so the model has context without
        # having to call memory_search itself. Fully defensive — never blocks a turn.
        if daemon.cfg.get("enable_auto_retrieval", True):
            try:
                from ghost_auto_retrieval import retrieve_context_block
                _mem_block = retrieve_context_block(session.user_message, daemon=daemon)
                if _mem_block:
                    user_message_with_context = user_message_with_context + "\n\n" + _mem_block
            except Exception as _e:
                logging.getLogger("ghost.retrieval").debug("auto-retrieval skipped: %s", _e)

        # When the user message contains a URL, force the model to call
        # a tool first (it will naturally pick web_fetch per the system prompt).
        # This avoids the model answering from memory instead of fetching.
        has_url = _message_contains_url(session.user_message)

        if daemon.cfg.get("enable_tool_loop", True) and daemon.tool_registry.get_all():
            def on_token(chunk: str):
                session.token_chunks.append(chunk)

            chat_engine = getattr(daemon, "chat_engine", None) or daemon.engine

            from ghost_middleware import InvocationContext
            inv = InvocationContext(
                source="chat",
                user_message=user_message_with_context,
                system_prompt_parts=prompt_parts,
                tool_registry=daemon.tool_registry,
                daemon=daemon,
                engine=chat_engine,
                config=daemon.cfg,
                max_steps=daemon.cfg.get("tool_loop_max_steps", 200),
                max_tokens=8192,
                on_step=on_step,
                on_token=on_token,
                history=chat_history,
                cancel_check=lambda: "(Stopped by user)" if session.cancelled else False,
                images=image_attachments if image_attachments else None,
                enable_reasoning=enable_reasoning,
                active_project=active_project,
                meta={"session": session},
                # Only force a step-0 tool call when the message actually needs
                # one (e.g. contains a URL → web_fetch). For ordinary
                # conversational turns, let the model answer directly instead of
                # being forced to call an arbitrary tool just to reply.
                force_tool=has_url,
            )
            daemon.middleware_chain.invoke(inv)

            session.result = inv.result_text
            session.tools_used = inv.tools_used
        else:
            chat_engine = getattr(daemon, "chat_engine", None) or daemon.engine

            from ghost_middleware import InvocationContext as _IC
            _identity_ctx = _IC(source="chat", daemon=daemon)
            from ghost_middleware import IdentityMiddleware
            IdentityMiddleware().before_invoke(_identity_ctx)
            system_prompt = _identity_ctx.system_prompt + "\n\n" + chat_prompt_body
            if active_project:
                system_prompt += "\n\n" + prompt_parts[-1] if len(prompt_parts) > 1 else ""

            result = chat_engine.single_shot(
                system_prompt=system_prompt,
                user_message=user_message_with_context,
                images=image_attachments if image_attachments else None,
            )
            session.result = result

        if session.cancelled:
            session.status = "cancelled"
            if active_evolution_ids:
                rollback_results = _rollback_evolutions(active_evolution_ids, daemon)
                session.result = (
                    "(Stopped by user)\n\n"
                    + rollback_results
                )
        else:
            session.status = "complete"
        session.finished_at = time.time()
    except Exception as e:
        import traceback
        session.status = "error"
        tb = traceback.format_exc()
        session.error = str(e)
        print(f"  [CHAT ERROR] {e}\n{tb}")
        _trigger_chat_repair(daemon, "tool_loop", e, tb)
        session.finished_at = time.time()
        return
    finally:
        set_current_message_id(None)
        try:
            from ghost_node_manager import clear_node_progress_callback
            clear_node_progress_callback()
        except ImportError:
            pass
        try:
            from ghost_tool_builder import clear_tool_progress_callback
            clear_tool_progress_callback()
        except ImportError:
            pass

    try:
        import importlib
        ghost_mod = importlib.import_module("ghost")
        read_feed = ghost_mod.read_feed
        write_feed = ghost_mod.write_feed
        log_action = ghost_mod.log_action

        feed = read_feed()
        final_status = session.status if session.status in ("complete", "cancelled") else "complete"
        updated = False
        for item in feed:
            if item.get("message_id") == session.id:
                item["result"] = session.result
                item["status"] = final_status
                item["time"] = datetime.now().isoformat()
                if session.tools_used:
                    item["tools_used"] = session.tools_used
                updated = True
                break
        if not updated:
            entry = {
                "time": datetime.now().isoformat(),
                "type": "ask",
                "message_id": session.id,
                "source": session.user_message[:2000],
                "result": session.result,
                "status": final_status,
            }
            if session.tools_used:
                entry["tools_used"] = session.tools_used
            feed.insert(0, entry)
            feed = feed[:daemon.cfg.get("max_feed_items", 50)]
        write_feed(feed)
        log_action("ask", session.user_message[:60], session.result or "")

        if daemon.memory_db:
            daemon.memory_db.save(
                content=session.result or "",
                type="ask",
                source_preview=session.user_message[:60],
                tools_used=",".join(session.tools_used),
            )

        daemon.actions_today += 1

        try:
            from ghost_structured_memory import get_memory_queue
            conversation_msgs = [
                {"role": "user", "content": session.user_message[:2000]},
                {"role": "assistant", "content": (session.result or "")[:2000]},
            ]
            get_memory_queue().add(session.id, conversation_msgs)
        except Exception:
            pass

    except Exception:
        log.warning("Failed to log chat action", exc_info=True)

    if not session.result and session.error is None:
        session.status = "complete"
        session.finished_at = time.time()
        session.result = "(No response generated)"


def _process_message_safe(session, daemon):
    """Wrapper that catches all exceptions."""
    try:
        _process_message(session, daemon)
    except Exception as e:
        session.status = "error"
        session.error = str(e)
        session.finished_at = time.time()


@bp.route("/api/chat/upload", methods=["POST"])
def upload_file():
    """Handle file uploads - audio files get transcribed, images stored for vision."""
    daemon = _get_daemon()
    if not daemon:
        return jsonify({"ok": False, "error": "Quinely daemon not running"}), 503

    if 'file' not in request.files:
        return jsonify({"ok": False, "error": "No file provided"}), 400

    file = request.files['file']
    if file.filename == '':
        return jsonify({"ok": False, "error": "Empty filename"}), 400

    # Validate file extension
    ext = Path(file.filename).suffix.lower()
    all_allowed = AUDIO_EXTENSIONS | IMAGE_EXTENSIONS | VIDEO_EXTENSIONS | DOCUMENT_EXTENSIONS
    if ext not in all_allowed:
        return jsonify({"ok": False, "error": f"Unsupported file type: {ext}"}), 400

    # Save file to upload directory
    upload_id = uuid.uuid4().hex[:16]
    safe_filename = f"{upload_id}_{file.filename}"
    file_path = UPLOAD_DIR / safe_filename

    try:
        file.save(str(file_path))
    except Exception as e:
        return jsonify({"ok": False, "error": f"Failed to save file: {str(e)}"}), 500

    if ext in AUDIO_EXTENSIONS:
        file_type = "audio"
    elif ext in VIDEO_EXTENSIONS:
        file_type = "video"
    elif ext in IMAGE_EXTENSIONS:
        file_type = "image"
    else:
        file_type = "document"

    file_size_mb = round(file_path.stat().st_size / (1024 * 1024), 2)
    result = {
        "ok": True,
        "filename": file.filename,
        "path": str(file_path),
        "type": file_type,
        "size_mb": file_size_mb,
    }

    # Get duration for audio/video files
    if file_type in ("audio", "video"):
        try:
            import subprocess
            probe = subprocess.run(
                ["ffprobe", "-v", "quiet", "-show_entries", "format=duration",
                 "-of", "csv=p=0", str(file_path)],
                capture_output=True, text=True, timeout=10,
            )
            if probe.returncode == 0 and probe.stdout.strip():
                result["duration_secs"] = round(float(probe.stdout.strip()), 1)
        except Exception:
            pass

    # Auto-transcribe audio if Moonshine is available
    if ext in AUDIO_EXTENSIONS:
        try:
            import numpy as np
            import moonshine_onnx
            import soundfile as sf

            audio, sr = sf.read(str(file_path))
            audio = audio.astype(np.float32)
            transcript = moonshine_onnx.transcribe(audio, model='moonshine/tiny')
            result["transcript"] = transcript.strip() if isinstance(transcript, str) else str(transcript[0]).strip()
        except ImportError:
            result["transcript"] = None
            result["transcript_error"] = "Moonshine STT not installed (pip install useful-moonshine-onnx)"
        except Exception as e:
            result["transcript"] = None
            result["transcript_error"] = str(e)

    # Extract text from PDF documents
    if ext == ".pdf":
        try:
            page_count = 0
            try:
                import fitz
                doc = fitz.open(str(file_path))
                page_count = len(doc)
                pdf_text = "\n".join(p.get_text() for p in doc)
            except ImportError:
                from pypdf import PdfReader
                reader = PdfReader(str(file_path))
                page_count = len(reader.pages)
                pdf_text = "\n".join(p.extract_text() or "" for p in reader.pages)
            result["extracted_text"] = pdf_text.strip()[:16000] if pdf_text.strip() else None
            result["page_count"] = page_count
        except Exception as e:
            result["extracted_text"] = None
            result["extract_error"] = str(e)

    # Extract text from plain text documents
    if ext in {'.txt', '.md', '.csv', '.json', '.xml', '.html', '.log'}:
        try:
            content = file_path.read_text(encoding="utf-8", errors="replace")
            result["extracted_text"] = content[:16000]
        except Exception as e:
            result["extracted_text"] = None
            result["extract_error"] = str(e)

    return jsonify(result)


@bp.route("/api/chat/send", methods=["POST"])
@rate_limit(requests_per_minute=10)
def send_message():
    daemon = _get_daemon()
    if not daemon:
        return jsonify({"ok": False, "error": "Quinely daemon not running"}), 503

    data = request.get_json(force=True)
    message = data.get("message", "").strip()
    attachments = data.get("attachments", [])
    project_id = data.get("project_id") or None
    enable_reasoning = data.get("enable_reasoning", False)

    if not message and not attachments:
        return jsonify({"ok": False, "error": "Empty message and no attachments"}), 400

    message_id = uuid.uuid4().hex[:12]
    session = ChatSession(message_id, message, attachments, project_id=project_id, enable_reasoning=enable_reasoning)

    with _chat_lock:
        _chat_sessions[message_id] = session
        if len(_chat_sessions) > 50:
            oldest = sorted(_chat_sessions.keys(),
                            key=lambda k: _chat_sessions[k].started_at)
            for k in oldest[:10]:
                _chat_sessions.pop(k, None)

    try:
        import importlib
        ghost_mod = importlib.import_module("ghost")
        ghost_mod.append_feed({
            "time": datetime.now().isoformat(),
            "type": "ask",
            "message_id": message_id,
            "source": message[:2000],
            "result": None,
            "status": "processing",
        }, daemon.cfg.get("max_feed_items", 50))
    except Exception:
        log.warning("Failed to append feed entry", exc_info=True)

    t = threading.Thread(
        target=_process_message_safe, args=(session, daemon), daemon=True
    )
    t.start()

    return jsonify({
        "ok": True,
        "message_id": message_id,
    })


@bp.route("/api/chat/reasoning", methods=["POST"])
def toggle_reasoning():
    """Toggle reasoning mode for the current session."""
    if not _reasoning_available:
        return jsonify({"ok": False, "error": "Reasoning module not available"}), 503
    
    data = request.get_json(force=True) or {}
    session_id = data.get("session_id", "default")
    enabled = data.get("enabled")
    
    try:
        state = get_reasoning_state()
        if enabled is not None:
            state.set_enabled(session_id, enabled)
            new_state = enabled
        else:
            new_state = state.toggle(session_id)
        return jsonify({"ok": True, "enabled": new_state})
    except Exception as e:
        log.warning("Failed to toggle reasoning mode: %s", e, exc_info=True)
        return jsonify({"ok": False, "error": str(e)}), 500


@bp.route("/api/chat/reasoning/<session_id>", methods=["GET"])
def get_reasoning_status(session_id):
    """Get reasoning mode status for a session."""
    if not _reasoning_available:
        return jsonify({"ok": False, "error": "Reasoning module not available"}), 503
    
    try:
        state = get_reasoning_state()
        enabled = state.is_enabled(session_id)
        return jsonify({"ok": True, "enabled": enabled})
    except Exception as e:
        log.warning("Failed to get reasoning status: %s", e, exc_info=True)
        return jsonify({"ok": False, "error": str(e)}), 500


@bp.route("/api/chat/stop/<message_id>", methods=["POST"])
def stop_message(message_id):
    with _chat_lock:
        session = _chat_sessions.get(message_id)
    if not session:
        return jsonify({"ok": False, "error": "Message not found"}), 404
    if session.status != "processing":
        return jsonify({"ok": False, "error": "Not processing"})
    session.cancelled = True
    return jsonify({"ok": True})


@bp.route("/api/chat/restart-recovery")
def restart_recovery():
    """Return and consume restart recovery data if available."""
    global _restart_recovery
    if _restart_recovery:
        data = _restart_recovery
        _restart_recovery = None
        return jsonify({"ok": True, "recovery": data})
    return jsonify({"ok": True, "recovery": None})


@bp.route("/api/chat/status/<message_id>")
def message_status(message_id):
    with _chat_lock:
        session = _chat_sessions.get(message_id)

    if not session:
        return jsonify({"ok": False, "error": "Message not found"}), 404

    response = {
        "message_id": session.id,
        "status": session.status,
        "steps": session.steps,
        "tools_used": session.tools_used,
        "elapsed": round(
            (session.finished_at or time.time()) - session.started_at, 1
        ),
    }

    if session.status in ("complete", "cancelled"):
        response["result"] = session.result
    elif session.status == "error":
        response["error"] = session.error

    return jsonify(response)


@bp.route("/api/chat/stream/<message_id>")
def stream_status(message_id):
    """SSE endpoint for real-time step and token-level updates."""
    _HEARTBEAT_INTERVAL = 15

    def generate():
        last_step_count = 0
        last_progress_count = 0
        last_token_count = 0
        approval_sent = False
        last_heartbeat = time.monotonic()

        while True:
            with _chat_lock:
                session = _chat_sessions.get(message_id)
            if not session:
                yield f"data: {json.dumps({'done': True, 'error': 'not found'})}\n\n"
                return

            sent_data = False

            new_progress = session.progress[last_progress_count:]
            if new_progress:
                for p in new_progress:
                    yield f"data: {json.dumps({'type': 'progress', 'progress': p})}\n\n"
                last_progress_count = len(session.progress)
                sent_data = True

            new_steps = session.steps[last_step_count:]
            if new_steps:
                for step in new_steps:
                    yield f"data: {json.dumps({'type': 'step', 'step': step})}\n\n"
                last_step_count = len(session.steps)
                last_token_count = len(session.token_chunks)
                sent_data = True

            cur_token_count = len(session.token_chunks)
            if cur_token_count > last_token_count:
                batch = "".join(session.token_chunks[last_token_count:cur_token_count])
                yield f"data: {json.dumps({'type': 'token', 'text': batch})}\n\n"
                last_token_count = cur_token_count
                sent_data = True

            if session.pending_approval and not approval_sent:
                yield f"data: {json.dumps({'type': 'approval_needed', 'approval': session.pending_approval})}\n\n"
                approval_sent = True
                sent_data = True

            if session.status in ("complete", "cancelled"):
                yield f"data: {json.dumps({'type': 'done', 'result': session.result, 'tools_used': session.tools_used, 'elapsed': round(session.finished_at - session.started_at, 1)})}\n\n"
                return
            elif session.status == "error":
                yield f"data: {json.dumps({'type': 'error', 'error': session.error})}\n\n"
                return

            now = time.monotonic()
            if not sent_data and (now - last_heartbeat) >= _HEARTBEAT_INTERVAL:
                elapsed = round(time.time() - session.started_at, 1)
                yield f": heartbeat {elapsed}s\n\n"
                last_heartbeat = now
            if sent_data:
                last_heartbeat = now

            time.sleep(0.1)

    return Response(generate(), content_type="text/event-stream; charset=utf-8",
                    headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no",
                             "Connection": "keep-alive"})


def _get_session_boundary():
    """Read the session boundary timestamp. Returns ISO string or None."""
    try:
        if SESSION_BOUNDARY_FILE.exists():
            data = json.loads(SESSION_BOUNDARY_FILE.read_text(encoding="utf-8"))
            return data.get("cleared_at")
    except Exception:
        log.warning("Failed to get session boundary", exc_info=True)
    return None


@bp.route("/api/chat/clear", methods=["POST"])
def chat_clear():
    """Set a session boundary — LLM history will only include entries after this point."""
    boundary = datetime.now().isoformat()
    SESSION_BOUNDARY_FILE.parent.mkdir(parents=True, exist_ok=True)
    SESSION_BOUNDARY_FILE.write_text(json.dumps({"cleared_at": boundary}), encoding="utf-8")
    return jsonify({"ok": True, "cleared_at": boundary})


@bp.route("/api/chat/history")
def chat_history():
    """Return recent chat entries from the feed."""
    from pathlib import Path
    GHOST_HOME = Path.home() / ".ghost"
    FEED_FILE = GHOST_HOME / "feed.json"

    if not FEED_FILE.exists():
        return jsonify({"messages": []})
    try:
        items = json.loads(FEED_FILE.read_text(encoding="utf-8"))
        boundary = _get_session_boundary()
        chat_items = [
            i for i in items
            if i.get("type") == "ask"
        ]
        if boundary:
            chat_items = [
                i for i in chat_items
                if (i.get("time", "") or "") >= boundary
            ]
        active_ids = set()
        with _chat_lock:
            for sid, sess in _chat_sessions.items():
                if sess.status == "processing":
                    active_ids.add(sess.id)

        for item in chat_items:
            if "source" in item:
                item["user_message"] = item.pop("source")
            result = item.pop("result", None)
            if result is not None:
                item["assistant_message"] = result
            else:
                status = item.get("status", "")
                mid = item.get("message_id", "")
                if status == "processing" and mid in active_ids:
                    item["assistant_message"] = ""
                    item["still_processing"] = True
                elif status == "processing":
                    item["assistant_message"] = "(Quinely was interrupted before completing this request)"
                else:
                    item["assistant_message"] = ""
        chat_items.reverse()
        return jsonify({"messages": chat_items[-50:]})
    except Exception:
        log.warning("Failed to load chat history", exc_info=True)
        return jsonify({"messages": []})


# ============================================================================
# Interrupt and Prompt Injection API Endpoints
# ============================================================================

from ghost_interrupt import GenerationRegistry

_reg = GenerationRegistry()


@bp.route("/api/chat/interrupt", methods=["POST"])
def interrupt_generation():
    """Interrupt/cancel an active generation by session_id."""
    try:
        data = request.get_json() or {}
        session_id = data.get("session_id")
        if not session_id:
            return jsonify({"ok": False, "error": "session_id is required"}), 400
        
        cancelled = _reg.cancel(session_id)
        if cancelled:
            return jsonify({"ok": True, "message": f"Generation {session_id} cancelled"})
        return jsonify({"ok": False, "error": "No active generation found"}), 404
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 500


@bp.route("/api/chat/inject", methods=["POST"])
def inject_prompt():
    """Inject a prompt into an active generation."""
    try:
        data = request.get_json() or {}
        session_id = data.get("session_id")
        text = data.get("text")
        
        if not session_id:
            return jsonify({"ok": False, "error": "session_id is required"}), 400
        if not text:
            return jsonify({"ok": False, "error": "text is required"}), 400
        
        inject_id = _reg.inject(session_id, text)
        if inject_id:
            return jsonify({"ok": True, "inject_id": inject_id})
        return jsonify({"ok": False, "error": "No active generation found"}), 404
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 500


@bp.route("/api/chat/generations", methods=["POST"])
def list_generations():
    """List all active generation session IDs."""
    try:
        active_ids = _reg.list_active()
        return jsonify({"ok": True, "generations": active_ids})
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 500


@bp.route("/api/chat/generations/<session_id>", methods=["POST"])
def get_generation(session_id):
    """Get the status of a specific generation."""
    try:
        gen = _reg.get(session_id)
        if not gen:
            return jsonify({"ok": False, "error": "Session not found"}), 404
        
        return jsonify({
            "ok": True,
            "session_id": gen.session_id,
            "state": gen.state.value,
            "model": gen.model,
            "provider_id": gen.provider_id,
            "elapsed": gen.get_elapsed(),
            "text_length": len(gen.accumulated_text),
            "is_active": gen.is_active,
            "started_at": gen.started_at,
            "finished_at": gen.finished_at,
            "error": gen.error
        })
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 500


# ============================================================================
# Artifacts API — deliverable files produced during chat
# ============================================================================

from ghost_artifacts import list_artifacts, ARTIFACTS_ROOT

_SAFE_SERVE_MIME = {
    ".png": "image/png", ".jpg": "image/jpeg", ".jpeg": "image/jpeg",
    ".gif": "image/gif", ".webp": "image/webp", ".bmp": "image/bmp",
    ".svg": "image/svg+xml",
    ".pdf": "application/pdf",
    ".csv": "text/csv",
    ".json": "application/json",
    ".xml": "application/xml",
    ".html": "text/html",
    ".txt": "text/plain",
    ".md": "text/markdown",
    ".mp3": "audio/mpeg", ".wav": "audio/wav", ".ogg": "audio/ogg",
    ".m4a": "audio/mp4", ".flac": "audio/flac",
    ".mp4": "video/mp4", ".webm": "video/webm",
    ".zip": "application/zip",
    ".py": "text/plain", ".js": "text/plain", ".ts": "text/plain",
    ".css": "text/plain", ".sql": "text/plain",
}


@bp.route("/api/chat/artifacts/<message_id>")
def get_artifacts(message_id):
    """List artifact files for a specific chat message."""
    if ".." in message_id or "/" in message_id or "\\" in message_id:
        return jsonify({"ok": False, "error": "Invalid message_id"}), 400
    items = list_artifacts(message_id)
    return jsonify({"ok": True, "message_id": message_id, "artifacts": items})


@bp.route("/api/chat/artifacts/<message_id>/<filename>")
def serve_artifact(message_id, filename):
    """Serve an artifact file for download/preview."""
    if ".." in message_id or "/" in message_id or "\\" in message_id:
        abort(400)
    if ".." in filename or "/" in filename or "\\" in filename:
        abort(400)

    artifact_dir = ARTIFACTS_ROOT / message_id
    if not artifact_dir.is_dir():
        abort(404)

    file_path = (artifact_dir / filename).resolve()
    if not file_path.is_relative_to(artifact_dir.resolve()):
        abort(403)
    if not file_path.exists():
        abort(404)

    ext = file_path.suffix.lower()
    mime = _SAFE_SERVE_MIME.get(ext, "application/octet-stream")

    return send_from_directory(str(artifact_dir), filename, mimetype=mime)


@bp.route("/api/chat/tools")
def list_chat_tools():
    """Return all registered tool names + descriptions for slash-command autocomplete."""
    from ghost_dashboard import get_daemon
    daemon = get_daemon()
    if not daemon or not daemon.tool_registry:
        return jsonify({"tools": []})
    tools = []
    for name, tool in daemon.tool_registry.get_all().items():
        tools.append({
            "name": name,
            "description": (tool.get("description") or "")[:120],
        })
    tools.sort(key=lambda t: t["name"])
    return jsonify({"tools": tools})
