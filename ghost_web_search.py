"""
GHOST Multi-Provider Web Search

Providers (in fallback order):
  1. Perplexity via OpenRouter (uses existing OPENROUTER_API_KEY)
  2. Perplexity direct (optional, needs PERPLEXITY_API_KEY)
  3. Grok/xAI (uses existing Grok API key from integrations)
  4. OpenAI (web_search tool via Responses API, uses OPENAI_API_KEY)
  5. Brave Search (optional, needs BRAVE_API_KEY)
  6. Gemini with Google Search grounding (uses GOOGLE_AI_API_KEY)

If the primary provider fails, automatically tries the next one.
Keys are resolved from: env vars → auth_profiles → legacy config.
Results are cached in-memory for 15 minutes to avoid repeat queries.

Storage: in-memory only (no disk persistence for search cache)
"""

import hashlib
import json
import os
import threading
import time
from pathlib import Path
from typing import Optional

import requests

GHOST_HOME = Path.home() / ".ghost"

# ═════════════════════════════════════════════════════════════════════
#  PROVIDER CONFIGURATIONS
# ═════════════════════════════════════════════════════════════════════

OPENROUTER_BASE = "https://openrouter.ai/api/v1"
PERPLEXITY_BASE = "https://api.perplexity.ai"
XAI_BASE = "https://api.x.ai/v1"
OPENAI_RESPONSES_BASE = "https://api.openai.com/v1/responses"
BRAVE_BASE = "https://api.search.brave.com/res/v1/web/search"
GEMINI_BASE = "https://generativelanguage.googleapis.com/v1beta"

DEFAULT_PERPLEXITY_MODEL = "perplexity/sonar-pro"
DEFAULT_PERPLEXITY_DIRECT_MODEL = "sonar-pro"
DEFAULT_GROK_MODEL = "grok-3-fast"
DEFAULT_OPENAI_SEARCH_MODEL = "gpt-4.1-mini"
DEFAULT_GEMINI_MODEL = "gemini-2.5-flash"

_resolved_models: dict[str, str] = {}


def _get_model(key: str, default: str) -> str:
    """Get model ID from resolved config or fall back to module default."""
    return _resolved_models.get(key, default)

CACHE_TTL_S = 900  # 15 minutes
CACHE_MAX_ENTRIES = 100
REQUEST_TIMEOUT_S = 30

FRESHNESS_MAP = {
    "day": "pd", "pd": "pd",
    "week": "pw", "pw": "pw",
    "month": "pm", "pm": "pm",
    "year": "py", "py": "py",
}

# ═════════════════════════════════════════════════════════════════════
#  CACHE
# ═════════════════════════════════════════════════════════════════════

_cache: dict[str, dict] = {}
_cache_lock = threading.Lock()


def _cache_key(provider: str, query: str, **extras) -> str:
    raw = f"{provider}:{query}:{json.dumps(extras, sort_keys=True)}"
    return hashlib.sha256(raw.encode()).hexdigest()[:16]


def _cache_get(key: str) -> Optional[dict]:
    with _cache_lock:
        entry = _cache.get(key)
        if entry and time.time() - entry["ts"] < CACHE_TTL_S:
            return entry["data"]
        if entry:
            del _cache[key]
        return None


def _cache_set(key: str, data: dict):
    with _cache_lock:
        if len(_cache) >= CACHE_MAX_ENTRIES:
            oldest_key = min(_cache, key=lambda k: _cache[k]["ts"])
            del _cache[oldest_key]
        _cache[key] = {"data": data, "ts": time.time()}


# ═════════════════════════════════════════════════════════════════════
#  API KEY RESOLUTION
#  Checks: env var → auth_profiles → legacy config (in that order)
# ═════════════════════════════════════════════════════════════════════

def _get_auth_store_key(provider_id: str) -> Optional[str]:
    """Resolve API key from Ghost's auth profile store."""
    try:
        from ghost_auth_profiles import get_auth_store
        store = get_auth_store()
        key = store.get_api_key(provider_id)
        if key and key != "__SETUP_PENDING__":
            return key
    except Exception:
        pass
    return None


