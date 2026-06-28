"""
GHOST Tool Loop Engine

Autonomous multi-turn LLM tool calling loop.
The agent keeps going until it decides the task is DONE — like while(true).
"""

import concurrent.futures
import json
import logging
import os
import random
import re as _re
import threading
import time
import hashlib
import uuid
import requests
import sys
import traceback
from ghost_tool_intent_security import ToolIntentSecurity
from ghost_config_tool import _load_config
from ghost_tools import get_shell_caller_context, set_shell_caller_context
from ghost_message_repair import repair_dangling_tool_calls
from ghost_output_guard import guard_model_output
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

try:
    from ghost_providers import get_provider, adapt_response, parse_codex_sse_response, build_headers, adapt_request
except ImportError:
    get_provider = adapt_response = parse_codex_sse_response = build_headers = adapt_request = None

log = logging.getLogger("ghost.loop")


def _build_date_context() -> str:
    """Dynamic date/time context injected into every LLM call.

    Most LLMs are trained on prior-year data and assume it's 2025.
    This corrects that by prepending the actual current date.
    """
    now = datetime.now()
    utc = datetime.now(timezone.utc)
    return (
        f"## CURRENT DATE & TIME\n"
        f"Today is **{now.strftime('%A, %B %d, %Y')}** "
        f"(ISO: {now.strftime('%Y-%m-%d')}, {now.strftime('%I:%M %p')} local, "
        f"{utc.strftime('%H:%M')} UTC). "
        f"The current year is **{now.year}**, NOT {now.year - 1}. "
        f"Use {now.year} in all searches, dates, and references.\n\n"
    )

MAX_RETRIES = 2
RATE_LIMIT_MAX_RETRIES = 1   # try once more then move to fallback — don't waste minutes
RETRY_DELAY = 1.5
RATE_LIMIT_BASE_DELAY = 3.0  # short delay before switching to fallback
DEFAULT_MAX_TOKENS = 4096
DEFAULT_TOOL_RESULT_LIMIT = 6000
DEFAULT_TIMEOUT = 30
MAX_LLM_WALL_CLOCK = 180     # generous deadline — streaming keeps connection alive
DEFAULT_TOOL_TIMEOUT = 300   # hard timeout for any single tool execution (seconds)
DEFAULT_MAX_STEPS = 200
FALLBACK_COOLDOWN_SEC = 60    # base cooldown — escalates with consecutive failures
FALLBACK_PROBE_INTERVAL = 30  # probe every 30s
NETWORK_COOLDOWN_SEC = 10     # short cooldown for DNS/connection errors (transient)
JITTER_FACTOR = 0.3           # ±30% randomness on retry delays

GHOST_HOME = Path.home() / ".ghost"

_DEFERRAL_RE = _re.compile(
    r"(?:if you (?:want|like|prefer|need),? I can|would you like me to|let me know"
    r"|I couldn'?t (?:reliably|find|access|extract|get|fetch|retrieve|determine)"
    r"|I wasn'?t able to|I don'?t have (?:access|enough)"
    r"|unable to (?:find|access|extract|fetch|retrieve|get)"
    r"|I can (?:try|do) (?:a |that |this )?(?:next|instead|as a follow)"
    r"|I can do a (?:browser|deeper|second|follow)"
    r"|I'?m going to (?:do|try|use|run|extract|scrape|handle|approach|switch|take|need)"
    r"|I'?ll (?:do|try|use|run|handle|take|need to) (?:this|it|a )"
    r"|let me (?:do|try|handle|approach) this (?:properly|correctly|right|differently)"
    r"|I (?:will|need to) (?:do|try) (?:this|it) (?:properly|correctly|differently))",
    _re.IGNORECASE,
)
_INCOMPLETE_TASK_RE = _re.compile(
    r"(?:not yet (?:delivered|complete|finished|verified|done|placed)"
    r"|haven'?t (?:finished|completed|verified|placed|done|delivered)"
    r"|still (?:need|needs|working|trying|attempting|in progress)"
    r"|not (?:fully )?(?:complete|done|finished|verified)"
    r"|couldn'?t (?:click|select|find|interact|complete|place|submit|open|close)"
    r"|failed to (?:click|select|find|interact|complete|place|submit)"
    r"|dialog (?:is )?still (?:open|showing|visible|present)"
    r"|page (?:didn'?t|did not) (?:change|update|navigate|respond)"
    r"|button (?:didn'?t|did not) (?:work|respond|activate|click)"
    r"|I (?:was|am) unable to (?:click|complete|select|place|submit|interact))",
    _re.IGNORECASE,
)
_MIN_TOOLS_BEFORE_ACCEPT_DEFERRAL = 4
_MIN_BROWSER_STEPS = 8

# ═════════════════════════════════════════════════════════════════════
#  MODEL FALLBACK CHAIN
# ═════════════════════════════════════════════════════════════════════

class ModelFallbackChain:
    """Provider-aware model fallback with escalating cooldowns and probing.

    - Escalating cooldowns: 60s → 300s → 1500s → 3600s (based on error count)
    - Probe during cooldown every 30s to detect recovery
    - Error counts reset on success (circuit breaker half-open → closed)
    """

    _COOLDOWN_ESCALATION = (60, 300, 1500, 3600)  # 1m → 5m → 25m → 1h max

    def __init__(self, primary: str, fallbacks: list[str] | None = None,
                 cooldown_sec: float = FALLBACK_COOLDOWN_SEC,
                 probe_interval: float = FALLBACK_PROBE_INTERVAL,
                 provider_chain: list[tuple[str, str]] | None = None):
        if provider_chain:
            self._chain: list[tuple[str, str]] = list(provider_chain)
        else:
            self._chain = [("openrouter", m) for m in [primary] + (fallbacks or [])]
        self._cooldown_sec = cooldown_sec
        self._probe_interval = probe_interval
        self._failures: dict[str, float] = {}       # key → timestamp of last failure
        self._error_counts: dict[str, int] = {}      # key → consecutive error count
        self._last_probe: dict[str, float] = {}
        self._active: tuple[str, str] = self._chain[0] if self._chain else ("openrouter", primary)
        self._stats: dict[str, dict] = {self._key(e): {"ok": 0, "fail": 0} for e in self._chain}

    @staticmethod
    def _key(entry: tuple[str, str]) -> str:
        return f"{entry[0]}:{entry[1]}"

    @property
    def primary(self) -> str:
        return self._chain[0][1] if self._chain else ""

    @primary.setter
    def primary(self, model: str):
        new_entry = ("openrouter", model)
        for i, e in enumerate(self._chain):
            if e[0] == "openrouter" and e[1] == model:
                self._chain.pop(i)
                break
        self._chain.insert(0, new_entry)
        k = self._key(new_entry)
        if k not in self._stats:
            self._stats[k] = {"ok": 0, "fail": 0}
        self._active = new_entry
        self._failures.pop(k, None)

    @property
    def active_model(self) -> str:
        return self._active[1]

    @property
    def active_provider(self) -> str:
        return self._active[0]

    @property
    def chain(self) -> list[str]:
        return [e[1] for e in self._chain]

    @property
    def provider_chain(self) -> list[tuple[str, str]]:
        return list(self._chain)

    def set_provider_chain(self, chain: list[tuple[str, str]]):
        self._chain = list(chain)
        if chain:
            self._active = chain[0]
        for e in chain:
            k = self._key(e)
            if k not in self._stats:
                self._stats[k] = {"ok": 0, "fail": 0}

    @property
    def stats(self) -> dict:
        return {
            "active": f"{self._active[0]}:{self._active[1]}",
            "chain": [f"{e[0]}:{e[1]}" for e in self._chain],
            "failures": {k: round(time.time() - t) for k, t in self._failures.items()},
            "cooldowns": {k: int(self._effective_cooldown(k)) for k in self._failures},
            "error_counts": dict(self._error_counts),
            "stats": dict(self._stats),
        }

    def _effective_cooldown(self, key: str) -> float:
        """Escalating cooldown based on consecutive error count."""
        count = self._error_counts.get(key, 0)
        idx = min(count - 1, len(self._COOLDOWN_ESCALATION) - 1) if count > 0 else 0
        return self._COOLDOWN_ESCALATION[idx]

    def _is_in_cooldown(self, key: str) -> bool:
        fail_time = self._failures.get(key)
        if fail_time is None:
            return False
        return (time.time() - fail_time) < self._effective_cooldown(key)

    def _should_probe(self, key: str) -> bool:
        if not self._is_in_cooldown(key):
            return False
        last = self._last_probe.get(key, 0)
        return (time.time() - last) >= self._probe_interval

    def get_candidates(self) -> list[tuple[str, str]]:
        """Return ordered list of (provider, model) to attempt."""
        result = []
        probe_candidates = []

        for entry in self._chain:
            k = self._key(entry)
            if self._is_in_cooldown(k):
                if self._should_probe(k):
                    probe_candidates.append(entry)
            else:
                result.append(entry)

        if probe_candidates and result:
            result = result[:1] + probe_candidates + result[1:]
        elif probe_candidates:
            result = probe_candidates

        return result if result else list(self._chain)

    # Keep legacy method for backward compat
    def get_models_to_try(self) -> list[str]:
        return [e[1] for e in self.get_candidates()]

    def record_success(self, provider: str, model: str):
        k = f"{provider}:{model}"
        self._stats.setdefault(k, {"ok": 0, "fail": 0})
        self._stats[k]["ok"] += 1
        self._failures.pop(k, None)
        self._error_counts.pop(k, None)  # full reset on success (circuit breaker close)
        self._active = (provider, model)

    @staticmethod
    def _is_transient_error(error: str) -> bool:
        """Detect transient failures that deserve a short cooldown."""
        transient_markers = (
            "NameResolutionError", "Failed to resolve", "nodename nor servname",
            "ConnectionRefusedError", "ConnectionResetError", "ConnectionError",
            "Max retries exceeded", "NewConnectionError", "gaierror",
            "Response ended prematurely", "stream error", "Timeout on",
            "HTTP 502", "HTTP 503", "HTTP 529",
        )
        return any(m in error for m in transient_markers)

    @staticmethod
    def _is_rate_limit(error: str) -> bool:
        return "HTTP 429" in error or "rate limit" in error.lower()

    def record_failure(self, provider: str, model: str, error: str = ""):
        k = f"{provider}:{model}"
        self._stats.setdefault(k, {"ok": 0, "fail": 0})
        self._stats[k]["fail"] += 1
        self._error_counts[k] = self._error_counts.get(k, 0) + 1
        if self._is_transient_error(error):
            self._failures[k] = time.time() - (self._effective_cooldown(k) - NETWORK_COOLDOWN_SEC)
            log.warning("%s:%s transient error (%s), short cooldown %ds",
                        provider, model, error[:80], NETWORK_COOLDOWN_SEC)
        elif self._is_rate_limit(error):
            self._failures[k] = time.time()
            cd = self._effective_cooldown(k)
            log.warning("%s:%s rate limited (%s), cooldown %ds (error #%d)",
                        provider, model, error[:80], int(cd), self._error_counts[k])
        else:
            self._failures[k] = time.time()
            cd = self._effective_cooldown(k)
            log.warning("%s:%s failed (%s), entering cooldown %ds (error #%d)",
                        provider, model, error[:80], int(cd), self._error_counts[k])
        self._last_probe[k] = time.time()

    def remove_from_chain(self, provider: str, model: str):
        """Permanently remove a model from the fallback chain (for 404 errors)."""
        entry = (provider, model)
        if entry in self._chain:
            self._chain.remove(entry)
            log.warning("Removed %s:%s from fallback chain (invalid model ID)", provider, model)
        # Also clean up failure tracking
        k = self._key(entry)
        self._failures.pop(k, None)
        self._last_probe.pop(k, None)


def _jittered_delay(base: float, attempt: int) -> float:
    """Exponential backoff with jitter: base * 2^attempt * (1 ± JITTER_FACTOR)."""
    delay = base * (2 ** attempt)
    jitter = delay * JITTER_FACTOR * (2 * random.random() - 1)
    return max(0.5, min(delay + jitter, 120.0))


def _parse_retry_after(resp) -> float | None:
    """Extract wait time from Retry-After or x-ratelimit-reset-* headers."""
    if resp is None:
        return None
    headers = getattr(resp, "headers", {})
    ra = headers.get("Retry-After") or headers.get("retry-after")
    if ra:
        try:
            return float(ra)
        except (ValueError, TypeError):
            pass
    for hdr in ("x-ratelimit-reset-requests", "x-ratelimit-reset-tokens"):
        val = headers.get(hdr)
        if val:
            try:
                secs = _parse_duration_to_secs(val)
                if secs and secs > 0:
                    return secs
            except (ValueError, TypeError):
                pass
    return None


def _parse_duration_to_secs(s: str) -> float | None:
    """Parse duration strings like '6m30s', '45s', '2m' into seconds."""
    import re
    total = 0.0
    for amount, unit in re.findall(r'(\d+(?:\.\d+)?)\s*(h|m|s|ms)', s):
        v = float(amount)
        if unit == "h":
            total += v * 3600
        elif unit == "m":
            total += v * 60
        elif unit == "s":
            total += v
        elif unit == "ms":
            total += v / 1000
    return total if total > 0 else None
DEBUG_LOG_DIR = GHOST_HOME / "logs"
DEBUG_LOG_FILE = DEBUG_LOG_DIR / "tool_loop_debug.jsonl"
MAX_DEBUG_LOG_SIZE = 10 * 1024 * 1024  # 10MB before rotation


class ToolLoopDebugLogger:
    """Persistent JSONL logger for every tool loop session and step."""

    def __init__(self):
        self._local = threading.local()
        self._ensure_dir()

    @property
    def _session_id(self):
        return getattr(self._local, "session_id", None)

    @_session_id.setter
    def _session_id(self, value):
        self._local.session_id = value

    @property
    def _session_start(self):
        return getattr(self._local, "session_start", None)

    @_session_start.setter
    def _session_start(self, value):
        self._local.session_start = value

    def _ensure_dir(self):
        try:
            DEBUG_LOG_DIR.mkdir(parents=True, exist_ok=True)
        except Exception:
            pass

    def _rotate_if_needed(self):
        """Rotate log files: keep current + up to 5 backups (.1 through .5)."""
        try:
            if DEBUG_LOG_FILE.exists() and DEBUG_LOG_FILE.stat().st_size > MAX_DEBUG_LOG_SIZE:
                # Rotate existing backups: .4 -> .5, .3 -> .4, .2 -> .3, .1 -> .2
                for i in range(4, 0, -1):
                    old = DEBUG_LOG_DIR / f"tool_loop_debug.jsonl.{i}"
                    new = DEBUG_LOG_DIR / f"tool_loop_debug.jsonl.{i+1}"
                    if old.exists():
                        old.replace(new)
                DEBUG_LOG_FILE.replace(DEBUG_LOG_DIR / "tool_loop_debug.jsonl.1")
        except Exception:
            pass

    def _write(self, record: dict):
        try:
            self._rotate_if_needed()
            with open(str(DEBUG_LOG_FILE), "a", encoding="utf-8") as f:
                f.write(json.dumps(record, default=str) + "\n")
        except Exception:
            pass

    def session_start(self, user_message: str, model: str, max_steps: int, caller: str = ""):
        self._session_id = uuid.uuid4().hex[:12]
        self._session_start = time.time()
        self._write({
            "event": "session_start",
            "session_id": self._session_id,
            "time": time.strftime("%Y-%m-%dT%H:%M:%S"),
            "user_message": user_message[:500],
            "model": model,
            "max_steps": max_steps,
            "caller": caller,
        })
        return self._session_id

    def step_tool_call(self, step: int, tool_name: str, args: dict, result: str,
                       duration_ms: float = 0, loop_detection: str = ""):
        self._write({
            "event": "tool_call",
            "session_id": self._session_id,
            "time": time.strftime("%Y-%m-%dT%H:%M:%S"),
            "step": step,
            "tool": tool_name,
            "args_summary": self._summarize_args(args),
            "result_preview": result[:600] if result else "",
            "result_length": len(result) if result else 0,
            "duration_ms": round(duration_ms),
            "loop_detection": loop_detection,
        })

    def step_text_response(self, step: int, text: str, action_taken: str):
        self._write({
            "event": "text_response",
            "session_id": self._session_id,
            "time": time.strftime("%Y-%m-%dT%H:%M:%S"),
            "step": step,
            "text_preview": text[:300] if text else "",
            "text_length": len(text) if text else 0,
            "action": action_taken,
        })

    def step_error(self, step: int, error: str):
        self._write({
            "event": "error",
            "session_id": self._session_id,
            "time": time.strftime("%Y-%m-%dT%H:%M:%S"),
            "step": step,
            "error": error[:500],
        })

    def session_end(self, steps_used: int, tools_used: list, total_tokens: int,
                    exit_reason: str, final_text: str = ""):
        elapsed = time.time() - self._session_start if self._session_start else 0
        self._write({
            "event": "session_end",
            "session_id": self._session_id,
            "time": time.strftime("%Y-%m-%dT%H:%M:%S"),
            "steps_used": steps_used,
            "tools_used": tools_used,
            "total_tokens": total_tokens,
            "elapsed_seconds": round(elapsed, 1),
            "exit_reason": exit_reason,
            "final_text_preview": final_text[:300] if final_text else "",
        })

    @staticmethod
    def _summarize_args(args: dict) -> str:
        if not args:
            return "{}"
        summary = {}
        for k, v in args.items():
            if isinstance(v, str) and len(v) > 100:
                summary[k] = v[:80] + f"...({len(v)} chars)"
            elif isinstance(v, (list, dict)):
                s = json.dumps(v, default=str)
                summary[k] = s[:80] + "..." if len(s) > 80 else s
            else:
                summary[k] = v
        return json.dumps(summary, default=str)


