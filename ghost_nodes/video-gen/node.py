"""
Video Generation Node — text-to-video and image-to-video.

Backend selection (in priority order):
  1. Wan2.1 via diffusers (primary, cross-platform: CUDA / MPS / CPU)
     - T2V: Wan-AI/Wan2.1-T2V-1.3B-Diffusers  (~8 GB VRAM, consumer-friendly)
     - I2V: Wan-AI/Wan2.1-I2V-14B-480P-Diffusers (CPU-offloaded for consumer GPUs)
  2. Apple MLX: LTX-2 via mlx-video (user-selectable, Apple Silicon only)
  3. Fallback: LTX-Video or CogVideoX-2b via diffusers (legacy)

Model is cached after first load for fast subsequent calls.
"""

import json
import logging
import platform
import time
from pathlib import Path

log = logging.getLogger("quinely.node.video_gen")

_pipe = None
_current_model = None
_pipe_type = None
_backend = None  # "wan", "mlx", or "diffusers-legacy"

WAN_T2V_1_3B = "Wan-AI/Wan2.1-T2V-1.3B-Diffusers"
WAN_I2V_14B_480P = "Wan-AI/Wan2.1-I2V-14B-480P-Diffusers"
LTX_VIDEO_MODEL = "Lightricks/LTX-Video"
COGVIDEOX_MODEL = "THUDM/CogVideoX-2b"
MLX_MODEL_REPO = "Lightricks/LTX-2"

WAN_DEFAULT_NEGATIVE = (
    "Bright tones, overexposed, static, blurred details, subtitles, style, "
    "works, paintings, images, static, overall gray, worst quality, low quality, "
    "JPEG compression residue, ugly, incomplete, extra fingers, poorly drawn hands, "
    "poorly drawn faces, deformed, disfigured, misshapen limbs, fused fingers, "
    "still picture, messy background, three legs, many people in the background, "
    "walking backwards"
)


WAN_1_3B_FLOW_SHIFT = 8.0   # official shift for 1.3B at 480P
WAN_14B_FLOW_SHIFT = 5.0    # official shift for 14B at 720P
WAN_1_3B_GUIDANCE = 6.0     # official guidance for 1.3B
WAN_DEFAULT_STEPS = 50      # minimum for decent quality with flow matching


def _align_frames_wan(n):
    """Wan2.1 requires frame count = 4k + 1."""
    k = max(1, round((n - 1) / 4))
    return 4 * k + 1


