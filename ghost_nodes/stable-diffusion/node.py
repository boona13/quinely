"""
Stable Diffusion / FLUX Node — text-to-image and image-to-image via diffusers.

Supports (in priority order):
  - FLUX.2 (klein-4B, klein-9B, dev-32B) — latest generation
  - FLUX.1 (schnell, dev) — fast, high quality
  - SDXL (base, turbo) — classic fallback

Backend selection:
  - NVIDIA CUDA: torch + diffusers (default for NVIDIA GPUs)
  - Apple MLX: mlx-community models (when available, fastest on Apple Silicon)
  - Apple MPS: torch + diffusers (fallback for Apple Silicon)
  - CPU: torch + diffusers (slowest, always available)

Models are loaded lazily on first use and managed by the ResourceManager.
"""

import io
import json
import logging
import time
from pathlib import Path

log = logging.getLogger("quinely.node.stable_diffusion")

_pipe = None
_current_model = None
_current_backend = None

# ── Model presets with optimized defaults ───────────────────────────

MODEL_PRESETS = {
    "black-forest-labs/FLUX.2-klein-4B": {
        "pipeline_cls": "Flux2Pipeline",
        "steps": 4,
        "guidance_scale": 0.0,
        "vram_gb": 8,
        "supports_negative": False,
    },
    "black-forest-labs/FLUX.2-klein-9B": {
        "pipeline_cls": "Flux2Pipeline",
        "steps": 8,
        "guidance_scale": 4.0,
        "vram_gb": 13,
        "supports_negative": False,
    },
    "black-forest-labs/FLUX.2-dev": {
        "pipeline_cls": "Flux2Pipeline",
        "steps": 50,
        "guidance_scale": 4.0,
        "vram_gb": 20,
        "supports_negative": False,
    },
    "black-forest-labs/FLUX.1-schnell": {
        "pipeline_cls": "FluxPipeline",
        "steps": 4,
        "guidance_scale": 0.0,
        "vram_gb": 12,
        "supports_negative": False,
    },
    "black-forest-labs/FLUX.1-dev": {
        "pipeline_cls": "FluxPipeline",
        "steps": 50,
        "guidance_scale": 3.5,
        "vram_gb": 12,
        "supports_negative": False,
    },
    "stabilityai/stable-diffusion-xl-base-1.0": {
        "pipeline_cls": "StableDiffusionXLPipeline",
        "img2img_cls": "StableDiffusionXLImg2ImgPipeline",
        "steps": 30,
        "guidance_scale": 7.5,
        "vram_gb": 6.5,
        "supports_negative": True,
    },
    "stabilityai/sdxl-turbo": {
        "pipeline_cls": "StableDiffusionXLPipeline",
        "img2img_cls": "StableDiffusionXLImg2ImgPipeline",
        "steps": 4,
        "guidance_scale": 0.0,
        "vram_gb": 6.5,
        "supports_negative": True,
    },
}


def _select_best_model(api):
    """Pick the best model for the detected hardware."""
    device_info = api.resource_manager.device_info
    available_gb = api.resource_manager.available_gb

    if device_info.has_cuda:
        if available_gb >= 20:
            return "black-forest-labs/FLUX.2-dev"
        if available_gb >= 13:
            return "black-forest-labs/FLUX.2-klein-9B"
        if available_gb >= 8:
            return "black-forest-labs/FLUX.2-klein-4B"
        return "stabilityai/sdxl-turbo"

    if device_info.has_mlx or device_info.has_mps:
        unified = device_info.unified_memory_gb
        if unified >= 32:
            return "black-forest-labs/FLUX.2-klein-9B"
        if unified >= 16:
            return "black-forest-labs/FLUX.2-klein-4B"
        if unified >= 10:
            return "black-forest-labs/FLUX.1-schnell"
        return "stabilityai/sdxl-turbo"

    return "stabilityai/sdxl-turbo"


