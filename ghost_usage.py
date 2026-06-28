"""
GHOST Usage Telemetry Module

Live token/model usage tracking for dashboard visibility.
Provides real-time usage state: current model, active call indicator, session token count.
"""

import threading
import time
from dataclasses import dataclass, field
from typing import Optional, Callable

# A single LLM HTTP request is bounded by a short read timeout (~30s) plus a few
# retries. If "active" stays set far longer than that, the call almost certainly
# leaked (e.g. a cancellation path that returned before call_completed ran), so
# we auto-expire the flag rather than show "working" forever.
ACTIVE_TTL_SECONDS = 180


@dataclass
class UsageSnapshot:
    """Snapshot of current usage state."""
    model: str = ""
    provider: str = ""
    active: bool = False
    session_tokens: int = 0
    calls_this_session: int = 0
    last_call_timestamp: Optional[float] = None
    last_call_tokens: int = 0
    
    def to_dict(self) -> dict:
        return {
            "model": self.model,
            "provider": self.provider,
            "active": self.active,
            "session_tokens": self.session_tokens,
            "calls_this_session": self.calls_this_session,
            "last_call_timestamp": self.last_call_timestamp,
            "last_call_tokens": self.last_call_tokens,
        }


class UsageTracker:
    """Thread-safe usage telemetry tracker.
    
    Tracks live LLM usage state including:
    - Current active model and provider
    - Whether a call is in progress
    - Session-level token accumulation
    - Per-call token breakdown
    """
    
    def __init__(self):
        self._lock = threading.RLock()
        self._snapshot = UsageSnapshot()
        self._callbacks: list[Callable[[UsageSnapshot], None]] = []
        self._active_since: Optional[float] = None
    
    def register_callback(self, callback: Callable[[UsageSnapshot], None]) -> None:
        """Register a callback to be called on usage updates."""
        with self._lock:
            self._callbacks.append(callback)
    
    def unregister_callback(self, callback: Callable[[UsageSnapshot], None]) -> None:
        """Unregister a callback."""
        with self._lock:
            if callback in self._callbacks:
                self._callbacks.remove(callback)
    
    def _notify(self) -> None:
        """Notify all callbacks with current snapshot."""
        snapshot = self.get_snapshot()
        for cb in self._callbacks:
            try:
                cb(snapshot)
            except Exception:
                pass  # Don't let callbacks break the tracker
    
    def call_started(self, provider: str, model: str) -> None:
        """Mark that an LLM call has started."""
        with self._lock:
            self._snapshot.provider = provider
            self._snapshot.model = model
            self._snapshot.active = True
            self._active_since = time.time()
        self._notify()
    
    def call_completed(self, tokens_used: int, success: bool = True) -> None:
        """Mark that an LLM call has completed."""
        with self._lock:
            self._snapshot.active = False
            self._active_since = None
            self._snapshot.last_call_tokens = tokens_used if success else 0
            self._snapshot.last_call_timestamp = time.time()
            if success:
                self._snapshot.session_tokens += tokens_used
                self._snapshot.calls_this_session += 1
        self._notify()
    
    def get_snapshot(self) -> UsageSnapshot:
        """Get a copy of the current usage snapshot."""
        with self._lock:
            # Self-heal a leaked "active" flag: if a call has been in progress
            # far longer than any real request could take, treat it as idle.
            if (self._snapshot.active and self._active_since is not None
                    and (time.time() - self._active_since) > ACTIVE_TTL_SECONDS):
                self._snapshot.active = False
                self._active_since = None
            return UsageSnapshot(
                model=self._snapshot.model,
                provider=self._snapshot.provider,
                active=self._snapshot.active,
                session_tokens=self._snapshot.session_tokens,
                calls_this_session=self._snapshot.calls_this_session,
                last_call_timestamp=self._snapshot.last_call_timestamp,
                last_call_tokens=self._snapshot.last_call_tokens,
            )
    
    def reset_session(self) -> None:
        """Reset session-level counters."""
        with self._lock:
            self._snapshot.session_tokens = 0
            self._snapshot.calls_this_session = 0
            self._snapshot.last_call_timestamp = None
            self._snapshot.last_call_tokens = 0
        self._notify()
    
    def update_model(self, provider: str, model: str) -> None:
        """Update the current model/provider without a call."""
        with self._lock:
            self._snapshot.provider = provider
            self._snapshot.model = model
        self._notify()


# Global singleton for dashboard access
_global_tracker: Optional[UsageTracker] = None
_global_lock = threading.Lock()


def get_usage_tracker() -> UsageTracker:
    """Get the global usage tracker instance (creates if needed)."""
    global _global_tracker
    with _global_lock:
        if _global_tracker is None:
            _global_tracker = UsageTracker()
        return _global_tracker


def set_usage_tracker(tracker: UsageTracker) -> None:
    """Set the global usage tracker (used by daemon)."""
    global _global_tracker
    with _global_lock:
        _global_tracker = tracker
