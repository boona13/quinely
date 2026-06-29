"""
Gateway Lifecycle Manager

  - Start/stop gateway connections (WhatsApp bridge, Signal CLI, etc.)
  - QR code login flow for gateway-based channels
  - Status tracking: DISCONNECTED, CONNECTING, CONNECTED, ERROR
  - Auto-reconnect with exponential backoff
  - Clean shutdown and logout

Usage:
    if isinstance(provider, GatewayMixin):
        provider.gateway_start(config)
        status = provider.gateway_status()
        provider.gateway_stop()
"""

import time
import threading
import logging
from dataclasses import dataclass, field
from enum import Enum
from typing import Optional, Dict, Any, Callable

log = logging.getLogger("quinely.channels.gateway")


class GatewayState(Enum):
    DISCONNECTED = "disconnected"
    CONNECTING = "connecting"
    CONNECTED = "connected"
    ERROR = "error"
    LOGGED_OUT = "logged_out"


@dataclass
class GatewayStatus:
    """Current state of a gateway connection."""
    state: GatewayState = GatewayState.DISCONNECTED
    account_id: str = ""
    connected_at: float = 0.0
    last_error: str = ""
    reconnect_count: int = 0
    uptime_seconds: float = 0.0

    def to_dict(self) -> Dict[str, Any]:
        return {
            "state": self.state.value,
            "account_id": self.account_id,
            "connected_at": self.connected_at,
            "last_error": self.last_error,
            "reconnect_count": self.reconnect_count,
            "uptime_seconds": (time.time() - self.connected_at
                               if self.state == GatewayState.CONNECTED
                               and self.connected_at > 0 else 0),
        }


@dataclass
class QrLoginResult:
    """Result of a QR code login initiation."""
    qr_data_url: str = ""
    qr_text: str = ""
    message: str = ""
    success: bool = False


@dataclass
class LogoutResult:
    cleared: bool = False
    logged_out: bool = False
    message: str = ""


RECONNECT_BACKOFF = [5, 10, 30, 60, 120, 300]  # seconds
MAX_RECONNECT_ATTEMPTS = 10


