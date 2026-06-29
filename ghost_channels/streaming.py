"""
Streaming Adapter with Coalescing

  - Real-time message editing as LLM generates tokens
  - Coalescing: buffer tokens, flush on idle (configurable min_chars, idle_ms)
  - Update existing messages in-place (Telegram editMessageText, Slack chat.update, etc.)
  - Integrates with Quinely's LLM engine on_step callback

Usage:
    streamer = MessageStreamer(provider, chat_id)
    streamer.start()
    streamer.append("Hello ")
    streamer.append("world!")
    result = streamer.finalize()
"""

import time
import threading
import logging
from dataclasses import dataclass, field
from typing import Optional, Dict, Any, Callable

log = logging.getLogger("quinely.channels.streaming")


@dataclass
class StreamConfig:
    """Configuration for message streaming."""
    min_chars: int = 40
    idle_ms: int = 500
    max_edits_per_second: float = 2.0
    placeholder: str = "..."
    enable: bool = True


class StreamingMixin:
    """Mixin for ChannelProvider subclasses that support in-place message editing.

    Providers must implement edit_message_text() to enable streaming.
    block_streaming_coalesce_defaults() can be overridden for channel-specific
    buffering defaults.
    """

    def supports_streaming(self) -> bool:
        """Whether this channel supports streaming (in-place edits)."""
        return False

    def block_streaming_coalesce_defaults(self) -> StreamConfig:
        return StreamConfig()

    def edit_message_text(self, message_id: str, new_text: str,
                           to: str = "", **kwargs) -> bool:
        """Edit an existing message's text in-place. Returns True on success."""
        return False

    def send_placeholder(self, to: str, placeholder: str = "...",
                          **kwargs) -> Optional[str]:
        """Send a placeholder message and return its message_id.
        Override if sending needs special handling for streaming.
        """
        return None


class MessageStreamer:
    """Buffers LLM output tokens and streams them to a channel via edits.

    Manages coalescing (buffering until min_chars reached or idle timeout),
    rate limiting (max_edits_per_second), and finalization.
    """

    def __init__(self, provider, to: str, config: StreamConfig = None,
                 initial_message_id: str = None, **send_kwargs):
        if not isinstance(provider, StreamingMixin):
            raise TypeError(f"Provider does not support streaming: {type(provider)}")
        self._provider = provider
        self._to = to
        self._config = config or provider.block_streaming_coalesce_defaults()
        self._send_kwargs = send_kwargs

        self._buffer = ""
        self._sent_text = ""
        self._message_id = initial_message_id
        self._lock = threading.Lock()
        self._last_edit_time = 0.0
        self._idle_timer: Optional[threading.Timer] = None
        self._finalized = False
        self._edit_count = 0

    @property
    def message_id(self) -> Optional[str]:
        return self._message_id

    @property
    def edit_count(self) -> int:
        return self._edit_count

    def start(self):
        """Send initial placeholder message if needed."""
        if not self._message_id and self._config.placeholder:
            self._message_id = self._provider.send_placeholder(
                self._to, self._config.placeholder, **self._send_kwargs
            )

    def append(self, text: str):
        """Append new tokens to the buffer. Triggers flush when conditions met."""
        if self._finalized:
            return
        with self._lock:
            self._buffer += text

            if self._idle_timer:
                self._idle_timer.cancel()

            pending = self._buffer
            if len(pending) >= self._config.min_chars:
                self._flush_locked()
            else:
                self._idle_timer = threading.Timer(
                    self._config.idle_ms / 1000.0,
                    self._idle_flush,
                )
                self._idle_timer.daemon = True
                self._idle_timer.start()

    def _idle_flush(self):
        """Called when idle timer fires — flush whatever is buffered."""
        with self._lock:
            if self._buffer and not self._finalized:
                self._flush_locked()

    def _flush_locked(self):
        """Flush buffer to channel via edit. Must hold self._lock."""
        now = time.time()
        min_interval = 1.0 / self._config.max_edits_per_second
        if now - self._last_edit_time < min_interval:
            return

        new_text = self._sent_text + self._buffer
        self._buffer = ""

        if not self._message_id:
            return

        try:
            ok = self._provider.edit_message_text(
                self._message_id, new_text, to=self._to, **self._send_kwargs
            )
            if ok:
                self._sent_text = new_text
                self._last_edit_time = now
                self._edit_count += 1
        except Exception as exc:
            log.debug("Stream edit failed: %s", exc)

    def finalize(self) -> Optional[str]:
        """Flush remaining buffer and mark stream as done.
        Returns the final message_id.
        """
        with self._lock:
            if self._idle_timer:
                self._idle_timer.cancel()
                self._idle_timer = None

            self._finalized = True

            if self._buffer:
                final_text = self._sent_text + self._buffer
                self._buffer = ""
                if self._message_id:
                    try:
                        self._provider.edit_message_text(
                            self._message_id, final_text,
                            to=self._to, **self._send_kwargs
                        )
                        self._sent_text = final_text
                    except Exception as exc:
                        log.debug("Final stream edit failed: %s", exc)

        return self._message_id

    def cancel(self):
        """Cancel streaming and optionally delete the placeholder."""
        with self._lock:
            if self._idle_timer:
                self._idle_timer.cancel()
                self._idle_timer = None
            self._finalized = True
            self._buffer = ""


def create_stream_callback(provider, to: str, config: StreamConfig = None,
                            **kwargs) -> tuple:
    """Create a (streamer, on_step_callback) pair for the LLM engine.

    Returns:
        (MessageStreamer, Callable): The streamer and a callback function
        suitable for passing as on_step to the LLM engine.
    """
    streamer = MessageStreamer(provider, to, config=config, **kwargs)
    streamer.start()

    def on_step(step_data: dict):
        text = step_data.get("text", "")
        if text:
            streamer.append(text)

    return streamer, on_step
