"""
Runware Video Node — unified cloud video generation via Runware.ai middleware.

Provides access to 40+ video models (Kling, Runway, Minimax, Google Veo,
OpenAI Sora) through a single API key and unified REST interface.

API docs: https://runware.ai/docs/getting-started/introduction
Video:    https://runware.ai/docs/video-inference/api-reference
Polling:  https://runware.ai/docs/utilities/task-responses

Request format:  POST https://api.runware.ai/v1  (JSON array of task objects)
Auth:            Authorization: Bearer <RUNWARE_API_KEY>
Video task:      taskType: "videoInference", deliveryMethod: "async"
Poll task:       taskType: "getResponse", taskUUID: "<original UUID>"
Model IDs (AIR): provider:id@version  (e.g. "klingai:kling-video@3-pro")
"""

import base64
import json
import logging
import time
import uuid
from pathlib import Path

log = logging.getLogger("quinely.node.runware_video")

PROVIDER = "runware"
API_URL = "https://api.runware.ai/v1"

MODEL_ALIASES = {
    "auto":               "klingai:kling-video@3-pro",
    "best":               "klingai:kling-video@3-pro",
    "kling-v3-pro":       "klingai:kling-video@3-pro",
    "kling-v3-std":       "klingai:kling-video@3-standard",
    "kling-o3-pro":       "klingai:kling-video@o3-pro",
    "kling-o3-std":       "klingai:kling-video@o3-standard",
    "runway-gen4.5":      "runway:1@2",
    "runway-gen4-turbo":  "runway:1@1",
    "runway-aleph":       "runway:2@1",
    "minimax-s2v":        "minimax:4@1",
    "minimax-live":       "minimax:4@2",
    "hailuo":             "minimax:4@1",
    "veo3":               "google:3@0",
    "veo3.5":             "google:3@2",
    "sora2":              "openai:3@2",
}

MODEL_CATALOG = [
    {"alias": "kling-v3-pro",      "air_id": "klingai:kling-video@3-pro",       "provider": "Kling AI",        "name": "Kling V3 Pro",         "workflows": ["T2V", "I2V"], "duration": "3-15s",  "notes": "Best quality, multi-shot, native audio, 1080p"},
    {"alias": "kling-v3-std",      "air_id": "klingai:kling-video@3-standard",  "provider": "Kling AI",        "name": "Kling V3 Standard",    "workflows": ["T2V", "I2V"], "duration": "3-15s",  "notes": "Good quality, more affordable"},
    {"alias": "kling-o3-pro",      "air_id": "klingai:kling-video@o3-pro",      "provider": "Kling AI",        "name": "Kling O3 Pro",         "workflows": ["T2V", "I2V"], "duration": "3-15s",  "notes": "Reference-guided, element consistency"},
    {"alias": "kling-o3-std",      "air_id": "klingai:kling-video@o3-standard", "provider": "Kling AI",        "name": "Kling O3 Standard",    "workflows": ["T2V", "I2V"], "duration": "3-15s",  "notes": "Reference-guided, affordable"},
    {"alias": "runway-gen4.5",     "air_id": "runway:1@2",                      "provider": "Runway",          "name": "Runway Gen-4.5",       "workflows": ["T2V", "I2V"], "duration": "5,8,10s", "notes": "Cinematic, high fidelity, 24fps"},
    {"alias": "runway-gen4-turbo", "air_id": "runway:1@1",                      "provider": "Runway",          "name": "Runway Gen-4 Turbo",   "workflows": ["I2V"],        "duration": "2-10s",  "notes": "Fast image-to-video"},
    {"alias": "runway-aleph",      "air_id": "runway:2@1",                      "provider": "Runway",          "name": "Runway Aleph",         "workflows": ["V2V"],        "duration": "varies", "notes": "Video-to-video transformation"},
    {"alias": "minimax-s2v",       "air_id": "minimax:4@1",                     "provider": "Minimax",         "name": "Minimax S2V-01",       "workflows": ["T2V", "I2V"], "duration": "2-10s",  "notes": "Hailuo video generation"},
    {"alias": "minimax-live",      "air_id": "minimax:4@2",                     "provider": "Minimax",         "name": "Minimax S2V-01 Live",  "workflows": ["T2V", "I2V"], "duration": "2-10s",  "notes": "Hailuo live-style generation"},
    {"alias": "veo3",              "air_id": "google:3@0",                      "provider": "Google",          "name": "Google Veo 3",         "workflows": ["T2V", "I2V"], "duration": "6,8s",   "notes": "Google's flagship, audio generation"},
    {"alias": "veo3.5",            "air_id": "google:3@2",                      "provider": "Google",          "name": "Google Veo 3.5 Flash", "workflows": ["T2V", "I2V"], "duration": "5-8s",   "notes": "Faster Veo variant"},
    {"alias": "sora2",             "air_id": "openai:3@2",                      "provider": "OpenAI",          "name": "OpenAI Sora 2 Pro",    "workflows": ["T2V", "I2V"], "duration": "varies", "notes": "OpenAI's video model, high realism"},
]

