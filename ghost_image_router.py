"""
Ghost Image Router — pre-LLM intent classification for image attachments.

When a user attaches an image, Ghost needs to decide:

  1. TOOL-ACTIONABLE — the user wants to DO something to the image
     (remove background, upscale, convert, compress, etc.)
     → Don't send base64 to the LLM. Just tell it the file path and hint the tool.
       Saves massive tokens and ensures the right tool is called.

  2. VISION-REQUIRED — the user wants the AI to SEE/understand the image
     (describe, analyze, OCR, compare, answer questions about content, etc.)
     → Send base64 to a vision-capable model.

The classification uses a fast/cheap LLM call (gemini-flash) with the user's
message and the list of available image tools. This is NOT regex or pattern
matching — it's a proper semantic classifier.
"""

from __future__ import annotations

import json
import logging
import re
import time
from typing import Any

import requests

log = logging.getLogger("quinely.image_router")

# ──────────────────────────────────────────────────────────────────────
#  Vision Capability Registry
# ──────────────────────────────────────────────────────────────────────

_VISION_CAPABLE_PATTERNS: set[str] = {
    # OpenAI
    "gpt-4o",
    "gpt-4o-mini",
    "gpt-4-turbo",
    "gpt-4-vision",
    "gpt-5",
    "gpt-5.3",
    "o1",
    "o3",
    "o4-mini",
    # Anthropic
    "claude-sonnet-4",
    "claude-opus-4",
    "claude-3.5-sonnet",
    "claude-3-opus",
    "claude-3-sonnet",
    "claude-3-haiku",
    # Google
    "gemini-2.0-flash",
    "gemini-2.5-flash",
    "gemini-2.5-pro",
    "gemini-pro-vision",
    "gemini-1.5-pro",
    "gemini-1.5-flash",
    # Meta
    "llama-3.2-11b-vision",
    "llama-3.2-90b-vision",
    # Local / Ollama
    "llava",
    "moondream",
    "bakllava",
    # Qwen
    "qwen-vl",
    "qwen2-vl",
    "qwen2.5-vl",
}

_TEXT_ONLY_PATTERNS: set[str] = {
    "gpt-5.3-codex",
    "codex",
    "qwen3.5-plus",
    "deepseek",
    "command-r",
    "mistral",
    "mixtral",
    "minimax",
    "phi-",
    "llama-3.1",
    "llama-3.3",
}


def supports_vision(model_id: str) -> bool:
    """Check if a model supports vision (image_url content blocks).

    Uses a two-tier check:
    1. Explicit text-only patterns → False
    2. Known vision-capable patterns → True
    3. Unknown models → True (optimistic default; most modern models support vision)
    """
    if not model_id:
        return False

    normalized = model_id.lower()
    # Strip provider prefixes (openrouter/openai/gpt-4o → gpt-4o)
    for prefix in ("openrouter/", "openai/", "anthropic/", "google/",
                   "meta-llama/", "qwen/", "mistralai/", "deepseek/"):
        if normalized.startswith(prefix):
            normalized = normalized[len(prefix):]
    # Strip secondary provider prefix (e.g., openai/gpt-4o after openrouter/)
    for prefix in ("openai/", "anthropic/", "google/", "meta-llama/"):
        if normalized.startswith(prefix):
            normalized = normalized[len(prefix):]

    for pattern in _TEXT_ONLY_PATTERNS:
        if pattern in normalized:
            return False

    for pattern in _VISION_CAPABLE_PATTERNS:
        if pattern in normalized:
            return True

    # Default: optimistic — most frontier models support vision
    return True


# ──────────────────────────────────────────────────────────────────────
#  Image Tool Discovery
# ──────────────────────────────────────────────────────────────────────

_IMAGE_PARAM_NAMES = {"image_path", "image_url", "input_image", "source_image", "img_path"}

_VISION_ANALYSIS_TOOLS = {
    "image_analyze", "screenshot_analyze", "analyze_image",
    "florence_analyze", "ocr_extract",
}


def get_image_tools(tool_registry) -> list[dict]:
    """Find tools that PROCESS/TRANSFORM images (not analyze/understand them).

    Returns a list of {name, description, param_name} dicts for tools
    that accept an image file path and produce a transformed output.
    Vision/analysis tools are excluded — those are for understanding content.
    """
    if not tool_registry:
        return []

    image_tools = []
    for name, tool_def in tool_registry.get_all().items():
        if name in _VISION_ANALYSIS_TOOLS:
            continue

        params = tool_def.get("parameters", {})
        properties = params.get("properties", {})
        for param_name, param_def in properties.items():
            if param_name in _IMAGE_PARAM_NAMES:
                image_tools.append({
                    "name": name,
                    "description": tool_def.get("description", ""),
                    "param_name": param_name,
                })
                break
            desc_lower = (param_def.get("description") or "").lower()
            if param_def.get("type") == "string" and (
                "image" in param_name.lower() and "path" in desc_lower
            ):
                image_tools.append({
                    "name": name,
                    "description": tool_def.get("description", ""),
                    "param_name": param_name,
                })
                break

    return image_tools


# ──────────────────────────────────────────────────────────────────────
#  Intent Classification (fast LLM call)
# ──────────────────────────────────────────────────────────────────────

