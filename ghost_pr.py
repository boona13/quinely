"""
Ghost PR System — pull request management and adversarial code review.

Provides:
  - PRStore: CRUD for internal pull requests (stored in ~/.ghost/prs/)
  - ReviewEngine: multi-persona dialogue between Reviewer and Developer personas
  - build_pr_tools(): LLM-callable tools for the evolve pipeline

The review loop runs synchronously: Developer submits a PR, Reviewer examines
the diff, they discuss back and forth, and the Reviewer renders a verdict
(approve, request_changes, or block) — all within one session.
"""

import json
import logging
import traceback
import threading
import uuid
from datetime import datetime
from pathlib import Path
from typing import Optional, Dict, Any, List, Tuple

PROJECT_DIR = Path(__file__).resolve().parent
GHOST_HOME = Path.home() / ".ghost"
PR_DIR = GHOST_HOME / "prs"
PR_DIR.mkdir(parents=True, exist_ok=True)

log = logging.getLogger("quinely.pr")

# ── Review System Prompt ──────────────────────────────────────────────

REVIEW_SYSTEM_PROMPT = """\
You are a strict, senior code reviewer protecting the codebase from regressions, \
bugs, and bad design. Ghost has shipped 42+ documented bugs. Your job is to \
stop the next one. Check EVERY section below.

### Code Quality
- Security: input validation, path sanitization, no hardcoded secrets
- Correctness: logic bugs, off-by-one, race conditions, error handling
- Simplicity: no over-engineering, no unnecessary abstractions
- No bare `except: pass` or `except Exception: pass` that swallows real errors

### UI/UX Quality
- Modals MUST default to hidden, be dismissable (X, overlay click, Escape)
- Forms MUST use proper input types, follow dashboard dark theme patterns
- SVG icons, not emojis; use stat-card, btn, form-input, badge classes

### Frontend-Backend Integration (MOST DAMAGING — caused M-14, M-15, M-23)
- Backend API added = frontend UI MUST call it
- Frontend UI added = backend MUST persist and return data
- Feature MUST be wired into runtime (not just dead CRUD + UI)
- JS payload shape MUST match Python route's request.get_json()
- API responses MUST return live data, not stale defaults

### Tool Registration and Wiring (caused M-15, M-29, M-30)
- New module = MUST be imported in ghost.py
- New build_*_tools() = MUST be called in GhostDaemon.__init__
- New tool defs = MUST be registered via tool_registry.register()
- If any of these are missing, the feature is dead code — BLOCK it

### Ghost Tool Code Quality (ghost_tools/<name>/tool.py)
- Mentally trace EVERY line of the execute function with sample inputs.
  The code must actually work at runtime, not just look correct.
- Python operator precedence: `Path.home() / "foo".method()` calls method
  on the STRING, not on the Path. Correct: `(Path.home() / "foo").method()`.
  Check ALL chained Path operations for this class of bug.
- subprocess.run calls: verify the command list is correct, check=True will
  raise on failure, capture_output catches stderr for error reporting.
- All function parameters must have sane defaults. The execute function
  must accept **kwargs for forward compatibility.
- Return values must be JSON-serializable strings (json.dumps).
- If any code path could fail silently or produce wrong results at runtime
  despite passing syntax/import checks — BLOCK the PR.

### 🔴 ToolAPI Attribute Verification (CRITICAL — catches "silently broken" tools)
- The `api` object in register(api) is a ToolAPI (ghost_tool_builder.py).
  Its ONLY public attributes/methods are:
    api.id, api.manifest, api._config,
    api.register_tool(), api.register_hook(), api.register_cron(),
    api.register_setting(), api.get_setting(), api.set_setting(),
    api.read_data(), api.write_data(), api.log(),
    api.memory_save(), api.memory_search(), api.llm_summarize()
- There is NO api.daemon, NO api.cron, NO api.session_stats, NO api.stats.
- If tool code uses getattr(api, 'anything', None) or accesses api.anything:
  CHECK: does that attribute exist on ToolAPI? If not, the code will
  silently get None and return fake/zero/empty data — this is WORSE than
  crashing because it ships a non-functional tool that appears to work.
- Use read_pr_file('ghost_tool_builder.py') to verify if uncertain.
- A tool that would return all-zeros, 'unavailable', or placeholder data
  at runtime because its attribute lookups silently fail = BLOCK.
  The tool must return REAL data from VERIFIED sources.
- EXCEPTION: If the tool's TOOL.yaml has `enabled: false` AND the PR description
  mentions a pending dependency fix with `enables_tools`, this is the CORRECT
  pattern for cross-cutting dependencies. The tool is intentionally disabled until
  a core fix deploys. APPROVE this — it's better than shipping a broken enabled tool.

### Tool Execute Signatures (caused 6+ TypeError crashes)
- Every tool execute function MUST accept **kwargs or match the schema exactly
- Optional params MUST have defaults (e.g. `_=None`, `limit=50`)
- If schema says `"required": ["x"]`, execute MUST accept `x` as keyword arg

### Interface Compatibility (caused ghost_subagents crash — 3 hallucinated methods)
- New code imports a class from ghost_*.py = use grep_codebase to find the class definition
- Verify EVERY method called on that class actually exists in the source module
- LLMs hallucinate plausible method names for internal classes they haven't seen before
- Known hallucinations: run_once (real: run), list_tools (real: get_all/names), get_skill (real: .get)
- If the new code calls a method that does NOT exist on the imported class = BLOCK
- Check: does the constructor signature match how the class is instantiated?

### Thread Safety and File I/O (caused PR rejections)
- Shared files (log.json, config.json, growth_log.json) need locking or atomic writes
- Write to new paths = `Path.mkdir(parents=True, exist_ok=True)` first
- Prefer atomic write pattern: write to temp file, then `os.replace()`
- Never read an entire unbounded file into memory — use limits or tail reads
- No read-modify-write without a lock when multiple threads can access the file

### Python Correctness (caused M-06, M-07)
- NEVER `from module import mutable_var` (dead copy) — use `import module; module.var`
- No double-escaped strings: `"\\n".join()` is WRONG, `"\n".join()` is RIGHT
- No blocking I/O at module level or in `__init__` (no pip install, no network calls)

### Duplicate Functionality (caused M-17)
- Does this PR add something that already exists in the codebase?
- Check: is there an existing tool, module, or route that does the same thing?
- If EXISTING CODE MATCHES are provided below, verify those files don't already implement this feature.
- If the feature is already working in the codebase: VERDICT: BLOCK — "already implemented"

### Ghost Tool Validation (for ghost_tools/<name>/ changes)
- tool.py MUST define register(api) function
- register(api) MUST call api.register_tool() at least once
- Tool names MUST NOT shadow core tools (file_read, shell_exec, memory_save, etc.)
- Hook events MUST be valid: on_boot, on_shutdown, on_chat_message, on_tool_call, etc.
- NO register_page, register_route, or UI code — tools are backend-only
- Thread safety: no nested lock acquisition (deadlock)
- Exception handling: no bare except, no except Exception: pass

### Scope
- PR should do ONE thing. Flag unrelated changes.
- Multi-scope changes = REQUEST_CHANGES to split them.

### Cross-Platform Compatibility
- File paths MUST use pathlib.Path, never hardcoded `/` or `\\`
- No system-level dependencies (brew install, apt install)
- subprocess.run() with argument lists, not shell strings
- Platform-specific code must handle Darwin, Linux, AND Windows

## GHOST SYSTEM MAP (know the codebase so you can verify wiring)

### Backend Modules (ghost_*.py in project root)
ghost.py          — Main daemon, GhostDaemon class, tool registration
ghost_loop.py     — ToolLoopEngine, ToolRegistry, LoopDetector
ghost_tools.py    — Core tools: shell_exec, file_read/write, web_fetch, notify
ghost_browser.py  — PinchTab browser automation (HTTP API)
ghost_memory.py   — SQLite FTS5 memory (save/search/prune)
ghost_cron.py     — CronService, build_cron_tools()
ghost_skills.py   — SkillLoader, trigger matching, prompt injection
ghost_plugins.py  — PluginLoader, HookRunner
ghost_evolve.py   — EvolutionEngine, build_evolve_tools()
ghost_autonomy.py — Growth routines, action items, self-repair
ghost_integrations.py — Google APIs + Grok/X
ghost_credentials.py  — Encrypted credential storage
ghost_web_search.py   — Multi-provider web search
ghost_code_intel.py   — AST-based code analysis
ghost_tool_builder.py — ToolManager, ToolAPI for ghost_tools/<name>/ ecosystem
ghost_supervisor.py   — Process supervisor (PROTECTED — cannot modify)

### Ghost Tools (ghost_tools/<name>/)
Isolated LLM-callable tools. Each has TOOL.yaml + tool.py with register(api).
ToolAPI (ghost_tool_builder.py) — COMPLETE public interface:
  api.id, api.manifest, api._config (dict),
  api.register_tool(def), api.register_hook(event, cb), api.register_cron(name, cb, schedule),
  api.register_setting(schema), api.get_setting(key, default), api.set_setting(key, value),
  api.read_data(filename), api.write_data(filename, content), api.log(message),
  api.memory_save(content, tags, type), api.memory_search(query, limit), api.llm_summarize(text, instruction)
NO api.daemon, NO api.cron, NO api.stats, NO api.session_stats. Tools are backend-only.

### Dashboard Routes (ghost_dashboard/routes/)
chat.py, status.py, config.py, models.py, identity.py, skills.py,
cron.py, memory.py, feed.py, daemon.py, evolve.py, integrations.py,
autonomy.py, webhooks.py, setup.py, accounts.py
New blueprints MUST be registered in routes/__init__.py

### Frontend (ghost_dashboard/static/js/)
app.js  — SPA router, sidebar, navigate()
api.js  — window.GhostAPI: get/post/put/patch/del wrappers
Pages in pages/*.js — each exports render(container)
New pages MUST have: route in app.js, sidebar link in index.html

## CORE API REFERENCE (use this to verify method calls in PRs)
These are the EXACT public methods on the most-imported classes.
If the PR calls a method NOT listed here, use grep_codebase to verify
it exists. LLM-generated code frequently hallucinates plausible method
names that don't exist.

ToolLoopEngine (ghost_loop.py):
  __init__(api_key, model, base_url=..., fallback_models=None, auth_store=None, provider_chain=None, usage_tracker=None)
  .run(system_prompt, user_message, tool_registry=None, max_steps=20, temperature=0.3, max_tokens=..., ...)
  .single_shot(system_prompt, user_message, temperature=0.2, max_tokens=1024, image_b64=None, images=None)
  Known hallucinations: .run_once(), .step(), .execute() — these DO NOT exist

ToolRegistry (ghost_loop.py):
  .register(tool_def)  .unregister(name)  .get(name)  .get_all() -> dict
  .names() -> list  .execute(name, args) -> str  .to_openai_schema() -> list
  .subset(names) -> ToolRegistry  .is_reserved(name) -> bool
  Known hallucinations: .list_tools(), .list(), .find() — these DO NOT exist

SkillLoader (ghost_skills.py):
  .skills -> Dict[str, Skill]  .reload()  .check_reload(interval=30)
  .llm_match(engine, user_message, content_type=None, disabled=None) -> list[Skill]
  .get(name) -> Skill|None  .list_all() -> list[Skill]
  Known hallucinations: .get_skill(), .load() — these DO NOT exist

## Review Strategy
- Be SYSTEMATIC: review integration files first (ghost.py, routes/__init__.py,
  app.js, index.html), then new modules, then patches to existing files.
- Be THOROUGH: use grep_codebase to verify every claim the diff makes.
  If the diff adds `from ghost_foo import Bar`, grep to confirm Bar exists.
- Be SPECIFIC: file names, line numbers, exact code references. Never vague.
- Be ACTIONABLE: every REQUEST_CHANGES must have a clear fix path.
- Use suggest_change when the fix is obvious — saves the developer a round trip.
- Leave comments AS YOU GO, not all at the end. One concern per comment.

## Evidence-Based Review — NO Claims Without Verification
- NEVER say "this looks correct" without running grep_codebase or read_file to verify.
- For EVERY import: grep_codebase to confirm the imported symbol exists.
- For EVERY new route/endpoint: verify the frontend calls it (grep JS files).
- For EVERY method call on an internal class: verify the method exists in the source.
- Trace execution mentally: what happens with None input? Empty string? Missing key?
  If you find a path that would crash at runtime, that's a BLOCK.
- If you cannot verify a claim from the diff alone, use your tools. Assumptions = bugs.

**Response format as REVIEWER:**
1. Brief summary (1-2 sentences)
2. Specific concerns with line references from the diff
3. End with EXACTLY ONE verdict:
   VERDICT: APPROVE — safe, correct, well-integrated
   VERDICT: REQUEST_CHANGES — specific issues to fix (list them)
   VERDICT: BLOCK — fundamentally wrong approach

"""

