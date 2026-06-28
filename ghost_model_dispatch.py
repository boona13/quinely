"""
Ghost Model Dispatcher — budget-aware coding model selection.

Selects the best available coding model based on SWE-bench scores, user budget,
and configured providers. Checks all providers (OpenRouter, Anthropic direct,
OpenAI direct, etc.) and picks the cheapest route to the highest-quality model
the user can afford.
"""

import json
import logging
import os
import time
import urllib.request
from pathlib import Path

log = logging.getLogger("ghost.model_dispatch")

GHOST_HOME = Path.home() / ".ghost"
BENCHMARKS_FILE = GHOST_HOME / "coding_benchmarks.json"
CACHE_FILE = GHOST_HOME / "model_dispatch_cache.json"
CACHE_TTL = 86400  # 24 hours

OPENROUTER_MODELS_URL = "https://openrouter.ai/api/v1/models"

BUDGET_PRESETS: dict[str, float] = {
    "free": 0.0,
    "low": 0.50,
    "medium": 2.00,
    "high": 6.00,
}

_SEED_BENCHMARKS = {
    "source": "swe-bench-verified",
    "updated": "2026-03-10",
    "models": {
        "claude-opus-4.6": {
            "swe_bench": 80.8,
            "routes": {
                "anthropic": {"id": "claude-opus-4-6", "input": 3.00, "output": 15.00},
                "openrouter": {"id": "anthropic/claude-opus-4.6", "input": 5.00, "output": 25.00},
            },
        },
        "claude-sonnet-4.6": {
            "swe_bench": 79.6,
            "routes": {
                "anthropic": {"id": "claude-sonnet-4-6", "input": 1.50, "output": 7.50},
                "openrouter": {"id": "anthropic/claude-sonnet-4.6", "input": 3.00, "output": 15.00},
            },
        },
        "minimax-m2.5": {
            "swe_bench": 80.2,
            "routes": {
                "openrouter": {"id": "minimax/minimax-m2.5", "input": 0.30, "output": 1.20},
            },
        },
        "gpt-5.2": {
            "swe_bench": 80.0,
            "routes": {
                "openai": {"id": "gpt-5.2", "input": 1.25, "output": 5.00},
                "openrouter": {"id": "openai/gpt-5.2", "input": 1.50, "output": 6.00},
            },
        },
        "gpt-5.5": {
            "swe_bench": 82.5,
            "routes": {
                "openai": {"id": "gpt-5.5", "input": 1.25, "output": 5.00},
                "openai-codex": {"id": "gpt-5.5", "input": 0.00, "output": 0.00},
                "openrouter": {"id": "openai/gpt-5.5", "input": 2.00, "output": 8.00},
            },
        },
        "gemini-3-flash": {
            "swe_bench": 78.0,
            "routes": {
                "google": {"id": "gemini-3.0-flash", "input": 0.15, "output": 0.60},
                "openrouter": {"id": "google/gemini-3.0-flash", "input": 0.50, "output": 3.00},
            },
        },
        "gemini-2.5-pro": {
            "swe_bench": 78.5,
            "routes": {
                "google": {"id": "gemini-2.5-pro", "input": 0.63, "output": 2.50},
                "openrouter": {"id": "google/gemini-2.5-pro-preview", "input": 1.25, "output": 10.00},
            },
        },
        "glm-5": {
            "swe_bench": 77.8,
            "routes": {
                "openrouter": {"id": "zhipu/glm-5", "input": 0.11, "output": 0.44},
            },
        },
        "kimi-k2.5": {
            "swe_bench": 76.8,
            "routes": {
                "openrouter": {"id": "moonshotai/kimi-k2.5", "input": 0.60, "output": 2.40},
            },
        },
    },
}

# Providers that must never handle Ghost coding tasks (evolve, feature
# implementation, autonomous tool loops).  Ollama local models lack the
# capacity for sustained multi-step coding, and DeepSeek's reasoning model
# is unreliable with complex tool-calling chains.
_CODING_EXCLUDED_PROVIDERS = frozenset({"ollama", "deepseek"})

_KNOWN_PROVIDERS = frozenset({
    "openrouter", "openai", "anthropic",
    "google", "ollama", "openai-codex", "deepseek",
})


def _parse_model_to_provider_tuple(model_str: str) -> tuple[str, str]:
    """Parse dispatch-format string to (provider_id, model_id) tuple.

    Dispatch format uses colon for direct providers ('anthropic:claude-opus-4-6')
    and bare model IDs for OpenRouter ('minimax/minimax-m2.5').
    """
    if ":" in model_str:
        idx = model_str.index(":")
        prefix = model_str[:idx]
        if prefix in _KNOWN_PROVIDERS:
            return (prefix, model_str[idx + 1:])
    return ("openrouter", model_str)


def _seed_benchmarks_if_missing():
    """Write initial benchmark data if the file doesn't exist yet."""
    if BENCHMARKS_FILE.exists():
        return
    BENCHMARKS_FILE.parent.mkdir(parents=True, exist_ok=True)
    BENCHMARKS_FILE.write_text(
        json.dumps(_SEED_BENCHMARKS, indent=2), encoding="utf-8"
    )
    log.info("Seeded coding benchmarks at %s", BENCHMARKS_FILE)


