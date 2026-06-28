"""Run tracing for Ghost — one trace per agent invocation.

Every agent run (chat message, cron job, channel message, monitor action)
gets a single ``run_id`` that ties together:

    trigger (source + message/job)  →
        model spans  (each LLM call: model, latency, tokens, success)  →
        tool spans   (each tool call: name, args, result, latency, success)  →
    outcome (final text, tools used, total tokens, status)

This is the missing correlation layer.  Before this module, model usage,
tool calls, console events and the activity feed lived in separate,
truncated, unlinked logs.  Now they share a run_id and a queryable store.

Design:
  * Module-level singleton ``get_tracer()`` (like ghost_usage / ghost_console)
    so routes and the loop can reach it without daemon plumbing.
  * Thread-local stack of active runs — safe for concurrent invocations and
    subagents (each thread gets its own run).  Nested engine.run() calls on
    the SAME thread (escalation / integrity retries) attach their spans to
    the SAME run, which is the desired behaviour.
  * Spans are recorded only when a run is active on the current thread, so
    out-of-band LLM calls (skill matching, validators, single_shot) are
    silently ignored.
  * Completed runs are kept in an in-memory ring buffer for fast listing and
    appended (one JSON line per run) to ~/.ghost/logs/runs.jsonl with simple
    size-based rotation.  Pure stdlib, cross-platform.
"""

from __future__ import annotations

import json
import threading
import time
import uuid
from collections import deque
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Optional

GHOST_HOME = Path.home() / ".ghost"
RUNS_LOG_DIR = GHOST_HOME / "logs"
RUNS_LOG_FILE = RUNS_LOG_DIR / "runs.jsonl"
MAX_RUNS_LOG_SIZE = 10 * 1024 * 1024  # 10 MB before rotation
_MAX_BACKUPS = 5

# How many completed runs to keep in memory for the dashboard.
_RING_SIZE = 300

# Truncation limits — keep traces useful without ballooning.
_ARGS_MAX = 400
_RESULT_PREVIEW = 800
_TRIGGER_MAX = 500
_OUTCOME_MAX = 800


def _now() -> float:
    return time.time()


def _truncate(s: Any, limit: int) -> str:
    if s is None:
        return ""
    if not isinstance(s, str):
        try:
            s = json.dumps(s, default=str)
        except Exception:
            s = str(s)
    if len(s) > limit:
        return s[:limit] + f"...(+{len(s) - limit} chars)"
    return s


# Markers for context the entry points append to the user message (RAG, etc.).
# Stripped from the trace trigger so it shows the user's actual request.
_AUGMENT_MARKERS = (
    "\n\n## Relevant memory (auto-retrieved)",
    "\n\n## Relevant memory",
    "\n\n## ATTACHED FILES",
)


def _clean_trigger(user_message: str) -> str:
    msg = user_message or ""
    cut = len(msg)
    for marker in _AUGMENT_MARKERS:
        idx = msg.find(marker)
        if idx != -1:
            cut = min(cut, idx)
    return msg[:cut].strip()


def _summarize_args(args: dict) -> str:
    if not args:
        return "{}"
    summary = {}
    for k, v in args.items():
        if isinstance(v, str) and len(v) > 120:
            summary[k] = v[:100] + f"...({len(v)} chars)"
        elif isinstance(v, (list, dict)):
            s = json.dumps(v, default=str)
            summary[k] = s[:120] + "..." if len(s) > 120 else s
        else:
            summary[k] = v
    return _truncate(summary, _ARGS_MAX)


