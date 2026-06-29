"""
Ghost TTS — Multi-provider text-to-speech tool.

Provider fallback chain:
  1. Edge TTS (free, no API key — via edge-tts Python package)
  2. OpenAI TTS (tts-1 / tts-1-hd)
  3. ElevenLabs (if API key configured)

Auto-summarizes long text (>1500 chars) before synthesis.
Saves audio to ~/.ghost/audio/ and returns file path.
"""

import json
import logging
import os
import time
import uuid
import requests
from pathlib import Path

log = logging.getLogger("quinely.tts")

GHOST_HOME = Path.home() / ".ghost"
AUDIO_DIR = GHOST_HOME / "audio"
AUDIO_DIR.mkdir(parents=True, exist_ok=True)

MAX_TEXT_CHARS = 1500
DEFAULT_VOICE = "en-US-MichelleNeural"

OPENAI_VOICES = ["alloy", "echo", "fable", "onyx", "nova", "shimmer"]
OPENAI_TTS_URL = "https://api.openai.com/v1/audio/speech"
ELEVENLABS_TTS_URL = "https://api.elevenlabs.io/v1/text-to-speech"
ELEVENLABS_DEFAULT_VOICE_ID = "pMsXgVXv3BLzUgSXRplE"


def _get_output_path(ext: str = "mp3") -> Path:
    ts = time.strftime("%Y%m%d_%H%M%S")
    return AUDIO_DIR / f"tts_{ts}_{uuid.uuid4().hex[:6]}.{ext}"


def _tts_edge(text: str, voice: str = DEFAULT_VOICE) -> str:
    """Synthesize speech using Microsoft Edge TTS (free, no API key)."""
    try:
        import asyncio
        import edge_tts
    except ImportError:
        raise RuntimeError("edge-tts not installed. Run: pip install edge-tts")

    output_path = _get_output_path("mp3")

    async def _generate():
        communicate = edge_tts.Communicate(text, voice)
        await communicate.save(str(output_path))

    try:
        loop = asyncio.get_event_loop()
        if loop.is_running():
            import concurrent.futures
            with concurrent.futures.ThreadPoolExecutor() as pool:
                future = pool.submit(asyncio.run, _generate())
                future.result(timeout=60)
        else:
            loop.run_until_complete(_generate())
    except RuntimeError:
        asyncio.run(_generate())

    if not output_path.exists() or output_path.stat().st_size == 0:
        raise RuntimeError("Edge TTS produced empty output")

    return str(output_path)


def _tts_openai(text: str, voice: str = "nova", model: str = "",
                api_key: str = "") -> str:
    """Synthesize speech using OpenAI TTS API."""
    if not api_key:
        raise RuntimeError("No OpenAI API key for TTS")

    if voice not in OPENAI_VOICES:
        voice = "nova"

    resp = requests.post(
        OPENAI_TTS_URL,
        headers={
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
        },
        json={
            "model": model,
            "input": text,
            "voice": voice,
            "response_format": "mp3",
        },
        timeout=60,
    )
    resp.raise_for_status()

    output_path = _get_output_path("mp3")
    output_path.write_bytes(resp.content)
    return str(output_path)


def _tts_elevenlabs(text: str, voice_id: str = ELEVENLABS_DEFAULT_VOICE_ID,
                    api_key: str = "", model_id: str = "") -> str:
    """Synthesize speech using ElevenLabs API."""
    if not api_key:
        raise RuntimeError("No ElevenLabs API key for TTS")

    resp = requests.post(
        f"{ELEVENLABS_TTS_URL}/{voice_id}",
        headers={
            "xi-api-key": api_key,
            "Content-Type": "application/json",
        },
        json={
            "text": text,
            "model_id": model_id or "eleven_multilingual_v2",
            "voice_settings": {
                "stability": 0.5,
                "similarity_boost": 0.75,
            },
        },
        timeout=60,
    )
    resp.raise_for_status()

    output_path = _get_output_path("mp3")
    output_path.write_bytes(resp.content)
    return str(output_path)


def _resolve_keys(auth_store=None) -> dict:
    """Resolve API keys from auth store and environment."""
    keys = {}

    openai_key = os.environ.get("OPENAI_API_KEY", "")
    if not openai_key and auth_store:
        try:
            openai_key = auth_store.get_api_key("openai") or ""
        except Exception:
            pass
    keys["openai"] = openai_key

    el_key = os.environ.get("ELEVENLABS_API_KEY", "") or os.environ.get("XI_API_KEY", "")
    keys["elevenlabs"] = el_key

    return keys


