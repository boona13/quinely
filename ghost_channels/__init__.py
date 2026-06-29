"""
QUINELY Multi-Channel Messaging — Phase 2

Plugin-based messaging across 20+ channels (Slack, Discord, Telegram, WhatsApp,
Signal, ntfy, Email, Webhook, etc.).  Integrated with Quinely's autonomy engine.

Phase 1:
  - ChannelProvider ABC with optional adapter slots (outbound, inbound, media)
  - ChannelRegistry for auto-discovery and lazy loading of providers
  - MessageRouter with preferred-channel selection and fallback chain
  - InboundDispatcher for bidirectional chat from any channel
  - build_channel_tools() for LLM-accessible tool registration

Phase 2:
  - Write-ahead delivery queue with exponential backoff retries (queue.py)
  - Per-channel message formatting pipeline (formatting.py)
  - Message actions: react, edit, unsend, poll (actions.py)
  - Advanced threading: modes, context, history (threading_ext.py)
  - Streaming: coalesced in-place message editing (streaming.py)
  - Multi-account support per provider
  - Directory & resolver: contacts, groups, target resolution (directory.py)
  - Gateway lifecycle: start/stop/QR login/reconnect (gateway.py)
  - Enhanced health monitoring: probe, audit, snapshots (health.py)
  - Security: DM policies, rate limiting, audit log (security.py)
  - Onboarding wizards: step-by-step setup (onboard.py)
  - Mention handling: strip, extract, format (mentions.py)
  - Agent prompt adapter: per-channel hints (agent_prompts.py)

Each sub-module in this package exports a `Provider` class that subclasses
`ChannelProvider`.  The registry auto-discovers them at startup via
`pkgutil.iter_modules`.
"""

import json
import time
import threading
import logging
import importlib
import pkgutil
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import Optional, Dict, Any, List, Callable

import requests

GHOST_HOME = Path.home() / ".ghost"
CHANNELS_CONFIG_FILE = GHOST_HOME / "channels.json"
CHANNEL_STATE_FILE = GHOST_HOME / "channel_state.json"
INBOUND_LOG_FILE = GHOST_HOME / "channel_inbound.json"

MAX_INBOUND_LOG = 200

log = logging.getLogger("quinely.channels")


# ═══════════════════════════════════════════════════════════════
#  DATA TYPES
# ═══════════════════════════════════════════════════════════════

class DeliveryMode(Enum):
    DIRECT = "direct"
    GATEWAY = "gateway"
    WEBHOOK = "webhook"


class NotifyPriority(Enum):
    LOW = "low"
    NORMAL = "normal"
    HIGH = "high"
    CRITICAL = "critical"


@dataclass
class ChannelMeta:
    id: str
    label: str
    emoji: str = ""
    supports_media: bool = False
    supports_threads: bool = False
    supports_reactions: bool = False
    supports_groups: bool = False
    supports_inbound: bool = False
    supports_edit: bool = False
    supports_unsend: bool = False
    supports_polls: bool = False
    supports_streaming: bool = False
    supports_directory: bool = False
    supports_gateway: bool = False
    text_chunk_limit: int = 4000
    delivery_mode: DeliveryMode = DeliveryMode.DIRECT
    docs_url: str = ""
    max_accounts: int = 1


@dataclass
class OutboundResult:
    ok: bool
    message_id: Optional[str] = None
    error: Optional[str] = None
    channel_id: str = ""
    provider_data: Dict[str, Any] = field(default_factory=dict)


@dataclass
class InboundMessage:
    channel_id: str
    sender_id: str
    sender_name: str
    text: str
    thread_id: Optional[str] = None
    reply_to_id: Optional[str] = None
    media_urls: List[str] = field(default_factory=list)
    raw: Dict[str, Any] = field(default_factory=dict)
    timestamp: float = 0.0


# ═══════════════════════════════════════════════════════════════
#  CHANNEL PROVIDER ABC
# ═══════════════════════════════════════════════════════════════

