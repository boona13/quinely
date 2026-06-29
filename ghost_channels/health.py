"""
Enhanced Status & Health System

  - probe(): Quick connectivity check
  - audit(): Deep inspection (permissions, webhook validity, etc.)
  - build_snapshot(): Comprehensive account snapshot
  - collect_issues(): Detect and report degradation
  - Periodic health checks via cron integration
  - Rate limit tracking and reporting

Usage:
    if isinstance(provider, HealthMixin):
        probe = provider.probe(timeout_ms=5000)
        issues = provider.collect_issues()
"""

import time
import json
import threading
import logging
from dataclasses import dataclass, field, asdict
from pathlib import Path
from typing import Optional, Dict, Any, List
from enum import Enum

log = logging.getLogger("quinely.channels.health")

GHOST_HOME = Path.home() / ".ghost"
HEALTH_LOG_FILE = GHOST_HOME / "channel_health.json"
MAX_HEALTH_LOG = 500


class IssueSeverity(Enum):
    INFO = "info"
    WARNING = "warning"
    ERROR = "error"
    CRITICAL = "critical"


@dataclass
class HealthProbe:
    """Quick connectivity check result."""
    ok: bool
    latency_ms: float = 0.0
    channel_id: str = ""
    error: str = ""
    checked_at: float = 0.0

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


@dataclass
class HealthAudit:
    """Deep inspection result."""
    channel_id: str = ""
    permissions_ok: bool = True
    webhook_valid: bool = True
    token_valid: bool = True
    rate_limit_remaining: int = -1
    rate_limit_reset_at: float = 0.0
    bot_info: Dict[str, Any] = field(default_factory=dict)
    warnings: List[str] = field(default_factory=list)
    checked_at: float = 0.0

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


@dataclass
class StatusIssue:
    """A detected issue with a channel."""
    channel_id: str
    severity: IssueSeverity
    message: str
    category: str = ""
    detected_at: float = 0.0

    def to_dict(self) -> Dict[str, Any]:
        d = asdict(self)
        d["severity"] = self.severity.value
        return d


@dataclass
class AccountSnapshot:
    """Comprehensive snapshot of a channel account's state."""
    channel_id: str
    account_id: str = ""
    state: str = "unknown"
    configured: bool = False
    enabled: bool = False
    connected: bool = False
    last_send_at: float = 0.0
    last_send_ok: bool = True
    last_error: str = ""
    message_count: int = 0
    probe: Optional[HealthProbe] = None
    issues: List[StatusIssue] = field(default_factory=list)

    def to_dict(self) -> Dict[str, Any]:
        d = {
            "channel_id": self.channel_id,
            "account_id": self.account_id,
            "state": self.state,
            "configured": self.configured,
            "enabled": self.enabled,
            "connected": self.connected,
            "last_send_at": self.last_send_at,
            "last_send_ok": self.last_send_ok,
            "last_error": self.last_error,
            "message_count": self.message_count,
        }
        if self.probe:
            d["probe"] = self.probe.to_dict()
        if self.issues:
            d["issues"] = [i.to_dict() for i in self.issues]
        return d


_health_lock = threading.Lock()


def _append_health_log(entry: Dict[str, Any]):
    with _health_lock:
        entries = []
        if HEALTH_LOG_FILE.exists():
            try:
                entries = json.loads(HEALTH_LOG_FILE.read_text(encoding="utf-8"))
            except Exception:
                entries = []
        entries.insert(0, entry)
        entries = entries[:MAX_HEALTH_LOG]
        GHOST_HOME.mkdir(parents=True, exist_ok=True)
        HEALTH_LOG_FILE.write_text(json.dumps(entries, indent=2), encoding="utf-8")


class HealthMixin:
    """Mixin for ChannelProvider subclasses with enhanced health monitoring.

    Override probe(), audit(), and collect_issues() for channel-specific
    health checking.
    """

    def probe(self, timeout_ms: int = 5000) -> HealthProbe:
        """Quick connectivity check. Override per channel.

        Default implementation delegates to health_check().
        """
        channel_id = getattr(getattr(self, "meta", None), "id", "unknown")
        start = time.time()
        try:
            h = self.health_check() if hasattr(self, "health_check") else {}
            latency = (time.time() - start) * 1000
            ok = h.get("configured", False) and h.get("status") != "error"
            result = HealthProbe(
                ok=ok, latency_ms=latency, channel_id=channel_id,
                error=h.get("last_error", ""),
                checked_at=time.time(),
            )
        except Exception as exc:
            result = HealthProbe(
                ok=False, latency_ms=(time.time() - start) * 1000,
                channel_id=channel_id, error=str(exc),
                checked_at=time.time(),
            )

        _append_health_log({
            "type": "probe",
            "channel": channel_id,
            "ok": result.ok,
            "latency_ms": result.latency_ms,
            "time": time.time(),
        })
        return result

    def audit(self, timeout_ms: int = 10000) -> HealthAudit:
        """Deep inspection. Override per channel for permission/webhook checks."""
        channel_id = getattr(getattr(self, "meta", None), "id", "unknown")
        return HealthAudit(channel_id=channel_id, checked_at=time.time())

    def build_snapshot(self) -> AccountSnapshot:
        """Build comprehensive snapshot. Override for richer data."""
        channel_id = getattr(getattr(self, "meta", None), "id", "unknown")
        h = self.health_check() if hasattr(self, "health_check") else {}
        return AccountSnapshot(
            channel_id=channel_id,
            configured=h.get("configured", False),
            enabled=h.get("configured", False),
            connected=h.get("status") == "connected",
            state=h.get("status", "unknown"),
            last_error=h.get("last_error", ""),
        )

    def collect_issues(self) -> List[StatusIssue]:
        """Detect issues with this channel. Override per channel."""
        channel_id = getattr(getattr(self, "meta", None), "id", "unknown")
        issues = []
        h = self.health_check() if hasattr(self, "health_check") else {}
        if h.get("status") == "error":
            issues.append(StatusIssue(
                channel_id=channel_id,
                severity=IssueSeverity.ERROR,
                message=h.get("last_error", "Unknown error"),
                category="connectivity",
                detected_at=time.time(),
            ))
        if not h.get("configured", False):
            issues.append(StatusIssue(
                channel_id=channel_id,
                severity=IssueSeverity.WARNING,
                message="Channel not configured",
                category="configuration",
                detected_at=time.time(),
            ))
        return issues


