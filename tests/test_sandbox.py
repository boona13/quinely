"""Tests for ghost_sandbox — env scrubbing, resource limits, timeout + process
group kill. POSIX-specific assertions are skipped on Windows.

Run: python -m pytest tests/test_sandbox.py -q
"""

import os
import sys
import time
import tempfile

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import ghost_sandbox as S

_POSIX = not sys.platform.startswith("win")


# ── env scrubbing ────────────────────────────────────────────────────────

def test_scrub_secrets_removes_secret_keys():
    base = {"PATH": "/usr/bin", "FOO": "bar", "OPENAI_API_KEY": "sk-secret",
            "GITHUB_TOKEN": "ghp_x", "MY_PASSWORD": "p", "HOME": "/home/x"}
    out = S.scrub_env(base, mode="scrub_secrets")
    assert "OPENAI_API_KEY" not in out
    assert "GITHUB_TOKEN" not in out
    assert "MY_PASSWORD" not in out
    assert out["FOO"] == "bar"
    assert out["PATH"] == "/usr/bin"


def test_scrub_minimal_allowlist_only():
    base = {"PATH": "/usr/bin", "FOO": "bar", "HOME": "/h"}
    out = S.scrub_env(base, mode="minimal")
    assert "FOO" not in out
    assert "PATH" in out and "HOME" in out


def test_scrub_full_keeps_everything():
    base = {"OPENAI_API_KEY": "sk", "FOO": "bar"}
    out = S.scrub_env(base, mode="full")
    assert out == base


def test_is_secret_key():
    assert S.is_secret_key("ANTHROPIC_API_KEY")
    assert S.is_secret_key("db_password")
    assert S.is_secret_key("STRIPE_SECRET")
    assert not S.is_secret_key("PATH")
    assert not S.is_secret_key("EDITOR")


# ── run() basics ─────────────────────────────────────────────────────────

def test_run_basic_echo():
    res = S.run("echo hello-sandbox", timeout=10)
    assert res.returncode == 0
    assert "hello-sandbox" in res.stdout
    assert res.timed_out is False


@pytest.mark.skipif(not _POSIX, reason="POSIX shell var expansion")
def test_run_env_is_scrubbed_by_default():
    os.environ["GHOST_TEST_SECRET_TOKEN"] = "leaky"
    try:
        res = S.run("echo [$GHOST_TEST_SECRET_TOKEN]", timeout=10)
        assert "[]" in res.stdout  # variable was scrubbed -> empty
    finally:
        os.environ.pop("GHOST_TEST_SECRET_TOKEN", None)


@pytest.mark.skipif(not _POSIX, reason="POSIX env passthrough")
def test_run_env_full_mode_passes_through():
    os.environ["GHOST_TEST_SECRET_TOKEN"] = "leaky"
    try:
        cfg = {"sandbox": {"env_mode": "full"}}
        env = S.scrub_env(os.environ, "full")
        res = S.run("echo [$GHOST_TEST_SECRET_TOKEN]", cfg=cfg, env=env, timeout=10)
        assert "[leaky]" in res.stdout
    finally:
        os.environ.pop("GHOST_TEST_SECRET_TOKEN", None)


# ── timeout + process-group kill ─────────────────────────────────────────

@pytest.mark.skipif(not _POSIX, reason="POSIX process groups")
def test_timeout_kills_orphan_children():
    pidfile = tempfile.mktemp()
    # Background child sleeps 30s; parent waits. On timeout the whole group
    # (including the backgrounded sleep) must be killed.
    cmd = f"(sleep 30 & echo $! > {pidfile}; wait)"
    t0 = time.time()
    res = S.run(cmd, timeout=2)
    elapsed = time.time() - t0
    assert res.timed_out is True
    assert elapsed < 12  # returned promptly after kill
    time.sleep(0.5)
    with open(pidfile) as f:
        child_pid = int(f.read().strip())
    os.remove(pidfile)
    # The backgrounded child must be dead (no orphan survived the timeout).
    with pytest.raises(ProcessLookupError):
        os.kill(child_pid, 0)


# ── resource limits ──────────────────────────────────────────────────────

@pytest.mark.skipif(not _POSIX, reason="RLIMIT_FSIZE is POSIX-only")
def test_file_size_limit_enforced():
    d = tempfile.mkdtemp()
    cfg = {"sandbox": {"file_size_mb": 1, "wall_timeout": 20}}
    # Try to write 8 MB; RLIMIT_FSIZE=1MB should abort it (SIGXFSZ).
    res = S.run("dd if=/dev/zero of=big.bin bs=1048576 count=8", cfg=cfg,
                cwd=d, timeout=20)
    big = os.path.join(d, "big.bin")
    size = os.path.getsize(big) if os.path.exists(big) else 0
    assert size <= 2 * 1024 * 1024  # capped near 1 MB, not 8 MB
    assert res.returncode != 0


@pytest.mark.skipif(not _POSIX, reason="RLIMIT_CPU is POSIX-only")
def test_cpu_limit_kills_busy_loop():
    cfg = {"sandbox": {"cpu_seconds": 1, "wall_timeout": 20}}
    py = sys.executable
    t0 = time.time()
    res = S.run(f'{py} -c "\nwhile True:\n    pass\n"', cfg=cfg, timeout=20)
    elapsed = time.time() - t0
    assert res.returncode != 0
    assert elapsed < 15  # killed by CPU limit well before wall timeout


# ── introspection ────────────────────────────────────────────────────────

def test_status_keys():
    st = S.status()
    for k in ("enabled", "platform", "rlimits_supported", "env_mode",
              "isolation", "bwrap_available"):
        assert k in st


def test_popen_kwargs_isolates_process_group():
    kw = S.popen_kwargs(None, persistent=False)
    if _POSIX:
        assert ("preexec_fn" in kw) or ("start_new_session" in kw)
    else:
        assert "creationflags" in kw


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-q"]))
