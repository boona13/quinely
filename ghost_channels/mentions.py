"""
Mention Handling

  - Per-channel mention syntax (@user, <@U123>, etc.)
  - Strip bot mentions from inbound messages
  - Format user mentions in outbound messages
  - Mention detection and extraction

Usage:
    if isinstance(provider, MentionMixin):
        clean = provider.strip_bot_mention(text)
        mentions = provider.extract_mentions(text)
        formatted = provider.format_mention(user_id, user_name)
"""

import re
import logging
from typing import List, Dict, Any, Optional
from dataclasses import dataclass

log = logging.getLogger("quinely.channels.mentions")


@dataclass
class Mention:
    """An extracted mention from message text."""
    raw: str
    user_id: str = ""
    user_name: str = ""
    start: int = 0
    end: int = 0
    is_bot: bool = False


CHANNEL_MENTION_PATTERNS: Dict[str, List[str]] = {
    "slack": [
        r'<@([A-Z0-9]+)(?:\|([^>]*))?>'
    ],
    "discord": [
        r'<@!?(\d+)>',
    ],
    "telegram": [
        r'@(\w{5,32})',
    ],
    "matrix": [
        r'@([a-z0-9._=\-/]+):([a-z0-9.\-]+)',
    ],
    "msteams": [
        r'<at>([^<]+)</at>',
    ],
    "mattermost": [
        r'@(\w+)',
    ],
    "irc": [
        r'^(\S+?)[:,]\s',
        r'\b(\S+?):\s',
    ],
    "googlechat": [
        r'<users/(\d+)>',
    ],
}

BOT_MENTION_PATTERNS: Dict[str, str] = {
    "slack": r'<@[A-Z0-9]+(?:\|[^>]*)?>\s*',
    "discord": r'<@!?\d+>\s*',
    "telegram": r'(?<!\S)@\w+\s*',
    "msteams": r'<at>[^<]+</at>\s*',
    "mattermost": r'(?<!\S)@\w+\s*',
}


class MentionMixin:
    """Mixin for ChannelProvider subclasses with mention handling.

    Override strip_patterns() and format_mention() for channel-specific
    mention formatting.
    """

    def strip_patterns(self) -> List[str]:
        """Return regex patterns for mentions to strip from inbound text.

        Override per channel if default patterns aren't sufficient.
        """
        channel_id = getattr(getattr(self, "meta", None), "id", "unknown")
        return CHANNEL_MENTION_PATTERNS.get(channel_id, [])

    def strip_bot_mention(self, text: str, bot_id: str = "",
                           bot_name: str = "") -> str:
        """Remove bot mentions from inbound text.

        Only strips @mentions that are standalone (not part of an email).
        """
        if bot_name:
            text = re.sub(rf'(?<!\S)@?{re.escape(bot_name)}(?!\S)\s*', '', text,
                          flags=re.IGNORECASE).strip()
        if bot_id:
            text = re.sub(rf'(?<!\S)@{re.escape(bot_id)}(?!\S)', '', text).strip()

        channel_id = getattr(getattr(self, "meta", None), "id", "unknown")
        pattern = BOT_MENTION_PATTERNS.get(channel_id)
        if pattern and not bot_name and not bot_id:
            text = re.sub(pattern, "", text, count=1).strip()

        return text

    def strip_all_mentions(self, text: str) -> str:
        """Remove all mention formatting from text."""
        channel_id = getattr(getattr(self, "meta", None), "id", "unknown")
        patterns = CHANNEL_MENTION_PATTERNS.get(channel_id, [])
        for pattern in patterns:
            text = re.sub(pattern, lambda m: m.group(1) if m.lastindex else "",
                          text)
        return text.strip()

    def extract_mentions(self, text: str) -> List[Mention]:
        """Extract mentions from message text."""
        channel_id = getattr(getattr(self, "meta", None), "id", "unknown")
        patterns = CHANNEL_MENTION_PATTERNS.get(channel_id, [])
        mentions = []
        for pattern in patterns:
            for m in re.finditer(pattern, text):
                mention = Mention(
                    raw=m.group(0),
                    user_id=m.group(1) if m.lastindex >= 1 else "",
                    user_name=m.group(2) if m.lastindex and m.lastindex >= 2 else "",
                    start=m.start(),
                    end=m.end(),
                )
                mentions.append(mention)
        return mentions

    def format_mention(self, user_id: str, user_name: str = "") -> str:
        """Format a mention for outbound messages. Override per channel."""
        channel_id = getattr(getattr(self, "meta", None), "id", "unknown")

        formatters = {
            "slack": lambda uid, name: f"<@{uid}>",
            "discord": lambda uid, name: f"<@{uid}>",
            "telegram": lambda uid, name: f"@{name}" if name else f"@{uid}",
            "matrix": lambda uid, name: uid if ":" in uid else f"@{uid}",
            "msteams": lambda uid, name: f"<at>{name or uid}</at>",
            "mattermost": lambda uid, name: f"@{name or uid}",
            "irc": lambda uid, name: f"{name or uid}: ",
            "googlechat": lambda uid, name: f"<users/{uid}>",
        }

        fmt = formatters.get(channel_id)
        if fmt:
            return fmt(user_id, user_name)

        return f"@{user_name or user_id}"

    def is_mention_of_bot(self, text: str, bot_id: str = "",
                           bot_name: str = "") -> bool:
        """Check if the text contains a mention of the bot."""
        if bot_id and bot_id in text:
            return True
        if bot_name and re.search(rf'@{re.escape(bot_name)}\b', text,
                                   re.IGNORECASE):
            return True
        return False
