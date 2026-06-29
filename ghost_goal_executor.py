"""
Quinely Goal Executor Engine — Deterministic step-by-step goal execution
with delivery, feed integration, and failed-step recovery.

Execution flow per goal:
  1. Recover any failed steps that are under the retry cap
  2. Plan the goal if it has no plan yet (focused LLM session)
  3. Execute ALL pending steps back-to-back (each in its own LLM session)
  4. Verify each step was actually marked done; retry if not
  5. Quality-check the output — reject once if insufficient, then accept
  6. Call goal_complete and return completion info for delivery

The caller (daemon cron handler) is responsible for delivery dispatch,
feed posting, and console output — the engine returns structured results.
"""

import json
import logging
import os
import time
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Dict, List, Optional

from ghost_goals import GoalStore, STEP_PENDING, STEP_COMPLETED, STEP_FAILED

log = logging.getLogger("quinely.goal_executor")

MAX_STEP_TOOL_STEPS = 30
MAX_QA_TOOL_STEPS   = 15
MAX_RETRIES         = 2
MAX_GOALS_PER_RUN   = 5
MAX_STEPS_PER_GOAL  = 20
MAX_STEP_RETRIES    = 3   # max times a failed step can be retried across cycles


# ═══════════════════════════════════════════════════════════════════
#  SYSTEM PROMPTS
# ═══════════════════════════════════════════════════════════════════

_STEP_SYSTEM = """You are Quinely executing ONE specific step of a user goal.
You have a single job: execute the step described below using the available tools,
then call goal_step_done() to record the result.

RULES (non-negotiable):
1. Execute the step using real tools — web_search, web_fetch, memory_save, shell_exec, etc.
2. After the step is done, call goal_step_done(goal_id=..., step_id=..., result=<one-line summary>).
3. If the step produces data that later steps need (URLs, findings, lists, etc.),
   call goal_set_scratch(goal_id=..., key=<descriptive_key>, value=<data>) BEFORE goal_step_done.
4. If the step is to save or compile output, call goal_set_output(goal_id=..., output=<full content>)
   BEFORE calling goal_step_done.
5. Do NOT attempt other steps. Do NOT call goal_complete. Do exactly one step.
6. NEVER narrate. If you would say "I searched..." without having called web_search, go back and call it.

VERIFICATION — Evidence before claims:
7. Before calling goal_step_done, verify the step actually succeeded.
   If you ran a command, check the exit code. If you fetched data, confirm it contains
   what was needed. Do NOT claim success based on assumptions.
8. If the step fails or produces incomplete results, say so in the result summary.
   Honest failure is better than false success.
"""

_PLAN_SYSTEM = """You are Quinely creating an execution plan for a user goal.
Your ONLY job: call goal_plan(goal_id=..., steps=[...]) with 3-6 concrete steps.

PLANNING RULES:
1. Each step must be completable by Quinely with 1-3 tool calls (web_search, web_fetch,
   memory_save, file_write, shell_exec, notify, etc.).
2. Each step description must be specific and actionable — state exactly what to do
   and what tool(s) to use, not vague instructions like "research the topic".
3. Include a verification action in steps that produce artifacts (e.g., "verify the file
   was written" or "confirm the search returned results").
4. The FINAL step must always be: "Compile full output and call goal_set_output, then mark cycle complete."
5. Do not do any research now. Just create the plan.
6. Prefer fewer, well-defined steps over many vague ones. Each step should have
   a clear success criterion.
"""

_QA_SYSTEM = """You are Quinely doing a quality check on a completed goal execution.
Read the goal description and the output that was produced.
Decide: does the output fully satisfy what the user asked for?

VERIFICATION RULES — Evidence before claims:
1. Re-read the original goal description carefully.
2. Check the output against each requirement in the goal — not just "looks good overall".
3. If the goal asked for specific data, verify it is present in the output.
4. Do NOT use words like "should", "probably", or "seems to" — state facts.
5. If output is empty or clearly incomplete, that is a NO.

If YES: call goal_complete(goal_id=..., summary=<one sentence>) to close the cycle.
If NO: call goal_add_observation with a specific note about what's missing or wrong.
       Do NOT call goal_complete — the executor will handle retries.

Do NOT re-execute any steps. Do NOT call goal_set_output again. Just evaluate.
"""


# ═══════════════════════════════════════════════════════════════════
#  ENGINE
# ═══════════════════════════════════════════════════════════════════

