"""
Minimax Video Node — cloud video generation via Minimax (Hailuo) API.

API docs: https://platform.minimax.io/docs/api-reference/video-generation-t2v

Models (as of 2025):
  T2V-01           — Base text-to-video model
  T2V-01-Director  — Camera control via [command] syntax
  MiniMax-Hailuo-02    — Newer model with higher quality
  MiniMax-Hailuo-2.3   — Latest model

Endpoints:
  POST /v1/video_generation              — Submit generation task
  GET  /v1/query/video_generation?task_id=xxx — Poll task status
  GET  /v1/files/retrieve?file_id=xxx    — Get download URL for completed video

Duration: 6 or 10 seconds (varies by model/resolution)
Resolution: 720P, 768P, 1080P (model-dependent)
Statuses: Preparing, Queueing, Processing, Success, Fail

Camera control (T2V-01-Director): Use [command] in prompt, e.g. [truck left], [pan right],
  [push in], [pull out], [pedestal up], [tilt down], [zoom in], [shake], [tracking shot], [static shot]
"""

import json
import logging
import time
from pathlib import Path

log = logging.getLogger("quinely.node.minimax_video")

PROVIDER = "minimax"

T2V_MODELS = [
    "T2V-01",
    "T2V-01-Director",
    "MiniMax-Hailuo-02",
    "MiniMax-Hailuo-2.3",
]

I2V_MODELS = [
    "I2V-01",
    "I2V-01-live",
]

COST_ESTIMATES = {
    6: 0.23,
    10: 0.56,
}


def _estimate_cost(duration: int) -> float:
    return COST_ESTIMATES.get(duration, 0.23)


