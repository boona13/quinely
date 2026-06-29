"""
Image-to-Video Node — animate any image into a cinematic video using SVD.

Uses Stable Video Diffusion to bring still images to life with natural motion.
SVD generates 14 frames, SVD-XT generates 25 frames for smoother videos.
"""

import json
import logging
import time
from pathlib import Path

log = logging.getLogger("quinely.node.image_to_video")

_pipe = None
_current_model = None

MODELS = {
    "svd": "stabilityai/stable-video-diffusion-img2vid",
    "svd-xt": "stabilityai/stable-video-diffusion-img2vid-xt",
}


def _cast_float64_to_float32(pipe):
    """Convert all float64 parameters and buffers to float32 (MPS compat)."""
    import torch
    import torch.nn as nn
    components = getattr(pipe, 'components', {})
    for name, component in components.items():
        if not isinstance(component, nn.Module):
            continue
        for module in component.modules():
            for key, buf in list(module._buffers.items()):
                if buf is not None and buf.dtype == torch.float64:
                    module._buffers[key] = buf.to(torch.float32)
            for key, param in list(module._parameters.items()):
                if param is not None and param.dtype == torch.float64:
                    module._parameters[key] = torch.nn.Parameter(
                        param.data.to(torch.float32), requires_grad=param.requires_grad,
                    )


def _ensure_pipeline(api, variant="svd"):
    global _pipe, _current_model

    model_id = MODELS.get(variant, MODELS["svd"])
    if _pipe is not None and _current_model == model_id:
        api.resource_manager.touch(model_id)
        return _pipe

    if _pipe is not None:
        api.release_gpu(_current_model)
        _pipe = None

    try:
        import torch
        from diffusers import StableVideoDiffusionPipeline
    except ImportError:
        raise RuntimeError("Required: pip install torch diffusers transformers imageio imageio-ffmpeg")

    device_str = api.acquire_gpu(model_id, estimated_vram_gb=6.0)
    dtype = torch.float16 if "cuda" in device_str else torch.float32
    is_mps = "mps" in device_str

    api.log(f"Loading Stable Video Diffusion ({variant}) — first run downloads ~9GB...")
    _pipe = StableVideoDiffusionPipeline.from_pretrained(
        model_id, torch_dtype=dtype, cache_dir=api.models_dir,
        token=getattr(api, 'hf_token', None),
    )

    if is_mps:
        _cast_float64_to_float32(_pipe)

    _pipe.to(device_str)

    try:
        _pipe.enable_model_cpu_offload()
    except Exception:
        pass

    _current_model = model_id
    api.log(f"SVD loaded on {device_str}")
    return _pipe


def register(api):

    def execute_img2vid(image_path="", variant="svd", fps=7,
                        motion_bucket_id=127, noise_aug_strength=0.02,
                        steps=25, filename="", **_kw):
        if not image_path:
            return json.dumps({"status": "error", "error": "image_path is required"})
        if not Path(image_path).exists():
            return json.dumps({"status": "error", "error": f"File not found: {image_path}"})

        try:
            import torch
            from PIL import Image
            import imageio

            pipe = _ensure_pipeline(api, variant=variant)
            image = Image.open(image_path).convert("RGB")
            import platform
            if platform.system() == "Darwin":
                image = image.resize((512, 288))
            else:
                image = image.resize((1024, 576))

            api.log(f"Generating video from image ({variant}, {steps} steps)...")
            t0 = time.time()

            frames = pipe(
                image,
                num_inference_steps=min(steps, 50),
                motion_bucket_id=motion_bucket_id,
                noise_aug_strength=noise_aug_strength,
                fps=fps,
            ).frames[0]

            elapsed = time.time() - t0

            ts = time.strftime("%Y%m%d_%H%M%S")
            fname = filename or f"animated_{ts}.mp4"
            out_path = Path(api.models_dir).parent / "media" / "video" / fname
            out_path.parent.mkdir(parents=True, exist_ok=True)

            writer = imageio.get_writer(str(out_path), fps=fps, codec="libx264")
            import numpy as np
            for frame in frames:
                if hasattr(frame, 'numpy'):
                    arr = frame.numpy()
                elif isinstance(frame, Image.Image):
                    arr = np.array(frame)
                else:
                    arr = np.array(frame)
                if arr.dtype == np.float32 or arr.dtype == np.float64:
                    arr = (arr * 255).clip(0, 255).astype(np.uint8)
                writer.append_data(arr)
            writer.close()

            video_bytes = out_path.read_bytes()
            saved = api.save_media(
                data=video_bytes, filename=fname, media_type="video",
                prompt=f"Image-to-video: {Path(image_path).name}",
                params={"variant": variant, "fps": fps, "steps": steps},
                metadata={
                    "source": str(image_path), "variant": variant,
                    "fps": fps, "frames": len(frames),
                    "motion_bucket_id": motion_bucket_id,
                    "elapsed_secs": round(elapsed, 2),
                },
            )
            return json.dumps({
                "status": "ok", "path": saved,
                "frames": len(frames), "fps": fps,
                "variant": variant,
                "elapsed_secs": round(elapsed, 2),
            })

        except Exception as e:
            log.error("Image-to-video error: %s", e, exc_info=True)
            return json.dumps({"status": "error", "error": str(e)[:500]})

    api.register_tool({
        "name": "animate_image_svd",
        "description": (
            "Animate any image into a cinematic video using Stable Video Diffusion (local). "
            "Best for smooth camera-like motion on photos. SVD generates 14 frames, "
            "SVD-XT generates 25 frames for smoother results. "
            "Control motion intensity with motion_bucket_id. No API key needed."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "image_path": {"type": "string", "description": "Path to input image to animate."},
                "variant": {
                    "type": "string", "enum": ["svd", "svd-xt"],
                    "description": "SVD (14 frames, faster) or SVD-XT (25 frames, smoother). Default: svd.",
                    "default": "svd",
                },
                "fps": {"type": "integer", "description": "Frames per second (default: 7).", "default": 7},
                "motion_bucket_id": {"type": "integer", "description": "Motion intensity 1-255 (higher=more motion, default: 127).", "default": 127},
                "noise_aug_strength": {"type": "number", "description": "Noise augmentation 0-1 (default: 0.02).", "default": 0.02},
                "steps": {"type": "integer", "description": "Inference steps (default: 25, max: 50).", "default": 25},
                "filename": {"type": "string", "description": "Output filename (optional)."},
            },
            "required": ["image_path"],
        },
        "execute": execute_img2vid,
    })