class ChannelProvider(ABC):
    """Base class all channel providers must implement.

    Phase 2 additions:
      - Multi-account support via account_id parameter
      - Adapter detection helpers (has_actions, has_threading, etc.)
      - list_accounts / resolve_account for multi-account channels
    """

    meta: ChannelMeta

    @abstractmethod
    def configure(self, config: Dict[str, Any]) -> bool:
        """Validate and store config.  Return True if provider is usable."""

    @abstractmethod
    def send_text(self, to: str, text: str, **kwargs) -> OutboundResult:
        """Send a plain-text message to *to*.  Chunking handled by caller."""

    def send_media(self, to: str, media_path: str, caption: str = "",
                   **kwargs) -> OutboundResult:
        return OutboundResult(ok=False, error=f"{self.meta.id}: media not supported",
                              channel_id=self.meta.id)

    def send_typing(self, to: str, **kwargs) -> bool:
        """Send a typing/processing indicator. Override per channel."""
        return False

    def chunk_text(self, text: str) -> List[str]:
        """Split *text* into chunks respecting the channel's limit."""
        limit = self.meta.text_chunk_limit
        if len(text) <= limit:
            return [text]
        chunks: List[str] = []
        paragraphs = text.split("\n\n")
        current = ""
        for para in paragraphs:
            candidate = f"{current}\n\n{para}" if current else para
            if len(candidate) > limit:
                if current:
                    chunks.append(current)
                while len(para) > limit:
                    chunks.append(para[:limit])
                    para = para[limit:]
                current = para
            else:
                current = candidate
        if current:
            chunks.append(current)
        return chunks or [text[:limit]]

    def start_inbound(self, on_message: Callable[[InboundMessage], None]) -> bool:
        """Begin listening for messages from the user.  Return False if unsupported."""
        return False

    def stop_inbound(self):
        pass

    def health_check(self) -> Dict[str, Any]:
        """Return provider health: configured, connected, last_error, etc."""
        return {"status": "unknown", "configured": False}

    def get_config_schema(self) -> Dict[str, Any]:
        """Return a JSON-schema-like dict describing required config fields."""
        return {}

    # ── Multi-Account (Phase 2) ──────────────────────────────

    def list_accounts(self) -> List[Dict[str, Any]]:
        """List configured accounts.  Default: single implicit account."""
        return [{"account_id": "default", "is_default": True}]

    def resolve_account(self, account_id: str = None) -> Optional[str]:
        """Resolve which account to use.  Returns account_id or None."""
        return account_id or "default"

    def set_default_account(self, account_id: str) -> bool:
        """Set the default account.  Override for multi-account channels."""
        return False

    # ── Adapter detection helpers (Phase 2) ──────────────────

    def get_capabilities(self) -> Dict[str, bool]:
        """Return a dict of all capabilities for introspection."""
        caps = {
            "media": self.meta.supports_media,
            "threads": self.meta.supports_threads,
            "reactions": self.meta.supports_reactions,
            "groups": self.meta.supports_groups,
            "inbound": self.meta.supports_inbound,
            "edit": self.meta.supports_edit,
            "unsend": self.meta.supports_unsend,
            "polls": self.meta.supports_polls,
            "streaming": self.meta.supports_streaming,
            "directory": self.meta.supports_directory,
            "gateway": self.meta.supports_gateway,
        }
        try:
            from ghost_channels.actions import ActionsMixin
            caps["actions"] = isinstance(self, ActionsMixin)
        except ImportError:
            pass
        try:
            from ghost_channels.threading_ext import ThreadingMixin
            caps["advanced_threading"] = isinstance(self, ThreadingMixin)
        except ImportError:
            pass
        try:
            from ghost_channels.streaming import StreamingMixin
            caps["streaming_edits"] = isinstance(self, StreamingMixin)
        except ImportError:
            pass
        try:
            from ghost_channels.directory import DirectoryMixin
            caps["directory_listing"] = isinstance(self, DirectoryMixin)
        except ImportError:
            pass
        try:
            from ghost_channels.gateway import GatewayMixin
            caps["gateway_lifecycle"] = isinstance(self, GatewayMixin)
        except ImportError:
            pass
        try:
            from ghost_channels.security import SecurityMixin
            caps["security"] = isinstance(self, SecurityMixin)
        except ImportError:
            pass
        try:
            from ghost_channels.onboard import OnboardingMixin
            caps["onboarding"] = isinstance(self, OnboardingMixin)
        except ImportError:
            pass
        try:
            from ghost_channels.mentions import MentionMixin
            caps["mentions"] = isinstance(self, MentionMixin)
        except ImportError:
            pass
        return caps


