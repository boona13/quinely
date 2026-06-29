"""
WhatsApp Channel Provider — Dual-Mode

Mode 1: "web" (default) — Personal WhatsApp via QR code linking (neonize/whatsmeow).
         User scans a QR code in the Quinely dashboard to link their WhatsApp account.
         Full bidirectional messaging without any Meta developer account.

Mode 2: "business" — WhatsApp Business Cloud API (Meta Graph API).
         Requires access_token + phone_number_id from Meta Developer Portal.
         Webhook-based inbound.

The mode is selected via the `mode` config field.
"""

import io
import os
import time
import base64
import logging
import threading
from pathlib import Path
from typing import Dict, Any, Callable, Optional

import requests

from ghost_channels import (
    ChannelProvider, ChannelMeta, DeliveryMode,
    OutboundResult, InboundMessage, GHOST_HOME,
)

log = logging.getLogger("ghost.channels.whatsapp")

GRAPH_API = "https://graph.facebook.com/v21.0"
WA_DATA_DIR = GHOST_HOME / "whatsapp_web"


# ═══════════════════════════════════════════════════════════════
#  Neonize (Web mode) — lazy loaded
# ═══════════════════════════════════════════════════════════════

_neonize_available = None
_neonize_error = None


def _ensure_magic_module():
    """Ensure a `magic` module is available before neonize is imported.

    neonize depends on python-magic which requires the libmagic C library
    installed on the system.  That's a bad UX for fresh installs on any OS.

    This function injects a pure-Python shim into sys.modules so neonize
    can import `magic` and call `magic.from_buffer(data, mime=True)`
    without any system dependency.  The shim uses Python's built-in
    `mimetypes` module + header sniffing — good enough for WhatsApp media.
    """
    import sys
    if "magic" in sys.modules:
        return

    try:
        import magic  # noqa: F401 — real python-magic works, nothing to do
        return
    except (ImportError, OSError):
        pass

    import types
    import mimetypes

    _SIGNATURES = [
        (b"\xff\xd8\xff",           "image/jpeg"),
        (b"\x89PNG\r\n\x1a\n",     "image/png"),
        (b"GIF87a",                 "image/gif"),
        (b"GIF89a",                 "image/gif"),
        (b"RIFF",                   "image/webp"),      # RIFF....WEBP
        (b"\x00\x00\x00",          "video/mp4"),        # ftyp box
        (b"\x1a\x45\xdf\xa3",      "video/webm"),
        (b"OggS",                   "audio/ogg"),
        (b"ID3",                    "audio/mpeg"),
        (b"\xff\xfb",              "audio/mpeg"),
        (b"\xff\xf3",              "audio/mpeg"),
        (b"fLaC",                   "audio/flac"),
        (b"%PDF",                   "application/pdf"),
        (b"PK\x03\x04",           "application/zip"),
    ]

    def _from_buffer(data, mime=False):
        if not isinstance(data, (bytes, bytearray)):
            return "application/octet-stream" if mime else "data"
        header = bytes(data[:16])
        for sig, mimetype in _SIGNATURES:
            if header.startswith(sig):
                return mimetype if mime else mimetype.split("/")[1]
        if b"WEBP" in header:
            return "image/webp" if mime else "webp"
        if b"ftyp" in header[:12]:
            return "video/mp4" if mime else "mp4"
        return "application/octet-stream" if mime else "data"

    def _from_file(path, mime=False):
        guess, _ = mimetypes.guess_type(str(path))
        if guess:
            return guess if mime else guess.split("/")[1]
        try:
            with open(path, "rb") as f:
                return _from_buffer(f.read(16), mime=mime)
        except Exception:
            return "application/octet-stream" if mime else "data"

    shim = types.ModuleType("magic")
    shim.from_buffer = _from_buffer
    shim.from_file = _from_file
    shim.__doc__ = "Pure-Python magic shim for neonize (no libmagic needed)"
    shim.__ghost_shim__ = True
    sys.modules["magic"] = shim
    log.debug("Injected pure-Python magic shim (no libmagic needed)")


def _check_neonize():
    """Check if neonize is importable.  Returns True/False.
    On failure, _neonize_error is set with a user-friendly install hint."""
    global _neonize_available, _neonize_error
    if _neonize_available is not None:
        return _neonize_available

    _ensure_magic_module()

    try:
        from neonize.client import NewClient as _NC  # noqa: F401
        _neonize_available = True
        _neonize_error = None
    except ImportError as exc:
        _neonize_available = False
        _neonize_error = (
            f"neonize not installed.\n\n"
            f"Install with:\n  pip install neonize\n\n"
            f"Then restart Quinely."
        )
        log.warning("WhatsApp Web mode unavailable: %s", exc)
    return _neonize_available


