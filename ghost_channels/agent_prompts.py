"""
Agent Prompt Adapter

  - Per-channel tool hints for the LLM
  - Channel-specific system prompt additions
  - Dynamic tool availability based on channel capabilities
  - Context-aware prompt building for inbound messages

Usage:
    adapter = AgentPromptAdapter(registry)
    hints = adapter.get_tool_hints("telegram")
    prompt = adapter.build_channel_prompt("slack", sender_name="Alice")
"""

import logging
from typing import Dict, Any, List, Optional

log = logging.getLogger("quinely.channels.agent_prompts")


CHANNEL_TOOL_HINTS: Dict[str, List[str]] = {
    "slack": [
        "Use Slack mrkdwn formatting: *bold*, _italic_, ~strikethrough~, `code`",
        "For links use <url|display text> format",
        "You can react to messages with channel_react",
        "Thread replies are supported — use the thread_id from inbound messages",
        "Keep messages under 4000 chars; longer messages will be chunked",
    ],
    "discord": [
        "Use standard Markdown formatting: **bold**, *italic*, ~~strike~~, `code`",
        "Discord supports rich embeds but plain markdown is preferred",
        "Keep messages under 2000 chars",
        "You can react with emojis using channel_react",
    ],
    "telegram": [
        "Use Markdown formatting: **bold**, *italic*, `code`, ```code blocks```",
        "Telegram supports up to 4096 chars per message",
        "You can send documents and media with send_media",
        "Use reply_to_id to reply in threads",
        "Polls are supported via channel_poll",
    ],
    "whatsapp": [
        "Use WhatsApp formatting: *bold*, _italic_, ~strikethrough~, ```code```",
        "Keep messages concise — WhatsApp users expect shorter messages",
        "Media attachments are supported",
    ],
    "signal": [
        "Signal uses plain text — avoid markdown formatting",
        "Keep messages concise",
        "Media attachments are supported",
    ],
    "matrix": [
        "Matrix supports HTML formatting in messages",
        "Use standard Markdown which will be converted to HTML",
        "Large messages are well-supported (64KB limit)",
        "Threading is supported via room threads",
    ],
    "email_channel": [
        "Email supports rich HTML formatting",
        "Include a clear subject line in the title",
        "Keep the main message concise with details below",
        "Attachments are supported via send_media",
    ],
    "msteams": [
        "Teams supports Markdown and Adaptive Cards",
        "Keep messages professional and structured",
        "Use bullet points and headers for clarity",
    ],
    "ntfy": [
        "ntfy is a push notification service — keep messages short",
        "The title field is important for notification display",
        "Priority levels affect notification urgency on the device",
        "Markdown is supported in message body",
    ],
    "sms": [
        "SMS is limited to 160 chars (or 1600 for long SMS)",
        "Use plain text only — no formatting",
        "Be extremely concise",
    ],
    "irc": [
        "IRC uses plain text only",
        "Keep messages under 512 chars (protocol limit)",
        "No media support",
    ],
    "pushover": [
        "Pushover supports basic HTML: <b>bold</b>, <i>italic</i>, <a href>links</a>",
        "Priority levels: -2 (silent) to 2 (emergency)",
        "Keep messages concise for push notifications",
    ],
}

CHANNEL_PROMPT_TEMPLATES: Dict[str, str] = {
    "slack": (
        "The user is messaging via Slack. Use Slack-specific formatting. "
        "You can use threads, reactions, and rich text. "
        "Be conversational but professional."
    ),
    "discord": (
        "The user is messaging via Discord. Use Discord markdown. "
        "Be friendly and informal — Discord culture is more casual."
    ),
    "telegram": (
        "The user is messaging via Telegram. Use Markdown formatting. "
        "You can send polls, react to messages, and use threads. "
        "Be concise — mobile users prefer shorter messages."
    ),
    "whatsapp": (
        "The user is messaging via WhatsApp. Use WhatsApp formatting. "
        "Keep messages short and conversational."
    ),
    "signal": (
        "The user is messaging via Signal. Use plain text only. "
        "Be concise and direct."
    ),
    "matrix": (
        "The user is messaging via Matrix. HTML formatting is supported. "
        "You can use standard Markdown which will be converted."
    ),
    "email_channel": (
        "The user is communicating via Email. Use a formal, well-structured format. "
        "Include a clear subject in the title. Use HTML formatting."
    ),
    "sms": (
        "The user is messaging via SMS. Be extremely concise — "
        "SMS has strict character limits. Plain text only."
    ),
    "irc": (
        "The user is messaging via IRC. Use plain text, be brief. "
        "No media or rich formatting available."
    ),
}


