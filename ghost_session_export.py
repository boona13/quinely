"""
Quinely Session Export — Export and import chat sessions in multiple formats.

Supports JSON (full data), Markdown (readable), and HTML (styled) formats.
Exports include messages, tool calls, skills used, model info, timestamps, token counts.
All file operations are thread-safe with atomic writes.
"""

import json
import logging
import os
import re
import threading
import uuid
from datetime import datetime
from pathlib import Path
from typing import Any, Optional

log = logging.getLogger("ghost.session_export")

GHOST_HOME = Path.home() / ".ghost"
EXPORTS_DIR = GHOST_HOME / "exports"
EXPORTS_DIR.mkdir(parents=True, exist_ok=True)

# Thread-safe file operations
_export_lock = threading.Lock()

# Export index for tracking
EXPORTS_INDEX_FILE = EXPORTS_DIR / "index.json"
MAX_EXPORTS_PER_SESSION = 10

# HTML template for styled exports
_HTML_TEMPLATE = '''<!DOCTYPE html>
<html lang="en">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>Quinely Chat Export — {title}</title>
    <style>
        * { box-sizing: border-box; margin: 0; padding: 0; }
        body {
            font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, sans-serif;
            background: linear-gradient(135deg, #0a0a12 0%, #151520 100%);
            color: #e4e4e7;
            line-height: 1.6;
            min-height: 100vh;
            padding: 2rem 1rem;
        }
        .container { max-width: 900px; margin: 0 auto; }
        .header {
            background: rgba(139, 92, 246, 0.1);
            border: 1px solid rgba(139, 92, 246, 0.2);
            border-radius: 12px;
            padding: 1.5rem;
            margin-bottom: 1.5rem;
        }
        .header h1 { color: #a78bfa; font-size: 1.5rem; margin-bottom: 0.5rem; }
        .header .meta { color: #71717a; font-size: 0.875rem; }
        .header .meta span { margin-right: 1rem; }
        .message {
            background: rgba(255, 255, 255, 0.03);
            border: 1px solid rgba(255, 255, 255, 0.06);
            border-radius: 12px;
            padding: 1.25rem;
            margin-bottom: 1rem;
        }
        .message.user { border-left: 3px solid #3b82f6; }
        .message.assistant { border-left: 3px solid #8b5cf6; }
        .message .role {
            font-size: 0.75rem;
            font-weight: 600;
            text-transform: uppercase;
            letter-spacing: 0.05em;
            margin-bottom: 0.5rem;
        }
        .message.user .role { color: #60a5fa; }
        .message.assistant .role { color: #a78bfa; }
        .message .content {
            color: #e4e4e7;
            white-space: pre-wrap;
            word-wrap: break-word;
        }
        .message .content code {
            background: rgba(139, 92, 246, 0.1);
            padding: 0.125rem 0.375rem;
            border-radius: 4px;
            font-family: 'SF Mono', Monaco, monospace;
            font-size: 0.875em;
        }
        .message .content pre {
            background: rgba(0, 0, 0, 0.3);
            padding: 1rem;
            border-radius: 8px;
            overflow-x: auto;
            margin: 0.75rem 0;
        }
        .message .content pre code { background: none; padding: 0; }
        .tools {
            margin-top: 0.75rem;
            padding-top: 0.75rem;
            border-top: 1px solid rgba(255, 255, 255, 0.06);
        }
        .tools .label { font-size: 0.75rem; color: #71717a; margin-bottom: 0.375rem; }
        .tools .list { display: flex; flex-wrap: wrap; gap: 0.375rem; }
        .tool-tag {
            background: rgba(139, 92, 246, 0.15);
            color: #a78bfa;
            font-size: 0.75rem;
            padding: 0.25rem 0.5rem;
            border-radius: 4px;
        }
        .footer {
            text-align: center;
            color: #52525b;
            font-size: 0.75rem;
            margin-top: 2rem;
            padding-top: 1.5rem;
            border-top: 1px solid rgba(255, 255, 255, 0.06);
        }
    </style>
</head>
<body>
    <div class="container">
        <div class="header">
            <h1>&#x1F47B; Quinely Chat Session</h1>
            <div class="meta">
                <span>&#x1F4C5; {date}</span>
                <span>&#x1F4AC; {message_count} messages</span>
                {model_info}
            </div>
        </div>
        {messages}
        <div class="footer">Exported from Quinely Autonomous Agent</div>
    </div>
</body>
</html>'''


