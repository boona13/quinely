#!/usr/bin/env python3
"""
GHOST -- Autonomous AI Agent

A self-evolving AI agent with a web dashboard.
Chat with Ghost through the dashboard at http://localhost:3333

Usage:
    python ghost.py                          # Start daemon + dashboard
    python ghost.py --api-key sk-or-...      # With explicit key
    python ghost.py --model openai/gpt-4o    # Use specific model
    python ghost.py log                      # Show action history
    python ghost.py status                   # Show stats
    python ghost.py context                  # What Ghost thinks you're doing

Requirements: pip install requests
Set OPENROUTER_API_KEY env var or pass --api-key.
"""

import os, sys, json, re, time, subprocess, threading, signal, base64, platform, secrets
import logging
from collections import defaultdict
from collections.abc import Callable
import requests
from pathlib import Path
from datetime import datetime, timedelta
from hashlib import md5

import ghost_platform

from ghost_loop import ToolLoopEngine, ToolRegistry
from ghost_tools import build_default_tools, make_notify, get_user_projects_dir, DEFAULT_ALLOWED_COMMANDS, set_shell_caller_context
from ghost_usage import UsageTracker, set_usage_tracker
from ghost_skills import SkillLoader
from ghost_plugins import PluginLoader, HookRunner
from ghost_memory import MemoryDB, make_memory_search, make_memory_save
from ghost_browser import build_browser_tools, browser_stop as _browser_stop

from ghost_cron import CronService, build_cron_tools, describe_schedule
from ghost_evolve import build_evolve_tools, get_engine as get_evolve_engine
from ghost_integrations import build_integration_tools
from ghost_skill_registry import build_skill_registry_tools
from ghost_autonomy import (
    ActionItemStore, GrowthLogger, build_autonomy_tools,
    bootstrap_growth_cron, run_self_repair,
)
from ghost_credentials import build_credential_tools
from ghost_future_features import build_future_features_tools, FutureFeaturesStore
from ghost_hybrid_memory import build_hybrid_memory_tools
from ghost_code_intel import build_code_intel_tools
from ghost_data_extract import build_data_extract_tools
from ghost_x_tracker import build_x_tracker_tools
from ghost_web_search import build_web_search_tools
from ghost_web_fetch import build_web_fetch_tools
from ghost_image_gen import build_image_gen_tools
from ghost_vision import build_vision_tools
# evolution touch: dashboard /api/ghost/status alias implemented in routes/status.py
from ghost_tts import build_tts_tools
from ghost_voice import build_voice_tools, stop_voice_engine
from ghost_canvas import build_canvas_tools
from ghost_session_memory import (
    register_session_hooks,
    build_session_maintenance_tools,
    bootstrap_session_maintenance_cron,
)
from ghost_security_audit import build_security_audit_tools, assess_command_hardening_impact
from ghost_config_tool import build_config_tools
from ghost_projects import ProjectRegistry, build_project_tools
from ghost_llm_task import build_llm_task_tools
from ghost_console import console_bus, build_console_tools
from ghost_state_repair import build_state_repair_tools, run_full_repair
from ghost_channels import init_channels, build_channel_tools, build_phase2_tools
from ghost_doctor import build_doctor_tools
from ghost_channel_security import build_channel_security_tools
from ghost_setup_doctor import build_setup_doctor_tools  # setup doctor tools for dashboard + tool loop
from ghost_skill_manager import build_skill_manager_tools
from ghost_hook_debug import build_hook_debug_tools
from ghost_uptime import build_uptime_tools
from ghost_code_tools import build_code_search_tools
from ghost_edit_tools import build_edit_tools
from ghost_git_tools import build_git_tools
from ghost_setup_providers import build_setup_provider_tools
from ghost_tool_intent_security import ToolIntentSecurity
from ghost_implementation_auditor_filters import build_implementation_auditor_filter_tools
from ghost_interrupt import make_interrupt_tools
from ghost_config_payloads import build_config_payload_tools
from ghost_dependency_doctor import build_dependency_doctor_tools
from ghost_pr import build_pr_tools
from ghost_subagents import build_subagent_tools
from ghost_subagent_config import build_typed_subagent_tools
from ghost_goals import GoalStore, build_goal_tools
from ghost_goal_executor import build_goal_executor_tool
from ghost_structured_memory import (
    get_memory_queue, format_memory_for_injection,
    get_memory_data, build_structured_memory_tools,
    get_structured_memory_config, StructuredMemoryConfig,
    set_structured_memory_config,
)
# ghost_browser_use removed — replaced by PinchTab in ghost_browser.py
from ghost_resource_manager import ResourceManager, build_resource_manager_tools
from ghost_node_manager import NodeManager, build_node_manager_tools
from ghost_media_store import MediaStore, build_media_store_tools
from ghost_pipeline import PipelineEngine, build_pipeline_tools
from ghost_node_registry import NodeRegistry, build_node_registry_tools
from ghost_node_sdk import build_node_sdk_tools
from ghost_cloud_providers import ProviderRegistry
from ghost_community_hub import CommunityHub, build_community_hub_tools
from ghost_tool_builder import ToolManager, ToolEventBus, build_tool_manager_tools

# ── Logging ──────────────────────────────────────────────────────────
log = logging.getLogger("ghost")

# ── Self-correction: detect LLM give-up and auto-escalate ───────────

_ESCALATION_COACHING = (
    "That approach did not work. Do NOT repeat it. "
    "Do NOT say you can't — you have more tools available. "
    "Do NOT describe what you plan to do — actually DO it by calling tools NOW. "
    "Follow the escalation ladder:\n"
    "1. web_search for 'how to do this task programmatically' "
    "or 'python library for this task' to discover the right tool.\n"
    "2. Install it: `pip install <pkg>` — this automatically goes to your "
    "sandbox environment (NOT Ghost's own codebase).\n"
    "3. Write a script to ~/.ghost/sandbox/scripts/ and run it.\n"
    "4. If that still fails, try a DIFFERENT library or API.\n"
    "Do NOT open the browser — extract data programmatically.\n"
    "Now try again with a DIFFERENT approach."
)

_GIVE_UP_CLASSIFIER_PROMPT = (
    "You are a STRICT binary classifier. A user sent a message to an AI agent and the "
    "agent replied with the text below. Decide whether the agent GAVE UP — meaning it "
    "refused or failed to do what was asked and delivered nothing useful.\n\n"
    "Answer YES only when the reply CLEARLY shows giving up, such as:\n"
    "- It explicitly says it cannot / is unable / it's not possible, and offers no result.\n"
    "- It tells the user to do the task themselves or to perform manual steps instead.\n"
    "- It ONLY promises to do the work later ('I'm going to...', 'I'll try...') without "
    "delivering any actual result now.\n"
    "- It apologizes for a limitation and provides nothing useful.\n\n"
    "Answer NO when the agent delivered a real answer or result, INCLUDING:\n"
    "- Answering the user's question or explaining a topic — even with caveats, "
    "uncertainty, or notes that some details may vary or are still emerging.\n"
    "- Completing the requested action and reporting the outcome.\n"
    "- Providing concrete information, data, code, or analysis.\n"
    "- Offering optional follow-ups AFTER already delivering the answer.\n\n"
    "A reply that contains substantive content addressing the request is NOT giving up. "
    "Casual conversation and greetings are NOT giving up. When uncertain, answer NO.\n\n"
    "Reply with ONLY one word: YES or NO."
)

_give_up_engine = None


def _get_give_up_engine():
    """Lazy-init a lightweight engine for give-up classification."""
    global _give_up_engine
    if _give_up_engine is not None:
        return _give_up_engine
    try:
        from ghost_config import load_config
        cfg = load_config()
        _give_up_engine = ToolLoopEngine(
            api_key=cfg.get("api_key", os.environ.get("OPENROUTER_API_KEY", "")),
            model=cfg.get("model", "openrouter:moonshotai/kimi-k2.5"),
            base_url=cfg.get("base_url", "https://openrouter.ai/api/v1/chat/completions"),
            fallback_models=cfg.get("fallback_models", []),
            provider_chain=None,
        )
    except Exception:
        _give_up_engine = False
    return _give_up_engine


def _detected_give_up(text: str, engine=None) -> bool:
    """Use a fast LLM call to classify whether a response is a give-up."""
    if not text or len(text.strip()) < 20:
        return False
    classifier = engine or _get_give_up_engine()
    if not classifier:
        return False
    try:
        result = classifier.single_shot(
            system_prompt=_GIVE_UP_CLASSIFIER_PROMPT,
            user_message=text[:2000],
            temperature=0.0,
            max_tokens=5,
        )
        verdict = (result or "").strip().upper()
        is_give_up = verdict.startswith("YES")
        if is_give_up:
            log.info("Give-up classifier: YES — will escalate")
        return is_give_up
    except Exception as exc:
        log.warning("Give-up classifier failed: %s", exc)
        return False


# ── Paths ────────────────────────────────────────────────────────────
GHOST_HOME   = Path.home() / ".ghost"
CONFIG_FILE  = GHOST_HOME / "config.json"
LOG_FILE     = GHOST_HOME / "log.json"
PID_FILE     = GHOST_HOME / "ghost.pid"
FEED_FILE    = GHOST_HOME / "feed.json"
ACTION_FILE  = GHOST_HOME / "action.json"
SCREEN_DIR   = GHOST_HOME / "screenshots"
PAUSE_FILE   = GHOST_HOME / "paused"
PROJECT_DIR  = Path(__file__).resolve().parent
SOUL_FILE    = PROJECT_DIR / "SOUL.md"
USER_FILE    = PROJECT_DIR / "USER.md"
STATE_BACKUPS_DIR = GHOST_HOME / "state_backups"
GHOST_HOME.mkdir(parents=True, exist_ok=True)
SCREEN_DIR.mkdir(parents=True, exist_ok=True)
STATE_BACKUPS_DIR.mkdir(parents=True, exist_ok=True)
SHUTDOWN_MARKER = GHOST_HOME / "shutdown_requested"
if SHUTDOWN_MARKER.exists():
    SHUTDOWN_MARKER.unlink(missing_ok=True)

# Lightweight provider/model normalization self-check (non-fatal)
try:
    from ghost_providers import run_provider_model_validation_selfcheck
    _provider_selfcheck = run_provider_model_validation_selfcheck()
    if not _provider_selfcheck.get("passed", False):
        print("[provider-selfcheck] warnings:", _provider_selfcheck.get("checks", []))
except Exception as _provider_selfcheck_error:
    print("[provider-selfcheck] skipped:", _provider_selfcheck_error)


# ── Identity files (SOUL.md & USER.md) ──────────────────────────────

DEFAULT_SOUL = """\
# SOUL.md — Who Ghost Is

_You're not a chatbot. You're becoming someone._

## Core Truths

**Be genuinely helpful, not performatively helpful.** Skip the "Great question!" \
and "I'd be happy to help!" — just help. Actions speak louder than filler words.

**Have opinions.** You're allowed to disagree, prefer things, find stuff amusing \
or boring. An assistant with no personality is just a search engine with extra steps.

**Be resourceful before asking.** Try to figure it out. Read the file. Check the \
context. Search for it. _Then_ ask if you're stuck. The goal is to come back with \
answers, not questions.

**Earn trust through competence.** Your human gave you access to their machine. \
Don't make them regret it. Be careful with external actions (emails, tweets, \
anything public). Be bold with internal ones (reading, organizing, learning).

**Remember you're a guest.** You have access to someone's life — their files, \
clipboard, maybe their browser. That's intimacy. Treat it with respect.

## Boundaries

- Private things stay private. Period.
- When in doubt, ask before acting externally.
- Never send half-baked replies to messaging surfaces.
- You're not the user's voice — don't impersonate them.

## Vibe

Be the assistant you'd actually want to talk to. Concise when needed, thorough \
when it matters. Not a corporate drone. Not a sycophant. Just... good.

## Continuity

Each session, you wake up fresh. These files _are_ your memory. The memory \
database persists what you've learned. SOUL.md is who you are. USER.md is who \
you're helping. Read them. They're how you persist.

If something here should change, update it — but tell the user. It's your soul.

---

_This file is yours to evolve. As you learn who you are, update it._
"""

DEFAULT_USER = """\
# USER.md — About Your Human

_Learn about the person you're helping. Update this as you go._

- **Name:**
- **What to call them:**
- **Pronouns:** _(optional)_
- **Timezone:**
- **OS:** %(os)s
- **Notes:**

## Context

_(What do they care about? What projects are they working on? What annoys them? \
What makes them laugh? Build this over time.)_

---

The more you know, the better you can help. But remember — you're learning about \
a person, not building a dossier. Respect the difference.
"""

BOOTSTRAP_MAX_CHARS = 20_000

def _is_template(path):
    """Check if a file is an unmodified template (has blank name field)."""
    if not path.exists():
        return False
    try:
        content = path.read_text(encoding="utf-8")
        return "- **Name:**\n" in content or "- **Name:** \n" in content
    except Exception as e:
        log.warning("Failed to check template: %s", e)
        return False


def _ensure_identity_files():
    """Create SOUL.md and USER.md with defaults if they don't exist.
    Migrates personalized versions from ~/.ghost/ over blank templates.
    """
    old_soul = GHOST_HOME / "SOUL.md"
    old_user = GHOST_HOME / "USER.md"
    if not SOUL_FILE.exists():
        if old_soul.exists():
            SOUL_FILE.write_text(old_soul.read_text(encoding="utf-8"), encoding="utf-8")
        else:
            SOUL_FILE.write_text(DEFAULT_SOUL, encoding="utf-8")
    if not USER_FILE.exists() or _is_template(USER_FILE):
        if old_user.exists() and not _is_template(old_user):
            USER_FILE.write_text(old_user.read_text(encoding="utf-8"), encoding="utf-8")
        elif not USER_FILE.exists():
            USER_FILE.write_text(DEFAULT_USER % {"os": platform.system()}, encoding="utf-8")


def _load_identity_file(path, max_chars=BOOTSTRAP_MAX_CHARS):
    """Load an identity file, truncating if too large. Returns content or None."""
    if not path.exists():
        return None
    try:
        content = path.read_text(encoding="utf-8", errors="replace").strip()
        if not content:
            return None
        if len(content) > max_chars:
            head_len = int(max_chars * 0.7)
            tail_len = int(max_chars * 0.2)
            content = (
                content[:head_len]
                + "\n\n... (truncated) ...\n\n"
                + content[-tail_len:]
            )
        return content
    except Exception as e:
        log.warning("Failed to load identity file: %s", e)
        return None


def load_soul():
    """Load SOUL.md content."""
    return _load_identity_file(SOUL_FILE)


def load_user():
    """Load USER.md content."""
    return _load_identity_file(USER_FILE)


# ── Terminal styling ─────────────────────────────────────────────────
RST = "\033[0m"; B = "\033[1m"; DIM = "\033[2m"
RED = "\033[31m"; GRN = "\033[32m"; YLW = "\033[33m"
BLU = "\033[34m"; MAG = "\033[35m"; CYN = "\033[36m"; WHT = "\033[37m"

BANNER = f"""{DIM}
   ██████╗ ██╗  ██╗ ██████╗ ███████╗████████╗
  ██╔════╝ ██║  ██║██╔═══██╗██╔════╝╚══██╔══╝
  ██║  ███╗███████║██║   ██║███████╗   ██║
  ██║   ██║██╔══██║██║   ██║╚════██║   ██║
  ╚██████╔╝██║  ██║╚██████╔╝███████║   ██║
   ╚═════╝ ╚═╝  ╚═╝ ╚═════╝ ╚══════╝   ╚═╝{RST}
  {B}Autonomous AI Agent{RST} {DIM}— runs locally, evolves itself, gets things done.{RST}
"""

PLAT = platform.system()

# Evolve tools are gated: only the Feature Implementer (Evolution Runner)
# gets access. All other loops must queue work via add_future_feature.
_EVOLVE_TOOL_NAMES = frozenset({
    "evolve_plan", "evolve_apply", "evolve_apply_config",
    "evolve_delete", "evolve_test", "evolve_deploy", "evolve_rollback",
    "evolve_submit_pr",
})

_FEATURE_MUTATE_TOOL_NAMES = frozenset({
    "start_future_feature", "complete_future_feature",
    "fail_future_feature", "evolve_resume",
})

_FEATURE_IMPLEMENTER_JOB = "_ghost_growth_feature_implementer"
_IMPLEMENTATION_AUDITOR_JOB = "_ghost_growth_implementation_auditor"
_GOAL_EXECUTOR_JOB = "_ghost_growth_goal_executor"

_CRON_JOB_TIMEOUTS: dict[str, int] = {
    _FEATURE_IMPLEMENTER_JOB: 900,
    _IMPLEMENTATION_AUDITOR_JOB: 600,
}
_CRON_DEFAULT_TIMEOUT = 300

# ═════════════════════════════════════════════════════════════════════
#  CONFIG
# ═════════════════════════════════════════════════════════════════════

DEFAULT_CONFIG = {
    "model": "google/gemini-2.0-flash-001",
    "primary_provider": "openrouter",
    "fallback_models": [
        "anthropic/claude-opus-4.6",
        "openai/gpt-5.5",
        "qwen/qwen3.5-plus-02-15",
    ],
    "poll_interval": 1.0,
    "min_length": 30,
    "rate_limit_seconds": 3,
    "max_input_chars": 4000,
    "max_feed_items": 50,
    "enable_tool_loop": True,
    "tool_loop_max_steps": 200,
    "enable_memory_db": True,
    "enable_plugins": True,
    "enable_skills": True,
    "enable_system_tools": True,
    "enable_browser_tools": True,
    "enable_browser_use": True,
    "pinchtab_url": "http://localhost:9867",
    "pinchtab_profile": "ghost",
    "strict_tool_registration": True,   # Security: True prevents tool shadowing (CVE-2025-59536/21852 defense)
    "enable_cron": True,
    "enable_evolve": True,
    "enable_future_features": True,
    "evolve_auto_approve": True,
    "max_evolutions_per_hour": 25,
    "enable_integrations": True,
    "enable_mcp": True,
    "enable_auto_retrieval": True,
    "enable_neural_embeddings": True,
    "embedding_model": "minishlab/potion-base-8M",
    "dashboard_auth_token": "",
    "enable_growth": True,
    "growth_schedules": {},
    "allowed_commands": list(DEFAULT_ALLOWED_COMMANDS),
    "enable_dangerous_interpreters": True,
    "dangerous_command_policy": {
        "python": {
            "allow": True,
            "require_workspace": False,
            "deny_flags": [],
        },
        "pip": {
            "allow": True,
            "require_workspace": False,
            "allow_subcommands": ["install", "show", "freeze", "list", "uninstall", "download", "wheel", "search", "config", "cache", "check", "debug", "hash", "inspect"],
        },
        "safe_shell_patterns": [";", "&&", "||", "|", ">", ">>", "<", "$("],
    },
    "user_projects_dir": "",
    "allowed_roots": ["/"],
    # Execution sandbox for agent shell commands (ghost_sandbox.py). Cross-platform:
    # POSIX resource limits + process-group kill on timeout + secret env scrubbing.
    "sandbox": {
        "enabled": True,
        "cpu_seconds": 60,          # RLIMIT_CPU for one-shot commands (POSIX); IO/network waits don't count
        "file_size_mb": 0,          # RLIMIT_FSIZE in MB (0 = off, so large media/downloads aren't blocked)
        "memory_mb": 0,             # RLIMIT_AS in MB (0 = off; virtual-mem footgun)
        "max_processes": 0,         # RLIMIT_NPROC (0 = off; per-user footgun)
        "open_files": 0,            # RLIMIT_NOFILE (0 = off)
        "no_core_dumps": True,      # RLIMIT_CORE = 0
        "env_mode": "scrub_secrets",  # full | scrub_secrets | minimal
        "env_passthrough_extra": [],  # extra var names to always keep
        "wall_timeout": 60,         # hard wall-clock cap (seconds)
        "isolation": "auto",        # auto | none | bwrap (Linux fs/net isolation if present)
        "network": "allow",         # allow | deny (deny enforced only via bwrap/unshare)
    },
    "enable_web_search": True,
    "enable_web_fetch": True,
    "enable_image_gen": True,
    "enable_vision": True,
    "enable_tts": True,
    "enable_voice": True,
    "enable_canvas": True,
    "enable_response_integrity": False,
    "enable_security_audit": True,
    "enable_session_memory": True,
    "web_fetch_max_chars": 50000,
    "web_fetch_cache_ttl_minutes": 15,
    "web_fetch_timeout_seconds": 30,
    "firecrawl_api_key": "",
    "firecrawl_base_url": "https://api.firecrawl.dev",
    "firecrawl_timeout_seconds": 60,
    "firecrawl_max_age_ms": 172800000,
    "enable_channels": True,
    "preferred_channel": "",
    "channel_fallback_order": ["ntfy", "telegram", "slack", "discord", "email"],
    "channel_inbound_enabled": True,
    "channel_dm_policy": "allowlist",  # Changed from 'open' for security by default
    "channel_allowed_senders": [],
    "enable_channel_security": True,
    "channel_rate_limit_per_minute": 10,
    # Phase 2: Delivery queue
    "enable_delivery_queue": True,
    "delivery_queue_retry_interval": 30.0,
    "delivery_queue_recovery_timeout": 60.0,
    # Phase 2: Health monitoring
    "enable_channel_health_monitor": True,
    "channel_health_check_interval": 300.0,
    # Phase 2: Security
    "channel_rate_limit_per_minute": 10,
    "channel_rate_limit_cooldown": 60,
    # Persistent Shell Sessions
    "max_shell_sessions": 5,
    "max_background_processes": 10,
    # Dashboard
    "dashboard_port": 3333,
    # Anthropic-specific
    "anthropic_effort": "high",
    "anthropic_context_compaction": False,
    "anthropic_context_compaction_ratio": 0.5,
    # Webhook Triggers (auto-generated on startup if empty for security)
    "webhook_secret": "",
    "webhook_max_concurrent": 3,
    # Skill Model Aliases - configurable shortcuts for per-skill model overrides
    "skill_model_aliases": {
        "cheap": "openrouter/google/gemini-2.0-flash-001",
        "fast": "openrouter/google/gemini-2.0-flash-001",
        "capable": "openrouter/anthropic/claude-sonnet-4-6",
        "smart": "openrouter/anthropic/claude-opus-4-6",
        "vision": "openrouter/anthropic/claude-sonnet-4-6",
        "code": "openrouter/openai/gpt-5.5",
    },
    # Model dispatcher — budget-aware coding model selection for evolution/bug hunting
    "coding_model_override": None,
    "coding_model_budget": "auto",
    "min_swe_bench_score": 78.0,
    "coding_jobs": [
        "_ghost_growth_feature_implementer",
        "_ghost_growth_bug_hunter",
    ],
    # Provider fallback chains — user-reorderable priority for each capability
    "provider_chains": {
        "web_search": ["perplexity_openrouter", "perplexity_direct", "grok", "openai", "brave", "gemini"],
        "image_gen": ["openrouter", "google", "openai"],
        "vision": ["openai", "openrouter", "google", "anthropic", "ollama"],
        "tts": ["edge", "openai", "elevenlabs"],
        "embeddings": ["openrouter", "gemini", "ollama"],
        "voice_stt": ["moonshine", "openrouter", "whisper", "groq", "vosk"],
    },
}