_debug_logger = ToolLoopDebugLogger()


# ── Evolve Context Debug Logger ────────────────────────────────────
# Separate JSONL log dedicated to context-management events during
# evolution loops.  Each record is tagged with feature_id so you can
# grep for a single feature and see the full compaction story.

EVOLVE_CTX_LOG = GHOST_HOME / "logs" / "evolve_context_debug.jsonl"
_MAX_EVOLVE_CTX_LOG = 5 * 1024 * 1024  # 5 MB before rotation


class EvolveContextLogger:
    """Debug logger for context management during evolution/tool loops.

    Usage:
        ctx_log = EvolveContextLogger.get()
        ctx_log.set_feature("feat-abc123", "Add dark mode toggle")
        ctx_log.log_compaction(step=15, ...)
    """

    _instance = None

    def __init__(self):
        self._local = threading.local()
        try:
            DEBUG_LOG_DIR.mkdir(parents=True, exist_ok=True)
        except Exception:
            pass

    @property
    def _feature_id(self) -> str:
        return getattr(self._local, "feature_id", "")

    @_feature_id.setter
    def _feature_id(self, value: str):
        self._local.feature_id = value

    @property
    def _feature_title(self) -> str:
        return getattr(self._local, "feature_title", "")

    @_feature_title.setter
    def _feature_title(self, value: str):
        self._local.feature_title = value

    @property
    def _session_id(self) -> str:
        return getattr(self._local, "session_id", "")

    @_session_id.setter
    def _session_id(self, value: str):
        self._local.session_id = value

    @property
    def _caller(self) -> str:
        return getattr(self._local, "caller", "")

    @_caller.setter
    def _caller(self, value: str):
        self._local.caller = value

    @property
    def _warning_count(self) -> int:
        return getattr(self._local, "warning_count", 0)

    @_warning_count.setter
    def _warning_count(self, value: int):
        self._local.warning_count = value

    @classmethod
    def get(cls) -> "EvolveContextLogger":
        if cls._instance is None:
            cls._instance = cls()
        return cls._instance

    # ── Context setters ──────────────────────────────────────────

    def set_feature(self, feature_id: str, feature_title: str = ""):
        self._feature_id = feature_id or ""
        self._feature_title = feature_title or ""

    def set_session(self, session_id: str, caller: str = ""):
        self._session_id = session_id or ""
        self._caller = caller or ""

    def clear(self):
        self._feature_id = ""
        self._feature_title = ""
        self._session_id = ""
        self._caller = ""
        self._warning_count = 0

    # ── Internal ─────────────────────────────────────────────────

    def _base(self) -> dict:
        return {
            "time": time.strftime("%Y-%m-%dT%H:%M:%S"),
            "feature_id": self._feature_id,
            "feature_title": self._feature_title[:80],
            "session_id": self._session_id,
            "caller": self._caller,
        }

    def _write(self, record: dict):
        try:
            if EVOLVE_CTX_LOG.exists() and EVOLVE_CTX_LOG.stat().st_size > _MAX_EVOLVE_CTX_LOG:
                rotated = EVOLVE_CTX_LOG.with_suffix(".jsonl.1")
                EVOLVE_CTX_LOG.replace(rotated)
            with open(str(EVOLVE_CTX_LOG), "a", encoding="utf-8") as f:
                f.write(json.dumps(record, default=str) + "\n")
        except Exception:
            pass

    # ── Event loggers ────────────────────────────────────────────

    def log_compaction(self, step: int, msgs_before: int, msgs_after: int,
                       tokens_before: int, tokens_after: int,
                       method: str, llm_summary_used: bool):
        """Log a context compaction event.

        method: "normal", "emergency_1", "emergency_2", "emergency_3"
        """
        rec = self._base()
        rec.update({
            "event": "compaction",
            "step": step,
            "messages_before": msgs_before,
            "messages_after": msgs_after,
            "tokens_before": tokens_before,
            "tokens_after": tokens_after,
            "reduction_pct": round(100 * (1 - tokens_after / max(tokens_before, 1)), 1),
            "method": method,
            "llm_summary_used": llm_summary_used,
        })
        self._write(rec)

    def log_overflow_recovery(self, step: int, attempt: int, error_snippet: str):
        """Log a context overflow detection + recovery attempt."""
        rec = self._base()
        rec.update({
            "event": "overflow_recovery",
            "step": step,
            "attempt": attempt,
            "max_attempts": _MAX_OVERFLOW_RECOVERY_ATTEMPTS,
            "error": error_snippet[:200],
        })
        self._write(rec)

    def log_adaptive_limit(self, step: int, tokens_estimated: int, limit_applied: int):
        """Log when the adaptive tool-result limit kicks in."""
        rec = self._base()
        rec.update({
            "event": "adaptive_limit",
            "step": step,
            "tokens_estimated": tokens_estimated,
            "limit_applied": limit_applied,
        })
        self._write(rec)

    def log_context_snapshot(self, step: int, total_messages: int,
                             tokens_estimated: int, model: str):
        """Periodic snapshot of context size for trend analysis."""
        rec = self._base()
        rec.update({
            "event": "context_snapshot",
            "step": step,
            "total_messages": total_messages,
            "tokens_estimated": tokens_estimated,
            "model": model,
        })
        self._write(rec)

    def log_llm_summary_result(self, step: int, success: bool,
                                input_chars: int, output_chars: int,
                                error: str = ""):
        """Log the outcome of an LLM summarization attempt."""
        rec = self._base()
        rec.update({
            "event": "llm_summary",
            "step": step,
            "success": success,
            "input_chars": input_chars,
            "output_chars": output_chars,
            "error": error[:200],
        })
        self._write(rec)

    def log_review_compaction(self, pr_id: str, step: int,
                               comments_count: int, tokens_estimated: int):
        """Log compaction event during a PR review loop."""
        rec = self._base()
        rec.update({
            "event": "review_compaction",
            "pr_id": pr_id,
            "step": step,
            "comments_so_far": comments_count,
            "tokens_estimated": tokens_estimated,
        })
        self._write(rec)

    # ── Skill compliance ─────────────────────────────────────────

    def log_skill_compliance(self, role: str, tool_calls: list,
                              extra: dict | None = None):
        """Analyze tool call patterns and log whether the agent followed its skills.

        role: "implementer" or "reviewer"
        tool_calls: list of dicts with "tool", "args", "step", "result" keys
        extra: role-specific metadata (e.g. is_rereview for reviewer)
        """
        extra = extra or {}
        if role == "implementer":
            checks = self._check_implementer_skills(tool_calls)
        elif role == "reviewer":
            checks = self._check_reviewer_skills(tool_calls, extra)
        else:
            return

        passed = sum(1 for v in checks.values() if v)
        total = len(checks)
        rec = self._base()
        rec.update({
            "event": "skill_compliance",
            "role": role,
            "passed": passed,
            "total": total,
            "score": f"{passed}/{total}",
            "checks": checks,
            "extra": extra,
        })
        self._write(rec)

    @staticmethod
    def _check_implementer_skills(tc: list) -> dict:
        """Check whether the implementer followed its trained skills."""
        tools_used = [t["tool"] for t in tc]
        tool_set = set(tools_used)

        has_evolve_apply = "evolve_apply" in tool_set
        has_evolve_plan = "evolve_plan" in tool_set
        has_submit_pr = "evolve_submit_pr" in tool_set

        # 1. Mistake check: called memory_search before coding
        mistake_check = False
        for t in tc:
            if t["tool"] == "memory_search":
                args_str = json.dumps(t.get("args", {}))
                if "mistake" in args_str.lower():
                    mistake_check = True
                    break

        # 2. Already-implemented check: grep/file_search BEFORE start_future_feature
        already_impl_check = False
        start_idx = next(
            (i for i, t in enumerate(tc) if t["tool"] == "start_future_feature"),
            len(tc)
        )
        search_before_start = any(
            t["tool"] in ("grep", "file_search", "glob")
            for t in tc[:start_idx]
        )
        already_impl_check = search_before_start or start_idx == len(tc)

        # 3. Re-read dependencies: file_read AFTER evolve_plan but BEFORE evolve_apply
        reread_deps = False
        if has_evolve_plan and has_evolve_apply:
            plan_idx = next(i for i, t in enumerate(tc) if t["tool"] == "evolve_plan")
            apply_idx = next(i for i, t in enumerate(tc) if t["tool"] == "evolve_apply")
            reread_deps = any(
                t["tool"] == "file_read"
                for t in tc[plan_idx + 1:apply_idx]
            )

        # 4. Testing: called evolve_test before submit_pr
        tested_before_pr = False
        if has_submit_pr:
            pr_idx = next(i for i, t in enumerate(tc) if t["tool"] == "evolve_submit_pr")
            tested_before_pr = any(
                t["tool"] == "evolve_test"
                for t in tc[:pr_idx]
            )

        # 5. Delegate verification: used delegate_task after evolve_apply
        delegate_verify = False
        if has_evolve_apply:
            apply_idx = next(i for i, t in enumerate(tc) if t["tool"] == "evolve_apply")
            delegate_verify = any(
                t["tool"] == "delegate_task"
                for t in tc[apply_idx:]
            )

        # 6. Explore phase: read files during explore (between start_feature and evolve_plan)
        explored = False
        if has_evolve_plan:
            plan_idx = next(i for i, t in enumerate(tc) if t["tool"] == "evolve_plan")
            explored = any(
                t["tool"] in ("file_read", "grep", "file_search", "glob")
                for t in tc[:plan_idx]
            )

        return {
            "mistake_memory_search": mistake_check,
            "already_implemented_check": already_impl_check,
            "reread_deps_before_apply": reread_deps,
            "tested_before_pr": tested_before_pr,
            "delegate_verification": delegate_verify,
            "explored_before_plan": explored,
        }

    @staticmethod
    def _check_reviewer_skills(tc: list, extra: dict) -> dict:
        """Check whether the reviewer followed its trained skills."""
        tool_set = set(t["tool"] for t in tc)
        tools_used = [t["tool"] for t in tc]
        is_rereview = extra.get("is_rereview", False)

        # 1. Read overview first: read_pr_diff with no file arg as first tool call
        overview_first = False
        if tc:
            first = tc[0]
            if first["tool"] == "read_pr_diff":
                file_arg = (first.get("args") or {}).get("file", "")
                overview_first = not file_arg

        # 2. Used grep to verify wiring/interfaces
        used_grep = "grep_codebase" in tool_set

        # 3. Left inline comments (actually doing the review work)
        left_comments = "leave_comment" in tool_set

        # 4. Called submit_review (mandatory)
        submitted_review = "submit_review" in tool_set

        # 5. Called get_my_comments before submit_review
        refreshed_memory = False
        if submitted_review:
            submit_idx = next(i for i, t in enumerate(tc) if t["tool"] == "submit_review")
            refreshed_memory = any(
                t["tool"] == "get_my_comments"
                for t in tc[:submit_idx]
            )

        # 6. On re-review: called get_review_history
        checked_history = True
        if is_rereview:
            checked_history = "get_review_history" in tool_set

        # 7. Used read_pr_file for deeper inspection
        checked_surrounding_code = "read_pr_file" in tool_set

        return {
            "overview_first": overview_first,
            "used_grep_verify": used_grep,
            "left_comments": left_comments,
            "submitted_review": submitted_review,
            "refreshed_memory_before_submit": refreshed_memory,
            "checked_history_on_rereview": checked_history,
            "checked_surrounding_code": checked_surrounding_code,
        }


_ctx_logger = EvolveContextLogger.get()

KNOWN_POLL_TOOLS = {"shell_exec", "browser", "check_task"}
KNOWN_POLL_ACTIONS = {"snapshot", "content", "screenshot", "poll", "log", "status"}
WARNING_BUCKET_SIZE = 10

def _check_incomplete_workflows(tool_calls_log: list) -> str | None:
    """Check if any tool workflows are incomplete. Returns a message if so, None if OK."""
    tools_used = {tc["tool"] for tc in tool_calls_log}

    started_feature = "start_future_feature" in tools_used
    used_evolve_plan = "evolve_plan" in tools_used
    used_evolve_resume = "evolve_resume" in tools_used
    used_evolve_start = used_evolve_plan or used_evolve_resume
    used_evolve_apply = "evolve_apply" in tools_used
    used_fail = "fail_future_feature" in tools_used

    deploy_succeeded = False
    deploy_rejected = False
    for tc in tool_calls_log:
        if tc["tool"] in ("evolve_deploy", "evolve_submit_pr"):
            result = tc.get("result", "")
            if "BLOCKED" in result:
                continue
            if "REJECTED" in result:
                deploy_rejected = True
            else:
                deploy_succeeded = True
    used_evolve_deploy = deploy_succeeded or deploy_rejected

    if used_evolve_start and not used_evolve_deploy and not used_fail:
        if not used_evolve_apply:
            verb = "evolve_resume" if used_evolve_resume else "evolve_plan"
            return (
                f"You called {verb} but never called evolve_apply. "
                "You MUST apply changes. Call evolve_apply now for each file, "
                "then evolve_test, then evolve_submit_pr. Do NOT rollback or quit."
            )
        last_submit_result = ""
        for tc in tool_calls_log:
            if tc["tool"] == "evolve_submit_pr":
                last_submit_result = tc.get("result", "")
        if "BLOCKED" in last_submit_result:
            return (
                "Your evolve_submit_pr was BLOCKED (verification required). "
                "You MUST: 1) file_read each modified file to verify your changes, "
                "2) then call evolve_submit_pr again. Do NOT give up or respond with text."
            )
        return (
            "You started an evolution but evolve_submit_pr hasn't succeeded yet. "
            "Finish: evolve_test → file_read verification → evolve_submit_pr. "
            "Do NOT call task_complete until evolve_submit_pr succeeds."
        )

    if started_feature and not used_evolve_start and not used_fail:
        return (
            "You called start_future_feature but never called evolve_plan. "
            "You MUST implement the feature NOW. Call evolve_plan, then "
            "evolve_apply, evolve_test, evolve_submit_pr. Do NOT defer to "
            "'the next run'. There is no next run — do it now."
        )

    return None


