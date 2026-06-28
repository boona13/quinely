"""
Ghost Shell Sessions — Persistent Shell & Background Process Management

Provides two capabilities alongside the existing one-shot shell_exec:

1. Interactive Sessions: Named shell subprocesses (/bin/sh on Unix, cmd.exe on
   Windows) that persist across tool calls. Environment, cwd, and shell state
   carry over between commands.

2. Background Processes: Fire-and-forget commands with output capture. The LLM
   can start servers/watchers, check their output, and kill them.

Security: Every command is validated through the same allowlist/blocklist/
dangerous-interpreter gates used by shell_exec in ghost_tools.py.
"""

import logging
import os
import platform
import signal
import subprocess

import ghost_platform
import threading
import time
import uuid
from collections import deque
from pathlib import Path
from typing import Optional

from ghost_tools import (
    _check_command_allowed,
    _check_dangerous_command_policy,
    CORE_COMMANDS,
    DEFAULT_ALLOWED_COMMANDS,
    DEFAULT_BLOCKED_COMMANDS,
    get_workspace,
)

log = logging.getLogger("ghost.shell_sessions")

_MARKER_PREFIX = "__GHOST_END_"
_DEFAULT_SESSION = "default"
_IDLE_TIMEOUT_S = 600  # 10 minutes
_CLEANUP_INTERVAL_S = 30
_MAX_CMD_TIMEOUT = 300  # 5 minutes per interactive command
_OUTPUT_LINE_LIMIT = 500
_RESULT_CHAR_LIMIT = 4000


# ═══════════════════════════════════════════════════════════════
#  INTERACTIVE SESSION
# ═══════════════════════════════════════════════════════════════

class InteractiveSession:
    """A persistent /bin/sh subprocess with marker-based I/O.

    Commands are sent via stdin. A unique end-marker is appended after each
    command so we can reliably detect when output is complete and extract
    the exit code.
    """

    def __init__(self, name: str, cwd: Optional[str] = None):
        self.name = name
        self.created_at = time.time()
        self.last_used = time.time()
        self._lock = threading.Lock()
        self._output_lines: deque = deque(maxlen=_OUTPUT_LINE_LIMIT)
        self._pending_output: list = []
        self._marker_event = threading.Event()
        self._current_marker: Optional[str] = None
        self._marker_exit_code: Optional[int] = None
        self._closed = False

        shell = "/bin/sh"
        if platform.system() == "Windows":
            shell = "cmd.exe"

        # Sandbox: secret-scrubbed env + resource limits + own process group so
        # the whole shell tree can be killed cleanly. CPU limit is omitted for
        # persistent sessions (it is cumulative and would kill a long-lived shell).
        try:
            import ghost_sandbox
            env = ghost_sandbox.scrub_env(os.environ)
            _sb_kwargs = ghost_sandbox.popen_kwargs(None, persistent=True)
        except Exception:
            env = os.environ.copy()
            _sb_kwargs = {}
        env["TERM"] = "dumb"

        self._proc = subprocess.Popen(
            [shell],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            cwd=cwd or str(Path.home()),
            env=env,
            bufsize=0,
            **_sb_kwargs,
        )

        self._reader = threading.Thread(
            target=self._read_loop,
            name=f"shell-session-{name}",
            daemon=True,
        )
        self._reader.start()

    @property
    def alive(self) -> bool:
        return self._proc.poll() is None and not self._closed

    @property
    def idle_seconds(self) -> float:
        return time.time() - self.last_used

    def send(self, command: str, timeout: float = 30) -> str:
        """Send a command and wait for output. Returns output + exit code."""
        if not self.alive:
            return "[session dead] Shell process has exited."

        with self._lock:
            self.last_used = time.time()
            marker_id = uuid.uuid4().hex[:12]
            marker = f"{_MARKER_PREFIX}{marker_id}__"
            self._current_marker = marker
            self._marker_exit_code = None
            self._pending_output.clear()
            self._marker_event.clear()

            echo_cmd = ghost_platform.exit_code_echo_cmd(marker)
            full_cmd = f"{command}\n{echo_cmd}\n"
            try:
                self._proc.stdin.write(full_cmd.encode())
                self._proc.stdin.flush()
            except (BrokenPipeError, OSError):
                self._closed = True
                return "[session dead] Shell process has exited."

            if not self._marker_event.wait(timeout=min(timeout, _MAX_CMD_TIMEOUT)):
                output_so_far = "\n".join(self._pending_output)
                self._pending_output.clear()
                return (
                    f"{output_so_far}\n"
                    f"[timeout after {timeout}s — command may still be running in session '{self.name}']"
                ).strip()

            output = "\n".join(self._pending_output)
            exit_code = self._marker_exit_code
            self._pending_output.clear()

            if len(output) > _RESULT_CHAR_LIMIT:
                output = output[:_RESULT_CHAR_LIMIT] + "\n... (truncated)"

            if exit_code and exit_code != 0:
                output += f"\n[exit code: {exit_code}]"

            return output.strip() or "(no output)"

    def kill(self):
        """Terminate the shell process (and its whole process group)."""
        self._closed = True
        try:
            import ghost_sandbox
            ghost_sandbox.kill_session_process(self._proc)
            return
        except Exception:
            pass
        try:
            self._proc.terminate()
            self._proc.wait(timeout=3)
        except subprocess.TimeoutExpired:
            self._proc.kill()
            self._proc.wait(timeout=2)
        except Exception:
            pass

    def _read_loop(self):
        """Continuously read stdout, detect end-markers."""
        try:
            for raw_line in iter(self._proc.stdout.readline, b""):
                if self._closed:
                    break
                line = raw_line.decode("utf-8", errors="replace").rstrip("\n\r")
                self._output_lines.append(line)

                if self._current_marker and self._current_marker in line:
                    suffix = line.split(self._current_marker, 1)[1]
                    try:
                        self._marker_exit_code = int(suffix)
                    except (ValueError, TypeError):
                        self._marker_exit_code = None
                    self._marker_event.set()
                else:
                    self._pending_output.append(line)
        except Exception:
            pass
        finally:
            self._closed = True
            self._marker_event.set()
            try:
                self._proc.wait(timeout=2)
            except Exception:
                pass


