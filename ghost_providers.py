"""
GHOST Multi-Provider LLM Support

Provider registry with API format adapters for OpenRouter, OpenAI, Anthropic,
OpenAI Codex (ChatGPT subscription), Google Gemini, xAI, and Ollama.
"""

import json
import logging
import requests
from dataclasses import dataclass
from typing import Any

log = logging.getLogger("quinely.providers")


# ═════════════════════════════════════════════════════════════════════
#  PROVIDER REGISTRY
# ═════════════════════════════════════════════════════════════════════

@dataclass
class ProviderConfig:
    id: str
    name: str
    base_url: str
    api_format: str          # "openai" | "anthropic" | "codex_responses"
    auth_type: str           # "api_key" | "oauth" | "none"
    default_model: str
    env_key: str = ""        # env var name for API key
    models: list = None      # popular models for this provider
    description: str = ""

    def __post_init__(self):
        if self.models is None:
            self.models = []


PROVIDERS: dict[str, ProviderConfig] = {
    "openrouter": ProviderConfig(
        id="openrouter",
        name="OpenRouter",
        base_url="https://openrouter.ai/api/v1/chat/completions",
        api_format="openai",
        auth_type="api_key",
        default_model="moonshotai/kimi-k2.5",
        env_key="OPENROUTER_API_KEY",
        models=[
            "moonshotai/kimi-k2.5",
            "anthropic/claude-opus-4.6",
            "openai/gpt-5.5",
            "openai/gpt-5.4",
            "openai/gpt-5.4-pro",
            "google/gemini-2.5-pro",
            "anthropic/claude-sonnet-4",
            "openai/gpt-4.1",
            "qwen/qwen3.5-plus-02-15",
            "qwen/qwen3.5-27b",
        ],
        description="Access 200+ models with one API key",
    ),
    "openai": ProviderConfig(
        id="openai",
        name="OpenAI",
        base_url="https://api.openai.com/v1/chat/completions",
        api_format="openai",
        auth_type="api_key",
        default_model="gpt-5.5",
        env_key="OPENAI_API_KEY",
        models=[
            "gpt-5.5",
            "gpt-5.4",
            "gpt-5.4-extended",
            "gpt-5.4-pro",
            "gpt-5.4-thinking",
            "gpt-4.1",
            "gpt-4.1-mini",
            "o3",
            "o4-mini",
        ],
        description="Direct OpenAI API access",
    ),
    "openai-codex": ProviderConfig(
        id="openai-codex",
        name="OpenAI Codex",
        base_url="https://chatgpt.com/backend-api/codex/responses",
        api_format="codex_responses",
        auth_type="oauth",
        default_model="gpt-5.5",
        models=["gpt-5.5", "gpt-5.4", "gpt-5.4-mini"],
        description="Use your ChatGPT subscription — no extra cost",
    ),
    "anthropic": ProviderConfig(
        id="anthropic",
        name="Anthropic",
        base_url="https://api.anthropic.com/v1/messages",
        api_format="anthropic",
        auth_type="api_key",
        default_model="claude-opus-4-6",
        env_key="ANTHROPIC_API_KEY",
        models=[
            "claude-opus-4-6",
            "claude-sonnet-4-6",
            "claude-sonnet-4-5-20250514",
            "claude-haiku-3-5-20241022",
        ],
        description="Direct Claude API access",
    ),
    "google": ProviderConfig(
        id="google",
        name="Google Gemini",
        base_url="https://generativelanguage.googleapis.com/v1beta/openai/chat/completions",
        api_format="openai",
        auth_type="api_key",
        default_model="gemini-2.5-pro",
        env_key="GOOGLE_AI_API_KEY",
        models=[
            "gemini-2.5-pro",
            "gemini-2.5-flash",
            "gemini-2.0-flash",
        ],
        description="Google AI API with free tier",
    ),
    "ollama": ProviderConfig(
        id="ollama",
        name="Ollama",
        base_url="http://localhost:11434/v1/chat/completions",
        api_format="openai",
        auth_type="none",
        default_model="llama3.1",
        models=["llama3.2", "llama3.1", "llama3", "codellama", "mistral", "mixtral", "phi3"],
        description="Run models locally — completely free",
    ),
    "deepseek": ProviderConfig(
        id="deepseek",
        name="DeepSeek",
        base_url="https://api.deepseek.com/v1/chat/completions",
        api_format="openai",
        auth_type="api_key",
        default_model="deepseek-chat",
        env_key="DEEPSEEK_API_KEY",
        models=[
            "deepseek-chat",
            "deepseek-coder",
            "deepseek-reasoner",
        ],
        description="High-performance open-source LLMs with strong coding capabilities",
    ),
}