def _get_neonize_error() -> str:
    """Return human-readable error about why neonize can't load."""
    _check_neonize()
    return _neonize_error or "neonize is not available"


class _WebSession:
    """Manages a neonize WhatsApp Web session in a background thread."""

    def __init__(self, db_path: str):
        self.db_path = db_path
        self._client = None
        self._thread: Optional[threading.Thread] = None
        self._inbound_cb: Optional[Callable] = None
        self._connected = False
        self._qr_data_url: Optional[str] = None
        self._qr_raw: Optional[bytes] = None
        self._error: Optional[str] = None
        self._lock = threading.Lock()
        self._linking = False
        self._link_started_at: float = 0.0
        self._owner_phone: Optional[str] = None

    @property
    def connected(self) -> bool:
        return self._connected

    @property
    def qr_data_url(self) -> Optional[str]:
        return self._qr_data_url

    @property
    def error(self) -> Optional[str]:
        return self._error

    @property
    def linking(self) -> bool:
        if self._linking and (time.time() - self._link_started_at > 180):
            self._linking = False
            self._error = "QR link timed out (3 min)"
        return self._linking

    def _on_qr(self, client, qr_data: bytes):
        """Intercept QR code from neonize and convert to base64 PNG."""
        try:
            import segno
            qr_img = segno.make_qr(qr_data)
            buf = io.BytesIO()
            qr_img.save(buf, kind="png", scale=8, border=2,
                        dark="#000000", light="#ffffff")
            b64 = base64.b64encode(buf.getvalue()).decode()
            with self._lock:
                self._qr_raw = qr_data
                self._qr_data_url = f"data:image/png;base64,{b64}"
                self._error = None
            log.info("WhatsApp Web QR code generated, waiting for scan...")
        except Exception as exc:
            log.error("Failed to render QR: %s", exc)
            with self._lock:
                self._error = f"QR render error: {exc}"

    @property
    def owner_phone(self) -> Optional[str]:
        return self._owner_phone

    def _on_connected(self, client, event):
        """Called when WhatsApp Web session is linked."""
        with self._lock:
            self._connected = True
            self._linking = False
            self._qr_data_url = None
            self._qr_raw = None
            self._error = None

        if not self._owner_phone:
            for c in (self._client, client):
                if not c:
                    continue
                try:
                    device = c.get_me()
                    if hasattr(device, 'JID') and hasattr(device.JID, 'User') and device.JID.User:
                        self._owner_phone = device.JID.User
                        log.info("WhatsApp Web linked as +%s", self._owner_phone)
                        return
                except Exception as exc:
                    log.debug("get_me() failed: %s", exc)

        if self._owner_phone:
            log.info("WhatsApp Web linked as +%s", self._owner_phone)
        else:
            log.info("WhatsApp Web linked (owner phone unknown)")

    def _is_self_chat(self, chat_jid) -> bool:
        """Check if a chat JID is the user's own self-chat (Note to Self)."""
        if not self._owner_phone:
            return False
        chat_user = chat_jid.User if hasattr(chat_jid, 'User') else str(chat_jid)
        return chat_user == self._owner_phone

    def _handle_inbound(self, client, event):
        """Handle inbound messages from WhatsApp Web.

        Filtering logic:
        - Skip @status and @broadcast JIDs
        - If IsFromMe and NOT self-chat → skip (echo from user's own typing)
        - If IsFromMe and IS self-chat → process (user talking to Quinely)
        - If NOT IsFromMe → process (someone else messaging the user)
        """
        if not self._inbound_cb:
            return
        try:
            from neonize.utils.jid import Jid2String

            source = event.Info.MessageSource
            chat_jid = source.Chat
            is_from_me = source.IsFromMe
            is_group = source.IsGroup

            chat_str = Jid2String(chat_jid)
            if "@broadcast" in chat_str or "@status" in chat_str:
                return

            self_chat = self._is_self_chat(chat_jid)

            if is_from_me and not self_chat:
                return

            text = ""
            msg = event.Message
            if msg.conversation:
                text = msg.conversation
            elif msg.extendedTextMessage and msg.extendedTextMessage.text:
                text = msg.extendedTextMessage.text
            if not text:
                return

            sender_jid = source.Sender
            sender_id = sender_jid.User if hasattr(sender_jid, 'User') else str(sender_jid)
            sender_name = event.Info.Pushname if hasattr(event.Info, 'Pushname') else ""
            if not sender_name:
                sender_name = event.Info.PushName if hasattr(event.Info, 'PushName') else sender_id

            reply_to = chat_str if is_group or self_chat else sender_id

            inbound = InboundMessage(
                channel_id="whatsapp",
                sender_id=sender_id,
                sender_name=sender_name or sender_id,
                text=text,
                timestamp=time.time(),
                thread_id=reply_to,
                raw={
                    "chat": chat_str,
                    "sender": Jid2String(sender_jid),
                    "is_from_me": is_from_me,
                    "is_group": is_group,
                    "self_chat": self_chat,
                },
            )
            self._inbound_cb(inbound)
        except Exception as exc:
            log.error("Error processing WhatsApp Web inbound: %s", exc)

    def start_link(self):
        """Begin the QR code linking process."""
        if self._connected:
            return
        with self._lock:
            self._linking = True
            self._link_started_at = time.time()
            self._qr_data_url = None
            self._error = None

        if self._thread and self._thread.is_alive():
            return

        self._thread = threading.Thread(target=self._run, daemon=True,
                                        name="ghost-wa-web")
        self._thread.start()

    def _run(self):
        """Background thread: create neonize client and connect."""
        try:
            from neonize.client import NewClient
            from neonize.events import ConnectedEv, MessageEv, PairStatusEv, event as neo_event

            WA_DATA_DIR.mkdir(parents=True, exist_ok=True)

            self._client = NewClient(self.db_path)

            @self._client.qr
            def qr_handler(client, qr_data: bytes):
                self._on_qr(client, qr_data)

            @self._client.event(ConnectedEv)
            def on_connected(client, ev):
                self._on_connected(client, ev)

            @self._client.event(MessageEv)
            def on_message(client, ev):
                self._handle_inbound(client, ev)

            @self._client.event(PairStatusEv)
            def on_pair(client, ev):
                if hasattr(ev, 'ID') and hasattr(ev.ID, 'User') and ev.ID.User:
                    self._owner_phone = ev.ID.User
                    log.info("WhatsApp Web paired as +%s", ev.ID.User)
                else:
                    log.info("WhatsApp Web paired (unknown number)")

            self._client.connect()
        except Exception as exc:
            log.error("WhatsApp Web session error: %s", exc)
            with self._lock:
                self._error = str(exc)
                self._linking = False

    def _resolve_jid(self, target: str):
        """Resolve a target to a JID. Handles phone numbers and group JIDs."""
        from neonize.utils import build_jid

        if "@g.us" in target:
            user_part = target.split("@")[0]
            return build_jid(user_part, server="g.us")

        to_clean = target.replace("+", "").replace("-", "").replace(" ", "")
        if "@" in to_clean:
            to_clean = to_clean.split("@")[0]
        jid = build_jid(to_clean)
        try:
            results = self._client.is_on_whatsapp([to_clean])
            for r in results:
                if r.IsIn and hasattr(r, 'JID'):
                    return r.JID
        except Exception as exc:
            log.debug("is_on_whatsapp lookup failed for %s: %s", to_clean, exc)
        return jid

    def send_text(self, to: str, text: str) -> OutboundResult:
        """Send a text message via the connected Web session."""
        if not self._connected or not self._client:
            return OutboundResult(ok=False, error="WhatsApp Web not connected",
                                 channel_id="whatsapp")
        try:
            from neonize.proto.waE2E.WAWebProtobufsE2E_pb2 import Message

            jid = self._resolve_jid(to)
            resp = self._client.send_message(jid, Message(conversation=text))
            msg_id = resp.ID if hasattr(resp, 'ID') else ""
            return OutboundResult(ok=True, message_id=str(msg_id),
                                 channel_id="whatsapp")
        except Exception as exc:
            return OutboundResult(ok=False, error=str(exc),
                                 channel_id="whatsapp")

    def send_media(self, to: str, media_path: str, caption: str = "") -> OutboundResult:
        """Send media via the connected Web session."""
        if not self._connected or not self._client:
            return OutboundResult(ok=False, error="WhatsApp Web not connected",
                                 channel_id="whatsapp")
        try:
            jid = self._resolve_jid(to)

            ext = Path(media_path).suffix.lower()
            if ext in (".jpg", ".jpeg", ".png", ".gif", ".webp"):
                self._client.send_image(jid, media_path, caption=caption)
            elif ext in (".mp4", ".mov", ".avi", ".webm"):
                self._client.send_video(jid, media_path, caption=caption)
            elif ext in (".mp3", ".ogg", ".wav", ".m4a"):
                self._client.send_audio(jid, media_path)
            else:
                self._client.send_document(jid, media_path, caption=caption,
                                            filename=Path(media_path).name)
            return OutboundResult(ok=True, channel_id="whatsapp")
        except Exception as exc:
            return OutboundResult(ok=False, error=str(exc), channel_id="whatsapp")

    def start_inbound(self, on_message: Callable):
        """Register the inbound message handler."""
        self._inbound_cb = on_message

    def disconnect(self):
        """Disconnect the session."""
        if self._client:
            try:
                self._client.disconnect()
            except Exception:
                pass
        self._connected = False

    def logout(self):
        """Logout and clear session data."""
        if self._client:
            try:
                self._client.logout()
            except Exception:
                pass
        self._connected = False
        self._qr_data_url = None
        self._linking = False


