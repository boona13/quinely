"""
Ghost Community Hub — central registry client for discovering, installing, and
publishing nodes and tools.

The registry is a GitHub repo (ghost-ai/community-hub) containing metadata JSON
files. Actual node/tool code lives in developer GitHub repos. This module
fetches the index, supports search, and handles install-from-hub and publish flows.

Registry layout (GitHub repo):
    community-hub/
      nodes/registry.json          — node metadata index
      nodes/submissions/           — one YAML per node (for review)
"""

import json
import logging
import time
from pathlib import Path
from typing import Optional
from urllib.request import Request, urlopen
from urllib.error import HTTPError, URLError

log = logging.getLogger("quinely.community_hub")

GHOST_HOME = Path.home() / ".ghost"
HUB_CACHE_DIR = GHOST_HOME / "hub_cache"
HUB_CACHE_DIR.mkdir(parents=True, exist_ok=True)

CACHE_TTL = 3600  # 1 hour


class CommunityHub:
    """Client for the Ghost Community Hub registry."""

    REGISTRY_REPO = "ghost-ai/community-hub"
    REGISTRY_RAW_URL = "https://raw.githubusercontent.com/ghost-ai/community-hub/main"
    REGISTRY_API_URL = "https://api.github.com/repos/ghost-ai/community-hub"

    def __init__(self, github_token: str = ""):
        self._github_token = github_token

    def _fetch_json(self, url: str, timeout: int = 15) -> dict:
        """Fetch JSON from a URL with optional auth."""
        headers = {"Accept": "application/json"}
        if self._github_token:
            headers["Authorization"] = f"token {self._github_token}"
        req = Request(url, headers=headers)
        try:
            with urlopen(req, timeout=timeout) as resp:
                return json.loads(resp.read().decode("utf-8"))
        except (HTTPError, URLError, json.JSONDecodeError) as e:
            log.warning("Community Hub fetch failed (%s): %s", url, e)
            return {}

    def _get_cached(self, kind: str) -> Optional[list]:
        """Return cached registry data if fresh enough."""
        cache_file = HUB_CACHE_DIR / f"{kind}.json"
        if cache_file.exists():
            try:
                data = json.loads(cache_file.read_text(encoding="utf-8"))
                if time.time() - data.get("_cached_at", 0) < CACHE_TTL:
                    return data.get("items", [])
            except Exception:
                pass
        return None

    def _set_cache(self, kind: str, items: list):
        cache_file = HUB_CACHE_DIR / f"{kind}.json"
        cache_file.write_text(json.dumps({
            "_cached_at": time.time(),
            "items": items,
        }, indent=2), encoding="utf-8")

    # ── Fetch indices ──────────────────────────────────────────────

    def fetch_nodes_index(self, force_refresh: bool = False) -> list[dict]:
        """Fetch the nodes registry index."""
        if not force_refresh:
            cached = self._get_cached("nodes")
            if cached is not None:
                return cached

        url = f"{self.REGISTRY_RAW_URL}/nodes/registry.json"
        data = self._fetch_json(url)
        items = data.get("nodes", [])
        self._set_cache("nodes", items)
        return items

    # ── Search ─────────────────────────────────────────────────────

    def search(self, query: str, category: str = "",
               kind: str = "nodes") -> list[dict]:
        """Search the registry by query string and optional category."""
        items = self.fetch_nodes_index()

        query_lower = query.lower()
        results = []
        for item in items:
            score = 0
            name = item.get("name", "").lower()
            desc = item.get("description", "").lower()
            tags = [t.lower() for t in item.get("tags", [])]

            if query_lower in name:
                score += 10
            if query_lower in desc:
                score += 5
            if any(query_lower in t for t in tags):
                score += 3

            if category and item.get("category", "") != category:
                continue
            if score > 0:
                results.append((score, item))

        results.sort(key=lambda x: -x[0])
        return [item for _, item in results]

    def get_node_details(self, name: str) -> Optional[dict]:
        """Get full details for a specific node."""
        for node in self.fetch_nodes_index():
            if node.get("name") == name:
                return node
        return None

    # ── Install from Hub ───────────────────────────────────────────

    def install_node_from_hub(self, name: str, node_manager) -> dict:
        """Install a node from the community hub."""
        details = self.get_node_details(name)
        if not details:
            return {"status": "error", "error": f"Node '{name}' not found in community hub"}

        repo_url = details.get("repo", "")
        if not repo_url:
            return {"status": "error", "error": f"No repo URL for '{name}'"}

        result = node_manager.install_from_github(repo_url)
        if result.get("status") == "ok":
            self._report_download(name, kind="nodes")
        return result

    def _report_download(self, name: str, kind: str = "nodes"):
        """Best-effort download count increment (non-blocking)."""
        pass

    # ── Publish ────────────────────────────────────────────────────

    def publish_node(self, node_dir: Path) -> dict:
        """Validate and publish a node to the community hub."""
        return self._publish(node_dir, kind="node")

    def _publish(self, source_dir: Path, kind: str = "node") -> dict:
        if not self._github_token:
            return {"status": "error", "error": "GitHub token required for publishing. Set github_token in config."}

        manifest_name = "NODE.yaml"
        entry_name = "node.py"

        manifest_path = source_dir / manifest_name
        if not manifest_path.exists():
            return {"status": "error", "error": f"No {manifest_name} found in {source_dir}"}

        entry_path = source_dir / entry_name
        if not entry_path.exists():
            return {"status": "error", "error": f"No {entry_name} found in {source_dir}"}

        try:
            from ghost_node_manager import NodeManifest
            manifest = NodeManifest.from_yaml(manifest_path)
        except Exception as e:
            return {"status": "error", "error": f"Invalid manifest: {e}"}

        import ast
        try:
            source = entry_path.read_text(encoding="utf-8")
            tree = ast.parse(source, filename=str(entry_path))
            has_register = any(
                isinstance(node, ast.FunctionDef) and node.name == "register"
                for node in ast.walk(tree)
            )
            if not has_register:
                return {"status": "error", "error": f"{entry_name} missing register() function"}
        except SyntaxError as e:
            return {"status": "error", "error": f"Syntax error in {entry_name}: {e}"}

        submission = {
            "name": manifest.name,
            "version": manifest.version,
            "description": manifest.description,
            "author": manifest.author,
            "category": getattr(manifest, "category", "utility"),
        }

        try:
            fork_url = f"https://api.github.com/repos/{self.REGISTRY_REPO}/forks"
            req = Request(fork_url, method="POST", headers={
                "Authorization": f"token {self._github_token}",
                "Accept": "application/vnd.github.v3+json",
            })
            fork_full_name = ""
            try:
                with urlopen(req, timeout=30) as resp:
                    fork_data = json.loads(resp.read().decode("utf-8"))
                fork_full_name = fork_data.get("full_name", "")
            except HTTPError as e:
                log.warning("Failed to fork registry repo: %s", e)

            return {
                "status": "ok" if fork_full_name else "partial",
                "message": (
                    f"Submission prepared for '{manifest.name}'. "
                    f"To complete publishing, submit a PR to {self.REGISTRY_REPO} with "
                    f"the file 'nodes/submissions/{manifest.name}.yaml'."
                ),
                "submission": submission,
                "fork": fork_full_name,
            }

        except Exception as e:
            return {
                "status": "partial",
                "message": f"Validation passed but PR creation failed: {e}. Submit manually.",
                "submission": submission,
            }

    # ── Hub status ─────────────────────────────────────────────────

    def get_hub_status(self) -> dict:
        """Check if the community hub registry is reachable."""
        url = f"{self.REGISTRY_RAW_URL}/nodes/registry.json"
        try:
            req = Request(url, method="HEAD")
            with urlopen(req, timeout=5):
                return {"reachable": True, "repo": self.REGISTRY_REPO}
        except Exception:
            return {"reachable": False, "repo": self.REGISTRY_REPO}


