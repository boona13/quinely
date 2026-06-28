"""
ghost_git_tools.py — First-class git tools for the agent loop.

Exposes structured git operations (status / diff / log / add / commit / branch /
init) that the LLM can call directly on USER project directories, instead of
shelling out with raw `git ...` strings.

Safety:
  • All commands run with an explicit argument list (no shell=True) and a
    timeout — cross-platform safe.
  • Operations are refused inside Ghost's own install directory. Ghost's source
    repo is managed exclusively by the evolution pipeline (ghost_git.py); letting
    the chat agent commit/branch there could corrupt evolve state.

Pure stdlib — works on macOS, Linux, and Windows wherever `git` is installed.
"""

import shutil
import subprocess
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

PROJECT_DIR = Path(__file__).resolve().parent
MAX_OUTPUT_CHARS = 8000


def _git_available() -> bool:
    return shutil.which("git") is not None


def _resolve_repo(path: str) -> Tuple[Optional[Path], Optional[str]]:
    """Resolve and validate a working directory for a git operation."""
    stripped = (path or "").strip()
    if not stripped:
        return None, (
            "ERROR: path is required — pass the absolute path to the project "
            "directory (e.g. ~/Projects/myapp). Git tools do not operate on "
            "Ghost's own directory."
        )
    p = Path(stripped).expanduser()
    try:
        p = p.resolve()
    except Exception:
        return None, f"ERROR: invalid path '{path}'"
    if not p.exists():
        return None, f"ERROR: directory not found: {p}"
    if not p.is_dir():
        return None, f"ERROR: not a directory: {p}"

    ghost = PROJECT_DIR.resolve()
    if p == ghost or ghost in p.parents:
        return None, (
            f"BLOCKED: '{p}' is inside Ghost's own install directory. Ghost's "
            "source repo is managed by the evolution pipeline, not these tools. "
            "Use git tools on user project directories instead."
        )
    return p, None


def _run(cwd: Path, args: List[str], timeout: int = 30) -> Tuple[int, str, str]:
    try:
        r = subprocess.run(
            ["git"] + args,
            cwd=str(cwd),
            capture_output=True,
            text=True,
            timeout=timeout,
        )
        return r.returncode, r.stdout, r.stderr
    except subprocess.TimeoutExpired:
        return 124, "", f"git {' '.join(args)} timed out after {timeout}s"
    except Exception as e:
        return 1, "", str(e)


def _truncate(text: str) -> str:
    if len(text) > MAX_OUTPUT_CHARS:
        return text[:MAX_OUTPUT_CHARS] + f"\n... (truncated, {len(text) - MAX_OUTPUT_CHARS} more chars)"
    return text


def _is_repo(cwd: Path) -> bool:
    code, out, _ = _run(cwd, ["rev-parse", "--is-inside-work-tree"])
    return code == 0 and out.strip() == "true"


# ─────────────────────────────────────────────────────
#  Operations
# ─────────────────────────────────────────────────────

def _status(path: str) -> str:
    repo, err = _resolve_repo(path)
    if err:
        return err
    if not _is_repo(repo):
        return f"Not a git repository: {repo}. Use git_init to create one."
    branch_code, branch_out, _ = _run(repo, ["branch", "--show-current"])
    branch = branch_out.strip() or "(detached)"
    code, out, errout = _run(repo, ["status", "--porcelain=v1", "--branch"])
    if code != 0:
        return f"git status failed: {errout.strip()}"
    body = out.strip() or "(working tree clean)"
    return f"Branch: {branch}\n\n{_truncate(body)}"


def _diff(path: str, staged: bool, file: str) -> str:
    repo, err = _resolve_repo(path)
    if err:
        return err
    if not _is_repo(repo):
        return f"Not a git repository: {repo}."
    args = ["diff"]
    if staged:
        args.append("--staged")
    if file:
        args += ["--", file]
    code, out, errout = _run(repo, args)
    if code != 0:
        return f"git diff failed: {errout.strip()}"
    if not out.strip():
        return "(no changes)" if not staged else "(no staged changes)"
    return _truncate(out)


def _log(path: str, limit: int) -> str:
    repo, err = _resolve_repo(path)
    if err:
        return err
    if not _is_repo(repo):
        return f"Not a git repository: {repo}."
    limit = max(1, min(int(limit or 15), 100))
    code, out, errout = _run(repo, ["log", f"-{limit}", "--pretty=format:%h %ad %an %s", "--date=short"])
    if code != 0:
        return f"git log failed: {errout.strip()}"
    return _truncate(out.strip() or "(no commits yet)")


def _add(path: str, files: str) -> str:
    repo, err = _resolve_repo(path)
    if err:
        return err
    if not _is_repo(repo):
        return f"Not a git repository: {repo}. Use git_init first."
    spec = (files or "").strip()
    if not spec or spec.lower() == "all":
        args = ["add", "-A"]
    else:
        args = ["add", "--"] + spec.split()
    code, out, errout = _run(repo, args)
    if code != 0:
        return f"git add failed: {errout.strip()}"
    # Show what is now staged.
    _, staged, _ = _run(repo, ["diff", "--staged", "--name-status"])
    return f"OK: staged {'all changes' if args == ['add', '-A'] else spec}\n\n{_truncate(staged.strip()) or '(nothing staged)'}"


