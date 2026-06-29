"""
GhostNodes Registry — discover and install nodes from the community catalog.

Similar to ghost_skill_registry.py, this provides a GitHub-based registry
for browsing, searching, and installing community-contributed AI nodes.
"""

import json
import logging
import time
from pathlib import Path
from typing import Optional

log = logging.getLogger("quinely.node_registry")

GHOST_HOME = Path.home() / ".ghost"
REGISTRY_CACHE_DIR = GHOST_HOME / "node_registry_cache"
REGISTRY_CACHE_DIR.mkdir(parents=True, exist_ok=True)

REGISTRY_INDEX_URL = "https://raw.githubusercontent.com/ghost-ai/node-registry/main/index.json"
CACHE_TTL_SECS = 3600  # 1 hour


class NodeRegistry:
    """Browse and search the community node registry."""

    def __init__(self, cfg: dict | None = None):
        self.cfg = cfg or {}
        self._cache_file = REGISTRY_CACHE_DIR / "index.json"
        self._cache: list[dict] = []
        self._last_fetch: float = 0
        self._load_cache()

    def _load_cache(self):
        if self._cache_file.exists():
            try:
                data = json.loads(self._cache_file.read_text(encoding="utf-8"))
                self._cache = data.get("nodes", [])
                self._last_fetch = data.get("fetched_at", 0)
            except Exception:
                pass

    def _save_cache(self):
        self._cache_file.write_text(json.dumps({
            "nodes": self._cache,
            "fetched_at": self._last_fetch,
        }, indent=2), encoding="utf-8")

    def refresh(self) -> dict:
        """Fetch the latest registry index from GitHub."""
        import requests

        try:
            resp = requests.get(REGISTRY_INDEX_URL, timeout=30)
            resp.raise_for_status()
            data = resp.json()

            remote_nodes = data.get("nodes", [])
            self._cache = _merge_with_builtin(remote_nodes)
            self._last_fetch = time.time()
            self._save_cache()

            return {
                "status": "ok",
                "count": len(self._cache),
                "fetched_at": self._last_fetch,
            }
        except Exception as e:
            return {"status": "error", "error": str(e)[:300]}

    def _ensure_fresh(self):
        """Refresh if cache is stale or empty; always merge with builtin catalog."""
        if not self._cache or time.time() - self._last_fetch > CACHE_TTL_SECS:
            self.refresh()
        if not self._cache:
            self._cache = list(BUILTIN_CATALOG)

    def search(self, query: str = "", category: str = "",
               tags: list | None = None, limit: int = 20) -> list[dict]:
        """Search the registry by name, description, category, or tags."""
        self._ensure_fresh()

        results = []
        query_lower = query.lower()
        tags_set = set(t.lower() for t in (tags or []))

        for node in self._cache:
            if category and node.get("category", "") != category:
                continue

            if tags_set:
                node_tags = set(t.lower() for t in node.get("tags", []))
                if not tags_set.intersection(node_tags):
                    continue

            if query_lower:
                searchable = (
                    node.get("name", "") + " " +
                    node.get("description", "") + " " +
                    " ".join(node.get("tags", []))
                ).lower()
                if query_lower not in searchable:
                    continue

            results.append(node)

        return results[:limit]

    def get(self, name: str) -> Optional[dict]:
        """Get a specific node from the registry by name."""
        self._ensure_fresh()
        for node in self._cache:
            if node.get("name") == name:
                return node
        return None

    def get_stats(self) -> dict:
        """Registry statistics."""
        self._ensure_fresh()
        categories = {}
        for node in self._cache:
            cat = node.get("category", "utility")
            categories[cat] = categories.get(cat, 0) + 1

        return {
            "total_nodes": len(self._cache),
            "categories": categories,
            "last_fetched": self._last_fetch,
            "cache_age_secs": int(time.time() - self._last_fetch) if self._last_fetch else None,
        }

    def get_install_url(self, name: str) -> Optional[str]:
        """Get the GitHub URL to install a registry node."""
        node = self.get(name)
        if not node:
            return None
        return node.get("repo_url") or node.get("url")


# ═════════════════════════════════════════════════════════════════════
#  BUILT-IN CATALOG (ships with Ghost when GitHub is unreachable)
# ═════════════════════════════════════════════════════════════════════

