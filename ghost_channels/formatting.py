"""
Per-Channel Message Formatting

  - Markdown-to-plain conversion for channels that don't support it (SMS, IRC)
  - Markdown-to-channel-native (Slack mrkdwn, Telegram MarkdownV2, Discord, etc.)
  - Table rendering (code blocks vs plain depending on channel)
  - Per-channel formatters registered via the plugin system
  - Paragraph-aware and length-aware chunking with mode support

Usage:
    formatter = MessageFormatter()
    chunks = formatter.format_and_chunk("**Hello** world!", "telegram", limit=4096)
"""

import re
import logging
from typing import List, Dict, Any, Optional, Callable

log = logging.getLogger("quinely.channels.formatting")


class FormatMode:
    PLAIN = "plain"
    MARKDOWN = "markdown"
    SLACK_MRKDWN = "slack_mrkdwn"
    TELEGRAM_MD2 = "telegram_md2"
    TELEGRAM_HTML = "telegram_html"
    DISCORD_MD = "discord_md"
    HTML = "html"
    MATRIX_HTML = "matrix_html"


CHANNEL_FORMAT_DEFAULTS: Dict[str, str] = {
    "telegram": FormatMode.MARKDOWN,
    "slack": FormatMode.SLACK_MRKDWN,
    "discord": FormatMode.DISCORD_MD,
    "matrix": FormatMode.MATRIX_HTML,
    "msteams": FormatMode.MARKDOWN,
    "googlechat": FormatMode.MARKDOWN,
    "mattermost": FormatMode.MARKDOWN,
    "email_channel": FormatMode.HTML,
    "webhook": FormatMode.MARKDOWN,

    "ntfy": FormatMode.MARKDOWN,
    "pushover": FormatMode.HTML,
    "line": FormatMode.PLAIN,
    "sms": FormatMode.PLAIN,
    "irc": FormatMode.PLAIN,
    "signal": FormatMode.PLAIN,
    "nostr": FormatMode.PLAIN,
    "imessage": FormatMode.PLAIN,
    "whatsapp": FormatMode.MARKDOWN,
}


def markdown_to_plain(text: str) -> str:
    """Strip markdown formatting, preserving readable text."""
    text = re.sub(r'\*\*(.+?)\*\*', r'\1', text)
    text = re.sub(r'__(.+?)__', r'\1', text)
    text = re.sub(r'\*(.+?)\*', r'\1', text)
    text = re.sub(r'_(.+?)_', r'\1', text)
    text = re.sub(r'~~(.+?)~~', r'\1', text)
    text = re.sub(r'`{3}[\w]*\n?', '', text)
    text = re.sub(r'`(.+?)`', r'\1', text)
    text = re.sub(r'\[(.+?)\]\((.+?)\)', r'\1 (\2)', text)
    text = re.sub(r'^#{1,6}\s+', '', text, flags=re.MULTILINE)
    text = re.sub(r'^>\s?', '', text, flags=re.MULTILINE)
    text = re.sub(r'^[-*+]\s', '- ', text, flags=re.MULTILINE)
    text = re.sub(r'^\d+\.\s', lambda m: m.group(), text, flags=re.MULTILINE)
    return text.strip()


def markdown_to_slack_mrkdwn(text: str) -> str:
    """Convert standard markdown to Slack's mrkdwn format."""
    text = re.sub(r'\*\*(.+?)\*\*', r'*\1*', text)
    text = re.sub(r'__(.+?)__', r'*\1*', text)
    text = re.sub(r'(?<!\*)\*(?!\*)(.+?)(?<!\*)\*(?!\*)', r'_\1_', text)
    text = re.sub(r'(?<!_)_(?!_)(.+?)(?<!_)_(?!_)', r'_\1_', text)
    text = re.sub(r'~~(.+?)~~', r'~\1~', text)
    text = re.sub(r'\[(.+?)\]\((.+?)\)', r'<\2|\1>', text)
    text = re.sub(r'^#{1,6}\s+(.+)$', r'*\1*', text, flags=re.MULTILINE)
    text = re.sub(r'^>\s?(.+)$', r'> \1', text, flags=re.MULTILINE)
    return text


def markdown_to_telegram_md2(text: str) -> str:
    """Convert standard markdown to Telegram MarkdownV2 (escape special chars)."""
    SPECIAL = r'_[]()~`>#+-=|{}.!'

    def escape_outside_entities(t: str) -> str:
        parts = re.split(r'(`[^`]+`|\*\*[^*]+\*\*|__[^_]+__|_[^_]+_|\[.+?\]\(.+?\))', t)
        result = []
        for i, part in enumerate(parts):
            if i % 2 == 0:
                escaped = ""
                for ch in part:
                    if ch in SPECIAL:
                        escaped += "\\" + ch
                    else:
                        escaped += ch
                result.append(escaped)
            else:
                result.append(part)
        return "".join(result)

    text = escape_outside_entities(text)
    return text


def markdown_to_html(text: str) -> str:
    """Simple markdown-to-HTML for email and Matrix."""
    text = re.sub(r'\*\*(.+?)\*\*', r'<b>\1</b>', text)
    text = re.sub(r'__(.+?)__', r'<b>\1</b>', text)
    text = re.sub(r'\*(.+?)\*', r'<i>\1</i>', text)
    text = re.sub(r'_(.+?)_', r'<i>\1</i>', text)
    text = re.sub(r'~~(.+?)~~', r'<s>\1</s>', text)
    text = re.sub(r'`(.+?)`', r'<code>\1</code>', text)
    text = re.sub(r'```(\w*)\n(.*?)```', r'<pre><code>\2</code></pre>',
                  text, flags=re.DOTALL)
    text = re.sub(r'\[(.+?)\]\((.+?)\)', r'<a href="\2">\1</a>', text)
    text = re.sub(r'^#{1,6}\s+(.+)$',
                  lambda m: f'<b>{m.group(1)}</b>', text, flags=re.MULTILINE)
    text = re.sub(r'^>\s?(.+)$', r'<blockquote>\1</blockquote>',
                  text, flags=re.MULTILINE)
    text = text.replace("\n", "<br>\n")
    return text