def get_provider(provider_id: str) -> ProviderConfig | None:
    return PROVIDERS.get(provider_id)


# ── Live Codex (ChatGPT subscription) model discovery ──
# The chatgpt.com Codex backend gates models per-account. The static
# PROVIDERS["openai-codex"].models list can drift from what an account is
# actually entitled to, so we discover the live list at runtime and treat
# the static list only as an offline fallback.
CODEX_MODELS_URL = "https://chatgpt.com/backend-api/codex/models?client_version=1.0.0"
CODEX_MODELS_CACHE_TTL = 300
_codex_models_cache: dict = {"models": None, "fetched_at": 0.0}


def fetch_codex_models(auth_store=None, force: bool = False) -> list[dict] | None:
    """Fetch the Codex models the user's ChatGPT account is entitled to.

    Returns a list of dicts with keys ``id``, ``name``, ``context_length``,
    ``input_modalities``, ``description`` and ``supported_in_api``. Returns
    ``None`` on any failure (no token, network error, non-200) so callers can
    fall back to the static ``PROVIDERS["openai-codex"].models`` list. Results
    are cached for ``CODEX_MODELS_CACHE_TTL`` seconds.
    """
    import time

    now = time.time()
    cached = _codex_models_cache.get("models")
    if (
        not force
        and cached is not None
        and (now - _codex_models_cache.get("fetched_at", 0.0)) < CODEX_MODELS_CACHE_TTL
    ):
        return cached

    try:
        if auth_store is None:
            from ghost_auth_profiles import get_auth_store
            auth_store = get_auth_store()
        from ghost_oauth import ensure_fresh_token
        token = ensure_fresh_token(auth_store)
        if not token:
            return None
        profile = auth_store.get_provider_profile("openai-codex") or {}
        account_id = profile.get("account_id", "")
    except Exception:
        log.warning("Codex model discovery: could not obtain OAuth token", exc_info=True)
        return None

    headers = {"Authorization": f"Bearer {token}", "User-Agent": "Ghost/1.0"}
    if account_id:
        headers["ChatGPT-Account-Id"] = account_id

    try:
        resp = requests.get(CODEX_MODELS_URL, headers=headers, timeout=15)
        if resp.status_code != 200:
            log.warning("Codex model discovery returned HTTP %s", resp.status_code)
            return None
        data = resp.json()
    except Exception:
        log.warning("Codex model discovery request failed", exc_info=True)
        return None

    models = []
    for m in data.get("models", []) or []:
        slug = m.get("slug")
        if not slug:
            continue
        # Skip internal/hidden entries (e.g. codex-auto-review) — only list
        # models the picker should surface.
        visibility = m.get("visibility")
        if visibility and visibility != "list":
            continue
        models.append({
            "id": slug,
            "name": m.get("display_name", slug),
            "context_length": int(m.get("context_window") or 0),
            "input_modalities": m.get("input_modalities") or ["text"],
            "description": (m.get("description") or "")[:200],
            "supported_in_api": bool(m.get("supported_in_api", False)),
        })

    if not models:
        return None

    _codex_models_cache["models"] = models
    _codex_models_cache["fetched_at"] = now
    return models


