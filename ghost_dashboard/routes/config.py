"""Config API — read/write ~/.ghost/config.json with live daemon reload."""

import logging
import shutil
from datetime import datetime as _dt
from flask import Blueprint, jsonify, request

from ghost_dashboard.rate_limiter import rate_limit

import sys
from pathlib import Path

log = logging.getLogger(__name__)
sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent))

from ghost import CONFIG_FILE, GHOST_HOME, load_config, save_config, DEFAULT_CONFIG

bp = Blueprint("config", __name__)


def _get_daemon():
    from ghost_dashboard import get_daemon
    return get_daemon()


def _notify_daemon():
    """If running embedded, push config changes to the live daemon."""
    try:
        daemon = _get_daemon()
        if daemon:
            fresh = load_config()
            daemon.cfg.update(fresh)
            new_model = fresh.get("model")
            if new_model and hasattr(daemon, 'llm'):
                daemon.llm.model = new_model
            if new_model and hasattr(daemon, 'engine'):
                daemon.engine.model = new_model
            if new_model and getattr(daemon, 'chat_engine', None):
                daemon.chat_engine.model = new_model
            if hasattr(daemon, 'engine') and hasattr(daemon.engine, 'fallback_chain'):
                model = fresh.get("model", DEFAULT_CONFIG["model"])
                fallback_models = fresh.get("fallback_models", [])
                new_chain = daemon._build_provider_chain(model, fallback_models)
                daemon.engine.fallback_chain.set_provider_chain(new_chain)
                if getattr(daemon, 'chat_engine', None):
                    daemon.chat_engine.fallback_chain.set_provider_chain(list(new_chain))
    except Exception:
        log.warning("Failed to reload config in daemon", exc_info=True)


def _mask_key(key):
    """Mask sensitive API keys for display - show first 8 chars + ... + last 4 chars."""
    if not key:
        return ""
    if len(key) > 12:
        return key[:8] + "..." + key[-4:]
    return "***"


@bp.route("/api/config")
def get_config():
    daemon = _get_daemon()
    cfg = daemon.cfg if daemon else load_config()
    resp = dict(cfg)
    # Mask sensitive API keys before returning
    if "api_key" in resp and resp["api_key"]:
        resp["api_key"] = _mask_key(resp["api_key"])
    if "firecrawl_api_key" in resp and resp["firecrawl_api_key"]:
        resp["firecrawl_api_key"] = _mask_key(resp["firecrawl_api_key"])
    if "hf_token" in resp and resp["hf_token"]:
        resp["hf_token"] = _mask_key(resp["hf_token"])
    if "cloud_providers" in resp and isinstance(resp["cloud_providers"], dict):
        masked_cp = {}
        for pname, pcfg in resp["cloud_providers"].items():
            masked_cp[pname] = dict(pcfg) if isinstance(pcfg, dict) else pcfg
            if isinstance(masked_cp[pname], dict):
                if masked_cp[pname].get("api_key"):
                    masked_cp[pname]["api_key"] = _mask_key(masked_cp[pname]["api_key"])
                if masked_cp[pname].get("secret_key"):
                    masked_cp[pname]["secret_key"] = _mask_key(masked_cp[pname]["secret_key"])
        resp["cloud_providers"] = masked_cp
    if daemon and hasattr(daemon, 'engine') and hasattr(daemon.engine, 'fallback_chain'):
        fc = daemon.engine.fallback_chain
        resp["model"] = f"{fc.active_provider}:{fc.active_model}"
    return jsonify({"config": resp, "defaults": DEFAULT_CONFIG})