def _get_openrouter_key() -> Optional[str]:
    key = os.environ.get("OPENROUTER_API_KEY", "")
    if key and key != "__SETUP_PENDING__":
        return key
    profile_key = _get_auth_store_key("openrouter")
    if profile_key:
        return profile_key
    try:
        cfg_file = GHOST_HOME / "config.json"
        if cfg_file.exists():
            cfg = json.loads(cfg_file.read_text(encoding="utf-8"))
            k = cfg.get("api_key", "")
            if k and k != "__SETUP_PENDING__":
                return k
    except Exception:
        pass
    return None


def _get_perplexity_key() -> Optional[str]:
    """Direct Perplexity key (pplx-...) from env."""
    return os.environ.get("PERPLEXITY_API_KEY") or None


def _get_grok_key() -> Optional[str]:
    key = os.environ.get("XAI_API_KEY")
    if key:
        return key
    try:
        int_file = GHOST_HOME / "integrations.json"
        if int_file.exists():
            cfg = json.loads(int_file.read_text(encoding="utf-8"))
            return cfg.get("grok", {}).get("api_key") or None
    except Exception:
        pass
    return None


def _get_brave_key() -> Optional[str]:
    return os.environ.get("BRAVE_API_KEY") or None


def _get_openai_key() -> Optional[str]:
    key = os.environ.get("OPENAI_API_KEY")
    if key:
        return key
    return _get_auth_store_key("openai")


def _get_gemini_key() -> Optional[str]:
    key = os.environ.get("GEMINI_API_KEY") or os.environ.get("GOOGLE_AI_API_KEY")
    if key:
        return key
    return _get_auth_store_key("google")


# ═════════════════════════════════════════════════════════════════════
#  PROVIDER IMPLEMENTATIONS
# ═════════════════════════════════════════════════════════════════════

def _search_perplexity_openrouter(query: str, count: int = 5,
                                   freshness: str = "") -> dict:
    """Search via Perplexity model on OpenRouter."""
    api_key = _get_openrouter_key()
    if not api_key:
        raise ValueError("No OpenRouter API key available")

    freshness_hint = ""
    if freshness:
        labels = {"day": "the last 24 hours", "week": "the past week",
                  "month": "the past month", "year": "the past year"}
        period = labels.get(freshness, freshness)
        freshness_hint = f" Focus ONLY on results from {period}."

    model = _get_model("web_search_perplexity", DEFAULT_PERPLEXITY_MODEL)
    body = {
        "model": model,
        "messages": [
            {
                "role": "system",
                "content": (
                    "You are a web search assistant. Return SPECIFIC, DATED news items "
                    "with concrete details — names, companies, dates, numbers. "
                    "Do NOT return vague trend summaries or broad themes. Each item must have: "
                    "what happened, who was involved, when it happened, and a source URL."
                    + freshness_hint
                ),
            },
            {"role": "user", "content": query},
        ],
    }

    resp = requests.post(
        f"{OPENROUTER_BASE}/chat/completions",
        headers={
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
            "HTTP-Referer": "https://github.com/ghost-ai",
            "X-Title": "Ghost Web Search",
        },
        json=body,
        timeout=REQUEST_TIMEOUT_S,
    )
    resp.raise_for_status()
    data = resp.json()

    content = data.get("choices", [{}])[0].get("message", {}).get("content", "")
    citations = data.get("citations", [])

    return {
        "provider": "perplexity (openrouter)",
        "query": query,
        "content": content,
        "citations": citations,
        "model": model,
    }


