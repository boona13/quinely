"""Security API — AI-driven audit + deterministic quick scan + auto-fix."""

import json
import threading
import time
import uuid
from datetime import datetime
from flask import Blueprint, jsonify, request, Response
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent))

from ghost_security_audit import run_security_audit, auto_fix

# Import the key posture analyzer
from ghost_api_key_posture import analyze_provider_key_posture

bp = Blueprint("security", __name__)

_audit_sessions = {}
_audit_lock = threading.Lock()

SECURITY_AUDIT_PROMPT = (
    "You are Quinely performing a COMPREHENSIVE SECURITY AUDIT.\n\n"

    "## MANDATORY: ALL FIXES GO THROUGH THE EVOLVE LOOP\n"
    "This is the ONLY acceptable sequence for making ANY change:\n\n"
    "  1. `evolve_plan(description='Security hardening', files=[])` — creates rollback checkpoint\n"
    "  2. `evolve_apply_config(evolution_id, updates={...})` — for config changes\n"
    "     `evolve_apply(evolution_id, file_path, patches=[...])` — for code changes\n"
    "  3. `evolve_test(evolution_id)` — validates changes\n"
    "  4. `evolve_deploy(evolution_id)` — deploys and restarts Quinely\n\n"
    "FORBIDDEN TOOLS FOR MODIFICATION:\n"
    "- `config_patch` — NEVER use this. Use `evolve_apply_config` instead.\n"
    "- `shell_exec` for writes — NEVER chmod, rm, or modify files. Read-only ONLY.\n"
    "- `security_fix` — NEVER use this. Report permission issues via `add_action_item`.\n"
    "- `file_write` — NEVER use this. Use `evolve_apply` instead.\n\n"
    "If you use any forbidden tool, the audit is INVALID.\n\n"

    "## EXACT PROCEDURE (follow this step-by-step):\n\n"

    "### Step 1: Scan (read-only)\n"
    "- Call `security_audit` for baseline findings\n"
    "- Call `config_get` to review runtime config\n"
    "- Call `shell_exec` with read-only commands: `ls -la ~/.ghost/`, `ps aux | grep ghost`\n\n"

    "### Step 2: Plan the evolution\n"
    "Call `evolve_plan` with:\n"
    "- description: 'Security audit hardening — config + permissions'\n"
    "- files: [] (empty list if only config changes, or list source files if code fixes needed)\n"
    "This creates a FULL backup (project + config) as rollback checkpoint.\n\n"

    "### Step 3: Apply fixes using the evolution_id\n"
    "Config hardening example:\n"
    "```\n"
    "evolve_apply_config(evolution_id='abc123', updates={\n"
    "  'strict_tool_registration': true\n"
    "})\n"
    "```\n"
    "NOTE: Do NOT touch `evolve_auto_approve` — that is a user autonomy preference, not a security issue.\n"
    "Code fixes example:\n"
    "```\n"
    "evolve_apply(evolution_id='abc123', file_path='ghost_tools.py', patches=[...])\n"
    "```\n\n"

    "### Step 4: Test\n"
    "Call `evolve_test(evolution_id)`. Must PASS.\n\n"

    "### Step 5: Deploy\n"
    "Call `evolve_deploy(evolution_id)`. Quinely restarts with hardened config.\n\n"

    "### Step 6: Report\n"
    "Use `task_complete` with a report containing:\n"
    "- Evolution ID and backup path\n"
    "- Each finding and how you fixed it\n"
    "- Things only the user can fix (e.g. rotate API keys externally)\n"
)


class AuditSession:
    """Tracks an AI-driven security audit."""
    def __init__(self, session_id):
        self.id = session_id
        self.status = "pending"
        self.steps = []
        self.result = None
        self.error = None
        self.started_at = time.time()
        self.finished_at = None
        self.tools_used = []
        self.pending_approval = None
        self.cancelled = False


def _run_ai_audit(session, daemon):
    """Run the AI-driven security audit through Quinely's tool loop."""
    try:
        session.status = "processing"
        tool_names = daemon.tool_registry.names() if daemon.tool_registry else []
        PROJECT_DIR = Path(__file__).resolve().parent.parent.parent

        system_prompt = (
            "You are Quinely, an AUTONOMOUS AI agent running LOCALLY.\n"
            f"Quinely project root: **{PROJECT_DIR}**\n"
            f"Available tools: {', '.join(tool_names)}\n\n"
            "CRITICAL: When done, call `task_complete(summary='...')` with your full report.\n\n"
        )

        def on_step(step_num, tool_name, tool_result):
            result_str = str(tool_result)[:500]
            session.steps.append({
                "step": step_num,
                "tool": tool_name,
                "result": result_str,
                "time": datetime.now().isoformat(),
            })

        loop_result = daemon.engine.run(
            system_prompt=system_prompt,
            user_message=SECURITY_AUDIT_PROMPT,
            tool_registry=daemon.tool_registry,
            max_steps=daemon.cfg.get("tool_loop_max_steps", 200),
            max_tokens=8192,
            force_tool=False,
            on_step=on_step,
            cancel_check=lambda: "(Stopped by user)" if session.cancelled else False,
            tool_event_bus=getattr(daemon, "tool_event_bus", None),
        )
        session.result = loop_result.text
        session.tools_used = [tc["tool"] for tc in loop_result.tool_calls]
        session.status = "complete"
        session.finished_at = time.time()
    except Exception as e:
        session.status = "error"
        session.error = str(e)
        session.finished_at = time.time()