def _slugify(text: str, max_len: int = 40) -> str:
    """Convert text to a URL-safe slug."""
    if not text:
        return "session"
    text = text.lower().strip()
    text = re.sub(r"[^\w\s-]", "", text)
    text = re.sub(r"[\s_]+", "-", text)
    text = re.sub(r"-+", "-", text).strip("-")
    return text[:max_len] or "session"


def _sanitize_filename(filename: str) -> str:
    """Sanitize a filename to prevent directory traversal."""
    filename = Path(filename).name
    filename = re.sub(r'[<>:"/\\|?*\x00-\x1f]', "_", filename)
    return filename[:200]


def _atomic_write_json(filepath: Path, data: dict) -> None:
    """Write JSON to file atomically using temp file + rename."""
    temp_path = filepath.with_suffix(filepath.suffix + ".tmp")
    temp_path.write_text(json.dumps(data, indent=2, default=str), encoding="utf-8")
    os.replace(str(temp_path), str(filepath))


def _atomic_write_text(filepath: Path, content: str) -> None:
    """Write text to file atomically using temp file + rename."""
    temp_path = filepath.with_suffix(filepath.suffix + ".tmp")
    temp_path.write_text(content, encoding="utf-8")
    os.replace(str(temp_path), str(filepath))


def _load_exports_index() -> dict:
    """Load the exports index file (thread-safe)."""
    with _export_lock:
        if EXPORTS_INDEX_FILE.exists():
            try:
                with open(EXPORTS_INDEX_FILE, "r", encoding="utf-8") as f:
                    return json.load(f)
            except (json.JSONDecodeError, IOError) as e:
                log.warning("Failed to load exports index: %s", e)
                return {"exports": {}, "sessions": {}}
        return {"exports": {}, "sessions": {}}


def _save_exports_index(index: dict) -> None:
    """Save the exports index file atomically (thread-safe)."""
    with _export_lock:
        try:
            temp_file = EXPORTS_INDEX_FILE.with_suffix(".tmp")
            with open(temp_file, "w", encoding="utf-8") as f:
                json.dump(index, f, indent=2, ensure_ascii=False)
            os.replace(str(temp_file), str(EXPORTS_INDEX_FILE))
        except IOError as e:
            log.error("Failed to save exports index: %s", e)
            raise


def _cleanup_old_exports(session_id: str, max_exports: int = MAX_EXPORTS_PER_SESSION) -> None:
    """Remove oldest exports when exceeding max_exports limit."""
    index = _load_exports_index()
    session_exports = index.get("sessions", {}).get(session_id, [])
    
    if len(session_exports) <= max_exports:
        return
    
    to_remove = session_exports[:-max_exports]
    for export_id in to_remove:
        try:
            delete_export(export_id, update_index=False)
        except Exception as e:
            log.warning("Failed to cleanup old export %s: %s", export_id, e)
    
    index = _load_exports_index()
    index["sessions"][session_id] = [
        eid for eid in session_exports 
        if eid not in to_remove and eid in index.get("exports", {})
    ]
    _save_exports_index(index)


def get_export(export_id: str, **kwargs) -> Optional[dict]:
    """Get export metadata by ID."""
    index = _load_exports_index()
    return index.get("exports", {}).get(export_id)


def get_session_exports(session_id: str, **kwargs) -> list:
    """Get all exports for a specific session (newest first)."""
    index = _load_exports_index()
    export_ids = index.get("sessions", {}).get(session_id, [])
    exports = [index["exports"][eid] for eid in export_ids if eid in index.get("exports", {})]
    return sorted(exports, key=lambda x: x.get("created_at", ""), reverse=True)