@dataclass
class Span:
    """A single model call or tool call inside a run."""

    kind: str                      # "model" | "tool"
    name: str = ""                 # model id or tool name
    step: int = 0
    started_at: float = field(default_factory=_now)
    duration_ms: float = 0.0
    ok: bool = True
    # model-specific
    prompt_tokens: int = 0
    completion_tokens: int = 0
    total_tokens: int = 0
    # tool-specific
    args_summary: str = ""
    result_preview: str = ""
    result_length: int = 0
    # shared
    error: str = ""

    def to_dict(self) -> dict:
        d = {
            "kind": self.kind,
            "name": self.name,
            "step": self.step,
            "started_at": self.started_at,
            "duration_ms": round(self.duration_ms),
            "ok": self.ok,
        }
        if self.kind == "model":
            d.update({
                "prompt_tokens": self.prompt_tokens,
                "completion_tokens": self.completion_tokens,
                "total_tokens": self.total_tokens,
            })
        elif self.kind == "tool":
            d.update({
                "args_summary": self.args_summary,
                "result_preview": self.result_preview,
                "result_length": self.result_length,
            })
        if self.error:
            d["error"] = self.error
        return d


@dataclass
class Run:
    """A single agent invocation."""

    run_id: str
    source: str = ""               # chat | cron | channel | monitor | action
    trigger: str = ""              # user message preview or job name
    job_name: str = ""             # cron job name (if any)
    caller_context: str = ""
    started_at: float = field(default_factory=_now)
    ended_at: Optional[float] = None
    status: str = "running"        # running | ok | error | cancelled
    session_id: str = ""           # links to tool_loop_debug.jsonl
    model: str = ""
    spans: list[Span] = field(default_factory=list)
    tools_used: list[str] = field(default_factory=list)
    total_tokens: int = 0
    escalation_count: int = 0
    result_preview: str = ""
    error: str = ""
    meta: dict = field(default_factory=dict)
    _lock: threading.Lock = field(default_factory=threading.Lock, repr=False)

    @property
    def duration_ms(self) -> float:
        end = self.ended_at if self.ended_at is not None else _now()
        return (end - self.started_at) * 1000.0

    @property
    def num_model_calls(self) -> int:
        return sum(1 for s in self.spans if s.kind == "model")

    @property
    def num_tool_calls(self) -> int:
        return sum(1 for s in self.spans if s.kind == "tool")

    def to_summary(self) -> dict:
        """Lightweight record for list views (no span detail)."""
        return {
            "run_id": self.run_id,
            "source": self.source,
            "trigger": self.trigger,
            "job_name": self.job_name,
            "status": self.status,
            "started_at": self.started_at,
            "ended_at": self.ended_at,
            "duration_ms": round(self.duration_ms),
            "session_id": self.session_id,
            "model": self.model,
            "num_model_calls": self.num_model_calls,
            "num_tool_calls": self.num_tool_calls,
            "tools_used": list(dict.fromkeys(self.tools_used)),
            "total_tokens": self.total_tokens,
            "escalation_count": self.escalation_count,
            "result_preview": self.result_preview,
            "error": self.error,
        }

    def to_dict(self) -> dict:
        """Full record including spans."""
        d = self.to_summary()
        d["caller_context"] = self.caller_context
        d["meta"] = self.meta
        d["spans"] = [s.to_dict() for s in self.spans]
        return d