# ═══════════════════════════════════════════════════════════════
#  BACKGROUND PROCESS
# ═══════════════════════════════════════════════════════════════

class BackgroundProcess:
    """A fire-and-forget subprocess with output capture."""

    def __init__(self, command: str, label: str,
                 cwd: Optional[str] = None):
        self.command = command
        self.label = label
        self.created_at = time.time()
        self._output: deque = deque(maxlen=_OUTPUT_LINE_LIMIT)
        self._lock = threading.Lock()

        try:
            import ghost_sandbox
            _bg_env = ghost_sandbox.scrub_env(os.environ)
            _sb_kwargs = ghost_sandbox.popen_kwargs(None, persistent=True)
        except Exception:
            _bg_env = os.environ.copy()
            _sb_kwargs = {}

        popen_kwargs: dict = {
            "stdout": subprocess.PIPE,
            "stderr": subprocess.STDOUT,
            "cwd": cwd or str(Path.home()),
            "bufsize": 0,
            "env": _bg_env,
            **_sb_kwargs,
        }
        if ghost_platform.IS_WIN:
            self._proc = subprocess.Popen(
                ["cmd.exe", "/c", command], **popen_kwargs,
            )
        else:
            self._proc = subprocess.Popen(
                command, shell=True, **popen_kwargs,
            )
        self.pid = self._proc.pid

        self._reader = threading.Thread(
            target=self._drain,
            name=f"shell-bg-{label}",
            daemon=True,
        )
        self._reader.start()

    @property
    def alive(self) -> bool:
        return self._proc.poll() is None

    @property
    def exit_code(self) -> Optional[int]:
        return self._proc.poll()

    @property
    def runtime_seconds(self) -> float:
        return time.time() - self.created_at

    def read_output(self, lines: int = 50) -> str:
        with self._lock:
            recent = list(self._output)[-lines:]
        return "\n".join(recent)

    def kill(self):
        """Terminate the process (and its whole process group) cross-platform."""
        try:
            import ghost_sandbox
            ghost_sandbox.kill_session_process(self._proc)
            return
        except Exception:
            pass
        try:
            if ghost_platform.IS_WIN:
                ghost_platform.kill_process(self.pid)
            else:
                self._proc.terminate()
            self._proc.wait(timeout=3)
        except subprocess.TimeoutExpired:
            if ghost_platform.IS_WIN:
                ghost_platform.kill_process_group(self.pid)
            else:
                self._proc.kill()
            self._proc.wait(timeout=2)
        except Exception:
            pass

    def _drain(self):
        """Read output until process exits."""
        try:
            for raw_line in iter(self._proc.stdout.readline, b""):
                line = raw_line.decode("utf-8", errors="replace").rstrip("\n\r")
                with self._lock:
                    self._output.append(line)
        except Exception:
            pass
        finally:
            try:
                self._proc.wait(timeout=2)
            except Exception:
                pass


