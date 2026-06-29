"""
Ghost Artifacts — manages deliverable files produced during chat sessions.

Artifacts are files Ghost generates for the user (CSVs, images, charts, PDFs,
audio, etc.) that should be downloadable from the chat UI. Each chat message
gets its own folder under ~/.ghost/artifacts/<message_id>/.

This module:
- Provides helpers to register artifacts (copy/move files into the right folder)
- Exposes a listing/stat API used by the dashboard routes
- Is imported by ghost_tools (file_write hook), ghost_image_gen, ghost_tts, etc.
"""

import json
import logging
import mimetypes
import os
import shutil
import time
from pathlib import Path

log = logging.getLogger("quinely.artifacts")

GHOST_HOME = Path.home() / ".ghost"
ARTIFACTS_ROOT = GHOST_HOME / "artifacts"
ARTIFACTS_ROOT.mkdir(parents=True, exist_ok=True)

_ARTIFACT_EXTENSIONS = {
    # Images
    ".png", ".jpg", ".jpeg", ".gif", ".webp", ".bmp", ".svg", ".tiff",
    # Documents
    ".pdf", ".csv", ".json", ".xml", ".html", ".md", ".txt", ".log",
    # Spreadsheets
    ".xlsx", ".xls",
    # Audio
    ".mp3", ".wav", ".ogg", ".m4a", ".flac", ".aac",
    # Video
    ".mp4", ".webm", ".mov", ".avi",
    # Archives
    ".zip", ".tar", ".gz",
    # Code
    ".py", ".js", ".ts", ".css", ".sql",
}


def get_artifacts_dir(message_id: str) -> Path:
    """Get (and create) the artifacts directory for a message."""
    d = ARTIFACTS_ROOT / message_id
    d.mkdir(parents=True, exist_ok=True)
    return d


def register_artifact(message_id: str, source_path: str | Path,
                      copy: bool = True) -> str | None:
    """Copy or move a file into the artifacts folder for a message.

    Returns the destination path string, or None on failure.
    """
    if not message_id:
        return None
    src = Path(source_path)
    if not src.exists():
        log.warning("Artifact source not found: %s", src)
        return None

    dest_dir = get_artifacts_dir(message_id)
    dest = dest_dir / src.name

    # Avoid overwriting — add suffix if name collision
    counter = 1
    while dest.exists():
        stem = src.stem
        dest = dest_dir / f"{stem}_{counter}{src.suffix}"
        counter += 1

    try:
        if copy:
            shutil.copy2(str(src), str(dest))
        else:
            shutil.move(str(src), str(dest))
        log.info("Artifact registered: %s -> %s", src.name, dest)
        return str(dest)
    except Exception:
        log.warning("Failed to register artifact: %s", source_path, exc_info=True)
        return None


def register_artifact_bytes(message_id: str, data: bytes, filename: str) -> str | None:
    """Write raw bytes as an artifact."""
    if not message_id:
        return None
    dest_dir = get_artifacts_dir(message_id)
    dest = dest_dir / filename
    counter = 1
    while dest.exists():
        stem = Path(filename).stem
        ext = Path(filename).suffix
        dest = dest_dir / f"{stem}_{counter}{ext}"
        counter += 1
    try:
        dest.write_bytes(data)
        log.info("Artifact (bytes) registered: %s", dest)
        return str(dest)
    except Exception:
        log.warning("Failed to write artifact bytes: %s", filename, exc_info=True)
        return None


def list_artifacts(message_id: str) -> list[dict]:
    """List all artifact files for a message. Returns list of file info dicts."""
    d = ARTIFACTS_ROOT / message_id
    if not d.exists() or not d.is_dir():
        return []

    items = []
    for f in sorted(d.iterdir()):
        if not f.is_file():
            continue
        ext = f.suffix.lower()
        mime, _ = mimetypes.guess_type(str(f))
        size = f.stat().st_size
        category = _categorize(ext)
        items.append({
            "filename": f.name,
            "size": size,
            "size_human": _human_size(size),
            "mime": mime or "application/octet-stream",
            "category": category,
            "ext": ext,
            "modified": f.stat().st_mtime,
        })
    return items


def is_artifact_worthy(path: str) -> bool:
    """Check if a file path looks like something the user would want to download."""
    ext = Path(path).suffix.lower()
    return ext in _ARTIFACT_EXTENSIONS


