"""Public Skill Registry (GhostHub) — discover and install community skills from GitHub.

Fetches skill manifests from a GitHub-based registry (github.com/ghost-ai/skills-registry),
caches them locally with TTL, and provides search/install functionality.
"""

from __future__ import annotations

import json
import logging
import os
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional

import requests

from ghost_skills import SKILLS_USER_DIR

log = logging.getLogger("quinely.skill_registry")

# Default registry configuration
DEFAULT_REGISTRY_REPO = "boona13/skills-registry"
DEFAULT_REGISTRY_BRANCH = "main"
DEFAULT_CACHE_TTL_SECONDS = 3600  # 1 hour
DEFAULT_REGISTRY_INDEX_URL = "https://raw.githubusercontent.com/{repo}/{branch}/index.json"
DEFAULT_SKILL_RAW_URL = "https://raw.githubusercontent.com/{repo}/{branch}/skills/{skill_name}/SKILL.md"

GHOST_HOME = Path.home() / ".ghost"
REGISTRY_CACHE_DIR = GHOST_HOME / "skill_registry"
REGISTRY_CACHE_FILE = REGISTRY_CACHE_DIR / "cache.json"
REGISTRY_META_FILE = REGISTRY_CACHE_DIR / "meta.json"

_lock = threading.Lock()


@dataclass
class RegistrySkill:
    """A skill entry from the public registry."""
    name: str
    description: str
    author: str
    version: str
    tags: List[str] = field(default_factory=list)
    triggers: List[str] = field(default_factory=list)
    tools: List[str] = field(default_factory=list)
    requires: Dict[str, Any] = field(default_factory=dict)
    installs: int = 0
    rating: float = 0.0
    updated_at: str = ""

    def to_dict(self) -> Dict[str, Any]:
        return {
            "name": self.name,
            "description": self.description,
            "author": self.author,
            "version": self.version,
            "tags": self.tags,
            "triggers": self.triggers,
            "tools": self.tools,
            "requires": self.requires,
            "installs": self.installs,
            "rating": self.rating,
            "updated_at": self.updated_at,
        }

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "RegistrySkill":
        return cls(
            name=data.get("name", ""),
            description=data.get("description", ""),
            author=data.get("author", ""),
            version=data.get("version", ""),
            tags=data.get("tags", []),
            triggers=data.get("triggers", []),
            tools=data.get("tools", []),
            requires=data.get("requires", {}),
            installs=data.get("installs", 0),
            rating=data.get("rating", 0.0),
            updated_at=data.get("updated_at", ""),
        )


