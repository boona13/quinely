"""
MusicGen Node — generate music from text descriptions using Meta MusicGen.
"""

import io
import json
import logging
import time
from pathlib import Path

log = logging.getLogger("quinely.node.music_gen")

_model = None
_processor = None
_current_model_id = None


def _ensure_model(api, model_id=None):
    global _model, _processor, _current_model_id

    model_id = model_id or "facebook/musicgen-small"
    if _model is not None and _current_model_id == model_id:
        api.resource_manager.touch(model_id)
        return _model, _processor

    if _model is not None:
        api.release_gpu(_current_model_id)

    try:
        import torch
        from transformers import AutoProcessor, MusicgenForConditionalGeneration
    except ImportError:
        raise RuntimeError(
            "Required packages missing. Run: pip install torch transformers scipy"
        )

    vram_map = {"facebook/musicgen-small": 2.0, "facebook/musicgen-medium": 4.0, "facebook/musicgen-large": 8.0}
    vram = vram_map.get(model_id, 4.0)
    device = api.acquire_gpu(model_id, estimated_vram_gb=vram)
    dtype = torch.float16 if device == "cuda" else torch.float32

    api.log(f"Downloading & loading MusicGen — first run may take a few minutes...")
    api.log(f"Loading MusicGen ({model_id}) on {device}...")
    _processor = AutoProcessor.from_pretrained(model_id, cache_dir=str(api.models_dir), token=getattr(api, 'hf_token', None))

    # Fix transformers bug: MusicgenForConditionalGeneration.config_class is
    # incorrectly set to MusicgenDecoderConfig instead of MusicgenConfig
    from transformers.models.musicgen.configuration_musicgen import MusicgenConfig
    MusicgenForConditionalGeneration.config_class = MusicgenConfig

    _model = MusicgenForConditionalGeneration.from_pretrained(
        model_id, torch_dtype=dtype, cache_dir=str(api.models_dir),
        token=getattr(api, 'hf_token', None),
    )

    _model.to(device)
    _current_model_id = model_id
    api.log(f"Model loaded on {device}")
    api.log("MusicGen ready")
    return _model, _processor


def register(api):

    def execute_generate(prompt="", duration_secs=10, model="",
                         filename="", **_kw):
        if not prompt:
            return json.dumps({"status": "error", "error": "prompt is required"})

        try:
            import torch
            import scipy.io.wavfile

            model_obj, processor = _ensure_model(api, model_id=model or None)

            duration_secs = max(1, min(duration_secs, 30))
            max_new_tokens = int(duration_secs * 50)

            inputs = processor(text=[prompt], padding=True, return_tensors="pt")
            inputs = {k: v.to(model_obj.device) for k, v in inputs.items()}

            api.log(f"Generating {duration_secs}s of music...")
            t0 = time.time()
            with torch.no_grad():
                audio_values = model_obj.generate(**inputs, max_new_tokens=max_new_tokens)
            elapsed = time.time() - t0

            audio_array = audio_values[0, 0].cpu().numpy()
            sample_rate = model_obj.config.audio_encoder.sampling_rate

            buf = io.BytesIO()
            scipy.io.wavfile.write(buf, rate=sample_rate, data=audio_array)
            audio_bytes = buf.getvalue()

            ts = time.strftime("%Y%m%d_%H%M%S")
            fname = filename or f"music_{ts}.wav"

            path = api.save_media(
                data=audio_bytes, filename=fname, media_type="audio",
                prompt=prompt[:200],
                params={"duration_secs": duration_secs, "model": _current_model_id},
                metadata={
                    "prompt": prompt[:200], "duration_secs": duration_secs,
                    "model": _current_model_id, "sample_rate": sample_rate,
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
            return json.dumps({"status": "error", "error": str(e)[:500]})

    api.register_tool({
        "name": "generate_music",
        "description": (
            "Generate music from a text description using Meta MusicGen (local). "
            "Create royalty-free background music, soundtracks, and audio compositions. "
            "No API key needed."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "prompt": {"type": "string", "description": "Description of the music (e.g. 'upbeat electronic dance music with heavy bass')."},
                "duration_secs": {"type": "integer", "description": "Duration in seconds (1-30, default 10).", "default": 10},
                "model": {"type": "string", "description": "Model size: facebook/musicgen-small, medium, or large."},
                "filename": {"type": "string", "description": "Output filename (optional)."},
            },
            "required": ["prompt"],
        },
        "execute": execute_generate,
    })