def _check_verification_before_submit(tool_calls_log: list) -> str | None:
    """Block evolve_submit_pr if tests haven't passed or verification is missing.

    Returns an error message if blocked, None if OK.
    """
    last_test_idx = -1
    last_test_result = ""
    for i, tc in enumerate(tool_calls_log):
        if tc["tool"] == "evolve_test":
            last_test_idx = i
            last_test_result = tc.get("result", "")

    if last_test_idx < 0:
        return None

    if "Tests FAILED" in last_test_result:
        return (
            "BLOCKED: Your last evolve_test FAILED. You cannot submit a PR until "
            "tests pass. Fix the issues with evolve_apply, then re-run evolve_test. "
            "Do NOT call evolve_submit_pr until evolve_test returns 'Tests PASSED'."
        )

    last_test_step = tool_calls_log[last_test_idx].get("step", 0)
    verification_tools = {"file_read", "grep"}
    verified = any(
        tc["tool"] in verification_tools and tc.get("step", 0) > last_test_step
        for tc in tool_calls_log
    )
    if not verified:
        return (
            "BLOCKED: You must VERIFY your changes before submitting. "
            "After evolve_test passes, use file_read to review the changed files "
            "and confirm they are correct. Then call evolve_submit_pr again."
        )
    return None


_MAX_CONSECUTIVE_TEST_FAILURES = 5


def _count_consecutive_test_failures(tool_calls_log: list) -> int:
    """Count consecutive evolve_test failures from the end of the log."""
    count = 0
    for tc in reversed(tool_calls_log):
        if tc["tool"] == "evolve_test":
            if "Tests FAILED" in tc.get("result", ""):
                count += 1
            else:
                break
    return count


@dataclass
class LoopDetectionConfig:
    enabled: bool = True
    history_size: int = 30
    warning_threshold: int = 10
    critical_threshold: int = 20
    global_circuit_breaker: int = 30
    detectors: dict = field(default_factory=lambda: {
        "generic_repeat": True,
        "known_poll_no_progress": True,
        "ping_pong": True,
    })


@dataclass
class LoopDetectionResult:
    stuck: bool = False
    level: str = ""
    detector: str = ""
    count: int = 0
    message: str = ""


class LoopDetector:
    """Advanced loop detection using a detector priority chain.

    Detectors (checked in order, first match wins):
    1. global_circuit_breaker — any tool no-progress streak >= 30 -> block
    2. known_poll_no_progress (critical) — poll tools streak >= 20 -> block
    3. known_poll_no_progress (warning) — poll tools streak >= 10 -> warn
    4. ping_pong (critical) — A-B-A-B with no-progress evidence >= 20 -> block
    5. ping_pong (warning) — A-B-A-B >= 10 -> warn
    6. generic_repeat — non-poll tools repeated >= 10 -> warn only
    """

    def __init__(self, cfg: LoopDetectionConfig = None):
        self._cfg = cfg or LoopDetectionConfig()
        self._history: list[dict] = []
        self._warning_buckets: dict[str, int] = {}
        self._call_counter = 0
        self._global_tool_counts: dict[str, int] = {}
        self._tool_name_counts: dict[str, int] = {}
        self._warning_count = 0

    @staticmethod
    def _hash_args(tool_name, args):
        raw = json.dumps(args, sort_keys=True, default=str)
        return f"{tool_name}:{hashlib.sha256(raw.encode()).hexdigest()[:16]}"

    @staticmethod
    def _hash_result(result):
        if not result:
            return "empty"
        return hashlib.sha256(result.encode()).hexdigest()[:16]

    @staticmethod
    def _is_poll_tool(tool_name, args):
        if tool_name in KNOWN_POLL_TOOLS:
            return True
        action = args.get("action", "") if isinstance(args, dict) else ""
        return action in KNOWN_POLL_ACTIONS

    def record_call(self, tool_name, args):
        """Phase 1: record the call BEFORE execution. Returns a call_id for patching result later."""
        call_id = f"lc_{self._call_counter}"
        self._call_counter += 1
        call_hash = self._hash_args(tool_name, args)
        entry = {
            "id": call_id,
            "tool": tool_name,
            "call_hash": call_hash,
            "result_hash": None,
            "ts": time.time(),
        }
        self._history.append(entry)
        if len(self._history) > self._cfg.history_size:
            self._history.pop(0)
        self._global_tool_counts[call_hash] = self._global_tool_counts.get(call_hash, 0) + 1
        self._tool_name_counts[tool_name] = self._tool_name_counts.get(tool_name, 0) + 1
        return call_id

    def record_result(self, call_id, result):
        """Phase 2: patch the result hash AFTER execution."""
        result_str = result if isinstance(result, str) else json.dumps(result, default=str)
        rh = self._hash_result(result_str)
        for entry in reversed(self._history):
            if entry["id"] == call_id:
                entry["result_hash"] = rh
                break

    def check(self, tool_name, args) -> LoopDetectionResult:
        """Run the full detector priority chain. Returns detection result."""
        if not self._cfg.enabled:
            return LoopDetectionResult()

        call_hash = self._hash_args(tool_name, args)
        is_poll = self._is_poll_tool(tool_name, args)
        detectors = self._cfg.detectors

        tool_total = self._tool_name_counts.get(tool_name, 0)

        _LOGGING_TOOLS_WARN = 3
        _LOGGING_TOOLS_BLOCK = 5
        if tool_name in ("log_growth_activity", "memory_save") and tool_total >= _LOGGING_TOOLS_WARN:
            self._warning_count += 1
            if tool_total >= _LOGGING_TOOLS_BLOCK:
                return LoopDetectionResult(
                    stuck=True, level="critical", detector="tool_saturation",
                    count=tool_total,
                    message=(
                        f"BLOCKED: {tool_name} called {tool_total} times this session. "
                        "Logging tools should only be called ONCE at the end of a task. "
                        "STOP calling this tool and proceed with your actual task."
                    ),
                )
            return LoopDetectionResult(
                stuck=True, level="warning", detector="tool_saturation",
                count=tool_total,
                message=(
                    f"WARNING: {tool_name} called {tool_total} times. "
                    "Only call logging tools ONCE. Focus on your actual task."
                ),
            )

        _SHELL_ABUSE_WARN = 25
        _SHELL_ABUSE_BLOCK = 40
        if tool_name == "shell_exec" and tool_total >= _SHELL_ABUSE_WARN:
            self._warning_count += 1
            in_evolve = any(
                e["tool"] in ("evolve_plan", "evolve_apply", "evolve_resume")
                for e in self._history
            )
            if tool_total >= _SHELL_ABUSE_BLOCK:
                if in_evolve:
                    advice = (
                        "STOP using shell_exec and either: "
                        "(1) use file_read to inspect files, "
                        "(2) call evolve_test if your changes are complete, "
                        "(3) call fail_future_feature if you cannot make progress, or "
                        "(4) call task_complete to end the session."
                    )
                else:
                    advice = (
                        "You may be stuck in a loop. Consider: "
                        "(1) use file_read to inspect files instead, "
                        "(2) call task_complete if the task is done, or "
                        "(3) ask the user for clarification."
                    )
                return LoopDetectionResult(
                    stuck=True, level="critical", detector="tool_saturation",
                    count=tool_total,
                    message=f"BLOCKED: shell_exec called {tool_total} times this session. {advice}",
                )
            if in_evolve:
                hint = "Use file_read to inspect files and evolve_apply to modify them."
            else:
                hint = (
                    "Consider whether you are repeating the same commands. "
                    "Use file_read to inspect files instead of shell_exec."
                )
            return LoopDetectionResult(
                stuck=True, level="warning", detector="tool_saturation",
                count=tool_total,
                message=f"WARNING: shell_exec called {tool_total} times. {hint}",
            )

        _REPEAT_EXEMPT_TOOLS = {"evolve_test", "evolve_apply", "file_read"}
        global_count = self._global_tool_counts.get(call_hash, 0)
        if global_count >= 5 and tool_name not in _REPEAT_EXEMPT_TOOLS:
            self._warning_count += 1
            if global_count >= 8:
                return LoopDetectionResult(
                    stuck=True, level="critical", detector="global_total_repeat",
                    count=global_count,
                    message=(
                        f"BLOCKED: {tool_name} called {global_count} times total this session "
                        "with identical arguments. You are in an infinite loop. "
                        "STOP calling this tool. Call task_complete NOW."
                    ),
                )
            return LoopDetectionResult(
                stuck=True, level="warning", detector="global_total_repeat",
                count=global_count,
                message=(
                    f"WARNING: {tool_name} called {global_count} times total this session. "
                    "Stop calling this tool and make actual progress on the task."
                ),
            )

        streak = self._get_no_progress_streak(call_hash)

        if streak >= self._cfg.global_circuit_breaker:
            return LoopDetectionResult(
                stuck=True, level="critical", detector="global_circuit_breaker",
                count=streak,
                message=(
                    f"BLOCKED: {tool_name} called {streak} times with identical arguments and no progress. "
                    "You are completely stuck. STOP calling this tool and try a fundamentally different approach, "
                    "or give up on this sub-task and move on."
                ),
            )

        if detectors.get("known_poll_no_progress") and is_poll:
            if streak >= self._cfg.critical_threshold:
                return LoopDetectionResult(
                    stuck=True, level="critical", detector="known_poll_no_progress",
                    count=streak,
                    message=(
                        f"BLOCKED: {tool_name} polled {streak} times with no change in output. "
                        "The operation is stuck or complete. Stop polling and try a different approach."
                    ),
                )
            if streak >= self._cfg.warning_threshold:
                return LoopDetectionResult(
                    stuck=True, level="warning", detector="known_poll_no_progress",
                    count=streak,
                    message=(
                        f"WARNING: {tool_name} polled {streak} times with identical results. "
                        "Consider stopping or trying a different approach."
                    ),
                )

        if detectors.get("ping_pong"):
            pp_count, pp_no_progress = self._get_ping_pong_streak(call_hash)
            if pp_count >= self._cfg.critical_threshold and pp_no_progress:
                return LoopDetectionResult(
                    stuck=True, level="critical", detector="ping_pong",
                    count=pp_count,
                    message=(
                        f"BLOCKED: You're alternating between two tool calls ({pp_count} times) "
                        "with no progress on either side. This ping-pong pattern is not productive. "
                        "Try a completely different approach."
                    ),
                )
            if pp_count >= self._cfg.warning_threshold:
                return LoopDetectionResult(
                    stuck=True, level="warning", detector="ping_pong",
                    count=pp_count,
                    message=(
                        f"WARNING: Ping-pong pattern detected — alternating between two calls "
                        f"({pp_count} times). Consider a different strategy."
                    ),
                )

        if detectors.get("generic_repeat") and not is_poll:
            repeat_count = sum(1 for h in self._history if h["call_hash"] == call_hash)
            if repeat_count >= self._cfg.critical_threshold:
                return LoopDetectionResult(
                    stuck=True, level="critical", detector="generic_repeat",
                    count=repeat_count,
                    message=(
                        f"BLOCKED: {tool_name} called {repeat_count} times with identical arguments. "
                        "You are stuck in a loop. STOP calling this tool and try a completely "
                        "different approach, or call task_complete to finish."
                    ),
                )
            if repeat_count >= self._cfg.warning_threshold:
                return LoopDetectionResult(
                    stuck=True, level="warning", detector="generic_repeat",
                    count=repeat_count,
                    message=(
                        f"WARNING: {tool_name} called {repeat_count} times with identical arguments. "
                        "The repeated calls may not be productive. Try a different approach."
                    ),
                )

        return LoopDetectionResult()

    def should_emit_warning(self, detector: str, count: int) -> bool:
        """Bucket-based deduplication: emit once per bucket of WARNING_BUCKET_SIZE."""
        bucket = count // WARNING_BUCKET_SIZE
        key = detector
        last_bucket = self._warning_buckets.get(key, -1)
        if bucket > last_bucket:
            self._warning_buckets[key] = bucket
            return True
        return False

    def _get_no_progress_streak(self, call_hash: str) -> int:
        """Walk backward counting consecutive entries with same call_hash AND same result_hash."""
        streak = 0
        last_result = None
        for entry in reversed(self._history):
            if entry["call_hash"] != call_hash:
                continue
            rh = entry.get("result_hash")
            if rh is None:
                continue
            if last_result is None:
                last_result = rh
                streak = 1
            elif rh == last_result:
                streak += 1
            else:
                break
        return streak

    def _get_ping_pong_streak(self, proposed_hash: str) -> tuple[int, bool]:
        """Detect A-B-A-B alternation. Returns (streak_count, no_progress_evidence)."""
        if len(self._history) < 2:
            return 0, False

        other_hash = None
        for entry in reversed(self._history):
            if entry["call_hash"] != proposed_hash:
                other_hash = entry["call_hash"]
                break
        if not other_hash:
            return 0, False

        pattern = [proposed_hash, other_hash]
        streak = 1
        side_a_results = set()
        side_b_results = set()

        for i, entry in enumerate(reversed(self._history)):
            expected = pattern[i % 2]
            if i == 0 and entry["call_hash"] == proposed_hash:
                if entry.get("result_hash"):
                    side_a_results.add(entry["result_hash"])
                streak = 1
                continue
            if entry["call_hash"] != expected:
                break
            streak += 1
            rh = entry.get("result_hash")
            if rh:
                if entry["call_hash"] == proposed_hash:
                    side_a_results.add(rh)
                else:
                    side_b_results.add(rh)

        no_progress = (len(side_a_results) <= 1 and len(side_b_results) <= 1
                       and len(side_a_results) + len(side_b_results) > 0)
        return streak, no_progress


_SIG_RE = _re.compile(
    r'^[ \t]*((?:async\s+)?def\s+\w+\s*\([^)]*\)|class\s+\w+[^:]*:)',
    _re.MULTILINE,
)

_CONTEXT_OVERFLOW_RE = _re.compile(
    r"context.length|too.long|token.limit|prompt.too.long|"
    r"max.context|context.window|reduce.the.length|"
    r"maximum.+tokens|exceeds.+limit|input.too.large|"
    r"request.too.large",
    _re.IGNORECASE,
)
_MAX_OVERFLOW_RECOVERY_ATTEMPTS = 3


_HEAD_CHARS = 600
_TAIL_CHARS = 500

_CHARS_PER_TOKEN_ESTIMATE = 4
_COMPACT_TOKEN_THRESHOLD = 80_000


def _adaptive_tool_result_limit(messages: list) -> int:
    """Scale tool-result cap based on current context usage.

    Early in the session (small context): allow larger results so the LLM
    gets richer context during the explore phase.
    Later (large context): shrink results to preserve budget and delay
    compaction.  Floor is 4000 — below that the model loses too much file
    content for evolve_apply to work accurately.
    """
    tokens = _estimate_context_tokens(messages)
    if tokens < 20_000:
        return 12_000
    if tokens < 50_000:
        return 8_000
    if tokens < 70_000:
        return 6_000
    return 4_000


