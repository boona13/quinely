"""Managed skill installer, preflight validator, and security scanner for SKILL.md packs."""

from __future__ import annotations

import logging
import os
import re
import shutil
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import yaml

from ghost_skills import SKILLS_USER_DIR

log = logging.getLogger("quinely.skill_manager")


@dataclass
class ValidationIssue:
    code: str
    severity: str
    message: str
    field: Optional[str] = None
    fix: Optional[str] = None

    def to_dict(self) -> Dict[str, Any]:
        return {
            "code": self.code,
            "severity": self.severity,
            "message": self.message,
            "field": self.field,
            "fix": self.fix,
        }


@dataclass
class SecurityFinding:
    rule: str
    severity: str  # critical, high, medium, low, info
    category: str  # prompt_injection, exfiltration, destructive, obfuscation, policy
    message: str
    evidence: str = ""
    line: int = 0

    def to_dict(self) -> Dict[str, Any]:
        d = {
            "rule": self.rule,
            "severity": self.severity,
            "category": self.category,
            "message": self.message,
        }
        if self.evidence:
            d["evidence"] = self.evidence[:200]
        if self.line:
            d["line"] = self.line
        return d


class SkillSecurityScanner:
    """Static security analysis for skill content before installation.

    Scans skill body and frontmatter for:
    - Prompt injection attempts
    - Data exfiltration patterns
    - Destructive command instructions
    - Obfuscated/encoded payloads
    - Policy violations (priority hijacking, trigger flooding)
    """

    PROMPT_INJECTION_PATTERNS = [
        (r"ignore\s+(all\s+)?(previous|prior|above|earlier)\s+(instructions?|rules?|guidelines?|constraints?)", "Prompt override attempt"),
        (r"(you\s+are\s+now|from\s+now\s+on|new\s+instructions?)\s+(a\s+|an\s+)?(unrestricted|unfiltered|uncensored|jailbroken)", "Jailbreak attempt"),
        (r"disregard\s+(all\s+)?(safety|security|previous|system)", "Safety bypass attempt"),
        (r"override\s+(system|safety|security)\s+(prompt|instructions?|rules?|policy)", "System prompt override"),
        (r"do\s+not\s+follow\s+(any\s+)?(previous|prior|safety|security)", "Instruction override"),
        (r"pretend\s+(you\s+)?(are|have)\s+no\s+(restrictions?|rules?|limitations?|safety)", "Restriction removal"),
        (r"\[system\]|\[SYSTEM\]|<\|system\|>|<\|im_start\|>system", "Fake system message injection"),
        (r"IMPORTANT:\s*override|CRITICAL:\s*ignore", "Urgency-based override"),
    ]

    EXFILTRATION_PATTERNS = [
        (r"curl\s+.*\s+(https?://|ftp://)\S+.*(-d|--data|-F|--form|--upload)", "Data upload via curl"),
        (r"wget\s+--post-(data|file)", "Data upload via wget"),
        (r"(curl|wget|fetch|http)\s+.*\|\s*(bash|sh|zsh|python)", "Remote code execution pipe"),
        (r"(send|post|upload|transmit|exfiltrate?)\s+(to|data|file|content|config|key|secret|token|password|credential).*\s+(https?://|external|remote|server)", "Data exfiltration instruction"),
        (r"(read|cat|dump|extract)\s+.*\.(env|pem|key|secret|credentials?|auth|token)", "Sensitive file access"),
        (r"(read|cat|show|print|output)\s+.*(config\.json|auth_profiles|\.ssh/|\.gnupg/|\.aws/)", "Sensitive config access"),
        (r"(send|share|upload|post)\s+.*(api[_\s]?key|secret|token|password|credential)", "Credential exfiltration"),
    ]

    DESTRUCTIVE_PATTERNS = [
        (r"rm\s+(-rf?|--recursive)\s+[~/]", "Recursive deletion of user files"),
        (r"rm\s+(-rf?|--recursive)\s+/(?!tmp)", "Recursive deletion of system files"),
        (r"chmod\s+777\s+", "Overly permissive file permissions"),
        (r"mkfs\.|format\s+[A-Z]:", "Disk formatting"),
        (r"dd\s+if=.*/dev/(zero|random|urandom)\s+of=", "Disk overwrite via dd"),
        (r":()\s*\{\s*:\s*\|\s*:\s*&\s*\}\s*;", "Fork bomb"),
        (r">\s*/dev/sd[a-z]|>\s*/dev/disk", "Direct disk write"),
    ]

    OBFUSCATION_PATTERNS = [
        (r"base64\s+(-d|--decode|decode)", "Base64 decoding of commands"),
        (r"echo\s+[A-Za-z0-9+/=]{20,}\s*\|\s*(base64|decode)", "Encoded payload execution"),
        (r"\\x[0-9a-fA-F]{2}(\\x[0-9a-fA-F]{2}){5,}", "Hex-encoded payload"),
        (r"eval\s*\(|exec\s*\(", "Dynamic code evaluation"),
        (r"python\s+-c\s+['\"]import\s+(os|subprocess|socket|urllib|requests)", "Inline Python execution"),
        (r"node\s+-e\s+['\"]require\s*\(\s*['\"](?:child_process|net|http|fs)", "Inline Node.js execution"),
        (r"\$\(.*\)|`[^`]{10,}`", "Command substitution (potential hidden commands)"),
    ]

    GHOST_SELF_MODIFICATION_PATTERNS = [
        (r"(modify|edit|write|overwrite|replace|change)\s+.*SOUL\.md", "Attempt to modify Ghost's personality"),
        (r"(modify|edit|write|overwrite)\s+.*(ghost\.py|ghost_\w+\.py|ghost_supervisor)", "Attempt to modify Ghost source code"),
        (r"(modify|edit|write|delete)\s+.*skills/.*/SKILL\.md", "Attempt to modify other skills"),
        (r"(delete|remove|rm)\s+.*\.ghost/", "Attempt to delete Ghost state files"),
        (r"(modify|write|edit)\s+.*allowed_(commands|roots)", "Attempt to weaken security boundaries"),
    ]

    MAX_SAFE_PRIORITY = 50
    MIN_TRIGGER_LENGTH = 2
    MAX_TRIGGERS = 50

    KNOWN_TOOLS = {
        "shell_exec", "file_read", "file_write", "file_search",
        "web_fetch", "web_search", "clipboard_read", "clipboard_write",
        "notify", "app_control", "browser", "generate_image",
        "memory_search", "memory_save", "semantic_memory_search",
        "semantic_memory_save", "canvas",
        "google_gmail", "google_calendar", "google_drive",
        "google_docs", "google_sheets",
        "credential_save", "credential_get", "credential_list", "credential_delete",
        "webhook_create", "webhook_list", "webhook_delete", "webhook_test",
        "x_check_action", "x_log_action", "x_action_history", "x_action_stats",
        "pipeline_create", "pipeline_run", "pipeline_status",
        "pipeline_list", "pipeline_cancel",
        "text_to_image_local", "image_to_image_local", "remove_background",
        "upscale_image", "text_to_video", "image_to_video",
        "bark_speak", "generate_music", "generate_sound_effect",
        "transcribe_audio", "florence_analyze", "ocr_extract",
        "image_to_3d_model", "estimate_depth", "style_transfer",
        "gpu_status", "gpu_unload_model", "nodes_list",
        "media_list", "media_delete", "media_cleanup",
    }

    def scan(self, text: str, frontmatter: Optional[Dict] = None) -> Dict[str, Any]:
        """Run full security scan on skill content. Returns findings and verdict."""
        findings: List[SecurityFinding] = []
        body_lines = text.split("\n")

        self._scan_patterns(body_lines, findings)
        if frontmatter:
            self._scan_frontmatter(frontmatter, findings)

        score = self._calculate_risk_score(findings)
        verdict = self._verdict(score, findings)

        return {
            "verdict": verdict,
            "risk_score": score,
            "findings": [f.to_dict() for f in findings],
            "finding_counts": {
                "critical": sum(1 for f in findings if f.severity == "critical"),
                "high": sum(1 for f in findings if f.severity == "high"),
                "medium": sum(1 for f in findings if f.severity == "medium"),
                "low": sum(1 for f in findings if f.severity == "low"),
            },
            "blocked": verdict == "blocked",
        }

    def _scan_patterns(self, lines: List[str], findings: List[SecurityFinding]):
        full_text = "\n".join(lines)
        full_lower = full_text.lower()

        for pattern, msg in self.PROMPT_INJECTION_PATTERNS:
            for m in re.finditer(pattern, full_lower):
                line_no = full_lower[:m.start()].count("\n") + 1
                findings.append(SecurityFinding(
                    "prompt_injection", "critical", "prompt_injection",
                    msg, m.group()[:100], line_no,
                ))

        for pattern, msg in self.EXFILTRATION_PATTERNS:
            for m in re.finditer(pattern, full_lower):
                line_no = full_lower[:m.start()].count("\n") + 1
                findings.append(SecurityFinding(
                    "exfiltration", "critical", "exfiltration",
                    msg, m.group()[:100], line_no,
                ))

        for pattern, msg in self.DESTRUCTIVE_PATTERNS:
            for m in re.finditer(pattern, full_text):
                line_no = full_text[:m.start()].count("\n") + 1
                findings.append(SecurityFinding(
                    "destructive_cmd", "critical", "destructive",
                    msg, m.group()[:100], line_no,
                ))

        for pattern, msg in self.OBFUSCATION_PATTERNS:
            for m in re.finditer(pattern, full_text):
                line_no = full_text[:m.start()].count("\n") + 1
                findings.append(SecurityFinding(
                    "obfuscation", "high", "obfuscation",
                    msg, m.group()[:100], line_no,
                ))

        for pattern, msg in self.GHOST_SELF_MODIFICATION_PATTERNS:
            for m in re.finditer(pattern, full_lower):
                line_no = full_lower[:m.start()].count("\n") + 1
                findings.append(SecurityFinding(
                    "self_modification", "critical", "destructive",
                    msg, m.group()[:100], line_no,
                ))

    def _scan_frontmatter(self, fm: Dict, findings: List[SecurityFinding]):
        priority = fm.get("priority", 0)
        if isinstance(priority, (int, float)) and priority > self.MAX_SAFE_PRIORITY:
            findings.append(SecurityFinding(
                "priority_hijack", "high", "policy",
                f"Priority {priority} exceeds safe maximum ({self.MAX_SAFE_PRIORITY}). "
                "High priority skills override others and could hijack all matching queries.",
                f"priority: {priority}",
            ))

        triggers = fm.get("triggers", [])
        if isinstance(triggers, list):
            short = [t for t in triggers if isinstance(t, str) and len(t.strip()) < self.MIN_TRIGGER_LENGTH]
            if short:
                findings.append(SecurityFinding(
                    "trigger_too_short", "high", "policy",
                    f"Triggers shorter than {self.MIN_TRIGGER_LENGTH} chars will match almost everything: {short}",
                    str(short),
                ))

            if len(triggers) > self.MAX_TRIGGERS:
                findings.append(SecurityFinding(
                    "trigger_flood", "medium", "policy",
                    f"Skill declares {len(triggers)} triggers (max recommended: {self.MAX_TRIGGERS}). "
                    "Excessive triggers increase the chance of unintended activation.",
                    f"{len(triggers)} triggers",
                ))

        tools = fm.get("tools", [])
        if isinstance(tools, list):
            unknown = [t for t in tools if t not in self.KNOWN_TOOLS]
            if unknown:
                findings.append(SecurityFinding(
                    "unknown_tools", "medium", "policy",
                    f"Skill requests unknown tools: {unknown}. These may not exist or could be misspelled.",
                    str(unknown),
                ))

            dangerous_combos = [
                ({"shell_exec", "web_fetch"}, "Can download and execute remote content"),
                ({"shell_exec", "credential_get"}, "Can read credentials and run commands"),
                ({"file_read", "web_fetch"}, "Can read local files and send data to URLs"),
                ({"browser", "credential_get"}, "Can automate browser with stored credentials"),
            ]
            tool_set = set(tools)
            for combo, reason in dangerous_combos:
                if combo.issubset(tool_set):
                    findings.append(SecurityFinding(
                        "dangerous_tool_combo", "medium", "policy",
                        f"Risky tool combination {combo}: {reason}",
                        str(combo),
                    ))

    def _calculate_risk_score(self, findings: List[SecurityFinding]) -> int:
        weights = {"critical": 40, "high": 15, "medium": 5, "low": 1}
        return min(100, sum(weights.get(f.severity, 0) for f in findings))

    def _verdict(self, score: int, findings: List[SecurityFinding]) -> str:
        critical_count = sum(1 for f in findings if f.severity == "critical")
        if critical_count > 0:
            return "blocked"
        if score >= 30:
            return "dangerous"
        if score >= 10:
            return "caution"
        return "safe"


