"""
Ghost Config Tool — LLM-accessible runtime configuration management.

Allows Ghost to read and safely modify its own config at runtime.
Safety: blocklist for dangerous keys (auth, secrets), approval via action items
for critical changes, hot-reload signaling.

Tools: config_get, config_patch, config_schema
"""

import copy
import json
import logging
import shutil
from datetime import datetime
from pathlib import Path

log = logging.getLogger("quinely.config_tool")

GHOST_HOME = Path.home() / ".ghost"
CONFIG_FILE = GHOST_HOME / "config.json"
CONFIG_BACKUP_DIR = GHOST_HOME / "config_backups"

BLOCKED_KEYS = frozenset({
    "api_key",
    "firecrawl_api_key",
    "hf_token",
    "google_client_id",
    "google_client_secret",
    "google_refresh_token",
    "cloud_providers",
})

SENSITIVE_KEYS = frozenset({
    "allowed_commands",
    "allowed_roots",
    "blocked_commands",
    "strict_tool_registration",
    "enable_future_features",
})

PROTECTED_ALWAYS_ON_KEYS = frozenset({
    "enable_future_features",
})

PROTECTED_OVERRIDE_TOKEN = "ALLOW_PROTECTED_CONFIG_WRITE"

_HARDENING_VALUES = {
    "strict_tool_registration": True,
}


def _is_hardening_change(key, value):
    """Return True when a sensitive-key change makes the system MORE secure."""
    if key in _HARDENING_VALUES:
        return value == _HARDENING_VALUES[key]
    if key == "blocked_commands" and isinstance(value, list):
        return True
    if key == "allowed_commands" and isinstance(value, list):
        from ghost_tools import CORE_COMMANDS
        missing = [c for c in CORE_COMMANDS if c not in value]
        if missing:
            return False
    return True


def _reject_protected_always_on_change(changes: dict):
    """Reject attempts to disable protected always-on safety controls."""
    if not isinstance(changes, dict):
        return None
    if "enable_future_features" not in changes:
        return None

    requested = changes.get("enable_future_features")
    if requested is True:
        return None

    token = str(changes.get("protected_config_override_token", "")).strip()
    if token == PROTECTED_OVERRIDE_TOKEN:
        log.warning(
            "Rejected override token attempt for protected key enable_future_features"
        )

    return {
        "ok": False,
        "error": (
            "Rejected insecure config change: enable_future_features cannot be false. "
            "Future Features queue is security-critical and always enabled at runtime."
        ),
        "actionable_next_step": (
            "Remove enable_future_features=false from your patch. "
            "This key is protected and cannot be disabled via config_patch."
        ),
    }


def _backup_config() -> str | None:
    """Snapshot the current config before modification (rollback safety)."""
    if not CONFIG_FILE.exists():
        return None
    CONFIG_BACKUP_DIR.mkdir(parents=True, exist_ok=True)
    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    backup_path = CONFIG_BACKUP_DIR / f"config_{ts}.json"
    shutil.copy2(CONFIG_FILE, backup_path)
    backups = sorted(CONFIG_BACKUP_DIR.glob("config_*.json"),
                     key=lambda p: p.stat().st_mtime)
    while len(backups) > 20:
        backups.pop(0).unlink()
    log.info("Config backup created: %s", backup_path)
    return str(backup_path)