def _normalize_ollama_model_name(name: str) -> str:
    cleaned = (name or "").strip()
    if not cleaned:
        return ""
    if cleaned.endswith(":latest"):
        return cleaned[:-7]
    return cleaned


def _get_ollama_available_models(timeout: int = 3) -> list[str]:
    try:
        resp = requests.get("http://localhost:11434/api/tags", timeout=timeout)
        resp.raise_for_status()
        tags = resp.json().get("models", [])
    except (requests.exceptions.RequestException, ValueError) as exc:
        log.debug("Ollama model probe unavailable: %s", exc)
        return []

    names: list[str] = []
    for item in tags:
        if not isinstance(item, dict):
            continue
        normalized = _normalize_ollama_model_name(str(item.get("name", "")))
        if normalized and normalized not in names:
            names.append(normalized)
    return names


def _select_ollama_model(requested_model: str | None, provider_cfg: ProviderConfig) -> str:
    installed = _get_ollama_available_models(timeout=3)
    requested = _normalize_ollama_model_name(requested_model or "")
    default = _normalize_ollama_model_name(provider_cfg.default_model)

    if requested and requested in installed:
        return requested
    if default and default in installed:
        if requested and requested != default:
            log.warning(
                "Ollama model '%s' not installed; switching to '%s'",
                requested,
                default,
            )
        return default

    preferred = ["llama3.2", "llama3.1", "llama3", "mistral", "phi3", "codellama", "mixtral"]
    for candidate in preferred:
        for available in installed:
            if available == candidate or available.startswith(candidate + ":") or available.startswith(candidate + "-"):
                if requested and requested != available:
                    log.warning(
                        "Ollama model '%s' not installed; switching to '%s'",
                        requested,
                        available,
                    )
                return available

    if installed:
        fallback = installed[0]
        if requested and requested != fallback:
            log.warning(
                "Ollama model '%s' not installed; switching to '%s'",
                requested,
                fallback,
            )
        return fallback

    return requested or default or provider_cfg.default_model


def validate_model_for_provider(provider_id: str, model: str) -> tuple[bool, str]:
    """Validate model identifier for a given provider.

    Returns (True, normalized_model) when valid, otherwise
    (False, reason_with_allowed_models).
    """
    prov = get_provider(provider_id)
    if not prov:
        return False, f"Unknown provider: {provider_id}"

    candidate = (model or "").strip()
    if not candidate:
        return False, f"Empty model for provider {provider_id}"

    allowed = set(prov.models or [])

    # Codex (ChatGPT subscription) gates models per-account. Accept anything
    # the live account is entitled to, falling back to the static list when
    # discovery is unavailable.
    if provider_id == "openai-codex":
        try:
            live = fetch_codex_models()
            if live:
                allowed |= {m["id"] for m in live}
        except Exception:
            pass

    # OpenRouter allows a broad catalog; accept explicit provider/model form
    # in addition to known curated defaults.
    if provider_id == "openrouter":
        if candidate in allowed:
            return True, candidate
        if "/" in candidate and " " not in candidate:
            return True, candidate
        return False, (
            "Invalid OpenRouter model. Use provider/model format or one of: "
            + ", ".join(sorted(allowed))
        )

    # Canonicalize accidental provider-prefixed direct-provider model IDs.
    # Example: provider=google, model=google/gemini-2.5-pro -> gemini-2.5-pro
    # Keep this fail-closed by only stripping known provider aliases.
    if "/" in candidate:
        prefix, suffix = candidate.split("/", 1)
        prefix_norm = prefix.strip().lower()
        alias_map = {
            "openai": {"openai"},
            "openai-codex": {"openai-codex", "codex", "openai"},
            "anthropic": {"anthropic", "claude"},
            "google": {"google", "gemini"},
            "ollama": {"ollama"},
            "deepseek": {"deepseek"},
        }
        allowed_prefixes = alias_map.get(provider_id, {provider_id})
        if prefix_norm in allowed_prefixes:
            candidate = suffix.strip()

    if candidate in allowed:
        return True, candidate

    return False, (
        f"Invalid model '{candidate}' for provider '{provider_id}'. "
        f"Allowed: {', '.join(prov.models or [])}"
    )