class Tracer:
    """Thread-safe run tracer with file-backed history."""

    def __init__(self):
        self._local = threading.local()
        self._lock = threading.Lock()
        self._recent: deque[Run] = deque(maxlen=_RING_SIZE)
        self._active: dict[str, Run] = {}

    # -- thread-local active-run stack ------------------------------------

    @property
    def _stack(self) -> list[Run]:
        st = getattr(self._local, "stack", None)
        if st is None:
            st = []
            self._local.stack = st
        return st

    @property
    def _current(self) -> Optional[Run]:
        st = self._stack
        return st[-1] if st else None

    @property
    def current_run_id(self) -> str:
        run = self._current
        return run.run_id if run else ""

    # -- lifecycle --------------------------------------------------------

    def start_run(self, source: str, user_message: str = "",
                  meta: Optional[dict] = None,
                  caller_context: str = "") -> str:
        meta = meta or {}
        job_name = meta.get("job_name", "") or ""
        trigger = job_name or _truncate(_clean_trigger(user_message), _TRIGGER_MAX)
        # Copy only JSON-safe, useful metadata (never sessions/engines).
        safe_meta = {}
        for k in ("job_name", "content_type", "channel", "is_evolution_runner",
                  "feature_id"):
            if k in meta and isinstance(meta[k], (str, int, float, bool)):
                safe_meta[k] = meta[k]
        run = Run(
            run_id=uuid.uuid4().hex[:12],
            source=source or "",
            trigger=trigger,
            job_name=job_name,
            caller_context=caller_context or "",
            meta=safe_meta,
        )
        self._stack.append(run)
        with self._lock:
            self._active[run.run_id] = run
        return run.run_id

    def attach_session(self, session_id: str, model: str = "") -> None:
        """Link the active run to its tool_loop_debug session + model."""
        run = self._current
        if not run:
            return
        with run._lock:
            if session_id:
                run.session_id = session_id
            if model and not run.model:
                run.model = model

    def set_exit_reason(self, exit_reason: str) -> None:
        """Record the engine loop's exit reason on the active run."""
        run = self._current
        if not run or not exit_reason:
            return
        with run._lock:
            run.meta["exit_reason"] = exit_reason

    def add_model_span(self, step: int, model: str, duration_ms: float,
                       prompt_tokens: int = 0, completion_tokens: int = 0,
                       total_tokens: int = 0, ok: bool = True,
                       error: str = "") -> None:
        run = self._current
        if not run:
            return
        span = Span(
            kind="model", name=model or run.model, step=step,
            duration_ms=duration_ms, ok=ok,
            prompt_tokens=prompt_tokens, completion_tokens=completion_tokens,
            total_tokens=total_tokens, error=_truncate(error, 300),
        )
        with run._lock:
            run.spans.append(span)
            if model and not run.model:
                run.model = model

    def add_tool_span(self, step: int, tool_name: str, args: dict,
                      result: str, duration_ms: float = 0.0,
                      ok: bool = True) -> None:
        run = self._current
        if not run:
            return
        result = result or ""
        is_err = (not ok) or result.startswith((
            "Tool error", "Tool execution failed", "BLOCKED", "Error:",
        ))
        span = Span(
            kind="tool", name=tool_name, step=step,
            duration_ms=duration_ms, ok=not is_err,
            args_summary=_summarize_args(args),
            result_preview=_truncate(result, _RESULT_PREVIEW),
            result_length=len(result),
            error=_truncate(result, 300) if is_err else "",
        )
        with run._lock:
            run.spans.append(span)
            run.tools_used.append(tool_name)

    def end_run(self, run_id: str = "", status: str = "ok",
                result_text: str = "", tools_used: Optional[list] = None,
                total_tokens: int = 0, escalation_count: int = 0,
                error: str = "") -> Optional[dict]:
        st = self._stack
        run = None
        if run_id:
            for i in range(len(st) - 1, -1, -1):
                if st[i].run_id == run_id:
                    run = st.pop(i)
                    break
        if run is None and st:
            run = st.pop()
        if run is None:
            return None

        with run._lock:
            run.ended_at = _now()
            exit_reason = run.meta.get("exit_reason", "")
            # Derive final status: explicit error > exit reason > caller status.
            if error:
                final = "error"
            elif exit_reason == "cancelled":
                final = "cancelled"
            elif exit_reason in (
                "llm_error", "context_overflow", "interpreter_shutdown",
            ):
                final = "error"
            elif status and status != "ok":
                final = status
            else:
                final = "ok"
            run.status = final
            if result_text:
                run.result_preview = _truncate(result_text, _OUTCOME_MAX)
            if tools_used:
                run.tools_used = list(tools_used)
            if total_tokens:
                run.total_tokens = total_tokens
            elif not run.total_tokens:
                run.total_tokens = sum(
                    s.total_tokens for s in run.spans if s.kind == "model"
                )
            run.escalation_count = escalation_count
            if error:
                run.error = _truncate(error, 500)
            snapshot = run.to_dict()

        with self._lock:
            self._active.pop(run.run_id, None)
            self._recent.append(run)
        self._persist(snapshot)
        return snapshot

    # -- queries ----------------------------------------------------------

    def list_runs(self, limit: int = 50, source: str = "",
                  status: str = "") -> list[dict]:
        with self._lock:
            runs = list(self._recent)
            active = list(self._active.values())
        # active first (running), then most-recent completed
        combined = active + list(reversed(runs))
        out = []
        seen = set()
        for r in combined:
            if r.run_id in seen:
                continue
            seen.add(r.run_id)
            if source and r.source != source:
                continue
            if status and r.status != status:
                continue
            out.append(r.to_summary())
            if len(out) >= limit:
                break
        return out

    def get_run(self, run_id: str) -> Optional[dict]:
        with self._lock:
            if run_id in self._active:
                return self._active[run_id].to_dict()
            for r in self._recent:
                if r.run_id == run_id:
                    return r.to_dict()
        return self._read_run_from_file(run_id)

    def stats(self) -> dict:
        with self._lock:
            runs = list(self._recent)
            active = len(self._active)
        total = len(runs)
        ok = sum(1 for r in runs if r.status == "ok")
        errors = sum(1 for r in runs if r.status == "error")
        tokens = sum(r.total_tokens for r in runs)
        model_calls = sum(r.num_model_calls for r in runs)
        tool_calls = sum(r.num_tool_calls for r in runs)
        durations = [r.duration_ms for r in runs if r.ended_at]
        avg_ms = round(sum(durations) / len(durations)) if durations else 0
        by_source: dict[str, int] = {}
        for r in runs:
            by_source[r.source] = by_source.get(r.source, 0) + 1
        return {
            "active": active,
            "recent_total": total,
            "ok": ok,
            "errors": errors,
            "total_tokens": tokens,
            "model_calls": model_calls,
            "tool_calls": tool_calls,
            "avg_duration_ms": avg_ms,
            "by_source": by_source,
        }

    # -- persistence ------------------------------------------------------

    def _persist(self, snapshot: dict) -> None:
        try:
            RUNS_LOG_DIR.mkdir(parents=True, exist_ok=True)
            self._rotate_if_needed()
            with open(str(RUNS_LOG_FILE), "a", encoding="utf-8") as f:
                f.write(json.dumps(snapshot, default=str) + "\n")
        except Exception:
            pass

    def _rotate_if_needed(self) -> None:
        try:
            if (RUNS_LOG_FILE.exists()
                    and RUNS_LOG_FILE.stat().st_size > MAX_RUNS_LOG_SIZE):
                for i in range(_MAX_BACKUPS - 1, 0, -1):
                    old = RUNS_LOG_DIR / f"runs.jsonl.{i}"
                    new = RUNS_LOG_DIR / f"runs.jsonl.{i + 1}"
                    if old.exists():
                        old.replace(new)
                RUNS_LOG_FILE.replace(RUNS_LOG_DIR / "runs.jsonl.1")
        except Exception:
            pass

    def _read_run_from_file(self, run_id: str) -> Optional[dict]:
        """Fallback: scan the JSONL log for an older run (newest first)."""
        files = [RUNS_LOG_FILE] + [
            RUNS_LOG_DIR / f"runs.jsonl.{i}" for i in range(1, _MAX_BACKUPS + 1)
        ]
        for path in files:
            try:
                if not path.exists():
                    continue
                with open(str(path), "r", encoding="utf-8") as f:
                    lines = f.readlines()
                for line in reversed(lines):
                    line = line.strip()
                    if not line or run_id not in line:
                        continue
                    try:
                        rec = json.loads(line)
                    except Exception:
                        continue
                    if rec.get("run_id") == run_id:
                        return rec
            except Exception:
                continue
        return None


_tracer: Optional[Tracer] = None
_tracer_lock = threading.Lock()


def get_tracer() -> Tracer:
    global _tracer
    if _tracer is None:
        with _tracer_lock:
            if _tracer is None:
                _tracer = Tracer()
    return _tracer
