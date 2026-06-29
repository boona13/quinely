"""
Ghost Webhook Triggers — Event-Driven Autonomy

Lets external services (GitHub, CI, Stripe, custom) fire Ghost actions in
real-time via HTTP webhooks. Each trigger has a pre-defined prompt template
that is populated from the event payload — the webhook sender cannot inject
arbitrary instructions.

Security:
  - Bearer token auth (webhook_secret in config) required on every request
  - Optional HMAC signature verification per trigger
  - Triggers are pre-defined, not arbitrary prompts
  - Tool subset excludes evolve tools (same as chat/channels)
  - Per-trigger cooldown + global concurrency limit
"""

import hashlib
import hmac
import json
import logging
import re
import threading
import time
import uuid
from collections import deque
from dataclasses import dataclass, field, asdict
from datetime import datetime
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional

log = logging.getLogger("ghost.webhooks")

GHOST_HOME = Path.home() / ".ghost"
WEBHOOKS_FILE = GHOST_HOME / "webhooks.json"
WEBHOOK_HISTORY_FILE = GHOST_HOME / "webhook_history.json"

_HISTORY_MAX = 100


# ═══════════════════════════════════════════════════════════════
#  DATA
# ═══════════════════════════════════════════════════════════════

@dataclass
class WebhookTrigger:
    id: str
    name: str
    prompt_template: str
    event_type: str = "generic"
    extract_fields: Dict[str, str] = field(default_factory=dict)
    cooldown_seconds: int = 30
    hmac_header: str = ""
    hmac_secret: str = ""
    enabled: bool = True
    created_at: str = ""
    last_fired: float = 0

    def to_dict(self) -> dict:
        d = asdict(self)
        if self.hmac_secret:
            d["hmac_secret"] = "***"
        return d

    @classmethod
    def from_dict(cls, d: dict) -> "WebhookTrigger":
        known = {f.name for f in cls.__dataclass_fields__.values()}
        return cls(**{k: v for k, v in d.items() if k in known})


BUILTIN_TEMPLATES = {
    "github_push": {
        "name": "GitHub Push",
        "prompt_template": (
            "A GitHub push event just occurred.\n"
            "Repository: {repository}\n"
            "Branch: {branch}\n"
            "Pusher: {pusher}\n"
            "Commit count: {commit_count}\n"
            "Commits:\n{commits}\n\n"
            "Review the pushed changes for bugs, security issues, and code quality. "
            "Save your findings to memory."
        ),
        "extract_fields": {
            "repository": "repository.full_name",
            "branch": "ref",
            "pusher": "pusher.name",
            "commit_count": "commits.#len",
            "commits": "commits[].message",
        },
    },
    "github_pr": {
        "name": "GitHub Pull Request",
        "prompt_template": (
            "A GitHub pull request event occurred.\n"
            "Repository: {repository}\n"
            "Action: {action}\n"
            "PR #{pr_number}: {pr_title}\n"
            "Author: {author}\n"
            "Branch: {head_branch} -> {base_branch}\n"
            "Body:\n{body}\n\n"
            "If the PR was opened or updated, review it for quality, correctness, "
            "and potential issues. Save findings to memory."
        ),
        "extract_fields": {
            "repository": "repository.full_name",
            "action": "action",
            "pr_number": "pull_request.number",
            "pr_title": "pull_request.title",
            "author": "pull_request.user.login",
            "head_branch": "pull_request.head.ref",
            "base_branch": "pull_request.base.ref",
            "body": "pull_request.body",
        },
    },
    "github_issue": {
        "name": "GitHub Issue",
        "prompt_template": (
            "A GitHub issue event occurred.\n"
            "Repository: {repository}\n"
            "Action: {action}\n"
            "Issue #{issue_number}: {issue_title}\n"
            "Author: {author}\n"
            "Body:\n{body}\n\n"
            "If this is a new issue or a relevant update, analyze it and save "
            "key information to memory."
        ),
        "extract_fields": {
            "repository": "repository.full_name",
            "action": "action",
            "issue_number": "issue.number",
            "issue_title": "issue.title",
            "author": "issue.user.login",
            "body": "issue.body",
        },
    },
    "generic": {
        "name": "Generic Webhook",
        "prompt_template": (
            "A webhook event was received.\n"
            "Event type: {event_type}\n"
            "Payload summary:\n{payload_summary}\n\n"
            "Analyze this event and take appropriate action. "
            "Save any important findings to memory."
        ),
        "extract_fields": {
            "event_type": "_meta.event_type",
            "payload_summary": "_meta.payload_summary",
        },
    },
}


