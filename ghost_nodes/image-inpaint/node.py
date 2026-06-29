"""
Image Inpainting Node — magically remove or replace objects in images.

Uses Stable Diffusion Inpainting to fill masked regions with AI-generated content.
Can erase objects or replace them with something else via text prompts.
"""

import json
import logging
import time
import io
from pathlib import Path

log = logging.getLogger("quinely.node.image_inpaint")

_pipe = None
_current_model = None

MODELS = {
    "sd2": "stabilityai/stable-diffusion-2-inpainting",
    # RunwayML removed their HF repos in 2024; use the community-maintained mirror.
    "sd15": "stable-diffusion-v1-5/stable-diffusion-inpainting",
}


def _ensure_pipeline(api, model_key="sd15"):
    global _pipe, _current_model

    model_id = MODELS.get(model_key, MODELS["sd15"])
    if _pipe is not None and _current_model == model_id:
        api.resource_manager.touch(model_id)
        return _pipe

    if _pipe is not None:
        api.release_gpu(_current_model)
        _pipe = None

    try:
        import torch
        from diffusers import StableDiffusionInpaintPipeline
    except ImportError:
        raise RuntimeError("Required: pip install torch diffusers transformers accelerate Pillow")

    device = api.acquire_gpu(model_id, estimated_vram_gb=4.0)
    dtype = torch.float16 if device == "cuda" else torch.float32

    api.log(f"Loading inpainting model ({model_id})...")
    _pipe = StableDiffusionInpaintPipeline.from_pretrained(
        model_id, torch_dtype=dtype, cache_dir=api.models_dir,
        token=getattr(api, 'hf_token', None),
    )
    _pipe.to(device)
    try:
        _pipe.enable_attention_slicing()
    except Exception:
        pass
    _current_model = model_id
    api.log(f"Inpainting model loaded on {device}")
    return _pipe


def _create_simple_mask(image, region="center"):
    """Create a simple mask for common use cases."""
    from PIL import Image, ImageDraw
    w, h = image.size
    mask = Image.new("L", (w, h), 0)
    draw = ImageDraw.Draw(mask)

    if region == "center":
        cx, cy = w // 2, h // 2
        r = min(w, h) // 4
        draw.ellipse([cx - r, cy - r, cx + r, cy + r], fill=255)
    elif region == "top":
        draw.rectangle([0, 0, w, h // 3], fill=255)
    elif region == "bottom":
        draw.rectangle([0, 2 * h // 3, w, h], fill=255)
    elif region == "left":
        draw.rectangle([0, 0, w // 3, h], fill=255)
    elif region == "right":
        draw.rectangle([2 * w // 3, 0, w, h], fill=255)
    else:
        cx, cy = w // 2, h // 2
        r = min(w, h) // 4
        draw.ellipse([cx - r, cy - r, cx + r, cy + r], fill=255)

    return mask


def register(api):

    def execute_inpaint(image_path="", mask_path="", prompt="",
                        mask_region="", model="sd15",
                        steps=30, strength=0.8,
                        filename="", **_kw):
        if not image_path:
            return json.dumps({"status": "error", "error": "image_path is required"})
        if not Path(image_path).exists():
            return json.dumps({"status": "error", "error": f"File not found: {image_path}"})

        try:
            from PIL import Image

            pipe = _ensure_pipeline(api, model_key=model)
            image = Image.open(image_path).convert("RGB")
            image = image.resize(((image.width // 8) * 8, (image.height // 8) * 8))

            if mask_path and Path(mask_path).exists():
                mask = Image.open(mask_path).convert("L")
                mask = mask.resize(image.size)
            elif mask_region:
                mask = _create_simple_mask(image, region=mask_region)
            else:
                mask = _create_simple_mask(image, region="center")

            fill_prompt = prompt or "seamless natural background, high quality"

            api.log(f"Inpainting {image.size[0]}x{image.size[1]}, {steps} steps...")
            t0 = time.time()

            result = pipe(
                prompt=fill_prompt,
                image=image,
                mask_image=mask,
                num_inference_steps=steps,
                strength=strength,
            ).images[0]

            elapsed = time.time() - t0

            buf = io.BytesIO()
            result.save(buf, format="PNG")

            ts = time.strftime("%Y%m%d_%H%M%S")
            fname = filename or f"inpaint_{ts}.png"

            path = api.save_media(
                data=buf.getvalue(), filename=fname, media_type="image",
                prompt=fill_prompt[:200],
                params={"model": _current_model, "steps": steps, "strength": strength},
                metadata={
                    "source": str(image_path), "prompt": fill_prompt[:200],
                    "model": _current_model, "steps": steps,
                    "elapsed_secs": round(elapsed, 2),
                },
            )
            return json.dumps({
                "status": "ok", "path": path,
                "model": _current_model,
                "elapsed_secs": round(elapsed, 2),
            })

        except Exception as e:
            log.error("Inpaint error: %s", e, exc_info=True)
            return json.dumps({"status": "error", "error": str(e)[:500]})

    api.register_tool({
        "name": "inpaint_image",
        "description": (
            "Remove or replace objects in an image using AI inpainting (local). "
            "Provide an image and either a mask image (white=edit area) or a "
            "region name (center/top/bottom/left/right). Optionally describe "
            "what to fill the area with. No API key needed."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "image_path": {"type": "string", "description": "Path to the input image."},
                "mask_path": {"type": "string", "description": "Path to mask image (white=area to inpaint). Optional."},
                "prompt": {"type": "string", "description": "What to fill the masked area with (e.g. 'a beautiful garden')."},
                "mask_region": {
                    "type": "string", "enum": ["center", "top", "bottom", "left", "right"],
                    "description": "Quick mask region if no mask_path provided.",
                },
                "model": {"type": "string", "enum": ["sd15", "sd2"], "description": "Model variant. Default: sd15.", "default": "sd15"},
                "steps": {"type": "integer", "description": "Inference steps (default: 30).", "default": 30},
                "strength": {"type": "number", "description": "Inpainting strength 0-1 (default: 0.8).", "default": 0.8},
                "filename": {"type": "string", "description": "Output filename (optional)."},
            },
            "required": ["image_path"],
        },
        "execute": execute_inpaint,
    })
