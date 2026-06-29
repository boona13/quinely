"""
Ghost Goal Engine — Persistent Cognitive Architecture for arbitrary user goals.

Brings the same Planner + Working Memory + Executor pattern that Ghost uses for
self-evolution to any long-horizon user goal.

Architecture (mirrors future_features + feature_implementer):
  - GoalStore        ≈ FutureFeaturesStore  (persistent working memory)
  - goal_executor    ≈ feature_implementer  (cron-driven executor)
  - goal_create      ≈ add_future_feature   (user/agent creates work items)

A goal has three phases:
  1. pending_plan  — created but not yet decomposed into steps
  2. active        — plan exists, executor runs one step per cron fire
  3. completed     — all steps done (recurring goals reset automatically)

Usage:
  User: "Every Monday, research competitors A and B and email me a summary"
  Ghost: goal_create(title=..., goal_text=..., recurrence="0 9 * * 1")
  [goal_executor cron fires every 30 min]
    → goal_plan(goal_id, steps=[...])
    → goal_step_done / goal_step_fail per step
    → goal_complete(summary)   → resets steps for next cycle if recurring
"""

import json
import logging
import os
import tempfile
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

log = logging.getLogger("quinely.goals")

GHOST_HOME = Path.home() / ".ghost"
GOALS_FILE = GHOST_HOME / "goals.json"
GOALS_BACKUP_DIR = GHOST_HOME / "goals_backups"

# Goal statuses
STATUS_PENDING_PLAN = "pending_plan"
STATUS_ACTIVE = "active"
STATUS_PAUSED = "paused"
STATUS_COMPLETED = "completed"
STATUS_ABANDONED = "abandoned"

GOAL_STATUSES = [
    STATUS_PENDING_PLAN,
    STATUS_ACTIVE,
    STATUS_PAUSED,
    STATUS_COMPLETED,
    STATUS_ABANDONED,
]

# Step statuses
STEP_PENDING = "pending"
STEP_RUNNING = "running"
STEP_COMPLETED = "completed"
STEP_FAILED = "failed"
STEP_SKIPPED = "skipped"


# ═══════════════════════════════════════════════════════════════════
#  GOAL STORE
# ═══════════════════════════════════════════════════════════════════