def _search_perplexity_direct(query: str, count: int = 5,
                               freshness: str = "") -> dict:
    """Search via direct Perplexity API."""
    api_key = _get_perplexity_key()
    if not api_key:
        raise ValueError("No Perplexity API key available")

    model = _get_model("web_search_perplexity_direct", DEFAULT_PERPLEXITY_DIRECT_MODEL)
    body = {
        "model": model,
        "messages": [
            {
                "role": "system",
                "content": (
                    "You are a web search assistant. Return SPECIFIC, DATED news items "
                    "with concrete details — names, companies, dates, numbers. "
                    "Do NOT return vague trend summaries or broad themes. Each item must have: "
                    "what happened, who was involved, when it happened, and a source URL."
                ),
            },
            {"role": "user", "content": query},
        ],
    }

    recency = FRESHNESS_MAP.get(freshness, "")
    if recency:
        pplx_recency = {"pd": "day", "pw": "week", "pm": "month", "py": "year"}
        body["search_recency_filter"] = pplx_recency.get(recency, "")

    resp = requests.post(
        f"{PERPLEXITY_BASE}/chat/completions",
        headers={
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
        },
        json=body,
        timeout=REQUEST_TIMEOUT_S,
    )
    resp.raise_for_status()
    data = resp.json()

    content = data.get("choices", [{}])[0].get("message", {}).get("content", "")
    citations = data.get("citations", [])

    return {
        "provider": "perplexity (direct)",
        "query": query,
        "content": content,
        "citations": citations,
        "model": model,
    }


def _build_temporal_query(query: str, freshness: str) -> str:
    """Append a temporal constraint to the query when freshness is set."""
    if not freshness:
        return query
    labels = {"day": "past 24 hours", "week": "past week",
              "month": "past month", "year": "past year"}
    period = labels.get(freshness)
    if period:
        return f"{query} (only results from the {period}, with specific dates and sources)"
    return query


def _search_grok(query: str, count: int = 5, freshness: str = "") -> dict:
    """Search via Grok/xAI with web search tool."""
    api_key = _get_grok_key()
    if not api_key:
        raise ValueError("No Grok/xAI API key available")

    model = _get_model("web_search_grok", DEFAULT_GROK_MODEL)
    effective_query = _build_temporal_query(query, freshness)
    body = {
        "model": model,
        "input": [{"role": "user", "content": effective_query}],
        "tools": [{"type": "web_search"}],
    }

    resp = requests.post(
        f"{XAI_BASE}/responses",
        headers={
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
        },
        json=body,
        timeout=REQUEST_TIMEOUT_S,
    )
    resp.raise_for_status()
    data = resp.json()

    content = ""
    citations = []
    for item in data.get("output", []):
        if item.get("type") == "message":
            for part in item.get("content", []):
                if part.get("type") == "output_text":
                    content += part.get("text", "")
                elif part.get("type") == "cite":
                    url = part.get("url", "")
                    if url:
                        citations.append(url)

    if not content:
        content = data.get("output_text", "")

    url_citations = data.get("citations", [])
    if url_citations:
        citations = url_citations

    return {
        "provider": "grok",
        "query": query,
        "content": content,
        "citations": citations,
        "model": model,
    }


def _search_openai(query: str, count: int = 5, freshness: str = "") -> dict:
    """Search via OpenAI Responses API with web_search tool."""
    api_key = _get_openai_key()
    if not api_key:
        raise ValueError("No OpenAI API key available")

    model = _get_model("web_search_openai", DEFAULT_OPENAI_SEARCH_MODEL)
    effective_query = _build_temporal_query(query, freshness)
    resp = requests.post(
        OPENAI_RESPONSES_BASE,
        headers={
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
        },
        json={
            "model": model,
            "tools": [{"type": "web_search"}],
            "input": effective_query,
        },
        timeout=REQUEST_TIMEOUT_S,
    )
    resp.raise_for_status()
    data = resp.json()

    content = ""
    citations = []

    for item in data.get("output", []):
        if item.get("type") == "message" and item.get("role") == "assistant":
            for block in item.get("content", []):
                if block.get("type") == "output_text":
                    content += block.get("text", "")
                    for ann in block.get("annotations", []):
                        if ann.get("type") == "url_citation":
                            url = ann.get("url", "")
                            if url and url not in citations:
                                citations.append(url)

    return {
        "provider": "openai",
        "query": query,
        "content": content,
        "citations": citations,
        "model": model,
    }


