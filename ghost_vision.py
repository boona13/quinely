"""
Ghost Vision — Multi-provider image analysis tool.

Ghost can generate images (ghost_image_gen.py) but needs to SEE too.
This module adds multi-provider vision analysis with fallback:
  OpenAI (gpt-4o) → Google Gemini → Anthropic → Ollama (local)

Accepts: local file paths, http(s):// URLs (SSRF-protected), base64 data URIs.
"""

import base64
import json
import logging
import os
import re
import requests
from pathlib import Path

log = logging.getLogger("quinely.vision")

GHOST_HOME = Path.home() / ".ghost"
MAX_IMAGE_SIZE_MB = 20

_VISION_CONFIG_KEYS = {
    "openai": "vision_openai",
    "openrouter": "vision_openrouter",
    "google": "vision_gemini",
    "anthropic": "vision_anthropic",
    "ollama": "vision_ollama",
}

VISION_PROVIDERS = [
    {
        "id": "openai",
        "model": "gpt-4o",
        "env_key": "OPENAI_API_KEY",
        "base_url": "https://api.openai.com/v1/chat/completions",
        "format": "openai",
    },
    {
        "id": "openrouter",
        "model": "openai/gpt-4o",
        "env_key": "OPENROUTER_API_KEY",
        "base_url": "https://openrouter.ai/api/v1/chat/completions",
        "format": "openai",
    },
    {
        "id": "google",
        "model": "gemini-2.5-flash",
        "env_key": "GOOGLE_AI_API_KEY",
        "base_url": "https://generativelanguage.googleapis.com/v1beta",
        "format": "gemini",
    },
    {
        "id": "anthropic",
        "model": "claude-sonnet-4-20250514",
        "env_key": "ANTHROPIC_API_KEY",
        "base_url": "https://api.anthropic.com/v1/messages",
        "format": "anthropic",
    },
    {
        "id": "ollama",
        "model": "llava",
        "env_key": None,
        "base_url": "http://localhost:11434/api/chat",
        "format": "ollama",
    },
]


def _apply_model_overrides(providers: list[dict], cfg: dict | None) -> list[dict]:
    """Return a copy of providers with model IDs overridden from config."""
    if not cfg:
        return providers
    try:
        from ghost_config_tool import get_tool_model
    except ImportError:
        return providers
    out = []
    for p in providers:
        copy = dict(p)
        config_key = _VISION_CONFIG_KEYS.get(p["id"])
        if config_key:
            copy["model"] = get_tool_model(config_key, cfg)
        out.append(copy)
    return out


def _resolve_image(image_input: str) -> tuple[str, str]:
    """Resolve an image input to (base64_data, mime_type).

    Accepts:
      - Local file path
      - http(s):// URL (with SSRF protection)
      - data: URI (base64)
    """
    if image_input.startswith("data:"):
        match = re.match(r"data:(image/\w+);base64,(.+)", image_input)
        if match:
            return match.group(2), match.group(1)
        raise ValueError("Invalid data URI format")

    if image_input.startswith(("http://", "https://")):
        try:
            from ghost_web_fetch import validate_url
            validate_url(image_input)
        except ImportError:
            pass
        except ValueError as e:
            raise ValueError(f"SSRF blocked: {e}")

        resp = requests.get(
            image_input,
            headers={"User-Agent": "Ghost-Vision/1.0"},
            timeout=30,
            stream=True,
        )
        resp.raise_for_status()
        content_type = resp.headers.get("Content-Type", "image/png")
        if not content_type.startswith("image/"):
            raise ValueError(f"URL did not return an image: {content_type}")
        data = resp.content
        if len(data) > MAX_IMAGE_SIZE_MB * 1024 * 1024:
            raise ValueError(f"Image too large ({len(data) // 1024 // 1024}MB > {MAX_IMAGE_SIZE_MB}MB)")
        mime = content_type.split(";")[0].strip()
        return base64.b64encode(data).decode(), mime

    path = Path(image_input).expanduser().resolve()
    if not path.exists():
        raise FileNotFoundError(f"Image not found: {image_input}")
    if path.stat().st_size > MAX_IMAGE_SIZE_MB * 1024 * 1024:
        raise ValueError(f"Image too large ({path.stat().st_size // 1024 // 1024}MB)")

    suffix = path.suffix.lower()
    mime_map = {
        ".png": "image/png", ".jpg": "image/jpeg", ".jpeg": "image/jpeg",
        ".gif": "image/gif", ".webp": "image/webp", ".bmp": "image/bmp",
        ".svg": "image/svg+xml",
    }
    mime = mime_map.get(suffix, "image/png")
    return base64.b64encode(path.read_bytes()).decode(), mime