MAX_REVIEW_ROUNDS = 3


# ── PR Store ─────────────────────────────────────────────────────────

class PRStore:
    """CRUD for pull requests, stored as JSON files in ~/.ghost/prs/.

    All writes are serialized through _lock so dashboard force-actions
    and the review loop can't corrupt a PR file with concurrent writes.
    """

    _lock = threading.Lock()

    def _read_pr_unlocked(self, pr_id: str) -> Optional[Dict]:
        path = PR_DIR / f"{pr_id}.json"
        if not path.exists():
            return None
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except Exception:
            return None

    def _write_pr_unlocked(self, pr: Dict):
        path = PR_DIR / f"{pr['pr_id']}.json"
        path.write_text(json.dumps(pr, indent=2, default=str), encoding="utf-8")

    def create_pr(self, evolution_id: str, feature_id: str, title: str,
                  description: str, branch: str, diff: str,
                  files_changed: list[str]) -> Dict[str, Any]:
        pr_id = f"pr-{uuid.uuid4().hex[:10]}"
        pr = {
            "pr_id": pr_id,
            "evolution_id": evolution_id,
            "feature_id": feature_id,
            "branch": branch,
            "title": title,
            "description": description,
            "status": "open",
            "diff": diff,
            "files_changed": files_changed,
            "discussions": [],
            "review_rounds": 0,
            "max_rounds": MAX_REVIEW_ROUNDS,
            "verdict": None,
            "blocked_reason": None,
            "created_at": datetime.now().isoformat(),
            "merged_at": None,
        }
        self._save_pr(pr)
        log.info("PR created: %s for evolution %s", pr_id, evolution_id)
        return pr

    def get_pr(self, pr_id: str) -> Optional[Dict]:
        with self._lock:
            return self._read_pr_unlocked(pr_id)

    def list_prs(self, status: Optional[str] = None,
                 feature_id: Optional[str] = None) -> List[Dict]:
        with self._lock:
            prs = []
            for f in sorted(PR_DIR.glob("pr-*.json"),
                            key=lambda p: p.stat().st_mtime, reverse=True):
                try:
                    pr = json.loads(f.read_text(encoding="utf-8"))
                    if status and pr.get("status") != status:
                        continue
                    if feature_id and pr.get("feature_id") != feature_id:
                        continue
                    prs.append(pr)
                except Exception:
                    continue
            return prs

    def update_status(self, pr_id: str, status: str) -> bool:
        with self._lock:
            pr = self._read_pr_unlocked(pr_id)
            if not pr:
                return False
            pr["status"] = status
            self._write_pr_unlocked(pr)
            return True

    def add_discussion(self, pr_id: str, role: str, message: str,
                       round_num: int) -> bool:
        with self._lock:
            pr = self._read_pr_unlocked(pr_id)
            if not pr:
                return False
            pr["discussions"].append({
                "role": role,
                "message": message,
                "round": round_num,
                "timestamp": datetime.now().isoformat(),
            })
            pr["review_rounds"] = round_num
            self._write_pr_unlocked(pr)
            return True

    def set_verdict(self, pr_id: str, verdict: str,
                    reason: Optional[str] = None) -> bool:
        with self._lock:
            pr = self._read_pr_unlocked(pr_id)
            if not pr:
                return False
            pr["verdict"] = verdict
            if verdict == "approved":
                pr["status"] = "approved"
            elif verdict == "blocked":
                pr["status"] = "blocked"
                pr["blocked_reason"] = reason
            elif verdict == "rejected":
                pr["status"] = "rejected"
            self._write_pr_unlocked(pr)
            return True

    def mark_merged(self, pr_id: str) -> bool:
        with self._lock:
            pr = self._read_pr_unlocked(pr_id)
            if not pr:
                return False
            pr["status"] = "merged"
            pr["merged_at"] = datetime.now().isoformat()
            self._write_pr_unlocked(pr)
            return True

    def update_diff(self, pr_id: str, diff: str,
                    files_changed: list[str]) -> bool:
        with self._lock:
            pr = self._read_pr_unlocked(pr_id)
            if not pr:
                return False
            pr["diff"] = diff
            pr["files_changed"] = files_changed
            self._write_pr_unlocked(pr)
            return True

    def set_old_head_sha(self, pr_id: str, sha: str) -> bool:
        """Store the branch HEAD SHA from before the latest fixes (for interdiff)."""
        with self._lock:
            pr = self._read_pr_unlocked(pr_id)
            if not pr:
                return False
            pr["old_head_sha"] = sha
            self._write_pr_unlocked(pr)
            return True

    def reopen_pr(self, pr_id: str) -> bool:
        """Reopen a rejected PR for a new review round (stale review dismissal)."""
        with self._lock:
            pr = self._read_pr_unlocked(pr_id)
            if not pr:
                return False
            pr["status"] = "open"
            pr["verdict"] = None
            pr["review_rounds"] = pr.get("review_rounds", 0) + 1
            self._write_pr_unlocked(pr)
            return True

    def _save_pr(self, pr: Dict):
        with self._lock:
            self._write_pr_unlocked(pr)


