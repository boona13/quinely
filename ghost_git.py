"""
Ghost Git Operations — version control layer for the PR review system.

Wraps git commands for branch management, committing, diffing, and merging.
Auto-initializes the repo on first use. All operations use subprocess.run
with argument lists (no shell=True) for cross-platform safety.
"""

import subprocess
import logging
import threading
from pathlib import Path
from typing import Tuple, Optional

PROJECT_DIR = Path(__file__).resolve().parent

_git_lock = threading.Lock()

_GITIGNORE_CONTENT = """\
.venv/
venv/
__pycache__/
*.pyc
*.pyo
*.egg-info/
.mypy_cache/
.pytest_cache/
node_modules/
.DS_Store
*.db-wal
*.db-shm
memory.db
.env
"""

log = logging.getLogger("ghost.git")


def _run(args: list[str], check: bool = True,
         capture: bool = True) -> subprocess.CompletedProcess:
    """Run a git command inside PROJECT_DIR.

    All git commands are serialized through _git_lock to prevent
    concurrent operations from corrupting the repo state (e.g.
    Feature Implementer doing submit_pr while dashboard does force-merge).
    """
    with _git_lock:
        return subprocess.run(
            ["git"] + args,
            cwd=str(PROJECT_DIR),
            capture_output=capture,
            text=True,
            check=check,
            timeout=30,
        )


def is_initialized() -> bool:
    """Check if the git repo exists and has at least one commit."""
    git_dir = PROJECT_DIR / ".git"
    if not git_dir.is_dir():
        return False
    try:
        r = _run(["rev-parse", "HEAD"], check=False)
        return r.returncode == 0
    except Exception:
        return False


def init_repo() -> Tuple[bool, str]:
    """Initialize the git repo with a baseline commit if not already initialized."""
    if is_initialized():
        branch = current_branch()
        if branch != "main":
            try:
                _run(["checkout", "main"])
            except subprocess.CalledProcessError:
                pass
        return True, "Git repo already initialized"

    try:
        _run(["init"])

        gitignore_path = PROJECT_DIR / ".gitignore"
        existing = ""
        if gitignore_path.exists():
            existing = gitignore_path.read_text(encoding="utf-8")
        if ".venv/" not in existing:
            gitignore_path.write_text(existing.rstrip("\n") + "\n" + _GITIGNORE_CONTENT, encoding="utf-8")

        _run(["add", "."])

        _run(["config", "user.email", "quinely@localhost"])
        _run(["config", "user.name", "Quinely"])

        _run(["commit", "-m", "Initial commit: Quinely baseline"])

        r = _run(["branch", "--show-current"])
        if r.stdout.strip() != "main":
            _run(["branch", "-M", "main"])

        return True, "Git repo initialized with baseline commit on main"
    except subprocess.CalledProcessError as e:
        return False, f"Git init failed: {e.stderr or e.stdout or str(e)}"
    except Exception as e:
        return False, f"Git init error: {e}"


def current_branch() -> str:
    """Return the name of the current branch."""
    try:
        r = _run(["branch", "--show-current"])
        return r.stdout.strip()
    except Exception:
        return "unknown"


def branch_exists(name: str) -> bool:
    """Check if a branch exists."""
    try:
        r = _run(["rev-parse", "--verify", name], check=False)
        return r.returncode == 0
    except Exception:
        return False


def create_branch(name: str) -> Tuple[bool, str]:
    """Create a new branch from main and check it out.

    Auto-commits any uncommitted changes on main first (e.g. after a
    backup restore leaves the working tree diverged from HEAD).
    """
    ok, msg = init_repo()
    if not ok:
        return False, msg

    r = _run(["status", "--porcelain"], check=False)
    if r.stdout.strip():
        _run(["add", "-A"], check=False)
        _run(["commit", "-m", "Auto-commit: clean state before branch creation"],
             check=False)

    if branch_exists(name):
        try:
            _run(["checkout", name])
            return True, f"Checked out existing branch {name}"
        except subprocess.CalledProcessError as e:
            return False, f"Cannot checkout branch {name}: {e.stderr}"

    try:
        _run(["checkout", "-b", name, "main"])
        return True, f"Created and checked out branch {name}"
    except subprocess.CalledProcessError as e:
        return False, f"Cannot create branch {name}: {e.stderr}"


def checkout(branch: str) -> Tuple[bool, str]:
    """Switch to an existing branch."""
    try:
        _run(["checkout", branch])
        return True, f"Switched to {branch}"
    except subprocess.CalledProcessError as e:
        return False, f"Checkout failed: {e.stderr}"


def commit(message: str) -> Tuple[bool, str]:
    """Stage all changes and commit on the current branch."""
    try:
        _run(["add", "-A"])
        r = _run(["diff", "--cached", "--stat"], check=False)
        if not r.stdout.strip():
            return True, "Nothing to commit"
        _run(["commit", "-m", message])
        return True, f"Committed: {message}"
    except subprocess.CalledProcessError as e:
        return False, f"Commit failed: {e.stderr}"