def text_to_speech(text: str, voice: str = "", provider: str = "",
                   auth_store=None, cfg: dict = None) -> dict:
    """Convert text to speech with multi-provider fallback.

    Returns: {"provider": str, "file": str, "duration_hint": str} or {"error": str}
    """
    if not text or not text.strip():
        return {"error": "No text provided"}

    try:
        from ghost_config_tool import get_tool_model
        openai_model = get_tool_model("tts_openai", cfg)
        el_model = get_tool_model("tts_elevenlabs", cfg)
    except ImportError:
        openai_model = "tts-1"
        el_model = "eleven_multilingual_v2"

    keys = _resolve_keys(auth_store)
    errors = []

    default_chain = (cfg or {}).get("provider_chains", {}).get(
        "tts", ["edge", "openai", "elevenlabs"])

    providers_to_try = []
    if provider:
        providers_to_try.append(provider)
    providers_to_try.extend(default_chain)
    seen = set()
    ordered = []
    for p in providers_to_try:
        if p not in seen:
            seen.add(p)
            ordered.append(p)

    for prov in ordered:
        try:
            if prov == "edge":
                v = voice or DEFAULT_VOICE
                path = _tts_edge(text, voice=v)
                return {"provider": "edge", "voice": v, "file": path}

            elif prov == "openai":
                if not keys.get("openai"):
                    continue
                v = voice if voice in OPENAI_VOICES else "nova"
                path = _tts_openai(text, voice=v, model=openai_model,
                                   api_key=keys["openai"])
                return {"provider": "openai", "voice": v, "file": path,
                        "model": openai_model}

            elif prov == "elevenlabs":
                if not keys.get("elevenlabs"):
                    continue
                path = _tts_elevenlabs(text, api_key=keys["elevenlabs"],
                                       model_id=el_model)
                return {"provider": "elevenlabs", "voice": "default",
                        "file": path, "model": el_model}

        except Exception as e:
            errors.append(f"{prov}: {e}")
            log.debug("TTS provider %s failed: %s", prov, e)
            continue

    return {"error": f"All TTS providers failed: {'; '.join(errors)}"}


def build_tts_tools(auth_store=None, cfg=None):
    """Build LLM-callable TTS tools for the tool registry."""

    def tts_exec(text, voice="", provider="", summarize_long=True):
        synth_text = text

        if summarize_long and len(text) > MAX_TEXT_CHARS:
            synth_text = text[:MAX_TEXT_CHARS] + "..."

        result = text_to_speech(
            text=synth_text, voice=voice, provider=provider,
            auth_store=auth_store, cfg=cfg,
        )

        if "error" in result:
            return f"TTS error: {result['error']}"

        try:
            from ghost_artifacts import auto_register
            auto_register(result["file"])
        except Exception:
            pass

        size_kb = Path(result["file"]).stat().st_size / 1024
        return json.dumps({
            "status": "ok",
            "provider": result["provider"],
            "voice": result.get("voice", "default"),
            "file": result["file"],
            "size_kb": round(size_kb, 1),
            "text_length": len(synth_text),
        })

    def tts_voices_exec():
        voices = {
            "edge": [
                "en-US-MichelleNeural", "en-US-GuyNeural",
                "en-US-JennyNeural", "en-US-AriaNeural",
                "en-GB-SoniaNeural", "en-GB-RyanNeural",
            ],
            "openai": OPENAI_VOICES,
            "elevenlabs": ["(use ElevenLabs dashboard to manage voices)"],
        }
        keys = _resolve_keys(auth_store)
        available = ["edge"]
        if keys.get("openai"):
            available.append("openai")
        if keys.get("elevenlabs"):
            available.append("elevenlabs")

        return json.dumps({
            "available_providers": available,
            "voices_by_provider": voices,
            "default_voice": DEFAULT_VOICE,
        })

    return [
        {
            "name": "text_to_speech",
            "description": (
                "Convert text to speech audio. Multi-provider fallback: "
                "Edge TTS (free), OpenAI TTS, ElevenLabs. Auto-truncates long text. "
                "Returns the file path of the generated audio."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "text": {
                        "type": "string",
                        "description": "Text to convert to speech",
                    },
                    "voice": {
                        "type": "string",
                        "description": "Voice name (provider-specific). Leave empty for default.",
                        "default": "",
                    },
                    "provider": {
                        "type": "string",
                        "description": "Preferred provider: edge, openai, elevenlabs. Leave empty for auto.",
                        "default": "",
                    },
                    "summarize_long": {
                        "type": "boolean",
                        "description": "Auto-truncate text longer than 1500 chars",
                        "default": True,
                    },
                },
                "required": ["text"],
            },
            "execute": tts_exec,
        },
        {
            "name": "tts_voices",
            "description": "List available TTS providers and their voice options.",
            "parameters": {
                "type": "object",
                "properties": {},
            },
            "execute": tts_voices_exec,
        },
    ]
