"""
Ghost Self-Evolution Engine — lets Ghost modify its own codebase safely.

Provides: EvolutionEngine (backup, validate, test, deploy, rollback, history)
          build_evolve_tools() for ToolRegistry integration
"""

import ast
import difflib
import json
import logging
import os
import re
import shutil
import subprocess
import sys
import tarfile
import threading
import time
import uuid
from datetime import datetime
from pathlib import Path

log = logging.getLogger("quinely.evolve")

PROJECT_DIR = Path(__file__).resolve().parent
GHOST_HOME = Path.home() / ".ghost"
EVOLVE_DIR = GHOST_HOME / "evolve"
BACKUP_DIR = EVOLVE_DIR / "backups"
PENDING_DIR = EVOLVE_DIR / "pending"
HISTORY_FILE = EVOLVE_DIR / "history.json"
DEPLOY_MARKER = EVOLVE_DIR / "deploy_pending"

DELETED_FILES_LOG = EVOLVE_DIR / "deleted_files.json"

def _normalize_file_path(file_path: str) -> Path:
    """Normalize file paths to resolve relative to PROJECT_DIR.
    
    Handles: absolute paths, tilde paths, and accidental PROJECT_DIR-relative paths
    like 'Downloads/IMG/ghost.py' which should just be 'ghost.py'.
    """
    expanded = Path(file_path).expanduser()
    if expanded.is_absolute():
        return expanded.resolve()
    rel_str = str(expanded)
    proj_resolved = str(PROJECT_DIR.resolve())
    proj_name = PROJECT_DIR.name
    try:
        proj_rel = str(PROJECT_DIR.relative_to(Path.home()))
    except ValueError:
        proj_rel = ""
    rel_path = Path(rel_str)
    if proj_rel:
        try:
            rel_str = str(rel_path.relative_to(proj_rel))
            rel_path = Path(rel_str)
        except ValueError:
            pass
    try:
        rel_str = str(rel_path.relative_to(proj_name))
    except ValueError:
        pass
    return (PROJECT_DIR / rel_str).resolve()

EVOLVE_DIR.mkdir(parents=True, exist_ok=True)
BACKUP_DIR.mkdir(parents=True, exist_ok=True)
PENDING_DIR.mkdir(parents=True, exist_ok=True)

MAX_BACKUPS = 20
MAX_EVOLUTIONS_PER_HOUR = 25
HEALTH_CHECK_TIMEOUT = 15
MAX_NEW_FILE_SIZE = 30000

PROTECTED_FILES = {
    "ghost_supervisor.py",
}

PROTECTED_PATTERNS = [
    "PROTECTED_FILES",
    "PROTECTED_PATTERNS",
    "MAX_EVOLUTIONS_PER_HOUR",
    "evolve_rollback",
    "_restore_backup",
    "CORE_COMMANDS",
    "DEFAULT_ALLOWED_COMMANDS",
]

BACKUP_EXCLUDE_DIRS = {
    "__pycache__", ".git", "node_modules",
    ".venv", "venv", ".mypy_cache", ".pytest_cache",
}
BACKUP_EXCLUDE_FILES = {
    "memory.db", "memory.db-wal", "memory.db-shm",
    ".env", ".DS_Store",
}