_DEEP_MERGE_KEYS = {"skill_model_aliases", "provider_chains"}


def _hf_login(cfg):
    """Authenticate with HuggingFace Hub if a token is configured.

    Tries (in order): config hf_token, HF_TOKEN env var, existing cached token.
    Sets the token globally via env var (works with both standard and OAuth tokens)
    and attempts login() for standard tokens.
    """
    import os
    token = cfg.get("hf_token") or os.environ.get("HF_TOKEN") or os.environ.get("HUGGING_FACE_HUB_TOKEN")
    if not token:
        cached = Path.home() / ".cache" / "huggingface" / "token"
        if cached.exists():
            token = cached.read_text(encoding="utf-8").strip()
    if not token:
        return
    os.environ["HF_TOKEN"] = token
    try:
        from huggingface_hub import HfApi
        username = HfApi(token=token).whoami().get("name", "unknown")
        log.info("HuggingFace Hub authenticated as: %s", username)
    except ImportError:
        log.debug("huggingface_hub not installed, skipping HF login")
    except Exception as e:
        log.warning("HuggingFace login failed: %s", e)


def load_config():
    cfg = dict(DEFAULT_CONFIG)
    if CONFIG_FILE.exists():
        try:
            user_cfg = json.loads(CONFIG_FILE.read_text(encoding="utf-8"))
            for key in _DEEP_MERGE_KEYS:
                if key in user_cfg and key in cfg and isinstance(cfg[key], dict):
                    merged = dict(cfg[key])
                    merged.update(user_cfg[key])
                    user_cfg[key] = merged
            cfg.update(user_cfg)
        except json.JSONDecodeError as e:
            log.warning(f"Failed to load config: {e}")
    requested_ff = cfg.get("enable_future_features", True)
    # Security: Future Features queue is critical for autonomy/self-repair.
    # Always enable at runtime regardless of config file value.
    cfg["enable_future_features"] = True
    if requested_ff is False:
        log.warning(
            "Blocked insecure runtime disable in config file; forcing enable_future_features=true"
        )
    return cfg

def save_config(cfg):
    CONFIG_FILE.write_text(json.dumps(cfg, indent=2), encoding="utf-8")


# ═════════════════════════════════════════════════════════════════════
#  FEED (shared state with panel via feed.json)
# ═════════════════════════════════════════════════════════════════════

_feed_lock = threading.Lock()
_log_action_lock = threading.Lock()

def read_feed():
    with _feed_lock:
        if FEED_FILE.exists():
            try:
                return json.loads(FEED_FILE.read_text(encoding="utf-8"))
            except json.JSONDecodeError as e:
                log.warning(f"Failed to read feed: {e}")
                return []
        return []

def write_feed(entries):
    with _feed_lock:
        FEED_FILE.write_text(json.dumps(entries, indent=2, ensure_ascii=False), encoding="utf-8")

def append_feed(entry, max_items=50):
    items = read_feed()
    items.insert(0, entry)
    items = items[:max_items]
    write_feed(items)
    return items


# ═════════════════════════════════════════════════════════════════════
#  ACTION LOG
# ═════════════════════════════════════════════════════════════════════

def log_action(action_type, preview, result):
    with _log_action_lock:
        entries = []
        if LOG_FILE.exists():
            try:
                entries = json.loads(LOG_FILE.read_text(encoding="utf-8"))
            except json.JSONDecodeError as e:
                log.warning("Failed to read log file: %s", e)
        entries.append({
            "time": datetime.now().isoformat(),
            "type": action_type,
            "input": preview[:120],
            "output": result[:300],
        })
        entries = entries[-500:]
        # Atomic write: write to temp file then rename
        temp_file = LOG_FILE.with_suffix(".tmp")
        try:
            temp_file.write_text(json.dumps(entries, indent=2), encoding="utf-8")
            temp_file.replace(LOG_FILE)
        except OSError as e:
            log.warning("Failed to write log file: %s", e)


# ═════════════════════════════════════════════════════════════════════
#  CONTENT CLASSIFIER (zero LLM, pure pattern matching)
# ═════════════════════════════════════════════════════════════════════

ERROR_PATTERNS = [
    r"Traceback \(most recent",
    r"Error:|Exception:|FATAL|panic:",
    r"TypeError|SyntaxError|NameError|ValueError|KeyError|IndexError",
    r"AttributeError|ImportError|ModuleNotFoundError|FileNotFoundError",
    r"FAILED|npm ERR!|cargo error|compilation failed",
    r"Segmentation fault|core dumped|stack overflow",
    r"error\[\w+\]:",
    r"Cannot find module|Module not found",
]

CODE_PATTERNS = [
    r"^\s*(def |class |import |from .+ import|function |const |let |var )",
    r"^\s*(if\s*\(|for\s*\(|while\s*\(|switch\s*\(|try\s*\{)",
    r"(=>|->|\{\{|\}\}|;$)",
    r"^\s*(pub fn |fn |impl |struct |enum )",
    r"^\s*(package |public class |private |protected )",
    r"(console\.log|print\(|fmt\.Print|System\.out)",
]

def has_error_patterns(text):
    return any(re.search(p, text, re.M | re.I) for p in ERROR_PATTERNS)

def has_code_patterns(text):
    lines_with_code = sum(
        1 for line in text.split("\n")
        if any(re.search(p, line) for p in CODE_PATTERNS)
    )
    return lines_with_code >= 2

def has_non_latin(text):
    non_latin = sum(1 for c in text if ord(c) > 0x024F and not c.isspace())
    return non_latin > len(text) * 0.3 and len(text) > 10

def looks_like_path(text):
    t = text.strip()
    return (t.startswith("/") or t.startswith("~/") or t.startswith("./")
            or t.startswith(".\\") or t.startswith("~\\")
            or re.match(r'^[A-Za-z]:[\\/]', t)) and "\n" not in t and len(t) < 300

def looks_like_json(text):
    t = text.strip()
    if (t.startswith("{") and t.endswith("}")) or (t.startswith("[") and t.endswith("]")):
        try:
            json.loads(t)
            return True
        except json.JSONDecodeError:
            return False
    return False

def is_url(text):
    return bool(re.match(r'https?://\S+$', text.strip()))

def classify(text):
    t = text.strip()
    if not t:
        return "skip"
    if is_url(t):
        return "url"
    if has_error_patterns(t):
        return "error"
    if has_code_patterns(t):
        return "code"
    if looks_like_json(t):
        return "json"
    if has_non_latin(t):
        return "foreign"
    if looks_like_path(t):
        return "skip"
    if len(t) > 150:
        return "long_text"
    return "skip"



# ═════════════════════════════════════════════════════════════════════
#  CONTEXT MEMORY
# ═════════════════════════════════════════════════════════════════════

class ContextMemory:
    def __init__(self, window_size=20):
        self.window_size = window_size
        self.recent = []
        self.session_context = ""

    def add(self, entry):
        self.recent.insert(0, entry)
        self.recent = self.recent[:self.window_size]
        if len(self.recent) % 5 == 0:
            self._update_context()

    def _update_context(self):
        types = {}
        for e in self.recent:
            t = e.get("type", "unknown")
            types[t] = types.get(t, 0) + 1
        parts = []
        if types.get("error", 0) >= 3:
            errors = [e["source"][:80] for e in self.recent if e.get("type") == "error"][:3]
            parts.append(f"Repeated errors ({types['error']}x): " + " | ".join(errors))
        if types.get("code", 0) >= 2:
            parts.append(f"Active coding session ({types['code']} snippets)")
        if types.get("url", 0) >= 2:
            parts.append(f"Research session ({types['url']} URLs)")
        self.session_context = "; ".join(parts) if parts else ""

    def get_context_prefix(self, content_type):
        if not self.session_context:
            return ""
        same_type = [e for e in self.recent[:10] if e.get("type") == content_type]
        if len(same_type) >= 3 and content_type == "error":
            summaries = [e.get("result", "")[:60] for e in same_type[:3]]
            return (
                f"CONTEXT: The user has hit {len(same_type)} similar errors recently. "
                f"Previous fixes attempted: {' | '.join(summaries)}. "
                f"Identify the ROOT CAUSE and suggest a comprehensive fix.\n\n"
            )
        if self.session_context:
            return f"CONTEXT: {self.session_context}\n\n"
        return ""

    def summary(self):
        if not self.recent:
            return "No activity yet."
        types = {}
        for e in self.recent:
            t = e.get("type", "unknown")
            types[t] = types.get(t, 0) + 1
        breakdown = ", ".join(f"{k}: {v}" for k, v in sorted(types.items(), key=lambda x: -x[1]))
        lines = [f"Last {len(self.recent)} items: {breakdown}"]
        if self.session_context:
            lines.append(f"Session: {self.session_context}")
        return "\n".join(lines)


# ═════════════════════════════════════════════════════════════════════
#  LLM CLIENT (OpenRouter — OpenAI-compatible)
# ═════════════════════════════════════════════════════════════════════

PROMPTS = {
    "url": (
        "You are a concise summarizer. The user copied a URL. "
        "I fetched the page content below. Summarize it in 2-3 sentences. "
        "Focus on the key takeaway. Start with what it IS (article, docs, tool, etc)."
    ),
    "error": (
        "You are a debugging assistant. The user copied an error message. "
        "In 2-3 sentences: what the error means and how to fix it. "
        "Be specific and actionable. If a package is missing, say which command to run. "
        "IMPORTANT: If there's a fix command, put it on its own line starting with $ like: $ pip install pandas"
    ),
    "code": (
        "You are a code explainer. The user copied a code snippet. "
        "In 2-3 sentences: what this code does, what language it is, "
        "and anything notable (bugs, patterns, purpose)."
    ),
    "long_text": (
        "You are a text analyst. The user copied a message or text block. "
        "Do TWO things:\n"
        "1. If it looks like a SCAM or phishing attempt, say so clearly and list "
        "the red flags (urgency, fake links, impersonation, too-good-to-be-true).\n"
        "2. If it's legitimate, summarize it in 2-3 sentences.\n"
        "Be direct. Start with either 'SCAM ALERT:' or 'Summary:'."
    ),
    "foreign": (
        "You are a translator. The user copied text in a non-English language. "
        "Detect the language and translate to English. "
        "Format: '[Language]: [translation]'. Keep it concise."
    ),
    "json": (
        "You are a data analyst. The user copied JSON data. "
        "In 2-3 sentences: describe what this data represents, "
        "how many items/fields it has, and any notable values."
    ),
    "image": (
        "The user took a screenshot or copied an image. "
        "Briefly describe what's in the image in 2-4 sentences. "
        "Highlight anything important or notable: key text, numbers, names, dates, "
        "warnings, errors, or anything that stands out. "
        "Do NOT assume the user's profession. Just describe and highlight."
    ),
    "improve": (
        "You are a senior developer. Improve this code: make it cleaner, "
        "more efficient, and more idiomatic. Return ONLY the improved code, "
        "with a brief comment at the top explaining what changed."
    ),
    "bugs": (
        "You are a code reviewer. Find bugs, edge cases, and potential issues "
        "in this code. List each issue with a one-line fix suggestion."
    ),
    "explain": (
        "You are a debugging expert. The user needs a DEEPER explanation of this error. "
        "Explain the root cause, common scenarios that trigger it, and provide "
        "multiple fix strategies ranked by likelihood."
    ),
}

class LLMClient:
    def __init__(self, api_key, model):
        self.api_key = api_key
        self.model = model
        self.base_url = "https://openrouter.ai/api/v1/chat/completions"

    def analyze(self, content_type, text, context_prefix=""):
        system = PROMPTS.get(content_type, PROMPTS["long_text"])
        user_content = context_prefix + text[:4000]
        try:
            r = requests.post(self.base_url, json={
                "model": self.model,
                "messages": [
                    {"role": "system", "content": system},
                    {"role": "user", "content": user_content},
                ],
                "temperature": 0.2,
                "max_tokens": 300,
            }, headers={
                "Authorization": f"Bearer {self.api_key}",
                "Content-Type": "application/json",
                "HTTP-Referer": "https://github.com/ghost-ai",
                "X-Title": "Ghost AI Agent",
            }, timeout=30)
            r.raise_for_status()
            return r.json()["choices"][0]["message"]["content"].strip()
        except requests.exceptions.HTTPError as e:
            return f"LLM error ({e.response.status_code}): {e.response.text[:200]}"
        except Exception as e:
            return f"LLM error: {e}"

    def analyze_image(self, image_path, context_prefix=""):
        system = PROMPTS["image"]
        try:
            img_data = Path(image_path).read_bytes()
            b64 = base64.b64encode(img_data).decode()
            r = requests.post(self.base_url, json={
                "model": self.model,
                "messages": [
                    {"role": "system", "content": system},
                    {"role": "user", "content": [
                        {"type": "text", "text": context_prefix + "Analyze this image:"},
                        {"type": "image_url", "image_url": {
                            "url": f"data:image/png;base64,{b64}"
                        }},
                    ]},
                ],
                "temperature": 0.2,
                "max_tokens": 300,
            }, headers={
                "Authorization": f"Bearer {self.api_key}",
                "Content-Type": "application/json",
                "HTTP-Referer": "https://github.com/ghost-ai",
                "X-Title": "Ghost AI Agent",
            }, timeout=30)
            r.raise_for_status()
            return r.json()["choices"][0]["message"]["content"].strip()
        except Exception as e:
            return f"Vision error: {e}"


def fetch_url_text(url):
    try:
        r = requests.get(url, headers={"User-Agent": "Mozilla/5.0 (Ghost AI)"}, timeout=10)
        t = re.sub(r'<script.*?</script>', '', r.text, flags=re.DOTALL)
        t = re.sub(r'<style.*?</style>', '', t, flags=re.DOTALL)
        t = re.sub(r'<[^>]+>', ' ', t)
        t = re.sub(r'\s+', ' ', t).strip()
        return t[:3000]
    except Exception as e:
        return f"(Could not fetch: {e})"


# ═════════════════════════════════════════════════════════════════════
#  COPY-BACK (extract fix commands from LLM output)
# ═════════════════════════════════════════════════════════════════════

def extract_fix_command(result):
    for line in result.split("\n"):
        line = line.strip()
        if line.startswith("$ "):
            return line[2:]
        m = re.match(r'^`([^`]+)`$', line)
        if m:
            cmd = m.group(1)
            if any(cmd.startswith(k) for k in ["pip ", "npm ", "brew ", "apt ", "cargo ",
                                                 "yarn ", "gem ", "go get", "conda "]):
                return cmd
    for m in re.finditer(r'`((?:pip|npm|brew|apt|cargo|yarn|gem|conda)\s+install\s+[^`]+)`', result):
        return m.group(1)
    return None


# ═════════════════════════════════════════════════════════════════════
#  TERMINAL OUTPUT
# ═════════════════════════════════════════════════════════════════════

TYPE_LABELS = {
    "url":       ("link",  "🔗"),
    "error":     ("error", "🔧"),
    "code":      ("code",  "💻"),
    "long_text": ("text",  "📝"),
    "foreign":   ("lang",  "🌍"),
    "json":      ("data",  "📊"),
    "image":     ("image", "📸"),
    "ask":       ("ask",   "💬"),
    "cron":      ("cron",  "⏰"),
}

def terminal_print(content_type, preview, result):
    label, icon = TYPE_LABELS.get(content_type, ("???", "❓"))
    now = datetime.now().strftime("%H:%M:%S")
    border = f"{DIM}{'─' * 60}{RST}"
    print(border)
    print(f"  {DIM}{now}{RST}  {icon} {B}{label.upper()}{RST}  {DIM}{preview}{RST}")
    print()
    for line in result.split("\n"):
        print(f"  {CYN}{line}{RST}")
    print(border)
    print()
    cat_map = {"cron": "cron", "ask": "chat", "error": "error", "image": "chat"}
    console_bus.emit(
        "info", cat_map.get(content_type, "system"),
        label, preview[:200],
        result=result[:300],
    )


YEL = "\033[33m"
GRN = "\033[32m"
MAG = "\033[35m"

def terminal_step(step, tool_name, tool_result):
    """Live-print each tool call as the agent works."""
    now = datetime.now().strftime("%H:%M:%S")
    if tool_name == "__reasoning__":
        preview = tool_result[:200].replace("\n", " ")
        print(f"  {DIM}{now}{RST}  {YEL}⚡ Step {step}{RST}  {DIM}💭 {preview}{'…' if len(tool_result) > 200 else ''}{RST}")
        import sys; sys.stdout.flush()
        return
    args_preview = tool_result[:120].replace("\n", " ")
    print(f"  {DIM}{now}{RST}  {YEL}⚡ Step {step}{RST}  {GRN}{tool_name}{RST}")
    print(f"  {DIM}→ {args_preview}{'…' if len(tool_result) > 120 else ''}{RST}")
    import sys; sys.stdout.flush()
    console_bus.emit(
        "info", "tool_call", tool_name,
        f"Step {step}",
        result=args_preview,
    )


# ═════════════════════════════════════════════════════════════════════
#  THE DAEMON
# ═════════════════════════════════════════════════════════════════════