# ═══════════════════════════════════════════════════════════════
#  CHANNEL REGISTRY
# ═══════════════════════════════════════════════════════════════

class ChannelRegistry:
    """Discovers, registers, and manages channel providers."""

    def __init__(self):
        self._providers: Dict[str, ChannelProvider] = {}

    def register(self, provider: ChannelProvider):
        self._providers[provider.meta.id] = provider

    def get(self, channel_id: str) -> Optional[ChannelProvider]:
        return self._providers.get(channel_id)

    def list_all(self) -> List[ChannelMeta]:
        return [p.meta for p in self._providers.values()]

    def list_configured(self) -> List[str]:
        ids = []
        for cid, prov in self._providers.items():
            try:
                h = prov.health_check()
                if h.get("configured"):
                    ids.append(cid)
            except Exception:
                pass
        return ids

    def list_available(self) -> List[str]:
        return list(self._providers.keys())

    def auto_discover(self, channels_config: Dict[str, Any]):
        """Import every provider from the ghost_channels package and configure."""
        import ghost_channels as pkg

        for importer, modname, _ispkg in pkgutil.iter_modules(pkg.__path__):
            if modname.startswith("_"):
                continue
            try:
                mod = importlib.import_module(f"ghost_channels.{modname}")
                provider_cls = getattr(mod, "Provider", None)
                if provider_cls and issubclass(provider_cls, ChannelProvider):
                    provider = provider_cls()
                    cfg_section = channels_config.get(provider.meta.id, {})
                    if cfg_section.get("enabled", False):
                        provider.configure(cfg_section)
                    self.register(provider)
            except Exception as exc:
                log.debug("Failed to load channel provider %s: %s", modname, exc)


# ═══════════════════════════════════════════════════════════════
#  CHANNEL CONFIG I/O
# ═══════════════════════════════════════════════════════════════

_config_lock = threading.Lock()


def load_channels_config() -> Dict[str, Any]:
    with _config_lock:
        if CHANNELS_CONFIG_FILE.exists():
            try:
                return json.loads(CHANNELS_CONFIG_FILE.read_text(encoding="utf-8"))
            except Exception:
                pass
    return {}


def save_channels_config(cfg: Dict[str, Any]):
    with _config_lock:
        GHOST_HOME.mkdir(parents=True, exist_ok=True)
        CHANNELS_CONFIG_FILE.write_text(json.dumps(cfg, indent=2), encoding="utf-8")


def _load_state() -> Dict[str, Any]:
    if CHANNEL_STATE_FILE.exists():
        try:
            return json.loads(CHANNEL_STATE_FILE.read_text(encoding="utf-8"))
        except Exception:
            pass
    return {}


def _save_state(state: Dict[str, Any]):
    GHOST_HOME.mkdir(parents=True, exist_ok=True)
    CHANNEL_STATE_FILE.write_text(json.dumps(state, indent=2), encoding="utf-8")


def _mask_secret(val: str) -> str:
    if not val or len(val) < 8:
        return "***"
    return val[:4] + "..." + val[-4:]


SECRET_KEYS = {"bot_token", "api_key", "app_token", "password", "secret",
               "webhook_url", "access_token", "client_secret"}


def _sanitize_config(cfg: Dict[str, Any]) -> Dict[str, Any]:
    """Return a copy of *cfg* with secrets masked."""
    out = {}
    for k, v in cfg.items():
        if isinstance(v, dict):
            out[k] = _sanitize_config(v)
        elif isinstance(v, str) and k in SECRET_KEYS:
            out[k] = _mask_secret(v)
        else:
            out[k] = v
    return out