def _categorize(ext: str) -> str:
    """Categorize file extension into a display group."""
    ext = ext.lower()
    if ext in {".png", ".jpg", ".jpeg", ".gif", ".webp", ".bmp", ".svg", ".tiff"}:
        return "image"
    if ext in {".mp3", ".wav", ".ogg", ".m4a", ".flac", ".aac"}:
        return "audio"
    if ext in {".mp4", ".webm", ".mov", ".avi"}:
        return "video"
    if ext == ".pdf":
        return "pdf"
    if ext in {".csv", ".xlsx", ".xls"}:
        return "spreadsheet"
    if ext in {".json", ".xml"}:
        return "data"
    return "file"


def _human_size(nbytes: int) -> str:
    for unit in ("B", "KB", "MB", "GB"):
        if nbytes < 1024:
            return f"{nbytes:.1f} {unit}" if unit != "B" else f"{nbytes} B"
        nbytes /= 1024
    return f"{nbytes:.1f} TB"


# ── Thread-local message_id context ──────────────────────────────────
# The chat pipeline sets this before running the tool loop so that
# file_write, image_gen, TTS, etc. can automatically register artifacts.

import threading

_artifact_ctx = threading.local()


def set_current_message_id(message_id: str | None):
    """Set the message_id for the current thread (called by chat pipeline)."""
    _artifact_ctx.message_id = message_id


def get_current_message_id() -> str | None:
    """Get the message_id for the current thread."""
    return getattr(_artifact_ctx, "message_id", None)


def auto_register(source_path: str | Path, copy: bool = True) -> str | None:
    """Register an artifact using the thread-local message_id.

    Called by tool implementations (file_write, image gen, TTS) after producing
    a deliverable file. No-op if no message_id is set (autonomous/cron context).
    """
    mid = get_current_message_id()
    if not mid:
        return None
    if not is_artifact_worthy(str(source_path)):
        return None
    src = Path(source_path).resolve()
    dest_dir = get_artifacts_dir(mid).resolve()
    if str(src).startswith(str(dest_dir)):
        return str(src)
    return register_artifact(mid, source_path, copy=copy)


# ── Programmatic post-step scanner ──────────────────────────────────
# This is the PRIMARY artifact registration mechanism. It runs after
# every tool step in the chat pipeline, scans the tool result for file
# paths, and registers any artifact-worthy files. Zero LLM reliance.

import re

_PATH_RE = re.compile(
    r'(?:'
    r'"(?:path|file|filename|output|saved)"\s*:\s*"([^"]+)"'  # JSON keys
    r'|'
    r'(?:saved?|wrote|written|generated|created|output)\s+(?:to|at|:)\s*[`"]?'
    r'([/~][^\s`"\'<>,;]+)'                                    # prose paths
    r'|'
    r'(?:^|[\s"`:])(/(?:Users|home|tmp|var)[^\s`"\'<>,;]+)'   # absolute paths
    r'|'
    r'(~/.ghost/[^\s`"\'<>,;]+)'                               # ~/.ghost/ paths
    r')',
    re.IGNORECASE,
)


def scan_tool_result_for_artifacts(message_id: str, tool_name: str,
                                   tool_result: str) -> list[str]:
    """Scan a tool result string for file paths and register artifacts.

    Called by the chat pipeline's on_step callback after every tool execution.
    Returns list of newly registered artifact paths.

    This is the MAIN artifact registration path — it catches everything
    regardless of whether individual tool hooks remembered to call auto_register.
    """
    if not message_id or not tool_result:
        return []

    registered = []

    for m in _PATH_RE.finditer(tool_result):
        raw = m.group(1) or m.group(2) or m.group(3) or m.group(4)
        if not raw:
            continue
        raw = raw.rstrip('.,;:)\'"')
        path = Path(raw).expanduser()
        if not path.is_absolute():
            continue
        if not path.exists() or not path.is_file():
            continue
        if not is_artifact_worthy(str(path)):
            continue

        dest_dir = get_artifacts_dir(message_id).resolve()
        if str(path.resolve()).startswith(str(dest_dir)):
            continue

        result = register_artifact(message_id, str(path), copy=True)
        if result:
            registered.append(result)
            log.info("Post-step artifact scan [%s]: %s -> %s",
                     tool_name, path.name, result)

    return registered
