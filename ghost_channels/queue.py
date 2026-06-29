"""
Write-Ahead Delivery Queue with Exponential Backoff Retries

  - Persist every outbound message to ~/.ghost/channel_queue/ BEFORE sending
  - On success: delete the queue file (ack)
  - On failure: increment retry counter, apply exponential backoff
  - Permanent errors: move to failed/ subdirectory
  - Crash recovery: on startup scan queue dir and re-attempt pending deliveries

Thread-safe, file-based persistence, no external dependencies.
"""

import json
import os
import time
import uuid
import threading
import logging
from dataclasses import dataclass, field, asdict
from pathlib import Path
from typing import Optional, Dict, Any, List, Callable

log = logging.getLogger("quinely.channels.queue")

GHOST_HOME = Path.home() / ".ghost"
QUEUE_DIR = GHOST_HOME / "channel_queue"
FAILED_DIR = QUEUE_DIR / "failed"

MAX_RETRIES = 5

BACKOFF_MS = [
    5_000,      # retry 1: 5 seconds
    25_000,     # retry 2: 25 seconds
    120_000,    # retry 3: 2 minutes
    600_000,    # retry 4: 10 minutes
    1_800_000,  # retry 5: 30 minutes
]

PERMANENT_ERROR_PATTERNS = [
    "chat not found",
    "user not found",
    "bot was blocked",
    "bot was kicked",
    "forbidden",
    "invalid recipient",
    "chat_id is empty",
    "no channel available",
    "not configured",
    "unauthorized",
    "invalid token",
    "account deactivated",
]


@dataclass
class QueuedDelivery:
    id: str
    channel: str
    to: str
    text: str
    enqueued_at: float
    retry_count: int = 0
    last_error: str = ""
    priority: str = "normal"
    title: str = ""
    kwargs: Dict[str, Any] = field(default_factory=dict)
    account_id: str = ""


def _ensure_dirs():
    QUEUE_DIR.mkdir(parents=True, exist_ok=True)
    FAILED_DIR.mkdir(parents=True, exist_ok=True)


def _entry_path(entry_id: str) -> Path:
    return QUEUE_DIR / f"{entry_id}.json"


def _failed_path(entry_id: str) -> Path:
    return FAILED_DIR / f"{entry_id}.json"


def _write_atomic(path: Path, data: dict):
    """Write JSON atomically via tmp-file + replace (cross-platform)."""
    tmp = path.with_suffix(f".{os.getpid()}.tmp")
    tmp.write_text(json.dumps(data, indent=2, default=str), encoding="utf-8")
    tmp.replace(path)


def is_permanent_error(error: str) -> bool:
    err_lower = error.lower()
    return any(pattern in err_lower for pattern in PERMANENT_ERROR_PATTERNS)


def compute_backoff_ms(retry_count: int) -> int:
    if retry_count <= 0:
        return 0
    idx = min(retry_count - 1, len(BACKOFF_MS) - 1)
    return BACKOFF_MS[idx]


def enqueue(channel: str, to: str, text: str, priority: str = "normal",
            title: str = "", account_id: str = "", **kwargs) -> str:
    """Persist a delivery entry to disk before attempting send. Returns entry ID."""
    _ensure_dirs()
    entry_id = str(uuid.uuid4())
    entry = QueuedDelivery(
        id=entry_id,
        channel=channel,
        to=to,
        text=text,
        enqueued_at=time.time(),
        priority=priority,
        title=title,
        kwargs=kwargs,
        account_id=account_id,
    )
    _write_atomic(_entry_path(entry_id), asdict(entry))
    return entry_id


def ack(entry_id: str):
    """Remove a successfully delivered entry from the queue."""
    try:
        _entry_path(entry_id).unlink(missing_ok=True)
    except Exception as exc:
        log.debug("ack failed for %s: %s", entry_id, exc)


def fail(entry_id: str, error: str):
    """Update a queue entry after a failed delivery attempt."""
    path = _entry_path(entry_id)
    if not path.exists():
        return
    try:
        entry = json.loads(path.read_text(encoding="utf-8"))
        entry["retry_count"] = entry.get("retry_count", 0) + 1
        entry["last_error"] = error
        _write_atomic(path, entry)
    except Exception as exc:
        log.debug("fail update failed for %s: %s", entry_id, exc)


