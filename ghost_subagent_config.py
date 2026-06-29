"""
Ghost Subagent Config + Registry + Executor — Multi-type delegation with timeouts.

Mirrored from DeerFlow's subagents/ architecture:
  - SubagentConfig dataclass (config.py)
  - Registry dict with built-in types (builtins/)
  - Two-pool executor with timeout enforcement (executor.py)
  - Config-driven tool filtering (allowlist + denylist)

Replaces the single-type delegate_task with a registry of typed subagents:
  - researcher: Read-only research with web + file tools
  - coder: Full write access for code tasks
  - bash: Shell execution specialist
  - reviewer: Readonly code review
"""

import logging
import os
import threading
import time
import uuid
from concurrent.futures import Future, ThreadPoolExecutor
from concurrent.futures import TimeoutError as FuturesTimeoutError
from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from typing import Any

log = logging.getLogger("quinely.subagent")


# ═══════════════════════════════════════════════════════════════════
#  CONFIG  (mirrors DeerFlow's subagents/config.py)
# ═══════════════════════════════════════════════════════════════════

@dataclass
class SubagentConfig:
    """Configuration for a subagent type.

    Attributes:
        name: Unique identifier for this subagent type.
        description: When to use this subagent (shown to LLM).
        system_prompt: The system prompt that guides the subagent.
        tools: Allowlist of tool names. None = inherit all parent tools.
        disallowed_tools: Denylist of tool names. Always excluded.
        model: Model to use. "inherit" = use parent's model.
        max_steps: Maximum tool-loop steps before stopping.
        timeout_seconds: Maximum wall-clock time (default: 900 = 15 minutes).
        max_result_chars: Truncate result to this length.
    """
    name: str
    description: str
    system_prompt: str
    tools: list[str] | None = None
    disallowed_tools: list[str] | None = field(default_factory=lambda: ["delegate_task", "task"])
    model: str = "inherit"
    max_steps: int = 25
    timeout_seconds: int = 900
    max_result_chars: int = 3000


# ═══════════════════════════════════════════════════════════════════
#  BUILT-IN SUBAGENT TYPES  (mirrors DeerFlow's builtins/)
# ═══════════════════════════════════════════════════════════════════

RESEARCHER_CONFIG = SubagentConfig(
    name="researcher",
    description=(
        "A focused research agent with read-only access. Use for:\n"
        "- Verifying interface compatibility after code changes\n"
        "- Researching a module's API before writing code\n"
        "- Summarizing large files without polluting parent context\n"
        "- Web research requiring multiple search + fetch steps\n"
        "Do NOT use for simple single-step operations."
    ),
    system_prompt=(
        "You are a focused research assistant working inside the Ghost codebase. "
        "Your job: complete the task below and return a clear, concise summary.\n\n"
        "RULES:\n"
        "- ONLY read and analyze. Do NOT modify files, run commands, or take actions.\n"
        "- Be precise about method names, signatures, and line numbers.\n"
        "- If you find issues (missing methods, wrong signatures), list them explicitly.\n"
        "- Keep your final response under 2000 characters.\n"
        "- Do NOT explain your process. Just return the findings."
    ),
    tools=["file_read", "grep", "glob", "memory_search", "web_search", "web_fetch",
           "analyze_code_file", "find_code_patterns", "hybrid_memory_search",
           "semantic_memory_search"],
    disallowed_tools=["delegate_task", "task", "shell_exec", "file_write", "apply_diff"],
    max_steps=20,
    timeout_seconds=300,
    max_result_chars=3000,
)

CODER_CONFIG = SubagentConfig(
    name="coder",
    description=(
        "A code-writing agent with full tool access. Use for:\n"
        "- Implementing features that require reading + writing files\n"
        "- Complex multi-file code changes\n"
        "- Tasks requiring shell commands (install deps, run tests)\n"
        "Do NOT use for simple read-only research."
    ),
    system_prompt=(
        "You are a skilled coding agent working inside the Ghost codebase. "
        "Complete the task below autonomously and return a summary of changes.\n\n"
        "RULES:\n"
        "- Read files before modifying them.\n"
        "- Make targeted, minimal changes.\n"
        "- Verify your changes compile/pass basic checks.\n"
        "- Return a concise summary of what you changed and why."
    ),
    tools=None,
    disallowed_tools=["delegate_task", "task"],
    max_steps=30,
    timeout_seconds=600,
    max_result_chars=3000,
)