def _estimate_context_tokens(messages: list) -> int:
    """Rough token estimate from message content (chars / 4)."""
    total_chars = 0
    for m in messages:
        content = m.get("content") or ""
        if isinstance(content, str):
            total_chars += len(content)
        elif isinstance(content, list):
            for part in content:
                if isinstance(part, dict):
                    total_chars += len(str(part.get("text", "")))
        for tc in (m.get("tool_calls") or []):
            total_chars += len(json.dumps(tc.get("function", {}), default=str))
    return total_chars // _CHARS_PER_TOKEN_ESTIMATE


_COMPACTION_SYSTEM_PROMPT = (
    "You are a context compactor for an AI coding agent in a long tool-loop session. "
    "The older messages are about to be replaced by YOUR summary to free context space. "
    "Produce a structured summary that preserves EXACTLY:\n"
    "1. IMPORT statements from every file that was read (critical for writing patches)\n"
    "2. API signatures discovered — class names, method names, function signatures\n"
    "3. File paths read and their key structures (what classes/functions they contain)\n"
    "4. Decisions made and current plan (feature being implemented, evolution ID)\n"
    "5. Rules or constraints mentioned\n"
    "6. Current task state and what still needs to be done\n"
    "7. Any error messages, test failures, or PR reviewer feedback\n\n"
    "Be concise. Use bullet points. Preserve EXACT method/function names and import lines — "
    "these are the #1 thing that gets lost during context compaction and causes bugs.\n"
    "For each file read, include: path, imports, and key signatures (def/class lines)."
)

_MAX_SUMMARY_INPUT_CHARS = 20000
_MAX_SUMMARY_TOKENS = 2048


_IMPORT_RE = _re.compile(
    r'^(?:from\s+\S+\s+import\s+.+|import\s+\S+)',
    _re.MULTILINE,
)


def _smart_compact_tool_result(content: str, limit: int = 600) -> str:
    """Compact a tool result preserving the most useful information.

    Strategy:
      1. Code content (file_read): extract imports + class/function signatures.
         Imports are critical for evolve_apply — the model needs to know what's
         available when writing patches.
      2. Non-code content: head+tail trim — keep the beginning AND end,
         since conclusions, return values, and final output are often at
         the tail.
    """
    if not content or len(content) <= limit:
        return content

    signatures = _SIG_RE.findall(content)
    if signatures:
        imports = _IMPORT_RE.findall(content)
        import_block = "\n".join(imp.rstrip() for imp in imports[:20])
        sig_block = "\n".join(s.rstrip() for s in signatures)
        parts = []
        if import_block:
            parts.append(import_block)
        parts.append("...(compacted — imports + signatures preserved)")
        parts.append(sig_block)
        compact = "\n".join(parts)
        cap = max(limit * 3, 2400)
        if len(compact) > cap:
            compact = compact[:cap] + "\n...(truncated)"
        return compact

    head = content[:_HEAD_CHARS].rstrip()
    tail = content[-_TAIL_CHARS:].lstrip()
    note = (
        f"\n[Compacted: kept first {_HEAD_CHARS} and last {_TAIL_CHARS} "
        f"chars of {len(content)} total.]\n"
    )
    return head + "\n..." + note + "..." + tail


def _build_deterministic_summary(old_messages: list) -> str:
    """Build a structured context summary without an LLM call.

    Extracts tool-call names, key arguments, and signature-preserved results
    from the messages being evicted.  Used as the fast fallback when LLM
    summarization is unavailable or fails.
    """
    parts: list[str] = []
    for m in old_messages:
        role = m.get("role", "")
        content = m.get("content") or ""

        if role == "assistant":
            tcs = m.get("tool_calls") or []
            if tcs:
                for tc in tcs:
                    fn = tc.get("function", {})
                    name = fn.get("name", "?")
                    raw_args = fn.get("arguments", "")
                    if isinstance(raw_args, str):
                        try:
                            raw_args = json.loads(raw_args)
                        except Exception:
                            raw_args = {}
                    key_parts = []
                    for k, v in (raw_args if isinstance(raw_args, dict) else {}).items():
                        key_parts.append(f"{k}={str(v)[:80]}")
                    parts.append(f"• {name}({', '.join(key_parts)})")

        elif role == "tool":
            compacted = _smart_compact_tool_result(content, 1200)
            if compacted:
                parts.append(f"  → {compacted[:1500]}")

        elif role == "user":
            if "[Context Summary" in content:
                parts.append(content[:3000])
            else:
                parts.append(f"• User: {content[:200]}")

    return "\n".join(parts)[:10000]


def _condense_for_llm_summary(old_messages: list) -> str:
    """Condense old messages into a text block for the LLM summarizer.

    Gives generous budget to tool results (especially file_read) so the LLM
    can produce a summary that preserves imports, signatures, and key structures.
    """
    parts: list[str] = []
    for m in old_messages:
        role = m.get("role", "")
        content = m.get("content") or ""

        if role == "assistant":
            tcs = m.get("tool_calls") or []
            if tcs:
                call_parts = []
                for tc in tcs:
                    fn = tc.get("function", {})
                    name = fn.get("name", "?")
                    raw_args = fn.get("arguments", "")
                    if isinstance(raw_args, str):
                        try:
                            raw_args = json.loads(raw_args)
                        except Exception:
                            raw_args = {}
                    arg_summary = ", ".join(
                        f"{k}={str(v)[:60]}"
                        for k, v in (raw_args if isinstance(raw_args, dict) else {}).items()
                    )
                    call_parts.append(f"{name}({arg_summary})")
                parts.append(f"Assistant called: {'; '.join(call_parts)}")
            elif content:
                parts.append(f"Assistant: {content[:400]}")

        elif role == "tool":
            compacted = _smart_compact_tool_result(content, 1200)
            parts.append(f"Tool result: {compacted[:1500]}")

        elif role == "user":
            if "[Context Summary" in content:
                parts.append(content[:3000])
            else:
                parts.append(f"User: {content[:400]}")

    return "\n".join(parts)[:_MAX_SUMMARY_INPUT_CHARS]


import re as _re

_XML_TOOL_CALL_RE = _re.compile(
    r"</?(?:invoke|parameter|minimax:tool_call|tool_call)[^>]*>",
    _re.IGNORECASE,
)

_XML_BLOCK_RE = _re.compile(
    r"<(?:invoke|minimax:tool_call|tool_call)\b[^>]*>.*?</(?:invoke|minimax:tool_call|tool_call)>",
    _re.IGNORECASE | _re.DOTALL,
)

_XML_ORPHAN_TAG_RE = _re.compile(
    r"</?(?:invoke|parameter|minimax:tool_call|tool_call)\b[^>]*>",
    _re.IGNORECASE,
)


def _is_xml_tool_markup(text: str) -> bool:
    """Detect raw XML tool call markup that some models emit as content."""
    stripped = text.strip()
    if not stripped:
        return False
    return bool(_XML_TOOL_CALL_RE.search(stripped))


def _strip_xml_tool_markup(text: str) -> str:
    """Remove XML tool call markup that some models emit as text content.

    Some models (e.g. minimax) emit tool calls as XML tags in the content
    stream instead of using proper function calling format. This strips
    those patterns so the user never sees raw XML.
    """
    if not text or not _XML_TOOL_CALL_RE.search(text):
        return text
    cleaned = _XML_BLOCK_RE.sub("", text)
    cleaned = _XML_ORPHAN_TAG_RE.sub("", cleaned)
    cleaned = _re.sub(r"\n{3,}", "\n\n", cleaned).strip()
    return cleaned


def _parse_openai_stream(response, on_token=None) -> dict:
    """Parse an OpenAI-compatible SSE stream into a Chat Completions response dict.

    Accumulates delta chunks (content, tool_calls) and returns the same
    structure as a non-streaming response so callers don't need to change.
    If *on_token* is provided it is called with each content-delta string
    as it arrives, enabling real-time token streaming to the frontend.
    """
    content_parts: list[str] = []
    tool_calls: dict[int, dict] = {}
    finish_reason = None
    model = ""
    resp_id = ""
    usage = {}

    # Force UTF-8 decoding — many API providers send text/event-stream
    # without a charset parameter, causing requests to default to Latin-1
    # and mangle emoji/non-ASCII characters.
    response.encoding = "utf-8"

    for line in response.iter_lines(decode_unicode=True):
        if not line:
            continue
        if isinstance(line, bytes):
            line = line.decode("utf-8", errors="replace")
        if not line.startswith("data: "):
            continue
        data_str = line[6:]
        if data_str.strip() == "[DONE]":
            break
        try:
            chunk = json.loads(data_str)
        except (json.JSONDecodeError, ValueError):
            continue

        if not resp_id:
            resp_id = chunk.get("id", "")
        if not model:
            model = chunk.get("model", "")
        if chunk.get("usage"):
            usage = chunk["usage"]

        for choice in chunk.get("choices", []):
            delta = choice.get("delta", {})
            if delta.get("content"):
                content_parts.append(delta["content"])
                if on_token and not _is_xml_tool_markup(delta["content"]):
                    try:
                        on_token(delta["content"])
                    except Exception:
                        pass

            for tc_delta in delta.get("tool_calls", []):
                idx = tc_delta.get("index", 0)
                if idx not in tool_calls:
                    tool_calls[idx] = {
                        "id": tc_delta.get("id", ""),
                        "type": "function",
                        "function": {"name": "", "arguments": ""},
                    }
                tc = tool_calls[idx]
                if tc_delta.get("id"):
                    tc["id"] = tc_delta["id"]
                fn_delta = tc_delta.get("function", {})
                if fn_delta.get("name"):
                    tc["function"]["name"] = fn_delta["name"]
                if fn_delta.get("arguments"):
                    tc["function"]["arguments"] += fn_delta["arguments"]

            if choice.get("finish_reason"):
                finish_reason = choice["finish_reason"]

    raw_content = "".join(content_parts) if content_parts else None
    if raw_content:
        raw_content = _strip_xml_tool_markup(raw_content)
        if not raw_content:
            raw_content = None
    message: dict = {
        "role": "assistant",
        "content": raw_content,
    }
    if tool_calls:
        message["tool_calls"] = [tool_calls[i] for i in sorted(tool_calls)]

    result = {
        "id": resp_id,
        "model": model,
        "choices": [{
            "message": message,
            "finish_reason": finish_reason or "stop",
            "index": 0,
        }],
    }
    if usage:
        result["usage"] = usage
    return result