# ═══════════════════════════════════════════════════════════════
#  INBOUND LOG
# ═══════════════════════════════════════════════════════════════

_inbound_lock = threading.Lock()


def _append_inbound_log(msg: InboundMessage):
    with _inbound_lock:
        entries: list = []
        if INBOUND_LOG_FILE.exists():
            try:
                entries = json.loads(INBOUND_LOG_FILE.read_text(encoding="utf-8"))
            except Exception:
                entries = []
        entries.insert(0, {
            "channel": msg.channel_id,
            "sender_id": msg.sender_id,
            "sender_name": msg.sender_name,
            "text": msg.text[:500],
            "timestamp": msg.timestamp or time.time(),
        })
        entries = entries[:MAX_INBOUND_LOG]
        GHOST_HOME.mkdir(parents=True, exist_ok=True)
        INBOUND_LOG_FILE.write_text(json.dumps(entries, indent=2), encoding="utf-8")


# ═══════════════════════════════════════════════════════════════
#  MESSAGE ROUTER
# ═══════════════════════════════════════════════════════════════

class MessageRouter:
    """Selects the right channel, chunks text, delivers, and tracks state.

    Phase 2 additions:
      - Integrated write-ahead delivery queue with retries
      - Per-channel message formatting pipeline
      - Queue stats and recovery
    """

    def __init__(self, registry: ChannelRegistry, config: dict):
        self.registry = registry
        self.config = config
        self._queue = None
        self._formatter = None

        try:
            from ghost_channels.formatting import MessageFormatter
            self._formatter = MessageFormatter()
        except Exception:
            pass

    def enable_queue(self, retry_interval: float = 30.0):
        """Enable the write-ahead delivery queue with background retries."""
        try:
            from ghost_channels.queue import DeliveryQueue
            self._queue = DeliveryQueue(self._raw_send, retry_interval)
            self._queue.start()
            log.info("Delivery queue enabled (retry interval: %.0fs)", retry_interval)
        except Exception as exc:
            log.warning("Failed to enable delivery queue: %s", exc)

    def disable_queue(self):
        """Stop the delivery queue."""
        if self._queue:
            self._queue.stop()
            self._queue = None

    def recover_queue(self, max_seconds: float = 60.0) -> Dict[str, int]:
        """Recover pending deliveries from the queue (called on startup)."""
        if self._queue:
            return self._queue.recover(max_seconds)
        return {"recovered": 0, "failed": 0, "skipped": 0}

    def queue_stats(self) -> Dict[str, Any]:
        """Get delivery queue statistics."""
        try:
            from ghost_channels.queue import queue_stats
            return queue_stats()
        except Exception:
            return {}

    def _resolve_channel(self, channel: Optional[str] = None) -> Optional[ChannelProvider]:
        if channel:
            prov = self.registry.get(channel)
            if prov:
                return prov

        preferred = self.config.get("preferred_channel", "")
        if preferred:
            prov = self.registry.get(preferred)
            if prov and prov.health_check().get("configured"):
                return prov

        fallback = self.config.get("channel_fallback_order",
                                   ["ntfy", "telegram", "slack", "discord", "email"])
        for cid in fallback:
            prov = self.registry.get(cid)
            if prov:
                try:
                    h = prov.health_check()
                    if h.get("configured"):
                        return prov
                except Exception:
                    continue
        return None

    def _raw_send(self, channel: str, to: str, text: str,
                  **kwargs) -> OutboundResult:
        """Direct send without queue — used by DeliveryQueue internally."""
        prov = self.registry.get(channel)
        if not prov:
            return OutboundResult(ok=False, error=f"Unknown channel: {channel}")

        if self._formatter:
            text = self._formatter.format_text(text, channel)
            chunks = self._formatter.chunk_text(
                text, channel, limit=prov.meta.text_chunk_limit
            )
        else:
            chunks = prov.chunk_text(text)

        last_result = OutboundResult(ok=False, error="no chunks")
        for chunk in chunks:
            last_result = prov.send_text(to=to or "", text=chunk, **kwargs)
            if not last_result.ok:
                break
        last_result.channel_id = prov.meta.id
        self._record_send(prov.meta.id, last_result)
        return last_result

    def send(self, text: str, channel: str = None, to: str = None,
             priority: str = "normal", **kwargs) -> OutboundResult:
        prov = self._resolve_channel(channel)
        if not prov:
            return OutboundResult(ok=False, error="No configured channel available")

        resolved_channel = prov.meta.id

        if self._queue:
            return self._queue.deliver(
                resolved_channel, to or "", text,
                priority=priority, **kwargs
            )

        return self._raw_send(resolved_channel, to or "", text, **kwargs)

    def send_to_all(self, text: str, channels: List[str] = None,
                    **kwargs) -> List[OutboundResult]:
        targets = channels or self.registry.list_configured()
        results = []
        for cid in targets:
            results.append(self.send(text, channel=cid, **kwargs))
        return results

    def get_preferred_channel(self) -> Optional[str]:
        prov = self._resolve_channel()
        return prov.meta.id if prov else None

    def _record_send(self, channel_id: str, result: OutboundResult):
        try:
            state = _load_state()
            cs = state.setdefault(channel_id, {})
            cs["last_send_at"] = time.time()
            cs["last_send_ok"] = result.ok
            if result.error:
                cs["last_error"] = result.error
            _save_state(state)
        except Exception:
            pass


