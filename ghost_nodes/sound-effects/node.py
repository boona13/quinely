"""
Sound Effects Node — generate any sound effect from text using AudioLDM2.

Create high-quality sound effects, ambient sounds, and foley audio:
- Explosions, gunshots, whooshes
- Rain, thunder, wind, ocean waves
- Footsteps, door creaks, glass breaking
- Sci-fi lasers, engine hums, alarms
- Animal sounds, crowd noise, city ambience
"""

import json
import logging
import time
import io
from pathlib import Path

log = logging.getLogger("quinely.node.sound_effects")

_pipe = None
_current_model = None


def _ensure_pipeline(api, model_variant="base"):
    global _pipe, _current_model

    model_id = "cvssp/audioldm2-music" if model_variant == "music" else "cvssp/audioldm2"
    if _pipe is not None and _current_model == model_id:
        api.resource_manager.touch(model_id)
        return _pipe

    if _pipe is not None:
        api.release_gpu(_current_model)
        _pipe = None

    try:
        import torch
        from diffusers import AudioLDM2Pipeline
    except ImportError:
        raise RuntimeError("Required: pip install torch diffusers transformers scipy")

    device = api.acquire_gpu(model_id, estimated_vram_gb=4.0)
    dtype = torch.float16 if device == "cuda" else torch.float32

    api.log(f"Loading AudioLDM2 ({model_variant}) — first run downloads ~3GB...")
    _pipe = AudioLDM2Pipeline.from_pretrained(
        model_id, torch_dtype=dtype, cache_dir=api.models_dir,
        token=getattr(api, 'hf_token', None),
    )
    _pipe.to(device)

    lm = getattr(_pipe, 'language_model', None)
    if lm is not None:
        from transformers import GenerationMixin
        for method_name in ('_get_initial_cache_position', '_update_model_kwargs_for_generation'):
            if not hasattr(lm, method_name) and hasattr(GenerationMixin, method_name):
                import types
                fn = getattr(GenerationMixin, method_name)
                setattr(lm, method_name, types.MethodType(fn, lm))

    _current_model = model_id
    api.log(f"AudioLDM2 loaded on {device}")
    return _pipe


def register(api):

    def execute_sound_fx(prompt="", duration_secs=5, model_variant="base",
                          negative_prompt="", steps=50,
                          filename="", **_kw):
        if not prompt:
            return json.dumps({"status": "error", "error": "prompt is required"})

        try:
            import torch
            import scipy.io.wavfile
            import numpy as np

            pipe = _ensure_pipeline(api, model_variant=model_variant)

            duration_secs = max(1, min(int(duration_secs), 30))
            audio_length = int(duration_secs * 16000)

            neg = negative_prompt or "low quality, distorted, noise"

            api.log(f"Generating {duration_secs}s sound: '{prompt[:60]}'...")
            t0 = time.time()

            result = pipe(
                prompt=prompt,
                negative_prompt=neg,
                num_inference_steps=min(steps, 100),
                audio_length_in_s=duration_secs,
            )

            audio = result.audios[0]
            elapsed = time.time() - t0

            if isinstance(audio, np.ndarray):
                audio_data = audio
            else:
                audio_data = np.array(audio)

            if audio_data.dtype == np.float32 or audio_data.dtype == np.float64:
                audio_data = np.clip(audio_data, -1.0, 1.0)
                audio_int16 = (audio_data * 32767).astype(np.int16)
            else:
                audio_int16 = audio_data

            sample_rate = 16000
            buf = io.BytesIO()
            scipy.io.wavfile.write(buf, rate=sample_rate, data=audio_int16)
            audio_bytes = buf.getvalue()

            ts = time.strftime("%Y%m%d_%H%M%S")
            fname = filename or f"sfx_{ts}.wav"

            path = api.save_media(
                data=audio_bytes, filename=fname, media_type="audio",
                prompt=prompt[:200],
                params={"duration_secs": duration_secs, "model": _current_model},
                metadata={
                    "prompt": prompt[:200], "duration_secs": duration_secs,
                    "model": _current_model, "sample_rate": sample_rate,
                    "elapsed_secs": round(elapsed, 2),
                },
            )
            return json.dumps({
                "status": "ok", "path": path,
                "duration_secs": duration_secs,
                "sample_rate": sample_rate,
                "elapsed_secs": round(elapsed, 2),
            })

        except Exception as e:
            log.error("Sound FX error: %s", e, exc_info=True)
            return json.dumps({"status": "error", "error": str(e)[:500]})

    api.register_tool({
        "name": "generate_sound_effect",
        "description": (
            "Generate sound effects from text descriptions using AudioLDM2 (local). "
            "Create explosions, rain, footsteps, sci-fi lasers, animal sounds, "
            "ambient noise, and much more. Perfect for videos, games, and creative "
            "projects. No API key needed.\n\n"
            "Examples: 'thunder and heavy rain', 'spaceship engine humming', "
            "'crowd cheering in a stadium', 'cat purring softly'"
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "prompt": {"type": "string", "description": "Description of the sound to generate."},
                "duration_secs": {"type": "integer", "description": "Duration in seconds (1-30, default: 5).", "default": 5},
                "model_variant": {
                    "type": "string", "enum": ["base", "music"],
                    "description": "Use 'base' for sound effects, 'music' for musical sounds. Default: base.",
                    "default": "base",
                },
                "negative_prompt": {"type": "string", "description": "What to avoid (optional)."},
                "steps": {"type": "integer", "description": "Inference steps (default: 50, max: 100).", "default": 50},
                "filename": {"type": "string", "description": "Output filename (optional)."},
            },
            "required": ["prompt"],
        },
        "execute": execute_sound_fx,
    })