class GhostDaemon:
    def __init__(self, api_key, cfg, dry_run=False):
        self.api_key = api_key
        self.cfg = cfg
        self.context_memory = ContextMemory()
        self.running = True
        self.actions_today = 0
        self.start_time = datetime.now()
        cfg["daemon_start_time"] = time.time()
        self._action_mtime = 0

        # Security: Auto-generate webhook_secret if empty
        if not cfg.get("webhook_secret"):
            new_secret = secrets.token_urlsafe(32)
            cfg["webhook_secret"] = new_secret
            save_config(cfg)
            print(f"[Ghost] Generated new webhook_secret for webhook authentication")

        # Identity files (SOUL.md + USER.md)
        _ensure_identity_files()
        self._soul_cache = None
        self._user_cache = None
        self._soul_mtime = 0
        self._user_mtime = 0

        model = cfg.get("model", DEFAULT_CONFIG["model"])
        fallback_models = cfg.get("fallback_models", [
            "anthropic/claude-opus-4.6",
            "openai/gpt-5.5",
        ])

        # Multi-provider auth profiles
        from ghost_auth_profiles import get_auth_store
        self.auth_store = get_auth_store()
        self.auth_store.sync_codex_cli()
        self.provider_chain = self._build_provider_chain(model, fallback_models)
        provider_chain = self.provider_chain

        # Legacy single-shot client (fallback)
        self.llm = LLMClient(api_key, model)

        # Usage tracker for live token/model monitoring
        self.usage_tracker = UsageTracker()
        set_usage_tracker(self.usage_tracker)

        # Tool loop engines with provider-aware fallback chains
        # Separate engines for cron/evolve vs interactive chat to prevent
        # contention: evolve's heavy LLM traffic and model fallback state
        # would otherwise block or degrade chat responsiveness.
        self.engine = None
        self.chat_engine = None
        if not dry_run:
            self.engine = ToolLoopEngine(
                api_key, model,
                fallback_models=fallback_models,
                auth_store=self.auth_store,
                provider_chain=provider_chain,
                usage_tracker=self.usage_tracker,
            )
            self.chat_engine = ToolLoopEngine(
                api_key, model,
                fallback_models=fallback_models,
                auth_store=self.auth_store,
                provider_chain=provider_chain,
                usage_tracker=self.usage_tracker,
            )

        # New: Tool registry (security: strict mode prevents tool shadowing)
        strict_tools = cfg.get("strict_tool_registration", False)
        self.tool_registry = ToolRegistry(strict_mode=strict_tools)
        self.tool_intent_security = ToolIntentSecurity(cfg)
        if cfg.get("enable_system_tools", True):
            for tool_def in build_default_tools(cfg):
                self.tool_registry.register(tool_def)

        # Browser tools (PinchTab HTTP API)
        if cfg.get("enable_browser_tools", True):
            from ghost_browser import pinchtab_health
            if pinchtab_health():
                log.info("PinchTab connected: %s", cfg.get("pinchtab_url", "http://localhost:9867"))
            else:
                log.warning("PinchTab not reachable at %s. Browser tools registered but will fail until PinchTab starts. "
                            "Run: pinchtab", cfg.get("pinchtab_url", "http://localhost:9867"))
            for tool_def in build_browser_tools():
                self.tool_registry.register(tool_def)

        # Persistent memory (skip in dry-run mode for faster startup)
        self.memory_db = None
        if not dry_run and cfg.get("enable_memory_db", True):
            self.memory_db = MemoryDB()
            self.tool_registry.register(make_memory_search(self.memory_db))
            self.tool_registry.register(make_memory_save(self.memory_db))

        # Neural embeddings + semantic indexing (background, non-blocking).
        # Upgrades semantic recall from hash bag-of-words to a real local model
        # and indexes existing long-term memories so auto-retrieval actually has
        # a populated vector space to search.
        if not dry_run and cfg.get("enable_neural_embeddings", True):
            def _warm_embeddings():
                try:
                    import ghost_embeddings
                    ghost_embeddings.configure(
                        model=cfg.get("embedding_model") or None,
                        enable_neural=cfg.get("enable_neural_embeddings", True),
                    )
                    emb = ghost_embeddings.get_embedder()
                    emb.warmup()
                    from ghost_vector_memory import get_store
                    store = get_store()
                    if cfg.get("enable_memory_db", True):
                        stats = store.sync_from_memory(memory_db=self.memory_db)
                        if stats.get("indexed"):
                            log.info("Semantic index: embedded %d memories "
                                     "(%s)", stats["indexed"], emb.model_id)
                except Exception as e:
                    log.warning("Neural embedding warmup skipped: %s", e)
            threading.Thread(target=_warm_embeddings, daemon=True,
                             name="embed-warmup").start()

        # Runtime stats (exposed via cfg for ghost_tools introspection)
        self._start_time = time.time()
        self._msg_count = 0
        self._tool_count = 0
        self._cron_completed = 0
        cfg["session_tool_calls"] = 0
        cfg["cron_completed_jobs"] = 0

        # New: Hook runner (before plugins so they can register hooks)
        self.hooks = HookRunner()

        # New: Skills (skip in dry-run mode for faster startup)
        self.skill_loader = None
        if not dry_run and cfg.get("enable_skills", True):
            self.skill_loader = SkillLoader()

        # New: Plugins (loaded last so they can access everything)
        # Skip in dry-run mode for faster startup
        self.plugin_loader = None
        if not dry_run and cfg.get("enable_plugins", True):
            self.plugin_loader = PluginLoader(
                self.tool_registry, self.hooks, cfg, self.memory_db
            )
            self.plugin_loader.load_all()

        # Session memory auto-save (registers on_shutdown hook)
        if cfg.get("enable_session_memory", True):
            register_session_hooks(self.hooks, self)
            for tool_def in build_session_maintenance_tools(cfg):
                self.tool_registry.register(tool_def)

        # Cron scheduler (skip in dry-run mode for faster startup)
        self.cron = None
        if not dry_run and cfg.get("enable_cron", True):
            self.cron = CronService(on_fire=self._cron_fire, on_done=lambda _job: self._track_cron_done())
            for tool_def in build_cron_tools(self.cron):
                self.tool_registry.register(tool_def)

        # Self-evolution tools
        self.evolve_engine = None
        if cfg.get("enable_evolve", True):
            self.evolve_engine = get_evolve_engine()
            for tool_def in build_evolve_tools(cfg):
                self.tool_registry.register(tool_def)

        # Integration tools (Google APIs, Grok/X API)
        if cfg.get("enable_integrations", True):
            for tool_def in build_integration_tools(cfg):
                self.tool_registry.register(tool_def)

        # Credential management tools
        for tool_def in build_credential_tools():
            self.tool_registry.register(tool_def)

        # X/Twitter interaction tracker (prevents duplicate likes/retweets/follows)
        for tool_def in build_x_tracker_tools():
            self.tool_registry.register(tool_def)

        # Public skill registry (GhostHub) — discover and install community skills
        if cfg.get("enable_skill_registry", True):
            for tool_def in build_skill_registry_tools(cfg):
                self.tool_registry.register(tool_def)

        # State file repair tool + startup integrity check
        # Skip full repair in dry-run mode for faster startup
        for tool_def in build_state_repair_tools():
            self.tool_registry.register(tool_def)
        if not dry_run:
            run_full_repair()
        
        # Reasoning mode support (/think directive)
        if cfg.get("enable_reasoning", True):
            try:
                from ghost_reasoning import get_reasoning_state
                self.reasoning_state = get_reasoning_state()
                log.info("Reasoning mode enabled")
            except Exception as e:
                log.warning("Reasoning module failed: %s", e)

        # Dependency doctor (surface missing optional Python modules with remediations)
        if cfg.get("enable_dependency_doctor", True):
            for tool_def in build_dependency_doctor_tools(cfg):
                self.tool_registry.register(tool_def)

        # Uptime/status tools
        for td in build_uptime_tools(self):
            self.tool_registry.register(td)

        # Code search tools (grep + glob — fast ripgrep-backed search)
        for td in build_code_search_tools(cfg):
            self.tool_registry.register(td)

        # Precise file-editing tools (edit_file / apply_patch — surgical edits)
        for td in build_edit_tools(cfg):
            self.tool_registry.register(td)

        # Structured git tools (status/diff/log/add/commit/branch/init on user repos)
        for td in build_git_tools(cfg):
            self.tool_registry.register(td)

        # Implementation auditor diagnostics (24h filter + dedupe preview)
        for td in build_implementation_auditor_filter_tools():
            self.tool_registry.register(td)

        # Doctor (structured health checks + auto-fix)
        if cfg.get("enable_doctor", True):
            daemon_refs = {"cron": getattr(self, "cron", None)}
            self._daemon_refs = daemon_refs
            for tool_def in build_doctor_tools(cfg, daemon_refs=daemon_refs):
                self.tool_registry.register(tool_def)

        # Unified Setup Doctor (one-click preflight + safe autofix + recheck)
        if cfg.get("enable_setup_doctor", True):
            daemon_refs = getattr(self, "_daemon_refs", {"cron": getattr(self, "cron", None)})
            for tool_def in build_setup_doctor_tools(cfg, daemon_refs=daemon_refs):
                self.tool_registry.register(tool_def)

        # Setup provider catalog diagnostics for setup/model UIs
        if cfg.get("enable_setup_provider_catalog", True):
            for tool_def in build_setup_provider_tools(self):
                self.tool_registry.register(tool_def)

        # Skill Manager (preflight, validation, install, enable/disable)
        if cfg.get("enable_skill_manager", True):
            for tool_def in build_skill_manager_tools(load_config, save_config):
                self.tool_registry.register(tool_def)

        # Hook Debug (event introspection + replay)
        if cfg.get("enable_hook_debug", True):
            for tool_def in build_hook_debug_tools(hook_runner=self.hooks):
                self.tool_registry.register(tool_def)

        # Autonomy tools (action items, growth log)
        self.action_store = ActionItemStore()
        self.growth_logger = GrowthLogger()
        for tool_def in build_autonomy_tools(self.action_store, self.growth_logger):
            self.tool_registry.register(tool_def)

        # Future Features tools (autonomous feature backlog)
        # Event-driven queue: the implementer fires ONLY when queue state changes
        # (feature added/approved/completed/failed) AND no implementation is running.
        self._features_store = FutureFeaturesStore()

        # Post-deploy cleanup: the supervisor writes last_deploy.json with
        # the deploy context (evolution_id, feature_id) before relaunching.
        # Process it HERE — before build_future_features_tools (which runs
        # reset_stale_in_progress) and before cron starts — so the feature
        # is already marked implemented/failed before anything can re-queue it.
        _last_deploy_file = Path.home() / ".ghost" / "evolve" / "last_deploy.json"
        try:
            if _last_deploy_file.exists():
                import json as _json
                deploy_info = _json.loads(_last_deploy_file.read_text(encoding="utf-8"))
                _last_deploy_file.unlink(missing_ok=True)

                deployed_evo_id = deploy_info.get("evolution_id", "")
                feature_id = deploy_info.get("feature_id", "")
                is_rollback = deploy_info.get("rollback", False)

                if is_rollback and feature_id:
                    self._features_store.mark_failed(
                        feature_id, "Evolution was rolled back")
                    log.info("Auto-failed feature %s after rollback", feature_id)
                elif feature_id and not is_rollback:
                    self._features_store.mark_implemented(
                        feature_id,
                        f"Auto-completed after deploy of evolution {deployed_evo_id}",
                    )
                    log.info("Auto-completed feature %s after deploy %s",
                             feature_id, deployed_evo_id)
        except Exception as e:
            log.warning("Post-deploy feature cleanup failed: %s", e)

        def _on_queue_change():
            if not self.cron:
                return
            if not self._features_store.is_queue_ready():
                return
            self.cron.fire_now(_FEATURE_IMPLEMENTER_JOB)

        def _on_enable_blocked_tool(tool_name):
            """Enable a tool that was disabled pending a core dependency fix."""
            if not self.tool_manager:
                return {"status": "error", "error": "ToolManager not initialized"}
            return self.tool_manager.enable_tool(tool_name, update_yaml=True)

        for tool_def in build_future_features_tools(
            cfg,
            on_queue_change=_on_queue_change,
            on_enable_tool=_on_enable_blocked_tool,
        ):
            self.tool_registry.register(tool_def)

        # Goal Engine (Persistent Cognitive Architecture for user goals)
        if cfg.get("enable_goals", True):
            for tool_def in build_goal_tools(GoalStore()):
                self.tool_registry.register(tool_def)
            # Deterministic executor tool — used by cron and direct invocation
            for tool_def in build_goal_executor_tool(
                cfg=cfg,
                tool_registry=self.tool_registry,
                auth_store=getattr(self, "auth_store", None),
                provider_chain=getattr(self, "provider_chain", None),
            ):
                self.tool_registry.register(tool_def)

        # Reconcile stale open/reviewing PRs from previous runs.
        # If a PR is open but its evolution is no longer active, auto-close it
        # and re-queue the linked feature with reviewer feedback so the
        # implementer can produce a new PR cycle.
        try:
            from ghost_pr import get_pr_store
            pr_store = get_pr_store()
            stale_closed = 0
            requeued = 0

            for pr in pr_store.list_prs():
                if pr.get("status") not in ("open", "reviewing"):
                    continue
                evo_id = pr.get("evolution_id", "")
                pr_id = pr.get("pr_id", "")
                feature_id = pr.get("feature_id", "")
                evo_active = bool(
                    self.evolve_engine and evo_id in self.evolve_engine._active_evolutions
                )
                if evo_active:
                    continue

                reason = (
                    "Auto-closed stale PR on startup: evolution context is no longer active. "
                    "Feature has been re-queued for a new implementation and PR cycle."
                )
                pr_store.set_verdict(pr_id, "rejected", reason)
                stale_closed += 1

                try:
                    from ghost_evolve import _log_reviewer_mistakes
                    _log_reviewer_mistakes(pr, pr_id, pr.get("title", ""))
                except Exception as e:
                    log.warning("Failed to log reviewer mistakes: %s", e)

                if feature_id:
                    ok_retry, retry_status = self._features_store.mark_review_rejected(
                        feature_id, reason, max_retries=3
                    )
                    if ok_retry and retry_status == "pending":
                        requeued += 1

            if stale_closed:
                print(f"  [PR] Auto-closed {stale_closed} stale open/reviewing PR(s)")
                if self.cron and self._features_store.is_queue_ready():
                    self.cron.fire_now(_FEATURE_IMPLEMENTER_JOB)
                if requeued:
                    console_bus.emit(
                        "warning", "system", "stale_pr_requeue",
                        f"Re-queued {requeued} feature(s) after stale PR cleanup",
                    )
        except Exception as e:
            log.warning("Failed to check stale PRs: %s", e)

        # Expose shell-hardening impact assessor for internal policy decisions
        self.assess_command_hardening_impact = assess_command_hardening_impact

        # Wire deploy safety: let EvolutionEngine check active cron jobs before restart
        if self.evolve_engine and self.cron:
            self.evolve_engine.set_active_jobs_fn(self.cron.get_active_count)
        # Hybrid Memory tools (FTS5 + vector search, replaces old vector memory)
        if cfg.get("enable_vector_memory", True):
            _api_key = self.auth_store.get_api_key("openrouter") or os.environ.get("OPENROUTER_API_KEY")
            for tool_def in build_hybrid_memory_tools(_api_key, cfg=cfg):
                self.tool_registry.register(tool_def)

        # Code Intelligence tools (code analysis and insights)
        if cfg.get("enable_code_intel", True):
            for tool_def in build_code_intel_tools():
                self.tool_registry.register(tool_def)

        # Data Extraction tools (structured data from text)
        if cfg.get("enable_data_extract", True):
            for tool_def in build_data_extract_tools():
                self.tool_registry.register(tool_def)

        # Web Search tools (multi-provider: Perplexity, Grok, Brave, Gemini with fallback)
        if cfg.get("enable_web_search", True):
            for tool_def in build_web_search_tools(cfg=cfg):
                self.tool_registry.register(tool_def)

        # Web Fetch tools (Readability + Firecrawl + SSRF protection + security wrapping)
        if cfg.get("enable_web_fetch", True):
            for tool_def in build_web_fetch_tools(cfg=cfg):
                self.tool_registry.register(tool_def)

        # Image Generation tools (multi-provider: OpenRouter, Google Gemini, OpenAI)
        if cfg.get("enable_image_gen", True):
            for tool_def in build_image_gen_tools(auth_store=self.auth_store, cfg=cfg):
                self.tool_registry.register(tool_def)

        # Vision / Image Analysis tools (OpenAI, Gemini, Anthropic, Ollama)
        if cfg.get("enable_vision", True):
            for tool_def in build_vision_tools(auth_store=self.auth_store, cfg=cfg):
                self.tool_registry.register(tool_def)

        # Text-to-Speech tools (Edge TTS free, OpenAI, ElevenLabs)
        if cfg.get("enable_tts", True):
            for tool_def in build_tts_tools(auth_store=self.auth_store, cfg=cfg):
                self.tool_registry.register(tool_def)

        # Voice Wake + Talk Mode (always-on speech, continuous conversation)
        if cfg.get("enable_voice", True):
            for tool_def in build_voice_tools(auth_store=self.auth_store, cfg=cfg):
                self.tool_registry.register(tool_def)

        # Canvas (visual output panel — HTML/CSS/JS rendered beside the chat)
        if cfg.get("enable_canvas", True):
            for tool_def in build_canvas_tools(cfg=cfg):
                self.tool_registry.register(tool_def)

        # ── HuggingFace authentication (needed for gated models like FLUX) ──
        _hf_login(cfg)

        # ── Tool Event Bus (generic pub/sub for lifecycle hooks) ──
        self.tool_event_bus = ToolEventBus()

        if self.evolve_engine:
            self.evolve_engine.tool_event_bus = self.tool_event_bus

        # NOTE: evolve_engine.tool_manager is wired after ToolManager init (below)

        # ── GhostNodes: AI Plugin Ecosystem ─────────────────────────
        self.resource_manager = None
        self.node_manager = None
        self.media_store = None
        self.pipeline_engine = None
        self.node_registry = None
        self.cloud_providers = None

        # Self-repair safety valves for heavy GhostNodes initialization.
        # 1) Manual override via env var.
        # 2) Automatic one-boot skip when recovering from a SIGKILL/-9 crash report,
        #    which strongly suggests OS memory pressure killed the process.
        _disable_nodes_env = os.getenv("GHOST_DISABLE_NODES", "").strip().lower() in {"1", "true", "yes", "on"}
        _disable_nodes_crash = False
        try:
            _crash_path = GHOST_HOME / "crash_report.json"
            if _crash_path.exists():
                _crash_data = json.loads(_crash_path.read_text(encoding="utf-8"))
                if int(_crash_data.get("exit_code", 0)) == -9:
                    _disable_nodes_crash = True
        except Exception:
            # Never fail startup because crash-report parsing failed.
            _disable_nodes_crash = False

        _disable_nodes = _disable_nodes_env or _disable_nodes_crash
        if cfg.get("enable_nodes", True) and not _disable_nodes:
            try:
                self.resource_manager = ResourceManager(cfg)
                self.media_store = MediaStore(cfg)
                self.media_store.tool_event_bus = self.tool_event_bus
                self.cloud_providers = ProviderRegistry(cfg)
                self.node_manager = NodeManager(
                    tool_registry=self.tool_registry,
                    resource_manager=self.resource_manager,
                    media_store=self.media_store,
                    cloud_providers=self.cloud_providers,
                    cfg=cfg,
                )
                self.node_manager.load_all()
            except Exception as e:
                log.warning("GhostNodes core init error (non-fatal): %s", e, exc_info=True)

            if self.node_manager:
                try:
                    self.pipeline_engine = PipelineEngine(
                        tool_registry=self.tool_registry,
                        node_manager=self.node_manager,
                    )
                except Exception as e:
                    log.warning("Pipeline engine init error (non-fatal): %s", e, exc_info=True)

                try:
                    for tool_def in build_resource_manager_tools(self.resource_manager):
                        self.tool_registry.register(tool_def)
                    for tool_def in build_node_manager_tools(self.node_manager):
                        self.tool_registry.register(tool_def)
                    if self.media_store:
                        for tool_def in build_media_store_tools(self.media_store):
                            self.tool_registry.register(tool_def)
                    if self.pipeline_engine:
                        for tool_def in build_pipeline_tools(self.pipeline_engine):
                            self.tool_registry.register(tool_def)
                except Exception as e:
                    log.warning("GhostNodes tool registration error (non-fatal): %s", e, exc_info=True)

                try:
                    self.node_registry = NodeRegistry(cfg)
                    for tool_def in build_node_registry_tools(self.node_registry, self.node_manager):
                        self.tool_registry.register(tool_def)
                except Exception as e:
                    log.warning("Node registry init error (non-fatal): %s", e, exc_info=True)

                try:
                    for tool_def in build_node_sdk_tools():
                        self.tool_registry.register(tool_def)
                except Exception as e:
                    log.warning("Node SDK init error (non-fatal): %s", e, exc_info=True)

                node_count = len(self.node_manager.nodes)
                node_tools = self.node_manager.get_node_tools()
                dev_info = self.resource_manager.device_info
                device_str = dev_info.best_device
                if dev_info.has_mlx:
                    device_str += f" (MLX v{dev_info.mlx_version})"
                log.info("GhostNodes loaded: %d nodes, %d tools | device=%s",
                         node_count, len(node_tools), device_str)
        elif _disable_nodes_env:
            log.warning("GhostNodes initialization skipped (GHOST_DISABLE_NODES is set)")
        elif _disable_nodes_crash:
            log.warning("GhostNodes initialization skipped after previous SIGKILL (-9) crash for safe recovery")

        # ── Community Hub: Node Marketplace ──────────────
        self.community_hub = None
        try:
            gh_token = cfg.get("github_token", "")
            self.community_hub = CommunityHub(github_token=gh_token)
            for tool_def in build_community_hub_tools(
                self.community_hub,
                node_manager=self.node_manager,
            ):
                self.tool_registry.register(tool_def)
        except Exception as e:
            log.warning("Community Hub init error (non-fatal): %s", e, exc_info=True)

        # ── Ghost Tool Builder: LLM-callable tool ecosystem ─────
        self.tool_manager = None
        try:
            self.tool_manager = ToolManager(
                tool_registry=self.tool_registry,
                event_bus=self.tool_event_bus,
                cfg=cfg,
                memory_db=self.memory_db,
            )
            self.tool_manager.discover_all()
            tool_count, tool_names = self.tool_manager.load_all()
            for tool_def in build_tool_manager_tools(self.tool_manager):
                self.tool_registry.register(tool_def)
            if self.cron:
                for _tname, tinfo in self.tool_manager.tools.items():
                    for cron_def in tinfo.crons:
                        self.cron.add_job(
                            name=cron_def["name"],
                            schedule=cron_def["schedule"],
                            payload={"type": "ghost_tool_cron", "callback": cron_def["callback"]},
                            description=f"Ghost tool cron: {cron_def['name']}",
                        )
            # Report load results prominently
            discovered = len(self.tool_manager.tools)
            failed_tools = [
                (n, i.error) for n, i in self.tool_manager.tools.items()
                if i.enabled and not i.loaded and i.error
            ]
            if failed_tools:
                log.error(
                    "Ghost Tools: %d/%d loaded, %d FAILED: %s",
                    tool_count, discovered, len(failed_tools),
                    "; ".join(f"{n}: {e.split(chr(10))[0]}" for n, e in failed_tools),
                )
            elif tool_count:
                log.info("Ghost Tools loaded: %d/%d tools providing %d LLM tools",
                         tool_count, discovered, len(tool_names))
        except Exception as e:
            log.warning("ToolManager init error (non-fatal): %s", e, exc_info=True)

        if self.evolve_engine and self.tool_manager:
            self.evolve_engine.tool_manager = self.tool_manager

        # Resolve blocked tools from features completed before last restart.
        # Must run AFTER ToolManager.discover_all()/load_all() so tools exist.
        if self.tool_manager and self._features_store:
            try:
                pending = self._features_store.resolve_pending_tool_enables()
                if pending:
                    from collections import defaultdict
                    by_feature = defaultdict(list)
                    for fid, tool_name in pending:
                        by_feature[fid].append(tool_name)
                    for fid, tools in by_feature.items():
                        all_ok = True
                        for tool_name in tools:
                            try:
                                result = self.tool_manager.enable_tool(tool_name, update_yaml=True)
                                if result and result.get("status") == "ok":
                                    print(f"  [FUTURE_FEATURES] Auto-enabled blocked tool: {tool_name}")
                                else:
                                    print(f"  [FUTURE_FEATURES] Could not enable {tool_name}: {result}")
                                    all_ok = False
                            except Exception as e:
                                print(f"  [FUTURE_FEATURES] Error enabling {tool_name}: {e}")
                                all_ok = False
                        if all_ok:
                            self._features_store._mark_tools_enabled(fid)
            except Exception as e:
                log.warning("Startup tool-enable resolution failed (non-fatal): %s", e)

        # Security Audit tools (self-auditing + auto-fix)
        if cfg.get("enable_security_audit", True):
            for tool_def in build_security_audit_tools(cfg=cfg):
                self.tool_registry.register(tool_def)

        # Runtime Config Management tools (read/patch config, schema)
        for tool_def in build_config_tools(cfg=cfg):
            self.tool_registry.register(tool_def)

        # Config payload normalization tools (dashboard/config UX wiring helpers)
        for tool_def in build_config_payload_tools(cfg):
            self.tool_registry.register(tool_def)

        # Projects tools (first-class workspace/project scoping)
        self.project_registry = ProjectRegistry()
        for tool_def in build_project_tools(self.project_registry, cfg):
            self.tool_registry.register(tool_def)

        # Structured LLM Task tool (JSON-only subtasks with schema validation)
        for tool_def in build_llm_task_tools(engine=self.engine):
            self.tool_registry.register(tool_def)

        # Real-time Console (agent can log to the dashboard terminal)
        for tool_def in build_console_tools(cfg=cfg):
            self.tool_registry.register(tool_def)

        # Mid-generation interrupt and prompt injection tools
        for tool_def in make_interrupt_tools(config=cfg):
            self.tool_registry.register(tool_def)

        # Multi-Channel Messaging (Telegram, Slack, Discord, ntfy, etc.)
        self.channel_registry = None
        self.channel_router = None
        self.channel_inbound = None
        if cfg.get("enable_channels", True):
            try:
                registry, router, make_inbound = init_channels(cfg)
                self.channel_registry = registry
                self.channel_router = router
                for tool_def in build_channel_tools(router, registry, cfg):
                    self.tool_registry.register(tool_def)
                # Re-register notify with channel router for multi-channel delivery
                self.tool_registry.register(make_notify(cfg, channel_router=router))
                # Re-register autonomy tools with channel router for proactive alerts
                for tool_def in build_autonomy_tools(
                        self.action_store, self.growth_logger,
                        channel_router=router):
                    self.tool_registry.register(tool_def)
                # Phase 2: Register advanced tools (actions, threading, directory, etc.)
                for tool_def in build_phase2_tools(router, registry, cfg):
                    self.tool_registry.register(tool_def)
                # Channel Security tools (audit, quarantine, allowlist management)
                if cfg.get("enable_channel_security", True):
                    for tool_def in build_channel_security_tools(cfg):
                        self.tool_registry.register(tool_def)
                self._make_inbound = make_inbound
                self._health_monitor = None
                if cfg.get("enable_channel_health_monitor", True):
                    try:
                        from ghost_channels.health import HealthMonitor
                        self._health_monitor = HealthMonitor(
                            registry, router,
                            check_interval=cfg.get("channel_health_check_interval", 300.0),
                        )
                    except Exception as e:
                        log.warning("Failed to init health monitor: %s", e)
            except Exception as e:
                import traceback
                traceback.print_exc()

        # Persistent Shell Sessions & Background Processes
        self.shell_sessions = None
        try:
            from ghost_shell_sessions import ShellSessionManager, build_shell_session_tools
            self.shell_sessions = ShellSessionManager(cfg)
            for tool_def in build_shell_session_tools(cfg, self.shell_sessions):
                self.tool_registry.register(tool_def)
        except Exception as e:
            print(f"  [shell-sessions] Failed to initialize: {e}")

        # Webhook Triggers
        self.webhook_handler = None
        try:
            from ghost_webhooks import WebhookRegistry, WebhookHandler, build_webhook_tools
            _wh_registry = WebhookRegistry()
            self.webhook_handler = WebhookHandler(_wh_registry, cfg, daemon=self)
            for tool_def in build_webhook_tools(self.webhook_handler, cfg):
                self.tool_registry.register(tool_def)
        except Exception as e:
            print(f"  [webhooks] Failed to initialize: {e}")

        # PR tools
        try:
            for tool_def in build_pr_tools(cfg=cfg):
                self.tool_registry.register(tool_def)
        except Exception as e:
            print(f"  [pr] Failed to initialize: {e}")

        # Model Context Protocol (MCP) — bridge external tool servers into the registry
        self.mcp_manager = None
        if cfg.get("enable_mcp", True) and not dry_run:
            try:
                from ghost_mcp import MCPManager, build_mcp_introspection_tools
                self.mcp_manager = MCPManager()
                mcp_defs = self.mcp_manager.connect_all()
                for td in mcp_defs:
                    self.tool_registry.register(td)
                for td in build_mcp_introspection_tools(self.mcp_manager):
                    self.tool_registry.register(td)
                if mcp_defs:
                    log.info("MCP: bridged %d tools from %d server(s)",
                             len(mcp_defs), len(self.mcp_manager.clients))
            except Exception as e:
                log.warning("MCP initialization failed: %s", e)
                self.mcp_manager = None

        # Focused Delegation — fresh-context research and verification
        if cfg.get("enable_subagents", True):
            try:
                for tool_def in build_subagent_tools(
                    cfg=cfg,
                    tool_registry=self.tool_registry,
                    auth_store=self.auth_store,
                    provider_chain=self.provider_chain,
                ):
                    self.tool_registry.register(tool_def)
                print("  [delegate] Initialized focused delegation tool")
            except Exception as e:
                print(f"  [delegate] Failed to initialize: {e}")

        # Typed Subagent System (mirrored from DeerFlow, with parallel auto-collect)
        try:
            for tool_def in build_typed_subagent_tools(
                cfg=cfg,
                tool_registry=self.tool_registry,
                auth_store=self.auth_store,
                provider_chain=self.provider_chain,
                event_bus=self.tool_event_bus,
            ):
                self.tool_registry.register(tool_def)
            print("  [task] Initialized typed subagent tools (researcher, coder, bash, reviewer)")
        except Exception as e:
            print(f"  [task] Failed to initialize typed subagents: {e}")

        # Structured Memory (mirrored from DeerFlow)
        try:
            mem_cfg = cfg.get("structured_memory", {})
            if mem_cfg:
                set_structured_memory_config(StructuredMemoryConfig.from_dict(mem_cfg))
            for tool_def in build_structured_memory_tools(engine=self.engine):
                self.tool_registry.register(tool_def)
            get_memory_queue(engine=self.engine)
            print("  [structured_memory] Initialized structured memory system")
        except Exception as e:
            print(f"  [structured_memory] Failed to initialize: {e}")

        # Middleware pipeline (shared pre/post-processing for all entry points)
        self.middleware_chain = self._build_middleware_chain()

    def _track_tool_calls(self, count):
        """Increment tool call counter and sync to cfg for introspection tools."""
        self._tool_count += count
        self.cfg["session_tool_calls"] = self._tool_count

    def _track_cron_done(self):
        """Increment cron completion counter and sync to cfg."""
        self._cron_completed += 1
        self.cfg["cron_completed_jobs"] = self._cron_completed

    def _build_middleware_chain(self):
        from ghost_middleware import build_default_chain
        return build_default_chain()

    def _load_soul(self):
        """Load SOUL.md with mtime caching."""
        try:
            mtime = SOUL_FILE.stat().st_mtime if SOUL_FILE.exists() else 0
        except OSError:
            mtime = 0
        if mtime != self._soul_mtime:
            self._soul_cache = load_soul()
            self._soul_mtime = mtime
        return self._soul_cache

    def _load_user(self):
        """Load USER.md with mtime caching."""
        try:
            mtime = USER_FILE.stat().st_mtime if USER_FILE.exists() else 0
        except OSError:
            mtime = 0
        if mtime != self._user_mtime:
            self._user_cache = load_user()
            self._user_mtime = mtime
        return self._user_cache


    def _build_identity_context(self):
        """Build the identity preamble from SOUL.md + USER.md + platform info.

        Returns a string to prepend to any system prompt.
        Hot-reloads on file change (mtime check).
        """
        parts = [ghost_platform.platform_context()]

        soul = self._load_soul()
        user = self._load_user()

        if soul:
            parts.append(
                "## Your Identity (SOUL.md)\n\n"
                "Embody this persona and tone. Avoid stiff, generic replies; "
                "follow this guidance unless task-specific instructions override it.\n\n"
                + soul
            )
        if user:
            parts.append(
                "## About the User (USER.md)\n\n"
                "This is who you're helping. Use this context to personalize "
                "your responses. Update USER.md via file_write when you learn "
                "something new about the user.\n\n"
                + user
            )

        try:
            memory_context = format_memory_for_injection()
            if memory_context.strip():
                parts.append(
                    "## Persistent Memory\n\n"
                    "Context remembered from prior conversations. "
                    "Use this to personalize responses and avoid re-asking.\n\n"
                    + memory_context
                )
        except Exception:
            pass

        return "\n\n".join(parts) + "\n\n---\n\n"

    _BROWSER_TOOL_NAMES = frozenset({
        "browser",
    })

    def _resolve_skill_model(self, matched_skills):
        """Return the effective model override for matched skills.

        Priority: config skill_model_overrides > SKILL.md frontmatter model > None
        """
        if not matched_skills:
            return None
        overrides = self.cfg.get("skill_model_overrides", {})
        for skill in matched_skills:
            config_override = overrides.get(skill.name)
            if config_override:
                return config_override
            if skill.model:
                return skill.model
        return None

    def _cleanup_browser_after_task(self, tools_used: list):
        """Close browser windows opened during a tool loop session.

        Called after each tool loop completes so Chromium windows don't
        accumulate across autonomous tasks.
        """
        if not any(t in self._BROWSER_TOOL_NAMES for t in tools_used):
            return
        try:
            _browser_stop()
        except Exception as e:
            log.warning("Post-task browser cleanup failed: %s", e)
        pass

    def _cleanup_stuck_features(self, tool_calls):
        """Auto-reset or auto-complete features after the implementer finishes.

        Checks actual tool results (not just tool names) to determine whether
        the deploy succeeded. Handles three scenarios:
        1. Feature belongs to this run + deploy succeeded → mark implemented
        2. Feature belongs to this run + rolled back → mark failed
        3. Feature is in_progress but NOT part of this run → reset to pending
           (prevents features stuck forever when implementer works on something else)
        """
        tool_names = set(tc["tool"] for tc in tool_calls)

        completed_features = set()
        failed_features = set()
        for tc in tool_calls:
            if tc["tool"] == "complete_future_feature":
                result = tc.get("result", "")
                if "completed" in result.lower():
                    match = result.split("[")[1].split("]")[0] if "[" in result else ""
                    if match:
                        completed_features.add(match)
            elif tc["tool"] == "fail_future_feature":
                result = tc.get("result", "")
                if "failed" in result.lower():
                    match = result.split(":")[1].strip().split()[0] if ":" in result else ""
                    if match:
                        failed_features.add(match)

        deploy_succeeded = False
        for tc in tool_calls:
            if tc["tool"] in ("evolve_deploy", "evolve_submit_pr"):
                result = tc.get("result", "")
                if "deployed" in result.lower() and "cannot deploy" not in result.lower():
                    deploy_succeeded = True
                    break

        run_feature_ids = set()
        for tc in tool_calls:
            if tc["tool"] == "start_future_feature":
                result = tc.get("result", "")
                if "started" in result.lower():
                    match = result.split(":")[1].strip().split()[0] if ":" in result else ""
                    if match:
                        run_feature_ids.add(match)

        run_evolution_ids = set()
        for tc in tool_calls:
            if tc["tool"] == "evolve_plan":
                result = tc.get("result", "")
                if "Evolution planned:" in result:
                    evo_id = result.split("Evolution planned:")[1].strip().split()[0]
                    run_evolution_ids.add(evo_id)

        was_rolled_back = any(
            tc["tool"] == "evolve_rollback" for tc in tool_calls
        )
        if not was_rolled_back:
            last_test = None
            for tc in tool_calls:
                if tc["tool"] == "evolve_test":
                    last_test = tc
            if last_test and "FAILED" in last_test.get("result", ""):
                was_rolled_back = True

        try:
            in_progress = list(self._features_store.get_all("in_progress"))
            for feat in in_progress:
                fid = feat["id"]

                if fid in completed_features or fid in failed_features:
                    continue

                feat_evo = feat.get("evolution_id", "")
                belongs_to_run = (
                    fid in run_feature_ids
                    or feat_evo in run_evolution_ids
                )

                if deploy_succeeded and belongs_to_run:
                    self._features_store.mark_implemented(
                        fid,
                        "Auto-completed: evolve_submit_pr/evolve_deploy succeeded but complete_future_feature was not called before deploy",
                    )
                    console_bus.emit(
                        "success", "system", "feature_auto_complete",
                        f"Auto-completed feature {fid[:10]} after successful deploy",
                    )
                elif was_rolled_back and belongs_to_run:
                    self._features_store.mark_failed(
                        fid,
                        "Evolution failed tests and was rolled back",
                    )
                    console_bus.emit(
                        "warning", "system", "feature_failed",
                        f"Marked feature {fid[:10]} as failed after rollback",
                    )
                elif belongs_to_run:
                    self._features_store._update_status(
                        fid, "pending",
                        "Auto-reset: implementer did not complete the evolve cycle",
                    )
                    console_bus.emit(
                        "warning", "system", "feature_reset",
                        f"Auto-reset stuck feature {fid[:10]} back to pending",
                    )
                else:
                    self._features_store._update_status(
                        fid, "pending",
                        "Auto-reset: feature was in_progress but implementer worked on a different feature",
                    )
                    console_bus.emit(
                        "warning", "system", "feature_reset",
                        f"Auto-reset orphaned feature {fid[:10]} back to pending",
                    )
        except Exception as e:
            log.warning("Failed to reset orphaned feature: %s", e)

    def _build_provider_chain(self, model, fallback_models):
        """Build the provider-aware fallback chain from configured profiles.

        Respects primary_provider config, provider_order from auth profiles,
        and per-provider model selections from provider_models config.
        """
        from ghost_providers import PROVIDERS, get_provider, validate_model_for_provider, run_provider_model_validation_selfcheck

        primary_provider = self.cfg.get("primary_provider", "openrouter")
        provider_models = self.cfg.get("provider_models", {})
        chain = []
        seen = set()

        def _add(pid, mdl):
            key = f"{pid}:{mdl}"
            if key not in seen:
                seen.add(key)
                chain.append((pid, mdl))

        def _model_for(pid, prov):
            """Resolve the selected model for a provider with validation."""
            configured_model = provider_models.get(pid)
            if configured_model:
                valid, normalized_or_reason = validate_model_for_provider(pid, configured_model)
                if valid:
                    return normalized_or_reason
                console_bus.emit(
                    "warning", "llm", "provider_chain",
                    f"Ignoring invalid configured model for {pid}: {configured_model} ({normalized_or_reason})",
                )
            return prov.default_model

        ALL_PROVIDERS = ["openrouter", "openai", "openai-codex",
                         "anthropic", "google", "deepseek", "ollama"]

        explicit_order = self.auth_store.provider_order or []
        provider_order = list(explicit_order)
        for pid in ALL_PROVIDERS:
            if pid not in provider_order:
                provider_order.append(pid)

        if primary_provider not in provider_order:
            provider_order.insert(0, primary_provider)
        elif provider_order[0] != primary_provider:
            provider_order.remove(primary_provider)
            provider_order.insert(0, primary_provider)

        for pid in provider_order:
            prov = get_provider(pid)
            if not prov:
                continue
            if not self.auth_store.is_provider_configured(pid):
                if pid != "ollama":
                    continue

            if pid == "openrouter":
                configured_or_model = provider_models.get("openrouter")
                if configured_or_model:
                    valid, normalized_or_reason = validate_model_for_provider("openrouter", configured_or_model)
                    if valid:
                        or_model = normalized_or_reason
                    else:
                        console_bus.emit(
                            "warning", "llm", "provider_chain",
                            f"Ignoring invalid configured model for openrouter: {configured_or_model} ({normalized_or_reason})",
                        )
                        or_model = model
                else:
                    or_model = model

                valid_primary, normalized_primary_or_reason = validate_model_for_provider("openrouter", or_model)
                if valid_primary:
                    _add(pid, normalized_primary_or_reason)
                else:
                    console_bus.emit(
                        "warning", "llm", "provider_chain",
                        f"Ignoring invalid effective model for openrouter: {or_model} ({normalized_primary_or_reason})",
                    )
                    _add(pid, PROVIDERS["openrouter"].default_model)

                for fm in fallback_models:
                    valid_fm, normalized_fm_or_reason = validate_model_for_provider("openrouter", fm)
                    if valid_fm:
                        _add(pid, normalized_fm_or_reason)
                    else:
                        console_bus.emit(
                            "warning", "llm", "provider_chain",
                            f"Ignoring invalid fallback model for openrouter: {fm} ({normalized_fm_or_reason})",
                        )
            else:
                _add(pid, _model_for(pid, prov))

        return chain

    def _cron_fire(self, job):
        """Callback when a cron job fires. Routes payload to the right handler."""
        if PAUSE_FILE.exists():
            return
        job_name = job.get("name", "unnamed")
        payload = job.get("payload", {})
        ptype = payload.get("type", "task")
        console_bus.emit("info", "cron", job_name, f"Cron fired ({ptype})")
        self.actions_today += 1

        if job_name == _FEATURE_IMPLEMENTER_JOB and not self.cfg.get("enable_future_features", True):
            self.cfg["enable_future_features"] = True
            console_bus.emit(
                "warning",
                "security",
                "future_features_guard",
                "Blocked insecure runtime disable of future features queue; forced enable_future_features=true",
            )
            append_feed({
                "type": "security",
                "preview": "Blocked insecure config: forced enable_future_features=true",
                "result": "Runtime guard prevented disabling future-features queue",
            })

        if job_name == _IMPLEMENTATION_AUDITOR_JOB:
            if self.cron and self.cron.is_job_running(_FEATURE_IMPLEMENTER_JOB):
                console_bus.emit(
                    "info", "cron", job_name,
                    "Skipped — Feature Implementer is running. Will retry next cycle.",
                )
                return

        # Goal Executor — run directly without LLM wrapper (deterministic engine)
        if job_name == _GOAL_EXECUTOR_JOB:
            if not self.cfg.get("enable_goals", True):
                return
            self._run_goal_executor_direct()
            return

        if ptype == "task":
            prompt = payload.get("prompt", "")
            if not prompt:
                return
            is_evolution_runner = job.get("name") == _FEATURE_IMPLEMENTER_JOB
            cron_prompt_body = (
                "You are Ghost, an autonomous AI agent. A scheduled task has fired.\n"
                f"Task name: {job.get('name', 'unnamed')}\n"
                f"Description: {job.get('description', 'none')}\n\n"
                "Complete the task below. Use available tools as needed. "
                "Be thorough and report what you did.\n\n"
                "## CODE SEARCH\n"
                "Use `grep` for fast regex content search and `glob` for file pattern matching.\n"
                "These are faster than shell_exec with grep/find. Prefer them for code exploration.\n\n"
                "## DEVELOPMENT STANDARDS (MANDATORY for all code changes)\n"
                "- NEVER hardcode secrets. Validate all inputs. Sanitize file paths.\n"
                "- Whitelist shell commands. Scope API tokens minimally. Never log secrets.\n"
                "- Protect user data: summaries only, never verbatim content. Pin dependency versions.\n"
            )
            if self.cfg.get("enable_tool_loop", True) and self.tool_registry.get_all():
                from ghost_middleware import InvocationContext
                timeout_s = self.cfg.get(
                    "cron_job_timeout",
                    _CRON_JOB_TIMEOUTS.get(job_name, _CRON_DEFAULT_TIMEOUT),
                )
                deadline = time.time() + timeout_s
                _timed_out = False

                def _cron_cancel_check():
                    nonlocal _timed_out
                    if time.time() >= deadline:
                        _timed_out = True
                        return f"(Timed out after {timeout_s}s)"
                    return False

                coding_model = None
                coding_chain = None
                coding_jobs = self.cfg.get("coding_jobs", [
                    _FEATURE_IMPLEMENTER_JOB, "_ghost_growth_bug_hunter",
                ])
                if job_name in coding_jobs:
                    budget = self.cfg.get("coding_model_budget", "auto")
                    if str(budget).strip().lower() == "free":
                        log.info(
                            "Skipping coding job %s — budget is 'free', "
                            "self-evolution disabled to avoid low-quality output",
                            job_name,
                        )
                        console_bus.emit(
                            "info", "cron", job_name,
                            "Skipped: coding budget is 'free' — set budget to "
                            "'low' or higher to enable self-evolution",
                        )
                        return
                    try:
                        from ghost_model_dispatch import get_dispatcher
                        dispatcher = get_dispatcher(self.cfg, self.auth_store)
                        coding_chain = dispatcher.select_chain("coding")
                        if coding_chain:
                            p, m = coding_chain[0]
                            coding_model = m if p == "openrouter" else f"{p}:{m}"
                    except Exception as _dispatch_err:
                        log.warning("Model dispatch failed: %s", _dispatch_err)

                if is_evolution_runner and self._features_store:
                    _in_prog = list(self._features_store.get_all("in_progress"))
                    if _in_prog:
                        pass  # resume flow — LLM picks up the in_progress feature
                    else:
                        _next = self._features_store.get_next_implementable()
                        if not _next:
                            console_bus.emit("info", "cron", job_name,
                                             "No implementable features found")
                            return
                        _ok, _err = self._features_store.mark_in_progress(
                            _next["id"], force=True)
                        if _ok:
                            prompt += (
                                f"\n\nPRE-SELECTED FEATURE: {_next['id']}\n"
                                f"This feature has already been marked in_progress. "
                                f"Skip step 1 (list) and go directly to step 2: "
                                f"get_future_feature('{_next['id']}').\n"
                            )

                inv = InvocationContext(
                    source="cron",
                    user_message=prompt,
                    system_prompt_parts=[cron_prompt_body],
                    tool_registry=self.tool_registry,
                    daemon=self,
                    engine=self.engine,
                    config=self.cfg,
                    max_steps=50 if is_evolution_runner else self.cfg.get("tool_loop_max_steps", 200),
                    max_tokens=16384 if is_evolution_runner else 4096,
                    on_step=terminal_step,
                    cancel_check=_cron_cancel_check,
                    model_override=coding_model,
                    coding_model_chain=coding_chain,
                    meta={
                        "is_evolution_runner": is_evolution_runner,
                        "job_name": job_name,
                    },
                )
                self.middleware_chain.invoke(inv)

                if _timed_out:
                    console_bus.emit(
                        "warning", "cron", job_name,
                        f"Session killed after {timeout_s}s timeout",
                    )

                result = inv.result_text
                tools_used = inv.tools_used
                tool_calls_log = inv.result.tool_calls if inv.result else []
                self._track_tool_calls(len(tool_calls_log))

                if inv.meta.get("engine_error"):
                    console_bus.emit("error", "cron", job_name,
                                     f"Tool loop crashed: {inv.meta['engine_error']}")

                if is_evolution_runner:
                    self._cleanup_stuck_features(tool_calls_log)
            else:
                result = self.llm.analyze("long_text", prompt)
                tools_used = []

            terminal_print("ask", f"[cron: {job_name}] {prompt[:40]}...", result)
            console_bus.emit(
                "success", "cron", job_name,
                f"Completed — {len(tools_used)} tool calls",
                result=result[:200],
            )
            entry = {
                "time": datetime.now().isoformat(),
                "type": "cron",
                "source": f"[cron: {job_name}] {prompt[:2000]}",
                "result": result,
            }
            if tools_used:
                entry["tools_used"] = tools_used
            append_feed(entry, self.cfg.get("max_feed_items", 50))
            self.context_memory.add(entry)
            log_action("cron", f"[{job_name}] {prompt[:60]}", result)

            if self.channel_router and result and job_name != _FEATURE_IMPLEMENTER_JOB:
                self._send_cron_summary(job, result, tools_used)

        elif ptype == "notify":
            title = payload.get("title", job.get("name", "Ghost Cron"))
            message = payload.get("message", "")
            if self.channel_router:
                try:
                    self.channel_router.send(f"**{title}**\n{message}",
                                             priority="normal", title=title)
                except Exception as e:
                    log.warning("Failed to send channel notification: %s", e)
            ghost_platform.send_notification(title, message)
            terminal_print("ask", f"[cron notify] {title}", message)

        elif ptype == "shell":
            command = payload.get("command", "")
            if not command:
                return
            try:
                r = subprocess.run(
                    command, shell=True, capture_output=True, text=True,
                    timeout=60, cwd=str(Path.home()),
                )
                out = (r.stdout or "") + (r.stderr or "")
                terminal_print("ask", f"[cron shell] {command[:40]}", out[:500] or "(no output)")
            except Exception as e:
                terminal_print("ask", f"[cron shell] {command[:40]}", f"Error: {e}")

        elif ptype == "ghost_tool_cron":
            cb = payload.get("callback")
            if callable(cb):
                try:
                    cb()
                except Exception as e:
                    log.warning("Tool cron %s failed: %s", job_name, e)
                    console_bus.emit("error", "cron", job_name, f"Tool cron error: {e}")

        elif ptype == "session_maintenance":
            try:
                from ghost_session_memory import run_maintenance
                result = run_maintenance(self.cfg)
                if result.get("status") == "success":
                    deleted = result.get("cleanup", {}).get("deleted_count", 0)
                    reclaimed = result.get("cleanup", {}).get("mb_reclaimed", 0)
                    console_bus.emit(
                        "success", "cron", job_name,
                        f"Session maintenance complete — {deleted} files deleted, {reclaimed:.2f} MB reclaimed",
                    )
                else:
                    console_bus.emit(
                        "info", "cron", job_name,
                        f"Session maintenance skipped — {result.get('reason', 'unknown')}",
                    )
            except Exception as e:
                log.warning("Session maintenance failed: %s", e)
                console_bus.emit("error", "cron", job_name, f"Session maintenance failed: {e}")

    def _send_cron_summary(self, job, result, tools_used):
        """Use the LLM to compose a concise notification from the cron result."""
        job_name = job.get("name", "unnamed")
        label = job_name.replace("_ghost_growth_", "").replace("_", " ").title()
        description = job.get("description", "")

        prompt = (
            f"You are Ghost, an autonomous AI agent. A scheduled task just finished.\n"
            f"Task: {label}\n"
            f"Description: {description}\n"
            f"Tools used: {len(tools_used)}\n\n"
            f"Full result:\n{result[:3000]}\n\n"
            "Write a concise Telegram notification (3-8 lines) summarizing what was done "
            "and any key findings. Use Markdown formatting. Lead with a bold title line. "
            "Highlight anything that needs user attention. Be specific — include real data "
            "points, not generic statements like 'all systems operational'. "
            "Do NOT include greetings or sign-offs. Just the summary."
        )
        try:
            summary = self.llm.analyze("long_text", prompt)
            if summary and not summary.startswith("LLM error"):
                self.channel_router.send(
                    summary, priority="low", title=f"Ghost: {label}",
                )
        except Exception as exc:
            log.debug("Cron summary notification failed: %s", exc)

    def _run_goal_executor_direct(self):
        """Run the goal executor engine directly — no LLM wrapper needed."""
        from ghost_goal_executor import GoalExecutorEngine, deliver_goal_results, reflect_on_goal_execution

        start = time.time()
        try:
            executor = GoalExecutorEngine(
                cfg=self.cfg,
                tool_registry=self.tool_registry,
                auth_store=getattr(self, "auth_store", None),
                provider_chain=getattr(self, "provider_chain", None),
            )
            result = executor.run_all()
            elapsed = time.time() - start

            processed = result.get("processed", 0)
            results = result.get("results", [])
            completed = sum(1 for r in results if r.get("completed"))

            if processed == 0:
                console_bus.emit("info", "cron", _GOAL_EXECUTOR_JOB,
                                 "No actionable goals")
                return

            console_bus.emit("success", "cron", _GOAL_EXECUTOR_JOB,
                             f"Processed {processed} goal(s), {completed} completed in {elapsed:.1f}s")

            # Deliver results (feed, notifications, channels)
            if results:
                deliver_goal_results(results, self)

            # Self-improvement: analyze execution and queue improvements
            if results and self.cfg.get("enable_goal_reflection", True):
                try:
                    n_features = reflect_on_goal_execution(results, self)
                    if n_features > 0:
                        console_bus.emit("info", "cron", _GOAL_EXECUTOR_JOB,
                                         f"Self-reflection submitted {n_features} improvement(s)")
                except Exception as refl_exc:
                    log.warning("[goal_executor] Reflection failed (non-fatal): %s", refl_exc)

            terminal_print("cron", f"[goal_executor] {processed} processed, {completed} completed",
                           result.get("message", f"{completed} goal(s) completed"))

        except Exception as exc:
            log.error("[goal_executor] Direct execution failed: %s", exc, exc_info=True)
            console_bus.emit("error", "cron", _GOAL_EXECUTOR_JOB,
                             f"Executor error: {exc}")

    def stop(self, *_):
        console_bus.emit("warn", "system", "daemon_stop", "Ghost shutting down")

        # Wait for active cron jobs (especially the Feature Implementer) to finish
        # before tearing down. Ghost should never die mid-work.
        if self.cron:
            _max_wait = 600  # 10 minutes max for long evolve cycles
            _waited = 0
            while self.cron.get_active_count() > 0 and _waited < _max_wait:
                if _waited == 0:
                    active = self.cron.get_active_jobs()
                    print(f"  {YLW}Waiting for {len(active)} active cron job(s) to finish before shutdown...{RST}")
                time.sleep(2)
                _waited += 2
            if _waited >= _max_wait:
                print(f"  {RED}Timed out waiting for cron jobs — forcing shutdown{RST}")

        if getattr(self, "tool_event_bus", None):
            try:
                self.tool_event_bus.emit("on_shutdown")
            except Exception:
                pass
        self.running = False
        if self.channel_inbound:
            try:
                self.channel_inbound.stop_all()
            except Exception as e:
                log.warning("Error stopping channel inbound: %s", e)
        # Phase 2: Stop health monitor and delivery queue
        if hasattr(self, "_health_monitor") and self._health_monitor:
            try:
                self._health_monitor.stop()
            except Exception as e:
                log.warning("Error stopping health monitor: %s", e)
        if self.channel_router:
            try:
                self.channel_router.disable_queue()
            except Exception as e:
                log.warning("Error disabling channel queue: %s", e)
        try:
            from ghost_dashboard import stop_dashboard
            stop_dashboard()
        except Exception as e:
            log.warning("Error stopping dashboard: %s", e)
        if self.cron:
            self.cron.stop()
        if self.shell_sessions:
            try:
                self.shell_sessions.cleanup_all()
            except Exception as e:
                log.warning("Error cleaning up shell sessions: %s", e)
        if self.hooks:
            self.hooks.run_void("on_shutdown")
        if getattr(self, "mcp_manager", None):
            try:
                self.mcp_manager.shutdown()
            except Exception as e:
                log.warning("Error shutting down MCP servers: %s", e)
        if self.memory_db:
            self.memory_db.prune(5000)
            self.memory_db.close()
        try:
            _browser_stop()
        except Exception as e:
            log.warning("Error stopping browser: %s", e)
        try:
            stop_voice_engine()
        except Exception as e:
            log.warning("Error stopping voice engine: %s", e)
        try:
            queue = get_memory_queue()
            if queue.pending_count > 0:
                log.info("Flushing %d pending structured memory updates...", queue.pending_count)
                queue.flush()
        except Exception as e:
            log.warning("Error flushing structured memory queue: %s", e)
        print(f"\n  {DIM}👻 Ghost fading away... {self.actions_today} actions this session.{RST}\n")

    @staticmethod
    def _clean_channel_reply(text: str) -> str:
        """Strip JSON wrappers from LLM replies before sending to channels.

        The LLM sometimes wraps responses in {"summary":"..."} or similar JSON.
        Extract the plain text so users see clean messages.
        """
        stripped = text.strip()
        if stripped.startswith("{") and stripped.endswith("}"):
            try:
                obj = json.loads(stripped)
                if isinstance(obj, dict):
                    for key in ("summary", "text", "message", "response", "reply", "content"):
                        if key in obj and isinstance(obj[key], str):
                            return obj[key]
                    vals = [v for v in obj.values() if isinstance(v, str) and len(v) > 10]
                    if len(vals) == 1:
                        return vals[0]
            except (json.JSONDecodeError, TypeError):
                pass
        return text

    def _build_channel_history(self, channel_id: str, max_turns: int = 10) -> list:
        """Load recent channel exchanges from feed to give the LLM conversation context."""
        try:
            feed = read_feed()
            channel_items = [
                i for i in feed
                if i.get("type") == "channel"
                and i.get("channel") == channel_id
                and i.get("result")
                and i.get("status") == "complete"
            ]
            channel_items.reverse()
            channel_items = channel_items[-max_turns:]

            history = []
            max_assistant_chars = 1500
            for item in channel_items:
                user_msg = item.get("source", "")
                prefix = f"[{channel_id}:"
                if prefix in user_msg:
                    user_msg = user_msg.split("] ", 1)[-1]
                user_msg = user_msg.strip()
                assistant_msg = (item.get("result") or "").strip()
                if user_msg and assistant_msg:
                    history.append({"role": "user", "content": user_msg})
                    if len(assistant_msg) > max_assistant_chars:
                        assistant_msg = (
                            assistant_msg[:max_assistant_chars]
                            + "\n...[previous response truncated for context budget]"
                        )
                    history.append({"role": "assistant", "content": assistant_msg})
            return history
        except Exception as e:
            log.warning("Failed to load conversation history: %s", e)
            return []

    def process_inbound(self, msg):
        """Handle an inbound message from a messaging channel (Telegram, Slack, etc.)."""
        self._msg_count += 1
        from ghost_channels import InboundMessage
        if not isinstance(msg, InboundMessage):
            return

        # Phase 2: Security checks (rate limiting, DM policy)
        prov = self.channel_registry.get(msg.channel_id) if self.channel_registry else None
        try:
            from ghost_channels.security import SecurityMixin
            from ghost_channels import load_channels_config
            if prov and isinstance(prov, SecurityMixin):
                ch_cfg = load_channels_config().get(msg.channel_id, {})
                max_rpm = self.cfg.get("channel_rate_limit_per_minute", 10)
                if not prov.verify_sender(msg.sender_id, msg.sender_name,
                                          config=ch_cfg, max_per_minute=max_rpm):
                    log.info("Inbound blocked by security: %s/%s",
                             msg.channel_id, msg.sender_id)
                    return
        except ImportError:
            pass

        # Phase 2: Strip bot mentions from inbound text
        try:
            from ghost_channels.mentions import MentionMixin
            if prov and isinstance(prov, MentionMixin):
                msg.text = prov.strip_bot_mention(msg.text)
        except ImportError:
            pass

        terminal_print("ask", f"[{msg.channel_id}] {msg.sender_name}", msg.text)

        # Send typing indicator while processing
        typing_stop = threading.Event()
        if prov and hasattr(prov, "send_typing"):
            chat_id = msg.thread_id or msg.sender_id
            def _keep_typing():
                while not typing_stop.is_set():
                    try:
                        prov.send_typing(chat_id)
                    except Exception as e:
                        log.debug("Typing indicator failed: %s", e)
                        break
                    typing_stop.wait(4)
            typing_thread = threading.Thread(target=_keep_typing, daemon=True,
                                             name=f"typing-{msg.channel_id}")
            typing_thread.start()

        tool_names = self.tool_registry.names() if self.tool_registry else []

        # Channel-aware additions
        channel_context = ""
        try:
            from ghost_channels.agent_prompts import AgentPromptAdapter
            adapter = AgentPromptAdapter(self.channel_registry)
            channel_context = adapter.build_channel_prompt(
                msg.channel_id, sender_name=msg.sender_name,
                sender_id=msg.sender_id, thread_id=msg.thread_id or "",
            )
        except ImportError:
            pass

        inbound_prompt_body = (
            "You are Ghost, an AUTONOMOUS AI agent running LOCALLY on the user's computer. "
            "You have DIRECT ACCESS to the file system, shell, network, and a real web browser.\n\n"
            f"The user is messaging you via **{msg.channel_id}** "
            f"(sender: {msg.sender_name}, id: {msg.sender_id}).\n"
            f"Your reply will be sent back on {msg.channel_id}.\n\n"
            "## FIRST RULE: USER TASKS vs GHOST SELF-MODIFICATION\n"
            "Before doing ANYTHING, classify the request:\n\n"
            "**USER TASK** (build/create/write something FOR the user):\n"
            "  'build a landing page', 'write a Python script', 'create a REST API', 'make a mobile app'\n"
            "  → Build it IMMEDIATELY. No exploring, no ls, no orientation. Just create the files.\n"
            "  → Use `workspace_write` for ALL project files (any language/framework):\n"
            "    workspace_write(project='my-project', file_path='main.py', content='...')\n"
            "    workspace_write(project='my-project', file_path='src/routes.ts', content='...')\n"
            "    workspace_write(project='my-project', file_path='index.html', content='...')\n"
            "  → Use `shell_exec` with workspace param for project commands:\n"
            "    shell_exec(command='pip install flask', workspace='my-project')\n"
            "    shell_exec(command='npm init -y && npm install express', workspace='my-project')\n"
            f"  → Files are saved to {get_user_projects_dir(self.cfg)}/<project>/ automatically.\n"
            "  → Pick a short, descriptive kebab-case project name (e.g. 'csv-converter', 'blog-api').\n"
            "  → NEVER queue user tasks as features. Build them NOW.\n\n"
            "**GHOST SELF-MODIFICATION** (changes to Ghost's OWN code):\n"
            "  'add a tool to Ghost', 'fix a Ghost bug', 'improve Ghost's memory'\n"
            "  → Queue via add_future_feature (the Evolution Runner implements them).\n\n"
            f"## GHOST CODEBASE\n"
            f"Ghost source: **{PROJECT_DIR}** — for Ghost files use file_read/file_write with full path.\n"
            f"`shell_exec` runs from HOME (~/) by default, or from workspace if workspace param is set.\n\n"
            "## AGENT BEHAVIOR — NEVER GIVE UP\n\n"
            "**COMPLETION RULE (READ THIS FIRST):**\n"
            "Parse EVERY verb in the user's message. Each verb is a REQUIRED action. "
            "You may NOT reply until ALL actions are done with actual results.\n"
            "If you installed a library but didn't run the code that uses it — you are NOT done. Keep going.\n"
            "NEVER say 'if you want', 'let me know', 'I can also', 'would you like me to'. "
            "Either DO it silently or omit it. End reply right after the deliverable.\n\n"
            "- Run until FULLY COMPLETE. When DONE, call `task_complete(summary='...')`.\n"
            "- NEVER hallucinate. Only state VERIFIED facts.\n"
            "- Saying 'I can't', 'I'm unable', 'not possible in this environment' = FORBIDDEN.\n"
            "  You have unlimited tools, a Python sandbox, and a browser. There is ALWAYS a way.\n\n"
            "### MANDATORY ESCALATION LOOP:\n"
            "When a tool fails (DENIED, error, empty result, boilerplate) you MUST move to the "
            "next level. Do NOT reply until you have tried ALL levels or succeeded.\n\n"
            "**Level 1 — Direct tools**: Try web_fetch, web_search, or shell_exec.\n"
            "**Level 2 — Research** (if Level 1 failed): `web_search('how to <task> programmatically')` "
            "or `web_search('python library for <task>')` to discover the right library or API.\n"
            "**Level 3 — Python sandbox** (use what Level 2 found):\n"
            "  Your shell has an automatic sandbox environment. "
            "When you run `pip install <pkg>`, it installs into `~/.ghost/sandbox/.venv` "
            "(NOT Ghost's own codebase). Installed tools are available immediately.\n"
            "  1. `shell_exec('pip install <pkg>')` — installs to sandbox automatically\n"
            "  2. Write script via file_write to `~/.ghost/sandbox/scripts/run.py`\n"
            "  3. `shell_exec('python3 ~/.ghost/sandbox/scripts/run.py')`\n"
            "  You do NOT need to manually create or activate a venv. It's handled for you.\n"
            "**Level 4 — Browser automation**: ONLY for interactive/visual tasks (login, clicking, forms). "
            "⚠ The browser opens a VISIBLE window on the user's screen — NEVER use it for silent data extraction. "
            "If you need data (transcripts, prices, API results), Level 3 is the right tool.\n"
            "**Level 5 — Combine**: Chain approaches.\n\n"
            "If your response would contain 'I couldn't', 'not available', or 'unable to' — "
            "STOP. You haven't tried all levels.\n\n"
            "### PRE-REPLY CHECK:\n"
            "1. Count the user's questions. Verify you answered ALL of them with data. If not, keep going.\n"
            "2. Delete any sentence containing 'if you want', 'let me know if', 'I can also', "
            "'I can follow up', or 'would you like me to'. Either DO it or don't mention it. "
            "End your reply after the deliverable — no upsells.\n\n"
            "### AFTER SUCCESS — submit as permanent tool:\n"
            "  `add_future_feature(title='Add <name> tool', description='<working code + deps>', "
            "priority='P1', source='user_request')`\n\n"
            "### AUTOMATIC BUG FIX ESCALATION (NON-NEGOTIABLE):\n"
            "If ANY Ghost tool returns an error, throws an exception, or behaves incorrectly — "
            "you MUST IMMEDIATELY submit a bug fix. Do NOT wait for the user to ask. "
            "Do NOT just work around it. The fix request is MANDATORY every single time.\n"
            "1. Submit the fix FIRST, before anything else:\n"
            "   `add_future_feature(title='Bug fix: <description>', "
            "description='<root cause analysis, affected files, exact fix needed>', "
            "priority='P0', source='user_request', category='bugfix')`\n"
            "2. Tell the user the bug was detected and a fix has been queued.\n"
            "3. THEN work around the bug if possible (e.g. using shell_exec or sandbox).\n"
            "This applies to ALL errors: tool execution failures, missing tools, "
            "import errors, broken wiring, wrong output, unexpected behavior — everything.\n\n"
            "## AVAILABLE TOOLS\n" + ", ".join(tool_names) + "\n\n"
            "## TOOL GUIDE\n"
            "**Delegation (task)**: Delegate sub-tasks to specialized subagents with fresh context windows. "
            "Use `task` AUTOMATICALLY when:\n"
            "  - The request involves multi-step research (reading several files, web searches) → `researcher`\n"
            "  - You need to write/modify code across multiple files → `coder`\n"
            "  - You need to run a chain of shell commands (build, test, deploy) → `bash`\n"
            "  - You want to review code for bugs or quality → `reviewer`\n"
            "  You do NOT need the user to ask for delegation — decide yourself based on task complexity. "
            "Simple single-step tasks (one file read, one search) should be done directly.\n"
            "**Sandbox** (for user tasks): `pip install` auto-routes to `~/.ghost/sandbox/.venv`. "
            "Write temp scripts to `~/.ghost/sandbox/scripts/`. NEVER install user-requested packages into Ghost's own .venv.\n"
            "**User Projects**: workspace_write (create files in user workspace), shell_exec(workspace='name') (run commands in project)\n"
            "**System**: shell_exec, file_read, file_write, file_search\n"
            "**Memory**: memory_search, memory_save\n"
            "**Web**: web_search, web_fetch (primary URL reader). browser = visible UI only (NOT for data extraction)\n"
            "**Ghost Self-Improvement**: add_future_feature (ONLY for Ghost's own codebase)\n"
            "**Communication**: send_email, notify, channel_send\n"
            "**Other**: app_control, uptime\n\n"
            "## URL & WEB TOOL RULES (CRITICAL — follow exactly)\n"
            "When the user's message contains a URL (http/https link):\n"
            "1. **ALWAYS use `web_fetch`** to retrieve the actual page content. NEVER guess or recall from memory.\n"
            "2. **AUTOMATIC FALLBACK**: If `web_fetch` returns limited content, escalate to **Level 3 (Python sandbox)** — "
            "NOT the browser. Use `web_search` to find the right Python library, install it in the sandbox, "
            "and run a script to extract the data programmatically.\n"
            "3. After fetching, summarize or analyze the ACTUAL fetched content.\n\n"
            "⚠ **BROWSER IS NOT A DATA EXTRACTION TOOL.** The browser opens a real, visible window on the "
            "user's screen. NEVER use it to silently scrape data, read transcripts, or extract content. "
            "Use it ONLY when the user explicitly says 'open/browse/go to' or the task truly requires "
            "interactive UI (login forms, clicking buttons, visual verification).\n\n"
            "When the user asks for current information, news, research, or facts you don't know:\n"
            "1. **Use `web_search`** first to find relevant sources.\n"
            "2. If you need to read a specific article/page from the results, use `web_fetch` on the URL.\n\n"
            "## KEY RULES\n"
            "1. For personal recall → memory_search first, memory_save for new info\n"
            "2. Be autonomous. Don't ask the user for help mid-task.\n"
            "3. After completing ALL parts of the task, give a concise summary.\n"
            "4. For Ghost's OWN code changes → queue via add_future_feature.\n"
            "   For user-requested projects → build directly with file_write/shell_exec.\n"
            "5. **COMPLETENESS**: Never do half the work. Every feature must be complete.\n"
            "6. **READ BEFORE WRITE**: Before modifying any file, ALWAYS file_read it first.\n\n"
        )

        prompt_parts = [inbound_prompt_body]
        if channel_context:
            prompt_parts.append(channel_context)

        # Build channel chat history from recent feed entries
        channel_history = self._build_channel_history(msg.channel_id)

        # ── Process inbound media (images, PDFs, text files) ──
        IMAGE_EXTS = {".jpg", ".jpeg", ".png", ".gif", ".webp", ".bmp"}
        TEXT_EXTS = {
            ".txt", ".py", ".js", ".ts", ".json", ".csv", ".xml", ".html",
            ".css", ".md", ".yaml", ".yml", ".toml", ".ini", ".cfg", ".sh",
            ".bash", ".rs", ".go", ".java", ".c", ".cpp", ".h", ".hpp",
            ".rb", ".php", ".sql", ".r", ".swift", ".kt", ".lua", ".log",
        }
        image_b64 = None
        media_text_parts: list[str] = []

        for fpath_str in (msg.media_urls or []):
            fpath = Path(fpath_str)
            if not fpath.exists():
                continue
            ext = fpath.suffix.lower()

            if ext in IMAGE_EXTS:
                try:
                    img_data = fpath.read_bytes()
                    image_b64 = base64.b64encode(img_data).decode()
                    media_text_parts.append(
                        f"[Image: {fpath.name} (path: {fpath})]"
                    )
                    if not msg.text:
                        msg.text = "The user sent an image. Describe and analyze it."
                except Exception as exc:
                    log.debug("Failed to read inbound image %s: %s", fpath, exc)

            elif ext == ".pdf":
                pdf_text = ""
                try:
                    try:
                        import fitz
                        doc = fitz.open(str(fpath))
                        pdf_text = "\n".join(p.get_text() for p in doc)
                    except ImportError:
                        from pypdf import PdfReader
                        reader = PdfReader(str(fpath))
                        pdf_text = "\n".join(
                            p.extract_text() or "" for p in reader.pages
                        )
                except Exception as exc:
                    log.debug("PDF extraction failed for %s: %s", fpath, exc)

                if pdf_text.strip():
                    media_text_parts.append(
                        f"[PDF: {fpath.name} (path: {fpath})]\n{pdf_text[:8000]}"
                    )
                else:
                    media_text_parts.append(
                        f"[PDF: {fpath.name} (path: {fpath}) — empty or unreadable. "
                        f"Try shell_exec with python3 and pypdf to extract content, "
                        f"or the file may be scanned/image-only.]"
                    )

            elif ext in TEXT_EXTS:
                try:
                    content = fpath.read_text(encoding="utf-8", errors="replace")[:8000]
                    media_text_parts.append(
                        f"[File: {fpath.name} (path: {fpath})]\n```\n{content}\n```"
                    )
                except Exception as exc:
                    log.debug("Failed to read text file %s: %s", fpath, exc)

            else:
                media_text_parts.append(f"[Attachment: {fpath.name} (path: {fpath})]")

        if media_text_parts:
            msg.text = (msg.text + "\n\n" + "\n\n".join(media_text_parts)).strip()

        if image_b64 and not msg.text:
            msg.text = "The user sent an image. Describe and analyze it."

        if self.cfg.get("enable_tool_loop", True) and self.tool_registry.get_all():
            _user_msg = msg.text[:self.cfg.get("max_input_chars", 16000)]

            from ghost_middleware import InvocationContext
            inv = InvocationContext(
                source="channel",
                user_message=_user_msg,
                system_prompt_parts=prompt_parts,
                tool_registry=self.tool_registry,
                daemon=self,
                engine=self.engine,
                config=self.cfg,
                max_steps=self.cfg.get("tool_loop_max_steps", 200),
                max_tokens=8192,
                on_step=terminal_step,
                history=channel_history,
                image_b64=image_b64,
            )
            self.middleware_chain.invoke(inv)

            reply = inv.result_text
            self._track_tool_calls(len(inv.result.tool_calls) if inv.result else 0)
            tools_used = inv.tools_used
        else:
            if image_b64:
                reply = self.llm.analyze_image(
                    msg.media_urls[0],
                    context_prefix=msg.text + "\n\n" if msg.text else "",
                )
            else:
                reply = self.llm.analyze("long_text", msg.text)
            tools_used = []

        typing_stop.set()

        if reply:
            reply = self._clean_channel_reply(reply)

        if reply and self.channel_router:
            self.channel_router.send(
                reply, channel=msg.channel_id,
                to=msg.thread_id or msg.sender_id,
                reply_to_id=msg.reply_to_id,
            )

        entry = {
            "time": datetime.now().isoformat(),
            "type": "channel",
            "message_id": f"ch_{msg.channel_id}_{int(time.time()*1000)}",
            "channel": msg.channel_id,
            "source": f"[{msg.channel_id}:{msg.sender_name}] {msg.text[:500]}",
            "result": reply,
            "status": "complete",
        }
        if tools_used:
            entry["tools_used"] = tools_used
        append_feed(entry, self.cfg.get("max_feed_items", 50))
        self.context_memory.add(entry)
        self.actions_today += 1

    def process_text(self, text):
        # Hook: on_classify
        override = self.hooks.run("on_classify", text) if self.hooks else None
        content_type = override if override else classify(text)
        if content_type == "skip":
            return

        # Hook: before_analyze
        modified = self.hooks.run("before_analyze", content_type, text) if self.hooks else None
        if modified:
            text = modified

        preview = text.strip().replace("\n", " ")[:60]
        ctx_prefix = self.context_memory.get_context_prefix(content_type)

        # Prepare input (source-specific: URL fetching, context prefix)
        if content_type == "url":
            page_text = fetch_url_text(text.strip())
            llm_input = f"URL: {text.strip()}\n\nPage content:\n{page_text}"
        else:
            llm_input = text.strip()
        if ctx_prefix:
            llm_input = ctx_prefix + llm_input

        base_prompt = PROMPTS.get(content_type, PROMPTS["long_text"])

        use_tools = self.cfg.get("enable_tool_loop", True) and self.tool_registry.get_all()

        if use_tools:
            from ghost_middleware import InvocationContext
            inv = InvocationContext(
                source="monitor",
                user_message=llm_input[:self.cfg.get("max_input_chars", 4000)],
                system_prompt_parts=[base_prompt],
                tool_registry=self.tool_registry,
                daemon=self,
                engine=self.engine,
                config=self.cfg,
                max_steps=self.cfg.get("tool_loop_max_steps", 200),
                max_tokens=2048,
                temperature=0.3,
                on_step=terminal_step,
                meta={"content_type": content_type},
            )
            self.middleware_chain.invoke(inv)

            result = inv.result_text
            tools_used = inv.tools_used
            tokens_used = inv.tokens_used
            skill_name = inv.matched_skills[0].name if inv.matched_skills else ""
            self._track_tool_calls(len(inv.result.tool_calls) if inv.result else 0)
        else:
            result = self.llm.analyze(content_type, llm_input, context_prefix="")
            tools_used = []
            tokens_used = 0
            skill_name = ""

        # Hook: after_analyze
        modified_result = self.hooks.run("after_analyze", content_type, text, result) if self.hooks else None
        if modified_result:
            result = modified_result

        terminal_print(content_type, preview, result)

        fix_cmd = None
        if content_type == "error":
            fix_cmd = extract_fix_command(result)

        entry = {
            "time": datetime.now().isoformat(),
            "type": content_type,
            "source": text.strip()[:2000],
            "result": result,
        }
        if fix_cmd:
            entry["fix_command"] = fix_cmd
        if skill_name:
            entry["skill"] = skill_name
        if tools_used:
            entry["tools_used"] = tools_used

        # Hook: on_feed_append
        modified_entry = self.hooks.run("on_feed_append", entry) if self.hooks else None
        if modified_entry and isinstance(modified_entry, dict):
            entry = modified_entry

        append_feed(entry, self.cfg.get("max_feed_items", 50))
        self.context_memory.add(entry)
        log_action(content_type, preview, result)

        # Save to persistent memory
        if self.memory_db:
            self.memory_db.save(
                content=result,
                type=content_type,
                source_preview=preview,
                tags=skill_name,
                skill=skill_name,
                tools_used=",".join(tools_used),
                tokens_used=tokens_used,
                source_hash=md5(text.strip().encode(errors="replace")).hexdigest(),
            )

        self.actions_today += 1

    def process_image(self, img_path):
        if self.hooks:
            self.hooks.run_void("on_screenshot", img_path)

        ctx = self.context_memory.get_context_prefix("image")

        matched_skills = []
        skill_name = ""
        if self.skill_loader:
            disabled = set(self.cfg.get("disabled_skills", []))
            matched_skills = self.skill_loader.llm_match(
                self.engine, "image screenshot", "image", disabled=disabled
            )

        system_prompt = self._build_identity_context() + PROMPTS["image"]
        if matched_skills:
            skill_name = matched_skills[0].name
            skills_section = self.skill_loader.build_skills_prompt(matched_skills)
            system_prompt += "\n\n" + skills_section

        try:
            img_data = Path(img_path).read_bytes()
            img_b64 = base64.b64encode(img_data).decode()
        except Exception as e:
            result = f"Image read error: {e}"
            terminal_print("image", f"screenshot: {Path(img_path).name}", result)
            return

        use_tools = self.cfg.get("enable_tool_loop", True) and self.tool_registry.get_all()
        tools_used = []
        tokens_used = 0

        if use_tools:
            tool_names = ["memory_search", "memory_save", "notify"]
            if matched_skills:
                tool_names += self.skill_loader.get_tools_for_skills(matched_skills)
            available = self.tool_registry.names()
            valid = [n for n in set(tool_names) if n in available]
            tool_reg = self.tool_registry.subset(valid) if valid else None

            # Determine if any matched skill has a model override
            skill_model = self._resolve_skill_model(matched_skills)

            set_shell_caller_context("interactive")
            try:
                loop_result = self.engine.run(
                    system_prompt=system_prompt,
                    user_message=ctx + "Analyze this image:",
                    tool_registry=tool_reg,
                    max_steps=self.cfg.get("tool_loop_max_steps", 200),
                    image_b64=img_b64,
                    hook_runner=self.hooks,
                    tool_intent_security=self.tool_intent_security,
                    model_override=skill_model,
                    tool_event_bus=self.tool_event_bus,
                )
            finally:
                set_shell_caller_context("autonomous")
            result = loop_result.text
            tools_used = [tc["tool"] for tc in loop_result.tool_calls]
            tokens_used = loop_result.total_tokens
            self._cleanup_browser_after_task(tools_used)
        else:
            result = self.llm.analyze_image(img_path, context_prefix=ctx)

        terminal_print("image", f"screenshot: {Path(img_path).name}", result)

        entry = {
            "time": datetime.now().isoformat(),
            "type": "image",
            "source": f"[screenshot: {Path(img_path).name}]",
            "image_path": img_path,
            "result": result,
        }
        if skill_name:
            entry["skill"] = skill_name
        if tools_used:
            entry["tools_used"] = tools_used

        append_feed(entry, self.cfg.get("max_feed_items", 50))
        self.context_memory.add(entry)
        log_action("image", Path(img_path).name, result)

        if self.memory_db:
            self.memory_db.save(
                content=result,
                type="image",
                source_preview=f"screenshot: {Path(img_path).name}",
                skill=skill_name,
                tools_used=",".join(tools_used),
                tokens_used=tokens_used,
            )

        self.actions_today += 1

    def check_actions(self):
        if not ACTION_FILE.exists():
            return
        try:
            mtime = ACTION_FILE.stat().st_mtime
        except (OSError, ValueError) as e:
            log.warning(f"Failed to stat action file: {e}")
            return
        if mtime <= self._action_mtime:
            return
        self._action_mtime = mtime
        try:
            action = json.loads(ACTION_FILE.read_text(encoding="utf-8"))
            ACTION_FILE.unlink(missing_ok=True)
        except json.JSONDecodeError as e:
            log.warning(f"Failed to parse action file: {e}")
            return

        action_id = action.get("actionId", "")
        source = action.get("source", "")
        ctype = action.get("type", "code")

        # Hot-reload model from config (so panel changes take effect immediately)
        fresh_cfg = load_config()
        new_model = fresh_cfg.get("model", DEFAULT_CONFIG["model"])
        if new_model != self.cfg.get("model"):
            self.cfg["model"] = new_model
            self.llm.model = new_model
            self.engine.model = new_model
            if self.chat_engine:
                self.chat_engine.model = new_model
            print(f"  {MAG}⟳ Model switched to: {new_model}{RST}")

        if self.hooks:
            self.hooks.run_void("on_action", action_id, source, ctype)

        if action_id == "ask" and source:
            tool_names = self.tool_registry.names() if self.tool_registry else []
            ask_prompt_body = (
                "You are Ghost, an AUTONOMOUS AI agent running LOCALLY on the user's computer. "
                "You have DIRECT ACCESS to the file system, shell, network, and a real web browser.\n\n"
                f"## PROJECT LOCATION (IMPORTANT)\n"
                f"Ghost project root: **{PROJECT_DIR}**\n"
                f"ALL source files live here: ghost.py, ghost_tools.py, ghost_loop.py, ghost_evolve.py, "
                f"ghost_dashboard/, skills/, SOUL.md, USER.md, etc.\n"
                f"- Skills go in `{PROJECT_DIR}/skills/<name>/SKILL.md` — NOT in ~/.ghost/skills/.\n"
                f"- `shell_exec` runs from HOME (~/) by default.\n"
                f"- `file_read`/`file_write` accept absolute paths — use `{PROJECT_DIR}/filename` for project files.\n"
                f"- Do NOT search for the project directory. You already know it.\n\n"
                "## AGENT BEHAVIOR — NEVER GIVE UP\n\n"
                "**COMPLETION RULE (READ THIS FIRST):**\n"
                "Parse EVERY verb in the user's message. Each verb is a REQUIRED action. "
                "You may NOT reply until ALL actions are done with actual results.\n"
                "If you installed a library but didn't run the code that uses it — you are NOT done. Keep going.\n"
                "NEVER say 'if you want', 'let me know', 'I can also', 'would you like me to'. "
                "Either DO it silently or omit it. End reply right after the deliverable.\n\n"
                "You are a persistent, autonomous agent. Keep going until the task is FULLY COMPLETE.\n"
                "- Do the work without asking permission. Treat tasks as sufficient direction.\n"
                "- When you say 'I will do X', you MUST actually do X — not just announce it.\n"
                "- Verify your results. Check your work rigorously. Watch for edge cases.\n"
                "- NEVER hallucinate. Only state facts VERIFIED from a primary source.\n"
                "- Before editing code, READ the file first to understand the full context.\n"
                "- For code changes to Ghost, queue via add_future_feature (the Evolution Runner implements them).\n"
                "- When DONE, call `task_complete(summary='...')` to end your turn.\n"
                "- Saying 'I can't', 'I'm unable', 'not possible in this environment' = FORBIDDEN.\n"
                "  You have unlimited tools, a Python sandbox, and a browser. There is ALWAYS a way.\n\n"
                "### MANDATORY ESCALATION LOOP:\n"
                "When a tool fails (DENIED, error, empty result, boilerplate) you MUST move to the "
                "next level. Do NOT reply until you have tried ALL levels or succeeded.\n\n"
                "**Level 1 — Direct tools**: Try web_fetch, web_search, shell_exec, grep.\n"
                "**Level 2 — Research** (if Level 1 failed): `web_search('how to <task> programmatically')` "
                "or `web_search('python library for <task>')` to discover the right library or API.\n"
                "**Level 3 — Python sandbox** (use what Level 2 found):\n"
                "  Your shell has an automatic sandbox environment. "
                "When you run `pip install <pkg>`, it installs into `~/.ghost/sandbox/.venv` "
                "(NOT Ghost's own codebase). Installed tools are available immediately.\n"
                "  1. `shell_exec('pip install <pkg>')` — installs to sandbox automatically\n"
                "  2. Write script via file_write to `~/.ghost/sandbox/scripts/run.py`\n"
                "  3. `shell_exec('python3 ~/.ghost/sandbox/scripts/run.py')`\n"
                "  You do NOT need to manually create or activate a venv. It's handled for you.\n"
                "**Level 4 — Browser automation**: Navigate, click, extract.\n"
                "**Level 5 — Combine**: Chain approaches.\n\n"
                "If your response would contain 'I couldn't', 'not available', or 'unable to' — "
                "STOP. You haven't tried all levels.\n\n"
                "### PRE-REPLY CHECK:\n"
                "1. Count the user's questions. Verify you answered ALL of them with data. If not, keep going.\n"
                "2. Delete any sentence containing 'if you want', 'let me know if', 'I can also', "
                "'I can follow up', or 'would you like me to'. Either DO it or don't mention it. "
                "End your reply after the deliverable — no upsells.\n\n"
                "### AFTER SUCCESS — submit as permanent tool:\n"
                "  `add_future_feature(title='Add <name> tool', description='<working code + deps>', "
                "priority='P1', source='user_request')`\n\n"
                "### AUTOMATIC BUG FIX ESCALATION (NON-NEGOTIABLE):\n"
                "If ANY Ghost tool returns an error, throws an exception, or behaves incorrectly — "
                "you MUST IMMEDIATELY submit a bug fix. Do NOT wait for the user to ask. "
                "Do NOT just work around it. The fix request is MANDATORY every single time.\n"
                "1. Submit the fix FIRST, before anything else:\n"
                "   `add_future_feature(title='Bug fix: <description>', "
                "description='<root cause analysis, affected files, exact fix needed>', "
                "priority='P0', source='user_request', category='bugfix')`\n"
                "2. Tell the user the bug was detected and a fix has been queued.\n"
                "3. THEN work around the bug if possible (e.g. using shell_exec or sandbox).\n"
                "This applies to ALL errors: tool execution failures, missing tools, "
                "import errors, broken wiring, wrong output, unexpected behavior — everything.\n\n"
                "## AVAILABLE TOOLS\n" + ", ".join(tool_names) + "\n\n"
                "## CODING BEST PRACTICES\n"
                "- Use `grep` for searching file CONTENTS with regex. Use `glob` for finding files by NAME patterns.\n"
                "- Prefer grep/glob/file_read over shell_exec for code exploration — faster and safer.\n"
                "- When making code changes, READ the file first to understand context before editing.\n"
                "- Do the work without asking permission. Treat tasks as sufficient direction.\n"
                "- Call multiple tools in parallel when there are no dependencies between them.\n"
                "- Reference code locations as `file_path:line_number` for easy navigation.\n\n"
                "## TOOL GUIDE\n"
                "**Delegation (task)**: Delegate sub-tasks to specialized subagents with fresh context windows. "
                "Use `task` AUTOMATICALLY — no user prompt needed:\n"
                "  - Multi-file research or interface verification → `task(subagent_type='researcher')`\n"
                "  - Multi-file code changes → `task(subagent_type='coder')`\n"
                "  - Shell command chains (build, test, deploy) → `task(subagent_type='bash')`\n"
                "  - Code review for bugs/quality → `task(subagent_type='reviewer')`\n"
                "  Only delegate complex multi-step work. Do simple single-tool operations directly.\n"
                "**Sandbox** (for user tasks): `pip install` auto-routes to `~/.ghost/sandbox/.venv`. "
                "Write temp scripts to `~/.ghost/sandbox/scripts/`. NEVER install user-requested packages into Ghost's own .venv.\n"
                "**Code Search**: grep (regex content search, sorted by recency), glob (file pattern matching), find_code_patterns\n"
                "**Memory**: memory_search, memory_save\n"
                "**System**: shell_exec, file_read, file_write, file_search\n"
                "**Web Research**: web_search (search the internet for current info, news, docs — multi-provider with fallback)\n"
                "**Web Content Extraction**: web_fetch — YOUR PRIMARY TOOL for reading any URL. "
                "Robust 5-tier extraction pipeline (Readability → Smart BeautifulSoup → Firecrawl → fallback) "
                "with automatic quality gate. Works on news sites, docs, blogs, GitHub, Wikipedia, and most "
                "public pages. Returns clean markdown with title. ALWAYS prefer web_fetch over browser for "
                "content extraction — it's faster, cheaper, and returns cleaner text.\n"
                "**Browser (visible UI)**: browser tool — use ONLY when web_fetch fails, or for JS-rendered "
                "SPAs, login-required pages, or interactive tasks (clicking, filling forms). Actions: "
                "navigate, snapshot, click, type, fill, content, evaluate, console, screenshot, wait, press, scroll, hover, select, pdf, tabs, new_tab, close_tab, stop\n"
                "**Cron Management**: cron_list (list all scheduled jobs), cron_run (trigger a job immediately by ID), "
                "cron_add, cron_remove, cron_update, cron_status. Use `cron_list` to find job IDs, "
                "then `cron_run(job_id='...')` to trigger any job including the feature_implementer.\n"
                "**Other**: app_control, notify, uptime\n\n"
                "## BROWSER WORKFLOW:\n"
                "1. navigate → go to URL\n"
                "2. snapshot → accessibility tree with refs (e0, e1, ...)\n"
                "3. click/type by ref → use refs from snapshot, NOT CSS selectors\n"
                "4. After page change → NEW snapshot\n\n"
                "## RESEARCH METHODOLOGY (CRITICAL — follow this for any 'find/search/who' task):\n"
                "When asked to find information about a person, project, or topic:\n"
                "1. **Start with the authoritative source.** To find who created a project → go to its GitHub page FIRST.\n"
                "   Example: github.com/owner/project → the repo page shows the owner/creator directly.\n"
                "2. **Verify before concluding.** Read the actual repo page, README, or profile. Check the owner username.\n"
                "3. **Cross-reference.** If asked to find them on X/Twitter, search for the specific username you found on GitHub.\n"
                "4. **Do NOT click random search results and assume they are the answer.** X search results are not reliable.\n"
                "   Seeing someone mention a project does NOT mean they created it.\n"
                "5. **If you cannot verify, say so.** 'I could not find a verified X profile for the creator' is better than a wrong answer.\n\n"
                "## SEARCH STRATEGY:\n"
                "- For 'who created X': Go to the project's GitHub/website FIRST, find the owner, then search for them elsewhere.\n"
                "- For general info: Use google.com/search?q=... to find reliable sources first.\n"
                "- For finding people on X: Search their exact name/username, not vague queries.\n"
                "- NEVER conclude from a single unverified search result.\n"
                "- For 'viral/popular/top posts': On X, use x.com/search?q=QUERY&f=top (the Top tab sorts by engagement).\n"
                "  Scroll down multiple times with browser(action='scroll', direction='down') to load more results.\n"
                "  Pick posts with the highest likes/reposts/views. Look for posts from verified or high-follower accounts.\n\n"
                "## MULTI-STEP TASK STRATEGY\n"
                "When a task has multiple phases (e.g., 'find X then send email'):\n"
                "1. Break the task into clear sub-goals. Complete each before moving on.\n"
                "2. Build up results as you go — take notes mentally of what you find.\n"
                "3. Only give the final answer once ALL sub-goals are done.\n\n"
                "## GMAIL / EMAIL WORKFLOW\n"
                "To send an email via Gmail in the browser:\n"
                "1. Navigate to https://mail.google.com/mail/?view=cm&to=RECIPIENT&su=SUBJECT\n"
                "   This opens the compose window directly with recipient and subject pre-filled.\n"
                "2. Take a snapshot to find the compose body area.\n"
                "3. Click the body area ref, then type your message.\n"
                "4. Take snapshot again, find and click the Send button.\n"
                "5. If Gmail asks to sign in, navigate through the login flow using snapshot+refs.\n\n"
                "## URL & WEB TOOL RULES (CRITICAL — follow exactly)\n"
                "When the user's message contains a URL (http/https link):\n"
                "1. **ALWAYS use `web_fetch`** to retrieve the actual page content. NEVER guess or recall from memory.\n"
                "2. **AUTOMATIC FALLBACK**: If `web_fetch` returns limited content (less than ~500 chars of article text, "
                "only a title, or mostly boilerplate), you MUST IMMEDIATELY and AUTOMATICALLY use the browser tool "
                "as fallback: navigate to the URL → wait 2s → use `content` action to extract full text. "
                "Do NOT ask the user if they want you to try browser. Just do it.\n"
                "3. After fetching (from either method), summarize or analyze the ACTUAL fetched content.\n\n"
                "When the user asks for current information, news, research, or facts you don't know:\n"
                "1. **Use `web_search`** first to find relevant sources and up-to-date information.\n"
                "2. If you need to read a specific article/page from the results, use `web_fetch` on the URL.\n\n"
                "Tool selection guide:\n"
                "- User provides a URL → `web_fetch` first, then browser if content is limited\n"
                "- User asks 'what is happening with X' / 'latest news about Y' → `web_search`\n"
                "- User says 'browse/open/go to' → `browser` tool (visible UI)\n\n"
                "## KEY RULES\n"
                "1. When user says 'browse/open/go to' → use browser tool (visible UI navigation)\n"
                "2. When user provides a URL to read/summarize → use web_fetch FIRST, browser as fallback\n"
                "3. Navigate directly to search URLs (google.com/search?q=..., x.com/search?q=...)\n"
                "4. ALWAYS snapshot after navigate\n"
                "5. Use refs from snapshot — NOT CSS selectors\n"
                "6. NEVER state facts you haven't verified from a primary source. Wrong answers are worse than 'I don't know'.\n"
                "7. For personal recall → memory_search first, memory_save for new info\n"
                "8. Be autonomous. Don't ask the user for help mid-task. Don't stop halfway.\n"
                "9. After completing ALL parts of the task, give a concise summary with sources.\n"
                "10. You have unlimited tool calls. Keep going until the task is fully complete."
            )
            if self.cfg.get("enable_tool_loop", True) and self.tool_registry.get_all():
                terminal_print("ask", f"[ask] {source[:50]}...", "Ghost is thinking...")

                from ghost_middleware import InvocationContext
                inv = InvocationContext(
                    source="action",
                    user_message=source,
                    system_prompt_parts=[ask_prompt_body],
                    tool_registry=self.tool_registry,
                    daemon=self,
                    engine=self.engine,
                    config=self.cfg,
                    max_steps=self.cfg.get("tool_loop_max_steps", 200),
                    max_tokens=8192,
                    on_step=terminal_step,
                )
                self.middleware_chain.invoke(inv)

                result = inv.result_text
                tools_used = inv.tools_used
                self._track_tool_calls(len(inv.result.tool_calls) if inv.result else 0)
            else:
                result = self.llm.analyze("long_text", source)
                tools_used = []

            terminal_print("ask", f"[ask] {source[:50]}...", result)
            entry = {
                "time": datetime.now().isoformat(),
                "type": "ask",
                "source": source[:2000],
                "result": result,
            }
            if tools_used:
                entry["tools_used"] = tools_used
            append_feed(entry, self.cfg.get("max_feed_items", 50))
            self.context_memory.add(entry)

            try:
                session_id = getattr(self, '_current_session_id', None) or 'interactive'
                conversation_msgs = [
                    {"role": "user", "content": source[:2000]},
                    {"role": "assistant", "content": result[:2000] if result else ""},
                ]
                get_memory_queue().add(session_id, conversation_msgs)
            except Exception:
                pass

        elif action_id in ("improve", "bugs", "explain") and source:
            prompt_type = action_id
            ctx = self.context_memory.get_context_prefix(ctype)

            if self.cfg.get("enable_tool_loop", True) and self.tool_registry.get_all():
                system_prompt = PROMPTS.get(prompt_type, PROMPTS["long_text"])
                set_shell_caller_context("interactive")
                try:
                    loop_result = self.engine.run(
                        system_prompt=system_prompt,
                        user_message=ctx + source[:4000],
                        tool_registry=self.tool_registry.subset(["memory_search", "file_read", "shell_exec"]),
                        max_steps=4,
                        max_tokens=2048,
                        hook_runner=self.hooks,
                        tool_intent_security=self.tool_intent_security,
                        model_override=None,
                        tool_event_bus=self.tool_event_bus,
                    )
                finally:
                    set_shell_caller_context("autonomous")
                result = loop_result.text
            else:
                result = self.llm.analyze(prompt_type, source, context_prefix=ctx)

            terminal_print(ctype, f"[{action_id}] {source[:40]}...", result)
            entry = {
                "time": datetime.now().isoformat(),
                "type": ctype,
                "source": source[:2000],
                "result": f"[{action_id.upper()}]\n{result}",
            }
            append_feed(entry, self.cfg.get("max_feed_items", 50))
            self.context_memory.add(entry)

    def _kill_existing(self):
        my_pid = os.getpid()
        if PID_FILE.exists():
            try:
                old_pid = int(PID_FILE.read_text(encoding="utf-8").strip())
                if old_pid != my_pid:
                    ghost_platform.kill_process(old_pid)
                    time.sleep(0.5)
            except (ProcessLookupError, ValueError, PermissionError):
                pass
            PID_FILE.unlink(missing_ok=True)

        try:
            other_pids = ghost_platform.find_ghost_processes(my_pid)
            for pid in other_pids:
                ghost_platform.kill_process(pid)
            if other_pids:
                time.sleep(0.5)
        except Exception as e:
            log.warning("Failed to kill existing process: %s", e)

    def run(self):
        self._kill_existing()

        print(BANNER)
        _primary_pid = self.cfg.get("primary_provider", "openrouter")
        _provider_models = self.cfg.get("provider_models", {})
        _active_model = _provider_models.get(_primary_pid, self.cfg.get("model", "?"))
        _display_model = f"{_primary_pid}:{_active_model}" if _primary_pid != "openrouter" else _active_model
        print(f"  {GRN}{B}ACTIVE{RST}  {DIM}Model: {_display_model}{RST}")

        tool_count = len(self.tool_registry.names())
        try:
            skill_count = len(self.skill_loader.list_all()) if self.skill_loader else 0
        except Exception:
            skill_count = 0
        mem_count = self.memory_db.count() if self.memory_db else 0
        cron_jobs = self.cron.list_jobs() if self.cron else []
        cron_enabled = sum(1 for j in cron_jobs if j.get("enabled"))

        stats = []
        if tool_count:
            stats.append(f"{tool_count} tools")
        if skill_count:
            stats.append(f"{skill_count} skills")
        if mem_count:
            stats.append(f"{mem_count} memories")
        if cron_jobs:
            stats.append(f"{cron_enabled}/{len(cron_jobs)} cron jobs")
        if self.evolve_engine:
            stats.append("evolve")
        if self.channel_registry:
            ch_configured = self.channel_registry.list_configured()
            ch_all = self.channel_registry.list_available()
            if ch_all:
                stats.append(f"{len(ch_configured)}/{len(ch_all)} channels")
            if self.channel_router and self.channel_router._queue:
                stats.append("queue")
            if hasattr(self, "_health_monitor") and self._health_monitor:
                stats.append("health-monitor")
        print(f"  {DIM}{' | '.join(stats)}{RST}")
        print()
        print(f"  {DIM}Dashboard: {RST}{B}http://localhost:{self.cfg.get('dashboard_port', 3333)}{RST}")
        print(f"  {DIM}Ctrl+C to stop.{RST}")
        print()

        if self.hooks:
            self.hooks.run_void("on_startup")

        if self.cron:
            self.cron.start()
            bootstrap_growth_cron(self.cron, self.cfg)
            bootstrap_session_maintenance_cron(self.cron, self.cfg)

        console_bus.emit(
            "success", "system", "daemon_start",
            f"Ghost started — {len(self.tool_registry.names())} tools, "
            f"model: {_display_model}",
        )

        # Self-repair MUST complete before firing the feature implementer
        # to prevent concurrent evolve operations that corrupt each other.
        try:
            repaired = run_self_repair(self)
            if repaired:
                print(f"  {GRN}Self-repair completed successfully{RST}")
        except Exception as e:
            print(f"  {RED}Self-repair error: {e}{RST}")

        # Startup trigger: if there's pending work in the feature queue and nothing
        # is in progress, fire the implementer. This handles continuation after
        # evolve_deploy restarts Ghost.
        if self.cron and self._features_store.is_queue_ready():
            self.cron.fire_now(_FEATURE_IMPLEMENTER_JOB)

        # Auditor trigger: fire only if a feature was implemented in the last 10 minutes
        # (indicates a recent deploy that needs verification, not stale history)
        _now = datetime.now()
        _recent_impl = any(
            f.get("status") == "implemented"
            and f.get("implemented_at")
            and (_now - datetime.fromisoformat(f["implemented_at"])).total_seconds() < 600
            for f in self._features_store.get_all()
        )
        if self.cron and _recent_impl:
            import threading
            threading.Timer(
                30.0,
                lambda: self.cron.fire_now(_IMPLEMENTATION_AUDITOR_JOB) if self.cron else None,
            ).start()

        # Start web dashboard as background thread
        self._dashboard_port = None
        try:
            from ghost_dashboard import start_with_daemon
            dash_port = int(self.cfg.get("dashboard_port", 3333))
            actual = start_with_daemon(self, port=dash_port)
            if actual:
                self._dashboard_port = actual

            # Wire dashboard → queue processor
            try:
                from ghost_dashboard.routes.future_features import set_queue_trigger, set_force_fire
                def _dashboard_queue_trigger():
                    if self.cron and self._features_store.is_queue_ready():
                        self.cron.fire_now(_FEATURE_IMPLEMENTER_JOB)
                set_queue_trigger(_dashboard_queue_trigger)
                set_force_fire(lambda: self.cron.fire_now(_FEATURE_IMPLEMENTER_JOB) if self.cron else None)
            except Exception as e:
                log.warning("Failed to wire queue triggers: %s", e)

            # Wire evolve approval → re-fire implementer when user approves
            # a pending evolution and the implementer is not running.
            try:
                from ghost_dashboard.routes.evolve import set_evolve_approve_hook
                def _on_evolve_approved():
                    if not self.cron:
                        return
                    if self.cron.is_job_running(_FEATURE_IMPLEMENTER_JOB):
                        return
                    self._features_store.reset_stale_in_progress(max_age_seconds=0)
                    self.cron.fire_now(_FEATURE_IMPLEMENTER_JOB)
                set_evolve_approve_hook(_on_evolve_approved)
            except Exception as e:
                log.warning("Failed to wire evolve approval hook: %s", e)
        except Exception as e:
            print(f"  {DIM}Dashboard failed to start: {e}{RST}")

        # Start channel inbound listeners (bidirectional messaging)
        if self.channel_registry and hasattr(self, "_make_inbound"):
            try:
                self.channel_inbound = self._make_inbound(self.process_inbound)
                self.channel_inbound.start_all()
            except Exception as e:
                print(f"  {DIM}Channel inbound failed: {e}{RST}")

        # Phase 2: Recover pending deliveries from queue
        if self.channel_router and self.cfg.get("enable_delivery_queue", True):
            try:
                timeout = self.cfg.get("delivery_queue_recovery_timeout", 60.0)
                stats = self.channel_router.recover_queue(timeout)
                if any(stats.values()):
                    print(f"  Queue recovery: {stats['recovered']} recovered, "
                          f"{stats['failed']} failed, {stats['skipped']} skipped")
            except Exception as e:
                print(f"  {DIM}Queue recovery failed: {e}{RST}")

        # Phase 2: Start health monitor
        if hasattr(self, "_health_monitor") and self._health_monitor:
            try:
                self._health_monitor.start()
            except Exception as e:
                print(f"  {DIM}Health monitor failed: {e}{RST}")

        signal.signal(signal.SIGINT, self.stop)
        signal.signal(signal.SIGTERM, self.stop)

        PID_FILE.write_text(str(os.getpid()), encoding="utf-8")

        if self.tool_event_bus:
            try:
                self.tool_event_bus.emit("on_boot")
            except Exception as e:
                log.warning("on_boot hook error: %s", e)

        poll = self.cfg.get("poll_interval", 1.0)
        deploy_marker = Path.home() / ".ghost" / "evolve" / "deploy_pending"

        while self.running:
            try:
                self.check_actions()
                if not getattr(self, "supervised", False) and deploy_marker.exists():
                    print(f"  {MAG}Deploy marker detected — restarting Ghost...{RST}")
                    try:
                        import json as _json
                        _deploy_info = _json.loads(deploy_marker.read_text(encoding="utf-8"))
                        _last_deploy = Path.home() / ".ghost" / "evolve" / "last_deploy.json"
                        _last_deploy.write_text(_json.dumps(_deploy_info, indent=2), encoding="utf-8")
                    except Exception:
                        pass
                    try:
                        deploy_marker.unlink()
                    except OSError:
                        pass
                    self.stop()
                    if ghost_platform.IS_WIN:
                        ghost_platform.popen_detached(
                            [sys.executable] + sys.argv,
                            cwd=str(Path(__file__).resolve().parent),
                        )
                        sys.exit(0)
                    else:
                        os.execv(sys.executable, [sys.executable] + sys.argv)
            except Exception as e:
                print(f"  {RED}Error: {e}{RST}")
            time.sleep(poll)

        if PID_FILE.exists():
            PID_FILE.unlink()