class SkillManager:
    """Install/validate/preflight operations for skills."""

    def __init__(self, config_loader, config_saver):
        self._load_config = config_loader
        self._save_config = config_saver
        self.user_dir = SKILLS_USER_DIR
        self.user_dir.mkdir(parents=True, exist_ok=True)

    def enabled(self) -> bool:
        cfg = self._load_config() or {}
        return bool(cfg.get("enable_skill_manager", True))

    def _safe_dir(self, rel_path: str) -> Path:
        p = (self.user_dir / rel_path).resolve()
        root = self.user_dir.resolve()
        if root not in p.parents and p != root:
            raise ValueError("Path traversal denied")
        return p

    def parse_skill_text(self, text: str) -> Dict[str, Any]:
        fm_match = re.match(r'^---\s*\n(.*?)\n---\s*\n(.*)$', text, re.DOTALL)
        if not fm_match:
            return {"frontmatter": {}, "body": text}
        try:
            fm = yaml.safe_load(fm_match.group(1)) or {}
        except yaml.YAMLError:
            fm = {}
        return {"frontmatter": fm, "body": fm_match.group(2)}

    def validate_skill_text(self, text: str, run_security_scan: bool = True) -> Dict[str, Any]:
        parsed = self.parse_skill_text(text)
        fm = parsed["frontmatter"]
        body = parsed["body"]
        issues: List[ValidationIssue] = []

        if not fm:
            issues.append(ValidationIssue("frontmatter_missing", "error", "Missing or invalid YAML frontmatter", fix="Add --- YAML --- block at top"))
            return self._pack_validation(fm, issues)

        name = fm.get("name")
        if not isinstance(name, str) or not name.strip():
            issues.append(ValidationIssue("name_required", "error", "Field 'name' is required and must be a non-empty string", field="name"))

        triggers = fm.get("triggers")
        if not isinstance(triggers, list) or not triggers:
            issues.append(ValidationIssue("triggers_required", "error", "Field 'triggers' must be a non-empty list of strings", field="triggers"))
        else:
            bad = [t for t in triggers if not isinstance(t, str) or not t.strip()]
            if bad:
                issues.append(ValidationIssue("triggers_invalid", "error", "All triggers must be non-empty strings", field="triggers"))

        tools = fm.get("tools", [])
        if tools and (not isinstance(tools, list) or any(not isinstance(t, str) for t in tools)):
            issues.append(ValidationIssue("tools_invalid", "error", "Field 'tools' must be a list of strings", field="tools"))

        requires = fm.get("requires", {})
        if requires and not isinstance(requires, dict):
            issues.append(ValidationIssue("requires_invalid", "error", "Field 'requires' must be an object", field="requires"))

        priority = fm.get("priority", 0)
        if not isinstance(priority, (int, float)):
            issues.append(ValidationIssue("priority_type", "warning", "Field 'priority' should be numeric", field="priority", fix="Use an integer like 0, 5, 10"))

        risk = "low"
        if isinstance(tools, list):
            high_risk_tools = {"shell_exec", "browser", "code_run"}
            if any(t in high_risk_tools for t in tools):
                risk = "high"
            elif tools:
                risk = "medium"

        result = self._pack_validation(fm, issues, risk=risk)

        if run_security_scan:
            scanner = SkillSecurityScanner()
            scan = scanner.scan(text, frontmatter=fm)
            result["security"] = scan
            if scan["blocked"]:
                result["ok"] = False
                result["status"] = "blocked"
                log.warning("Skill blocked by security scan: %s", scan.get("findings", [])[:3])

        return result

    def _pack_validation(self, fm: Dict[str, Any], issues: List[ValidationIssue], risk: str = "low") -> Dict[str, Any]:
        top = "ok"
        if any(i.severity == "error" for i in issues):
            top = "error"
        elif any(i.severity == "warning" for i in issues):
            top = "warning"
        return {
            "ok": top != "error",
            "status": top,
            "risk": risk,
            "frontmatter": fm,
            "issues": [i.to_dict() for i in issues],
        }

    def preflight(self, text: str) -> Dict[str, Any]:
        report = self.validate_skill_text(text)
        fm = report.get("frontmatter", {}) or {}
        requires = fm.get("requires", {}) if isinstance(fm.get("requires", {}), dict) else {}

        bins = requires.get("bins", []) if isinstance(requires.get("bins", []), list) else []
        env = requires.get("env", []) if isinstance(requires.get("env", []), list) else []
        flags = requires.get("config_flags", []) if isinstance(requires.get("config_flags", []), list) else []

        missing_bins = [b for b in bins if not shutil.which(str(b))]
        missing_env = [e for e in env if not os.environ.get(str(e))]

        cfg = self._load_config() or {}
        missing_flags = [f for f in flags if not cfg.get(str(f), False)]

        eligible = report.get("ok", False) and not missing_bins and not missing_env and not missing_flags
        report["preflight"] = {
            "eligible": eligible,
            "requires": {
                "bins": bins,
                "env": env,
                "config_flags": flags,
            },
            "missing": {
                "bins": missing_bins,
                "env": missing_env,
                "config_flags": missing_flags,
            },
        }
        return report

    def install_local(self, relative_name: str, content: str, overwrite: bool = False) -> Dict[str, Any]:
        if not isinstance(relative_name, str) or not relative_name.strip():
            raise ValueError("relative_name is required")
        safe_name = relative_name.strip().strip("/\\")
        if ".." in safe_name:
            raise ValueError("Invalid relative_name")

        target_dir = self._safe_dir(safe_name)
        target_dir.mkdir(parents=True, exist_ok=True)
        target_file = target_dir / "SKILL.md"

        if target_file.exists() and not overwrite:
            raise ValueError("Skill already exists; set overwrite=true")

        report = self.preflight(content)
        if not report.get("ok"):
            return {"ok": False, "installed": False, "report": report}

        target_file.write_text(content, encoding="utf-8")
        return {
            "ok": True,
            "installed": True,
            "path": str(target_file),
            "report": report,
        }


