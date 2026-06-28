"""Models API — multi-provider model browser, selection, and fallback chain management."""

import logging
import time
import urllib.request
import json
from flask import Blueprint, jsonify, request

import sys
from pathlib import Path

log = logging.getLogger(__name__)
sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent))

from ghost import load_config, save_config, DEFAULT_CONFIG
from ghost_providers import validate_model_for_provider

bp = Blueprint("models", __name__)

OPENROUTER_MODELS_URL = "https://openrouter.ai/api/v1/models"

_cache = {"models": [], "fetched_at": 0}
CACHE_TTL = 300


def _parse_provider(model_name):
    if ":" in model_name:
        return model_name.split(":")[0].strip()
    return ""


def _classify_tier(model_id, pricing):
    prompt_cost = float(pricing.get("prompt", "0"))
    if ":free" in model_id or prompt_cost == 0:
        return "free"
    per_m = prompt_cost * 1_000_000
    if per_m >= 3.0:
        return "premium"
    if per_m >= 0.5:
        return "standard"
    return "fast"


def _fetch_openrouter_models():
    now = time.time()
    if _cache["models"] and (now - _cache["fetched_at"]) < CACHE_TTL:
        return _cache["models"]

    try:
        req = urllib.request.Request(OPENROUTER_MODELS_URL, headers={"User-Agent": "Ghost-Dashboard/1.0"})
        with urllib.request.urlopen(req, timeout=10) as resp:
            data = json.loads(resp.read().decode("utf-8"))

        raw = data.get("data", [])
        models = []
        for m in raw:
            mid = m.get("id", "")
            name = m.get("name", mid)
            pricing = m.get("pricing", {})
            ctx = m.get("context_length", 0)
            arch = m.get("architecture", {})
            modality = arch.get("modality", "text->text")
            prompt_cost = float(pricing.get("prompt", "0"))
            completion_cost = float(pricing.get("completion", "0"))
            provider = _parse_provider(name)
            short_name = name.split(":", 1)[1].strip() if ":" in name else name

            models.append({
                "id": mid,
                "name": short_name,
                "provider": provider,
                "tier": _classify_tier(mid, pricing),
                "context_length": ctx,
                "modality": modality,
                "pricing": {
                    "prompt_per_m": round(prompt_cost * 1_000_000, 2),
                    "completion_per_m": round(completion_cost * 1_000_000, 2),
                },
                "description": (m.get("description") or "")[:200],
                "source": "openrouter",
            })

        _cache["models"] = models
        _cache["fetched_at"] = now
        return models

    except Exception:
        log.warning("Failed to fetch models from OpenRouter", exc_info=True)
        return _cache["models"] if _cache["models"] else []


def _get_provider_models(provider_id):
    """Get models for a specific direct provider."""
    from ghost_providers import get_provider
    prov = get_provider(provider_id)
    if not prov:
        return []
    return [
        {
            "id": m,
            "name": m,
            "provider": prov.name,
            "tier": "standard",
            "context_length": 0,
            "modality": "text->text",
            "pricing": {},
            "description": "",
            "source": provider_id,
        }
        for m in prov.models
    ]


def _get_codex_models():
    """Get the live list of Codex models the ChatGPT account is entitled to.

    Falls back to the static provider list if live discovery is unavailable
    (e.g. no OAuth token yet, or network error)."""
    from ghost_providers import fetch_codex_models, get_provider

    store = None
    try:
        from ghost_dashboard import get_daemon
        daemon = get_daemon()
        store = getattr(daemon, "auth_store", None)
    except Exception:
        store = None
    if store is None:
        try:
            from ghost_auth_profiles import get_auth_store
            store = get_auth_store()
        except Exception:
            store = None

    try:
        live = fetch_codex_models(store)
    except Exception:
        log.warning("Live Codex model discovery failed", exc_info=True)
        live = None

    if not live:
        return _get_provider_models("openai-codex")

    prov = get_provider("openai-codex")
    provider_name = prov.name if prov else "OpenAI Codex"
    out = []
    for m in live:
        mods = m.get("input_modalities") or ["text"]
        modality = "text+image->text" if "image" in mods else "text->text"
        out.append({
            "id": m["id"],
            "name": m.get("name", m["id"]),
            "provider": provider_name,
            "tier": "free",  # included with the ChatGPT subscription
            "context_length": m.get("context_length", 0),
            "modality": modality,
            "pricing": {},
            "description": m.get("description", ""),
            "source": "openai-codex",
            "live": True,
        })
    return out