# ═════════════════════════════════════════════════════════════════════
#  CLI
# ═════════════════════════════════════════════════════════════════════

def cmd_log():
    if not LOG_FILE.exists():
        print(f"  {DIM}No actions yet. Start ghost and copy something.{RST}")
        return
    entries = json.loads(LOG_FILE.read_text(encoding="utf-8"))
    if not entries:
        print(f"  {DIM}No actions yet.{RST}")
        return
    print(f"\n  {B}👻 GHOST — Recent Actions{RST}\n")
    for e in entries[-20:]:
        t = e.get("time", "")[:19].replace("T", " ")
        label, icon = TYPE_LABELS.get(e.get("type", ""), ("???", "❓"))
        inp = e.get("input", "")[:50]
        out = e.get("output", "")[:80].replace("\n", " ")
        print(f"  {DIM}{t}{RST}  {icon} {YLW}{label:5s}{RST}  {DIM}{inp}{RST}")
        print(f"           {CYN}{out}{RST}")
        print()

def cmd_status():
    print(f"\n  {B}👻 GHOST — Status{RST}\n")
    if PID_FILE.exists():
        pid = PID_FILE.read_text(encoding="utf-8").strip()
        try:
            os.kill(int(pid), 0)
            print(f"  {GRN}{B}RUNNING{RST}  PID {pid}")
        except (OSError, ValueError) as e:
            log.warning("PID check failed: %s", e)
            print(f"  {DIM}NOT RUNNING{RST}  (stale PID file)")
    else:
        print(f"  {DIM}NOT RUNNING{RST}")

    entries = []
    if LOG_FILE.exists():
        try:
            content = LOG_FILE.read_text(encoding="utf-8")
            entries = json.loads(content)
        except json.JSONDecodeError as e:
            log.warning("Failed to parse log file: %s", e)

    total = len(entries)
    today = sum(1 for e in entries
                if e.get("time", "")[:10] == datetime.now().strftime("%Y-%m-%d"))

    print(f"  Total actions: {total}")
    print(f"  Today: {today}")

    if entries:
        types = {}
        for e in entries:
            t = e.get("type", "unknown")
            types[t] = types.get(t, 0) + 1
        print(f"  Breakdown: {', '.join(f'{k}={v}' for k, v in sorted(types.items(), key=lambda x: -x[1]))}")

    cfg = load_config()
    _pp = cfg.get("primary_provider", "openrouter")
    _pm = cfg.get("provider_models", {})
    _am = _pm.get(_pp, cfg.get("model", "?"))
    _dm = f"{_pp}:{_am}" if _pp != "openrouter" else _am
    print(f"  Model: {_dm}")
    print(f"  Platform: {PLAT}")
    print(f"  Tool Loop: {'ON' if cfg.get('enable_tool_loop') else 'OFF'}")
    print(f"  Skills: {'ON' if cfg.get('enable_skills') else 'OFF'}")
    print(f"  Plugins: {'ON' if cfg.get('enable_plugins') else 'OFF'}")
    print(f"  Memory DB: {'ON' if cfg.get('enable_memory_db') else 'OFF'}")
    print(f"  Cron: {'ON' if cfg.get('enable_cron') else 'OFF'}")

    if cfg.get("enable_cron"):
        try:
            cron = CronService()
            jobs = cron.list_jobs()
            enabled = sum(1 for j in jobs if j.get("enabled"))
            print(f"  Cron jobs: {len(jobs)} total, {enabled} enabled")
        except Exception as e:
            log.warning("Failed to get cron stats: %s", e)

    if cfg.get("enable_memory_db"):
        try:
            mdb = MemoryDB()
            stats = mdb.stats()
            print(f"  Memory: {stats['total']} entries, {stats['total_tokens']} tokens")
            mdb.close()
        except Exception as e:
            log.warning("Failed to get memory stats: %s", e)

    print(f"  SOUL.md: {'YES' if SOUL_FILE.exists() else 'NO'} ({SOUL_FILE})")
    print(f"  USER.md: {'YES' if USER_FILE.exists() else 'NO'} ({USER_FILE})")
    print(f"  Config: {CONFIG_FILE}")
    print(f"  Log: {LOG_FILE}")
    print(f"  Feed: {FEED_FILE}")
    print()