@bp.route("/api/security/ai-audit", methods=["POST"])
def start_ai_audit():
    """Start an AI-driven security audit through Quinely's tool loop."""
    from ghost_dashboard import get_daemon
    daemon = get_daemon()
    if not daemon:
        return jsonify({"ok": False, "error": "Quinely daemon not running"}), 503

    session_id = f"sec_{uuid.uuid4().hex[:10]}"
    session = AuditSession(session_id)

    with _audit_lock:
        _audit_sessions[session_id] = session
        if len(_audit_sessions) > 10:
            oldest = sorted(_audit_sessions.keys(),
                            key=lambda k: _audit_sessions[k].started_at)
            for k in oldest[:5]:
                _audit_sessions.pop(k, None)

    t = threading.Thread(target=_run_ai_audit, args=(session, daemon), daemon=True)
    t.start()

    return jsonify({"ok": True, "session_id": session_id})


@bp.route("/api/security/ai-audit/stream/<session_id>")
def stream_ai_audit(session_id):
    """SSE endpoint for streaming AI audit progress."""
    def generate():
        last_step_count = 0
        while True:
            with _audit_lock:
                session = _audit_sessions.get(session_id)
            if not session:
                yield f"data: {json.dumps({'done': True, 'error': 'not found'})}\n\n"
                return

            new_steps = session.steps[last_step_count:]
            if new_steps:
                for step in new_steps:
                    yield f"data: {json.dumps({'type': 'step', 'step': step})}\n\n"
                last_step_count = len(session.steps)

            if session.status == "complete":
                yield f"data: {json.dumps({'type': 'done', 'result': session.result, 'tools_used': session.tools_used, 'elapsed': round(session.finished_at - session.started_at, 1)})}\n\n"
                return
            elif session.status == "error":
                yield f"data: {json.dumps({'type': 'error', 'error': session.error})}\n\n"
                return

            time.sleep(0.5)

    return Response(generate(), mimetype="text/event-stream",
                    headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})


@bp.route("/api/security/ai-audit/stop/<session_id>", methods=["POST"])
def stop_ai_audit(session_id):
    """Cancel an in-progress AI audit."""
    with _audit_lock:
        session = _audit_sessions.get(session_id)
    if not session:
        return jsonify({"ok": False, "error": "Session not found"}), 404
    session.cancelled = True
    return jsonify({"ok": True})


@bp.route("/api/security/audit", methods=["POST"])
def run_audit():
    """Quick deterministic scan (no AI) — kept as a fast baseline check."""
    data = request.get_json(silent=True) or {}
    categories = data.get("categories")
    report = run_security_audit(categories=categories)
    return jsonify(report)


@bp.route("/api/security/fix", methods=["POST"])
def fix_issues():
    report = run_security_audit()
    fixable = [f for f in report["findings"]
               if f["severity"] in ("critical", "warning")
               and f["category"] == "filesystem"]
    if not fixable:
        return jsonify({"actions": [], "message": "No auto-fixable issues found"})
    actions = auto_fix(fixable)
    return jsonify({"actions": actions, "remaining": report["summary"]})


@bp.route("/api/security/key-posture", methods=["POST"])
def get_key_posture():
    """Get provider API key posture analysis.
    
    Analyzes key scope drift, key hygiene issues, and cross-provider key reuse
    without exposing actual key material.
    """
    from flask import current_app
    
    daemon = current_app.daemon if hasattr(current_app, 'daemon') else None
    if not daemon or not hasattr(daemon, 'auth_store'):
        # Return empty posture if auth store unavailable
        return jsonify({
            "posture": "green",
            "finding_count": 0,
            "findings": [],
            "summary": {"critical": 0, "warning": 0, "info": 0},
            "error": "Auth store not available"
        }), 503
    
    # Get config from daemon if available
    cfg = daemon.cfg if hasattr(daemon, 'cfg') else {}
    auth_store = daemon.auth_store
    
    try:
        result = analyze_provider_key_posture(auth_store, cfg)
        response = jsonify(result)
        response.headers.add('Access-Control-Allow-Origin', '*')
        return response
    except Exception as e:
        return jsonify({
            "posture": "unknown",
            "finding_count": 0,
            "findings": [],
            "summary": {"critical": 0, "warning": 0, "info": 0},
            "error": str(e)
        }), 500
