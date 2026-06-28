"""Middleware pipeline for Ghost agent invocations.

DeerFlow-grade architecture with 6 hook points:
  before_invoke / after_invoke   — wrap the entire engine.run() call
  before_model  / after_model    — wrap EACH LLM call inside the loop
  wrap_tool_call                 — intercept individual tool executions
  after_tool_call                — modify tool results after execution

The engine calls back into the chain at each LLM call and tool call,
giving middlewares full control over every stage of the agent loop.
"""

from __future__ import annotations

import logging
import re
import threading
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

log = logging.getLogger("ghost.middleware")


def _push_chat_progress(ctx: InvocationContext, message: str) -> None:
    """Push a progress event to the chat SSE stream (no-op for non-chat sources)."""
    if ctx.source != "chat":
        return
    session = ctx.meta.get("session") if ctx.meta else None
    if session and hasattr(session, "progress"):
        session.progress.append({
            "message": message,
            "time": datetime.now().isoformat(),
        })

# ---------------------------------------------------------------------------
# InvocationContext — carries ALL state for a single agent invocation
# ---------------------------------------------------------------------------


@dataclass
class InvocationContext:
    """All state for a single agent invocation.

    Every field that differs between entry points is a separate, explicit
    field — never hidden inside generic middleware logic.
    """

    # -- Source identification --
    source: str  # "chat" | "cron" | "channel" | "monitor" | "action"

    # -- Input (set by entry point BEFORE chain runs) --
    user_message: str = ""
    system_prompt_parts: list[str] = field(default_factory=list)
    tool_registry: Any = None
    history: list | None = None
    images: list | None = None      # list of {data, mime} dicts  (chat)
    image_b64: str | None = None    # single base64 string        (inbound)
    max_steps: int = 200
    max_tokens: int = 4096
    temperature: float = 0.3
    force_tool: bool = True
    enable_reasoning: bool = False
    model_override: str | None = None
    coding_model_chain: list | None = None  # list[tuple[str, str]] from ModelDispatcher
    cancel_check: Any = None
    on_step: Any = None
    on_token: Any = None

    # -- Daemon references --
    daemon: Any = None
    engine: Any = None              # caller sets this: chat_engine or engine
    config: dict = field(default_factory=dict)

    # -- Enrichment (set by middleware) --
    matched_skills: list = field(default_factory=list)
    active_project: Any = None      # set by entry point, used by SkillMatch
    caller_context: str = "autonomous"

    # -- Output (set after engine.run) --
    result: Any = None              # ToolLoopResult
    result_text: str = ""
    tools_used: list = field(default_factory=list)
    tokens_used: int = 0
    escalation_count: int = 0

    # -- Source-specific metadata --
    meta: dict = field(default_factory=dict)

    @property
    def system_prompt(self) -> str:
        return "\n\n".join(p for p in self.system_prompt_parts if p)


# ---------------------------------------------------------------------------
# Middleware base class — 6 hook points
# ---------------------------------------------------------------------------


class Middleware:
    """Base class for all middleware.

    Subclasses override any combination of hook methods.  Returning None
    from a hook means "no modification" — the chain continues with the
    original value.
    """

    # -- Invocation-level hooks (wrap the entire engine.run) --

    def before_invoke(self, ctx: InvocationContext) -> None:
        """Called once before engine.run().  May mutate *ctx* in-place."""

    def after_invoke(self, ctx: InvocationContext) -> None:
        """Called once after engine.run().  May mutate *ctx* in-place."""

    # -- Per-LLM-call hooks (called every step inside the engine loop) --

    def before_model(self, ctx: InvocationContext, messages: list, step: int) -> list | None:
        """Called before each LLM API call inside the loop.

        May inspect or modify the messages list.  Return a new list to
        replace messages, or None to keep them unchanged.
        """

    def after_model(self, ctx: InvocationContext, messages: list, response_msg: dict, step: int) -> dict | None:
        """Called after each LLM response, before tool call processing.

        *response_msg* is the assistant message dict that was just appended
        to *messages*.  Return a replacement dict or None to keep it.
        """

    # -- Per-tool-call hooks (called for every individual tool execution) --

    def wrap_tool_call(self, ctx: InvocationContext, tool_name: str, args: dict, step: int) -> str | None:
        """Called before a tool is executed.

        Return a string to INTERCEPT the call (the string becomes the tool
        result and normal execution is skipped).  Return None to let the
        tool execute normally.
        """

    def after_tool_call(self, ctx: InvocationContext, tool_name: str, args: dict, result: str, step: int) -> str | None:
        """Called after a tool has executed.

        Return a modified result string, or None to keep the original.
        """


# ---------------------------------------------------------------------------
# MiddlewareChain — ordered execution with engine call in the middle
# ---------------------------------------------------------------------------