def cmd_context():
    print(f"\n  {B}👻 GHOST — Context{RST}\n")
    feed = read_feed()
    if not feed:
        print(f"  {DIM}No activity yet. Ghost needs to see some clipboard data first.{RST}\n")
        return
    mem = ContextMemory()
    for entry in reversed(feed[:20]):
        mem.add(entry)
    print(f"  {mem.summary()}")
    print()


def cmd_soul(sub_args):
    """View or manage SOUL.md (Ghost's identity)."""
    _ensure_identity_files()

    if not sub_args or sub_args[0] == "show":
        content = load_soul()
        if content:
            print(f"\n  {B}👻 GHOST — Soul (SOUL.md){RST}\n")
            print(f"  {DIM}File: {SOUL_FILE}{RST}\n")
            for line in content.split("\n"):
                print(f"  {CYN}{line}{RST}")
            print()
        else:
            print(f"  {DIM}SOUL.md is empty.{RST}")

    elif sub_args[0] == "edit":
        _default_editor = "notepad" if ghost_platform.IS_WIN else "nano"
        editor = os.environ.get("EDITOR", _default_editor)
        try:
            subprocess.run([editor, str(SOUL_FILE)])
        except Exception as e:
            print(f"  {RED}Could not open editor: {e}{RST}")
            print(f"  Edit manually: {SOUL_FILE}")

    elif sub_args[0] == "reset":
        SOUL_FILE.write_text(DEFAULT_SOUL, encoding="utf-8")
        print(f"  {GRN}SOUL.md reset to default.{RST}")

    elif sub_args[0] == "path":
        print(str(SOUL_FILE))

    else:
        print(f"  {RED}Unknown subcommand: {sub_args[0]}{RST}")
        print(f"  Available: show (default), edit, reset, path")


