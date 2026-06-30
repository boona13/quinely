"""
Ghost Session Memory — Auto-save conversation summaries to persistent memory.

Registers on_shutdown and on_session_end hooks to capture session context.
Generates LLM-summarized markdown files with descriptive slugs.
Saves to ~/.ghost/memory/sessions/YYYY-MM-DD-{slug}.md for searchable recall.

Session Maintenance: Automatic cleanup with configurable limits.
"""

import gzip
import json
import logging
import os
import re
import tempfile
import threading
import time
from datetime import datetime, timedelta
from pathlib import Path

log = logging.getLogger("quinely.session_memory")

GHOST_HOME = Path.home() / ".ghost"
SESSION_MEMORY_DIR = GHOST_HOME / "memory" / "sessions"
SESSION_ARCHIVE_DIR = GHOST_HOME / "memory" / "sessions" / "archive"
SESSION_MEMORY_DIR.mkdir(parents=True, exist_ok=True)
SESSION_ARCHIVE_DIR.mkdir(parents=True, exist_ok=True)

MAX_MESSAGES_TO_SUMMARIZE = 15
MAX_ENTRIES_TO_CAPTURE = 30

# Session maintenance defaults
DEFAULT_SESSION_MAX_COUNT = 100
DEFAULT_SESSION_MAX_AGE_DAYS = 30
DEFAULT_SESSION_DISK_BUDGET_MB = 500
DEFAULT_SESSION_AUTO_CLEANUP = True
DEFAULT_SESSION_ARCHIVE_AGE_DAYS = 7

# Thread safety lock for maintenance operations
_maintenance_lock = threading.Lock()


def _slugify(text: str, max_len: int = 50) -> str:
    """Convert text to a URL-safe slug."""
    text = text.lower().strip()
    text = re.sub(r"[^\w\s-]", "", text)
    text = re.sub(r"[\s_]+", "-", text)
    text = re.sub(r"-+", "-", text).strip("-")
    return text[:max_len] or "session"


def _extract_session_content(feed_entries: list) -> str:
    """Extract recent session entries into a readable conversation log."""
    lines = []
    for entry in feed_entries[:MAX_ENTRIES_TO_CAPTURE]:
        etype = entry.get("type", "unknown")
        source = entry.get("source", "")[:200]
        result = entry.get("result", "")[:300]
        tools = entry.get("tools_used", [])
        skill = entry.get("skill", "")

        if etype == "ask":
            lines.append(f"User: {source}")
            if tools:
                lines.append(f"  [Used tools: {', '.join(tools)}]")
            lines.append(f"Ghost: {result}")
        elif etype == "cron":
            lines.append(f"[Cron] {source[:100]}")
            lines.append(f"  Result: {result[:200]}")
        elif etype in ("error", "code", "url", "long_text"):
            lines.append(f"[{etype}] {source[:100]}")
            lines.append(f"  Analysis: {result[:200]}")

        if skill:
            lines.append(f"  [Skill: {skill}]")
        lines.append("")

    return "\n".join(lines)


def _generate_summary_and_slug(content: str, engine=None) -> tuple[str, str]:
    """Generate a summary and slug using the LLM, or fall back to timestamp."""
    if engine:
        try:
            prompt = (
                "Summarize this conversation session in 2-3 sentences. "
                "Then provide a 3-5 word slug (lowercase, hyphens) that captures the main topic.\n"
                "Format your response EXACTLY as:\n"
                "SUMMARY: <your summary>\n"
                "SLUG: <your-slug>\n\n"
                f"Conversation:\n{content[:3000]}"
            )
            result = engine.single_shot(
                system_prompt="You generate concise session summaries. Be specific about what was discussed.",
                user_message=prompt,
                temperature=0.2,
                max_tokens=200,
            )
            if result:
                summary = ""
                slug = ""
                for line in result.split("\n"):
                    line = line.strip()
                    if line.upper().startswith("SUMMARY:"):
                        summary = line[8:].strip()
                    elif line.upper().startswith("SLUG:"):
                        slug = _slugify(line[5:].strip())
                if summary and slug:
                    return summary, slug
                if summary:
                    return summary, _slugify(summary[:50])
        except Exception as e:
            log.debug("LLM summary generation failed: %s", e)

    ts = datetime.now().strftime("%H%M")
    return "Session auto-saved on shutdown.", f"session-{ts}"


