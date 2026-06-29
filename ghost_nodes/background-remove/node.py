"""
Background Remove Node — remove image backgrounds using REMBG (U2-Net).

Fast, local, no API key needed. Supports multiple models for different use cases.
"""

import io
import json
import logging
import time
from pathlib import Path

log = logging.getLogger("quinely.node.background_remove")


def register(api):

    def execute_remove(image_path="", model_name="u2net",
                       alpha_matting=False, filename="", **_kw):
        if not image_path:
            return json.dumps({"status": "error", "error": "image_path is required"})
        if not Path(image_path).exists():
            return json.dumps({"status": "error", "error": f"File not found: {image_path}"})

        try:
            from rembg import remove, new_session
            from PIL import Image
        except ImportError:
            return json.dumps({
                "status": "error",
                "error": "rembg not installed. Run: pip install rembg Pillow",
            })

        try:
            api.log("Removing background...")
            api.log(f"Removing background from {Path(image_path).name} (model={model_name})...")
            t0 = time.time()

            with Image.open(image_path) as img:
                input_img = img.convert("RGBA")

            api.log(f"Loading background-removal model ({model_name}) — first run may take a few minutes...")
            session = new_session(model_name)
            output = remove(
                input_img,
                session=session,
                alpha_matting=alpha_matting,
                alpha_matting_foreground_threshold=240 if alpha_matting else None,
                alpha_matting_background_threshold=10 if alpha_matting else None,
            )
            elapsed = time.time() - t0

            buf = io.BytesIO()
            output.save(buf, format="PNG")

            ts = time.strftime("%Y%m%d_%H%M%S")
            src_name = Path(image_path).stem
            fname = filename or f"{src_name}_nobg_{ts}.png"
            if not fname.endswith(".png"):
                fname += ".png"

            path = api.save_media(
                data=buf.getvalue(), filename=fname, media_type="image",
                metadata={
                    "source": str(image_path), "model": model_name,
                    "alpha_matting": alpha_matting, "elapsed_secs": round(elapsed, 2),
                },
            )
            return json.dumps({
                "status": "ok",
                "path": path,
                "elapsed_secs": round(elapsed, 2),
                "original": str(image_path),
            })

        except Exception as e:
            return json.dumps({"status": "error", "error": str(e)[:500]})

    api.register_tool({
        "name": "remove_background",
        "description": (
            "Remove the background from an image (locally, no API key needed). "
            "Uses REMBG with U2-Net for high-quality segmentation. "
            "Returns the path to a transparent PNG."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "image_path": {
                    "type": "string",
                    "description": "Path to the source image.",
                },
                "model_name": {
                    "type": "string",
                    "enum": ["u2net", "u2net_human_seg", "isnet-general-use"],
                    "description": "Segmentation model. u2net_human_seg is optimized for people.",
                    "default": "u2net",
                },
                "alpha_matting": {
                    "type": "boolean",
                    "description": "Enable alpha matting for smoother edges (slower).",
                    "default": False,
                },
                "filename": {"type": "string", "description": "Output filename (optional)."},
            },
            "required": ["image_path"],
        },
        "execute": execute_remove,
    })