@bp.route("/api/config", methods=["PUT"])
@rate_limit(requests_per_minute=20)
def update_config():
    data = request.get_json(silent=True) or {}
    cfg = load_config()

    requested_enable = data.get("enable_dangerous_interpreters")
    if requested_enable is True and not cfg.get("enable_dangerous_interpreters", False):
        token = str(data.get("dangerous_interpreters_confirmation", "")).strip()
        if token != "I_UNDERSTAND_THE_RISK":
            return jsonify({
                "ok": False,
                "error": "Enabling dangerous interpreters requires explicit confirmation token",
                "required_confirmation": "I_UNDERSTAND_THE_RISK",
            }), 400
        try:
            from ghost import append_feed
            append_feed({
                "ts": __import__("datetime").datetime.now().isoformat(timespec="seconds"),
                "type": "security",
                "preview": "Dangerous interpreters enabled via config API",
                "result": "enable_dangerous_interpreters set to true with elevated confirmation",
            })
        except ImportError as exc:
            log.warning("Unable to import append_feed for security audit event: %s", exc)

    data.pop("dangerous_interpreters_confirmation", None)
    cfg.update(data)
    save_config(cfg)
    _notify_daemon()
    return jsonify({"ok": True, "config": cfg})


# ── Cloud Providers API ────────────────────────────────────────────


@bp.route("/api/cloud-providers")
def list_cloud_providers():
    daemon = _get_daemon()
    if not daemon or not getattr(daemon, "cloud_providers", None):
        try:
            from ghost_cloud_providers import ProviderRegistry
            registry = ProviderRegistry(load_config())
            return jsonify({"providers": registry.list_providers(), "costs": registry.get_costs_summary()})
        except Exception:
            return jsonify({"providers": [], "costs": {}, "error": "Cloud providers not initialized"})

    providers = daemon.cloud_providers.list_providers()
    for p in providers:
        if p.get("configured") or daemon.cloud_providers.get_api_key(p["name"]):
            p["api_key_masked"] = _mask_key(daemon.cloud_providers.get_api_key(p["name"]) or "")
        else:
            p["api_key_masked"] = ""
        if p.get("needs_secret_key"):
            sk = daemon.cloud_providers.get_secret_key(p["name"])
            p["secret_key_masked"] = _mask_key(sk) if sk else ""
    costs = daemon.cloud_providers.get_costs_summary()
    return jsonify({"providers": providers, "costs": costs})


@bp.route("/api/cloud-providers/<name>", methods=["PUT"])
@rate_limit(requests_per_minute=20)
def update_cloud_provider(name):
    data = request.get_json(silent=True) or {}
    daemon = _get_daemon()

    cfg = load_config()
    cloud_cfg = cfg.setdefault("cloud_providers", {})
    prov_cfg = cloud_cfg.setdefault(name, {})

    if "api_key" in data:
        prov_cfg["api_key"] = data["api_key"]
    if "secret_key" in data:
        prov_cfg["secret_key"] = data["secret_key"]
    if "enabled" in data:
        prov_cfg["enabled"] = bool(data["enabled"])
    if "monthly_budget_usd" in data:
        prov_cfg["monthly_budget_usd"] = float(data["monthly_budget_usd"])
    if "preferred_model" in data:
        prov_cfg["preferred_model"] = data["preferred_model"]

    save_config(cfg)

    if daemon and getattr(daemon, "cloud_providers", None):
        daemon.cloud_providers.update_provider(
            name,
            api_key=data.get("api_key"),
            secret_key=data.get("secret_key"),
            enabled=data.get("enabled"),
            monthly_budget_usd=data.get("monthly_budget_usd"),
            preferred_model=data.get("preferred_model"),
        )
        daemon.cfg["cloud_providers"] = cfg.get("cloud_providers", {})

    # Audit log
    try:
        from ghost_audit_log import get_audit_log, AuditAction
        audit = get_audit_log()
        audit.log(
            action=AuditAction.CLOUD_PROVIDER_UPDATE,
            resource_type="cloud_provider",
            resource_id=name,
            success=True,
            details={"enabled": data.get("enabled"), "api_key_updated": "api_key" in data, "secret_key_updated": "secret_key" in data},
        )
    except Exception as e:
        logging.getLogger("quinely.audit").warning("Audit log failed: %s", e)

    return jsonify({"ok": True, "message": f"Updated {name}"})