class AgentPromptAdapter:
    """Provides per-channel context and hints for the LLM agent."""

    def __init__(self, registry=None):
        self._registry = registry
        self._custom_hints: Dict[str, List[str]] = {}
        self._custom_prompts: Dict[str, str] = {}

    def register_hints(self, channel_id: str, hints: List[str]):
        self._custom_hints[channel_id] = hints

    def register_prompt(self, channel_id: str, prompt: str):
        self._custom_prompts[channel_id] = prompt

    def get_tool_hints(self, channel_id: str) -> List[str]:
        """Get formatting and tool hints for a specific channel."""
        if channel_id in self._custom_hints:
            return self._custom_hints[channel_id]
        return CHANNEL_TOOL_HINTS.get(channel_id, [])

    def get_channel_prompt(self, channel_id: str) -> str:
        """Get channel-specific system prompt addition."""
        if channel_id in self._custom_prompts:
            return self._custom_prompts[channel_id]
        return CHANNEL_PROMPT_TEMPLATES.get(channel_id, "")

    def build_channel_prompt(self, channel_id: str,
                              sender_name: str = "",
                              sender_id: str = "",
                              thread_id: str = "",
                              is_group: bool = False) -> str:
        """Build a complete channel-aware system prompt section."""
        parts = []

        base = self.get_channel_prompt(channel_id)
        if base:
            parts.append(base)

        if sender_name:
            parts.append(f"Sender: {sender_name}"
                         + (f" (id: {sender_id})" if sender_id else ""))

        if is_group:
            parts.append("This is a group conversation — "
                         "only respond when directly addressed or relevant.")

        if thread_id:
            parts.append(f"You are in thread {thread_id}. "
                         "Reply within the same thread.")

        hints = self.get_tool_hints(channel_id)
        if hints:
            parts.append("Channel tips: " + "; ".join(hints[:3]))

        return "\n".join(parts)

    def get_available_tools(self, channel_id: str) -> List[str]:
        """Get list of tool names available for this channel.

        Filters tools based on channel capabilities.
        """
        if not self._registry:
            return []

        prov = self._registry.get(channel_id)
        if not prov:
            return []

        tools = ["channel_send", "channel_list", "channel_status"]

        meta = prov.meta
        if meta.supports_media:
            tools.append("channel_send_media")

        from ghost_channels.actions import ActionsMixin
        if isinstance(prov, ActionsMixin):
            for action_tool in ["channel_react", "channel_edit",
                                "channel_unsend", "channel_poll"]:
                tools.append(action_tool)

        from ghost_channels.threading_ext import ThreadingMixin
        if isinstance(prov, ThreadingMixin):
            tools.append("channel_thread_context")

        from ghost_channels.directory import DirectoryMixin, ResolverMixin
        if isinstance(prov, DirectoryMixin):
            tools.append("channel_list_contacts")
        if isinstance(prov, ResolverMixin):
            tools.append("channel_resolve")

        return tools


def build_prompt_tools(registry) -> list:
    """Build LLM tools for prompt/hint inspection."""
    adapter = AgentPromptAdapter(registry)
    tools = []

    def channel_hints(channel: str) -> str:
        hints = adapter.get_tool_hints(channel)
        if not hints:
            return f"No specific hints for {channel}"
        lines = [f"Hints for {channel}:"]
        for h in hints:
            lines.append(f"  - {h}")
        available = adapter.get_available_tools(channel)
        if available:
            lines.append(f"\nAvailable tools: {', '.join(available)}")
        return "\n".join(lines)

    tools.append({
        "name": "channel_hints",
        "description": "Get formatting hints and available tools for a messaging channel.",
        "parameters": {
            "type": "object",
            "properties": {
                "channel": {"type": "string",
                            "description": "Channel ID to get hints for"},
            },
            "required": ["channel"],
        },
        "execute": channel_hints,
    })

    return tools
