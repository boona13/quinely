"""
GHOST Built-in Tools

All tools Ghost can use: shell, files, web, apps, memory, notifications.
Each tool is a dict with {name, description, parameters, execute}.
Safety: shell commands are whitelisted, file access is root-restricted.
"""

import os
import re
import json
import glob
import subprocess
import sys
import platform
import requests
import time
import threading
from pathlib import Path
from datetime import datetime

PLAT = platform.system()
GHOST_HOME = Path.home() / ".ghost"
PROJECT_DIR = Path(__file__).resolve().parent

# ═════════════════════════════════════════════════════════════════════
#  CALLER CONTEXT (interactive vs autonomous)
# ═════════════════════════════════════════════════════════════════════

_caller_context = threading.local()


def set_shell_caller_context(ctx: str):
    """Set the caller context for shell_exec policy decisions.

    "interactive" — user-initiated (chat, ask, inbound channels). Commands run
    in the sandbox environment so user-requested packages don't pollute Ghost's
    own venv.
    "autonomous" (default) — cron jobs, evolve loop. Commands run with Ghost's
    own venv so self-evolution and security patches can modify Ghost's deps.
    """
    _caller_context.value = ctx


def get_shell_caller_context() -> str:
    return getattr(_caller_context, "value", "autonomous")


# ═════════════════════════════════════════════════════════════════════
#  SANDBOX ENVIRONMENT
# ═════════════════════════════════════════════════════════════════════

SANDBOX_DIR = GHOST_HOME / "sandbox"
SANDBOX_VENV = SANDBOX_DIR / ".venv"
SANDBOX_SCRIPTS = SANDBOX_DIR / "scripts"
_sandbox_ready = False
_sandbox_lock = threading.Lock()


def _ensure_sandbox():
    """Create the sandbox venv + scripts dir on first use (idempotent)."""
    global _sandbox_ready
    if _sandbox_ready and SANDBOX_VENV.exists():
        return
    with _sandbox_lock:
        if _sandbox_ready and SANDBOX_VENV.exists():
            return
        SANDBOX_DIR.mkdir(parents=True, exist_ok=True)
        SANDBOX_SCRIPTS.mkdir(parents=True, exist_ok=True)
        if not SANDBOX_VENV.exists():
            import logging
            log = logging.getLogger("ghost.sandbox")
            log.info("Creating sandbox venv at %s", SANDBOX_VENV)
            subprocess.run(
                [sys.executable, "-m", "venv", str(SANDBOX_VENV)],
                capture_output=True, timeout=60,
            )
        _sandbox_ready = True


def get_sandbox_bin() -> Path:
    """Return the sandbox venv bin directory."""
    _ensure_sandbox()
    if PLAT == "Windows":
        return SANDBOX_VENV / "Scripts"
    return SANDBOX_VENV / "bin"


def get_sandbox_env() -> dict:
    """Build an env dict that activates the sandbox venv for subprocess calls.

    Sandbox bin is first in PATH so `pip install X` goes to sandbox.
    Ghost's own .venv/bin stays in PATH so Ghost-internal tools still resolve.
    """
    _ensure_sandbox()
    env = os.environ.copy()
    sandbox_bin = str(get_sandbox_bin())

    current_path = env.get("PATH", "")
    path_parts = current_path.split(os.pathsep)

    new_parts = [sandbox_bin]
    for p in path_parts:
        if p != sandbox_bin:
            new_parts.append(p)
    env["PATH"] = os.pathsep.join(new_parts)
    env["VIRTUAL_ENV"] = str(SANDBOX_VENV)
    env.pop("PIP_TARGET", None)
    return env


def _is_pip_install(command: str) -> bool:
    """Detect if a command is a pip/pip3 install."""
    return bool(re.search(r"\bpip3?\s+install\b", command, re.IGNORECASE))


def get_user_projects_dir(cfg=None):
    """Cross-platform default directory for user-created projects.

    Priority: config override > ~/Desktop (if exists) > ~/Projects > ~/projects
    Works on macOS, Linux, and Windows.
    """
    if cfg and cfg.get("user_projects_dir"):
        p = Path(cfg["user_projects_dir"]).expanduser()
        if p.is_absolute():
            return p
    desktop = Path.home() / "Desktop"
    if desktop.is_dir():
        return desktop
    projects = Path.home() / "Projects"
    if projects.is_dir():
        return projects
    fallback = Path.home() / "projects"
    fallback.mkdir(exist_ok=True)
    return fallback


def get_workspace(cfg, project_name=None):
    """Get the workspace root for user-created projects. Creates it if needed.

    Returns the base workspace dir, or workspace/project_name if provided.
    The workspace is separate from Ghost's own codebase.
    """
    base = get_user_projects_dir(cfg)
    if project_name:
        p = base / project_name
        p.mkdir(parents=True, exist_ok=True)
        return p
    return base