def save_session(feed_entries: list, engine=None, memory_db=None,
                 hybrid_memory=None) -> str | None:
    """Save the current session to a markdown file and optionally to memory DB.

    Returns the file path if saved, None if nothing to save.
    """
    if not feed_entries:
        return None

    recent = [e for e in feed_entries if e.get("type") in
              ("ask", "cron", "error", "code", "url", "long_text", "image")]
    if not recent:
        return None

    content = _extract_session_content(recent)
    if not content.strip():
        return None

    summary, slug = _generate_summary_and_slug(content, engine)

    date_str = datetime.now().strftime("%Y-%m-%d")
    filename = f"{date_str}-{slug}.md"
    filepath = SESSION_MEMORY_DIR / filename

    counter = 1
    while filepath.exists():
        filepath = SESSION_MEMORY_DIR / f"{date_str}-{slug}-{counter}.md"
        counter += 1

    session_md = (
        f"# Session: {date_str} — {slug}\n\n"
        f"**Summary:** {summary}\n\n"
        f"**Timestamp:** {datetime.now().isoformat()}\n"
        f"**Entries:** {len(recent)}\n"
        f"**Tools used:** {', '.join(set(t for e in recent for t in e.get('tools_used', []))) or 'none'}\n\n"
        f"---\n\n"
        f"## Conversation Log\n\n"
        f"{content}\n"
    )

    filepath.write_text(session_md, encoding="utf-8")
    log.info("Session saved to %s", filepath)

    if memory_db:
        try:
            memory_db.save(
                content=f"Session summary ({date_str}): {summary}",
                type="session",
                source_preview=f"session:{slug}",
                tags="session,auto-save",
            )
        except Exception as e:
            log.debug("Failed to save session to memory DB: %s", e)

    if hybrid_memory:
        try:
            from ghost_hybrid_memory import get_manager
            mgr = get_manager()
            mgr.index_file(str(filepath), source="session")
        except Exception as e:
            log.debug("Failed to index session in hybrid memory: %s", e)

    return str(filepath)


def build_session_maintenance_tools(cfg):
    """Build tools for session maintenance management."""
    
    def session_stats(**kwargs):
        """Get session storage statistics."""
        return get_session_stats()
    
    def session_cleanup(dry_run=False, max_count=None, max_age_days=None, **kwargs):
        """Run session cleanup based on configured limits.
        
        Args:
            dry_run: If True, only report what would be deleted without actually deleting
            max_count: Override max session count (uses config default if not set)
            max_age_days: Override max age in days (uses config default if not set)
        """
        if max_count is None:
            max_count = cfg.get("session_max_count", DEFAULT_SESSION_MAX_COUNT)
        if max_age_days is None:
            max_age_days = cfg.get("session_max_age_days", DEFAULT_SESSION_MAX_AGE_DAYS)
        
        result = cleanup_old_sessions(
            max_count=max_count,
            max_age_days=max_age_days,
            dry_run=dry_run
        )
        result["dry_run"] = dry_run
        return result
    
    def session_maintenance(**kwargs):
        """Run full session maintenance (cleanup + disk budget enforcement)."""
        return run_maintenance(cfg)
    
    def archive_sessions_tool(older_than_days=7, dry_run=False, **kwargs):
        """Archive sessions older than N days (compress to .md.gz)."""
        return archive_sessions(older_than_days=older_than_days, dry_run=dry_run)
    
    return [
        {
            "name": "session_stats",
            "description": "Get statistics about session storage (file count, disk usage, age range)",
            "parameters": {"type": "object", "properties": {}},
            "execute": session_stats,
        },
        {
            "name": "session_cleanup",
            "description": "Clean up old session files based on count and age limits",
            "parameters": {
                "type": "object",
                "properties": {
                    "dry_run": {"type": "boolean", "description": "Only report what would be deleted", "default": False},
                    "max_count": {"type": "integer", "description": "Override max session count"},
                    "max_age_days": {"type": "integer", "description": "Override max age in days"},
                },
            },
            "execute": session_cleanup,
        },
        {
            "name": "session_maintenance",
            "description": "Run full session maintenance including disk budget enforcement",
            "parameters": {"type": "object", "properties": {}},
            "execute": session_maintenance,
        },
        {
            "name": "archive_sessions",
            "description": "Compress and archive session files older than a specified number of days to save disk space",
            "parameters": {
                "type": "object",
                "properties": {
                    "older_than_days": {"type": "integer", "description": "Archive sessions older than this many days", "default": 7},
                    "dry_run": {"type": "boolean", "description": "Only report what would be archived without actually archiving", "default": False},
                },
            },
            "execute": archive_sessions_tool,
        },
    ]