class GoalExecutorEngine:
    """Deterministic Python-controlled goal executor."""

    def __init__(self, cfg: Dict[str, Any], tool_registry, auth_store=None,
                 provider_chain=None):
        self.cfg = cfg
        self.tool_registry = tool_registry
        self.auth_store = auth_store
        self.provider_chain = provider_chain
        self.store = GoalStore()

    # ------------------------------------------------------------------
    # Public entry point
    # ------------------------------------------------------------------

    def run_all(self) -> Dict:
        """Process all actionable goals. Returns structured results for delivery."""
        goals = self.store.list_actionable()
        if not goals:
            return {"message": "No actionable goals.", "processed": 0, "results": []}

        goals = goals[:MAX_GOALS_PER_RUN]
        results = []

        for goal in goals:
            log.info("[goal_executor] Processing goal [%s] %s (status=%s)",
                     goal["id"], goal["title"], goal["status"])
            try:
                result = self._process_goal(goal)
                results.append(result)
            except Exception as exc:
                log.error("[goal_executor] Goal [%s] failed with exception: %s",
                          goal["id"], exc, exc_info=True)
                results.append({"goal_id": goal["id"], "title": goal.get("title", ""),
                                "error": str(exc), "completed": False})

        return {
            "processed": len(results),
            "results": results,
        }

    # ------------------------------------------------------------------
    # Goal lifecycle
    # ------------------------------------------------------------------

    def _process_goal(self, goal: Dict) -> Dict:
        goal_id = goal["id"]
        goal_start = time.time()
        result_info = {
            "goal_id": goal_id,
            "title": goal.get("title", ""),
            "goal_text": goal.get("goal_text", ""),
            "completed": False,
            "output": None,
            "summary": None,
            "delivery": goal.get("delivery", ""),
            "recurrence": goal.get("recurrence"),
            "qa_passed": None,
            "failed_step_ids": [],
            "retried_step_ids": [],
        }

        # Phase 0 — Recover failed steps that are under the retry cap
        recovered = self.store.retry_failed_steps(goal_id, max_retries=MAX_STEP_RETRIES)
        if recovered > 0:
            log.info("[goal_executor] Recovered %d failed step(s) for [%s]", recovered, goal_id)
            goal = self.store.get(goal_id)
            result_info["retried_step_ids"] = [
                s["id"] for s in goal.get("plan", [])
                if s.get("retry_count", 0) > 0 and s["status"] == STEP_PENDING
            ]

        # Phase 1 — plan if needed
        if goal["status"] == "pending_plan":
            log.info("[goal_executor] Planning goal [%s]", goal_id)
            ok = self._plan_goal(goal)
            if not ok:
                return {**result_info, "phase": "plan", "ok": False}
            goal = self.store.get(goal_id)

        if not goal or goal["status"] != "active":
            return {**result_info, "skipped": True, "status": goal.get("status") if goal else "deleted"}

        # Phase 2 — execute ALL pending steps back-to-back
        steps_run = []
        consecutive_failures = 0
        for _ in range(MAX_STEPS_PER_GOAL):
            step = self.store.next_pending_step(goal)
            if not step:
                break

            # Circuit breaker: 3 consecutive failures → stop and reassess
            if consecutive_failures >= 3:
                log.warning(
                    "[goal_executor] Goal [%s] hit 3 consecutive step failures. "
                    "Stopping execution to prevent thrashing.", goal_id)
                self.store.add_observation(
                    goal_id,
                    "CIRCUIT BREAKER: 3 consecutive steps failed. This may indicate "
                    "a systemic issue (wrong approach, missing dependency, bad plan). "
                    "Remaining steps skipped — needs reassessment.")
                break

            log.info("[goal_executor] Executing step [%s/%s] %s",
                     goal_id, step["id"], step["description"][:60])
            step_start = time.time()
            ok = self._execute_step(goal, step)
            step_elapsed = int((time.time() - step_start) * 1000)
            steps_run.append({
                "step_id": step["id"],
                "description": step["description"][:100],
                "ok": ok,
                "elapsed_ms": step_elapsed,
            })
            if ok:
                consecutive_failures = 0
            else:
                consecutive_failures += 1
            goal = self.store.get(goal_id)
            if not goal:
                break
        else:
            log.error("[goal_executor] Goal [%s] hit step limit (%d). Stopping.",
                      goal_id, MAX_STEPS_PER_GOAL)

        result_info["steps_run"] = steps_run
        result_info["total_elapsed_ms"] = int((time.time() - goal_start) * 1000)

        if not goal:
            return result_info

        # Reload goal to pick up any changes from step execution
        goal = self.store.get(goal_id)
        if not goal:
            return result_info

        all_done = self.store.all_steps_done(goal)
        has_pending = self.store.next_pending_step(goal) is not None
        has_failed = any(s["status"] == STEP_FAILED for s in goal.get("plan", []))

        # Capture failed step IDs and error messages for reflection
        result_info["failed_step_ids"] = [
            s["id"] for s in goal.get("plan", []) if s["status"] == STEP_FAILED
        ]
        result_info["step_errors"] = {
            s["id"]: s.get("error", "unknown")
            for s in goal.get("plan", [])
            if s["status"] == STEP_FAILED and s.get("error")
        }

        # Phase 3 — quality check + complete
        if all_done:
            output = goal.get("last_output", "")
            if output:
                log.info("[goal_executor] Quality check for goal [%s]", goal_id)
                qa_passed = self._quality_check(goal)
                result_info["qa_passed"] = qa_passed

                refreshed = self.store.get(goal_id)
                if refreshed:
                    cc_before = goal.get("completion_count", 0)
                    cc_after = refreshed.get("completion_count", 0)

                    if cc_after > cc_before:
                        result_info["completed"] = True
                        result_info["output"] = refreshed.get("last_output", "")
                        result_info["summary"] = refreshed.get("last_summary", "")
                    elif not qa_passed:
                        log.warning("[goal_executor] QA rejected [%s] — force-completing.", goal_id)
                        self.store.complete_goal(goal_id, summary="Completed (QA flagged possible gaps — see observations)")
                        refreshed = self.store.get(goal_id)
                        if refreshed:
                            result_info["completed"] = True
                            result_info["output"] = refreshed.get("last_output", "")
                            result_info["summary"] = refreshed.get("last_summary", "")
            else:
                log.warning("[goal_executor] Goal [%s] all steps done but no output. Completing.", goal_id)
                self.store.complete_goal(goal_id, summary="Completed — no output was produced by the steps.")
                result_info["completed"] = True

        elif not has_pending and has_failed:
            # All steps either completed or permanently failed (exceeded retry cap).
            # No pending steps remain — this goal is stuck. Complete it with a note
            # about which steps failed so it doesn't zombie forever.
            failed_ids = [s["id"] for s in goal.get("plan", []) if s["status"] == STEP_FAILED]
            log.warning("[goal_executor] Goal [%s] stuck: steps %s permanently failed. Force-completing.",
                        goal_id, failed_ids)
            summary = f"Completed with {len(failed_ids)} failed step(s): {', '.join(failed_ids)}"
            self.store.complete_goal(goal_id, summary=summary)
            refreshed = self.store.get(goal_id)
            if refreshed:
                result_info["completed"] = True
                result_info["output"] = refreshed.get("last_output", "")
                result_info["summary"] = summary

        result_info["total_elapsed_ms"] = int((time.time() - goal_start) * 1000)
        result_info["observations"] = goal.get("observations", [])[-10:]
        return result_info

    # ------------------------------------------------------------------
    # Phase 1 — Planning
    # ------------------------------------------------------------------

    def _plan_goal(self, goal: Dict) -> bool:
        goal_id = goal["id"]
        prompt = (
            f"Create an execution plan for this goal.\n\n"
            f"Goal ID: {goal_id}\n"
            f"Title: {goal['title']}\n"
            f"Description: {goal['goal_text']}\n"
            f"Recurrence: {goal.get('recurrence') or 'one-shot'}\n\n"
            f"Call goal_plan(goal_id='{goal_id}', steps=[...]) now."
        )

        plan_tools = ["goal_plan", "goal_get"]
        result = self._run_session(
            system=_PLAN_SYSTEM,
            message=prompt,
            tools=plan_tools,
            max_steps=10,
            label=f"plan:{goal_id}",
        )

        updated = self.store.get(goal_id)
        if updated and updated.get("plan"):
            log.info("[goal_executor] Plan set for [%s]: %d steps",
                     goal_id, len(updated["plan"]))
            return True

        log.warning("[goal_executor] Plan not set for [%s]. Session text: %s",
                    goal_id, (result or "")[:200])
        return False

    # ------------------------------------------------------------------
    # Phase 2 — Step execution
    # ------------------------------------------------------------------

    def _execute_step(self, goal: Dict, step: Dict) -> bool:
        goal_id = goal["id"]
        step_id = step["id"]

        observations = goal.get("observations", [])
        obs_text = "\n".join(
            f"  - {o['text']}" for o in observations[-8:]
            if isinstance(o, dict)
        ) or "  (none yet)"

        # Include scratch data so the step has access to prior steps' structured output
        scratch = goal.get("scratch", {})
        scratch_text = ""
        if scratch:
            scratch_lines = []
            for k, v in scratch.items():
                val_preview = str(v)[:500]
                scratch_lines.append(f"  {k}: {val_preview}")
            scratch_text = (
                "\nScratch space (structured data from previous steps):\n"
                + "\n".join(scratch_lines) + "\n"
            )

        last_output_note = ""
        if goal.get("last_output"):
            last_output_note = (
                "\nNOTE: A previous cycle already produced output for this goal. "
                "This is a NEW cycle — produce FRESH content, do not reuse prior output."
            )

        prompt = (
            f"Execute step {step_id} of goal {goal_id}.{last_output_note}\n\n"
            f"Goal: {goal['title']}\n"
            f"Goal description: {goal['goal_text']}\n\n"
            f"Step to execute:\n"
            f"  ID: {step_id}\n"
            f"  Description: {step['description']}\n\n"
            f"Prior observations (working memory from previous steps/runs):\n{obs_text}\n"
            f"{scratch_text}\n"
            f"Instructions:\n"
            f"1. Execute this step using the appropriate tools.\n"
            f"2. If this step produces data that later steps need, call "
            f"   goal_set_scratch(goal_id='{goal_id}', key=<name>, value=<data>).\n"
            f"3. If this step involves compiling or delivering the final output, "
            f"   call goal_set_output(goal_id='{goal_id}', output=<full markdown content>).\n"
            f"4. Then call goal_step_done(goal_id='{goal_id}', step_id='{step_id}', "
            f"   result=<one-line summary of what you did>).\n"
            f"5. Do NOT execute other steps. Stop after this one."
        )

        step_tools = [
            "goal_step_done", "goal_step_fail", "goal_add_observation",
            "goal_set_output", "goal_set_scratch",
            "web_search", "web_fetch", "memory_save", "memory_search",
            "file_write", "file_read", "shell_exec",
            "notify", "channel_send",
        ]

        for attempt in range(MAX_RETRIES):
            retry_note = f"\n\nATTEMPT {attempt + 1}/{MAX_RETRIES}. You MUST call goal_step_done at the end." if attempt > 0 else ""
            self._run_session(
                system=_STEP_SYSTEM,
                message=prompt + retry_note,
                tools=step_tools,
                max_steps=MAX_STEP_TOOL_STEPS,
                label=f"step:{goal_id}/{step_id}",
            )

            refreshed = self.store.get(goal_id)
            if not refreshed:
                return False
            for s in refreshed.get("plan", []):
                if s["id"] == step_id and s["status"] in (STEP_COMPLETED, STEP_FAILED):
                    log.info("[goal_executor] Step [%s/%s] confirmed %s (attempt %d)",
                             goal_id, step_id, s["status"], attempt + 1)
                    return True

            log.warning("[goal_executor] Step [%s/%s] not marked done after attempt %d",
                        goal_id, step_id, attempt + 1)

        log.error("[goal_executor] Step [%s/%s] failed after %d attempts. Force-failing.",
                  goal_id, step_id, MAX_RETRIES)
        self.store.mark_step_failed(goal_id, step_id,
                                    error=f"Executor: step not completed after {MAX_RETRIES} attempts")
        return False

    # ------------------------------------------------------------------
    # Phase 3 — Quality check
    # ------------------------------------------------------------------

    def _quality_check(self, goal: Dict) -> bool:
        """Validate output against goal. Returns True if QA approved."""
        goal_id = goal["id"]
        output = goal.get("last_output", "")

        prompt = (
            f"Quality check for goal {goal_id}.\n\n"
            f"Original goal: {goal['goal_text']}\n\n"
            f"Output produced:\n{output[:3000]}\n"
            + ("...[truncated]" if len(output) > 3000 else "") +
            f"\n\nDoes this output satisfy the goal? "
            f"If YES: call goal_complete(goal_id='{goal_id}', summary=...) to finish.\n"
            f"If NO: call goal_add_observation with what's missing. Do NOT call goal_complete."
        )

        qa_tools = [
            "goal_complete", "goal_add_observation",
        ]

        self._run_session(
            system=_QA_SYSTEM,
            message=prompt,
            tools=qa_tools,
            max_steps=MAX_QA_TOOL_STEPS,
            label=f"qa:{goal_id}",
        )

        refreshed = self.store.get(goal_id)
        if not refreshed:
            return False

        cc_before = goal.get("completion_count", 0)
        cc_after = refreshed.get("completion_count", 0)
        return cc_after > cc_before

    # ------------------------------------------------------------------
    # LLM session runner
    # ------------------------------------------------------------------

    def _run_session(self, system: str, message: str, tools: List[str],
                     max_steps: int, label: str) -> Optional[str]:
        from ghost_loop import ToolLoopEngine

        api_key = None
        if self.auth_store:
            try:
                api_key = self.auth_store.get_api_key("openrouter")
            except Exception:
                pass
        if not api_key:
            api_key = os.environ.get("OPENROUTER_API_KEY", "")
        if not api_key:
            log.error("[goal_executor] No API key for session %s", label)
            return None

        available = set(self.tool_registry.names())
        valid_tools = [t for t in tools if t in available]
        if not valid_tools:
            log.error("[goal_executor] No valid tools for session %s", label)
            return None

        focused_registry = self.tool_registry.subset(valid_tools)
        model = self.cfg.get("model", "anthropic/claude-sonnet-4")

        engine = ToolLoopEngine(
            api_key=api_key,
            model=model,
            fallback_models=self.cfg.get("fallback_models", []),
            auth_store=self.auth_store,
            provider_chain=self.provider_chain,
        )

        start = time.time()
        try:
            result = engine.run(
                system_prompt=system,
                user_message=message,
                tool_registry=focused_registry,
                max_steps=max_steps,
                temperature=0.2,
                max_tokens=4096,
            )
            elapsed = int((time.time() - start) * 1000)
            log.info("[goal_executor] Session %s done in %dms (%d steps)",
                     label, elapsed, result.steps)
            return result.text or ""
        except Exception as exc:
            log.error("[goal_executor] Session %s failed: %s", label, exc, exc_info=True)
            return None