def move_to_failed(entry_id: str):
    """Move a queue entry to the failed/ subdirectory (max retries exceeded)."""
    _ensure_dirs()
    src = _entry_path(entry_id)
    dest = _failed_path(entry_id)
    try:
        if src.exists():
            src.replace(dest)
    except Exception as exc:
        log.debug("move_to_failed failed for %s: %s", entry_id, exc)


def load_pending() -> List[QueuedDelivery]:
    """Load all pending delivery entries from the queue directory."""
    if not QUEUE_DIR.exists():
        return []
    entries = []
    for f in QUEUE_DIR.iterdir():
        if not f.suffix == ".json" or not f.is_file():
            continue
        try:
            data = json.loads(f.read_text(encoding="utf-8"))
            entries.append(QueuedDelivery(**{
                k: data[k] for k in QueuedDelivery.__dataclass_fields__
                if k in data
            }))
        except Exception:
            pass
    entries.sort(key=lambda e: e.enqueued_at)
    return entries


def load_failed() -> List[QueuedDelivery]:
    """Load all failed delivery entries."""
    if not FAILED_DIR.exists():
        return []
    entries = []
    for f in FAILED_DIR.iterdir():
        if not f.suffix == ".json" or not f.is_file():
            continue
        try:
            data = json.loads(f.read_text(encoding="utf-8"))
            entries.append(QueuedDelivery(**{
                k: data[k] for k in QueuedDelivery.__dataclass_fields__
                if k in data
            }))
        except Exception:
            pass
    return entries


def queue_stats() -> Dict[str, Any]:
    """Return queue statistics."""
    pending = load_pending()
    failed = load_failed()
    return {
        "pending_count": len(pending),
        "failed_count": len(failed),
        "oldest_pending": min((e.enqueued_at for e in pending), default=None),
        "total_retries": sum(e.retry_count for e in pending),
    }