def register_session_hooks(hook_runner, daemon):
    """Register session memory hooks with the hook runner.

    Called during daemon initialization to wire up auto-save.
    """

    def _on_shutdown():
        try:
            from ghost import read_feed
            feed = read_feed()
            if feed:
                save_session(
                    feed_entries=feed,
                    engine=daemon.engine,
                    memory_db=daemon.memory_db,
                )
        except Exception as e:
            log.debug("Session memory save on shutdown failed: %s", e)

    def _on_session_end(entries):
        try:
            if entries:
                save_session(
                    feed_entries=entries,
                    engine=daemon.engine,
                    memory_db=daemon.memory_db,
                )
        except Exception as e:
            log.debug("Session memory save on session_end failed: %s", e)

    hook_runner.register("on_shutdown", _on_shutdown, priority=10, plugin_id="session_memory")
    hook_runner.register("on_session_end", _on_session_end, priority=10, plugin_id="session_memory")
    log.info("Session memory hooks registered")


# ═════════════════════════════════════════════════════════════════════
#  SESSION MAINTENANCE — Auto-cleanup with configurable limits
# ═════════════════════════════════════════════════════════════════════

def _get_session_files():
    """Get all session markdown files sorted by modification time (oldest first)."""
    files = []
    try:
        for f in SESSION_MEMORY_DIR.glob("*.md"):
            if f.is_file():
                stat = f.stat()
                files.append({
                    "path": f,
                    "size": stat.st_size,
                    "mtime": stat.st_mtime,
                    "name": f.name,
                })
        files.sort(key=lambda x: x["mtime"])
    except OSError as exc:
        log.warning("Failed to list session files: %s", exc)
    return files


def get_session_stats():
    """Get statistics about session storage.
    
    Returns dict with:
    - file_count: number of session files
    - total_bytes: total disk usage
    - oldest_file: timestamp of oldest session
    - newest_file: timestamp of newest session
    - archived_count: number of archived sessions
    """
    files = _get_session_files()
    archived = []
    try:
        archived = list(SESSION_ARCHIVE_DIR.glob("*.md.gz"))
    except OSError as exc:
        log.debug("Failed to list archived sessions: %s", exc)
    
    if not files:
        return {
            "file_count": 0,
            "total_bytes": 0,
            "total_mb": 0,
            "oldest_file": None,
            "newest_file": None,
            "archived_count": len(archived),
        }
    
    total_bytes = sum(f["size"] for f in files)
    return {
        "file_count": len(files),
        "total_bytes": total_bytes,
        "total_mb": round(total_bytes / (1024 * 1024), 2),
        "oldest_file": datetime.fromtimestamp(files[0]["mtime"]).isoformat(),
        "newest_file": datetime.fromtimestamp(files[-1]["mtime"]).isoformat(),
        "archived_count": len(archived),
    }


