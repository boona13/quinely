"""
Ghost Message Repair — Fix dangling tool calls in conversation history.

Mirrored from DeerFlow's DanglingToolCallMiddleware.

A dangling tool call occurs when an assistant message contains tool_calls but
there are no corresponding tool result messages in the history (e.g., due to
crash, user interruption, or request cancellation). This causes LLM errors
due to incomplete message format.

This module scans conversation history and patches such gaps by inserting
synthetic tool-result messages with an error indicator immediately after the
assistant message that made the dangling calls.
"""

import logging
import threading

log = logging.getLogger("quinely.message_repair")

_repair_stats = {"total_scanned": 0, "repairs_performed": 0, "dangling_found": 0}
_repair_stats_lock = threading.Lock()


def get_repair_stats() -> dict:
    with _repair_stats_lock:
        return dict(_repair_stats)


def repair_dangling_tool_calls(messages: list[dict]) -> list[dict]:
    """Scan message history and insert placeholders for orphaned tool calls.

    For each assistant message with tool_calls that lack a corresponding
    tool-result message, a synthetic error response is inserted immediately
    after the offending assistant message.

    Args:
        messages: List of OpenAI-format message dicts.

    Returns:
        New list with patches inserted (or the original list if no patches needed).
    """
    existing_tool_call_ids: set[str] = set()
    for msg in messages:
        if msg.get("role") == "tool":
            tc_id = msg.get("tool_call_id")
            if tc_id:
                existing_tool_call_ids.add(tc_id)

    needs_patch = False
    for msg in messages:
        if msg.get("role") != "assistant":
            continue
        for tc in msg.get("tool_calls") or []:
            tc_id = tc.get("id")
            if tc_id and tc_id not in existing_tool_call_ids:
                needs_patch = True
                break
        if needs_patch:
            break

    with _repair_stats_lock:
        _repair_stats["total_scanned"] += len(messages)

    if not needs_patch:
        return messages

    patched: list[dict] = []
    patched_ids: set[str] = set()
    patch_count = 0

    for msg in messages:
        patched.append(msg)
        if msg.get("role") != "assistant":
            continue
        for tc in msg.get("tool_calls") or []:
            tc_id = tc.get("id")
            if tc_id and tc_id not in existing_tool_call_ids and tc_id not in patched_ids:
                patched.append({
                    "role": "tool",
                    "tool_call_id": tc_id,
                    "name": tc.get("function", {}).get("name", "unknown"),
                    "content": "[Tool call was interrupted and did not return a result.]",
                })
                patched_ids.add(tc_id)
                patch_count += 1

    log.warning("Injected %d placeholder tool message(s) for dangling tool calls", patch_count)
    with _repair_stats_lock:
        _repair_stats["dangling_found"] += patch_count
        _repair_stats["repairs_performed"] += 1
    return patched


def count_dangling_tool_calls(messages: list[dict]) -> int:
    """Count how many tool calls lack a corresponding tool result.

    Useful for diagnostics without modifying the message list.
    """
    existing_ids: set[str] = set()
    for msg in messages:
        if msg.get("role") == "tool":
            tc_id = msg.get("tool_call_id")
            if tc_id:
                existing_ids.add(tc_id)

    count = 0
    for msg in messages:
        if msg.get("role") != "assistant":
            continue
        for tc in msg.get("tool_calls") or []:
            tc_id = tc.get("id")
            if tc_id and tc_id not in existing_ids:
                count += 1
    return count