DEFAULT_MODEL = "klingai:kling-video@3-pro"
POLL_INITIAL_DELAY = 10
POLL_INTERVAL_MIN = 5
POLL_INTERVAL_MAX = 15
POLL_TIMEOUT = 600


def _resolve_model(model_str: str) -> str:
    """Map user-friendly alias to AIR ID, or pass through if already an AIR ID."""
    if not model_str or model_str in ("auto", "best"):
        return DEFAULT_MODEL
    lower = model_str.lower().strip()
    if lower in MODEL_ALIASES:
        return MODEL_ALIASES[lower]
    if ":" in model_str and "@" in model_str:
        return model_str
    return DEFAULT_MODEL


def _runware_post(cloud, payload_items: list, timeout: int = 60) -> dict:
    """POST a task array to the Runware API. Returns parsed response."""
    import urllib.request
    import urllib.error

    headers = cloud.get_auth_headers(PROVIDER)
    if not headers:
        raise RuntimeError("Runware API key not configured")
    headers["Content-Type"] = "application/json"

    body = json.dumps(payload_items).encode("utf-8")
    req = urllib.request.Request(API_URL, data=body, headers=headers, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        error_body = ""
        try:
            error_body = e.read().decode("utf-8", errors="replace")[:500]
        except Exception:
            pass
        raise RuntimeError(f"Runware API error HTTP {e.code}: {error_body}")


def _poll_runware_task(cloud, task_uuid: str, log_prefix: str = "Runware") -> dict:
    """Poll a Runware async task until success, error, or timeout."""
    start = time.time()
    time.sleep(POLL_INITIAL_DELAY)
    interval = POLL_INTERVAL_MIN
    last_status = ""

    while time.time() - start < POLL_TIMEOUT:
        try:
            result = _runware_post(cloud, [
                {"taskType": "getResponse", "taskUUID": task_uuid},
            ], timeout=30)
        except RuntimeError as e:
            log.warning("%s poll error: %s", log_prefix, e)
            time.sleep(interval)
            interval = min(interval * 1.5, POLL_INTERVAL_MAX)
            continue

        errors = result.get("errors", [])
        if errors:
            err = errors[0]
            raise RuntimeError(
                f"{log_prefix} task failed: {err.get('code', 'unknown')} — "
                f"{err.get('message', 'no details')}"
            )

        data_list = result.get("data", [])
        for item in data_list:
            status = item.get("status", "")
            if status != last_status:
                log.info("%s task status: %s", log_prefix, status)
                last_status = status

            if status == "success":
                return item
            if status == "error":
                raise RuntimeError(
                    f"{log_prefix} task error: {item.get('error', item.get('message', 'unknown'))}"
                )

        time.sleep(interval)
        interval = min(interval * 1.3, POLL_INTERVAL_MAX)

    raise RuntimeError(f"{log_prefix} task timed out after {POLL_TIMEOUT}s (last status: {last_status})")


def register(api):
    cloud = api.cloud_providers
    if not cloud:
        log.warning("Cloud providers not available — runware-video node disabled")
        return

    def _check_runware_ready():
        key = api.get_provider_key(PROVIDER)
        if not key:
            return json.dumps({
                "status": "error",
                "error": (
                    "Runware API key not configured. "
                    "Set it in Dashboard > Config > Cloud Providers, "
                    "or set the RUNWARE_API_KEY env var. "
                    "Get a key at https://my.runware.ai/signup"
                ),
            })
        if not cloud.check_budget(PROVIDER):
            remaining = cloud.get_budget_remaining(PROVIDER)
            return json.dumps({
                "status": "error",
                "error": f"Monthly Runware budget exhausted (remaining: ${remaining:.2f}).",
            })
        return None

    def execute_runware_t2v(prompt="", model="auto", duration=5,
                            width=1280, height=720, negative_prompt="",
                            seed=None, generate_audio=False, **_kw):
        if not prompt:
            return json.dumps({"status": "error", "error": "prompt is required"})

        err = _check_runware_ready()
        if err:
            return err

        air_id = _resolve_model(model)
        task_uuid = str(uuid.uuid4())

        api.log(f"Submitting Runware T2V task (model: {air_id}, {duration}s, {width}x{height})...")
        t0 = time.time()

        payload = {
            "taskType": "videoInference",
            "taskUUID": task_uuid,
            "model": air_id,
            "positivePrompt": prompt[:1000],
            "duration": duration,
            "width": width,
            "height": height,
            "deliveryMethod": "async",
            "includeCost": True,
            "numberResults": 1,
        }
        if negative_prompt:
            payload["negativePrompt"] = negative_prompt[:1000]
        if seed is not None:
            payload["seed"] = seed

        provider_key = air_id.split(":")[0] if ":" in air_id else ""
        provider_settings = {}
        if generate_audio and provider_key in ("klingai", "google"):
            provider_settings[provider_key] = {"generateAudio": True}
        if provider_settings:
            payload["providerSettings"] = provider_settings

        try:
            result = _runware_post(cloud, [payload])
        except RuntimeError as e:
            return json.dumps({"status": "error", "error": str(e)[:500]})

        errors = result.get("errors", [])
        if errors:
            return json.dumps({
                "status": "error",
                "error": f"Runware submission error: {errors[0].get('message', str(errors[0]))}",
            })

        api.log(f"Runware T2V task submitted (UUID: {task_uuid}). Polling...")

        try:
            completed = _poll_runware_task(cloud, task_uuid, "Runware T2V")
        except RuntimeError as e:
            return json.dumps({"status": "error", "error": str(e)[:500]})

        return _download_and_save(api, cloud, completed, prompt, t0, air_id, duration)

    def execute_runware_i2v(prompt="", image_path="", model="auto",
                            duration=5, width=1280, height=720,
                            negative_prompt="", seed=None,
                            generate_audio=False, **_kw):
        if not image_path:
            return json.dumps({"status": "error", "error": "image_path is required"})

        err = _check_runware_ready()
        if err:
            return err

        p = Path(image_path)
        if not p.exists():
            return json.dumps({"status": "error", "error": f"File not found: {image_path}"})
        if p.stat().st_size > 20 * 1024 * 1024:
            return json.dumps({"status": "error", "error": "Image exceeds 20MB limit"})

        image_data = p.read_bytes()
        b64 = base64.b64encode(image_data).decode("utf-8")
        suffix = p.suffix.lower().lstrip(".")
        mime = {"png": "image/png", "jpg": "image/jpeg", "jpeg": "image/jpeg",
                "webp": "image/webp"}.get(suffix, "image/jpeg")
        data_uri = f"data:{mime};base64,{b64}"

        air_id = _resolve_model(model)
        task_uuid = str(uuid.uuid4())

        api.log(f"Submitting Runware I2V task (model: {air_id}, {duration}s)...")
        t0 = time.time()

        payload = {
            "taskType": "videoInference",
            "taskUUID": task_uuid,
            "model": air_id,
            "positivePrompt": (prompt or "animate this image with natural cinematic motion")[:1000],
            "duration": duration,
            "width": width,
            "height": height,
            "frameImages": [
                {"inputImage": data_uri, "frame": "first"},
            ],
            "deliveryMethod": "async",
            "includeCost": True,
            "numberResults": 1,
        }
        if negative_prompt:
            payload["negativePrompt"] = negative_prompt[:1000]
        if seed is not None:
            payload["seed"] = seed

        provider_key = air_id.split(":")[0] if ":" in air_id else ""
        provider_settings = {}
        if generate_audio and provider_key in ("klingai", "google"):
            provider_settings[provider_key] = {"generateAudio": True}
        if provider_settings:
            payload["providerSettings"] = provider_settings

        try:
            result = _runware_post(cloud, [payload])
        except RuntimeError as e:
            return json.dumps({"status": "error", "error": str(e)[:500]})

        errors = result.get("errors", [])
        if errors:
            return json.dumps({
                "status": "error",
                "error": f"Runware submission error: {errors[0].get('message', str(errors[0]))}",
            })

        api.log(f"Runware I2V task submitted (UUID: {task_uuid}). Polling...")

        try:
            completed = _poll_runware_task(cloud, task_uuid, "Runware I2V")
        except RuntimeError as e:
            return json.dumps({"status": "error", "error": str(e)[:500]})

        return _download_and_save(api, cloud, completed, prompt, t0, air_id, duration,
                                  source_image=image_path)

    def _download_and_save(api, cloud, completed_data, prompt, t0, air_id,
                           duration, source_image=""):
        video_url = completed_data.get("videoURL", "")
        if not video_url:
            for key in ("videoUrl", "video_url", "url"):
                candidate = completed_data.get(key, "")
                if isinstance(candidate, str) and candidate.startswith("http"):
                    video_url = candidate
                    break

        if not video_url:
            return json.dumps({
                "status": "error",
                "error": f"No video URL in response: {json.dumps(completed_data)[:500]}",
            })

        api.log("Downloading video from Runware...")
        try:
            video_bytes = cloud.download_file(video_url)
        except Exception as e:
            return json.dumps({"status": "error", "error": f"Download failed: {e}"})

        elapsed = time.time() - t0
        cost = completed_data.get("cost", 0.0)
        ts = time.strftime("%Y%m%d_%H%M%S")

        alias = ""
        for a, aid in MODEL_ALIASES.items():
            if aid == air_id:
                alias = a
                break
        name_part = alias.replace(".", "").replace("-", "_") if alias else "gen"
        fname = f"runware_{name_part}_{ts}.mp4"

        params_dict = {
            "model": air_id, "duration": duration,
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
                "provider": PROVIDER, "model": air_id,
                "duration_secs": duration, "cost_usd": cost,
                "elapsed_secs": round(elapsed, 2),
                "prompt": (prompt or "")[:200],
                "video_uuid": completed_data.get("videoUUID", ""),
            },
            provider=PROVIDER,
            cost_usd=cost,
        )

        cloud.track_cost(
            PROVIDER,
            "text_to_video" if not source_image else "image_to_video",
            cost,
        )
        api.log(f"Runware video saved: {fname} (${cost:.2f}, {elapsed:.1f}s, model: {air_id})")

        return json.dumps({
            "status": "ok",
            "path": path,
            "provider": PROVIDER,
            "model": air_id,
            "cost_usd": cost,
            "duration_secs": duration,
            "elapsed_secs": round(elapsed, 2),
        })

    def execute_runware_list_models(**_kw):
        return json.dumps({
            "status": "ok",
            "models": MODEL_CATALOG,
            "usage": (
                "Use the 'alias' or 'air_id' as the 'model' parameter in "
                "runware_text_to_video or runware_image_to_video. "
                "Example: model='kling-v3-pro' or model='klingai:kling-video@3-pro'"
            ),
        })

    all_aliases = list(MODEL_ALIASES.keys())
    all_air_ids = list({v for v in MODEL_ALIASES.values()})

    api.register_tool({
        "name": "runware_text_to_video",
        "description": (
            "Generate video from text using Runware.ai unified API. "
            "Access 40+ models from Kling, Runway, Minimax, Google Veo, and OpenAI Sora "
            "with a single Runware API key. "
            "Popular models: kling-v3-pro (best quality, 3-15s, multi-shot), "
            "runway-gen4.5 (cinematic, 5-10s), veo3 (Google, audio), sora2 (OpenAI). "
            "Use model='auto' for best available. "
            "PAID service — costs vary by model (billed through Runware account). "
            "For free local generation, use text_to_video instead."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "prompt": {
                    "type": "string",
                    "description": "Text description of the video to generate (max 1000 chars).",
                },
                "model": {
                    "type": "string",
                    "description": (
                        "Model alias or AIR ID. Aliases: " + ", ".join(all_aliases) + ". "
                        "Default: auto (Kling V3 Pro)."
                    ),
                    "default": "auto",
                },
                "duration": {
                    "type": "integer",
                    "description": "Video duration in seconds. Range depends on model. Default: 5.",
                    "default": 5,
                },
                "width": {
                    "type": "integer",
                    "description": "Output width in pixels. Default: 1280.",
                    "default": 1280,
                },
                "height": {
                    "type": "integer",
                    "description": "Output height in pixels. Default: 720.",
                    "default": 720,
                },
                "negative_prompt": {
                    "type": "string",
                    "description": "Things to avoid in the video (optional).",
                },
                "seed": {
                    "type": "integer",
                    "description": "Random seed for reproducibility (optional).",
                },
                "generate_audio": {
                    "type": "boolean",
                    "description": "Generate native audio (Kling/Google models only). Default: false.",
                    "default": False,
                },
            },
            "required": ["prompt"],
        },
        "execute": execute_runware_t2v,
    })

    api.register_tool({
        "name": "runware_image_to_video",
        "description": (
            "Animate an image into video using Runware.ai unified API. "
            "Access Kling, Runway Gen-4 Turbo/Gen-4.5, Minimax, Veo, and Sora models. "
            "The source image is used as the first frame. "
            "PAID service — costs vary by model."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "prompt": {
                    "type": "string",
                    "description": "How to animate the image (optional, max 1000 chars).",
                },
                "image_path": {
                    "type": "string",
                    "description": "Path to the source image (JPG/PNG/WebP, max 20MB).",
                },
                "model": {
                    "type": "string",
                    "description": (
                        "Model alias or AIR ID. Default: auto (Kling V3 Pro). "
                        "Good I2V models: runway-gen4-turbo, runway-gen4.5, kling-v3-pro."
                    ),
                    "default": "auto",
                },
                "duration": {
                    "type": "integer",
                    "description": "Video duration in seconds. Default: 5.",
                    "default": 5,
                },
                "width": {
                    "type": "integer",
                    "description": "Output width in pixels. Default: 1280.",
                    "default": 1280,
                },
                "height": {
                    "type": "integer",
                    "description": "Output height in pixels. Default: 720.",
                    "default": 720,
                },
                "negative_prompt": {
                    "type": "string",
                    "description": "Things to avoid (optional).",
                },
                "seed": {
                    "type": "integer",
                    "description": "Random seed for reproducibility (optional).",
                },
                "generate_audio": {
                    "type": "boolean",
                    "description": "Generate native audio (Kling/Google models only). Default: false.",
                    "default": False,
                },
            },
            "required": ["image_path"],
        },
        "execute": execute_runware_i2v,
    })

    api.register_tool({
        "name": "runware_list_models",
        "description": (
            "List all video models available through Runware.ai. "
            "Shows model aliases, AIR IDs, supported workflows, durations, and notes."
        ),
        "parameters": {
            "type": "object",
            "properties": {},
        },
        "execute": execute_runware_list_models,
    })
