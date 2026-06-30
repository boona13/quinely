"""Regression tests for the Codex Responses-API call_id handling.

The Responses API rejects empty `call_id` on function_call / function_call_output
items (HTTP 400 "empty_string"). Tool calls reconstructed from history or produced
by providers that omit ids can arrive without one. The adapter must synthesize
stable ids and pair outputs to calls by order so codex stays usable in multi-step
tool loops.
"""

from ghost_providers import _adapt_to_codex_responses


def _call_ids(adapted):
    return [
        (it["type"], it["call_id"])
        for it in adapted["input"]
        if it.get("type") in ("function_call", "function_call_output")
    ]


def test_empty_ids_are_synthesized_and_paired():
    payload = {
        "model": "gpt-5.5",
        "messages": [
            {"role": "user", "content": "list files"},
            {"role": "assistant", "content": "", "tool_calls": [
                {"id": "", "type": "function",
                 "function": {"name": "glob", "arguments": '{"pattern":"*.py"}'}},
            ]},
            {"role": "tool", "tool_call_id": "", "content": "a.py"},
        ],
    }
    items = _call_ids(_adapt_to_codex_responses(payload))
    assert items == [("function_call", "call_auto_0"), ("function_call_output", "call_auto_0")]
    # No empty call_id may ever reach the API.
    assert all(cid for _, cid in items)


def test_multiple_missing_ids_pair_in_order():
    payload = {
        "model": "gpt-5.5",
        "messages": [
            {"role": "assistant", "content": "", "tool_calls": [
                {"type": "function", "function": {"name": "glob", "arguments": "{}"}},
            ]},
            {"role": "tool", "content": "a.py"},
            {"role": "assistant", "content": "", "tool_calls": [
                {"type": "function", "function": {"name": "glob", "arguments": "{}"}},
            ]},
            {"role": "tool", "content": "b.md"},
        ],
    }
    items = _call_ids(_adapt_to_codex_responses(payload))
    assert items == [
        ("function_call", "call_auto_0"),
        ("function_call_output", "call_auto_0"),
        ("function_call", "call_auto_1"),
        ("function_call_output", "call_auto_1"),
    ]


def test_existing_ids_are_preserved():
    payload = {
        "model": "gpt-5.5",
        "messages": [
            {"role": "assistant", "content": "", "tool_calls": [
                {"id": "call_real", "type": "function",
                 "function": {"name": "glob", "arguments": "{}"}},
            ]},
            {"role": "tool", "tool_call_id": "call_real", "content": "a.py"},
        ],
    }
    items = _call_ids(_adapt_to_codex_responses(payload))
    assert items == [("function_call", "call_real"), ("function_call_output", "call_real")]


def test_orphan_tool_output_without_matching_call_is_dropped():
    # A function_call_output whose id matches no function_call is dropped (existing
    # behavior) — and must never carry an empty id.
    payload = {
        "model": "gpt-5.5",
        "messages": [
            {"role": "tool", "tool_call_id": "call_missing", "content": "stale"},
        ],
    }
    adapted = _adapt_to_codex_responses(payload)
    outputs = [it for it in adapted["input"] if it.get("type") == "function_call_output"]
    assert outputs == []