# ═══════════════════════════════════════════════════════════════
#  SESSION MANAGER
# ═══════════════════════════════════════════════════════════════

class ShellSessionManager:
    """Manages named interactive sessions and background processes.

    Thread-safe. Handles cleanup of idle sessions and dead processes.
    """

    def __init__(self, cfg: dict):
        self._cfg = cfg
        self._sessions: dict[str, InteractiveSession] = {}
        self._bg_procs: dict[str, BackgroundProcess] = {}
        self._lock = threading.Lock()
        self._max_sessions = cfg.get("max_shell_sessions", 5)
        self._max_bg = cfg.get("max_background_processes", 10)
        self._running = True

        self._cleanup_thread = threading.Thread(
            target=self._cleanup_loop,
            name="shell-session-cleanup",
            daemon=True,
        )
        self._cleanup_thread.start()

    # ── Security ──────────────────────────────────────────────

    def _validate_command(self, command: str, workspace: Optional[str] = None) -> tuple:
        """Run the same security gates as shell_exec. Returns (ok, reason)."""
        allowed = self._cfg.get("allowed_commands", DEFAULT_ALLOWED_COMMANDS)
        for cmd in CORE_COMMANDS:
            if cmd not in allowed:
                allowed.append(cmd)
        blocked = self._cfg.get("blocked_commands", DEFAULT_BLOCKED_COMMANDS)

        ok, reason = _check_command_allowed(command, allowed, blocked)
        if not ok:
            return False, reason

        ok, reason = _check_dangerous_command_policy(command, self._cfg, workspace=workspace)
        if not ok:
            # Emit structured security log event for audit telemetry
            deny_code = "POLICY_DENY"
            if reason.startswith("POLICY_DENY:"):
                parts = reason.split(":", 2)
                if len(parts) >= 2:
                    deny_code = parts[1]
            log.warning(
                "POLICY_DENY session/bg: code=%s workspace=%s cmd_prefix=%s",
                deny_code,
                workspace or "(none)",
                command[:40].replace("\n", " ") if command else "(empty)"
            )
            return False, reason

        return True, ""

    # ── Interactive Sessions ──────────────────────────────────

    def session_exec(self, command: str, session: str = _DEFAULT_SESSION,
                     timeout: float = 30, cwd: Optional[str] = None) -> str:
        """Send a command to a named interactive session."""
        ok, reason = self._validate_command(command)
        if not ok:
            return f"DENIED: {reason}"

        with self._lock:
            sess = self._sessions.get(session)
            if sess and not sess.alive:
                sess.kill()
                del self._sessions[session]
                sess = None

            if not sess:
                if len(self._sessions) >= self._max_sessions:
                    oldest = min(self._sessions.values(),
                                 key=lambda s: s.last_used)
                    oldest.kill()
                    del self._sessions[oldest.name]

                sess = InteractiveSession(session, cwd=cwd)
                self._sessions[session] = sess
                log.info("Created shell session '%s' (cwd=%s)", session, cwd)
            elif cwd:
                sess.send(f"cd {cwd}", timeout=5)

        return sess.send(command, timeout=timeout)

    def list_sessions(self) -> list[dict]:
        with self._lock:
            result = []
            for name, sess in list(self._sessions.items()):
                result.append({
                    "name": name,
                    "alive": sess.alive,
                    "idle_seconds": round(sess.idle_seconds, 1),
                    "created_at": sess.created_at,
                })
            return result

    def kill_session(self, name: str) -> str:
        with self._lock:
            sess = self._sessions.pop(name, None)
        if not sess:
            return f"Session '{name}' not found."
        sess.kill()
        return f"Session '{name}' terminated."

    # ── Background Processes ──────────────────────────────────

    def bg_start(self, command: str, label: Optional[str] = None,
                 cwd: Optional[str] = None) -> str:
        """Start a background process."""
        ok, reason = self._validate_command(command)
        if not ok:
            return f"DENIED: {reason}"

        if not label:
            label = f"bg-{uuid.uuid4().hex[:8]}"

        with self._lock:
            self._reap_dead_bg()

            if label in self._bg_procs:
                existing = self._bg_procs[label]
                if existing.alive:
                    return (
                        f"Background process '{label}' already running "
                        f"(pid={existing.pid}, runtime={existing.runtime_seconds:.0f}s). "
                        f"Kill it first with shell_bg_kill(label='{label}')."
                    )
                del self._bg_procs[label]

            if len(self._bg_procs) >= self._max_bg:
                return (
                    f"Max background processes ({self._max_bg}) reached. "
                    f"Kill some first with shell_bg_kill."
                )

            proc = BackgroundProcess(command, label, cwd=cwd)
            self._bg_procs[label] = proc
            log.info("Started background process '%s' (pid=%d): %s",
                     label, proc.pid, command[:80])

        return (
            f"Background process started.\n"
            f"  label: {label}\n"
            f"  pid: {proc.pid}\n"
            f"  command: {command}\n"
            f"Use shell_bg_status(label='{label}') to check output."
        )

    def bg_status(self, label: str, lines: int = 50) -> str:
        with self._lock:
            proc = self._bg_procs.get(label)
        if not proc:
            return f"No background process with label '{label}'. Use shell_bg_start to create one."

        status = "running" if proc.alive else f"exited (code={proc.exit_code})"
        output = proc.read_output(lines)

        parts = [
            f"[{label}] {status} | pid={proc.pid} | runtime={proc.runtime_seconds:.1f}s",
            f"command: {proc.command}",
        ]
        if output:
            parts.append(f"--- output (last {lines} lines) ---")
            if len(output) > _RESULT_CHAR_LIMIT:
                output = output[-_RESULT_CHAR_LIMIT:]
                parts.append("... (truncated)")
            parts.append(output)
        else:
            parts.append("(no output yet)")

        return "\n".join(parts)

    def bg_kill(self, label: str) -> str:
        with self._lock:
            proc = self._bg_procs.pop(label, None)
        if not proc:
            return f"No background process with label '{label}'."

        was_alive = proc.alive
        proc.kill()
        if was_alive:
            return f"Background process '{label}' (pid={proc.pid}) terminated."
        return f"Background process '{label}' (pid={proc.pid}) was already exited (code={proc.exit_code})."

    def list_bg(self) -> list[dict]:
        with self._lock:
            result = []
            for label, proc in list(self._bg_procs.items()):
                result.append({
                    "label": label,
                    "command": proc.command[:100],
                    "alive": proc.alive,
                    "pid": proc.pid,
                    "runtime_seconds": round(proc.runtime_seconds, 1),
                    "exit_code": proc.exit_code,
                })
            return result

    # ── Cleanup ───────────────────────────────────────────────

    def _reap_dead_bg(self):
        """Remove dead background processes (called under lock)."""
        dead = [l for l, p in self._bg_procs.items()
                if not p.alive and p.runtime_seconds > 60]
        for label in dead:
            del self._bg_procs[label]

    def _cleanup_loop(self):
        """Periodically reap idle sessions and dead processes."""
        while self._running:
            time.sleep(_CLEANUP_INTERVAL_S)
            try:
                with self._lock:
                    idle = [
                        name for name, sess in self._sessions.items()
                        if sess.idle_seconds > _IDLE_TIMEOUT_S or not sess.alive
                    ]
                    for name in idle:
                        sess = self._sessions.pop(name, None)
                        if sess:
                            sess.kill()
                            log.info("Reaped idle shell session '%s'", name)

                    self._reap_dead_bg()
            except Exception as e:
                log.warning("Shell session cleanup error: %s", e)

    def cleanup_all(self):
        """Kill everything. Called on daemon shutdown."""
        self._running = False
        with self._lock:
            for name, sess in list(self._sessions.items()):
                try:
                    sess.kill()
                except Exception:
                    pass
            self._sessions.clear()

            for label, proc in list(self._bg_procs.items()):
                try:
                    proc.kill()
                except Exception:
                    pass
            self._bg_procs.clear()
        log.info("All shell sessions and background processes cleaned up.")