def run_provider_model_validation_selfcheck() -> dict:
    """Lightweight regression self-check for model/provider normalization paths."""
    checks: list[tuple[str, bool, str]] = []

    ok, value = validate_model_for_provider("google", "google/gemini-2.5-pro")
    checks.append(("google_prefixed_valid", ok and value == "gemini-2.5-pro", value))

    ok, value = validate_model_for_provider("google", "gemini-2.5-pro")
    checks.append(("google_native_valid", ok and value == "gemini-2.5-pro", value))

    ok, value = validate_model_for_provider("google", "google/gemini-flash-1.5")
    checks.append(("google_prefixed_invalid", (not ok) and "Invalid model" in value, value))

    ok, value = validate_model_for_provider("openrouter", "google/gemini-2.5-pro")
    checks.append(("openrouter_prefixed_valid", ok and value == "google/gemini-2.5-pro", value))

    passed = all(item[1] for item in checks)
    return {
        "passed": passed,
        "checks": [
            {"name": name, "passed": status, "detail": detail}
            for name, status, detail in checks
        ],
    }


def list_providers() -> list[dict]:
    return [
        {
            "id": p.id,
            "name": p.name,
            "auth_type": p.auth_type,
            "default_model": p.default_model,
            "models": p.models,
            "description": p.description,
        }
        for p in PROVIDERS.values()
    ]


# ═════════════════════════════════════════════════════════════════════
#  API FORMAT ADAPTERS
# ═════════════════════════════════════════════════════════════════════

def build_headers(provider: ProviderConfig, api_key: str = "") -> dict:
    """Build HTTP headers for a provider."""
    headers = {"Content-Type": "application/json"}

    if provider.api_format == "anthropic":
        headers["x-api-key"] = api_key
        headers["anthropic-version"] = "2023-06-01"
    elif provider.auth_type == "none":
        pass
    else:
        if api_key:
            headers["Authorization"] = f"Bearer {api_key}"

    if provider.id == "openrouter":
        headers["HTTP-Referer"] = "https://github.com/ghost-ai"
        headers["X-Title"] = "Ghost AI Agent"

    return headers


def adapt_request(provider: ProviderConfig, payload: dict) -> dict:
    """Convert an OpenAI-format payload to the provider's native format."""
    for t in payload.get("tools", []):
        fn = t.get("function", t)
        params = fn.get("parameters")
        if isinstance(params, dict):
            _fix_array_schemas(params)

    if provider.api_format == "openai":
        prepared = dict(payload)
        if provider.id == "ollama":
            selected_model = _select_ollama_model(prepared.get("model"), provider)
            prepared["model"] = selected_model
        return _adapt_openai(provider, prepared)
    elif provider.api_format == "anthropic":
        return _adapt_to_anthropic(payload)
    elif provider.api_format == "codex_responses":
        return _adapt_to_codex_responses(payload)
    return payload


def adapt_response(provider: ProviderConfig, data: dict) -> dict:
    """Convert a provider's response back to OpenAI format."""
    if provider.api_format == "anthropic":
        return _adapt_from_anthropic(data)
    elif provider.api_format == "codex_responses":
        return _adapt_from_codex_responses(data)
    return data


# ── OpenAI-compatible adapter ──

def _adapt_openai(provider: ProviderConfig, payload: dict) -> dict:
    """For OpenAI-compatible providers, just strip the provider prefix from model."""
    result = dict(payload)
    model = result.get("model", "")
    if provider.id == "google" and model.startswith("google/"):
        result["model"] = model.split("/", 1)[1]
    elif provider.id == "openai" and "/" in model:
        result["model"] = model.split("/", 1)[1]
    elif provider.id == "openai-codex" and "/" in model:
        result["model"] = model.split("/", 1)[1]
    elif provider.id == "ollama" and "/" in model:
        result["model"] = model.split("/", 1)[1]
    return result