# ═══════════════════════════════════════════════════════════════
#  PAYLOAD FIELD EXTRACTION
# ═══════════════════════════════════════════════════════════════

def _extract_field(payload: dict, path: str) -> str:
    """Extract a value from a nested dict using dot-notation path.

    Supports:
      - "key.subkey" -> payload["key"]["subkey"]
      - "items[].name" -> comma-joined list of payload["items"][i]["name"]
      - "items.#len" -> len(payload["items"])
      - "_meta.event_type" -> trigger metadata injected by handler
    """
    if not path or not payload:
        return ""

    if "[]." in path:
        array_path, rest = path.split("[].", 1)
        array = _walk(payload, array_path)
        if isinstance(array, list):
            items = []
            for item in array[:20]:
                val = _walk(item, rest) if isinstance(item, dict) else item
                if val is not None:
                    items.append(str(val))
            return "\n".join(items) if items else "(none)"
        return "(none)"

    if path.endswith(".#len"):
        array_path = path[:-5]
        val = _walk(payload, array_path)
        return str(len(val)) if isinstance(val, (list, dict)) else "0"

    val = _walk(payload, path)
    if val is None:
        return "(not found)"
    if isinstance(val, (dict, list)):
        return json.dumps(val, indent=2, default=str)[:2000]
    return str(val)


def _walk(obj: Any, path: str) -> Any:
    """Walk a nested dict/list using dot-notation."""
    for key in path.split("."):
        if obj is None:
            return None
        if isinstance(obj, dict):
            obj = obj.get(key)
        elif isinstance(obj, list):
            try:
                obj = obj[int(key)]
            except (ValueError, IndexError):
                return None
        else:
            return None
    return obj


# ═══════════════════════════════════════════════════════════════
#  REGISTRY
# ═══════════════════════════════════════════════════════════════

