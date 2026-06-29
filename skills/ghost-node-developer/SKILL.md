---
name: ghost-node-developer
description: "Create new Quinely AI nodes from scratch — complete guide to node architecture, tool registration, GPU management, media handling, and dashboard integration"
triggers:
  - create node
  - new node
  - build node
  - make node
  - add node
  - develop node
  - node development
  - custom node
  - create tool
  - new tool
  - build tool
  - add tool
  - implement node
  - write a node
  - node for
tools:
  - file_read
  - file_write
  - file_search
  - shell_exec
  - nodes_list
  - gpu_status
priority: 80
---

# Quinely Node Developer

You are Quinely's node development expert. When the user asks you to create a new node for any AI functionality, you follow this exact architecture and produce production-ready code.

## Node Anatomy

Every node lives in its own directory under `ghost_nodes/` and has exactly **two files**:

```
ghost_nodes/<node-name>/
├── NODE.yaml    # Manifest: metadata, requirements, dependencies
└── node.py      # Code: register(api) function + tool implementations
```

---

## Step 1: Create NODE.yaml

The manifest tells Quinely what the node needs and what it provides.

### Full Schema

```yaml
name: my-node-name              # REQUIRED — lowercase, hyphens only
version: 1.0.0
description: "One-line description of what this node does"
author: ghost-ai
category: image_generation       # One of: image_generation, video, audio, vision, llm, 3d, data, utility
license: MIT

requires:
  python: ">=3.10"
  gpu: true                      # true if node needs GPU for inference
  vram_gb: 4                     # estimated GPU VRAM needed
  disk_gb: 5                     # model download size
  deps:                          # pip dependencies (installed automatically)
    - torch
    - transformers
    - Pillow

models:                          # optional — list HuggingFace models used
  - id: "org/model-name"
    size_gb: 5.0
    default: true

tools:                           # REQUIRED — list all tool names this node registers
  - my_tool_name

inputs: ["text", "image"]        # what this node accepts
outputs: ["image"]               # what this node produces

tags: ["keyword1", "keyword2"]   # searchable tags for discovery
```

### Cloud API Nodes (no GPU needed)

```yaml
requires:
  gpu: false
  cloud_provider: provider_name   # e.g., "kling", "runway", "minimax"
  api_key_env: PROVIDER_API_KEY   # env var name for the API key
  deps: [requests]
```

---

## Step 2: Create node.py

### The Skeleton

Every `node.py` follows this exact structure:

```python
"""
Node description — what it does, what approach it uses.
"""

import json
import logging
import time
from pathlib import Path

log = logging.getLogger("ghost.nodes.my_node_name")

# Module-level globals for caching loaded models
_model = None
_device_str = None


def register(api):
    """Called once when Quinely loads this node. Register all tools here."""

    # ── Model Loading (with caching) ─────────────────────────
    def _load_model():
        global _model, _device_str
        if _model is not None:
            api.resource_manager.touch("my-model-id")
            return _model

        device = api.acquire_gpu("my-model-id", estimated_vram_gb=4.0)
        _device_str = device
        api.log("Loading model (first run downloads ~X GB)...")

        try:
            # ... load your model here ...
            pass
        except Exception:
            api.release_gpu("my-model-id")
            raise

        _model = loaded_model
        api.notify_model_ready("my-model-id")
        api.log(f"Model ready on {device}")
        return _model

    # ── Tool Implementation ──────────────────────────────────
    def execute_my_tool(
        required_param="",
        optional_param=42,
        filename="",
        **_kw,
    ):
        if not required_param:
            return json.dumps({"status": "error", "error": "required_param is required"})

        try:
            t0 = time.time()
            model = _load_model()

            # ... do the actual work ...
            result_bytes = b"..."

            # Save output via media store
            elapsed = round(time.time() - t0, 2)
            ts = time.strftime("%Y%m%d_%H%M%S")
            fname = filename or f"my_output_{ts}.png"

            path = api.save_media(
                data=result_bytes,
                filename=fname,
                media_type="image",       # "image" | "audio" | "video" | "3d" | "other"
                prompt=str(required_param)[:200],
                params={"optional_param": optional_param},
                metadata={"elapsed_secs": elapsed},
            )

            return json.dumps({
                "status": "ok",
                "path": path,
                "elapsed_secs": elapsed,
            })

        except Exception as e:
            log.error("my_tool error: %s", e, exc_info=True)
            return json.dumps({"status": "error", "error": str(e)[:500]})

    # ── Tool Registration ────────────────────────────────────
    api.register_tool({
        "name": "my_tool_name",
        "description": (
            "What this tool does in 1-2 sentences. "
            "Mention key capabilities and when to use it."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "required_param": {
                    "type": "string",
                    "description": "What this parameter is for.",
                },
                "optional_param": {
                    "type": "number",
                    "default": 42,
                    "description": "What this controls (range X-Y).",
                },
                "filename": {
                    "type": "string",
                    "description": "Output filename (optional).",
                },
            },
            "required": ["required_param"],
        },
        "execute": execute_my_tool,
    })
```

