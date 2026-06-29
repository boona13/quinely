"""
Ghost Cloud Providers — API key management, cost tracking, polling, and budget
enforcement for paid cloud services (Kling, Runway, Minimax, etc.).

Used by cloud video/media nodes to handle common patterns: authentication,
async job polling, per-provider monthly budgets, and cumulative cost tracking.
"""

import json
import logging
import os
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

log = logging.getLogger("quinely.cloud_providers")

GHOST_HOME = Path.home() / ".ghost"
COSTS_FILE = GHOST_HOME / "cloud_costs.json"

KNOWN_PROVIDERS = {
    "kling": {
        "display_name": "Kling AI",
        "api_base": "https://api.klingai.com/v1",
        "env_var": "KLING_ACCESS_KEY",
        "secret_env_var": "KLING_SECRET_KEY",
        "auth_type": "kling_jwt",
        "docs_url": "https://app.klingai.com/global/dev/document-api/apiReference/model/textToVideo",
        "credits_url": "https://klingai.com/global/dev/model/video",
        "keys_url": "https://app.klingai.com/global/dev",
        "models": [
            "kling-v3.0-pro", "kling-v3.0-std", "kling-video-o3",
            "kling-v2.6-pro", "kling-v2.6-std", "kling-v2.5-turbo", "kling-video-o1",
        ],
        "resolutions": ["16:9", "9:16", "1:1"],
        "durations": [3, 4, 5, 6, 7, 8, 9, 10, 11, 12, 13, 14, 15],
    },
    "runway": {
        "display_name": "Runway",
        "api_base": "https://api.dev.runwayml.com/v1",
        "env_var": "RUNWAY_API_KEY",
        "auth_type": "bearer",
        "docs_url": "https://docs.dev.runwayml.com/",
        "credits_url": "https://dev.runwayml.com/",
        "keys_url": "https://dev.runwayml.com/",
        "extra_headers": {"X-Runway-Version": "2024-11-06"},
        "models": ["gen4.5", "gen4_turbo", "gen3a_turbo", "veo3.1", "veo3.1_fast", "veo3"],
        "resolutions": ["1280:720", "720:1280", "960:960", "1104:832", "832:1104", "1584:672"],
        "durations": [2, 3, 4, 5, 6, 7, 8, 9, 10],
    },
    "minimax": {
        "display_name": "Minimax (Hailuo)",
        "api_base": "https://api.minimaxi.chat/v1",
        "env_var": "MINIMAX_API_KEY",
        "auth_type": "bearer",
        "docs_url": "https://platform.minimax.io/docs/api-reference/video-generation-t2v",
        "credits_url": "https://platform.minimax.io/subscribe/subscription",
        "keys_url": "https://platform.minimax.io/user-center/basic-information/interface-key",
        "models": ["MiniMax-Hailuo-2.3", "MiniMax-Hailuo-02", "T2V-01", "T2V-01-Director"],
        "resolutions": ["720P", "768P", "1080P"],
        "durations": [6, 10],
    },
    "luma": {
        "display_name": "Luma Dream Machine",
        "api_base": "https://api.lumalabs.ai/dream-machine/v1",
        "env_var": "LUMA_API_KEY",
        "auth_type": "bearer",
        "docs_url": "https://docs.lumalabs.ai/",
        "models": [],
        "resolutions": [],
        "durations": [],
    },
    "pika": {
        "display_name": "Pika",
        "api_base": "https://api.pika.art/v1",
        "env_var": "PIKA_API_KEY",
        "auth_type": "bearer",
        "docs_url": "https://pika.art/",
        "models": [],
        "resolutions": [],
        "durations": [],
    },
    "runware": {
        "display_name": "Runware",
        "api_base": "https://api.runware.ai/v1",
        "env_var": "RUNWARE_API_KEY",
        "auth_type": "bearer",
        "docs_url": "https://runware.ai/docs/getting-started/introduction",
        "credits_url": "https://my.runware.ai/",
        "keys_url": "https://my.runware.ai/",
        "models": [
            "klingai:kling-video@3-pro",
            "klingai:kling-video@3-standard",
            "klingai:kling-video@o3-pro",
            "klingai:kling-video@o3-standard",
            "runway:1@2",
            "runway:1@1",
            "minimax:4@1",
            "minimax:4@2",
            "google:3@0",
            "google:3@2",
            "openai:3@2",
        ],
        "resolutions": ["16:9", "9:16", "1:1", "4:3", "3:4", "21:9"],
        "durations": [2, 3, 4, 5, 6, 7, 8, 9, 10, 11, 12, 13, 14, 15],
    },
}