def _commit(path: str, message: str, add_all: bool) -> str:
    repo, err = _resolve_repo(path)
    if err:
        return err
    if not _is_repo(repo):
        return f"Not a git repository: {repo}. Use git_init first."
    if not (message or "").strip():
        return "ERROR: commit message is required."
    if add_all:
        _run(repo, ["add", "-A"])
    # Anything to commit?
    code, staged, _ = _run(repo, ["diff", "--staged", "--name-only"])
    if not staged.strip():
        return "Nothing to commit (no staged changes). Use git_add or set add_all=true."
    code, out, errout = _run(repo, ["commit", "-m", message])
    if code != 0:
        return f"git commit failed: {errout.strip() or out.strip()}"
    _, head, _ = _run(repo, ["log", "-1", "--pretty=format:%h %s"])
    return f"OK: committed.\n{head.strip()}"


def _branch(path: str, action: str, name: str) -> str:
    repo, err = _resolve_repo(path)
    if err:
        return err
    if not _is_repo(repo):
        return f"Not a git repository: {repo}."
    action = (action or "list").strip().lower()

    if action == "list":
        code, out, errout = _run(repo, ["branch", "--all"])
        if code != 0:
            return f"git branch failed: {errout.strip()}"
        return _truncate(out.strip() or "(no branches)")

    if action in ("create", "checkout", "switch", "delete"):
        if not (name or "").strip():
            return f"ERROR: name is required for action '{action}'."
        name = name.strip()
        if action == "create":
            code, out, errout = _run(repo, ["checkout", "-b", name])
        elif action in ("checkout", "switch"):
            code, out, errout = _run(repo, ["checkout", name])
        else:  # delete
            code, out, errout = _run(repo, ["branch", "-D", name])
        if code != 0:
            return f"git {action} failed: {errout.strip() or out.strip()}"
        return f"OK: {action} branch '{name}'\n{(out or errout).strip()}"

    return f"ERROR: unknown action '{action}'. Use list, create, checkout, or delete."


def _init(path: str) -> str:
    repo, err = _resolve_repo(path)
    if err:
        return err
    if _is_repo(repo):
        return f"Already a git repository: {repo}"
    code, out, errout = _run(repo, ["init"])
    if code != 0:
        return f"git init failed: {errout.strip()}"
    return f"OK: initialized empty git repository in {repo}"


# ─────────────────────────────────────────────────────
#  TOOL BUILDER
# ─────────────────────────────────────────────────────

def build_git_tools(cfg: dict = None) -> List[Dict[str, Any]]:
    """Build structured git tools for the Ghost tool registry."""
    cfg = cfg or {}

    if not _git_available():
        # Register nothing if git is not installed — avoids dead tools.
        return []

    _path_param = {
        "type": "string",
        "description": "Absolute path to the project directory (e.g. ~/Projects/myapp). Must NOT be Ghost's own directory.",
    }

    return [
        {
            "name": "git_status",
            "description": "Show the working-tree status and current branch of a git repository (porcelain format). Use before committing to see what changed.",
            "parameters": {
                "type": "object",
                "properties": {"path": _path_param},
                "required": ["path"],
            },
            "execute": lambda path: _status(path),
        },
        {
            "name": "git_diff",
            "description": "Show changes in a git repository as a unified diff. Set staged=true to see staged changes; pass a file to scope the diff.",
            "parameters": {
                "type": "object",
                "properties": {
                    "path": _path_param,
                    "staged": {"type": "boolean", "description": "Show staged (index) changes instead of working-tree changes", "default": False},
                    "file": {"type": "string", "description": "Optional file path to scope the diff", "default": ""},
                },
                "required": ["path"],
            },
            "execute": lambda path, staged=False, file="": _diff(path, bool(staged), file),
        },
        {
            "name": "git_log",
            "description": "Show recent commit history (hash, date, author, subject) for a git repository.",
            "parameters": {
                "type": "object",
                "properties": {
                    "path": _path_param,
                    "limit": {"type": "integer", "description": "Number of commits to show (default 15, max 100)", "default": 15},
                },
                "required": ["path"],
            },
            "execute": lambda path, limit=15: _log(path, limit),
        },
        {
            "name": "git_add",
            "description": "Stage changes in a git repository. Pass files='all' (default) to stage everything, or a space-separated list of paths.",
            "parameters": {
                "type": "object",
                "properties": {
                    "path": _path_param,
                    "files": {"type": "string", "description": "'all' or a space-separated list of file paths to stage", "default": "all"},
                },
                "required": ["path"],
            },
            "execute": lambda path, files="all": _add(path, files),
        },
        {
            "name": "git_commit",
            "description": "Commit staged changes in a git repository with a message. With add_all=true (default) all changes are staged first.",
            "parameters": {
                "type": "object",
                "properties": {
                    "path": _path_param,
                    "message": {"type": "string", "description": "Commit message"},
                    "add_all": {"type": "boolean", "description": "Stage all changes before committing", "default": True},
                },
                "required": ["path", "message"],
            },
            "execute": lambda path, message, add_all=True: _commit(path, message, bool(add_all)),
        },
        {
            "name": "git_branch",
            "description": "Manage branches in a git repository. action: 'list' (default), 'create', 'checkout', or 'delete'. name is required for all actions except list.",
            "parameters": {
                "type": "object",
                "properties": {
                    "path": _path_param,
                    "action": {"type": "string", "description": "list | create | checkout | delete", "default": "list"},
                    "name": {"type": "string", "description": "Branch name (required for create/checkout/delete)", "default": ""},
                },
                "required": ["path"],
            },
            "execute": lambda path, action="list", name="": _branch(path, action, name),
        },
        {
            "name": "git_init",
            "description": "Initialize a new empty git repository in a user project directory.",
            "parameters": {
                "type": "object",
                "properties": {"path": _path_param},
                "required": ["path"],
            },
            "execute": lambda path: _init(path),
        },
    ]