def delete_export(export_id: str, update_index: bool = True, **kwargs) -> bool:
    """Delete an export by ID."""
    index = _load_exports_index()
    export = index.get("exports", {}).get(export_id)
    if not export:
        return False
    
    filepath = Path(export.get("filepath", ""))
    session_id = export.get("session_id")
    
    # Delete file
    try:
        if filepath.exists():
            filepath.unlink()
    except OSError as e:
        log.warning("Failed to delete export file %s: %s", filepath, e)
    
    if update_index:
        # Remove from index
        del index["exports"][export_id]
        if session_id and session_id in index.get("sessions", {}):
            index["sessions"][session_id] = [
                eid for eid in index["sessions"][session_id] if eid != export_id
            ]
        _save_exports_index(index)
    
    log.info("Deleted export %s", export_id)
    return True


def export_session(
    messages: list[dict],
    format: str = "json",
    encrypt: bool = False,
    model: str = "",
    metadata: dict | None = None,
    session_id: str = "",
    **kwargs
) -> dict[str, Any]:
    """Export a chat session to a file."""
    if not isinstance(messages, list):
        return {"ok": False, "error": "messages must be a list", "file_path": None}
    if format not in ("json", "markdown", "html"):
        return {"ok": False, "error": f"Invalid format: {format}", "file_path": None}
    
    try:
        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        slug = "session"
        for msg in messages:
            if msg.get("role") == "user":
                content = msg.get("content", "")
                if content:
                    slug = _slugify(content[:100])
                    break
        filename_base = f"ghost_export_{timestamp}_{slug}"
        
        with _export_lock:
            if format == "json":
                return _export_json(messages, filename_base, model, metadata, session_id)
            elif format == "markdown":
                return _export_markdown(messages, filename_base, model, metadata, session_id)
            elif format == "html":
                return _export_html(messages, filename_base, model, metadata, session_id)
    except Exception as e:
        log.error("Export failed: %s", e, exc_info=True)
        return {"ok": False, "error": f"Export failed: {e}", "file_path": None}
    return {"ok": False, "error": "Unknown export error", "file_path": None}


def _export_json(messages: list[dict], filename_base: str, model: str, metadata: dict | None, session_id: str = "") -> dict:
    """Export session as JSON with full data."""
    export_id = str(uuid.uuid4())
    filename = _sanitize_filename(f"{filename_base}-{export_id[:8]}.json")
    filepath = EXPORTS_DIR / filename
    
    export_data = {
        "version": "1.0",
        "export_format": "json",
        "exported_at": datetime.now().isoformat(),
        "model": model,
        "message_count": len(messages),
        "metadata": metadata or {},
        "messages": messages,
    }
    _atomic_write_json(filepath, export_data)
    
    # Update index
    index = _load_exports_index()
    export_record = {
        "id": export_id,
        "session_id": session_id or "unknown",
        "format": "json",
        "filepath": str(filepath),
        "filename": filename,
        "created_at": datetime.now().isoformat(),
        "size_bytes": len(json.dumps(export_data).encode("utf-8")),
        "message_count": len(messages),
    }
    index["exports"][export_id] = export_record
    if session_id:
        if session_id not in index["sessions"]:
            index["sessions"][session_id] = []
        index["sessions"][session_id].append(export_id)
    _save_exports_index(index)
    
    # Cleanup old exports
    if session_id:
        _cleanup_old_exports(session_id)
    
    log.info("Exported session to JSON: %s (id: %s)", filepath, export_id)
    return {
        "ok": True, 
        "export_id": export_id,
        "file_path": str(filepath), 
        "filename": filename, 
        "format": "json",
        "share_url": f"/api/chat/exports/{export_id}",
        "download_url": f"/api/chat/exports/{export_id}?download=1",
        "created_at": export_record["created_at"],
    }