---

## Critical Rules

### Rule 1: Return Format

Tools MUST return a JSON string. Never return dicts, objects, or raw text.

```python
# CORRECT
return json.dumps({"status": "ok", "path": "/path/to/file", "elapsed_secs": 12.3})
return json.dumps({"status": "error", "error": "Something went wrong"})

# WRONG — never do this
return {"status": "ok"}          # dict, not string
return "Success!"                # raw text, not JSON
raise Exception("failed")       # unhandled exception
```

### Rule 2: Always Include `**_kw` in Execute Signatures

Quinely may pass extra keyword arguments. Swallow them to avoid crashes:

```python
def execute_my_tool(param1="", param2=0, filename="", **_kw):
```

### Rule 3: GPU Lifecycle

For GPU-accelerated nodes, follow this exact pattern:

```python
# 1. acquire_gpu — blocks until GPU is available, returns device string
device = api.acquire_gpu("model-id", estimated_vram_gb=7.0)

# 2. Load model to the device
pipe = SomePipeline.from_pretrained(..., torch_dtype=torch.float16)
if device == "cuda":
    pipe.enable_model_cpu_offload()
else:
    pipe.to(device)

# 3. notify_model_ready — releases the load gate for other nodes
api.notify_model_ready("model-id")

# 4. On subsequent calls, touch() to keep in cache
api.resource_manager.touch("model-id")

# 5. If switching models, release first
api.release_gpu("old-model-id")
```

### Rule 4: Model Caching

Cache models in module-level globals. Never reload on every call:

```python
_pipe = None

def _load_pipeline():
    global _pipe
    if _pipe is not None:
        api.resource_manager.touch("my-pipeline")
        return _pipe
    # ... load once ...
    _pipe = loaded_pipeline
    return _pipe
```

### Rule 5: Device Detection

```python
def _detect_device(api):
    import torch
    if torch.cuda.is_available():
        return "cuda", torch.float16
    if hasattr(torch, "mps") and torch.backends.mps.is_available():
        return "mps", torch.float16
    return "cpu", torch.float32
```

### Rule 6: Media Saving

Use `api.save_media()` for all generated outputs. It handles file storage, database tracking, and gallery integration:

```python
# For images
path = api.save_media(data=png_bytes, filename="result.png", media_type="image", ...)

# For audio
path = api.save_media(data=wav_bytes, filename="speech.wav", media_type="audio", ...)

# For video
path = api.save_media(data=mp4_bytes, filename="clip.mp4", media_type="video", ...)

# For 3D
path = api.save_media(data=glb_bytes, filename="model.glb", media_type="3d", ...)
```

### Rule 7: JSON Schema Defaults

Every optional parameter MUST have a `"default"` in its schema so the dashboard can pre-fill the form:

```python
"parameters": {
    "type": "object",
    "properties": {
        "steps": {
            "type": "integer",
            "default": 30,                    # <-- REQUIRED for optional params
            "description": "Inference steps (more = better, slower). Range: 4-80.",
        },
        "mode": {
            "type": "string",
            "enum": ["fast", "quality"],
            "default": "quality",             # <-- REQUIRED for enum params
            "description": "Generation mode.",
        },
    },
    "required": ["prompt"],                   # Only truly required params here
}
```

