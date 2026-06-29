"""
Runway Video Node — cloud video generation via Runway API.

API docs: https://docs.dev.runwayml.com/

Models (as of 2025):
  gen4.5      — Latest flagship model
  gen4_turbo  — Fast Gen-4 variant
  gen3a_turbo — Gen-3 Alpha Turbo (legacy)
  veo3.1      — Google Veo integration
  veo3.1_fast — Veo fast variant
  veo3        — Google Veo base

Endpoint:
  POST /v1/image_to_video — Create generation task (also used for T2V with promptText only)
  GET  /v1/tasks/{taskId} — Poll task status

Required header: X-Runway-Version: 2024-11-06

Ratios are pixel dimensions: "1280:720", "720:1280", "1104:832", "960:960", "832:1104", "1584:672"
Duration: 2-10 seconds (integer)
Statuses: PENDING, SUCCEEDED, FAILED, CANCELED

Image input: HTTPS URL or data URI (base64). URL max 16MB, data URI max 5MB.
Video output: artifacts[].url
"""

import json
import logging
import time
from pathlib import Path

log = logging.getLogger("quinely.node.runway_video")

PROVIDER = "runway"

RUNWAY_MODELS = [
    "gen4.5",
    "gen4_turbo",
    "gen3a_turbo",
    "veo3.1",
    "veo3.1_fast",
    "veo3",
]

RATIO_MAP = {
    "16:9": "1280:720",
    "9:16": "720:1280",
    "4:3": "1104:832",
    "1:1": "960:960",
    "3:4": "832:1104",
    "21:9": "1584:672",
    "1280:720": "1280:720",
    "720:1280": "720:1280",
    "1104:832": "1104:832",
    "960:960": "960:960",
    "832:1104": "832:1104",
    "1584:672": "1584:672",
}

RUNWAY_EXTRA_HEADERS = {
    "X-Runway-Version": "2024-11-06",
}

COST_PER_SECOND = 0.05


def _estimate_cost(duration: int) -> float:
    return round(duration * COST_PER_SECOND, 2)