def archive_sessions(older_than_days=7, dry_run=False):
    """Compress and archive sessions older than N days.
    
    Args:
        older_than_days: Archive sessions older than this many days
        dry_run: If True, only report what would be archived
    
    Returns:
        Dict with archived_count, bytes_reclaimed, errors
    """
    cutoff = time.time() - (older_than_days * 24 * 3600)
    files = _get_session_files()
    to_archive = [f for f in files if f["mtime"] < cutoff]
    
    archived_count = 0
    bytes_reclaimed = 0
    errors = []
    
    for f in to_archive:
        src = f["path"]
        dst = SESSION_ARCHIVE_DIR / f"{src.stem}.md.gz"
        
        if dry_run:
            archived_count += 1
            bytes_reclaimed += f["size"]
            continue
        
        try:
            with open(src, "rb") as sf:
                with gzip.open(dst, "wb") as df:
                    df.write(sf.read())
            src.unlink()
            archived_count += 1
            bytes_reclaimed += f["size"]
            log.info("Archived session: %s -> %s", src.name, dst.name)
        except (OSError, IOError, gzip.BadGzipFile) as exc:
            log.warning("Failed to archive %s: %s", src.name, exc)
            errors.append(f"{src.name}: {exc}")
    
    return {
        "archived_count": archived_count,
        "bytes_reclaimed": bytes_reclaimed,
        "mb_reclaimed": round(bytes_reclaimed / (1024 * 1024), 2),
        "errors": errors,
    }


def cleanup_old_sessions(max_count=None, max_age_days=None, dry_run=False):
    """Delete old session files based on count and age limits.
    
    Args:
        max_count: Keep only the most recent N sessions
        max_age_days: Delete sessions older than this many days
        dry_run: If True, only report what would be deleted
    
    Returns:
        Dict with deleted_count, bytes_reclaimed, errors
    """
    max_count = max_count or DEFAULT_SESSION_MAX_COUNT
    max_age_days = max_age_days or DEFAULT_SESSION_MAX_AGE_DAYS
    
    files = _get_session_files()
    cutoff = time.time() - (max_age_days * 24 * 3600)
    
    to_delete = []
    
    # Delete files exceeding max_count (oldest first)
    if len(files) > max_count:
        to_delete.extend(files[:-max_count])
    
    # Delete files older than max_age_days
    for f in files:
        if f["mtime"] < cutoff and f not in to_delete:
            to_delete.append(f)
    
    deleted_count = 0
    bytes_reclaimed = 0
    errors = []
    
    for f in to_delete:
        if dry_run:
            deleted_count += 1
            bytes_reclaimed += f["size"]
            continue
        
        try:
            f["path"].unlink()
            deleted_count += 1
            bytes_reclaimed += f["size"]
            log.info("Deleted old session: %s", f["name"])
        except OSError as exc:
            log.warning("Failed to delete %s: %s", f["name"], exc)
            errors.append(f"{f['name']}: {exc}")
    
    return {
        "deleted_count": deleted_count,
        "bytes_reclaimed": bytes_reclaimed,
        "mb_reclaimed": round(bytes_reclaimed / (1024 * 1024), 2),
        "errors": errors,
    }