@dataclass
class ProviderConfig:
    name: str
    api_key: str = ""
    secret_key: str = ""
    enabled: bool = False
    monthly_budget_usd: float = 0.0
    preferred_model: str = ""
    extra: dict = field(default_factory=dict)


class ProviderRegistry:
    """Manages cloud provider configs, API keys, cost tracking, and budgets."""

    def __init__(self, cfg: dict | None = None):
        self._cfg = cfg or {}
        self._providers: dict[str, ProviderConfig] = {}
        self._costs_lock = threading.Lock()
        self._load_providers()

    def _load_providers(self):
        cloud_cfg = self._cfg.get("cloud_providers", {})
        for name, info in KNOWN_PROVIDERS.items():
            pcfg = cloud_cfg.get(name, {})
            self._providers[name] = ProviderConfig(
                name=name,
                api_key=pcfg.get("api_key", ""),
                secret_key=pcfg.get("secret_key", ""),
                enabled=pcfg.get("enabled", False),
                monthly_budget_usd=pcfg.get("monthly_budget_usd", 0.0),
                preferred_model=pcfg.get("preferred_model", ""),
                extra=pcfg.get("extra", {}),
            )

    def reload(self, cfg: dict):
        self._cfg = cfg
        self._load_providers()

    def get(self, provider_name: str) -> Optional[ProviderConfig]:
        return self._providers.get(provider_name)

    def get_api_key(self, provider_name: str) -> Optional[str]:
        """Resolve API key (or access key): config → env var → None."""
        prov = self._providers.get(provider_name)
        if prov and prov.api_key:
            return prov.api_key
        env_var = KNOWN_PROVIDERS.get(provider_name, {}).get("env_var", "")
        if env_var:
            val = os.environ.get(env_var, "")
            if val:
                return val
        return None

    def get_secret_key(self, provider_name: str) -> Optional[str]:
        """Resolve secret key: config → env var → None."""
        prov = self._providers.get(provider_name)
        if prov and prov.secret_key:
            return prov.secret_key
        secret_env = KNOWN_PROVIDERS.get(provider_name, {}).get("secret_env_var", "")
        if secret_env:
            val = os.environ.get(secret_env, "")
            if val:
                return val
        return None

    def is_configured(self, provider_name: str) -> bool:
        info = KNOWN_PROVIDERS.get(provider_name, {})
        has_key = bool(self.get_api_key(provider_name))
        if info.get("auth_type") == "kling_jwt":
            return has_key and bool(self.get_secret_key(provider_name))
        return has_key

    def is_enabled(self, provider_name: str) -> bool:
        prov = self._providers.get(provider_name)
        return bool(prov and prov.enabled and self.get_api_key(provider_name))

    def list_providers(self) -> list[dict]:
        result = []
        for name, info in KNOWN_PROVIDERS.items():
            prov = self._providers.get(name)
            configured = self.is_configured(name)
            spend = self.get_month_spend(name)
            budget = prov.monthly_budget_usd if prov else 0.0
            needs_secret = bool(info.get("secret_env_var"))
            entry = {
                "name": name,
                "display_name": info["display_name"],
                "enabled": prov.enabled if prov else False,
                "configured": configured,
                "monthly_budget_usd": budget,
                "month_spend_usd": spend,
                "budget_remaining_usd": max(0, budget - spend) if budget > 0 else None,
                "preferred_model": prov.preferred_model if prov else "",
                "docs_url": info.get("docs_url", ""),
                "credits_url": info.get("credits_url", ""),
                "keys_url": info.get("keys_url", ""),
                "models": info.get("models", []),
                "resolutions": info.get("resolutions", []),
                "durations": info.get("durations", []),
                "needs_secret_key": needs_secret,
            }
            if needs_secret:
                entry["has_secret_key"] = bool(self.get_secret_key(name))
            result.append(entry)
        return result

    def update_provider(self, provider_name: str, api_key: str | None = None,
                        secret_key: str | None = None,
                        enabled: bool | None = None,
                        monthly_budget_usd: float | None = None,
                        preferred_model: str | None = None) -> bool:
        if provider_name not in KNOWN_PROVIDERS:
            return False
        prov = self._providers.get(provider_name)
        if not prov:
            prov = ProviderConfig(name=provider_name)
            self._providers[provider_name] = prov
        if api_key is not None:
            prov.api_key = api_key
        if secret_key is not None:
            prov.secret_key = secret_key
        if enabled is not None:
            prov.enabled = enabled
        if monthly_budget_usd is not None:
            prov.monthly_budget_usd = monthly_budget_usd
        if preferred_model is not None:
            prov.preferred_model = preferred_model

        cloud_cfg = self._cfg.setdefault("cloud_providers", {})
        pcfg = {
            "api_key": prov.api_key,
            "enabled": prov.enabled,
            "monthly_budget_usd": prov.monthly_budget_usd,
            "preferred_model": prov.preferred_model,
        }
        if prov.secret_key:
            pcfg["secret_key"] = prov.secret_key
        cloud_cfg[provider_name] = pcfg
        return True

    def _generate_kling_jwt(self, access_key: str, secret_key: str) -> str:
        """Generate a short-lived JWT token for Kling API authentication."""
        try:
            import jwt
        except ImportError:
            raise RuntimeError(
                "PyJWT is required for Kling authentication. "
                "Install it: pip install PyJWT"
            )
        headers = {"alg": "HS256", "typ": "JWT"}
        now = int(time.time())
        payload = {
            "iss": access_key,
            "exp": now + 1800,
            "nbf": now - 5,
        }
        return jwt.encode(payload, secret_key, algorithm="HS256", headers=headers)

    def get_auth_headers(self, provider_name: str) -> dict:
        key = self.get_api_key(provider_name)
        if not key:
            return {}
        info = KNOWN_PROVIDERS.get(provider_name, {})
        auth_type = info.get("auth_type", "bearer")
        headers = {}
        if auth_type == "kling_jwt":
            secret = self.get_secret_key(provider_name)
            if not secret:
                log.warning("Kling secret key not configured — cannot generate JWT")
                return {}
            token = self._generate_kling_jwt(key, secret)
            headers["Authorization"] = f"Bearer {token}"
        elif auth_type == "bearer":
            headers["Authorization"] = f"Bearer {key}"
        else:
            headers["X-Api-Key"] = key
        extra = info.get("extra_headers", {})
        if extra:
            headers.update(extra)
        return headers

    def get_api_base(self, provider_name: str) -> str:
        return KNOWN_PROVIDERS.get(provider_name, {}).get("api_base", "")

    # ── Cost Tracking ──────────────────────────────────────────────

    def _load_costs(self) -> dict:
        if COSTS_FILE.exists():
            try:
                return json.loads(COSTS_FILE.read_text(encoding="utf-8"))
            except Exception:
                pass
        return {}

    def _save_costs(self, data: dict):
        COSTS_FILE.parent.mkdir(parents=True, exist_ok=True)
        COSTS_FILE.write_text(json.dumps(data, indent=2), encoding="utf-8")

    def _current_month_key(self) -> str:
        return time.strftime("%Y-%m")

    def track_cost(self, provider_name: str, operation: str,
                   cost_usd: float, media_id: str = ""):
        with self._costs_lock:
            costs = self._load_costs()
            month = self._current_month_key()
            month_data = costs.setdefault(month, {})
            prov_data = month_data.setdefault(provider_name, {
                "total_usd": 0.0, "generations": 0, "operations": [],
            })
            prov_data["total_usd"] = round(prov_data["total_usd"] + cost_usd, 4)
            prov_data["generations"] += 1
            prov_data["operations"].append({
                "op": operation,
                "cost_usd": cost_usd,
                "media_id": media_id,
                "ts": time.time(),
            })
            if len(prov_data["operations"]) > 500:
                prov_data["operations"] = prov_data["operations"][-500:]
            self._save_costs(costs)
        log.info("Cost tracked: %s %s $%.4f (media: %s)",
                 provider_name, operation, cost_usd, media_id)

    def get_month_spend(self, provider_name: str) -> float:
        costs = self._load_costs()
        month = self._current_month_key()
        return costs.get(month, {}).get(provider_name, {}).get("total_usd", 0.0)

    def get_costs_summary(self) -> dict:
        costs = self._load_costs()
        month = self._current_month_key()
        month_data = costs.get(month, {})
        by_provider = {}
        total = 0.0
        for prov_name, prov_data in month_data.items():
            spend = prov_data.get("total_usd", 0.0)
            by_provider[prov_name] = {
                "total_usd": spend,
                "generations": prov_data.get("generations", 0),
            }
            total += spend
        return {
            "month": month,
            "total_usd": round(total, 4),
            "by_provider": by_provider,
        }

    def check_budget(self, provider_name: str) -> bool:
        """Return True if the provider is within its monthly budget (or has no budget set)."""
        prov = self._providers.get(provider_name)
        if not prov or prov.monthly_budget_usd <= 0:
            return True
        spend = self.get_month_spend(provider_name)
        return spend < prov.monthly_budget_usd

    def get_budget_remaining(self, provider_name: str) -> Optional[float]:
        prov = self._providers.get(provider_name)
        if not prov or prov.monthly_budget_usd <= 0:
            return None
        return max(0, prov.monthly_budget_usd - self.get_month_spend(provider_name))

    # ── Async Job Polling ──────────────────────────────────────────

    def poll_until_complete(self, status_url: str, headers: dict,
                           timeout: float = 300, interval: float = 10,
                           success_statuses: tuple = ("succeed", "completed", "complete", "done"),
                           fail_statuses: tuple = ("failed", "error", "cancelled"),
                           status_key: str = "status",
                           data_key: str = "data",
                           log_prefix: str = "Cloud") -> dict:
        """Poll an async job endpoint until it completes or times out.

        Returns the parsed response dict on success.
        Raises RuntimeError on failure or timeout.
        """
        import urllib.request
        import urllib.error

        start = time.time()
        last_status = ""
        while time.time() - start < timeout:
            try:
                req = urllib.request.Request(status_url, headers=headers, method="GET")
                with urllib.request.urlopen(req, timeout=30) as resp:
                    result = json.loads(resp.read().decode("utf-8"))
            except urllib.error.HTTPError as e:
                body = ""
                try:
                    body = e.read().decode("utf-8", errors="replace")[:300]
                except Exception:
                    pass
                raise RuntimeError(f"{log_prefix} API error HTTP {e.code}: {body}")
            except Exception as e:
                log.warning("%s poll error: %s", log_prefix, e)
                time.sleep(interval)
                continue

            data = result.get(data_key, result)
            if isinstance(data, dict):
                status = str(data.get(status_key, "")).lower()
            else:
                status = str(result.get(status_key, "")).lower()

            if status != last_status:
                log.info("%s job status: %s", log_prefix, status)
                last_status = status

            if status in success_statuses:
                return data if isinstance(data, dict) else result

            if status in fail_statuses:
                error_msg = ""
                if isinstance(data, dict):
                    error_msg = data.get("error", data.get("message", ""))
                raise RuntimeError(f"{log_prefix} job failed: {status} — {error_msg}")

            time.sleep(interval)

        raise RuntimeError(f"{log_prefix} job timed out after {timeout}s (last status: {last_status})")

    # ── HTTP Helpers ───────────────────────────────────────────────

    def api_post(self, provider_name: str, endpoint: str,
                 payload: dict, extra_headers: dict | None = None,
                 timeout: int = 60) -> dict:
        """POST to a provider's API. Returns parsed JSON response."""
        import urllib.request
        import urllib.error

        base = self.get_api_base(provider_name)
        url = f"{base}{endpoint}" if not endpoint.startswith("http") else endpoint
        headers = self.get_auth_headers(provider_name)
        headers["Content-Type"] = "application/json"
        if extra_headers:
            headers.update(extra_headers)

        body = json.dumps(payload).encode("utf-8")
        req = urllib.request.Request(url, data=body, headers=headers, method="POST")
        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                return json.loads(resp.read().decode("utf-8"))
        except urllib.error.HTTPError as e:
            error_body = ""
            try:
                error_body = e.read().decode("utf-8", errors="replace")[:500]
            except Exception:
                pass
            raise RuntimeError(
                f"{KNOWN_PROVIDERS.get(provider_name, {}).get('display_name', provider_name)} "
                f"API error HTTP {e.code}: {error_body}"
            )

    def api_get(self, provider_name: str, endpoint: str,
                extra_headers: dict | None = None,
                timeout: int = 30) -> dict:
        """GET from a provider's API. Returns parsed JSON response."""
        import urllib.request
        import urllib.error

        base = self.get_api_base(provider_name)
        url = f"{base}{endpoint}" if not endpoint.startswith("http") else endpoint
        headers = self.get_auth_headers(provider_name)
        if extra_headers:
            headers.update(extra_headers)

        req = urllib.request.Request(url, headers=headers, method="GET")
        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                return json.loads(resp.read().decode("utf-8"))
        except urllib.error.HTTPError as e:
            error_body = ""
            try:
                error_body = e.read().decode("utf-8", errors="replace")[:500]
            except Exception:
                pass
            raise RuntimeError(
                f"{KNOWN_PROVIDERS.get(provider_name, {}).get('display_name', provider_name)} "
                f"API error HTTP {e.code}: {error_body}"
            )

    def download_file(self, url: str, headers: dict | None = None,
                      timeout: int = 120) -> bytes:
        """Download a file from a URL and return its bytes."""
        import urllib.request
        req = urllib.request.Request(url, headers=headers or {}, method="GET")
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return resp.read()


