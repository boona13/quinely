"""
Message Actions Adapter

  - Standard actions: react, edit, unsend/delete, poll, pin
  - Capability checking per provider
  - LLM tool definitions for Quinely to invoke actions
  - Per-provider implementation via mixin pattern

Usage:
    if isinstance(provider, ActionsMixin):
        provider.react(message_id, "👍", to=chat_id)
        provider.edit_message(message_id, "updated text", to=chat_id)
        provider.unsend(message_id, to=chat_id)
"""

import logging
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from enum import Enum
from typing import Optional, Dict, Any, List

log = logging.getLogger("ghost.channels.actions")


class ActionType(Enum):
    REACT = "react"
    EDIT = "edit"
    UNSEND = "unsend"
    PIN = "pin"
    UNPIN = "unpin"
    POLL = "poll"


@dataclass
class ActionResult:
    ok: bool
    action: str
    channel_id: str = ""
    message_id: str = ""
    error: str = ""
    data: Dict[str, Any] = field(default_factory=dict)


@dataclass
class PollOption:
    text: str
    id: str = ""


@dataclass
class PollResult:
    ok: bool
    poll_id: str = ""
    message_id: str = ""
    channel_id: str = ""
    error: str = ""


class ActionsMixin(ABC):
    """Mixin for ChannelProvider subclasses that support message actions.

    Providers implement only the actions they support and override
    `supported_actions` accordingly.
    """

    def supported_actions(self) -> List[ActionType]:
        """Return list of supported actions for this channel."""
        return []

    def supports_action(self, action: ActionType) -> bool:
        return action in self.supported_actions()

    def react(self, message_id: str, emoji: str, to: str = "",
              **kwargs) -> ActionResult:
        return ActionResult(ok=False, action="react",
                            error="react not supported by this channel")

    def edit_message(self, message_id: str, new_text: str, to: str = "",
                     **kwargs) -> ActionResult:
        return ActionResult(ok=False, action="edit",
                            error="edit not supported by this channel")

    def unsend(self, message_id: str, to: str = "",
               **kwargs) -> ActionResult:
        return ActionResult(ok=False, action="unsend",
                            error="unsend not supported by this channel")

    def pin_message(self, message_id: str, to: str = "",
                    **kwargs) -> ActionResult:
        return ActionResult(ok=False, action="pin",
                            error="pin not supported by this channel")

    def unpin_message(self, message_id: str, to: str = "",
                      **kwargs) -> ActionResult:
        return ActionResult(ok=False, action="unpin",
                            error="unpin not supported by this channel")

    def create_poll(self, question: str, options: List[PollOption],
                    to: str = "", **kwargs) -> PollResult:
        return PollResult(ok=False, error="polls not supported by this channel")


def build_action_tools(router, registry) -> list:
    """Build LLM tools for message actions."""
    import json
    tools = []

    def channel_react(channel: str, message_id: str, emoji: str,
                      to: str = "") -> str:
        prov = registry.get(channel)
        if not prov:
            return f"Unknown channel: {channel}"
        if not isinstance(prov, ActionsMixin):
            return f"{channel} does not support message actions"
        if not prov.supports_action(ActionType.REACT):
            return f"{channel} does not support reactions"
        result = prov.react(message_id, emoji, to=to)
        return f"OK: reacted with {emoji}" if result.ok else f"FAILED: {result.error}"

    tools.append({
        "name": "channel_react",
        "description": "Add a reaction emoji to a message on a messaging channel.",
        "parameters": {
            "type": "object",
            "properties": {
                "channel": {"type": "string",
                            "description": "Channel ID (e.g. 'slack', 'discord')"},
                "message_id": {"type": "string",
                               "description": "ID of the message to react to"},
                "emoji": {"type": "string",
                          "description": "Emoji to react with (e.g. '👍', 'thumbsup')"},
                "to": {"type": "string",
                       "description": "Chat/channel ID where the message is",
                       "default": ""},
            },
            "required": ["channel", "message_id", "emoji"],
        },
        "execute": channel_react,
    })

    def channel_edit(channel: str, message_id: str, new_text: str,
                     to: str = "") -> str:
        prov = registry.get(channel)
        if not prov:
            return f"Unknown channel: {channel}"
        if not isinstance(prov, ActionsMixin):
            return f"{channel} does not support message actions"
        if not prov.supports_action(ActionType.EDIT):
            return f"{channel} does not support editing"
        result = prov.edit_message(message_id, new_text, to=to)
        return "OK: message edited" if result.ok else f"FAILED: {result.error}"

    tools.append({
        "name": "channel_edit",
        "description": "Edit a previously sent message on a messaging channel.",
        "parameters": {
            "type": "object",
            "properties": {
                "channel": {"type": "string",
                            "description": "Channel ID"},
                "message_id": {"type": "string",
                               "description": "ID of the message to edit"},
                "new_text": {"type": "string",
                             "description": "New message text"},
                "to": {"type": "string",
                       "description": "Chat/channel ID",
                       "default": ""},
            },
            "required": ["channel", "message_id", "new_text"],
        },
        "execute": channel_edit,
    })

    def channel_unsend(channel: str, message_id: str, to: str = "") -> str:
        prov = registry.get(channel)
        if not prov:
            return f"Unknown channel: {channel}"
        if not isinstance(prov, ActionsMixin):
            return f"{channel} does not support message actions"
        if not prov.supports_action(ActionType.UNSEND):
            return f"{channel} does not support unsend/delete"
        result = prov.unsend(message_id, to=to)
        return "OK: message deleted" if result.ok else f"FAILED: {result.error}"

    tools.append({
        "name": "channel_unsend",
        "description": "Delete/unsend a previously sent message on a messaging channel.",
        "parameters": {
            "type": "object",
            "properties": {
                "channel": {"type": "string",
                            "description": "Channel ID"},
                "message_id": {"type": "string",
                               "description": "ID of the message to delete"},
                "to": {"type": "string",
                       "description": "Chat/channel ID",
                       "default": ""},
            },
            "required": ["channel", "message_id"],
        },
        "execute": channel_unsend,
    })

    def channel_poll(channel: str, question: str, options: str,
                     to: str = "") -> str:
        prov = registry.get(channel)
        if not prov:
            return f"Unknown channel: {channel}"
        if not isinstance(prov, ActionsMixin):
            return f"{channel} does not support message actions"
        if not prov.supports_action(ActionType.POLL):
            return f"{channel} does not support polls"
        opts = [PollOption(text=o.strip()) for o in options.split(",") if o.strip()]
        if len(opts) < 2:
            return "Need at least 2 options (comma-separated)"
        result = prov.create_poll(question, opts, to=to)
        return f"OK: poll created (id={result.poll_id})" if result.ok else f"FAILED: {result.error}"

    tools.append({
        "name": "channel_poll",
        "description": "Create a poll on a messaging channel.",
        "parameters": {
            "type": "object",
            "properties": {
                "channel": {"type": "string",
                            "description": "Channel ID"},
                "question": {"type": "string",
                             "description": "Poll question"},
                "options": {"type": "string",
                            "description": "Comma-separated poll options"},
                "to": {"type": "string",
                       "description": "Chat/channel ID",
                       "default": ""},
            },
            "required": ["channel", "question", "options"],
        },
        "execute": channel_poll,
    })

    return tools
