"""
Telegram Channel Provider

Outbound via Bot API (plain requests).  Inbound via getUpdates polling.
No external dependencies beyond `requests`.
"""

import json
import time
import threading
import logging
import tempfile
from pathlib import Path
from typing import Dict, Any, List, Callable, Optional

import requests

from ghost_channels import (
    ChannelProvider, ChannelMeta, DeliveryMode,
    OutboundResult, InboundMessage,
)
from ghost_channels.actions import ActionsMixin, ActionType, ActionResult, PollOption, PollResult
from ghost_channels.threading_ext import ThreadingMixin, ThreadMessage
from ghost_channels.streaming import StreamingMixin, StreamConfig
from ghost_channels.health import HealthMixin, HealthProbe, HealthAudit
from ghost_channels.security import SecurityMixin
from ghost_channels.onboard import OnboardingMixin, SetupStep, StepType, StepValidation
from ghost_channels.mentions import MentionMixin
from ghost_channels.directory import DirectoryMixin, DirectoryEntry

log = logging.getLogger("quinely.channels.telegram")

API_BASE = "https://api.telegram.org"


class Provider(ChannelProvider, ActionsMixin, ThreadingMixin, StreamingMixin,
               HealthMixin, SecurityMixin, OnboardingMixin, MentionMixin,
               DirectoryMixin):

    meta = ChannelMeta(
        id="telegram",
        label="Telegram",
        emoji="\U0001f4e8",
        supports_media=True,
        supports_threads=True,
        supports_reactions=True,
        supports_groups=True,
        supports_inbound=True,
        supports_edit=True,
        supports_unsend=True,
        supports_polls=True,
        supports_streaming=True,
        text_chunk_limit=4096,
        delivery_mode=DeliveryMode.DIRECT,
        docs_url="https://core.telegram.org/bots/api",
    )

    def __init__(self):
        self.bot_token: str = ""
        self.default_chat_id: str = ""
        self._configured = False
        self._stop_event = threading.Event()
        self._poll_thread: Optional[threading.Thread] = None
        self._last_update_id = 0

    def configure(self, config: Dict[str, Any]) -> bool:
        self.bot_token = config.get("bot_token", "")
        self.default_chat_id = str(config.get("default_chat_id", ""))
        self._configured = bool(self.bot_token)
        return self._configured

    def _api(self, method: str, **kwargs) -> dict:
        url = f"{API_BASE}/bot{self.bot_token}/{method}"
        resp = requests.post(url, json=kwargs, timeout=30)
        resp.raise_for_status()
        data = resp.json()
        if not data.get("ok"):
            raise RuntimeError(data.get("description", "Telegram API error"))
        return data.get("result", {})

    def _auto_detect_chat_id(self) -> str:
        """Discover chat_id from the most recent message sent to the bot."""
        if not self.bot_token:
            return ""
        try:
            url = f"{API_BASE}/bot{self.bot_token}/getUpdates"
            resp = requests.get(url, params={"limit": 5, "allowed_updates": ["message"]},
                                timeout=10)
            data = resp.json()
            if not data.get("ok"):
                return ""
            for update in reversed(data.get("result", [])):
                msg = update.get("message", {})
                cid = msg.get("chat", {}).get("id")
                if cid:
                    self.default_chat_id = str(cid)
                    log.info("Auto-detected Telegram chat_id: %s", self.default_chat_id)
                    try:
                        from ghost_channels import load_channels_config, save_channels_config
                        cfg = load_channels_config()
                        tg = cfg.get("telegram", {})
                        tg["default_chat_id"] = self.default_chat_id
                        cfg["telegram"] = tg
                        save_channels_config(cfg)
                    except Exception:
                        pass
                    return self.default_chat_id
        except Exception as exc:
            log.debug("Chat ID auto-detect failed: %s", exc)
        return ""

    def send_typing(self, to: str, **kwargs) -> bool:
        chat_id = to or self.default_chat_id
        if not chat_id:
            return False
        try:
            self._api("sendChatAction", chat_id=chat_id, action="typing")
            return True
        except Exception:
            return False

    def send_text(self, to: str, text: str, **kwargs) -> OutboundResult:
        chat_id = to or self.default_chat_id
        if not chat_id:
            chat_id = self._auto_detect_chat_id()
        if not chat_id:
            return OutboundResult(ok=False,
                                 error="No chat_id specified — send /start to the bot first",
                                 channel_id=self.meta.id)
        try:
            params: Dict[str, Any] = {
                "chat_id": chat_id,
                "text": text,
                "parse_mode": "Markdown",
            }
            reply_to = kwargs.get("reply_to_id")
            if reply_to:
                params["reply_to_message_id"] = reply_to
            result = self._api("sendMessage", **params)
            msg_id = str(result.get("message_id", ""))
            return OutboundResult(ok=True, message_id=msg_id,
                                  channel_id=self.meta.id)
        except Exception as exc:
            # Retry without Markdown if parsing fails
            if "parse" in str(exc).lower():
                try:
                    params["parse_mode"] = ""
                    result = self._api("sendMessage", **params)
                    msg_id = str(result.get("message_id", ""))
                    return OutboundResult(ok=True, message_id=msg_id,
                                          channel_id=self.meta.id)
                except Exception as exc2:
                    return OutboundResult(ok=False, error=str(exc2),
                                          channel_id=self.meta.id)
            return OutboundResult(ok=False, error=str(exc),
                                  channel_id=self.meta.id)

    def send_media(self, to: str, media_path: str, caption: str = "",
                   **kwargs) -> OutboundResult:
        chat_id = to or self.default_chat_id
        if not chat_id:
            chat_id = self._auto_detect_chat_id()
        if not chat_id:
            return OutboundResult(ok=False,
                                 error="No chat_id specified — send /start to the bot first",
                                 channel_id=self.meta.id)
        try:
            url = f"{API_BASE}/bot{self.bot_token}/sendDocument"
            with open(media_path, "rb") as f:
                files = {"document": f}
                data = {"chat_id": chat_id}
                if caption:
                    data["caption"] = caption
                resp = requests.post(url, data=data, files=files, timeout=60)
            resp.raise_for_status()
            body = resp.json()
            if body.get("ok"):
                return OutboundResult(ok=True,
                                      message_id=str(body["result"].get("message_id", "")),
                                      channel_id=self.meta.id)
            return OutboundResult(ok=False, error=body.get("description", ""),
                                  channel_id=self.meta.id)
        except Exception as exc:
            return OutboundResult(ok=False, error=str(exc),
                                  channel_id=self.meta.id)

    def start_inbound(self, on_message: Callable[[InboundMessage], None]) -> bool:
        if not self._configured:
            return False
        self._stop_event.clear()
        self._poll_thread = threading.Thread(
            target=self._poll_updates, args=(on_message,),
            daemon=True, name="telegram-inbound",
        )
        self._poll_thread.start()
        return True

    def stop_inbound(self):
        self._stop_event.set()
        if self._poll_thread:
            self._poll_thread.join(timeout=5)
            self._poll_thread = None

    def _download_tg_file(self, file_id: str, suffix: str = "") -> str:
        """Download a Telegram file by file_id, return local path."""
        resp = self._api("getFile", file_id=file_id)
        file_path = resp.get("file_path", "")
        if not file_path:
            return ""
        url = f"{API_BASE}/file/bot{self.bot_token}/{file_path}"
        dl = requests.get(url, timeout=60)
        dl.raise_for_status()
        if not suffix:
            suffix = Path(file_path).suffix or ".bin"
        media_dir = Path.home() / ".ghost" / "inbound_media"
        media_dir.mkdir(parents=True, exist_ok=True)
        dest = media_dir / f"tg_{int(time.time())}_{file_id[:8]}{suffix}"
        dest.write_bytes(dl.content)
        return str(dest)

    def _poll_updates(self, on_message: Callable[[InboundMessage], None]):
        """Long-poll Telegram getUpdates and relay messages."""
        while not self._stop_event.is_set():
            try:
                url = f"{API_BASE}/bot{self.bot_token}/getUpdates"
                params: Dict[str, Any] = {"timeout": 30, "allowed_updates": ["message"]}
                if self._last_update_id:
                    params["offset"] = self._last_update_id + 1
                resp = requests.get(url, params=params, timeout=35)
                data = resp.json()
                if not data.get("ok"):
                    time.sleep(5)
                    continue
                for update in data.get("result", []):
                    self._last_update_id = max(self._last_update_id,
                                               update.get("update_id", 0))
                    message = update.get("message", {})
                    text = message.get("text", "") or message.get("caption", "")
                    media_urls: List[str] = []

                    # Photos — pick the largest resolution
                    photos = message.get("photo")
                    if photos:
                        best = max(photos, key=lambda p: p.get("file_size", 0))
                        try:
                            local = self._download_tg_file(best["file_id"], ".jpg")
                            if local:
                                media_urls.append(local)
                        except Exception as exc:
                            log.debug("Telegram photo download failed: %s", exc)

                    # Documents (PDF, text, code files, etc.)
                    doc = message.get("document")
                    if doc:
                        try:
                            fname = doc.get("file_name", "")
                            suffix = Path(fname).suffix if fname else ".bin"
                            local = self._download_tg_file(doc["file_id"], suffix)
                            if local:
                                media_urls.append(local)
                        except Exception as exc:
                            log.debug("Telegram document download failed: %s", exc)

                    # Voice / audio
                    voice = message.get("voice") or message.get("audio")
                    if voice:
                        try:
                            local = self._download_tg_file(voice["file_id"], ".ogg")
                            if local:
                                media_urls.append(local)
                        except Exception as exc:
                            log.debug("Telegram audio download failed: %s", exc)

                    # Video
                    video = message.get("video")
                    if video:
                        try:
                            local = self._download_tg_file(video["file_id"], ".mp4")
                            if local:
                                media_urls.append(local)
                        except Exception as exc:
                            log.debug("Telegram video download failed: %s", exc)

                    if not text and not media_urls:
                        continue

                    sender = message.get("from", {})
                    msg = InboundMessage(
                        channel_id="telegram",
                        sender_id=str(sender.get("id", "")),
                        sender_name=(sender.get("first_name", "") + " " +
                                     sender.get("last_name", "")).strip() or
                                    sender.get("username", "unknown"),
                        text=text,
                        thread_id=str(message.get("chat", {}).get("id", "")),
                        reply_to_id=str(message.get("reply_to_message", {}).get(
                            "message_id", "")) if message.get("reply_to_message") else None,
                        media_urls=media_urls,
                        timestamp=message.get("date", time.time()),
                        raw=update,
                    )
                    on_message(msg)
            except Exception as exc:
                if not self._stop_event.is_set():
                    log.debug("Telegram polling error, retrying: %s", exc)
                    time.sleep(5)

    def health_check(self) -> Dict[str, Any]:
        status: Dict[str, Any] = {
            "configured": self._configured,
            "has_token": bool(self.bot_token),
            "default_chat_id": self.default_chat_id,
        }
        if self._configured:
            try:
                me = self._api("getMe")
                status["bot_username"] = me.get("username", "")
                status["status"] = "connected"
            except Exception as exc:
                status["status"] = "error"
                status["last_error"] = str(exc)
        else:
            status["status"] = "not configured"
        return status

    def get_config_schema(self) -> Dict[str, Any]:
        return {
            "bot_token": {"type": "string", "required": True, "sensitive": True,
                          "description": "Telegram Bot API token from @BotFather"},
            "default_chat_id": {"type": "string",
                                "description": "Default chat ID for outbound messages"},
        }

    # ── Phase 2: Actions ─────────────────────────────────────

    def supported_actions(self) -> List:
        return [ActionType.REACT, ActionType.EDIT, ActionType.UNSEND,
                ActionType.PIN, ActionType.POLL]

    def react(self, message_id: str, emoji: str, to: str = "",
              **kwargs) -> ActionResult:
        chat_id = to or self.default_chat_id
        if not chat_id:
            return ActionResult(ok=False, action="react", error="No chat_id",
                                channel_id=self.meta.id)
        try:
            self._api("setMessageReaction", chat_id=chat_id,
                       message_id=int(message_id),
                       reaction=[{"type": "emoji", "emoji": emoji}])
            return ActionResult(ok=True, action="react", message_id=message_id,
                                channel_id=self.meta.id)
        except Exception as exc:
            return ActionResult(ok=False, action="react", error=str(exc),
                                channel_id=self.meta.id)

    def edit_message(self, message_id: str, new_text: str, to: str = "",
                     **kwargs) -> ActionResult:
        chat_id = to or self.default_chat_id
        if not chat_id:
            return ActionResult(ok=False, action="edit", error="No chat_id",
                                channel_id=self.meta.id)
        try:
            self._api("editMessageText", chat_id=chat_id,
                       message_id=int(message_id), text=new_text)
            return ActionResult(ok=True, action="edit", message_id=message_id,
                                channel_id=self.meta.id)
        except Exception as exc:
            return ActionResult(ok=False, action="edit", error=str(exc),
                                channel_id=self.meta.id)

    def unsend(self, message_id: str, to: str = "",
               **kwargs) -> ActionResult:
        chat_id = to or self.default_chat_id
        if not chat_id:
            return ActionResult(ok=False, action="unsend", error="No chat_id",
                                channel_id=self.meta.id)
        try:
            self._api("deleteMessage", chat_id=chat_id,
                       message_id=int(message_id))
            return ActionResult(ok=True, action="unsend", message_id=message_id,
                                channel_id=self.meta.id)
        except Exception as exc:
            return ActionResult(ok=False, action="unsend", error=str(exc),
                                channel_id=self.meta.id)

    def pin_message(self, message_id: str, to: str = "",
                    **kwargs) -> ActionResult:
        chat_id = to or self.default_chat_id
        try:
            self._api("pinChatMessage", chat_id=chat_id,
                       message_id=int(message_id))
            return ActionResult(ok=True, action="pin", channel_id=self.meta.id)
        except Exception as exc:
            return ActionResult(ok=False, action="pin", error=str(exc),
                                channel_id=self.meta.id)

    def create_poll(self, question: str, options: List[PollOption],
                    to: str = "", **kwargs) -> PollResult:
        chat_id = to or self.default_chat_id
        if not chat_id:
            return PollResult(ok=False, error="No chat_id", channel_id=self.meta.id)
        try:
            result = self._api("sendPoll", chat_id=chat_id, question=question,
                                options=[o.text for o in options],
                                is_anonymous=kwargs.get("anonymous", True))
            msg_id = str(result.get("message_id", ""))
            poll_id = str(result.get("poll", {}).get("id", ""))
            return PollResult(ok=True, poll_id=poll_id, message_id=msg_id,
                              channel_id=self.meta.id)
        except Exception as exc:
            return PollResult(ok=False, error=str(exc), channel_id=self.meta.id)

    # ── Phase 2: Streaming ───────────────────────────────────

    def supports_streaming(self) -> bool:
        return True

    def block_streaming_coalesce_defaults(self) -> StreamConfig:
        return StreamConfig(min_chars=60, idle_ms=600, max_edits_per_second=1.5)

    def edit_message_text(self, message_id: str, new_text: str,
                           to: str = "", **kwargs) -> bool:
        chat_id = to or self.default_chat_id
        try:
            self._api("editMessageText", chat_id=chat_id,
                       message_id=int(message_id), text=new_text)
            return True
        except Exception:
            return False

    def send_placeholder(self, to: str, placeholder: str = "...",
                          **kwargs) -> Optional[str]:
        chat_id = to or self.default_chat_id
        if not chat_id:
            return None
        try:
            result = self._api("sendMessage", chat_id=chat_id, text=placeholder)
            return str(result.get("message_id", ""))
        except Exception:
            return None

    # ── Phase 2: Onboarding ──────────────────────────────────

    def get_setup_steps(self) -> List[SetupStep]:
        return [
            SetupStep(
                id="bot_token", label="Bot Token",
                description="Create a bot via @BotFather and paste the token here",
                step_type=StepType.SECRET_INPUT, required=True,
                config_key="bot_token",
                help_url="https://core.telegram.org/bots#how-do-i-create-a-bot",
                validation_regex=r'^\d+:.+$',
                validation_message="Token should be like 123456:ABC-DEF...",
            ),
            SetupStep(
                id="default_chat_id", label="Default Chat ID",
                description="Chat ID for outbound messages (use /start then check getUpdates)",
                step_type=StepType.TEXT_INPUT, required=False,
                config_key="default_chat_id",
            ),
        ]

    def validate_step(self, step_id: str, user_input: str) -> StepValidation:
        if step_id == "bot_token" and user_input:
            try:
                url = f"{API_BASE}/bot{user_input}/getMe"
                resp = requests.get(url, timeout=10)
                data = resp.json()
                if data.get("ok"):
                    name = data["result"].get("username", "")
                    return StepValidation(ok=True,
                                          message=f"Valid! Bot: @{name}")
                return StepValidation(ok=False,
                                      message=data.get("description", "Invalid token"))
            except Exception as exc:
                return StepValidation(ok=False, message=f"Connection error: {exc}")
        return super().validate_step(step_id, user_input)
