"""
Directory & Resolver Adapters

  - Self identity: get_self() returns the bot's own identity
  - Contacts: list_peers(), list_groups(), list_group_members()
  - Resolution: resolve_target(name_or_id) -> normalized target
  - Caching: contacts stored in ~/.ghost/channel_contacts.json
  - Both live and cached variants for expensive API calls

Usage:
    if isinstance(provider, DirectoryMixin):
        me = provider.get_self()
        peers = provider.list_peers()
        groups = provider.list_groups()
"""

import json
import time
import logging
import threading
from dataclasses import dataclass, field, asdict
from pathlib import Path
from typing import Optional, Dict, Any, List

log = logging.getLogger("quinely.channels.directory")

GHOST_HOME = Path.home() / ".ghost"
CONTACTS_CACHE_FILE = GHOST_HOME / "channel_contacts.json"
CACHE_TTL = 300  # 5 minutes

_cache_lock = threading.Lock()


@dataclass
class DirectoryEntry:
    """A contact/peer/group entry."""
    id: str
    name: str
    kind: str = "user"
    channel_id: str = ""
    avatar_url: str = ""
    is_bot: bool = False
    metadata: Dict[str, Any] = field(default_factory=dict)


@dataclass
class ResolveResult:
    """Result of target resolution."""
    input: str
    resolved: bool
    id: str = ""
    name: str = ""
    note: str = ""


def _load_cache() -> Dict[str, Any]:
    with _cache_lock:
        if CONTACTS_CACHE_FILE.exists():
            try:
                return json.loads(CONTACTS_CACHE_FILE.read_text(encoding="utf-8"))
            except Exception:
                pass
    return {}


def _save_cache(data: Dict[str, Any]):
    with _cache_lock:
        GHOST_HOME.mkdir(parents=True, exist_ok=True)
        CONTACTS_CACHE_FILE.write_text(json.dumps(data, indent=2, default=str), encoding="utf-8")


class DirectoryMixin:
    """Mixin for ChannelProvider subclasses with directory/contact support.

    Providers override live methods; caching is handled automatically.
    """

    def get_self(self) -> Optional[DirectoryEntry]:
        """Return the bot's own identity on this channel."""
        return None

    def list_peers(self, query: str = "", limit: int = 50) -> List[DirectoryEntry]:
        """List contacts/peers. Override for live results."""
        return []

    def list_peers_cached(self, query: str = "", limit: int = 50) -> List[DirectoryEntry]:
        """List peers from cache, falling back to live."""
        channel_id = getattr(getattr(self, "meta", None), "id", "unknown")
        cache = _load_cache()
        key = f"{channel_id}:peers"
        cached = cache.get(key)
        if cached and time.time() - cached.get("_ts", 0) < CACHE_TTL:
            entries = [DirectoryEntry(**e) for e in cached.get("entries", [])
                       if isinstance(e, dict)]
            if query:
                q = query.lower()
                entries = [e for e in entries
                           if q in e.name.lower() or q in e.id.lower()]
            return entries[:limit]

        live = self.list_peers(query=query, limit=limit)
        try:
            cache[key] = {
                "_ts": time.time(),
                "entries": [asdict(e) for e in live],
            }
            _save_cache(cache)
        except Exception:
            pass
        return live

    def list_groups(self, query: str = "", limit: int = 50) -> List[DirectoryEntry]:
        """List groups/channels. Override for live results."""
        return []

    def list_groups_cached(self, query: str = "", limit: int = 50) -> List[DirectoryEntry]:
        """List groups from cache, falling back to live."""
        channel_id = getattr(getattr(self, "meta", None), "id", "unknown")
        cache = _load_cache()
        key = f"{channel_id}:groups"
        cached = cache.get(key)
        if cached and time.time() - cached.get("_ts", 0) < CACHE_TTL:
            entries = [DirectoryEntry(**e) for e in cached.get("entries", [])
                       if isinstance(e, dict)]
            if query:
                q = query.lower()
                entries = [e for e in entries
                           if q in e.name.lower() or q in e.id.lower()]
            return entries[:limit]

        live = self.list_groups(query=query, limit=limit)
        try:
            cache[key] = {
                "_ts": time.time(),
                "entries": [asdict(e) for e in live],
            }
            _save_cache(cache)
        except Exception:
            pass
        return live

    def list_group_members(self, group_id: str,
                            limit: int = 100) -> List[DirectoryEntry]:
        """List members of a specific group. Override per channel."""
        return []

    def invalidate_cache(self):
        """Clear cached contacts for this channel."""
        channel_id = getattr(getattr(self, "meta", None), "id", "unknown")
        cache = _load_cache()
        keys_to_remove = [k for k in cache if k.startswith(f"{channel_id}:")]
        for k in keys_to_remove:
            del cache[k]
        _save_cache(cache)