@bp.route("/api/models")
def get_models():
    from ghost_dashboard import get_daemon
    daemon = get_daemon()
    cfg = daemon.cfg if daemon else load_config()
    current = cfg.get("model", DEFAULT_CONFIG["model"])

    from ghost_auth_profiles import get_auth_store
    store = get_auth_store()
    primary = cfg.get("primary_provider", "openrouter")
    status = store.get_provider_status(primary)
    has_key = status.get("configured", False)
    masked = status.get("masked_key", "")

    all_models = _fetch_openrouter_models()

    return jsonify({
        "current": current,
        "models": all_models,
        "total": len(all_models),
        "has_api_key": has_key,
        "api_key_masked": masked,
    })


@bp.route("/api/models", methods=["PUT"])
def set_model():
    data = request.get_json(silent=True) or {}
    cfg = load_config()

    provider = (data.get("provider", "") or "").strip().lower() or "openrouter"
    model_id = data.get("model", "").strip()
    effective_model_id = ""

    if "api_key" in data and data["api_key"]:
        cfg["api_key"] = data["api_key"]

    if model_id:
        valid, normalized_or_reason = validate_model_for_provider(provider, model_id)
        if not valid:
            hint = ""
            if provider != "openrouter":
                hint = " Use provider-native model id, e.g. gemini-2.5-pro"
            return jsonify({
                "ok": False,
                "error": f"{normalized_or_reason}{hint}",
                "provider": provider,
                "model": model_id,
            }), 400

        normalized_model = normalized_or_reason
        if provider != "openrouter" and "/" in normalized_model:
            return jsonify({
                "ok": False,
                "error": "Invalid model format for direct provider. Use provider-native model id, e.g. gemini-2.5-pro",
                "provider": provider,
                "model": model_id,
            }), 400

        effective_model_id = normalized_model
        provider_models = cfg.setdefault("provider_models", {})
        provider_models[provider] = normalized_model
        full_model_id = f"{provider}:{normalized_model}" if provider != "openrouter" else normalized_model
        cfg["model"] = full_model_id

    save_config(cfg)

    from ghost_dashboard import get_daemon
    daemon = get_daemon()
    if daemon:
        daemon.cfg.update(cfg)
        new_model = cfg.get("model")
        if new_model:
            if hasattr(daemon, 'llm'):
                daemon.llm.model = new_model
            if hasattr(daemon, 'engine'):
                daemon.engine.model = new_model
            if getattr(daemon, 'chat_engine', None):
                daemon.chat_engine.model = new_model

        if provider and effective_model_id and hasattr(daemon, 'engine'):
            chain = daemon._build_provider_chain(
                cfg.get("model", DEFAULT_CONFIG["model"]),
                cfg.get("fallback_models", []),
            )
            daemon.engine.fallback_chain.set_provider_chain(chain)
            if getattr(daemon, 'chat_engine', None):
                daemon.chat_engine.fallback_chain.set_provider_chain(list(chain))

    return jsonify({
        "ok": True,
        "model": cfg.get("model"),
        "provider": provider,
        "provider_model": effective_model_id,
    })


# ═════════════════════════════════════════════════════════════════
#  Multi-provider endpoints
# ═════════════════════════════════════════════════════════════════

@bp.route("/api/providers")
def get_providers():
    """List all providers with their configuration status."""
    from ghost_providers import list_providers
    from ghost_auth_profiles import get_auth_store
    store = get_auth_store()
    providers = list_providers()
    for p in providers:
        status = store.get_provider_status(p["id"])
        p.update(status)
    return jsonify({"providers": providers})



@bp.route("/api/providers/<provider_id>/models")
def get_provider_models(provider_id):
    """Get available models for a specific provider."""
    if provider_id == "openrouter":
        models = _fetch_openrouter_models()
    elif provider_id == "ollama":
        models = _get_ollama_models()
    elif provider_id == "openai-codex":
        models = _get_codex_models()
    else:
        models = _get_provider_models(provider_id)
    return jsonify({"provider": provider_id, "models": models})