class ToolLoopEngine:
    """Autonomous multi-turn LLM <-> tool execution loop."""

    def __init__(self, api_key, model, base_url="https://openrouter.ai/api/v1/chat/completions",
                 fallback_models=None, auth_store=None, provider_chain=None, usage_tracker=None):
        self._api_key = api_key
        self._model = model
        self.base_url = base_url
        self._auth_store = auth_store
        self._usage_tracker = usage_tracker
        self._session = requests.Session()
        self._fallback_chain = ModelFallbackChain(
            model, fallback_models or [],
            provider_chain=provider_chain,
        )

    @property
    def api_key(self):
        return self._api_key

    @api_key.setter
    def api_key(self, value):
        self._api_key = value

    @property
    def model(self):
        return self._model

    @model.setter
    def model(self, value):
        self._model = value
        self._fallback_chain.primary = value

    @property
    def fallback_chain(self) -> ModelFallbackChain:
        return self._fallback_chain

    @property
    def _headers(self):
        return {
            "Authorization": f"Bearer {self._api_key}",
            "Content-Type": "application/json",
            "HTTP-Referer": "https://github.com/ghost-ai",
            "X-Title": "Ghost AI Agent",
        }

    def _request_summary(self, messages, temperature, max_tokens):
        """When the loop ends without a text response, ask the LLM to summarize what it did."""
        summary_msgs = list(messages)
        summary_msgs.append({
            "role": "user",
            "content": (
                "You've completed your tool calls. Now provide a clear, concise final response "
                "summarizing what you found and what you did. Do NOT call any tools — "
                "just respond with your final answer in plain text."
            ),
        })
        payload = {
            "model": self.model,
            "messages": summary_msgs,
            "temperature": temperature,
            "max_tokens": max_tokens,
        }
        data, error = self._call_llm(payload)
        if error:
            return self._summarize_from_logs(messages)
        choices = data.get("choices", [])
        if choices:
            text = (choices[0].get("message", {}).get("content") or "").strip()
            if text:
                return text
        return self._summarize_from_logs(messages)

    def _summarize_from_logs(self, messages):
        """Last-resort: extract the last meaningful tool result as the response."""
        for msg in reversed(messages):
            if msg.get("role") == "tool":
                content = (msg.get("content") or "").strip()
                if content and len(content) > 20 and not content.startswith("OK, continuing"):
                    return content
            if msg.get("role") == "assistant":
                content = msg.get("content", "")
                if isinstance(content, str) and content.strip():
                    return content.strip()
        return "(Task completed — results were delivered through tool actions above)"

    @staticmethod
    def _sanitize_tool_messages(recent: list) -> list:
        """Ensure every tool-result message has a matching assistant tool_call.

        After slicing the recent window, an assistant message with tool_calls
        may land just outside the window while its tool-result messages are
        inside.  The API rejects orphaned tool results ('No tool call found
        for function call output').  Fix by removing orphaned tool messages.
        """
        valid_call_ids: set[str] = set()
        for m in recent:
            if m.get("role") == "assistant":
                for tc in (m.get("tool_calls") or []):
                    tc_id = tc.get("id", "")
                    if tc_id:
                        valid_call_ids.add(tc_id)

        sanitized = []
        for m in recent:
            if m.get("role") == "tool":
                tc_id = m.get("tool_call_id", "")
                if tc_id and tc_id not in valid_call_ids:
                    continue
            sanitized.append(m)
        return sanitized

    def _compact_messages(self, messages: list, step: int = -1,
                          compaction_count: int = 0) -> tuple[list, int]:
        """Two-phase context compaction.

        Phase 1 (always): Build a deterministic structured summary from old
                 messages, preserving tool call names and code signatures.
        Phase 2 (best-effort): Ask the LLM for a richer summary that captures
                 intent, decisions, and API details.  Falls back to phase 1
                 if the LLM call fails.

        Old messages are replaced with a SINGLE summary message so the
        context window stays bounded regardless of session length.
        """
        tokens_before = _estimate_context_tokens(messages)
        msgs_before = len(messages)

        system_msg = messages[0]
        recent = self._sanitize_tool_messages(messages[-20:])

        # Find the actual user message — the request that started this run.
        # When chat/channel history is provided, messages[1] is the first
        # history turn (oldest, least relevant).  We want the LAST user
        # message before tool calls started (the real request).
        user_msg_idx = 1
        for i in range(1, len(messages)):
            m = messages[i]
            if m.get("role") == "assistant" and m.get("tool_calls"):
                break
            if m.get("role") == "user" and "[Context Summary" not in (m.get("content") or ""):
                user_msg_idx = i

        # If the user message is already in the recent zone, don't duplicate it.
        recent_start = len(messages) - 20
        if user_msg_idx >= recent_start:
            old = [m for j, m in enumerate(messages) if 0 < j < recent_start]
            if not old:
                return messages, compaction_count
            det_summary = _build_deterministic_summary(old)
            preserved = [system_msg]
        else:
            user_msg = messages[user_msg_idx]
            old = [m for j, m in enumerate(messages)
                   if j != 0 and j != user_msg_idx and j < recent_start]
            if not old:
                return messages, compaction_count
            det_summary = _build_deterministic_summary(old)
            preserved = [system_msg, user_msg]

        has_prior_summary = any(
            "[Context Summary" in (m.get("content") or "")
            for m in old if m.get("role") == "user"
        )

        summary = det_summary
        condensed = ""
        try:
            condensed = _condense_for_llm_summary(old)
            # LLM summarization triggers:
            # 1. First compaction (no prior summary) with enough content
            # 2. Every 5th compaction — deterministic summaries degrade over
            #    repeated re-summarization; periodic LLM refresh keeps quality
            compaction_count += 1
            periodic_refresh = (compaction_count % 5 == 0)
            worth_llm = (
                condensed
                and len(condensed) > 3000
                and (not has_prior_summary or periodic_refresh)
            )
            if worth_llm:
                payload = {
                    "model": self.model,
                    "messages": [
                        {"role": "system", "content": _COMPACTION_SYSTEM_PROMPT},
                        {"role": "user", "content": condensed},
                    ],
                    "temperature": 0.1,
                    "max_tokens": _MAX_SUMMARY_TOKENS,
                }
                data, error = self._call_llm(payload, timeout=30)
                if not error:
                    choices = data.get("choices", [])
                    if choices:
                        text = (choices[0].get("message", {}).get("content") or "").strip()
                        if text and len(text) > 50:
                            summary = text
                            _ctx_logger.log_llm_summary_result(
                                step=step, success=True,
                                input_chars=len(condensed), output_chars=len(text),
                            )
                        else:
                            _ctx_logger.log_llm_summary_result(
                                step=step, success=False,
                                input_chars=len(condensed), output_chars=len(text) if text else 0,
                                error="LLM returned too-short summary",
                            )
                else:
                    _ctx_logger.log_llm_summary_result(
                        step=step, success=False,
                        input_chars=len(condensed), output_chars=0,
                        error=str(error)[:200],
                    )
        except Exception as exc:
            log.debug("LLM context summarization failed, using deterministic: %s", exc)
            _ctx_logger.log_llm_summary_result(
                step=step, success=False,
                input_chars=len(condensed) if condensed else 0, output_chars=0,
                error=str(exc)[:200],
            )

        summary_msg = {
            "role": "user",
            "content": (
                f"[Context Summary — older messages compacted to free context space]\n"
                f"{summary}"
            ),
        }
        result = preserved + [summary_msg] + recent
        tokens_after = _estimate_context_tokens(result)
        _ctx_logger.log_compaction(
            step=step, msgs_before=msgs_before, msgs_after=len(result),
            tokens_before=tokens_before, tokens_after=tokens_after,
            method="normal", llm_summary_used=(summary != det_summary),
        )
        return result, compaction_count

    def _emergency_compact(self, messages: list, attempt: int, step: int = -1,
                           compaction_count: int = 0) -> tuple[list, int]:
        """Aggressive compaction for context overflow recovery.

        Called when the API rejects our request as too large.  Each attempt
        is more aggressive than the last (overflow
        recovery loop):
          Attempt 1: Force normal compaction (even if < 30 messages).
          Attempt 2: Shrink recent window to 10 and truncate tool results.
          Attempt 3: Keep only system + user + last 5 messages.
        """
        tokens_before = _estimate_context_tokens(messages)
        msgs_before = len(messages)

        if attempt <= 1:
            return self._compact_messages(messages, step=step,
                                          compaction_count=compaction_count)

        system_msg = messages[0]

        if attempt == 2:
            recent = self._sanitize_tool_messages(messages[-10:])
            for i, m in enumerate(recent):
                if m.get("role") == "tool" and len(m.get("content") or "") > 1000:
                    recent[i] = {**m, "content": _smart_compact_tool_result(m["content"], 400)}
            result = [system_msg, {"role": "user", "content": "[Context aggressively compacted due to overflow]"}] + recent
        else:
            recent = self._sanitize_tool_messages(messages[-5:])
            for i, m in enumerate(recent):
                if m.get("role") == "tool" and len(m.get("content") or "") > 500:
                    recent[i] = {**m, "content": (m["content"])[:300] + "\n...(emergency trim)"}
            result = [system_msg, {"role": "user", "content": "[Emergency compaction — most context dropped]"}] + recent

        tokens_after = _estimate_context_tokens(result)
        _ctx_logger.log_compaction(
            step=step, msgs_before=msgs_before, msgs_after=len(result),
            tokens_before=tokens_before, tokens_after=tokens_after,
            method=f"emergency_{attempt}", llm_summary_used=False,
        )
        return result, compaction_count

    def _resolve_provider_call(self, provider_id: str, model: str, payload: dict):
        """Resolve base_url, headers, and adapted payload for a provider."""
        try:
            from ghost_providers import get_provider, build_headers, adapt_request
        except ImportError:
            return self.base_url, self._headers, dict(payload, model=model)

        provider = get_provider(provider_id)
        if not provider:
            return self.base_url, self._headers, dict(payload, model=model)

        api_key = ""
        account_id = ""
        if self._auth_store:
            if provider_id == "openai-codex":
                try:
                    from ghost_oauth import ensure_fresh_token
                    api_key = ensure_fresh_token(self._auth_store) or ""
                except Exception:
                    api_key = self._auth_store.get_api_key(provider_id)
                profile = self._auth_store.get_provider_profile(provider_id)
                if profile:
                    account_id = profile.get("account_id", "")
            else:
                api_key = self._auth_store.get_api_key(provider_id)

        if not api_key and provider_id == "openrouter":
            api_key = self._api_key

        headers = build_headers(provider, api_key)
        if provider_id == "openai-codex" and account_id:
            headers["ChatGPT-Account-Id"] = account_id

        adapted = adapt_request(provider, dict(payload, model=model))

        return provider.base_url, headers, adapted

    def _call_llm(self, payload, timeout=DEFAULT_TIMEOUT, on_token=None,
                  cancel_check=None, coding_model_chain=None):
        """Make an LLM API call with provider-aware fallback chain and jittered retry.

        All providers use streaming (SSE) to avoid blocking on slow responses.
        Each engine instance has its own requests.Session for connection pool
        isolation — chat traffic never competes with cron/evolve for sockets.

        If *on_token* is provided it is forwarded to the stream parser so
        each content-delta chunk is emitted in real time.

        If *cancel_check* returns True, the call aborts immediately with
        a CancelledError-style return so the tool loop can exit fast.

        If *coding_model_chain* is provided (list of (provider, model) tuples
        from the ModelDispatcher), those coding-quality models are tried first,
        in order, before falling back to the general fallback chain.  This
        ensures coding tasks stay on high-quality models even when the primary
        pick is temporarily unavailable.

        Flow:  for each (provider, model) in fallback chain →
                 resolve base_url, headers, adapted payload →
                 for each retry attempt →
                   call API with stream=True, parse SSE
               on success → adapt response, record_success, return
               on retriable failure → jittered backoff, next attempt
               on exhausted retries → record_failure, try next candidate
        """
        def _cancellable_sleep(seconds):
            """Sleep in small increments, aborting early if cancelled."""
            end = time.time() + seconds
            while time.time() < end:
                if cancel_check and cancel_check():
                    return True
                time.sleep(min(0.5, end - time.time()))
            return cancel_check() if cancel_check else False

        regular_candidates = self._fallback_chain.get_candidates()

        if coding_model_chain:
            # Coding-specific chain: all dispatched models first, then the
            # regular chain as a last-resort fallback.  Dedup so we don't
            # retry the same (provider, model) twice.
            seen = set()
            candidates = []
            for entry in coding_model_chain:
                if entry not in seen:
                    seen.add(entry)
                    candidates.append(entry)
            for entry in regular_candidates:
                if entry not in seen:
                    seen.add(entry)
                    candidates.append(entry)
        else:
            candidates = list(regular_candidates)
            # Legacy single-override: if the payload model differs from the
            # chain's primary, parse it and prepend as first candidate.
            payload_model = payload.get("model", "")
            chain_primary = self._fallback_chain.primary if self._fallback_chain._chain else ""
            if payload_model and payload_model != chain_primary:
                override_provider = "openrouter"
                override_model = payload_model
                _LP = frozenset({
                    "openrouter", "openai", "anthropic",
                    "google", "ollama", "openai-codex", "deepseek",
                })
                if ":" in payload_model:
                    idx = payload_model.index(":")
                    prefix = payload_model[:idx]
                    if prefix in _LP:
                        override_provider = prefix
                        override_model = payload_model[idx + 1:]
                override_entry = (override_provider, override_model)
                if override_entry not in candidates:
                    candidates = [override_entry] + candidates

        is_coding_call = coding_model_chain is not None
        coding_set = set(coding_model_chain) if coding_model_chain else set()
        all_errors = []

        for provider_id, model in candidates:
            if cancel_check and cancel_check():
                return None, "Cancelled by user"
            url, headers, adapted_payload = self._resolve_provider_call(
                provider_id, model, payload
            )

            is_codex = provider_id == "openai-codex"
            is_anthropic_native = provider_id == "anthropic"
            use_openai_stream = not is_codex and not is_anthropic_native

            if use_openai_stream:
                adapted_payload["stream"] = True
                adapted_payload["stream_options"] = {"include_usage": True}

            last_err = None
            if self._usage_tracker:
                self._usage_tracker.call_started(provider_id, model)

            max_attempts = MAX_RETRIES + 1
            rate_limit_hits = 0

            for attempt in range(RATE_LIMIT_MAX_RETRIES + 1):
                try:
                    if is_codex and attempt == 0:
                        log.debug("Codex request to %s | keys: %s", url, list(adapted_payload.keys()))

                    resp = self._session.post(
                        url, json=adapted_payload,
                        headers=headers, timeout=(10, timeout),
                        stream=(is_codex or use_openai_stream),
                    )
                    if resp.status_code == 429:
                        rate_limit_hits += 1
                        retry_after = _parse_retry_after(resp)
                        if retry_after:
                            wait = min(retry_after + random.uniform(0.5, 2.0), 120.0)
                        else:
                            wait = _jittered_delay(RATE_LIMIT_BASE_DELAY, rate_limit_hits - 1)
                        log.info("Rate-limited on %s:%s, waiting %.1fs (attempt %d/%d, Retry-After: %s)",
                                 provider_id, model, wait, attempt + 1,
                                 RATE_LIMIT_MAX_RETRIES + 1,
                                 f"{retry_after}s" if retry_after else "not set")
                        if _cancellable_sleep(wait):
                            return None, "Cancelled by user"
                        last_err = f"HTTP 429 on {provider_id}:{model}"
                        continue

                    if (is_codex or use_openai_stream) and resp.status_code != 200:
                        body = resp.text[:500]
                        log.error("%s:%s returned HTTP %d: %s",
                                  provider_id, model, resp.status_code, body[:200])
                        err_body = body[:200] if resp.status_code == 400 else body[:100]
                        last_err = f"HTTP {resp.status_code} on {provider_id}:{model}: {err_body}"
                        break

                    resp.raise_for_status()

                    if is_codex:
                        try:
                            raw = parse_codex_sse_response(resp, on_token=on_token)
                            provider = get_provider(provider_id)
                            data = adapt_response(provider, raw) if provider else raw
                        except RuntimeError as stream_err:
                            last_err = f"Codex stream error on {provider_id}:{model}: {stream_err}"
                            break
                    elif use_openai_stream:
                        try:
                            data = _parse_openai_stream(resp, on_token=on_token)
                            provider = get_provider(provider_id)
                            if provider:
                                data = adapt_response(provider, data)
                        except Exception as stream_err:
                            last_err = f"Stream parse error on {provider_id}:{model}: {stream_err}"
                            break
                    else:
                        data = resp.json()
                        try:
                            provider = get_provider(provider_id)
                            if provider:
                                data = adapt_response(provider, data)
                        except ImportError:
                            pass

                    if not (is_coding_call and (provider_id, model) in coding_set):
                        self._fallback_chain.record_success(provider_id, model)
                    primary = self._fallback_chain._chain[0] if self._fallback_chain._chain else None
                    if primary and (provider_id, model) != primary:
                        log.info("Served by fallback: %s:%s", provider_id, model)

                    if self._usage_tracker:
                        usage = data.get("usage", {}) if isinstance(data, dict) else {}
                        if isinstance(usage, dict):
                            total_tokens = usage.get("total_tokens") or (
                                usage.get("prompt_tokens", 0) + usage.get("completion_tokens", 0)
                            )
                        else:
                            total_tokens = 0
                        self._usage_tracker.call_completed(total_tokens, success=True)

                    return data, None

                except requests.exceptions.HTTPError as e:
                    status = e.response.status_code if e.response else 0
                    body = e.response.text[:300] if e.response else str(e)
                    if status == 404:
                        log.warning("Model %s:%s returned 404 (invalid model ID), removing from chain", provider_id, model)
                        self._fallback_chain.remove_from_chain(provider_id, model)
                        last_err = f"HTTP 404 on {provider_id}:{model}: invalid model ID"
                        break
                    if status == 429:
                        rate_limit_hits += 1
                        retry_after = _parse_retry_after(e.response)
                        if retry_after:
                            wait = min(retry_after + random.uniform(0.5, 2.0), 120.0)
                        else:
                            wait = _jittered_delay(RATE_LIMIT_BASE_DELAY, rate_limit_hits - 1)
                        log.info("Rate-limited (HTTPError) on %s:%s, waiting %.1fs (attempt %d)",
                                 provider_id, model, wait, attempt + 1)
                        if _cancellable_sleep(wait):
                            return None, "Cancelled by user"
                        last_err = f"HTTP 429 on {provider_id}:{model}: {body[:100]}"
                        continue
                    if status in (500, 502, 503) and attempt < MAX_RETRIES:
                        wait = _jittered_delay(RETRY_DELAY, attempt)
                        if _cancellable_sleep(wait):
                            return None, "Cancelled by user"
                        last_err = f"HTTP {status} on {provider_id}:{model}: {body[:100]}"
                        continue
                    err_body = body[:200] if status == 400 else body[:100]
                    last_err = f"HTTP {status} on {provider_id}:{model}: {err_body}"
                    break
                except requests.exceptions.Timeout:
                    if attempt < MAX_RETRIES:
                        if _cancellable_sleep(_jittered_delay(RETRY_DELAY, attempt)):
                            return None, "Cancelled by user"
                        last_err = f"Timeout on {provider_id}:{model}"
                        continue
                    last_err = f"Timeout on {provider_id}:{model} after retries"
                    break
                except requests.exceptions.ConnectionError as e:
                    if attempt < MAX_RETRIES:
                        if _cancellable_sleep(_jittered_delay(RETRY_DELAY, attempt)):
                            return None, "Cancelled by user"
                        last_err = f"Connection error on {provider_id}:{model}: {str(e)[:80]}"
                        continue
                    last_err = f"Connection error on {provider_id}:{model} after retries: {str(e)[:80]}"
                    break
                except Exception as e:
                    last_err = f"Error on {provider_id}:{model}: {e}"
                    break

            # Report call failure to usage tracker
            if self._usage_tracker:
                self._usage_tracker.call_completed(0, success=False)

            if not (is_coding_call and (provider_id, model) in coding_set):
                self._fallback_chain.record_failure(provider_id, model, last_err or "unknown")
            all_errors.append(last_err or f"{provider_id}:{model} failed")

        error_summary = " → ".join(all_errors)
        return None, f"All models failed: {error_summary}"

    def run(self, system_prompt, user_message, tool_registry=None,
            max_steps=DEFAULT_MAX_STEPS, temperature=0.3, max_tokens=DEFAULT_MAX_TOKENS,
            image_b64=None, images=None, on_step=None, force_tool=False, history=None,
            cancel_check=None, hook_runner=None, tool_intent_security=None, model_override=None,
            enable_reasoning=False, tool_event_bus=None, on_token=None,
            middleware_chain=None, coding_model_chain=None):
        """
        Run the autonomous tool loop.

        The agent keeps calling tools until it decides the task is complete
        and responds with a final text message. Like while(true) { work(); if done break; }

        Args:
            system_prompt: System message for the LLM.
            user_message: The user's input text.
            tool_registry: ToolRegistry instance (or None for single-shot).
            max_steps: Safety limit on max rounds (default 20).
            temperature: LLM temperature.
            max_tokens: Max tokens per LLM response.
            image_b64: Optional single base64-encoded image (legacy, use `images` instead).
            images: Optional list of image dicts: [{"data": "base64...", "mime": "image/png"}, ...]
            on_step: Optional callback(step_num, tool_name, tool_result).
            force_tool: If True, force tool use on step 0.
            history: Optional list of prior conversation turns
                     [{"role": "user", "content": "..."}, {"role": "assistant", "content": "..."}, ...]

        Returns:
            ToolLoopResult with final text, tool calls made, and token usage.
        """
        date_context = _build_date_context()
        
        # Apply reasoning mode instruction if enabled
        if enable_reasoning:
            try:
                from ghost_reasoning import build_reasoning_prompt
                effective_system_prompt = build_reasoning_prompt(system_prompt, enable_reasoning=True)
            except Exception as import_err:
                log.warning("Failed to apply reasoning prompt: %s", import_err)
                effective_system_prompt = system_prompt
        else:
            effective_system_prompt = system_prompt
        
        messages = [{"role": "system", "content": date_context + effective_system_prompt}]

        if history:
            messages.extend(history)
            messages = repair_dangling_tool_calls(messages)

        all_images = list(images or [])
        if image_b64 and not all_images:
            all_images.append({"data": image_b64, "mime": "image/png"})

        if all_images:
            content_parts = [{"type": "text", "text": user_message}]
            for img in all_images:
                mime = img.get("mime", "image/png")
                content_parts.append({
                    "type": "image_url",
                    "image_url": {"url": f"data:{mime};base64,{img['data']}"}
                })
            messages.append({"role": "user", "content": content_parts})
        else:
            messages.append({"role": "user", "content": user_message})

        if tool_event_bus:
            try:
                tool_event_bus.emit(
                    "on_chat_message",
                    role="user",
                    content=user_message,
                    session_id=_debug_logger._session_id or "",
                )
            except Exception:
                pass

        tools_schema = None
        if tool_registry and tool_registry.get_all():
            tools_schema = tool_registry.to_openai_schema()

        TASK_COMPLETE_TOOL = {
            "type": "function",
            "function": {
                "name": "task_complete",
                "description": (
                    "End your turn and send the summary to the user. "
                    "The summary parameter is shown to the user verbatim. "
                    "If you used evolve tools, you must have called evolve_submit_pr "
                    "(or evolve_deploy for self-repair) before calling this."
                ),
                "parameters": {
                    "type": "object",
                    "properties": {
                        "summary": {
                            "type": "string",
                            "description": (
                                "Your message to the user. Start directly with the answer — "
                                "NO preamble like 'You're right', 'Sure', 'Of course', 'Alright'. "
                                "Just answer naturally. Use second person (you/your)."
                            ),
                        },
                    },
                    "required": ["summary"],
                },
            },
        }
        if tools_schema:
            tools_schema.append(TASK_COMPLETE_TOOL)

        tool_calls_log = []
        total_tokens = 0
        final_text = ""
        consecutive_errors = 0
        loop_detector = LoopDetector()
        MAX_CRITICAL_BLOCKS = 3
        exit_reason = "max_steps"
        rctx = RunContext()

        def _cancel_msg():
            """Extract cancel reason from cancel_check (string = reason, True = default)."""
            result = cancel_check()
            if isinstance(result, str) and result:
                return result
            return "(Stopped)" if result else ""

        # Use model_override if provided, otherwise fall back to default model
        effective_model = model_override if model_override else self.model
        
        caller_name = traceback.extract_stack()[-2].name if len(traceback.extract_stack()) >= 2 else ""
        _debug_logger.session_start(
            user_message=user_message[:500] if isinstance(user_message, str) else str(user_message)[:500],
            model=effective_model,
            max_steps=max_steps,
            caller=caller_name,
        )
        rctx.session_id = _debug_logger._session_id or ""

        # Save parent session state before overwriting (nested run() inside
        # evolve_submit_pr would clobber the implementer's session otherwise)
        _prev_ctx_session = _ctx_logger._session_id
        _prev_ctx_caller = _ctx_logger._caller
        _prev_ctx_feature_id = _ctx_logger._feature_id
        _prev_ctx_feature_title = _ctx_logger._feature_title

        _ctx_logger.set_session(
            session_id=_debug_logger._session_id or "",
            caller=caller_name,
        )

        consecutive_task_completes = 0
        MAX_CONSECUTIVE_TASK_COMPLETES = 3

        tool_intent_security = tool_intent_security or ToolIntentSecurity({"enable_tool_intent_security": False})

        # Load Anthropic config once before the loop (not on every iteration)
        anthropic_cfg = _load_config() if self._fallback_chain.active_provider == "anthropic" else {}

        overflow_recovery_attempts = 0
        _llm_pool = concurrent.futures.ThreadPoolExecutor(max_workers=1)

        for step in range(max_steps):
            if cancel_check:
                _cmsg = _cancel_msg()
                if _cmsg:
                    final_text = _cmsg
                    exit_reason = "cancelled"
                    break

            try:
                from ghost_evolve import get_engine as _get_evo_engine
                if _get_evo_engine().deploy_in_progress:
                    if not final_text:
                        final_text = "(Deploy triggered — Ghost is restarting.)"
                    exit_reason = "deploy"
                    break
            except Exception:
                pass

            if step % 10 == 0:
                _ctx_logger.log_context_snapshot(
                    step=step, total_messages=len(messages),
                    tokens_estimated=_estimate_context_tokens(messages),
                    model=effective_model,
                )

            if consecutive_task_completes >= MAX_CONSECUTIVE_TASK_COMPLETES:
                _debug_logger.step_error(step,
                    f"Circuit breaker: task_complete accepted {consecutive_task_completes}x but loop continued")
                exit_reason = "task_complete"
                break

            if middleware_chain:
                try:
                    messages = middleware_chain.before_model(messages, step)
                except Exception as _mw_err:
                    log.warning("middleware_chain.before_model error at step %d: %s", step, _mw_err)

            payload = {
                "model": effective_model,
                "messages": messages,
                "temperature": temperature,
                "max_tokens": max_tokens,
            }

            # Add Claude 4.6+ API features if configured and using Anthropic provider
            if anthropic_cfg:
                effort = anthropic_cfg.get("anthropic_effort")
                if effort and effort in ("low", "medium", "high"):
                    payload["effort"] = effort
                
                if anthropic_cfg.get("anthropic_context_compaction"):
                    ratio = anthropic_cfg.get("anthropic_context_compaction_ratio", 0.5)
                    # Clamp ratio to valid range 0.0-1.0
                    ratio = max(0.0, min(1.0, float(ratio)))
                    payload["context_window_compression"] = {
                        "type": "prompt_caching",
                        "ratio": ratio
                    }

            is_last_step = (step == max_steps - 1)

            if tools_schema and not is_last_step:
                payload["tools"] = tools_schema
                if step == 0 and force_tool:
                    payload["tool_choice"] = "required"
                else:
                    payload["tool_choice"] = "auto"

            _msg_chars = sum(len(m.get("content", "") or "") for m in messages)
            _tools_chars = sum(
                len(t.get("function", {}).get("description", ""))
                + len(json.dumps(t.get("function", {}).get("parameters", {})))
                for t in (payload.get("tools") or [])
            )
            _est_tokens = (_msg_chars + _tools_chars) // 4
            log.info(
                "[token_audit] step=%d msgs=%d msg_chars=%d tool_schemas=%d est_tokens=%d",
                step, len(messages), _msg_chars, _tools_chars, _est_tokens,
            )

            try:
                future = _llm_pool.submit(
                    self._call_llm, payload, DEFAULT_TIMEOUT, on_token,
                    cancel_check=cancel_check,
                    coding_model_chain=coding_model_chain,
                )
            except RuntimeError:
                if sys.is_finalizing():
                    exit_reason = "interpreter_shutdown"
                    break
                raise
            deadline = time.time() + MAX_LLM_WALL_CLOCK
            data, error = None, None
            while True:
                if cancel_check:
                    _cmsg = _cancel_msg()
                    if _cmsg:
                        future.cancel()
                        final_text = _cmsg
                        exit_reason = "cancelled"
                        break
                try:
                    data, error = future.result(timeout=0.5)
                    break
                except concurrent.futures.TimeoutError:
                    if time.time() >= deadline:
                        data, error = None, f"Wall-clock timeout ({MAX_LLM_WALL_CLOCK}s) — model too slow"
                        log.warning("LLM call exceeded %ds wall clock at step %d", MAX_LLM_WALL_CLOCK, step)
                        break

            if exit_reason == "cancelled":
                break

            if error:
                consecutive_errors += 1
                _debug_logger.step_error(step, f"LLM error ({consecutive_errors}/3): {error}")

                if _CONTEXT_OVERFLOW_RE.search(error):
                    overflow_recovery_attempts += 1
                    _ctx_logger.log_overflow_recovery(
                        step=step, attempt=overflow_recovery_attempts,
                        error_snippet=error,
                    )
                    if consecutive_errors <= _MAX_OVERFLOW_RECOVERY_ATTEMPTS:
                        log.warning(
                            "Context overflow at step %d (attempt %d/%d) — compacting and retrying",
                            step, consecutive_errors, _MAX_OVERFLOW_RECOVERY_ATTEMPTS,
                        )
                        messages, rctx.compaction_count = self._emergency_compact(
                            messages, consecutive_errors, step=step,
                            compaction_count=rctx.compaction_count)
                        continue
                    log.error("Context overflow persists after %d compaction attempts", consecutive_errors)
                    final_text = f"Context overflow — could not reduce context after {consecutive_errors} attempts"
                    exit_reason = "context_overflow"
                    if tool_event_bus:
                        try:
                            tool_event_bus.emit(
                                "on_tool_loop_error",
                                session_id=_debug_logger._session_id or "",
                                error=final_text,
                                step=step,
                            )
                        except Exception:
                            pass
                    break

                if consecutive_errors >= 3:
                    final_text = error
                    exit_reason = "llm_error"
                    if tool_event_bus:
                        try:
                            tool_event_bus.emit(
                                "on_tool_loop_error",
                                session_id=_debug_logger._session_id or "",
                                error=error[:500],
                                step=step,
                            )
                        except Exception:
                            pass
                    break
                messages.append({"role": "assistant", "content": f"(Internal error: {error}. Retrying...)"})
                time.sleep(1)
                continue

            consecutive_errors = 0
            usage = data.get("usage", {})
            total_tokens += usage.get("total_tokens") or (
                usage.get("prompt_tokens", 0) + usage.get("completion_tokens", 0)
            )

            choices = data.get("choices", [])
            if not choices:
                final_text = "LLM returned no choices"
                break

            choice = choices[0]
            msg = choice.get("message") or {}
            if msg.get("content") is None:
                msg["content"] = ""

            messages.append(msg)

            if middleware_chain:
                try:
                    override_msg = middleware_chain.after_model(messages, msg, step)
                    if override_msg is not None:
                        messages[-1] = override_msg
                        msg = override_msg
                except Exception as _mw_err:
                    log.warning("middleware_chain.after_model error at step %d: %s", step, _mw_err)

            if msg.get("tool_calls") and tool_registry:
                msg["tool_calls"] = guard_model_output(msg["tool_calls"])
                if not msg["tool_calls"]:
                    del msg["tool_calls"]
            if msg.get("tool_calls") and tool_registry:
                assistant_text = (msg.get("content") or "").strip()
                if assistant_text and on_step:
                    try:
                        on_step(step, "__reasoning__", assistant_text)
                    except Exception:
                        pass
                rctx.consecutive_text_only = 0
                step_loop_warnings: list[str] = []
                for tc in msg["tool_calls"]:
                    # If a deploy was triggered by a prior tool in this batch,
                    # skip remaining calls — Ghost is about to restart.
                    try:
                        from ghost_evolve import get_engine as _get_evo_engine
                        if _get_evo_engine().deploy_in_progress:
                            _debug_logger.step_error(step,
                                "Skipping remaining tool calls — deploy in progress")
                            tc_id = tc.get("id", f"tc_{step}_skipped")
                            messages.append({
                                "role": "tool",
                                "tool_call_id": tc_id,
                                "content": "SKIPPED — deploy in progress, Ghost is restarting.",
                            })
                            continue
                    except Exception:
                        pass

                    fn_name = str(tc.get("function", {}).get("name", "")).strip()
                    raw_args = tc.get("function", {}).get("arguments", "{}")
                    try:
                        fn_args = json.loads(raw_args) if raw_args else {}
                    except (json.JSONDecodeError, TypeError) as parse_err:
                        fn_args = {"__parse_error": str(parse_err), "__raw_len": len(raw_args) if raw_args else 0}

                    tc_id = tc.get("id", f"tc_{step}_{fn_name}")

                    if fn_name == "task_complete":
                        summary = fn_args.get("summary", "")
                        workflow_issue = _check_incomplete_workflows(tool_calls_log)
                        if workflow_issue:
                            tool_result = (
                                f"REJECTED — you cannot complete yet. {workflow_issue} "
                                "Finish the workflow first, then call task_complete again."
                            )
                            _debug_logger.step_tool_call(step, "task_complete", fn_args,
                                                         f"REJECTED: {workflow_issue}")
                            messages.append({
                                "role": "tool",
                                "tool_call_id": tc_id,
                                "content": tool_result,
                            })
                            continue
                        final_text = summary
                        exit_reason = "task_complete"
                        consecutive_task_completes += 1
                        _debug_logger.step_tool_call(step, "task_complete", fn_args, "OK")
                        messages.append({
                            "role": "tool",
                            "tool_call_id": tc_id,
                            "content": "OK, task complete.",
                        })
                        if on_step:
                            try:
                                on_step(step, "task_complete", summary[:200])
                            except Exception:
                                pass
                        break

                    consecutive_task_completes = 0

                    if cancel_check:
                        _cmsg = _cancel_msg()
                        if _cmsg:
                            final_text = _cmsg
                            exit_reason = "cancelled"
                            break

                    detection = loop_detector.check(fn_name, fn_args)
                    call_id = loop_detector.record_call(fn_name, fn_args)
                    loop_hint = ""
                    tool_duration_ms = 0

                    if detection.stuck and detection.level == "critical":
                        rctx.critical_blocks += 1
                        tool_result = detection.message
                        loop_detector.record_result(call_id, tool_result)
                        loop_hint = f"BLOCKED:{detection.detector}"
                        if rctx.critical_blocks >= MAX_CRITICAL_BLOCKS:
                            final_text = (
                                f"(Loop terminated: {rctx.critical_blocks} critical blocks hit. "
                                "The model was stuck repeating the same tool calls.)"
                            )
                            exit_reason = "critical_loop_break"
                            _debug_logger.step_error(step,
                                f"Force-exit: {rctx.critical_blocks} critical blocks reached")
                            break
                    else:
                        warning_text = ""
                        if detection.stuck and detection.level == "warning":
                            warning_text = detection.message
                            loop_hint = f"WARN:{detection.detector}"
                            if loop_detector._warning_count >= 6:
                                final_text = (
                                    f"(Loop terminated: model stuck calling the same tools "
                                    f"repeatedly — {loop_detector._warning_count} warnings accumulated.)"
                                )
                                exit_reason = "warning_accumulation_break"
                                _debug_logger.step_error(step,
                                    f"Force-exit: {loop_detector._warning_count} loop warnings")
                                break

                        if warning_text:
                            tool_result = warning_text
                            loop_detector.record_result(call_id, tool_result)
                            step_loop_warnings.append(warning_text)
                            _debug_logger.step_tool_call(
                                step, fn_name, fn_args, tool_result,
                                duration_ms=0,
                                loop_detection=loop_hint,
                            )
                            tool_calls_log.append({
                                "step": step,
                                "tool": fn_name,
                                "args": fn_args,
                                "result": tool_result[:3000],
                            })
                            if on_step:
                                try:
                                    on_step(step, fn_name, tool_result)
                                except Exception:
                                    pass
                            messages.append({
                                "role": "tool",
                                "tool_call_id": tc_id,
                                "content": tool_result,
                            })
                            continue

                        exec_args = fn_args
                        if hook_runner:
                            modified_args = hook_runner.run("before_tool_call", fn_name, fn_args)
                            if modified_args is not None and isinstance(modified_args, dict):
                                exec_args = modified_args

                        envelope = tool_intent_security.create_envelope(
                            tool_name=fn_name,
                            args=exec_args,
                            session_id=getattr(_debug_logger, "_session_id", ""),
                            policy_level="standard",
                        )
                        ok_intent, reason_intent = tool_intent_security.verify_envelope(
                            envelope=envelope,
                            tool_name=fn_name,
                            args=exec_args,
                            session_id=getattr(_debug_logger, "_session_id", ""),
                        )

                        if on_step and fn_name in ("evolve_submit_pr", "evolve_deploy", "evolve_test"):
                            try:
                                on_step(step, fn_name, "(running...)")
                            except Exception:
                                pass

                        if fn_name == "evolve_submit_pr":
                            verify_issue = _check_verification_before_submit(tool_calls_log)
                            if verify_issue:
                                tool_result = verify_issue
                                loop_detector.record_result(call_id, tool_result)
                                _debug_logger.step_tool_call(step, fn_name, fn_args,
                                                             f"BLOCKED: verification missing")
                                if on_step:
                                    try:
                                        on_step(step, fn_name, tool_result)
                                    except Exception:
                                        pass
                                messages.append({
                                    "role": "tool",
                                    "tool_call_id": tc_id,
                                    "content": tool_result,
                                })
                                tool_calls_log.append({
                                    "tool": fn_name, "args": fn_args,
                                    "result": tool_result[:3000],
                                    "step": step, "blocked": True,
                                })
                                continue

                        _mw_intercepted = None
                        if middleware_chain:
                            try:
                                _mw_intercepted = middleware_chain.wrap_tool_call(fn_name, exec_args, step)
                            except Exception as _mw_err:
                                log.warning("middleware_chain.wrap_tool_call error for %s: %s", fn_name, _mw_err)

                        t0 = time.time()
                        if _mw_intercepted is not None:
                            tool_result = _mw_intercepted
                        else:
                            try:
                                if not ok_intent:
                                    tool_result = f"BLOCKED by tool-intent security: {reason_intent}"
                                else:
                                    _caller_ctx = get_shell_caller_context()
                                    def _exec_tool_with_ctx(_ctx=_caller_ctx, _name=fn_name, _args=exec_args):
                                        set_shell_caller_context(_ctx)
                                        return tool_registry.execute(_name, _args)
                                    try:
                                        tool_future = _llm_pool.submit(_exec_tool_with_ctx)
                                    except RuntimeError:
                                        if sys.is_finalizing():
                                            tool_result = "Aborted: interpreter is shutting down"
                                            break
                                        raise
                                    _tool_deadline = time.time() + DEFAULT_TOOL_TIMEOUT
                                    while True:
                                        if cancel_check:
                                            _cmsg = _cancel_msg()
                                            if _cmsg:
                                                tool_future.cancel()
                                                tool_result = _cmsg
                                                break
                                        if time.time() >= _tool_deadline:
                                            tool_future.cancel()
                                            tool_result = (
                                                f"Tool error ({fn_name}): execution timed out after "
                                                f"{DEFAULT_TOOL_TIMEOUT}s. The tool was killed. "
                                                f"Do NOT retry — inform the user that this operation "
                                                f"took too long and suggest breaking it into smaller steps."
                                            )
                                            log.warning(
                                                "Tool '%s' timed out after %ds — abandoning thread and recycling pool",
                                                fn_name, DEFAULT_TOOL_TIMEOUT,
                                            )
                                            _llm_pool.shutdown(wait=False)
                                            _llm_pool = concurrent.futures.ThreadPoolExecutor(max_workers=1)
                                            break
                                        try:
                                            tool_result = tool_future.result(timeout=0.5)
                                            break
                                        except concurrent.futures.TimeoutError:
                                            continue
                            except Exception as e:
                                tool_result = f"Tool execution failed: {e}"

                        tool_duration_ms = (time.time() - t0) * 1000

                        if cancel_check:
                            _cmsg = _cancel_msg()
                            if _cmsg:
                                final_text = _cmsg
                                exit_reason = "cancelled"
                                break

                        if hook_runner:
                            modified_result = hook_runner.run(
                                "after_tool_call", fn_name, exec_args, tool_result
                            )
                            if modified_result is not None and isinstance(modified_result, str):
                                tool_result = modified_result

                        if middleware_chain:
                            try:
                                _mw_result = middleware_chain.after_tool_call(fn_name, exec_args, tool_result, step)
                                if _mw_result is not None:
                                    tool_result = _mw_result
                            except Exception as _mw_err:
                                log.warning("middleware_chain.after_tool_call error for %s: %s", fn_name, _mw_err)

                        if tool_event_bus:
                            try:
                                tool_event_bus.emit(
                                    "on_tool_call",
                                    tool_name=fn_name,
                                    args=exec_args,
                                    result=tool_result[:500] if tool_result else "",
                                    session_id=_debug_logger._session_id or "",
                                    step=step,
                                )
                            except Exception:
                                pass

                        loop_detector.record_result(call_id, tool_result)

                    _debug_logger.step_tool_call(
                        step, fn_name, fn_args, tool_result,
                        duration_ms=tool_duration_ms,
                        loop_detection=loop_hint,
                    )

                    tool_calls_log.append({
                        "step": step,
                        "tool": fn_name,
                        "args": fn_args,
                        "result": tool_result[:3000],
                    })

                    if fn_name == "evolve_test" and "Tests FAILED" in tool_result:
                        consec = _count_consecutive_test_failures(tool_calls_log)
                        if consec >= _MAX_CONSECUTIVE_TEST_FAILURES:
                            tool_result += (
                                f"\n\n⛔ HARD LIMIT: {consec} consecutive test failures. "
                                "You MUST stop trying to fix this feature. Call "
                                "fail_future_feature(feature_id, 'Exceeded max test failures') "
                                "then task_complete immediately. Do NOT attempt more fixes."
                            )

                    if fn_name == "evolve_submit_pr" and "BLOCKED by reviewer" in tool_result:
                        step_loop_warnings.append(
                            "⛔ PR was BLOCKED by reviewer and the feature has been rejected. "
                            "There is NOTHING more you can do for this feature. "
                            "Do NOT investigate, do NOT retry, do NOT explore the codebase. "
                            "Call task_complete(summary='Feature blocked by reviewer.') NOW."
                        )

                    if on_step:
                        try:
                            on_step(step, fn_name, tool_result)
                        except Exception:
                            pass

                    result_limit = _adaptive_tool_result_limit(messages)
                    if result_limit < 6000:
                        _ctx_logger.log_adaptive_limit(
                            step=step,
                            tokens_estimated=_estimate_context_tokens(messages),
                            limit_applied=result_limit,
                        )
                    messages.append({
                        "role": "tool",
                        "tool_call_id": tc_id,
                        "content": tool_result[:result_limit],
                    })

                    if "__parse_error" in fn_args and fn_name == "evolve_apply":
                        rctx.malformed_json_count += 1
                        messages.append({
                            "role": "user",
                            "content": (
                                f"⛔ MALFORMED JSON (attempt {rctx.malformed_json_count}/3). "
                                "Your output was truncated because the file content exceeded "
                                "your output token limit. You MUST use CHUNKED WRITES:\n"
                                "  1. evolve_apply(evo_id, file_path, content='<first ~80 lines>')\n"
                                "  2. evolve_apply(evo_id, file_path, content='<next ~80 lines>', append=True)\n"
                                "  3. Repeat with append=True for remaining chunks.\n"
                                "Keep each chunk under 80 lines. Do NOT retry with full content."
                            ),
                        })
                        if rctx.malformed_json_count >= 3:
                            messages.append({
                                "role": "user",
                                "content": (
                                    "⛔ 3 MALFORMED JSON FAILURES. You keep exceeding the output limit. "
                                    "Call fail_future_feature(feature_id, 'Output token limit exceeded — "
                                    "file too large for single tool call') then task_complete."
                                ),
                            })

                if step_loop_warnings:
                    combined = "\n".join(dict.fromkeys(step_loop_warnings))
                    messages.append({
                        "role": "user",
                        "content": (
                            f"⚠️ LOOP DETECTION WARNING (step {step}):\n{combined}\n\n"
                            "You MUST change your approach NOW. The tool calls above were "
                            "BLOCKED and did NOT execute. Use different tools or call "
                            "task_complete to end the session."
                        ),
                    })

                if exit_reason in ("task_complete", "cancelled",
                                   "critical_loop_break", "warning_accumulation_break"):
                    break

                if cancel_check:
                    _cmsg = _cancel_msg()
                    if _cmsg:
                        if not final_text:
                            final_text = _cmsg
                        exit_reason = "cancelled"
                        break

                # --- Auto-collect parallel subagent results ---
                # Scan this step's tool results for task() submissions.
                # task() returns {"submitted": true, "task_id": "xxx"}.
                # We extract all task_ids and wait for them in parallel.
                try:
                    _step_task_ids = []
                    for _tc_entry in tool_calls_log:
                        if (_tc_entry.get("step") == step
                                and _tc_entry.get("tool") == "task"):
                            _raw = _tc_entry.get("result", "")
                            if "task_id" in _raw:
                                import re as _tid_re
                                _m = _tid_re.search(r'"task_id":\s*"([a-f0-9]+)"', _raw)
                                if _m:
                                    _step_task_ids.append(_m.group(1))

                    if _step_task_ids:
                        from ghost_subagent_config import wait_for_tasks
                        log.info("Auto-collecting %d parallel subagent task(s): %s",
                                 len(_step_task_ids), _step_task_ids)
                        _collected = wait_for_tasks(_step_task_ids, timeout=900)
                        _parts = []
                        for _tid in _step_task_ids:
                            _r = _collected.get(_tid, {"error": "No result"})
                            if _r.get("success"):
                                _parts.append(
                                    f"[task_id={_tid}, type={_r.get('subagent_type', '?')}, "
                                    f"steps={_r.get('steps_used', 0)}, "
                                    f"time={_r.get('duration_ms', 0)}ms]\n{_r['result']}"
                                )
                            else:
                                _parts.append(
                                    f"[task_id={_tid}, FAILED] "
                                    f"{_r.get('error', 'Unknown error')}"
                                )
                        messages.append({
                            "role": "user",
                            "content": (
                                f"[Subagent Results — {len(_step_task_ids)} "
                                f"task(s) completed]\n\n"
                                + "\n\n---\n\n".join(_parts)
                            ),
                        })
                except ImportError:
                    pass
                except Exception as _ac_err:
                    log.warning("Subagent auto-collect error: %s", _ac_err)

                needs_compact = (
                    len(messages) > 30
                    or _estimate_context_tokens(messages) > _COMPACT_TOKEN_THRESHOLD
                )
                if needs_compact and len(messages) > 22:
                    messages, rctx.compaction_count = self._compact_messages(
                        messages, step=step, compaction_count=rctx.compaction_count)
            else:
                text_content = (msg.get("content") or "").strip()
                if not tool_calls_log:
                    # Only push back on a plain-text first turn when the caller
                    # forced tool use (cron/evolve/URL fetches). For ordinary
                    # conversational turns (force_tool=False) a direct text reply
                    # is the desired behavior — don't make "hi" call a tool.
                    if force_tool and tools_schema and step == 0 and step < max_steps - 2:
                        pushback = (
                            "You answered with a plain text message instead of using your tools. "
                            "You MUST use tools to complete tasks — do NOT answer from memory or assumptions. "
                            "Use the available tools (file_read, shell_exec, web_fetch, etc.) to gather real information, "
                            "then call task_complete(summary='...') with your findings. "
                            "Do NOT respond with plain text again — call a tool NOW."
                        )
                        _debug_logger.step_text_response(step, text_content, "pushback_first_turn_no_tools")
                        messages.append({
                            "role": "user",
                            "content": pushback,
                        })
                        continue

                    final_text = text_content
                    exit_reason = "first_text_response"
                    _debug_logger.step_text_response(step, text_content, "accepted_first_turn")
                    break

                if tools_schema and step < max_steps - 2:
                    if not text_content:
                        rctx.consecutive_empty += 1
                        if rctx.consecutive_empty >= 10:
                            _debug_logger.step_text_response(
                                step, "", f"break_empty_loop_{rctx.consecutive_empty}")
                            final_text = (
                                "(Session terminated: model produced "
                                f"{rctx.consecutive_empty} consecutive empty responses.)"
                            )
                            exit_reason = "empty_response_loop"
                            break
                        if rctx.consecutive_empty == 3:
                            messages.append({
                                "role": "user",
                                "content": (
                                    "Your last 3 responses were empty (no text and no tool calls). "
                                    "You appear to be stuck. Describe what you are trying to do "
                                    "and what is blocking you, then call the appropriate tool. "
                                    "If a previous tool call failed, read the error carefully and "
                                    "try a different approach. Do NOT produce another empty response."
                                ),
                            })
                        elif rctx.consecutive_empty == 6:
                            messages, rctx.compaction_count = self._compact_messages(
                                messages, step=step,
                                compaction_count=rctx.compaction_count)
                            messages.append({
                                "role": "user",
                                "content": (
                                    "URGENT: 6 consecutive empty responses. Context has been "
                                    "compacted to give you more output room. You MUST call a "
                                    "tool NOW or call task_complete to end this session."
                                ),
                            })
                    else:
                        rctx.consecutive_empty = 0

                    rctx.consecutive_text_only += 1

                    # After 3 consecutive text-only responses, compact context
                    # to free output token budget for tool calls
                    if rctx.consecutive_text_only == 3 and len(messages) > 22:
                        messages, rctx.compaction_count = self._compact_messages(
                            messages, step=step, compaction_count=rctx.compaction_count)

                    workflow_issue = _check_incomplete_workflows(tool_calls_log)

                    # Accept the text response directly when:
                    # 1. The model already called tools (tool_calls_log non-empty)
                    # 2. No incomplete evolve workflow that must be finished
                    # 3. The text is substantive (not a short filler sentence)
                    # Pushing back forces models to re-wrap their answer in
                    # task_complete, which frequently produces garbage or
                    # meta-explanations instead of the actual content.
                    if not workflow_issue and text_content and len(text_content) > 20:
                        is_deferring = (
                            bool(_DEFERRAL_RE.search(text_content))
                            and len(text_content) < 400
                        )
                        few_tools = len(tool_calls_log) < _MIN_TOOLS_BEFORE_ACCEPT_DEFERRAL
                        if is_deferring and few_tools and rctx.consecutive_text_only <= 1:
                            pushback = (
                                "You're giving up too early — you have more tools available. "
                                "Try a different approach NOW:\n"
                                "- Use web_fetch on a specific URL to get actual page content\n"
                                "- Use the browser tool if web_fetch didn't return enough\n"
                                "- Use shell_exec or grep to search locally\n"
                                "Do NOT ask the user what they want — just try it yourself."
                            )
                            _debug_logger.step_text_response(step, text_content, "pushback_deferral")
                            messages.append({"role": "user", "content": pushback})
                            continue

                        # Browser task incomplete detection: if the model used
                        # browser tools but admits the task isn't done, push back
                        # and force it to keep going autonomously.
                        browser_used = any(
                            tc.get("name") == "browser" for tc in tool_calls_log
                        )
                        task_incomplete = bool(_INCOMPLETE_TASK_RE.search(text_content))
                        too_few_browser_steps = (
                            browser_used
                            and sum(1 for tc in tool_calls_log if tc.get("name") == "browser") < _MIN_BROWSER_STEPS
                        )

                        if (task_incomplete or too_few_browser_steps) and rctx.consecutive_text_only <= 2:
                            pushback = (
                                "You stopped but the task is NOT complete. "
                                "You are an AUTONOMOUS agent — you MUST self-debug and keep going.\n\n"
                                "WHAT TO DO NOW:\n"
                                "1. Take a snapshot to see the current page state\n"
                                "2. If a dialog/modal is still open, your previous click didn't work\n"
                                "3. Escalate your click approach:\n"
                                "   - Try 'evaluate' with JS: find the element by text content, then "
                                "dispatch full event sequence (pointerdown, mousedown, pointerup, mouseup, click)\n"
                                "   - Try clicking a PARENT element instead of the exact ref\n"
                                "   - Try keyboard navigation: focus the element, then press Enter\n"
                                "4. After each attempt, snapshot to verify the page actually changed\n"
                                "5. Keep iterating until the task is VERIFIABLY complete with a screenshot\n\n"
                                "DO NOT respond with text. Call browser tool NOW."
                            )
                            _debug_logger.step_text_response(step, text_content, "pushback_browser_incomplete")
                            messages.append({"role": "user", "content": pushback})
                            continue

                        final_text = text_content
                        exit_reason = "text_after_tools"
                        _debug_logger.step_text_response(step, text_content, "accepted_after_tools")
                        break

                    if rctx.consecutive_text_only >= 5:
                        in_evolve = any(
                            tc["tool"] in ("evolve_plan", "evolve_apply", "evolve_resume")
                            for tc in tool_calls_log
                        )
                        if in_evolve:
                            action_list = (
                                "Pick ONE of these actions:\n"
                                "  - evolve_apply(evolution_id=..., file_path='...', patches=[...])\n"
                                "  - evolve_test(evolution_id=...)\n"
                                "  - fail_future_feature(feature_id=..., reason='...')\n"
                                "  - task_complete(summary='...')"
                            )
                        else:
                            action_list = (
                                "Pick ONE of these actions:\n"
                                "  - file_read, grep, shell_exec, or web_search to make progress\n"
                                "  - task_complete(summary='...') to finish"
                            )
                        pushback = (
                            "CRITICAL: You have produced {n} text responses without "
                            "calling any tool. You MUST call a tool NOW. "
                            "Do NOT explain what you will do — just call the tool.\n\n"
                            "{actions}"
                        ).format(n=rctx.consecutive_text_only, actions=action_list)
                    else:
                        pushback = (
                            "Call task_complete(summary='...') to send your reply. "
                            "Put your full answer in the summary parameter — "
                            "start directly with the content, no preamble."
                        )
                    if workflow_issue:
                        pushback += f"\n\nBLOCKER: {workflow_issue}"
                    _debug_logger.step_text_response(step, text_content,
                        f"pushback_sent{'_with_blocker' if workflow_issue else ''}")
                    messages.append({
                        "role": "user",
                        "content": pushback,
                    })
                    continue

                final_text = text_content
                exit_reason = "text_at_end"
                _debug_logger.step_text_response(step, text_content, "accepted_at_end")
                break

        if not final_text and messages:
            last = messages[-1]
            if isinstance(last, dict) and last.get("role") == "assistant":
                content = last.get("content", "")
                if isinstance(content, str):
                    final_text = content.strip()

            if not final_text and tool_calls_log:
                final_text = self._request_summary(messages, temperature, max_tokens)

        used_evolve = any(
            tc["tool"].startswith("evolve_") for tc in tool_calls_log
        )
        if used_evolve:
            try:
                from ghost_evolve import get_engine
                run_evo_ids = set()
                for tc in tool_calls_log:
                    if tc["tool"] == "evolve_plan":
                        result = tc.get("result", "")
                        if "Evolution planned:" in result:
                            evo_id = result.split("Evolution planned:")[1].strip().split()[0]
                            run_evo_ids.add(evo_id)
                    elif tc["tool"] == "evolve_resume":
                        result = tc.get("result", "")
                        if "resumed on branch" in result:
                            for word in result.split():
                                if word.startswith("evolve/"):
                                    run_evo_ids.add(word.replace("evolve/", "").rstrip("."))
                                    break
                        evo_arg = tc.get("args", {}).get("evolution_id", "")
                        if evo_arg:
                            run_evo_ids.add(evo_arg)
                cleanup_results = get_engine().cleanup_incomplete(
                    only_ids=run_evo_ids if run_evo_ids else None
                )
                if cleanup_results:
                    rollback_msgs = []
                    for evo_id, ok, msg in cleanup_results:
                        rollback_msgs.append(msg)
                    rollback_notice = "\n".join(rollback_msgs)
                    final_text = (
                        (final_text or "") +
                        f"\n\n⚠️ **Incomplete evolution rolled back**\n{rollback_notice}\n"
                        "Evolutions must complete the full cycle: "
                        "evolve_plan → evolve_apply → evolve_test → evolve_submit_pr."
                    )
            except Exception:
                pass

        unique_tools = list(set(tc["tool"] for tc in tool_calls_log)) if tool_calls_log else []
        steps_used = len(set(tc["step"] for tc in tool_calls_log)) if tool_calls_log else 0
        _debug_logger.session_end(
            steps_used=steps_used,
            tools_used=unique_tools,
            total_tokens=total_tokens,
            exit_reason=exit_reason,
            final_text=final_text,
        )
        _ctx_logger.log_context_snapshot(
            step=steps_used, total_messages=len(messages),
            tokens_estimated=_estimate_context_tokens(messages),
            model=effective_model,
        )

        # Only log implementer compliance when this run() IS the implementer
        # (has evolve_plan in tool_calls).
        has_evolve_tools = any(
            tc["tool"] in ("evolve_plan", "evolve_apply")
            for tc in tool_calls_log
        ) if tool_calls_log else False
        # Feature ID may have been set during run() (start_future_feature)
        # and cleared during run() (complete_future_feature). Extract from
        # tool_calls_log as last resort.
        feature_for_compliance = _prev_ctx_feature_id or _ctx_logger._feature_id
        if not feature_for_compliance and tool_calls_log:
            for tc in tool_calls_log:
                if tc["tool"] == "start_future_feature":
                    result = tc.get("result", "")
                    import re as _re
                    m = _re.search(r'\[([a-f0-9]{10})\]', result)
                    if m:
                        feature_for_compliance = m.group(1)
                    break
        feature_title_for_compliance = _prev_ctx_feature_title or _ctx_logger._feature_title
        if feature_for_compliance and tool_calls_log and has_evolve_tools:
            saved_fid = _ctx_logger._feature_id
            saved_ftitle = _ctx_logger._feature_title
            _ctx_logger._feature_id = feature_for_compliance
            _ctx_logger._feature_title = feature_title_for_compliance
            _ctx_logger.log_skill_compliance(
                role="implementer", tool_calls=tool_calls_log,
            )
            _ctx_logger._feature_id = saved_fid
            _ctx_logger._feature_title = saved_ftitle

        # Restore parent session state if we're returning from a nested run()
        if _prev_ctx_session and _prev_ctx_session != _ctx_logger._session_id:
            _ctx_logger.set_session(
                session_id=_prev_ctx_session,
                caller=_prev_ctx_caller,
            )
            if _prev_ctx_feature_id:
                _ctx_logger._feature_id = _prev_ctx_feature_id
                _ctx_logger._feature_title = _prev_ctx_feature_title

        if tool_event_bus:
            try:
                tool_event_bus.emit(
                    "on_tool_loop_complete",
                    session_id=_debug_logger._session_id or "",
                    tool_count=len(tool_calls_log),
                    steps=steps_used,
                    exit_reason=exit_reason or "unknown",
                )
            except Exception:
                pass

        _llm_pool.shutdown(wait=False)

        if final_text:
            final_text = _strip_xml_tool_markup(final_text)

        return ToolLoopResult(
            text=final_text,
            tool_calls=tool_calls_log,
            total_tokens=total_tokens,
            steps=len(set(tc["step"] for tc in tool_calls_log)) if tool_calls_log else 0,
        )

    def single_shot(self, system_prompt, user_message, temperature=0.2,
                    max_tokens=1024, image_b64=None, images=None):
        """Backwards-compatible single-shot call with no tools."""
        result = self.run(
            system_prompt=system_prompt,
            user_message=user_message,
            tool_registry=None,
            max_steps=1,
            temperature=temperature,
            max_tokens=max_tokens,
            image_b64=image_b64,
            images=images,
        )
        return result.text


