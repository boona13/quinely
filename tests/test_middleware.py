"""Tests for the ghost_middleware module.

Covers:
  - InvocationContext dataclass
  - Middleware base class (all 6 hook points)
  - MiddlewareChain (invocation + per-step delegation)
  - IdentityMiddleware
  - SkillMatchMiddleware
  - ToolScopeMiddleware
  - CallerContextMiddleware
  - GiveUpDetectionMiddleware
  - BrowserCleanupMiddleware
  - DanglingToolCallRepairMiddleware   (per-step: before_model)
  - SubagentLimitMiddleware            (per-step: after_model)
  - ContextSummarizationMiddleware     (per-step: before_model)
  - ToolCallInterceptMiddleware        (per-step: wrap_tool_call)
  - build_default_chain factory
  - Integration tests
"""

from __future__ import annotations

import types
from unittest.mock import MagicMock, patch

import pytest

from ghost_middleware import (
    BrowserCleanupMiddleware,
    CallerContextMiddleware,
    ContextSummarizationMiddleware,
    DanglingToolCallRepairMiddleware,
    GiveUpDetectionMiddleware,
    IdentityMiddleware,
    InvocationContext,
    Middleware,
    MiddlewareChain,
    SkillMatchMiddleware,
    SubagentLimitMiddleware,
    ToolCallInterceptMiddleware,
    ToolScopeMiddleware,
    build_default_chain,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_ctx(source="monitor", **kw) -> InvocationContext:
    return InvocationContext(source=source, **kw)


def _fake_tool_registry(names: list[str]):
    """Return a mock ToolRegistry with .names() and .subset()."""
    reg = MagicMock()
    reg.names.return_value = list(names)

    def _subset(keep):
        return _fake_tool_registry(keep)

    reg.subset.side_effect = _subset
    return reg


def _fake_daemon(
    *,
    identity="You are Ghost.",
    skills=None,
    skill_model=None,
    has_browser_cleanup=True,
):
    """Return a mock daemon with the attrs middleware expects."""
    d = MagicMock()
    d._build_identity_context.return_value = identity
    d._resolve_skill_model.return_value = skill_model
    d.hooks = MagicMock()
    d.tool_intent_security = MagicMock()
    d.tool_event_bus = MagicMock()
    d.skill_loader = MagicMock()
    d.skill_loader.match.return_value = skills or []
    d.skill_loader.build_skills_prompt.return_value = "<skills>...</skills>"
    d.skill_loader.get_tools_for_skills.return_value = []
    d.skill_loader.check_reload.return_value = None
    if has_browser_cleanup:
        d._cleanup_browser_after_task = MagicMock()
    else:
        del d._cleanup_browser_after_task
    return d


def _fake_engine(text="done", tool_calls=None, total_tokens=100):
    """Return a mock engine whose .run() returns a result-like object."""
    result = types.SimpleNamespace(
        text=text,
        tool_calls=tool_calls or [],
        total_tokens=total_tokens,
    )
    engine = MagicMock()
    engine.run.return_value = result
    return engine


# ---------------------------------------------------------------------------
# InvocationContext
# ---------------------------------------------------------------------------


class TestInvocationContext:
    def test_system_prompt_joins_parts(self):
        ctx = _make_ctx(system_prompt_parts=["Part A", "Part B"])
        assert ctx.system_prompt == "Part A\n\nPart B"

    def test_system_prompt_skips_empty(self):
        ctx = _make_ctx(system_prompt_parts=["A", "", None, "B"])
        assert ctx.system_prompt == "A\n\nB"

    def test_defaults(self):
        ctx = _make_ctx()
        assert ctx.result is None
        assert ctx.result_text == ""
        assert ctx.tools_used == []
        assert ctx.tokens_used == 0
        assert ctx.escalation_count == 0
        assert ctx.caller_context == "autonomous"
        assert ctx.matched_skills == []


# ---------------------------------------------------------------------------
# Middleware base — all 6 hook points are no-ops
# ---------------------------------------------------------------------------


class TestMiddlewareBase:
    def test_noop_invoke_hooks(self):
        mw = Middleware()
        ctx = _make_ctx()
        mw.before_invoke(ctx)
        mw.after_invoke(ctx)

    def test_noop_model_hooks(self):
        mw = Middleware()
        ctx = _make_ctx()
        msgs = [{"role": "system", "content": "hi"}]
        assert mw.before_model(ctx, msgs, 0) is None
        assert mw.after_model(ctx, msgs, {"role": "assistant", "content": "ok"}, 0) is None

    def test_noop_tool_hooks(self):
        mw = Middleware()
        ctx = _make_ctx()
        assert mw.wrap_tool_call(ctx, "file_read", {}, 0) is None
        assert mw.after_tool_call(ctx, "file_read", {}, "content", 0) is None


# ---------------------------------------------------------------------------
# MiddlewareChain — invocation lifecycle
# ---------------------------------------------------------------------------


class TestMiddlewareChain:
    def test_invoke_calls_engine(self):
        engine = _fake_engine(text="hi")
        ctx = _make_ctx(
            engine=engine,
            user_message="hello",
            system_prompt_parts=["sys"],
        )
        chain = MiddlewareChain()
        chain.invoke(ctx)
        assert engine.run.called
        assert ctx.result_text == "hi"

    def test_invoke_without_engine(self):
        ctx = _make_ctx(engine=None)
        chain = MiddlewareChain()
        chain.invoke(ctx)
        assert ctx.result is None

    def test_before_and_after_order(self):
        order = []

        class MW1(Middleware):
            def before_invoke(self, ctx):
                order.append("b1")

            def after_invoke(self, ctx):
                order.append("a1")

        class MW2(Middleware):
            def before_invoke(self, ctx):
                order.append("b2")

            def after_invoke(self, ctx):
                order.append("a2")

        ctx = _make_ctx(engine=_fake_engine())
        MiddlewareChain([MW1(), MW2()]).invoke(ctx)
        assert order == ["b1", "b2", "a1", "a2"]

    def test_before_error_does_not_block_chain(self):
        class Boom(Middleware):
            def before_invoke(self, ctx):
                raise RuntimeError("boom")

        engine = _fake_engine(text="ok")
        ctx = _make_ctx(engine=engine)
        MiddlewareChain([Boom()]).invoke(ctx)
        assert ctx.result_text == "ok"

    def test_engine_error_stored_in_meta(self):
        engine = MagicMock()
        engine.run.side_effect = RuntimeError("kaboom")
        ctx = _make_ctx(engine=engine)
        MiddlewareChain().invoke(ctx)
        assert "engine_error" in ctx.meta
        assert "kaboom" in ctx.result_text

    def test_tool_calls_extracted(self):
        engine = _fake_engine(
            text="ok",
            tool_calls=[{"tool": "web_search"}, {"tool": "file_read"}],
            total_tokens=42,
        )
        ctx = _make_ctx(engine=engine)
        MiddlewareChain().invoke(ctx)
        assert ctx.tools_used == ["web_search", "file_read"]
        assert ctx.tokens_used == 42

    def test_middleware_chain_passes_self_to_engine(self):
        engine = _fake_engine(text="ok")
        ctx = _make_ctx(engine=engine)
        chain = MiddlewareChain()
        chain.invoke(ctx)
        call_kwargs = engine.run.call_args[1]
        assert call_kwargs.get("middleware_chain") is chain

    def test_active_ctx_set_during_invoke(self):
        captured = {}

        class Spy(Middleware):
            def before_invoke(self, ctx):
                captured["ctx"] = ctx

        chain = MiddlewareChain([Spy()])
        ctx = _make_ctx(engine=_fake_engine())
        chain.invoke(ctx)
        assert captured["ctx"] is ctx
        assert chain._active_ctx is None


# ---------------------------------------------------------------------------
# MiddlewareChain — per-step hook delegation
# ---------------------------------------------------------------------------


class TestChainPerStepHooks:
    def test_before_model_chains_middlewares(self):
        class AddMsg(Middleware):
            def before_model(self, ctx, messages, step):
                return messages + [{"role": "user", "content": "injected"}]

        chain = MiddlewareChain([AddMsg()])
        ctx = _make_ctx(engine=_fake_engine())
        chain._active_ctx = ctx
        msgs = [{"role": "system", "content": "sys"}]
        result = chain.before_model(msgs, 0)
        assert len(result) == 2
        assert result[-1]["content"] == "injected"

    def test_before_model_none_passthrough(self):
        chain = MiddlewareChain([Middleware()])
        chain._active_ctx = _make_ctx()
        msgs = [{"role": "system", "content": "sys"}]
        result = chain.before_model(msgs, 0)
        assert result is msgs

    def test_before_model_error_does_not_crash(self):
        class Boom(Middleware):
            def before_model(self, ctx, messages, step):
                raise RuntimeError("explode")

        chain = MiddlewareChain([Boom()])
        chain._active_ctx = _make_ctx()
        msgs = [{"role": "system", "content": "sys"}]
        result = chain.before_model(msgs, 0)
        assert result is msgs

    def test_after_model_returns_override(self):
        class Truncate(Middleware):
            def after_model(self, ctx, messages, response_msg, step):
                return {"role": "assistant", "content": "truncated"}

        chain = MiddlewareChain([Truncate()])
        chain._active_ctx = _make_ctx()
        msgs = []
        result = chain.after_model(msgs, {"role": "assistant", "content": "long"}, 0)
        assert result["content"] == "truncated"

    def test_after_model_none_passthrough(self):
        chain = MiddlewareChain([Middleware()])
        chain._active_ctx = _make_ctx()
        result = chain.after_model([], {"role": "assistant"}, 0)
        assert result is None

    def test_wrap_tool_call_first_wins(self):
        class Intercept1(Middleware):
            def wrap_tool_call(self, ctx, name, args, step):
                return "intercepted_1"

        class Intercept2(Middleware):
            def wrap_tool_call(self, ctx, name, args, step):
                return "intercepted_2"

        chain = MiddlewareChain([Intercept1(), Intercept2()])
        chain._active_ctx = _make_ctx()
        result = chain.wrap_tool_call("tool", {}, 0)
        assert result == "intercepted_1"

    def test_wrap_tool_call_none_means_proceed(self):
        chain = MiddlewareChain([Middleware()])
        chain._active_ctx = _make_ctx()
        result = chain.wrap_tool_call("tool", {}, 0)
        assert result is None

    def test_after_tool_call_chains(self):
        class Suffix(Middleware):
            def after_tool_call(self, ctx, name, args, result, step):
                return result + " [modified]"

        chain = MiddlewareChain([Suffix()])
        chain._active_ctx = _make_ctx()
        result = chain.after_tool_call("tool", {}, "original", 0)
        assert result == "original [modified]"

    def test_after_tool_call_none_keeps_original(self):
        chain = MiddlewareChain([Middleware()])
        chain._active_ctx = _make_ctx()
        result = chain.after_tool_call("tool", {}, "original", 0)
        assert result == "original"


# ---------------------------------------------------------------------------
# IdentityMiddleware
# ---------------------------------------------------------------------------


class TestIdentityMiddleware:
    def test_prepends_identity(self):
        daemon = _fake_daemon(identity="SOUL+USER")
        ctx = _make_ctx(daemon=daemon, system_prompt_parts=["body"])
        IdentityMiddleware().before_invoke(ctx)
        assert ctx.system_prompt_parts[0] == "SOUL+USER"
        assert ctx.system_prompt_parts[1] == "body"

    def test_no_daemon(self):
        ctx = _make_ctx(daemon=None, system_prompt_parts=["body"])
        IdentityMiddleware().before_invoke(ctx)
        assert ctx.system_prompt_parts == ["body"]

    def test_empty_identity_skipped(self):
        daemon = _fake_daemon(identity="")
        ctx = _make_ctx(daemon=daemon, system_prompt_parts=["body"])
        IdentityMiddleware().before_invoke(ctx)
        assert ctx.system_prompt_parts == ["body"]

    def test_exception_does_not_propagate(self):
        daemon = MagicMock()
        daemon._build_identity_context.side_effect = RuntimeError("fail")
        ctx = _make_ctx(daemon=daemon, system_prompt_parts=["body"])
        IdentityMiddleware().before_invoke(ctx)
        assert ctx.system_prompt_parts == ["body"]


# ---------------------------------------------------------------------------
# SkillMatchMiddleware
# ---------------------------------------------------------------------------


class TestSkillMatchMiddleware:
    def test_injects_skills_prompt(self):
        skill = MagicMock()
        skill.name = "web"
        skill.model = None
        daemon = _fake_daemon(skills=[skill])
        ctx = _make_ctx(
            daemon=daemon,
            config={},
            user_message="search the web",
        )
        SkillMatchMiddleware().before_invoke(ctx)
        assert ctx.matched_skills == [skill]
        assert "<skills>" in ctx.system_prompt_parts[-1]

    def test_content_type_passed_for_monitor(self):
        daemon = _fake_daemon()
        ctx = _make_ctx(
            source="monitor",
            daemon=daemon,
            config={},
            user_message="hi",
            meta={"content_type": "url"},
        )
        SkillMatchMiddleware().before_invoke(ctx)
        daemon.skill_loader.match.assert_called_once_with(
            "hi", "url", disabled=set()
        )

    def test_content_type_none_for_chat(self):
        daemon = _fake_daemon()
        ctx = _make_ctx(
            source="chat",
            daemon=daemon,
            config={},
            user_message="hi",
        )
        SkillMatchMiddleware().before_invoke(ctx)
        daemon.skill_loader.match.assert_called_once_with(
            "hi", None, disabled=set()
        )

    def test_disabled_skills_merged(self):
        daemon = _fake_daemon()
        proj = MagicMock()
        proj.config = {"disabled_skills": ["x"]}
        ctx = _make_ctx(
            daemon=daemon,
            config={"disabled_skills": ["y"]},
            active_project=proj,
            user_message="hi",
        )
        SkillMatchMiddleware().before_invoke(ctx)
        call_args = daemon.skill_loader.match.call_args
        assert call_args[1]["disabled"] == {"x", "y"}

    def test_skill_model_override(self):
        skill = MagicMock()
        skill.name = "coding"
        skill.model = "gpt-4"
        daemon = _fake_daemon(skills=[skill], skill_model="gpt-4")
        ctx = _make_ctx(daemon=daemon, config={}, user_message="code")
        SkillMatchMiddleware().before_invoke(ctx)
        assert ctx.model_override == "gpt-4"

    def test_existing_model_override_preserved(self):
        skill = MagicMock()
        skill.name = "coding"
        skill.model = "gpt-4"
        daemon = _fake_daemon(skills=[skill], skill_model="gpt-4")
        ctx = _make_ctx(
            daemon=daemon,
            config={},
            user_message="code",
            model_override="my-model",
        )
        SkillMatchMiddleware().before_invoke(ctx)
        assert ctx.model_override == "my-model"

    def test_no_skill_loader(self):
        daemon = MagicMock()
        daemon.skill_loader = None
        ctx = _make_ctx(daemon=daemon, config={})
        SkillMatchMiddleware().before_invoke(ctx)
        assert ctx.matched_skills == []

    def test_project_skill_filtering(self):
        skill_a = MagicMock()
        skill_a.name = "a"
        skill_b = MagicMock()
        skill_b.name = "b"
        daemon = _fake_daemon(skills=[skill_a, skill_b])
        proj = MagicMock()
        proj.config = {"skills": ["a"], "disabled_skills": []}
        ctx = _make_ctx(
            daemon=daemon,
            config={},
            active_project=proj,
            user_message="hi",
        )
        SkillMatchMiddleware().before_invoke(ctx)
        assert ctx.matched_skills == [skill_a]


# ---------------------------------------------------------------------------
# ToolScopeMiddleware
# ---------------------------------------------------------------------------


class TestToolScopeMiddleware:
    def test_removes_evolve_tools_for_chat(self):
        names = ["file_read", "evolve_plan", "evolve_apply", "web_search"]
        reg = _fake_tool_registry(names)
        ctx = _make_ctx(source="chat", tool_registry=reg)
        ToolScopeMiddleware().before_invoke(ctx)
        result_names = ctx.tool_registry.names()
        assert "evolve_plan" not in result_names
        assert "file_read" in result_names

    def test_cron_also_removes_feature_mutate(self):
        names = [
            "file_read", "evolve_plan", "start_future_feature",
            "complete_future_feature",
        ]
        reg = _fake_tool_registry(names)
        ctx = _make_ctx(source="cron", tool_registry=reg)
        ToolScopeMiddleware().before_invoke(ctx)
        result_names = ctx.tool_registry.names()
        assert "start_future_feature" not in result_names
        assert "evolve_plan" not in result_names
        assert "file_read" in result_names

    def test_evolution_runner_gets_allowlist(self):
        names = [
            "file_read", "web_search", "evolve_plan", "evolve_apply",
            "shell_exec", "task_complete", "some_random_tool",
        ]
        reg = _fake_tool_registry(names)
        ctx = _make_ctx(
            source="cron",
            tool_registry=reg,
            meta={"is_evolution_runner": True},
        )
        ToolScopeMiddleware().before_invoke(ctx)
        result_names = set(ctx.tool_registry.names())
        assert "evolve_plan" in result_names
        assert "shell_exec" in result_names
        assert "some_random_tool" not in result_names

    def test_none_registry_noop(self):
        ctx = _make_ctx(tool_registry=None)
        ToolScopeMiddleware().before_invoke(ctx)
        assert ctx.tool_registry is None

    def test_skill_scoping_for_monitor(self):
        skill = MagicMock()
        skill.name = "web"
        daemon = _fake_daemon()
        daemon.skill_loader.get_tools_for_skills.return_value = [
            "web_search", "web_fetch"
        ]
        names = [
            "file_read", "web_search", "web_fetch", "memory_save",
            "memory_search", "notify", "shell_exec",
        ]
        reg = _fake_tool_registry(names)
        ctx = _make_ctx(
            source="monitor",
            tool_registry=reg,
            daemon=daemon,
            matched_skills=[skill],
        )
        ToolScopeMiddleware().before_invoke(ctx)
        result_names = set(ctx.tool_registry.names())
        assert "web_search" in result_names
        assert "memory_save" in result_names
        assert "notify" in result_names
        assert "shell_exec" not in result_names

    def test_skill_scoping_chat_project_broader(self):
        skill = MagicMock()
        skill.name = "coding"
        proj = MagicMock()
        daemon = _fake_daemon()
        daemon.skill_loader.get_tools_for_skills.return_value = ["shell_exec"]
        names = [
            "file_read", "shell_exec", "memory_save", "memory_search",
            "notify", "project_list", "grep", "task_complete",
        ]
        reg = _fake_tool_registry(names)
        ctx = _make_ctx(
            source="chat",
            tool_registry=reg,
            daemon=daemon,
            matched_skills=[skill],
            active_project=proj,
        )
        ToolScopeMiddleware().before_invoke(ctx)
        result_names = set(ctx.tool_registry.names())
        assert "shell_exec" in result_names
        assert "project_list" in result_names
        assert "grep" in result_names
        assert "task_complete" in result_names


# ---------------------------------------------------------------------------
# CallerContextMiddleware
# ---------------------------------------------------------------------------


class TestCallerContextMiddleware:
    @pytest.mark.parametrize(
        "source,expected",
        [
            ("chat", "interactive"),
            ("channel", "interactive"),
            ("action", "interactive"),
            ("monitor", "interactive"),
            ("cron", "autonomous"),
            ("unknown", "autonomous"),
        ],
    )
    def test_mapping(self, source, expected):
        ctx = _make_ctx(source=source)
        CallerContextMiddleware().before_invoke(ctx)
        assert ctx.caller_context == expected


# ---------------------------------------------------------------------------
# GiveUpDetectionMiddleware
# ---------------------------------------------------------------------------


class TestGiveUpDetection:
    def test_skips_cron(self):
        ctx = _make_ctx(
            source="cron",
            result_text="I cannot do this task, sorry.",
        )
        GiveUpDetectionMiddleware().after_invoke(ctx)
        assert ctx.escalation_count == 0

    def test_skips_monitor(self):
        ctx = _make_ctx(
            source="monitor",
            result_text="I cannot do this task, sorry.",
        )
        GiveUpDetectionMiddleware().after_invoke(ctx)
        assert ctx.escalation_count == 0

    def test_skips_short_text(self):
        ctx = _make_ctx(source="chat", result_text="ok")
        GiveUpDetectionMiddleware().after_invoke(ctx)
        assert ctx.escalation_count == 0

    @patch("ghost_middleware.GiveUpDetectionMiddleware.MAX_RETRIES", 1)
    def test_escalation_on_give_up(self):
        engine = _fake_engine(text="retried ok")
        ctx = _make_ctx(
            source="chat",
            engine=engine,
            result_text="I'm sorry, I cannot complete this task for you.",
            user_message="do something",
            history=[],
        )
        with patch("ghost._detected_give_up", side_effect=[True, False]):
            with patch("ghost._ESCALATION_COACHING", "try harder"):
                GiveUpDetectionMiddleware().after_invoke(ctx)
        assert ctx.escalation_count == 1
        assert ctx.result_text == "retried ok"

    def test_respects_cancel(self):
        ctx = _make_ctx(
            source="action",
            result_text="I'm sorry, I cannot complete this task for you.",
            cancel_check=lambda: True,
            engine=_fake_engine(),
        )
        with patch("ghost._detected_give_up", return_value=True):
            GiveUpDetectionMiddleware().after_invoke(ctx)
        assert ctx.escalation_count == 0

    def test_clears_chat_token_chunks(self):
        session = MagicMock()
        session.token_chunks = ["a", "b"]
        engine = _fake_engine(text="retried")
        ctx = _make_ctx(
            source="chat",
            engine=engine,
            result_text="I'm sorry, I cannot complete this task for you.",
            user_message="do it",
            history=[],
            meta={"session": session},
        )
        with patch("ghost._detected_give_up", side_effect=[True, False]):
            with patch("ghost._ESCALATION_COACHING", "try again"):
                GiveUpDetectionMiddleware().after_invoke(ctx)
        assert session.token_chunks == []


# ---------------------------------------------------------------------------
# BrowserCleanupMiddleware
# ---------------------------------------------------------------------------


class TestBrowserCleanup:
    def test_calls_cleanup(self):
        daemon = _fake_daemon()
        ctx = _make_ctx(
            daemon=daemon,
            tools_used=["browser_navigate", "browser_click"],
        )
        BrowserCleanupMiddleware().after_invoke(ctx)
        daemon._cleanup_browser_after_task.assert_called_once_with(
            ["browser_navigate", "browser_click"]
        )

    def test_no_tools_noop(self):
        daemon = _fake_daemon()
        ctx = _make_ctx(daemon=daemon, tools_used=[])
        BrowserCleanupMiddleware().after_invoke(ctx)
        daemon._cleanup_browser_after_task.assert_not_called()

    def test_no_cleanup_method(self):
        daemon = _fake_daemon(has_browser_cleanup=False)
        ctx = _make_ctx(
            daemon=daemon,
            tools_used=["browser_navigate"],
        )
        BrowserCleanupMiddleware().after_invoke(ctx)

    def test_skips_chat(self):
        daemon = _fake_daemon()
        ctx = _make_ctx(
            source="chat",
            daemon=daemon,
            tools_used=["browser_navigate"],
        )
        BrowserCleanupMiddleware().after_invoke(ctx)
        daemon._cleanup_browser_after_task.assert_not_called()


# ===========================================================================
# PER-STEP MIDDLEWARES
# ===========================================================================


# ---------------------------------------------------------------------------
# DanglingToolCallRepairMiddleware
# ---------------------------------------------------------------------------


class TestDanglingToolCallRepair:
    def test_no_repair_when_clean(self):
        mw = DanglingToolCallRepairMiddleware()
        ctx = _make_ctx()
        msgs = [
            {"role": "system", "content": "sys"},
            {"role": "user", "content": "hello"},
            {"role": "assistant", "content": "", "tool_calls": [
                {"id": "tc1", "function": {"name": "file_read", "arguments": "{}"}}
            ]},
            {"role": "tool", "tool_call_id": "tc1", "content": "file content"},
        ]
        result = mw.before_model(ctx, msgs, 0)
        assert result is None

    def test_patches_dangling_call(self):
        mw = DanglingToolCallRepairMiddleware()
        ctx = _make_ctx()
        msgs = [
            {"role": "system", "content": "sys"},
            {"role": "assistant", "content": "", "tool_calls": [
                {"id": "tc1", "function": {"name": "shell_exec", "arguments": "{}"}},
                {"id": "tc2", "function": {"name": "file_read", "arguments": "{}"}},
            ]},
            {"role": "tool", "tool_call_id": "tc1", "content": "ok"},
        ]
        result = mw.before_model(ctx, msgs, 5)
        assert result is not None
        tool_msgs = [m for m in result if m.get("role") == "tool"]
        assert len(tool_msgs) == 2
        patched = [m for m in tool_msgs if m.get("tool_call_id") == "tc2"]
        assert len(patched) == 1
        assert "interrupted" in patched[0]["content"]

    def test_patches_inserted_after_assistant(self):
        mw = DanglingToolCallRepairMiddleware()
        ctx = _make_ctx()
        msgs = [
            {"role": "system", "content": "sys"},
            {"role": "assistant", "content": "", "tool_calls": [
                {"id": "orphan", "function": {"name": "web_fetch", "arguments": "{}"}}
            ]},
            {"role": "user", "content": "what happened?"},
        ]
        result = mw.before_model(ctx, msgs, 0)
        assert result[0]["role"] == "system"
        assert result[1]["role"] == "assistant"
        assert result[2]["role"] == "tool"
        assert result[2]["tool_call_id"] == "orphan"
        assert result[3]["role"] == "user"

    def test_no_false_positive_on_normal_messages(self):
        mw = DanglingToolCallRepairMiddleware()
        ctx = _make_ctx()
        msgs = [
            {"role": "system", "content": "sys"},
            {"role": "user", "content": "hello"},
            {"role": "assistant", "content": "hi there"},
        ]
        result = mw.before_model(ctx, msgs, 0)
        assert result is None

    def test_stats_updated(self):
        mw = DanglingToolCallRepairMiddleware()
        ctx = _make_ctx()
        msgs = [
            {"role": "assistant", "content": "", "tool_calls": [
                {"id": "x", "function": {"name": "t", "arguments": "{}"}}
            ]},
        ]
        mw.before_model(ctx, msgs, 0)
        stats = DanglingToolCallRepairMiddleware.get_stats()
        assert stats["repairs"] >= 1
        assert stats["patched_calls"] >= 1


# ---------------------------------------------------------------------------
# SubagentLimitMiddleware
# ---------------------------------------------------------------------------


class TestSubagentLimit:
    def test_no_truncation_under_limit(self):
        mw = SubagentLimitMiddleware(max_concurrent=3)
        ctx = _make_ctx()
        msg = {"role": "assistant", "content": "", "tool_calls": [
            {"function": {"name": "delegate_task", "arguments": "{}"}},
            {"function": {"name": "delegate_task", "arguments": "{}"}},
            {"function": {"name": "file_read", "arguments": "{}"}},
        ]}
        result = mw.after_model(ctx, [], msg, 0)
        assert result is None

    def test_truncates_excess_delegates(self):
        mw = SubagentLimitMiddleware(max_concurrent=2)
        ctx = _make_ctx()
        msg = {"role": "assistant", "content": "", "tool_calls": [
            {"function": {"name": "delegate_task", "arguments": "{}"}},
            {"function": {"name": "file_read", "arguments": "{}"}},
            {"function": {"name": "delegate_task", "arguments": "{}"}},
            {"function": {"name": "delegate_task", "arguments": "{}"}},
            {"function": {"name": "delegate_task", "arguments": "{}"}},
        ]}
        result = mw.after_model(ctx, [], msg, 0)
        assert result is not None
        delegate_count = sum(
            1 for tc in result["tool_calls"]
            if tc["function"]["name"] == "delegate_task"
        )
        assert delegate_count == 2
        total_count = len(result["tool_calls"])
        assert total_count == 3

    def test_no_tool_calls_noop(self):
        mw = SubagentLimitMiddleware()
        ctx = _make_ctx()
        msg = {"role": "assistant", "content": "just text"}
        assert mw.after_model(ctx, [], msg, 0) is None

    def test_limit_clamped(self):
        mw = SubagentLimitMiddleware(max_concurrent=100)
        assert mw._max == 5
        mw2 = SubagentLimitMiddleware(max_concurrent=0)
        assert mw2._max == 2


# ---------------------------------------------------------------------------
# ContextSummarizationMiddleware
# ---------------------------------------------------------------------------


class TestContextSummarization:
    def _build_messages(self, count: int, content_len: int = 500) -> list:
        msgs = [{"role": "system", "content": "You are Ghost."}]
        for i in range(count - 1):
            role = "user" if i % 2 == 0 else "assistant"
            msgs.append({"role": role, "content": f"msg {i} " + "x" * content_len})
        return msgs

    def test_no_action_below_threshold(self):
        mw = ContextSummarizationMiddleware(token_threshold=100_000)
        ctx = _make_ctx()
        msgs = self._build_messages(10, content_len=100)
        result = mw.before_model(ctx, msgs, 0)
        assert result is None

    def test_no_action_too_few_messages(self):
        mw = ContextSummarizationMiddleware(min_messages=50)
        ctx = _make_ctx()
        msgs = self._build_messages(10, content_len=10000)
        result = mw.before_model(ctx, msgs, 0)
        assert result is None

    def test_summarizes_large_context(self):
        mw = ContextSummarizationMiddleware(token_threshold=100, min_messages=5)
        ctx = _make_ctx()
        msgs = self._build_messages(30, content_len=200)
        result = mw.before_model(ctx, msgs, 5)
        assert result is not None
        assert len(result) < len(msgs)
        assert result[0]["role"] == "system"
        has_summary = any("[Context Summary" in (m.get("content") or "") for m in result)
        assert has_summary

    def test_preserves_system_message(self):
        mw = ContextSummarizationMiddleware(token_threshold=10, min_messages=3)
        ctx = _make_ctx()
        msgs = self._build_messages(20, content_len=500)
        result = mw.before_model(ctx, msgs, 0)
        assert result[0] == msgs[0]

    def test_summary_contains_tool_info(self):
        mw = ContextSummarizationMiddleware(token_threshold=10, min_messages=3)
        ctx = _make_ctx()
        msgs = [
            {"role": "system", "content": "sys"},
        ]
        for i in range(20):
            msgs.append({"role": "assistant", "content": "", "tool_calls": [
                {"function": {"name": "file_read", "arguments": "{}"}}
            ]})
            msgs.append({"role": "tool", "content": "x" * 500, "name": "file_read"})
        msgs.append({"role": "user", "content": "what now?"})
        result = mw.before_model(ctx, msgs, 3)
        summary_msgs = [m for m in result if "[Context Summary" in (m.get("content") or "")]
        assert len(summary_msgs) == 1
        assert "file_read" in summary_msgs[0]["content"]


# ---------------------------------------------------------------------------
# ToolCallInterceptMiddleware
# ---------------------------------------------------------------------------


class TestToolCallIntercept:
    def test_static_intercept(self):
        mw = ToolCallInterceptMiddleware({"dangerous_tool": "BLOCKED: not allowed"})
        ctx = _make_ctx()
        result = mw.wrap_tool_call(ctx, "dangerous_tool", {}, 0)
        assert result == "BLOCKED: not allowed"

    def test_dynamic_intercept(self):
        def handler(ctx, name, args):
            return f"Intercepted {name} with {len(args)} args"

        mw = ToolCallInterceptMiddleware({"my_tool": handler})
        ctx = _make_ctx()
        result = mw.wrap_tool_call(ctx, "my_tool", {"a": 1, "b": 2}, 0)
        assert result == "Intercepted my_tool with 2 args"

    def test_no_intercept_passes_through(self):
        mw = ToolCallInterceptMiddleware({"other_tool": "blocked"})
        ctx = _make_ctx()
        result = mw.wrap_tool_call(ctx, "safe_tool", {}, 0)
        assert result is None

    def test_register_and_unregister(self):
        mw = ToolCallInterceptMiddleware()
        ctx = _make_ctx()
        assert mw.wrap_tool_call(ctx, "tool_x", {}, 0) is None

        mw.register("tool_x", "intercepted")
        assert mw.wrap_tool_call(ctx, "tool_x", {}, 0) == "intercepted"

        mw.unregister("tool_x")
        assert mw.wrap_tool_call(ctx, "tool_x", {}, 0) is None

    def test_handler_exception_returns_error(self):
        def bad_handler(ctx, name, args):
            raise ValueError("handler broke")

        mw = ToolCallInterceptMiddleware({"tool": bad_handler})
        ctx = _make_ctx()
        result = mw.wrap_tool_call(ctx, "tool", {}, 0)
        assert "Intercept error" in result

    def test_empty_intercepts(self):
        mw = ToolCallInterceptMiddleware()
        ctx = _make_ctx()
        assert mw.wrap_tool_call(ctx, "anything", {}, 0) is None


# ---------------------------------------------------------------------------
# build_default_chain
# ---------------------------------------------------------------------------


class TestBuildDefaultChain:
    def test_returns_full_default_chain(self):
        from ghost_middleware import (
            ImageIntentMiddleware, ResponseIntegrityMiddleware,
            TraceMiddleware,
        )
        chain = build_default_chain()
        expected = [
            IdentityMiddleware,
            SkillMatchMiddleware,
            ImageIntentMiddleware,
            ToolScopeMiddleware,
            CallerContextMiddleware,
            DanglingToolCallRepairMiddleware,
            ContextSummarizationMiddleware,
            SubagentLimitMiddleware,
            GiveUpDetectionMiddleware,
            ResponseIntegrityMiddleware,
            BrowserCleanupMiddleware,
            TraceMiddleware,
        ]
        assert len(chain._middlewares) == len(expected)
        for mw, cls in zip(chain._middlewares, expected):
            assert isinstance(mw, cls)
        # TraceMiddleware must be LAST so it records the final, post-retry outcome.
        assert isinstance(chain._middlewares[-1], TraceMiddleware)


# ---------------------------------------------------------------------------
# Integration: full chain
# ---------------------------------------------------------------------------


class TestIntegration:
    def test_full_chain_monitor_source(self):
        """Simulate a process_text invocation through the full chain."""
        engine = _fake_engine(text="Analysis complete.", total_tokens=50)
        daemon = _fake_daemon(identity="I am Ghost.")
        ctx = _make_ctx(
            source="monitor",
            user_message="some clipboard text",
            system_prompt_parts=["Analyze this."],
            tool_registry=_fake_tool_registry(
                ["file_read", "web_search", "evolve_plan"]
            ),
            daemon=daemon,
            engine=engine,
            config={},
            meta={"content_type": "long_text"},
        )
        chain = build_default_chain()
        chain.invoke(ctx)

        assert ctx.system_prompt_parts[0] == "I am Ghost."
        assert ctx.result_text == "Analysis complete."
        assert ctx.tokens_used == 50
        assert ctx.caller_context == "interactive"

    def test_full_chain_cron_source(self):
        """Simulate a cron invocation — no give-up detection."""
        engine = _fake_engine(text="Cron done.")
        daemon = _fake_daemon(identity="Ghost cron")
        ctx = _make_ctx(
            source="cron",
            user_message="run task",
            system_prompt_parts=["Do the thing."],
            tool_registry=_fake_tool_registry(
                ["file_read", "evolve_plan", "start_future_feature"]
            ),
            daemon=daemon,
            engine=engine,
            config={},
        )
        chain = build_default_chain()
        chain.invoke(ctx)

        assert ctx.result_text == "Cron done."
        assert ctx.caller_context == "autonomous"
        scoped_names = set(ctx.tool_registry.names())
        assert "evolve_plan" not in scoped_names
        assert "start_future_feature" not in scoped_names

    def test_chain_passes_middleware_chain_to_engine(self):
        """Verify that the engine receives the chain for per-step hooks."""
        engine = _fake_engine(text="ok")
        daemon = _fake_daemon()
        ctx = _make_ctx(
            source="chat",
            engine=engine,
            daemon=daemon,
            config={},
            user_message="hi",
        )
        chain = build_default_chain()
        chain.invoke(ctx)
        call_kwargs = engine.run.call_args[1]
        assert call_kwargs["middleware_chain"] is chain