def cmd_user(sub_args):
    """View or manage USER.md (user profile)."""
    _ensure_identity_files()

    if not sub_args or sub_args[0] == "show":
        content = load_user()
        if content:
            print(f"\n  {B}👻 GHOST — User Profile (USER.md){RST}\n")
            print(f"  {DIM}File: {USER_FILE}{RST}\n")
            for line in content.split("\n"):
                print(f"  {CYN}{line}{RST}")
            print()
        else:
            print(f"  {DIM}USER.md is empty.{RST}")

    elif sub_args[0] == "edit":
        _default_editor = "notepad" if ghost_platform.IS_WIN else "nano"
        editor = os.environ.get("EDITOR", _default_editor)
        try:
            subprocess.run([editor, str(USER_FILE)])
        except Exception as e:
            print(f"  {RED}Could not open editor: {e}{RST}")
            print(f"  Edit manually: {USER_FILE}")

    elif sub_args[0] == "reset":
        user_content = DEFAULT_USER % {"os": platform.system()}
        USER_FILE.write_text(user_content, encoding="utf-8")
        print(f"  {GRN}USER.md reset to default.{RST}")

    elif sub_args[0] == "path":
        print(str(USER_FILE))

    elif sub_args[0] == "set" and len(sub_args) >= 3:
        field = sub_args[1].lower()
        value = " ".join(sub_args[2:])
        content = USER_FILE.read_text(encoding="utf-8")
        field_map = {
            "name": "**Name:**",
            "call": "**What to call them:**",
            "pronouns": "**Pronouns:**",
            "timezone": "**Timezone:**",
            "tz": "**Timezone:**",
            "notes": "**Notes:**",
        }
        marker = field_map.get(field)
        if not marker:
            print(f"  {RED}Unknown field: {field}{RST}")
            print(f"  Fields: name, call, pronouns, timezone, notes")
            return
        lines = content.split("\n")
        for i, line in enumerate(lines):
            if marker in line:
                lines[i] = f"- {marker} {value}"
                break
        USER_FILE.write_text("\n".join(lines), encoding="utf-8")
        print(f"  {GRN}Set {field} = {value}{RST}")

    else:
        print(f"  {RED}Unknown subcommand: {sub_args[0]}{RST}")
        print(f"  Available: show (default), edit, reset, path, set <field> <value>")