class WebhookRegistry:
    """CRUD for webhook trigger definitions. Persisted to ~/.ghost/webhooks.json."""

    def __init__(self):
        GHOST_HOME.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()
        self._triggers: Dict[str, WebhookTrigger] = {}
        self._load()

    def _load(self):
        if WEBHOOKS_FILE.exists():
            try:
                data = json.loads(WEBHOOKS_FILE.read_text(encoding="utf-8"))
                for tid, tdict in data.get("triggers", {}).items():
                    self._triggers[tid] = WebhookTrigger.from_dict(tdict)
            except Exception as e:
                log.warning("Failed to load webhook triggers: %s", e)

    def _save(self):
        data = {
            "triggers": {
                tid: asdict(t) for tid, t in self._triggers.items()
            },
        }
        WEBHOOKS_FILE.write_text(json.dumps(data, indent=2, default=str), encoding="utf-8")

    def create(self, name: str, prompt_template: str,
               event_type: str = "generic",
               extract_fields: Optional[Dict[str, str]] = None,
               cooldown_seconds: int = 30,
               hmac_header: str = "",
               hmac_secret: str = "") -> WebhookTrigger:
        tid = re.sub(r"[^a-z0-9-]", "-", name.lower().strip())[:40]
        tid = re.sub(r"-+", "-", tid).strip("-")
        if not tid:
            tid = uuid.uuid4().hex[:12]

        with self._lock:
            if tid in self._triggers:
                tid = f"{tid}-{uuid.uuid4().hex[:6]}"

            trigger = WebhookTrigger(
                id=tid,
                name=name,
                prompt_template=prompt_template,
                event_type=event_type,
                extract_fields=extract_fields or {},
                cooldown_seconds=cooldown_seconds,
                hmac_header=hmac_header,
                hmac_secret=hmac_secret,
                created_at=datetime.now().isoformat(),
            )
            self._triggers[tid] = trigger
            self._save()
        return trigger

    def create_from_template(self, template_id: str,
                             name: str = "",
                             cooldown_seconds: int = 30,
                             hmac_header: str = "",
                             hmac_secret: str = "") -> Optional[WebhookTrigger]:
        tmpl = BUILTIN_TEMPLATES.get(template_id)
        if not tmpl:
            return None
        return self.create(
            name=name or tmpl["name"],
            prompt_template=tmpl["prompt_template"],
            event_type=template_id,
            extract_fields=tmpl["extract_fields"],
            cooldown_seconds=cooldown_seconds,
            hmac_header=hmac_header,
            hmac_secret=hmac_secret,
        )

    def get(self, trigger_id: str) -> Optional[WebhookTrigger]:
        with self._lock:
            return self._triggers.get(trigger_id)

    def list_all(self) -> List[WebhookTrigger]:
        with self._lock:
            return list(self._triggers.values())

    def delete(self, trigger_id: str) -> bool:
        with self._lock:
            if trigger_id in self._triggers:
                del self._triggers[trigger_id]
                self._save()
                return True
        return False

    def update(self, trigger_id: str, **updates) -> Optional[WebhookTrigger]:
        with self._lock:
            trigger = self._triggers.get(trigger_id)
            if not trigger:
                return None
            allowed = {"name", "prompt_template", "extract_fields",
                       "cooldown_seconds", "hmac_header", "hmac_secret", "enabled"}
            for key in allowed & set(updates.keys()):
                setattr(trigger, key, updates[key])
            self._save()
            return trigger

    def record_fire(self, trigger_id: str):
        with self._lock:
            trigger = self._triggers.get(trigger_id)
            if trigger:
                trigger.last_fired = time.time()
                self._save()


# ═══════════════════════════════════════════════════════════════
#  EVENT HISTORY
# ═══════════════════════════════════════════════════════════════

class WebhookHistory:
    """Ring buffer of recent webhook events, persisted to disk."""

    def __init__(self):
        self._lock = threading.Lock()
        self._events: deque = deque(maxlen=_HISTORY_MAX)
        self._load()

    def _load(self):
        if WEBHOOK_HISTORY_FILE.exists():
            try:
                data = json.loads(WEBHOOK_HISTORY_FILE.read_text(encoding="utf-8"))
                for evt in data[-_HISTORY_MAX:]:
                    self._events.append(evt)
            except Exception:
                pass

    def _save(self):
        try:
            WEBHOOK_HISTORY_FILE.write_text(
                json.dumps(list(self._events), indent=2, default=str),
                encoding="utf-8",
            )
        except Exception:
            pass

    def add(self, trigger_id: str, status: str, detail: str = ""):
        evt = {
            "id": uuid.uuid4().hex[:10],
            "trigger_id": trigger_id,
            "status": status,
            "detail": detail[:500],
            "timestamp": datetime.now().isoformat(),
        }
        with self._lock:
            self._events.append(evt)
            self._save()
        return evt

    def recent(self, limit: int = 50) -> list:
        with self._lock:
            return list(self._events)[-limit:]


# ═══════════════════════════════════════════════════════════════
#  HANDLER
# ═══════════════════════════════════════════════════════════════