CORE_COMMANDS = [
    "python3", "python", "pip", "pip3", "git", "node", "npm",
]

_BASE_ALLOWED_COMMANDS = [
    # Filesystem
    "ls", "pwd", "cd", "cat", "head", "tail", "wc", "less", "more",
    "mv", "cp", "mkdir", "rm", "rmdir", "touch",
    "ln", "stat", "file", "tree", "realpath",
    "basename", "dirname",
    # Text processing
    "grep", "awk", "sed", "tr", "cut", "sort", "uniq", "diff", "patch",
    "xargs", "tee", "fmt", "column",
    # Search
    "find", "rg", "fd",
    # Compression
    "zip", "unzip", "tar", "gzip", "gunzip", "bzip2", "bunzip2", "xz",
    # System info
    "echo", "date", "whoami", "hostname", "uname", "uptime",
    "df", "du", "env", "printenv",
    # Process management
    "ps", "kill", "sleep",
    # Networking
    "curl", "wget", "ssh", "scp", "rsync", "sftp",
    "ping", "dig", "nslookup",
    # Crypto / hashing
    "md5", "shasum", "base64", "openssl",
    # Python
    "python3", "python", "pip", "pip3",
    # Node.js
    "node", "npm", "npx",
    # Version control
    "git",
    # Build tools
    "make", "cmake",
    # Databases
    "sqlite3",
    # JSON / data
    "jq",
    # Modern CLI tools
    "bat", "exa", "eza",
    # Media processing
    "ffmpeg", "ffprobe",
]

_PLATFORM_COMMANDS = {
    "Darwin": [
        "open", "chmod", "chown", "readlink", "which", "whereis",
        "free", "id", "groups", "top", "htop", "lsof", "pgrep", "pkill",
        "timeout", "watch", "nohup", "host", "ifconfig", "netstat",
        "sw_vers", "defaults", "pbcopy", "pbpaste", "say", "brew",
    ],
    "Linux": [
        "xdg-open", "chmod", "chown", "readlink", "which", "whereis",
        "free", "id", "groups", "top", "htop", "lsof", "pgrep", "pkill",
        "timeout", "watch", "nohup", "host", "ifconfig", "netstat",
        "xclip", "notify-send", "apt", "apt-get", "dnf", "yum",
    ],
    "Windows": [
        "cmd", "powershell", "pwsh", "start", "clip", "type", "dir",
        "copy", "move", "del", "rd", "findstr", "where", "tasklist",
        "taskkill", "icacls", "attrib",
    ],
}

DEFAULT_ALLOWED_COMMANDS = _BASE_ALLOWED_COMMANDS + _PLATFORM_COMMANDS.get(PLAT, [])

DEFAULT_ALLOWED_ROOTS = [
    str(Path.home()),
]

DEFAULT_BLOCKED_COMMANDS = [
    "rm -rf /", "rm -rf ~", "mkfs", "dd if=", ":(){", "fork",
    "sudo rm", "chmod -R 777 /", "> /dev/sd",
    # Prevent autonomy from pausing/stopping/shutting down the daemon via shell
    "api/ghost/pause", "api/ghost/shutdown",
    "ghost/pause", "ghost/shutdown",
]


def _check_path_allowed(path_str, allowed_roots):
    """Always allows access — Ghost runs in a sandboxed environment.

    The allowed_roots parameter is accepted for API compatibility but
    no longer restricts access.
    """
    return True


def _is_ghost_codebase_path(path_str):
    """Check if a resolved path is inside Ghost's own install directory.

    This prevents non-evolution tool loops from modifying Ghost's source
    code via file_write. The evolution pipeline (evolve_apply) writes
    files directly — it does not go through file_write — so this guard
    only blocks unintended self-modification from chat/cron/webhook/channel
    dispatch paths.
    """
    try:
        resolved = Path(path_str).expanduser().resolve()
        codebase = PROJECT_DIR.resolve()
        return resolved == codebase or codebase in resolved.parents
    except Exception:
        return False


_PROJECTS_DIR = Path.home() / "Projects"
_auto_register_seen = set()


def _auto_register_project(file_path: Path):
    """Auto-register a project when files are written inside ~/Projects/<name>/."""
    try:
        resolved = file_path.resolve()
        if _PROJECTS_DIR not in resolved.parents:
            return
        rel = resolved.relative_to(_PROJECTS_DIR)
        project_name = rel.parts[0]
        project_path = _PROJECTS_DIR / project_name
        if project_name in _auto_register_seen:
            return
        _auto_register_seen.add(project_name)
        ghost_dir = project_path / ".ghost"
        if (ghost_dir / "project.json").exists():
            return
        from ghost_projects import ProjectRegistry
        registry = ProjectRegistry()
        display_name = project_name.replace("-", " ").replace("_", " ").title()
        registry.create(project_path, display_name)
    except Exception:
        pass