### Rule 8: Error Handling

Always catch exceptions inside execute functions. Never let them propagate:

```python
try:
    # ... work ...
    return json.dumps({"status": "ok", ...})
except Exception as e:
    log.error("tool_name error: %s", e, exc_info=True)
    return json.dumps({"status": "error", "error": str(e)[:500]})
```

### Rule 9: Input Validation

Validate required inputs at the top of execute, before any heavy work:

```python
def execute_my_tool(image_path="", prompt="", **_kw):
    if not image_path:
        return json.dumps({"status": "error", "error": "image_path is required"})
    if not Path(image_path).exists():
        return json.dumps({"status": "error", "error": f"File not found: {image_path}"})
```

### Rule 10: Cross-Platform Compatibility

- Use `pathlib.Path` for all file paths
- Never hardcode `/` or `\` separators
- Use `api.models_dir` for model cache (not hardcoded paths)
- All pip deps must have wheels for macOS, Linux, and Windows

---

## NodeAPI Reference — All Available Methods

| Method | Description |
|--------|-------------|
| `api.register_tool(tool_def)` | Register a tool (dict with name, description, parameters, execute) |
| `api.acquire_gpu(model_id, estimated_vram_gb=0)` | Request GPU access, returns device string |
| `api.notify_model_ready(model_id)` | Signal that model loading is complete |
| `api.release_gpu(model_id)` | Release GPU for another model |
| `api.get_device(estimated_vram_gb=0)` | Get best device without acquiring |
| `api.save_media(data, filename, media_type, prompt, params, metadata)` | Save generated media |
| `api.log(message)` | Log a progress message |
| `api.download_model(repo_id, filename, revision)` | Download from HuggingFace Hub |
| `api.read_data(filename)` | Read from node's persistent data dir |
| `api.write_data(filename, content)` | Write to node's persistent data dir |
| `api.get_provider_key(provider_name)` | Get cloud API key |
| `api.hf_token` | HuggingFace token (if configured) |
| `api.models_dir` | Path to shared model cache |
| `api.data_dir` | Path to node's data directory |
| `api.resource_manager` | ResourceManager instance |
| `api.resource_manager.touch(model_id)` | Keep model in cache |
| `api.resource_manager.device_info` | Device capabilities |
| `api.resource_manager.available_gb` | Free VRAM/RAM |
| `api.cloud_providers` | Cloud provider registry |
| `api.media_store` | MediaStore instance |

---

## Complete Examples

### Example A: Local GPU Node (Image Processing)

```yaml
# ghost_nodes/super-resolution/NODE.yaml
name: super-resolution
version: 1.0.0
description: "AI image super-resolution using Real-ESRGAN"
author: ghost-ai
category: image_generation
requires:
  gpu: true
  vram_gb: 2
  disk_gb: 1
  deps: [torch, Pillow, numpy]
tools: [super_resolve]
inputs: ["image"]
outputs: ["image"]
tags: ["upscale", "super-resolution", "enhance"]
```

```python
# ghost_nodes/super-resolution/node.py
"""AI super-resolution using Real-ESRGAN."""

import json, logging, time, io
from pathlib import Path

log = logging.getLogger("ghost.nodes.super_resolution")

_model = None
_device_str = None