class HealthMonitor:
    """Periodic health checker for all channels.

    Runs probes at configurable intervals, logs results,
    and triggers notifications for degraded channels.
    """

    def __init__(self, registry, router=None,
                 check_interval: float = 300.0):
        self._registry = registry
        self._router = router
        self._interval = check_interval
        self._stop_event = threading.Event()
        self._thread: Optional[threading.Thread] = None
        self._last_results: Dict[str, HealthProbe] = {}

    def start(self):
        self._stop_event.clear()
        self._thread = threading.Thread(
            target=self._check_loop, daemon=True,
            name="health-monitor",
        )
        self._thread.start()
        log.info("Health monitor started (interval: %.0fs)", self._interval)

    def stop(self):
        self._stop_event.set()
        if self._thread:
            self._thread.join(timeout=10)
            self._thread = None

    def check_all(self) -> Dict[str, HealthProbe]:
        """Probe all configured channels and return results."""
        results = {}
        for cid in self._registry.list_configured():
            prov = self._registry.get(cid)
            if prov and isinstance(prov, HealthMixin):
                try:
                    results[cid] = prov.probe()
                except Exception as exc:
                    results[cid] = HealthProbe(
                        ok=False, channel_id=cid, error=str(exc),
                        checked_at=time.time(),
                    )
        self._last_results = results
        return results

    def collect_all_issues(self) -> List[StatusIssue]:
        """Collect issues across all channels."""
        issues = []
        for cid in self._registry.list_configured():
            prov = self._registry.get(cid)
            if prov and isinstance(prov, HealthMixin):
                try:
                    issues.extend(prov.collect_issues())
                except Exception:
                    pass
        return issues

    def get_snapshots(self) -> List[AccountSnapshot]:
        """Build snapshots for all configured channels."""
        snapshots = []
        for cid in self._registry.list_configured():
            prov = self._registry.get(cid)
            if prov and isinstance(prov, HealthMixin):
                try:
                    snapshots.append(prov.build_snapshot())
                except Exception:
                    pass
        return snapshots

    def _check_loop(self):
        while not self._stop_event.is_set():
            self._stop_event.wait(self._interval)
            if self._stop_event.is_set():
                break
            try:
                results = self.check_all()
                degraded = [cid for cid, p in results.items() if not p.ok]
                if degraded and self._router:
                    msg = "Channel health alert: " + ", ".join(
                        f"{cid} ({results[cid].error or 'unhealthy'})"
                        for cid in degraded
                    )
                    try:
                        self._router.send(msg, priority="high",
                                          title="Channel Health Alert")
                    except Exception:
                        pass
            except Exception as exc:
                log.debug("Health check loop error: %s", exc)


def build_health_tools(registry) -> list:
    """Build LLM tools for health monitoring."""
    tools = []

    def channel_health(channel: str = "") -> str:
        if channel:
            prov = registry.get(channel)
            if not prov:
                return f"Unknown channel: {channel}"
            if not isinstance(prov, HealthMixin):
                h = prov.health_check() if hasattr(prov, "health_check") else {}
                return f"{channel}: {json.dumps(h, indent=2)}"
            probe = prov.probe()
            audit = prov.audit()
            issues = prov.collect_issues()
            lines = [
                f"{channel} health:",
                f"  Probe: {'OK' if probe.ok else 'FAIL'} ({probe.latency_ms:.0f}ms)",
                f"  Token valid: {audit.token_valid}",
                f"  Permissions OK: {audit.permissions_ok}",
            ]
            if audit.rate_limit_remaining >= 0:
                lines.append(f"  Rate limit remaining: {audit.rate_limit_remaining}")
            if issues:
                lines.append(f"  Issues ({len(issues)}):")
                for issue in issues:
                    lines.append(f"    [{issue.severity.value}] {issue.message}")
            return "\n".join(lines)

        lines = ["Channel health summary:"]
        for cid in registry.list_configured():
            prov = registry.get(cid)
            if prov and isinstance(prov, HealthMixin):
                probe = prov.probe()
                status = "OK" if probe.ok else f"FAIL ({probe.error})"
                lines.append(f"  {cid}: {status} ({probe.latency_ms:.0f}ms)")
            elif prov:
                h = prov.health_check() if hasattr(prov, "health_check") else {}
                lines.append(f"  {cid}: {h.get('status', 'unknown')}")
        if len(lines) == 1:
            return "No configured channels to check"
        return "\n".join(lines)

    tools.append({
        "name": "channel_health",
        "description": "Run health checks on messaging channels. Probes connectivity, checks permissions, and reports issues.",
        "parameters": {
            "type": "object",
            "properties": {
                "channel": {"type": "string",
                            "description": "Channel ID to check, or empty for all",
                            "default": ""},
            },
        },
        "execute": channel_health,
    })

    return tools