CONFIG_SCHEMA = {
    "model": {
        "type": "string",
        "description": "Primary LLM model (e.g. google/gemini-2.0-flash-001)",
    },
    "primary_provider": {
        "type": "string",
        "description": "Primary LLM provider: openrouter, openai, openai-codex, anthropic, google, ollama",
    },
    "fallback_models": {
        "type": "array",
        "description": "Fallback models in priority order",
    },
    "poll_interval": {
        "type": "number",
        "description": "Main loop poll interval in seconds",
    },
    "tool_loop_max_steps": {
        "type": "integer",
        "description": "Max steps per tool loop run (1-500)",
    },
    "enable_memory_db": {"type": "boolean", "description": "Enable persistent memory"},
    "enable_plugins": {"type": "boolean", "description": "Enable plugin system"},
    "enable_skills": {"type": "boolean", "description": "Enable skills system"},
    "enable_browser_tools": {"type": "boolean", "description": "Enable browser automation"},
    "enable_browser_use": {"type": "boolean", "description": "Enable AI-native browser automation (browser-use)"},
    "enable_channels": {"type": "boolean", "description": "Enable messaging channels (Telegram, Discord, etc.)"},
    "enable_cron": {"type": "boolean", "description": "Enable cron scheduler"},
    "enable_evolve": {"type": "boolean", "description": "Enable self-evolution"},
    "enable_future_features": {"type": "boolean", "description": "Enable autonomous feature implementation (evolve loop)"},
    "enable_goals": {"type": "boolean", "description": "Enable Goal Engine — persistent multi-step user goals with autonomous execution (goal_executor cron)"},
    "enable_integrations": {"type": "boolean", "description": "Enable Google/Grok integrations"},
    "enable_growth": {"type": "boolean", "description": "Enable autonomy growth routines"},
    "enable_web_search": {"type": "boolean", "description": "Enable web search tool"},
    "enable_web_fetch": {"type": "boolean", "description": "Enable web fetch tool"},
    "enable_nodes": {"type": "boolean", "description": "Enable GhostNodes AI plugin ecosystem (local image gen, video, audio, vision, 3D)"},
    "hf_token": {"type": "string", "description": "HuggingFace API token for downloading gated models (FLUX, etc.). Get yours at https://huggingface.co/settings/tokens"},
    "hf_oauth_client_id": {"type": "string", "description": "HuggingFace OAuth app client ID for device-flow login. Create a public app at https://huggingface.co/settings/applications/new"},
    "gpu_memory_budget_gb": {"type": "number", "description": "GPU VRAM budget in GB for GhostNodes models (0 = auto-detect 85% of total)"},
    "media_disk_budget_mb": {"type": "integer", "description": "Max disk space for generated media in MB (default 5000)"},
    "disabled_nodes": {"type": "array", "description": "List of node names to keep disabled"},
    "enable_image_gen": {"type": "boolean", "description": "Enable image generation"},
    "enable_vision": {"type": "boolean", "description": "Enable image analysis/vision"},
    "enable_tts": {"type": "boolean", "description": "Enable text-to-speech"},
    "enable_voice": {"type": "boolean", "description": "Enable Voice Wake + Talk Mode (always-on speech)"},
    "voice_wake_words": {
        "type": "array",
        "description": "Wake words for Voice Wake mode (default: ['ghost', 'hey ghost'])",
    },
    "voice_stt_provider": {
        "type": "string",
        "description": "Speech-to-text provider: auto, whisper, groq, vosk",
    },
    "voice_silence_threshold": {
        "type": "number",
        "description": "Audio energy threshold for silence detection (0.001-1.0, default 0.02)",
    },
    "voice_silence_duration": {
        "type": "number",
        "description": "Seconds of silence before ending capture (0.5-10.0, default 2.0)",
    },
    "voice_chime": {"type": "boolean", "description": "Play chime on wake word detection"},
    "enable_security_audit": {"type": "boolean", "description": "Enable security audit tools"},
    "enable_session_memory": {"type": "boolean", "description": "Enable auto-save session memory"},
    "session_auto_cleanup": {
        "type": "boolean",
        "description": "Automatically clean up old session files (default: true)",
    },
    "session_max_count": {
        "type": "integer",
        "description": "Maximum number of session files to keep (default: 100, min: 10)",
    },
    "session_max_age_days": {
        "type": "integer",
        "description": "Delete sessions older than this many days (default: 30, min: 1)",
    },
    "session_disk_budget_mb": {
        "type": "integer",
        "description": "Maximum disk space for sessions in MB (default: 500, min: 50)",
    },
    "anthropic_effort": {
        "type": "string",
        "enum": ["low", "medium", "high"],
        "description": "Claude 4.6+ reasoning effort level: 'low' (fastest), 'medium' (balanced), 'high' (best quality). Only applies to direct Anthropic API.",
    },
    "anthropic_context_compaction": {
        "type": "boolean",
        "description": "Enable Claude 4.6+ context window compression to automatically manage long contexts. Only applies to direct Anthropic API.",
    },
    "anthropic_context_compaction_ratio": {
        "type": "number",
        "description": "Compression ratio for Claude 4.6+ context window (0.0-1.0, default 0.5). Higher = more aggressive compression. Only applies when anthropic_context_compaction is enabled.",
    },
    "strict_tool_registration": {"type": "boolean", "description": "Security: True prevents tool shadowing by plugins (CVE-2025-59536/21852 defense)"},
    "max_feed_items": {"type": "integer", "description": "Max items in feed (10-500)"},
    "rate_limit_seconds": {"type": "number", "description": "Rate limit between actions"},
    "web_fetch_max_chars": {"type": "integer", "description": "Max characters to keep from web fetch results (default: 50000)"},
    "web_fetch_timeout_seconds": {"type": "integer", "description": "Timeout for web fetch requests in seconds (default: 30)"},
    "max_shell_sessions": {"type": "integer", "description": "Max concurrent shell sessions (default: 5)"},
    "max_background_processes": {"type": "integer", "description": "Max background processes allowed (default: 10)"},
    "growth_schedules": {"type": "object", "description": "Override cron schedules for growth routines"},
    "dashboard_port": {"type": "integer", "description": "Dashboard HTTP port (1024-65535, default: 3333)"},
    "disabled_skills": {"type": "array", "description": "List of skill names to disable"},
    "tool_models": {
        "type": "object",
        "description": "Override model IDs used by tools (image gen, vision, web search, TTS, embeddings)",
        "properties": {
            "image_gen_openrouter": "google/gemini-3-pro-image-preview",
            "image_gen_gemini": "gemini-3-pro-image-preview",
            "image_gen_openai": "gpt-image-1",
            "vision_openai": "gpt-4o",
            "vision_openrouter": "openai/gpt-4o",
            "vision_gemini": "gemini-2.5-flash",
            "vision_anthropic": "claude-sonnet-4-20250514",
            "vision_ollama": "llava",
            "web_search_perplexity": "perplexity/sonar-pro",
            "web_search_perplexity_direct": "sonar-pro",
            "web_search_grok": "grok-3-fast",
            "web_search_openai": "gpt-4.1-mini",
            "web_search_gemini": "gemini-2.5-flash",
            "grok_openrouter": "x-ai/grok-4-fast",
            "tts_openai": "tts-1",
            "tts_elevenlabs": "eleven_multilingual_v2",
            "embedding_openrouter": "openai/text-embedding-3-small",
            "embedding_gemini": "text-embedding-004",
            "embedding_ollama": "nomic-embed-text",
        },
    },
    "skill_model_aliases": {
        "type": "object",
        "description": "Model aliases for per-skill model overrides. Keys are alias names, values are provider/model strings. Built-in aliases: cheap, fast, capable, smart, vision, code. User-defined aliases override defaults.",
        "additionalProperties": {"type": "string"},
    },
    "provider_chains": {
        "type": "object",
        "description": "Configurable provider fallback order for each capability. Each key is a capability name, value is an ordered array of provider IDs.",
        "properties": {
            "web_search": {"type": "array", "items": {"type": "string"}, "description": "Web search provider fallback order"},
            "image_gen": {"type": "array", "items": {"type": "string"}, "description": "Image generation provider fallback order"},
            "vision": {"type": "array", "items": {"type": "string"}, "description": "Vision/image analysis provider fallback order"},
            "tts": {"type": "array", "items": {"type": "string"}, "description": "Text-to-speech provider fallback order"},
            "embeddings": {"type": "array", "items": {"type": "string"}, "description": "Embedding provider fallback order"},
            "voice_stt": {"type": "array", "items": {"type": "string"}, "description": "Voice speech-to-text provider fallback order"},
        },
    },
}