_CLASSIFY_SYSTEM_PROMPT = """\
You are an intent classifier for an AI agent that has image-processing tools.

Given a user message and a list of available image tools, determine whether
the user wants:

1. "tool" — to PROCESS/TRANSFORM the image using one of the available tools
   (remove background, upscale, convert format, compress, crop, add effects,
   generate video from image, etc.). The AI does NOT need to see or understand
   the image content — just route it to the tool.

2. "vision" — to have the AI LOOK AT and UNDERSTAND the image (describe it,
   analyze content, answer questions about what's in it, OCR, compare with
   another image, identify objects/people, provide feedback on design, etc.).

COMPOUND INTENTS: If the user wants BOTH processing AND understanding
(e.g., "describe this image and remove the background"), classify as "vision"
because the AI needs to see the image. The tools will still be available.

Respond with ONLY a JSON object — no markdown fences, no extra text:
{"intent": "tool", "tool_name": "<best matching tool name>", "confidence": 0.0-1.0}
or
{"intent": "vision", "confidence": 0.0-1.0}"""

_CLASSIFY_TIMEOUT = 8  # seconds — this must be fast


def classify_image_intent(
    user_message: str,
    image_tools: list[dict],
    auth_store: Any = None,
    config: dict | None = None,
) -> dict:
    """Classify whether the user wants tool-processing or vision understanding.

    Makes a fast LLM call using the cheapest available model.
    Falls back to {"intent": "vision"} on any error (safe default).
    """
    if not image_tools:
        return {"intent": "vision", "confidence": 1.0, "reason": "no_image_tools"}

    # Truncate user message to avoid sending huge text to the classifier.
    # The classifier only needs the user's intent, not the full attachment context.
    msg_for_classify = user_message[:1000]
    if len(user_message) > 1000:
        msg_for_classify += "..."

    tool_list = "\n".join(
        f"- {t['name']}: {t['description']}" for t in image_tools
    )
    user_prompt = (
        f"User message: \"{msg_for_classify}\"\n\n"
        f"Available image tools:\n{tool_list}\n\n"
        f"Classify the intent."
    )

    try:
        result = _fast_llm_classify(user_prompt, auth_store, config)
        if result:
            return result
    except Exception as e:
        log.warning("Image intent classification failed: %s", e)

    return {"intent": "vision", "confidence": 0.5, "reason": "classification_failed"}


def _fast_llm_classify(
    user_prompt: str,
    auth_store: Any = None,
    config: dict | None = None,
) -> dict | None:
    """Make a fast, cheap LLM call for classification."""
    config = config or {}

    api_key = None
    if auth_store and hasattr(auth_store, "get_api_key"):
        api_key = auth_store.get_api_key("openrouter")
    if not api_key:
        import os
        api_key = os.environ.get("OPENROUTER_API_KEY")
    if not api_key:
        api_key = config.get("api_key")
    if not api_key:
        return None

    model = config.get("image_router_model", "google/gemini-2.0-flash-001")

    payload = {
        "model": model,
        "messages": [
            {"role": "system", "content": _CLASSIFY_SYSTEM_PROMPT},
            {"role": "user", "content": user_prompt},
        ],
        "temperature": 0.0,
        "max_tokens": 100,
    }

    headers = {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json",
    }

    t0 = time.time()
    resp = requests.post(
        "https://openrouter.ai/api/v1/chat/completions",
        headers=headers,
        json=payload,
        timeout=_CLASSIFY_TIMEOUT,
    )
    elapsed = time.time() - t0

    if resp.status_code != 200:
        log.warning("Image router classify HTTP %d: %s", resp.status_code, resp.text[:200])
        return None

    data = resp.json()
    text = (data.get("choices") or [{}])[0].get("message", {}).get("content", "")
    log.info("Image router classified in %.1fs: %s", elapsed, text[:200])

    # Parse JSON from response (strip markdown fences if present)
    text = text.strip()
    if text.startswith("```"):
        text = re.sub(r"^```(?:json)?\s*", "", text)
        text = re.sub(r"\s*```$", "", text)

    try:
        result = json.loads(text)
        if result.get("intent") in ("tool", "vision"):
            return result
    except json.JSONDecodeError:
        log.warning("Image router: failed to parse LLM response as JSON: %s", text[:200])

    return None


# ──────────────────────────────────────────────────────────────────────
#  Model Resolution — ensure vision-capable model when needed
# ──────────────────────────────────────────────────────────────────────


def resolve_vision_model(
    current_model: str | None,
    model_override: str | None,
    config: dict | None = None,
) -> str | None:
    """If the effective model doesn't support vision, return a vision-capable override.

    Returns None if the current model is already vision-capable (no swap needed).
    """
    effective = model_override or current_model or ""
    if supports_vision(effective):
        return None

    config = config or {}
    aliases = config.get("skill_model_aliases", {})
    vision_model = aliases.get("vision")
    if vision_model and supports_vision(vision_model):
        log.info(
            "Image router: model %s lacks vision, swapping to %s",
            effective, vision_model,
        )
        return vision_model

    vision_chain = config.get("provider_chains", {}).get("vision", [])
    if vision_chain:
        log.info(
            "Image router: model %s lacks vision, will rely on vision provider chain",
            effective,
        )

    return aliases.get("capable") or None