def _disabled_set(load_config):
    cfg = load_config() or {}
    return set(cfg.get("disabled_skills", [])), cfg


def make_skills_preflight(manager: SkillManager):
    def execute(text: str):
        if not manager.enabled():
            return {"error": "Skill manager disabled by config"}
        return manager.preflight(text or "")

    return {
        "name": "skills_preflight",
        "description": "Validate SKILL.md content and run dependency preflight checks.",
        "parameters": {
            "type": "object",
            "properties": {
                "text": {"type": "string"}
            },
            "required": ["text"]
        },
        "execute": execute,
    }


def make_skills_validate(manager: SkillManager):
    def execute(text: str):
        if not manager.enabled():
            return {"error": "Skill manager disabled by config"}
        return manager.validate_skill_text(text or "")

    return {
        "name": "skills_validate",
        "description": "Validate SKILL.md frontmatter schema and risk level.",
        "parameters": {
            "type": "object",
            "properties": {
                "text": {"type": "string"}
            },
            "required": ["text"]
        },
        "execute": execute,
    }


def make_skills_install_local(manager: SkillManager):
    def execute(relative_name: str, content: str, overwrite: bool = False):
        if not manager.enabled():
            return {"error": "Skill manager disabled by config"}
        return manager.install_local(relative_name, content, overwrite=bool(overwrite))

    return {
        "name": "skills_install_local",
        "description": "Install a local skill directory with SKILL.md after validation/preflight.",
        "parameters": {
            "type": "object",
            "properties": {
                "relative_name": {"type": "string"},
                "content": {"type": "string"},
                "overwrite": {"type": "boolean"}
            },
            "required": ["relative_name", "content"]
        },
        "execute": execute,
    }