TOOL_MODEL_DEFAULTS = {
    "image_gen_openrouter": "google/gemini-3-pro-image-preview",
    "image_gen_gemini": "gemini-3-pro-image-preview",
    "image_gen_openai": "gpt-image-1",
    "vision_openai": "gpt-4o",
    "vision_openrouter": "openai/gpt-4o",
    "vision_gemini": "gemini-2.5-flash",
    "vision_anthropic": "claude-sonnet-4-20250514",
    "vision_ollama": "llava",
    "web_search_perplexity": "perplexity/sonar-pro",
    "web_search_perplexity_direct": "sonar-pro",
    "web_search_grok": "grok-3-fast",
    "web_search_openai": "gpt-4.1-mini",
    "web_search_gemini": "gemini-2.5-flash",
    "grok_openrouter": "x-ai/grok-4-fast",
    "tts_openai": "tts-1",
    "tts_elevenlabs": "eleven_multilingual_v2",
    "embedding_openrouter": "openai/text-embedding-3-small",
    "embedding_gemini": "text-embedding-004",
    "embedding_ollama": "nomic-embed-text",
}


def get_tool_model(key: str, cfg: dict | None = None) -> str:
    """Resolve a tool model from config with built-in default fallback.

    Reads from cfg["tool_models"][key], falling back to TOOL_MODEL_DEFAULTS[key].
    """
    if cfg:
        return cfg.get("tool_models", {}).get(key, TOOL_MODEL_DEFAULTS.get(key, ""))
    return TOOL_MODEL_DEFAULTS.get(key, "")