class DeliveryQueue:
    """Thread-safe delivery queue manager integrated with MessageRouter.

    Usage:
        queue = DeliveryQueue(send_fn)
        queue.start()     # begin background retry thread
        result = queue.deliver(channel, to, text, ...)  # write-ahead + send
        queue.stop()      # shutdown
    """

    def __init__(self, send_fn: Callable, retry_interval: float = 30.0):
        """
        Args:
            send_fn: Callable(channel, to, text, **kwargs) -> OutboundResult
            retry_interval: Seconds between retry scans
        """
        self._send_fn = send_fn
        self._retry_interval = retry_interval
        self._stop_event = threading.Event()
        self._retry_thread: Optional[threading.Thread] = None
        self._lock = threading.Lock()

    def start(self):
        """Start the background retry thread."""
        self._stop_event.clear()
        self._retry_thread = threading.Thread(
            target=self._retry_loop, daemon=True,
            name="delivery-queue-retry",
        )
        self._retry_thread.start()
        log.info("Delivery queue started (retry interval: %.0fs)", self._retry_interval)

    def stop(self):
        """Stop the background retry thread."""
        self._stop_event.set()
        if self._retry_thread:
            self._retry_thread.join(timeout=10)
            self._retry_thread = None
        log.info("Delivery queue stopped")

    def deliver(self, channel: str, to: str, text: str,
                priority: str = "normal", title: str = "",
                skip_queue: bool = False, **kwargs):
        """Write-ahead enqueue, then attempt immediate delivery.

        Returns the result from send_fn on success. On failure, the entry
        remains in the queue for background retry.
        """
        from ghost_channels import OutboundResult

        entry_id = None
        if not skip_queue:
            try:
                entry_id = enqueue(channel, to, text,
                                   priority=priority, title=title, **kwargs)
            except Exception as exc:
                log.debug("Queue write failed (proceeding anyway): %s", exc)

        try:
            result = self._send_fn(channel, to, text, **kwargs)
            if hasattr(result, "ok") and result.ok:
                if entry_id:
                    ack(entry_id)
                return result
            else:
                error_msg = getattr(result, "error", "send returned not-ok")
                if entry_id:
                    if is_permanent_error(str(error_msg)):
                        move_to_failed(entry_id)
                    else:
                        fail(entry_id, str(error_msg))
                return result
        except Exception as exc:
            if entry_id:
                if is_permanent_error(str(exc)):
                    move_to_failed(entry_id)
                else:
                    fail(entry_id, str(exc))
            return OutboundResult(ok=False, error=str(exc), channel_id=channel)

    def recover(self, max_recovery_seconds: float = 60.0) -> Dict[str, int]:
        """Scan queue and retry pending entries. Called on startup.

        Returns dict with recovered, failed, skipped counts.
        """
        pending = load_pending()
        if not pending:
            return {"recovered": 0, "failed": 0, "skipped": 0}

        log.info("Found %d pending deliveries — starting recovery", len(pending))
        deadline = time.time() + max_recovery_seconds
        recovered = 0
        failed_count = 0
        skipped = 0

        for entry in pending:
            if time.time() >= deadline:
                remaining = len(pending) - recovered - failed_count - skipped
                log.warning("Recovery time budget exceeded — %d entries deferred", remaining)
                break

            if entry.retry_count >= MAX_RETRIES:
                log.warning("Entry %s exceeded max retries (%d/%d) — moving to failed/",
                            entry.id, entry.retry_count, MAX_RETRIES)
                move_to_failed(entry.id)
                skipped += 1
                continue

            backoff_s = compute_backoff_ms(entry.retry_count + 1) / 1000.0
            if time.time() + backoff_s >= deadline:
                remaining = len(pending) - recovered - failed_count - skipped
                log.warning("Recovery time budget exceeded — %d entries deferred", remaining)
                break

            if backoff_s > 0:
                log.info("Waiting %.1fs before retrying %s", backoff_s, entry.id)
                if self._stop_event.wait(backoff_s):
                    break

            try:
                result = self._send_fn(
                    entry.channel, entry.to, entry.text, **entry.kwargs
                )
                if hasattr(result, "ok") and result.ok:
                    ack(entry.id)
                    recovered += 1
                    log.info("Recovered delivery %s to %s:%s",
                             entry.id, entry.channel, entry.to)
                else:
                    error_msg = getattr(result, "error", "send failed")
                    if is_permanent_error(str(error_msg)):
                        move_to_failed(entry.id)
                        failed_count += 1
                    else:
                        fail(entry.id, str(error_msg))
                        failed_count += 1
            except Exception as exc:
                err_msg = str(exc)
                if is_permanent_error(err_msg):
                    move_to_failed(entry.id)
                else:
                    fail(entry.id, err_msg)
                failed_count += 1
                log.warning("Retry failed for %s: %s", entry.id, err_msg)

        log.info("Recovery complete: %d recovered, %d failed, %d skipped",
                 recovered, failed_count, skipped)
        return {"recovered": recovered, "failed": failed_count, "skipped": skipped}

    def _retry_loop(self):
        """Background thread: periodically scan queue for entries ready to retry."""
        while not self._stop_event.is_set():
            self._stop_event.wait(self._retry_interval)
            if self._stop_event.is_set():
                break
            try:
                self._retry_pass()
            except Exception as exc:
                log.debug("Retry pass error: %s", exc)

    def _retry_pass(self):
        """Single retry scan of pending entries."""
        pending = load_pending()
        now = time.time()

        for entry in pending:
            if self._stop_event.is_set():
                break
            if entry.retry_count >= MAX_RETRIES:
                move_to_failed(entry.id)
                continue

            backoff_s = compute_backoff_ms(entry.retry_count + 1) / 1000.0
            last_attempt = entry.enqueued_at
            path = _entry_path(entry.id)
            if path.exists():
                last_attempt = path.stat().st_mtime

            if now - last_attempt < backoff_s:
                continue

            try:
                result = self._send_fn(
                    entry.channel, entry.to, entry.text, **entry.kwargs
                )
                if hasattr(result, "ok") and result.ok:
                    ack(entry.id)
                    log.info("Retry succeeded for %s -> %s:%s",
                             entry.id, entry.channel, entry.to)
                else:
                    error_msg = getattr(result, "error", "send failed")
                    if is_permanent_error(str(error_msg)):
                        move_to_failed(entry.id)
                    else:
                        fail(entry.id, str(error_msg))
            except Exception as exc:
                err_msg = str(exc)
                if is_permanent_error(err_msg):
                    move_to_failed(entry.id)
                else:
                    fail(entry.id, err_msg)