def register(api):
    cloud = api.cloud_providers
    if not cloud:
        log.warning("Cloud providers not available — runway-video node disabled")
        return

    def execute_runway_t2v(prompt="", duration=5, ratio="16:9",
                           model="gen4_turbo", seed=None, **_kw):
        if not prompt:
            return json.dumps({"status": "error", "error": "prompt is required"})

        key = api.get_provider_key(PROVIDER)
        if not key:
            return json.dumps({
                "status": "error",
                "error": "Runway API key not configured. Set it in Dashboard > Config > Cloud Providers, or set RUNWAY_API_KEY env var.",
            })

        if not cloud.check_budget(PROVIDER):
            return json.dumps({"status": "error", "error": "Monthly Runway budget exhausted."})

        duration = max(2, min(duration, 10))
        pixel_ratio = RATIO_MAP.get(ratio, "1280:720")

        api.log(f"Submitting Runway {model} text-to-video ({duration}s, {pixel_ratio})...")
        t0 = time.time()

        payload = {
            "promptText": prompt[:1000],
            "model": model,
            "duration": duration,
            "ratio": pixel_ratio,
        }
        if seed is not None:
            payload["seed"] = max(0, min(seed, 4294967295))

        try:
            result = cloud.api_post(
                PROVIDER, "/image_to_video", payload,
                extra_headers=RUNWAY_EXTRA_HEADERS,
            )
        except RuntimeError as e:
            return json.dumps({"status": "error", "error": str(e)[:500]})

        task_id = result.get("id", "")
        if not task_id:
            return json.dumps({"status": "error", "error": f"No task ID: {json.dumps(result)[:300]}"})

        api.log(f"Runway job submitted (task: {task_id}). Polling...")

        headers = {**cloud.get_auth_headers(PROVIDER), **RUNWAY_EXTRA_HEADERS}
        try:
            completed = cloud.poll_until_complete(
                status_url=f"{cloud.get_api_base(PROVIDER)}/tasks/{task_id}",
                headers=headers,
                timeout=360,
                interval=10,
                success_statuses=("succeeded",),
                fail_statuses=("failed", "canceled"),
                status_key="status",
                log_prefix="Runway T2V",
            )
        except RuntimeError as e:
            return json.dumps({"status": "error", "error": str(e)[:500]})

        return _download_and_save(api, cloud, completed, prompt, t0, duration, model)

    def execute_runway_i2v(prompt="", image_path="", image_url="",
                           duration=5, model="gen4_turbo", ratio="16:9",
                           seed=None, **_kw):
        if not image_path and not image_url:
            return json.dumps({"status": "error", "error": "image_path or image_url is required"})

        key = api.get_provider_key(PROVIDER)
        if not key:
            return json.dumps({
                "status": "error",
                "error": "Runway API key not configured.",
            })

        if not cloud.check_budget(PROVIDER):
            return json.dumps({"status": "error", "error": "Monthly Runway budget exhausted."})

        if image_path and not image_url:
            import base64
            p = Path(image_path)
            if not p.exists():
                return json.dumps({"status": "error", "error": f"File not found: {image_path}"})
            if p.stat().st_size > 5 * 1024 * 1024:
                return json.dumps({"status": "error", "error": "Image exceeds 5MB limit for data URI (use image_url for up to 16MB)"})
            b64 = base64.b64encode(p.read_bytes()).decode("utf-8")
            suffix = p.suffix.lower().lstrip(".")
            mime = {"png": "image/png", "jpg": "image/jpeg", "jpeg": "image/jpeg",
                    "webp": "image/webp"}.get(suffix, "image/jpeg")
            image_url = f"data:{mime};base64,{b64}"

        duration = max(2, min(duration, 10))
        pixel_ratio = RATIO_MAP.get(ratio, "1280:720")

        api.log(f"Submitting Runway {model} image-to-video ({duration}s, {pixel_ratio})...")
        t0 = time.time()

        payload = {
            "promptImage": image_url,
            "model": model,
            "duration": duration,
            "ratio": pixel_ratio,
        }
        if prompt:
            payload["promptText"] = prompt[:1000]
        if seed is not None:
            payload["seed"] = max(0, min(seed, 4294967295))

        try:
            result = cloud.api_post(
                PROVIDER, "/image_to_video", payload,
                extra_headers=RUNWAY_EXTRA_HEADERS,
            )
        except RuntimeError as e:
            return json.dumps({"status": "error", "error": str(e)[:500]})

        task_id = result.get("id", "")
        if not task_id:
            return json.dumps({"status": "error", "error": f"No task ID: {json.dumps(result)[:300]}"})

        api.log(f"Runway I2V job submitted (task: {task_id}). Polling...")

        headers = {**cloud.get_auth_headers(PROVIDER), **RUNWAY_EXTRA_HEADERS}
        try:
            completed = cloud.poll_until_complete(
                status_url=f"{cloud.get_api_base(PROVIDER)}/tasks/{task_id}",
                headers=headers,
                timeout=360,
                interval=10,
                success_statuses=("succeeded",),
                fail_statuses=("failed", "canceled"),
                status_key="status",
                log_prefix="Runway I2V",
            )
        except RuntimeError as e:
            return json.dumps({"status": "error", "error": str(e)[:500]})

        return _download_and_save(api, cloud, completed, prompt, t0, duration, model,
                                  source_image=image_path)

    def _download_and_save(api, cloud, completed_data, prompt, t0, duration, model,
                           source_image=""):
        video_url = ""

        artifacts = completed_data.get("artifacts", [])
        if isinstance(artifacts, list):
            for artifact in artifacts:
                if isinstance(artifact, dict):
                    url = artifact.get("url", "")
                    if url:
                        video_url = url
                        break
                elif isinstance(artifact, str) and artifact.startswith("http"):
                    video_url = artifact
                    break

        if not video_url:
            for key in ("output", "url", "video_url", "download_url"):
                candidate = completed_data.get(key, "")
                if isinstance(candidate, str) and candidate.startswith("http"):
                    video_url = candidate
                    break
                if isinstance(candidate, list) and candidate:
                    first = candidate[0]
                    if isinstance(first, str) and first.startswith("http"):
                        video_url = first
                        break

        if not video_url:
            return json.dumps({
                "status": "error",
                "error": f"No video URL in response: {json.dumps(completed_data)[:500]}",
            })

        api.log("Downloading video from Runway...")
        try:
            video_bytes = cloud.download_file(video_url)
        except Exception as e:
            return json.dumps({"status": "error", "error": f"Download failed: {e}"})

        elapsed = time.time() - t0
        cost = _estimate_cost(duration)
        ts = time.strftime("%Y%m%d_%H%M%S")
        fname = f"runway_{ts}.mp4"

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
            },
            provider=PROVIDER, cost_usd=cost,
        )

        cloud.track_cost(PROVIDER, "text_to_video" if not source_image else "image_to_video", cost)
        api.log(f"Runway video saved: {fname} (${cost:.2f}, {elapsed:.1f}s)")

        return json.dumps({
            "status": "ok", "path": path, "provider": PROVIDER,
            "model": model, "cost_usd": cost, "duration_secs": duration,
            "elapsed_secs": round(elapsed, 2),
        })

    api.register_tool({
        "name": "runway_text_to_video",
        "description": (
            "Generate a video from text using Runway (cloud API). "
            "Cinematic quality with scene-level understanding. "
            "Models: gen4.5 (latest), gen4_turbo (fast), veo3.1 (Google Veo). "
            "Duration: 2-10 seconds. "
            "Ratios: 16:9 (landscape), 9:16 (portrait), 1:1 (square), 4:3, 21:9 (ultrawide). "
            "REQUIRES a Runway API key. PAID service (~$0.05/second)."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "prompt": {"type": "string", "description": "Text description (max 1000 chars)."},
                "duration": {"type": "integer", "description": "Duration in seconds (2-10). Default: 5.", "default": 5},
                "ratio": {
                    "type": "string",
                    "description": "Aspect ratio. Default: 16:9.",
                    "enum": ["16:9", "9:16", "1:1", "4:3", "3:4", "21:9"],
                    "default": "16:9",
                },
                "model": {
                    "type": "string",
                    "description": "Model. Default: gen4_turbo.",
                    "enum": RUNWAY_MODELS,
                    "default": "gen4_turbo",
                },
                "seed": {"type": "integer", "description": "Seed for reproducibility (0-4294967295, optional)."},
            },
            "required": ["prompt"],
        },
        "execute": execute_runway_t2v,
    })

    api.register_tool({
        "name": "runway_image_to_video",
        "description": (
            "Animate an image using Runway (cloud API). "
            "Image input: HTTPS URL (max 16MB) or local file path (max 5MB). "
            "Duration: 2-10 seconds. REQUIRES a Runway API key. PAID service (~$0.05/second)."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "prompt": {"type": "string", "description": "Animation guidance (max 1000 chars)."},
                "image_path": {"type": "string", "description": "Path to source image (max 5MB for upload)."},
                "image_url": {"type": "string", "description": "HTTPS URL of source image (max 16MB)."},
                "duration": {"type": "integer", "description": "Duration in seconds (2-10). Default: 5.", "default": 5},
                "model": {"type": "string", "description": "Model. Default: gen4_turbo.", "enum": RUNWAY_MODELS, "default": "gen4_turbo"},
                "ratio": {"type": "string", "description": "Aspect ratio.", "enum": ["16:9", "9:16", "1:1", "4:3", "3:4", "21:9"], "default": "16:9"},
                "seed": {"type": "integer", "description": "Seed for reproducibility (optional)."},
            },
            "required": ["image_path"],
        },
        "execute": execute_runway_i2v,
    })