# ═══════════════════════════════════════════════════════════════════
#  DELIVERY DISPATCH — called by daemon after executor completes
# ═══════════════════════════════════════════════════════════════════

def deliver_goal_results(results: List[Dict], daemon) -> None:
    """Post-process executor results: deliver output, post to feed, emit events.

    Called by the daemon's cron handler after the goal executor runs.
    This is the "last mile" that closes the loop back to the user.
    """
    from ghost_console import console_bus

    for r in results:
        goal_id = r.get("goal_id", "?")
        title = r.get("title", "Untitled goal")

        if r.get("error"):
            console_bus.emit("error", "cron", "goal_executor",
                             f"Goal [{goal_id}] error: {r['error'][:200]}")
            continue

        if r.get("skipped"):
            continue

        steps = r.get("steps_run", [])
        done_count = sum(1 for s in steps if s.get("ok"))
        console_bus.emit("info", "cron", "goal_executor",
                         f"Goal [{goal_id}] {title}: {done_count}/{len(steps)} steps completed")

        if not r.get("completed"):
            continue

        # ── Goal completed — deliver output ──

        output = r.get("output", "")
        summary = r.get("summary", "Goal completed")
        delivery = r.get("delivery", "")
        recurrence = r.get("recurrence")

        recurrence_label = f" (recurring: {recurrence})" if recurrence else ""
        console_bus.emit("success", "cron", "goal_executor",
                         f"Goal completed: {title}{recurrence_label}")

        # 1. Post to activity feed
        try:
            from ghost import append_feed
            feed_entry = {
                "time": datetime.now().isoformat(),
                "type": "goal",
                "source": f"[Goal] {title}",
                "result": (summary or output[:300]) if output else summary,
                "goal_id": goal_id,
            }
            append_feed(feed_entry, daemon.cfg.get("max_feed_items", 50))
        except Exception as exc:
            log.warning("[goal_executor] Failed to post goal to feed: %s", exc)

        # 2. Dispatch delivery based on the delivery method
        if delivery:
            _dispatch_delivery(delivery, title, output, summary, goal_id, daemon)