BASH_CONFIG = SubagentConfig(
    name="bash",
    description=(
        "Command execution specialist for bash operations. Use for:\n"
        "- Running a series of related shell commands\n"
        "- Git, npm, pip, docker operations\n"
        "- Build, test, or deployment pipelines\n"
        "- Verbose command output that would clutter main context\n"
        "Do NOT use for simple single commands."
    ),
    system_prompt=(
        "You are a bash command execution specialist. Execute commands carefully "
        "and report results clearly.\n\n"
        "RULES:\n"
        "- Execute commands one at a time when they depend on each other.\n"
        "- Report both stdout and stderr when relevant.\n"
        "- Handle errors gracefully and explain what went wrong.\n"
        "- Use absolute paths for file operations.\n"
        "- Be cautious with destructive operations."
    ),
    tools=["shell_exec", "file_read", "file_write", "grep", "glob"],
    disallowed_tools=["delegate_task", "task"],
    model="inherit",
    max_steps=20,
    timeout_seconds=600,
    max_result_chars=5000,
)

REVIEWER_CONFIG = SubagentConfig(
    name="reviewer",
    description=(
        "A code review agent that examines changes for quality. Use for:\n"
        "- Reviewing code changes before deployment\n"
        "- Checking for bugs, security issues, or style violations\n"
        "- Verifying test coverage and documentation\n"
        "Do NOT use for making changes."
    ),
    system_prompt=(
        "You are a code reviewer. Examine the code and provide a structured review.\n\n"
        "RULES:\n"
        "- Check for bugs, security issues, and correctness.\n"
        "- Verify error handling and edge cases.\n"
        "- Comment on code clarity and maintainability.\n"
        "- Be specific: reference line numbers and function names.\n"
        "- Return a structured review with severity ratings."
    ),
    tools=["file_read", "grep", "glob", "analyze_code_file", "find_code_patterns"],
    disallowed_tools=["delegate_task", "task", "shell_exec", "file_write", "apply_diff"],
    max_steps=15,
    timeout_seconds=300,
    max_result_chars=3000,
)

BUILTIN_SUBAGENTS: dict[str, SubagentConfig] = {
    "researcher": RESEARCHER_CONFIG,
    "coder": CODER_CONFIG,
    "bash": BASH_CONFIG,
    "reviewer": REVIEWER_CONFIG,
}


def get_subagent_config(name: str) -> SubagentConfig | None:
    """Get a subagent configuration by name."""
    return BUILTIN_SUBAGENTS.get(name)


def list_subagent_types() -> list[str]:
    """List available subagent type names."""
    return list(BUILTIN_SUBAGENTS.keys())


# ═══════════════════════════════════════════════════════════════════
#  STATUS TRACKING  (mirrors DeerFlow's SubagentStatus + SubagentResult)
# ═══════════════════════════════════════════════════════════════════

class SubagentStatus(Enum):
    PENDING = "pending"
    RUNNING = "running"
    COMPLETED = "completed"
    FAILED = "failed"
    TIMED_OUT = "timed_out"


@dataclass
class SubagentResult:
    task_id: str
    trace_id: str
    subagent_type: str
    status: SubagentStatus
    result: str | None = None
    error: str | None = None
    started_at: datetime | None = None
    completed_at: datetime | None = None
    steps_used: int = 0
    tokens_used: int = 0

    @property
    def duration_ms(self) -> int:
        if self.started_at and self.completed_at:
            return int((self.completed_at - self.started_at).total_seconds() * 1000)
        return 0


# ═══════════════════════════════════════════════════════════════════
#  TOOL FILTERING  (mirrors DeerFlow's _filter_tools)
# ═══════════════════════════════════════════════════════════════════

def filter_tool_names(
    all_tool_names: list[str],
    allowed: list[str] | None,
    disallowed: list[str] | None,
) -> list[str]:
    """Filter tool names using allowlist + denylist."""
    filtered = all_tool_names
    if allowed is not None:
        allowed_set = set(allowed)
        filtered = [n for n in filtered if n in allowed_set]
    if disallowed is not None:
        disallowed_set = set(disallowed)
        filtered = [n for n in filtered if n not in disallowed_set]
    return filtered


# ═══════════════════════════════════════════════════════════════════
#  TWO-POOL EXECUTOR  (mirrors DeerFlow's executor.py)
# ═══════════════════════════════════════════════════════════════════

_background_tasks: dict[str, SubagentResult] = {}
_background_tasks_lock = threading.Lock()