def _search_brave(query: str, count: int = 5, freshness: str = "") -> dict:
    """Search via Brave Search API."""
    api_key = _get_brave_key()
    if not api_key:
        raise ValueError("No Brave API key available")

    params = {"q": query, "count": str(min(count, 10))}
    brave_freshness = FRESHNESS_MAP.get(freshness, "")
    if brave_freshness:
        params["freshness"] = brave_freshness

    resp = requests.get(
        BRAVE_BASE,
        headers={
            "Accept": "application/json",
            "X-Subscription-Token": api_key,
        },
        params=params,
        timeout=REQUEST_TIMEOUT_S,
    )
    resp.raise_for_status()
    data = resp.json()

    results = []
    for item in data.get("web", {}).get("results", [])[:count]:
        results.append({
            "title": item.get("title", ""),
            "url": item.get("url", ""),
            "description": item.get("description", ""),
            "published": item.get("age", ""),
        })

    lines = []
    for i, r in enumerate(results, 1):
        lines.append(f"{i}. **{r['title']}**")
        lines.append(f"   {r['url']}")
        if r["description"]:
            lines.append(f"   {r['description']}")
        if r["published"]:
            lines.append(f"   Published: {r['published']}")

    return {
        "provider": "brave",
        "query": query,
        "content": "\n".join(lines),
        "citations": [r["url"] for r in results],
        "results": results,
    }


def _search_gemini(query: str, count: int = 5, freshness: str = "") -> dict:
    """Search via Google Gemini with grounding."""
    api_key = _get_gemini_key()
    if not api_key:
        raise ValueError("No Gemini API key available")

    model = _get_model("web_search_gemini", DEFAULT_GEMINI_MODEL)
    effective_query = _build_temporal_query(query, freshness)
    endpoint = f"{GEMINI_BASE}/models/{model}:generateContent"
    resp = requests.post(
        endpoint,
        headers={
            "Content-Type": "application/json",
            "x-goog-api-key": api_key,
        },
        json={
            "contents": [{"parts": [{"text": effective_query}]}],
            "tools": [{"google_search": {}}],
        },
        timeout=REQUEST_TIMEOUT_S,
    )
    resp.raise_for_status()
    data = resp.json()

    content = ""
    citations = []

    for candidate in data.get("candidates", []):
        for part in candidate.get("content", {}).get("parts", []):
            content += part.get("text", "")

        grounding = candidate.get("groundingMetadata", {})
        for chunk in grounding.get("groundingChunks", []):
            web = chunk.get("web", {})
            url = web.get("uri", "")
            if url:
                citations.append(url)

    return {
        "provider": "gemini",
        "query": query,
        "content": content,
        "citations": citations,
        "model": model,
    }


_DDG_FRESHNESS = {"day": "d", "week": "w", "month": "m", "year": "y"}


def _search_duckduckgo(query: str, count: int = 5, freshness: str = "") -> dict:
    """Keyless web search via DuckDuckGo's HTML endpoint.

    Requires NO API key, so it works for every user regardless of which LLM
    provider (openai-codex, openrouter, etc.) they use. Used as the final
    fallback so web_search never hard-fails on a fresh install.
    """
    try:
        from bs4 import BeautifulSoup
    except ImportError as exc:
        raise ValueError(f"DuckDuckGo fallback needs beautifulsoup4: {exc}")

    import urllib.parse as _up

    data = {"q": query}
    df = _DDG_FRESHNESS.get(freshness, "")
    if df:
        data["df"] = df

    resp = requests.post(
        "https://html.duckduckgo.com/html/",
        data=data,
        headers={
            "User-Agent": (
                "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/124.0.0.0 Safari/537.36"
            ),
            "Accept": "text/html,application/xhtml+xml",
        },
        timeout=REQUEST_TIMEOUT_S,
    )
    resp.raise_for_status()

    soup = BeautifulSoup(resp.text, "html.parser")
    results = []
    for res in soup.select("div.result")[: max(count, 1) * 2]:
        a = res.select_one("a.result__a")
        if not a:
            continue
        href = a.get("href", "")
        if "uddg=" in href:
            parsed = _up.parse_qs(_up.urlparse(href).query).get("uddg", [""])[0]
            href = _up.unquote(parsed) if parsed else href
        if href.startswith("//"):
            href = "https:" + href
        title = a.get_text(" ", strip=True)
        snippet_el = res.select_one(".result__snippet")
        snippet = snippet_el.get_text(" ", strip=True) if snippet_el else ""
        if title and href:
            results.append({"title": title, "url": href, "description": snippet})
        if len(results) >= count:
            break

    if not results:
        raise ValueError("DuckDuckGo returned no parseable results")

    lines = []
    for i, r in enumerate(results, 1):
        lines.append(f"{i}. **{r['title']}**")
        lines.append(f"   {r['url']}")
        if r["description"]:
            lines.append(f"   {r['description']}")

    return {
        "provider": "duckduckgo (keyless)",
        "query": query,
        "content": "\n".join(lines),
        "citations": [r["url"] for r in results],
        "results": results,
    }


