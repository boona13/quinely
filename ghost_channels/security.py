"""
Security Adapter

  - DM policy resolution per channel (open, allowlist, blocklist)
  - Security warnings collection
  - Sender verification hooks
  - Rate limiting per sender
  - Audit log of security events

Usage:
    if isinstance(provider, SecurityMixin):
        policy = provider.resolve_dm_policy(config)
        allowed = provider.is_sender_allowed(sender_id, config)
        warnings = provider.collect_security_warnings(config)
"""

import time
import json
import threading
import logging
from dataclasses import dataclass, field
from pathlib import Path
from enum import Enum
from typing import Optional, Dict, Any, List, Set

log = logging.getLogger("quinely.channels.security")

GHOST_HOME = Path.home() / ".ghost"
SECURITY_LOG_FILE = GHOST_HOME / "channel_security.json"
MAX_SECURITY_LOG = 1000


class DmPolicy(Enum):
    OPEN = "open"
    ALLOWLIST = "allowlist"
    BLOCKLIST = "blocklist"
    DISABLED = "disabled"


@dataclass
class SecurityEvent:
    """A security-relevant event."""
    channel_id: str
    event_type: str
    sender_id: str = ""
    sender_name: str = ""
    message: str = ""
    allowed: bool = True
    timestamp: float = 0.0

    def to_dict(self) -> Dict[str, Any]:
        return {
            "channel_id": self.channel_id,
            "event_type": self.event_type,
            "sender_id": self.sender_id,
            "sender_name": self.sender_name,
            "message": self.message,
            "allowed": self.allowed,
            "timestamp": self.timestamp or time.time(),
        }


@dataclass
class RateLimitState:
    """Per-sender rate limiting state."""
    sender_id: str
    channel_id: str
    message_count: int = 0
    window_start: float = 0.0
    blocked_until: float = 0.0


_security_lock = threading.Lock()
_rate_limits: Dict[str, RateLimitState] = {}


def _log_security_event(event: SecurityEvent):
    with _security_lock:
        entries = []
        if SECURITY_LOG_FILE.exists():
            try:
                entries = json.loads(SECURITY_LOG_FILE.read_text(encoding="utf-8"))
            except Exception:
                entries = []
        entries.insert(0, event.to_dict())
        entries = entries[:MAX_SECURITY_LOG]
        GHOST_HOME.mkdir(parents=True, exist_ok=True)
        SECURITY_LOG_FILE.write_text(json.dumps(entries, indent=2), encoding="utf-8")


def load_security_log(limit: int = 50) -> List[Dict[str, Any]]:
    """Load recent security events."""
    if not SECURITY_LOG_FILE.exists():
        return []
    try:
        entries = json.loads(SECURITY_LOG_FILE.read_text(encoding="utf-8"))
        return entries[:limit]
    except Exception:
        return []