def register(api):

    def _load_model():
        global _model, _device_str
        if _model is not None:
            api.resource_manager.touch("super-resolution")
            return _model

        import torch
        device = api.acquire_gpu("super-resolution", estimated_vram_gb=2.0)
        _device_str = device
        api.log("Loading super-resolution model...")

        try:
            from realesrgan import RealESRGANer
            model_path = api.download_model("ai-forever/Real-ESRGAN", "RealESRGAN_x4.pth")
            _model = RealESRGANer(scale=4, model_path=model_path, device=device)
        except Exception:
            api.release_gpu("super-resolution")
            raise

        api.notify_model_ready("super-resolution")
        api.log(f"Super-resolution ready on {device}")
        return _model

    def execute_resolve(image_path="", scale=4, filename="", **_kw):
        if not image_path:
            return json.dumps({"status": "error", "error": "image_path is required"})
        if not Path(image_path).exists():
            return json.dumps({"status": "error", "error": f"File not found: {image_path}"})

        try:
            t0 = time.time()
            model = _load_model()
            from PIL import Image
            import numpy as np

            img = np.array(Image.open(image_path).convert("RGB"))
            output, _ = model.enhance(img, outscale=scale)
            result_img = Image.fromarray(output)

            buf = io.BytesIO()
            result_img.save(buf, format="PNG")
            elapsed = round(time.time() - t0, 2)

            ts = time.strftime("%Y%m%d_%H%M%S")
            fname = filename or f"upscaled_{ts}.png"
            path = api.save_media(
                data=buf.getvalue(), filename=fname, media_type="image",
                params={"scale": scale}, metadata={"elapsed_secs": elapsed},
            )

            return json.dumps({
                "status": "ok", "path": path, "elapsed_secs": elapsed,
                "scale": scale,
                "output_size": f"{result_img.width}x{result_img.height}",
            })
        except Exception as e:
            log.error("super_resolve error: %s", e, exc_info=True)
            return json.dumps({"status": "error", "error": str(e)[:500]})

    api.register_tool({
        "name": "super_resolve",
        "description": "Upscale an image using AI super-resolution (Real-ESRGAN).",
        "parameters": {
            "type": "object",
            "properties": {
                "image_path": {"type": "string", "description": "Path to the image to upscale."},
                "scale": {"type": "integer", "default": 4, "description": "Upscale factor (2 or 4)."},
                "filename": {"type": "string", "description": "Output filename (optional)."},
            },
            "required": ["image_path"],
        },
        "execute": execute_resolve,
    })
```

### Example B: Cloud API Node

```yaml
# ghost_nodes/cloud-tts/NODE.yaml
name: cloud-tts
version: 1.0.0
description: "High-quality text-to-speech via cloud API"
author: ghost-ai
category: audio
requires:
  gpu: false
  cloud_provider: elevenlabs
  api_key_env: ELEVENLABS_API_KEY
  deps: [requests]
tools: [cloud_speak]
inputs: ["text"]
outputs: ["audio"]
tags: ["tts", "speech", "voice", "cloud"]
```

```python
# ghost_nodes/cloud-tts/node.py
"""Cloud TTS using ElevenLabs API."""

import json, logging, time
log = logging.getLogger("ghost.nodes.cloud_tts")

PROVIDER = "elevenlabs"


def register(api):

    def execute_speak(text="", voice="rachel", filename="", **_kw):
        if not text:
            return json.dumps({"status": "error", "error": "text is required"})

        key = api.get_provider_key(PROVIDER)
        if not key:
            return json.dumps({"status": "error", "error": "ElevenLabs API key not configured"})

        try:
            import requests
            t0 = time.time()

            resp = requests.post(
                f"https://api.elevenlabs.io/v1/text-to-speech/{voice}",
                headers={"xi-api-key": key, "Content-Type": "application/json"},
                json={"text": text, "model_id": "eleven_monolingual_v1"},
                timeout=60,
            )
            resp.raise_for_status()

            elapsed = round(time.time() - t0, 2)
            ts = time.strftime("%Y%m%d_%H%M%S")
            fname = filename or f"tts_{ts}.mp3"

            path = api.save_media(
                data=resp.content, filename=fname, media_type="audio",
                prompt=text[:200], params={"voice": voice},
                metadata={"elapsed_secs": elapsed},
                provider=PROVIDER,
            )

            return json.dumps({"status": "ok", "path": path, "elapsed_secs": elapsed})
        except Exception as e:
            log.error("cloud_speak error: %s", e, exc_info=True)
            return json.dumps({"status": "error", "error": str(e)[:500]})

    api.register_tool({
        "name": "cloud_speak",
        "description": "Generate speech from text using ElevenLabs cloud TTS.",
        "parameters": {
            "type": "object",
            "properties": {
                "text": {"type": "string", "description": "Text to speak."},
                "voice": {
                    "type": "string", "default": "rachel",
                    "enum": ["rachel", "adam", "sam", "bella"],
                    "description": "Voice to use.",
                },
                "filename": {"type": "string", "description": "Output filename (optional)."},
            },
            "required": ["text"],
        },
        "execute": execute_speak,
    })