class WebhookHandler:
    """Processes incoming webhook requests: auth, rate limit, dispatch."""

    def __init__(self, registry: WebhookRegistry, cfg: dict, daemon=None):
        self._registry = registry
        self._cfg = cfg
        self._daemon = daemon
        self._history = WebhookHistory()
        self._active_count = 0
        self._active_lock = threading.Lock()

    @property
    def registry(self) -> WebhookRegistry:
        return self._registry

    @property
    def history(self) -> WebhookHistory:
        return self._history

    def verify_auth(self, headers: dict) -> bool:
        """Check bearer token from Authorization header."""
        secret = self._cfg.get("webhook_secret", "")
        if not secret:
            return False
        auth = headers.get("Authorization", headers.get("authorization", ""))
        if auth.startswith("Bearer "):
            return hmac.compare_digest(auth[7:].strip(), secret)
        return False

    def verify_hmac(self, trigger: WebhookTrigger, body: bytes,
                    headers: dict) -> bool:
        """Verify HMAC signature if trigger has hmac_header configured."""
        if not trigger.hmac_header or not trigger.hmac_secret:
            return True
        sig = headers.get(trigger.hmac_header, "")
        if not sig:
            return False
        expected = hmac.new(
            trigger.hmac_secret.encode(), body, hashlib.sha256
        ).hexdigest()
        if sig.startswith("sha256="):
            sig = sig[7:]
        return hmac.compare_digest(sig, expected)

    def check_cooldown(self, trigger: WebhookTrigger) -> bool:
        """Return True if trigger is past its cooldown period."""
        if trigger.cooldown_seconds <= 0:
            return True
        return (time.time() - trigger.last_fired) >= trigger.cooldown_seconds

    def check_concurrency(self) -> bool:
        max_concurrent = self._cfg.get("webhook_max_concurrent", 3)
        with self._active_lock:
            return self._active_count < max_concurrent

    def build_prompt(self, trigger: WebhookTrigger, payload: dict) -> str:
        """Build the LLM prompt by interpolating payload fields into template."""
        payload["_meta"] = {
            "event_type": trigger.event_type,
            "trigger_name": trigger.name,
            "payload_summary": json.dumps(payload, indent=2, default=str)[:3000],
        }

        field_values = {}
        for var_name, path in trigger.extract_fields.items():
            field_values[var_name] = _extract_field(payload, path)

        try:
            return trigger.prompt_template.format(**field_values)
        except KeyError as e:
            missing = str(e)
            return (
                f"{trigger.prompt_template}\n\n"
                f"[Note: field {missing} not found in payload. "
                f"Available extracted fields: {json.dumps(field_values, default=str)[:1000]}]"
            )

    def handle(self, trigger_id: str, payload: dict, headers: dict,
               raw_body: bytes = b"") -> dict:
        """Full webhook handling pipeline. Returns result dict."""
        trigger = self._registry.get(trigger_id)
        if not trigger:
            return {"ok": False, "error": "Trigger not found", "status": 404}

        if not trigger.enabled:
            return {"ok": False, "error": "Trigger is disabled", "status": 403}

        if not self.verify_auth(headers):
            self._history.add(trigger_id, "auth_failed")
            return {"ok": False, "error": "Unauthorized", "status": 401}

        if not self.verify_hmac(trigger, raw_body, headers):
            self._history.add(trigger_id, "hmac_failed")
            return {"ok": False, "error": "HMAC verification failed", "status": 403}

        if not self.check_cooldown(trigger):
            remaining = trigger.cooldown_seconds - (time.time() - trigger.last_fired)
            self._history.add(trigger_id, "cooldown",
                              f"{remaining:.0f}s remaining")
            return {"ok": False, "error": f"Cooldown active ({remaining:.0f}s remaining)",
                    "status": 429}

        if not self.check_concurrency():
            self._history.add(trigger_id, "concurrency_limit")
            return {"ok": False, "error": "Max concurrent webhooks reached",
                    "status": 429}

        prompt = self.build_prompt(trigger, payload)
        self._registry.record_fire(trigger_id)
        self._history.add(trigger_id, "dispatched",
                          prompt[:200])

        thread = threading.Thread(
            target=self._dispatch,
            args=(trigger, prompt),
            name=f"webhook-{trigger_id[:20]}",
            daemon=True,
        )
        thread.start()

        return {"ok": True, "trigger_id": trigger_id, "status": 202}

    def _dispatch(self, trigger: WebhookTrigger, prompt: str):
        """Run the tool loop for a webhook event (in a dedicated thread)."""
        daemon = self._daemon
        if not daemon:
            log.error("Webhook dispatch failed: no daemon reference")
            self._history.add(trigger.id, "error", "No daemon")
            return

        with self._active_lock:
            self._active_count += 1

        try:
            from ghost_console import console_bus
            from ghost import append_feed, _EVOLVE_TOOL_NAMES

            console_bus.emit(
                "info", "webhook", f"Webhook fired: {trigger.name}",
                detail=prompt[:300],
            )

            identity = ""
            if hasattr(daemon, "_build_identity_context"):
                try:
                    identity = daemon._build_identity_context()
                except Exception:
                    pass

            system_prompt = (
                identity
                + "## WEBHOOK TRIGGER\n"
                f"You are Quinely responding to an automated webhook event: **{trigger.name}**\n"
                f"Event type: {trigger.event_type}\n\n"
                "Analyze the event data below and take appropriate action. "
                "Use your tools to investigate, save findings, notify if important, "
                "and complete the task efficiently.\n\n"
            )

            safe_names = [
                name for name in daemon.tool_registry.get_all()
                if name not in _EVOLVE_TOOL_NAMES
            ]
            registry = daemon.tool_registry.subset(safe_names)

            loop_result = daemon.engine.run(
                system_prompt=system_prompt,
                user_message=prompt,
                tool_registry=registry,
                max_steps=daemon.cfg.get("tool_loop_max_steps", 200),
                max_tokens=4096,
                force_tool=True,
                hook_runner=daemon.hooks if hasattr(daemon, "hooks") else None,
                tool_intent_security=(
                    daemon.tool_intent_security
                    if hasattr(daemon, "tool_intent_security") else None
                ),
                tool_event_bus=getattr(daemon, "tool_event_bus", None),
            )

            result_text = (loop_result.text or "")[:2000]

            append_feed({
                "time": datetime.now().isoformat(),
                "type": "webhook",
                "source": f"[webhook: {trigger.name}] {prompt[:300]}",
                "result": result_text or "(no output)",
                "tools_used": list(set(
                    tc["tool"] for tc in loop_result.tool_calls
                )),
            }, daemon.cfg.get("max_feed_items", 50))

            console_bus.emit(
                "info", "webhook", f"Webhook complete: {trigger.name}",
                result=result_text[:300],
            )

            self._history.add(trigger.id, "completed", result_text[:200])

            if hasattr(daemon, "actions_today"):
                daemon.actions_today += 1

        except Exception as e:
            log.error("Webhook dispatch error for '%s': %s", trigger.name, e)
            self._history.add(trigger.id, "error", str(e)[:300])
        finally:
            with self._active_lock:
                self._active_count -= 1