# ═══════════════════════════════════════════════════════════════
#  Main Provider
# ═══════════════════════════════════════════════════════════════

class Provider(ChannelProvider):

    meta = ChannelMeta(
        id="whatsapp",
        label="WhatsApp",
        emoji="\U0001f4f1",
        supports_media=True,
        supports_inbound=True,
        supports_groups=True,
        supports_gateway=True,
        text_chunk_limit=4096,
        delivery_mode=DeliveryMode.GATEWAY,
        docs_url="https://faq.whatsapp.com/1317564962315842",
    )

    def __init__(self):
        self.mode: str = "web"
        self.default_recipient: str = ""
        self._configured = False
        self._on_message: Optional[Callable] = None

        # Business API fields
        self.access_token: str = ""
        self.phone_number_id: str = ""
        self.verify_token: str = ""

        # Web session
        self._web: Optional[_WebSession] = None

    def _ensure_web_session(self) -> _WebSession:
        if self._web is None:
            db_path = str(WA_DATA_DIR / "session.db")
            self._web = _WebSession(db_path)
        return self._web

    # ── Configuration ─────────────────────────────────────────

    def configure(self, config: Dict[str, Any]) -> bool:
        self.mode = config.get("mode", "web")
        self.default_recipient = config.get("default_recipient", "")

        if self.mode == "business":
            self.access_token = config.get("access_token", "")
            self.phone_number_id = config.get("phone_number_id", "")
            self.verify_token = config.get("verify_token", "ghost_whatsapp_verify")
            self._configured = bool(self.access_token and self.phone_number_id)
        else:
            self._configured = True
            web = self._ensure_web_session()
            if not web.connected and not web.linking:
                db_path = str(WA_DATA_DIR / "session.db")
                if Path(db_path).exists() and Path(db_path).stat().st_size > 0:
                    web.start_link()

        return self._configured

    def get_config_schema(self) -> Dict[str, Any]:
        return {
            "mode": {
                "type": "select",
                "required": True,
                "default": "web",
                "options": ["web", "business"],
                "description": "Connection mode: 'web' = QR code scan (personal), 'business' = Cloud API",
            },
            "default_recipient": {
                "type": "string",
                "description": "Default recipient phone number with country code (e.g. +1234567890)",
            },
            "access_token": {
                "type": "string",
                "sensitive": True,
                "description": "Business API access token (business mode only)",
                "show_if": {"mode": "business"},
            },
            "phone_number_id": {
                "type": "string",
                "description": "Business phone number ID (business mode only)",
                "show_if": {"mode": "business"},
            },
            "verify_token": {
                "type": "string",
                "sensitive": True,
                "description": "Webhook verification token (business mode only)",
                "show_if": {"mode": "business"},
            },
        }

    # ── QR Link Flow (Web mode) ───────────────────────────────

    def start_qr_link(self) -> Dict[str, Any]:
        """Start the QR code linking process. Returns status dict."""
        if self.mode != "web":
            return {"ok": False, "error": "QR linking only available in web mode"}
        if not _check_neonize():
            return {"ok": False, "error": _get_neonize_error(),
                    "error_type": "missing_dependency"}

        web = self._ensure_web_session()
        if web.connected:
            return {"ok": True, "status": "already_connected",
                    "message": "WhatsApp is already linked."}

        web.start_link()

        for _ in range(30):
            time.sleep(0.5)
            if web.qr_data_url:
                return {"ok": True, "status": "qr_ready",
                        "qr_data_url": web.qr_data_url,
                        "message": "Scan this QR code with WhatsApp → Linked Devices"}
            if web.error:
                return {"ok": False, "status": "error", "error": web.error}

        return {"ok": False, "status": "timeout",
                "error": "Timed out waiting for QR code generation"}

    def get_link_status(self) -> Dict[str, Any]:
        """Check the current linking/connection status."""
        web = self._ensure_web_session()
        if web.connected:
            result = {"status": "connected", "connected": True}
            if web.owner_phone:
                result["owner_phone"] = f"+{web.owner_phone}"
            return result
        if web.error:
            return {"status": "error", "connected": False, "error": web.error}
        if web.linking:
            result = {"status": "linking", "connected": False}
            if web.qr_data_url:
                result["qr_data_url"] = web.qr_data_url
            return result
        return {"status": "idle", "connected": False}

    def logout_web(self) -> Dict[str, Any]:
        """Logout from WhatsApp Web and clear session."""
        if self._web:
            self._web.logout()
        self._configured = False
        return {"ok": True, "message": "Logged out from WhatsApp Web"}

    # ── Sending ───────────────────────────────────────────────

    def _resolve_recipient(self, to: str) -> str:
        """Resolve the recipient: explicit > config default > linked owner phone."""
        if to:
            return to
        if self.default_recipient:
            return self.default_recipient
        if self._web and self._web.owner_phone:
            return self._web.owner_phone
        return ""

    def send_text(self, to: str, text: str, **kwargs) -> OutboundResult:
        recipient = self._resolve_recipient(to)
        if not recipient:
            return OutboundResult(
                ok=False,
                error="No recipient — WhatsApp is not linked yet.",
                channel_id=self.meta.id,
            )

        if self.mode == "web":
            return self._send_text_web(recipient, text)
        return self._send_text_business(recipient, text)

    def _send_text_web(self, to: str, text: str) -> OutboundResult:
        web = self._ensure_web_session()
        if not web.connected:
            return OutboundResult(ok=False, error="WhatsApp Web not connected. Link via QR first.",
                                 channel_id=self.meta.id)
        return web.send_text(to, text)

    def _send_text_business(self, to: str, text: str) -> OutboundResult:
        url = f"{GRAPH_API}/{self.phone_number_id}/messages"
        headers = {
            "Authorization": f"Bearer {self.access_token}",
            "Content-Type": "application/json",
        }
        payload = {
            "messaging_product": "whatsapp",
            "to": to,
            "type": "text",
            "text": {"body": text},
        }
        try:
            resp = requests.post(url, json=payload, headers=headers, timeout=15)
            data = resp.json()
            if resp.status_code in (200, 201):
                msg_id = ""
                messages = data.get("messages", [])
                if messages:
                    msg_id = messages[0].get("id", "")
                return OutboundResult(ok=True, message_id=msg_id,
                                      channel_id=self.meta.id)
            error_msg = data.get("error", {}).get("message", resp.text[:200])
            return OutboundResult(ok=False, error=f"HTTP {resp.status_code}: {error_msg}",
                                  channel_id=self.meta.id)
        except Exception as exc:
            return OutboundResult(ok=False, error=str(exc), channel_id=self.meta.id)

    # ── Media ─────────────────────────────────────────────────

    def send_media(self, to: str, media_path: str, caption: str = "",
                   **kwargs) -> OutboundResult:
        recipient = self._resolve_recipient(to)
        if not recipient:
            return OutboundResult(
                ok=False,
                error="No recipient — WhatsApp is not linked yet.",
                channel_id=self.meta.id,
            )

        if self.mode == "web":
            web = self._ensure_web_session()
            if not web.connected:
                return OutboundResult(ok=False, error="WhatsApp Web not connected",
                                     channel_id=self.meta.id)
            return web.send_media(recipient, media_path, caption)

        return self._send_media_business(recipient, media_path, caption)

    def _send_media_business(self, to: str, media_path: str,
                             caption: str = "") -> OutboundResult:
        if not to:
            return OutboundResult(ok=False, error="No recipient phone number",
                                 channel_id=self.meta.id)
        upload_url = f"{GRAPH_API}/{self.phone_number_id}/media"
        headers = {"Authorization": f"Bearer {self.access_token}"}
        try:
            with open(media_path, "rb") as f:
                resp = requests.post(upload_url, headers=headers,
                                     files={"file": f},
                                     data={"messaging_product": "whatsapp"},
                                     timeout=30)
            if resp.status_code not in (200, 201):
                return OutboundResult(ok=False,
                                     error=f"Media upload failed: HTTP {resp.status_code}",
                                     channel_id=self.meta.id)
            media_id = resp.json().get("id", "")
            msg_url = f"{GRAPH_API}/{self.phone_number_id}/messages"
            payload = {
                "messaging_product": "whatsapp",
                "to": to,
                "type": "document",
                "document": {"id": media_id, "caption": caption},
            }
            resp2 = requests.post(msg_url, json=payload,
                                  headers={**headers, "Content-Type": "application/json"},
                                  timeout=15)
            if resp2.status_code in (200, 201):
                return OutboundResult(ok=True, channel_id=self.meta.id)
            return OutboundResult(ok=False, error=f"HTTP {resp2.status_code}",
                                 channel_id=self.meta.id)
        except Exception as exc:
            return OutboundResult(ok=False, error=str(exc), channel_id=self.meta.id)

    # ── Inbound ───────────────────────────────────────────────

    def start_inbound(self, on_message: Callable[[InboundMessage], None]) -> bool:
        self._on_message = on_message
        if self.mode == "web":
            web = self._ensure_web_session()
            web.start_inbound(on_message)
            return web.connected
        return True

    def stop_inbound(self):
        self._on_message = None
        if self.mode == "web" and self._web:
            self._web._inbound_cb = None
            try:
                self._web.disconnect()
            except Exception:
                pass

    # ── Business API webhooks ─────────────────────────────────

    def handle_webhook_verify(self, params: dict) -> Optional[str]:
        mode = params.get("hub.mode")
        token = params.get("hub.verify_token")
        challenge = params.get("hub.challenge")
        if mode == "subscribe" and token == self.verify_token:
            return challenge
        return None

    def handle_webhook_event(self, data: dict):
        if not self._on_message:
            return
        for entry in data.get("entry", []):
            for change in entry.get("changes", []):
                value = change.get("value", {})
                for message in value.get("messages", []):
                    text = ""
                    if message.get("type") == "text":
                        text = message.get("text", {}).get("body", "")
                    if not text:
                        continue
                    contact = {}
                    for c in value.get("contacts", []):
                        if c.get("wa_id") == message.get("from"):
                            contact = c
                            break
                    msg = InboundMessage(
                        channel_id="whatsapp",
                        sender_id=message.get("from", ""),
                        sender_name=contact.get("profile", {}).get("name",
                                    message.get("from", "unknown")),
                        text=text,
                        timestamp=float(message.get("timestamp", time.time())),
                        raw=message,
                    )
                    self._on_message(msg)

    # ── Health ────────────────────────────────────────────────

    def health_check(self) -> Dict[str, Any]:
        status: Dict[str, Any] = {
            "configured": self._configured,
            "mode": self.mode,
        }

        if self.mode == "web":
            if not _check_neonize():
                status["status"] = "missing dependency"
                status["setup_hint"] = _get_neonize_error()
                return status
            web = self._ensure_web_session()
            status["web_connected"] = web.connected
            status["web_linking"] = web.linking
            if web.connected:
                status["status"] = "connected"
            elif web.linking:
                status["status"] = "linking"
            elif self._configured:
                status["status"] = "ready"
            else:
                status["status"] = "not configured"
            if web.error:
                status["last_error"] = web.error
        else:
            status["phone_number_id"] = self.phone_number_id
            status["has_token"] = bool(self.access_token)
            if self._configured:
                try:
                    url = f"{GRAPH_API}/{self.phone_number_id}"
                    headers = {"Authorization": f"Bearer {self.access_token}"}
                    resp = requests.get(url, headers=headers, timeout=10)
                    if resp.status_code == 200:
                        data = resp.json()
                        status["phone_number"] = data.get("display_phone_number", "")
                        status["status"] = "connected"
                    else:
                        status["status"] = "error"
                        status["last_error"] = f"HTTP {resp.status_code}"
                except Exception as exc:
                    status["status"] = "error"
                    status["last_error"] = str(exc)
            else:
                status["status"] = "not configured"

        return status