def run_maintenance(cfg=None):
    """Run full session maintenance based on config.
    
    This is the main entry point called by cron or manual trigger.
    Applies count limits, age limits, and disk budget enforcement.
    
    Args:
        cfg: Ghost config dict (optional, loads from file if not provided)
    
    Returns:
        Dict with maintenance results and statistics
    """
    with _maintenance_lock:
        if cfg is None:
            try:
                config_path = GHOST_HOME / "config.json"
                if config_path.exists():
                    cfg = json.loads(config_path.read_text(encoding="utf-8"))
                else:
                    cfg = {}
            except json.JSONDecodeError as exc:
                log.warning("Failed to load config for maintenance: %s", exc)
                cfg = {}
        
        # Read config with defaults. Coalesce None (an explicit ``null`` in the
        # config file) to the defaults too — otherwise the numeric limits below
        # compare against None and raise TypeError, aborting maintenance.
        auto_cleanup = cfg.get("session_auto_cleanup", DEFAULT_SESSION_AUTO_CLEANUP)
        max_count = cfg.get("session_max_count") or DEFAULT_SESSION_MAX_COUNT
        max_age_days = cfg.get("session_max_age_days") or DEFAULT_SESSION_MAX_AGE_DAYS
        disk_budget_mb = cfg.get("session_disk_budget_mb") or DEFAULT_SESSION_DISK_BUDGET_MB
        archive_age_days = cfg.get("session_archive_age_days") or DEFAULT_SESSION_ARCHIVE_AGE_DAYS
        
        if not auto_cleanup:
            log.info("Session auto-cleanup disabled, skipping maintenance")
            return {"status": "skipped", "reason": "auto_cleanup_disabled"}
        
        log.info("Starting session maintenance (max_count=%s, max_age=%s days, budget=%s MB)",
                 max_count, max_age_days, disk_budget_mb)
        
        results = {
            "status": "success",
            "config": {
                "max_count": max_count,
                "max_age_days": max_age_days,
                "disk_budget_mb": disk_budget_mb,
                "archive_age_days": archive_age_days,
            },
            "before": get_session_stats(),
        }
        
        # Step 1: Archive old sessions before deletion (preserves history)
        archive_result = archive_sessions(
            older_than_days=archive_age_days,
            dry_run=False
        )
        results["archive"] = archive_result
        
        # Step 2: Apply count and age limits (deletion)
        cleanup_result = cleanup_old_sessions(
            max_count=max_count,
            max_age_days=max_age_days,
            dry_run=False
        )
        results["cleanup"] = cleanup_result
        
        # Step 2: Enforce disk budget if still over
        files = _get_session_files()
        total_mb = sum(f["size"] for f in files) / (1024 * 1024)
        disk_cleanup = {"deleted_count": 0, "bytes_reclaimed": 0, "mb_reclaimed": 0}
        
        while total_mb > disk_budget_mb and len(files) > 10:
            # Delete oldest files until under budget or minimum reached
            to_delete = files.pop(0)
            try:
                to_delete["path"].unlink()
                disk_cleanup["deleted_count"] += 1
                disk_cleanup["bytes_reclaimed"] += to_delete["size"]
                total_mb -= to_delete["size"] / (1024 * 1024)
                log.info("Disk budget cleanup: deleted %s", to_delete["name"])
            except OSError as exc:
                log.warning("Failed to delete %s: %s", to_delete["name"], exc)
        
        disk_cleanup["mb_reclaimed"] = round(disk_cleanup["bytes_reclaimed"] / (1024 * 1024), 2)
        results["disk_cleanup"] = disk_cleanup
        
        results["after"] = get_session_stats()
        
        total_reclaimed = (
            archive_result.get("bytes_reclaimed", 0) +
            cleanup_result.get("bytes_reclaimed", 0) +
            disk_cleanup.get("bytes_reclaimed", 0)
        )
        total_deleted = cleanup_result.get("deleted_count", 0) + disk_cleanup.get("deleted_count", 0)
        total_archived = archive_result.get("archived_count", 0)
        log.info("Session maintenance complete: archived %s files, deleted %s files, reclaimed %.2f MB",
                 total_archived, total_deleted, total_reclaimed / (1024 * 1024))
        
        return results


def bootstrap_session_maintenance_cron(cron_service, cfg):
    """Register session maintenance as a daily cron job. Idempotent — skips already registered.
    
    Args:
        cron_service: The CronService instance
        cfg: Ghost configuration dict
    """
    if not cron_service:
        return
    
    # Only register if session memory is enabled
    if not cfg.get("enable_session_memory", True):
        return
    
    from ghost_cron import make_job
    store = cron_service.store
    
    job_name = "session_maintenance"
    existing_jobs = {j["name"]: j for j in store.get_all()}
    
    if job_name in existing_jobs:
        # Already registered, nothing to do
        return
    
    # Default: run daily at 3 AM (low activity period)
    schedule = {"kind": "cron", "expr": "0 3 * * *"}
    
    job = make_job(
        name=job_name,
        schedule=schedule,
        payload={"type": "session_maintenance"},
        description="Daily session cleanup to enforce count/age limits and disk budget",
        enabled=True,
    )
    store.add(job)
    cron_service._arm_timer()
    log.info("Session maintenance cron job registered (runs daily at 3 AM)")