# ── Codex Responses API adapter ──
# Endpoint: chatgpt.com/backend-api/codex/responses
# Uses OpenAI Responses API format (input/output) instead of Chat Completions (messages/choices).
# Requires store=false.

def _adapt_to_codex_responses(payload: dict) -> dict:
    """Convert OpenAI Chat Completions → Codex Responses API format."""
    messages = payload.get("messages", [])
    model = payload.get("model", "")
    if "/" in model:
        model = model.split("/", 1)[1]

    instructions = None
    input_items = []
    # The Responses API rejects empty call_id on function_call / function_call_output
    # (HTTP 400 "empty_string"). Tool calls reconstructed from history or produced by
    # providers that omit ids can arrive without one, which would break every codex
    # call after the first tool use. Synthesize stable ids and pair outputs to calls
    # by order when ids are missing so codex stays usable in multi-step tool loops.
    _synthetic_counter = 0
    _pending_call_ids: list[str] = []

    for msg in messages:
        role = msg.get("role", "")
        content = msg.get("content", "")

        if role in ("system", "developer"):
            if isinstance(content, str) and content.strip():
                if instructions:
                    instructions += "\n" + content
                else:
                    instructions = content
            continue

        if role == "user":
            if isinstance(content, str):
                input_items.append({
                    "role": "user",
                    "content": content,
                    "type": "message",
                })
            elif isinstance(content, list):
                resp_content = []
                for part in content:
                    ptype = part.get("type", "")
                    if ptype == "text":
                        resp_content.append({"type": "input_text", "text": part.get("text", "")})
                    elif ptype == "image_url":
                        url = part.get("image_url", {})
                        if isinstance(url, dict):
                            resp_content.append({"type": "input_image", "image_url": url.get("url", ""), "detail": url.get("detail", "auto")})
                        else:
                            resp_content.append({"type": "input_image", "image_url": str(url), "detail": "auto"})
                input_items.append({"role": "user", "content": resp_content, "type": "message"})

        elif role == "assistant":
            tool_calls = msg.get("tool_calls", [])
            if tool_calls:
                if isinstance(content, str) and content.strip():
                    input_items.append({
                        "role": "assistant",
                        "content": [{"type": "output_text", "text": content}],
                        "type": "message",
                    })
                for tc in tool_calls:
                    fn = tc.get("function", {})
                    cid = tc.get("id") or ""
                    if not cid:
                        cid = f"call_auto_{_synthetic_counter}"
                        _synthetic_counter += 1
                    _pending_call_ids.append(cid)
                    input_items.append({
                        "type": "function_call",
                        "call_id": cid,
                        "name": fn.get("name", ""),
                        "arguments": fn.get("arguments", "{}"),
                    })
            elif isinstance(content, str) and content.strip():
                input_items.append({
                    "role": "assistant",
                    "content": [{"type": "output_text", "text": content}],
                    "type": "message",
                })

        elif role == "tool":
            cid = msg.get("tool_call_id") or ""
            if cid:
                if cid in _pending_call_ids:
                    _pending_call_ids.remove(cid)
            elif _pending_call_ids:
                cid = _pending_call_ids.pop(0)
            else:
                cid = f"call_auto_{_synthetic_counter}"
                _synthetic_counter += 1
            input_items.append({
                "type": "function_call_output",
                "call_id": cid,
                "output": content if isinstance(content, str) else json.dumps(content),
            })

    valid_call_ids = {
        item["call_id"] for item in input_items
        if item.get("type") == "function_call" and item.get("call_id")
    }
    input_items = [
        item for item in input_items
        if item.get("type") != "function_call_output"
        or item.get("call_id", "") in valid_call_ids
    ]

    result = {
        "model": model,
        "input": input_items,
        "store": False,
        "stream": True,
    }

    result["instructions"] = instructions or "You are a helpful AI assistant."

    tool_choice = payload.get("tool_choice")
    if tool_choice:
        result["tool_choice"] = tool_choice

    tools = payload.get("tools")
    if tools:
        resp_tools = []
        for t in tools:
            fn = t.get("function", t)
            params = fn.get("parameters", {"type": "object", "properties": {}})
            _fix_array_schemas(params)
            resp_tools.append({
                "type": "function",
                "name": fn.get("name", ""),
                "description": fn.get("description", ""),
                "parameters": params,
                "strict": False,
            })
        result["tools"] = resp_tools

    return result