class GatewayMixin:
    """Mixin for ChannelProvider subclasses that need gateway lifecycle management.

    Gateway channels (WhatsApp, Signal, iMessage) require a persistent
    connection that must be started, monitored, and can disconnect.
    """

    def __init_gateway__(self):
        """Call from provider __init__ to set up gateway state."""
        self._gw_status = GatewayStatus()
        self._gw_stop_event = threading.Event()
        self._gw_thread: Optional[threading.Thread] = None
        self._gw_reconnect_thread: Optional[threading.Thread] = None
        self._gw_on_status_change: Optional[Callable[[GatewayStatus], None]] = None

    def gateway_start(self, config: Dict[str, Any] = None,
                       on_status_change: Callable = None) -> bool:
        """Start the gateway connection.

        Returns True if the gateway was started successfully.
        Override _gateway_connect() with channel-specific connection logic.
        """
        if not hasattr(self, "_gw_status"):
            self.__init_gateway__()

        self._gw_on_status_change = on_status_change
        self._gw_stop_event.clear()
        self._gw_status.state = GatewayState.CONNECTING
        self._notify_status_change()

        try:
            connected = self._gateway_connect(config or {})
            if connected:
                self._gw_status.state = GatewayState.CONNECTED
                self._gw_status.connected_at = time.time()
                self._gw_status.last_error = ""
                self._notify_status_change()
                return True
            else:
                self._gw_status.state = GatewayState.ERROR
                self._gw_status.last_error = "Connection failed"
                self._notify_status_change()
                return False
        except Exception as exc:
            self._gw_status.state = GatewayState.ERROR
            self._gw_status.last_error = str(exc)
            self._notify_status_change()
            log.error("Gateway start failed: %s", exc)
            return False

    def gateway_stop(self) -> bool:
        """Stop the gateway connection."""
        if not hasattr(self, "_gw_stop_event"):
            return True
        self._gw_stop_event.set()
        try:
            self._gateway_disconnect()
        except Exception as exc:
            log.debug("Gateway disconnect error: %s", exc)
        self._gw_status.state = GatewayState.DISCONNECTED
        self._notify_status_change()
        if self._gw_reconnect_thread:
            self._gw_reconnect_thread.join(timeout=5)
            self._gw_reconnect_thread = None
        return True

    def gateway_status(self) -> GatewayStatus:
        """Return current gateway status."""
        if not hasattr(self, "_gw_status"):
            return GatewayStatus()
        return self._gw_status

    def gateway_login_qr(self, force: bool = False,
                          timeout_ms: int = 60000) -> QrLoginResult:
        """Initiate QR code login. Override for gateway channels."""
        return QrLoginResult(message="QR login not supported by this channel")

    def gateway_logout(self) -> LogoutResult:
        """Logout from the gateway. Override for gateway channels."""
        return LogoutResult(message="Logout not supported by this channel")

    def gateway_auto_reconnect(self, config: Dict[str, Any] = None):
        """Start auto-reconnect loop in background."""
        if not hasattr(self, "_gw_stop_event"):
            self.__init_gateway__()

        def _reconnect_loop():
            attempt = 0
            while not self._gw_stop_event.is_set() and attempt < MAX_RECONNECT_ATTEMPTS:
                if self._gw_status.state == GatewayState.CONNECTED:
                    self._gw_stop_event.wait(10)
                    continue

                backoff = RECONNECT_BACKOFF[min(attempt, len(RECONNECT_BACKOFF) - 1)]
                log.info("Gateway reconnect attempt %d in %ds", attempt + 1, backoff)
                if self._gw_stop_event.wait(backoff):
                    break

                try:
                    connected = self._gateway_connect(config or {})
                    if connected:
                        self._gw_status.state = GatewayState.CONNECTED
                        self._gw_status.connected_at = time.time()
                        self._gw_status.reconnect_count += 1
                        self._gw_status.last_error = ""
                        self._notify_status_change()
                        attempt = 0
                    else:
                        attempt += 1
                        self._gw_status.state = GatewayState.ERROR
                        self._gw_status.last_error = f"Reconnect attempt {attempt} failed"
                        self._notify_status_change()
                except Exception as exc:
                    attempt += 1
                    self._gw_status.state = GatewayState.ERROR
                    self._gw_status.last_error = str(exc)
                    self._notify_status_change()

            if attempt >= MAX_RECONNECT_ATTEMPTS:
                log.error("Gateway max reconnect attempts (%d) exceeded",
                          MAX_RECONNECT_ATTEMPTS)

        self._gw_reconnect_thread = threading.Thread(
            target=_reconnect_loop, daemon=True,
            name=f"gateway-reconnect-{getattr(getattr(self, 'meta', None), 'id', 'unknown')}",
        )
        self._gw_reconnect_thread.start()

    def _gateway_connect(self, config: Dict[str, Any]) -> bool:
        """Channel-specific connection logic. Override in provider."""
        return False

    def _gateway_disconnect(self):
        """Channel-specific disconnection logic. Override in provider."""
        pass

    def _notify_status_change(self):
        if hasattr(self, "_gw_on_status_change") and self._gw_on_status_change:
            try:
                self._gw_on_status_change(self._gw_status)
            except Exception:
                pass


def build_gateway_tools(registry) -> list:
    """Build LLM tools for gateway management."""
    tools = []

    def channel_gateway_status(channel: str = "") -> str:
        import json
        if channel:
            prov = registry.get(channel)
            if not prov:
                return f"Unknown channel: {channel}"
            if not isinstance(prov, GatewayMixin):
                return f"{channel} is not a gateway channel"
            status = prov.gateway_status()
            return f"{channel} gateway: {json.dumps(status.to_dict(), indent=2)}"
        lines = ["Gateway statuses:"]
        for cid in registry.list_available():
            prov = registry.get(cid)
            if prov and isinstance(prov, GatewayMixin):
                status = prov.gateway_status()
                lines.append(f"  {cid}: {status.state.value}"
                             + (f" (error: {status.last_error})"
                                if status.last_error else ""))
        if len(lines) == 1:
            return "No gateway channels configured"
        return "\n".join(lines)

    tools.append({
        "name": "channel_gateway_status",
        "description": "Check gateway connection status for channels that use persistent connections (WhatsApp, Signal, etc.).",
        "parameters": {
            "type": "object",
            "properties": {
                "channel": {"type": "string",
                            "description": "Channel ID, or empty for all gateways",
                            "default": ""},
            },
        },
        "execute": channel_gateway_status,
    })

    return tools