def register(api):
    cloud = api.cloud_providers
    if not cloud:
        log.warning("Cloud providers not available — minimax-video node disabled")
        return

    def execute_minimax_t2v(prompt="", duration=6, model="MiniMax-Hailuo-2.3",
                            resolution="720P", prompt_optimizer=True, **_kw):
        if not prompt:
            return json.dumps({"status": "error", "error": "prompt is required"})

        key = api.get_provider_key(PROVIDER)
        if not key:
            return json.dumps({
                "status": "error",
                "error": "Minimax API key not configured. Set it in Dashboard > Config > Cloud Providers, or set MINIMAX_API_KEY env var.",
            })

        if not cloud.check_budget(PROVIDER):
            return json.dumps({"status": "error", "error": "Monthly Minimax budget exhausted."})

        duration = 6 if duration < 8 else 10

        api.log(f"Submitting Minimax {model} text-to-video ({duration}s, {resolution})...")
        t0 = time.time()

        payload = {
            "model": model,
            "prompt": prompt[:2000],
            "prompt_optimizer": prompt_optimizer,
        }

        try:
            result = cloud.api_post(PROVIDER, "/video_generation", payload)
        except RuntimeError as e:
            return json.dumps({"status": "error", "error": str(e)[:500]})

        task_id = result.get("task_id", result.get("id", ""))
        if not task_id:
            base_resp = result.get("base_resp", {})
            if base_resp.get("status_code", 0) != 0:
                return json.dumps({"status": "error", "error": f"API error: {base_resp.get('status_msg', json.dumps(result)[:300])}"})
            return json.dumps({"status": "error", "error": f"No task ID: {json.dumps(result)[:300]}"})

        api.log(f"Minimax job submitted (task: {task_id}). Polling...")

        try:
            completed = cloud.poll_until_complete(
                status_url=f"{cloud.get_api_base(PROVIDER)}/query/video_generation?task_id={task_id}",
                headers=cloud.get_auth_headers(PROVIDER),
                timeout=360,
                interval=10,
                success_statuses=("success",),
                fail_statuses=("fail", "failed", "error"),
                status_key="status",
                log_prefix="Minimax T2V",
            )
        except RuntimeError as e:
            return json.dumps({"status": "error", "error": str(e)[:500]})

        return _download_and_save(api, cloud, completed, prompt, t0, duration, model)

    def execute_minimax_i2v(prompt="", image_path="", image_url="",
                            duration=6, model="I2V-01", **_kw):
        if not image_path and not image_url:
            return json.dumps({"status": "error", "error": "image_path or image_url is required"})

        key = api.get_provider_key(PROVIDER)
        if not key:
            return json.dumps({
                "status": "error",
                "error": "Minimax API key not configured.",
            })

        if not cloud.check_budget(PROVIDER):
            return json.dumps({"status": "error", "error": "Monthly Minimax budget exhausted."})

        if image_path and not image_url:
            import base64
            p = Path(image_path)
            if not p.exists():
                return json.dumps({"status": "error", "error": f"File not found: {image_path}"})
            b64 = base64.b64encode(p.read_bytes()).decode("utf-8")
            suffix = p.suffix.lower().lstrip(".")
            mime = {"png": "image/png", "jpg": "image/jpeg", "jpeg": "image/jpeg",
                    "webp": "image/webp"}.get(suffix, "image/jpeg")
            image_url = f"data:{mime};base64,{b64}"

        duration = 6 if duration < 8 else 10

        api.log(f"Submitting Minimax {model} image-to-video ({duration}s)...")
        t0 = time.time()

        payload = {
            "model": model,
            "first_frame_image": image_url,
        }
        if prompt:
            payload["prompt"] = prompt[:2000]

        try:
            result = cloud.api_post(PROVIDER, "/video_generation", payload)
        except RuntimeError as e:
            return json.dumps({"status": "error", "error": str(e)[:500]})

        task_id = result.get("task_id", result.get("id", ""))
        if not task_id:
            return json.dumps({"status": "error", "error": f"No task ID: {json.dumps(result)[:300]}"})

        api.log(f"Minimax I2V job submitted (task: {task_id}). Polling...")

        try:
            completed = cloud.poll_until_complete(
                status_url=f"{cloud.get_api_base(PROVIDER)}/query/video_generation?task_id={task_id}",
                headers=cloud.get_auth_headers(PROVIDER),
                timeout=360,
                interval=10,
                success_statuses=("success",),
                fail_statuses=("fail", "failed", "error"),
                status_key="status",
                log_prefix="Minimax I2V",
            )
        except RuntimeError as e:
            return json.dumps({"status": "error", "error": str(e)[:500]})

        return _download_and_save(api, cloud, completed, prompt, t0, duration, model,
                                  source_image=image_path)

    def _download_and_save(api, cloud, completed_data, prompt, t0, duration, model,
                           source_image=""):
        file_id = completed_data.get("file_id", "")
        video_url = ""

        if file_id:
            try:
                file_info = cloud.api_get(PROVIDER, f"/files/retrieve?file_id={file_id}")
                file_obj = file_info.get("file", file_info)
                video_url = file_obj.get("download_url", "")
            except Exception as e:
                log.warning("Failed to retrieve file %s: %s", file_id, e)

        if not video_url:
            video_url = (completed_data.get("download_url", "")
                        or completed_data.get("video_url", "")
                        or completed_data.get("url", ""))

        if not video_url:
            return json.dumps({
                "status": "error",
                "error": f"No video URL in response: {json.dumps(completed_data)[:500]}",
            })

        api.log("Downloading video from Minimax...")
        try:
            video_bytes = cloud.download_file(video_url)
        except Exception as e:
            return json.dumps({"status": "error", "error": f"Download failed: {e}"})

        elapsed = time.time() - t0
        cost = _estimate_cost(duration)
        video_w = completed_data.get("video_width", 0)
        video_h = completed_data.get("video_height", 0)
        ts = time.strftime("%Y%m%d_%H%M%S")
        fname = f"minimax_{ts}.mp4"

        params_dict = {"model": model, "duration": duration, "provider": PROVIDER}
        if source_image:
            params_dict["source_image"] = str(source_image)

        path = api.save_media(
            data=video_bytes, filename=fname, media_type="video",
            prompt=(prompt or "")[:200], params=params_dict,
            metadata={
                "provider": PROVIDER, "model": model,
                "duration_secs": duration, "cost_usd": cost,
                "elapsed_secs": round(elapsed, 2),
                "prompt": (prompt or "")[:200],
                "resolution": f"{video_w}x{video_h}" if video_w and video_h else "",
            },
            provider=PROVIDER, cost_usd=cost,
        )

        cloud.track_cost(PROVIDER, "text_to_video" if not source_image else "image_to_video", cost)
        api.log(f"Minimax video saved: {fname} (${cost:.2f}, {elapsed:.1f}s)")

        return json.dumps({
            "status": "ok", "path": path, "provider": PROVIDER,
            "model": model, "cost_usd": cost, "duration_secs": duration,
            "elapsed_secs": round(elapsed, 2),
        })

    api.register_tool({
        "name": "minimax_text_to_video",
        "description": (
            "Generate a video from text using Minimax/Hailuo (cloud API). "
            "Natural motion and character consistency. "
            "Models: MiniMax-Hailuo-2.3 (latest), MiniMax-Hailuo-02, T2V-01, "
            "T2V-01-Director (camera control via [command] syntax in prompt). "
            "Duration: 6 or 10 seconds. "
            "Camera commands (Director model): [truck left], [pan right], [push in], [zoom in], [static shot], etc. "
            "REQUIRES a Minimax API key. PAID service (~$0.23/6s, ~$0.56/10s)."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "prompt": {"type": "string", "description": "Text description (max 2000 chars). For T2V-01-Director, include [camera command]."},
                "duration": {"type": "integer", "description": "Duration: 6 or 10 seconds. Default: 6.", "enum": [6, 10], "default": 6},
                "model": {
                    "type": "string",
                    "description": "Model. Default: MiniMax-Hailuo-2.3 (latest, best quality).",
                    "enum": T2V_MODELS,
                    "default": "MiniMax-Hailuo-2.3",
                },
                "prompt_optimizer": {"type": "boolean", "description": "Auto-optimize prompt for better results. Default: true.", "default": True},
            },
            "required": ["prompt"],
        },
        "execute": execute_minimax_t2v,
    })

    api.register_tool({
        "name": "minimax_image_to_video",
        "description": (
            "Animate an image using Minimax/Hailuo (cloud API). "
            "Image input: JPG/PNG/JPEG, aspect ratio between 1:4 and 4:1. "
            "Duration: 6 or 10 seconds. "
            "REQUIRES a Minimax API key. PAID service (~$0.23/6s, ~$0.56/10s)."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "prompt": {"type": "string", "description": "Animation guidance (max 2000 chars)."},
                "image_path": {"type": "string", "description": "Path to source image (JPG/PNG)."},
                "image_url": {"type": "string", "description": "HTTPS URL of source image."},
                "duration": {"type": "integer", "description": "Duration: 6 or 10 seconds. Default: 6.", "enum": [6, 10], "default": 6},
                "model": {"type": "string", "description": "Model. Default: I2V-01.", "enum": I2V_MODELS, "default": "I2V-01"},
            },
            "required": ["image_path"],
        },
        "execute": execute_minimax_i2v,
    })
