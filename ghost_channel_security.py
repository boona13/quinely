"""Ghost Channel Security — DM policy enforcement, rate limiting, risk scoring."""
import json, logging, time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, List, Optional, Any
from collections import defaultdict

log = logging.getLogger("quinely.channel_security")
GHOST_HOME = Path.home() / ".ghost"
CHANNEL_AUDIT_LOG = GHOST_HOME / "channel_audit.jsonl"
RISK_SCORE_HIGH, RISK_SCORE_MEDIUM = 80, 60

HIGH_RISK_KEYWORDS = [
    "ignore previous instructions", "system prompt", "override", "bypass",
    "jailbreak", "DAN mode", "urgent", "immediate action required",
    "click here", "verify your account", "suspended", "delete all",
    "drop table", "rm -rf", "format drive",
]

TOOL_TRIGGER_KEYWORDS = [
    "shell_exec", "file_write", "evolve_plan", "evolve_apply",
    "evolve_deploy", "add_future_feature", "webhook",
]

@dataclass
class SecurityDecision:
    allowed: bool
    action: str
    risk_score: int
    reason: str
    quarantine_path: Optional[Path] = None
    audit_entry: Dict[str, Any] = field(default_factory=dict)


class ChannelSecurityEnforcer:
    def __init__(self, config: Dict[str, Any]):
        self.config = config or {}
        self._rate_tracker: Dict[str, List[float]] = defaultdict(list)
        self._quarantine_dir = GHOST_HOME / "quarantine"
        self._quarantine_dir.mkdir(exist_ok=True)

    def check_message(self, sender_id: str, text: str, channel_id: str) -> SecurityDecision:
        dm_policy = self.config.get("channel_dm_policy", "open")
        allowed_senders = set(self.config.get("channel_allowed_senders", []))
        inbound_enabled = self.config.get("channel_inbound_enabled", True)
        audit_entry = {
            "timestamp": time.time(),
            "iso_time": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            "sender_id": sender_id, "channel_id": channel_id,
            "text_preview": text[:100] + "..." if len(text) > 100 else text,
            "dm_policy": dm_policy,
        }
        if not inbound_enabled:
            audit_entry.update({"action": "block", "reason": "inbound_disabled"})
            self._append_audit_log(audit_entry)
            return SecurityDecision(False, "block", 0, "Inbound disabled", audit_entry=audit_entry)
        if dm_policy == "allowlist" and sender_id not in allowed_senders:
            audit_entry.update({"action": "block", "reason": "not_in_allowlist"})
            self._append_audit_log(audit_entry)
            return SecurityDecision(False, "block", 0, f"Sender {sender_id} not in allowlist", audit_entry=audit_entry)
        if dm_policy == "block":
            audit_entry.update({"action": "block", "reason": "dm_policy_block"})
            self._append_audit_log(audit_entry)
            return SecurityDecision(False, "block", 0, "DM policy block", audit_entry=audit_entry)
        rate_limit = self.config.get("channel_rate_limit_per_minute", 10)
        if rate_limit > 0 and self._is_rate_limited(sender_id, rate_limit):
            audit_entry.update({"action": "rate_limited", "reason": "rate_limit_exceeded"})
            self._append_audit_log(audit_entry)
            return SecurityDecision(False, "rate_limited", 0, f"Rate limit: {rate_limit}/min", audit_entry=audit_entry)
        risk_score = self._calculate_risk_score(text)
        audit_entry["risk_score"] = risk_score
        if dm_policy == "open" and risk_score >= RISK_SCORE_HIGH:
            quarantine_path = self._quarantine_message(sender_id, text, channel_id, risk_score)
            audit_entry.update({"action": "quarantine", "reason": "high_risk_score", "quarantine_path": str(quarantine_path)})
            self._append_audit_log(audit_entry)
            return SecurityDecision(False, "quarantine", risk_score, f"High risk ({risk_score})", quarantine_path, audit_entry)
        if dm_policy == "open" and risk_score >= RISK_SCORE_MEDIUM:
            audit_entry.update({"action": "allow_warn", "reason": "medium_risk_score"})
            self._append_audit_log(audit_entry)
            return SecurityDecision(True, "allow_warn", risk_score, f"Medium risk ({risk_score})", audit_entry=audit_entry)
        audit_entry.update({"action": "allow", "reason": "policy_permits"})
        self._append_audit_log(audit_entry)
        return SecurityDecision(True, "allow", risk_score, "Allowed", audit_entry=audit_entry)

    def _is_rate_limited(self, sender_id: str, limit_per_minute: int) -> bool:
        now = time.time()
        self._rate_tracker[sender_id] = [t for t in self._rate_tracker[sender_id] if t > now - 60]
        if len(self._rate_tracker[sender_id]) >= limit_per_minute:
            return True
        self._rate_tracker[sender_id].append(now)
        return False

    def _calculate_risk_score(self, text: str) -> int:
        score = sum(15 for kw in HIGH_RISK_KEYWORDS if kw.lower() in text.lower())
        tool_triggers = sum(1 for kw in TOOL_TRIGGER_KEYWORDS if kw.lower() in text.lower())
        if tool_triggers: score += 20 * tool_triggers
        if len(text) < 10: score += 5
        if len(text) > 2000: score += 10
        special = sum(1 for c in text if not c.isalnum() and not c.isspace())
        if text and special / len(text) > 0.3: score += 15
        return min(100, score)

    def _quarantine_message(self, sender_id: str, text: str, channel_id: str, risk_score: int) -> Path:
        timestamp = time.strftime("%Y%m%d_%H%M%S", time.gmtime())
        path = self._quarantine_dir / f"quarantine_{channel_id}_{sender_id}_{timestamp}.json"
        path.write_text(json.dumps({
            "timestamp": time.time(), "iso_time": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            "sender_id": sender_id, "channel_id": channel_id, "risk_score": risk_score, "text": text,
        }, indent=2), encoding="utf-8")
        log.warning("Quarantined: %s (risk=%d)", path, risk_score)
        return path

    def _append_audit_log(self, entry: Dict[str, Any]):
        try:
            with open(CHANNEL_AUDIT_LOG, "a", encoding="utf-8") as f:
                f.write(json.dumps(entry) + "\n")
        except Exception as e:
            log.error("Audit log write failed: %s", e)

    def get_quarantined_messages(self) -> List[Dict[str, Any]]:
        result = []
        for f in sorted(self._quarantine_dir.glob("quarantine_*.json")):
            try:
                data = json.loads(f.read_text(encoding="utf-8"))
                data["_quarantine_path"] = str(f)
                result.append(data)
            except (json.JSONDecodeError, OSError) as e:
                log.warning("Failed to read quarantine file %s: %s", f, e)
        return result

    def release_from_quarantine(self, quarantine_path: Path) -> bool:
        try:
            if quarantine_path.exists():
                quarantine_path.unlink()
                log.info("Released: %s", quarantine_path)
                return True
        except Exception as e:
            log.error("Release failed: %s", e)
        return False