def _load_config() -> dict:
    if CONFIG_FILE.exists():
        try:
            return json.loads(CONFIG_FILE.read_text(encoding="utf-8"))
        except Exception:
            pass
    return {}


def _save_config(cfg: dict):
    CONFIG_FILE.write_text(json.dumps(cfg, indent=2), encoding="utf-8")


def _sanitize_for_display(cfg: dict) -> dict:
    """Remove sensitive values from config for display."""
    sanitized = {}
    for k, v in cfg.items():
        if k in BLOCKED_KEYS:
            if isinstance(v, str) and v:
                sanitized[k] = v[:4] + "..." + v[-4:] if len(v) > 8 else "***"
            else:
                sanitized[k] = "(set)" if v else "(empty)"
        else:
            sanitized[k] = v
    return sanitized


def _validate_dangerous_command_policy(policy: dict) -> tuple[bool, str]:
    """Validate dangerous_command_policy schema."""
    if not isinstance(policy, dict):
        return False, "dangerous_command_policy must be an object"
    
    for section_name, section in policy.items():
        if not isinstance(section, dict):
            return False, f"dangerous_command_policy.{section_name} must be an object"
        
        # Validate known fields
        allowed_fields = {"allow", "require_workspace", "deny_flags", "allow_subcommands", "safe_shell_patterns"}
        for field in section:
            if field not in allowed_fields:
                return False, f"dangerous_command_policy.{section_name}.{field} is not a recognized field"
        
        # Validate types
        if "allow" in section and not isinstance(section["allow"], bool):
            return False, f"dangerous_command_policy.{section_name}.allow must be boolean"
        if "require_workspace" in section and not isinstance(section["require_workspace"], bool):
            return False, f"dangerous_command_policy.{section_name}.require_workspace must be boolean"
        if "deny_flags" in section and not isinstance(section["deny_flags"], list):
            return False, f"dangerous_command_policy.{section_name}.deny_flags must be an array"
        if "allow_subcommands" in section and not isinstance(section["allow_subcommands"], list):
            return False, f"dangerous_command_policy.{section_name}.allow_subcommands must be an array"
        if "safe_shell_patterns" in section and not isinstance(section["safe_shell_patterns"], list):
            return False, f"dangerous_command_policy.{section_name}.safe_shell_patterns must be an array"
    
    return True, ""


