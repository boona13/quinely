"""
Surya OCR Node — multilingual document OCR for 90+ languages.

Uses the Surya 0.17+ Predictor API with FoundationPredictor.
Falls back to legacy run_ocr API for older versions.
"""

import json
import logging
import time
from pathlib import Path

log = logging.getLogger("quinely.node.surya_ocr")

_det_predictor = None
_rec_predictor = None
_foundation = None
_api_version = None  # "new" (0.17+) or "legacy"


def _ensure_models(api):
    """Load and cache Surya OCR models."""
    global _det_predictor, _rec_predictor, _foundation, _api_version

    if _det_predictor is not None:
        api.resource_manager.touch("surya-ocr")
        return

    device = api.acquire_gpu("surya-ocr", estimated_vram_gb=2.0)
    api.log("Downloading & loading Surya OCR — first run may take a few minutes...")

    try:
        from surya.foundation import FoundationPredictor
        from surya.detection import DetectionPredictor
        from surya.recognition import RecognitionPredictor

        _foundation = FoundationPredictor(device=device)
        _rec_predictor = RecognitionPredictor(_foundation)
        _det_predictor = DetectionPredictor(device=device)
        _api_version = "new"
        api.log(f"Surya OCR (v0.17+) loaded on {device}")
    except ImportError:
        try:
            from surya.model.detection.model import load_model as load_det
            from surya.model.detection.model import load_processor as load_det_proc
            from surya.model.recognition.model import load_model as load_rec
            from surya.model.recognition.processor import load_processor as load_rec_proc

            _det_predictor = (load_det(), load_det_proc())
            _rec_predictor = (load_rec(), load_rec_proc())
            _api_version = "legacy"
            api.log(f"Surya OCR (legacy) loaded on {device}")
        except ImportError:
            raise RuntimeError("surya-ocr not installed. Run: pip install surya-ocr Pillow")


def _run_ocr_new(images, langs_list):
    """Run OCR using new Predictor API (surya 0.17+)."""
    return _rec_predictor(
        images, task_names=['ocr_without_boxes'] * len(images),
        det_predictor=_det_predictor, highres_images=images,
    )


def _run_ocr_legacy(images, langs_list):
    """Run OCR using legacy run_ocr API (surya < 0.17)."""
    from surya.ocr import run_ocr
    det_model, det_proc = _det_predictor
    rec_model, rec_proc = _rec_predictor
    return run_ocr(images, langs_list, det_model, det_proc, rec_model, rec_proc)


def register(api):

    def execute_ocr(image_path="", languages=None, **_kw):
        if not image_path:
            return json.dumps({"status": "error", "error": "image_path is required"})
        if not Path(image_path).exists():
            return json.dumps({"status": "error", "error": f"File not found: {image_path}"})

        try:
            from PIL import Image
        except ImportError:
            return json.dumps({"status": "error", "error": "Pillow not installed"})

        try:
            _ensure_models(api)

            image = Image.open(image_path).convert("RGB")
            langs = languages or ["en"]

            api.log(f"Extracting text from {Path(image_path).name}...")
            t0 = time.time()

            if _api_version == "new":
                results = _run_ocr_new([image], [langs])
            else:
                results = _run_ocr_legacy([image], [langs])

            elapsed = time.time() - t0

            text_lines = []
            regions = []
            if results:
                for line in results[0].text_lines:
                    text_lines.append(line.text)
                    regions.append({
                        "text": line.text,
                        "confidence": round(line.confidence, 3),
                        "bbox": getattr(line, 'bbox', None),
                    })

            full_text = "\n".join(text_lines)

            return json.dumps({
                "status": "ok",
                "text": full_text,
                "image_path": str(image_path),
                "lines": len(text_lines),
                "regions": regions[:100],
                "languages": langs,
                "elapsed_secs": round(elapsed, 2),
            })

        except RuntimeError as e:
            return json.dumps({"status": "error", "error": str(e)[:500]})
        except Exception as e:
            log.error("OCR error: %s", e, exc_info=True)
            return json.dumps({"status": "error", "error": str(e)[:500]})

    api.register_tool({
        "name": "ocr_extract",
        "description": (
            "Extract text from images using Surya OCR (local). "
            "Supports 90+ languages including Arabic, Chinese, Japanese, Hindi, etc. "
            "Returns text with line regions and confidence scores. No API key needed."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "image_path": {"type": "string", "description": "Path to image or scanned document."},
                "languages": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "Language codes (e.g. ['en', 'ar']). Default: ['en'].",
                    "default": ["en"],
                },
            },
            "required": ["image_path"],
        },
        "execute": execute_ocr,
    })