def _try_load_mlx_pipeline(model_id, api):
    """Attempt to load an MLX-native pipeline for Apple Silicon."""
    if not api.resource_manager.device_info.has_mlx:
        return None

    mlx_model_map = {
        "black-forest-labs/FLUX.2-klein-9B": "mlx-community/FLUX.2-klein-9B",
        "black-forest-labs/FLUX.2-klein-4B": "mlx-community/FLUX.2-klein-4B",
        "black-forest-labs/FLUX.1-schnell": "mlx-community/FLUX.1-schnell",
    }
    mlx_id = mlx_model_map.get(model_id)
    if not mlx_id:
        return None

    try:
        import mlx.core as mx
        from mlx_flux import FluxPipeline as MLXFluxPipeline

        api.log(f"Loading MLX-native model: {mlx_id}")
        pipe = MLXFluxPipeline.from_pretrained(mlx_id)
        return ("mlx", pipe)
    except ImportError:
        log.debug("mlx_flux not installed, falling back to torch MPS")
    except Exception as e:
        log.debug("MLX pipeline load failed: %s, falling back to torch", e)
    return None


def _import_pipeline_class(cls_name: str):
    """Import a specific diffusers pipeline class without triggering lazy-import cascade.

    Uses direct imports to avoid diffusers' lazy __getattr__ which
    cascades through all pipeline modules (including ones with broken deps).
    """
    _DIRECT_IMPORTS = {
        "FluxPipeline": lambda: __import__("diffusers", fromlist=["FluxPipeline"]).FluxPipeline,
        "Flux2Pipeline": lambda: __import__("diffusers", fromlist=["Flux2Pipeline"]).Flux2Pipeline,
        "FluxImg2ImgPipeline": lambda: __import__("diffusers", fromlist=["FluxImg2ImgPipeline"]).FluxImg2ImgPipeline,
        "StableDiffusionXLPipeline": lambda: __import__("diffusers", fromlist=["StableDiffusionXLPipeline"]).StableDiffusionXLPipeline,
        "StableDiffusionXLImg2ImgPipeline": lambda: __import__("diffusers", fromlist=["StableDiffusionXLImg2ImgPipeline"]).StableDiffusionXLImg2ImgPipeline,
    }
    loader = _DIRECT_IMPORTS.get(cls_name)
    if loader:
        try:
            return loader()
        except (ImportError, RuntimeError):
            pass
    from diffusers import FluxPipeline
    return FluxPipeline


def _load_torch_pipeline(model_id, api, for_img2img=False):
    """Load a diffusers pipeline with the appropriate class and device."""
    import torch

    preset = MODEL_PRESETS.get(model_id, MODEL_PRESETS["stabilityai/stable-diffusion-xl-base-1.0"])
    vram = preset["vram_gb"]
    device = api.acquire_gpu(model_id, estimated_vram_gb=vram)

    if device in ("cuda", "mps"):
        dtype = torch.bfloat16
    else:
        dtype = torch.float32

    cls_name = preset["pipeline_cls"]
    if for_img2img:
        cls_name = preset.get("img2img_cls", "FluxImg2ImgPipeline")

    PipelineCls = _import_pipeline_class(cls_name)

    api.log(f"Downloading & loading {model_id} (this may take a few minutes on first run)...")
    hf_token = getattr(api, 'hf_token', None)
    pipe = PipelineCls.from_pretrained(
        model_id,
        torch_dtype=dtype,
        cache_dir=str(api.models_dir),
        token=hf_token,
    )
    api.log(f"Model loaded on {device}")

    if device == "cuda":
        try:
            pipe.enable_model_cpu_offload()
        except Exception:
            pipe.to(device)
    else:
        pipe.to(device)

    if device == "cuda":
        try:
            pipe.enable_attention_slicing()
        except Exception:
            pass

    return ("torch", pipe)


def _get_pipeline(api, model_id=None, for_img2img=False):
    """Load or reuse the diffusion pipeline. Tries MLX first on Apple Silicon."""
    global _pipe, _current_model, _current_backend

    if model_id is None:
        model_id = _select_best_model(api)

    if _pipe is not None and _current_model == model_id and not for_img2img:
        api.resource_manager.touch(model_id)
        return _pipe, _current_backend, model_id

    if _pipe is not None and _current_model != model_id:
        api.release_gpu(_current_model)
        _pipe = None
        _current_model = None
        _current_backend = None

    try:
        import torch  # noqa: F401 — verify torch is available
    except ImportError:
        raise RuntimeError(
            "PyTorch not installed. Install deps: "
            "pip install torch diffusers transformers accelerate safetensors sentencepiece protobuf"
        )

    if not for_img2img:
        mlx_result = _try_load_mlx_pipeline(model_id, api)
        if mlx_result:
            _current_backend, _pipe = mlx_result
            _current_model = model_id
            api.log(f"Pipeline ready (MLX native): {model_id}")
            return _pipe, _current_backend, model_id

    backend, pipe = _load_torch_pipeline(model_id, api, for_img2img=for_img2img)
    _current_backend = backend
    _pipe = pipe
    _current_model = model_id
    api.log(f"Pipeline ready ({backend}): {model_id}")
    return pipe, backend, model_id