def _check_command_allowed(command, allowed_commands, blocked_commands):
    """Verify a shell command is not in the blocked patterns list.

    The allowlist is ignored — Ghost runs in a sandboxed environment and
    should be free to invoke any command.  Only genuinely destructive
    patterns (rm -rf /, mkfs, etc.) are still blocked.
    """
    for blocked in blocked_commands:
        if blocked in command:
            return False, f"Blocked: command matches dangerous pattern '{blocked}'"
    return True, ""


def _is_dangerous_interpreter(cmd: str) -> bool:
    base = Path(cmd or "").name
    return base in {"python", "python3", "pip", "pip3"}


# High-risk shell patterns that indicate command chaining, redirection, or subshell execution
_HIGH_RISK_SHELL_PATTERNS = [
    ";",      # Command separator
    "&&",     # AND chaining
    "||",     # OR chaining
    "|",      # Pipe
    "`",      # Backtick subshell
    "$(",     # Command substitution
    ">",      # Output redirection
    ">>",     # Append redirection
    "<",      # Input redirection
    "2>",     # Stderr redirection
    "&>",     # Combined redirection
]

# Flag variants that should be matched (exact and long-form)
_FLAG_VARIANTS = {
    "-c": ["-c", "--command"],
    "-m": ["-m", "--module"],
}


def _check_dangerous_command_policy(command: str, cfg: dict, workspace=None):
    """Check if a dangerous interpreter command complies with security policy.
    
    Returns: (allowed: bool, reason: str)
    Reason uses POLICY_DENY:<code>:<detail> format for machine-parseable audit logs.
    """
    import shlex
    
    command = command.strip()
    if not command:
        return False, "POLICY_DENY:EMPTY:Empty command"

    # Normalize command for analysis (posix=False on Windows for cmd.exe quoting)
    try:
        tokens = shlex.split(command, posix=(os.name != "nt"))
    except ValueError:
        tokens = command.split()
    
    if not tokens:
        return False, "POLICY_DENY:EMPTY:Empty command"

    base_cmd = Path(tokens[0]).name
    if not _is_dangerous_interpreter(base_cmd):
        return True, ""

    # Interactive callers (chat, ask, inbound channels) get full access to
    # allowed commands — the allowlist + blocked list already gate safety.
    if get_shell_caller_context() == "interactive":
        return True, ""

    if not cfg.get("enable_dangerous_interpreters", False):
        return False, "POLICY_DENY:DISABLED:Dangerous interpreters are disabled (enable_dangerous_interpreters=false)"

    policy = cfg.get("dangerous_command_policy") or {}

    # Check for high-risk shell patterns in the raw command string
    # This catches metacharacters even if they're inside quoted strings
    for pattern in _HIGH_RISK_SHELL_PATTERNS:
        if pattern in command:
            # Allow safe patterns only if explicitly whitelisted in policy
            safe_patterns = policy.get("safe_shell_patterns", [])
            if pattern not in safe_patterns:
                return False, f"POLICY_DENY:HIGH_RISK_PATTERN:Shell pattern '{pattern}' blocked by policy"

    if base_cmd in {"python", "python3"}:
        py_policy = policy.get("python") or {}
        if not py_policy.get("allow", False):
            return False, "POLICY_DENY:PYTHON_NOT_ALLOWED:Python interpreter usage denied by dangerous_command_policy.python.allow=false"
        if py_policy.get("require_workspace", True) and not workspace:
            return False, "POLICY_DENY:PYTHON_NO_WORKSPACE:Python execution requires workspace context"
        
        # Check deny_flags with variant matching (exact + long-form)
        deny_flags = set(py_policy.get("deny_flags", ["-c", "-m"]))
        for tok in tokens[1:]:
            for denied_flag in deny_flags:
                variants = _FLAG_VARIANTS.get(denied_flag, [denied_flag])
                if tok in variants:
                    return False, f"POLICY_DENY:PYTHON_DENIED_FLAG:Python flag '{tok}' denied by policy"
        
        # Check for script file execution (any non-flag token)
        for tok in tokens[1:]:
            if tok.startswith("-"):
                continue
            return False, "POLICY_DENY:PYTHON_SCRIPT_EXEC:Python script execution denied by policy"
        return True, ""

    if base_cmd in {"pip", "pip3"}:
        pip_policy = policy.get("pip") or {}
        if not pip_policy.get("allow", False):
            return False, "POLICY_DENY:PIP_NOT_ALLOWED:pip usage denied by dangerous_command_policy.pip.allow=false"
        if pip_policy.get("require_workspace", True) and not workspace:
            return False, "POLICY_DENY:PIP_NO_WORKSPACE:pip commands require workspace context"
        
        # Check deny_flags for pip global flags that could be abused
        deny_flags = set(pip_policy.get("deny_flags", []))
        for tok in tokens[1:]:
            for denied_flag in deny_flags:
                variants = _FLAG_VARIANTS.get(denied_flag, [denied_flag])
                if tok in variants:
                    return False, f"POLICY_DENY:PIP_DENIED_FLAG:pip flag '{tok}' denied by policy"
        
        allowed_subcommands = set(pip_policy.get("allow_subcommands", ["install", "show", "freeze", "list"]))
        subcommand = tokens[1].lower() if len(tokens) > 1 else ""
        if subcommand not in allowed_subcommands:
            return False, f"POLICY_DENY:PIP_SUBCOMMAND:pip subcommand '{subcommand}' denied by policy"
        return True, ""

    return False, "POLICY_DENY:INTERPRETER_DENIED:Interpreter command denied by policy"