def get_diff(base: str, head: str) -> str:
    """Get unified diff between two branches."""
    try:
        r = _run(["diff", f"{base}...{head}"], check=False)
        return r.stdout
    except Exception as e:
        return f"(diff error: {e})"


def get_diff_stat(base: str, head: str) -> str:
    """Get a summary of changes between two branches."""
    try:
        r = _run(["diff", "--stat", f"{base}...{head}"], check=False)
        return r.stdout
    except Exception:
        return ""


def get_changed_files(base: str, head: str) -> list[str]:
    """List files changed between two branches."""
    try:
        r = _run(["diff", "--name-only", f"{base}...{head}"], check=False)
        return [f for f in r.stdout.strip().split("\n") if f]
    except Exception:
        return []


def merge(source_branch: str) -> Tuple[bool, str]:
    """Merge source_branch into current branch (fast-forward preferred).

    If the merge fails (e.g. conflict), automatically aborts to leave
    the repo in a clean state.
    """
    try:
        _run(["merge", "--ff-only", source_branch])
        return True, f"Fast-forward merged {source_branch}"
    except subprocess.CalledProcessError:
        try:
            _run(["merge", source_branch, "-m",
                  f"Merge {source_branch} into {current_branch()}"])
            return True, f"Merged {source_branch} (merge commit)"
        except subprocess.CalledProcessError as e:
            _run(["merge", "--abort"], check=False)
            return False, f"Merge failed (aborted): {e.stderr}"


def delete_branch(name: str) -> Tuple[bool, str]:
    """Delete a branch (must not be currently checked out)."""
    if current_branch() == name:
        return False, f"Cannot delete the currently checked-out branch {name}"
    try:
        _run(["branch", "-D", name])
        return True, f"Deleted branch {name}"
    except subprocess.CalledProcessError as e:
        return False, f"Delete branch failed: {e.stderr}"


def get_log(branch: Optional[str] = None, limit: int = 10) -> str:
    """Get recent commit log."""
    args = ["log", "--oneline", f"-{limit}"]
    if branch:
        args.append(branch)
    try:
        r = _run(args, check=False)
        return r.stdout
    except Exception:
        return ""


def stash_and_checkout(branch: str) -> Tuple[bool, str]:
    """Commit uncommitted changes on the current branch, then checkout target.

    Previous behaviour stashed changes and never popped them, silently losing
    work (both user edits and evolution changes).  Committing instead keeps
    the changes on the source branch where they can be recovered via git log.
    """
    try:
        r = _run(["status", "--porcelain"], check=False)
        if r.stdout.strip():
            current = current_branch() or "unknown"
            _run(["add", "-A"], check=False)
            _run(["commit", "-m",
                  f"Auto-save: uncommitted changes on {current} before checkout"],
                 check=False)
        ok, msg = checkout(branch)
        return ok, msg
    except Exception as e:
        return False, f"Stash-and-checkout failed: {e}"


def update_branch(feature_branch: str) -> Tuple[bool, str]:
    """Merge latest main into feature branch (GitHub's 'Update branch').

    Uses merge (not rebase) to preserve commit history. Aborts on conflict.
    """
    try:
        _run(["checkout", feature_branch])
        _run(["merge", "main", "-m",
              f"Update branch: merge main into {feature_branch}"])
        return True, f"Updated {feature_branch} with latest main"
    except subprocess.CalledProcessError as e:
        _run(["merge", "--abort"], check=False)
        return False, f"Conflict updating branch: {e.stderr}"


def get_interdiff(old_sha: str, new_sha: str) -> str:
    """Diff between two commits (shows changes between review rounds)."""
    try:
        r = _run(["diff", old_sha, new_sha], check=False)
        return r.stdout
    except Exception:
        return ""


def get_head_sha(branch: str = None) -> str:
    """Get HEAD SHA of a branch (or current HEAD)."""
    try:
        ref = branch or "HEAD"
        r = _run(["rev-parse", ref], check=False)
        return r.stdout.strip() if r.returncode == 0 else ""
    except Exception:
        return ""


def ensure_clean_main() -> Tuple[bool, str]:
    """Ensure we're on main with no uncommitted changes."""
    ok, msg = init_repo()
    if not ok:
        return False, msg

    branch = current_branch()
    if branch != "main":
        ok, msg = checkout("main")
        if not ok:
            return False, msg

    r = _run(["status", "--porcelain"], check=False)
    if r.stdout.strip():
        _run(["add", "-A"], check=False)
        _run(["commit", "-m", "Auto-commit uncommitted changes on main"],
             check=False)

    return True, "On main, clean state"