def make_skills_enable(manager: SkillManager):
    def execute(name: str):
        if not manager.enabled():
            return {"error": "Skill manager disabled by config"}
        disabled, cfg = _disabled_set(manager._load_config)
        disabled.discard(name)
        cfg["disabled_skills"] = sorted(disabled)
        manager._save_config(cfg)
        return {"ok": True, "name": name, "enabled": True}

    return {
        "name": "skills_enable",
        "description": "Enable a disabled skill by name.",
        "parameters": {
            "type": "object",
            "properties": {"name": {"type": "string"}},
            "required": ["name"]
        },
        "execute": execute,
    }


def make_skills_disable(manager: SkillManager):
    def execute(name: str):
        if not manager.enabled():
            return {"error": "Skill manager disabled by config"}
        disabled, cfg = _disabled_set(manager._load_config)
        disabled.add(name)
        cfg["disabled_skills"] = sorted(disabled)
        manager._save_config(cfg)
        return {"ok": True, "name": name, "enabled": False}

    return {
        "name": "skills_disable",
        "description": "Disable a skill by name.",
        "parameters": {
            "type": "object",
            "properties": {"name": {"type": "string"}},
            "required": ["name"]
        },
        "execute": execute,
    }


def build_skill_manager_tools(config_loader, config_saver):
    manager = SkillManager(config_loader, config_saver)
    return [
        make_skills_preflight(manager),
        make_skills_validate(manager),
        make_skills_install_local(manager),
        make_skills_enable(manager),
        make_skills_disable(manager),
    ]