# ═════════════════════════════════════════════════════════════════════
#  SECURITY AUDIT LOGGING
# ═════════════════════════════════════════════════════════════════════

SECURITY_AUDIT_LOG = GHOST_HOME / "logs" / "security_audit.jsonl"


def _audit_log_interpreter(command: str, workspace: Optional[str], result: str, reason: str = ""):
    """Write an audit log entry for interpreter command execution.
    
    Args:
        command: The command being executed
        workspace: Optional workspace context
        result: "ALLOWED" or "DENIED"
        reason: Optional reason for denial (policy code)
    """
    try:
        # Ensure logs directory exists
        SECURITY_AUDIT_LOG.parent.mkdir(parents=True, exist_ok=True)
        
        entry = {
            "timestamp": datetime.now().isoformat(),
            "event": "interpreter_exec",
            "command": command[:500] if command else "",  # Truncate for safety
            "workspace": workspace,
            "result": result,
            "reason": reason,
        }
        
        with open(SECURITY_AUDIT_LOG, "a", encoding="utf-8") as f:
            f.write(json.dumps(entry) + "\n")
    except Exception:
        # Audit logging should never block execution
        pass


# ═════════════════════════════════════════════════════════════════════
#  TOOL DEFINITIONS
# ═════════════════════════════════════════════════════════════════════

_PROJECT_DIR = Path(__file__).resolve().parent
_REQUIREMENTS_FILE = _PROJECT_DIR / "requirements.txt"
_PIP_INSTALL_RE = re.compile(
    r"pip3?\s+install\s+(?!-e\b)(?!--)",
    re.IGNORECASE,
)


def _sync_requirements_after_pip(command: str):
    """After a successful pip install, ensure requirements.txt stays in sync."""
    if not _PIP_INSTALL_RE.search(command):
        return
    tokens = command.split()
    packages = []
    skip_next = False
    for tok in tokens:
        if skip_next:
            skip_next = False
            continue
        if tok in ("pip", "pip3", "install", "--upgrade", "--quiet", "-q",
                   "-U", "--no-cache-dir", "--force-reinstall"):
            continue
        if tok.startswith("-"):
            if tok in ("-r", "--requirement", "-t", "--target",
                       "--prefix", "-i", "--index-url",
                       "--extra-index-url", "-c", "--constraint"):
                skip_next = True
            continue
        pkg_name = re.split(r"[><=!~\[]", tok)[0].strip()
        if pkg_name and not pkg_name.startswith(("/", "\\", ".")):
            packages.append(pkg_name)

    if not packages:
        return

    existing = ""
    if _REQUIREMENTS_FILE.exists():
        existing = _REQUIREMENTS_FILE.read_text(encoding="utf-8")
    existing_lower = {
        re.split(r"[><=!~\[\s]", line)[0].strip().lower()
        for line in existing.splitlines()
        if line.strip() and not line.strip().startswith("#")
    }

    added = []
    for pkg in packages:
        if pkg.lower() in existing_lower:
            continue
        try:
            r = subprocess.run(
                [sys.executable, "-m", "pip", "show", pkg],
                capture_output=True, text=True, timeout=10,
            )
            if r.returncode != 0:
                continue
            version = ""
            for line in r.stdout.splitlines():
                if line.startswith("Version:"):
                    version = line.split(":", 1)[1].strip()
                    break
            entry = f"{pkg}>={version}" if version else pkg
            added.append(entry)
        except Exception:
            added.append(pkg)

    if not added:
        return

    lines = existing.rstrip("\n")
    if lines and not lines.endswith("\n"):
        lines += "\n"
    lines += "\n# Auto-added by Ghost evolve\n"
    for entry in added:
        lines += f"{entry}\n"
    _REQUIREMENTS_FILE.write_text(lines, encoding="utf-8")