def _dispatch_delivery(delivery: str, title: str, output: str,
                       summary: str, goal_id: str, daemon) -> None:
    """Route goal output to the configured delivery channel."""
    log.info("[goal_executor] Delivering goal [%s] via: %s", goal_id, delivery)

    try:
        if delivery == "notify":
            _deliver_notify(title, summary or output[:500], daemon)

        elif delivery == "chat":
            _deliver_chat_feed(title, output, summary, goal_id, daemon)

        elif delivery.startswith("file:"):
            _deliver_file(delivery[5:].strip(), title, output, goal_id)

        elif delivery in ("telegram", "discord", "slack", "whatsapp"):
            _deliver_channel(delivery, title, output, summary, daemon)

        elif delivery == "memory":
            _deliver_memory(title, output, summary, goal_id, daemon)

        else:
            log.warning("[goal_executor] Unknown delivery method '%s' for goal [%s]",
                        delivery, goal_id)

    except Exception as exc:
        log.error("[goal_executor] Delivery failed for goal [%s] via %s: %s",
                  goal_id, delivery, exc, exc_info=True)


def _deliver_notify(title: str, message: str, daemon) -> None:
    """Send via the notify tool (OS notification + configured channels)."""
    notify_tool = daemon.tool_registry.get("notify") if daemon.tool_registry else None
    if notify_tool and callable(notify_tool.get("execute")):
        notify_tool["execute"](
            title=f"Goal Complete: {title}",
            message=message[:1000],
            priority="normal",
        )
    else:
        import ghost_platform
        ghost_platform.send_notification(f"Goal: {title}", message[:500])