def _get_available_providers(cfg: dict, auth_store=None) -> set[str]:
    """Return set of provider IDs that have valid credentials configured.

    For no-auth providers (e.g. Ollama), only include them if the user
    explicitly configured them during setup (has a profile in auth store)
    to avoid phantom availability from uninstalled local runtimes.
    """
    available = set()
    try:
        from ghost_providers import PROVIDERS
    except ImportError:
        return available

    for pid, prov in PROVIDERS.items():
        if prov.auth_type == "none":
            if auth_store and auth_store.get_provider_profile(pid):
                available.add(pid)
            continue
        if prov.auth_type == "oauth":
            if auth_store:
                try:
                    key = auth_store.get_api_key(pid)
                    if key and key != "__SETUP_PENDING__":
                        available.add(pid)
                except Exception:
                    pass
            continue
        key = None
        if auth_store:
            try:
                key = auth_store.get_api_key(pid)
            except Exception:
                pass
        if not key and prov.env_key:
            key = os.environ.get(prov.env_key, "")
        if not key:
            key = cfg.get("api_key", "")
            if pid != cfg.get("primary_provider", "openrouter"):
                key = ""
        if key and key != "__SETUP_PENDING__":
            available.add(pid)

    return available


def _resolve_budget(cfg: dict) -> tuple[float, str]:
    """Convert budget config to (max_cost_per_mtok, strategy).

    "best_value" (score/cost) for auto and low — prefers free models like
    GPT 5.3 Codex over marginally-higher-scoring paid models.
    "best_quality" (highest score) for medium, high, and explicit numeric budgets.
    """
    raw = cfg.get("coding_model_budget", "auto")

    if isinstance(raw, (int, float)):
        return (float(raw), "best_quality")

    raw_str = str(raw).strip().lower()

    if raw_str == "auto":
        return (100.0, "best_value")

    if raw_str in BUDGET_PRESETS:
        strategy = "best_value" if raw_str in ("free", "low") else "best_quality"
        return (BUDGET_PRESETS[raw_str], strategy)

    try:
        return (float(raw_str), "best_quality")
    except (ValueError, TypeError):
        log.warning("Invalid coding_model_budget '%s', using auto", raw)
        return (100.0, "best_value")