class MiddlewareChain:
    """Runs before-hooks → engine.run → after-hooks in order.

    The chain is also passed INTO the engine so it can call per-step
    hooks (before_model, after_model, wrap_tool_call, after_tool_call).
    """

    def __init__(self, middlewares: list[Middleware] | None = None):
        self._middlewares: list[Middleware] = list(middlewares or [])
        self._local = threading.local()

    @property
    def _active_ctx(self) -> InvocationContext | None:
        """Per-thread active context — safe for concurrent invocations."""
        return getattr(self._local, "ctx", None)

    @_active_ctx.setter
    def _active_ctx(self, value: InvocationContext | None) -> None:
        self._local.ctx = value

    def add(self, mw: Middleware) -> "MiddlewareChain":
        self._middlewares.append(mw)
        return self

    # -- Full invocation lifecycle --

    def invoke(self, ctx: InvocationContext) -> InvocationContext:
        """Execute the full pipeline: before → engine → after."""
        self._active_ctx = ctx
        log.debug("[MW] invoke source=%s user=%s",
                  ctx.source, (ctx.user_message or "")[:80])

        for mw in self._middlewares:
            try:
                mw.before_invoke(ctx)
            except Exception as exc:
                log.error("%s.before_invoke failed: %s",
                          type(mw).__name__, exc, exc_info=True)

        self._run_engine(ctx)

        for mw in self._middlewares:
            try:
                mw.after_invoke(ctx)
            except Exception as exc:
                log.error("%s.after_invoke failed: %s",
                          type(mw).__name__, exc, exc_info=True)

        self._active_ctx = None
        return ctx

    # -- Per-step hooks called by the engine --

    def before_model(self, messages: list, step: int) -> list:
        """Engine calls this before each LLM API call."""
        ctx = self._active_ctx
        for mw in self._middlewares:
            try:
                result = mw.before_model(ctx, messages, step)
                if result is not None:
                    messages = result
            except Exception as exc:
                log.error("%s.before_model failed: %s",
                          type(mw).__name__, exc, exc_info=True)
        return messages

    def after_model(self, messages: list, response_msg: dict, step: int) -> dict | None:
        """Engine calls this after each LLM response."""
        ctx = self._active_ctx
        override = None
        for mw in self._middlewares:
            try:
                result = mw.after_model(ctx, messages, response_msg, step)
                if result is not None:
                    response_msg = result
                    override = result
            except Exception as exc:
                log.error("%s.after_model failed: %s",
                          type(mw).__name__, exc, exc_info=True)
        return override

    def wrap_tool_call(self, tool_name: str, args: dict, step: int) -> str | None:
        """Engine calls this before executing a tool.  First non-None wins."""
        ctx = self._active_ctx
        for mw in self._middlewares:
            try:
                result = mw.wrap_tool_call(ctx, tool_name, args, step)
                if result is not None:
                    log.info("wrap_tool_call: %s intercepted %s at step %d",
                             type(mw).__name__, tool_name, step)
                    return result
            except Exception as exc:
                log.error("%s.wrap_tool_call failed: %s",
                          type(mw).__name__, exc, exc_info=True)
        return None

    def after_tool_call(self, tool_name: str, args: dict, result: str, step: int) -> str:
        """Engine calls this after a tool has executed."""
        ctx = self._active_ctx
        for mw in self._middlewares:
            try:
                modified = mw.after_tool_call(ctx, tool_name, args, result, step)
                if modified is not None:
                    result = modified
            except Exception as exc:
                log.error("%s.after_tool_call failed: %s",
                          type(mw).__name__, exc, exc_info=True)
        return result

    # -- private: faithful pass-through of ALL engine.run params -----------

    def _run_engine(self, ctx: InvocationContext) -> None:
        if ctx.engine is None:
            log.error("No engine on InvocationContext — skipping engine.run()")
            return

        from ghost_tools import set_shell_caller_context
        set_shell_caller_context(ctx.caller_context)

        try:
            ctx.result = ctx.engine.run(
                system_prompt=ctx.system_prompt,
                user_message=ctx.user_message,
                tool_registry=ctx.tool_registry,
                max_steps=ctx.max_steps,
                max_tokens=ctx.max_tokens,
                temperature=ctx.temperature,
                force_tool=ctx.force_tool,
                on_step=ctx.on_step,
                history=ctx.history,
                cancel_check=ctx.cancel_check,
                images=ctx.images,
                image_b64=ctx.image_b64,
                enable_reasoning=ctx.enable_reasoning,
                model_override=ctx.model_override,
                coding_model_chain=ctx.coding_model_chain,
                hook_runner=ctx.daemon.hooks if ctx.daemon else None,
                tool_intent_security=getattr(
                    ctx.daemon, "tool_intent_security", None),
                tool_event_bus=getattr(
                    ctx.daemon, "tool_event_bus", None),
                on_token=ctx.on_token,
                middleware_chain=self,
            )
            if ctx.result:
                ctx.result_text = ctx.result.text or ""
                ctx.tools_used = [
                    tc["tool"] for tc in (ctx.result.tool_calls or [])
                ]
                ctx.tokens_used = ctx.result.total_tokens or 0
        except Exception as exc:
            log.error("Engine.run failed: %s", exc, exc_info=True)
            ctx.result_text = f"Error: {exc}"
            ctx.meta["engine_error"] = exc
        finally:
            set_shell_caller_context("autonomous")


# ===========================================================================
# INVOCATION-LEVEL MIDDLEWARES (before_invoke / after_invoke)
# ===========================================================================


# ---------------------------------------------------------------------------
# 1. IdentityMiddleware
# ---------------------------------------------------------------------------


class IdentityMiddleware(Middleware):
    """Prepend SOUL.md + USER.md + platform info to the system prompt."""

    def before_invoke(self, ctx: InvocationContext) -> None:
        if ctx.daemon is None:
            return
        try:
            identity = ctx.daemon._build_identity_context()
            if identity:
                ctx.system_prompt_parts.insert(0, identity)
        except Exception as exc:
            log.warning("IdentityMiddleware: %s", exc)


# ---------------------------------------------------------------------------
# 2. SkillMatchMiddleware
# ---------------------------------------------------------------------------