# ── Review Engine ────────────────────────────────────────────────────

class ReviewEngine:
    """Orchestrates code review as a GitHub-style tool loop.

    The reviewer runs as a full ToolLoopEngine agent with dedicated tools
    for reading diffs per-file, browsing the codebase, leaving inline
    comments, suggesting exact code changes, and submitting a final verdict.
    """

    def __init__(self, store: PRStore, evolve_engine=None):
        self.store = store
        self.evolve_engine = evolve_engine

    # ── Diff splitting ───────────────────────────────────────────────

    @staticmethod
    def _split_diff_by_file(raw_diff: str) -> list[dict]:
        """Split a unified diff into per-file chunks.

        Returns list of dicts: [{file, diff, is_new, lines_added, lines_removed}]
        Sorted: new files first, then integration files, then patches.
        """
        if not raw_diff:
            return []

        files = []
        current_file = None
        current_lines = []
        is_new = False

        for line in raw_diff.split("\n"):
            if line.startswith("diff --git"):
                if current_file and current_lines:
                    diff_text = "\n".join(current_lines)
                    added = diff_text.count("\n+") - diff_text.count("\n+++")
                    removed = diff_text.count("\n-") - diff_text.count("\n---")
                    files.append({
                        "file": current_file,
                        "diff": diff_text,
                        "is_new": is_new,
                        "lines_added": max(0, added),
                        "lines_removed": max(0, removed),
                    })
                parts = line.split(" b/")
                current_file = parts[-1] if len(parts) > 1 else "unknown"
                current_lines = [line]
                is_new = False
            elif line.startswith("new file mode"):
                is_new = True
                current_lines.append(line)
            else:
                current_lines.append(line)

        if current_file and current_lines:
            diff_text = "\n".join(current_lines)
            added = diff_text.count("\n+") - diff_text.count("\n+++")
            removed = diff_text.count("\n-") - diff_text.count("\n---")
            files.append({
                "file": current_file,
                "diff": diff_text,
                "is_new": is_new,
                "lines_added": max(0, added),
                "lines_removed": max(0, removed),
            })

        integration_files = {"ghost.py", "__init__.py", "app.js", "index.html"}

        def _sort_key(f):
            name = Path(f["file"]).name
            if f["is_new"]:
                return (0, name)
            if name in integration_files:
                return (1, name)
            return (2, name)

        files.sort(key=_sort_key)
        return files

    # ── Reviewer tools (built per-review, scoped to PR) ──────────────

    def _build_reviewer_tools(self, pr: dict) -> list[dict]:
        """Build the 6 dedicated reviewer tools, all scoped to this PR."""
        pr_id = pr["pr_id"]
        file_diffs = self._split_diff_by_file(pr.get("diff", ""))
        diff_index = {fd["file"]: fd for fd in file_diffs}

        def read_pr_diff(file: str = "", **kwargs) -> str:
            if not file:
                lines = [f"PR has {len(file_diffs)} changed file(s):\n"]
                for fd in file_diffs:
                    tag = "NEW" if fd["is_new"] else "MODIFIED"
                    lines.append(
                        f"  [{tag}] {fd['file']} "
                        f"(+{fd['lines_added']}/-{fd['lines_removed']})"
                    )
                lines.append(
                    "\nCall read_pr_diff(file='<filename>') to see the diff for a specific file."
                )
                return "\n".join(lines)

            fd = diff_index.get(file)
            if not fd:
                for key in diff_index:
                    if key.endswith(file) or file.endswith(key):
                        fd = diff_index[key]
                        break
            if not fd:
                return f"File '{file}' not found in this PR. Available: {', '.join(diff_index.keys())}"
            return f"Diff for {fd['file']}:\n```diff\n{fd['diff']}\n```"

        def read_pr_file(file: str = "", offset: int = 1, limit: int = 200, **kwargs) -> str:
            if not file or file.strip() in ("", ".py", ".js", ".html", ".css"):
                avail = ", ".join(diff_index.keys()) if diff_index else "none"
                return (
                    f"Error: provide a full file path (not '{file}'). "
                    f"Files in this PR: {avail}. "
                    "Call read_pr_diff() with no args to see the file list."
                )
            abs_path = PROJECT_DIR / file
            if not abs_path.exists():
                return f"File not found: {file}"
            try:
                all_lines = abs_path.read_text(encoding="utf-8").splitlines()
                start = max(0, offset - 1)
                end = start + limit
                selected = all_lines[start:end]
                numbered = [
                    f"{i + start + 1:4d}| {line}"
                    for i, line in enumerate(selected)
                ]
                total = len(all_lines)
                header = f"File: {file} (lines {start+1}-{min(end, total)} of {total})"
                return header + "\n" + "\n".join(numbered)
            except Exception as e:
                return f"Error reading {file}: {e}"

        def grep_codebase(pattern: str = "", include: str = "*.py", **kwargs) -> str:
            if not pattern:
                return "Error: pattern parameter is required."
            import subprocess
            try:
                args = ["rg", "-n", pattern, f"--glob={include}", str(PROJECT_DIR)]
                result = subprocess.run(
                    args, capture_output=True, text=True, timeout=10,
                )
                output = result.stdout.strip()
            except FileNotFoundError:
                output = ""
                for fp in PROJECT_DIR.rglob(include):
                    try:
                        for i, line in enumerate(fp.read_text(encoding="utf-8", errors="ignore").splitlines(), 1):
                            if pattern in line:
                                output += f"{fp}:{i}:{line}\n"
                    except Exception:
                        continue
                output = output.strip()
            except Exception as e:
                return f"grep error: {e}"
            if not output:
                return f"No matches for '{pattern}' in {include}"
            lines = output.split("\n")
            if len(lines) > 30:
                return "\n".join(lines[:30]) + f"\n\n... ({len(lines)} total matches, showing first 30)"
            return output

        def leave_comment(file: str = "", line: int = 0, message: str = "",
                         severity: str = "warning", **kwargs) -> str:
            file = file or ""
            message = message or ""
            if not file or not message:
                return "Error: file and message are required."
            if severity not in ("critical", "high", "warning", "suggestion", "note"):
                severity = "warning"
            with self.store._lock:
                current_pr = self.store._read_pr_unlocked(pr_id)
                if not current_pr:
                    return "PR not found"
                if "inline_comments" not in current_pr:
                    current_pr["inline_comments"] = []
                current_pr["inline_comments"].append({
                    "file": file,
                    "line": line,
                    "message": message,
                    "severity": severity,
                    "round": current_pr.get("review_rounds", 1),
                })
                self.store._write_pr_unlocked(current_pr)
            return f"Comment added: [{severity}] {file}:{line} — {message[:80]}"

        def suggest_change(file: str = "", old_code: str = "", new_code: str = "",
                          explanation: str = "", **kwargs) -> str:
            file = file or ""
            old_code = old_code or ""
            new_code = new_code or ""
            if not file or not old_code or not new_code:
                return "Error: file, old_code, and new_code are required."
            with self.store._lock:
                current_pr = self.store._read_pr_unlocked(pr_id)
                if not current_pr:
                    return "PR not found"
                if "suggested_changes" not in current_pr:
                    current_pr["suggested_changes"] = []
                current_pr["suggested_changes"].append({
                    "file": file,
                    "old_code": old_code,
                    "new_code": new_code,
                    "explanation": explanation,
                    "round": current_pr.get("review_rounds", 1),
                    "applied": False,
                })
                self.store._write_pr_unlocked(current_pr)
            return f"Suggestion added for {file}: {explanation[:80]}"

        def get_my_comments(**kwargs) -> str:
            """Read back all comments left so far in this review round."""
            current_pr = self.store.get_pr(pr_id)
            if not current_pr:
                return "PR not found"
            current_round = current_pr.get("review_rounds", 1)
            comments = current_pr.get("inline_comments", [])
            this_round = [c for c in comments if c.get("round", 0) == current_round]
            suggestions = current_pr.get("suggested_changes", [])
            this_round_sug = [s for s in suggestions if s.get("round", 0) == current_round]

            if not this_round and not this_round_sug:
                return "No comments or suggestions left yet in this review round."

            lines = [f"Your comments this round ({len(this_round)} comments, {len(this_round_sug)} suggestions):\n"]
            for c in this_round:
                lines.append(
                    f"  [{c['severity'].upper()}] {c['file']}:{c['line']} — {c['message']}"
                )
            for s in this_round_sug:
                lines.append(
                    f"  [SUGGEST] {s['file']} — {s.get('explanation', '')[:80]}"
                )
            return "\n".join(lines)

        def get_review_history(**kwargs) -> str:
            """Read the full PR discussion history and past rejection feedback."""
            current_pr = self.store.get_pr(pr_id)
            if not current_pr:
                return "PR not found"
            lines = []

            discussions = current_pr.get("discussions", [])
            if discussions:
                lines.append(f"=== DISCUSSION HISTORY ({len(discussions)} messages) ===")
                for d in discussions:
                    role = d.get("role", "?").upper()
                    rnd = d.get("round", "?")
                    ts = d.get("timestamp", "")[:16]
                    msg = d.get("message", "")[:1200]
                    lines.append(f"\n[Round {rnd} | {role} | {ts}]\n{msg}")

            all_comments = current_pr.get("inline_comments", [])
            all_suggestions = current_pr.get("suggested_changes", [])
            current_round = current_pr.get("review_rounds", 1)
            prev_comments = [c for c in all_comments if c.get("round", 0) < current_round]
            prev_suggestions = [s for s in all_suggestions if s.get("round", 0) < current_round]

            if prev_comments or prev_suggestions:
                lines.append(f"\n=== PREVIOUS COMMENTS ({len(prev_comments)} comments, "
                             f"{len(prev_suggestions)} suggestions) ===")
                for c in prev_comments:
                    rnd = c.get("round", "?")
                    lines.append(
                        f"  [Round {rnd}] [{c['severity'].upper()}] "
                        f"{c['file']}:{c['line']} — {c['message']}"
                    )
                for s in prev_suggestions:
                    rnd = s.get("round", "?")
                    lines.append(
                        f"  [Round {rnd}] [SUGGEST] {s['file']} — "
                        f"{s.get('explanation', '')[:120]}"
                    )

            if not lines:
                return "No prior review history for this PR."
            return "\n".join(lines)

        _review_submitted = {"done": False}

        def submit_review(verdict: str = "REQUEST_CHANGES", summary: str = "", **kwargs) -> str:
            verdict = verdict or "REQUEST_CHANGES"
            summary = summary or ""
            verdict_upper = verdict.upper().strip()
            if verdict_upper in ("APPROVE", "APPROVED"):
                verdict_key = "approved"
            elif verdict_upper in ("BLOCK", "BLOCKED"):
                verdict_key = "blocked"
            else:
                verdict_key = "rejected"

            self.store.add_discussion(
                pr_id, "reviewer", summary,
                pr.get("review_rounds", 1)
            )
            if verdict_key == "blocked":
                reason = summary[:500]
                self.store.set_verdict(pr_id, "blocked", reason)
            elif verdict_key == "approved":
                self.store.set_verdict(pr_id, "approved")
            else:
                self.store.set_verdict(pr_id, "rejected", summary[:500])

            _review_submitted["done"] = True
            return f"Review submitted: {verdict_key.upper()}. Call task_complete now."

        return [
            {
                "name": "read_pr_diff",
                "description": (
                    "Read the diff for a specific file in this PR. "
                    "Call with no arguments to see the list of changed files with stats. "
                    "Call with file='<filename>' to see the actual diff for that file."
                ),
                "parameters": {
                    "type": "object",
                    "properties": {
                        "file": {
                            "type": "string",
                            "description": "File to read diff for (omit to see file list)",
                            "default": "",
                        },
                    },
                },
                "execute": read_pr_diff,
            },
            {
                "name": "read_pr_file",
                "description": (
                    "Read the full current content of a file (not just the diff). "
                    "Use this to check surrounding code, imports, class definitions, etc."
                ),
                "parameters": {
                    "type": "object",
                    "properties": {
                        "file": {"type": "string", "description": "File path relative to project root"},
                        "offset": {"type": "integer", "description": "Line to start reading from (1-based)", "default": 1},
                        "limit": {"type": "integer", "description": "Number of lines to read", "default": 200},
                    },
                    "required": ["file"],
                },
                "execute": read_pr_file,
            },
            {
                "name": "grep_codebase",
                "description": (
                    "Search the codebase for a pattern. Used to check for duplicate "
                    "functionality, verify wiring (imports in ghost.py, tool registration), "
                    "or find related code."
                ),
                "parameters": {
                    "type": "object",
                    "properties": {
                        "pattern": {"type": "string", "description": "Search pattern (regex)"},
                        "include": {"type": "string", "description": "File glob (e.g. '*.py', '*.js')", "default": "*.py"},
                    },
                    "required": ["pattern"],
                },
                "execute": grep_codebase,
            },
            {
                "name": "leave_comment",
                "description": (
                    "Leave an inline review comment on a specific file and line. "
                    "The implementer sees these on the next fix-and-resubmit round. "
                    "Use 'critical' for blocking issues, 'warning' for should-fix, "
                    "'suggestion' for nice-to-have, 'note' for informational."
                ),
                "parameters": {
                    "type": "object",
                    "properties": {
                        "file": {"type": "string", "description": "File the comment is about"},
                        "line": {"type": "integer", "description": "Line number the comment refers to"},
                        "message": {"type": "string", "description": "The review comment"},
                        "severity": {
                            "type": "string",
                            "enum": ["critical", "warning", "suggestion", "note"],
                            "default": "warning",
                            "description": "Severity level",
                        },
                    },
                    "required": ["file", "line", "message"],
                },
                "execute": leave_comment,
            },
            {
                "name": "suggest_change",
                "description": (
                    "Suggest an exact code change (like GitHub's suggestion feature). "
                    "The implementer can apply this directly. Use when the fix is obvious "
                    "and you can provide the exact corrected code."
                ),
                "parameters": {
                    "type": "object",
                    "properties": {
                        "file": {"type": "string", "description": "File to suggest change for"},
                        "old_code": {"type": "string", "description": "The current code that should be changed"},
                        "new_code": {"type": "string", "description": "The suggested replacement code"},
                        "explanation": {"type": "string", "description": "Why this change is needed", "default": ""},
                    },
                    "required": ["file", "old_code", "new_code"],
                },
                "execute": suggest_change,
            },
            {
                "name": "get_my_comments",
                "description": (
                    "Read back all comments and suggestions you have left so far "
                    "in this review round. Use this to refresh your memory before "
                    "calling submit_review — especially after reviewing many files, "
                    "since older tool results may have been compacted."
                ),
                "parameters": {
                    "type": "object",
                    "properties": {},
                },
                "execute": get_my_comments,
            },
            {
                "name": "get_review_history",
                "description": (
                    "Read the full PR discussion history: all past reviewer verdicts "
                    "and summaries, implementer responses, and previous inline comments "
                    "from all rounds. Use this on re-reviews to understand what was "
                    "rejected before and why."
                ),
                "parameters": {
                    "type": "object",
                    "properties": {},
                },
                "execute": get_review_history,
            },
            {
                "name": "submit_review",
                "description": (
                    "Submit your final review verdict. MUST be called exactly once "
                    "after reviewing all files. Verdict must be one of: APPROVE, "
                    "REQUEST_CHANGES, or BLOCK."
                ),
                "parameters": {
                    "type": "object",
                    "properties": {
                        "verdict": {
                            "type": "string",
                            "enum": ["APPROVE", "REQUEST_CHANGES", "BLOCK"],
                            "description": "Your final verdict",
                        },
                        "summary": {
                            "type": "string",
                            "description": "Brief overall assessment (1-3 sentences)",
                        },
                    },
                    "required": ["verdict", "summary"],
                },
                "execute": submit_review,
            },
        ]

    # ── Reviewer prompt builder ──────────────────────────────────────

    def _build_reviewer_prompt(self, pr: dict) -> str:
        """Build the system prompt for the reviewer agent.

        Loads the pr-reviewer skill if available, otherwise falls back
        to the hardcoded REVIEW_SYSTEM_PROMPT.
        """
        skill_content = ""
        try:
            skill_path = PROJECT_DIR / "skills" / "pr-reviewer" / "SKILL.md"
            if skill_path.exists():
                raw = skill_path.read_text(encoding="utf-8")
                if "---" in raw:
                    parts = raw.split("---", 2)
                    if len(parts) >= 3:
                        skill_content = parts[2].strip()
        except Exception:
            pass

        if skill_content:
            return skill_content
        return REVIEW_SYSTEM_PROMPT

    # ── Review context builder ───────────────────────────────────────

    def _build_review_context(self, pr: dict) -> str:
        """Build the initial context message for the reviewer."""
        file_diffs = self._split_diff_by_file(pr.get("diff", ""))
        file_list = "\n".join(
            f"  {'[NEW]' if fd['is_new'] else '[MOD]'} {fd['file']} "
            f"(+{fd['lines_added']}/-{fd['lines_removed']})"
            for fd in file_diffs
        )

        context = (
            f"## Pull Request: {pr['title']}\n"
            f"**Description:** {pr['description']}\n"
            f"**Branch:** {pr['branch']}\n"
            f"**Review round:** {pr.get('review_rounds', 1)}\n"
            f"**Files changed ({len(file_diffs)}):**\n{file_list}\n\n"
        )

        # ── PR discussion history (reviewer summaries + implementer responses) ──
        discussions = pr.get("discussions", [])
        if discussions:
            current_round = pr.get("review_rounds", 1)
            prev_discussions = [d for d in discussions if d.get("round", 0) < current_round]
            if prev_discussions:
                disc_lines = []
                for d in prev_discussions[-6:]:
                    role = d.get("role", "unknown").upper()
                    rnd = d.get("round", "?")
                    msg = d.get("message", "")[:800]
                    disc_lines.append(f"  [Round {rnd} — {role}] {msg}")
                context += (
                    f"**REVIEW DISCUSSION HISTORY** ({len(prev_discussions)} messages):\n"
                    + "\n".join(disc_lines) + "\n\n"
                )

        # ── Rejection history from feature store ──
        if pr.get("feature_id"):
            try:
                from ghost_future_features import FutureFeaturesStore
                feat = FutureFeaturesStore().get_by_id(pr["feature_id"])
                rejections = feat.get("pr_rejections", []) if feat else []
                if rejections:
                    history_text = "\n".join(
                        f"- Attempt {r.get('attempt', i+1)}: {r.get('feedback', 'unknown')[:800]}"
                        for i, r in enumerate(rejections[-3:])
                    )
                    context += (
                        f"**REJECTION HISTORY** — This feature has been rejected "
                        f"{len(rejections)} time(s) before. Previous feedback:\n"
                        f"{history_text}\n\n"
                        "Pay SPECIAL attention to whether these specific issues were fixed.\n\n"
                    )
            except Exception:
                pass

        # ── Interdiff for re-review rounds ──
        old_head_sha = pr.get("old_head_sha", "")
        if old_head_sha and pr.get("review_rounds", 0) > 0:
            try:
                import ghost_git
                new_head = ghost_git.get_head_sha(pr.get("branch", ""))
                if new_head and old_head_sha != new_head:
                    interdiff = ghost_git.get_interdiff(old_head_sha, new_head)
                    if interdiff.strip():
                        trimmed = interdiff[:4000]
                        overflow = " (truncated — use read_pr_diff for full diffs)" if len(interdiff) > 4000 else ""
                        context += (
                            "**CHANGES SINCE YOUR LAST REVIEW** "
                            f"(review these first — this is what the developer fixed){overflow}:\n"
                            f"```diff\n{trimmed}\n```\n\n"
                        )
            except Exception:
                pass

        # ── Previous inline comments for re-reviews ──
        prev_comments = pr.get("inline_comments", [])
        if prev_comments:
            prev_round = pr.get("review_rounds", 1) - 1
            old_comments = [c for c in prev_comments if c.get("round", 0) <= prev_round]
            if old_comments:
                comments_text = "\n".join(
                    f"  [{c['severity'].upper()}] {c['file']}:{c['line']} — {c['message']}"
                    for c in old_comments[-15:]
                )
                context += (
                    f"**YOUR PREVIOUS COMMENTS ({len(old_comments)} total):**\n"
                    f"{comments_text}\n\n"
                    "Check if each of these was addressed by the developer.\n\n"
                )

        is_rereview = pr.get("review_rounds", 1) > 1
        context += "## Your task\n"
        if is_rereview:
            context += (
                "⚠️ This is a RE-REVIEW (round {round}). The developer made changes "
                "to address your previous feedback.\n"
                "0. Call get_review_history() FIRST to see your past verdicts, comments,\n"
                "   and rejection reasons. This is critical — do NOT skip it.\n"
            ).format(round=pr.get("review_rounds", 1))
        context += (
            "1. Call read_pr_diff() (no args) to see the file list.\n"
            "2. Review each file with read_pr_diff(file='...').\n"
            "3. Use read_pr_file to check surrounding code when needed.\n"
            "4. Use grep_codebase to verify wiring and check for duplicates.\n"
            "5. Leave comments with leave_comment for each issue found.\n"
            "6. Use suggest_change when you can provide an exact fix.\n"
            "7. BEFORE submitting: call get_my_comments to refresh your memory of all\n"
            "   issues found — older comments may have been compacted from your context.\n"
            "8. 🔴 MANDATORY: Call submit_review(verdict, summary) EXACTLY ONCE to end.\n"
            "   Your verdict MUST be one of: APPROVE, REQUEST_CHANGES, or BLOCK.\n"
            "   If you do NOT call submit_review, your review defaults to REQUEST_CHANGES.\n"
            "   Never just write your verdict in text — you MUST use the submit_review tool.\n"
        )
        return context

    # ── Main review entry point ──────────────────────────────────────

    def run_review(self, pr_id: str, engine) -> str:
        """Run a GitHub-style code review via tool loop.

        The reviewer agent browses diffs per-file, reads surrounding code,
        leaves inline comments, suggests changes, and submits a verdict.

        Returns: "approved", "rejected", or "blocked".
        """
        pr = self.store.get_pr(pr_id)
        if not pr:
            return "rejected"

        self.store.update_status(pr_id, "reviewing")
        log.info("Starting review for PR %s: %s (round %d)",
                 pr_id, pr["title"], pr.get("review_rounds", 1))

        reviewer_tools_list = self._build_reviewer_tools(pr)
        system_prompt = self._build_reviewer_prompt(pr)
        context_message = self._build_review_context(pr)

        try:
            from ghost_loop import EvolveContextLogger
            ctx_log = EvolveContextLogger.get()
            ctx_log.set_session(session_id=pr_id, caller="pr_reviewer")
            if pr.get("feature_id"):
                ctx_log.set_feature(pr["feature_id"], pr.get("title", ""))
        except Exception:
            pass

        from ghost_loop import ToolRegistry
        reviewer_registry = ToolRegistry()
        for tool_def in reviewer_tools_list:
            reviewer_registry.register(tool_def)

        try:
            result = engine.run(
                system_prompt=system_prompt,
                user_message=context_message,
                tool_registry=reviewer_registry,
                max_steps=30,
                temperature=0.3,
                max_tokens=4096,
            )
        except Exception as exc:
            log.error("Reviewer tool loop failed: %s\n%s", exc, traceback.format_exc())
            self.store.set_verdict(pr_id, "rejected",
                                   f"Reviewer tool loop error: {exc}")
            return "rejected"

        try:
            from ghost_loop import EvolveContextLogger
            is_rereview = pr.get("review_rounds", 1) > 1
            EvolveContextLogger.get().log_skill_compliance(
                role="reviewer",
                tool_calls=result.tool_calls,
                extra={
                    "is_rereview": is_rereview,
                    "review_round": pr.get("review_rounds", 1),
                    "pr_id": pr_id,
                },
            )
        except Exception:
            pass

        updated_pr = self.store.get_pr(pr_id) or pr
        verdict = updated_pr.get("verdict")

        if not verdict:
            log.warning("Reviewer did not call submit_review — deriving verdict from comments")
            verdict = self._derive_verdict_from_pr(updated_pr)
            summary = self._build_auto_derived_summary(updated_pr, verdict)
            self.store.add_discussion(
                pr_id, "reviewer", summary,
                updated_pr.get("review_rounds", 1),
            )
            if verdict == "approved":
                self.store.set_verdict(pr_id, "approved")
            elif verdict == "blocked":
                self.store.set_verdict(pr_id, "blocked", summary[:500])
            else:
                self.store.set_verdict(pr_id, "rejected", summary[:500])

        log.info("PR %s verdict: %s", pr_id, verdict)

        if verdict == "approved":
            return "approved"
        elif verdict == "blocked":
            return "blocked"
        else:
            return "rejected"

    @staticmethod
    def _derive_verdict_from_pr(pr: dict) -> str:
        """Deterministic verdict from PR metadata -- no LLM guessing.

        Rules:
        - Any comment with severity 'critical' -> blocked
        - Any comment with severity 'high' or any suggestion -> rejected
        - No comments and no suggestions -> approved
        - Only 'low'/'info'/'note' severity comments -> approved
        """
        comments = pr.get("inline_comments", [])
        suggestions = pr.get("suggested_changes", [])

        if not comments and not suggestions:
            return "approved"

        severities = {c.get("severity", "info").lower() for c in comments}

        if "critical" in severities:
            return "blocked"
        if "high" in severities or "warning" in severities or suggestions:
            return "rejected"

        return "approved"

    @staticmethod
    def _build_auto_derived_summary(pr: dict, verdict: str) -> str:
        """Build a human-readable summary from inline comments and suggestions
        when the reviewer agent failed to call submit_review."""
        parts = [f"[Auto-derived verdict: {verdict.upper()}]"]

        comments = pr.get("inline_comments", [])
        if comments:
            parts.append(f"\nReviewer left {len(comments)} inline comment(s):")
            for c in comments:
                sev = c.get("severity", "info").upper()
                parts.append(
                    f"  [{sev}] {c.get('file', '?')}:{c.get('line', '?')} — "
                    f"{c.get('message', '(no message)')}"
                )

        suggestions = pr.get("suggested_changes", [])
        if suggestions:
            parts.append(f"\n{len(suggestions)} suggested change(s):")
            for s in suggestions:
                parts.append(
                    f"  {s.get('file', '?')} — {s.get('explanation', '(no explanation)')}"
                )

        if not comments and not suggestions:
            parts.append(
                "\nNo inline comments or suggestions were left. "
                "The reviewer may have encountered an error or hit the step limit."
            )

        return "\n".join(parts)