class SkillRegistryClient:
    """Client for the public skill registry."""

    def __init__(
        self,
        repo: str = DEFAULT_REGISTRY_REPO,
        branch: str = DEFAULT_REGISTRY_BRANCH,
        cache_ttl: int = DEFAULT_CACHE_TTL_SECONDS,
    ):
        self.repo = repo
        self.branch = branch
        self.cache_ttl = cache_ttl
        self._session = requests.Session()
        self._session.headers.update({
            "Accept": "application/json",
            "User-Agent": "Ghost-Skill-Registry/1.0",
        })
        REGISTRY_CACHE_DIR.mkdir(parents=True, exist_ok=True)

    def _get_index_url(self) -> str:
        return DEFAULT_REGISTRY_INDEX_URL.format(repo=self.repo, branch=self.branch)

    def _get_skill_raw_url(self, skill_name: str) -> str:
        return DEFAULT_SKILL_RAW_URL.format(
            repo=self.repo, branch=self.branch, skill_name=skill_name
        )

    def _atomic_write_json(self, path: Path, data: Any) -> None:
        """Thread-safe atomic JSON write using tempfile + os.replace."""
        import tempfile
        fd, tmp = tempfile.mkstemp(dir=path.parent, suffix=".tmp")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                json.dump(data, f, indent=2, default=str)
            os.replace(tmp, str(path))
        except BaseException:
            if Path(tmp).exists():
                os.unlink(tmp)
            raise

    def _load_cache(self) -> Optional[Dict[str, Any]]:
        """Load cached registry data if valid."""
        with _lock:
            try:
                if not REGISTRY_CACHE_FILE.exists():
                    return None

                # Check metadata for TTL
                if REGISTRY_META_FILE.exists():
                    meta = json.loads(REGISTRY_META_FILE.read_text(encoding="utf-8"))
                    cached_at = meta.get("cached_at", 0)
                    if time.time() - cached_at > self.cache_ttl:
                        return None

                data = json.loads(REGISTRY_CACHE_FILE.read_text(encoding="utf-8"))
                return data
            except (json.JSONDecodeError, OSError) as exc:
                log.warning("Failed to load registry cache: %s", exc)
                return None

    def _save_cache(self, data: Dict[str, Any]) -> None:
        """Save registry data to cache."""
        with _lock:
            try:
                self._atomic_write_json(REGISTRY_CACHE_FILE, data)
                self._atomic_write_json(REGISTRY_META_FILE, {
                    "cached_at": time.time(),
                    "repo": self.repo,
                    "branch": self.branch,
                })
            except OSError as exc:
                log.error("Failed to save registry cache: %s", exc)
                raise

    def fetch_index(self, force: bool = False) -> Dict[str, Any]:
        """Fetch the registry index from GitHub."""
        if not force:
            cached = self._load_cache()
            if cached is not None:
                log.debug("Using cached registry index")
                return cached

        url = self._get_index_url()
        log.info("Fetching skill registry from %s", url)

        try:
            resp = self._session.get(url, timeout=30)
            resp.raise_for_status()
            data = resp.json()

            # Validate basic structure
            if not isinstance(data, dict) or "skills" not in data:
                raise ValueError("Invalid registry index format: missing 'skills' key")

            self._save_cache(data)
            log.info("Fetched %d skills from registry", len(data.get("skills", [])))
            return data

        except requests.RequestException as exc:
            log.warning("Registry fetch failed (this is normal if the registry repo doesn't exist yet): %s", exc)
            cached = self._load_cache()
            if cached is not None:
                log.debug("Using stale cache due to fetch failure")
                return cached
            return {"skills": [], "_offline": True}
        except (json.JSONDecodeError, ValueError) as exc:
            log.error("Failed to parse registry index: %s", exc)
            raise SkillRegistryError(f"Invalid registry format: {exc}")


    def list_skills(self, force_refresh: bool = False) -> List[RegistrySkill]:
        """List all available skills from the registry."""
        data = self.fetch_index(force=force_refresh)
        skills_data = data.get("skills", [])
        return [RegistrySkill.from_dict(s) for s in skills_data]

    def search_skills(
        self,
        query: str = "",
        tags: Optional[List[str]] = None,
        author: str = "",
        force_refresh: bool = False,
    ) -> List[RegistrySkill]:
        """Search skills by query string, tags, or author."""
        skills = self.list_skills(force_refresh=force_refresh)

        if not query and not tags and not author:
            return skills

        query_lower = query.lower() if query else ""
        tag_set = set(t.lower() for t in (tags or []))
        author_lower = author.lower() if author else ""

        results = []
        for skill in skills:
            # Text search across name, description, triggers
            text_match = True
            if query_lower:
                text_fields = [
                    skill.name.lower(),
                    skill.description.lower(),
                    " ".join(t.lower() for t in skill.triggers),
                ]
                text_match = query_lower in " ".join(text_fields)

            # Tag filter
            tag_match = True
            if tag_set:
                skill_tags = set(t.lower() for t in skill.tags)
                tag_match = bool(tag_set & skill_tags)

            # Author filter
            author_match = True
            if author_lower:
                author_match = author_lower in skill.author.lower()

            if text_match and tag_match and author_match:
                results.append(skill)

        # Sort by rating (desc), then installs (desc)
        results.sort(key=lambda s: (-s.rating, -s.installs))
        return results

    def get_skill(self, name: str, force_refresh: bool = False) -> Optional[RegistrySkill]:
        """Get a specific skill by name."""
        skills = self.list_skills(force_refresh=force_refresh)
        for skill in skills:
            if skill.name.lower() == name.lower():
                return skill
        return None

    def fetch_skill_content(self, skill_name: str) -> str:
        """Fetch the raw SKILL.md content for a skill."""
        url = self._get_skill_raw_url(skill_name)
        log.debug("Fetching skill content from %s", url)

        try:
            resp = self._session.get(url, timeout=30)
            resp.raise_for_status()
            return resp.text
        except requests.RequestException as exc:
            log.error("Failed to fetch skill content for %s: %s", skill_name, exc)
            raise SkillRegistryError(f"Failed to fetch skill '{skill_name}': {exc}")