# ═══════════════════════════════════════════════════════════════
#  INBOUND DISPATCHER
# ═══════════════════════════════════════════════════════════════

class InboundDispatcher:
    """Starts inbound listeners on all configured channels and routes messages."""

    def __init__(self, registry: ChannelRegistry, config: dict,
                 on_message: Callable[[InboundMessage], None]):
        self.registry = registry
        self.config = config
        self.on_message = on_message
        self._active: List[str] = []
        self._security_enforcer = None

    def start_all(self):
        if not self.config.get("channel_inbound_enabled", True):
            return
        
        # Initialize security enforcer for enhanced security checking
        try:
            from ghost_channel_security import ChannelSecurityEnforcer
            self._security_enforcer = ChannelSecurityEnforcer(self.config)
            log.info("Channel security enforcer initialized")
        except Exception as e:
            log.warning("Failed to initialize channel security enforcer: %s", e)
            self._security_enforcer = None
        
        dm_policy = self.config.get("channel_dm_policy", "open")
        allowed = set(self.config.get("channel_allowed_senders", []))
        channels_cfg = load_channels_config()

        for cid in self.registry.list_configured():
            if not channels_cfg.get(cid, {}).get("enabled", False):
                continue
            prov = self.registry.get(cid)
            if not prov or not prov.meta.supports_inbound:
                continue

            def _make_handler(_cid, _prov):
                def handler(msg: InboundMessage):
                    msg.channel_id = _cid
                    
                    # Use security enforcer for comprehensive checks
                    if self._security_enforcer:
                        try:
                            decision = self._security_enforcer.check_message(
                                msg.sender_id, msg.text, _cid
                            )
                            if not decision.allowed:
                                log.warning("Inbound blocked by security: %s | %s | risk=%s | reason=%s",
                                           _cid, msg.sender_id, decision.risk_score, decision.reason)
                                return
                            if decision.action == "allow_warn":
                                log.warning("Inbound allowed with warning: %s | %s | risk=%s",
                                           _cid, msg.sender_id, decision.risk_score)
                        except Exception as e:
                            log.error("Security check failed, falling back to basic policy: %s", e)
                            # Fall back to basic policy check
                            if dm_policy == "allowlist" and msg.sender_id not in allowed:
                                log.info("Inbound from %s blocked (allowlist): %s", _cid, msg.sender_id)
                                return
                    else:
                        # Legacy policy check (fallback)
                        if dm_policy == "allowlist" and msg.sender_id not in allowed:
                            log.info("Inbound from %s blocked (allowlist): %s", _cid, msg.sender_id)
                            return
                    
                    _append_inbound_log(msg)
                    try:
                        self.on_message(msg)
                    except Exception as exc:
                        log.error("Error processing inbound from %s: %s", _cid, exc)
                return handler

            try:
                started = prov.start_inbound(_make_handler(cid, prov))
                if started:
                    self._active.append(cid)
                    log.info("Inbound listener started: %s", cid)
            except Exception as exc:
                log.error("Failed to start inbound for %s: %s", cid, exc)

    def start_one(self, channel_id: str) -> bool:
        """Start inbound listener for a single channel (e.g. after wizard config)."""
        if channel_id in self._active:
            return True
        if not self.config.get("channel_inbound_enabled", True):
            return False
        channels_cfg = load_channels_config()
        if not channels_cfg.get(channel_id, {}).get("enabled", False):
            return False
        prov = self.registry.get(channel_id)
        if not prov or not prov.meta.supports_inbound:
            return False

        dm_policy = self.config.get("channel_dm_policy", "open")
        allowed = set(self.config.get("channel_allowed_senders", []))

        def _make_handler(_cid, _prov):
            def handler(msg: InboundMessage):
                msg.channel_id = _cid
                if self._security_enforcer:
                    try:
                        decision = self._security_enforcer.check_message(
                            msg.sender_id, msg.text, _cid
                        )
                        if not decision.allowed:
                            log.warning("Inbound blocked by security: %s | %s | risk=%s | reason=%s",
                                       _cid, msg.sender_id, decision.risk_score, decision.reason)
                            return
                    except Exception as e:
                        log.error("Security check failed, falling back to basic policy: %s", e)
                        if dm_policy == "allowlist" and msg.sender_id not in allowed:
                            log.info("Inbound from %s blocked (allowlist): %s", _cid, msg.sender_id)
                            return
                else:
                    if dm_policy == "allowlist" and msg.sender_id not in allowed:
                        log.info("Inbound from %s blocked (allowlist): %s", _cid, msg.sender_id)
                        return
                _append_inbound_log(msg)
                try:
                    self.on_message(msg)
                except Exception as exc:
                    log.error("Error processing inbound from %s: %s", _cid, exc)
            return handler

        try:
            started = prov.start_inbound(_make_handler(channel_id, prov))
            if started:
                self._active.append(channel_id)
                log.info("Inbound listener started: %s", channel_id)
                return True
        except Exception as exc:
            log.error("Failed to start inbound for %s: %s", channel_id, exc)
        return False

    def stop_all(self):
        for cid in self._active:
            prov = self.registry.get(cid)
            if prov:
                try:
                    prov.stop_inbound()
                except Exception:
                    pass
        self._active.clear()