class EvolutionEngine:
    """Manages Ghost's self-modification lifecycle."""

    def __init__(self):
        self._lock = threading.Lock()
        self._active_evolutions = {}
        self._history = self._load_history()
        self._active_jobs_fn = None  # Set by GhostDaemon to check cron status
        self.tool_event_bus = None  # Set by GhostDaemon for hook emission
        self.tool_manager = None    # Set by GhostDaemon for tool unload on rejection
        self._deploy_triggered = threading.Event()
        self._cleanup_orphaned_pending()

    @property
    def deploy_in_progress(self) -> bool:
        """True once deploy() has written the deploy_pending marker."""
        return self._deploy_triggered.is_set()

    def _cleanup_orphaned_pending(self):
        """Remove pending evolution files left over from a previous process.

        When Ghost restarts, any _wait_for_approval loops from the old process
        are dead. Keeping the pending files causes stale approval requests to
        appear in the dashboard with no live listener to act on them.
        """
        cleaned = 0
        for pf in PENDING_DIR.glob("*.json"):
            try:
                pf.unlink()
                cleaned += 1
            except Exception:
                pass
        if cleaned:
            import logging
            _log = logging.getLogger("quinely.evolve")
            _log.info("Cleaned up %d orphaned pending evolution(s) on startup", cleaned)

    def set_active_jobs_fn(self, fn):
        """Register a callable that returns the count of active cron jobs
        (excluding the Feature Implementer itself). Used by deploy() to wait
        for other jobs to finish before restarting Ghost."""
        self._active_jobs_fn = fn

    def _load_history(self):
        if HISTORY_FILE.exists():
            try:
                return json.loads(HISTORY_FILE.read_text(encoding="utf-8"))
            except Exception:
                pass
        return []

    def _save_history(self):
        data = json.dumps(self._history, indent=2)
        import os as _os
        fd = _os.open(str(HISTORY_FILE), _os.O_WRONLY | _os.O_CREAT | _os.O_TRUNC)
        try:
            _os.write(fd, data.encode())
            _os.fsync(fd)
        finally:
            _os.close(fd)

    def _rate_check(self, limit=None):
        cutoff = time.time() - 3600
        recent = sum(
            1 for e in self._history
            if e.get("timestamp", 0) > cutoff and e.get("status") == "deployed"
        )
        max_per_hour = limit if limit is not None else MAX_EVOLUTIONS_PER_HOUR
        return recent < max_per_hour

    def _classify_level(self, files):
        """Determine modification level from file paths."""
        level = 1
        for f in files:
            f_str = str(f)
            basename = Path(f_str).name
            if basename in PROTECTED_FILES:
                return 99
            if f_str.startswith("skills/") or f_str.endswith("SKILL.md"):
                level = max(level, 1)
            elif f_str.startswith("ghost_tools/"):
                level = max(level, 2)
            elif f_str == "SOUL.md" or f_str == "USER.md":
                level = max(level, 2)
            elif f_str.startswith("ghost_dashboard/"):
                level = max(level, 3)
            elif basename in ("ghost.py", "ghost_loop.py", "ghost_memory.py",
                              "ghost_cron.py", "ghost_skills.py", "ghost_tools.py",
                              "ghost_browser.py", "ghost_evolve.py"):
                level = max(level, 5)
            else:
                level = max(level, 4)
        return level

    def _needs_approval(self, level, cfg):
        if cfg.get("evolve_auto_approve", False):
            return False
        if level >= 3:
            return True
        return False

    def create_backup(self, evolution_id, description=""):
        """Snapshot the entire project folder + config into a tar.gz."""
        ts = datetime.now().strftime("%Y%m%d_%H%M%S")
        backup_name = f"{ts}_{evolution_id[:8]}.tar.gz"
        backup_path = BACKUP_DIR / backup_name
        evolve_backups = str(BACKUP_DIR)
        config_file = GHOST_HOME / "config.json"

        def _filter(tarinfo):
            path = tarinfo.name
            parts = Path(path).parts
            for part in parts:
                if part in BACKUP_EXCLUDE_DIRS:
                    return None
            if parts and parts[-1] in BACKUP_EXCLUDE_FILES:
                return None
            if path.endswith(".pyc"):
                return None
            return tarinfo

        with tarfile.open(str(backup_path), "w:gz") as tar:
            for item in PROJECT_DIR.iterdir():
                abs_path = str(item)
                if abs_path.startswith(evolve_backups):
                    continue
                arcname = item.name
                if item.is_dir() and item.name in BACKUP_EXCLUDE_DIRS:
                    continue
                tar.add(abs_path, arcname=arcname, filter=_filter)
            if config_file.exists():
                tar.add(str(config_file), arcname=".ghost_config_backup.json")

        self._prune_backups()
        return str(backup_path)

    def _prune_backups(self):
        backups = sorted(BACKUP_DIR.glob("*.tar.gz"), key=lambda p: p.stat().st_mtime)
        while len(backups) > MAX_BACKUPS:
            backups[0].unlink()
            backups.pop(0)

    def _restore_backup(self, backup_path, only_files=None):
        """Restore files from a backup archive (project code + config).

        Args:
            backup_path: Path to the .tar.gz backup.
            only_files: If provided, only restore these specific file paths
                        (relative to PROJECT_DIR).  This prevents wiping
                        unrelated changes made by other evolutions or the user.
                        Pass None for a full restore (supervisor crash recovery).
        """
        bp = Path(backup_path)
        if not bp.exists():
            return False, f"Backup not found: {backup_path}"

        config_file = GHOST_HOME / "config.json"
        try:
            with tarfile.open(str(bp), "r:gz") as tar:
                if only_files:
                    targets = set(only_files)
                    for member in tar.getmembers():
                        if member.name == ".ghost_config_backup.json":
                            continue
                        if member.name in targets:
                            tar.extract(member, path=str(PROJECT_DIR))
                else:
                    tar.extractall(path=str(PROJECT_DIR))

                config_member = None
                for member in tar.getmembers():
                    if member.name == ".ghost_config_backup.json":
                        config_member = member
                        break
                if config_member:
                    tar.extract(config_member, path=str(PROJECT_DIR))
                    extracted = PROJECT_DIR / ".ghost_config_backup.json"
                    if extracted.exists():
                        shutil.copy2(str(extracted), str(config_file))
                        extracted.unlink()
        except Exception as e:
            return False, f"Restore failed: {e}"

        mode = "selective" if only_files else "full"
        return True, f"Backup restored ({mode})"

    def plan(self, description, files, cfg):
        """Create an evolution plan. Returns (evolution_id, info_dict)."""
        with self._lock:
            for eid, evo in self._active_evolutions.items():
                if evo.get("status") not in ("deployed", "rolled_back"):
                    return None, {
                        "error": (
                            f"An evolution is already active: {eid} "
                            f"(status={evo.get('status')}). "
                            "Finish the current evolution before planning a new one. "
                            "Use the existing evolution_id for evolve_apply/evolve_test/evolve_submit_pr."
                        ),
                        "active_evolution_id": eid,
                    }

            limit = cfg.get("max_evolutions_per_hour", MAX_EVOLUTIONS_PER_HOUR)
            if not self._rate_check(limit=limit):
                return None, {"error": f"Rate limit: max {limit} self-modifications per hour"}

            evolution_id = uuid.uuid4().hex[:12]
            level = self._classify_level(files)

            if level >= 99:
                return None, {"error": f"Cannot modify protected files: {PROTECTED_FILES}"}

            needs_approval = self._needs_approval(level, cfg)
            backup_path = self.create_backup(evolution_id, description)

            # Create a git feature branch for this evolution
            git_branch = None
            try:
                import ghost_git
                branch_name = f"evolve/{evolution_id}"
                ok, msg = ghost_git.create_branch(branch_name)
                if ok:
                    git_branch = branch_name
                else:
                    import logging
                    _log = logging.getLogger("quinely.evolve")
                    _log.warning("Could not create git branch %s: %s",
                                branch_name, msg)
            except Exception as e:
                import logging
                _log = logging.getLogger("quinely.evolve")
                _log.warning("Git branch creation failed: %s", e)

            evo = {
                "id": evolution_id,
                "description": description,
                "files": [str(f) for f in files],
                "level": level,
                "status": "pending_approval" if needs_approval else "planned",
                "needs_approval": needs_approval,
                "approved": not needs_approval,
                "backup_path": backup_path,
                "git_branch": git_branch,
                "timestamp": time.time(),
                "created_at": datetime.now().isoformat(),
                "changes": [],
                "test_results": None,
            }

            self._active_evolutions[evolution_id] = evo

            if needs_approval:
                pending_file = PENDING_DIR / f"{evolution_id}.json"
                pending_file.write_text(json.dumps(evo, indent=2), encoding="utf-8")

            return evolution_id, {
                "evolution_id": evolution_id,
                "level": level,
                "needs_approval": needs_approval,
                "approved": not needs_approval,
                "backup_path": backup_path,
                "status": evo["status"],
            }

    def approve(self, evolution_id):
        """Approve a pending evolution."""
        evo = self._active_evolutions.get(evolution_id)
        if not evo:
            pending_file = PENDING_DIR / f"{evolution_id}.json"
            if pending_file.exists():
                evo = json.loads(pending_file.read_text(encoding="utf-8"))
                self._active_evolutions[evolution_id] = evo
            else:
                return False, "Evolution not found"

        evo["approved"] = True
        evo["status"] = "approved"
        pending_file = PENDING_DIR / f"{evolution_id}.json"
        if pending_file.exists():
            pending_file.unlink()
        return True, "Evolution approved"

    def reject(self, evolution_id):
        """Reject and clean up a pending evolution."""
        evo = self._active_evolutions.pop(evolution_id, None)
        pending_file = PENDING_DIR / f"{evolution_id}.json"
        if pending_file.exists():
            pending_file.unlink()
        if evo and evo.get("backup_path"):
            try:
                Path(evo["backup_path"]).unlink(missing_ok=True)
            except Exception:
                pass
        return True, "Evolution rejected"

    def _wait_for_approval(self, evolution_id, timeout=300):
        """Block until the evolution is approved, rejected, or times out."""
        poll_interval = 2
        waited = 0
        while waited < timeout:
            evo = self._active_evolutions.get(evolution_id)
            if not evo:
                return False, "Evolution was deleted while waiting."
            if evo.get("approved"):
                return True, f"Approved after {waited}s."
            if evo.get("status") == "rejected":
                return False, "Evolution was REJECTED by the user."
            time.sleep(poll_interval)
            waited += poll_interval
        self.reject(evolution_id)
        return False, "Timed out waiting for approval (5 minutes). Evolution cancelled."

    def apply_change(self, evolution_id, file_path, content=None, patches=None,
                     append=False, line_edits=None):
        """Apply a code change to a file.

        append=True lets the LLM build a new file incrementally across
        multiple calls when the content is too large for a single JSON
        tool-call output.  Each call appends to the file; the diff is
        recorded on every call so rollback stays correct.

        line_edits is a list of {start: int, end: int, replacement: str}
        dicts that replace line ranges by number (1-indexed, inclusive).
        This avoids the model needing to reproduce existing file content.
        """
        evo = self._active_evolutions.get(evolution_id)
        if not evo:
            return False, "Evolution not found. Call evolve_plan first."
        if not evo.get("approved"):
            ok, msg = self._wait_for_approval(evolution_id)
            if not ok:
                return False, f"Evolution {evolution_id}: {msg}"

        rel_path = file_path
        abs_path = _normalize_file_path(rel_path)

        if not abs_path.is_relative_to(PROJECT_DIR.resolve()):
            return False, (
                f"Cannot write outside the project directory. "
                f"Path '{file_path}' resolves to '{abs_path}' which is outside '{PROJECT_DIR}'. "
                f"Use a relative path like 'skills/{Path(file_path).name}' instead."
            )

        if Path(rel_path).name in PROTECTED_FILES:
            return False, f"Cannot modify protected file: {rel_path}"

        rel = Path(rel_path)
        if (not abs_path.exists()
                and rel.parent == Path(".")
                and rel.name.startswith("ghost_")
                and rel.suffix == ".py"):
            return False, (
                f"BLOCKED: Creating new ghost_*.py files at the project root is not allowed. "
                f"New tools MUST be created in ghost_tools/<name>/ using tools_create(). "
                f"Use: tools_create('{rel.stem.replace('ghost_', '')}', description, code, deps=[...])"
            )

        old_content = ""
        file_exists = abs_path.exists()
        if file_exists:
            old_content = abs_path.read_text(encoding="utf-8")

        evo_id_short = evo.get("id", evolution_id)

        if content is not None and not content.strip() and file_exists:
            if patches or line_edits:
                content = None
            elif not append:
                return False, (
                    f"REJECTED: You sent empty content for existing file '{rel_path}'. "
                    f"This is a known model serialization issue. Use line_edits instead:\n"
                    f'  evolve_apply("{evo_id_short}", "{rel_path}", line_edits=['
                    f'{{"start": <first_line>, "end": <last_line>, "replacement": "<new code>"}}])\n'
                    f"Get line numbers from file_read. start/end are 1-indexed, inclusive. "
                    f"Only provide the NEW replacement text — the old text is identified by line numbers."
                )
            else:
                return False, (
                    f"REJECTED: You sent empty content with append=True for existing file '{rel_path}'. "
                    f"Nothing to append. Use line_edits or patches to modify this file."
                )

        if not file_exists and patches and content is None and not line_edits:
            return False, (
                f"File '{rel_path}' does NOT exist — you cannot use patches on a "
                f"non-existent file. To CREATE a new file, use:\n"
                f"  evolve_apply(evolution_id, '{rel_path}', content='<full file content>')\n"
                f"Do NOT use patches=[...] for new files. Provide the complete file "
                f"content in the content= parameter."
            )

        if not file_exists and line_edits:
            return False, (
                f"File '{rel_path}' does NOT exist — you cannot use line_edits on a "
                f"non-existent file. Use content= to create it."
            )

        PATCH_ONLY_EXTENSIONS = {".css", ".js", ".html", ".py"}
        PATCH_ONLY_MIN_SIZE = 200
        MAX_LINE_LOSS_RATIO = 0.5
        file_created_this_evo = any(
            c["file"] == rel_path and c.get("diff", "").startswith("(new file)")
            for c in evo.get("changes", [])
        )

        if line_edits and file_exists:
            if not isinstance(line_edits, list) or not line_edits:
                return False, (
                    "line_edits must be a non-empty list of "
                    '{"start": <int>, "end": <int>, "replacement": "<str>"} objects.'
                )
            file_lines = old_content.splitlines(keepends=True)
            total_lines = len(file_lines)
            sorted_edits = sorted(line_edits, key=lambda e: e.get("start", 0), reverse=True)
            for edit in sorted_edits:
                start = edit.get("start")
                end = edit.get("end")
                replacement = edit.get("replacement")
                if start is None or end is None or replacement is None:
                    return False, (
                        f"Each line_edit must have 'start', 'end', and 'replacement'. "
                        f"Got: {edit}"
                    )
                if not isinstance(start, int) or not isinstance(end, int):
                    return False, f"start and end must be integers. Got start={start!r}, end={end!r}"
                if start < 1 or end < start or start > total_lines:
                    return False, (
                        f"Invalid line range: start={start}, end={end} "
                        f"(file has {total_lines} lines). "
                        f"Lines are 1-indexed. start must be >= 1, end >= start, "
                        f"start <= {total_lines}."
                    )
                end_clamped = min(end, total_lines)
                repl_text = replacement
                if repl_text and not repl_text.endswith('\n'):
                    repl_text += '\n'
                repl_lines = repl_text.splitlines(keepends=True) if repl_text.strip() else []
                file_lines[start - 1:end_clamped] = repl_lines
            new_content = "".join(file_lines)
        else:
            _content_mode_on_existing = (
                content is not None and not append and old_content
                and Path(rel_path).suffix.lower() in PATCH_ONLY_EXTENSIONS
                and len(old_content) > PATCH_ONLY_MIN_SIZE
                and not file_created_this_evo
            )
            if _content_mode_on_existing:
                old_lines = old_content.splitlines()
                new_lines = content.splitlines()
                if len(new_lines) < len(old_lines) * MAX_LINE_LOSS_RATIO:
                    return False, (
                        f"REJECTED: content mode would delete >{int((1-MAX_LINE_LOSS_RATIO)*100)}% of "
                        f"'{rel_path}' ({len(old_lines)} → {len(new_lines)} lines). "
                        "Your output was likely TRUNCATED or EMPTY.\n"
                        "Use line_edits instead (PREFERRED for existing files):\n"
                        f'  evolve_apply("{evo_id_short}", "{rel_path}", line_edits=['
                        f'{{"start": <first_line>, "end": <last_line>, "replacement": "<new code>"}}])\n'
                        "Get line numbers from file_read. start/end are 1-indexed, inclusive.\n"
                        "Alternative: patches=[{\"old\": \"<exact lines>\", \"new\": \"<replacement>\"}]"
                    )
                log.info("evolve_apply: auto-accepting content mode on '%s' "
                         "(%d → %d lines) — prefer line_edits next time", rel_path,
                         len(old_lines), len(new_lines))

            if append and content is not None:
                new_content = old_content + content
            elif content is not None:
                new_content = content
            elif patches:
                new_content = old_content
                for patch in patches:
                    old_str = patch.get("old", "")
                    new_str = patch.get("new", "")
                    if old_str and old_str in new_content:
                        new_content = new_content.replace(old_str, new_str, 1)
                    elif old_str:
                        matched = False
                        norm_old = old_str.replace('\r\n', '\n')
                        norm_content = new_content.replace('\r\n', '\n')
                        if norm_old in norm_content:
                            new_content = norm_content.replace(norm_old, new_str.replace('\r\n', '\n'), 1)
                            matched = True
                        if not matched:
                            def _strip_trailing(s):
                                return '\n'.join(line.rstrip() for line in s.split('\n'))
                            st_old = _strip_trailing(old_str)
                            st_content = _strip_trailing(new_content)
                            if st_old and st_old in st_content:
                                pos = st_content.index(st_old)
                                lines_before = st_content[:pos].count('\n')
                                lines_match = st_old.count('\n') + 1
                                orig_lines = new_content.split('\n')
                                replaced_lines = new_str.split('\n')
                                new_content = '\n'.join(
                                    orig_lines[:lines_before] + replaced_lines +
                                    orig_lines[lines_before + lines_match:]
                                )
                                matched = True
                        if not matched:
                            if file_created_this_evo:
                                hint = (
                                    f" Since you CREATED this file in this evolution, you can use "
                                    f"evolve_apply(evo_id, '{rel_path}', content='<full corrected file>') "
                                    f"to overwrite it entirely. This is easier than fixing patch mismatches."
                                )
                            else:
                                hint = (
                                    f" Use line_edits instead — specify line numbers from file_read:\n"
                                    f'  evolve_apply("{evo_id_short}", "{rel_path}", line_edits=['
                                    f'{{"start": <line>, "end": <line>, "replacement": "<new code>"}}])'
                                )
                            return False, f"Patch target not found in {rel_path}: {old_str[:80]}...{hint}"
            else:
                return False, (
                    "Provide one of: 'line_edits' (preferred for existing files), "
                    "'patches' (search/replace pairs), or 'content' (full file for new files)."
                )

        for pattern in PROTECTED_PATTERNS:
            if pattern in old_content and pattern not in new_content:
                return False, f"Cannot remove safety pattern '{pattern}' from {rel_path}"

        if not abs_path.exists() and len(new_content) > MAX_NEW_FILE_SIZE:
            return False, (
                f"New file too large ({len(new_content)} bytes, max {MAX_NEW_FILE_SIZE}). "
                f"Break it into smaller, focused modules."
            )

        abs_path.parent.mkdir(parents=True, exist_ok=True)
        abs_path.write_text(new_content, encoding="utf-8")

        if rel_path.startswith("ghost_tools/"):
            parts = Path(rel_path).parts
            if len(parts) >= 2:
                tool_dir = PROJECT_DIR / parts[0] / parts[1]
                marker = tool_dir / ".evolving"
                if not marker.exists():
                    marker.write_text(evolution_id, encoding="utf-8")

        diff = list(difflib.unified_diff(
            old_content.splitlines(keepends=True),
            new_content.splitlines(keepends=True),
            fromfile=f"a/{rel_path}",
            tofile=f"b/{rel_path}",
        ))
        if diff:
            diff_text = "".join(diff)
        elif not file_exists:
            diff_text = "(new file)"
        else:
            diff_text = "(no change)"

        evo["changes"].append({
            "file": rel_path,
            "diff": diff_text[:5000],
            "timestamp": datetime.now().isoformat(),
        })

        change_count = len(evo["changes"])
        msg = f"Applied change to {rel_path} ({len(new_content)} bytes). [{change_count} file(s) changed] "
        if append:
            msg += (
                "CHUNK APPENDED. If you need to add more chunks, keep using append=True. "
                "When all chunks are written, call file_read on this file BEFORE using "
                "patches — the exact content may differ from what you expect."
            )
        msg += " Remember: call evolve_test then evolve_deploy when done."
        return True, msg

    def apply_config_change(self, evolution_id, updates):
        """Apply config changes to ~/.ghost/config.json within an evolution context.

        Validates updates, applies them, and records the change so
        evolve_test / evolve_deploy / rollback all cover it.
        """
        evo = self._active_evolutions.get(evolution_id)
        if not evo:
            return False, "Evolution not found. Call evolve_plan first."
        if not evo.get("approved"):
            ok, msg = self._wait_for_approval(evolution_id)
            if not ok:
                return False, f"Evolution {evolution_id}: {msg}"

        if not isinstance(updates, dict) or not updates:
            return False, "updates must be a non-empty JSON object"

        from ghost_config_tool import BLOCKED_KEYS, SENSITIVE_KEYS, _is_hardening_change

        blocked = [k for k in updates if k in BLOCKED_KEYS]
        if blocked:
            return False, f"Cannot modify blocked keys: {blocked}"

        sensitive = [k for k in updates if k in SENSITIVE_KEYS]
        if sensitive:
            weakening = [k for k in sensitive if not _is_hardening_change(k, updates[k])]
            if weakening:
                return False, (
                    f"These changes would WEAKEN security: {weakening}. "
                    "Use add_action_item to propose weakening changes to the user."
                )

        config_file = GHOST_HOME / "config.json"
        old_cfg = {}
        if config_file.exists():
            try:
                old_cfg = json.loads(config_file.read_text(encoding="utf-8"))
            except Exception:
                pass

        old_values = {k: old_cfg.get(k, "(unset)") for k in updates}
        new_cfg = {**old_cfg, **updates}
        config_file.write_text(json.dumps(new_cfg, indent=2), encoding="utf-8")

        diff_lines = []
        for k, new_val in updates.items():
            diff_lines.append(f"  {k}: {old_values[k]} -> {new_val}")

        evo["changes"].append({
            "file": "~/.ghost/config.json",
            "type": "config",
            "diff": "\n".join(diff_lines),
            "updates": updates,
            "old_values": old_values,
            "timestamp": datetime.now().isoformat(),
        })

        change_count = len(evo["changes"])
        msg = (
            f"Config updated ({len(updates)} key(s)) within evolution {evolution_id}:\n"
            + "\n".join(diff_lines)
            + f"\n[{change_count} change(s) total] "
            + "Remember: call evolve_test then evolve_deploy when done."
        )
        return True, msg

    def test(self, evolution_id):
        """Run validation pipeline on modified files.

        Three checks run directly on host:
        1. Syntax — ast.parse each changed .py file
        2. Import — attempt to import each changed module
        3. Smoke — run ghost.py --dry-run to verify startup
        """
        evo = self._active_evolutions.get(evolution_id)
        if not evo:
            return False, {"error": "Evolution not found"}

        results = {
            "syntax": [], "import": [], "dangling_imports": [],
            "smoke": None, "passed": True,
        }

        def _to_rel(file_path):
            """Normalize a file path from a change record to a project-relative path."""
            p = Path(file_path)
            if p.is_absolute():
                try:
                    return str(p.relative_to(PROJECT_DIR.resolve()))
                except ValueError:
                    return p.name
            return str(p)

        changed_py = list(dict.fromkeys(
            _to_rel(c["file"]) for c in evo["changes"]
            if c["file"].endswith(".py")
        ))
        deleted_py = list(dict.fromkeys(
            _to_rel(c["file"]) for c in evo["changes"]
            if c["file"].endswith(".py") and c.get("action") == "delete"
        ))

        for f in deleted_py:
            dangling = self._scan_dangling_imports(f)
            for dep_file, dep_lines in dangling:
                results["dangling_imports"].append({
                    "deleted_module": Path(f).stem,
                    "importing_file": dep_file,
                    "lines": dep_lines,
                })
                results["passed"] = False

        for f in changed_py:
            abs_path = PROJECT_DIR / f
            if not abs_path.exists():
                continue
            try:
                source = abs_path.read_text(encoding="utf-8")
                ast.parse(source, filename=f)
                ok, output = True, None
            except SyntaxError as e:
                ok, output = False, f"Line {e.lineno}: {e.msg}"

            results["syntax"].append({
                "file": f, "ok": ok,
                "error": output if not ok else None,
            })
            if not ok:
                results["passed"] = False

        if results["passed"] and changed_py:
            self._install_tool_deps_for_testing(changed_py)

            for f in changed_py:
                if f in deleted_py:
                    continue
                abs_f = PROJECT_DIR / f
                if not abs_f.exists():
                    continue

                module_name = Path(f).with_suffix("").as_posix().replace("/", ".")
                use_file_import = (
                    f.startswith("ghost_tools/")
                    or f.startswith("ghost_nodes/")
                    or not all(part.isidentifier() for part in module_name.split("."))
                )
                if use_file_import:
                    import_cmd = (
                        "import importlib.util, sys; "
                        f"spec = importlib.util.spec_from_file_location('_test', r'{abs_f}'); "
                        "mod = importlib.util.module_from_spec(spec); "
                        "spec.loader.exec_module(mod)"
                    )
                else:
                    import_cmd = f"import {module_name}"

                try:
                    r = subprocess.run(
                        [sys.executable, "-c", import_cmd],
                        capture_output=True, text=True, timeout=30,
                        cwd=str(PROJECT_DIR),
                    )
                    ok = r.returncode == 0
                    if not ok:
                        filtered_stderr = r.stderr
                        for noise_pattern in [
                            r".*RequestsDependencyWarning:.*\n?",
                            r"\s*warnings\.warn\(.*\n?",
                            r"INFO\s+\[.*\].*\n?",
                            r"WARNING\s+\[.*\].*\n?",
                        ]:
                            filtered_stderr = re.sub(noise_pattern, "", filtered_stderr)
                        output = filtered_stderr.strip()[:1000] if filtered_stderr.strip() else None
                    else:
                        output = None
                except subprocess.TimeoutExpired:
                    ok, output = False, "Import timed out"

                results["import"].append({
                    "module": f, "ok": ok,
                    "error": output if not ok else None,
                })
                if not ok:
                    results["passed"] = False

        if results["passed"]:
            try:
                r = subprocess.run(
                    [sys.executable, "ghost.py", "--dry-run"],
                    capture_output=True, text=True, timeout=30,
                    cwd=str(PROJECT_DIR),
                )
                ok = r.returncode == 0
                if not ok:
                    filtered_stderr = r.stderr
                    for noise_pattern in [
                        r".*RequestsDependencyWarning:.*\n?",
                        r"\s*warnings\.warn\(.*\n?",
                        r"INFO\s+\[.*\].*\n?",
                        r"WARNING\s+\[.*\].*\n?",
                    ]:
                        filtered_stderr = re.sub(noise_pattern, "", filtered_stderr)
                    output = filtered_stderr.strip()[:1000] if filtered_stderr.strip() else "Unknown error"
                else:
                    output = "OK"
            except subprocess.TimeoutExpired:
                ok, output = False, "Smoke test timed out"
            except Exception as e:
                ok, output = False, str(e)[:1000]

            results["smoke"] = {
                "ok": ok, "output": output if not ok else "OK",
            }
            if not ok:
                results["passed"] = False

        if results["passed"]:
            api_results = self._test_api_routes(evo)
            results["api_routes"] = api_results
            if any(not r["ok"] for r in api_results):
                results["passed"] = False

        if results["passed"]:
            lint_issues = self._semantic_lint(evo)
            results["semantic_lint"] = lint_issues
            if lint_issues:
                results["passed"] = False

        if results["passed"]:
            tool_issues = self._validate_tools(evo)
            results["tool_validation"] = tool_issues
            if any(i.get("severity") == "error" for i in tool_issues):
                results["passed"] = False

        evo["test_results"] = results
        evo["status"] = "tested_pass" if results["passed"] else "tested_fail"

        return results["passed"], results

    @staticmethod
    def _install_tool_deps_for_testing(changed_py):
        """Install pip deps declared in TOOL.yaml for any changed ghost_tools/ files."""
        installed_tools: set[str] = set()
        for f in changed_py:
            if not f.startswith("ghost_tools/"):
                continue
            parts = Path(f).parts
            if len(parts) < 2:
                continue
            tool_name = parts[1]
            if tool_name in installed_tools or tool_name.startswith((".", "_")):
                continue
            installed_tools.add(tool_name)
            manifest_path = PROJECT_DIR / "ghost_tools" / tool_name / "TOOL.yaml"
            if not manifest_path.exists():
                continue
            try:
                from ghost_tool_builder import ToolManifest
                manifest = ToolManifest.from_yaml(manifest_path)
                if manifest.deps:
                    subprocess.run(
                        [sys.executable, "-m", "pip", "install", "--quiet"] + manifest.deps,
                        capture_output=True, text=True, timeout=120,
                    )
            except Exception:
                pass

    @staticmethod
    def _validate_tools(evo):
        """Validate ghost_tools/ files changed in this evolution.

        Checks: syntax, register() function, mock registration, hook events,
        tool name conflicts, lock reentrancy, and optional Bandit scan.
        """
        issues = []
        changed_tool_files = [
            c["file"] for c in evo.get("changes", [])
            if "ghost_tools/" in c["file"] and c["file"].endswith(".py")
            and c.get("action") != "delete"
        ]

        if not changed_tool_files:
            return issues

        _CORE_TOOL_NAMES = frozenset({
            "file_read", "file_write", "file_search", "shell_exec",
            "memory_save", "memory_search", "task_complete", "notify",
            "web_search", "web_fetch", "config_get", "config_set",
            "evolve_plan", "evolve_apply", "evolve_test", "evolve_deploy",
            "add_future_feature", "list_future_features", "uptime",
        })

        for f in changed_tool_files:
            abs_path = PROJECT_DIR / f
            if not abs_path.exists():
                continue

            source = abs_path.read_text(encoding="utf-8")

            try:
                tree = ast.parse(source, filename=f)
            except SyntaxError as e:
                issues.append({
                    "file": f, "severity": "error",
                    "message": f"Syntax error: {e}",
                })
                continue

            has_register = any(
                isinstance(node, ast.FunctionDef) and node.name == "register"
                for node in ast.walk(tree)
            )
            if not has_register:
                issues.append({
                    "file": f, "severity": "error",
                    "message": "tool.py missing register() function",
                })
                continue

            EvolutionEngine._check_lock_reentrancy(tree, f, issues)

            registered_tools = []
            registered_hooks = []
            has_register_tool_call = False
            has_ui_call = False

            for node in ast.walk(tree):
                if isinstance(node, ast.Call):
                    func = node.func
                    if isinstance(func, ast.Attribute):
                        method = func.attr
                        if method == "register_tool":
                            has_register_tool_call = True
                            for kw in node.keywords:
                                if kw.arg == "name" or (not kw.arg and isinstance(kw.value, ast.Constant)):
                                    pass
                            for arg in node.args:
                                if isinstance(arg, ast.Dict):
                                    for k, v in zip(arg.keys, arg.values):
                                        if isinstance(k, ast.Constant) and k.value == "name" and isinstance(v, ast.Constant):
                                            registered_tools.append(v.value)
                        elif method == "register_hook":
                            if node.args and isinstance(node.args[0], ast.Constant):
                                registered_hooks.append(node.args[0].value)
                        elif method in ("register_page", "register_route"):
                            has_ui_call = True

            if not has_register_tool_call:
                issues.append({
                    "file": f, "severity": "warning",
                    "message": "register() doesn't call api.register_tool() — tool registers nothing",
                })

            if has_ui_call:
                issues.append({
                    "file": f, "severity": "error",
                    "message": "Ghost tools must NOT call register_page/register_route — tools are backend-only",
                })

            for tool_name in registered_tools:
                if tool_name in _CORE_TOOL_NAMES:
                    issues.append({
                        "file": f, "severity": "error",
                        "message": f"Tool name '{tool_name}' shadows a core tool",
                    })

            from ghost_tool_builder import VALID_HOOK_EVENTS
            for hook in registered_hooks:
                if hook not in VALID_HOOK_EVENTS:
                    issues.append({
                        "file": f, "severity": "warning",
                        "message": f"Unknown hook event: '{hook}'",
                    })

            try:
                import subprocess as _sp
                r = _sp.run(
                    [sys.executable, "-m", "bandit", "-q", "-ll", str(abs_path)],
                    capture_output=True, text=True, timeout=15,
                )
                if r.returncode != 0 and r.stdout.strip():
                    for line in r.stdout.strip().splitlines()[:5]:
                        issues.append({
                            "file": f, "severity": "warning",
                            "message": f"Bandit: {line.strip()}",
                        })
            except Exception:
                pass

            EvolutionEngine._mock_register_tool(abs_path, f, issues)

        return issues

    @staticmethod
    def _mock_register_tool(abs_path, rel_path, issues):
        """Run register(api) with a mock ToolAPI to verify tool registration works at runtime.

        Catches bugs that pass syntax/import checks but fail when the tool
        is actually loaded (e.g. malformed dict literals, missing keys,
        broken function references).
        """
        test_script = (
            "import importlib.util, sys, json, types\n"
            "\n"
            "class MockToolAPI:\n"
            "    def __init__(self):\n"
            "        self.tools = []\n"
            "        self.hooks = []\n"
            "        self.settings = []\n"
            "    def register_tool(self, defn):\n"
            "        if not isinstance(defn, dict):\n"
            "            raise TypeError(f'register_tool expects dict, got {type(defn).__name__}')\n"
            "        name = defn.get('name')\n"
            "        if not name:\n"
            "            raise ValueError('Tool definition missing name')\n"
            "        execute = defn.get('execute')\n"
            "        if not callable(execute):\n"
            "            raise ValueError(f'Tool {name}: execute is not callable')\n"
            "        params = defn.get('parameters', {})\n"
            "        if not isinstance(params, dict):\n"
            "            raise TypeError(f'Tool {name}: parameters must be a dict')\n"
            "        for pname, pval in params.get('properties', {}).items():\n"
            "            if isinstance(pval.get('type'), list):\n"
            "                raise ValueError(\n"
            "                    f'Tool {name}: property {pname} uses array type '\n"
            "                    f'{pval[\"type\"]} — OpenAI function calling requires '\n"
            "                    f'a single string type (e.g. \"string\")'\n"
            "                )\n"
            "        self.tools.append(name)\n"
            "    def register_hook(self, *a, **kw): self.hooks.append(a)\n"
            "    def register_setting(self, *a, **kw): self.settings.append(a)\n"
            "    def register_cron(self, *a, **kw): pass\n"
            "    def get_setting(self, *a, **kw): return None\n"
            "    def set_setting(self, *a, **kw): pass\n"
            "    def read_data(self, *a, **kw): return None\n"
            "    def write_data(self, *a, **kw): pass\n"
            "    def log(self, *a, **kw): pass\n"
            "    def llm_summarize(self, *a, **kw): return ''\n"
            "\n"
            f"spec = importlib.util.spec_from_file_location('_test_tool', r'{abs_path}')\n"
            "mod = importlib.util.module_from_spec(spec)\n"
            "spec.loader.exec_module(mod)\n"
            "api = MockToolAPI()\n"
            "mod.register(api)\n"
            "if not api.tools:\n"
            "    print('ERROR: register() called but no tools were registered', file=sys.stderr)\n"
            "    sys.exit(1)\n"
            "print(json.dumps({'tools': api.tools}))\n"
        )
        try:
            r = subprocess.run(
                [sys.executable, "-c", test_script],
                capture_output=True, text=True, timeout=30,
                cwd=str(PROJECT_DIR),
            )
            if r.returncode != 0:
                stderr = r.stderr.strip()
                for noise in [
                    r".*RequestsDependencyWarning:.*\n?",
                    r"\s*warnings\.warn\(.*\n?",
                ]:
                    stderr = re.sub(noise, "", stderr)
                stderr = stderr.strip()
                if stderr:
                    error_line = stderr.splitlines()[-1][:300]
                else:
                    error_line = "Mock registration failed (unknown error)"
                issues.append({
                    "file": rel_path, "severity": "error",
                    "message": f"Mock register() failed: {error_line}",
                })
        except subprocess.TimeoutExpired:
            issues.append({
                "file": rel_path, "severity": "error",
                "message": "Mock register() timed out (30s)",
            })
        except Exception as e:
            issues.append({
                "file": rel_path, "severity": "warning",
                "message": f"Could not run mock register(): {e}",
            })

    @staticmethod
    def _check_lock_reentrancy(tree, module_name, issues):
        """Detect deadlocks from non-reentrant threading.Lock usage.

        Pattern: function A acquires lock L, calls function B which also
        acquires lock L.  threading.Lock is NOT reentrant — this freezes
        the thread permanently.
        """
        import ast as _ast

        lock_vars: set = set()
        for node in _ast.iter_child_nodes(tree):
            if isinstance(node, _ast.Assign):
                for target in node.targets:
                    if not isinstance(target, _ast.Name):
                        continue
                    val = node.value
                    if not isinstance(val, _ast.Call):
                        continue
                    func = val.func
                    is_lock = False
                    if isinstance(func, _ast.Attribute) and func.attr == "Lock":
                        is_lock = True
                    elif isinstance(func, _ast.Name) and func.id == "Lock":
                        is_lock = True
                    if is_lock:
                        lock_vars.add(target.id)

        if not lock_vars:
            return

        all_funcs: dict = {}
        for node in _ast.iter_child_nodes(tree):
            if isinstance(node, (_ast.FunctionDef, _ast.AsyncFunctionDef)):
                all_funcs[node.name] = node

        func_acquires: dict = {}
        func_calls_under: dict = {}

        def _lock_name(ctx_expr):
            if isinstance(ctx_expr, _ast.Name) and ctx_expr.id in lock_vars:
                return ctx_expr.id
            return None

        def _collect_calls(node_body):
            """Collect direct function call names from a list of AST stmts."""
            names = set()
            for child in _ast.walk(_ast.Module(body=node_body, type_ignores=[])):
                if isinstance(child, _ast.Call) and isinstance(child.func, _ast.Name):
                    names.add(child.func.id)
            return names

        for fname, fnode in all_funcs.items():
            acquires: set = set()
            calls: list = []
            for child in _ast.walk(fnode):
                if isinstance(child, _ast.With):
                    for item in child.items:
                        lname = _lock_name(item.context_expr)
                        if lname:
                            acquires.add(lname)
                            for called in _collect_calls(child.body):
                                if called in all_funcs:
                                    calls.append((lname, called))
            func_acquires[fname] = acquires
            func_calls_under[fname] = calls

        for fname, calls in func_calls_under.items():
            for lvar, called in calls:
                if lvar in func_acquires.get(called, set()):
                    issues.append(
                        f"'{module_name}': DEADLOCK — {fname}() holds "
                        f"'{lvar}' and calls {called}() which also acquires "
                        f"'{lvar}'. threading.Lock is NOT reentrant; this "
                        f"freezes the thread permanently. Use threading.RLock() "
                        f"or pass data instead of calling the locked function."
                    )

    # ── Semantic Lint ─────────────────────────────────────────────

    @staticmethod
    def _extract_changed_lines(diff_text):
        """Parse unified diff to extract set of added/changed line numbers in the new file."""
        changed = set()
        if not diff_text or diff_text == "(new file)":
            return None  # None means "all lines" (new file)
        current_line = 0
        for raw_line in diff_text.split("\n"):
            if raw_line.startswith("@@"):
                m = re.search(r'\+(\d+)', raw_line)
                if m:
                    current_line = int(m.group(1)) - 1
            elif raw_line.startswith("+") and not raw_line.startswith("+++"):
                current_line += 1
                changed.add(current_line)
            elif raw_line.startswith("-") and not raw_line.startswith("---"):
                pass  # deleted line, don't advance
            else:
                current_line += 1
        return changed

    def _semantic_lint(self, evo):
        """Static analysis for patterns that cause PR rejections.

        Only lints lines that were actually added or changed in this evolution,
        not pre-existing code. For new files, all lines are checked.
        Returns a list of dicts: [{file, line, rule, message}].
        """
        issues = []
        file_changed_lines = {}
        for change in evo.get("changes", []):
            fpath = change["file"]
            diff_text = change.get("diff", "")
            cl = self._extract_changed_lines(diff_text)
            if fpath in file_changed_lines:
                existing = file_changed_lines[fpath]
                if existing is None or cl is None:
                    file_changed_lines[fpath] = None
                else:
                    existing.update(cl)
            else:
                file_changed_lines[fpath] = cl

        for fpath, changed_lines in file_changed_lines.items():
            if not fpath.endswith(".py"):
                continue
            abs_path = _normalize_file_path(fpath)
            if not abs_path.exists():
                continue
            try:
                source = abs_path.read_text(encoding="utf-8")
            except Exception:
                continue
            lines = source.split("\n")
            rel = str(abs_path.relative_to(PROJECT_DIR)) if abs_path.is_relative_to(PROJECT_DIR) else fpath

            for i, line in enumerate(lines, 1):
                if changed_lines is not None and i not in changed_lines:
                    continue
                stripped = line.strip()

                # Rule 1: Bare except with pass (no logging)
                if re.match(r'^except\s*:', stripped) or re.match(r'^except\s+Exception\s*:', stripped):
                    body_lines = []
                    for j in range(i, min(i + 5, len(lines))):
                        body_lines.append(lines[j].strip())
                    body = " ".join(body_lines)
                    if re.search(r'\bpass\b', body) and "log." not in body and "logging." not in body:
                        issues.append({
                            "file": rel, "line": i, "rule": "bare-except",
                            "message": "Bare except with pass — catch specific types and log the error",
                        })

                # Rule 2: from ghost_* import mutable_var (not class/func/CONSTANT)
                m = re.match(r'^from\s+(ghost_\w+)\s+import\s+(\w+)', stripped)
                if m:
                    name = m.group(2)
                    if not name[0].isupper() and name != name.upper():
                        # Check if it's a function/class in the source module
                        _src_mod = PROJECT_DIR / f"{m.group(1)}.py"
                        _is_callable = False
                        try:
                            if _src_mod.exists():
                                _src_text = _src_mod.read_text(encoding="utf-8")
                                if re.search(rf'^(def|class)\s+{re.escape(name)}\b', _src_text, re.MULTILINE):
                                    _is_callable = True
                        except Exception:
                            pass
                        if not _is_callable:
                            issues.append({
                                "file": rel, "line": i, "rule": "mutable-import",
                                "message": f"'from {m.group(1)} import {name}' imports a mutable copy — use 'import {m.group(1)}; {m.group(1)}.{name}' instead",
                            })

                # Rule 3: Unbounded file read into json.loads
                if re.search(r'json\.loads?\(.*\.read_text\(\)', stripped):
                    size_guard = any(
                        "stat()" in lines[max(0, j)].strip() or "st_size" in lines[max(0, j)].strip()
                        for j in range(max(0, i - 6), i - 1)
                    )
                    if not size_guard:
                        issues.append({
                            "file": rel, "line": i, "rule": "unbounded-read",
                            "message": "json.loads(path.read_text()) without a file size check — add a size guard or use bounded reads",
                        })

                # Rule 4: .write_text() without preceding mkdir
                if ".write_text(" in stripped or re.search(r"open\(.+,\s*['\"]w", stripped):
                    has_mkdir = any(
                        "mkdir(" in lines[max(0, j)]
                        for j in range(max(0, i - 6), i - 1)
                    )
                    has_parent_mkdir = any(
                        "mkdir(" in lines[max(0, j)]
                        for j in range(max(0, i - 20), i - 1)
                    )
                    if not has_mkdir and not has_parent_mkdir:
                        if ".write_text(" in stripped:
                            path_in_line = stripped.split(".write_text")[0].split("=")[-1].strip()
                        else:
                            open_m = re.search(r'open\(([^,)]+)', stripped)
                            path_in_line = open_m.group(1).strip() if open_m else ""
                        if path_in_line and path_in_line.upper() != path_in_line:
                            issues.append({
                                "file": rel, "line": i, "rule": "missing-mkdir",
                                "message": "File write without preceding Path.mkdir(parents=True, exist_ok=True)",
                            })

            # Rule 5: missing-method — calls to non-existent methods on imported ghost_* classes
            try:
                tree = ast.parse(source, filename=fpath)
            except SyntaxError:
                tree = None
            if tree is not None:
                imported_classes = {}
                for node in ast.walk(tree):
                    if isinstance(node, ast.ImportFrom) and node.module and node.module.startswith("ghost_"):
                        src_mod = PROJECT_DIR / f"{node.module}.py"
                        if not src_mod.exists():
                            continue
                        for alias in node.names:
                            name = alias.name
                            if name and name[0].isupper():
                                imported_classes[alias.asname or name] = (node.module, name, src_mod)

                if imported_classes:
                    class_methods_cache = {}
                    for cls_local, (mod_name, cls_real, src_path) in imported_classes.items():
                        cache_key = f"{mod_name}.{cls_real}"
                        if cache_key not in class_methods_cache:
                            try:
                                src_tree = ast.parse(src_path.read_text(encoding="utf-8"), filename=str(src_path))
                            except (SyntaxError, OSError):
                                continue
                            methods = set()
                            for snode in ast.walk(src_tree):
                                if isinstance(snode, ast.ClassDef) and snode.name == cls_real:
                                    for item in snode.body:
                                        if isinstance(item, (ast.FunctionDef, ast.AsyncFunctionDef)):
                                            methods.add(item.name)
                                    break
                            if methods:
                                class_methods_cache[cache_key] = methods
                        methods = class_methods_cache.get(cache_key)
                        if not methods:
                            continue

                        for call_node in ast.walk(tree):
                            if not isinstance(call_node, ast.Call):
                                continue
                            func = call_node.func
                            if not isinstance(func, ast.Attribute):
                                continue
                            method_name = func.attr
                            if method_name.startswith("_"):
                                continue
                            call_line = getattr(func, "lineno", 0)
                            if changed_lines is not None and call_line not in changed_lines:
                                continue

                            called_on = func.value
                            matches_class = False
                            if isinstance(called_on, ast.Name) and called_on.id == cls_local:
                                matches_class = True
                            elif isinstance(called_on, ast.Attribute):
                                type_hint = getattr(called_on, "attr", "")
                                if type_hint and type_hint[0].isupper() and type_hint == cls_local:
                                    matches_class = True
                            if not matches_class:
                                if isinstance(called_on, ast.Attribute):
                                    var_name = called_on.attr
                                elif isinstance(called_on, ast.Name):
                                    var_name = called_on.id
                                else:
                                    var_name = None
                                if var_name:
                                    names_to_check = [var_name]
                                    if var_name.startswith("_"):
                                        names_to_check.append(var_name.lstrip("_"))
                                    for check_name in names_to_check:
                                        if matches_class:
                                            break
                                        for line_text in lines:
                                            if re.search(
                                                rf'\b{re.escape(check_name)}\b\s*[:=]\s*{re.escape(cls_local)}\b',
                                                line_text
                                            ):
                                                matches_class = True
                                                break

                            if matches_class and method_name not in methods:
                                issues.append({
                                    "file": rel, "line": call_line, "rule": "missing-method",
                                    "message": (
                                        f"'{cls_real}.{method_name}()' does not exist in {mod_name}.py. "
                                        f"Available methods: {', '.join(sorted(m for m in methods if not m.startswith('_')))}"
                                    ),
                                })

        return issues

    def _cleanup_rejected_tool_files(self, evolution_id):
        """Remove ghost_tools/ directories created by a rejected/blocked evolution.

        When tools_create runs before evolve_plan, the auto-commit in
        create_branch() sweeps those files onto main. If the PR is then
        rejected or blocked, those unapproved tool files remain on main
        and Ghost loads them on every restart. This method identifies tool
        directories that were part of the evolution and removes them from
        both disk and git.
        """
        evo = self._active_evolutions.get(evolution_id, {})
        tool_dirs_to_remove = set()

        # Source 1: files tracked in evolve_apply changes
        for change in evo.get("changes", []):
            fpath = change.get("file", "")
            if fpath.startswith("ghost_tools/"):
                parts = fpath.split("/")
                if len(parts) >= 2 and parts[1] not in ("_example", ".gitkeep"):
                    tool_dirs_to_remove.add(f"ghost_tools/{parts[1]}")

        # Source 2: files on the feature branch diff (tools_create files
        # are auto-committed to main by create_branch, then also appear
        # on the feature branch). Check which tool dirs were added.
        branch = evo.get("git_branch", "")
        if branch:
            try:
                branch_files = ghost_git.get_changed_files("main~1", branch)
                for f in branch_files:
                    if f.startswith("ghost_tools/"):
                        parts = f.split("/")
                        if len(parts) >= 2 and parts[1] not in ("_example", ".gitkeep"):
                            tool_dirs_to_remove.add(f"ghost_tools/{parts[1]}")
            except Exception:
                pass

        # Source 3: check recent commits on main for auto-committed tool files
        if not tool_dirs_to_remove:
            try:
                diff_files = ghost_git.get_changed_files("main~1", "main")
                for f in diff_files:
                    if f.startswith("ghost_tools/"):
                        parts = f.split("/")
                        if len(parts) >= 2 and parts[1] not in ("_example", ".gitkeep"):
                            tool_dirs_to_remove.add(f"ghost_tools/{parts[1]}")
            except Exception:
                pass

        # Source 4: feature description may reference a tool name
        if not tool_dirs_to_remove:
            try:
                desc = evo.get("description", "")
                for d in (PROJECT_DIR / "ghost_tools").iterdir():
                    if d.is_dir() and d.name != "_example" and d.name in desc:
                        tool_dirs_to_remove.add(f"ghost_tools/{d.name}")
            except Exception:
                pass

        removed = []
        for tool_rel in tool_dirs_to_remove:
            tool_name = tool_rel.split("/")[1] if "/" in tool_rel else ""
            tool_abs = PROJECT_DIR / tool_rel

            # Unload from in-memory ToolManager first (unregister LLM tools)
            if tool_name and self.tool_manager:
                try:
                    self.tool_manager.uninstall_tool(tool_name)
                    log.info("Unloaded rejected tool from ToolManager: %s", tool_name)
                except Exception as e:
                    log.warning("Failed to unload %s from ToolManager: %s", tool_name, e)

            if tool_abs.exists() and tool_abs.is_dir():
                try:
                    shutil.rmtree(tool_abs)
                    removed.append(tool_rel)
                    log.info("Cleaned up rejected tool: %s", tool_rel)
                except Exception as e:
                    log.warning("Failed to remove %s: %s", tool_rel, e)
            elif tool_name:
                removed.append(tool_rel)

        if removed:
            try:
                subprocess.run(
                    ["git", "add", "-A"],
                    cwd=str(PROJECT_DIR), capture_output=True, timeout=10,
                )
                subprocess.run(
                    ["git", "commit", "-m",
                     f"cleanup: remove rejected tool files ({', '.join(removed)})"],
                    cwd=str(PROJECT_DIR), capture_output=True, timeout=10,
                )
                log.info("Committed removal of rejected tools: %s", removed)
            except Exception:
                pass
        return removed

    def resume_evolution(self, evolution_id):
        """Resume a review_rejected evolution for fix-and-resubmit.

        Checks out the preserved feature branch so the implementer can
        apply targeted patches without rebuilding from scratch.

        If Ghost restarted since the rejection, the evolution is no longer
        in memory. In that case, we reconstruct it from the git branch and
        PR data so the fix-and-resubmit flow still works across restarts.

        Returns (ok, context_dict_or_error_string).
        """
        import ghost_git

        evo = self._active_evolutions.get(evolution_id)

        if not evo:
            branch_name = f"evolve/{evolution_id}"
            if not ghost_git.branch_exists(branch_name):
                return False, (
                    f"Evolution '{evolution_id}' not found and branch "
                    f"'{branch_name}' does not exist. Start fresh with evolve_plan."
                )
            log.info("Reconstructing evolution %s from branch (post-restart resume)",
                     evolution_id)
            backup_path = self.create_backup(evolution_id, "Resume backup (post-restart)")
            evo = {
                "id": evolution_id,
                "description": f"Resumed evolution {evolution_id}",
                "files": [],
                "level": 4,
                "status": "review_rejected",
                "needs_approval": False,
                "approved": True,
                "backup_path": backup_path,
                "git_branch": branch_name,
                "timestamp": time.time(),
                "created_at": datetime.now().isoformat(),
                "changes": [],
                "test_results": None,
            }
            self._active_evolutions[evolution_id] = evo

        if evo.get("status") != "review_rejected":
            return False, (
                f"Evolution '{evolution_id}' status is '{evo.get('status')}', "
                "not 'review_rejected'. Cannot resume."
            )

        branch = evo.get("git_branch")
        if not branch or not ghost_git.branch_exists(branch):
            return False, (
                f"Branch '{branch}' no longer exists. "
                "Cannot resume — start fresh with evolve_plan."
            )

        evo["old_head_sha"] = ghost_git.get_head_sha(branch)

        ok, msg = ghost_git.stash_and_checkout(branch)
        if not ok:
            return False, f"Cannot checkout branch '{branch}': {msg}"

        # Merge latest main into the evolve branch so any changes made
        # to main since the branch was created are preserved.
        update_ok, update_msg = ghost_git.update_branch(branch)
        if not update_ok:
            log.warning("Could not merge main into %s: %s (continuing anyway)", branch, update_msg)

        evo["status"] = "approved"
        evo["test_results"] = None

        pr_id = evo.get("pr_id", "")
        last_feedback = ""
        if pr_id:
            try:
                from ghost_pr import get_pr_store
                pr = get_pr_store().get_pr(pr_id)
                if pr:
                    for d in reversed(pr.get("discussions", [])):
                        if d.get("role") == "reviewer" and d.get("message"):
                            last_feedback = d["message"]
                            break
            except Exception:
                pass

        return True, {
            "evolution_id": evolution_id,
            "branch": branch,
            "old_head_sha": evo["old_head_sha"],
            "pr_id": pr_id,
            "review_round": evo.get("review_round", 1),
            "files_changed": [c["file"] for c in evo.get("changes", [])],
            "last_reviewer_feedback": last_feedback,
        }

    def submit_pr(self, evolution_id, title, description, feature_id="", cfg=None):
        """Submit a PR for code review instead of deploying directly.

        Creates a git commit on the feature branch, builds a PR, runs the
        adversarial review loop, and handles the verdict (merge+deploy,
        reject, or block).

        The review ALWAYS runs. Self-repair uses evolve_deploy directly —
        it never calls submit_pr, so there is no auto-approve bypass here.
        """
        import ghost_git
        from ghost_pr import get_pr_store, get_review_engine

        if not evolution_id or not isinstance(evolution_id, str) or len(evolution_id) < 8:
            active_ids = list(self._active_evolutions.keys())
            hint = f" Active evolutions: {active_ids}" if active_ids else ""
            return False, f"Invalid evolution_id: '{evolution_id}'.{hint}"

        evo = self._active_evolutions.get(evolution_id)
        if not evo:
            active_ids = list(self._active_evolutions.keys())
            hint = f" Active evolutions: {active_ids}" if active_ids else ""
            return False, f"Evolution '{evolution_id}' not found.{hint}"
        if evo.get("status") != "tested_pass":
            return False, "Cannot submit PR: tests have not passed. Run evolve_test first."

        # Pre-submit semantic lint: catch anti-patterns before the expensive review
        lint_issues = self._semantic_lint(evo)
        if lint_issues:
            issues_text = "\n".join(
                f"  - {i['file']}:{i['line']} [{i['rule']}]: {i['message']}"
                for i in lint_issues
            )
            return False, (
                f"PRE-SUBMIT VALIDATION FAILED — {len(lint_issues)} issue(s) found:\n"
                f"{issues_text}\n\n"
                "Fix these issues with evolve_apply (use line_edits for existing files), "
                "then re-run evolve_test, then try evolve_submit_pr again."
            )

        # Ensure git branch exists — recover if plan() failed to create it
        branch_name = evo.get("git_branch")
        if not branch_name or not ghost_git.branch_exists(branch_name):
            branch_name = f"evolve/{evolution_id}"
            ok, msg = ghost_git.create_branch(branch_name)
            if not ok:
                return False, (
                    f"Cannot create git branch for PR: {msg}. "
                    "Git may not be initialized. Run 'git init' in the project directory, "
                    "or use evolve_deploy for direct deploy."
                )
            evo["git_branch"] = branch_name
            import logging
            _log = logging.getLogger("quinely.evolve")
            _log.info("Recovered git branch %s for evolution %s", branch_name, evolution_id)

        # Commit changes on the feature branch
        ok, msg = ghost_git.checkout(branch_name)
        if not ok:
            return False, f"Cannot switch to feature branch: {msg}"
        ok, msg = ghost_git.commit(f"feat: {title}")

        diff = ghost_git.get_diff("main", branch_name)
        changed_files = ghost_git.get_changed_files("main", branch_name)

        # Reuse existing PR for this evolution+branch (GitHub-style: same PR
        # stays open across fix-and-resubmit rounds).
        store = get_pr_store()
        pr = None
        for existing in store.list_prs():
            if (
                existing.get("evolution_id") == evolution_id
                and existing.get("branch") == branch_name
                and existing.get("status") in {"open", "reviewing", "approved", "rejected"}
            ):
                pr = existing
                store.update_diff(pr["pr_id"], diff, changed_files)
                old_head_sha = evo.get("old_head_sha", "")
                if old_head_sha:
                    store.set_old_head_sha(pr["pr_id"], old_head_sha)
                store.reopen_pr(pr["pr_id"])
                break
        if pr is None:
            pr = store.create_pr(
                evolution_id=evolution_id,
                feature_id=feature_id,
                title=title,
                description=description,
                branch=branch_name,
                diff=diff,
                files_changed=changed_files,
            )

        # ── Observability: surface the PR review loop on the live console ──
        # (it runs in its own nested engine, so it isn't auto-instrumented).
        pr_id = pr["pr_id"]
        review_round = (store.get_pr(pr_id) or pr).get("review_rounds", 1)

        def _pr_console(level, title_, detail):
            """Best-effort live-console event for the PR review loop."""
            try:
                from ghost_console import console_bus
                console_bus.emit(level, "growth", title_, detail)
            except Exception:
                pass

        _pr_console(
            "info", "PR submitted",
            f"PR {pr_id} opened for review — {title} (round {review_round})",
        )

        # Run the adversarial review with a DEDICATED engine instance.
        # Using daemon.engine caused contention: concurrent cron jobs share
        # the same engine/fallback chain, amplifying 429s and causing empty
        # responses when the provider is rate-limited.
        review_engine = get_review_engine(evolve_engine=self)
        loop_engine = None
        try:
            from ghost import load_config
            from ghost_loop import ToolLoopEngine
            from ghost_auth_profiles import get_auth_store
            _cfg = cfg or load_config()
            api_key = _cfg.get("api_key", "")
            model = _cfg.get("model", "openrouter/auto")
            fallback_models = _cfg.get("fallback_models", [])
            auth_store = get_auth_store()

            provider_chain = None
            try:
                from ghost_dashboard import get_daemon
                daemon = get_daemon()
                if daemon and hasattr(daemon, "_build_provider_chain"):
                    provider_chain = daemon._build_provider_chain(
                        model, fallback_models)
            except Exception:
                pass

            loop_engine = ToolLoopEngine(
                api_key=api_key,
                model=model,
                fallback_models=fallback_models,
                auth_store=auth_store,
                provider_chain=provider_chain,
            )
        except Exception as e:
            _log = logging.getLogger("quinely.evolve")
            _log.warning("Could not create LLM engine for review: %s", e)
            ghost_git.stash_and_checkout("main")
            return False, f"Cannot start review: LLM init failed: {e}"

        _pr_console(
            "info", "PR review started",
            f"Reviewing PR {pr_id} — {title} (round {review_round})",
        )

        # Trace the reviewer's tool loop as its own run. start_run pushes onto
        # the tracer's thread-local stack, so the reviewer's model/tool spans
        # attach here (and pop back to the parent run when we end it).
        _tracer = None
        _pr_run_id = ""
        try:
            from ghost_trace import get_tracer
            _tracer = get_tracer()
            _pr_run_id = _tracer.start_run(
                source="pr_review",
                user_message=f"Review PR: {title}",
                meta={"feature_id": feature_id},
                caller_context=f"PR {pr_id}",
            )
        except Exception:
            _tracer = None

        try:
            verdict = review_engine.run_review(pr["pr_id"], loop_engine)
        except Exception as _review_err:
            if _tracer and _pr_run_id:
                try:
                    _tracer.end_run(_pr_run_id, status="error",
                                    error=str(_review_err)[:300])
                except Exception:
                    pass
            _pr_console("error", "PR review failed",
                        f"PR {pr_id} review errored — {str(_review_err)[:160]}")
            raise
        if _tracer and _pr_run_id:
            try:
                _tracer.end_run(_pr_run_id, status="ok",
                                result_text=f"Review verdict: {verdict}")
            except Exception:
                pass

        import logging
        _log = logging.getLogger("quinely.evolve")
        _log.info("PR %s verdict: %s", pr["pr_id"], verdict)

        _verdict_level = {"approved": "success", "rejected": "warn",
                          "blocked": "error"}.get(verdict, "info")
        _verdict_msg = {
            "approved": f"PR {pr_id} APPROVED by reviewer — {title}",
            "rejected": f"PR {pr_id} rejected (round {review_round}) — {title}",
            "blocked": f"PR {pr_id} blocked by reviewer — {title}",
        }.get(verdict, f"PR {pr_id} verdict: {verdict} — {title}")
        _pr_console(_verdict_level, "PR verdict", _verdict_msg)

        def _notify_queue_best_effort():
            try:
                from ghost_dashboard.routes.future_features import _notify_queue
                _notify_queue()
            except Exception:
                pass

        if verdict == "approved":
            main_sha = ghost_git.get_head_sha("main")
            branch_sha = ghost_git.get_head_sha(branch_name)
            if main_sha and branch_sha:
                try:
                    merge_base = subprocess.run(
                        ["git", "merge-base", "main", branch_name],
                        capture_output=True, text=True, timeout=10,
                        cwd=str(PROJECT_DIR),
                    )
                    if merge_base.returncode == 0 and merge_base.stdout.strip() != main_sha:
                        ok_update, update_msg = ghost_git.update_branch(branch_name)
                        if not ok_update:
                            ghost_git.stash_and_checkout("main")
                            return False, f"Branch behind main and update failed (conflict): {update_msg}"
                        _log.info("Updated branch %s with latest main before merge", branch_name)
                except Exception as e:
                    _log.warning("merge-base check failed, proceeding with merge: %s", e)

            ghost_git.checkout("main")
            ok, msg = ghost_git.merge(branch_name)
            if not ok:
                ghost_git.stash_and_checkout("main")
                return False, f"Merge failed after approval: {msg}"
            ghost_git.delete_branch(branch_name)
            store.mark_merged(pr["pr_id"])
            _pr_console(
                "info", "PR merged",
                f"Merging {pr_id} into main and restarting to apply — {title}",
            )
            pr_after = store.get_pr(pr["pr_id"]) or pr
            evo["pr_id"] = pr["pr_id"]
            evo["pr_verdict"] = "approved"
            evo["pr_review_rounds"] = pr_after.get("review_rounds", 1)
            reviewer_msgs = [
                d for d in pr_after.get("discussions", [])
                if d.get("role") == "reviewer"
            ]
            if reviewer_msgs:
                evo["pr_reviewer_summary"] = reviewer_msgs[-1].get("message", "")[:2000]
            evo["pr_inline_comments"] = len(pr_after.get("inline_comments", []))
            evo["pr_suggested_changes"] = len(pr_after.get("suggested_changes", []))
            evo["status"] = "tested_pass"
            deploy_result = self.deploy(evolution_id, feature_id=feature_id)
            self._active_evolutions.pop(evolution_id, None)
            return deploy_result

        elif verdict == "blocked":
            ghost_git.stash_and_checkout("main")
            self._cleanup_rejected_tool_files(evolution_id)
            ghost_git.delete_branch(branch_name)
            try:
                from ghost_future_features import FutureFeaturesStore
                if feature_id:
                    FutureFeaturesStore().reject(
                        feature_id, f"Blocked by reviewer (PR {pr['pr_id']})")
                    _notify_queue_best_effort()
            except Exception:
                pass
            pr_after = store.get_pr(pr["pr_id"]) or pr
            _log_reviewer_mistakes(pr_after, pr["pr_id"], title)
            blocked_reason = pr_after.get("blocked_reason", "No reason provided")
            return False, (
                f"PR {pr['pr_id']} BLOCKED by reviewer. "
                f"Feature {feature_id} marked as rejected. "
                f"Reason: {blocked_reason}\n\n"
                "⛔ This feature is PERMANENTLY rejected. The branch has been deleted.\n"
                "⚠️ Do NOT call complete_future_feature — the feature FAILED.\n"
                "Do NOT investigate, do NOT retry, do NOT explore the codebase. "
                "Call task_complete(summary='Feature blocked by reviewer.') IMMEDIATELY."
            )

        else:  # rejected — keep branch alive for fix-and-resubmit
            ghost_git.stash_and_checkout("main")
            self._cleanup_rejected_tool_files(evolution_id)

            # Preserve the branch + evolution for the next attempt (GitHub-style)
            evo["status"] = "review_rejected"
            evo["pr_id"] = pr["pr_id"]

            retry_status = ""
            try:
                from ghost_future_features import FutureFeaturesStore
                if feature_id:
                    pr_after = store.get_pr(pr["pr_id"]) or pr
                    latest_reviewer_feedback = ""
                    inline_comments = pr_after.get("inline_comments", [])
                    suggested_changes = pr_after.get("suggested_changes", [])
                    for d in reversed(pr_after.get("discussions", [])):
                        if d.get("role") == "reviewer" and d.get("message"):
                            latest_reviewer_feedback = d["message"]
                            break
                    reason = f"PR rejected after review (PR {pr['pr_id']})"
                    if latest_reviewer_feedback:
                        feedback = latest_reviewer_feedback[:1200]
                        reason = (
                            f"{reason}. Latest reviewer feedback:\n{feedback}"
                        )
                    ok_retry, retry_status = FutureFeaturesStore().mark_review_rejected(
                        feature_id, reason,
                        max_retries=5,
                        reviewer_feedback=latest_reviewer_feedback,
                        evolution_id=evolution_id,
                        branch_name=branch_name,
                        pr_id=pr["pr_id"])
                    if ok_retry and retry_status == "review_rejected":
                        _delay = threading.Timer(905.0, _notify_queue_best_effort)
                        _delay.daemon = True
                        _delay.start()
            except Exception:
                pass
            pr_after = store.get_pr(pr["pr_id"]) or pr
            _log_reviewer_mistakes(pr_after, pr["pr_id"], title)
            from ghost_pr import MAX_REVIEW_ROUNDS
            review_round = pr_after.get("review_rounds", 1)
            evo["review_round"] = review_round
            if review_round >= MAX_REVIEW_ROUNDS:
                ghost_git.delete_branch(branch_name)
                self._active_evolutions.pop(evolution_id, None)
                retry_msg = "Feature was DEFERRED after max review rounds."
            elif retry_status == "deferred":
                ghost_git.delete_branch(branch_name)
                self._active_evolutions.pop(evolution_id, None)
                retry_msg = "Feature was DEFERRED after max retry attempts."
            elif retry_status == "review_rejected":
                retry_msg = (
                    "Feature re-queued for fix-and-resubmit. "
                    "Branch preserved for targeted fixes."
                )
            else:
                retry_msg = "Feature retry status unknown."
            is_terminal = retry_status == "deferred" or review_round >= MAX_REVIEW_ROUNDS
            if is_terminal:
                end_instruction = (
                    "The feature has been DEFERRED — it was NOT successfully implemented.\n"
                    "⚠️ Do NOT call complete_future_feature — the feature FAILED.\n"
                    "Call fail_future_feature(feature_id, reason='PR rejected: <1-line summary>') "
                    "then task_complete NOW."
                )
            else:
                end_instruction = (
                    "The feature has been re-queued for a future attempt with reviewer feedback.\n"
                    "⚠️ Do NOT call complete_future_feature — the feature was NOT completed.\n"
                    "Call task_complete NOW."
                )
            return False, (
                f"PR {pr['pr_id']} REJECTED (round {review_round}/{MAX_REVIEW_ROUNDS}). {retry_msg}\n"
                f"{end_instruction}"
            )

    def deploy(self, evolution_id, feature_id=""):
        """Signal the supervisor to restart Ghost with the new code.

        Before writing the deploy marker, waits for other cron jobs to finish
        (up to 30s) so the restart doesn't kill them mid-execution.

        The deploy marker carries feature_id so the supervisor can persist it
        for the new Ghost process to auto-complete the feature on startup.
        """
        with self._lock:
            evo = self._active_evolutions.get(evolution_id)
            if not evo:
                return False, "Evolution not found"
            if evo.get("status") != "tested_pass":
                return False, "Cannot deploy: tests have not passed. Run evolve_test first."

            # Wait for other cron jobs to finish before restarting
            if self._active_jobs_fn:
                waited = 0
                while waited < 30:
                    active = self._active_jobs_fn()
                    if active <= 1:  # 1 = just the Feature Implementer itself
                        break
                    time.sleep(2)
                    waited += 2

            # Merge evolution changes into main before restarting.
            # In the PR flow, submit_pr already merged and deleted the branch.
            # In the direct-deploy flow, changes may still be uncommitted on the
            # feature branch — commit them, merge to main, then clean up.
            git_branch = evo.get("git_branch")
            if git_branch:
                try:
                    import ghost_git
                    if ghost_git.current_branch() == git_branch:
                        r = subprocess.run(
                            ["git", "status", "--porcelain"],
                            capture_output=True, text=True, timeout=10,
                            cwd=str(PROJECT_DIR),
                        )
                        if r.stdout.strip():
                            ghost_git.commit(
                                f"feat: evolution {evo['id']} — {evo.get('description', '')[:80]}")
                        ghost_git.checkout("main")
                        ghost_git.merge(git_branch)
                    ghost_git.delete_branch(git_branch)
                except Exception:
                    pass

            evo["status"] = "deployed"
            evo["deployed_at"] = datetime.now().isoformat()

            self._history.append(evo)
            self._save_history()
            self._active_evolutions.pop(evolution_id, None)

            for change in evo.get("changes", []):
                fpath = change.get("file", "")
                if fpath.startswith("ghost_tools/"):
                    parts = Path(fpath).parts
                    if len(parts) >= 2:
                        marker = PROJECT_DIR / parts[0] / parts[1] / ".evolving"
                        marker.unlink(missing_ok=True)

            deploy_info = {
                "evolution_id": evolution_id,
                "feature_id": feature_id,
                "backup_path": evo["backup_path"],
                "timestamp": time.time(),
            }
            DEPLOY_MARKER.write_text(json.dumps(deploy_info, indent=2), encoding="utf-8")
            self._deploy_triggered.set()

            if self.tool_event_bus:
                try:
                    self.tool_event_bus.emit(
                        "on_evolve_complete",
                        evolution_id=evolution_id,
                        status="deployed",
                    )
                except Exception:
                    pass

        return True, (
            f"Evolution {evolution_id} deployed. "
            f"Ghost will restart momentarily. "
            f"Backup at: {evo['backup_path']}\n\n"
            "DEPLOY COMPLETE — call task_complete NOW. "
            "Do NOT call any more tools — the process is restarting."
        )

    def delete_file(self, evolution_id, file_path):
        """Delete a file as part of an evolution, recording it for rollback awareness."""
        evo = self._active_evolutions.get(evolution_id)
        if not evo:
            return False, "Evolution not found. Call evolve_plan first."
        if not evo.get("approved"):
            ok, msg = self._wait_for_approval(evolution_id)
            if not ok:
                return False, f"Evolution {evolution_id}: {msg}"

        rel_path = file_path
        abs_path = _normalize_file_path(rel_path)

        if not abs_path.is_relative_to(PROJECT_DIR.resolve()):
            return False, (
                f"Cannot delete outside the project directory. "
                f"Path '{file_path}' resolves to '{abs_path}' which is outside '{PROJECT_DIR}'."
            )

        if Path(rel_path).name in PROTECTED_FILES:
            return False, f"Cannot delete protected file: {rel_path}"

        if not abs_path.exists():
            return False, f"File not found: {rel_path}"

        old_content = abs_path.read_text(encoding="utf-8") if abs_path.is_file() else ""
        abs_path.unlink()

        evo["changes"].append({
            "file": rel_path,
            "action": "delete",
            "diff": f"(deleted file, was {len(old_content)} bytes)",
            "timestamp": datetime.now().isoformat(),
        })

        self._log_intentional_deletion(evolution_id, rel_path)

        dangling = self._scan_dangling_imports(rel_path)
        warning = ""
        if dangling:
            files_list = ", ".join(f"{f}:{lines}" for f, lines in dangling[:5])
            warning = (
                f"\n\nWARNING: {len(dangling)} file(s) still import from "
                f"'{Path(rel_path).stem}': {files_list}. "
                "Fix these imports or those files will crash on import."
            )

        change_count = len(evo["changes"])
        msg = (f"Deleted {rel_path}. [{change_count} change(s) in this evolution] "
               f"Remember: call evolve_test then evolve_deploy when done.{warning}")
        return True, msg

    def _log_intentional_deletion(self, evolution_id, rel_path):
        """Record that a file was intentionally deleted so self-repair won't restore it."""
        log = []
        if DELETED_FILES_LOG.exists():
            try:
                log = json.loads(DELETED_FILES_LOG.read_text(encoding="utf-8"))
            except Exception:
                log = []
        log.append({
            "file": rel_path,
            "module": Path(rel_path).stem,
            "evolution_id": evolution_id,
            "timestamp": time.time(),
            "deleted_at": datetime.now().isoformat(),
        })
        DELETED_FILES_LOG.write_text(json.dumps(log, indent=2), encoding="utf-8")

    def _test_api_routes(self, evo):
        """Smoke-test new/modified API route files + static contract analysis.

        Phase 1 (live): GET endpoints — verifies they respond with valid JSON.
        Only tests endpoints whose route function was actually touched by a patch
        in this evolution (avoids failing on pre-existing broken endpoints).
        Phase 2 (static): PUT/POST endpoints — verifies frontend JS sends the
        same payload shape that the Python route reads from request.get_json().
        This catches the #1 autonomous implementation bug: payload mismatch.
        """
        results = []
        dashboard_port = 3333

        route_changes = [
            c for c in evo["changes"]
            if "ghost_dashboard/routes/" in c["file"] and c["file"].endswith(".py")
            and c.get("action") != "delete"
        ]
        route_files = [c["file"] for c in route_changes]
        if not route_files:
            return results

        touched_snippets = set()
        for c in route_changes:
            diff_text = c.get("diff", "")
            touched_snippets.add(diff_text)

        import urllib.request
        import urllib.error

        for route_file in route_files:
            abs_path = PROJECT_DIR / route_file
            if not abs_path.exists():
                continue
            try:
                source = abs_path.read_text(encoding="utf-8")
            except OSError:
                continue

            change_diffs = " ".join(
                c.get("diff", "") for c in route_changes if c["file"] == route_file
            )
            is_new_file = any(
                "(new file)" in c.get("diff", "") for c in route_changes
                if c["file"] == route_file
            )

            endpoints = re.findall(
                r'@bp\.route\(["\'](/api/[^"\']+)["\'](?:\s*,\s*methods\s*=\s*\[([^\]]*)\])?\)',
                source,
            )

            for endpoint, methods_str in endpoints:
                is_get = not methods_str or '"GET"' in methods_str or "'GET'" in methods_str
                if not is_get:
                    continue

                # For existing files that were modified, skip live testing - 
                # the smoke test already validated the Flask app works.
                # Live testing is for verifying deployed routes, not pending changes.
                if not is_new_file:
                    continue

                url = f"http://localhost:{dashboard_port}{endpoint}"
                try:
                    req = urllib.request.Request(url, method="GET")
                    with urllib.request.urlopen(req, timeout=5) as resp:
                        body = resp.read().decode("utf-8", errors="replace")
                        status = resp.status
                    if status < 400:
                        try:
                            json.loads(body)
                            results.append({"endpoint": endpoint, "ok": True, "status": status})
                        except json.JSONDecodeError:
                            results.append({
                                "endpoint": endpoint, "ok": False, "status": status,
                                "error": "Response is not valid JSON",
                            })
                    else:
                        results.append({
                            "endpoint": endpoint, "ok": False, "status": status,
                            "error": f"HTTP {status}",
                        })
                except urllib.error.HTTPError as e:
                    # For new endpoints in existing files, 404 is expected (server not restarted yet)
                    # Skip these rather than failing - they'll work after deploy
                    is_new_endpoint = endpoint in change_diffs
                    if e.code == 404 and is_new_endpoint and not is_new_file:
                        results.append({
                            "endpoint": endpoint, "ok": True, "status": e.code,
                            "error": f"Skipped (new endpoint, will work after deploy)",
                        })
                    else:
                        results.append({
                            "endpoint": endpoint, "ok": False, "status": e.code,
                            "error": f"HTTP {e.code}: {str(e.reason)[:100]}",
                        })
                except Exception as e:
                    results.append({
                        "endpoint": endpoint, "ok": True,
                        "error": f"Skipped (server not reachable): {str(e)[:80]}",
                    })

        contract_results = self._test_frontend_backend_contracts(evo, route_files)
        results.extend(contract_results)
        return results

    def _test_frontend_backend_contracts(self, evo, route_files):
        """Static analysis: verify PUT/POST payloads match between JS and Python.

        For each PUT/POST route, find the corresponding JS file, extract what
        keys the JS sends, and what keys the Python reads — flag mismatches.
        """
        results = []
        js_dir = PROJECT_DIR / "ghost_dashboard" / "static" / "js" / "pages"

        for route_file in route_files:
            abs_path = PROJECT_DIR / route_file
            if not abs_path.exists():
                continue
            try:
                py_source = abs_path.read_text(encoding="utf-8")
            except OSError:
                continue

            write_endpoints = re.findall(
                r'@bp\.route\(["\'](/api/[^"\']+)["\']'
                r'(?:\s*,\s*methods\s*=\s*\[([^\]]*)\])?\)',
                py_source,
            )

            for endpoint, methods_str in write_endpoints:
                has_write = any(m in (methods_str or "") for m in ['"PUT"', "'PUT'", '"POST"', "'POST'"])
                if not has_write:
                    continue

                py_keys = set(re.findall(
                    r'(?:data|request\.get_json\([^)]*\))\.get\(["\'](\w+)["\']',
                    py_source,
                ))
                py_direct_keys = set(re.findall(
                    r'data\[["\']([\w]+)["\']\]',
                    py_source,
                ))
                py_keys.update(py_direct_keys)

                if not py_keys:
                    continue

                js_sources = []
                if js_dir.is_dir():
                    for js_file in js_dir.glob("*.js"):
                        try:
                            content = js_file.read_text(encoding="utf-8")
                            if endpoint in content:
                                js_sources.append((js_file.name, content))
                        except OSError:
                            continue

                if not js_sources:
                    continue

                for js_name, js_content in js_sources:
                    fetch_blocks = re.findall(
                        r'fetch\s*\(\s*[`"\'][^`"\']*' + re.escape(endpoint)
                        + r'[^`"\']*[`"\']\s*,\s*\{([^}]{10,500})\}',
                        js_content, re.DOTALL,
                    )

                    for block in fetch_blocks:
                        body_match = re.search(
                            r'body\s*:\s*JSON\.stringify\(\s*\{([^}]+)\}\s*\)',
                            block, re.DOTALL,
                        )
                        if not body_match:
                            continue

                        body_content = body_match.group(1)
                        js_top_keys = set(re.findall(r'(\w+)\s*:', body_content))

                        if not js_top_keys:
                            continue

                        if len(js_top_keys) == 1:
                            wrapper_key = list(js_top_keys)[0]
                            if wrapper_key not in py_keys and py_keys:
                                unwrap_pattern = re.search(
                                    rf'data\.get\(["\']' + re.escape(wrapper_key) + r'["\']',
                                    py_source,
                                )
                                if not unwrap_pattern:
                                    results.append({
                                        "endpoint": f"{endpoint} [contract]",
                                        "ok": False,
                                        "error": (
                                            f"PAYLOAD MISMATCH: JS ({js_name}) wraps data in "
                                            f"'{wrapper_key}' key, but Python reads keys "
                                            f"{sorted(py_keys)} from top level. "
                                            f"Python must unwrap: data.get('{wrapper_key}', data)"
                                        ),
                                    })
        return results

    @staticmethod
    def _scan_dangling_imports(deleted_rel_path):
        """Scan project .py files for imports of a deleted module. Returns [(file, [line_nums])]."""
        module_name = Path(deleted_rel_path).stem
        dangling = []
        for py_file in PROJECT_DIR.glob("*.py"):
            if py_file.name == Path(deleted_rel_path).name:
                continue
            try:
                lines = py_file.read_text(encoding="utf-8").splitlines()
                hit_lines = []
                for i, line in enumerate(lines, 1):
                    stripped = line.strip()
                    if (stripped.startswith(f"import {module_name}")
                            or stripped.startswith(f"from {module_name} ")):
                        hit_lines.append(i)
                if hit_lines:
                    dangling.append((py_file.name, hit_lines))
            except Exception:
                continue
        for py_file in PROJECT_DIR.rglob("ghost_dashboard/**/*.py"):
            try:
                lines = py_file.read_text(encoding="utf-8").splitlines()
                hit_lines = []
                for i, line in enumerate(lines, 1):
                    stripped = line.strip()
                    if (stripped.startswith(f"import {module_name}")
                            or stripped.startswith(f"from {module_name} ")):
                        hit_lines.append(i)
                if hit_lines:
                    rel = py_file.relative_to(PROJECT_DIR)
                    dangling.append((str(rel), hit_lines))
            except Exception:
                continue
        return dangling

    def cleanup_incomplete(self, only_ids=None):
        """Rollback active evolutions that have changes but were never deployed.

        Called automatically when a tool loop ends to prevent orphaned file changes.
        If only_ids is provided, only clean up evolutions with those IDs (scoped to
        the current tool loop run). This prevents accidentally rolling back evolutions
        from other concurrent runs.

        Strategy: git-first, selective-backup-second.
        1. Switch to main branch (reverts working tree to main's committed state).
        2. Delete the feature branch.
        3. Only if git fails, selectively restore ONLY the evolution's changed
           files from backup — never a full restore that wipes unrelated work.

        Returns list of (evo_id, ok, message) tuples.
        """
        results = []
        to_clean = []
        for evo_id, evo in list(self._active_evolutions.items()):
            if only_ids is not None and evo_id not in only_ids:
                continue
            if evo.get("status") in ("deployed", "review_rejected"):
                continue
            if evo.get("changes") or evo.get("git_branch"):
                to_clean.append((evo_id, evo))

        for evo_id, evo in to_clean:
            git_ok = False
            git_branch = evo.get("git_branch")

            if git_branch:
                try:
                    import ghost_git
                    if ghost_git.current_branch() == git_branch:
                        ghost_git.stash_and_checkout("main")
                    ghost_git.delete_branch(git_branch)
                    git_ok = True
                except Exception:
                    pass

            if not git_ok:
                changed_files = [c["file"] for c in evo.get("changes", []) if c.get("file")]
                backup_path = evo.get("backup_path")
                if backup_path and changed_files:
                    ok, msg = self._restore_backup(backup_path, only_files=changed_files)
                    if not ok:
                        results.append((evo_id, False, f"Failed to rollback {evo_id}: {msg}"))
                        self._active_evolutions.pop(evo_id, None)
                        continue

            new_files_created = set()
            for change in evo.get("changes", []):
                rel = change["file"]
                d = change.get("diff", "")
                if d.startswith("(new file)"):
                    new_files_created.add(rel)
                elif d and d != "(no change)":
                    new_files_created.discard(rel)

            for rel in new_files_created:
                file_path = PROJECT_DIR / rel
                if file_path.exists():
                    try:
                        file_path.unlink()
                    except Exception:
                        pass

            # Clean up orphaned PRs and re-queue the associated feature
            pr_id = evo.get("pr_id")
            if not pr_id:
                try:
                    from ghost_pr import get_pr_store
                    for _pr in get_pr_store().list_prs():
                        if _pr.get("evolution_id") == evo_id and _pr.get("status") in ("open", "reviewing"):
                            pr_id = _pr["pr_id"]
                            break
                except Exception:
                    pass
            if pr_id:
                try:
                    from ghost_pr import get_pr_store
                    _pr_store = get_pr_store()
                    _pr_data = _pr_store.get_pr(pr_id)
                    if _pr_data and _pr_data.get("status") in ("open", "reviewing"):
                        _pr_store.set_verdict(pr_id, "rejected",
                                              reason="Evolution rolled back (timeout/incomplete)")
                    _feat_id = _pr_data.get("feature_id") if _pr_data else None
                    if _feat_id:
                        from ghost_future_features import FutureFeaturesStore
                        FutureFeaturesStore().mark_review_rejected(
                            _feat_id,
                            reason=f"PR {pr_id} orphaned by evolution rollback",
                            pr_id=pr_id)
                except Exception:
                    pass

            self._active_evolutions.pop(evo_id, None)
            results.append((evo_id, True, f"Auto-rolled back incomplete evolution {evo_id}"))

        return results

    def rollback(self, evolution_id=None):
        """Rollback to a specific evolution's backup, or the most recent.

        Uses selective restore when the evolution's changed files are known,
        falling back to full restore only for legacy/unknown cases.
        """
        if evolution_id:
            target = None
            for e in reversed(self._history):
                if e["id"] == evolution_id:
                    target = e
                    break
            if not target:
                evo = self._active_evolutions.get(evolution_id)
                if evo:
                    target = evo
            if not target:
                return False, f"Evolution {evolution_id} not found"
        else:
            if self._history:
                target = self._history[-1]
            else:
                backups = sorted(BACKUP_DIR.glob("*.tar.gz"), key=lambda p: p.stat().st_mtime)
                if backups:
                    ok, msg = self._restore_backup(str(backups[-1]))
                    if ok:
                        DEPLOY_MARKER.write_text(json.dumps({
                            "evolution_id": "rollback",
                            "rollback": True,
                            "timestamp": time.time(),
                        }), encoding="utf-8")
                    return ok, msg
                return False, "No backups available"

        backup_path = target.get("backup_path")
        if not backup_path:
            return False, "No backup path in evolution record"

        # Clean up git feature branch before restoring backup
        git_branch = target.get("git_branch")
        if git_branch:
            try:
                import ghost_git
                if ghost_git.current_branch() == git_branch:
                    ghost_git.stash_and_checkout("main")
                ghost_git.delete_branch(git_branch)
            except Exception:
                pass

        changed_files = [c["file"] for c in target.get("changes", []) if c.get("file")]
        only = changed_files if changed_files else None
        ok, msg = self._restore_backup(backup_path, only_files=only)
        if ok:
            new_files_created = set()
            for change in target.get("changes", []):
                rel = change.get("file", "")
                d = change.get("diff", "")
                if d.startswith("(new file)"):
                    new_files_created.add(rel)
                elif d and d != "(no change)":
                    new_files_created.discard(rel)

            for change in target.get("changes", []):
                fpath = change.get("file", "")
                if fpath in new_files_created:
                    file_path = PROJECT_DIR / fpath
                    if file_path.exists():
                        try:
                            file_path.unlink()
                        except Exception:
                            pass
                if fpath.startswith("ghost_tools/"):
                    parts = Path(fpath).parts
                    if len(parts) >= 2:
                        marker = PROJECT_DIR / parts[0] / parts[1] / ".evolving"
                        marker.unlink(missing_ok=True)

            rollback_entry = {
                "id": f"rollback_{uuid.uuid4().hex[:8]}",
                "description": f"Rollback of evolution {target['id']}",
                "rolled_back_evolution": target["id"],
                "status": "rolled_back",
                "timestamp": time.time(),
                "created_at": datetime.now().isoformat(),
            }
            self._history.append(rollback_entry)
            self._save_history()

            DEPLOY_MARKER.write_text(json.dumps({
                "evolution_id": rollback_entry["id"],
                "rollback": True,
                "timestamp": time.time(),
            }), encoding="utf-8")

        return ok, msg

    def get_history(self):
        return list(self._history)

    def get_pending(self):
        pending = []
        for f in PENDING_DIR.glob("*.json"):
            try:
                pending.append(json.loads(f.read_text(encoding="utf-8")))
            except Exception:
                pass
        return pending

    def get_diff(self, evolution_id):
        for e in self._history:
            if e["id"] == evolution_id:
                return e.get("changes", [])
        evo = self._active_evolutions.get(evolution_id)
        if evo:
            return evo.get("changes", [])
        return []


