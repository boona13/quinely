"""
ghost_sandbox.py — Cross-platform execution sandbox for agent shell commands.

Ghost previously had only a *pip-isolation venv* plus substring blocklists. There
were no real resource limits, no environment scrubbing, and no reliable kill of
runaway/orphan child processes. A shell command therefore ran with roughly the
same privileges (and secrets) as the daemon user.

This module adds a real, dependency-free sandbox layer that works on macOS, Linux
and Windows using only the standard library:

  * Resource limits (POSIX): CPU time, max file size, core-dump suppression, and
    optional address-space / process / open-file caps via ``resource.setrlimit``
    applied in the child's ``preexec_fn``.
  * Reliable timeouts with **process-group kill** — children are started in a new
    session/process-group so a timeout (or stop) kills the whole tree, not just
    the top process, preventing orphans that outlive the timeout.
  * **Environment scrubbing** — secret-looking variables (API keys, tokens,
    passwords, provider creds) are removed from the subprocess environment by
    default so executed commands can't exfiltrate the daemon's secrets.
  * Optional OS-level isolation on Linux via ``bwrap`` (bubblewrap) when present —
    auto-detected, never required. Falls back to the above controls otherwise.

Everything degrades gracefully: on Windows ``resource`` is unavailable so rlimits
are skipped (timeout + process-group kill + env scrubbing still apply), and any
unexpected error never blocks execution harder than the caller intended.
"""

from __future__ import annotations

import os
import re
import shutil
import signal
import subprocess
import sys
import logging
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Tuple

log = logging.getLogger("quinely.sandbox")

try:
    import resource as _resource  # POSIX only
except Exception:
    _resource = None

_IS_WINDOWS = sys.platform.startswith("win")
_MB = 1024 * 1024

# --------------------------------------------------------------------------
# Environment scrubbing
# --------------------------------------------------------------------------

# Variable *names* matching this pattern are treated as secrets and removed
# from the subprocess environment in "scrub_secrets" mode.
_SECRET_KEY_RE = re.compile(
    r"(API[_-]?KEY|ACCESS[_-]?KEY|SECRET|TOKEN|PASSW(OR)?D|PASSWD|CREDENTIAL|"
    r"PRIVATE[_-]?KEY|CLIENT[_-]?SECRET|BEARER|SESSION[_-]?KEY|"
    r"OPENAI|ANTHROPIC|OPENROUTER|DEEPSEEK|GEMINI|GOOGLE_AI|GROK|XAI|"
    r"HF[_-]?TOKEN|HUGGINGFACE|GITHUB[_-]?TOKEN|GH[_-]?TOKEN|"
    r"TELEGRAM|DISCORD|WHATSAPP|SLACK|STRIPE|TWILIO|SENDGRID|"
    r"AWS[_-]|AZURE|GCP|GOOGLE_APPLICATION|"
    r"NPM[_-]?TOKEN|PYPI|DOCKER[_-]?PASSWORD)",
    re.IGNORECASE,
)

# Always preserved in "minimal" mode (non-secret, needed for normal tooling).
_MINIMAL_KEEP = {
    "PATH", "HOME", "USER", "LOGNAME", "SHELL", "LANG", "LC_ALL", "LC_CTYPE",
    "TERM", "TMPDIR", "TEMP", "TMP", "PWD", "VIRTUAL_ENV", "PYTHONPATH",
    "PYTHONHOME", "PYTHONIOENCODING", "SSL_CERT_FILE", "SSL_CERT_DIR",
    "SYSTEMROOT", "WINDIR", "COMSPEC", "PATHEXT", "NUMBER_OF_PROCESSORS",
    "PROCESSOR_ARCHITECTURE", "APPDATA", "LOCALAPPDATA", "USERPROFILE",
    "HOMEDRIVE", "HOMEPATH",
}


def is_secret_key(name: str) -> bool:
    return bool(_SECRET_KEY_RE.search(name or ""))


def scrub_env(base_env: Optional[Dict[str, str]], mode: str = "scrub_secrets",
              extra_keep: Optional[List[str]] = None) -> Dict[str, str]:
    """Return a copy of ``base_env`` with secrets handled per ``mode``.

    mode:
      * "full"          — no scrubbing (legacy behaviour)
      * "scrub_secrets" — remove only secret-looking variables (default)
      * "minimal"       — keep only a safe allowlist (+ extra_keep)
    """
    env = dict(base_env if base_env is not None else os.environ)
    keep = set(_MINIMAL_KEEP)
    if extra_keep:
        keep.update(extra_keep)

    if mode == "full":
        return env
    if mode == "minimal":
        return {k: v for k, v in env.items() if k in keep}
    # default: scrub_secrets
    return {k: v for k, v in env.items()
            if (k in keep) or not is_secret_key(k)}