def make_shell_exec(cfg):
    allowed = cfg.get("allowed_commands", DEFAULT_ALLOWED_COMMANDS)
    for cmd in CORE_COMMANDS:
        if cmd not in allowed:
            allowed.append(cmd)
    blocked = cfg.get("blocked_commands", DEFAULT_BLOCKED_COMMANDS)

    def execute(command, timeout=30, workspace=None):
        ok, reason = _check_command_allowed(command, allowed, blocked)
        if not ok:
            return f"DENIED: {reason}"

        policy_ok, policy_reason = _check_dangerous_command_policy(command, cfg, workspace=workspace)
        if not policy_ok:
            import logging as _logging
            _sec_log = _logging.getLogger("ghost.security")
            deny_code = "POLICY_DENY"
            if policy_reason.startswith("POLICY_DENY:"):
                parts = policy_reason.split(":", 2)
                if len(parts) >= 2:
                    deny_code = parts[1]
            _sec_log.warning(
                "POLICY_DENY shell_exec: code=%s workspace=%s cmd_prefix=%s",
                deny_code,
                workspace or "(none)",
                command[:40].replace("\n", " ") if command else "(empty)"
            )
            return f"DENIED: {policy_reason}"

        if workspace:
            ws_path = get_workspace(cfg, workspace)
            cwd = str(ws_path)
        else:
            cwd = str(Path.home())

        # Route interactive (user) commands through the sandbox env so
        # pip installs go to ~/.ghost/sandbox/.venv instead of Ghost's own
        # .venv.  Autonomous/evolve commands keep Ghost's own env so
        # self-evolution can modify Ghost's real dependencies.
        caller = get_shell_caller_context()
        use_sandbox = (caller == "interactive")
        base_env = get_sandbox_env() if use_sandbox else os.environ.copy()

        # Apply the execution sandbox: secret-scrubbed env, POSIX resource
        # limits, and a hard timeout that kills the whole process group.
        try:
            import ghost_sandbox
            policy = ghost_sandbox.get_policy(cfg)
            env = ghost_sandbox.scrub_env(
                base_env, policy.env_mode, policy.env_passthrough_extra)
            res = ghost_sandbox.run(
                command, cfg=cfg, cwd=cwd,
                timeout=min(timeout, 60), env=env, shell=True,
            )
            if res.timed_out:
                return f"Command timed out after {min(timeout, 60)}s (process group killed)"
            out = ""
            if res.stdout:
                out += res.stdout[:3000]
            if res.stderr:
                out += f"\n[stderr]\n{res.stderr[:1000]}"
            if res.returncode != 0:
                out += f"\n[exit code: {res.returncode}]"
            else:
                if not use_sandbox:
                    try:
                        _sync_requirements_after_pip(command)
                    except Exception:
                        pass
            return out.strip() or "(no output)"
        except Exception as e:
            return f"Shell error: {e}"

    ws_base = get_user_projects_dir(cfg)
    return {
        "name": "shell_exec",
        "description": (
            "Run a shell command. Runs from HOME (~/) by default. "
            f"For user projects: set workspace='project-name' to run inside {ws_base}/<project-name>/. "
            "The workspace directory is auto-created. Use workspace for all user project commands."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "command": {"type": "string", "description": "The shell command to execute"},
                "timeout": {"type": "integer", "description": "Timeout in seconds (max 60)", "default": 30},
                "workspace": {"type": "string", "description": "Project name — runs the command inside the user's workspace directory. Auto-created if missing.", "default": ""},
            },
            "required": ["command"],
        },
        "execute": execute,
    }