BUILTIN_CATALOG = [
    {
        "name": "stable-diffusion",
        "description": "Text-to-image and image-to-image generation using Stable Diffusion",
        "category": "image_generation",
        "author": "ghost-ai",
        "tags": ["sdxl", "flux", "text2img", "img2img"],
        "tier": "official",
        "bundled": True,
    },
    {
        "name": "whisper-stt",
        "description": "Speech-to-text transcription using OpenAI Whisper (99 languages)",
        "category": "audio",
        "author": "ghost-ai",
        "tags": ["stt", "transcription", "multilingual"],
        "tier": "official",
        "bundled": True,
    },
    {
        "name": "bark-tts",
        "description": "Expressive multilingual text-to-speech using Suno Bark",
        "category": "audio",
        "author": "ghost-ai",
        "tags": ["tts", "voice", "multilingual", "expressive"],
        "tier": "official",
        "bundled": True,
    },
    {
        "name": "background-remove",
        "description": "Remove backgrounds from images using REMBG (U2-Net)",
        "category": "image_generation",
        "author": "ghost-ai",
        "tags": ["background-removal", "segmentation"],
        "tier": "official",
        "bundled": True,
    },
    {
        "name": "video-gen",
        "description": "Text-to-video generation using CogVideoX",
        "category": "video",
        "author": "ghost-ai",
        "tags": ["text2video", "animation"],
        "tier": "official",
        "bundled": True,
    },
    {
        "name": "image-upscale",
        "description": "Upscale images 2x-4x using Real-ESRGAN",
        "category": "image_generation",
        "author": "ghost-ai",
        "tags": ["upscale", "super-resolution", "enhance"],
        "tier": "official",
        "bundled": True,
    },
    {
        "name": "florence-vision",
        "description": "Image understanding: captioning, OCR, object detection via Florence-2",
        "category": "vision",
        "author": "ghost-ai",
        "tags": ["captioning", "ocr", "detection"],
        "tier": "official",
        "bundled": True,
    },
    {
        "name": "music-gen",
        "description": "Generate music from text using Meta MusicGen",
        "category": "audio",
        "author": "ghost-ai",
        "tags": ["music", "composition", "soundtrack"],
        "tier": "official",
        "bundled": True,
    },
    {
        "name": "surya-ocr",
        "description": "Multilingual document OCR for 90+ languages using Surya",
        "category": "vision",
        "author": "ghost-ai",
        "tags": ["ocr", "document", "multilingual"],
        "tier": "official",
        "bundled": True,
    },
    {
        "name": "image-to-3d",
        "description": "Single-image 3D reconstruction using TripoSR",
        "category": "3d",
        "author": "ghost-ai",
        "tags": ["3d", "mesh", "reconstruction"],
        "tier": "official",
        "bundled": True,
    },
]


def _merge_with_builtin(registry_nodes: list[dict]) -> list[dict]:
    """Merge remote registry with built-in catalog."""
    seen = {n["name"] for n in registry_nodes}
    merged = list(registry_nodes)
    for node in BUILTIN_CATALOG:
        if node["name"] not in seen:
            merged.append(node)
    return merged


# ═════════════════════════════════════════════════════════════════════
#  TOOL BUILDER
# ═════════════════════════════════════════════════════════════════════

def build_node_registry_tools(node_registry: NodeRegistry, node_manager=None):
    """Build tools for searching and installing from the community registry."""

    def execute_search(query="", category="", **_kw):
        results = node_registry.search(query=query, category=category)
        if not results:
            results = [n for n in BUILTIN_CATALOG
                       if query.lower() in (n["name"] + n["description"]).lower()]
        stats = node_registry.get_stats()
        return json.dumps({
            "status": "ok", "results": results[:20], "stats": stats,
        }, default=str)

    def execute_refresh(**_kw):
        result = node_registry.refresh()
        return json.dumps(result, default=str)

    def execute_install(name="", **_kw):
        if not name:
            return json.dumps({"status": "error", "error": "name is required"})
        if not node_manager:
            return json.dumps({"status": "error", "error": "Node manager not available"})

        url = node_registry.get_install_url(name)
        if not url:
            for n in BUILTIN_CATALOG:
                if n["name"] == name and n.get("bundled"):
                    return json.dumps({
                        "status": "ok",
                        "message": f"{name} is already bundled with Ghost. Enable it via nodes_enable.",
                    })
            return json.dumps({"status": "error", "error": f"Node not found in registry: {name}"})

        result = node_manager.install_from_github(url)
        return json.dumps(result, default=str)

    return [
        {
            "name": "node_registry_search",
            "description": (
                "Search the GhostNodes community registry for AI nodes to install. "
                "Browse image gen, video, audio, vision, LLM, 3D, and data processing nodes."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "query": {"type": "string", "description": "Search query"},
                    "category": {
                        "type": "string",
                        "description": "Filter by category",
                        "enum": ["image_generation", "video", "audio", "vision", "llm", "3d", "data", "utility"],
                    },
                },
            },
            "execute": execute_search,
        },
        {
            "name": "node_registry_refresh",
            "description": "Refresh the node registry cache from GitHub.",
            "parameters": {"type": "object", "properties": {}},
            "execute": execute_refresh,
        },
        {
            "name": "node_registry_install",
            "description": "Install a node from the community registry by name.",
            "parameters": {
                "type": "object",
                "properties": {
                    "name": {"type": "string", "description": "Node name from registry search"},
                },
                "required": ["name"],
            },
            "execute": execute_install,
        },
    ]
