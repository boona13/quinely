"""
GhostNodes Media Store — unified storage for generated media with metadata tracking.

Stores images, audio, video, and 3D assets with SQLite metadata.
Provides format conversion helpers, TTL-based cleanup, and disk budgets.
"""

import json
import logging
import os
import sqlite3
import threading
import time
import uuid
from pathlib import Path
from typing import Optional

log = logging.getLogger("quinely.media_store")

GHOST_HOME = Path.home() / ".ghost"
MEDIA_DIR = GHOST_HOME / "media"

MEDIA_SUBDIRS = {
    "image": MEDIA_DIR / "images",
    "audio": MEDIA_DIR / "audio",
    "video": MEDIA_DIR / "video",
    "3d": MEDIA_DIR / "3d",
    "temp": MEDIA_DIR / "temp",
    "other": MEDIA_DIR / "other",
}

for d in MEDIA_SUBDIRS.values():
    d.mkdir(parents=True, exist_ok=True)

DB_PATH = MEDIA_DIR / "media.db"

_SCHEMA = """
CREATE TABLE IF NOT EXISTS media (
    id TEXT PRIMARY KEY,
    filename TEXT NOT NULL,
    media_type TEXT NOT NULL,
    path TEXT NOT NULL,
    source_node TEXT DEFAULT '',
    prompt TEXT DEFAULT '',
    params TEXT DEFAULT '{}',
    size_bytes INTEGER DEFAULT 0,
    width INTEGER DEFAULT 0,
    height INTEGER DEFAULT 0,
    duration_secs REAL DEFAULT 0,
    created_at REAL NOT NULL,
    ttl_secs INTEGER DEFAULT 0,
    tags TEXT DEFAULT '[]',
    pipeline_id TEXT DEFAULT '',
    metadata TEXT DEFAULT '{}'
);
CREATE INDEX IF NOT EXISTS idx_media_type ON media(media_type);
CREATE INDEX IF NOT EXISTS idx_media_created ON media(created_at);
CREATE INDEX IF NOT EXISTS idx_media_node ON media(source_node);
CREATE INDEX IF NOT EXISTS idx_media_pipeline ON media(pipeline_id);
"""