def _fix_array_schemas(schema: dict):
    """Recursively sanitize tool schemas for Codex Responses API.

    The Codex API enforces strict JSON Schema:
    - "type" must be a single string, not an array like ["object", "string"]
    - "type": "array" must have an "items" field
    """
    if not isinstance(schema, dict):
        return

    stype = schema.get("type")
    if isinstance(stype, list):
        non_null = [t for t in stype if t != "null"]
        pick = "string"
        if len(non_null) == 1:
            pick = non_null[0]
        elif "string" in non_null:
            pick = "string"
        elif "object" in non_null:
            pick = "object"
        elif "array" in non_null:
            pick = "array"
        elif non_null:
            pick = non_null[0]
        schema["type"] = pick

    if schema.get("type") == "array" and "items" not in schema:
        schema["items"] = {}

    for key in ("items", "additionalProperties"):
        if isinstance(schema.get(key), dict):
            _fix_array_schemas(schema[key])
    for prop in schema.get("properties", {}).values():
        if isinstance(prop, dict):
            _fix_array_schemas(prop)
    for variant in schema.get("anyOf", []) + schema.get("oneOf", []):
        if isinstance(variant, dict):
            _fix_array_schemas(variant)


def _adapt_from_codex_responses(data: dict) -> dict:
    """Convert Codex Responses API output → OpenAI Chat Completions format."""
    output_items = data.get("output", [])
    text_parts = []
    tool_calls = []
    tc_index = 0

    for item in output_items:
        item_type = item.get("type", "")

        if item_type == "message" and item.get("role") == "assistant":
            for block in item.get("content", []):
                btype = block.get("type", "")
                if btype == "output_text":
                    text_parts.append(block.get("text", ""))
                elif btype == "refusal":
                    text_parts.append(f"[Refusal: {block.get('refusal', '')}]")

        elif item_type == "function_call":
            tool_calls.append({
                "id": item.get("call_id", item.get("id", "")),
                "type": "function",
                "function": {
                    "name": item.get("name", ""),
                    "arguments": item.get("arguments", "{}"),
                },
                "index": tc_index,
            })
            tc_index += 1

    message = {
        "role": "assistant",
        "content": "\n".join(text_parts) if text_parts else None,
    }
    if tool_calls:
        message["tool_calls"] = tool_calls

    finish_reason = "stop"
    status = data.get("status", "completed")
    if tool_calls:
        finish_reason = "tool_calls"
    elif status == "incomplete":
        finish_reason = "length"

    return {
        "choices": [{
            "message": message,
            "finish_reason": finish_reason,
            "index": 0,
        }],
        "id": data.get("id", ""),
        "model": data.get("model", ""),
        "usage": {
            "prompt_tokens": data.get("usage", {}).get("input_tokens", 0),
            "completion_tokens": data.get("usage", {}).get("output_tokens", 0),
        },
    }


# ── Anthropic Messages adapter ──

