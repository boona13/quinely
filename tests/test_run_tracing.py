"""Tests for ghost_trace — run/span store, status derivation, thread isolation,
query API, and JSONL persistence/rotation.

Run: python -m pytest tests/test_run_tracing.py -q
"""

import os
import sys
import json
import threading

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import ghost_trace as T


@pytest.fixture
def tracer(tmp_path, monkeypatch):
    """Fresh tracer writing into a temp runs.jsonl (never the real ~/.ghost)."""
    monkeypatch.setattr(T, "RUNS_LOG_DIR", tmp_path)
    monkeypatch.setattr(T, "RUNS_LOG_FILE", tmp_path / "runs.jsonl")
    return T.Tracer()


# ── basic run lifecycle ───────────────────────────────────────────────────

def test_full_run_records_spans_and_outcome(tracer):
    rid = tracer.start_run("chat", user_message="hello world")
    assert rid and tracer.current_run_id == rid

    tracer.attach_session("sess123", model="gpt-x")
    tracer.add_model_span(0, "gpt-x", duration_ms=120, prompt_tokens=10,
                          completion_tokens=5, total_tokens=15)
    tracer.add_tool_span(0, "file_read", {"path": "a.txt"}, "file contents",
                         duration_ms=8)
    tracer.add_model_span(1, "gpt-x", duration_ms=90, total_tokens=7)

    snap = tracer.end_run(rid, result_text="done", tools_used=["file_read"])
    assert snap["status"] == "ok"
    assert snap["session_id"] == "sess123"
    assert snap["num_model_calls"] == 2
    assert snap["num_tool_calls"] == 1
    # total tokens fall back to sum of model spans when not supplied
    assert snap["total_tokens"] == 22
    assert snap["result_preview"] == "done"
    # current run cleared after end
    assert tracer.current_run_id == ""


def test_spans_noop_without_active_run(tracer):
    # No start_run on this thread → span calls must not raise and not record.
    tracer.add_model_span(0, "m", duration_ms=1)
    tracer.add_tool_span(0, "t", {}, "r")
    assert tracer.list_runs() == []


# ── status derivation ─────────────────────────────────────────────────────

def test_exit_reason_drives_status(tracer):
    rid = tracer.start_run("cron", meta={"job_name": "nightly"})
    tracer.set_exit_reason("llm_error")
    snap = tracer.end_run(rid, status="ok")
    assert snap["status"] == "error"
    assert snap["job_name"] == "nightly"
    assert snap["trigger"] == "nightly"


def test_cancelled_exit_reason(tracer):
    rid = tracer.start_run("chat", user_message="x")
    tracer.set_exit_reason("cancelled")
    snap = tracer.end_run(rid, status="ok")
    assert snap["status"] == "cancelled"


def test_explicit_error_overrides(tracer):
    rid = tracer.start_run("chat", user_message="x")
    snap = tracer.end_run(rid, status="error", error="boom")
    assert snap["status"] == "error"
    assert snap["error"] == "boom"


def test_failed_tool_span_marked(tracer):
    rid = tracer.start_run("chat", user_message="x")
    tracer.add_tool_span(0, "shell_exec", {"cmd": "x"},
                         "Tool error (shell_exec): timed out")
    run = tracer.get_run(rid)
    tool_spans = [s for s in run["spans"] if s["kind"] == "tool"]
    assert tool_spans and tool_spans[0]["ok"] is False
    tracer.end_run(rid)


# ── query API ─────────────────────────────────────────────────────────────

def test_list_filter_and_active(tracer):
    a = tracer.start_run("chat", user_message="a")
    tracer.end_run(a)
    b = tracer.start_run("cron", meta={"job_name": "j"})
    tracer.end_run(b)

    assert len(tracer.list_runs()) == 2
    chat_only = tracer.list_runs(source="chat")
    assert len(chat_only) == 1 and chat_only[0]["source"] == "chat"

    stats = tracer.stats()
    assert stats["recent_total"] == 2
    assert stats["by_source"].get("chat") == 1
    assert stats["by_source"].get("cron") == 1


def test_get_run_reads_from_file_after_eviction(tracer):
    rid = tracer.start_run("chat", user_message="persisted")
    tracer.add_model_span(0, "m", duration_ms=5, total_tokens=3)
    tracer.end_run(rid, result_text="kept")
    # Clear in-memory caches → forces file fallback.
    tracer._recent.clear()
    tracer._active.clear()
    run = tracer.get_run(rid)
    assert run is not None
    assert run["result_preview"] == "kept"


def test_persistence_writes_jsonl(tracer):
    rid = tracer.start_run("chat", user_message="persist me")
    tracer.end_run(rid, result_text="ok")
    assert T.RUNS_LOG_FILE.exists()
    lines = T.RUNS_LOG_FILE.read_text().strip().splitlines()
    assert len(lines) == 1
    rec = json.loads(lines[0])
    assert rec["run_id"] == rid
    assert rec["source"] == "chat"


# ── thread isolation ──────────────────────────────────────────────────────

def test_thread_local_isolation(tracer):
    results = {}

    def worker(name):
        rid = tracer.start_run("cron", meta={"job_name": name})
        tracer.add_tool_span(0, f"tool_{name}", {}, "r")
        # each thread should only see its own current run
        results[name] = tracer.current_run_id == rid
        tracer.end_run(rid)

    threads = [threading.Thread(target=worker, args=(n,)) for n in ("a", "b", "c")]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert all(results.values())
    assert len(tracer.list_runs()) == 3


def test_trigger_strips_rag_augmentation(tracer):
    msg = ("What is the weather?\n\n## Relevant memory (auto-retrieved)\n"
           "- some old memory blob that should not appear in the trigger")
    rid = tracer.start_run("chat", user_message=msg)
    runs = tracer.list_runs()
    assert runs[0]["trigger"] == "What is the weather?"
    tracer.end_run(rid)


def test_truncation_limits(tracer):
    rid = tracer.start_run("chat", user_message="m")
    big = "x" * 5000
    tracer.add_tool_span(0, "t", {"data": big}, big)
    run = tracer.get_run(rid)
    span = run["spans"][0]
    assert span["result_length"] == 5000
    assert len(span["result_preview"]) < 5000
    tracer.end_run(rid)