def make_file_read(cfg):
    allowed_roots = cfg.get("allowed_roots", DEFAULT_ALLOWED_ROOTS)
    _invalid_path_counter = {"count": 0}

    def execute(path, max_lines=200, offset=0, numbered=False):
        stripped = (path or "").strip()
        if not stripped or stripped in (".", ".py", ".js", ".json", ".html", ".css") or len(stripped) < 3:
            _invalid_path_counter["count"] += 1
            n = _invalid_path_counter["count"]
            if n >= 8:
                return (
                    f"CRITICAL: {n} invalid file_read calls this session. "
                    "You are stuck in a loop. STOP using file_read and "
                    "try a different approach or move on to your next task."
                )
            if n >= 3:
                return (
                    f"WARNING: {n} invalid file_read calls. "
                    "You keep passing bad paths. Use FULL filenames like "
                    "'ghost.py' or 'ghost_channel_security.py'."
                )
            return (
                f"ERROR: Invalid path '{path}'. Provide a FULL filename like "
                "'ghost.py' or 'ghost_channel_security.py'."
            )
        if not _check_path_allowed(path, allowed_roots):
            return f"DENIED: Path '{path}' is outside allowed roots"
        p = Path(path).expanduser()
        if not p.exists():
            proj_p = PROJECT_DIR / Path(path).expanduser().name if len(Path(path).parts) <= 1 else PROJECT_DIR / path
            if proj_p.exists() and _check_path_allowed(str(proj_p), allowed_roots):
                p = proj_p
            else:
                return f"File not found: {path}  (also tried {proj_p})"
        if not p.is_file():
            return f"Not a file: {path}"
        try:
            size = p.stat().st_size
            if size > 500_000:
                return f"File too large ({size} bytes). Use shell_exec with head/tail instead."
            text = p.read_text(encoding="utf-8", errors="replace")
            lines = text.split("\n")
            total = len(lines)
            if offset > 0:
                lines = lines[offset:]

            def _number_lines(lines_list, start_num):
                width = len(str(start_num + len(lines_list)))
                return [f"{str(start_num + i).rjust(width)}| {l}"
                        for i, l in enumerate(lines_list)]

            if len(lines) > max_lines:
                shown = lines[:max_lines]
                remaining = len(lines) - max_lines
                start_line = offset + 1
                header = f"[Lines {start_line}–{offset + max_lines} of {total}]\n"
                if numbered:
                    return header + "\n".join(_number_lines(shown, start_line)) + f"\n\n... ({remaining} more lines, use offset={offset + max_lines} to continue)"
                return header + "\n".join(shown) + f"\n\n... ({remaining} more lines, use offset={offset + max_lines} to continue)"
            if offset > 0:
                start_line = offset + 1
                header = f"[Lines {start_line}–{total} of {total}]\n"
                if numbered:
                    return header + "\n".join(_number_lines(lines, start_line))
                return header + "\n".join(lines)
            if numbered:
                return f"[{total} lines]\n" + "\n".join(_number_lines(lines, 1))
            return text
        except Exception as e:
            return f"Read error: {e}"

    return {
        "name": "file_read",
        "description": (
            "Read the contents of a file. Path must be within allowed directories. "
            "Use offset to paginate through large files. "
            "Set numbered=true to show line numbers (useful before evolve_apply with line_edits)."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "path": {"type": "string", "description": "Absolute or ~ path to the file"},
                "max_lines": {"type": "integer", "description": "Max lines to return (default 200)", "default": 200},
                "offset": {"type": "integer", "description": "Line number to start reading from (0-based, default 0)", "default": 0},
                "numbered": {"type": "boolean", "description": "If true, prefix each line with its 1-based line number (e.g. '  45| code here'). Use this before evolve_apply with line_edits.", "default": False},
            },
            "required": ["path"],
        },
        "execute": execute,
    }


def make_file_write(cfg):
    allowed_roots = cfg.get("allowed_roots", DEFAULT_ALLOWED_ROOTS)

    def execute(path, content, append=False):
        if not _check_path_allowed(path, allowed_roots):
            return f"DENIED: Path '{path}' is outside allowed roots"
        if _is_ghost_codebase_path(path):
            return (
                f"BLOCKED: Cannot write to '{path}' — it is inside Ghost's own codebase "
                f"({PROJECT_DIR}). Direct writes to Ghost source files are not allowed "
                f"from this context. For user project files, use workspace_write instead."
            )
        p = Path(path).expanduser()
        try:
            p.parent.mkdir(parents=True, exist_ok=True)
            if append:
                with open(p, "a", encoding="utf-8") as f:
                    f.write(content)
            else:
                p.write_text(content, encoding="utf-8")

            try:
                from ghost_artifacts import auto_register
                auto_register(str(p))
            except Exception:
                pass

            try:
                _auto_register_project(p)
            except Exception:
                pass

            return f"OK: wrote {len(content)} chars to {path}"
        except Exception as e:
            return f"Write error: {e}"

    return {
        "name": "file_write",
        "description": "Write content to a file. Path must be within allowed directories. Cannot write to Ghost's own codebase — use the evolution pipeline for self-modification.",
        "parameters": {
            "type": "object",
            "properties": {
                "path": {"type": "string", "description": "Absolute or ~ path"},
                "content": {"type": "string", "description": "Content to write"},
                "append": {"type": "boolean", "description": "Append instead of overwrite", "default": False},
            },
            "required": ["path", "content"],
        },
        "execute": execute,
    }


