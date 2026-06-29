"""
Advanced Threading Adapter

  - Threading modes: OFF, REPLY_FIRST, REPLY_ALL
  - Per-channel threading configuration
  - Thread context resolution for LLM system prompts
  - Thread history fetching for conversation continuity

Usage:
    if isinstance(provider, ThreadingMixin):
        mode = provider.resolve_reply_mode()
        context = provider.build_thread_context(thread_id, to)
"""

import logging
from abc import ABC
from dataclasses import dataclass, field
from enum import Enum
from typing import Optional, Dict, Any, List

log = logging.getLogger("quinely.channels.threading")


class ThreadingMode(Enum):
    OFF = "off"
    REPLY_FIRST = "first"
    REPLY_ALL = "all"


@dataclass
class ThreadMessage:
    """A single message in a thread history."""
    message_id: str
    sender_id: str
    sender_name: str
    text: str
    timestamp: float = 0.0
    is_bot: bool = False


@dataclass
class ThreadContext:
    """Resolved thread context for LLM system prompts."""
    thread_id: str
    channel_id: str
    messages: List[ThreadMessage] = field(default_factory=list)
    participant_count: int = 0
    topic: str = ""

    def to_prompt(self, max_messages: int = 10) -> str:
        """Build a system prompt snippet from thread context."""
        if not self.messages:
            return ""
        lines = [f"Thread context ({self.channel_id}, "
                 f"{len(self.messages)} messages):"]
        if self.topic:
            lines.append(f"Topic: {self.topic}")
        recent = self.messages[-max_messages:]
        for msg in recent:
            role = "Bot" if msg.is_bot else msg.sender_name
            lines.append(f"  [{role}]: {msg.text[:200]}")
        return "\n".join(lines)


class ThreadingMixin(ABC):
    """Mixin for ChannelProvider subclasses with threading support.

    Override methods to provide channel-specific threading behavior.
    """

    def resolve_reply_mode(self, config: Dict[str, Any] = None) -> ThreadingMode:
        """Determine threading mode from config. Defaults vary per channel."""
        if config and "threading_mode" in config:
            try:
                return ThreadingMode(config["threading_mode"])
            except ValueError:
                pass
        return ThreadingMode.REPLY_ALL

    def allow_explicit_reply_tags_when_off(self) -> bool:
        """When threading is OFF, should explicit @reply tags still work?"""
        return True

    def get_thread_history(self, thread_id: str, to: str = "",
                           limit: int = 20) -> List[ThreadMessage]:
        """Fetch recent messages from a thread. Override per channel."""
        return []

    def build_thread_context(self, thread_id: str, to: str = "",
                              channel_id: str = "",
                              limit: int = 10) -> ThreadContext:
        """Build a ThreadContext for the LLM from thread history."""
        messages = self.get_thread_history(thread_id, to=to, limit=limit)
        return ThreadContext(
            thread_id=thread_id,
            channel_id=channel_id or getattr(getattr(self, "meta", None), "id", ""),
            messages=messages,
            participant_count=len(set(m.sender_id for m in messages)),
        )

    def resolve_reply_to(self, mode: ThreadingMode,
                          thread_messages: List[ThreadMessage],
                          original_message_id: str = "") -> Optional[str]:
        """Determine which message_id to reply to based on mode."""
        if mode == ThreadingMode.OFF:
            return None
        if mode == ThreadingMode.REPLY_FIRST:
            if thread_messages:
                return thread_messages[0].message_id
            return original_message_id or None
        if mode == ThreadingMode.REPLY_ALL:
            return original_message_id or None
        return None


def build_threading_tools(registry) -> list:
    """Build LLM tools for thread management."""
    tools = []

    def channel_thread_context(channel: str, thread_id: str,
                                to: str = "", limit: int = 10) -> str:
        prov = registry.get(channel)
        if not prov:
            return f"Unknown channel: {channel}"
        if not isinstance(prov, ThreadingMixin):
            return f"{channel} does not support advanced threading"
        ctx = prov.build_thread_context(thread_id, to=to,
                                        channel_id=channel, limit=limit)
        if not ctx.messages:
            return f"No thread history found for thread {thread_id}"
        return ctx.to_prompt(max_messages=limit)

    tools.append({
        "name": "channel_thread_context",
        "description": (
            "Fetch recent thread history from a messaging channel for context. "
            "Useful before replying in a conversation thread."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "channel": {"type": "string",
                            "description": "Channel ID (e.g. 'slack', 'telegram')"},
                "thread_id": {"type": "string",
                              "description": "Thread ID to fetch history for"},
                "to": {"type": "string",
                       "description": "Chat/channel ID",
                       "default": ""},
                "limit": {"type": "integer",
                          "description": "Max messages to fetch",
                          "default": 10},
            },
            "required": ["channel", "thread_id"],
        },
        "execute": channel_thread_context,
    })

    return tools