def register(api):
    """Register image generation tools with Ghost."""

    def execute_text_to_image(prompt="", negative_prompt="", model="",
                              width=1024, height=1024, steps=0,
                              guidance_scale=-1, seed=-1, filename="", **_kw):
        if not prompt:
            return json.dumps({"status": "error", "error": "prompt is required"})

        try:
            import torch

            model_id = model or None
            pipe, backend, model_id = _get_pipeline(api, model_id=model_id)

            preset = MODEL_PRESETS.get(model_id, {})
            if steps <= 0:
                steps = preset.get("steps", 30)
            if guidance_scale < 0:
                guidance_scale = preset.get("guidance_scale", 7.5)

            generator = None
            if seed >= 0 and backend == "torch":
                generator = torch.Generator(device=pipe.device if hasattr(pipe, 'device') else "cpu").manual_seed(seed)

            width = max(256, min(width, 2048))
            height = max(256, min(height, 2048))
            width = (width // 8) * 8
            height = (height // 8) * 8
            steps = max(1, min(steps, 100))

            api.log(f"Generating {width}x{height}, steps={steps}, model={model_id}, backend={backend}...")
            t0 = time.time()

            gen_kwargs = {
                "prompt": prompt,
                "width": width,
                "height": height,
                "num_inference_steps": steps,
                "guidance_scale": guidance_scale,
            }
            if preset.get("supports_negative", True) and negative_prompt:
                gen_kwargs["negative_prompt"] = negative_prompt
            if generator:
                gen_kwargs["generator"] = generator

            if backend == "mlx":
                image = pipe.generate(**gen_kwargs)
            else:
                result = pipe(**gen_kwargs)
                image = result.images[0]

            elapsed = time.time() - t0

            buf = io.BytesIO()
            image.save(buf, format="PNG")
            img_bytes = buf.getvalue()

            ts = time.strftime("%Y%m%d_%H%M%S")
            fname = filename or f"flux_{ts}.png"
            if not fname.endswith(".png"):
                fname += ".png"

            gen_params = {
                "model": model_id, "backend": backend,
                "width": width, "height": height,
                "steps": steps, "guidance_scale": guidance_scale, "seed": seed,
            }
            path = api.save_media(
                data=img_bytes, filename=fname, media_type="image",
                prompt=prompt,
                params=gen_params,
                metadata={
                    **gen_params,
                    "negative_prompt": negative_prompt,
                    "elapsed_secs": round(elapsed, 2),
                },
            )
            return json.dumps({
                "status": "ok",
                "path": path,
                "model": model_id,
                "backend": backend,
                "size": f"{width}x{height}",
                "steps": steps,
                "elapsed_secs": round(elapsed, 2),
            })

        except Exception as e:
            log.error("text_to_image error: %s", e, exc_info=True)
            return json.dumps({"status": "error", "error": str(e)[:500]})

    def execute_image_to_image(prompt="", image_path="", model="",
                               strength=0.75, steps=0,
                               guidance_scale=-1, filename="", **_kw):
        if not prompt:
            return json.dumps({"status": "error", "error": "prompt is required"})
        if not image_path:
            return json.dumps({"status": "error", "error": "image_path is required"})

        try:
            from PIL import Image

            model_id = model or None
            if model_id is None:
                model_id = _select_best_model(api)

            preset = MODEL_PRESETS.get(model_id, {})
            if steps <= 0:
                steps = preset.get("steps", 30)
            if guidance_scale < 0:
                guidance_scale = preset.get("guidance_scale", 7.5)

            pipe, backend, model_id = _get_pipeline(api, model_id=model_id, for_img2img=True)

            init_image = Image.open(image_path).convert("RGB")

            api.log(f"img2img with strength={strength}, steps={steps}, model={model_id}...")
            t0 = time.time()
            result = pipe(
                prompt=prompt,
                image=init_image,
                strength=strength,
                num_inference_steps=steps,
                guidance_scale=guidance_scale,
            )
            elapsed = time.time() - t0

            image = result.images[0]
            buf = io.BytesIO()
            image.save(buf, format="PNG")

            ts = time.strftime("%Y%m%d_%H%M%S")
            fname = filename or f"flux_img2img_{ts}.png"
            if not fname.endswith(".png"):
                fname += ".png"

            path = api.save_media(
                data=buf.getvalue(), filename=fname, media_type="image",
                prompt=prompt,
                params={"model": model_id, "backend": backend, "strength": strength},
                metadata={
                    "prompt": prompt, "model": model_id, "backend": backend,
                    "strength": strength, "elapsed_secs": round(elapsed, 2),
                },
            )
            return json.dumps({
                "status": "ok", "path": path, "model": model_id,
                "backend": backend, "elapsed_secs": round(elapsed, 2),
            })

        except Exception as e:
            log.error("image_to_image error: %s", e, exc_info=True)
            return json.dumps({"status": "error", "error": str(e)[:500]})

    # ── Register tools ──────────────────────────────────────────────

    api.register_tool({
        "name": "text_to_image_local",
        "description": (
            "Generate an image locally using FLUX.2 / FLUX.1 / SDXL. "
            "Automatically selects the best model for your hardware. "
            "Uses MLX on Apple Silicon for maximum speed, CUDA on NVIDIA. "
            "No API key needed — runs 100% locally.\n\n"
            "Models (auto-selected by VRAM):\n"
            "- FLUX.2-klein-4B: 8GB, Apache 2.0, sub-second (default)\n"
            "- FLUX.2-klein-9B: 13GB, best quality/speed balance\n"
            "- FLUX.2-dev: 20GB+, maximum quality\n"
            "- FLUX.1-schnell: 12GB, Apache 2.0, 4-step fast\n"
            "- SDXL: 6.5GB, classic fallback"
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "prompt": {
                    "type": "string",
                    "description": "Detailed text description of the image to generate.",
                },
                "negative_prompt": {
                    "type": "string",
                    "description": "What to avoid (only used with SDXL models, ignored by FLUX).",
                },
                "model": {
                    "type": "string",
                    "description": (
                        "HuggingFace model ID. Leave empty for auto-selection. "
                        "Options: black-forest-labs/FLUX.2-klein-4B, "
                        "black-forest-labs/FLUX.2-klein-9B, "
                        "black-forest-labs/FLUX.2-dev, "
                        "black-forest-labs/FLUX.1-schnell, "
                        "black-forest-labs/FLUX.1-dev, "
                        "stabilityai/stable-diffusion-xl-base-1.0"
                    ),
                },
                "width": {"type": "integer", "description": "Image width (default 1024, multiple of 8)", "default": 1024},
                "height": {"type": "integer", "description": "Image height (default 1024, multiple of 8)", "default": 1024},
                "steps": {"type": "integer", "description": "Inference steps (0 = auto based on model)", "default": 0},
                "guidance_scale": {"type": "number", "description": "CFG scale (-1 = auto based on model)", "default": -1},
                "seed": {"type": "integer", "description": "Random seed (-1 for random)", "default": -1},
                "filename": {"type": "string", "description": "Output filename (optional)"},
            },
            "required": ["prompt"],
        },
        "execute": execute_text_to_image,
    })

    api.register_tool({
        "name": "image_to_image_local",
        "description": (
            "Transform an existing image using FLUX / SDXL img2img. "
            "Takes a source image and a prompt to guide the transformation. "
            "Auto-selects the best model for your hardware."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "prompt": {"type": "string", "description": "How to transform the image."},
                "image_path": {"type": "string", "description": "Path to the source image."},
                "model": {"type": "string", "description": "HuggingFace model ID (empty = auto-select)."},
                "strength": {"type": "number", "description": "Transformation strength 0-1 (default 0.75).", "default": 0.75},
                "steps": {"type": "integer", "description": "Inference steps (0 = auto).", "default": 0},
                "guidance_scale": {"type": "number", "description": "CFG scale (-1 = auto).", "default": -1},
                "filename": {"type": "string", "description": "Output filename (optional)."},
            },
            "required": ["prompt", "image_path"],
        },
        "execute": execute_image_to_image,
    })