class SkillMatchMiddleware(Middleware):
    """Match skills, inject prompt section, resolve model override."""

    def before_invoke(self, ctx: InvocationContext) -> None:
        daemon = ctx.daemon
        if not daemon or not getattr(daemon, "skill_loader", None):
            return
        try:
            daemon.skill_loader.check_reload()
            disabled = set(ctx.config.get("disabled_skills", []))

            if ctx.active_project:
                disabled |= set(
                    ctx.active_project.config.get("disabled_skills", [])
                )

            content_type = ctx.meta.get("content_type")

            engine = getattr(daemon, "engine", None)
            if engine:
                ctx.matched_skills = daemon.skill_loader.llm_match(
                    engine, ctx.user_message, content_type, disabled=disabled
                )
            else:
                ctx.matched_skills = []

            if ctx.active_project:
                project_enabled = ctx.active_project.config.get("skills", [])
                if project_enabled:
                    allowed = set(project_enabled)
                    ctx.matched_skills = [
                        s for s in ctx.matched_skills if s.name in allowed
                    ]

            if ctx.matched_skills:
                skills_prompt = daemon.skill_loader.build_skills_prompt(
                    ctx.matched_skills
                )
                ctx.system_prompt_parts.append(skills_prompt)

            if ctx.matched_skills and not ctx.model_override:
                skill_model = daemon._resolve_skill_model(ctx.matched_skills)
                if skill_model:
                    ctx.model_override = skill_model

        except Exception as exc:
            log.warning("SkillMatchMiddleware: %s", exc)
            ctx.matched_skills = []


# ---------------------------------------------------------------------------
# 3. ImageIntentMiddleware
# ---------------------------------------------------------------------------


class ImageIntentMiddleware(Middleware):
    """Classify image intent BEFORE the LLM sees the payload.

    When images are attached, this middleware determines whether the user wants
    to PROCESS the image (tool-actionable) or UNDERSTAND the image (vision).

    - Tool-actionable: strips base64 from context (saves huge tokens),
      injects a hint to use the right tool with the file path.
    - Vision-required: verifies the effective model supports vision;
      swaps to a vision-capable model if it doesn't.
    """

    def before_invoke(self, ctx: InvocationContext) -> None:
        if not ctx.images and not ctx.image_b64:
            return
        if not ctx.tool_registry:
            return
        if ctx.source not in ("chat", "channel"):
            return

        try:
            from ghost_image_router import (
                classify_image_intent,
                get_image_tools,
                resolve_vision_model,
                supports_vision,
            )
        except ImportError:
            log.warning("ImageIntentMiddleware: ghost_image_router not available")
            return

        image_tools = get_image_tools(ctx.tool_registry)
        auth_store = getattr(ctx.daemon, "auth_store", None) if ctx.daemon else None

        classification = classify_image_intent(
            ctx.user_message,
            image_tools,
            auth_store=auth_store,
            config=ctx.config,
        )
        intent = classification.get("intent", "vision")
        confidence = classification.get("confidence", 0.0)
        log.info(
            "ImageIntentMiddleware: intent=%s confidence=%.2f classification=%s",
            intent, confidence, classification,
        )

        if intent == "tool" and confidence >= 0.7:
            tool_name = classification.get("tool_name", "")
            matching = [t for t in image_tools if t["name"] == tool_name]
            param_name = matching[0]["param_name"] if matching else "image_path"

            if tool_name not in ctx.tool_registry.names():
                log.warning(
                    "ImageIntentMiddleware: classified tool %s not in registry, "
                    "falling through to vision path", tool_name,
                )
            else:
                log.info(
                    "ImageIntentMiddleware: stripping base64, locking to tool %s",
                    tool_name,
                )
                ctx.images = None
                ctx.image_b64 = None

                # Lock the registry so the identified tool is the ONLY option.
                # The model cannot wander off to shell_exec or file_read.
                ctx.tool_registry = ctx.tool_registry.subset([tool_name])
                # Store routing info so wrap_tool_call can verify
                ctx.meta["image_routed_tool"] = tool_name

                hint = (
                    f"\n\n## IMAGE PROCESSING TASK\n"
                    f"Call `{tool_name}` with `{param_name}` set to the "
                    f"file path from ATTACHED FILES.\n"
                )
                ctx.system_prompt_parts.append(hint)
                return
        else:
            engine = ctx.engine
            current_model = getattr(engine, "model", None) if engine else None
            vision_override = resolve_vision_model(
                current_model, ctx.model_override, ctx.config,
            )
            if vision_override:
                log.info(
                    "ImageIntentMiddleware: overriding model to %s for vision",
                    vision_override,
                )
                ctx.model_override = vision_override

            effective = ctx.model_override or current_model or ""
            if not supports_vision(effective):
                log.warning(
                    "ImageIntentMiddleware: model %s may not support vision, "
                    "but no alternative available — sending images anyway",
                    effective,
                )


# ---------------------------------------------------------------------------
# 4. ToolScopeMiddleware
# ---------------------------------------------------------------------------


