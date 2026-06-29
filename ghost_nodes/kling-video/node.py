"""
Kling Video Node — cloud video generation via Kling AI API.

Supports text-to-video and image-to-video.
API docs: https://app.klingai.com/cn/dev/document-api/apiReference/model/textToVideo

Models (as of March 2026):
  kling-v3.0-pro  — Latest: 3-15s, multi-shot, native audio, 1080p (Feb 2026)
  kling-v3.0-std  — Latest: 3-15s, multi-shot, native audio, 720p
  kling-video-o3  — Reference-to-video, video editing, element consistency
  kling-v2.6-pro  — Previous gen: native audio + Motion Control
  kling-v2.6-std  — Previous gen: fast generation with audio
  kling-v2.5-turbo — Fastest generation speed
  kling-video-o1  — Previous gen unified multimodal model

Endpoints:
  POST /v1/videos/text2video  — Submit T2V task
  POST /v1/videos/image2video — Submit I2V task
  GET  /v1/videos/{task_id}   — Poll task status

Resolutions: 16:9, 9:16, 1:1 (API handles internally)
Duration (v3): 3-15 seconds | Duration (v2.x): 5 or 10 seconds
"""

import base64
import json
import logging
import time
from pathlib import Path

log = logging.getLogger("quinely.node.kling_video")

PROVIDER = "kling"

KLING_MODELS = [
    "kling-v3.0-pro",
    "kling-v3.0-std",
    "kling-video-o3",
    "kling-v2.6-pro",
    "kling-v2.6-std",
    "kling-v2.5-turbo",
    "kling-video-o1",
]

V3_MODELS = {"kling-v3.0-pro", "kling-v3.0-std", "kling-video-o3"}
V3_DURATIONS = list(range(3, 16))
V2_DURATIONS = [5, 10]

COST_PER_SECOND = {
    "v3_pro": 0.15,
    "v3_std": 0.08,
    "v2_pro": 0.056,
    "v2_std": 0.014,
}


def _estimate_cost(model: str, mode: str, duration: int) -> float:
    is_v3 = model in V3_MODELS
    is_pro = "pro" in model or "pro" in mode or "o1" in model or "o3" in model
    if is_v3:
        rate = COST_PER_SECOND["v3_pro"] if is_pro else COST_PER_SECOND["v3_std"]
    else:
        rate = COST_PER_SECOND["v2_pro"] if is_pro else COST_PER_SECOND["v2_std"]
    return round(rate * duration, 2)