# --------------------------------------------------------------------------
# Policy
# --------------------------------------------------------------------------

@dataclass
class SandboxPolicy:
    enabled: bool = True
    cpu_seconds: int = 60          # RLIMIT_CPU (one-shot commands only)
    file_size_mb: int = 512        # RLIMIT_FSIZE
    memory_mb: int = 0             # RLIMIT_AS (0 = disabled; virtual-mem footgun)
    max_processes: int = 0         # RLIMIT_NPROC (0 = disabled; per-user footgun)
    open_files: int = 0            # RLIMIT_NOFILE (0 = disabled)
    no_core_dumps: bool = True     # RLIMIT_CORE = 0
    env_mode: str = "scrub_secrets"
    env_passthrough_extra: List[str] = field(default_factory=list)
    wall_timeout: int = 60
    isolation: str = "auto"        # auto | none | bwrap
    network: str = "allow"         # allow | deny (deny needs bwrap/unshare)


def get_policy(cfg: Optional[dict]) -> SandboxPolicy:
    raw = (cfg or {}).get("sandbox", {}) or {}
    p = SandboxPolicy()
    for k in vars(p):
        if k in raw and raw[k] is not None:
            setattr(p, k, raw[k])
    return p


# --------------------------------------------------------------------------
# Resource limits (POSIX)
# --------------------------------------------------------------------------

def _make_preexec(policy: SandboxPolicy, apply_cpu: bool):
    """Build a preexec_fn that starts a new session and applies rlimits.

    Returns None on platforms without ``resource`` / ``os.setsid`` (Windows).
    """
    if _IS_WINDOWS:
        return None

    def _preexec():
        # New session => new process group leader, so the whole tree can be
        # killed together on timeout/stop.
        try:
            os.setsid()
        except Exception:
            pass
        if _resource is None:
            return
        def _set(res, soft_hard):
            try:
                _resource.setrlimit(res, soft_hard)
            except Exception:
                pass
        if apply_cpu and policy.cpu_seconds and policy.cpu_seconds > 0:
            # +5s hard grace so the process can be SIGXCPU'd then SIGKILL'd.
            _set(_resource.RLIMIT_CPU,
                 (int(policy.cpu_seconds), int(policy.cpu_seconds) + 5))
        if policy.file_size_mb and policy.file_size_mb > 0:
            n = int(policy.file_size_mb) * _MB
            _set(_resource.RLIMIT_FSIZE, (n, n))
        if policy.no_core_dumps:
            _set(_resource.RLIMIT_CORE, (0, 0))
        if policy.memory_mb and policy.memory_mb > 0 and hasattr(_resource, "RLIMIT_AS"):
            n = int(policy.memory_mb) * _MB
            _set(_resource.RLIMIT_AS, (n, n))
        if policy.max_processes and policy.max_processes > 0 and hasattr(_resource, "RLIMIT_NPROC"):
            _set(_resource.RLIMIT_NPROC,
                 (int(policy.max_processes), int(policy.max_processes)))
        if policy.open_files and policy.open_files > 0:
            _set(_resource.RLIMIT_NOFILE,
                 (int(policy.open_files), int(policy.open_files)))

    return _preexec


def popen_kwargs(cfg: Optional[dict], persistent: bool = False) -> dict:
    """Extra kwargs for subprocess.Popen/run to enforce the sandbox.

    ``persistent=True`` (shell sessions / background procs) omits the cumulative
    CPU-time limit, which would otherwise kill a long-lived shell.
    """
    policy = get_policy(cfg)
    kw: dict = {}
    if not policy.enabled:
        # Still isolate the process group for clean kills.
        if _IS_WINDOWS:
            kw["creationflags"] = getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)
        else:
            kw["start_new_session"] = True
        return kw

    if _IS_WINDOWS:
        kw["creationflags"] = getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)
    else:
        pre = _make_preexec(policy, apply_cpu=not persistent)
        if pre is not None:
            kw["preexec_fn"] = pre
        else:
            kw["start_new_session"] = True
    return kw


# --------------------------------------------------------------------------
# Optional OS-level isolation (Linux bwrap)
# --------------------------------------------------------------------------

def bwrap_available() -> bool:
    return (not _IS_WINDOWS) and sys.platform.startswith("linux") \
        and shutil.which("bwrap") is not None


