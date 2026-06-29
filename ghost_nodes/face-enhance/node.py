"""
Face Enhancement Node — restore and enhance faces in photos.

Uses Swin2SR super-resolution model from transformers for high-quality
face and image restoration. Falls back to Pillow-based enhancement if
the model is unavailable.
"""

import json
import logging
import time
import io
from pathlib import Path

log = logging.getLogger("quinely.node.face_enhance")

_model = None
_processor = None


def _ensure_model(api):
    global _model, _processor
    if _model is not None:
        api.resource_manager.touch("swin2sr")
        return _model, _processor

    try:
        import torch
        from transformers import Swin2SRForImageSuperResolution, AutoImageProcessor
    except ImportError:
        return None, None

    model_id = "caidas/swin2SR-classical-sr-x2-64"
    device = api.acquire_gpu("swin2sr", estimated_vram_gb=0.5)

    api.log("Loading Swin2SR super-resolution model...")
    _processor = AutoImageProcessor.from_pretrained(model_id, cache_dir=api.models_dir, token=getattr(api, 'hf_token', None))
    _model = Swin2SRForImageSuperResolution.from_pretrained(model_id, cache_dir=api.models_dir, token=getattr(api, 'hf_token', None))
    _model.to(device)
    _model.eval()
    api.log(f"Swin2SR loaded on {device}")
    return _model, _processor


def _enhance_with_pillow(image, factor=2):
    """Pillow-based enhancement fallback with multi-pass sharpening."""
    from PIL import ImageEnhance, ImageFilter

    w, h = image.size
    upscaled = image.resize((w * factor, h * factor), resample=3)

    upscaled = upscaled.filter(ImageFilter.DETAIL)
    upscaled = upscaled.filter(ImageFilter.SMOOTH_MORE)
    enhancer = ImageEnhance.Sharpness(upscaled)
    upscaled = enhancer.enhance(1.6)
    enhancer = ImageEnhance.Contrast(upscaled)
    upscaled = enhancer.enhance(1.1)
    enhancer = ImageEnhance.Color(upscaled)
    upscaled = enhancer.enhance(1.05)

    return upscaled, "pillow"


def register(api):

    def execute_enhance(image_path="", upscale=2, filename="", **_kw):
        if not image_path:
            return json.dumps({"status": "error", "error": "image_path is required"})
        if not Path(image_path).exists():
            return json.dumps({"status": "error", "error": f"File not found: {image_path}"})

        try:
            import torch
            import numpy as np
            from PIL import Image

            image = Image.open(image_path).convert("RGB")
            upscale = max(1, min(int(upscale), 4))

            api.log(f"Enhancing {image.size[0]}x{image.size[1]} image...")
            t0 = time.time()

            model, processor = _ensure_model(api)
            if model is not None:
                inputs = processor(image, return_tensors="pt")
                inputs = {k: v.to(model.device) for k, v in inputs.items()}

                with torch.no_grad():
                    outputs = model(**inputs)

                output_img = outputs.reconstruction.squeeze().float().cpu().clamp(0, 1)
                output_img = output_img.permute(1, 2, 0).numpy()
                output_img = (output_img * 255).astype(np.uint8)
                result = Image.fromarray(output_img)

                if upscale > 2:
                    w, h = result.size
                    extra = upscale / 2
                    result = result.resize((int(w * extra), int(h * extra)), resample=3)

                method = "swin2sr"
                api.log("Image enhanced with Swin2SR")
            else:
                api.log("Swin2SR unavailable, using Pillow enhancement...")
                result, method = _enhance_with_pillow(image, factor=upscale)

            elapsed = time.time() - t0

            buf = io.BytesIO()
            result.save(buf, format="PNG")

            ts = time.strftime("%Y%m%d_%H%M%S")
            fname = filename or f"enhanced_{ts}.png"

            path = api.save_media(
                data=buf.getvalue(), filename=fname, media_type="image",
                prompt=f"Enhancement of {Path(image_path).name}",
                params={"method": method, "upscale": upscale},
                metadata={
                    "source": str(image_path), "method": method,
                    "upscale": upscale, "output_size": f"{result.size[0]}x{result.size[1]}",
                    "elapsed_secs": round(elapsed, 2),
                },
            )
            return json.dumps({
                "status": "ok", "path": path,
                "method": method,
                "output_size": f"{result.size[0]}x{result.size[1]}",
                "elapsed_secs": round(elapsed, 2),
            })

        except Exception as e:
            log.error("Face enhance error: %s", e, exc_info=True)
            return json.dumps({"status": "error", "error": str(e)[:500]})

    api.register_tool({
        "name": "enhance_face",
        "description": (
            "Restore and enhance faces in photos using GFPGAN (local). "
            "Fix blurry, low-resolution, or old photos with AI-powered face "
            "restoration. Falls back to enhanced Pillow upscaling if GFPGAN "
            "is unavailable. No API key needed."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "image_path": {"type": "string", "description": "Path to image with face(s) to enhance."},
                "upscale": {"type": "integer", "description": "Upscale factor 1-4 (default: 2).", "default": 2},
                "filename": {"type": "string", "description": "Output filename (optional)."},
            },
            "required": ["image_path"],
        },
        "execute": execute_enhance,
    })