def _export_markdown(messages: list[dict], filename_base: str, model: str, metadata: dict | None, session_id: str = "") -> dict:
    """Export session as readable Markdown."""
    export_id = str(uuid.uuid4())
    filename = _sanitize_filename(f"{filename_base}-{export_id[:8]}.md")
    filepath = EXPORTS_DIR / filename
    
    lines = [
        "# Quinely Chat Session",
        "",
        f"**Exported:** {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}",
    ]
    if model:
        lines.append(f"**Model:** {model}")
    if metadata:
        if metadata.get("skill"):
            lines.append(f"**Skill:** {metadata['skill']}")
        if metadata.get("tools_used"):
            tools = metadata["tools_used"]
            if isinstance(tools, list):
                lines.append(f"**Tools Used:** {', '.join(str(t) for t in tools)}")
    lines.extend(["", f"**Messages:** {len(messages)}", "", "---", ""])
    
    for msg in messages:
        role = msg.get("role", "unknown")
        content = msg.get("content", "")
        timestamp = msg.get("timestamp", "")
        if role == "user":
            lines.append(f"## &#x1F464; User")
        elif role == "assistant":
            lines.append(f"## &#x1F47B; Quinely")
        else:
            lines.append(f"## {role.capitalize()}")
        if timestamp:
            lines.append(f"*{timestamp}*")
        lines.append("")
        lines.append(content)
        tools = msg.get("tools_used") or msg.get("tool_calls")
        if tools and isinstance(tools, list):
            lines.append("")
            lines.append(f"**Tools:** {', '.join(str(t) for t in tools)}")
        lines.extend(["", "---", ""])
    
    content = "\n".join(lines)
    _atomic_write_text(filepath, content)
    
    # Update index
    index = _load_exports_index()
    export_record = {
        "id": export_id,
        "session_id": session_id or "unknown",
        "format": "markdown",
        "filepath": str(filepath),
        "filename": filename,
        "created_at": datetime.now().isoformat(),
        "size_bytes": len(content.encode("utf-8")),
        "message_count": len(messages),
    }
    index["exports"][export_id] = export_record
    if session_id:
        if session_id not in index["sessions"]:
            index["sessions"][session_id] = []
        index["sessions"][session_id].append(export_id)
    _save_exports_index(index)
    
    # Cleanup old exports
    if session_id:
        _cleanup_old_exports(session_id)
    
    log.info("Exported session to Markdown: %s (id: %s)", filepath, export_id)
    return {
        "ok": True, 
        "export_id": export_id,
        "file_path": str(filepath), 
        "filename": filename, 
        "format": "markdown",
        "share_url": f"/api/chat/exports/{export_id}",
        "download_url": f"/api/chat/exports/{export_id}?download=1",
        "created_at": export_record["created_at"],
    }


def _export_html(messages: list[dict], filename_base: str, model: str, metadata: dict | None, session_id: str = "") -> dict:
    """Export session as styled HTML."""
    export_id = str(uuid.uuid4())
    filename = _sanitize_filename(f"{filename_base}-{export_id[:8]}.html")
    filepath = EXPORTS_DIR / filename
    message_html = []
    
    for msg in messages:
        role = msg.get("role", "unknown")
        content = msg.get("content", "")
        # Escape HTML
        content = content.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
        # Format code
        content = _format_code_blocks(content)
        role_class = "user" if role == "user" else "assistant"
        role_label = "You" if role == "user" else "Quinely"
        
        tools_html = ""
        tools = msg.get("tools_used") or msg.get("tool_calls")
        if tools and isinstance(tools, list):
            tool_tags = "".join(f'<span class="tool-tag">{t}</span>' for t in tools)
            tools_html = f'<div class="tools"><div class="label">Tools:</div><div class="list">{tool_tags}</div></div>'
        
        msg_html = f'<div class="message {role_class}"><div class="role">{role_label}</div><div class="content">{content}</div>{tools_html}</div>'
        message_html.append(msg_html)
    
    model_info = f'<span>&#x1F916; {model}</span>' if model else ''
    html = _HTML_TEMPLATE.format(
        title=_slugify(filename_base).replace("-", " ").title(),
        date=datetime.now().strftime("%Y-%m-%d %H:%M"),
        message_count=len(messages),
        model_info=model_info,
        messages="\n".join(message_html),
    )
    _atomic_write_text(filepath, html)
    
    # Update index
    index = _load_exports_index()
    export_record = {
        "id": export_id,
        "session_id": session_id or "unknown",
        "format": "html",
        "filepath": str(filepath),
        "filename": filename,
        "created_at": datetime.now().isoformat(),
        "size_bytes": len(html.encode("utf-8")),
        "message_count": len(messages),
    }
    index["exports"][export_id] = export_record
    if session_id:
        if session_id not in index["sessions"]:
            index["sessions"][session_id] = []
        index["sessions"][session_id].append(export_id)
    _save_exports_index(index)
    
    # Cleanup old exports
    if session_id:
        _cleanup_old_exports(session_id)
    
    log.info("Exported session to HTML: %s (id: %s)", filepath, export_id)
    return {
        "ok": True, 
        "export_id": export_id,
        "file_path": str(filepath), 
        "filename": filename, 
        "format": "html",
        "share_url": f"/api/chat/exports/{export_id}",
        "download_url": f"/api/chat/exports/{export_id}?download=1",
        "created_at": export_record["created_at"],
    }


