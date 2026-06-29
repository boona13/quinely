"""
Whisper STT Node — local speech-to-text transcription using OpenAI Whisper.

Supports 99 languages, multiple model sizes, and word-level timestamps.
"""

import json
import logging
import time
from pathlib import Path

log = logging.getLogger("quinely.node.whisper_stt")

_model = None
_current_size = None


def _get_model(api, model_size="base"):
    global _model, _current_size

    if _model is not None and _current_size == model_size:
        api.resource_manager.touch(f"whisper-{model_size}")
        return _model

    if _model is not None:
        api.release_gpu(f"whisper-{_current_size}")
        _model = None

    try:
        import whisper
    except ImportError:
        raise RuntimeError("openai-whisper not installed. Run: pip install openai-whisper")

    vram_map = {"tiny": 0.5, "base": 0.5, "small": 1.0, "medium": 2.0, "large": 3.0, "large-v3": 3.0}
    vram = vram_map.get(model_size, 1.0)
    device = api.acquire_gpu(f"whisper-{model_size}", estimated_vram_gb=vram)

    api.log(f"Downloading & loading Whisper — first run may take a few minutes...")
    api.log(f"Loading Whisper {model_size} on {device}...")
    _model = whisper.load_model(model_size, device=device, download_root=str(api.models_dir))
    _current_size = model_size
    api.log(f"Model loaded on {device}")
    return _model


def register(api):

    def execute_transcribe(audio_path="", model_size="base", language="",
                           task="transcribe", **_kw):
        if not audio_path:
            return json.dumps({"status": "error", "error": "audio_path is required"})
        if not Path(audio_path).exists():
            return json.dumps({"status": "error", "error": f"File not found: {audio_path}"})

        try:
            model = _get_model(api, model_size=model_size)

            options = {"task": task}
            if language:
                options["language"] = language

            api.log("Transcribing audio...")
            api.log(f"Transcribing {audio_path} ({model_size})...")
            t0 = time.time()
            result = model.transcribe(audio_path, **options)
            elapsed = time.time() - t0

            text = result.get("text", "").strip()
            detected_lang = result.get("language", "unknown")

            segments = []
            for seg in result.get("segments", []):
                segments.append({
                    "start": round(seg["start"], 2),
                    "end": round(seg["end"], 2),
                    "text": seg["text"].strip(),
                })

            return json.dumps({
                "status": "ok",
                "text": text,
                "language": detected_lang,
                "segments": segments[:50],
                "model_size": model_size,
                "elapsed_secs": round(elapsed, 2),
            })

        except Exception as e:
            return json.dumps({"status": "error", "error": str(e)[:500]})

    api.register_tool({
        "name": "transcribe_audio",
        "description": (
            "Transcribe audio to text locally using OpenAI Whisper. "
            "Supports 99 languages, auto-detection, and translation to English. "
            "No API key or internet needed — runs entirely on your machine."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "audio_path": {
                    "type": "string",
                    "description": "Path to audio file (mp3, wav, m4a, flac, etc.)",
                },
                "model_size": {
                    "type": "string",
                    "enum": ["tiny", "base", "small", "medium", "large", "large-v3"],
                    "description": "Model size (default: base). Larger = more accurate but slower.",
                    "default": "base",
                },
                "language": {
                    "type": "string",
                    "description": "ISO language code (e.g. 'en', 'ar', 'zh'). Auto-detected if empty.",
                },
                "task": {
                    "type": "string",
                    "enum": ["transcribe", "translate"],
                    "description": "transcribe (keep original language) or translate (to English).",
                    "default": "transcribe",
                },
            },
            "required": ["audio_path"],
        },
        "execute": execute_transcribe,
    })
