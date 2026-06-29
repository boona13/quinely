"""
Style Transfer Node — IP-Adapter Plus + SDXL with InstantStyle.

State-of-the-art diffusion-based style transfer using IP-Adapter Plus (ViT-H)
with the InstantStyle technique. Takes a content image and a style reference
(painting, artwork, texture) and produces a high-quality stylized result.

Approach:
  - SDXL img2img preserves the content structure
  - IP-Adapter Plus (ViT-H encoder) injects style from the reference image
  - InstantStyle targets only style-specific UNet blocks, preventing
    content distortion while applying strong, consistent style

Supports CUDA, Apple MPS, and CPU backends.
"""

import io
import json
import logging
import time
from pathlib import Path

log = logging.getLogger("quinely.node.style_transfer")

_pipe = None
_device_str = None

STYLE_SCALES = {
    "style_only": {
        "up": {"block_0": [0.0, 1.0, 0.0]},
    },
    "style_and_layout": {
        "down": {"block_2": [0.0, 1.0]},
        "up": {"block_0": [0.0, 1.0, 0.0]},
    },
}


def _detect_device(api):
    """Pick the best available device and dtype."""
    import torch

    device_info = api.resource_manager.device_info

    if device_info.has_cuda:
        return "cuda", torch.float16
    if device_info.has_mps:
        return "mps", torch.float16
    return "cpu", torch.float32


def _load_pipeline(api):
    """Load SDXL img2img + IP-Adapter Plus ViT-H (lazy, cached)."""
    global _pipe, _device_str

    if _pipe is not None:
        api.resource_manager.touch("style-transfer-sdxl")
        return _pipe

    import torch
    from transformers import CLIPVisionModelWithProjection
    from diffusers import StableDiffusionXLImg2ImgPipeline

    device, dtype = _detect_device(api)
    _device_str = device

    api.log("Loading SDXL + IP-Adapter Plus for style transfer (first run downloads ~7 GB)...")
    gpu_handle = api.acquire_gpu("style-transfer-sdxl", estimated_vram_gb=7.0)

    try:
        cache = str(api.models_dir)
        hf_token = getattr(api, "hf_token", None)

        image_encoder = CLIPVisionModelWithProjection.from_pretrained(
            "h94/IP-Adapter",
            subfolder="models/image_encoder",
            torch_dtype=dtype,
            cache_dir=cache,
        )

        pipe = StableDiffusionXLImg2ImgPipeline.from_pretrained(
            "stabilityai/stable-diffusion-xl-base-1.0",
            image_encoder=image_encoder,
            torch_dtype=dtype,
            cache_dir=cache,
            token=hf_token,
        )

        pipe.load_ip_adapter(
            "h94/IP-Adapter",
            subfolder="sdxl_models",
            weight_name="ip-adapter-plus_sdxl_vit-h.safetensors",
        )

        if device == "cuda":
            try:
                pipe.enable_model_cpu_offload()
            except Exception:
                pipe.to(device)
        else:
            pipe.to(gpu_handle if isinstance(gpu_handle, str) else device)

    except Exception:
        api.release_gpu("style-transfer-sdxl")
        raise

    _pipe = pipe
    api.notify_model_ready("style-transfer-sdxl")
    api.log(f"Style transfer pipeline ready on {device}")
    return _pipe