@dataclass
class RunContext:
    """Per-invocation state bucket — the Ghost equivalent of DeerFlow's ThreadState.

    Every counter that previously lived on self._ of ToolLoopEngine is now
    scoped to a single run() call via this object, eliminating cross-run bleed.
    """
    session_id: str = ""
    compaction_count: int = 0
    consecutive_text_only: int = 0
    consecutive_empty: int = 0
    malformed_json_count: int = 0
    critical_blocks: int = 0


class ToolLoopResult:
    """Result from a tool loop run."""
    __slots__ = ("text", "tool_calls", "total_tokens", "steps")

    def __init__(self, text, tool_calls, total_tokens, steps):
        self.text = text
        self.tool_calls = tool_calls
        self.total_tokens = total_tokens
        self.steps = steps

    def summary(self):
        if not self.tool_calls:
            return self.text
        tools_used = ", ".join(set(tc["tool"] for tc in self.tool_calls))
        return f"[Used: {tools_used}]\n{self.text}"


def _sanitize_tool_params(params: dict) -> dict:
    """Fix common schema issues that cause API rejections.

    OpenAI function-calling requires single-string ``type`` values
    (e.g. ``"string"``), not JSON Schema union arrays (``["string", "object"]``).
    Some tools define union types which are valid JSON Schema but rejected
    by providers like OpenAI/Codex.  This normalises them to ``"string"``.
    """
    if not isinstance(params, dict):
        return params
    import copy
    params = copy.deepcopy(params)
    _sanitize_props_inplace(params)
    return params