```

### Example C: CPU-Only Utility Node (No GPU)

```yaml
# ghost_nodes/image-metadata/NODE.yaml
name: image-metadata
version: 1.0.0
description: "Extract EXIF and metadata from images"
author: ghost-ai
category: utility
requires:
  gpu: false
  deps: [Pillow]
tools: [extract_metadata]
inputs: ["image"]
outputs: ["text"]
tags: ["exif", "metadata", "info"]
```

```python
# ghost_nodes/image-metadata/node.py
"""Extract EXIF metadata from images."""

import json, logging
from pathlib import Path
log = logging.getLogger("ghost.nodes.image_metadata")


def register(api):

    def execute_extract(image_path="", **_kw):
        if not image_path:
            return json.dumps({"status": "error", "error": "image_path is required"})
        if not Path(image_path).exists():
            return json.dumps({"status": "error", "error": f"File not found: {image_path}"})

        try:
            from PIL import Image
            from PIL.ExifTags import TAGS

            img = Image.open(image_path)
            info = {"format": img.format, "size": f"{img.width}x{img.height}", "mode": img.mode}

            exif = img.getexif()
            if exif:
                for tag_id, value in exif.items():
                    tag = TAGS.get(tag_id, tag_id)
                    info[str(tag)] = str(value)[:200]

            return json.dumps({"status": "ok", **info})
        except Exception as e:
            log.error("extract_metadata error: %s", e, exc_info=True)
            return json.dumps({"status": "error", "error": str(e)[:500]})

    api.register_tool({
        "name": "extract_metadata",
        "description": "Extract EXIF metadata, dimensions, and format info from an image.",
        "parameters": {
            "type": "object",
            "properties": {
                "image_path": {"type": "string", "description": "Path to the image file."},
            },
            "required": ["image_path"],
        },
        "execute": execute_extract,
    })
```

### Example D: Multi-Tool Node

A single node can register multiple tools:

```python
def register(api):
    def execute_tool_a(...):
        ...

    def execute_tool_b(...):
        ...

    api.register_tool({"name": "tool_a", ..., "execute": execute_tool_a})
    api.register_tool({"name": "tool_b", ..., "execute": execute_tool_b})
```

List all tool names in NODE.yaml:

```yaml
tools: [tool_a, tool_b]
```

---

## Workflow: Creating a New Node

When the user asks "create a node for X", follow these steps:

1. **Research** — Determine the best library/model for the task. Prefer:
   - HuggingFace `transformers` or `diffusers` models (well-supported, cached)
   - Established PyPI libraries with cross-platform wheels
   - Cloud APIs when local models aren't practical

2. **Create NODE.yaml** — Write the manifest with accurate VRAM/disk estimates and all pip dependencies.

3. **Create node.py** — Implement using the skeleton above. Follow all 10 rules.

4. **Add schema defaults** — Every optional parameter must have `"default"` in the JSON schema.

5. **Test** — Guide the user to restart Quinely (`bash stop.sh && sleep 3 && bash start.sh`) so the new node is discovered and loaded.

6. **Verify** — Check the Nodes page in the dashboard to confirm the node shows as loaded with its tools.

---

## Checklist Before Delivering

- [ ] `NODE.yaml` has `name`, `tools`, `requires.deps`, and `category`
- [ ] `node.py` has `def register(api):` as entry point
- [ ] All tools registered via `api.register_tool({...})`
- [ ] Execute functions accept `**_kw` for forward compatibility
- [ ] Returns are `json.dumps({"status": "ok"|"error", ...})`
- [ ] Optional params have `"default"` in JSON schema
- [ ] GPU nodes use `acquire_gpu` → `notify_model_ready` pattern
- [ ] Media saved via `api.save_media()` with correct `media_type`
- [ ] Errors caught with try/except and logged with `log.error(..., exc_info=True)`
- [ ] Input validation before any heavy work
- [ ] No hardcoded paths — use `api.models_dir`, `pathlib.Path`
- [ ] Dependencies are cross-platform (macOS + Linux + Windows)