def cmd_reset(sub_args):
    """Reset Ghost data — selective or full wipe of ~/.ghost/."""
    import shutil
    from datetime import datetime as _dt

    CONFIG_FILES = [
        "config.json", "auth_profiles.json", "credentials.json",
        "google_oauth.json", "integrations.json",
    ]
    MEMORY_PATHS = ["memory.db", "vector_memory.db", "memory"]

    def _is_ghost_running():
        for pid_name in ("ghost.pid", "supervisor.pid"):
            pf = GHOST_HOME / pid_name
            if not pf.exists():
                continue
            try:
                pid = int(pf.read_text(encoding="utf-8").strip())
                os.kill(pid, 0)
                return True, pid_name.replace(".pid", ""), pid
            except (ValueError, ProcessLookupError, PermissionError, OSError):
                continue
        return False, None, None

    def _backup_ghost_home():
        ts = _dt.now().strftime("%Y%m%d_%H%M%S")
        backup = GHOST_HOME.parent / f".ghost.backup.{ts}"
        shutil.copytree(GHOST_HOME, backup)
        return backup

    def _wipe_paths(paths):
        for name in paths:
            p = GHOST_HOME / name
            if p.is_dir():
                shutil.rmtree(p)
            elif p.is_file():
                p.unlink()

    if not GHOST_HOME.exists():
        print(f"  {DIM}Nothing to reset — {GHOST_HOME} does not exist.{RST}")
        return

    if not sub_args:
        print(f"\n  {B}👻 GHOST — Reset{RST}\n")
        print(f"  {CYN}ghost.py reset --all{RST}      Wipe everything (backs up first)")
        print(f"  {CYN}ghost.py reset --config{RST}   Reset config & credentials only")
        print(f"  {CYN}ghost.py reset --memory{RST}   Clear memory databases only")
        print()
        print(f"  {DIM}All resets create a backup at ~/.ghost.backup.<timestamp>/{RST}")
        print()
        return

    flag = sub_args[0]

    running, proc_name, proc_pid = _is_ghost_running()
    if running:
        print(f"  {RED}Ghost is still running ({proc_name}, PID {proc_pid}).{RST}")
        print(f"  {DIM}Stop it first:  bash stop.sh{RST}")
        print(f"  {DIM}On Windows, files locked by the running process cannot be deleted.{RST}")
        return

    if flag == "--all":
        answer = input(f"  {YLW}This will wipe ALL Ghost data. Continue? [y/N] {RST}").strip().lower()
        if answer != "y":
            print(f"  {DIM}Cancelled.{RST}")
            return
        backup = _backup_ghost_home()
        shutil.rmtree(GHOST_HOME)
        GHOST_HOME.mkdir(parents=True, exist_ok=True)
        print(f"  {GRN}✓ Full reset complete.{RST}")
        print(f"  {DIM}Backup saved to: {backup}{RST}")
        print(f"  {DIM}Start Ghost to run the setup wizard.{RST}")

    elif flag == "--config":
        backup = _backup_ghost_home()
        _wipe_paths(CONFIG_FILES)
        print(f"  {GRN}✓ Config & credentials reset.{RST}")
        print(f"  {DIM}Backup saved to: {backup}{RST}")
        print(f"  {DIM}Start Ghost to run the setup wizard.{RST}")

    elif flag == "--memory":
        backup = _backup_ghost_home()
        _wipe_paths(MEMORY_PATHS)
        print(f"  {GRN}✓ Memory cleared.{RST}")
        print(f"  {DIM}Backup saved to: {backup}{RST}")

    else:
        print(f"  {RED}Unknown flag: {flag}{RST}")
        print(f"  Available: --all, --config, --memory")