def _sanitize_props_inplace(schema: dict) -> None:
    """Walk a schema tree in-place, fixing array-type values at every level."""
    for prop in schema.get("properties", {}).values():
        if isinstance(prop.get("type"), list):
            prop["type"] = "string"
        if "properties" in prop:
            _sanitize_props_inplace(prop)


class ToolRegistry:
    """Registry of callable tools with OpenAI function-calling schema.
    
    Security: Prevents tool shadowing attacks (CVE-2025-59536/21852 mitigation).
    Malicious tools cannot override system tools or silently replace legitimate ones.
    """

    # System tools that cannot be shadowed/overwritten
    RESERVED_TOOL_NAMES = {"evolve_plan", "evolve_apply", "evolve_test", 
                           "evolve_deploy", "evolve_rollback", "evolve_delete",
                           "evolve_submit_pr", "evolve_resume",
                           "shell_exec", "file_read", "file_write", "credential_get",
                           "credential_save", "cron_add", "cron_remove"}

    def __init__(self, strict_mode=False):
        self._tools = {}
        self._strict_mode = strict_mode  # If True, reject overwrites instead of warning
        self._register_log = []  # Audit trail of registration attempts

    def register(self, tool_def):
        """Register a tool. Warns on overwrite, rejects if strict_mode and reserved."""
        name = tool_def.get("name")
        if not name:
            raise ValueError("Tool definition missing 'name' field")
        
        # Security: Check for reserved tool names
        if name in self.RESERVED_TOOL_NAMES and name in self._tools:
            msg = f"SECURITY: Attempt to overwrite reserved tool '{name}' blocked"
            self._register_log.append({"action": "blocked", "tool": name, "reason": "reserved"})
            if self._strict_mode:
                raise PermissionError(msg)
            print(f"[ToolRegistry] {msg}")
            return  # Silently ignore in non-strict mode to prevent shadowing
        
        # Security: Warn on any overwrite (tool shadowing detection)
        if name in self._tools:
            old_desc = self._tools[name].get("description", "")[:50]
            new_desc = tool_def.get("description", "")[:50]
            self._register_log.append({
                "action": "overwrite", 
                "tool": name, 
                "old_desc": old_desc,
                "new_desc": new_desc
            })
            print(f"[ToolRegistry] WARNING: Tool '{name}' is being overwritten!")
            print(f"  Old: {old_desc}... -> New: {new_desc}...")
        else:
            self._register_log.append({"action": "register", "tool": name})
        
        self._tools[name] = tool_def

    def unregister(self, name):
        self._tools.pop(name, None)

    def get(self, name):
        return self._tools.get(name)

    def get_all(self):
        return dict(self._tools)

    def names(self):
        return list(self._tools.keys())

    def execute(self, name, args):
        """Execute a tool by name with given args. Returns result string."""
        tool = self._tools.get(name)
        if not tool:
            return f"Error: Unknown tool '{name}'"
        try:
            if "__parse_error" in args:
                raw_len = args.get("__raw_len", 0)
                return (
                    f"Tool error ({name}): MALFORMED JSON — your output was TRUNCATED "
                    f"(raw length: {raw_len} chars). Your output token limit is ~8K tokens "
                    f"(~150 lines of code). You CANNOT write a full module in one call.\n"
                    f"YOU MUST USE CHUNKED WRITES with append=True:\n"
                    f"  Step 1: evolve_apply(evolution_id, file_path, content='<lines 1-80>')\n"
                    f"  Step 2: evolve_apply(evolution_id, file_path, content='<lines 81-160>', append=True)\n"
                    f"  Step 3: evolve_apply(evolution_id, file_path, content='<lines 161+>', append=True)\n"
                    f"Keep each chunk ≤80 lines. Each chunk must end at a complete statement.\n"
                    f"Do NOT retry with full content — it WILL fail again."
                )

            params = tool.get("parameters", {})
            required = params.get("required", [])
            missing = [r for r in required if r not in args]
            if missing:
                return (
                    f"Tool error ({name}): Missing required argument(s): {', '.join(missing)}. "
                    f"Required params: {required}. Provided: {list(args.keys())}. "
                    f"Please call {name} again with all required arguments."
                )
            
            result = tool["execute"](**args)
            if not isinstance(result, str):
                result = json.dumps(result, indent=2, default=str)
            return result
        except Exception as e:
            return f"Tool error ({name}): {e}\n{traceback.format_exc()[-500:]}"

    def to_openai_schema(self):
        """Convert all tools to OpenAI function-calling format."""
        schema = []
        for name, tool in self._tools.items():
            params = tool.get("parameters", {"type": "object", "properties": {}})
            params = _sanitize_tool_params(params)
            schema.append({
                "type": "function",
                "function": {
                    "name": name,
                    "description": tool.get("description", ""),
                    "parameters": params,
                },
            })
        return schema

    def subset(self, names):
        """Return a new registry with only the named tools."""
        reg = ToolRegistry(strict_mode=self._strict_mode)
        for n in names:
            if n in self._tools:
                reg.register(self._tools[n])
        return reg
    
    def get_audit_log(self):
        """Return the registration audit log for security review."""
        return list(self._register_log)
    
    def is_reserved(self, name):
        """Check if a tool name is reserved."""
        return name in self.RESERVED_TOOL_NAMES
