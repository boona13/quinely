"""
Discord Channel Provider

Two modes:
  1. Webhook mode (outbound only): Just needs a webhook URL.  Zero dependencies.
  2. Bot mode (bidirectional): Uses `discord.py` if installed for gateway inbound.

Outbound always uses raw HTTP (no SDK needed for sending).
"""

import time
import threading
import logging
from pathlib import Path
from typing import Dict, Any, Callable, Optional, List

import requests

from ghost_channels import (
    ChannelProvider, ChannelMeta, DeliveryMode,
    OutboundResult, InboundMessage,
)
from ghost_channels.actions import ActionsMixin, ActionType, ActionResult
from ghost_channels.streaming import StreamingMixin, StreamConfig
from ghost_channels.health import HealthMixin
from ghost_channels.security import SecurityMixin
from ghost_channels.onboard import OnboardingMixin, SetupStep, StepType, StepValidation
from ghost_channels.mentions import MentionMixin

log = logging.getLogger("quinely.channels.discord")

DISCORD_API = "https://discord.com/api/v10"


class Provider(ChannelProvider, ActionsMixin, StreamingMixin,
               HealthMixin, SecurityMixin, OnboardingMixin, MentionMixin):

    meta = ChannelMeta(
        id="discord",
        label="Discord",
        emoji="\U0001f3ae",
        supports_media=True,
        supports_threads=True,
        supports_reactions=True,
        supports_groups=True,
        supports_inbound=True,
        supports_edit=True,
        supports_unsend=True,
        supports_streaming=True,
        text_chunk_limit=2000,
        delivery_mode=DeliveryMode.DIRECT,
        docs_url="https://discord.com/developers/docs",
    )

    def __init__(self):
        self.bot_token: str = ""
        self.webhook_url: str = ""
        self.default_channel_id: str = ""
        self._configured = False
        self._stop_event = threading.Event()
        self._bot_thread: Optional[threading.Thread] = None

    def configure(self, config: Dict[str, Any]) -> bool:
        self.bot_token = config.get("bot_token", "")
        self.webhook_url = config.get("webhook_url", "")
        self.default_channel_id = config.get("default_channel_id", "")
        self._configured = bool(self.bot_token or self.webhook_url)
        return self._configured

    def send_typing(self, to: str, **kwargs) -> bool:
        channel_id = to or self.default_channel_id
        if not self.bot_token or not channel_id:
            return False
        try:
            url = f"{DISCORD_API}/channels/{channel_id}/typing"
            headers = {"Authorization": f"Bot {self.bot_token}"}
            resp = requests.post(url, headers=headers, timeout=5)
            return resp.status_code == 204
        except Exception:
            return False

    def send_text(self, to: str, text: str, **kwargs) -> OutboundResult:
        channel_id = to or self.default_channel_id

        if self.webhook_url and not channel_id:
            return self._send_webhook(text)

        if self.bot_token and channel_id:
            return self._send_bot(channel_id, text, **kwargs)

        if self.webhook_url:
            return self._send_webhook(text)

        return OutboundResult(ok=False, error="No bot_token or webhook_url configured",
                             channel_id=self.meta.id)

    def _send_bot(self, channel_id: str, text: str, **kwargs) -> OutboundResult:
        url = f"{DISCORD_API}/channels/{channel_id}/messages"
        headers = {"Authorization": f"Bot {self.bot_token}",
                   "Content-Type": "application/json"}
        payload: Dict[str, Any] = {"content": text}
        ref = kwargs.get("reply_to_id")
        if ref:
            payload["message_reference"] = {"message_id": ref}
        try:
            resp = requests.post(url, json=payload, headers=headers, timeout=15)
            if resp.status_code in (200, 201):
                data = resp.json()
                return OutboundResult(ok=True, message_id=data.get("id", ""),
                                      channel_id=self.meta.id)
            return OutboundResult(ok=False,
                                  error=f"HTTP {resp.status_code}: {resp.text[:200]}",
                                  channel_id=self.meta.id)
        except Exception as exc:
            return OutboundResult(ok=False, error=str(exc), channel_id=self.meta.id)

    def _send_webhook(self, text: str) -> OutboundResult:
        try:
            resp = requests.post(self.webhook_url,
                                 json={"content": text, "username": "Quinely"},
                                 timeout=15)
            if resp.status_code in (200, 204):
                return OutboundResult(ok=True, channel_id=self.meta.id)
            return OutboundResult(ok=False, error=f"HTTP {resp.status_code}",
                                 channel_id=self.meta.id)
        except Exception as exc:
            return OutboundResult(ok=False, error=str(exc), channel_id=self.meta.id)

    def send_media(self, to: str, media_path: str, caption: str = "",
                   **kwargs) -> OutboundResult:
        channel_id = to or self.default_channel_id
        if self.webhook_url and not channel_id:
            try:
                with open(media_path, "rb") as f:
                    resp = requests.post(
                        self.webhook_url,
                        data={"content": caption, "username": "Quinely"},
                        files={"file": f},
                        timeout=60,
                    )
                if resp.status_code in (200, 204):
                    return OutboundResult(ok=True, channel_id=self.meta.id)
                return OutboundResult(ok=False, error=f"HTTP {resp.status_code}",
                                     channel_id=self.meta.id)
            except Exception as exc:
                return OutboundResult(ok=False, error=str(exc), channel_id=self.meta.id)

        if not self.bot_token or not channel_id:
            return OutboundResult(ok=False, error="Need bot_token + channel_id for media",
                                 channel_id=self.meta.id)
        url = f"{DISCORD_API}/channels/{channel_id}/messages"
        headers = {"Authorization": f"Bot {self.bot_token}"}
        try:
            with open(media_path, "rb") as f:
                resp = requests.post(
                    url, headers=headers,
                    data={"content": caption} if caption else None,
                    files={"file": f},
                    timeout=60,
                )
            if resp.status_code in (200, 201):
                return OutboundResult(ok=True, message_id=resp.json().get("id", ""),
                                      channel_id=self.meta.id)
            return OutboundResult(ok=False, error=f"HTTP {resp.status_code}",
                                 channel_id=self.meta.id)
        except Exception as exc:
            return OutboundResult(ok=False, error=str(exc), channel_id=self.meta.id)

    def start_inbound(self, on_message: Callable[[InboundMessage], None]) -> bool:
        if not self.bot_token:
            return False
        try:
            import discord
        except ImportError:
            log.info("discord.py not installed; Discord inbound disabled. "
                     "pip install discord.py")
            return False

        self._stop_event.clear()
        intents = discord.Intents.default()
        intents.message_content = True
        client = discord.Client(intents=intents)
        self._discord_client = client
        _inbound_callback = on_message

        @client.event
        async def on_message(message):
            if message.author == client.user or message.author.bot:
                return

            media_urls: List[str] = []
            for att in message.attachments:
                try:
                    media_dir = Path.home() / ".ghost" / "inbound_media"
                    media_dir.mkdir(parents=True, exist_ok=True)
                    suffix = Path(att.filename).suffix if att.filename else ".bin"
                    dest = media_dir / f"dc_{int(time.time())}_{att.id}{suffix}"
                    await att.save(dest)
                    media_urls.append(str(dest))
                except Exception as exc:
                    log.debug("Discord attachment download failed: %s", exc)

            text = message.content
            if not text and not media_urls:
                return

            msg = InboundMessage(
                channel_id="discord",
                sender_id=str(message.author.id),
                sender_name=str(message.author),
                text=text,
                thread_id=str(message.channel.id),
                reply_to_id=str(message.reference.message_id) if message.reference else None,
                media_urls=media_urls,
                timestamp=message.created_at.timestamp(),
                raw={"guild_id": str(message.guild.id) if message.guild else ""},
            )
            import asyncio
            await asyncio.to_thread(_inbound_callback, msg)

        def _run():
            try:
                client.run(self.bot_token, log_handler=None)
            except Exception as exc:
                if not self._stop_event.is_set():
                    log.error("Discord bot error: %s", exc)

        self._bot_thread = threading.Thread(
            target=_run, daemon=True, name="discord-gateway",
        )
        self._bot_thread.start()
        return True

    def stop_inbound(self):
        self._stop_event.set()
        if hasattr(self, "_discord_client"):
            import asyncio
            try:
                loop = self._discord_client.loop
                if loop and loop.is_running():
                    asyncio.run_coroutine_threadsafe(self._discord_client.close(), loop)
            except Exception:
                pass
        if self._bot_thread:
            self._bot_thread.join(timeout=5)
            self._bot_thread = None

    def health_check(self) -> Dict[str, Any]:
        status: Dict[str, Any] = {
            "configured": self._configured,
            "has_bot_token": bool(self.bot_token),
            "has_webhook": bool(self.webhook_url),
            "default_channel_id": self.default_channel_id,
        }
        if self.bot_token:
            try:
                headers = {"Authorization": f"Bot {self.bot_token}"}
                resp = requests.get(f"{DISCORD_API}/users/@me", headers=headers,
                                    timeout=10)
                if resp.status_code == 200:
                    data = resp.json()
                    status["bot_username"] = data.get("username", "")
                    status["status"] = "connected"
                else:
                    status["status"] = "error"
                    status["last_error"] = f"HTTP {resp.status_code}"
            except Exception as exc:
                status["status"] = "error"
                status["last_error"] = str(exc)
        elif self.webhook_url:
            status["status"] = "webhook-only"
        else:
            status["status"] = "not configured"
        return status

    def get_config_schema(self) -> Dict[str, Any]:
        return {
            "bot_token": {"type": "string", "sensitive": True,
                          "description": "Discord Bot token"},
            "webhook_url": {"type": "string", "sensitive": True,
                            "description": "Discord Webhook URL (outbound only)"},
            "default_channel_id": {"type": "string",
                                   "description": "Default channel ID for bot messages"},
        }

    # ── Phase 2: Actions ─────────────────────────────────────

    def supported_actions(self):
        return [ActionType.REACT, ActionType.EDIT, ActionType.UNSEND, ActionType.PIN]

    def react(self, message_id: str, emoji: str, to: str = "",
              **kwargs) -> ActionResult:
        if not self.bot_token:
            return ActionResult(ok=False, action="react", error="No bot_token",
                                channel_id=self.meta.id)
        channel_id = to or self.default_channel_id
        if not channel_id:
            return ActionResult(ok=False, action="react", error="No channel_id",
                                channel_id=self.meta.id)
        try:
            url = f"{DISCORD_API}/channels/{channel_id}/messages/{message_id}/reactions/{emoji}/@me"
            headers = {"Authorization": f"Bot {self.bot_token}"}
            resp = requests.put(url, headers=headers, timeout=10)
            if resp.status_code in (200, 204):
                return ActionResult(ok=True, action="react", message_id=message_id,
                                    channel_id=self.meta.id)
            return ActionResult(ok=False, action="react",
                                error=f"HTTP {resp.status_code}",
                                channel_id=self.meta.id)
        except Exception as exc:
            return ActionResult(ok=False, action="react", error=str(exc),
                                channel_id=self.meta.id)

    def edit_message(self, message_id: str, new_text: str, to: str = "",
                     **kwargs) -> ActionResult:
        if not self.bot_token:
            return ActionResult(ok=False, action="edit", error="No bot_token",
                                channel_id=self.meta.id)
        channel_id = to or self.default_channel_id
        try:
            url = f"{DISCORD_API}/channels/{channel_id}/messages/{message_id}"
            headers = {"Authorization": f"Bot {self.bot_token}",
                       "Content-Type": "application/json"}
            resp = requests.patch(url, json={"content": new_text},
                                  headers=headers, timeout=10)
            if resp.status_code == 200:
                return ActionResult(ok=True, action="edit", message_id=message_id,
                                    channel_id=self.meta.id)
            return ActionResult(ok=False, action="edit",
                                error=f"HTTP {resp.status_code}",
                                channel_id=self.meta.id)
        except Exception as exc:
            return ActionResult(ok=False, action="edit", error=str(exc),
                                channel_id=self.meta.id)

    def unsend(self, message_id: str, to: str = "",
               **kwargs) -> ActionResult:
        if not self.bot_token:
            return ActionResult(ok=False, action="unsend", error="No bot_token",
                                channel_id=self.meta.id)
        channel_id = to or self.default_channel_id
        try:
            url = f"{DISCORD_API}/channels/{channel_id}/messages/{message_id}"
            headers = {"Authorization": f"Bot {self.bot_token}"}
            resp = requests.delete(url, headers=headers, timeout=10)
            if resp.status_code in (200, 204):
                return ActionResult(ok=True, action="unsend", message_id=message_id,
                                    channel_id=self.meta.id)
            return ActionResult(ok=False, action="unsend",
                                error=f"HTTP {resp.status_code}",
                                channel_id=self.meta.id)
        except Exception as exc:
            return ActionResult(ok=False, action="unsend", error=str(exc),
                                channel_id=self.meta.id)

    # ── Phase 2: Streaming ───────────────────────────────────

    def supports_streaming(self) -> bool:
        return bool(self.bot_token)

    def edit_message_text(self, message_id: str, new_text: str,
                           to: str = "", **kwargs) -> bool:
        channel_id = to or self.default_channel_id
        if not self.bot_token or not channel_id:
            return False
        try:
            url = f"{DISCORD_API}/channels/{channel_id}/messages/{message_id}"
            headers = {"Authorization": f"Bot {self.bot_token}",
                       "Content-Type": "application/json"}
            resp = requests.patch(url, json={"content": new_text},
                                  headers=headers, timeout=10)
            return resp.status_code == 200
        except Exception:
            return False

    def send_placeholder(self, to: str, placeholder: str = "...",
                          **kwargs) -> Optional[str]:
        result = self.send_text(to, placeholder)
        return result.message_id if result.ok else None

    # ── Phase 2: Onboarding ──────────────────────────────────

    def get_setup_steps(self):
        return [
            SetupStep(
                id="bot_token", label="Bot Token",
                description="Discord Bot token from the Developer Portal",
                step_type=StepType.SECRET_INPUT, required=True,
                config_key="bot_token",
                help_url="https://discord.com/developers/docs/getting-started",
            ),
            SetupStep(
                id="default_channel_id", label="Default Channel ID",
                description="Right-click a channel and Copy ID (enable Developer Mode)",
                step_type=StepType.TEXT_INPUT, required=False,
                config_key="default_channel_id",
            ),
        ]

    def validate_step(self, step_id: str, user_input: str) -> StepValidation:
        if step_id == "bot_token" and user_input:
            try:
                headers = {"Authorization": f"Bot {user_input}"}
                resp = requests.get(f"{DISCORD_API}/users/@me", headers=headers,
                                    timeout=10)
                if resp.status_code == 200:
                    name = resp.json().get("username", "")
                    return StepValidation(ok=True,
                                          message=f"Valid! Bot: {name}")
                return StepValidation(ok=False,
                                      message=f"Invalid token (HTTP {resp.status_code})")
            except Exception as exc:
                return StepValidation(ok=False, message=f"Connection error: {exc}")
        return super().validate_step(step_id, user_input)
