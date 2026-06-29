"""
Depth Estimation Node — create depth maps from any image using Depth Anything V2.

Produces grayscale or colored depth maps from single images. Useful for:
- 3D parallax effects
- Spatial understanding
- AR/VR content
- Artistic depth-based compositions
"""

import json
import logging
import time
from pathlib import Path

log = logging.getLogger("quinely.node.depth_estimation")

_model = None
_processor = None
_current_model_id = None

MODEL_SIZES = {
    "small": "depth-anything/Depth-Anything-V2-Small-hf",
    "base": "depth-anything/Depth-Anything-V2-Base-hf",
    "large": "depth-anything/Depth-Anything-V2-Large-hf",
}


def _ensure_model(api, model_size="small"):
    global _model, _processor, _current_model_id

    model_id = MODEL_SIZES.get(model_size, MODEL_SIZES["small"])
    if _model is not None and _current_model_id == model_id:
        api.resource_manager.touch(model_id)
        return _model, _processor

    if _model is not None:
        api.release_gpu(_current_model_id)

    try:
        import torch
        from transformers import AutoImageProcessor, AutoModelForDepthEstimation
    except ImportError:
        raise RuntimeError("Required: pip install torch transformers Pillow")

    vram = {"small": 0.5, "base": 1.0, "large": 2.0}.get(model_size, 0.5)
    device = api.acquire_gpu(model_id, estimated_vram_gb=vram)

    api.log(f"Loading Depth Anything V2 ({model_size})...")
    _processor = AutoImageProcessor.from_pretrained(model_id, cache_dir=api.models_dir, token=getattr(api, 'hf_token', None))
    _model = AutoModelForDepthEstimation.from_pretrained(model_id, cache_dir=api.models_dir, token=getattr(api, 'hf_token', None))
    _model.to(device)
    _model.eval()
    _current_model_id = model_id
    api.log(f"Depth model loaded on {device}")
    return _model, _processor


def register(api):

    def execute_depth(image_path="", model_size="small", colorize=True,
                      filename="", **_kw):
        if not image_path:
            return json.dumps({"status": "error", "error": "image_path is required"})
        if not Path(image_path).exists():
            return json.dumps({"status": "error", "error": f"File not found: {image_path}"})

        try:
            import torch
            import numpy as np
            from PIL import Image

            model, processor = _ensure_model(api, model_size=model_size)
            image = Image.open(image_path).convert("RGB")

            api.log(f"Estimating depth for {image.size[0]}x{image.size[1]} image...")
            t0 = time.time()

            inputs = processor(images=image, return_tensors="pt")
            inputs = {k: v.to(model.device) for k, v in inputs.items()}

            with torch.no_grad():
                outputs = model(**inputs)
                depth = outputs.predicted_depth

            depth = torch.nn.functional.interpolate(
                depth.unsqueeze(1),
                size=image.size[::-1],
                mode="bicubic",
                align_corners=False,
            ).squeeze()

            depth_np = depth.cpu().numpy()
            depth_norm = (depth_np - depth_np.min()) / (depth_np.max() - depth_np.min() + 1e-8)
            depth_uint8 = (depth_norm * 255).astype(np.uint8)

            if colorize:
                depth_img = Image.fromarray(depth_uint8)
                depth_img = depth_img.convert("L")
                from matplotlib import cm
                try:
                    colormap = cm.get_cmap("inferno")
                    colored = (colormap(depth_norm)[:, :, :3] * 255).astype(np.uint8)
                    depth_img = Image.fromarray(colored)
                except ImportError:
                    hue = ((1.0 - depth_norm) * 0.7 * 179).astype(np.uint8)
                    sat = np.full_like(hue, 230)
                    val = np.full_like(hue, 230)
                    hsv = np.stack([hue, sat, val], axis=-1)
                    depth_img = Image.fromarray(hsv, "HSV").convert("RGB")
            else:
                depth_img = Image.fromarray(depth_uint8)

            elapsed = time.time() - t0
            import io
            buf = io.BytesIO()
            depth_img.save(buf, format="PNG")
            img_bytes = buf.getvalue()

            ts = time.strftime("%Y%m%d_%H%M%S")
            fname = filename or f"depth_{ts}.png"

            path = api.save_media(
                data=img_bytes, filename=fname, media_type="image",
                prompt=f"Depth map of {Path(image_path).name}",
                params={"model_size": model_size, "colorize": colorize},
                metadata={
                    "source": str(image_path), "model": _current_model_id,
                    "colorize": colorize, "elapsed_secs": round(elapsed, 2),
                },
            )
            return json.dumps({
                "status": "ok", "path": path,
                "model": _current_model_id,
                "colorize": colorize,
                "elapsed_secs": round(elapsed, 2),
            })

        except Exception as e:
            log.error("Depth estimation error: %s", e, exc_info=True)
            return json.dumps({"status": "error", "error": str(e)[:500]})

    api.register_tool({
        "name": "estimate_depth",
        "description": (
            "Create a depth map from any image using Depth Anything V2 (local). "
            "Outputs a grayscale or colorized depth visualization. Great for 3D "
            "effects, parallax videos, and spatial analysis. No API key needed."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "image_path": {"type": "string", "description": "Path to the input image."},
                "model_size": {
                    "type": "string", "enum": ["small", "base", "large"],
                    "description": "Model size: small (fast), base (balanced), large (best quality). Default: small.",
                    "default": "small",
                },
                "colorize": {
                    "type": "boolean",
                    "description": "Colorize the depth map (blue=near, red=far). Default: true.",
                    "default": True,
                },
                "filename": {"type": "string", "description": "Output filename (optional)."},
            },
            "required": ["image_path"],
        },
        "execute": execute_depth,
    })