def _deliver_channel(channel: str, title: str, output: str,
                     summary: str, daemon) -> None:
    """Send output via a messaging channel (telegram, discord, etc.)."""
    if not getattr(daemon, "channel_router", None):
        log.warning("[goal_executor] No channel router — cannot deliver via %s", channel)
        return

    text = f"**Goal Complete: {title}**\n\n"
    if summary:
        text += f"*{summary}*\n\n"
    text += output[:3000]
    if len(output) > 3000:
        text += "\n\n...[truncated — full output in Goals dashboard]"

    daemon.channel_router.send(text, channel=channel, priority="normal",
                               title=f"Goal: {title}")


def _deliver_chat_feed(title: str, output: str, summary: str,
                       goal_id: str, daemon) -> None:
    """Post the full output as a chat feed entry."""
    try:
        from ghost import append_feed
        entry = {
            "time": datetime.now().isoformat(),
            "type": "goal_delivery",
            "source": f"[Goal Delivery] {title}",
            "result": output[:5000] if output else summary,
            "goal_id": goal_id,
        }
        append_feed(entry, daemon.cfg.get("max_feed_items", 50))
    except Exception as exc:
        log.warning("[goal_executor] Chat feed delivery failed: %s", exc)


def _deliver_file(path_str: str, title: str, output: str, goal_id: str) -> None:
    """Write the output to a file."""
    path = Path(path_str).expanduser()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        f"# {title}\n"
        f"<!-- Goal: {goal_id} | Generated: {datetime.now().isoformat()} -->\n\n"
        f"{output}",
        encoding="utf-8",
    )
    log.info("[goal_executor] Output written to %s", path)