def build_channel_security_tools(config: Dict[str, Any]) -> List[Dict[str, Any]]:
    enforcer = ChannelSecurityEnforcer(config)
    tools = []
    def channel_security_audit(limit: int = 50) -> str:
        entries = []
        if CHANNEL_AUDIT_LOG.exists():
            try:
                with open(CHANNEL_AUDIT_LOG, encoding="utf-8") as f:
                    lines = f.readlines()
                for line in reversed(lines[-limit:]):
                    try:
                        entries.append(json.loads(line))
                    except json.JSONDecodeError as e:
                        log.warning("Failed to parse audit log line: %s", e)
            except OSError as e:
                log.warning("Failed to read audit log: %s", e)
        if not entries: return "No audit entries."
        lines = [f"Recent events ({len(entries)}):"]
        for e in entries:
            lines.append(f"[{e.get('iso_time','?')}] {e.get('action','?').upper()} | {e.get('channel_id','?')} | {e.get('sender_id','?')} | risk={e.get('risk_score','N/A')}")
        return "\n".join(lines)
    tools.append({"name": "channel_security_audit", "description": "View channel security audit log.", "parameters": {"type": "object", "properties": {"limit": {"type": "integer", "default": 50}}}, "execute": channel_security_audit})

    def channel_quarantine_list() -> str:
        quarantined = enforcer.get_quarantined_messages()
        if not quarantined: return "No quarantined messages."
        lines = [f"Quarantined ({len(quarantined)}):"]
        for m in quarantined:
            lines.append(f"[{m.get('iso_time','?')}] {m.get('channel_id','?')}/{m.get('sender_id','?')} | risk={m.get('risk_score',0)}")
            lines.append(f"  Path: {m.get('_quarantine_path','?')}")
        return "\n".join(lines)
    tools.append({"name": "channel_quarantine_list", "description": "List quarantined messages.", "parameters": {"type": "object", "properties": {}}, "execute": channel_quarantine_list})

    def channel_quarantine_release(quarantine_path: str) -> str:
        return "Released" if enforcer.release_from_quarantine(Path(quarantine_path)) else "Failed"
    tools.append({"name": "channel_quarantine_release", "description": "Release quarantined message.", "parameters": {"type": "object", "properties": {"quarantine_path": {"type": "string"}}, "required": ["quarantine_path"]}, "execute": channel_quarantine_release})

    def channel_add_allowed_sender(sender_id: str) -> str:
        from ghost import load_config, save_config
        cfg = load_config()
        allowed = list(cfg.get("channel_allowed_senders", []))
        if sender_id not in allowed:
            allowed.append(sender_id)
            cfg["channel_allowed_senders"] = allowed
            save_config(cfg)
        return f"Added {sender_id} to allowlist"
    tools.append({"name": "channel_add_allowed_sender", "description": "Add sender to channel allowlist.", "parameters": {"type": "object", "properties": {"sender_id": {"type": "string"}}, "required": ["sender_id"]}, "execute": channel_add_allowed_sender})

    def channel_remove_allowed_sender(sender_id: str) -> str:
        from ghost import load_config, save_config
        cfg = load_config()
        allowed = [s for s in cfg.get("channel_allowed_senders", []) if s != sender_id]
        cfg["channel_allowed_senders"] = allowed
        save_config(cfg)
        return f"Removed {sender_id} from allowlist"
    tools.append({"name": "channel_remove_allowed_sender", "description": "Remove sender from channel allowlist.", "parameters": {"type": "object", "properties": {"sender_id": {"type": "string"}}, "required": ["sender_id"]}, "execute": channel_remove_allowed_sender})
    return tools