class ToolScopeMiddleware(Middleware):
    """Restrict tool_registry based on source and context."""

    _EVOLVE_TOOLS = frozenset({
        "evolve_plan", "evolve_apply", "evolve_apply_config",
        "evolve_delete", "evolve_test", "evolve_deploy", "evolve_rollback",
        "evolve_submit_pr",
    })
    _FEATURE_MUTATE_TOOLS = frozenset({
        "start_future_feature", "complete_future_feature",
        "fail_future_feature", "evolve_resume",
    })

    _IMPLEMENTER_ALLOWLIST = [
        "evolve_plan", "evolve_apply", "evolve_apply_config",
        "evolve_test", "evolve_deploy", "evolve_rollback",
        "evolve_delete", "evolve_submit_pr",
        "list_future_features", "get_future_feature",
        "start_future_feature", "complete_future_feature",
        "fail_future_feature", "get_feature_stats",
        "add_future_feature",
        "file_read", "file_search", "file_write",
        "grep", "glob", "find_code_patterns",
        "shell_exec", "shell_session", "shell_bg_start",
        "shell_bg_status", "shell_bg_kill",
        "delegate_task",
        "web_fetch", "web_search",
        "browser_navigate", "browser_snapshot",
        "browser_click", "browser_type",
        "memory_save", "memory_search",
        "config_get", "config_set",
        "tools_list", "tools_create", "tools_install_github",
        "tools_uninstall", "tools_validate",
        "tools_enable", "tools_disable",
        "tools_reload", "tools_reload_all",
        "task_complete",
    ]

    def before_invoke(self, ctx: InvocationContext) -> None:
        if ctx.tool_registry is None:
            return

        is_evo_runner = ctx.meta.get("is_evolution_runner", False)

        if is_evo_runner:
            available = set(ctx.tool_registry.names())
            allowed = [t for t in self._IMPLEMENTER_ALLOWLIST
                       if t in available]
            log.info(
                "ToolScope: evo_runner filtering %d -> %d tools (available=%d)",
                len(available), len(allowed), len(available),
            )
            ctx.tool_registry = ctx.tool_registry.subset(allowed)
            return

        exclude = self._EVOLVE_TOOLS
        if ctx.source == "cron":
            exclude = self._EVOLVE_TOOLS | self._FEATURE_MUTATE_TOOLS
        safe_names = [
            n for n in ctx.tool_registry.names() if n not in exclude
        ]
        ctx.tool_registry = ctx.tool_registry.subset(safe_names)

        # Ghost's built-in growth routines use hardcoded prompts that specify
        # which tools they need; skill-based narrowing strips essential tools
        # like add_future_feature and log_growth_activity. User-scheduled cron
        # jobs should still benefit from skill matching.
        if ctx.source == "cron":
            job_name = (ctx.meta or {}).get("job_name", "")
            if job_name.startswith("_ghost_growth_"):
                return

        if not ctx.matched_skills:
            return
        if not ctx.daemon or not getattr(ctx.daemon, "skill_loader", None):
            return
        needed = ctx.daemon.skill_loader.get_tools_for_skills(
            ctx.matched_skills
        )
        if not needed:
            return

        _ALWAYS_CORE = {
            "memory_search", "memory_save", "task_complete",
            "file_read", "file_write", "file_search",
            "edit_file", "apply_patch",
            "git_status", "git_diff", "git_log",
            "git_add", "git_commit", "git_branch", "git_init",
            "shell_exec", "grep", "glob",
            "notify", "uptime", "app_control",
            "add_future_feature", "list_future_features",
            "get_future_feature", "get_feature_stats",
        }
        if ctx.source == "chat" and ctx.active_project:
            _ALWAYS = _ALWAYS_CORE | {
                "project_list", "project_get", "project_resolve",
            }
        elif ctx.source == "chat":
            _ALWAYS = _ALWAYS_CORE
        else:
            _ALWAYS = {"memory_search", "memory_save", "notify"}

        # Include all ToolBuilder-registered tools (ghost_tools/) — these are
        # user/evolve-created tools that should always be available regardless
        # of which skill is matched.
        tool_builder_tools = set()
        tool_mgr = getattr(ctx.daemon, "tool_manager", None)
        if tool_mgr:
            tool_builder_tools = set(tool_mgr.get_tool_names())

        all_names = list(set(needed) | _ALWAYS | tool_builder_tools)
        available = set(ctx.tool_registry.names())
        valid = [n for n in all_names if n in available]
        if valid:
            ctx.tool_registry = ctx.tool_registry.subset(valid)


# ---------------------------------------------------------------------------
# 4. CallerContextMiddleware
# ---------------------------------------------------------------------------


class CallerContextMiddleware(Middleware):
    """Map invocation source to shell caller context."""

    _MAP = {
        "chat": "interactive",
        "channel": "interactive",
        "action": "interactive",
        "monitor": "interactive",
        "cron": "autonomous",
    }

    def before_invoke(self, ctx: InvocationContext) -> None:
        ctx.caller_context = self._MAP.get(ctx.source, "autonomous")


# ---------------------------------------------------------------------------
# 5. GiveUpDetectionMiddleware
# ---------------------------------------------------------------------------