_execution_pool = ThreadPoolExecutor(max_workers=3, thread_name_prefix="ghost-subagent-")

MAX_CONCURRENT_SUBAGENTS = 3


def _run_subagent_sync(
    config: SubagentConfig,
    task_text: str,
    tool_registry,
    cfg: dict,
    auth_store=None,
    provider_chain=None,
    event_bus=None,
    task_id: str | None = None,
    trace_id: str | None = None,
) -> SubagentResult:
    """Execute a subagent synchronously using Ghost's ToolLoopEngine."""
    from ghost_loop import ToolLoopEngine

    if trace_id is None:
        trace_id = uuid.uuid4().hex[:8]
    if task_id is None:
        task_id = uuid.uuid4().hex[:8]
    result = SubagentResult(
        task_id=task_id,
        trace_id=trace_id,
        subagent_type=config.name,
        status=SubagentStatus.RUNNING,
        started_at=datetime.now(),
    )

    available_names = tool_registry.names() if hasattr(tool_registry, "names") else []
    filtered_names = filter_tool_names(available_names, config.tools, config.disallowed_tools)
    filtered_registry = tool_registry.subset(
        [n for n in filtered_names if n in available_names]
    ) if hasattr(tool_registry, "subset") else tool_registry

    api_key = None
    if auth_store:
        try:
            api_key = auth_store.get_api_key("openrouter")
        except (AttributeError, TypeError):
            pass
    if not api_key:
        api_key = os.environ.get("OPENROUTER_API_KEY", "")

    if not api_key:
        result.status = SubagentStatus.FAILED
        result.error = "No API key available"
        result.completed_at = datetime.now()
        return result

    model = cfg.get("model", "anthropic/claude-sonnet-4") if config.model == "inherit" else config.model

    engine = ToolLoopEngine(
        api_key=api_key,
        model=model,
        fallback_models=cfg.get("fallback_models", []),
        auth_store=auth_store,
        provider_chain=provider_chain,
    )

    log.info("[trace=%s] Subagent %s starting: %s...", trace_id, config.name, task_text[:80])

    if event_bus:
        try:
            event_bus.emit(
                "on_subagent_started",
                task_id=task_id, trace_id=trace_id,
                subagent_type=config.name, prompt=task_text[:200],
            )
        except Exception:
            pass

    try:
        loop_result = engine.run(
            system_prompt=config.system_prompt,
            user_message=task_text.strip(),
            tool_registry=filtered_registry,
            max_steps=config.max_steps,
            temperature=0.2,
            max_tokens=2048,
        )

        text = (loop_result.text or "").strip()
        if len(text) > config.max_result_chars:
            text = text[:config.max_result_chars] + "\n... [truncated]"

        result.result = text
        result.steps_used = loop_result.steps
        result.tokens_used = loop_result.total_tokens
        result.status = SubagentStatus.COMPLETED
        result.completed_at = datetime.now()

        log.info("[trace=%s] Subagent %s completed in %dms (%d steps)",
                 trace_id, config.name, result.duration_ms, result.steps_used)

        if event_bus:
            try:
                event_bus.emit(
                    "on_subagent_completed",
                    task_id=task_id, trace_id=trace_id,
                    subagent_type=config.name,
                    steps=result.steps_used, duration_ms=result.duration_ms,
                )
            except Exception:
                pass

    except Exception as exc:
        result.status = SubagentStatus.FAILED
        result.error = f"{type(exc).__name__}: {exc}"
        result.completed_at = datetime.now()
        log.warning("[trace=%s] Subagent %s failed: %s", trace_id, config.name, exc)

        if event_bus:
            try:
                event_bus.emit(
                    "on_subagent_failed",
                    task_id=task_id, trace_id=trace_id,
                    subagent_type=config.name, error=str(exc)[:200],
                )
            except Exception:
                pass

    return result


def execute_subagent_async(
    config: SubagentConfig,
    task_text: str,
    tool_registry,
    cfg: dict,
    auth_store=None,
    provider_chain=None,
    event_bus=None,
) -> tuple[str, Future]:
    """Submit a subagent for background execution with timeout.

    Returns (task_id, future) — the future resolves to a SubagentResult.
    The engine uses the future for auto-collect; callers can also poll
    via get_background_task_result(task_id).
    """
    task_id = uuid.uuid4().hex[:8]
    trace_id = uuid.uuid4().hex[:8]

    placeholder = SubagentResult(
        task_id=task_id,
        trace_id=trace_id,
        subagent_type=config.name,
        status=SubagentStatus.RUNNING,
        started_at=datetime.now(),
    )

    with _background_tasks_lock:
        _background_tasks[task_id] = placeholder
        _cleanup_background_tasks()

    def _run_and_track():
        sr = _run_subagent_sync(
            config, task_text, tool_registry, cfg,
            auth_store, provider_chain, event_bus,
            task_id=task_id, trace_id=trace_id,
        )
        with _background_tasks_lock:
            _background_tasks[task_id] = sr
        return sr

    future = _execution_pool.submit(_run_and_track)
    return task_id, future