def _get_api_key(provider: dict, auth_store=None) -> str:
    """Resolve API key from auth store, environment, or config."""
    pid = provider["id"]
    if auth_store:
        try:
            key = auth_store.get_api_key(pid)
            if key:
                return key
        except Exception:
            pass
    env_key = provider.get("env_key")
    if env_key:
        return os.environ.get(env_key, "")
    return ""


def _analyze_openai(provider: dict, b64: str, mime: str, prompt: str,
                    api_key: str, max_tokens: int = 1024) -> str:
    """Call OpenAI-compatible vision API."""
    resp = requests.post(
        provider["base_url"],
        headers={
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
        },
        json={
            "model": provider["model"],
            "messages": [{
                "role": "user",
                "content": [
                    {"type": "text", "text": prompt},
                    {"type": "image_url", "image_url": {
                        "url": f"data:{mime};base64,{b64}"
                    }},
                ],
            }],
            "max_tokens": max_tokens,
        },
        timeout=60,
    )
    resp.raise_for_status()
    return resp.json()["choices"][0]["message"]["content"].strip()


def _analyze_gemini(provider: dict, b64: str, mime: str, prompt: str,
                    api_key: str, max_tokens: int = 1024) -> str:
    """Call Google Gemini vision API."""
    url = f"{provider['base_url']}/models/{provider['model']}:generateContent?key={api_key}"
    resp = requests.post(
        url,
        headers={"Content-Type": "application/json"},
        json={
            "contents": [{
                "parts": [
                    {"text": prompt},
                    {"inline_data": {"mime_type": mime, "data": b64}},
                ],
            }],
            "generationConfig": {"maxOutputTokens": max_tokens},
        },
        timeout=60,
    )
    resp.raise_for_status()
    data = resp.json()
    candidates = data.get("candidates", [])
    if candidates:
        parts = candidates[0].get("content", {}).get("parts", [])
        return "".join(p.get("text", "") for p in parts).strip()
    return "No response from Gemini"


def _analyze_anthropic(provider: dict, b64: str, mime: str, prompt: str,
                       api_key: str, max_tokens: int = 1024) -> str:
    """Call Anthropic Messages API with vision."""
    media_type = mime
    if media_type == "image/jpg":
        media_type = "image/jpeg"
    resp = requests.post(
        provider["base_url"],
        headers={
            "x-api-key": api_key,
            "anthropic-version": "2023-06-01",
            "Content-Type": "application/json",
        },
        json={
            "model": provider["model"],
            "max_tokens": max_tokens,
            "messages": [{
                "role": "user",
                "content": [
                    {"type": "image", "source": {
                        "type": "base64", "media_type": media_type, "data": b64,
                    }},
                    {"type": "text", "text": prompt},
                ],
            }],
        },
        timeout=60,
    )
    resp.raise_for_status()
    data = resp.json()
    content = data.get("content", [])
    return "".join(c.get("text", "") for c in content if c.get("type") == "text").strip()


def _analyze_ollama(provider: dict, b64: str, mime: str, prompt: str,
                    **kwargs) -> str:
    """Call Ollama local vision model."""
    resp = requests.post(
        provider["base_url"],
        json={
            "model": provider["model"],
            "messages": [{
                "role": "user",
                "content": prompt,
                "images": [b64],
            }],
            "stream": False,
        },
        timeout=120,
    )
    resp.raise_for_status()
    data = resp.json()
    return (data.get("message", {}).get("content") or "").strip()


_DISPATCH = {
    "openai": _analyze_openai,
    "gemini": _analyze_gemini,
    "anthropic": _analyze_anthropic,
    "ollama": _analyze_ollama,
}