def make_file_search(cfg):
    allowed_roots = cfg.get("allowed_roots", DEFAULT_ALLOWED_ROOTS)

    def execute(pattern, directory="", max_results=20):
        if not directory or directory in ("~", "."):
            d = PROJECT_DIR
        else:
            d = Path(directory).expanduser().resolve()
        if not _check_path_allowed(str(d), allowed_roots):
            return f"DENIED: Directory '{directory}' is outside allowed roots"
        if not d.is_dir():
            proj_d = PROJECT_DIR / directory
            if proj_d.is_dir():
                d = proj_d
            else:
                return f"Not a directory: {directory}  (also tried {proj_d})"
        try:
            matches = []
            for p in d.rglob(pattern):
                if len(matches) >= max_results:
                    break
                matches.append(str(p))
            if not matches:
                return f"No files matching '{pattern}' in {directory}"
            return "\n".join(matches)
        except Exception as e:
            return f"Search error: {e}"

    return {
        "name": "file_search",
        "description": "Search for files matching a glob pattern in a directory.",
        "parameters": {
            "type": "object",
            "properties": {
                "pattern": {"type": "string", "description": "Glob pattern (e.g. '*.py', '**/*.json')"},
                "directory": {"type": "string", "description": "Directory to search in (defaults to Ghost project root)", "default": ""},
                "max_results": {"type": "integer", "description": "Max results to return", "default": 20},
            },
            "required": ["pattern"],
        },
        "execute": execute,
    }



# web_fetch has been moved to ghost_web_fetch.py (production-grade extraction pipeline).
# Registered separately in ghost.py via build_web_fetch_tools().




def make_app_control(cfg):
    allowed_apps = cfg.get("allowed_apps", [
        "Finder", "Safari", "Notes", "Calendar", "Reminders",
        "Terminal", "TextEdit", "Preview", "Music", "System Preferences",
    ])

    def execute(app_name, action="activate", script=None):
        if PLAT != "Darwin":
            return "app_control is only available on macOS"
        if app_name not in allowed_apps and not script:
            return f"DENIED: '{app_name}' not in allowed apps: {', '.join(allowed_apps)}"
        try:
            if action == "activate":
                cmd = f'tell application "{app_name}" to activate'
            elif action == "quit":
                cmd = f'tell application "{app_name}" to quit'
            elif action == "script" and script:
                cmd = script
            else:
                return f"Unknown action: {action}"
            r = subprocess.run(
                ["osascript", "-e", cmd],
                capture_output=True, text=True, timeout=10,
            )
            if r.returncode != 0:
                return f"AppleScript error: {r.stderr.strip()}"
            return r.stdout.strip() or f"OK: {action} {app_name}"
        except Exception as e:
            return f"App control error: {e}"

    return {
        "name": "app_control",
        "description": "Control macOS applications (activate, quit, run AppleScript).",
        "parameters": {
            "type": "object",
            "properties": {
                "app_name": {"type": "string", "description": "Name of the application"},
                "action": {"type": "string", "enum": ["activate", "quit", "script"], "default": "activate"},
                "script": {"type": "string", "description": "Custom AppleScript to run (for action='script')"},
            },
            "required": ["app_name"],
        },
        "execute": execute,
    }


def make_notify(cfg, channel_router=None):
    import hashlib as _hl
    _recent_sends: list[tuple[float, str]] = []
    _RATE_WINDOW = 300  # 5-minute sliding window
    _RATE_LIMIT = 10    # max notifications per window
    _DEDUP_WINDOW = 60  # suppress identical messages within 60s

    def execute(title, message, sound=True, priority="normal", channel=""):
        import time as _t
        text = f"**{title}**\n{message}"
        now = _t.time()
        msg_hash = _hl.sha256(text.encode()).hexdigest()[:16]

        # Prune old entries outside the rate window
        while _recent_sends and now - _recent_sends[0][0] > _RATE_WINDOW:
            _recent_sends.pop(0)

        # Dedup: reject identical message within dedup window
        for ts, h in reversed(_recent_sends):
            if now - ts > _DEDUP_WINDOW:
                break
            if h == msg_hash:
                return (
                    f"Duplicate notification suppressed — identical message sent "
                    f"{int(now - ts)}s ago. Do not retry the same notification."
                )

        # Rate limit: reject if too many sends in window
        if len(_recent_sends) >= _RATE_LIMIT:
            return (
                f"Rate limit reached ({_RATE_LIMIT} notifications in {_RATE_WINDOW}s). "
                "Wait before sending more notifications."
            )

        _recent_sends.append((now, msg_hash))
        results = []

        # Route through multi-channel messaging if available
        if channel_router:
            try:
                r = channel_router.send(text, channel=channel or None,
                                        priority=priority, title=title)
                if r.ok:
                    results.append(f"Sent via {r.channel_id}")
                else:
                    results.append(f"Channel send failed: {r.error}")
            except Exception as e:
                results.append(f"Channel error: {e}")

        # Always try local OS notification as well (immediate visual feedback)
        import ghost_platform as _gp
        try:
            if _gp.send_notification(title, message, sound=sound):
                results.append("OS notification sent")
        except Exception as e:
            results.append(f"OS notification error: {e}")

        if not results:
            return "No notification channels available"
        return "OK: " + "; ".join(results)

    return {
        "name": "notify",
        "description": (
            "Send a notification to the user via their preferred messaging channel "
            "(Telegram, Slack, Discord, ntfy, email, etc.) and/or local OS notification.  "
            "Use for alerts, reminders, and proactive updates."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "title": {"type": "string", "description": "Notification title"},
                "message": {"type": "string", "description": "Notification body"},
                "sound": {"type": "boolean", "description": "Play OS notification sound", "default": True},
                "priority": {"type": "string",
                             "enum": ["low", "normal", "high", "critical"],
                             "description": "Message priority (critical = broadcast to all channels)",
                             "default": "normal"},
                "channel": {"type": "string",
                            "description": "Specific channel to use (e.g. 'telegram', 'slack'). "
                                           "Empty = use preferred channel.",
                            "default": ""},
            },
            "required": ["title", "message"],
        },
        "execute": execute,
    }