class GiveUpDetectionMiddleware(Middleware):
    """Detect give-up responses and retry with escalation coaching."""

    MAX_RETRIES = 2

    _META_RESPONSE_PATTERNS = [
        re.compile(r"escalation\s+(path|ladder|directive)", re.IGNORECASE),
        re.compile(r"(got|received)\s+(the\s+)?correction", re.IGNORECASE),
        re.compile(r"follow\s+that\s+(exact\s+)?programmatic", re.IGNORECASE),
        re.compile(r"programmatic\s+escalation", re.IGNORECASE),
        re.compile(r"follow\s+the\s+escalation", re.IGNORECASE),
    ]

    @classmethod
    def _is_meta_response(cls, text: str) -> bool:
        """Detect if a response talks about internal process instead of answering the user."""
        if not text or len(text.strip()) < 20:
            return False
        for pattern in cls._META_RESPONSE_PATTERNS:
            if pattern.search(text):
                log.warning("Meta-response detected (matched: %s)", pattern.pattern)
                return True
        return False

    def after_invoke(self, ctx: InvocationContext) -> None:
        if ctx.source in ("cron", "monitor"):
            return
        if not ctx.result_text or len(ctx.result_text.strip()) < 20:
            return

        try:
            from ghost import _detected_give_up, _ESCALATION_COACHING
        except ImportError:
            return

        for attempt in range(self.MAX_RETRIES):
            cancelled = ctx.cancel_check() if ctx.cancel_check else False
            if cancelled:
                break
            if not _detected_give_up(ctx.result_text, engine=ctx.engine):
                break

            ctx.escalation_count = attempt + 1
            log.info("Give-up detected (attempt %d/%d), escalating",
                     attempt + 1, self.MAX_RETRIES)
            _push_chat_progress(ctx, f"Retrying response (escalation {attempt + 1}/{self.MAX_RETRIES})...")

            esc_history = list(ctx.history or [])
            esc_history.append({"role": "user", "content": ctx.user_message})
            esc_history.append({
                "role": "assistant", "content": ctx.result_text
            })

            if ctx.source == "chat" and "session" in ctx.meta:
                session = ctx.meta["session"]
                if hasattr(session, "token_chunks"):
                    session.token_chunks.clear()

            escalation_msg = (
                _ESCALATION_COACHING + "\n\n"
                "The user's original request you MUST fulfill:\n"
                + ctx.user_message + "\n\n"
                "Your response MUST directly answer the request above. "
                "Do NOT describe your approach, plans, or internal process — "
                "execute the task and return concrete results to the user."
            )

            from ghost_tools import set_shell_caller_context
            set_shell_caller_context(ctx.caller_context)
            try:
                retry_result = ctx.engine.run(
                    system_prompt=ctx.system_prompt,
                    user_message=escalation_msg,
                    tool_registry=ctx.tool_registry,
                    max_steps=ctx.max_steps,
                    max_tokens=ctx.max_tokens,
                    temperature=ctx.temperature,
                    force_tool=ctx.force_tool,
                    on_step=ctx.on_step,
                    history=esc_history,
                    cancel_check=ctx.cancel_check,
                    images=ctx.images,
                    image_b64=ctx.image_b64,
                    enable_reasoning=ctx.enable_reasoning,
                    model_override=ctx.model_override,
                    coding_model_chain=ctx.coding_model_chain,
                    hook_runner=(ctx.daemon.hooks
                                if ctx.daemon else None),
                    tool_intent_security=getattr(
                        ctx.daemon, "tool_intent_security", None),
                    tool_event_bus=getattr(
                        ctx.daemon, "tool_event_bus", None),
                    on_token=ctx.on_token,
                )
                if retry_result:
                    retry_text = retry_result.text or ""
                    if self._is_meta_response(retry_text):
                        log.warning(
                            "Escalation attempt %d produced meta-response, "
                            "keeping original response",
                            attempt + 1,
                        )
                        break
                    ctx.result = retry_result
                    ctx.result_text = retry_text
                    new_tools = [
                        tc["tool"]
                        for tc in (retry_result.tool_calls or [])
                    ]
                    ctx.tools_used = ctx.tools_used + new_tools
                    ctx.tokens_used += retry_result.total_tokens or 0
            except Exception as exc:
                log.warning("Escalation attempt %d failed: %s",
                            attempt + 1, exc)
                break
            finally:
                set_shell_caller_context("autonomous")


# ---------------------------------------------------------------------------
# 6. BrowserCleanupMiddleware
# ---------------------------------------------------------------------------


class BrowserCleanupMiddleware(Middleware):
    """Stop browser if any browser tools were used.

    Skips chat — users may have multi-turn browser sessions where the
    browser must stay open between messages.
    """

    def after_invoke(self, ctx: InvocationContext) -> None:
        if ctx.source == "chat":
            return
        if not ctx.tools_used:
            return
        if ctx.daemon and hasattr(ctx.daemon, "_cleanup_browser_after_task"):
            try:
                ctx.daemon._cleanup_browser_after_task(ctx.tools_used)
            except Exception as exc:
                log.warning("BrowserCleanupMiddleware: %s", exc)


# ===========================================================================
# PER-STEP MIDDLEWARES (before_model / after_model / wrap_tool_call)
# ===========================================================================


# ---------------------------------------------------------------------------
# 7. DanglingToolCallRepairMiddleware  (before_model)
#    Inspired by DeerFlow's DanglingToolCallMiddleware.
#    Runs EVERY step to catch dangling tool calls that appear mid-loop
#    (e.g. if a prior tool execution crashed and left an orphan).
# ---------------------------------------------------------------------------


class DanglingToolCallRepairMiddleware(Middleware):
    """Fix broken tool message sequences before each LLM call.

    Scans the message list for assistant messages whose tool_calls have no
    matching tool-result message and injects synthetic placeholders in the
    correct position (immediately after the offending assistant message).

    Unlike the one-shot repair at history load time, this runs every step
    so it catches breaks that happen mid-loop (crash in tool execution,
    timeout, etc.).
    """

    _stats_lock = threading.Lock()
    _stats = {"repairs": 0, "patched_calls": 0}

    @classmethod
    def get_stats(cls) -> dict:
        with cls._stats_lock:
            return dict(cls._stats)

    def before_model(self, ctx: InvocationContext, messages: list, step: int) -> list | None:
        existing_ids: set[str] = set()
        for msg in messages:
            if isinstance(msg, dict) and msg.get("role") == "tool":
                tc_id = msg.get("tool_call_id")
                if tc_id:
                    existing_ids.add(tc_id)

        needs_patch = False
        for msg in messages:
            if not isinstance(msg, dict) or msg.get("role") != "assistant":
                continue
            for tc in msg.get("tool_calls") or []:
                tc_id = tc.get("id")
                if tc_id and tc_id not in existing_ids:
                    needs_patch = True
                    break
            if needs_patch:
                break

        if not needs_patch:
            return None

        patched: list[dict] = []
        patched_ids: set[str] = set()
        patch_count = 0

        for msg in messages:
            patched.append(msg)
            if not isinstance(msg, dict) or msg.get("role") != "assistant":
                continue
            for tc in msg.get("tool_calls") or []:
                tc_id = tc.get("id")
                if tc_id and tc_id not in existing_ids and tc_id not in patched_ids:
                    patched.append({
                        "role": "tool",
                        "tool_call_id": tc_id,
                        "name": tc.get("function", {}).get("name", "unknown"),
                        "content": "[Tool call was interrupted and did not return a result.]",
                    })
                    patched_ids.add(tc_id)
                    patch_count += 1

        if patch_count:
            log.warning("DanglingToolCallRepair: patched %d orphan(s) at step %d",
                        patch_count, step)
            with self._stats_lock:
                self._stats["repairs"] += 1
                self._stats["patched_calls"] += patch_count

        return patched


