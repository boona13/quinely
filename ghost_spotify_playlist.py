"""Spotify playlist importer (no OAuth).

This module extracts playlist + track metadata from the public Spotify embed page
by parsing the __NEXT_DATA__ JSON blob.

Why: Spotify Web API generally requires OAuth tokens even for public playlists.
This implementation avoids auth by scraping the embed page.
"""

from __future__ import annotations

import json
import logging
import os
import re
import tempfile
import threading
from pathlib import Path
from typing import Any, Optional

import requests

log = logging.getLogger("quinely.spotify")

_GHOST_HOME = Path.home() / ".ghost"
_SPOTIFY_DIR = _GHOST_HOME / "artifacts" / "spotify"
_file_lock = threading.Lock()


def _atomic_write_json(path: Path, data: Any):
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=str(path.parent), suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2, ensure_ascii=False, default=str)
        os.replace(tmp, str(path))
    except BaseException:
        try:
            if Path(tmp).exists():
                os.unlink(tmp)
        except OSError:
            pass
        raise


def _normalize_playlist_id(playlist: str) -> str:
    if not isinstance(playlist, str) or not playlist.strip():
        raise ValueError("playlist must be a non-empty string")
    s = playlist.strip()

    m = re.search(r"spotify:playlist:([A-Za-z0-9]+)", s)
    if m:
        return m.group(1)

    m = re.search(r"open\.spotify\.com/(?:embed/)?playlist/([A-Za-z0-9]+)", s)
    if m:
        return m.group(1)

    # Accept raw ID
    if re.fullmatch(r"[A-Za-z0-9]{10,}", s):
        return s

    raise ValueError("Unrecognized Spotify playlist identifier")


def _fetch_text(url: str, timeout: float = 20.0, max_bytes: int = 2_000_000) -> str:
    headers = {
        "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/123 Safari/537.36",
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    }
    try:
        with requests.get(url, headers=headers, timeout=timeout, stream=True) as r:
            r.raise_for_status()
            chunks: list[bytes] = []
            total = 0
            for chunk in r.iter_content(chunk_size=65536):
                if not chunk:
                    continue
                total += len(chunk)
                if total > max_bytes:
                    raise ValueError(f"Response too large (> {max_bytes} bytes)")
                chunks.append(chunk)
            return b"".join(chunks).decode("utf-8", errors="replace")
    except requests.RequestException as exc:
        raise RuntimeError(f"Failed to fetch Spotify embed page: {exc}") from exc


def _extract_next_data(html: str) -> dict:
    if not html:
        raise ValueError("Empty HTML")
    # __NEXT_DATA__ script tag JSON
    m = re.search(r"<script[^>]+id=\"__NEXT_DATA__\"[^>]*>(.*?)</script>", html, re.DOTALL)
    if not m:
        raise ValueError("__NEXT_DATA__ not found in embed HTML")
    raw = m.group(1).strip()
    try:
        return json.loads(raw)
    except json.JSONDecodeError as exc:
        raise ValueError(f"Failed to parse __NEXT_DATA__ JSON: {exc}") from exc


def _walk(obj: Any):
    if isinstance(obj, dict):
        yield obj
        for v in obj.values():
            yield from _walk(v)
    elif isinstance(obj, list):
        for it in obj:
            yield from _walk(it)


def _find_playlist_entity(next_data: dict) -> Optional[dict]:
    # Spotify embed shape is not stable; search for an entity dict with type 'playlist'.
    for d in _walk(next_data):
        ent = d.get("entity") if isinstance(d, dict) else None
        if isinstance(ent, dict) and ent.get("type") == "playlist":
            return ent
    return None