def maybe_wrap_command(command: str, cfg: Optional[dict], cwd: Optional[str]) -> Tuple[str, bool]:
    """If Linux bubblewrap is available and requested, wrap ``command`` to run in
    an isolated mount/namespace (read-only system, writable cwd/tmp, optional net
    deny). Returns (possibly_wrapped_command, wrapped?)."""
    policy = get_policy(cfg)
    if policy.isolation == "none":
        return command, False
    if policy.isolation in ("auto", "bwrap") and bwrap_available():
        work = cwd or os.getcwd()
        args = [
            "bwrap", "--ro-bind", "/", "/",
            "--dev", "/dev", "--proc", "/proc", "--tmpfs", "/tmp",
            "--bind", work, work, "--chdir", work,
            "--die-with-parent",
        ]
        if policy.network == "deny":
            args.append("--unshare-net")
        # Run the user's command through a shell inside the sandbox.
        shell = "/bin/sh"
        args += [shell, "-c", command]
        return subprocess.list2cmdline(args), True
    return command, False


# --------------------------------------------------------------------------
# Run helper with process-group kill on timeout
# --------------------------------------------------------------------------

@dataclass
class SandboxResult:
    returncode: int
    stdout: str
    stderr: str
    timed_out: bool = False


def run(command, cfg: Optional[dict] = None, cwd: Optional[str] = None,
        timeout: int = 30, env: Optional[Dict[str, str]] = None,
        shell: bool = True, text: bool = True) -> SandboxResult:
    """Run a command under the sandbox: scrubbed env, resource limits, and a
    hard wall-clock timeout that kills the entire process group."""
    policy = get_policy(cfg)
    wall = min(int(timeout), int(policy.wall_timeout) if policy.wall_timeout else int(timeout))

    if env is None:
        env = scrub_env(os.environ, policy.env_mode, policy.env_passthrough_extra)

    run_cmd = command
    if shell and isinstance(command, str):
        wrapped, did = maybe_wrap_command(command, cfg, cwd)
        if did:
            run_cmd = wrapped

    kw = popen_kwargs(cfg, persistent=False)

    try:
        proc = subprocess.Popen(
            run_cmd, shell=shell, cwd=cwd, env=env,
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=text, **kw,
        )
    except Exception as e:
        return SandboxResult(returncode=127, stdout="", stderr=f"spawn error: {e}")

    try:
        out, err = proc.communicate(timeout=wall)
        return SandboxResult(returncode=proc.returncode, stdout=out or "",
                             stderr=err or "")
    except subprocess.TimeoutExpired:
        _kill_tree(proc)
        try:
            out, err = proc.communicate(timeout=5)
        except Exception:
            out, err = "", ""
        return SandboxResult(returncode=-1, stdout=out or "",
                             stderr=(err or "") + f"\n[sandbox] killed after {wall}s",
                             timed_out=True)


def _kill_tree(proc: "subprocess.Popen") -> None:
    """Kill the process group/tree started for ``proc``."""
    try:
        if _IS_WINDOWS:
            subprocess.run(["taskkill", "/F", "/T", "/PID", str(proc.pid)],
                           capture_output=True)
            return
        pgid = os.getpgid(proc.pid)
        try:
            os.killpg(pgid, signal.SIGTERM)
        except Exception:
            pass
        # Brief grace, then SIGKILL.
        try:
            proc.wait(timeout=3)
            return
        except Exception:
            pass
        try:
            os.killpg(pgid, signal.SIGKILL)
        except Exception:
            try:
                proc.kill()
            except Exception:
                pass
    except Exception:
        try:
            proc.kill()
        except Exception:
            pass


def kill_session_process(proc: "subprocess.Popen") -> None:
    """Public helper to kill a persistent shell's whole process group."""
    _kill_tree(proc)


def status(cfg: Optional[dict] = None) -> dict:
    """Introspection for the dashboard / status endpoints."""
    p = get_policy(cfg)
    return {
        "enabled": p.enabled,
        "platform": sys.platform,
        "rlimits_supported": _resource is not None and not _IS_WINDOWS,
        "cpu_seconds": p.cpu_seconds,
        "file_size_mb": p.file_size_mb,
        "memory_mb": p.memory_mb,
        "max_processes": p.max_processes,
        "no_core_dumps": p.no_core_dumps,
        "env_mode": p.env_mode,
        "isolation": p.isolation,
        "bwrap_available": bwrap_available(),
        "network": p.network,
    }