# ---------------------------------------------------------------------------
# 8. SubagentLimitMiddleware  (after_model)
#    Inspired by DeerFlow's SubagentLimitMiddleware.
#    Truncates excess parallel delegate_task calls from a single LLM response
#    to prevent resource exhaustion.
# ---------------------------------------------------------------------------


class SubagentLimitMiddleware(Middleware):
    """Truncate excess parallel subagent calls (task + delegate_task).

    When the LLM generates more subagent tool calls than allowed in
    a single response, this middleware keeps only the first N and removes
    the rest.  Enforced at the model-response level, more reliable than
    prompt-based limits.
    """

    _SUBAGENT_TOOL_NAMES = frozenset({"task", "delegate_task"})
    MIN_LIMIT = 2
    MAX_LIMIT = 5

    def __init__(self, max_concurrent: int = 3):
        self._max = max(self.MIN_LIMIT, min(self.MAX_LIMIT, max_concurrent))

    def after_model(self, ctx: InvocationContext, messages: list, response_msg: dict, step: int) -> dict | None:
        tool_calls = response_msg.get("tool_calls")
        if not tool_calls:
            return None

        subagent_indices = [
            i for i, tc in enumerate(tool_calls)
            if tc.get("function", {}).get("name") in self._SUBAGENT_TOOL_NAMES
        ]
        if len(subagent_indices) <= self._max:
            return None

        drop = set(subagent_indices[self._max:])
        truncated = [tc for i, tc in enumerate(tool_calls) if i not in drop]
        dropped = len(drop)
        log.warning("SubagentLimit: truncated %d excess subagent call(s) at step %d",
                    dropped, step)

        updated = dict(response_msg)
        updated["tool_calls"] = truncated
        return updated


# ---------------------------------------------------------------------------
# 9. ContextSummarizationMiddleware  (before_model)
#    Proactively summarize long conversations BEFORE the LLM call, so the
#    engine's emergency compaction is a last resort instead of the norm.
# ---------------------------------------------------------------------------


_SUMMARIZATION_TOKEN_THRESHOLD = 60_000  # trigger proactive summarization
_MIN_MESSAGES_FOR_SUMMARIZATION = 25