# ═════════════════════════════════════════════════════════════════════
#  PROVIDER REGISTRY & FALLBACK
# ═════════════════════════════════════════════════════════════════════

PROVIDERS = [
    {
        "id": "perplexity_openrouter",
        "name": "perplexity (openrouter)",
        "fn": _search_perplexity_openrouter,
        "key_fn": _get_openrouter_key,
    },
    {
        "id": "perplexity_direct",
        "name": "perplexity (direct)",
        "fn": _search_perplexity_direct,
        "key_fn": _get_perplexity_key,
    },
    {
        "id": "grok",
        "name": "grok",
        "fn": _search_grok,
        "key_fn": _get_grok_key,
    },
    {
        "id": "openai",
        "name": "openai",
        "fn": _search_openai,
        "key_fn": _get_openai_key,
    },
    {
        "id": "brave",
        "name": "brave",
        "fn": _search_brave,
        "key_fn": _get_brave_key,
    },
    {
        "id": "gemini",
        "name": "gemini",
        "fn": _search_gemini,
        "key_fn": _get_gemini_key,
    },
    {
        # Keyless last-resort fallback. key_fn always returns True so it is
        # ALWAYS in the fallback chain — this guarantees web_search works even
        # when the user has no API keys (e.g. running on openai-codex /
        # ChatGPT subscription with no OpenRouter key configured).
        "id": "duckduckgo",
        "name": "duckduckgo (keyless)",
        "fn": _search_duckduckgo,
        "key_fn": lambda: True,
    },
]

_PROVIDER_BY_ID = {p["id"]: p for p in PROVIDERS}


def _get_ordered_providers(cfg: dict | None = None) -> list[dict]:
    """Return PROVIDERS reordered by the user's configured chain."""
    if cfg:
        chain = (cfg.get("provider_chains") or {}).get("web_search")
        if chain:
            ordered = [_PROVIDER_BY_ID[pid] for pid in chain if pid in _PROVIDER_BY_ID]
            seen = {p["id"] for p in ordered}
            ordered.extend(p for p in PROVIDERS if p["id"] not in seen)
            return ordered
    return list(PROVIDERS)


def get_available_providers(cfg: dict = None) -> list[dict]:
    """Return list of providers with availability status."""
    result = []
    for p in _get_ordered_providers(cfg):
        has_key = bool(p["key_fn"]())
        result.append({"name": p["name"], "available": has_key})
    return result


def search(query: str, count: int = 5, freshness: str = "",
           provider: str = "", cfg: dict = None) -> dict:
    """Run a web search with automatic fallback across providers.
    
    If provider is specified, only that provider is tried.
    Otherwise, tries each available provider in the configured order.
    """
    ck = _cache_key(provider or "auto", query, count=count, freshness=freshness)
    cached = _cache_get(ck)
    if cached:
        cached["cached"] = True
        return cached

    errors = []
    ordered = _get_ordered_providers(cfg)

    if provider:
        targets = [p for p in ordered if provider.lower() in p["name"].lower()]
        if not targets:
            return {"error": f"Unknown provider '{provider}'. Available: {[p['name'] for p in ordered]}"}
    else:
        targets = [p for p in ordered if p["key_fn"]()]

    if not targets:
        return {
            "error": "No search providers available. Ghost needs at least one of: "
                     "OPENROUTER_API_KEY (for Perplexity), OPENAI_API_KEY (for OpenAI web search), "
                     "XAI_API_KEY (for Grok), BRAVE_API_KEY (for Brave), or GOOGLE_AI_API_KEY (for Gemini). "
                     "Set one up in the Providers panel on the dashboard."
        }

    for p in targets:
        try:
            result = p["fn"](query, count=count, freshness=freshness)
            result["cached"] = False
            _cache_set(ck, result)
            return result
        except Exception as e:
            errors.append(f"{p['name']}: {e}")
            continue

    return {
        "error": f"All search providers failed:\n" + "\n".join(f"  - {e}" for e in errors),
        "query": query,
    }