class MediaStore:
    """Persistent media storage with metadata tracking."""

    def __init__(self, cfg: dict | None = None):
        self.cfg = cfg or {}
        self._db = sqlite3.connect(str(DB_PATH), check_same_thread=False)
        self._db.row_factory = sqlite3.Row
        self._db.executescript(_SCHEMA)
        self._migrate_add_provider_columns()
        self._db_lock = threading.Lock()
        self._disk_budget_mb = self.cfg.get("media_disk_budget_mb", 5000)
        self._default_ttl = self.cfg.get("media_default_ttl_secs", 0)
        self.tool_event_bus = None

    def _migrate_add_provider_columns(self):
        """Add provider and cost_usd columns if they don't exist (v2 migration)."""
        try:
            cols = {row[1] for row in self._db.execute("PRAGMA table_info(media)").fetchall()}
            if "provider" not in cols:
                self._db.execute("ALTER TABLE media ADD COLUMN provider TEXT DEFAULT 'local'")
                log.info("Media store migration: added 'provider' column")
            if "cost_usd" not in cols:
                self._db.execute("ALTER TABLE media ADD COLUMN cost_usd REAL DEFAULT 0.0")
                log.info("Media store migration: added 'cost_usd' column")
            self._db.commit()
        except Exception as e:
            log.warning("Media store migration error (non-fatal): %s", e)

    def save(self, data: bytes, filename: str, media_type: str = "image",
             source_node: str = "", prompt: str = "", params: dict | None = None,
             metadata: dict | None = None, ttl_secs: int = 0,
             pipeline_id: str = "", tags: list | None = None,
             provider: str = "local", cost_usd: float = 0.0) -> str:
        """Save media to disk and record metadata. Returns absolute file path."""
        media_type = media_type if media_type in MEDIA_SUBDIRS else "other"
        target_dir = MEDIA_SUBDIRS[media_type]

        media_id = uuid.uuid4().hex[:12]
        ts = time.strftime("%Y%m%d_%H%M%S")

        fp = Path(filename)
        name_base, ext = fp.stem, fp.suffix
        if not ext:
            ext = _guess_extension(media_type)
        safe_name = f"{ts}_{media_id}{ext}"
        out_path = target_dir / safe_name

        out_path.write_bytes(data)
        size_bytes = out_path.stat().st_size

        width, height = 0, 0
        if media_type == "image":
            width, height = _get_image_dims(out_path)

        with self._db_lock:
            self._db.execute(
                "INSERT INTO media (id, filename, media_type, path, source_node, prompt, "
                "params, size_bytes, width, height, created_at, ttl_secs, tags, pipeline_id, "
                "metadata, provider, cost_usd) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (media_id, safe_name, media_type, str(out_path), source_node, prompt,
                 json.dumps(params or {}), size_bytes, width, height,
                 time.time(), ttl_secs or self._default_ttl,
                 json.dumps(tags or []), pipeline_id, json.dumps(metadata or {}),
                 provider or "local", cost_usd or 0.0),
            )
            self._db.commit()

        log.info("Media saved: %s (%s, %d bytes, node=%s, provider=%s)",
                 safe_name, media_type, size_bytes, source_node, provider)

        try:
            from ghost_artifacts import auto_register
            auto_register(str(out_path))
        except Exception:
            pass

        if self.tool_event_bus:
            try:
                self.tool_event_bus.emit(
                    "on_media_generated",
                    path=str(out_path),
                    type=media_type,
                    metadata={"source_node": source_node, "prompt": prompt},
                )
            except Exception:
                pass

        return str(out_path)

    def save_file(self, file_path: str, media_type: str = "image",
                  source_node: str = "", prompt: str = "",
                  params: dict | None = None, ttl_secs: int = 0,
                  tags: list | None = None, pipeline_id: str = "",
                  metadata: dict | None = None,
                  provider: str = "local", cost_usd: float = 0.0) -> str:
        """Register an already-existing file in the media store."""
        p = Path(file_path)
        if not p.exists():
            raise FileNotFoundError(f"File not found: {file_path}")

        media_id = uuid.uuid4().hex[:12]
        size_bytes = p.stat().st_size
        width, height = 0, 0
        if media_type == "image":
            width, height = _get_image_dims(p)

        with self._db_lock:
            self._db.execute(
                "INSERT INTO media (id, filename, media_type, path, source_node, prompt, "
                "params, size_bytes, width, height, created_at, ttl_secs, tags, pipeline_id, "
                "metadata, provider, cost_usd) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (media_id, p.name, media_type, str(p), source_node, prompt,
                 json.dumps(params or {}), size_bytes, width, height,
                 time.time(), ttl_secs or self._default_ttl,
                 json.dumps(tags or []), pipeline_id, json.dumps(metadata or {}),
                 provider or "local", cost_usd or 0.0),
            )
            self._db.commit()

        if self.tool_event_bus:
            try:
                self.tool_event_bus.emit(
                    "on_media_generated",
                    path=str(p),
                    type=media_type,
                    metadata={"source_node": source_node, "prompt": prompt},
                )
            except Exception:
                pass

        return str(p)

    def get(self, media_id: str) -> Optional[dict]:
        with self._db_lock:
            row = self._db.execute("SELECT * FROM media WHERE id = ?", (media_id,)).fetchone()
        return dict(row) if row else None

    def list_media(self, media_type: str | None = None, source_node: str | None = None,
                   pipeline_id: str | None = None,
                   limit: int = 50, offset: int = 0) -> list[dict]:
        """Query media with optional filters."""
        clauses = []
        params = []
        if media_type:
            clauses.append("media_type = ?")
            params.append(media_type)
        if source_node:
            clauses.append("source_node = ?")
            params.append(source_node)
        if pipeline_id:
            clauses.append("pipeline_id = ?")
            params.append(pipeline_id)

        where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
        sql = f"SELECT * FROM media {where} ORDER BY created_at DESC LIMIT ? OFFSET ?"
        params.extend([limit, offset])

        with self._db_lock:
            rows = self._db.execute(sql, params).fetchall()
        return [dict(r) for r in rows]

    def count(self, media_type: str | None = None) -> int:
        with self._db_lock:
            if media_type:
                row = self._db.execute(
                    "SELECT COUNT(*) FROM media WHERE media_type = ?", (media_type,)
                ).fetchone()
            else:
                row = self._db.execute("SELECT COUNT(*) FROM media").fetchone()
        return row[0] if row else 0

    def delete(self, media_id: str) -> bool:
        with self._db_lock:
            row = self._db.execute("SELECT path FROM media WHERE id = ?", (media_id,)).fetchone()
            if not row:
                return False
            p = Path(row["path"])
            if p.exists():
                p.unlink(missing_ok=True)
            self._db.execute("DELETE FROM media WHERE id = ?", (media_id,))
            self._db.commit()
        return True

    def cleanup_expired(self) -> int:
        """Delete media that has exceeded its TTL."""
        now = time.time()
        with self._db_lock:
            rows = self._db.execute(
                "SELECT id, path FROM media WHERE ttl_secs > 0 AND (created_at + ttl_secs) < ?",
                (now,),
            ).fetchall()
            for row in rows:
                p = Path(row["path"])
                if p.exists():
                    p.unlink(missing_ok=True)
            self._db.execute(
                "DELETE FROM media WHERE ttl_secs > 0 AND (created_at + ttl_secs) < ?", (now,),
            )
            self._db.commit()
        return len(rows)

    def enforce_disk_budget(self) -> int:
        """Delete oldest media if total disk usage exceeds the budget."""
        if self._disk_budget_mb <= 0:
            return 0

        with self._db_lock:
            budget_bytes = self._disk_budget_mb * 1024 * 1024
            total = self._db.execute(
                "SELECT COALESCE(SUM(size_bytes), 0) FROM media"
            ).fetchone()[0]
            if total <= budget_bytes:
                return 0

            deleted = 0
            rows = self._db.execute(
                "SELECT id, path, size_bytes FROM media ORDER BY created_at ASC"
            ).fetchall()
            for row in rows:
                if total <= budget_bytes:
                    break
                p = Path(row["path"])
                if p.exists():
                    p.unlink(missing_ok=True)
                self._db.execute("DELETE FROM media WHERE id = ?", (row["id"],))
                total -= row["size_bytes"]
                deleted += 1
            self._db.commit()
        return deleted

    def get_stats(self) -> dict:
        """Aggregate stats for the dashboard."""
        with self._db_lock:
            rows = self._db.execute(
                "SELECT media_type, COUNT(*) as cnt, COALESCE(SUM(size_bytes), 0) as total_bytes "
                "FROM media GROUP BY media_type"
            ).fetchall()
            cost_rows = self._db.execute(
                "SELECT provider, COUNT(*) as cnt, COALESCE(SUM(cost_usd), 0) as total_cost "
                "FROM media WHERE cost_usd > 0 GROUP BY provider"
            ).fetchall()
        by_type = {r["media_type"]: {"count": r["cnt"], "size_bytes": r["total_bytes"]} for r in rows}
        cost_by_provider = {
            r["provider"]: {"count": r["cnt"], "total_usd": round(r["total_cost"], 4)}
            for r in cost_rows
        }
        total_count = sum(v["count"] for v in by_type.values())
        total_bytes = sum(v["size_bytes"] for v in by_type.values())
        return {
            "total_count": total_count,
            "total_size_mb": round(total_bytes / (1024 * 1024), 2),
            "budget_mb": self._disk_budget_mb,
            "by_type": by_type,
            "cost_by_provider": cost_by_provider,
            "media_dir": str(MEDIA_DIR),
        }

    def close(self):
        with self._db_lock:
            self._db.close()


