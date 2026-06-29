"""
Image Upscale Node — enhance image resolution using Real-ESRGAN.

Falls back to high-quality Lanczos upscaling if Real-ESRGAN isn't installed.
"""

import io
import json
import logging
import time
from pathlib import Path

log = logging.getLogger("quinely.node.image_upscale")

_upsampler = None
_upsampler_scale = None

REALESRGAN_WEIGHTS_URL = "https://github.com/xinntao/Real-ESRGAN/releases/download/v0.1.0/RealESRGAN_x4plus.pth"


def _ensure_realesrgan(api, scale=4):
    """Load and cache the Real-ESRGAN model. Returns (upsampler, device) or raises ImportError."""
    global _upsampler, _upsampler_scale

    if _upsampler is not None and _upsampler_scale == scale:
        api.resource_manager.touch("real-esrgan")
        return _upsampler

    import torch
    from basicsr.archs.rrdbnet_arch import RRDBNet
    from realesrgan import RealESRGANer

    model = RRDBNet(num_in_ch=3, num_out_ch=3, num_feat=64,
                    num_block=23, num_grow_ch=32, scale=4)

    weights_dir = Path(api.models_dir) / "realesrgan"
    weights_dir.mkdir(parents=True, exist_ok=True)
    weights_path = weights_dir / "RealESRGAN_x4plus.pth"

    if not weights_path.exists():
        api.log("Downloading Real-ESRGAN weights...")
        try:
            from huggingface_hub import hf_hub_download
            downloaded = hf_hub_download(
                repo_id="ai-forever/Real-ESRGAN",
                filename="RealESRGAN_x4plus.pth",
                local_dir=str(weights_dir),
                token=getattr(api, 'hf_token', None),
            )
            weights_path = Path(downloaded)
        except Exception:
            import urllib.request
            urllib.request.urlretrieve(REALESRGAN_WEIGHTS_URL, str(weights_path))

    device = api.acquire_gpu("real-esrgan", estimated_vram_gb=0.5)

    _upsampler = RealESRGANer(
        scale=4, model_path=str(weights_path), dni_weight=None,
        model=model, tile=0, tile_pad=10, pre_pad=0,
        half=device == "cuda",
        device=device,
    )
    _upsampler_scale = scale
    api.log(f"Real-ESRGAN ready on {device}")
    return _upsampler


def register(api):

    def execute_upscale(image_path="", scale=4, filename="", **_kw):
        if not image_path:
            return json.dumps({"status": "error", "error": "image_path is required"})
        if not Path(image_path).exists():
            return json.dumps({"status": "error", "error": f"File not found: {image_path}"})

        scale = max(2, min(scale, 4))

        try:
            from PIL import Image
            import numpy as np
        except ImportError:
            return json.dumps({"status": "error", "error": "Pillow and numpy required. Run: pip install Pillow numpy"})

        try:
            t0 = time.time()

            upscaled = None
            method = "lanczos"

            try:
                upsampler = _ensure_realesrgan(api, scale)
                img = np.array(Image.open(image_path).convert("RGB"))
                output, _ = upsampler.enhance(img, outscale=scale)
                upscaled = Image.fromarray(output)
                method = "real-esrgan"
            except ImportError:
                api.log("Real-ESRGAN not available, using high-quality Lanczos upscaling")
                img = Image.open(image_path).convert("RGB")
                new_size = (img.width * scale, img.height * scale)
                upscaled = img.resize(new_size, Image.LANCZOS)
            except Exception as e:
                api.log(f"Real-ESRGAN error ({e}), falling back to Lanczos")
                img = Image.open(image_path).convert("RGB")
                new_size = (img.width * scale, img.height * scale)
                upscaled = img.resize(new_size, Image.LANCZOS)

            elapsed = time.time() - t0

            buf = io.BytesIO()
            upscaled.save(buf, format="PNG")

            ts = time.strftime("%Y%m%d_%H%M%S")
            src_name = Path(image_path).stem
            fname = filename or f"{src_name}_upscale{scale}x_{ts}.png"

            path = api.save_media(
                data=buf.getvalue(), filename=fname, media_type="image",
                metadata={
                    "source": str(image_path), "scale": scale,
                    "method": method, "output_size": f"{upscaled.width}x{upscaled.height}",
                    "elapsed_secs": round(elapsed, 2),
                },
            )
            return json.dumps({
                "status": "ok", "path": path, "method": method,
                "output_size": f"{upscaled.width}x{upscaled.height}",
                "elapsed_secs": round(elapsed, 2),
            })

        except Exception as e:
            return json.dumps({"status": "error", "error": str(e)[:500]})

    api.register_tool({
        "name": "upscale_image",
        "description": (
            "Upscale an image 2x-4x using Real-ESRGAN (falls back to Lanczos). "
            "Enhances resolution and sharpens details. No API key needed.\n"
            "For best quality, install: pip install realesrgan basicsr torch"
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "image_path": {"type": "string", "description": "Path to the source image."},
                "scale": {"type": "integer", "description": "Upscale factor: 2 or 4 (default 4).", "default": 4},
                "filename": {"type": "string", "description": "Output filename (optional)."},
            },
            "required": ["image_path"],
        },
        "execute": execute_upscale,
    })