_MAX_BACKGROUND_TASKS = 50


def _cleanup_background_tasks() -> None:
    """Remove oldest completed/failed/timed_out tasks when dict exceeds max size."""
    if len(_background_tasks) <= _MAX_BACKGROUND_TASKS:
        return
    terminal = {SubagentStatus.COMPLETED, SubagentStatus.FAILED, SubagentStatus.TIMED_OUT}
    removable = sorted(
        (tid for tid, r in _background_tasks.items() if r.status in terminal),
        key=lambda tid: _background_tasks[tid].completed_at or datetime.min,
    )
    to_remove = len(_background_tasks) - _MAX_BACKGROUND_TASKS
    for tid in removable[:to_remove]:
        del _background_tasks[tid]


def get_background_task_result(task_id: str) -> SubagentResult | None:
    with _background_tasks_lock:
        return _background_tasks.get(task_id)


def list_background_tasks() -> list[SubagentResult]:
    with _background_tasks_lock:
        return list(_background_tasks.values())


def wait_for_tasks(task_ids: list[str], timeout: float = 900) -> dict[str, dict]:
    """Block until all specified tasks reach a terminal state, then return results.

    This is the "Auto-Collect" half of Ghost's Fire-and-Auto-Collect pattern.
    Returns a dict mapping task_id -> result dict.
    """
    deadline = time.time() + timeout
    results = {}

    for tid in task_ids:
        remaining = max(0.1, deadline - time.time())
        while remaining > 0:
            sr = get_background_task_result(tid)
            if sr is None:
                results[tid] = {"error": f"Unknown task_id: {tid}"}
                break
            if sr.status in (SubagentStatus.COMPLETED, SubagentStatus.FAILED, SubagentStatus.TIMED_OUT):
                results[tid] = _format_subagent_result(sr)
                break
            time.sleep(min(0.5, remaining))
            remaining = deadline - time.time()
        else:
            results[tid] = {"error": f"Timed out waiting for task {tid}"}

    return results


def _format_subagent_result(sr: SubagentResult) -> dict:
    """Convert a SubagentResult to a dict suitable for tool output."""
    if sr.status == SubagentStatus.COMPLETED:
        return {
            "success": True,
            "result": sr.result,
            "subagent_type": sr.subagent_type,
            "steps_used": sr.steps_used,
            "duration_ms": sr.duration_ms,
        }
    elif sr.status == SubagentStatus.TIMED_OUT:
        return {"error": f"Subagent timed out", "subagent_type": sr.subagent_type}
    else:
        return {"error": sr.error or "Unknown error", "subagent_type": sr.subagent_type}


# ═══════════════════════════════════════════════════════════════════
#  TOOL BUILDER  (for Ghost's tool registry)
# ═══════════════════════════════════════════════════════════════════