def analyze_image(image: str, prompt: str = "Describe this image in detail.",
                  auth_store=None, preferred_provider: str = None,
                  max_tokens: int = 1024, cfg: dict = None) -> dict:
    """Analyze an image with multi-provider fallback.

    Returns: {"provider": str, "model": str, "analysis": str} or {"error": str}
    """
    try:
        b64, mime = _resolve_image(image)
    except Exception as e:
        return {"error": f"Failed to load image: {e}"}

    providers_to_try = _apply_model_overrides(VISION_PROVIDERS, cfg)

    chain = (cfg or {}).get("provider_chains", {}).get("vision")
    if chain:
        id_order = {pid: i for i, pid in enumerate(chain)}
        providers_to_try.sort(key=lambda p: id_order.get(p["id"], 999))

    if preferred_provider:
        providers_to_try.sort(key=lambda p: 0 if p["id"] == preferred_provider else 1)

    errors = []
    for prov in providers_to_try:
        api_key = _get_api_key(prov, auth_store)
        if not api_key and prov["id"] != "ollama":
            continue

        handler = _DISPATCH.get(prov["format"])
        if not handler:
            continue

        try:
            kwargs = {"provider": prov, "b64": b64, "mime": mime,
                      "prompt": prompt, "api_key": api_key, "max_tokens": max_tokens}
            result = handler(**kwargs)
            if result:
                log.info("Vision analysis via %s:%s", prov["id"], prov["model"])
                return {
                    "provider": prov["id"],
                    "model": prov["model"],
                    "analysis": result,
                }
        except Exception as e:
            errors.append(f"{prov['id']}: {e}")
            log.debug("Vision provider %s failed: %s", prov["id"], e)
            continue

    return {"error": f"All vision providers failed: {'; '.join(errors)}"}


def build_vision_tools(auth_store=None, cfg=None):
    """Build LLM-callable vision tools for the tool registry."""

    def image_analyze_exec(image, prompt="Describe this image in detail.",
                           max_tokens=1024):
        result = analyze_image(
            image=image, prompt=prompt,
            auth_store=auth_store, max_tokens=max_tokens, cfg=cfg,
        )
        if "error" in result:
            return f"Vision error: {result['error']}"
        return (
            f"[Analyzed via {result['provider']}:{result['model']}]\n\n"
            f"{result['analysis']}"
        )

    def screenshot_analyze_exec(prompt="What's shown in this screenshot?"):
        screen_dir = GHOST_HOME / "screenshots"
        if not screen_dir.exists():
            return "No screenshots directory found"

        screenshots = sorted(screen_dir.glob("*.png"), key=lambda p: p.stat().st_mtime, reverse=True)
        if not screenshots:
            return "No screenshots available"

        latest = str(screenshots[0])
        result = analyze_image(
            image=latest, prompt=prompt,
            auth_store=auth_store, cfg=cfg,
        )
        if "error" in result:
            return f"Screenshot analysis error: {result['error']}"
        return (
            f"[Screenshot: {Path(latest).name} | via {result['provider']}]\n\n"
            f"{result['analysis']}"
        )

    return [
        {
            "name": "image_analyze",
            "description": (
                "Analyze an image using vision AI. Accepts local file paths, "
                "URLs (http/https), or base64 data URIs. Multi-provider fallback: "
                "OpenAI, Gemini, Anthropic, Ollama. Use for understanding screenshots, "
                "documents, diagrams, charts, photos, or any visual content."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "image": {
                        "type": "string",
                        "description": "Image source: file path, URL, or data:image/...;base64,... URI",
                    },
                    "prompt": {
                        "type": "string",
                        "description": "What to analyze or ask about the image",
                        "default": "Describe this image in detail.",
                    },
                    "max_tokens": {
                        "type": "integer",
                        "description": "Max tokens for the analysis response",
                        "default": 1024,
                    },
                },
                "required": ["image"],
            },
            "execute": image_analyze_exec,
        },
        {
            "name": "screenshot_analyze",
            "description": (
                "Analyze the most recent screenshot in Ghost's screenshot directory. "
                "Useful for understanding what's currently on screen without needing "
                "the file path."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "prompt": {
                        "type": "string",
                        "description": "What to analyze in the screenshot",
                        "default": "What's shown in this screenshot?",
                    },
                },
            },
            "execute": screenshot_analyze_exec,
        },
    ]