class SkillRegistryError(Exception):
    """Error from the skill registry."""
    pass


class SkillRegistryManager:
    """High-level manager for registry operations with local skill integration."""

    def __init__(self, config_loader=None, config_saver=None):
        self.config_loader = config_loader
        self.config_saver = config_saver
        self.client = SkillRegistryClient()
        self.user_dir = SKILLS_USER_DIR
        self.user_dir.mkdir(parents=True, exist_ok=True)

    def enabled(self) -> bool:
        """Check if skill registry is enabled."""
        if self.config_loader:
            cfg = self.config_loader() or {}
            return bool(cfg.get("enable_skill_registry", True))
        return True

    def get_installed_skills(self) -> List[str]:
        """Get list of locally installed skill names."""
        installed = []
        if not self.user_dir.exists():
            return installed

        for item in self.user_dir.iterdir():
            if item.is_dir() and (item / "SKILL.md").exists():
                installed.append(item.name)
        return installed

    def is_installed(self, skill_name: str) -> bool:
        """Check if a skill is already installed."""
        skill_dir = self.user_dir / skill_name
        return skill_dir.exists() and (skill_dir / "SKILL.md").exists()

    def install_skill(
        self,
        skill_name: str,
        overwrite: bool = False,
    ) -> Dict[str, Any]:
        """Install a skill from the registry."""
        if not self.enabled():
            return {"ok": False, "error": "Skill registry is disabled"}

        # Check if already installed
        if self.is_installed(skill_name) and not overwrite:
            return {
                "ok": False,
                "error": f"Skill '{skill_name}' is already installed. Use overwrite=True to reinstall.",
            }

        # Fetch skill metadata
        skill = self.client.get_skill(skill_name)
        if not skill:
            return {"ok": False, "error": f"Skill '{skill_name}' not found in registry"}

        # Fetch skill content
        try:
            content = self.client.fetch_skill_content(skill_name)
        except SkillRegistryError as exc:
            return {"ok": False, "error": str(exc)}

        # Validate content + run security scan
        from ghost_skill_manager import SkillManager
        manager = SkillManager(self.config_loader, self.config_saver)
        validation = manager.validate_skill_text(content, run_security_scan=True)

        if not validation.get("ok", False):
            security = validation.get("security", {})
            if security.get("blocked"):
                findings = security.get("findings", [])
                top_findings = [f.get("message", "") for f in findings[:3]]
                log.warning("Skill '%s' BLOCKED by security scan: %s", skill_name, top_findings)
                return {
                    "ok": False,
                    "error": f"Skill '{skill_name}' blocked by security scan: contains potentially dangerous content",
                    "security": security,
                    "validation": validation,
                }
            return {
                "ok": False,
                "error": f"Skill validation failed: {validation.get('status', 'unknown error')}",
                "validation": validation,
            }

        # Warn but allow caution/dangerous (user already consented via UI)
        security = validation.get("security", {})
        verdict = security.get("verdict", "safe")

        # Install the skill
        try:
            result = manager.install_local(skill_name, content, overwrite=overwrite)
            if result.get("ok"):
                log.info("Installed skill '%s' v%s by %s (security: %s)", skill_name, skill.version, skill.author, verdict)
                return {
                    "ok": True,
                    "skill": skill.to_dict(),
                    "path": result.get("path"),
                    "security": security,
                    "message": f"Installed {skill_name} v{skill.version} by {skill.author}",
                }
            else:
                return result
        except Exception as exc:
            log.error("Failed to install skill %s: %s", skill_name, exc)
            return {"ok": False, "error": f"Installation failed: {exc}"}