def _validate_patch(patch: dict) -> tuple[bool, str]:
    """Validate a config patch. Returns (ok, error_message)."""
    for key in patch:
        if key in BLOCKED_KEYS:
            return False, f"Cannot modify blocked key: {key}"

    if "enable_future_features" in patch and patch["enable_future_features"] is False:
        return False, (
            "enable_future_features is security-protected and cannot be disabled at runtime "
            "because autonomous self-repair and security patching depend on it"
        )

    if "tool_loop_max_steps" in patch:
        val = patch["tool_loop_max_steps"]
        if not isinstance(val, int) or val < 1 or val > 500:
            return False, "tool_loop_max_steps must be 1-500"

    if "max_feed_items" in patch:
        val = patch["max_feed_items"]
        if not isinstance(val, int) or val < 10 or val > 500:
            return False, "max_feed_items must be 10-500"

    if "dashboard_port" in patch:
        val = patch["dashboard_port"]
        if not isinstance(val, int) or val < 1024 or val > 65535:
            return False, "dashboard_port must be 1024-65535"

    if "web_fetch_max_chars" in patch:
        val = patch["web_fetch_max_chars"]
        if not isinstance(val, int) or val < 1000 or val > 200000:
            return False, "web_fetch_max_chars must be 1000-200000"

    if "web_fetch_timeout_seconds" in patch:
        val = patch["web_fetch_timeout_seconds"]
        if not isinstance(val, int) or val < 5 or val > 120:
            return False, "web_fetch_timeout_seconds must be 5-120"

    if "max_shell_sessions" in patch:
        val = patch["max_shell_sessions"]
        if not isinstance(val, int) or val < 1 or val > 20:
            return False, "max_shell_sessions must be 1-20"

    if "max_background_processes" in patch:
        val = patch["max_background_processes"]
        if not isinstance(val, int) or val < 1 or val > 50:
            return False, "max_background_processes must be 1-50"

    if "anthropic_effort" in patch:
        val = patch["anthropic_effort"]
        if val not in ("low", "medium", "high"):
            return False, "anthropic_effort must be 'low', 'medium', or 'high'"

    if "anthropic_context_compaction_ratio" in patch:
        val = patch["anthropic_context_compaction_ratio"]
        if not isinstance(val, (int, float)) or val < 0 or val > 1:
            return False, "anthropic_context_compaction_ratio must be 0.0-1.0"

    if "provider_chains" in patch:
        val = patch["provider_chains"]
        if not isinstance(val, dict):
            return False, "provider_chains must be an object"
        VALID_PROVIDERS = {
            "web_search": {"perplexity_openrouter", "perplexity_direct", "grok", "openai", "brave", "gemini"},
            "image_gen": {"openrouter", "google", "openai"},
            "vision": {"openai", "openrouter", "google", "anthropic", "ollama"},
            "tts": {"edge", "openai", "elevenlabs"},
            "embeddings": {"openrouter", "gemini", "ollama"},
            "voice_stt": {"moonshine", "openrouter", "whisper", "groq", "vosk"},
        }
        for chain_key, chain_val in val.items():
            if chain_key not in VALID_PROVIDERS:
                return False, f"Unknown provider chain: {chain_key}"
            if not isinstance(chain_val, list):
                return False, f"provider_chains.{chain_key} must be an array"
            for pid in chain_val:
                if pid not in VALID_PROVIDERS[chain_key]:
                    return False, f"Unknown provider '{pid}' in {chain_key} chain. Valid: {', '.join(sorted(VALID_PROVIDERS[chain_key]))}"

    if "dangerous_command_policy" in patch:
        ok, err = _validate_dangerous_command_policy(patch["dangerous_command_policy"])
        if not ok:
            return False, err
    
    # Session maintenance config validation
    if "session_max_count" in patch:
        val = patch["session_max_count"]
        if not isinstance(val, int) or val < 10 or val > 10000:
            return False, "session_max_count must be 10-10000"
    
    if "session_max_age_days" in patch:
        val = patch["session_max_age_days"]
        if not isinstance(val, int) or val < 1 or val > 365:
            return False, "session_max_age_days must be 1-365"
    
    if "session_disk_budget_mb" in patch:
        val = patch["session_disk_budget_mb"]
        if not isinstance(val, int) or val < 50 or val > 10000:
            return False, "session_disk_budget_mb must be 50-10000"
    
    # GhostNodes config validation
    if "gpu_memory_budget_gb" in patch:
        val = patch["gpu_memory_budget_gb"]
        if not isinstance(val, (int, float)) or val < 0 or val > 128:
            return False, "gpu_memory_budget_gb must be 0-128 (0 = auto-detect)"

    if "media_disk_budget_mb" in patch:
        val = patch["media_disk_budget_mb"]
        if not isinstance(val, int) or val < 100 or val > 100000:
            return False, "media_disk_budget_mb must be 100-100000"

    if "disabled_nodes" in patch:
        val = patch["disabled_nodes"]
        if not isinstance(val, list) or not all(isinstance(n, str) for n in val):
            return False, "disabled_nodes must be an array of node name strings"

    # When enabling dangerous interpreters, require secure policy minimums
    if "enable_dangerous_interpreters" in patch:
        enabling = patch["enable_dangerous_interpreters"]
        if enabling is True:
            policy = patch.get("dangerous_command_policy") or {}
            py_policy = policy.get("python") or {}
            
            # Require policy presence with secure defaults
            if py_policy.get("allow", False):
                # If python is allowed, require workspace and deny_flags
                if not py_policy.get("require_workspace", True):
                    return False, "Enabling dangerous interpreters with python.allow=true requires require_workspace=true"
                deny_flags = py_policy.get("deny_flags", [])
                if "-c" not in deny_flags:
                    return False, "Enabling dangerous interpreters with python.allow=true requires deny_flags to include '-c'"

    return True, ""


