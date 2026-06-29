"""
Florence Vision Node — image understanding using vision-language models.

Primary: Microsoft Florence-2 (requires transformers 4.x).
Fallback: Salesforce BLIP (works with transformers 5.x).
Supports captioning, OCR, object detection, and more.
"""

import json
import logging
import time
from pathlib import Path

log = logging.getLogger("quinely.node.florence_vision")

_model = None
_processor = None
_current_model_id = None
_backend = None  # "florence" or "blip"

TASK_PROMPTS = {
    "caption": "<CAPTION>",
    "detailed_caption": "<DETAILED_CAPTION>",
    "more_detailed_caption": "<MORE_DETAILED_CAPTION>",
    "ocr": "<OCR>",
    "ocr_with_region": "<OCR_WITH_REGION>",
    "object_detection": "<OD>",
    "dense_region_caption": "<DENSE_REGION_CAPTION>",
    "region_proposal": "<REGION_PROPOSAL>",
}

BLIP_MODELS = {
    "caption": "Salesforce/blip-image-captioning-large",
    "default": "Salesforce/blip-image-captioning-base",
}


def _try_load_florence(api, model_id, device, dtype):
    """Attempt to load Florence-2. Returns (model, processor) or raises."""
    import torch
    from transformers import AutoProcessor, AutoModelForCausalLM

    processor = AutoProcessor.from_pretrained(
        model_id, trust_remote_code=True, cache_dir=str(api.models_dir),
        token=getattr(api, 'hf_token', None))
    model = AutoModelForCausalLM.from_pretrained(
        model_id, torch_dtype=dtype, trust_remote_code=True,
        cache_dir=str(api.models_dir),
        token=getattr(api, 'hf_token', None))
    model.to(device)
    return model, processor


def _load_blip_fallback(api, device, dtype):
    """Load BLIP as a fallback when Florence-2 is incompatible."""
    import torch
    from transformers import BlipProcessor, BlipForConditionalGeneration

    blip_id = BLIP_MODELS["caption"]
    api.log(f"Loading BLIP fallback ({blip_id})...")
    processor = BlipProcessor.from_pretrained(blip_id, cache_dir=str(api.models_dir), token=getattr(api, 'hf_token', None))
    model = BlipForConditionalGeneration.from_pretrained(
        blip_id, torch_dtype=dtype, cache_dir=str(api.models_dir),
        token=getattr(api, 'hf_token', None))
    model.to(device)
    return model, processor, blip_id


def _ensure_model(api, model_id=None):
    global _model, _processor, _current_model_id, _backend

    model_id = model_id or "microsoft/Florence-2-large"
    if _model is not None and _current_model_id == model_id:
        api.resource_manager.touch(model_id)
        return _model, _processor

    if _model is not None:
        api.release_gpu(_current_model_id)

    try:
        import torch
    except ImportError:
        raise RuntimeError("PyTorch not installed. Run: pip install transformers torch")

    vram = getattr(getattr(api, 'manifest', None), 'estimated_vram_gb', 2.0)
    device = api.acquire_gpu(model_id, estimated_vram_gb=vram)
    dtype = torch.float16 if device == "cuda" else torch.float32

    api.log(f"Downloading & loading vision model — first run may take a few minutes...")

    try:
        _model, _processor = _try_load_florence(api, model_id, device, dtype)
        _current_model_id = model_id
        _backend = "florence"
        api.log(f"Florence-2 loaded on {device}")
    except Exception as florence_err:
        log.warning("Florence-2 failed (%s), falling back to BLIP", florence_err)
        api.log(f"Florence-2 unavailable (transformers compatibility), loading BLIP fallback...")
        try:
            _model, _processor, blip_id = _load_blip_fallback(api, device, dtype)
            _current_model_id = blip_id
            _backend = "blip"
            api.log(f"BLIP loaded on {device}")
        except Exception as blip_err:
            raise RuntimeError(
                f"Both Florence-2 and BLIP failed. Florence: {florence_err}. BLIP: {blip_err}"
            )

    return _model, _processor


def _run_florence(model, processor, image, task_prompt, text_input=""):
    """Run inference with Florence-2."""
    import torch

    prompt = task_prompt + (text_input or "")
    inputs = processor(text=prompt, images=image, return_tensors="pt")
    inputs = {k: v.to(model.device) for k, v in inputs.items()}

    with torch.no_grad():
        generated_ids = model.generate(**inputs, max_new_tokens=1024, num_beams=3)

    generated_text = processor.batch_decode(generated_ids, skip_special_tokens=False)[0]
    parsed = processor.post_process_generation(
        generated_text, task=task_prompt,
        image_size=(image.width, image.height),
    )
    return parsed.get(task_prompt, parsed)


def _run_blip(model, processor, image, task="caption", text_input=""):
    """Run inference with BLIP."""
    import torch

    if text_input:
        inputs = processor(image, text_input, return_tensors="pt")
    else:
        inputs = processor(image, return_tensors="pt")
    inputs = {k: v.to(model.device) for k, v in inputs.items()}

    with torch.no_grad():
        out = model.generate(**inputs, max_new_tokens=200)

    caption = processor.decode(out[0], skip_special_tokens=True)
    return caption


def register(api):

    def execute_analyze(image_path="", task="caption", model="",
                        text_input="", **_kw):
        if not image_path:
            return json.dumps({"status": "error", "error": "image_path is required"})
        if not Path(image_path).exists():
            return json.dumps({"status": "error", "error": f"File not found: {image_path}"})

        task_prompt = TASK_PROMPTS.get(task, TASK_PROMPTS["caption"])

        try:
            from PIL import Image

            model_obj, processor = _ensure_model(api, model_id=model or None)
            image = Image.open(image_path).convert("RGB")

            api.log(f"Analyzing image ({task})...")
            t0 = time.time()

            if _backend == "florence":
                result_data = _run_florence(model_obj, processor, image, task_prompt, text_input)
            else:
                if task not in ("caption", "detailed_caption", "more_detailed_caption"):
                    api.log(f"BLIP fallback only supports captioning, not '{task}'. Using caption.")
                result_data = _run_blip(model_obj, processor, image, task, text_input)

            elapsed = time.time() - t0

            return json.dumps({
                "status": "ok",
                "task": task,
                "result": result_data,
                "model": _current_model_id,
                "backend": _backend,
                "elapsed_secs": round(elapsed, 2),
            }, default=str)

        except Exception as e:
            return json.dumps({"status": "error", "error": str(e)[:500]})

    api.register_tool({
        "name": "florence_analyze",
        "description": (
            "Analyze an image using a vision-language model (local). "
            "Supports: caption, detailed_caption, ocr, ocr_with_region, "
            "object_detection, dense_region_caption, region_proposal.\n"
            "Uses Florence-2 when available, falls back to BLIP. "
            "No API key needed — runs entirely on your machine."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "image_path": {"type": "string", "description": "Path to image file."},
                "task": {
                    "type": "string",
                    "enum": list(TASK_PROMPTS.keys()),
                    "description": "Analysis task (default: caption).",
                    "default": "caption",
                },
                "model": {"type": "string", "description": "HuggingFace model ID (optional)."},
                "text_input": {"type": "string", "description": "Additional text input for grounding tasks."},
            },
            "required": ["image_path"],
        },
        "execute": execute_analyze,
    })