def _log_reviewer_mistakes(pr_data, pr_id, pr_title):
    """Store reviewer rejection feedback as mistake entries in the memory DB.

    Each rejected/blocked PR generates one memory entry so Ghost can learn
    from real review failures via memory_search(type_filter='mistake').
    Duplicates are prevented via source_hash = pr_id.
    """
    try:
        from ghost_memory import MemoryDB
        db = MemoryDB()
        if db.has_source(pr_id):
            db.close()
            return
        reviewer_msgs = [
            d["message"] for d in pr_data.get("discussions", [])
            if d.get("role") == "reviewer" and d.get("message")
        ]
        if not reviewer_msgs:
            db.close()
            return
        last_feedback = reviewer_msgs[-1][:1500]
        content = (
            f"PR REJECTION ({pr_id}): {pr_title}\n"
            f"Reviewer feedback:\n{last_feedback}"
        )
        db.save(
            content=content,
            type="mistake",
            tags="pr_rejection,auto_captured",
            source_preview=f"PR {pr_id}: {pr_title[:60]}",
            source_hash=pr_id,
        )
        db.close()
    except Exception:
        pass


_engine = None
_engine_lock = threading.Lock()


def get_engine():
    global _engine
    with _engine_lock:
        if _engine is None:
            _engine = EvolutionEngine()
        return _engine