def register(api):

    def execute_style_transfer(
        content_image="",
        style_image="",
        style_strength=1.0,
        content_preservation=0.3,
        prompt="",
        negative_prompt="",
        mode="style_only",
        steps=40,
        seed=-1,
        filename="",
        **_kw,
    ):
        if not content_image:
            return json.dumps({"status": "error", "error": "content_image is required"})
        if not style_image:
            return json.dumps({"status": "error", "error": "style_image is required"})
        if not Path(content_image).exists():
            return json.dumps({"status": "error", "error": f"File not found: {content_image}"})
        if not Path(style_image).exists():
            return json.dumps({"status": "error", "error": f"File not found: {style_image}"})

        try:
            import torch
            from PIL import Image

            pipe = _load_pipeline(api)

            content_img = Image.open(content_image).convert("RGB")
            style_img = Image.open(style_image).convert("RGB")

            w, h = content_img.size
            max_dim = 1024
            if max(w, h) > max_dim:
                ratio = max_dim / max(w, h)
                w, h = int(w * ratio), int(h * ratio)
            w = (w // 8) * 8
            h = (h // 8) * 8
            content_img = content_img.resize((w, h), Image.LANCZOS)

            scale_dict = STYLE_SCALES.get(mode, STYLE_SCALES["style_only"])
            scaled = {}
            for block_dir, blocks in scale_dict.items():
                scaled[block_dir] = {}
                for block_name, weights in blocks.items():
                    scaled[block_dir][block_name] = [
                        v * style_strength for v in weights
                    ]
            pipe.set_ip_adapter_scale(scaled)

            img2img_strength = max(0.1, min(0.95, 1.0 - content_preservation))
            steps = max(4, min(steps, 80))

            gen_kwargs = {
                "prompt": prompt or "masterpiece, best quality, high quality",
                "image": content_img,
                "ip_adapter_image": style_img,
                "strength": img2img_strength,
                "num_inference_steps": steps,
                "guidance_scale": 7.5,
                "negative_prompt": negative_prompt or (
                    "photo, realistic, text, watermark, lowres, low quality, "
                    "worst quality, deformed, glitch, noisy, blurry"
                ),
            }

            if seed >= 0:
                device = _device_str or "cpu"
                if device == "cuda":
                    gen_kwargs["generator"] = torch.Generator(device="cuda").manual_seed(seed)
                else:
                    gen_kwargs["generator"] = torch.Generator(device="cpu").manual_seed(seed)

            api.log(
                f"Applying style ({mode}, strength={style_strength}, "
                f"preserve={content_preservation}, steps={steps})..."
            )
            t0 = time.time()
            result = pipe(**gen_kwargs)
            elapsed = time.time() - t0

            result_img = result.images[0]

            buf = io.BytesIO()
            result_img.save(buf, format="PNG")
            img_bytes = buf.getvalue()

            ts = time.strftime("%Y%m%d_%H%M%S")
            fname = filename or f"styled_{ts}.png"
            if not fname.endswith(".png"):
                fname += ".png"

            gen_params = {
                "mode": mode,
                "style_strength": style_strength,
                "content_preservation": content_preservation,
                "steps": steps,
                "seed": seed,
            }
            path = api.save_media(
                data=img_bytes,
                filename=fname,
                media_type="image",
                prompt=f"Style transfer: {Path(style_image).stem} → {Path(content_image).stem}",
                params=gen_params,
                metadata={
                    "content_image": str(content_image),
                    "style_image": str(style_image),
                    **gen_params,
                    "elapsed_secs": round(elapsed, 2),
                },
            )
            return json.dumps({
                "status": "ok",
                "path": path,
                "mode": mode,
                "style_strength": style_strength,
                "content_preservation": content_preservation,
                "steps": steps,
                "size": f"{w}x{h}",
                "elapsed_secs": round(elapsed, 2),
            })

        except Exception as e:
            log.error("Style transfer error: %s", e, exc_info=True)
            return json.dumps({"status": "error", "error": str(e)[:500]})

    api.register_tool({
        "name": "style_transfer",
        "description": (
            "Apply the artistic style of one image onto another (local, no API key). "
            "Uses IP-Adapter Plus + SDXL with the InstantStyle technique for "
            "state-of-the-art style transfer. Feed a content photo and a style "
            "reference (painting, artwork, texture) to create a stylized result.\n\n"
            "Two modes:\n"
            "- style_only (default): transfers colors, textures, brushwork while "
            "preserving the original composition and layout.\n"
            "- style_and_layout: transfers both the artistic style AND the spatial "
            "composition from the reference.\n\n"
            "Tip: include descriptive words about the style in the prompt for "
            "stronger results (e.g. 'oil painting, swirling brushstrokes')."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "content_image": {
                    "type": "string",
                    "description": "Path to the content/photo image to stylize.",
                },
                "style_image": {
                    "type": "string",
                    "description": (
                        "Path to the style reference image (painting, artwork, texture)."
                    ),
                },
                "style_strength": {
                    "type": "number",
                    "default": 1.0,
                    "description": (
                        "How strongly to apply the style (0.0-1.5). "
                        "Higher = stronger style."
                    ),
                },
                "content_preservation": {
                    "type": "number",
                    "default": 0.3,
                    "description": (
                        "How much to preserve the original content structure (0.0-0.9). "
                        "Higher = more faithful to original. "
                        "Use 0.1-0.2 for heavy stylization, 0.4-0.6 for subtle."
                    ),
                },
                "prompt": {
                    "type": "string",
                    "default": "",
                    "description": (
                        "Text prompt to guide the stylization. Describe the desired "
                        "style for best results (e.g. 'van gogh oil painting, "
                        "swirling brushstrokes, vibrant colors'). Optional."
                    ),
                },
                "negative_prompt": {
                    "type": "string",
                    "default": "",
                    "description": "What to avoid in the output (optional).",
                },
                "mode": {
                    "type": "string",
                    "enum": ["style_only", "style_and_layout"],
                    "default": "style_only",
                    "description": (
                        "Transfer mode. 'style_only' preserves layout, "
                        "'style_and_layout' also transforms composition."
                    ),
                },
                "steps": {
                    "type": "integer",
                    "default": 40,
                    "description": (
                        "Inference steps (more = better quality, slower). "
                        "Range: 4-80."
                    ),
                },
                "seed": {
                    "type": "integer",
                    "default": -1,
                    "description": "Random seed for reproducibility (-1 = random).",
                },
                "filename": {
                    "type": "string",
                    "description": "Output filename (optional).",
                },
            },
            "required": ["content_image", "style_image"],
        },
        "execute": execute_style_transfer,
    })