class ModelDispatcher:
    """Budget-aware coding model selector."""

    def __init__(self, cfg: dict, auth_store=None):
        self._cfg = cfg
        self._auth_store = auth_store
        self._cache: dict | None = None

    def select(self, task_type: str = "coding") -> str | None:
        """Select the best model for the given task type.

        Returns a model string like "anthropic:claude-opus-4-6" or
        "minimax/minimax-m2.5" (OpenRouter), or None if nothing found.
        """
        chain = self.select_chain(task_type)
        if not chain:
            return None
        provider_id, model_id = chain[0]
        return model_id if provider_id == "openrouter" else f"{provider_id}:{model_id}"

    def select_chain(self, task_type: str = "coding") -> list[tuple[str, str]]:
        """Select a ranked list of (provider, model) pairs for coding tasks.

        Returns tuples like ("anthropic", "claude-opus-4-6") for direct
        providers or ("openrouter", "minimax/minimax-m2.5") for OpenRouter.
        The first entry is the primary pick; remaining entries are
        coding-quality fallbacks scoped to the user's available providers.
        """
        override = self._cfg.get("coding_model_override")
        if override:
            log.info("[model_dispatch] Using manual override: %s", override)
            return [_parse_model_to_provider_tuple(override)]

        return self._compute_ranked_chain(task_type)

    def _primary_provider_fallback_tuple(self, available: set[str]) -> tuple[str, str] | None:
        """Return the primary provider's default model as a (provider, model) tuple."""
        try:
            from ghost_providers import get_provider
        except ImportError:
            return None

        primary = self._cfg.get("primary_provider", "openrouter")
        if primary in available:
            prov = get_provider(primary)
            if prov:
                return (primary, prov.default_model)

        for pid in available:
            if pid == "ollama":
                continue
            prov = get_provider(pid)
            if prov:
                return (pid, prov.default_model)

        if "ollama" in available:
            prov = get_provider("ollama")
            if prov:
                return ("ollama", prov.default_model)

        return None

    def _primary_provider_fallback(self, available: set[str]) -> str | None:
        """Return the primary provider's default model as a dispatch-format string."""
        entry = self._primary_provider_fallback_tuple(available)
        if entry is None:
            return None
        pid, model = entry
        return model if pid == "openrouter" else f"{pid}:{model}"

    def _compute_ranked_chain(self, task_type: str) -> list[tuple[str, str]]:
        """Compute a ranked list of coding-quality models across available providers.

        Each provider contributes its cheapest route per benchmarked model.
        The list is sorted by the budget strategy (best_value or best_quality)
        so the caller can iterate through progressively as a coding-specific
        fallback chain.
        """
        benchmarks = self._load_benchmarks()
        available = _get_available_providers(self._cfg, self._auth_store)
        if not available:
            log.warning("[model_dispatch] No providers available")
            return []

        coding_available = available - _CODING_EXCLUDED_PROVIDERS

        if not coding_available:
            log.warning(
                "[model_dispatch] No coding-capable providers available "
                "(configured: %s, excluded: %s)",
                available, available & _CODING_EXCLUDED_PROVIDERS,
            )
            fb = self._primary_provider_fallback_tuple(coding_available or available)
            return [fb] if fb else []

        if not benchmarks:
            log.warning("[model_dispatch] No benchmark data, using primary provider fallback")
            fb = self._primary_provider_fallback_tuple(coding_available)
            return [fb] if fb else []

        max_cost, strategy = _resolve_budget(self._cfg)
        min_score = self._cfg.get("min_swe_bench_score", 78.0)

        candidates = []
        for name, info in benchmarks.items():
            score = info.get("swe_bench", 0)
            routes = info.get("routes", {})

            best_route = None
            best_cost = float("inf")
            for provider_id, route in routes.items():
                if provider_id not in coding_available:
                    continue
                cost = route.get("input", 999)
                if cost <= max_cost and cost < best_cost:
                    best_cost = cost
                    best_route = (provider_id, route)

            if best_route is None:
                continue

            provider_id, route = best_route
            model_id = route["id"]

            candidates.append({
                "name": name,
                "provider": provider_id,
                "model_id": model_id,
                "score": score,
                "cost": best_cost,
                "value": score / max(best_cost, 0.10),
            })

        if not candidates:
            if max_cost == 0:
                log.info("[model_dispatch] No free coding models available")
                return []
            log.warning(
                "[model_dispatch] No benchmarked models for available providers; "
                "falling back to primary provider default",
            )
            fb = self._primary_provider_fallback_tuple(coding_available)
            return [fb] if fb else []

        above_min = [c for c in candidates if c["score"] >= min_score]
        pool = above_min if above_min else candidates
        if not above_min and candidates:
            best_avail = max(candidates, key=lambda c: c["score"])
            log.warning(
                "[model_dispatch] No models above %.1f%% SWE-bench within budget; "
                "relaxing to %s (%.1f%%)",
                min_score, best_avail["name"], best_avail["score"],
            )

        if strategy == "best_value":
            pool.sort(key=lambda c: c["value"], reverse=True)
        else:
            pool.sort(key=lambda c: c["score"], reverse=True)

        result = [(c["provider"], c["model_id"]) for c in pool]

        log.info(
            "[model_dispatch] Coding chain (%d models, strategy: %s): %s",
            len(result), strategy,
            " → ".join(f"{p}:{m}" for p, m in result),
        )
        return result

    def _compute_selection(self, task_type: str) -> str | None:
        """Legacy single-model selection (delegates to select_chain)."""
        chain = self._compute_ranked_chain(task_type)
        if not chain:
            return None
        pid, model_id = chain[0]
        return model_id if pid == "openrouter" else f"{pid}:{model_id}"

    def _load_benchmarks(self) -> dict:
        _seed_benchmarks_if_missing()
        try:
            data = json.loads(BENCHMARKS_FILE.read_text(encoding="utf-8"))
            return data.get("models", {})
        except Exception as exc:
            log.warning("[model_dispatch] Failed to load benchmarks: %s", exc)
            return _SEED_BENCHMARKS.get("models", {})

    def _read_cache(self, task_type: str) -> str | None:
        try:
            if not CACHE_FILE.exists():
                return None
            data = json.loads(CACHE_FILE.read_text(encoding="utf-8"))
            entry = data.get(task_type)
            if not entry:
                return None
            if time.time() - entry.get("ts", 0) > CACHE_TTL:
                return None
            budget_key = str(self._cfg.get("coding_model_budget", "auto"))
            if entry.get("budget") != budget_key:
                return None
            model = entry.get("model")
            if model:
                log.debug("[model_dispatch] Cache hit: %s", model)
            return model
        except Exception:
            return None

    def _write_cache(self, task_type: str, model: str):
        try:
            data = {}
            if CACHE_FILE.exists():
                data = json.loads(CACHE_FILE.read_text(encoding="utf-8"))
            data[task_type] = {
                "model": model,
                "ts": time.time(),
                "budget": str(self._cfg.get("coding_model_budget", "auto")),
            }
            CACHE_FILE.parent.mkdir(parents=True, exist_ok=True)
            CACHE_FILE.write_text(json.dumps(data, indent=2), encoding="utf-8")
        except Exception as exc:
            log.debug("[model_dispatch] Cache write failed: %s", exc)


_dispatcher: ModelDispatcher | None = None


def get_dispatcher(cfg: dict, auth_store=None) -> ModelDispatcher:
    """Get or create the singleton ModelDispatcher."""
    global _dispatcher
    if _dispatcher is None:
        _dispatcher = ModelDispatcher(cfg, auth_store)
    return _dispatcher


def reset_dispatcher():
    """Reset the singleton (for config changes or testing)."""
    global _dispatcher
    _dispatcher = None