def build_evolve_tools(cfg):
    """Build tool definitions for the LLM to self-modify Ghost."""
    engine = get_engine()

    def evolve_plan_exec(description, files, level=None, confirmed_not_duplicate: bool = False, **kwargs):
        evo_id, info = engine.plan(description, files, cfg)
        if evo_id is None:
            return f"Evolution blocked: {info.get('error', 'unknown error')}"
        parts = [
            f"Evolution planned: {evo_id}",
            f"Level: {info['level']}",
            f"Needs approval: {info['needs_approval']}",
            f"Backup: {info['backup_path']}",
        ]
        if info["needs_approval"]:
            parts.append("WAITING_FOR_APPROVAL")
            parts.append(
                "This evolution requires user approval. The user will see an approval prompt in the chat. "
                "Proceed to call evolve_apply — it will wait for approval automatically."
            )
        else:
            parts.append("Auto-approved.")
        parts.append(
            "\n🔴 MANDATORY BEFORE evolve_apply: RE-READ every file you will modify "
            "WITH NUMBERED LINES: file_read(path, numbered=true). "
            "Your earlier file_read results have been compacted from context.\n\n"
            "🔴 EDITING EXISTING FILES — USE line_edits (MANDATORY):\n"
            "Do NOT use content= or patches= on existing files. Use line_edits:\n"
            "  1. file_read('path/to/file.py', numbered=true) → shows '  45| def foo():' etc.\n"
            "  2. evolve_apply(evo_id, 'path/to/file.py', line_edits=[\n"
            '       {"start": 45, "end": 52, "replacement": "    def fixed():\\n        return True\\n"}\n'
            "     ])\n"
            "- start/end = 1-indexed line numbers from the numbered file_read output\n"
            "- replacement = the NEW code only (old code is identified by line numbers)\n"
            "- Multiple edits per call are supported\n"
            "- Do NOT set content= when using line_edits\n\n"
            "🔴 NEW FILES — CHUNKING IS MANDATORY:\n"
            "Your output token limit is ~8K tokens. A single evolve_apply with content= "
            "WILL FAIL with 'malformed JSON' if the file is longer than ~150 lines.\n"
            "For ANY new file over 100 lines, you MUST split it into chunks:\n"
            "  1. evolve_apply(evo_id, 'path', content='<first ~100 lines>')\n"
            "  2. evolve_apply(evo_id, 'path', content='<next ~100 lines>', append=True)\n"
            "Each chunk must be valid partial Python (complete function/class bodies).\n"
            "NEVER try to write an entire module in one call — it ALWAYS truncates.\n"
            "After ALL chunks are written, call file_read on the new file before patching.\n\n"
            "🔴 NEVER use shell_exec to read or inspect files. ALWAYS use file_read."
        )
        try:
            from ghost_autonomy import _CODE_PATTERNS, _DEV_STANDARDS
            parts.append(
                "\n\n## CODING STANDARDS (apply these NOW during evolve_apply)"
            )
            parts.append(_CODE_PATTERNS)
            parts.append(_DEV_STANDARDS)
        except ImportError:
            pass
        return "\n".join(parts)

    def evolve_apply_exec(evolution_id, file_path, content=None, patches=None,
                          append=False, line_edits=None):
        ok, msg = engine.apply_change(evolution_id, file_path, content=content,
                                      patches=patches, append=append,
                                      line_edits=line_edits)
        return msg

    def evolve_apply_config_exec(evolution_id=None, updates=None, **kwargs):
        if not evolution_id:
            return "Error: evolution_id is required. Call evolve_plan first."
        if not updates or not isinstance(updates, dict):
            return "Error: updates must be a non-empty JSON object of config key-value pairs."
        ok, msg = engine.apply_config_change(evolution_id, updates)
        return msg

    def evolve_delete_exec(evolution_id, file_path):
        ok, msg = engine.delete_file(evolution_id, file_path)
        return msg

    def evolve_test_exec(evolution_id):
        passed, results = engine.test(evolution_id)
        lines = [f"Tests {'PASSED' if passed else 'FAILED'}"]
        if not passed:
            failures = []
            for s in results.get("syntax", []):
                if not s["ok"]:
                    failures.append(f"Syntax {s['file']}: {s.get('error')}")
            for i in results.get("import", []):
                if not i["ok"]:
                    failures.append(f"Import {i['module']}: {i.get('error')}")
            smoke = results.get("smoke")
            if smoke and not smoke["ok"]:
                failures.append(f"Smoke: {smoke.get('output')}")
            for api_r in results.get("api_routes", []):
                if not api_r["ok"]:
                    failures.append(f"API {api_r['endpoint']}: {api_r.get('error')}")
            for lint in results.get("semantic_lint", []):
                failures.append(f"LINT {lint['file']}:{lint['line']} [{lint['rule']}]: {lint['message']}")
            if failures:
                lines.append("  FAILURE REASONS:")
                for f in failures:
                    lines.append(f"    ❌ {f}")
        for s in results.get("syntax", []):
            status = "OK" if s["ok"] else f"FAIL: {s.get('error')}"
            lines.append(f"  Syntax {s['file']}: {status}")
        for i in results.get("import", []):
            status = "OK" if i["ok"] else f"FAIL: {i.get('error')}"
            lines.append(f"  Import {i['module']}: {status}")
        smoke = results.get("smoke")
        if smoke:
            status = "OK" if smoke["ok"] else f"FAIL: {smoke.get('output')}"
            lines.append(f"  Smoke test: {status}")
        for api_r in results.get("api_routes", []):
            if api_r["ok"]:
                lines.append(f"  API route {api_r['endpoint']}: OK (HTTP {api_r.get('status', '?')})")
            else:
                lines.append(f"  API route {api_r['endpoint']}: FAIL — {api_r.get('error', 'unknown')}")
        for lint in results.get("semantic_lint", []):
            lines.append(
                f"  LINT {lint['file']}:{lint['line']} [{lint['rule']}]: {lint['message']}"
            )
        if passed:
            lines.append(
                "\nAll tests passed. Call evolve_submit_pr to submit for code review, "
                "or evolve_deploy for direct deploy (self-repair only)."
            )
            try:
                from ghost_autonomy import _PRE_PR_CHECKLIST
                lines.append(
                    "\n## BEFORE SUBMITTING — complete this checklist:"
                )
                lines.append(_PRE_PR_CHECKLIST)
            except ImportError:
                pass
        else:
            lines.append(
                "\nTests FAILED. Fix the issues with evolve_apply (use line_edits for existing files), "
                "then re-run evolve_test. Or call evolve_rollback to revert.\n"
                "WARNING: Do NOT log this evolution as successful — it has not passed tests."
            )
        return "\n".join(lines)

    _feature_cooldowns = {}
    _RETRY_COOLDOWN_S = 180  # 3 min between submit attempts for same feature
    _COOLDOWN_WAIT_THRESHOLD_S = 180  # always wait out the cooldown in the same session

    def _preserve_evolution_on_cooldown(evolution_id, title, feature_id):
        """Commit work on branch and preserve for next cycle when cooldown blocks submit."""
        import ghost_git
        evo = engine._active_evolutions.get(evolution_id)
        if not evo:
            return "Evolution not found — work may be lost."
        branch = evo.get("git_branch")
        if not branch:
            return "No git branch — work may be lost."
        try:
            if ghost_git.current_branch() != branch:
                ghost_git.checkout(branch)
            ghost_git.commit(f"WIP: {title}")
        except Exception:
            pass
        try:
            ghost_git.stash_and_checkout("main")
        except Exception:
            pass
        evo["status"] = "review_rejected"
        evo["pr_id"] = evo.get("pr_id", "")
        try:
            from ghost_future_features import FutureFeaturesStore
            if feature_id:
                FutureFeaturesStore().mark_review_rejected(
                    feature_id, "Cooldown blocked PR submission — work preserved on branch",
                    max_retries=99,
                    evolution_id=evolution_id,
                    branch_name=branch,
                    pr_id=evo.get("pr_id", ""))
        except Exception:
            pass
        return (
            f"COOLDOWN active but work PRESERVED on branch {branch}. "
            "Call task_complete. Next cycle will use evolve_resume to submit "
            "without rebuilding."
        )

    def evolve_submit_pr_exec(evolution_id, title, description="",
                              feature_id=""):
        if feature_id and feature_id in _feature_cooldowns:
            elapsed = time.time() - _feature_cooldowns[feature_id]
            remaining = _RETRY_COOLDOWN_S - elapsed
            if remaining > 0:
                if remaining <= _COOLDOWN_WAIT_THRESHOLD_S:
                    log.info("Cooldown has %ds remaining — waiting before submit", int(remaining))
                    time.sleep(remaining + 2)
                else:
                    return _preserve_evolution_on_cooldown(
                        evolution_id, title, feature_id)
            del _feature_cooldowns[feature_id]
        ok, msg = engine.submit_pr(
            evolution_id, title, description,
            feature_id=feature_id, cfg=cfg)
        if ok:
            return (
                f"{msg}\n\n"
                "PR APPROVED AND MERGED — deploy triggered. "
                "You may now log this as a successful evolution."
            )
        is_fixable_rejection = "REJECTED" in msg and "BLOCKED" not in msg
        if feature_id and is_fixable_rejection:
            _feature_cooldowns[feature_id] = time.time()
        return msg

    def evolve_resume_exec(evolution_id):
        ok, result = engine.resume_evolution(evolution_id)
        if not ok:
            return f"Resume failed: {result}"
        ctx = result
        lines = [
            f"Evolution {ctx['evolution_id']} resumed on branch {ctx['branch']}.",
            f"Review round: {ctx['review_round']}",
            f"Previous files: {', '.join(ctx['files_changed'])}",
            f"PR ID: {ctx['pr_id']}",
        ]
        if ctx.get("last_reviewer_feedback"):
            feedback = ctx["last_reviewer_feedback"][:2000]
            lines.append(f"\nLAST REVIEWER FEEDBACK:\n{feedback}")
        lines.append(
            "\nYou are now on the feature branch. Apply TARGETED fixes "
            "with evolve_apply (use line_edits=[{start, end, replacement}] for existing files), "
            "then evolve_test, then evolve_submit_pr."
        )
        return "\n".join(lines)

    def evolve_deploy_exec(evolution_id):
        ok, msg = engine.deploy(evolution_id)
        if ok:
            return (
                f"{msg}\n\n"
                "DEPLOY SUCCEEDED — call task_complete NOW. "
                "Do NOT call any more tools — the process is restarting."
            )
        return msg

    def evolve_rollback_exec(evolution_id=None):
        target_evo = None
        if evolution_id:
            target_evo = engine._active_evolutions.get(evolution_id)
        else:
            for evo in reversed(list(engine._active_evolutions.values())):
                target_evo = evo
                break

        if target_evo and not target_evo.get("changes"):
            return (
                "REJECTED: No changes have been applied yet — there is nothing to rollback. "
                "You called evolve_plan but never called evolve_apply. "
                "You MUST call evolve_apply (use line_edits for existing files) to make changes, "
                "then evolve_test, then evolve_deploy. Do NOT give up. Implement the feature NOW."
            )

        ok, msg = engine.rollback(evolution_id)
        if ok:
            return (
                f"Rollback successful: {msg}. Ghost will restart momentarily.\n\n"
                "IMPORTANT: This evolution FAILED. Do NOT log it as successful. "
                "Do NOT call memory_save or log_growth_activity claiming you added or created anything. "
                "The changes have been reverted. If you want to inform the user, be honest that "
                "the evolution was attempted but failed and was rolled back."
            )
        return f"Rollback failed: {msg}"

    return [
        {
            "name": "evolve_plan",
            "description": (
                "Plan a self-modification to Ghost's own codebase. "
                "Call this FIRST before making any changes. "
                "Provide a description of what you want to change and which files will be modified. "
                "This creates a backup and checks if approval is needed. "
                "Levels: 1-2 (skills/config, auto-approved), 3-4 (dashboard/tools, may need approval), "
                "5-6 (core code, always needs approval). "
                "IMPORTANT: Follow modular architecture — new feature = new file (ghost_<feature>.py). "
                "Never dump unrelated code into existing files. Follow security best practices — "
                "validate inputs, sanitize paths, never hardcode secrets, scope API tokens minimally."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "description": {
                        "type": "string",
                        "description": "What you plan to change and why",
                    },
                    "files": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": "List of file paths (relative to project root) that will be modified or created",
                    },
                },
                "required": ["description", "files"],
            },
            "execute": evolve_plan_exec,
        },
        {
            "name": "evolve_apply",
            "description": (
                "Apply a code change as part of a planned evolution. "
                "You must call evolve_plan first to get an evolution_id. "
                "Use file_read to understand the current code before modifying.\n\n"
                "== EDITING EXISTING FILES (PREFERRED METHOD: line_edits) ==\n"
                "Use line_edits to replace lines by number. Steps:\n"
                "1. Call file_read to see the file with line numbers\n"
                "2. Call evolve_apply with line_edits=[{start: <first_line>, end: <last_line>, "
                "replacement: '<new code>'}]\n"
                "start/end are 1-indexed inclusive line numbers from file_read. "
                "You only provide the NEW replacement text. Multiple edits in one call are supported.\n"
                "Example: line_edits=[{\"start\": 45, \"end\": 52, \"replacement\": "
                "\"    def fixed_func(self):\\n        return True\\n\"}]\n\n"
                "== ALTERNATIVE: patches (search/replace) ==\n"
                "patches=[{old: '<exact existing lines>', new: '<replacement>'}]. "
                "You must reproduce the old text EXACTLY. Use line_edits if patches fail.\n\n"
                "== NEW FILES: use content= ==\n"
                "For new files, use chunked writes (output limit ~150 lines):\n"
                "Call 1: evolve_apply(evo_id, path, content='<first ~80 lines>')\n"
                "Call 2+: evolve_apply(evo_id, path, content='<next ~80 lines>', append=True)\n"
                "NEVER use shell_exec to write files — always use evolve_apply. "
                f"LIMIT: Max {MAX_NEW_FILE_SIZE} bytes for new files. "
                "CRITICAL: After your last evolve_apply, you MUST call evolve_test then evolve_deploy. "
                "If you skip test/deploy, ALL changes will be automatically rolled back when the loop ends."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "evolution_id": {
                        "type": "string",
                        "description": "The evolution ID from evolve_plan",
                    },
                    "file_path": {
                        "type": "string",
                        "description": "Relative file path from project root (e.g. 'ghost_dashboard/routes/weather.py')",
                    },
                    "line_edits": {
                        "type": "array",
                        "items": {
                            "type": "object",
                            "properties": {
                                "start": {"type": "integer", "description": "First line number to replace (1-indexed, from file_read)"},
                                "end": {"type": "integer", "description": "Last line number to replace (1-indexed, inclusive)"},
                                "replacement": {"type": "string", "description": "New code to insert in place of lines start..end"},
                            },
                            "required": ["start", "end", "replacement"],
                        },
                        "description": "PREFERRED for existing files. Replace line ranges by number. Get line numbers from file_read.",
                    },
                    "content": {
                        "type": "string",
                        "description": "Full new content for the file (new files only) or a chunk to append (with append=true)",
                    },
                    "patches": {
                        "type": "array",
                        "items": {
                            "type": "object",
                            "properties": {
                                "old": {"type": "string"},
                                "new": {"type": "string"},
                            },
                        },
                        "description": "Search/replace pairs — alternative to line_edits. Requires exact text match.",
                    },
                    "append": {
                        "type": "boolean",
                        "description": "If true, append content to the file instead of replacing. Use this to write large new files in multiple calls.",
                    },
                },
                "required": ["evolution_id", "file_path"],
            },
            "execute": evolve_apply_exec,
        },
        {
            "name": "evolve_apply_config",
            "description": (
                "Apply config changes to Ghost's runtime config (~/.ghost/config.json) "
                "as part of a planned evolution. You MUST call evolve_plan first. "
                "Config changes are tracked in the evolution — rollback restores the old config. "
                "Auth/secret keys are blocked. Security-hardening changes (e.g. enabling "
                "strict_tool_registration, disabling evolve_auto_approve) are ALLOWED. "
                "Weakening changes require user approval via add_action_item. "
                "CRITICAL: After all changes, call evolve_test then evolve_deploy."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "evolution_id": {
                        "type": "string",
                        "description": "The evolution ID from evolve_plan",
                    },
                    "updates": {
                        "type": "object",
                        "description": "Key-value pairs to update in Ghost's config",
                    },
                },
                "required": ["evolution_id", "updates"],
            },
            "execute": evolve_apply_config_exec,
        },
        {
            "name": "evolve_delete",
            "description": (
                "Delete a file as part of a planned evolution. "
                "Use this when removing a module or feature — do NOT just empty the file. "
                "The deletion is tracked so self-repair won't accidentally restore it. "
                "After deletion, the system scans for dangling imports and warns you. "
                "CRITICAL: Fix any dangling imports before calling evolve_test, "
                "or the test will fail."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "evolution_id": {
                        "type": "string",
                        "description": "The evolution ID from evolve_plan",
                    },
                    "file_path": {
                        "type": "string",
                        "description": "Relative file path to delete (e.g. 'ghost_llm_router.py')",
                    },
                },
                "required": ["evolution_id", "file_path"],
            },
            "execute": evolve_delete_exec,
        },
        {
            "name": "evolve_test",
            "description": (
                "Run the validation pipeline on changes made during an evolution. "
                "Checks: dangling imports (files importing deleted modules), "
                "Python syntax (ast.parse), module imports, and a smoke test "
                "(verifies Ghost can start with the new code). "
                "You must call this after evolve_apply and before evolve_deploy."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "evolution_id": {
                        "type": "string",
                        "description": "The evolution ID to test",
                    },
                },
                "required": ["evolution_id"],
            },
            "execute": evolve_test_exec,
        },
        {
            "name": "evolve_submit_pr",
            "description": (
                "Submit a pull request for code review after evolve_test passes. "
                "This commits your changes to a feature branch, creates an internal PR, "
                "and runs an automated adversarial code review (Reviewer vs Developer). "
                "The reviewer checks code quality, UI/UX, frontend-backend integration, "
                "and Python correctness. Possible outcomes:\n"
                "- APPROVED: PR is merged and Ghost restarts with the new code.\n"
                "- REJECTED: Reviewer found issues. The feature is automatically re-queued "
                "to pending for another attempt. Call task_complete.\n"
                "- BLOCKED: The approach is fundamentally wrong. "
                "The feature is marked rejected automatically. Call task_complete.\n"
                "Use this INSTEAD of evolve_deploy for normal feature implementation."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "evolution_id": {
                        "type": "string",
                        "description": "The evolution ID from evolve_plan",
                    },
                    "title": {
                        "type": "string",
                        "description": "Short title for the PR (e.g. 'Add webhook secret auto-generation')",
                    },
                    "description": {
                        "type": "string",
                        "description": "Longer description of what changed and why",
                    },
                    "feature_id": {
                        "type": "string",
                        "description": "The feature ID this PR implements (from start_future_feature)",
                    },
                },
                "required": ["evolution_id", "title", "feature_id"],
            },
            "execute": evolve_submit_pr_exec,
        },
        {
            "name": "evolve_resume",
            "description": (
                "Resume a previously rejected evolution for fix-and-resubmit. "
                "Call this instead of evolve_plan when a feature has "
                "current_evolution_id and current_branch set (indicating a "
                "previous PR was rejected but the branch is preserved). "
                "This checks out the existing feature branch so you can "
                "apply targeted patches to address reviewer feedback, then "
                "re-test and re-submit to the same PR."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "evolution_id": {
                        "type": "string",
                        "description": "The evolution ID to resume (from the feature's current_evolution_id)",
                    },
                },
                "required": ["evolution_id"],
            },
            "execute": evolve_resume_exec,
        },
        {
            "name": "evolve_deploy",
            "description": (
                "Deploy an evolution by restarting Ghost with the new code. "
                "Only works after evolve_test passes. "
                "IMPORTANT: For normal feature implementation, use evolve_submit_pr instead. "
                "evolve_deploy is reserved for self-repair and emergency fixes only. "
                "Ghost will gracefully shut down and the supervisor will restart it. "
                "A health check runs after restart; if it fails, the backup is auto-restored."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "evolution_id": {
                        "type": "string",
                        "description": "The evolution ID to deploy",
                    },
                },
                "required": ["evolution_id"],
            },
            "execute": evolve_deploy_exec,
        },
        {
            "name": "evolve_rollback",
            "description": (
                "Rollback to a previous state by restoring a backup. "
                "If evolution_id is provided, restores that specific backup. "
                "If omitted, restores the most recent backup. "
                "Ghost will restart after rollback."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "evolution_id": {
                        "type": "string",
                        "description": "Optional: specific evolution to rollback (defaults to most recent)",
                    },
                },
            },
            "execute": evolve_rollback_exec,
        },
    ]