class ResolverMixin:
    """Mixin for target resolution — name/mention to ID mapping."""

    def resolve_targets(self, inputs: List[str],
                        kind: str = "user") -> List[ResolveResult]:
        """Resolve user/group names or IDs to normalized targets.

        Override per channel for API-backed resolution.
        """
        return [ResolveResult(input=inp, resolved=False, note="not implemented")
                for inp in inputs]

    def normalize_target(self, target: str) -> str:
        """Normalize a target identifier. Override per channel."""
        return target.strip()

    def format_target_display(self, target_id: str,
                               name: str = "") -> str:
        """Format a target for display. Override per channel."""
        if name:
            return f"{name} ({target_id})"
        return target_id


def build_directory_tools(registry) -> list:
    """Build LLM tools for directory/contact operations."""
    tools = []

    def channel_list_contacts(channel: str, query: str = "",
                               kind: str = "user", limit: int = 20) -> str:
        prov = registry.get(channel)
        if not prov:
            return f"Unknown channel: {channel}"
        if not isinstance(prov, DirectoryMixin):
            return f"{channel} does not support directory listing"
        if kind == "group":
            entries = prov.list_groups_cached(query=query, limit=limit)
        else:
            entries = prov.list_peers_cached(query=query, limit=limit)
        if not entries:
            return f"No {kind}s found" + (f" matching '{query}'" if query else "")
        lines = [f"{kind.title()}s on {channel}:"]
        for e in entries:
            bot_tag = " [bot]" if e.is_bot else ""
            lines.append(f"  {e.name} (id: {e.id}){bot_tag}")
        return "\n".join(lines)

    tools.append({
        "name": "channel_list_contacts",
        "description": (
            "List contacts (users or groups) on a messaging channel. "
            "Useful for finding who to message."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "channel": {"type": "string",
                            "description": "Channel ID (e.g. 'slack', 'telegram')"},
                "query": {"type": "string",
                          "description": "Search query to filter results",
                          "default": ""},
                "kind": {"type": "string",
                         "enum": ["user", "group"],
                         "description": "Type of contacts to list",
                         "default": "user"},
                "limit": {"type": "integer",
                          "description": "Max results",
                          "default": 20},
            },
            "required": ["channel"],
        },
        "execute": channel_list_contacts,
    })

    def channel_resolve(channel: str, targets: str,
                        kind: str = "user") -> str:
        prov = registry.get(channel)
        if not prov:
            return f"Unknown channel: {channel}"
        if not isinstance(prov, ResolverMixin):
            return f"{channel} does not support target resolution"
        inputs = [t.strip() for t in targets.split(",") if t.strip()]
        results = prov.resolve_targets(inputs, kind=kind)
        lines = [f"Resolution results for {channel}:"]
        for r in results:
            if r.resolved:
                lines.append(f"  '{r.input}' -> {r.name} (id: {r.id})")
            else:
                lines.append(f"  '{r.input}' -> NOT FOUND" +
                             (f" ({r.note})" if r.note else ""))
        return "\n".join(lines)

    tools.append({
        "name": "channel_resolve",
        "description": "Resolve user or group names to IDs on a messaging channel.",
        "parameters": {
            "type": "object",
            "properties": {
                "channel": {"type": "string",
                            "description": "Channel ID"},
                "targets": {"type": "string",
                            "description": "Comma-separated names/IDs to resolve"},
                "kind": {"type": "string",
                         "enum": ["user", "group"],
                         "description": "Resolve as user or group",
                         "default": "user"},
            },
            "required": ["channel", "targets"],
        },
        "execute": channel_resolve,
    })

    return tools