class GoalStore:
    """CRUD and lifecycle management for user goals."""

    def __init__(self):
        GHOST_HOME.mkdir(parents=True, exist_ok=True)

    # ------------------------------------------------------------------
    # Persistence
    # ------------------------------------------------------------------

    def _load(self) -> List[Dict]:
        if GOALS_FILE.exists():
            try:
                return json.loads(GOALS_FILE.read_text(encoding="utf-8"))
            except Exception as exc:
                log.warning("Failed to load goals.json: %s", exc)
        return []

    def _save(self, goals: List[Dict]) -> None:
        GHOST_HOME.mkdir(parents=True, exist_ok=True)
        fd, tmp = tempfile.mkstemp(dir=GHOST_HOME, suffix=".goals.tmp")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as fh:
                json.dump(goals, fh, indent=2, default=str)
            os.replace(tmp, str(GOALS_FILE))
        except Exception:
            try:
                os.unlink(tmp)
            except OSError:
                pass
            raise

    def _backup(self) -> None:
        if not GOALS_FILE.exists():
            return
        GOALS_BACKUP_DIR.mkdir(parents=True, exist_ok=True)
        ts = datetime.now().strftime("%Y%m%d_%H%M%S")
        import shutil
        backup = GOALS_BACKUP_DIR / f"goals_{ts}.json"
        shutil.copy2(GOALS_FILE, backup)
        # Keep last 20 backups
        backups = sorted(GOALS_BACKUP_DIR.glob("goals_*.json"),
                         key=lambda p: p.stat().st_mtime)
        while len(backups) > 20:
            backups.pop(0).unlink()

    # ------------------------------------------------------------------
    # CRUD
    # ------------------------------------------------------------------

    @staticmethod
    def _validate_cron(expr: str) -> bool:
        """Return True if ``expr`` is a valid 5-field cron expression."""
        if not expr or not expr.strip():
            return False
        try:
            from croniter import croniter
            croniter(expr.strip())
            return True
        except Exception:
            return False

    def add(
        self,
        title: str,
        goal_text: str,
        recurrence: Optional[str] = None,
        context: Optional[Dict] = None,
    ) -> Dict:
        """Create a new goal in pending_plan state."""
        goals = self._load()

        # Normalize recurrence: empty string → None
        if isinstance(recurrence, str):
            recurrence = recurrence.strip() or None

        # Validate cron expression if provided
        if recurrence and not self._validate_cron(recurrence):
            return {"_error": f"Invalid cron expression: '{recurrence}'. "
                    "Expected 5-field format like '0 9 * * 1' (every Monday 9am)."}

        # Lightweight duplicate check on title
        title_lower = title.lower().strip()
        for g in goals:
            if (g.get("title", "").lower().strip() == title_lower
                    and g.get("status") in (STATUS_PENDING_PLAN, STATUS_ACTIVE, STATUS_PAUSED)):
                return {**g, "_warning": "A goal with this title is already active."}

        goal: Dict[str, Any] = {
            "id": uuid.uuid4().hex[:10],
            "title": title,
            "goal_text": goal_text,
            "status": STATUS_PENDING_PLAN,
            "plan": [],
            "current_step": 0,
            "observations": [],
            "scratch": {},
            "context": context or {},
            "recurrence": recurrence,
            "delivery": context.get("delivery", "") if context else "",
            "created_at": datetime.now().isoformat(),
            "updated_at": datetime.now().isoformat(),
            "last_executed_at": None,
            "completed_at": None,
            "completion_count": 0,
            "last_summary": None,
            "last_output": None,
            "output_history": [],
        }
        goals.insert(0, goal)
        self._save(goals)
        log.info("Goal created: [%s] %s", goal["id"], title)
        return goal

    def get(self, goal_id: str) -> Optional[Dict]:
        """Get a goal by ID."""
        for g in self._load():
            if g["id"] == goal_id:
                return g
        return None

    def list_goals(
        self,
        status: Optional[str] = None,
        limit: int = 50,
    ) -> List[Dict]:
        """List goals, optionally filtered by status."""
        goals = self._load()
        if status:
            goals = [g for g in goals if g.get("status") == status]
        return goals[:limit]

    def list_actionable(self) -> List[Dict]:
        """Return goals that the executor should work on now.

        For recurring goals that already completed a cycle (all steps were
        reset to pending by complete_goal), skip them until their next
        scheduled run time.  But if any step is non-pending (in-progress,
        completed, failed), the goal is mid-execution and must stay
        actionable — even if ``last_executed_at`` was recently set.
        """
        goals = self._load()
        now = datetime.now()
        result = []
        for g in goals:
            if g.get("status") not in (STATUS_PENDING_PLAN, STATUS_ACTIVE):
                continue
            if g.get("recurrence") and g.get("last_executed_at"):
                # Only gate on schedule if ALL steps are pending (= cycle reset).
                # If any step is non-pending, we're mid-execution and must continue.
                all_pending = all(
                    s.get("status") == STEP_PENDING
                    for s in g.get("plan", [])
                )
                if all_pending and not self._is_due(g, now):
                    continue
            result.append(g)
        return result

    @staticmethod
    def _is_due(goal: Dict, now: datetime) -> bool:
        """Check if a recurring goal is due based on its cron expression.

        Returns True if the next scheduled run (computed from last_executed_at)
        is at or before ``now``.
        """
        recurrence = goal.get("recurrence", "")
        last_run_str = goal.get("last_executed_at", "")
        if not recurrence or not last_run_str:
            return True

        try:
            last_run = datetime.fromisoformat(last_run_str)
        except (ValueError, TypeError):
            return True

        try:
            from croniter import croniter
            cron = croniter(recurrence, last_run)
            next_run = cron.get_next(datetime)
            return now >= next_run
        except Exception:
            pass

        # Fallback: if croniter unavailable, use a simple 6-hour minimum gap
        # so the goal doesn't re-run every 30 minutes.
        elapsed = (now - last_run).total_seconds()
        return elapsed >= 6 * 3600

    def _update(self, goal_id: str, updates: Dict) -> Optional[Dict]:
        """Apply a dict of updates to a goal and persist."""
        goals = self._load()
        for g in goals:
            if g["id"] == goal_id:
                g.update(updates)
                g["updated_at"] = datetime.now().isoformat()
                self._save(goals)
                return g
        return None

    # ------------------------------------------------------------------
    # Plan management
    # ------------------------------------------------------------------

    def set_plan(self, goal_id: str, steps: List[Dict]) -> Optional[Dict]:
        """Store the plan and advance goal to active status."""
        normalized = []
        for i, step in enumerate(steps):
            if isinstance(step, str):
                step = {"description": step}
            normalized.append({
                "id": step.get("id") or f"s{i+1}",
                "description": step.get("description", f"Step {i+1}"),
                "status": STEP_PENDING,
                "result": None,
                "error": None,
                "retry_count": 0,
                "started_at": None,
                "completed_at": None,
            })

        return self._update(goal_id, {
            "plan": normalized,
            "current_step": 0,
            "status": STATUS_ACTIVE,
        })

    # ------------------------------------------------------------------
    # Step lifecycle
    # ------------------------------------------------------------------

    def _find_step(self, plan: List[Dict], step_id: str) -> Optional[int]:
        """Return the index of a step by ID, or None."""
        for i, s in enumerate(plan):
            if s["id"] == step_id:
                return i
        return None

    def mark_step_done(self, goal_id: str, step_id: str, result: str = "") -> Optional[Dict]:
        """Mark a step as completed and advance current_step pointer."""
        goals = self._load()
        for g in goals:
            if g["id"] != goal_id:
                continue
            idx = self._find_step(g.get("plan", []), step_id)
            if idx is None:
                return None
            g["plan"][idx]["status"] = STEP_COMPLETED
            g["plan"][idx]["result"] = result
            g["plan"][idx]["completed_at"] = datetime.now().isoformat()
            # Advance pointer to next pending step
            g["current_step"] = idx + 1
            g["last_executed_at"] = datetime.now().isoformat()
            g["updated_at"] = datetime.now().isoformat()
            self._save(goals)
            return g
        return None

    def mark_step_failed(self, goal_id: str, step_id: str, error: str = "") -> Optional[Dict]:
        """Mark a step as failed and increment its retry counter."""
        goals = self._load()
        for g in goals:
            if g["id"] != goal_id:
                continue
            idx = self._find_step(g.get("plan", []), step_id)
            if idx is None:
                return None
            g["plan"][idx]["status"] = STEP_FAILED
            g["plan"][idx]["error"] = error
            g["plan"][idx]["retry_count"] = g["plan"][idx].get("retry_count", 0) + 1
            g["plan"][idx]["completed_at"] = datetime.now().isoformat()
            g["last_executed_at"] = datetime.now().isoformat()
            g["updated_at"] = datetime.now().isoformat()
            self._save(goals)
            return g
        return None

    def add_observation(self, goal_id: str, observation: str) -> Optional[Dict]:
        """Append a finding to the goal's working memory."""
        goals = self._load()
        for g in goals:
            if g["id"] != goal_id:
                continue
            obs = g.setdefault("observations", [])
            obs.append({
                "text": observation,
                "at": datetime.now().isoformat(),
            })
            # Cap observations at 50 entries to prevent unbounded growth
            if len(obs) > 50:
                g["observations"] = obs[-50:]
            g["updated_at"] = datetime.now().isoformat()
            self._save(goals)
            return g
        return None

    # ------------------------------------------------------------------
    # Scratch space (inter-step structured data)
    # ------------------------------------------------------------------

    def set_scratch(self, goal_id: str, key: str, value: Any) -> Optional[Dict]:
        """Store a key-value pair in the goal's scratch space.

        Scratch survives across steps within a cycle, allowing steps to
        pass structured data (URLs, lists, intermediate results) forward.
        Scratch is cleared when a recurring goal resets for the next cycle.
        """
        goals = self._load()
        for g in goals:
            if g["id"] != goal_id:
                continue
            scratch = g.setdefault("scratch", {})
            scratch[key] = value
            g["updated_at"] = datetime.now().isoformat()
            self._save(goals)
            return g
        return None

    def get_scratch(self, goal_id: str) -> Dict:
        """Return the full scratch dict for a goal."""
        g = self.get(goal_id)
        if not g:
            return {}
        return g.get("scratch", {})

    # ------------------------------------------------------------------
    # Failed step recovery
    # ------------------------------------------------------------------

    def retry_failed_steps(self, goal_id: str, max_retries: int = 3) -> int:
        """Reset STEP_FAILED steps back to STEP_PENDING if under retry cap.

        Returns the number of steps reset.
        """
        goals = self._load()
        reset_count = 0
        for g in goals:
            if g["id"] != goal_id:
                continue
            for step in g.get("plan", []):
                if step["status"] == STEP_FAILED:
                    retries = step.get("retry_count", 0)
                    if retries < max_retries:
                        step["status"] = STEP_PENDING
                        step["error"] = None
                        step["completed_at"] = None
                        reset_count += 1
                    else:
                        log.info("Step [%s/%s] exceeded max retries (%d), staying failed",
                                 goal_id, step["id"], max_retries)
            if reset_count > 0:
                g["updated_at"] = datetime.now().isoformat()
                self._save(goals)
            return reset_count
        return 0

    def complete_goal(self, goal_id: str, summary: str = "") -> Optional[Dict]:
        """Mark a goal as completed.

        If the goal has a recurrence expression, snapshot the completed plan
        into last_completed_plan (so the user can see what ran), then reset
        all steps to pending so the executor picks it up again next cycle.
        """
        goals = self._load()
        for g in goals:
            if g["id"] != goal_id:
                continue
            g["last_summary"] = summary
            g["last_executed_at"] = datetime.now().isoformat()
            g["completion_count"] = g.get("completion_count", 0) + 1

            if g.get("recurrence"):
                # Snapshot the completed plan before resetting so user can
                # see which steps ran and what each produced.
                import copy
                g["last_completed_plan"] = {
                    "steps": copy.deepcopy(g.get("plan", [])),
                    "completed_at": datetime.now().isoformat(),
                    "run": g["completion_count"],
                }
                # Reset steps and scratch for the next cycle
                for step in g.get("plan", []):
                    step["status"] = STEP_PENDING
                    step["result"] = None
                    step["error"] = None
                    step["retry_count"] = 0
                    step["started_at"] = None
                    step["completed_at"] = None
                g["current_step"] = 0
                g["scratch"] = {}
                g["status"] = STATUS_ACTIVE
                g["completed_at"] = None
            else:
                g["status"] = STATUS_COMPLETED
                g["completed_at"] = datetime.now().isoformat()

            g["updated_at"] = datetime.now().isoformat()
            self._save(goals)
            log.info("Goal completed: [%s] %s (recurrence=%s)",
                     goal_id, g.get("title"), g.get("recurrence"))
            return g
        return None

    def set_output(self, goal_id: str, output: str) -> Optional[Dict]:
        """Store the full deliverable for a completed cycle.

        Saves to last_output (always current) and appends to output_history
        (keeps last 10 runs so the user can look back).
        """
        goals = self._load()
        for g in goals:
            if g["id"] != goal_id:
                continue
            g["last_output"] = output
            history = g.get("output_history") or []
            # completion_count hasn't been incremented yet (that happens in
            # complete_goal), so +1 to match the run number that
            # complete_goal / last_completed_plan will record.
            history.append({
                "output": output,
                "at": datetime.now().isoformat(),
                "run": g.get("completion_count", 0) + 1,
            })
            # Keep last 10 runs
            if len(history) > 10:
                history = history[-10:]
            g["output_history"] = history
            g["updated_at"] = datetime.now().isoformat()
            self._save(goals)
            return g
        return None

    # ------------------------------------------------------------------
    # Simple state transitions
    # ------------------------------------------------------------------

    def pause_goal(self, goal_id: str) -> Optional[Dict]:
        g = self.get(goal_id)
        if not g:
            return None
        if g["status"] not in (STATUS_ACTIVE, STATUS_PENDING_PLAN):
            return None
        return self._update(goal_id, {"status": STATUS_PAUSED})

    def resume_goal(self, goal_id: str) -> Optional[Dict]:
        g = self.get(goal_id)
        if not g:
            return None
        if g["status"] != STATUS_PAUSED:
            return None
        new_status = STATUS_ACTIVE if g.get("plan") else STATUS_PENDING_PLAN
        return self._update(goal_id, {"status": new_status})

    def abandon_goal(self, goal_id: str) -> Optional[Dict]:
        g = self.get(goal_id)
        if not g:
            return None
        return self._update(goal_id, {"status": STATUS_ABANDONED})

    def delete_goal(self, goal_id: str) -> bool:
        """Permanently remove a goal from storage. Creates a backup first."""
        goals = self._load()
        before = len(goals)
        goals = [g for g in goals if g["id"] != goal_id]
        if len(goals) == before:
            return False
        self._backup()
        self._save(goals)
        log.info("Goal deleted: [%s]", goal_id)
        return True

    # ------------------------------------------------------------------
    # Helpers for the executor
    # ------------------------------------------------------------------

    def next_pending_step(self, goal: Dict) -> Optional[Dict]:
        """Return the first step that is still pending, or None if all done."""
        for step in goal.get("plan", []):
            if step["status"] == STEP_PENDING:
                return step
        return None

    def all_steps_done(self, goal: Dict) -> bool:
        """True when every step in the plan has been completed or skipped."""
        plan = goal.get("plan", [])
        if not plan:
            return False
        return all(s["status"] in (STEP_COMPLETED, STEP_SKIPPED) for s in plan)

    def format_for_executor(self, goal: Dict) -> str:
        """Render a goal as a compact context block for the executor prompt."""
        lines = [
            f"## ACTIVE GOAL: {goal['title']}",
            f"ID: {goal['id']}",
            f"Goal: {goal['goal_text']}",
            f"Status: {goal['status']}",
        ]
        if goal.get("recurrence"):
            lines.append(f"Recurrence: {goal['recurrence']}")

        plan = goal.get("plan", [])
        if plan:
            lines.append("\n### Plan:")
            for step in plan:
                icon = {"pending": "⬜", "running": "🔄", "completed": "✅",
                        "failed": "❌", "skipped": "⏭️"}.get(step["status"], "⬜")
                lines.append(f"  {icon} [{step['id']}] {step['description']}")
                if step.get("result") and step["status"] == STEP_COMPLETED:
                    lines.append(f"     Result: {str(step['result'])[:200]}")
                if step.get("error") and step["status"] == STEP_FAILED:
                    lines.append(f"     Error: {str(step['error'])[:200]}")

        observations = goal.get("observations", [])
        if observations:
            lines.append("\n### Observations (working memory):")
            for obs in observations[-10:]:
                text = obs.get("text") if isinstance(obs, dict) else str(obs)
                lines.append(f"  - {text}")

        return "\n".join(lines)