def _adapt_to_anthropic(payload: dict) -> dict:
    """Convert OpenAI Chat Completions format → Anthropic Messages format."""
    messages = payload.get("messages", [])
    system_text = ""
    anthropic_messages = []

    for msg in messages:
        role = msg.get("role", "")
        content = msg.get("content", "")

        if role == "system":
            if isinstance(content, str):
                system_text += content + "\n"
            continue

        if role == "assistant":
            ant_content = []
            if isinstance(content, str) and content:
                ant_content.append({"type": "text", "text": content})
            tool_calls = msg.get("tool_calls", [])
            for tc in tool_calls:
                fn = tc.get("function", {})
                try:
                    args = json.loads(fn.get("arguments", "{}"))
                except (json.JSONDecodeError, TypeError):
                    args = {}
                ant_content.append({
                    "type": "tool_use",
                    "id": tc.get("id", ""),
                    "name": fn.get("name", ""),
                    "input": args,
                })
            if ant_content:
                anthropic_messages.append({"role": "assistant", "content": ant_content})

        elif role == "tool":
            anthropic_messages.append({
                "role": "user",
                "content": [{
                    "type": "tool_result",
                    "tool_use_id": msg.get("tool_call_id", ""),
                    "content": content if isinstance(content, str) else json.dumps(content),
                }],
            })

        elif role == "user":
            if isinstance(content, list):
                anthropic_messages.append({"role": "user", "content": content})
            else:
                anthropic_messages.append({
                    "role": "user",
                    "content": [{"type": "text", "text": str(content)}],
                })

    model = payload.get("model", "")
    if model.startswith("anthropic/"):
        model = model.split("/", 1)[1]

    result = {
        "model": model,
        "messages": anthropic_messages,
        "max_tokens": payload.get("max_tokens", 4096),
    }

    if system_text.strip():
        result["system"] = system_text.strip()

    if payload.get("temperature") is not None:
        result["temperature"] = payload["temperature"]

    # Claude 4.6+ API features: Context Compaction and Effort Controls
    effort = payload.get("effort")
    if effort:
        result["effort"] = effort

    context_compaction = payload.get("context_window_compression")
    if context_compaction:
        result["context_window_compression"] = context_compaction

    tools = payload.get("tools")
    if tools:
        ant_tools = []
        for t in tools:
            fn = t.get("function", t)
            ant_tools.append({
                "name": fn.get("name", ""),
                "description": fn.get("description", ""),
                "input_schema": fn.get("parameters", {"type": "object", "properties": {}}),
            })
        result["tools"] = ant_tools

    return result


def _adapt_from_anthropic(data: dict) -> dict:
    """Convert Anthropic Messages response → OpenAI Chat Completions format."""
    content_blocks = data.get("content", [])
    text_parts = []
    tool_calls = []

    for block in content_blocks:
        btype = block.get("type", "")
        if btype == "text":
            text_parts.append(block.get("text", ""))
        elif btype == "tool_use":
            tool_calls.append({
                "id": block.get("id", ""),
                "type": "function",
                "function": {
                    "name": block.get("name", ""),
                    "arguments": json.dumps(block.get("input", {})),
                },
            })

    message = {
        "role": "assistant",
        "content": "\n".join(text_parts) if text_parts else None,
    }
    if tool_calls:
        message["tool_calls"] = tool_calls

    stop_reason = data.get("stop_reason", "end_turn")
    finish_map = {"end_turn": "stop", "tool_use": "tool_calls", "max_tokens": "length"}

    return {
        "choices": [{
            "message": message,
            "finish_reason": finish_map.get(stop_reason, "stop"),
            "index": 0,
        }],
        "model": data.get("model", ""),
        "usage": {
            "prompt_tokens": data.get("usage", {}).get("input_tokens", 0),
            "completion_tokens": data.get("usage", {}).get("output_tokens", 0),
        },
    }


# ═════════════════════════════════════════════════════════════════════
#  CODEX SSE STREAM PARSER
# ═════════════════════════════════════════════════════════════════════