@bp.route("/api/providers/<provider_id>/test", methods=["POST"])
def test_provider_route(provider_id):
    """Test connection to a provider."""
    data = request.get_json(silent=True) or {}
    api_key = data.get("api_key", "")
    if not api_key:
        from ghost_auth_profiles import get_auth_store
        store = get_auth_store()
        if provider_id == "openai-codex":
            try:
                from ghost_oauth import ensure_fresh_token
                api_key = ensure_fresh_token(store) or ""
            except Exception:
                api_key = store.get_api_key(provider_id)
        else:
            api_key = store.get_api_key(provider_id)

    from ghost_providers import test_provider_connection
    result = test_provider_connection(provider_id, api_key)
    return jsonify(result)


@bp.route("/api/fallback-chain")
def get_fallback_chain():
    """Get the current fallback chain status."""
    from ghost_dashboard import get_daemon
    daemon = get_daemon()
    if daemon and hasattr(daemon, 'engine'):
        stats = daemon.engine.fallback_chain.stats
        return jsonify(stats)
    return jsonify({"chain": [], "active": ""})


@bp.route("/api/fallback-chain", methods=["PUT"])
def set_fallback_chain():
    """Update the fallback chain order."""
    data = request.get_json(silent=True) or {}
    chain = data.get("chain", [])

    if not chain:
        return jsonify({"ok": False, "error": "chain is required"}), 400

    parsed = []
    for item in chain:
        if isinstance(item, dict):
            parsed.append((item.get("provider", "openrouter"), item.get("model", "")))
        elif isinstance(item, str) and ":" in item:
            parts = item.split(":", 1)
            parsed.append((parts[0], parts[1]))
        elif isinstance(item, (list, tuple)) and len(item) == 2:
            parsed.append(tuple(item))

    from ghost_dashboard import get_daemon
    daemon = get_daemon()
    if daemon and hasattr(daemon, 'engine'):
        daemon.engine.fallback_chain.set_provider_chain(parsed)
        if getattr(daemon, 'chat_engine', None):
            daemon.chat_engine.fallback_chain.set_provider_chain(list(parsed))
        return jsonify({"ok": True, "chain": [f"{p}:{m}" for p, m in parsed]})

    return jsonify({"ok": False, "error": "Daemon not running"}), 503


@bp.route("/api/primary-provider", methods=["PUT"])
def set_primary_provider():
    """Set the primary LLM provider."""
    data = request.get_json(silent=True) or {}
    provider_id = data.get("provider", "").strip()

    if not provider_id:
        return jsonify({"ok": False, "error": "provider is required"}), 400

    from ghost_providers import get_provider
    if not get_provider(provider_id):
        return jsonify({"ok": False, "error": f"Unknown provider: {provider_id}"}), 400

    cfg = load_config()
    cfg["primary_provider"] = provider_id
    save_config(cfg)

    from ghost_dashboard import get_daemon
    daemon = get_daemon()
    if daemon:
        daemon.cfg["primary_provider"] = provider_id
        if hasattr(daemon, 'auth_store') and hasattr(daemon, 'engine'):
            model = cfg.get("model", DEFAULT_CONFIG["model"])
            fallback_models = cfg.get("fallback_models", [])
            new_chain = daemon._build_provider_chain(model, fallback_models)
            daemon.engine.fallback_chain.set_provider_chain(new_chain)
            if getattr(daemon, 'chat_engine', None):
                daemon.chat_engine.fallback_chain.set_provider_chain(list(new_chain))

    return jsonify({"ok": True, "primary_provider": provider_id})


@bp.route("/api/primary-provider")
def get_primary_provider():
    """Get the current primary provider."""
    cfg = load_config()
    return jsonify({
        "primary_provider": cfg.get("primary_provider", DEFAULT_CONFIG.get("primary_provider", "openrouter")),
    })


# ═════════════════════════════════════════════════════════════════
#  Coding Model Dispatcher endpoints
# ═════════════════════════════════════════════════════════════════