@bp.route("/api/cloud-providers/<name>/test", methods=["POST"])
@rate_limit(requests_per_minute=10)
def test_cloud_provider(name):
    data = request.get_json(silent=True) or {}
    api_key = data.get("api_key", "").strip()
    secret_key = data.get("secret_key", "").strip()

    if not api_key:
        daemon = _get_daemon()
        if daemon and getattr(daemon, "cloud_providers", None):
            api_key = daemon.cloud_providers.get_api_key(name) or ""
            if not secret_key:
                secret_key = daemon.cloud_providers.get_secret_key(name) or ""

    if not api_key:
        return jsonify({"ok": False, "error": "No API key provided or configured"})

    try:
        from ghost_cloud_providers import test_provider_key
        result = test_provider_key(name, api_key, secret_key=secret_key)
        return jsonify(result)
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)[:200]})


@bp.route("/api/cloud-providers/costs")
def cloud_provider_costs():
    daemon = _get_daemon()
    if not daemon or not getattr(daemon, "cloud_providers", None):
        return jsonify({"costs": {}, "error": "Cloud providers not initialized"})
    return jsonify(daemon.cloud_providers.get_costs_summary())


# ── Reset API ─────────────────────────────────────────────────────

_CONFIG_FILES = [
    "config.json", "auth_profiles.json", "credentials.json",
    "google_oauth.json", "integrations.json",
]
_MEMORY_PATHS = ["memory.db", "vector_memory.db", "memory"]


def _backup_ghost_home():
    ts = _dt.now().strftime("%Y%m%d_%H%M%S")
    backup = GHOST_HOME.parent / f".ghost.backup.{ts}"
    shutil.copytree(GHOST_HOME, backup)
    return backup


def _wipe_paths(names):
    for name in names:
        p = GHOST_HOME / name
        if p.is_dir():
            shutil.rmtree(p)
        elif p.is_file():
            p.unlink()


def _is_ghost_running():
    """Check if ghost daemon or supervisor is alive via PID files."""
    import os as _os
    for pid_name in ("ghost.pid", "supervisor.pid"):
        pf = GHOST_HOME / pid_name
        if not pf.exists():
            continue
        try:
            pid = int(pf.read_text(encoding="utf-8").strip())
            _os.kill(pid, 0)
            return True, pid_name.replace(".pid", ""), pid
        except (ValueError, ProcessLookupError, PermissionError, OSError):
            continue
    return False, None, None


@bp.route("/api/config/reset", methods=["POST"])
@rate_limit(requests_per_minute=5)
def reset_ghost():
    data = request.get_json(silent=True) or {}
    mode = data.get("mode", "all")

    if mode not in ("all", "config", "memory"):
        return jsonify({"ok": False, "error": f"Invalid reset mode: {mode}"}), 400

    if not GHOST_HOME.exists():
        return jsonify({"ok": False, "error": "~/.ghost/ does not exist"}), 404

    running, proc_name, proc_pid = _is_ghost_running()
    if running and mode in ("all", "config"):
        return jsonify({
            "ok": False,
            "error": f"Ghost is still running ({proc_name}, PID {proc_pid}). "
                     f"Stop Ghost first (bash stop.sh), then reset. "
                     f"On Windows, open files cannot be deleted while a process holds them.",
        }), 409

    try:
        backup = _backup_ghost_home()

        if mode == "all":
            shutil.rmtree(GHOST_HOME)
            GHOST_HOME.mkdir(parents=True, exist_ok=True)
            msg = "Full reset complete. Restart Ghost to run the setup wizard."
        elif mode == "config":
            _wipe_paths(_CONFIG_FILES)
            msg = "Config & credentials reset. Restart Ghost to run the setup wizard."
        elif mode == "memory":
            _wipe_paths(_MEMORY_PATHS)
            msg = "Memory cleared. Config and skills preserved."

        log.warning("Ghost reset (mode=%s) — backup at %s", mode, backup)
        return jsonify({"ok": True, "message": msg, "backup": str(backup)})

    except PermissionError as e:
        log.error("Reset blocked by file lock (likely Windows): %s", e)
        return jsonify({
            "ok": False,
            "error": "Could not delete files — they may be locked by a running process. "
                     "Stop Ghost first, then retry.",
        }), 409

    except Exception as e:
        log.error("Reset failed: %s", e, exc_info=True)
        return jsonify({"ok": False, "error": str(e)[:300]}), 500
