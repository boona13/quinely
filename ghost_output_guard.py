"""
Ghost Output Guard — Post-model output clamping and safety enforcement.

Mirrored from DeerFlow's SubagentLimitMiddleware.

Server-side enforcement of tool call limits, replacing prompt-based
"please don't call too many tools" with hard truncation after model output.

Also provides:
  - Duplicate tool call detection
  - Excessive tool call clamping (e.g., max N shell_exec per turn)
  - Runaway loop detection
"""

import logging
import threading
from collections import Counter

log = logging.getLogger("quinely.output_guard")

_guard_stats = {"total_processed": 0, "calls_clamped": 0, "duplicates_removed": 0}
_guard_stats_lock = threading.Lock()


def get_guard_stats() -> dict:
    with _guard_stats_lock:
        return dict(_guard_stats)


def clamp_tool_calls(
    tool_calls: list[dict],
    max_total: int = 10,
    per_tool_limits: dict[str, int] | None = None,
) -> list[dict]:
    """Truncate excess tool calls from a single model response.

    Mirrors DeerFlow's SubagentLimitMiddleware._truncate_task_calls but
    generalized for any tool, not just 'task'.

    Args:
        tool_calls: List of tool_call dicts from the model response.
        max_total: Maximum total tool calls allowed per turn.
        per_tool_limits: Optional per-tool limits, e.g. {"shell_exec": 5, "task": 3}.

    Returns:
        Filtered list of tool calls (may be shorter than input).
    """
    if not tool_calls:
        return tool_calls

    if per_tool_limits is None:
        per_tool_limits = {}

    kept: list[dict] = []
    tool_counts: Counter = Counter()
    dropped_count = 0

    for tc in tool_calls:
        tool_name = tc.get("function", {}).get("name", "")

        tool_counts[tool_name] += 1
        tool_limit = per_tool_limits.get(tool_name)
        if tool_limit is not None and tool_counts[tool_name] > tool_limit:
            dropped_count += 1
            continue

        if len(kept) >= max_total:
            dropped_count += 1
            continue

        kept.append(tc)

    if dropped_count > 0:
        log.warning(
            "Output guard: clamped %d excess tool call(s) from model response "
            "(max_total=%d, per_tool=%s)",
            dropped_count, max_total, per_tool_limits,
        )
        with _guard_stats_lock:
            _guard_stats["calls_clamped"] += dropped_count

    return kept


def deduplicate_tool_calls(tool_calls: list[dict]) -> list[dict]:
    """Remove duplicate tool calls (same function name + arguments).

    The model sometimes emits the same tool call twice in one response.
    """
    if not tool_calls:
        return tool_calls

    seen: set[str] = set()
    unique: list[dict] = []
    dropped = 0

    for tc in tool_calls:
        func = tc.get("function", {})
        key = f"{func.get('name', '')}:{func.get('arguments', '')}"
        if key in seen:
            dropped += 1
            continue
        seen.add(key)
        unique.append(tc)

    if dropped > 0:
        log.warning("Output guard: removed %d duplicate tool call(s)", dropped)
        with _guard_stats_lock:
            _guard_stats["duplicates_removed"] += dropped

    return unique


def detect_tool_loop(
    recent_tool_calls: list[str],
    window: int = 6,
    threshold: int = 3,
) -> bool:
    """Detect if the model is stuck in a tool-calling loop.

    Checks the last `window` tool calls for repeated patterns.

    Args:
        recent_tool_calls: List of recent tool names called (most recent last).
        window: Number of recent calls to examine.
        threshold: How many times a tool must appear in the window to be a loop.

    Returns:
        True if a loop is detected.
    """
    if len(recent_tool_calls) < window:
        return False

    last_n = recent_tool_calls[-window:]
    counts = Counter(last_n)

    for tool_name, count in counts.items():
        if count >= threshold:
            log.warning(
                "Output guard: loop detected — %s called %d times in last %d calls",
                tool_name, count, window,
            )
            return True

    return False


# Default per-tool limits for Ghost
DEFAULT_PER_TOOL_LIMITS = {
    "task": 3,
    "delegate_task": 3,
    "shell_exec": 8,
    "web_search": 5,
    "web_fetch": 5,
    "browser_navigate": 3,
}


def guard_model_output(
    tool_calls: list[dict],
    max_total: int = 10,
    per_tool_limits: dict[str, int] | None = None,
) -> list[dict]:
    """Full output guard pipeline: deduplicate → clamp.

    This is the main entry point for the output guard system.
    Call this after receiving tool_calls from the model, before executing them.

    Args:
        tool_calls: Raw tool_calls from model response.
        max_total: Maximum total tool calls per turn.
        per_tool_limits: Per-tool limits (defaults to DEFAULT_PER_TOOL_LIMITS).

    Returns:
        Cleaned, clamped list of tool calls.
    """
    if per_tool_limits is None:
        per_tool_limits = DEFAULT_PER_TOOL_LIMITS

    with _guard_stats_lock:
        _guard_stats["total_processed"] += len(tool_calls) if tool_calls else 0

    result = deduplicate_tool_calls(tool_calls)
    result = clamp_tool_calls(result, max_total=max_total, per_tool_limits=per_tool_limits)
    return result