@bp.route("/api/coding-model-dispatch")
def get_coding_dispatch():
    """Return dispatcher status: selected model, benchmarks, budget, cache."""
    from ghost_dashboard import get_daemon
    daemon = get_daemon()
    cfg = daemon.cfg if daemon else load_config()
    auth_store = getattr(daemon, "auth_store", None)

    benchmarks = {}
    selected = None
    coding_chain = []
    available_providers = []
    try:
        from ghost_model_dispatch import (
            ModelDispatcher, _get_available_providers, _resolve_budget,
            _seed_benchmarks_if_missing, BENCHMARKS_FILE,
        )
        _seed_benchmarks_if_missing()
        if BENCHMARKS_FILE.exists():
            benchmarks = json.loads(BENCHMARKS_FILE.read_text("utf-8")).get("models", {})

        avail = _get_available_providers(cfg, auth_store)
        available_providers = sorted(avail)

        dispatcher = ModelDispatcher(cfg, auth_store)
        selected = dispatcher._compute_selection("coding")
        coding_chain = dispatcher.select_chain("coding")

        max_cost, strategy = _resolve_budget(cfg)
    except Exception:
        log.warning("Failed to load coding model dispatch", exc_info=True)
        max_cost, strategy = 100.0, "best_value"

    budget_val = cfg.get("coding_model_budget", "auto")
    override = cfg.get("coding_model_override") or None

    models_list = []
    for name, info in benchmarks.items():
        routes = info.get("routes", {})
        cheapest_cost = None
        cheapest_provider = None
        for pid, route in routes.items():
            if pid in available_providers:
                cost = route.get("input", 999)
                if cheapest_cost is None or cost < cheapest_cost:
                    cheapest_cost = cost
                    cheapest_provider = pid
        models_list.append({
            "name": name,
            "swe_bench": info.get("swe_bench", 0),
            "cheapest_cost": cheapest_cost,
            "cheapest_provider": cheapest_provider,
            "available": cheapest_provider is not None,
            "routes": {pid: r for pid, r in routes.items()},
        })
    models_list.sort(key=lambda m: m["swe_bench"], reverse=True)

    return jsonify({
        "selected_model": selected,
        "coding_chain": [f"{p}:{m}" for p, m in coding_chain],
        "budget": budget_val,
        "budget_resolved": {"max_cost": max_cost, "strategy": strategy},
        "override": override,
        "min_swe_bench_score": cfg.get("min_swe_bench_score", 78.0),
        "coding_jobs": cfg.get("coding_jobs", []),
        "available_providers": available_providers,
        "benchmarks": models_list,
    })


@bp.route("/api/coding-model-dispatch", methods=["PUT"])
def update_coding_dispatch():
    """Update coding model dispatch config (budget, override, min_score)."""
    data = request.get_json(silent=True) or {}
    cfg = load_config()

    if "coding_model_budget" in data:
        val = data["coding_model_budget"]
        if isinstance(val, str) and val.replace(".", "", 1).isdigit():
            val = float(val)
        cfg["coding_model_budget"] = val
    if "coding_model_override" in data:
        cfg["coding_model_override"] = data["coding_model_override"] or None
    if "min_swe_bench_score" in data:
        try:
            cfg["min_swe_bench_score"] = float(data["min_swe_bench_score"])
        except (ValueError, TypeError):
            pass
    if "coding_jobs" in data and isinstance(data["coding_jobs"], list):
        cfg["coding_jobs"] = data["coding_jobs"]

    save_config(cfg)

    from ghost_dashboard import get_daemon
    daemon = get_daemon()
    if daemon:
        daemon.cfg.update(cfg)

    try:
        from ghost_model_dispatch import reset_dispatcher, CACHE_FILE
        reset_dispatcher()
        if CACHE_FILE.exists():
            CACHE_FILE.unlink()
    except Exception:
        pass

    return jsonify({"ok": True})


@bp.route("/api/coding-model-dispatch/refresh", methods=["POST"])
def refresh_coding_dispatch():
    """Force re-select the coding model (clear cache)."""
    try:
        from ghost_model_dispatch import reset_dispatcher, CACHE_FILE
        reset_dispatcher()
        if CACHE_FILE.exists():
            CACHE_FILE.unlink()
    except Exception:
        pass
    return jsonify({"ok": True})


def _get_ollama_models():
    """Fetch models from local Ollama instance."""
    try:
        import requests as req
        resp = req.get("http://localhost:11434/api/tags", timeout=5)
        if resp.ok:
            tags = resp.json().get("models", [])
            return [
                {
                    "id": m.get("name", ""),
                    "name": m.get("name", ""),
                    "provider": "Ollama",
                    "tier": "free",
                    "context_length": 0,
                    "modality": "text->text",
                    "pricing": {},
                    "description": f"Size: {m.get('size', 'unknown')}",
                    "source": "ollama",
                }
                for m in tags
            ]
    except Exception:
        log.warning("Failed to get Ollama tags", exc_info=True)
    return []