def _format_code_blocks(content: str) -> str:
    """Format markdown code blocks as HTML."""
    # Handle code blocks
    def replace_code_block(match):
        lang = match.group(1) or ""
        code = match.group(2)
        return f'<pre><code class="language-{lang}">{code}</code></pre>'
    content = re.sub(r'```(\w*)?\n(.*?)```', replace_code_block, content, flags=re.DOTALL)
    # Handle inline code
    content = re.sub(r'`([^`]+)`', r'<code>\1</code>', content)
    # Convert newlines outside pre tags
    parts = []
    i = 0
    while i < len(content):
        if content[i:i+5] == '<pre>':
            end = content.find('</pre>', i)
            if end == -1:
                parts.append(content[i:])
                break
            parts.append(content[i:end+6])
            i = end + 6
        else:
            end = content.find('<pre>', i)
            if end == -1:
                parts.append(content[i:].replace("\n", "<br>"))
                break
            parts.append(content[i:end].replace("\n", "<br>"))
            i = end
    return "".join(parts)


def import_session(file_path: str | Path) -> dict[str, Any]:
    """
    Import a chat session from an exported file.
    
    Args:
        file_path: Path to the exported file (JSON only supported for import)
    
    Returns:
        Dict with success status, messages, metadata, and any error
    """
    try:
        filepath = Path(file_path)
        if not filepath.exists():
            return {"ok": False, "error": f"File not found: {file_path}", "messages": None}
        
        suffix = filepath.suffix.lower()
        if suffix == ".json":
            return _import_json(filepath)
        else:
            return {"ok": False, "error": f"Import only supports JSON format, got: {suffix}", "messages": None}
    except Exception as e:
        log.error("Import failed: %s", e, exc_info=True)
        return {"ok": False, "error": f"Import failed: {e}", "messages": None}


def _import_json(filepath: Path) -> dict:
    """Import session from JSON file."""
    with _export_lock:
        data = json.loads(filepath.read_text(encoding="utf-8"))
    
    version = data.get("version", "1.0")
    messages = data.get("messages", [])
    metadata = data.get("metadata", {})
    model = data.get("model", "")
    
    log.info("Imported session from JSON: %s (%d messages)", filepath, len(messages))
    return {
        "ok": True,
        "messages": messages,
        "metadata": metadata,
        "model": model,
        "version": version,
        "message_count": len(messages),
    }


def list_exports(session_id: str = "", **kwargs) -> list[dict]:
    """List all exported sessions, optionally filtered by session_id."""
    if session_id:
        return get_session_exports(session_id)
    
    # Return all exports from index
    index = _load_exports_index()
    exports = list(index.get("exports", {}).values())
    return sorted(exports, key=lambda x: x.get("created_at", ""), reverse=True)


def delete_export(identifier: str, **kwargs) -> dict[str, Any]:
    """Delete an exported session by filename or export_id."""
    try:
        # Try UUID first
        if len(identifier) == 36 and '-' in identifier:
            success = delete_export_by_id(identifier)
            if success:
                return {"ok": True}
        
        # Fall back to filename-based deletion
        filename = _sanitize_filename(identifier)
        filepath = EXPORTS_DIR / filename
        
        # Ensure file is within exports dir (security check)
        if not filepath.resolve().is_relative_to(EXPORTS_DIR.resolve()):
            return {"ok": False, "error": "Invalid filename"}
        
        if not filepath.exists():
            return {"ok": False, "error": "File not found"}
        
        with _export_lock:
            filepath.unlink()
        
        log.info("Deleted export: %s", filepath)
        return {"ok": True}
    except Exception as e:
        log.error("Delete export failed: %s", e, exc_info=True)
        return {"ok": False, "error": str(e)}


def delete_export_by_id(export_id: str) -> bool:
    """Delete an export by its UUID."""
    return delete_export(export_id, update_index=True)