class SecurityMixin:
    """Mixin for ChannelProvider subclasses with security features.

    Handles DM policies, sender verification, rate limiting,
    and security warning collection.
    """

    def resolve_dm_policy(self, config: Dict[str, Any] = None) -> DmPolicy:
        """Determine the DM policy for this channel.

        Checks channel-specific config first, falls back to global.
        """
        if config:
            policy_str = config.get("dm_policy", "")
            if policy_str:
                try:
                    return DmPolicy(policy_str)
                except ValueError:
                    pass
        return DmPolicy.OPEN

    def get_allowlist(self, config: Dict[str, Any] = None) -> Set[str]:
        """Get the set of allowed sender IDs."""
        if config:
            return set(str(s) for s in config.get("allowed_senders", []))
        return set()

    def get_blocklist(self, config: Dict[str, Any] = None) -> Set[str]:
        """Get the set of blocked sender IDs."""
        if config:
            return set(str(s) for s in config.get("blocked_senders", []))
        return set()

    def is_sender_allowed(self, sender_id: str,
                           config: Dict[str, Any] = None) -> bool:
        """Check if a sender is allowed under the current DM policy."""
        channel_id = getattr(getattr(self, "meta", None), "id", "unknown")
        policy = self.resolve_dm_policy(config)

        if policy == DmPolicy.DISABLED:
            _log_security_event(SecurityEvent(
                channel_id=channel_id, event_type="dm_blocked",
                sender_id=sender_id, message="DM disabled",
                allowed=False,
            ))
            return False

        if policy == DmPolicy.ALLOWLIST:
            allowed = self.get_allowlist(config)
            if sender_id not in allowed:
                _log_security_event(SecurityEvent(
                    channel_id=channel_id, event_type="dm_blocked",
                    sender_id=sender_id,
                    message=f"Not in allowlist ({len(allowed)} entries)",
                    allowed=False,
                ))
                return False

        if policy == DmPolicy.BLOCKLIST:
            blocked = self.get_blocklist(config)
            if sender_id in blocked:
                _log_security_event(SecurityEvent(
                    channel_id=channel_id, event_type="dm_blocked",
                    sender_id=sender_id, message="In blocklist",
                    allowed=False,
                ))
                return False

        return True

    def check_rate_limit(self, sender_id: str,
                          max_per_minute: int = 10,
                          cooldown_seconds: int = 60) -> bool:
        """Check if sender has exceeded rate limit.

        Returns True if allowed, False if rate-limited.
        """
        channel_id = getattr(getattr(self, "meta", None), "id", "unknown")
        key = f"{channel_id}:{sender_id}"
        now = time.time()

        with _security_lock:
            state = _rate_limits.get(key)
            if not state:
                state = RateLimitState(sender_id=sender_id,
                                       channel_id=channel_id,
                                       window_start=now)
                _rate_limits[key] = state

            if state.blocked_until > now:
                return False

            if now - state.window_start > 60:
                state.message_count = 0
                state.window_start = now

            state.message_count += 1

            if state.message_count > max_per_minute:
                state.blocked_until = now + cooldown_seconds
                _log_security_event(SecurityEvent(
                    channel_id=channel_id, event_type="rate_limited",
                    sender_id=sender_id,
                    message=f"Exceeded {max_per_minute}/min limit",
                    allowed=False,
                ))
                return False

        return True

    def collect_security_warnings(self,
                                   config: Dict[str, Any] = None) -> List[str]:
        """Collect security warnings for this channel.

        Override per channel for specific checks (e.g., webhook URL exposure,
        missing permissions, insecure configurations).
        """
        warnings = []
        channel_id = getattr(getattr(self, "meta", None), "id", "unknown")
        policy = self.resolve_dm_policy(config)

        if policy == DmPolicy.OPEN:
            warnings.append(
                f"{channel_id}: DM policy is 'open' — anyone can message the bot. "
                "Consider 'allowlist' for production."
            )

        if config:
            for key in ("bot_token", "api_key", "access_token", "password"):
                val = config.get(key, "")
                if val and len(val) < 20:
                    warnings.append(
                        f"{channel_id}: {key} appears unusually short — "
                        "verify it is correct."
                    )

        return warnings

    def verify_sender(self, sender_id: str, sender_name: str = "",
                       config: Dict[str, Any] = None,
                       max_per_minute: int = 10) -> bool:
        """Combined check: DM policy + rate limiting.

        Returns True if the sender is allowed to communicate.
        """
        if not self.is_sender_allowed(sender_id, config):
            return False
        if not self.check_rate_limit(sender_id, max_per_minute=max_per_minute):
            return False

        channel_id = getattr(getattr(self, "meta", None), "id", "unknown")
        _log_security_event(SecurityEvent(
            channel_id=channel_id, event_type="message_allowed",
            sender_id=sender_id, sender_name=sender_name,
            allowed=True,
        ))
        return True


def build_security_tools(registry) -> list:
    """Build LLM tools for security management."""
    tools = []

    def channel_security_status(channel: str = "") -> str:
        from ghost_channels import load_channels_config
        all_cfg = load_channels_config()

        if channel:
            prov = registry.get(channel)
            if not prov:
                return f"Unknown channel: {channel}"
            cfg = all_cfg.get(channel, {})
            if not isinstance(prov, SecurityMixin):
                return f"{channel} does not have security features"
            policy = prov.resolve_dm_policy(cfg)
            warnings = prov.collect_security_warnings(cfg)
            allowlist = prov.get_allowlist(cfg)
            blocklist = prov.get_blocklist(cfg)
            lines = [
                f"{channel} security:",
                f"  DM policy: {policy.value}",
                f"  Allowlist: {len(allowlist)} entries",
                f"  Blocklist: {len(blocklist)} entries",
            ]
            if warnings:
                lines.append(f"  Warnings ({len(warnings)}):")
                for w in warnings:
                    lines.append(f"    - {w}")
            return "\n".join(lines)

        lines = ["Channel security overview:"]
        for cid in registry.list_configured():
            prov = registry.get(cid)
            if prov and isinstance(prov, SecurityMixin):
                cfg = all_cfg.get(cid, {})
                policy = prov.resolve_dm_policy(cfg)
                warnings = prov.collect_security_warnings(cfg)
                warn_str = f" ({len(warnings)} warnings)" if warnings else ""
                lines.append(f"  {cid}: {policy.value}{warn_str}")
        if len(lines) == 1:
            return "No channels with security features configured"
        return "\n".join(lines)

    tools.append({
        "name": "channel_security_status",
        "description": "Check security status and DM policies for messaging channels.",
        "parameters": {
            "type": "object",
            "properties": {
                "channel": {"type": "string",
                            "description": "Channel ID, or empty for all",
                            "default": ""},
            },
        },
        "execute": channel_security_status,
    })

    def channel_security_log(limit: int = 20) -> str:
        events = load_security_log(limit)
        if not events:
            return "No security events logged"
        lines = [f"Recent security events ({len(events)}):"]
        for e in events:
            allowed = "ALLOWED" if e.get("allowed") else "BLOCKED"
            lines.append(
                f"  [{e.get('event_type')}] {e.get('channel_id')}:"
                f"{e.get('sender_id', '')} -> {allowed}"
                f" ({e.get('message', '')})"
            )
        return "\n".join(lines)

    tools.append({
        "name": "channel_security_log",
        "description": "View recent security events (blocked senders, rate limits, etc.).",
        "parameters": {
            "type": "object",
            "properties": {
                "limit": {"type": "integer",
                          "description": "Max events to show",
                          "default": 20},
            },
        },
        "execute": channel_security_log,
    })

    return tools