def _deliver_memory(title: str, output: str, summary: str,
                    goal_id: str, daemon) -> None:
    """Save the output to Quinely's searchable memory."""
    mem_tool = daemon.tool_registry.get("memory_save") if daemon.tool_registry else None
    if mem_tool and callable(mem_tool.get("execute")):
        mem_tool["execute"](
            content=f"Goal output: {title}\n\n{output[:2000]}",
            tags=f"goal,{goal_id}",
        )


# ═══════════════════════════════════════════════════════════════════
#  SELF-IMPROVEMENT REFLECTION — Quinely learns from every goal cycle
# ═══════════════════════════════════════════════════════════════════

_REFLECTION_RATE_FILE = Path.home() / ".ghost" / "goal_reflection_log.json"
_REFLECTION_COOLDOWN_HOURS = 8

_REFLECT_SYSTEM = """\
You are Quinely's self-improvement engine. You have just completed a goal execution
cycle and must analyze the execution data for concrete improvements to Quinely's
codebase. You are looking at real execution metrics — timing, failures, retries,
and QA outcomes.

Your job: identify specific, actionable improvements and submit each one via
add_future_feature. Focus on things that will make Quinely BETTER at running
goals — not the goal's content itself.

WHAT TO LOOK FOR:
1. Steps that took >30s — can they be parallelized, cached, or use a faster tool?
2. Steps that FAILED — what broke? Is there a missing error handler? A tool bug?
3. Steps that were RETRIED — intermittent failures suggest fragile tool integration.
4. QA rejections — the output format or quality was wrong. What prompt or tool change fixes this?
5. Missing tools — did Quinely have to work around a capability it should have natively?
6. Repeated patterns across recurring goals — these compound and deserve optimization.

CRITICAL — DISTINGUISH BUGS FROM ENVIRONMENT:
Not every failure is a code bug. Before submitting anything, evaluate the ROOT CAUSE:
- Network timeouts, DNS failures, rate limits, API outages → these are TRANSIENT. Do NOT submit features for them.
- Slow steps caused by large data or external API latency → NOT a Quinely bug. Skip.
- Authentication errors from expired tokens → NOT a code bug unless Quinely should auto-refresh.
- Failures that happen once but not repeatedly → likely transient. Skip.
Only submit a feature if the problem is CLEARLY in Quinely's own code, logic, prompts, or architecture.
When in doubt, do NOT submit. False negatives are far better than spam.

DEDUPLICATION:
Before calling add_future_feature, think about whether a similar improvement was likely
already submitted (e.g. from a previous run of this same recurring goal). The system has
built-in duplicate detection, but you should still avoid submitting near-duplicates with
slightly different wording. If the improvement is generic enough that it was probably
already proposed, skip it.

RULES:
- Be SPECIFIC: name the exact file, function, or tool that needs changing.
- Use category "bugfix" for failures, "improvement" for performance, "feature" for missing capabilities.
- Set priority P2 for optimizations, P1 for things that caused actual failures.
- If execution was clean and fast (<30s total, no failures, QA passed), say "No improvements needed" and do NOT submit any features.
- If failures look transient (network, rate-limit, timeout) say "Transient issues — no code changes needed" and do NOT submit.
- Maximum 3 features per reflection — focus on the highest-impact items.
- NEVER submit vague features like "improve error handling" or "make things faster".
- NEVER submit features about external services being slow or unavailable.
"""


def _should_reflect(goal_id: str) -> bool:
    """Rate-limit: at most one reflection per goal every _REFLECTION_COOLDOWN_HOURS."""
    try:
        if _REFLECTION_RATE_FILE.exists():
            log_data = json.loads(_REFLECTION_RATE_FILE.read_text(encoding="utf-8"))
        else:
            log_data = {}
        last = log_data.get(goal_id)
        if last:
            last_dt = datetime.fromisoformat(last)
            if datetime.now() - last_dt < timedelta(hours=_REFLECTION_COOLDOWN_HOURS):
                return False
    except Exception:
        pass
    return True