CONVERTERS: Dict[str, Callable[[str], str]] = {
    FormatMode.PLAIN: markdown_to_plain,
    FormatMode.SLACK_MRKDWN: markdown_to_slack_mrkdwn,
    FormatMode.TELEGRAM_MD2: markdown_to_telegram_md2,
    FormatMode.TELEGRAM_HTML: markdown_to_html,
    FormatMode.HTML: markdown_to_html,
    FormatMode.MATRIX_HTML: markdown_to_html,
    FormatMode.MARKDOWN: lambda t: t,
    FormatMode.DISCORD_MD: lambda t: t,
}


def format_table_plain(rows: List[List[str]], headers: List[str] = None) -> str:
    """Render a table as aligned plain text."""
    all_rows = ([headers] if headers else []) + rows
    if not all_rows:
        return ""
    widths = [max(len(str(cell)) for cell in col) for col in zip(*all_rows)]
    lines = []
    for i, row in enumerate(all_rows):
        line = "  ".join(str(cell).ljust(w) for cell, w in zip(row, widths))
        lines.append(line)
        if i == 0 and headers:
            lines.append("  ".join("-" * w for w in widths))
    return "\n".join(lines)


def format_table_code_block(rows: List[List[str]], headers: List[str] = None) -> str:
    """Render a table inside a code block."""
    return "```\n" + format_table_plain(rows, headers) + "\n```"


def chunk_by_paragraphs(text: str, limit: int) -> List[str]:
    """Split text at paragraph boundaries respecting limit."""
    if len(text) <= limit:
        return [text]
    chunks: List[str] = []
    paragraphs = text.split("\n\n")
    current = ""
    for para in paragraphs:
        candidate = f"{current}\n\n{para}" if current else para
        if len(candidate) > limit:
            if current:
                chunks.append(current.strip())
            while len(para) > limit:
                chunks.append(para[:limit])
                para = para[limit:]
            current = para
        else:
            current = candidate
    if current.strip():
        chunks.append(current.strip())
    return chunks or [text[:limit]]


def chunk_by_newlines(text: str, limit: int) -> List[str]:
    """Split text at newline boundaries respecting limit."""
    if len(text) <= limit:
        return [text]
    chunks: List[str] = []
    lines = text.split("\n")
    current = ""
    for line in lines:
        candidate = f"{current}\n{line}" if current else line
        if len(candidate) > limit:
            if current:
                chunks.append(current)
            if len(line) > limit:
                while len(line) > limit:
                    chunks.append(line[:limit])
                    line = line[limit:]
                current = line
            else:
                current = line
        else:
            current = candidate
    if current:
        chunks.append(current)
    return chunks or [text[:limit]]


def chunk_preserving_code_blocks(text: str, limit: int) -> List[str]:
    """Chunk text while trying not to split code blocks across chunks."""
    if len(text) <= limit:
        return [text]

    parts = re.split(r'(```[\s\S]*?```)', text)
    chunks: List[str] = []
    current = ""

    for part in parts:
        if not part:
            continue
        candidate = current + part
        if len(candidate) <= limit:
            current = candidate
        else:
            if current:
                chunks.append(current.strip())
            if len(part) <= limit:
                current = part
            else:
                for sub in chunk_by_paragraphs(part, limit):
                    chunks.append(sub)
                current = ""

    if current.strip():
        chunks.append(current.strip())
    return chunks or [text[:limit]]


class MessageFormatter:
    """Per-channel message formatting and chunking engine."""

    def __init__(self):
        self._custom_formatters: Dict[str, Callable[[str], str]] = {}
        self._custom_chunkers: Dict[str, Callable[[str, int], List[str]]] = {}

    def register_formatter(self, channel_id: str, fn: Callable[[str], str]):
        self._custom_formatters[channel_id] = fn

    def register_chunker(self, channel_id: str,
                         fn: Callable[[str, int], List[str]]):
        self._custom_chunkers[channel_id] = fn

    def get_format_mode(self, channel_id: str) -> str:
        return CHANNEL_FORMAT_DEFAULTS.get(channel_id, FormatMode.PLAIN)

    def format_text(self, text: str, channel_id: str) -> str:
        """Convert markdown text to channel-appropriate format."""
        if channel_id in self._custom_formatters:
            return self._custom_formatters[channel_id](text)

        mode = self.get_format_mode(channel_id)
        converter = CONVERTERS.get(mode)
        if converter:
            return converter(text)
        return text

    def chunk_text(self, text: str, channel_id: str, limit: int = 4000,
                   mode: str = "paragraph") -> List[str]:
        """Split text into chunks appropriate for the channel."""
        if channel_id in self._custom_chunkers:
            return self._custom_chunkers[channel_id](text, limit)

        if mode == "newline":
            return chunk_by_newlines(text, limit)
        elif mode == "code_aware":
            return chunk_preserving_code_blocks(text, limit)
        else:
            return chunk_by_paragraphs(text, limit)

    def format_and_chunk(self, text: str, channel_id: str,
                         limit: int = 4000,
                         chunk_mode: str = "paragraph") -> List[str]:
        """Format text for the channel then split into chunks."""
        formatted = self.format_text(text, channel_id)
        return self.chunk_text(formatted, channel_id, limit, chunk_mode)