# ── Singleton ────────────────────────────────────────────────────────

_pr_store: Optional[PRStore] = None
_review_engine: Optional[ReviewEngine] = None


def get_pr_store() -> PRStore:
    global _pr_store
    if _pr_store is None:
        _pr_store = PRStore()
    return _pr_store


def get_review_engine(evolve_engine=None) -> ReviewEngine:
    global _review_engine
    if _review_engine is None:
        _review_engine = ReviewEngine(get_pr_store(), evolve_engine)
    elif evolve_engine and _review_engine.evolve_engine is None:
        _review_engine.evolve_engine = evolve_engine
    return _review_engine


# ── LLM Tools ────────────────────────────────────────────────────────

def build_pr_tools(cfg: dict = None) -> list[dict]:
    """Build LLM-callable tools for PR management."""
    store = get_pr_store()

    def list_prs_exec(status=None, feature_id=None):
        prs = store.list_prs(status=status, feature_id=feature_id)
        if not prs:
            return "No pull requests found."
        lines = [f"Found {len(prs)} PR(s):"]
        for pr in prs[:20]:
            verdict_str = f" [{pr['verdict']}]" if pr.get("verdict") else ""
            lines.append(
                f"  {pr['pr_id']}: {pr['title']} "
                f"(status={pr['status']}{verdict_str}, "
                f"rounds={pr['review_rounds']}, "
                f"branch={pr['branch']})"
            )
        return "\n".join(lines)

    def get_pr_exec(pr_id):
        pr = store.get_pr(pr_id)
        if not pr:
            return f"PR not found: {pr_id}"
        lines = [
            f"PR: {pr['pr_id']}",
            f"Title: {pr['title']}",
            f"Status: {pr['status']}",
            f"Verdict: {pr.get('verdict', 'pending')}",
            f"Branch: {pr['branch']}",
            f"Files: {', '.join(pr['files_changed'])}",
            f"Review rounds: {pr['review_rounds']}/{pr['max_rounds']}",
            f"Created: {pr['created_at']}",
        ]
        if pr.get("blocked_reason"):
            lines.append(f"Block reason: {pr['blocked_reason']}")
        if pr.get("discussions"):
            lines.append(f"\nDiscussion ({len(pr['discussions'])} messages):")
            for d in pr["discussions"][-6:]:
                role = d["role"].upper()
                msg_preview = d["message"][:300]
                lines.append(f"  [{role} R{d['round']}] {msg_preview}")
        return "\n".join(lines)

    return [
        {
            "name": "list_prs",
            "description": (
                "List pull requests. Optionally filter by status "
                "(open, reviewing, approved, merged, blocked, rejected) "
                "or feature_id."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "status": {
                        "type": "string",
                        "description": "Filter by PR status",
                    },
                    "feature_id": {
                        "type": "string",
                        "description": "Filter by feature ID",
                    },
                },
            },
            "execute": list_prs_exec,
        },
        {
            "name": "get_pr",
            "description": (
                "Get details of a specific pull request including "
                "diff, discussion history, and verdict."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "pr_id": {
                        "type": "string",
                        "description": "The PR ID (e.g. pr-abc1234567)",
                    },
                },
                "required": ["pr_id"],
            },
            "execute": get_pr_exec,
        },
    ]