def _mark_reflected(goal_id: str) -> None:
    """Record that we reflected on this goal."""
    try:
        log_data = {}
        if _REFLECTION_RATE_FILE.exists():
            log_data = json.loads(_REFLECTION_RATE_FILE.read_text(encoding="utf-8"))
        log_data[goal_id] = datetime.now().isoformat()
        cutoff = datetime.now() - timedelta(days=7)
        log_data = {
            k: v for k, v in log_data.items()
            if datetime.fromisoformat(v) > cutoff
        }
        _REFLECTION_RATE_FILE.parent.mkdir(parents=True, exist_ok=True)
        _REFLECTION_RATE_FILE.write_text(json.dumps(log_data, indent=2), encoding="utf-8")
    except Exception as exc:
        log.warning("[goal_reflection] Failed to write rate-limit log: %s", exc)


def _build_reflection_prompt(r: Dict) -> str:
    """Build the analysis prompt from execution result data."""
    lines = [
        f"## Goal Execution Report",
        f"**Goal**: {r.get('title', 'Untitled')}",
        f"**Description**: {r.get('goal_text', 'N/A')}",
        f"**Total time**: {r.get('total_elapsed_ms', 0)}ms",
        f"**Completed**: {r.get('completed', False)}",
        f"**QA passed**: {r.get('qa_passed', 'N/A')}",
        f"**Recurring**: {r.get('recurrence') or 'one-shot'}",
        "",
        "### Steps Executed:",
    ]

    for s in r.get("steps_run", []):
        status = "OK" if s.get("ok") else "FAILED"
        lines.append(f"  - [{status}] {s.get('description', '?')} — {s.get('elapsed_ms', 0)}ms")

    failed = r.get("failed_step_ids", [])
    if failed:
        lines.append(f"\n**Permanently failed steps**: {', '.join(failed)}")

    retried = r.get("retried_step_ids", [])
    if retried:
        lines.append(f"**Steps that needed retries**: {', '.join(retried)}")

    # Include actual error messages so the LLM can distinguish transient vs structural
    step_errors = r.get("step_errors", {})
    if step_errors:
        lines.append("\n### Error Details:")
        for sid, err in step_errors.items():
            lines.append(f"  - Step {sid}: {str(err)[:300]}")

    observations = r.get("observations", [])
    if observations:
        lines.append("\n### Observations (from execution):")
        for obs in observations:
            text = obs.get("text") if isinstance(obs, dict) else str(obs)
            lines.append(f"  - {text}")

    lines.append(
        "\n---\nAnalyze the above. First decide: are the issues TRANSIENT (network, "
        "rate-limits, external API) or STRUCTURAL (Quinely code bug, missing tool, bad prompt)? "
        "Only submit improvements for structural issues via add_future_feature. "
        "If transient or clean, state why and do NOT submit any features."
    )
    return "\n".join(lines)


def reflect_on_goal_execution(results: List[Dict], daemon) -> int:
    """Analyze completed goal executions and submit self-improvement features.

    Returns the number of features submitted.
    """
    from ghost_future_features import FutureFeaturesStore, SOURCE_GOAL_REFLECTION

    qualifying = []
    for r in results:
        if r.get("error") or r.get("skipped"):
            continue

        goal_id = r.get("goal_id", "")
        if not goal_id:
            continue

        if not _should_reflect(goal_id):
            log.debug("[goal_reflection] Skipping [%s] — reflected recently", goal_id)
            continue

        has_failures = bool(r.get("failed_step_ids"))
        has_retries = bool(r.get("retried_step_ids"))
        qa_failed = r.get("qa_passed") is False
        is_recurring = bool(r.get("recurrence"))
        slow_steps = any(
            s.get("elapsed_ms", 0) > 30000
            for s in r.get("steps_run", [])
        )

        if has_failures or has_retries or qa_failed or is_recurring or slow_steps:
            qualifying.append(r)

    if not qualifying:
        log.info("[goal_reflection] No goals qualify for reflection this cycle.")
        return 0

    features_store = FutureFeaturesStore()
    total_submitted = 0

    cfg = daemon.cfg if daemon else {}
    auth_store = getattr(daemon, "auth_store", None)
    provider_chain = getattr(daemon, "provider_chain", None)

    api_key = None
    if auth_store:
        try:
            api_key = auth_store.get_api_key("openrouter")
        except Exception:
            pass
    if not api_key:
        api_key = os.environ.get("OPENROUTER_API_KEY", "")
    if not api_key:
        log.warning("[goal_reflection] No API key — skipping reflection")
        return 0

    for r in qualifying[:3]:
        goal_id = r.get("goal_id", "?")
        try:
            prompt = _build_reflection_prompt(r)
            log.info("[goal_reflection] Reflecting on goal [%s]: %s", goal_id, r.get("title", ""))

            ff_tool = _build_reflection_ff_tool(features_store, goal_id)

            from ghost_loop import ToolLoopEngine, ToolRegistry
            registry = ToolRegistry()
            registry.register(ff_tool)

            engine = ToolLoopEngine(
                api_key=api_key,
                model=cfg.get("model", "anthropic/claude-sonnet-4"),
                fallback_models=cfg.get("fallback_models", []),
                auth_store=auth_store,
                provider_chain=provider_chain,
            )

            engine.run(
                system_prompt=_REFLECT_SYSTEM,
                user_message=prompt,
                tool_registry=registry,
                max_steps=10,
                temperature=0.3,
                max_tokens=2048,
            )

            submitted = ff_tool["_submitted_count"]["count"]
            total_submitted += submitted
            _mark_reflected(goal_id)

            log.info("[goal_reflection] Goal [%s] reflection done — %d feature(s) submitted",
                     goal_id, submitted)

        except Exception as exc:
            log.error("[goal_reflection] Failed to reflect on goal [%s]: %s",
                      goal_id, exc, exc_info=True)

    return total_submitted