def register(api):
    cloud = api.cloud_providers
    if not cloud:
        log.warning("Cloud providers not available — kling-video node disabled")
        return

    def execute_kling_t2v(prompt="", multi_prompt=None, duration=5,
                          aspect_ratio="16:9", model="kling-v3.0-pro",
                          mode="standard", negative_prompt="", cfg_scale=0.5,
                          camera_control="", generate_audio=True,
                          shot_type="customize", **_kw):
        if not prompt and not multi_prompt:
            return json.dumps({"status": "error", "error": "prompt or multi_prompt is required"})

        key = api.get_provider_key(PROVIDER)
        if not key:
            return json.dumps({
                "status": "error",
                "error": "Kling API keys not configured. Set Access Key + Secret Key in Dashboard > Config > Cloud Providers, or set KLING_ACCESS_KEY and KLING_SECRET_KEY env vars.",
            })

        if not cloud.get_secret_key(PROVIDER):
            return json.dumps({
                "status": "error",
                "error": "Kling Secret Key not configured. Kling requires both an Access Key and a Secret Key for JWT authentication.",
            })

        if not cloud.check_budget(PROVIDER):
            remaining = cloud.get_budget_remaining(PROVIDER)
            return json.dumps({
                "status": "error",
                "error": f"Monthly Kling budget exhausted (remaining: ${remaining:.2f}). Increase budget in config or wait for next month.",
            })

        is_v3 = model in V3_MODELS
        if is_v3:
            duration = max(3, min(15, duration))
        else:
            duration = 5 if duration < 8 else 10

        api.log(f"Submitting Kling {model} text-to-video ({mode}, {duration}s, {aspect_ratio})...")
        t0 = time.time()

        payload = {
            "model": model,
            "duration": str(duration),
            "aspect_ratio": aspect_ratio,
            "mode": mode,
            "cfg_scale": max(0.0, min(1.0, cfg_scale)),
        }

        if multi_prompt and is_v3:
            payload["multi_prompt"] = multi_prompt
            payload["shot_type"] = shot_type
        else:
            payload["prompt"] = (prompt or "")[:2500]

        if negative_prompt:
            payload["negative_prompt"] = negative_prompt[:2500]
        if camera_control and not is_v3:
            payload["camera_control"] = {"type": camera_control}
        if generate_audio:
            payload["generate_audio"] = True

        try:
            result = cloud.api_post(PROVIDER, "/videos/text2video", payload)
        except RuntimeError as e:
            return json.dumps({"status": "error", "error": str(e)[:500]})

        data = result.get("data", result)
        task_id = data.get("task_id", "")
        if not task_id:
            return json.dumps({"status": "error", "error": f"No task_id returned: {json.dumps(result)[:300]}"})

        api.log(f"Kling job submitted (task: {task_id}). Polling for completion...")

        try:
            completed = cloud.poll_until_complete(
                status_url=f"{cloud.get_api_base(PROVIDER)}/videos/{task_id}",
                headers=cloud.get_auth_headers(PROVIDER),
                timeout=360,
                interval=8,
                success_statuses=("completed", "success", "succeed"),
                fail_statuses=("failed", "error", "cancelled"),
                status_key="status",
                data_key="data",
                log_prefix="Kling T2V",
            )
        except RuntimeError as e:
            return json.dumps({"status": "error", "error": str(e)[:500]})

        return _download_and_save(api, cloud, completed, prompt, t0, mode, duration, model)

    def execute_kling_i2v(prompt="", image_path="", image_url="",
                          end_image_url="", duration=5, model="kling-v3.0-pro",
                          mode="standard", negative_prompt="", cfg_scale=0.5,
                          generate_audio=True, **_kw):
        if not image_path and not image_url:
            return json.dumps({"status": "error", "error": "image_path or image_url is required"})

        key = api.get_provider_key(PROVIDER)
        if not key:
            return json.dumps({
                "status": "error",
                "error": "Kling API keys not configured. Set Access Key + Secret Key in Dashboard > Config > Cloud Providers, or set KLING_ACCESS_KEY and KLING_SECRET_KEY env vars.",
            })

        if not cloud.get_secret_key(PROVIDER):
            return json.dumps({
                "status": "error",
                "error": "Kling Secret Key not configured. Kling requires both an Access Key and a Secret Key for JWT authentication.",
            })

        if not cloud.check_budget(PROVIDER):
            return json.dumps({"status": "error", "error": "Monthly Kling budget exhausted."})

        if image_path and not image_url:
            p = Path(image_path)
            if not p.exists():
                return json.dumps({"status": "error", "error": f"File not found: {image_path}"})
            if p.stat().st_size > 10 * 1024 * 1024:
                return json.dumps({"status": "error", "error": "Image exceeds 10MB limit"})
            image_data = p.read_bytes()
            b64 = base64.b64encode(image_data).decode("utf-8")
            suffix = p.suffix.lower().lstrip(".")
            mime = {"png": "image/png", "jpg": "image/jpeg", "jpeg": "image/jpeg",
                    "webp": "image/webp"}.get(suffix, "image/jpeg")
            image_url = f"data:{mime};base64,{b64}"

        is_v3 = model in V3_MODELS
        if is_v3:
            duration = max(3, min(15, duration))
        else:
            duration = 5 if duration < 8 else 10

        api.log(f"Submitting Kling {model} image-to-video ({mode}, {duration}s)...")
        t0 = time.time()

        image_key = "start_image_url" if is_v3 else "image"
        payload = {
            "model": model,
            image_key: image_url,
            "prompt": (prompt or "animate this image with natural cinematic motion")[:2500],
            "duration": str(duration),
            "mode": mode,
            "cfg_scale": max(0.0, min(1.0, cfg_scale)),
        }
        if negative_prompt:
            payload["negative_prompt"] = negative_prompt[:2500]
        if end_image_url and is_v3:
            payload["end_image_url"] = end_image_url
        if generate_audio:
            payload["generate_audio"] = True

        try:
            result = cloud.api_post(PROVIDER, "/videos/image2video", payload)
        except RuntimeError as e:
            return json.dumps({"status": "error", "error": str(e)[:500]})

        data = result.get("data", result)
        task_id = data.get("task_id", "")
        if not task_id:
            return json.dumps({"status": "error", "error": f"No task_id returned: {json.dumps(result)[:300]}"})

        api.log(f"Kling I2V job submitted (task: {task_id}). Polling for completion...")

        try:
            completed = cloud.poll_until_complete(
                status_url=f"{cloud.get_api_base(PROVIDER)}/videos/{task_id}",
                headers=cloud.get_auth_headers(PROVIDER),
                timeout=360,
                interval=8,
                success_statuses=("completed", "success", "succeed"),
                fail_statuses=("failed", "error", "cancelled"),
                status_key="status",
                data_key="data",
                log_prefix="Kling I2V",
            )
        except RuntimeError as e:
            return json.dumps({"status": "error", "error": str(e)[:500]})

        return _download_and_save(api, cloud, completed, prompt, t0, mode, duration, model,
                                  source_image=image_path)

    def _download_and_save(api, cloud, completed_data, prompt, t0, mode, duration,
                           model, source_image=""):
        video_url = ""

        response_urls = completed_data.get("response", [])
        if isinstance(response_urls, list) and response_urls:
            video_url = response_urls[0]
        elif isinstance(response_urls, str) and response_urls.startswith("http"):
            video_url = response_urls

        if not video_url:
            works = completed_data.get("works", completed_data.get("videos", []))
            if isinstance(works, list) and works:
                item = works[0]
                if isinstance(item, dict):
                    res = item.get("resource", {})
                    video_url = res.get("resource", "") if isinstance(res, dict) else str(res)
                elif isinstance(item, str) and item.startswith("http"):
                    video_url = item

        if not video_url:
            for key in ("video_url", "download_url", "url", "video"):
                candidate = completed_data.get(key, "")
                if isinstance(candidate, str) and candidate.startswith("http"):
                    video_url = candidate
                    break

        if not video_url:
            return json.dumps({
                "status": "error",
                "error": f"Could not extract video URL from response: {json.dumps(completed_data)[:500]}",
            })

        api.log("Downloading video from Kling...")
        try:
            video_bytes = cloud.download_file(video_url)
        except Exception as e:
            return json.dumps({"status": "error", "error": f"Download failed: {e}"})

        elapsed = time.time() - t0
        cost = _estimate_cost(model, mode, duration)
        credits_used = completed_data.get("consumed_credits", 0)
        ts = time.strftime("%Y%m%d_%H%M%S")
        fname = f"kling_{ts}.mp4"

        params_dict = {
            "model": model, "mode": mode, "duration": duration,
            "provider": PROVIDER,
        }
        if source_image:
            params_dict["source_image"] = str(source_image)

        path = api.save_media(
            data=video_bytes,
            filename=fname,
            media_type="video",
            prompt=(prompt or "")[:200],
            params=params_dict,
            metadata={
                "provider": PROVIDER, "model": model, "mode": mode,
                "duration_secs": duration, "cost_usd": cost,
                "credits_used": credits_used,
                "elapsed_secs": round(elapsed, 2),
                "prompt": (prompt or "")[:200],
            },
            provider=PROVIDER,
            cost_usd=cost,
        )

        cloud.track_cost(PROVIDER, "text_to_video" if not source_image else "image_to_video", cost)
        api.log(f"Kling video saved: {fname} (${cost:.2f}, {elapsed:.1f}s)")

        return json.dumps({
            "status": "ok",
            "path": path,
            "provider": PROVIDER,
            "model": model,
            "cost_usd": cost,
            "duration_secs": duration,
            "elapsed_secs": round(elapsed, 2),
        })

    api.register_tool({
        "name": "kling_text_to_video",
        "description": (
            "Generate a high-quality video from text using Kling AI (cloud API). "
            "Produces cinematic video with coherent subjects and motion. "
            "V3 models (Feb 2026): 3-15s duration, multi-shot narratives, native audio on by default. "
            "V2 models: 5 or 10 seconds. Modes: 'standard' (faster, cheaper) or 'pro' (best quality). "
            "Models: kling-v3.0-pro (latest, multi-shot, 1080p), kling-v3.0-std (latest, 720p), "
            "kling-video-o3 (reference/editing), kling-v2.6-pro (audio+motion), "
            "kling-v2.6-std, kling-v2.5-turbo, kling-video-o1. "
            "Aspect ratios: 16:9, 9:16, 1:1. "
            "REQUIRES a Kling API key. PAID service — v3 ~$0.08-$0.15/s, v2 ~$0.01-$0.06/s. "
            "Use for high-quality final output. For free drafts, use text_to_video (local) instead."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "prompt": {"type": "string", "description": "Detailed text description of the video (max 2500 chars). Use this OR multi_prompt, not both."},
                "multi_prompt": {
                    "type": "array",
                    "description": "V3 only: list of shot descriptions for multi-shot narrative video. Each item is a string prompt for one shot. Overrides prompt.",
                    "items": {"type": "string"},
                },
                "duration": {
                    "type": "integer",
                    "description": "Video duration in seconds. V3: 3-15s. V2: 5 or 10s. Default: 5.",
                    "default": 5,
                },
                "aspect_ratio": {
                    "type": "string",
                    "description": "Aspect ratio. Default: 16:9.",
                    "enum": ["16:9", "9:16", "1:1"],
                    "default": "16:9",
                },
                "model": {
                    "type": "string",
                    "description": "Kling model. Default: kling-v3.0-pro (latest, best quality, multi-shot, native audio).",
                    "enum": KLING_MODELS,
                    "default": "kling-v3.0-pro",
                },
                "mode": {
                    "type": "string",
                    "description": "Generation mode: 'standard' (faster, cheaper) or 'pro' (best quality). Default: standard.",
                    "enum": ["standard", "pro"],
                    "default": "standard",
                },
                "negative_prompt": {"type": "string", "description": "Things to avoid in the video (max 2500 chars, optional)."},
                "cfg_scale": {"type": "number", "description": "CFG guidance scale 0.0-1.0. Higher = more prompt adherence. Default: 0.5.", "default": 0.5},
                "camera_control": {
                    "type": "string",
                    "description": "Camera movement type (optional). V2.6-pro only, not supported on V3.",
                    "enum": ["simple", "down_back", "forward_up", "right_turn_forward", "left_turn_forward"],
                },
                "generate_audio": {"type": "boolean", "description": "Generate native audio with the video. Default: true (V3 default).", "default": True},
                "shot_type": {
                    "type": "string",
                    "description": "V3 multi-shot mode: 'customize' (manual shot control) or 'intelligent' (AI decides cuts). Default: customize.",
                    "enum": ["customize", "intelligent"],
                    "default": "customize",
                },
            },
            "required": ["prompt"],
        },
        "execute": execute_kling_t2v,
    })

    api.register_tool({
        "name": "kling_image_to_video",
        "description": (
            "Animate an image into a high-quality video using Kling AI (cloud API). "
            "Takes a source image (JPG/PNG, max 10MB, min 300px) and optional prompt to guide animation. "
            "V3: 3-15s with start/end frame control and native audio. V2: 5 or 10s. "
            "REQUIRES a Kling API key. PAID service."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "prompt": {"type": "string", "description": "How to animate the image (e.g. 'slow zoom in', 'the dog runs forward')."},
                "image_path": {"type": "string", "description": "Path to the source image (JPG/PNG, max 10MB, min 300px)."},
                "image_url": {"type": "string", "description": "URL of the source image (alternative to image_path)."},
                "end_image_url": {"type": "string", "description": "V3 only: URL of end-frame image for scene transition control."},
                "duration": {
                    "type": "integer",
                    "description": "Video duration in seconds. V3: 3-15s. V2: 5 or 10s. Default: 5.",
                    "default": 5,
                },
                "model": {
                    "type": "string",
                    "description": "Kling model. Default: kling-v3.0-pro (latest).",
                    "enum": KLING_MODELS,
                    "default": "kling-v3.0-pro",
                },
                "mode": {
                    "type": "string",
                    "description": "Generation mode: 'standard' or 'pro'. Default: standard.",
                    "enum": ["standard", "pro"],
                    "default": "standard",
                },
                "negative_prompt": {"type": "string", "description": "Things to avoid (optional)."},
                "cfg_scale": {"type": "number", "description": "CFG guidance scale 0.0-1.0. Default: 0.5.", "default": 0.5},
                "generate_audio": {"type": "boolean", "description": "Generate native audio. Default: true.", "default": True},
            },
            "required": ["image_path"],
        },
        "execute": execute_kling_i2v,
    })
