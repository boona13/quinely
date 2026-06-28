"""Status API — daemon state, stats, platform info.

When running embedded in the daemon, reads live in-memory state.
When running standalone, reads from PID file and log files on disk.
"""

import logging
import os, json, platform
from datetime import datetime
from pathlib import Path
from flask import Blueprint, jsonify

log = logging.getLogger(__name__)

import sys
sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent))

from ghost import (
    GHOST_HOME, PID_FILE, LOG_FILE, FEED_FILE, PAUSE_FILE,
    SOUL_FILE, USER_FILE, CONFIG_FILE, load_config, DEFAULT_CONFIG,
)

bp = Blueprint("status", __name__)


def _secret_status():
    """Encryption-at-rest status for local secrets (best-effort)."""
    try:
        from ghost_secret_store import status as _ss
        s = _ss() or {}
        return {
            "encrypted": bool(s.get("available")),
            "reason": s.get("reason", ""),
        }
    except Exception:
        return {"encrypted": False, "reason": "unavailable"}


def _daemon_running():
    if not PID_FILE.exists():
        return False, None
    try:
        pid = int(PID_FILE.read_text(encoding="utf-8").strip())
        os.kill(pid, 0)
        return True, pid
    except (ValueError, ProcessLookupError, PermissionError):
        return False, None


@bp.route("/api/status")       # Legacy route (backward compat)
@bp.route("/api/ghost/status")  # Primary route
def get_status():
    from ghost_dashboard import get_daemon
    daemon = get_daemon()

    running, pid = _daemon_running()
    paused = PAUSE_FILE.exists()

    if daemon:
        running = daemon.running
        pid = os.getpid()
        today_count = daemon.actions_today
        uptime_secs = int((datetime.now() - daemon.start_time).total_seconds())
        cfg = daemon.cfg
        model = cfg.get("model", DEFAULT_CONFIG["model"])
        if hasattr(daemon, 'engine') and hasattr(daemon.engine, 'fallback_chain'):
            fc = daemon.engine.fallback_chain
            model = f"{fc.active_provider}:{fc.active_model}"

        tool_count = len(daemon.tool_registry.names()) if daemon.tool_registry else 0
        tool_names = list(daemon.tool_registry.names()) if daemon.tool_registry else []
        skill_count = len(daemon.skill_loader.list_all()) if daemon.skill_loader else 0
        memory_count = daemon.memory_db.count() if daemon.memory_db else 0
        cron_jobs = daemon.cron.list_jobs() if daemon.cron else []
        cron_enabled = sum(1 for j in cron_jobs if j.get("enabled"))

        entries = []
        if LOG_FILE.exists():
            try:
                entries = json.loads(LOG_FILE.read_text(encoding="utf-8"))
            except Exception:
                log.warning("Failed to load log entries", exc_info=True)

        types = {}
        for e in entries:
            t = e.get("type", "unknown")
            types[t] = types.get(t, 0) + 1

        session_tokens = 0
        calls_this_session = 0
        try:
            from ghost_usage import get_usage_tracker
            snap = get_usage_tracker().get_snapshot()
            session_tokens = snap.session_tokens
            calls_this_session = snap.calls_this_session
        except Exception:
            pass

        guard_stats = {}
        try:
            from ghost_output_guard import get_guard_stats
            guard_stats = get_guard_stats()
        except Exception:
            pass

        repair_stats = {}
        try:
            from ghost_message_repair import get_repair_stats
            repair_stats = get_repair_stats()
        except Exception:
            pass

        return jsonify({
            "running": running,
            "embedded": True,
            "paused": paused,
            "pid": pid,
            "platform": platform.system(),
            "uptime_seconds": uptime_secs,
            "total_actions": len(entries),
            "today_actions": today_count,
            "type_breakdown": types,
            "model": model,
            "primary_provider": cfg.get("primary_provider", ""),
            "session_tokens": session_tokens,
            "calls_this_session": calls_this_session,
            "features": {
                "tool_loop": cfg.get("enable_tool_loop", True),
                "memory": cfg.get("enable_memory_db", True),
                "skills": cfg.get("enable_skills", True),
                "plugins": cfg.get("enable_plugins", True),
                "browser": cfg.get("enable_browser_tools", True),
                "cron": cfg.get("enable_cron", True),
                "vision": cfg.get("enable_vision", True),
                "tts": cfg.get("enable_tts", True),
                "security_audit": cfg.get("enable_security_audit", True),
                "session_memory": cfg.get("enable_session_memory", True),
                "nodes": cfg.get("enable_nodes", True),
            },
            "live": {
                "tools": tool_count,
                "tool_names": tool_names,
                "skills": skill_count,
                "memory_entries": memory_count,
                "cron_jobs": len(cron_jobs),
                "cron_enabled": cron_enabled,
            },
            "soul_exists": SOUL_FILE.exists(),
            "user_exists": USER_FILE.exists(),
            "secrets": _secret_status(),
            "safety": {
                "guard": guard_stats,
                "repair": repair_stats,
            },
        })

    # Standalone mode — read from files
    entries = []
    if LOG_FILE.exists():
        try:
            entries = json.loads(LOG_FILE.read_text(encoding="utf-8"))
        except Exception:
            log.warning("Failed to load log entries (standalone)", exc_info=True)

    today_str = datetime.now().strftime("%Y-%m-%d")
    today_count = sum(1 for e in entries if e.get("time", "")[:10] == today_str)

    types = {}
    for e in entries:
        t = e.get("type", "unknown")
        types[t] = types.get(t, 0) + 1

    cfg = load_config()

    return jsonify({
        "running": running,
        "embedded": False,
        "paused": paused,
        "pid": pid,
        "platform": platform.system(),
        "total_actions": len(entries),
        "today_actions": today_count,
        "type_breakdown": types,
        "model": cfg.get("model", DEFAULT_CONFIG["model"]),
        "features": {
            "tool_loop": cfg.get("enable_tool_loop", True),
            "memory": cfg.get("enable_memory_db", True),
            "skills": cfg.get("enable_skills", True),
            "plugins": cfg.get("enable_plugins", True),
            "browser": cfg.get("enable_browser_tools", True),
            "cron": cfg.get("enable_cron", True),
            "vision": cfg.get("enable_vision", True),
            "tts": cfg.get("enable_tts", True),
            "security_audit": cfg.get("enable_security_audit", True),
            "session_memory": cfg.get("enable_session_memory", True),
            "nodes": cfg.get("enable_nodes", True),
        },
        "soul_exists": SOUL_FILE.exists(),
        "user_exists": USER_FILE.exists(),
    })