def build_config_tools(cfg=None):
    """Build LLM-callable config management tools."""

    def config_get_exec(key=None):
        current = _load_config()
        sanitized = _sanitize_for_display(current)

        if key:
            if key in BLOCKED_KEYS:
                return f"Key '{key}' is blocked for security"
            val = sanitized.get(key)
            if val is None:
                return f"Key '{key}' not found in config"
            return json.dumps({key: val}, indent=2)

        return json.dumps(sanitized, indent=2)

    def config_patch_exec(updates, **kwargs):
        if not isinstance(updates, dict):
            return "Error: updates must be a JSON object"

        protected_rejection = _reject_protected_always_on_change(updates)
        if protected_rejection:
            log.warning(
                "Rejected protected config toggle attempt for enable_future_features: %r",
                updates.get("enable_future_features"),
            )
            return json.dumps(protected_rejection, ensure_ascii=False)

        ok, err = _validate_patch(updates)
        if not ok:
            return f"Validation error: {err}"

        has_sensitive = any(k in SENSITIVE_KEYS for k in updates)
        if has_sensitive:
            sensitive_keys = [k for k in updates if k in SENSITIVE_KEYS]
            weakening = [
                k for k in sensitive_keys
                if not _is_hardening_change(k, updates[k])
            ]
            if weakening:
                return (
                    f"These changes would WEAKEN security: {weakening}. "
                    "This requires user approval. Use add_action_item to propose the change."
                )
            log.info("Allowing security-hardening config changes: %s", sensitive_keys)

        backup_path = _backup_config()
        current = _load_config()
        old_values = {k: current.get(k) for k in updates}
        sanitized_updates = dict(updates)
        sanitized_updates.pop("protected_config_override_token", None)
        current.update(sanitized_updates)
        _save_config(current)

        changes = []
        for k, new_val in updates.items():
            old_val = old_values.get(k, "(unset)")
            changes.append(f"  {k}: {old_val} -> {new_val}")

        backup_note = f"\nBackup saved: {backup_path}" if backup_path else ""
        return (
            f"Config updated ({len(updates)} key(s)):\n"
            + "\n".join(changes)
            + backup_note
            + "\n\nNote: Some changes take effect on next restart."
        )

    def config_schema_exec():
        return json.dumps(CONFIG_SCHEMA, indent=2)

    return [
        {
            "name": "config_get",
            "description": (
                "Read Ghost's current configuration. Returns sanitized config "
                "(secrets are masked). Optionally get a specific key."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "key": {
                        "type": "string",
                        "description": "Specific config key to read. Leave empty for all.",
                    },
                },
            },
            "execute": config_get_exec,
        },
        {
            "name": "config_patch",
            "description": (
                "Update Ghost's configuration. Merges partial updates into existing config. "
                "Auth/secret keys are blocked. Security-hardening changes (e.g. enabling "
                "strict_tool_registration, disabling evolve_auto_approve) are ALLOWED — "
                "only weakening changes require user approval. "
                "Every patch creates an automatic config backup for rollback safety. "
                "Some changes take effect on restart."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "updates": {
                        "type": "object",
                        "description": "Key-value pairs to update in config",
                    },
                },
                "required": ["updates"],
            },
            "execute": config_patch_exec,
        },
        {
            "name": "config_schema",
            "description": "Show the config schema with descriptions of all configurable keys.",
            "parameters": {
                "type": "object",
                "properties": {},
            },
            "execute": config_schema_exec,
        },
    ]