def _extract_tracks(playlist_entity: dict, limit: int = 500) -> list[dict]:
    tracks_out: list[dict] = []
    if not isinstance(playlist_entity, dict):
        return tracks_out

    # Common shape: entity['trackList'] -> list of items w/ entityType == 'track'
    candidates = []
    for key in ("trackList", "tracks", "items"):
        val = playlist_entity.get(key)
        if isinstance(val, list):
            candidates = val
            break
        if isinstance(val, dict):
            inner = val.get("items")
            if isinstance(inner, list):
                candidates = inner
                break

    for item in candidates[: max(0, int(limit))]:
        if not isinstance(item, dict):
            continue
        if item.get("entityType") not in (None, "track"):
            continue
        title = item.get("title") or item.get("name") or ""
        artist = item.get("subtitle") or item.get("artist") or ""
        uri = item.get("uri") or ""
        duration_ms = item.get("duration") if isinstance(item.get("duration"), int) else None
        preview_url = None
        ap = item.get("audioPreview")
        if isinstance(ap, dict):
            preview_url = ap.get("url")
        tracks_out.append({
            "title": title,
            "artist": artist,
            "uri": uri,
            "duration_ms": duration_ms,
            "preview_url": preview_url,
        })
        if len(tracks_out) >= limit:
            break

    return tracks_out


def spotify_playlist_import(playlist: str, save_path: str = "", limit: int = 200, **kwargs) -> dict:
    """Import a Spotify playlist (public) without OAuth.

    Returns a dict with keys: id, title, description, owner, tracks.
    If save_path is provided, writes JSON to that path (relative to ~/.ghost/artifacts/spotify).
    """
    pid = _normalize_playlist_id(playlist)
    limit_i = int(limit) if isinstance(limit, (int, float, str)) else 200
    if limit_i < 1:
        limit_i = 1
    if limit_i > 500:
        limit_i = 500

    url = f"https://open.spotify.com/embed/playlist/{pid}"
    html = _fetch_text(url)
    next_data = _extract_next_data(html)
    ent = _find_playlist_entity(next_data)
    if not ent:
        # fallback: some builds keep playlist entity nested differently; at least expose next_data keys.
        raise ValueError("Could not locate playlist entity in __NEXT_DATA__")

    result = {
        "id": pid,
        "title": ent.get("name") or ent.get("title") or "",
        "description": ent.get("description") or "",
        "owner": (ent.get("owner") or {}).get("name") if isinstance(ent.get("owner"), dict) else "",
        "tracks": _extract_tracks(ent, limit=limit_i),
        "source_url": url,
    }

    if save_path:
        if not isinstance(save_path, str):
            raise ValueError("save_path must be a string")
        safe = re.sub(r"[^A-Za-z0-9._-]+", "_", save_path.strip())
        if not safe:
            raise ValueError("save_path cannot be empty")
        out_path = _SPOTIFY_DIR / safe
        if out_path.suffix.lower() != ".json":
            out_path = out_path.with_suffix(out_path.suffix + ".json") if out_path.suffix else out_path.with_suffix(".json")
        with _file_lock:
            _atomic_write_json(out_path, result)
        result["saved_to"] = str(out_path)

    return result


def build_spotify_playlist_tools(cfg: dict) -> list[dict]:
    if not cfg.get("enable_spotify_playlist_import", True):
        return []

    def _tool(playlist_url: str, save_as: str = "", limit: int = 200, **kwargs):
        return json.dumps(
            spotify_playlist_import(playlist_url, save_path=save_as, limit=limit, **kwargs),
            ensure_ascii=False,
            indent=2,
        )

    return [
        {
            "name": "spotify_playlist_import",
            "description": "Import a public Spotify playlist (no OAuth) by parsing the embed page and return track metadata as JSON.",
            "parameters": {
                "type": "object",
                "properties": {
                    "playlist_url": {"type": "string", "description": "Spotify playlist URL, URI, or raw playlist ID."},
                    "save_as": {"type": "string", "description": "Optional filename to save JSON under ~/.ghost/artifacts/spotify/ (e.g. my_playlist.json).", "default": ""},
                    "limit": {"type": "integer", "description": "Maximum number of tracks to return (1-500).", "default": 200},
                },
                "required": ["playlist_url"],
            },
            "execute": _tool,
        }
    ]