def _align_dim_wan(val, pipe=None):
    """Align dimension to Wan2.1 VAE requirements.

    Uses vae_scale_factor * patch_size when pipe is available,
    otherwise falls back to 16 (the common Wan alignment).
    """
    if pipe is not None:
        try:
            mod = pipe.vae_scale_factor_spatial * pipe.transformer.config.patch_size[1]
        except Exception:
            mod = 16
    else:
        mod = 16
    return max(mod, (val // mod) * mod)


# ---------------------------------------------------------------------------
# MLX backend (Apple Silicon only, user-selectable)
# ---------------------------------------------------------------------------

def _check_mlx_available():
    if platform.system() != "Darwin":
        return False
    try:
        import mlx.core  # noqa: F401
        return True
    except ImportError:
        return False


def _mlx_model_cached():
    cache_dir = Path.home() / ".cache" / "huggingface" / "hub" / "models--Lightricks--LTX-2"
    if not cache_dir.exists():
        return False
    snapshots = cache_dir / "snapshots"
    if not snapshots.exists():
        return False
    for snap in snapshots.iterdir():
        main_weights = snap / "ltx-2-19b-distilled.safetensors"
        if main_weights.exists() and main_weights.stat().st_size > 1_000_000_000:
            return True
    return False


def _try_mlx_text_to_video(api, prompt, num_frames=65, height=512, width=768,
                           fps=24, seed=None):
    if not _mlx_model_cached():
        return None
    try:
        from mlx_video import generate_video
    except ImportError:
        return None

    api.log("Generating video with LTX-2 (MLX native on Apple Silicon)...")
    t0 = time.time()
    ts = time.strftime("%Y%m%d_%H%M%S")
    out_path = Path(api.data_dir) / f"video_mlx_{ts}.mp4"

    generate_video(
        model_repo=MLX_MODEL_REPO, text_encoder_repo=None,
        prompt=prompt,
        height=(height // 32) * 32, width=(width // 32) * 32,
        num_frames=num_frames, seed=seed or 42, fps=fps,
        output_path=str(out_path), verbose=True, tiling="auto",
    )
    elapsed = time.time() - t0
    if not out_path.exists():
        return None
    return str(out_path), elapsed


def _try_mlx_image_to_video(api, image_path, prompt="", num_frames=65,
                            height=512, width=768, fps=24, seed=None):
    if not _mlx_model_cached():
        return None
    try:
        from mlx_video import generate_video
    except ImportError:
        return None

    api.log("Animating image with LTX-2 (MLX native on Apple Silicon)...")
    t0 = time.time()
    ts = time.strftime("%Y%m%d_%H%M%S")
    out_path = Path(api.data_dir) / f"i2v_mlx_{ts}.mp4"

    generate_video(
        model_repo=MLX_MODEL_REPO, text_encoder_repo=None,
        prompt=prompt or "animate this image with natural cinematic motion",
        image=str(image_path), image_strength=0.8,
        height=(height // 32) * 32, width=(width // 32) * 32,
        num_frames=num_frames, seed=seed or 42, fps=fps,
        output_path=str(out_path), verbose=True, tiling="auto",
    )
    elapsed = time.time() - t0
    if not out_path.exists():
        return None
    return str(out_path), elapsed


# ---------------------------------------------------------------------------
# Wan 2.1 pipeline loading
# ---------------------------------------------------------------------------

def _get_device_and_dtype(api, model_id, vram_gb):
    """Acquire GPU and determine dtype based on device capabilities."""
    import torch

    device_str = api.acquire_gpu(model_id, estimated_vram_gb=vram_gb)

    is_cuda = "cuda" in device_str
    is_mps = "mps" in device_str

    if is_cuda:
        dtype = torch.bfloat16
    elif is_mps:
        dtype = torch.float32
    else:
        dtype = torch.float32

    return device_str, dtype, is_cuda, is_mps


def _cast_float64_to_float32(pipe):
    """Convert float64 params/buffers to float32 for MPS compatibility."""
    import torch
    import torch.nn as nn
    components = getattr(pipe, 'components', {})
    for _name, component in components.items():
        if not isinstance(component, nn.Module):
            continue
        for module in component.modules():
            for key, buf in list(module._buffers.items()):
                if buf is not None and buf.dtype == torch.float64:
                    module._buffers[key] = buf.to(torch.float32)
            for key, param in list(module._parameters.items()):
                if param is not None and param.dtype == torch.float64:
                    module._parameters[key] = torch.nn.Parameter(
                        param.data.to(torch.float32),
                        requires_grad=param.requires_grad,
                    )


def _load_wan_t2v(api, model_id=None):
    """Load Wan2.1 text-to-video pipeline."""
    global _pipe, _current_model, _pipe_type, _backend
    import torch

    model_id = model_id or WAN_T2V_1_3B

    if (_pipe is not None and _current_model == model_id
            and _pipe_type == "text2video" and _backend == "wan"):
        api.resource_manager.touch(model_id)
        return _pipe

    if _pipe is not None:
        api.release_gpu(_current_model)
        _pipe = None

    device_str, dtype, is_cuda, is_mps = _get_device_and_dtype(api, model_id, vram_gb=10.0)
    hf_token = getattr(api, 'hf_token', None)

    api.log(f"Loading Wan2.1 text-to-video — first run downloads ~3 GB...")

    from diffusers import AutoModel, WanPipeline

    vae = AutoModel.from_pretrained(
        model_id, subfolder="vae", torch_dtype=torch.float32,
        cache_dir=str(api.models_dir), token=hf_token,
    )
    _pipe = WanPipeline.from_pretrained(
        model_id, vae=vae, torch_dtype=dtype,
        cache_dir=str(api.models_dir), token=hf_token,
    )

    is_1_3b = "1.3B" in model_id or "1.3b" in model_id
    shift = WAN_1_3B_FLOW_SHIFT if is_1_3b else WAN_14B_FLOW_SHIFT
    try:
        _pipe.scheduler.config.flow_shift = shift
    except Exception as e:
        log.debug("Could not set scheduler shift: %s", e)

    if is_mps:
        _cast_float64_to_float32(_pipe)

    _pipe.enable_model_cpu_offload()

    try:
        _pipe.enable_vae_slicing()
    except Exception:
        pass
    try:
        _pipe.enable_vae_tiling()
    except Exception:
        pass

    _current_model = model_id
    _pipe_type = "text2video"
    _backend = "wan"
    api.log(f"Wan2.1 T2V ready ({model_id.split('/')[-1]}) on {device_str} "
            f"(shift={shift})")
    return _pipe


def _load_wan_i2v(api, model_id=None):
    """Load Wan2.1 image-to-video pipeline."""
    global _pipe, _current_model, _pipe_type, _backend
    import torch

    model_id = model_id or WAN_I2V_14B_480P

    if (_pipe is not None and _current_model == model_id
            and _pipe_type == "img2video" and _backend == "wan"):
        api.resource_manager.touch(model_id)
        return _pipe

    if _pipe is not None:
        api.release_gpu(_current_model)
        _pipe = None

    device_str, dtype, is_cuda, is_mps = _get_device_and_dtype(api, model_id, vram_gb=14.0)
    hf_token = getattr(api, 'hf_token', None)

    api.log(f"Loading Wan2.1 image-to-video (14B-480P) — first run downloads ~28 GB...")

    from diffusers import AutoencoderKLWan, WanImageToVideoPipeline

    vae = AutoencoderKLWan.from_pretrained(
        model_id, subfolder="vae", torch_dtype=torch.float32,
        cache_dir=str(api.models_dir), token=hf_token,
    )
    _pipe = WanImageToVideoPipeline.from_pretrained(
        model_id, vae=vae, torch_dtype=dtype,
        cache_dir=str(api.models_dir), token=hf_token,
    )

    shift = WAN_14B_FLOW_SHIFT
    try:
        _pipe.scheduler.config.flow_shift = shift
    except Exception as e:
        log.debug("Could not set I2V scheduler shift: %s", e)

    if is_mps:
        _cast_float64_to_float32(_pipe)

    _pipe.enable_model_cpu_offload()

    try:
        _pipe.enable_vae_slicing()
    except Exception:
        pass
    try:
        _pipe.enable_vae_tiling()
    except Exception:
        pass

    _current_model = model_id
    _pipe_type = "img2video"
    _backend = "wan"
    api.log(f"Wan2.1 I2V ready ({model_id.split('/')[-1]}) on {device_str} "
            f"(shift={shift})")
    return _pipe


# ---------------------------------------------------------------------------
# Legacy fallback (LTX-Video / CogVideoX)
# ---------------------------------------------------------------------------

def _load_legacy_pipeline(api, pipeline_type="text2video", model_id=None):
    """Load LTX-Video or CogVideoX as a last-resort fallback."""
    global _pipe, _current_model, _pipe_type, _backend
    import torch

    model_id = model_id or LTX_VIDEO_MODEL

    if (_pipe is not None and _current_model == model_id
            and _pipe_type == pipeline_type and _backend == "diffusers-legacy"):
        api.resource_manager.touch(model_id)
        return _pipe

    if _pipe is not None:
        api.release_gpu(_current_model)
        _pipe = None

    device_str, dtype, is_cuda, is_mps = _get_device_and_dtype(api, model_id, vram_gb=10.0)
    hf_token = getattr(api, 'hf_token', None)

    use_ltx = "LTX" in model_id
    if use_ltx:
        api.log(f"Loading LTX-Video ({pipeline_type}) as fallback...")
        try:
            if pipeline_type == "img2video":
                from diffusers import LTXImageToVideoPipeline
                _pipe = LTXImageToVideoPipeline.from_pretrained(
                    model_id, torch_dtype=dtype, cache_dir=str(api.models_dir),
                    token=hf_token,
                )
            else:
                from diffusers import LTXPipeline
                _pipe = LTXPipeline.from_pretrained(
                    model_id, torch_dtype=dtype, cache_dir=str(api.models_dir),
                    token=hf_token,
                )
        except Exception as e:
            log.warning("LTX-Video load failed (%s), falling back to CogVideoX", e)
            use_ltx = False
            model_id = COGVIDEOX_MODEL

    if not use_ltx:
        api.log(f"Loading CogVideoX ({pipeline_type}) as legacy fallback...")
        from diffusers import CogVideoXPipeline, CogVideoXImageToVideoPipeline
        PipelineCls = (CogVideoXImageToVideoPipeline if pipeline_type == "img2video"
                       else CogVideoXPipeline)
        _pipe = PipelineCls.from_pretrained(
            model_id, torch_dtype=dtype, cache_dir=str(api.models_dir),
            token=hf_token,
        )

    if is_mps:
        _cast_float64_to_float32(_pipe)

    _pipe.to(device_str)
    try:
        _pipe.enable_attention_slicing()
    except Exception:
        pass

    _current_model = model_id
    _pipe_type = pipeline_type
    _backend = "diffusers-legacy"
    api.log(f"Legacy pipeline ready: {model_id} on {device_str}")
    return _pipe


# ---------------------------------------------------------------------------
# Video frame saving
# ---------------------------------------------------------------------------

def _save_video_frames(frames, fps, api, prompt, filename, source_image=None):
    """Write frames to MP4, save to media gallery, return result dict."""
    import numpy as np

    try:
        import imageio
    except ImportError:
        return {"status": "error", "error": "imageio not installed"}

    from PIL import Image as PILImage

    ts = time.strftime("%Y%m%d_%H%M%S")
    fname = filename or f"video_{ts}.mp4"
    if not fname.endswith(".mp4"):
        fname += ".mp4"

    temp_path = Path(api.data_dir) / fname
    temp_path.parent.mkdir(parents=True, exist_ok=True)

    processed = []
    for frame in frames:
        if isinstance(frame, PILImage.Image):
            arr = np.array(frame)
        elif hasattr(frame, 'numpy'):
            arr = frame.numpy()
        elif hasattr(frame, '__array__'):
            arr = np.array(frame)
        else:
            arr = frame
        if hasattr(arr, 'dtype') and arr.dtype in (np.float32, np.float64):
            arr = (arr * 255).clip(0, 255).astype(np.uint8)
        processed.append(arr)

    imageio.mimwrite(str(temp_path), processed, fps=fps, codec="libx264")
    video_bytes = temp_path.read_bytes()

    params = {"model": _current_model or "unknown", "frames": len(processed), "fps": fps}
    if source_image:
        params["source_image"] = str(source_image)

    path = api.save_media(
        data=video_bytes, filename=fname, media_type="video",
        prompt=(prompt or "")[:200], params=params,
        metadata={**params, "prompt": (prompt or "")[:200]},
    )
    temp_path.unlink(missing_ok=True)
    return {"status": "ok", "path": path, "frames": len(processed), "fps": fps}


# ---------------------------------------------------------------------------
# Tool registration
# ---------------------------------------------------------------------------

def register(api):
    has_mlx = _check_mlx_available()

    def execute_text_to_video(prompt="", model="", num_frames=81,
                               fps=16, width=832, height=480,
                               steps=0, guidance_scale=0,
                               seed=None, filename="", **_kw):
        if not prompt:
            return json.dumps({"status": "error", "error": "prompt is required"})

        t0 = time.time()

        # MLX backend — only if user explicitly requests it
        if model.lower() in ("ltx-2-mlx", "mlx") and has_mlx:
            api.log("Trying MLX-native LTX-2 for Apple Silicon...")
            try:
                result = _try_mlx_text_to_video(
                    api, prompt, num_frames=num_frames,
                    height=height, width=width, fps=fps, seed=seed,
                )
                if result:
                    out_path, elapsed = result
                    video_bytes = Path(out_path).read_bytes()
                    fname = filename or Path(out_path).name
                    path = api.save_media(
                        data=video_bytes, filename=fname, media_type="video",
                        prompt=prompt[:200],
                        params={"model": "LTX-2-MLX", "fps": fps},
                        metadata={"model": "LTX-2-MLX", "prompt": prompt[:200],
                                  "fps": fps, "elapsed_secs": round(elapsed, 2)},
                    )
                    Path(out_path).unlink(missing_ok=True)
                    return json.dumps({
                        "status": "ok", "path": path, "backend": "mlx",
                        "fps": fps, "elapsed_secs": round(elapsed, 2),
                    })
            except Exception as e:
                log.warning("MLX video generation failed: %s", e)

        # Apply Wan2.1-tuned defaults if caller used 0 (unset)
        if steps <= 0:
            steps = WAN_DEFAULT_STEPS
        if guidance_scale <= 0:
            guidance_scale = WAN_1_3B_GUIDANCE

        # Primary: Wan2.1 T2V
        try:
            pipe = _load_wan_t2v(api, model_id=model if model and "Wan" in model else None)

            nf = _align_frames_wan(min(num_frames, 81))
            w = _align_dim_wan(width, pipe)
            h = _align_dim_wan(height, pipe)

            api.log(f"Generating video with Wan2.1: {nf} frames, {w}x{h}, "
                    f"{steps} steps, guidance {guidance_scale}...")

            gen_kwargs = {
                "prompt": prompt,
                "negative_prompt": WAN_DEFAULT_NEGATIVE,
                "num_frames": nf,
                "width": w,
                "height": h,
                "num_inference_steps": steps,
                "guidance_scale": guidance_scale,
            }
            if seed is not None:
                import torch
                gen_kwargs["generator"] = torch.Generator().manual_seed(seed)

            result = pipe(**gen_kwargs)
            elapsed = time.time() - t0
            frames = result.frames[0]

            save_result = _save_video_frames(frames, fps, api, prompt, filename)
            save_result["elapsed_secs"] = round(elapsed, 2)
            save_result["backend"] = "wan2.1"
            save_result["model"] = _current_model
            return json.dumps(save_result)

        except Exception as e:
            log.warning("Wan2.1 T2V failed (%s), trying legacy fallback...", e)

        # Fallback: LTX-Video / CogVideoX
        try:
            pipe = _load_legacy_pipeline(api, pipeline_type="text2video")
            use_ltx = "LTX" in (_current_model or "")
            nf = min(num_frames, 161) if use_ltx else min(num_frames, 49)
            w = (width // 32) * 32 if use_ltx else (width // 8) * 8
            h = (height // 32) * 32 if use_ltx else (height // 8) * 8

            api.log(f"Generating video (fallback): {nf} frames, {w}x{h}...")

            gen_kwargs = {
                "prompt": prompt,
                "num_frames": nf,
                "width": w,
                "height": h,
                "num_inference_steps": steps,
            }
            if use_ltx:
                gen_kwargs["negative_prompt"] = (
                    "worst quality, inconsistent motion, blurry, jittery, distorted"
                )

            result = pipe(**gen_kwargs)
            elapsed = time.time() - t0
            frames = result.frames[0]

            save_result = _save_video_frames(frames, fps, api, prompt, filename)
            save_result["elapsed_secs"] = round(elapsed, 2)
            save_result["backend"] = "diffusers-legacy"
            return json.dumps(save_result)

        except Exception as e:
            log.error("text_to_video error: %s", e, exc_info=True)
            return json.dumps({"status": "error", "error": str(e)[:500]})

    def execute_image_to_video(prompt="", image_path="", model="",
                                num_frames=81, fps=16, width=832, height=480,
                                steps=0, guidance_scale=0,
                                seed=None, filename="", **_kw):
        if not image_path:
            return json.dumps({"status": "error", "error": "image_path is required"})
        if not Path(image_path).exists():
            return json.dumps({"status": "error", "error": f"File not found: {image_path}"})

        t0 = time.time()

        # MLX backend — only if user explicitly requests it
        if model.lower() in ("ltx-2-mlx", "mlx") and has_mlx:
            api.log("Trying MLX-native LTX-2 image-to-video...")
            try:
                result = _try_mlx_image_to_video(
                    api, image_path, prompt=prompt,
                    num_frames=num_frames, height=height, width=width, seed=seed,
                )
                if result:
                    out_path, elapsed = result
                    video_bytes = Path(out_path).read_bytes()
                    fname = filename or Path(out_path).name
                    path = api.save_media(
                        data=video_bytes, filename=fname, media_type="video",
                        prompt=prompt[:200],
                        params={"model": "LTX-2-MLX", "source_image": str(image_path)},
                        metadata={"model": "LTX-2-MLX", "prompt": prompt[:200],
                                  "source_image": str(image_path),
                                  "elapsed_secs": round(elapsed, 2)},
                    )
                    Path(out_path).unlink(missing_ok=True)
                    return json.dumps({
                        "status": "ok", "path": path, "backend": "mlx",
                        "fps": fps, "elapsed_secs": round(elapsed, 2),
                    })
            except Exception as e:
                log.warning("MLX image-to-video failed: %s", e)

        if steps <= 0:
            steps = WAN_DEFAULT_STEPS
        if guidance_scale <= 0:
            guidance_scale = WAN_1_3B_GUIDANCE

        # Primary: Wan2.1 I2V
        try:
            from PIL import Image

            pipe = _load_wan_i2v(api, model_id=model if model and "Wan" in model else None)

            nf = _align_frames_wan(min(num_frames, 81))
            w = _align_dim_wan(width, pipe)
            h = _align_dim_wan(height, pipe)

            init_image = Image.open(image_path).convert("RGB").resize((w, h))

            api.log(f"Animating image with Wan2.1: {nf} frames, {w}x{h}, "
                    f"{steps} steps, guidance {guidance_scale}...")

            gen_kwargs = {
                "image": init_image,
                "prompt": prompt or "animate this image with natural cinematic motion",
                "negative_prompt": WAN_DEFAULT_NEGATIVE,
                "num_frames": nf,
                "width": w,
                "height": h,
                "num_inference_steps": steps,
                "guidance_scale": guidance_scale,
            }
            if seed is not None:
                import torch
                gen_kwargs["generator"] = torch.Generator().manual_seed(seed)

            result = pipe(**gen_kwargs)
            elapsed = time.time() - t0
            frames = result.frames[0]

            save_result = _save_video_frames(
                frames, fps, api, prompt, filename, source_image=image_path,
            )
            save_result["elapsed_secs"] = round(elapsed, 2)
            save_result["backend"] = "wan2.1"
            save_result["model"] = _current_model
            return json.dumps(save_result)

        except Exception as e:
            log.warning("Wan2.1 I2V failed (%s), trying legacy fallback...", e)

        # Fallback: LTX-Video / CogVideoX
        try:
            from PIL import Image

            pipe = _load_legacy_pipeline(api, pipeline_type="img2video")
            use_ltx = "LTX" in (_current_model or "")
            w = (width // 32) * 32 if use_ltx else (width // 8) * 8
            h = (height // 32) * 32 if use_ltx else (height // 8) * 8
            nf = min(num_frames, 161) if use_ltx else min(num_frames, 49)

            init_image = Image.open(image_path).convert("RGB").resize((w, h))
            api.log(f"Animating image (fallback): {nf} frames, {w}x{h}...")

            gen_kwargs = {
                "image": init_image,
                "prompt": prompt or "animate this image with natural cinematic motion",
                "num_frames": nf,
                "num_inference_steps": steps,
                "width": w,
                "height": h,
            }

            result = pipe(**gen_kwargs)
            elapsed = time.time() - t0
            frames = result.frames[0]

            save_result = _save_video_frames(
                frames, fps, api, prompt, filename, source_image=image_path,
            )
            save_result["elapsed_secs"] = round(elapsed, 2)
            save_result["backend"] = "diffusers-legacy"
            return json.dumps(save_result)

        except Exception as e:
            log.error("image_to_video error: %s", e, exc_info=True)
            return json.dumps({"status": "error", "error": str(e)[:500]})

    mlx_note = " Pass model='ltx-2-mlx' for MLX-native speed on Apple Silicon." if has_mlx else ""

    api.register_tool({
        "name": "text_to_video",
        "description": (
            f"Generate a video from text using Wan2.1 (local).{mlx_note} "
            "Creates high-quality video clips with coherent motion and subjects. "
            "Default: 480P (832x480) at 16fps, up to 81 frames (~5 seconds). "
            "Works on consumer GPUs (8 GB+ VRAM). CUDA, MPS, and CPU supported. "
            "No API key needed."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "prompt": {"type": "string", "description": "Text description of the video to generate."},
                "model": {
                    "type": "string",
                    "description": (
                        "Model selection. Default: Wan2.1 T2V 1.3B. "
                        "Options: 'ltx-2-mlx' for MLX Apple Silicon, or a HuggingFace model ID."
                    ),
                },
                "num_frames": {"type": "integer", "description": "Number of frames (default 81 = ~5s at 16fps). Aligned to 4k+1 for Wan.", "default": 81},
                "fps": {"type": "integer", "description": "Frames per second (default 16).", "default": 16},
                "width": {"type": "integer", "description": "Video width (default 832 for 480P landscape).", "default": 832},
                "height": {"type": "integer", "description": "Video height (default 480 for 480P landscape).", "default": 480},
                "steps": {
                    "type": "integer",
                    "description": (
                        "Inference steps (default 50). Wan2.1 is a flow-matching model that needs "
                        "50 steps for good quality. Minimum 30 for acceptable results. "
                        "Do NOT use less than 30 — the output will be a blurry mess."
                    ),
                    "default": 0,
                },
                "guidance_scale": {
                    "type": "number",
                    "description": "Prompt guidance scale (default 6.0 for 1.3B). Higher follows prompt more closely.",
                    "default": 0,
                },
                "seed": {"type": "integer", "description": "Random seed for reproducibility (optional)."},
                "filename": {"type": "string", "description": "Output filename (optional)."},
            },
            "required": ["prompt"],
        },
        "execute": execute_text_to_video,
    })

    api.register_tool({
        "name": "image_to_video",
        "description": (
            f"Animate an image into a video using Wan2.1 I2V (local).{mlx_note} "
            "Takes a source image and optional prompt to guide the animation. "
            "High-quality output with coherent motion at 480P, 16fps. "
            "Uses CPU offloading to work on consumer GPUs. No API key needed. "
            "IMPORTANT: Use at least 50 steps for good quality."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "prompt": {"type": "string", "description": "How to animate (e.g. 'slow zoom in', 'camera pan left', 'the dog runs forward')."},
                "image_path": {"type": "string", "description": "Path to the source image."},
                "model": {
                    "type": "string",
                    "description": (
                        "Model selection. Default: Wan2.1 I2V 14B-480P. "
                        "Options: 'ltx-2-mlx' for MLX Apple Silicon, or a HuggingFace model ID."
                    ),
                },
                "num_frames": {"type": "integer", "description": "Number of frames (default 81 = ~5s at 16fps).", "default": 81},
                "fps": {"type": "integer", "description": "Frames per second (default 16).", "default": 16},
                "width": {"type": "integer", "description": "Video width (default 832).", "default": 832},
                "height": {"type": "integer", "description": "Video height (default 480).", "default": 480},
                "steps": {
                    "type": "integer",
                    "description": (
                        "Inference steps (default 50). Needs 50 steps for good quality. "
                        "Do NOT use less than 30."
                    ),
                    "default": 0,
                },
                "guidance_scale": {
                    "type": "number",
                    "description": "Prompt guidance scale (default 6.0).",
                    "default": 0,
                },
                "seed": {"type": "integer", "description": "Random seed for reproducibility (optional)."},
                "filename": {"type": "string", "description": "Output filename (optional)."},
            },
            "required": ["image_path"],
        },
        "execute": execute_image_to_video,
    })