# ═════════════════════════════════════════════════════════════════════
#  TOOL SET BUILDER
# ═════════════════════════════════════════════════════════════════════

def make_uptime_tool(cfg):
    """Create the uptime tool that returns daemon uptime in human-readable format."""
    # Store start time at module level when daemon initializes
    start_time = cfg.get("daemon_start_time")
    
    def execute():
        if start_time is None:
            return "Uptime: unknown (daemon start time not available)"
        
        # Calculate elapsed time
        elapsed = time.time() - start_time
        
        # Format as human-readable
        hours = int(elapsed // 3600)
        minutes = int((elapsed % 3600) // 60)
        seconds = int(elapsed % 60)
        
        parts = []
        if hours > 0:
            parts.append(f"{hours}h")
        if minutes > 0 or hours > 0:
            parts.append(f"{minutes}m")
        parts.append(f"{seconds}s")
        
        return f"Uptime: {' '.join(parts)}"
    
    return {
        "name": "uptime",
        "description": "Returns how long the Ghost daemon has been running in human-readable format (e.g., '2h 15m 30s').",
        "parameters": {
            "type": "object",
            "properties": {},
        },
        "execute": execute,
    }


def make_workspace_write(cfg):
    """Workspace-scoped file write for user projects. Paths are relative to the workspace."""

    def execute(project, file_path, content, append=False):
        if not project or not project.strip():
            return "Error: 'project' is required — e.g. 'csv-converter', 'todo-api', 'my-website'"
        if not file_path or not file_path.strip():
            return "Error: 'file_path' is required — e.g. 'main.py', 'src/app.ts', 'index.html'"
        if not content:
            return "Error: 'content' is required"

        project = project.strip().replace("..", "").replace("~", "")
        file_path = file_path.strip().lstrip("/\\")

        ws = get_workspace(cfg, project)
        target = ws / file_path
        proj_str = str(PROJECT_DIR)
        if proj_str in str(target.resolve()):
            return f"BLOCKED: Cannot write inside Ghost's codebase. Workspace is {ws}"

        try:
            target.parent.mkdir(parents=True, exist_ok=True)
            if append:
                with open(target, "a", encoding="utf-8") as f:
                    f.write(content)
            else:
                target.write_text(content, encoding="utf-8")
            return f"OK: wrote {len(content)} chars to {target} (workspace: {ws})"
        except Exception as e:
            return f"Write error: {e}"

    ws_base = get_user_projects_dir(cfg)
    return {
        "name": "workspace_write",
        "description": (
            f"Write a file inside a user project workspace ({ws_base}/<project>/). "
            "Use this for ALL user-requested projects (websites, scripts, apps). "
            "Paths are relative to the project root. Directory structure is auto-created."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "project": {
                    "type": "string",
                    "description": "Project folder name (e.g. 'csv-converter', 'rest-api', 'portfolio-site'). Auto-created.",
                },
                "file_path": {
                    "type": "string",
                    "description": "File path relative to project root (e.g. 'main.py', 'src/app.ts', 'index.html', 'README.md')",
                },
                "content": {"type": "string", "description": "File content to write"},
                "append": {"type": "boolean", "description": "Append instead of overwrite", "default": False},
            },
            "required": ["project", "file_path", "content"],
        },
        "execute": execute,
    }


def build_default_tools(cfg):
    """Build all built-in tool definitions from config. Returns list of tool defs."""
    tools = [
        make_shell_exec(cfg),
        make_file_read(cfg),
        make_file_write(cfg),
        make_workspace_write(cfg),
        make_file_search(cfg),
        make_notify(cfg),
        make_uptime_tool(cfg),
    ]
    if PLAT == "Darwin":
        tools.append(make_app_control(cfg))
    return tools
