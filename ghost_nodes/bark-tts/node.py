"""
Bark TTS Node — expressive multilingual text-to-speech using Suno Bark.

Supports laughter [laughter], music notes, speaker presets, and 13+ languages.
"""

import io
import json
import logging
import time
from pathlib import Path

log = logging.getLogger("quinely.node.bark_tts")

_model = None
_processor = None
_current_small = None


def _ensure_bark(api, small=False):
    global _model, _processor, _current_small

    model_key = "bark-small" if small else "bark"
    if _model is not None and _current_small == small:
        api.resource_manager.touch(model_key)
        return _model, _processor

    if _model is not None and _current_small != small:
        old_key = "bark-small" if _current_small else "bark"
        api.release_gpu(old_key)
        _model = None
        _processor = None

    try:
        from transformers import AutoProcessor, BarkModel
        import torch
    except ImportError:
        raise RuntimeError(
            "transformers not installed. Run: pip install transformers torch scipy"
        )

    model_id = "suno/bark-small" if small else "suno/bark"
    vram = 2.0 if small else 4.0
    device = api.acquire_gpu(model_key, estimated_vram_gb=vram)
    dtype = torch.float16 if device == "cuda" else torch.float32

    api.log(f"Downloading & loading Bark ({model_id}) — first run may take a few minutes...")
    _processor = AutoProcessor.from_pretrained(model_id, cache_dir=str(api.models_dir), token=getattr(api, 'hf_token', None))
    api.log("Processor ready, loading model weights...")
    _model = BarkModel.from_pretrained(
        model_id, torch_dtype=dtype, cache_dir=str(api.models_dir),
        token=getattr(api, 'hf_token', None),
    )
    _model.to(device)
    _current_small = small
    api.log(f"Bark ready on {device}")
    return _model, _processor


def register(api):

    def execute_speak(text="", voice_preset="v2/en_speaker_6",
                      small_model=False, filename="", **_kw):
        if not text:
            return json.dumps({"status": "error", "error": "text is required"})

        try:
            import torch
            import numpy as np

            model, processor = _ensure_bark(api, small=small_model)

            inputs = processor(text, voice_preset=voice_preset)
            inputs = {k: v.to(model.device) for k, v in inputs.items()}

            api.log(f"Generating speech ({len(text)} chars)...")
            t0 = time.time()
            with torch.no_grad():
                audio_array = model.generate(**inputs)
            elapsed = time.time() - t0

            audio_array = audio_array.cpu().numpy().squeeze()
            sample_rate = model.generation_config.sample_rate

            try:
                import scipy.io.wavfile
                buf = io.BytesIO()
                scipy.io.wavfile.write(buf, rate=sample_rate, data=audio_array)
                audio_bytes = buf.getvalue()
                ext = ".wav"
            except ImportError:
                audio_bytes = audio_array.tobytes()
                ext = ".raw"

            ts = time.strftime("%Y%m%d_%H%M%S")
            fname = filename or f"bark_{ts}{ext}"

            path = api.save_media(
                data=audio_bytes, filename=fname, media_type="audio",
                prompt=text[:200],
                params={"voice_preset": voice_preset, "sample_rate": sample_rate},
                metadata={
                    "text": text[:200], "voice_preset": voice_preset,
                    "sample_rate": sample_rate, "elapsed_secs": round(elapsed, 2),
                },
            )
            return json.dumps({
                "status": "ok",
                "path": path,
                "sample_rate": sample_rate,
                "elapsed_secs": round(elapsed, 2),
                "voice_preset": voice_preset,
            })

        except Exception as e:
            return json.dumps({"status": "error", "error": str(e)[:500]})

    api.register_tool({
        "name": "bark_speak",
        "description": (
            "Generate expressive speech from text using Suno Bark (local). "
            "Supports multiple languages, laughter [laughter], hesitation, "
            "singing, and speaker presets. No API key needed.\n\n"
            "Voice presets: v2/en_speaker_0 to v2/en_speaker_9, "
            "v2/zh_speaker_0-9, v2/fr_speaker_0-9, v2/de_speaker_0-9, etc."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "text": {
                    "type": "string",
                    "description": "Text to speak. Use [laughter], [sighs], ♪ for effects.",
                },
                "voice_preset": {
                    "type": "string",
                    "description": "Voice preset (e.g. v2/en_speaker_6). Default: v2/en_speaker_6.",
                    "default": "v2/en_speaker_6",
                },
                "small_model": {
                    "type": "boolean",
                    "description": "Use smaller/faster model (lower quality). Default: false.",
                    "default": False,
                },
                "filename": {"type": "string", "description": "Output filename (optional)."},
            },
            "required": ["text"],
        },
        "execute": execute_speak,
    })
