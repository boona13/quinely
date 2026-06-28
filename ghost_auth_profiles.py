"""
GHOST Auth Profile Store

Manages API keys and OAuth tokens for multiple LLM providers.
Stores credentials in ~/.ghost/auth_profiles.json.
Syncs credentials from external CLIs (~/.codex/auth.json).
"""

import json
import logging
import os
import threading
import time
from pathlib import Path

import ghost_secret_store as secret_store

log = logging.getLogger("ghost.auth_profiles")

GHOST_HOME = Path.home() / ".ghost"
PROFILES_FILE = GHOST_HOME / "auth_profiles.json"
CODEX_AUTH_FILE = Path.home() / ".codex" / "auth.json"

# Profile fields that hold secrets and must be encrypted at rest.
_SENSITIVE_FIELDS = ("key", "access_token", "refresh_token")

_EMPTY_STORE = {
    "version": 1,
    "profiles": {},
    "provider_order": [],
}


class AuthProfileStore:
    """Thread-safe auth profile manager."""

    def __init__(self, path: Path = None):
        self._path = path or PROFILES_FILE
        self._path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self._migration_needed = False
        self._store = self._load()
        # One-time migration: re-encrypt any legacy plaintext secrets on disk.
        if self._migration_needed and secret_store.is_available():
            try:
                self._save()
                log.info("Migrated auth profiles to encrypted-at-rest")
            except Exception as e:
                log.warning("Auth profile encryption migration failed: %s", e)

    def _load(self) -> dict:
        if self._path.exists():
            try:
                data = json.loads(self._path.read_text(encoding="utf-8"))
                if isinstance(data, dict) and "profiles" in data:
                    # Decrypt secrets into the in-memory store so callers see plaintext.
                    profiles = data.get("profiles", {})
                    for pid, prof in list(profiles.items()):
                        if isinstance(prof, dict):
                            for f in _SENSITIVE_FIELDS:
                                v = prof.get(f)
                                if isinstance(v, str) and v and not secret_store.is_encrypted(v):
                                    self._migration_needed = True
                            profiles[pid] = secret_store.decrypt_fields(prof, _SENSITIVE_FIELDS)
                    return data
            except (json.JSONDecodeError, OSError) as e:
                log.warning("Corrupted auth profiles, resetting: %s", e)
        return dict(_EMPTY_STORE)

    def _save(self):
        with self._lock:
            # Write an encrypted-at-rest copy without mutating the in-memory store.
            profiles = self._store.get("profiles", {})
            enc_profiles = {
                pid: (secret_store.encrypt_fields(prof, _SENSITIVE_FIELDS)
                      if isinstance(prof, dict) else prof)
                for pid, prof in profiles.items()
            }
            out = {
                "version": self._store.get("version", 1),
                "profiles": enc_profiles,
                "provider_order": self._store.get("provider_order", []),
            }
            self._path.write_text(json.dumps(out, indent=2), encoding="utf-8")
            try:
                os.chmod(self._path, 0o600)
            except Exception:
                pass

    @property
    def profiles(self) -> dict:
        return dict(self._store.get("profiles", {}))

    @property
    def provider_order(self) -> list[str]:
        return list(self._store.get("provider_order", []))

    @provider_order.setter
    def provider_order(self, order: list[str]):
        self._store["provider_order"] = list(order)
        self._save()

    def get_profile(self, profile_id: str) -> dict | None:
        return self._store.get("profiles", {}).get(profile_id)

    def get_provider_profile(self, provider_id: str) -> dict | None:
        """Get the default profile for a provider."""
        key = f"{provider_id}:default"
        return self.get_profile(key)

    def set_profile(self, profile_id: str, profile: dict):
        with self._lock:
            self._store.setdefault("profiles", {})[profile_id] = profile
            provider = profile.get("provider", profile_id.split(":")[0])
            order = self._store.setdefault("provider_order", [])
            if provider not in order:
                order.append(provider)
            self._save()
        log.info("Saved auth profile: %s", profile_id)
        # Audit log
        try:
            from ghost_audit_log import get_audit_log, AuditAction
            audit = get_audit_log()
            ptype = profile.get("type", "unknown")
            audit.log(
                action=AuditAction.AUTH_PROFILE_SET,
                resource_type="auth_profile",
                resource_id=profile_id,
                success=True,
                details={"provider": provider, "type": ptype},
            )
        except Exception as e:
            log.warning("Audit log failed: %s", e)

    def remove_profile(self, profile_id: str):
        with self._lock:
            profiles = self._store.get("profiles", {})
            if profile_id not in profiles:
                return
            del profiles[profile_id]
            self._save()
            log.info("Removed auth profile: %s", profile_id)
            # Audit log
            try:
                from ghost_audit_log import get_audit_log, AuditAction
                audit = get_audit_log()
                audit.log(
                    action=AuditAction.AUTH_PROFILE_REMOVE,
                    resource_type="auth_profile",
                    resource_id=profile_id,
                    success=True,
                )
            except Exception as e:
                log.warning("Audit log failed: %s", e)

    def set_api_key(self, provider_id: str, key: str, name: str = "default"):
        """Convenience: save an API key profile."""
        profile_id = f"{provider_id}:{name}"
        self.set_profile(profile_id, {
            "type": "api_key",
            "provider": provider_id,
            "key": key,
        })

    def set_oauth(self, provider_id: str, access_token: str, refresh_token: str = "",
                  expires_at: float = 0, account_id: str = "", name: str = "default"):
        """Convenience: save an OAuth profile."""
        profile_id = f"{provider_id}:{name}"
        self.set_profile(profile_id, {
            "type": "oauth",
            "provider": provider_id,
            "access_token": access_token,
            "refresh_token": refresh_token,
            "expires_at": expires_at,
            "account_id": account_id,
        })

    def set_no_auth(self, provider_id: str, name: str = "default"):
        """Convenience: save a no-auth profile (e.g. Ollama)."""
        profile_id = f"{provider_id}:{name}"
        self.set_profile(profile_id, {
            "type": "none",
            "provider": provider_id,
        })

    def get_api_key(self, provider_id: str) -> str:
        """Get the API key for a provider (from profile, env, or legacy config)."""
        profile = self.get_provider_profile(provider_id)
        if profile:
            ptype = profile.get("type", "")
            if ptype == "api_key":
                return profile.get("key", "")
            if ptype == "oauth":
                return profile.get("access_token", "")
            if ptype == "none":
                return ""

        from ghost_providers import get_provider
        prov = get_provider(provider_id)
        if prov and prov.env_key:
            env_val = os.environ.get(prov.env_key, "")
            if env_val:
                return env_val

        if provider_id == "openrouter":
            from ghost import load_config
            cfg = load_config()
            return cfg.get("api_key", os.environ.get("OPENROUTER_API_KEY", ""))

        return ""

    def is_provider_configured(self, provider_id: str) -> bool:
        """Check if a provider has valid auth configured."""
        if provider_id == "ollama":
            return True
        key = self.get_api_key(provider_id)
        return bool(key)

    def get_configured_providers(self) -> list[str]:
        """Return list of provider IDs that have valid auth."""
        configured = []
        from ghost_providers import PROVIDERS
        for pid in PROVIDERS:
            if self.is_provider_configured(pid):
                configured.append(pid)
        return configured

    def get_provider_status(self, provider_id: str) -> dict:
        """Get auth status for a provider."""
        profile = self.get_provider_profile(provider_id)
        if not profile:
            key = self.get_api_key(provider_id)
            if key:
                return {"configured": True, "type": "api_key",
                        "masked_key": key[:8] + "..." + key[-4:] if len(key) > 12 else "***"}
            if provider_id == "ollama":
                return {"configured": True, "type": "none"}
            return {"configured": False, "type": None}

        ptype = profile.get("type", "")
        status = {"configured": True, "type": ptype}

        if ptype == "api_key":
            k = profile.get("key", "")
            status["masked_key"] = k[:8] + "..." + k[-4:] if len(k) > 12 else "***"
        elif ptype == "oauth":
            status["has_refresh"] = bool(profile.get("refresh_token"))
            status["account_id"] = profile.get("account_id", "")
            exp = profile.get("expires_at", 0)
            status["expired"] = exp > 0 and time.time() > exp
        elif ptype == "none":
            pass

        return status

    def sync_codex_cli(self) -> bool:
        """Sync OAuth credentials from ~/.codex/auth.json if available."""
        if not CODEX_AUTH_FILE.exists():
            return False
        try:
            data = json.loads(CODEX_AUTH_FILE.read_text(encoding="utf-8"))
            access = data.get("access_token") or data.get("token", "")
            refresh = data.get("refresh_token", "")
            expires = data.get("expires_at", 0)
            account_id = data.get("account_id", "")

            if not access:
                return False

            if not account_id:
                account_id = _parse_jwt_account_id(access)

            self.set_oauth(
                "openai-codex", access, refresh,
                expires_at=expires, account_id=account_id
            )
            log.info("Synced Codex credentials from %s", CODEX_AUTH_FILE)
            return True
        except Exception as e:
            log.warning("Failed to sync Codex CLI credentials: %s", e)
            return False

    def summary(self) -> list[dict]:
        """Return a summary of all provider auth statuses."""
        from ghost_providers import PROVIDERS
        result = []
        for pid, prov in PROVIDERS.items():
            status = self.get_provider_status(pid)
            result.append({
                "provider_id": pid,
                "name": prov.name,
                "auth_type": prov.auth_type,
                **status,
            })
        return result


def _parse_jwt_account_id(token: str) -> str:
    """Extract chatgpt_account_id from an OpenAI JWT without external deps."""
    try:
        import base64
        parts = token.split(".")
        if len(parts) < 2:
            return ""
        payload = parts[1]
        padding = 4 - len(payload) % 4
        if padding != 4:
            payload += "=" * padding
        decoded = base64.urlsafe_b64decode(payload)
        claims = json.loads(decoded)
        return (
            claims.get("https://api.openai.com/auth", {}).get("chatgpt_account_id", "")
            or claims.get("account_id", "")
            or claims.get("sub", "")
        )
    except Exception:
        return ""


# Singleton
_store: AuthProfileStore | None = None

def get_auth_store() -> AuthProfileStore:
    global _store
    if _store is None:
        _store = AuthProfileStore()
    return _store