class ContextSummarizationMiddleware(Middleware):
    """Proactively summarize conversation when context grows too large.

    Runs before each LLM call.  When estimated tokens exceed the threshold
    and there are enough messages, builds a deterministic summary of older
    messages and replaces them with a compact context summary.

    This is complementary to the engine's built-in compaction (which kicks
    in at 80k tokens or 30 messages).  By acting earlier and at a lower
    threshold, we avoid hitting the engine's emergency path.
    """

    def __init__(self, token_threshold: int = _SUMMARIZATION_TOKEN_THRESHOLD,
                 min_messages: int = _MIN_MESSAGES_FOR_SUMMARIZATION):
        self._threshold = token_threshold
        self._min_msgs = min_messages

    def before_model(self, ctx: InvocationContext, messages: list, step: int) -> list | None:
        if len(messages) < self._min_msgs:
            return None

        est_tokens = self._estimate_tokens(messages)
        if est_tokens < self._threshold:
            return None

        return self._summarize(messages, step)

    @staticmethod
    def _estimate_tokens(messages: list) -> int:
        total = 0
        for m in messages:
            content = m.get("content") or ""
            if isinstance(content, str):
                total += len(content)
            elif isinstance(content, list):
                for part in content:
                    if isinstance(part, dict):
                        total += len(str(part.get("text", "")))
        return total // 4

    def _summarize(self, messages: list, step: int) -> list:
        system_msg = messages[0]
        recent_count = min(15, len(messages) // 2)
        recent = messages[-recent_count:]
        old = messages[1:-recent_count]
        if not old:
            return messages

        user_msg = None
        user_idx = None
        for i, m in enumerate(old):
            if m.get("role") == "user" and "[Context Summary" not in (m.get("content") or ""):
                user_msg = m
                user_idx = i

        summary_parts = []
        tool_names_seen: set[str] = set()
        assistant_count = 0
        user_count = 0

        for m in old:
            role = m.get("role", "")
            if role == "assistant":
                assistant_count += 1
                for tc in m.get("tool_calls") or []:
                    name = tc.get("function", {}).get("name", "")
                    if name:
                        tool_names_seen.add(name)
            elif role == "user":
                user_count += 1
            elif role == "tool":
                content = m.get("content") or ""
                if len(content) > 200:
                    name = m.get("name", "tool")
                    first_line = content.split("\n")[0][:150]
                    summary_parts.append(f"  {name}: {first_line}...")

        summary_lines = [
            f"[Context Summary — {len(old)} older messages compacted at step {step}]",
            f"  Turns: {user_count} user, {assistant_count} assistant",
        ]
        if tool_names_seen:
            summary_lines.append(f"  Tools used: {', '.join(sorted(tool_names_seen))}")
        if summary_parts:
            summary_lines.append("  Key results:")
            summary_lines.extend(summary_parts[:10])

        summary_text = "\n".join(summary_lines)

        result = [system_msg]
        if user_msg:
            result.append(user_msg)
        result.append({"role": "user", "content": summary_text})
        result.extend(recent)

        log.info("ContextSummarization: compacted %d→%d messages at step %d",
                 len(messages), len(result), step)
        return result


# ---------------------------------------------------------------------------
# 10. ToolCallInterceptMiddleware  (wrap_tool_call)
#     Inspired by DeerFlow's ClarificationMiddleware.
#     Generic interceptor that can redirect specific tool calls.
# ---------------------------------------------------------------------------


class ToolCallInterceptMiddleware(Middleware):
    """Intercept specific tool calls and return custom results.

    Supports two modes:
    1. Static intercept: tool_name → fixed result string
    2. Dynamic intercept: tool_name → callable(ctx, tool_name, args) → str

    When a tool call is intercepted, the tool is NOT executed — the
    intercept result is returned directly to the LLM.

    Primary use cases:
    - Clarification: intercept "ask_clarification" to pause the loop
    - Safety: block dangerous tools at a finer grain than ToolScopeMiddleware
    - Mocking: replace real tools with test results during testing
    """

    def __init__(self, intercepts: dict[str, str | callable] | None = None):
        self._intercepts: dict[str, str | callable] = dict(intercepts or {})

    def register(self, tool_name: str, handler: str | callable) -> None:
        """Register an intercept for a tool name."""
        self._intercepts[tool_name] = handler

    def unregister(self, tool_name: str) -> None:
        """Remove an intercept."""
        self._intercepts.pop(tool_name, None)

    def wrap_tool_call(self, ctx: InvocationContext, tool_name: str, args: dict, step: int) -> str | None:
        handler = self._intercepts.get(tool_name)
        if handler is None:
            return None

        log.info("ToolCallIntercept: intercepting %s at step %d", tool_name, step)
        if callable(handler):
            try:
                return handler(ctx, tool_name, args)
            except Exception as exc:
                log.error("ToolCallIntercept handler for %s failed: %s",
                          tool_name, exc, exc_info=True)
                return f"Intercept error: {exc}"
        return str(handler)


# ---------------------------------------------------------------------------
# 11. ResponseIntegrityMiddleware  (after_invoke)
#
#     Uses a fast LLM subagent to detect when the primary model's response
#     claims it performed tool actions that never actually executed.
#     If fabrication is detected, re-runs the engine with a correction
#     prompt so the model either performs the real action or responds
#     honestly.  This is a semantic check — no brittle regex patterns.
# ---------------------------------------------------------------------------


class ResponseIntegrityMiddleware(Middleware):
    """Catch fabricated tool-action claims via LLM-based validation."""

    MAX_RETRIES = 1

    _ACTIVE_SOURCES = {"chat", "channel", "action"}

    # Tools that only read data — fabricating side effects is impossible
    # when only these were called, so we can skip the integrity check.
    _READ_ONLY_TOOLS = frozenset({
        "list_future_features", "get_future_feature", "get_feature_stats",
        "memory_search",
        "file_read", "file_search",
        "web_search", "web_fetch",
        "uptime", "task_complete", "__reasoning__",
    })

    _VALIDATOR_PROMPT = (
        "You are a response auditor. Compare the assistant's response against "
        "the tools it actually called. Does the response claim a completed "
        "action that required a tool NOT in the tools_used list?\n\n"
        "Examples of fabrication:\n"
        "- Says 'feature queued' but add_future_feature not in tools_used\n"
        "- Says 'file saved' but file_write not in tools_used\n"
        "- Says 'email sent' but send_email not in tools_used\n\n"
        "Mentioning tools in passing or suggesting future actions is NOT fabrication.\n\n"
        "Reply with exactly one word: PASS or FAIL\n"
        "If FAIL, add a colon and short reason. Example: FAIL: claims feature "
        "was queued but add_future_feature was never called"
    )

    def after_invoke(self, ctx: InvocationContext) -> None:
        if not ctx.config.get("enable_response_integrity", True):
            log.debug("ResponseIntegrity: disabled by config")
            return
        if ctx.source not in self._ACTIVE_SOURCES:
            log.debug("ResponseIntegrity: skipped (source=%s)", ctx.source)
            return
        if not ctx.result_text or len(ctx.result_text.strip()) < 30:
            log.debug("ResponseIntegrity: skipped (short/empty result)")
            return
        if not ctx.tools_used:
            log.debug("ResponseIntegrity: skipped (no tools used)")
            return
        engine = ctx.engine
        if not engine:
            log.debug("ResponseIntegrity: skipped (no engine)")
            return

        unique_tools = set(ctx.tools_used)
        if unique_tools <= self._READ_ONLY_TOOLS:
            log.info(
                "ResponseIntegrity: skipped (all %d tool(s) are read-only: %s)",
                len(unique_tools),
                ", ".join(sorted(unique_tools)),
            )
            return

        tools_used_str = ", ".join(sorted(unique_tools)) or "(none)"
        log.info(
            "ResponseIntegrity: validating response (tools_used=[%s], response_len=%d)",
            tools_used_str, len(ctx.result_text),
        )
        _push_chat_progress(ctx, "Validating response integrity...")

        validator_input = (
            f"## Tools actually called\n[{tools_used_str}]\n\n"
            f"## Assistant response to validate\n{ctx.result_text[:3000]}"
        )

        verdict = None
        for _attempt in range(2):
            try:
                raw = engine.single_shot(
                    system_prompt=self._VALIDATOR_PROMPT,
                    user_message=validator_input,
                    temperature=0.1 + _attempt * 0.2,
                    max_tokens=256,
                )
                if raw and raw.strip():
                    verdict = raw.strip()
                    break
                log.info(
                    "ResponseIntegrity: validator attempt %d returned empty",
                    _attempt + 1,
                )
            except Exception as exc:
                log.warning(
                    "ResponseIntegrityMiddleware: validator attempt %d failed: %s",
                    _attempt + 1, exc,
                )

        if not verdict:
            log.warning(
                "ResponseIntegrity: validator returned no verdict after retries — "
                "cannot validate, proceeding without check"
            )
            return

        log.info("ResponseIntegrity: verdict=%s", verdict[:200])

        if verdict.upper().startswith("PASS"):
            return

        if not verdict.upper().startswith("FAIL"):
            log.info("ResponseIntegrity: unrecognized verdict format — skipping")
            return

        explanation = verdict[5:].strip().lstrip(":").strip()
        log.warning(
            "ResponseIntegrity FAIL — tools_used=[%s] explanation=%s",
            tools_used_str, explanation,
        )

        for attempt in range(self.MAX_RETRIES):
            cancelled = ctx.cancel_check() if ctx.cancel_check else False
            if cancelled:
                break
            _push_chat_progress(ctx, "Correcting response (integrity retry)...")

            correction_msg = (
                f"INTEGRITY VIOLATION (detected by automated audit):\n"
                f"{explanation}\n\n"
                f"Tools you actually called this session: [{tools_used_str}].\n"
                f"Your previous response claimed to have performed an action that "
                f"was never executed. You MUST either:\n"
                f"1. Actually call the missing tool NOW to complete the action, OR\n"
                f"2. Respond honestly — tell the user what you actually did and "
                f"what still needs to be done.\n"
                f"Do NOT repeat the false claim."
            )

            retry_history = list(ctx.history or [])
            retry_history.append({"role": "user", "content": ctx.user_message})
            retry_history.append({"role": "assistant", "content": ctx.result_text})

            if ctx.source == "chat" and "session" in ctx.meta:
                session = ctx.meta["session"]
                if hasattr(session, "token_chunks"):
                    session.token_chunks.clear()

            from ghost_tools import set_shell_caller_context
            set_shell_caller_context(ctx.caller_context)
            try:
                retry_result = engine.run(
                    system_prompt=ctx.system_prompt,
                    user_message=correction_msg,
                    tool_registry=ctx.tool_registry,
                    max_steps=ctx.max_steps,
                    max_tokens=ctx.max_tokens,
                    temperature=ctx.temperature,
                    force_tool=ctx.force_tool,
                    on_step=ctx.on_step,
                    history=retry_history,
                    cancel_check=ctx.cancel_check,
                    images=ctx.images,
                    image_b64=ctx.image_b64,
                    enable_reasoning=ctx.enable_reasoning,
                    model_override=ctx.model_override,
                    coding_model_chain=ctx.coding_model_chain,
                    hook_runner=(ctx.daemon.hooks if ctx.daemon else None),
                    tool_intent_security=getattr(
                        ctx.daemon, "tool_intent_security", None),
                    tool_event_bus=getattr(
                        ctx.daemon, "tool_event_bus", None),
                    on_token=ctx.on_token,
                )
                if retry_result:
                    ctx.result = retry_result
                    ctx.result_text = retry_result.text or ""
                    new_tools = [
                        tc["tool"]
                        for tc in (retry_result.tool_calls or [])
                    ]
                    ctx.tools_used = ctx.tools_used + new_tools
                    ctx.tokens_used += retry_result.total_tokens or 0
            except Exception as exc:
                log.warning(
                    "ResponseIntegrity retry %d failed: %s",
                    attempt + 1, exc,
                )
                break
            finally:
                set_shell_caller_context("autonomous")


# ===========================================================================
# FACTORY — builds the default chain with all middlewares
# ===========================================================================


def build_default_chain() -> MiddlewareChain:
    """Construct the standard middleware pipeline.

    Order matters:
    1.  IdentityMiddleware       — prepend identity to system prompt
    2.  SkillMatchMiddleware     — match skills, inject prompts
    3.  ImageIntentMiddleware    — classify image intent, strip base64
                                   or swap to vision model
    4.  ToolScopeMiddleware      — restrict available tools
    5.  CallerContextMiddleware  — set caller context for shell
    6.  DanglingToolCallRepairMiddleware  — fix broken history each step
    7.  ContextSummarizationMiddleware    — proactive context compaction
    8.  SubagentLimitMiddleware  — cap parallel delegate_task calls
    9.  GiveUpDetectionMiddleware         — retry on give-up responses
    10. ResponseIntegrityMiddleware       — catch fabricated action claims
    11. BrowserCleanupMiddleware — cleanup browser after task
    """
    return MiddlewareChain([
        IdentityMiddleware(),
        SkillMatchMiddleware(),
        ImageIntentMiddleware(),
        ToolScopeMiddleware(),
        CallerContextMiddleware(),
        DanglingToolCallRepairMiddleware(),
        ContextSummarizationMiddleware(),
        SubagentLimitMiddleware(),
        GiveUpDetectionMiddleware(),
        ResponseIntegrityMiddleware(),
        BrowserCleanupMiddleware(),
    ])