def build_skill_registry_tools(cfg=None):
    """Build tools for the public skill registry (GhostHub)."""
    cfg = cfg or {}
    if not cfg.get("enable_skill_registry", True):
        return []

    client = SkillRegistryClient()
    from ghost_skill_manager import SkillManager

    def _load_save_config():
        from ghost import load_config, save_config
        return load_config, save_config

    def search_registry_skills(query="", tags=None, author="", **kwargs):
        """Search the public skill registry (GhostHub) for community skills."""
        try:
            results = client.search_skills(
                query=query,
                tags=tags or [],
                author=author,
            )
            return {
                "ok": True,
                "count": len(results),
                "skills": [s.to_dict() for s in results],
            }
        except Exception as exc:
            log.warning("Registry search failed: %s", exc)
            return {"ok": False, "error": str(exc)}

    def get_registry_skill(name, **kwargs):
        """Get detailed info about a skill from the public registry."""
        try:
            skill = client.get_skill(name)
            if not skill:
                return {"ok": False, "error": f"Skill '{name}' not found in registry"}
            return {"ok": True, "skill": skill.to_dict()}
        except Exception as exc:
            log.warning("Registry get_skill failed: %s", exc)
            return {"ok": False, "error": str(exc)}

    def install_registry_skill(name, overwrite=False, **kwargs):
        """Install a skill from the public registry (GhostHub)."""
        try:
            load_config, save_config = _load_save_config()
            manager = SkillManager(load_config, save_config)
            result = client.install_skill(name, manager, overwrite=overwrite)
            return result
        except Exception as exc:
            log.error("Failed to install registry skill %s: %s", name, exc)
            return {"ok": False, "error": str(exc)}

    def refresh_registry_cache(**kwargs):
        """Force refresh the local skill registry cache from GitHub."""
        try:
            client.clear_cache()
            # Trigger a fetch to repopulate
            skills = client.list_skills(force_refresh=True)
            return {
                "ok": True,
                "message": f"Registry cache refreshed. {len(skills)} skills available.",
                "count": len(skills),
            }
        except Exception as exc:
            log.error("Failed to refresh registry cache: %s", exc)
            return {"ok": False, "error": str(exc)}

    def get_registry_stats(**kwargs):
        """Get statistics about the public skill registry."""
        try:
            skills = client.list_skills()
            tags = set()
            authors = set()
            for s in skills:
                tags.update(s.tags or [])
                if s.author:
                    authors.add(s.author)
            return {
                "ok": True,
                "total_skills": len(skills),
                "unique_tags": len(tags),
                "unique_authors": len(authors),
                "tags": sorted(tags)[:50],  # Limit for brevity
                "source": client.repo,
            }
        except Exception as exc:
            log.warning("Registry stats failed: %s", exc)
            return {"ok": False, "error": str(exc)}

    return [
        {
            "name": "search_registry_skills",
            "description": "Search the public skill registry (GhostHub) for community skills by keyword, tags, or author",
            "parameters": {
                "type": "object",
                "properties": {
                    "query": {"type": "string", "description": "Search query string", "default": ""},
                    "tags": {"type": "array", "items": {"type": "string"}, "description": "Filter by tags", "default": []},
                    "author": {"type": "string", "description": "Filter by author username", "default": ""},
                },
            },
            "execute": search_registry_skills,
        },
        {
            "name": "get_registry_skill",
            "description": "Get detailed information about a specific skill from the public registry",
            "parameters": {
                "type": "object",
                "properties": {
                    "name": {"type": "string", "description": "Name of the skill to look up"},
                },
                "required": ["name"],
            },
            "execute": get_registry_skill,
        },
        {
            "name": "install_registry_skill",
            "description": "Install a skill from the public registry (GhostHub) into your local skills directory",
            "parameters": {
                "type": "object",
                "properties": {
                    "name": {"type": "string", "description": "Name of the skill to install"},
                    "overwrite": {"type": "boolean", "description": "Overwrite if skill already exists", "default": False},
                },
                "required": ["name"],
            },
            "execute": install_registry_skill,
        },
        {
            "name": "refresh_registry_cache",
            "description": "Force refresh the local skill registry cache from GitHub",
            "parameters": {"type": "object", "properties": {}},
            "execute": refresh_registry_cache,
        },
        {
            "name": "get_registry_stats",
            "description": "Get statistics about the public skill registry (GhostHub)",
            "parameters": {"type": "object", "properties": {}},
            "execute": get_registry_stats,
        },
    ]