def build_community_hub_tools(hub: CommunityHub, node_manager=None):
    """Build LLM tools for interacting with the Community Hub."""

    def execute_browse(category: str = "", query: str = "", **_kw):
        if query:
            items = hub.search(query, category=category)
        else:
            items = hub.fetch_nodes_index()

        if category:
            items = [i for i in items if i.get("category") == category]

        return json.dumps({
            "status": "ok",
            "count": len(items),
            "items": items[:50],
        }, default=str)

    def execute_install(name: str = "", **_kw):
        if not name:
            return json.dumps({"status": "error", "error": "name required"})
        if node_manager:
            return json.dumps(hub.install_node_from_hub(name, node_manager), default=str)
        return json.dumps({"status": "error", "error": "Node manager not available"})

    return [
        {
            "name": "community_hub_browse",
            "description": (
                "Browse the Ghost Community Hub for nodes. "
                "Search by query, filter by category, or list all."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "category": {
                        "type": "string",
                        "description": "Optional category filter",
                    },
                    "query": {
                        "type": "string",
                        "description": "Search query",
                    },
                },
            },
            "execute": execute_browse,
        },
        {
            "name": "community_hub_install",
            "description": "Install a node from the Community Hub by name.",
            "parameters": {
                "type": "object",
                "properties": {
                    "name": {
                        "type": "string",
                        "description": "Name of the node to install",
                    },
                },
                "required": ["name"],
            },
            "execute": execute_install,
        },
    ]