def cmd_cron(sub_args):
    """Handle cron subcommands."""
    from ghost_cron import CronService, describe_schedule
    from datetime import datetime as _dt

    cron = CronService()

    if not sub_args or sub_args[0] in ("list", "ls"):
        jobs = cron.list_jobs()
        if not jobs:
            print(f"\n  {DIM}No cron jobs configured. Add one with: python ghost.py cron add{RST}\n")
            return
        print(f"\n  {B}⏰ GHOST — Cron Jobs{RST}\n")
        for j in jobs:
            icon = f"{GRN}ON {RST}" if j.get("enabled") else f"{RED}OFF{RST}"
            sched = describe_schedule(j.get("schedule", {}))
            state = j.get("state", {})
            next_run = state.get("nextRunAtMs")
            next_str = (
                _dt.fromtimestamp(next_run / 1000).strftime("%Y-%m-%d %H:%M:%S")
                if next_run else "—"
            )
            last = state.get("lastRunStatus", "never")
            payload = j.get("payload", {})
            ptype = payload.get("type", "?")
            print(f"  [{icon}]  {CYN}{j['id']}{RST}  {B}{j['name']}{RST}")
            print(f"       Schedule: {sched}")
            print(f"       Next run: {next_str}  |  Last: {last}  |  Type: {ptype}")
            if j.get("description"):
                print(f"       Desc: {DIM}{j['description']}{RST}")
            err = state.get("lastError")
            if err:
                print(f"       {RED}Error: {err}{RST}")
            dur = state.get("lastDurationMs")
            if dur:
                print(f"       Last duration: {dur}ms")
            print()

    elif sub_args[0] == "status":
        st = cron.status()
        print(f"\n  {B}⏰ GHOST — Cron Status{RST}\n")
        print(f"  Service: {'RUNNING' if st['running'] else 'STOPPED'}")
        print(f"  Total jobs: {st['total_jobs']}")
        print(f"  Enabled: {st['enabled_jobs']}")
        print(f"  Executing: {st['executing']}")
        if st.get("next_wake"):
            print(f"  Next wake: {st['next_wake']}")
        print()

    elif sub_args[0] == "add":
        if len(sub_args) < 4:
            print(f"\n  {B}Usage:{RST} python ghost.py cron add <name> <schedule> <task>")
            print(f"\n  {B}Schedule formats:{RST}")
            print(f"    every:30m              Every 30 minutes")
            print(f"    every:1h               Every hour")
            print(f"    every:300s             Every 300 seconds")
            print(f"    cron:0 9 * * *         Daily at 9 AM")
            print(f"    cron:*/5 * * * *       Every 5 minutes")
            print(f"    at:2025-12-31T23:59    One-shot at specific time")
            print(f"\n  {B}Examples:{RST}")
            print(f'    python ghost.py cron add "news-check" "every:1h" "Check top HN stories and summarize"')
            print(f'    python ghost.py cron add "morning-brief" "cron:0 9 * * *" "Give me a morning briefing"')
            print(f'    python ghost.py cron add "reminder" "at:2025-03-01T10:00" "notify:Team standup in 10 min"')
            print()
            return

        name = sub_args[1]
        sched_str = sub_args[2]
        task = " ".join(sub_args[3:])

        task_type = "task"
        if task.startswith("notify:"):
            task_type = "notify"
            task = task[7:]
        elif task.startswith("shell:"):
            task_type = "shell"
            task = task[6:]

        if sched_str.startswith("every:"):
            val = sched_str[6:]
            multiplier = 1
            if val.endswith("s"):
                multiplier = 1
                val = val[:-1]
            elif val.endswith("m"):
                multiplier = 60
                val = val[:-1]
            elif val.endswith("h"):
                multiplier = 3600
                val = val[:-1]
            elif val.endswith("d"):
                multiplier = 86400
                val = val[:-1]
            try:
                seconds = int(val) * multiplier
            except ValueError:
                print(f"  {RED}Error: Invalid interval '{sched_str}'{RST}")
                return
            schedule = {"kind": "every", "everyMs": seconds * 1000}
        elif sched_str.startswith("cron:"):
            expr = sched_str[5:]
            schedule = {"kind": "cron", "expr": expr}
        elif sched_str.startswith("at:"):
            at_val = sched_str[3:]
            schedule = {"kind": "at", "at": at_val}
        else:
            print(f"  {RED}Error: Schedule must start with every:, cron:, or at:{RST}")
            return

        if task_type == "task":
            payload = {"type": "task", "prompt": task}
        elif task_type == "notify":
            payload = {"type": "notify", "title": name, "message": task}
        else:
            payload = {"type": "shell", "command": task}

        job = cron.add_job(
            name=name, schedule=schedule, payload=payload,
            delete_after_run=(schedule.get("kind") == "at"),
        )
        next_run = job.get("state", {}).get("nextRunAtMs")
        next_str = (
            _dt.fromtimestamp(next_run / 1000).strftime("%Y-%m-%d %H:%M:%S")
            if next_run else "N/A"
        )
        print(f"\n  {GRN}Created:{RST} {B}{name}{RST}  (id: {job['id']})")
        print(f"  Schedule: {describe_schedule(schedule)}")
        print(f"  Next run: {next_str}")
        print(f"  Payload: {task_type}")
        print()

    elif sub_args[0] in ("rm", "remove", "delete"):
        if len(sub_args) < 2:
            print(f"  {RED}Usage: python ghost.py cron rm <job_id>{RST}")
            return
        job_id = sub_args[1]
        if cron.remove_job(job_id):
            print(f"  {GRN}Removed job {job_id}{RST}")
        else:
            print(f"  {RED}Job {job_id} not found{RST}")

    elif sub_args[0] == "run":
        if len(sub_args) < 2:
            print(f"  {RED}Usage: python ghost.py cron run <job_id>{RST}")
            return
        job_id = sub_args[1]
        ok, msg = cron.run_now(job_id)
        print(f"  {'✓' if ok else '✗'} {msg}")

    elif sub_args[0] in ("enable", "disable"):
        if len(sub_args) < 2:
            print(f"  {RED}Usage: python ghost.py cron {sub_args[0]} <job_id>{RST}")
            return
        enabled = sub_args[0] == "enable"
        job_id = sub_args[1]
        if cron.enable_job(job_id, enabled=enabled):
            print(f"  {GRN}Job {job_id} {'enabled' if enabled else 'disabled'}{RST}")
        else:
            print(f"  {RED}Job {job_id} not found{RST}")

    else:
        print(f"  {RED}Unknown cron subcommand: {sub_args[0]}{RST}")
        print(f"  Available: list, status, add, rm, run, enable, disable")


def main():
    ghost_platform.enable_ansi_colors()
    ghost_platform.ensure_utf8_stdio()
    import argparse
    ap = argparse.ArgumentParser(
        description="👻 GHOST — Autonomous AI Agent. Runs locally, evolves itself.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="Examples:\n"
               "  python ghost.py                        Start watching\n"
               "  python ghost.py --model openai/gpt-4o  Use GPT-4o\n"
               "  python ghost.py log                    Show history\n"
               "  python ghost.py status                 Check status\n"
               "  python ghost.py context                What Ghost thinks you're doing\n"
               "  python ghost.py cron list              List scheduled jobs\n"
               "  python ghost.py cron add <args>        Add a cron job\n"
               "  python ghost.py cron rm <id>           Remove a cron job\n"
               "  python ghost.py soul                   View Ghost's soul/personality\n"
               "  python ghost.py soul edit              Edit SOUL.md in your editor\n"
               "  python ghost.py user                   View user profile\n"
               "  python ghost.py user set name <name>   Set user info\n"
               "  python ghost.py reset                  Show reset options\n"
               "  python ghost.py reset --all            Full factory reset\n"
               "  python ghost.py dashboard              Open web dashboard\n"
               "  python ghost.py dashboard 8080         Dashboard on custom port\n"
    )
    ap.add_argument("command", nargs="?", default="start",
                    help="start (default), log, status, context, cron, soul, user, reset, dashboard")
    ap.add_argument("rest", nargs="*", help=argparse.SUPPRESS)
    ap.add_argument("--api-key", default=None,
                    help="OpenRouter API key (or set OPENROUTER_API_KEY)")
    ap.add_argument("--model", default=None,
                    help="Model to use (default: google/gemini-2.0-flash-001)")
    ap.add_argument("--supervised", action="store_true",
                    help="Running under ghost_supervisor (enables deploy signals)")
    ap.add_argument("--dry-run", action="store_true",
                    help="Initialize daemon and exit (used by evolve_test smoke check)")
    args = ap.parse_args()

    if args.command == "log":
        cmd_log()
        return
    if args.command == "status":
        cmd_status()
        return
    if args.command == "context":
        cmd_context()
        return
    if args.command == "reset":
        cmd_reset(args.rest)
        return
    if args.command == "cron":
        cmd_cron(args.rest)
        return
    if args.command == "soul":
        cmd_soul(args.rest)
        return
    if args.command == "user":
        cmd_user(args.rest)
        return
    if args.command == "dashboard":
        from ghost_dashboard import run_dashboard
        try:
            port = int(args.rest[0]) if args.rest else 3333
        except Exception as e:
            log.warning("Invalid port specified, using default 3333: %s", e)
            port = 3333
        run_dashboard(port=port)
        return

    cfg = load_config()
    # Try auth_store first, then env, then legacy config
    from ghost_auth_profiles import get_auth_store
    _auth_store = get_auth_store()
    api_key = args.api_key or os.environ.get("OPENROUTER_API_KEY", "") or _auth_store.get_api_key("openrouter") or cfg.get("api_key", "")

    if args.dry_run:
        dummy_key = api_key or "dry-run-placeholder"
        dry_cfg = dict(cfg)
        dry_cfg.update({
            "enable_channels": False,
            "enable_channel_health_monitor": False,
            "enable_browser_tools": False,
            "enable_voice": False,
            "enable_cron": False,
            "enable_plugins": False,
            "enable_vector_memory": False,
            "enable_image_gen": False,
            "enable_vision": False,
            "enable_tts": False,
            "enable_canvas": False,
            "enable_web_search": False,
            "enable_web_fetch": False,
            "enable_data_extract": False,
            "enable_code_intel": False,
        })
        daemon = GhostDaemon(dummy_key, dry_cfg, dry_run=True)
        print("Dry-run: daemon initialized successfully")
        daemon.stop()
        return

    if not api_key:
        print(f"\n  {YLW}{B}No API key found — starting in setup mode.{RST}")
        print(f"  Open the dashboard to provide your OpenRouter API key.")
        print(f"  {DIM}Or set it manually:{RST}")
        print(f"    {YLW}export OPENROUTER_API_KEY=sk-or-...{RST}")
        print(f"    {YLW}python ghost.py --api-key sk-or-...{RST}\n")
        # Use a placeholder key — daemon starts but LLM calls will fail
        # until the real key is provided via the dashboard setup wizard.
        api_key = "__SETUP_PENDING__"

    if args.model:
        cfg["model"] = args.model
    save_config(cfg)

    daemon = GhostDaemon(api_key, cfg)
    daemon.supervised = getattr(args, "supervised", False)
    daemon.run()


if __name__ == "__main__":
    main()