def test_provider_key(provider_name: str, api_key: str,
                      secret_key: str = "") -> dict:
    """Test a provider API key by making a lightweight validation call.

    For Kling, both api_key (access key) and secret_key are needed to generate
    a JWT token for authentication.
    """
    import urllib.request
    import urllib.error

    info = KNOWN_PROVIDERS.get(provider_name)
    if not info:
        return {"ok": False, "error": f"Unknown provider: {provider_name}"}

    base = info["api_base"]
    auth_type = info.get("auth_type", "bearer")

    if auth_type == "kling_jwt":
        if not secret_key:
            return {"ok": False, "error": "Kling requires both Access Key and Secret Key"}
        try:
            import jwt
        except ImportError:
            return {"ok": False, "error": "PyJWT package not installed (pip install PyJWT)"}
        now = int(time.time())
        token = jwt.encode(
            {"iss": api_key, "exp": now + 1800, "nbf": now - 5},
            secret_key, algorithm="HS256",
            headers={"alg": "HS256", "typ": "JWT"},
        )
        headers = {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}
    else:
        headers = {"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"}

    # Runware validates keys via a POST authentication task
    if provider_name == "runware":
        url = base
        payload = json.dumps([{"taskType": "authentication", "apiKey": api_key}]).encode()
        try:
            req = urllib.request.Request(url, data=payload, headers=headers, method="POST")
            with urllib.request.urlopen(req, timeout=15) as resp:
                result = json.loads(resp.read().decode("utf-8"))
            data = result.get("data", [])
            if data and data[0].get("connectionSessionUUID"):
                return {"ok": True, "message": "Connected to Runware"}
            errors = result.get("errors", [])
            if errors:
                return {"ok": False, "error": errors[0].get("message", "Authentication failed")}
            return {"ok": True, "message": "Connected to Runware"}
        except urllib.error.HTTPError as e:
            if e.code in (401, 403):
                return {"ok": False, "error": "Invalid Runware API key"}
            body = ""
            try:
                body = e.read().decode("utf-8", errors="replace")[:200]
            except Exception:
                pass
            return {"ok": False, "error": f"HTTP {e.code}: {body}"}
        except Exception as e:
            return {"ok": False, "error": str(e)[:200]}

    test_endpoints = {
        "kling": "/videos/text2video",
        "runway": "/tasks",
        "minimax": "/video_generation",
        "luma": "/generations",
        "pika": "/generate",
    }
    endpoint = test_endpoints.get(provider_name, "/")
    url = f"{base}{endpoint}"

    extra = info.get("extra_headers", {})
    if extra:
        headers.update(extra)

    try:
        req = urllib.request.Request(url, headers=headers, method="GET")
        with urllib.request.urlopen(req, timeout=15) as resp:
            return {"ok": True, "message": f"Connected to {info['display_name']}"}
    except urllib.error.HTTPError as e:
        if e.code == 401:
            return {"ok": False, "error": "Invalid API key (401 Unauthorized)"}
        if e.code == 403:
            return {"ok": False, "error": "Access denied (403 Forbidden)"}
        if e.code in (404, 405):
            return {"ok": True, "message": f"API key accepted by {info['display_name']}"}
        body = ""
        try:
            body = e.read().decode("utf-8", errors="replace")[:200]
        except Exception:
            pass
        return {"ok": False, "error": f"HTTP {e.code}: {body}"}
    except Exception as e:
        return {"ok": False, "error": str(e)[:200]}