def _get_image_dims(path: Path) -> tuple[int, int]:
    try:
        from PIL import Image
        with Image.open(path) as img:
            return img.size
    except Exception:
        return (0, 0)


def _guess_extension(media_type: str) -> str:
    return {
        "image": ".png",
        "audio": ".mp3",
        "video": ".mp4",
        "3d": ".glb",
    }.get(media_type, ".bin")


# ═════════════════════════════════════════════════════════════════════
#  TOOL BUILDER
# ═════════════════════════════════════════════════════════════════════

def build_media_store_tools(media_store: MediaStore):
    """Build tools for browsing and managing the media gallery."""

    def execute_list(media_type: str = "", limit: int = 20, **_kw):
        items = media_store.list_media(
            media_type=media_type or None, limit=min(limit, 100),
        )
        stats = media_store.get_stats()
        return json.dumps({"status": "ok", "stats": stats, "items": items}, default=str)

    def execute_delete(media_id: str = "", **_kw):
        if not media_id:
            return json.dumps({"status": "error", "error": "media_id required"})
        ok = media_store.delete(media_id)
        return json.dumps({"status": "ok" if ok else "error",
                           "message": f"Deleted {media_id}" if ok else "Not found"})

    def execute_cleanup(**_kw):
        expired = media_store.cleanup_expired()
        budget = media_store.enforce_disk_budget()
        return json.dumps({
            "status": "ok",
            "expired_deleted": expired,
            "budget_deleted": budget,
        })

    return [
        {
            "name": "media_list",
            "description": (
                "List generated media (images, audio, video, 3D) from the GhostNodes media gallery. "
                "Filter by type: image, audio, video, 3d."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "media_type": {
                        "type": "string",
                        "description": "Filter by media type",
                        "enum": ["image", "audio", "video", "3d"],
                    },
                    "limit": {"type": "integer", "description": "Max results (default 20)"},
                },
            },
            "execute": execute_list,
        },
        {
            "name": "media_delete",
            "description": "Delete a specific media item from the gallery.",
            "parameters": {
                "type": "object",
                "properties": {
                    "media_id": {"type": "string", "description": "Media ID to delete"},
                },
                "required": ["media_id"],
            },
            "execute": execute_delete,
        },
        {
            "name": "media_cleanup",
            "description": "Clean up expired media and enforce disk budget.",
            "parameters": {"type": "object", "properties": {}},
            "execute": execute_cleanup,
        },
    ]