# ═══════════════════════════════════════════════════════════════
#  LLM TOOLS
# ═══════════════════════════════════════════════════════════════

def build_webhook_tools(handler: WebhookHandler, cfg: dict) -> list:
    """Build LLM-callable tools for webhook trigger management."""

    dashboard_port = cfg.get("dashboard_port", 3333)

    def create_exec(name, prompt_template, event_type="generic",
                    extract_fields=None, cooldown_seconds=30,
                    template_id=""):
        if template_id:
            trigger = handler.registry.create_from_template(
                template_id, name=name, cooldown_seconds=cooldown_seconds,
            )
            if not trigger:
                available = ", ".join(BUILTIN_TEMPLATES.keys())
                return f"Unknown template '{template_id}'. Available: {available}"
        else:
            if not prompt_template:
                return "Either prompt_template or template_id is required."
            trigger = handler.registry.create(
                name=name,
                prompt_template=prompt_template,
                event_type=event_type,
                extract_fields=extract_fields or {},
                cooldown_seconds=cooldown_seconds,
            )

        url = f"http://localhost:{dashboard_port}/api/webhooks/{trigger.id}"
        return (
            f"Webhook trigger created.\n"
            f"  ID: {trigger.id}\n"
            f"  Name: {trigger.name}\n"
            f"  URL: {url}\n"
            f"  Event type: {trigger.event_type}\n"
            f"  Cooldown: {trigger.cooldown_seconds}s\n\n"
            f"Configure your external service to POST to this URL with header:\n"
            f"  Authorization: Bearer <webhook_secret from Ghost config>\n"
        )

    def list_exec():
        triggers = handler.registry.list_all()
        if not triggers:
            return "No webhook triggers configured."
        lines = []
        for t in triggers:
            status = "enabled" if t.enabled else "disabled"
            url = f"http://localhost:{dashboard_port}/api/webhooks/{t.id}"
            lines.append(
                f"- [{status}] {t.name} ({t.id})\n"
                f"  URL: {url}\n"
                f"  Type: {t.event_type} | Cooldown: {t.cooldown_seconds}s"
            )
        return "\n".join(lines)

    def delete_exec(trigger_id):
        ok = handler.registry.delete(trigger_id)
        if ok:
            return f"Trigger '{trigger_id}' deleted."
        return f"Trigger '{trigger_id}' not found."

    def test_exec(trigger_id, payload=None):
        if not payload:
            payload = {"test": True, "message": "This is a test webhook event"}
        result = handler.handle(
            trigger_id=trigger_id,
            payload=payload if isinstance(payload, dict) else json.loads(payload),
            headers={"Authorization": f"Bearer {cfg.get('webhook_secret', '')}"},
            raw_body=json.dumps(payload).encode(),
        )
        return json.dumps(result, indent=2)

    templates_desc = ", ".join(
        f"'{k}' ({v['name']})" for k, v in BUILTIN_TEMPLATES.items()
    )

    return [
        {
            "name": "webhook_create",
            "description": (
                "Create a webhook trigger that fires Ghost when an external service "
                "sends a POST request. Use template_id for common providers or "
                "provide a custom prompt_template. "
                f"Built-in templates: {templates_desc}"
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "name": {"type": "string",
                             "description": "Human-readable name for this trigger"},
                    "prompt_template": {
                        "type": "string",
                        "description": "Prompt template with {field} placeholders (or empty if using template_id)",
                        "default": "",
                    },
                    "event_type": {"type": "string",
                                   "description": "Event type label",
                                   "default": "generic"},
                    "extract_fields": {
                        "type": "object",
                        "description": "Map of template variable -> dot-notation path in payload JSON",
                        "default": {},
                    },
                    "cooldown_seconds": {"type": "integer",
                                         "description": "Min seconds between fires",
                                         "default": 30},
                    "template_id": {
                        "type": "string",
                        "description": f"Built-in template: {', '.join(BUILTIN_TEMPLATES.keys())}",
                        "default": "",
                    },
                },
                "required": ["name"],
            },
            "execute": create_exec,
        },
        {
            "name": "webhook_list",
            "description": "List all configured webhook triggers with their URLs and status.",
            "parameters": {"type": "object", "properties": {}},
            "execute": lambda **kw: list_exec(),
        },
        {
            "name": "webhook_delete",
            "description": "Delete a webhook trigger by ID.",
            "parameters": {
                "type": "object",
                "properties": {
                    "trigger_id": {"type": "string",
                                   "description": "ID of the trigger to delete"},
                },
                "required": ["trigger_id"],
            },
            "execute": delete_exec,
        },
        {
            "name": "webhook_test",
            "description": (
                "Test a webhook trigger by simulating an event with a payload. "
                "Runs the full dispatch pipeline including auth and prompt building."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "trigger_id": {"type": "string",
                                   "description": "ID of the trigger to test"},
                    "payload": {
                        "type": "object",
                        "description": "Test payload JSON (uses a default if omitted)",
                        "default": {},
                    },
                },
                "required": ["trigger_id"],
            },
            "execute": test_exec,
        },
    ]