# ═══════════════════════════════════════════════════════════════════
#  LLM-CALLABLE TOOLS
# ═══════════════════════════════════════════════════════════════════

def build_goal_tools(store: Optional[GoalStore] = None) -> List[Dict]:
    """Build the LLM-callable goal management tools."""
    if store is None:
        store = GoalStore()

    # ------------------------------------------------------------------
    # goal_create
    # ------------------------------------------------------------------
    def goal_create(title: str, goal_text: str,
                    recurrence: str = "", context: dict = None, **kwargs):
        """
        Create a new persistent goal with optional recurrence.

        The goal starts in 'pending_plan' state. The goal_executor cron will
        decompose it into steps automatically, or you can call goal_plan() now.

        Args:
            title: Short label for the goal.
            goal_text: Full description of what needs to be achieved.
            recurrence: Cron expression for recurring goals (e.g. "0 9 * * 1"
                        for every Monday at 9am). Omit for one-shot goals.
            context: Optional dict of extra metadata (notify_email, etc.).
        """
        result = store.add(
            title=title.strip(),
            goal_text=goal_text.strip(),
            recurrence=recurrence or None,
            context=context or {},
        )
        if result.get("_error"):
            return {"error": result["_error"]}
        if result.get("_warning"):
            return result
        return {
            "ok": True,
            "goal_id": result["id"],
            "status": result["status"],
            "message": (
                f"Goal created (id={result['id']}). "
                "The goal_executor cron will plan and execute it autonomously. "
                "You can also call goal_plan() now to set the plan immediately."
            ),
        }

    # ------------------------------------------------------------------
    # goal_plan
    # ------------------------------------------------------------------
    def goal_plan(goal_id: str, steps: list, **kwargs):
        """
        Store a step-by-step plan for a goal and mark it as active.

        Each step is a string description or a dict with 'id' and 'description'.
        The executor will run one step per cron fire.

        Args:
            goal_id: The goal ID returned by goal_create.
            steps: List of step descriptions (3-6 steps recommended).
        """
        if not steps:
            return {"error": "steps list cannot be empty."}
        result = store.set_plan(goal_id, steps)
        if not result:
            return {"error": f"Goal not found: {goal_id}"}
        step_ids = [s["id"] for s in result["plan"]]
        return {
            "ok": True,
            "goal_id": goal_id,
            "status": result["status"],
            "steps": step_ids,
            "message": f"Plan saved with {len(steps)} steps. Executor will run step-by-step.",
        }

    # ------------------------------------------------------------------
    # goal_step_done
    # ------------------------------------------------------------------
    def goal_step_done(goal_id: str, step_id: str, result: str = "", **kwargs):
        """
        Mark a step as completed and record its result.

        Call this after successfully executing a step. The executor pointer
        advances to the next pending step.

        Args:
            goal_id: The goal ID.
            step_id: The step ID (e.g. "s1", "s2").
            result: Summary of what was accomplished (stored as working memory).
        """
        updated = store.mark_step_done(goal_id, step_id, result=result)
        if not updated:
            return {"error": f"Goal or step not found: {goal_id}/{step_id}"}
        next_step = store.next_pending_step(updated)
        all_done = store.all_steps_done(updated)
        return {
            "ok": True,
            "goal_id": goal_id,
            "step_id": step_id,
            "all_steps_done": all_done,
            "next_step": next_step["id"] if next_step else None,
            "message": (
                "All steps complete — call goal_complete() to finish."
                if all_done else
                f"Step done. Next: [{next_step['id']}] {next_step['description']}"
            ),
        }

    # ------------------------------------------------------------------
    # goal_step_fail
    # ------------------------------------------------------------------
    def goal_step_fail(goal_id: str, step_id: str, error: str = "", **kwargs):
        """
        Mark a step as failed and record the error.

        The goal remains active. Add an observation with the root cause, then
        decide whether to retry the step or skip to the next one.

        Args:
            goal_id: The goal ID.
            step_id: The step ID.
            error: Description of what went wrong.
        """
        updated = store.mark_step_failed(goal_id, step_id, error=error)
        if not updated:
            return {"error": f"Goal or step not found: {goal_id}/{step_id}"}
        return {
            "ok": True,
            "goal_id": goal_id,
            "step_id": step_id,
            "message": f"Step marked failed. Use goal_add_observation() to log the cause.",
        }

    # ------------------------------------------------------------------
    # goal_add_observation
    # ------------------------------------------------------------------
    def goal_add_observation(goal_id: str, observation: str, **kwargs):
        """
        Append a finding to the goal's working memory (observations list).

        Observations persist across executor runs, solving the ReAct loop's
        statelessness problem. Use for: key findings, discovered constraints,
        partial results, or anything a future step needs to know.

        Args:
            goal_id: The goal ID.
            observation: The finding to store (kept concise, under 500 chars).
        """
        updated = store.add_observation(goal_id, observation[:500])
        if not updated:
            return {"error": f"Goal not found: {goal_id}"}
        obs_count = len(updated.get("observations", []))
        return {
            "ok": True,
            "goal_id": goal_id,
            "observations_count": obs_count,
        }

    # ------------------------------------------------------------------
    # goal_complete
    # ------------------------------------------------------------------
    def goal_complete(goal_id: str, summary: str = "", **kwargs):
        """
        Mark a goal as completed.

        For recurring goals (those with a recurrence cron expression), this
        resets all steps to pending so the next cycle runs automatically.
        For one-shot goals, this marks the goal as permanently completed.

        Args:
            goal_id: The goal ID.
            summary: Brief summary of what was accomplished.
        """
        updated = store.complete_goal(goal_id, summary=summary)
        if not updated:
            return {"error": f"Goal not found: {goal_id}"}
        if updated.get("recurrence"):
            msg = (f"Goal cycle complete (run #{updated.get('completion_count', 1)}). "
                   f"Steps reset for next cycle (recurrence: {updated['recurrence']}).")
        else:
            msg = f"Goal completed. Summary: {summary[:200]}" if summary else "Goal completed."
        return {
            "ok": True,
            "goal_id": goal_id,
            "status": updated["status"],
            "completion_count": updated.get("completion_count", 1),
            "message": msg,
        }

    # ------------------------------------------------------------------
    # goal_list
    # ------------------------------------------------------------------
    def goal_list(status: str = "", limit: int = 20, **kwargs):
        """
        List goals, optionally filtered by status.

        Args:
            status: Filter by status. One of: pending_plan, active, paused,
                    completed, abandoned. Leave empty for all goals.
            limit: Max results to return (default 20).
        """
        goals = store.list_goals(status=status or None, limit=limit)
        summary = []
        for g in goals:
            plan = g.get("plan", [])
            done = sum(1 for s in plan if s["status"] in (STEP_COMPLETED, STEP_SKIPPED))
            total = len(plan)
            summary.append({
                "id": g["id"],
                "title": g["title"],
                "status": g["status"],
                "steps_done": done,
                "steps_total": total,
                "recurrence": g.get("recurrence"),
                "last_executed_at": g.get("last_executed_at"),
                "created_at": g.get("created_at"),
            })
        return {"goals": summary, "count": len(summary)}

    # ------------------------------------------------------------------
    # goal_get
    # ------------------------------------------------------------------
    def goal_get(goal_id: str, **kwargs):
        """
        Get the full details of a goal including its plan and observations.

        Args:
            goal_id: The goal ID.
        """
        g = store.get(goal_id)
        if not g:
            return {"error": f"Goal not found: {goal_id}"}
        return g

    # ------------------------------------------------------------------
    # goal_pause
    # ------------------------------------------------------------------
    def goal_pause(goal_id: str, **kwargs):
        """
        Pause an active goal. The executor will skip it until resumed.

        Args:
            goal_id: The goal ID.
        """
        updated = store.pause_goal(goal_id)
        if not updated:
            return {"error": f"Goal not found or cannot be paused: {goal_id}"}
        return {"ok": True, "goal_id": goal_id, "status": updated["status"]}

    # ------------------------------------------------------------------
    # goal_resume
    # ------------------------------------------------------------------
    def goal_resume(goal_id: str, **kwargs):
        """
        Resume a paused goal.

        Args:
            goal_id: The goal ID.
        """
        updated = store.resume_goal(goal_id)
        if not updated:
            return {"error": f"Goal not found or not paused: {goal_id}"}
        return {"ok": True, "goal_id": goal_id, "status": updated["status"]}

    # ------------------------------------------------------------------
    # goal_abandon
    # ------------------------------------------------------------------
    def goal_abandon(goal_id: str, **kwargs):
        """
        Permanently abandon a goal. It will no longer be executed.

        Args:
            goal_id: The goal ID.
        """
        updated = store.abandon_goal(goal_id)
        if not updated:
            return {"error": f"Goal not found: {goal_id}"}
        return {"ok": True, "goal_id": goal_id, "status": updated["status"]}

    # ------------------------------------------------------------------
    # goal_set_output
    # ------------------------------------------------------------------
    def goal_set_output(goal_id: str, output: str, **kwargs):
        """
        Store the full deliverable produced by this goal execution cycle.

        ALWAYS call this before goal_complete(). This is what the user sees
        in the Goals dashboard as the actual result of the goal run. It also
        appends to output_history so the user can review past runs.

        For example:
          - Weekly news digest → the full text of the 5 stories + summaries
          - Competitor report → the complete pricing comparison table
          - Code quality sprint → the list of files improved + what changed

        Args:
            goal_id: The goal ID.
            output: The full deliverable content. Can be markdown, plain text,
                    a JSON summary, etc. No length limit — include everything
                    the user would want to read.
        """
        updated = store.set_output(goal_id, output)
        if not updated:
            return {"error": f"Goal not found: {goal_id}"}
        return {
            "ok": True,
            "goal_id": goal_id,
            "output_length": len(output),
            "run_count": len(updated.get("output_history", [])),
            "message": "Output saved. User can now see it in the Goals dashboard. Call goal_complete() next.",
        }

    # ------------------------------------------------------------------
    # goal_set_scratch
    # ------------------------------------------------------------------
    def goal_set_scratch(goal_id: str, key: str, value: str, **kwargs):
        """
        Store structured data in the goal's scratch space for use by later steps.

        Scratch persists across steps within the same execution cycle. Use it to
        pass URLs, lists, intermediate results, or any data that a future step
        needs. Scratch is cleared when a recurring goal resets for the next cycle.

        Args:
            goal_id: The goal ID.
            key: A descriptive key (e.g. 'competitor_urls', 'research_findings').
            value: The data to store (string — use JSON for structured data).
        """
        updated = store.set_scratch(goal_id, key, value)
        if not updated:
            return {"error": f"Goal not found: {goal_id}"}
        return {
            "ok": True,
            "goal_id": goal_id,
            "key": key,
            "scratch_keys": list(updated.get("scratch", {}).keys()),
        }

    # ------------------------------------------------------------------
    # goal_set_delivery
    # ------------------------------------------------------------------
    def goal_set_delivery(goal_id: str, delivery: str, **kwargs):
        """
        Set how Ghost should deliver the goal output to the user after each run.

        Delivery options:
          - "notify"     — send a push notification with a summary
          - "discord"    — post the output to the Discord channel
          - "telegram"   — send via Telegram
          - "file:<path>"  — write output to a file, e.g. "file:~/digests/ai-news.md"
          - "memory"     — save to Ghost's searchable memory (default, always done)
          - "chat"       — post as a chat message in the dashboard feed
          - ""           — no extra delivery, output visible only in Goals dashboard

        Args:
            goal_id: The goal ID.
            delivery: Delivery method string (see options above).
        """
        updated = store._update(goal_id, {"delivery": delivery})
        if not updated:
            return {"error": f"Goal not found: {goal_id}"}
        return {
            "ok": True,
            "goal_id": goal_id,
            "delivery": delivery,
            "message": f"Delivery set to: {delivery}. The executor will use this after each cycle.",
        }

    # ------------------------------------------------------------------
    # Tool definitions
    # ------------------------------------------------------------------
    return [
        {
            "name": "goal_create",
            "description": (
                "Create a new persistent user goal with an optional recurrence schedule. "
                "Use this when the user asks Ghost to do something repeatedly or "
                "autonomously over time (e.g. 'every Monday research competitors', "
                "'daily check my email and summarize', 'monitor stock prices weekly'). "
                "The goal_executor cron will plan and execute it step-by-step."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "title": {
                        "type": "string",
                        "description": "Short label for the goal (e.g. 'Weekly competitor report').",
                    },
                    "goal_text": {
                        "type": "string",
                        "description": "Full description of what needs to be achieved.",
                    },
                    "recurrence": {
                        "type": "string",
                        "description": (
                            "Cron expression for recurring goals "
                            "(e.g. '0 9 * * 1' = every Monday at 9am). "
                            "Leave empty for one-shot goals."
                        ),
                    },
                    "context": {
                        "type": "object",
                        "description": "Optional metadata (notify_email, topic, etc.).",
                    },
                },
                "required": ["title", "goal_text"],
            },
            "execute": goal_create,
        },
        {
            "name": "goal_plan",
            "description": (
                "Store a step-by-step execution plan for a goal. "
                "Break the goal into 3-6 concrete, tool-actionable steps. "
                "The executor runs one step per cron fire."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "goal_id": {"type": "string", "description": "Goal ID from goal_create."},
                    "steps": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": "List of step descriptions in execution order.",
                    },
                },
                "required": ["goal_id", "steps"],
            },
            "execute": goal_plan,
        },
        {
            "name": "goal_step_done",
            "description": (
                "Mark a goal step as completed and record its result. "
                "Call this after successfully executing a step."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "goal_id": {"type": "string"},
                    "step_id": {"type": "string", "description": "Step ID (e.g. 's1', 's2')."},
                    "result": {"type": "string", "description": "Summary of what was accomplished."},
                },
                "required": ["goal_id", "step_id"],
            },
            "execute": goal_step_done,
        },
        {
            "name": "goal_step_fail",
            "description": (
                "Mark a goal step as failed and record the error. "
                "The goal remains active for the next executor run."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "goal_id": {"type": "string"},
                    "step_id": {"type": "string"},
                    "error": {"type": "string", "description": "Description of what went wrong."},
                },
                "required": ["goal_id", "step_id"],
            },
            "execute": goal_step_fail,
        },
        {
            "name": "goal_add_observation",
            "description": (
                "Append a finding to the goal's persistent working memory. "
                "Observations survive across executor runs, solving context loss. "
                "Use for: key findings, discovered constraints, partial results, "
                "anything a future step needs to know."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "goal_id": {"type": "string"},
                    "observation": {
                        "type": "string",
                        "description": "The finding to persist (keep under 500 chars).",
                    },
                },
                "required": ["goal_id", "observation"],
            },
            "execute": goal_add_observation,
        },
        {
            "name": "goal_complete",
            "description": (
                "Mark a goal as completed. For recurring goals, this resets all "
                "steps for the next cycle. For one-shot goals, this archives it."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "goal_id": {"type": "string"},
                    "summary": {
                        "type": "string",
                        "description": "Brief summary of what was accomplished.",
                    },
                },
                "required": ["goal_id"],
            },
            "execute": goal_complete,
        },
        {
            "name": "goal_list",
            "description": (
                "List user goals. Filter by status: pending_plan, active, paused, "
                "completed, or abandoned. Leave empty for all goals."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "status": {
                        "type": "string",
                        "description": "Filter by status (leave empty for all).",
                    },
                    "limit": {
                        "type": "integer",
                        "description": "Max results (default 20).",
                        "default": 20,
                    },
                },
            },
            "execute": goal_list,
        },
        {
            "name": "goal_get",
            "description": "Get full details of a goal including its plan and all observations.",
            "parameters": {
                "type": "object",
                "properties": {
                    "goal_id": {"type": "string", "description": "Goal ID."},
                },
                "required": ["goal_id"],
            },
            "execute": goal_get,
        },
        {
            "name": "goal_pause",
            "description": "Pause an active goal. The executor will skip it until resumed.",
            "parameters": {
                "type": "object",
                "properties": {
                    "goal_id": {"type": "string"},
                },
                "required": ["goal_id"],
            },
            "execute": goal_pause,
        },
        {
            "name": "goal_resume",
            "description": "Resume a paused goal.",
            "parameters": {
                "type": "object",
                "properties": {
                    "goal_id": {"type": "string"},
                },
                "required": ["goal_id"],
            },
            "execute": goal_resume,
        },
        {
            "name": "goal_abandon",
            "description": "Permanently abandon a goal. It will no longer be executed.",
            "parameters": {
                "type": "object",
                "properties": {
                    "goal_id": {"type": "string"},
                },
                "required": ["goal_id"],
            },
            "execute": goal_abandon,
        },
        {
            "name": "goal_set_output",
            "description": (
                "Store the full deliverable produced by this goal execution cycle. "
                "ALWAYS call this before goal_complete(). This is what the user sees "
                "in the Goals dashboard — the actual result of the run (the news digest, "
                "the report, the analysis, etc.). Output is also kept in output_history "
                "so the user can review all past runs."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "goal_id": {"type": "string"},
                    "output": {
                        "type": "string",
                        "description": (
                            "The full deliverable content — the actual result the user wants to see. "
                            "Include everything: all the news stories, the full report, the complete "
                            "analysis. Use markdown formatting. No length limit."
                        ),
                    },
                },
                "required": ["goal_id", "output"],
            },
            "execute": goal_set_output,
        },
        {
            "name": "goal_set_scratch",
            "description": (
                "Store structured data in a goal's scratch space for later steps. "
                "Use to pass URLs, lists, findings, or intermediate results between "
                "steps. Persists within the execution cycle. Cleared on cycle reset."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "goal_id": {"type": "string"},
                    "key": {
                        "type": "string",
                        "description": "Descriptive key (e.g. 'research_urls', 'price_data').",
                    },
                    "value": {
                        "type": "string",
                        "description": "Data to store. Use JSON string for structured data.",
                    },
                },
                "required": ["goal_id", "key", "value"],
            },
            "execute": goal_set_scratch,
        },
        {
            "name": "goal_set_delivery",
            "description": (
                "Set how Ghost delivers the goal output after each run. "
                "Options: 'notify' (push notification), 'discord', 'telegram', "
                "'file:<path>' (write to file), 'chat' (post in dashboard feed), "
                "'' (Goals dashboard only). Call when the user says how they want results delivered."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "goal_id": {"type": "string"},
                    "delivery": {
                        "type": "string",
                        "description": "Delivery method: notify, discord, telegram, file:<path>, chat, or empty.",
                    },
                },
                "required": ["goal_id", "delivery"],
            },
            "execute": goal_set_delivery,
        },
    ]