def parse_codex_sse_response(response, on_token=None) -> dict:
    """Parse a streaming SSE response from the Codex Responses API.
    Collects events and returns the final completed response dict.

    If *on_token* is provided it is called with each content-delta string
    as it arrives (from ``response.output_text.delta`` events).
    """
    final_response = None
    # The Codex backend returns an empty ``output`` array in the
    # ``response.completed`` event when ``store=false`` (which we always send).
    # The actual content/tool-calls only arrive via streamed item events, so we
    # accumulate them here and reconstruct ``output`` if the completed event is
    # empty. Applies to message text AND function_call (tool) items.
    collected_items: list = []
    text_buffer: list = []
    for line in response.iter_lines():
        if not line:
            continue
        if isinstance(line, bytes):
            line = line.decode("utf-8", errors="replace")
        if not line.startswith("data: "):
            continue
        data_str = line[6:]
        if data_str == "[DONE]":
            break
        try:
            event = json.loads(data_str)
        except (json.JSONDecodeError, ValueError):
            continue
        event_type = event.get("type", "")
        if event_type == "response.output_text.delta":
            delta = event.get("delta", "")
            if delta:
                text_buffer.append(delta)
                if on_token:
                    try:
                        on_token(delta)
                    except Exception:
                        pass
        elif event_type == "response.output_item.done":
            item = event.get("item")
            if isinstance(item, dict):
                collected_items.append(item)
        if event_type == "response.completed":
            final_response = event.get("response", event)
            break
        elif event_type == "response.failed":
            error = event.get("response", {}).get("error", {})
            raise RuntimeError(
                f"Codex API error: {error.get('message', 'unknown')} "
                f"(code={error.get('code', '')})"
            )

    if final_response is None:
        if not collected_items and not text_buffer:
            raise RuntimeError("Codex stream ended without a response.completed event")
        final_response = {"status": "completed"}

    # Backfill output from streamed items when the completed event omitted it.
    if not final_response.get("output"):
        if collected_items:
            final_response["output"] = collected_items
        elif text_buffer:
            final_response["output"] = [{
                "type": "message",
                "role": "assistant",
                "content": [{"type": "output_text", "text": "".join(text_buffer)}],
            }]

    return final_response


# ═════════════════════════════════════════════════════════════════════
#  CONNECTION TESTING
# ═════════════════════════════════════════════════════════════════════

def test_provider_connection(provider_id: str, api_key: str = "") -> dict:
    """Test if a provider is reachable and the key is valid."""
    provider = get_provider(provider_id)
    if not provider:
        return {"ok": False, "error": f"Unknown provider: {provider_id}"}

    try:
        if provider.id == "ollama":
            resp = requests.get("http://localhost:11434/api/tags", timeout=5)
            if resp.ok:
                tags = resp.json().get("models", [])
                model_names = [m.get("name", "") for m in tags]
                return {"ok": True, "models": model_names[:20], "count": len(tags)}
            return {"ok": False, "error": "Ollama not running at localhost:11434"}

        headers = build_headers(provider, api_key)
        test_payload = {
            "model": provider.default_model,
            "messages": [{"role": "user", "content": "Hi"}],
            "max_tokens": 5,
        }
        adapted = adapt_request(provider, test_payload)

        is_stream = provider.api_format == "codex_responses"
        resp = requests.post(provider.base_url, json=adapted,
                             headers=headers, timeout=30,
                             stream=is_stream)

        if resp.status_code == 401:
            return {"ok": False, "error": "Invalid or expired credentials (401 Unauthorized)"}
        if resp.status_code == 403:
            return {"ok": False, "error": "Access denied (403 Forbidden)"}
        if resp.status_code == 429:
            return {"ok": True, "status": 429, "note": "Connected but rate-limited (429)"}

        resp.raise_for_status()

        if is_stream:
            raw = parse_codex_sse_response(resp)
            return {"ok": True, "status": 200,
                    "model": raw.get("model", "")}

        return {"ok": True, "status": resp.status_code}

    except requests.exceptions.ConnectionError:
        return {"ok": False, "error": f"Cannot connect to {provider.name}"}
    except requests.exceptions.Timeout:
        return {"ok": False, "error": f"{provider.name} timed out"}
    except Exception as e:
        return {"ok": False, "error": str(e)[:200]}