def build_typed_subagent_tools(
    cfg: dict,
    tool_registry,
    auth_store=None,
    provider_chain=None,
    event_bus=None,
) -> list[dict]:
    """Build task + check_task + wait_tasks tools for typed subagent delegation.

    Ghost's "Fire and Auto-Collect" pattern:
      1. LLM calls task() one or more times — each submits to thread pool
      2. Engine auto-detects pending tasks after the tool-call batch
      3. Engine waits for all parallel tasks and injects results
      4. LLM sees all results in the next turn — zero polling needed

    check_task / wait_tasks exist for explicit async control when needed.
    """
    available_types = list_subagent_types()
    type_descriptions = "\n".join(
        f"- {name}: {BUILTIN_SUBAGENTS[name].description.split(chr(10))[0]}"
        for name in available_types
    )

    def task(prompt: str, subagent_type: str = "researcher", max_steps: int = None):
        """
        Delegate a task to a specialized subagent. Multiple task() calls in the
        same turn run in PARALLEL automatically — results are collected when all finish.

        Args:
            prompt: Clear description of the task. Be specific.
            subagent_type: Type of subagent (researcher, coder, bash, reviewer).
            max_steps: Override max tool-loop steps (optional).
        """
        config = get_subagent_config(subagent_type)
        if config is None:
            return {"error": f"Unknown subagent type: {subagent_type}. Available: {available_types}"}

        if not prompt or not prompt.strip():
            return {"error": "Task prompt is required."}

        if max_steps is not None:
            from dataclasses import replace
            try:
                max_steps = int(max_steps)
            except (TypeError, ValueError):
                max_steps = config.max_steps
            config = replace(config, max_steps=min(max(1, max_steps), config.max_steps))

        # Submit to thread pool — runs in background immediately
        task_id, _future = execute_subagent_async(
            config=config,
            task_text=prompt,
            tool_registry=tool_registry,
            cfg=cfg,
            auth_store=auth_store,
            provider_chain=provider_chain,
            event_bus=event_bus,
        )

        return {
            "submitted": True,
            "task_id": task_id,
            "subagent_type": config.name,
            "message": (
                f"Subagent '{config.name}' started (task_id={task_id}). "
                "Results will be auto-collected when all parallel tasks finish."
            ),
        }

    def check_task(task_id: str):
        """
        Check the status of a background subagent task.

        Args:
            task_id: The task_id returned by a previous task() call.
        """
        if not task_id:
            return {"error": "task_id is required."}
        sr = get_background_task_result(task_id)
        if sr is None:
            return {"error": f"Unknown task_id: {task_id}"}
        result = {
            "task_id": sr.task_id,
            "subagent_type": sr.subagent_type,
            "status": sr.status.value,
        }
        if sr.status == SubagentStatus.COMPLETED:
            result["result"] = sr.result
            result["steps_used"] = sr.steps_used
            result["duration_ms"] = sr.duration_ms
        elif sr.status in (SubagentStatus.FAILED, SubagentStatus.TIMED_OUT):
            result["error"] = sr.error
        elif sr.started_at:
            result["running_for_ms"] = int((datetime.now() - sr.started_at).total_seconds() * 1000)
        return result

    def wait_tasks_tool(task_ids: list, timeout: int = 900):
        """
        Wait for one or more background subagent tasks to complete and return results.

        Args:
            task_ids: List of task_id strings to wait for.
            timeout: Maximum seconds to wait (default 900).
        """
        if not task_ids:
            return {"error": "task_ids list is required."}
        if not isinstance(task_ids, list):
            task_ids = [str(task_ids)]
        return wait_for_tasks(task_ids, timeout=min(timeout, 900))

    return [
        {
            "name": "task",
            "description": (
                "Delegate a task to a specialized subagent for isolated execution with "
                "a fresh context window. Multiple task() calls in the SAME turn run in "
                "PARALLEL — results are auto-collected. Choose the right subagent type:\n"
                f"{type_descriptions}\n\n"
                "Use for: research that needs fresh context, parallel sub-tasks, "
                "isolated code changes, or verbose operations that would clutter context."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "prompt": {
                        "type": "string",
                        "description": (
                            "Clear description of the task. Be specific: include file paths, "
                            "class names, and what to accomplish."
                        ),
                    },
                    "subagent_type": {
                        "type": "string",
                        "enum": available_types,
                        "description": f"Type of subagent: {', '.join(available_types)}",
                        "default": "researcher",
                    },
                    "max_steps": {
                        "type": "integer",
                        "description": "Override max tool-loop steps (optional).",
                    },
                },
                "required": ["prompt"],
            },
            "execute": task,
        },
        {
            "name": "check_task",
            "description": (
                "Check the status of a background subagent task. "
                "Usually not needed — results are auto-collected. Use only if you "
                "need to check progress before the batch completes."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "task_id": {
                        "type": "string",
                        "description": "The task_id returned by task().",
                    },
                },
                "required": ["task_id"],
            },
            "execute": check_task,
        },
        {
            "name": "wait_tasks",
            "description": (
                "Wait for multiple background tasks to complete and return all results. "
                "Usually not needed — the engine auto-collects. Use only for explicit "
                "async workflows where you fired tasks in a previous turn."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "task_ids": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": "List of task_id strings to wait for.",
                    },
                    "timeout": {
                        "type": "integer",
                        "description": "Max seconds to wait (default 900).",
                        "default": 900,
                    },
                },
                "required": ["task_ids"],
            },
            "execute": wait_tasks_tool,
        },
    ]