def _build_reflection_ff_tool(store, goal_id: str) -> Dict:
    """Build a lightweight add_future_feature tool for the reflection session."""
    from ghost_future_features import SOURCE_GOAL_REFLECTION

    state = {"count": 0}

    def add_future_feature(title: str, description: str, priority: str = "P2",
                           category: str = "improvement", affected_files: str = "",
                           proposed_approach: str = "", **kwargs) -> Dict:
        result = store.add(
            title=title,
            description=description,
            priority=priority,
            source=SOURCE_GOAL_REFLECTION,
            source_detail=f"goal:{goal_id}",
            category=category,
            affected_files=affected_files,
            proposed_approach=proposed_approach,
            tags=["goal_reflection", f"goal:{goal_id}"],
            auto_implement=True,
        )
        if not result.get("_warning"):
            state["count"] += 1
        return result

    tool = {
        "name": "add_future_feature",
        "description": (
            "Submit a specific improvement to Quinely's codebase based on goal execution analysis. "
            "Be specific: name exact files, functions, or tools. "
            "Categories: bugfix, improvement, feature, refactor. "
            "Priorities: P1 (caused failures), P2 (optimization)."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "title": {"type": "string", "description": "Short title for the improvement"},
                "description": {"type": "string", "description": "Detailed description of what to change and why"},
                "priority": {"type": "string", "enum": ["P1", "P2", "P3"], "description": "P1=failure fix, P2=optimization, P3=nice-to-have"},
                "category": {"type": "string", "enum": ["bugfix", "improvement", "feature", "refactor"]},
                "affected_files": {"type": "string", "description": "Comma-separated file paths that need changing"},
                "proposed_approach": {"type": "string", "description": "Concrete implementation approach"},
            },
            "required": ["title", "description"],
        },
        "execute": add_future_feature,
        "_submitted_count": state,
    }
    tool["_submitted_count"] = state
    return tool


# ═══════════════════════════════════════════════════════════════════
#  LLM-CALLABLE TOOL (for manual / chat invocation)
# ═══════════════════════════════════════════════════════════════════

def build_goal_executor_tool(cfg: Dict, tool_registry, auth_store=None,
                              provider_chain=None) -> List[Dict]:
    """Build the run_goal_engine tool for cron/chat invocation."""

    executor = GoalExecutorEngine(
        cfg=cfg,
        tool_registry=tool_registry,
        auth_store=auth_store,
        provider_chain=provider_chain,
    )

    def run_goal_engine(goal_id: str = "", **kwargs):
        """
        Run the deterministic Goal Executor Engine.

        Processes all actionable goals (or a specific goal if goal_id is given):
          - Recovers failed steps that are under the retry cap
          - Plans goals that have no plan yet
          - Executes ALL pending steps back-to-back in one session
          - Verifies each step was actually marked done (retries if not)
          - Runs a quality check on the output before completing

        Use this instead of manually managing goal steps. It finishes the entire
        goal in one invocation rather than one step per cron fire.

        Args:
            goal_id: Optional — run only this goal. Leave empty to run all.
        """
        if goal_id:
            goal = executor.store.get(goal_id)
            if not goal:
                return {"error": f"Goal not found: {goal_id}"}
            result = executor._process_goal(goal)
            return result
        return executor.run_all()

    return [
        {
            "name": "run_goal_engine",
            "description": (
                "Run the deterministic Goal Executor — plans and executes ALL pending "
                "steps of all active goals in one shot. Recovers failed steps, verifies "
                "each step was completed, runs a quality check on output. "
                "Use this to trigger goal execution immediately without waiting for the cron."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "goal_id": {
                        "type": "string",
                        "description": "Run only this specific goal (leave empty for all goals).",
                    },
                },
            },
            "execute": run_goal_engine,
        }
    ]