# ═══════════════════════════════════════════════════════════════
#  LLM TOOLS
# ═══════════════════════════════════════════════════════════════

def build_channel_tools(router: MessageRouter, registry: ChannelRegistry,
                        channels_config: dict) -> list:
    """Build Quinely tools for channel messaging.  Returns list of tool defs."""

    tools: list = []

    # --- channel_send ---
    def channel_send(message: str, channel: str = "", to: str = "",
                     priority: str = "normal") -> str:
        result = router.send(message, channel=channel or None,
                             to=to or None, priority=priority)
        if result.ok:
            return f"OK: sent via {result.channel_id}" + (
                f" (msg_id={result.message_id})" if result.message_id else "")
        return f"FAILED: {result.error}"

    tools.append({
        "name": "channel_send",
        "description": (
            "Send a message to the user via their preferred messaging channel "
            "(Telegram, Slack, Discord, ntfy, email, etc.).  "
            "Use for proactive notifications, alerts, and replies."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "message": {"type": "string",
                            "description": "The message text to send"},
                "channel": {"type": "string",
                            "description": "Channel ID (e.g. 'telegram', 'slack'). "
                                           "Leave empty to use preferred channel.",
                            "default": ""},
                "to": {"type": "string",
                       "description": "Recipient ID/address on the channel. "
                                      "Leave empty for default recipient.",
                       "default": ""},
                "priority": {"type": "string",
                             "enum": ["low", "normal", "high", "critical"],
                             "description": "Message priority level",
                             "default": "normal"},
            },
            "required": ["message"],
        },
        "execute": channel_send,
    })

    # --- channel_broadcast ---
    def channel_broadcast(message: str, channels: str = "") -> str:
        ch_list = [c.strip() for c in channels.split(",") if c.strip()] if channels else None
        results = router.send_to_all(message, channels=ch_list)
        ok_count = sum(1 for r in results if r.ok)
        fail_count = len(results) - ok_count
        parts = [f"Broadcast: {ok_count} sent, {fail_count} failed"]
        for r in results:
            status = "OK" if r.ok else f"FAIL: {r.error}"
            parts.append(f"  {r.channel_id}: {status}")
        return "\n".join(parts)

    tools.append({
        "name": "channel_broadcast",
        "description": "Broadcast a message to multiple channels at once (for critical alerts).",
        "parameters": {
            "type": "object",
            "properties": {
                "message": {"type": "string", "description": "The message to broadcast"},
                "channels": {"type": "string",
                             "description": "Comma-separated channel IDs. "
                                            "Empty = all configured channels.",
                             "default": ""},
            },
            "required": ["message"],
        },
        "execute": channel_broadcast,
    })

    # --- channel_list ---
    def channel_list() -> str:
        all_channels = registry.list_all()
        configured = set(registry.list_configured())
        lines = ["Available channels:\n"]
        for meta in sorted(all_channels, key=lambda m: m.id):
            status = "configured" if meta.id in configured else "not configured"
            caps = []
            if meta.supports_inbound:
                caps.append("inbound")
            if meta.supports_media:
                caps.append("media")
            if meta.supports_threads:
                caps.append("threads")
            cap_str = f" [{', '.join(caps)}]" if caps else ""
            lines.append(f"  {meta.emoji} {meta.label} ({meta.id}): {status}{cap_str}")
        preferred = router.get_preferred_channel()
        if preferred:
            lines.append(f"\nPreferred channel: {preferred}")
        return "\n".join(lines)

    tools.append({
        "name": "channel_list",
        "description": "List all available messaging channels and their configuration status.",
        "parameters": {"type": "object", "properties": {}},
        "execute": channel_list,
    })

    # --- channel_configure ---
    def channel_configure(channel: str, config_json: str) -> str:
        prov = registry.get(channel)
        if not prov:
            available = ", ".join(registry.list_available())
            return f"Unknown channel '{channel}'. Available: {available}"
        try:
            new_cfg = json.loads(config_json)
        except json.JSONDecodeError as e:
            return f"Invalid JSON: {e}"
        new_cfg["enabled"] = True
        ok = prov.configure(new_cfg)
        all_cfg = load_channels_config()
        all_cfg[channel] = new_cfg
        save_channels_config(all_cfg)
        safe = _sanitize_config(new_cfg)
        if ok:
            return f"OK: {channel} configured. Settings: {json.dumps(safe)}"
        return f"WARNING: {channel} saved but configure() returned False. Check credentials."

    tools.append({
        "name": "channel_configure",
        "description": (
            "Configure a messaging channel with credentials/settings.  "
            "Pass config as a JSON string.  Use channel_list to see available channels "
            "and their required fields."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "channel": {"type": "string",
                            "description": "Channel ID (e.g. 'telegram', 'slack', 'ntfy')"},
                "config_json": {"type": "string",
                                "description": "JSON string with channel config "
                                               "(e.g. '{\"bot_token\":\"...\",\"default_chat_id\":\"123\"}')"},
            },
            "required": ["channel", "config_json"],
        },
        "execute": channel_configure,
    })

    # --- channel_status ---
    def channel_status(channel: str = "") -> str:
        if channel:
            prov = registry.get(channel)
            if not prov:
                return f"Unknown channel: {channel}"
            h = prov.health_check()
            return f"{prov.meta.label} ({channel}):\n{json.dumps(h, indent=2)}"
        lines = ["Channel status:\n"]
        for cid in registry.list_available():
            prov = registry.get(cid)
            if prov:
                h = prov.health_check()
                status = "ready" if h.get("configured") else "not configured"
                err = h.get("last_error", "")
                err_str = f" (error: {err})" if err else ""
                lines.append(f"  {prov.meta.label}: {status}{err_str}")
        return "\n".join(lines)

    tools.append({
        "name": "channel_status",
        "description": "Check the health/connection status of messaging channels.",
        "parameters": {
            "type": "object",
            "properties": {
                "channel": {"type": "string",
                            "description": "Channel ID to check, or empty for all",
                            "default": ""},
            },
        },
        "execute": channel_status,
    })

    # --- channel_set_default ---
    def channel_set_default(channel: str) -> str:
        prov = registry.get(channel)
        if not prov:
            return f"Unknown channel: {channel}"
        h = prov.health_check()
        if not h.get("configured"):
            return f"{channel} is not configured yet.  Configure it first with channel_configure."
        router.config["preferred_channel"] = channel
        return f"OK: preferred channel set to {prov.meta.label} ({channel})"

    tools.append({
        "name": "channel_set_default",
        "description": "Set the user's preferred notification channel.",
        "parameters": {
            "type": "object",
            "properties": {
                "channel": {"type": "string",
                            "description": "Channel ID to set as default"},
            },
            "required": ["channel"],
        },
        "execute": channel_set_default,
    })

    return tools