# ═══════════════════════════════════════════════════════════════
#  LLM TOOLS
# ═══════════════════════════════════════════════════════════════

def build_shell_session_tools(cfg: dict,
                              manager: ShellSessionManager) -> list[dict]:
    """Build LLM-callable tools for persistent shells and background processes."""

    def session_exec(command, session=_DEFAULT_SESSION, timeout=30, cwd=""):
        return manager.session_exec(
            command=command,
            session=session or _DEFAULT_SESSION,
            timeout=timeout,
            cwd=cwd or None,
        )

    def bg_start_exec(command, label="", cwd=""):
        return manager.bg_start(
            command=command,
            label=label or None,
            cwd=cwd or None,
        )

    def bg_status_exec(label, lines=50):
        return manager.bg_status(label=label, lines=lines)

    def bg_kill_exec(label):
        return manager.bg_kill(label=label)

    return [
        {
            "name": "shell_session",
            "description": (
                "Run a command in a persistent shell session. Unlike shell_exec (one-shot), "
                "sessions persist between calls — environment variables, working directory, "
                "and shell state carry over. Use for multi-step workflows: cd into a dir, "
                "set env vars, then build/test. Sessions are named — use different names "
                "for independent workflows. Default session name is 'default'."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "command": {
                        "type": "string",
                        "description": "The shell command to execute",
                    },
                    "session": {
                        "type": "string",
                        "description": "Session name (reused across calls). Default: 'default'",
                        "default": "default",
                    },
                    "timeout": {
                        "type": "integer",
                        "description": "Max seconds to wait for output (default 30, max 300)",
                        "default": 30,
                    },
                    "cwd": {
                        "type": "string",
                        "description": "Working directory (only used when creating a new session)",
                        "default": "",
                    },
                },
                "required": ["command"],
            },
            "execute": session_exec,
        },
        {
            "name": "shell_bg_start",
            "description": (
                "Start a long-running command in the background (e.g. dev servers, "
                "watchers, builds). Returns immediately with a label. Use "
                "shell_bg_status(label) to check output and shell_bg_kill(label) to stop."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "command": {
                        "type": "string",
                        "description": "The command to run in background",
                    },
                    "label": {
                        "type": "string",
                        "description": "Human-readable label for this process (auto-generated if empty)",
                        "default": "",
                    },
                    "cwd": {
                        "type": "string",
                        "description": "Working directory for the process",
                        "default": "",
                    },
                },
                "required": ["command"],
            },
            "execute": bg_start_exec,
        },
        {
            "name": "shell_bg_status",
            "description": (
                "Check the status and recent output of a background process "
                "started with shell_bg_start. Shows whether it is running or exited, "
                "the exit code, and the last N lines of output."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "label": {
                        "type": "string",
                        "description": "Label of the background process",
                    },
                    "lines": {
                        "type": "integer",
                        "description": "Number of recent output lines to return",
                        "default": 50,
                    },
                },
                "required": ["label"],
            },
            "execute": bg_status_exec,
        },
        {
            "name": "shell_bg_kill",
            "description": "Stop a background process started with shell_bg_start.",
            "parameters": {
                "type": "object",
                "properties": {
                    "label": {
                        "type": "string",
                        "description": "Label of the background process to kill",
                    },
                },
                "required": ["label"],
            },
            "execute": bg_kill_exec,
        },
    ]