# ═════════════════════════════════════════════════════════════════════
#  TOOL BUILDERS
# ═════════════════════════════════════════════════════════════════════

def build_web_search_tools(cfg: dict = None) -> list[dict]:
    """Build tool defs for Ghost's tool registry."""
    global _resolved_models
    if cfg:
        try:
            from ghost_config_tool import get_tool_model, TOOL_MODEL_DEFAULTS
            _resolved_models = {
                k: get_tool_model(k, cfg)
                for k in TOOL_MODEL_DEFAULTS
                if k.startswith("web_search_")
            }
        except ImportError:
            pass

    def web_search_exec(query: str, count: int = 5, freshness: str = "",
                        provider: str = ""):
        result = search(query, count=count, freshness=freshness, provider=provider, cfg=cfg)

        if "error" in result:
            return result["error"]

        lines = []
        prov = result.get("provider", "unknown")
        cached_tag = " (cached)" if result.get("cached") else ""
        lines.append(f"[Search via {prov}{cached_tag}]")
        lines.append("")

        content = result.get("content", "")
        if content:
            lines.append(content)

        citations = result.get("citations", [])
        if citations:
            lines.append("")
            lines.append("Sources:")
            for i, url in enumerate(citations[:10], 1):
                lines.append(f"  {i}. {url}")

        return "\n".join(lines)

    def web_search_providers_exec():
        providers = get_available_providers(cfg)
        lines = ["Web Search Providers:"]
        for p in providers:
            icon = "active" if p["available"] else "no API key"
            lines.append(f"  - {p['name']}: {icon}")

        active = sum(1 for p in providers if p["available"])
        lines.append(f"\n{active} provider(s) available for fallback chain.")
        return "\n".join(lines)

    return [
        {
            "name": "web_search",
            "description": (
                "Search the web for current information. Returns specific, dated results "
                "with source citations. Automatically falls back across multiple providers "
                "(Perplexity, Grok, OpenAI, Brave, Gemini, and a keyless DuckDuckGo "
                "fallback that always works without any API key) if one fails. Use this for any "
                "question requiring up-to-date information: news, tech updates, library docs, "
                "security advisories, current events, etc. "
                "IMPORTANT: For news or recent events, ALWAYS set freshness to 'day' or 'week' "
                "to get actual recent items instead of generic summaries."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "query": {
                        "type": "string",
                        "description": "The search query. Be specific for better results.",
                    },
                    "count": {
                        "type": "integer",
                        "description": "Number of results (1-10, only affects Brave provider)",
                        "default": 5,
                    },
                    "freshness": {
                        "type": "string",
                        "enum": ["day", "week", "month", "year", ""],
                        "description": (
                            "Recency filter. MUST be set for news/current events queries. "
                            "'day' = last 24h, 'week' = last 7 days, 'month' = last 30 days, "
                            "'year' = last 365 days. Use 'week' as default for news queries."
                        ),
                        "default": "",
                    },
                    "provider": {
                        "type": "string",
                        "description": "Force a specific provider (e.g. 'perplexity', 'grok', 'brave', 'gemini'). Leave empty for automatic fallback.",
                        "default": "",
                    },
                },
                "required": ["query"],
            },
            "execute": web_search_exec,
        },
        {
            "name": "web_search_providers",
            "description": "List available web search providers and their status.",
            "parameters": {"type": "object", "properties": {}},
            "execute": web_search_providers_exec,
        },
    ]