# ═══════════════════════════════════════════════════════════════
#  MODULE-LEVEL BOOTSTRAP
# ═══════════════════════════════════════════════════════════════

def init_channels(cfg: dict) -> tuple:
    """Initialize the full channel stack.

    Returns (registry, router, inbound_dispatcher_factory).
    The inbound_dispatcher_factory is a callable(on_message) -> InboundDispatcher
    so the daemon can provide its callback later.
    """
    registry = ChannelRegistry()
    channels_cfg = load_channels_config()
    registry.auto_discover(channels_cfg)

    router = MessageRouter(registry, cfg)

    if cfg.get("enable_delivery_queue", True):
        retry_interval = cfg.get("delivery_queue_retry_interval", 30.0)
        router.enable_queue(retry_interval)

    def make_inbound(on_message):
        return InboundDispatcher(registry, cfg, on_message)

    return registry, router, make_inbound


def build_phase2_tools(router, registry, cfg: dict) -> list:
    """Build all Phase 2 LLM tools (actions, threading, directory, etc.)."""
    tools = []

    try:
        from ghost_channels.actions import build_action_tools
        tools.extend(build_action_tools(router, registry))
    except Exception as exc:
        log.debug("Failed to load action tools: %s", exc)

    try:
        from ghost_channels.threading_ext import build_threading_tools
        tools.extend(build_threading_tools(registry))
    except Exception as exc:
        log.debug("Failed to load threading tools: %s", exc)

    try:
        from ghost_channels.directory import build_directory_tools
        tools.extend(build_directory_tools(registry))
    except Exception as exc:
        log.debug("Failed to load directory tools: %s", exc)

    try:
        from ghost_channels.gateway import build_gateway_tools
        tools.extend(build_gateway_tools(registry))
    except Exception as exc:
        log.debug("Failed to load gateway tools: %s", exc)

    try:
        from ghost_channels.health import build_health_tools
        tools.extend(build_health_tools(registry))
    except Exception as exc:
        log.debug("Failed to load health tools: %s", exc)

    try:
        from ghost_channels.security import build_security_tools
        tools.extend(build_security_tools(registry))
    except Exception as exc:
        log.debug("Failed to load security tools: %s", exc)

    try:
        from ghost_channels.onboard import build_onboarding_tools
        tools.extend(build_onboarding_tools(registry))
    except Exception as exc:
        log.debug("Failed to load onboarding tools: %s", exc)

    try:
        from ghost_channels.agent_prompts import build_prompt_tools
        tools.extend(build_prompt_tools(registry))
    except Exception as exc:
        log.debug("Failed to load prompt tools: %s", exc)

    return tools
