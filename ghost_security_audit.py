"""
Ghost Security Audit — Comprehensive self-auditing system.

Adapted for Ghost's
Self-Evolution and Autonomy style. Runs as a tool + autonomy growth routine.

Audit categories:
  - Config hygiene (hardcoded secrets, permissive settings)
  - Filesystem permissions (~/.ghost/ world-readable checks)
  - API key exposure (keys in config, logs, or environment)
  - Tool policy (dangerous tool configurations)
  - Dependency health (outdated packages)
  - Network exposure (dashboard bind address)
"""

import json
import logging
import os
import re
import stat
import sys

import ghost_platform
import subprocess
from datetime import datetime
from pathlib import Path

log = logging.getLogger("quinely.security_audit")

GHOST_HOME = Path.home() / ".ghost"
PROJECT_DIR = Path(__file__).resolve().parent

SEVERITY_CRITICAL = "critical"
SEVERITY_WARNING = "warning"
SEVERITY_INFO = "info"

SECRET_PATTERNS = [
    (r"sk-[a-zA-Z0-9]{20,}", "OpenRouter/OpenAI API key"),
    (r"sk-or-v1-[a-zA-Z0-9]{40,}", "OpenRouter API key"),
    (r"sk-ant-[a-zA-Z0-9-]{40,}", "Anthropic API key"),
    (r"AIza[a-zA-Z0-9_-]{35}", "Google API key"),
    (r"xai-[a-zA-Z0-9]{40,}", "xAI/Grok API key"),
    (r"xi-[a-zA-Z0-9]{32,}", "ElevenLabs API key"),
]

DANGEROUS_SHELL_PATTERNS = [
    "rm -rf /", "rm -rf ~", "mkfs", "dd if=", ":(){",
    "sudo rm", "chmod -R 777 /", "> /dev/sd",
]

DANGEROUS_INTERPRETERS = {"python", "python3", "pip", "pip3"}
AUTONOMY_CRITICAL_COMMANDS = {"python", "python3", "pip", "pip3", "node", "npm", "git", "curl", "jq"}


def assess_command_hardening_impact(current_allowed: list[str], proposed_removed: list[str] | set[str]) -> dict:
    """Assess capability impact before tightening shell allowlist.

    Returns a compact evidence object suitable for feature briefs and logs.
    """
    allowed_set = set((current_allowed or []))
    removed_set = set((proposed_removed or []))
    removed_present = sorted(cmd for cmd in removed_set if cmd in allowed_set)
    critical_removed = sorted(cmd for cmd in removed_present if cmd in AUTONOMY_CRITICAL_COMMANDS)
    capability_areas = []
    if any(cmd in critical_removed for cmd in ("python", "python3", "pip", "pip3")):
        capability_areas.extend(["autonomy", "self_repair", "evolution", "setup_doctor"])
    if any(cmd in critical_removed for cmd in ("git", "curl", "jq", "node", "npm")):
        capability_areas.extend(["integrations", "diagnostics", "automation"])

    return {
        "blocked": bool(critical_removed),
        "removed_present": removed_present,
        "critical_removed": critical_removed,
        "capability_areas": sorted(set(capability_areas)),
        "required_mitigations": [
            "guarded_policy_gate",
            "elevated_confirmation",
            "audit_logging",
            "validated_regression_pass",
        ] if critical_removed else [],
    }


class Finding:
    """A single security audit finding."""
    __slots__ = ("category", "severity", "title", "detail", "remediation")

    def __init__(self, category, severity, title, detail="", remediation=""):
        self.category = category
        self.severity = severity
        self.title = title
        self.detail = detail
        self.remediation = remediation

    def to_dict(self):
        return {
            "category": self.category,
            "severity": self.severity,
            "title": self.title,
            "detail": self.detail,
            "remediation": self.remediation,
        }


def _check_config_hygiene(cfg: dict) -> list[Finding]:
    """Check configuration for security issues."""
    findings = []

    config_file = GHOST_HOME / "config.json"
    if config_file.exists():
        try:
            raw = config_file.read_text(encoding="utf-8")
            for pattern, desc in SECRET_PATTERNS:
                if re.search(pattern, raw):
                    findings.append(Finding(
                        "config", SEVERITY_CRITICAL,
                        f"Hardcoded secret in config.json",
                        f"Found what appears to be a {desc} in config.json",
                        "Move API keys to environment variables or ~/.ghost/auth_profiles.json",
                    ))
        except Exception:
            pass

    if not cfg.get("strict_tool_registration", False):
        findings.append(Finding(
            "config", SEVERITY_WARNING,
            "Tool registration not in strict mode",
            "strict_tool_registration is False — tools can be shadowed by plugins",
            "Set strict_tool_registration: true in config.json",
        ))

    if not cfg.get("diagnostic_redaction_enabled", True):
        findings.append(Finding(
            "config", SEVERITY_WARNING,
            "Diagnostic redaction disabled",
            "diagnostic_redaction_enabled=false can persist sensitive process/network metadata",
            "Set diagnostic_redaction_enabled: true to persist only summarized redacted diagnostics",
        ))
    else:
        findings.append(Finding(
            "config", SEVERITY_INFO,
            "Diagnostic redaction enabled",
            "High-sensitivity diagnostics are configured for redacted persistence",
            "Keep diagnostic_redaction_enabled=true",
        ))

    # Check channel DM policy security
    if cfg.get("channel_inbound_enabled", False):
        dm_policy = cfg.get("channel_dm_policy", "open")
        allowed_senders = cfg.get("channel_allowed_senders", [])
        if dm_policy == "open" and not allowed_senders:
            findings.append(Finding(
                "config", SEVERITY_WARNING,
                "Open DM channel policy without allowlist",
                "channel_inbound_enabled=true with channel_dm_policy='open' and empty channel_allowed_senders. "
                "Any sender can trigger actions via inbound messages.",
                "Set channel_dm_policy to 'allowlist' and add trusted senders to channel_allowed_senders, "
                "or use 'block' to disable inbound DMs entirely.",
            ))

    # evolve_auto_approve is a user autonomy preference, not a security issue

    allowed_cmds = cfg.get("allowed_commands", [])
    dangerous_cmds = {"sudo", "su", "chroot", "mount", "umount"}
    found_dangerous = set(allowed_cmds) & dangerous_cmds
    if found_dangerous:
        findings.append(Finding(
            "config", SEVERITY_CRITICAL,
            "Dangerous commands in allowlist",
            f"Commands {found_dangerous} are in allowed_commands",
            "Remove dangerous commands from allowed_commands",
        ))

    found_interpreters = set(allowed_cmds) & DANGEROUS_INTERPRETERS

    # Check interpreter security posture
    policy = cfg.get("dangerous_command_policy") or {}
    py_policy = policy.get("python") or {}
    pip_policy = policy.get("pip") or {}
    blocked_commands_raw = cfg.get("blocked_commands") or []
    blocked_commands = {
        str(cmd).strip() for cmd in blocked_commands_raw if isinstance(cmd, str) and str(cmd).strip()
    }

    interpreters_enabled = bool(cfg.get("enable_dangerous_interpreters", False))
    
    if interpreters_enabled:
        # Check if policy has adequate protections
        has_metachar_protection = True  # Now enforced by _check_dangerous_command_policy
        has_workspace_requirement = py_policy.get("require_workspace", True) and pip_policy.get("require_workspace", True)
        has_deny_flags = bool(py_policy.get("deny_flags", ["-c", "-m"]))
        python_allowed = py_policy.get("allow", False)
        pip_allowed = pip_policy.get("allow", False)
        
        if python_allowed or pip_allowed:
            if not has_workspace_requirement or not has_deny_flags:
                findings.append(Finding(
                    "config", SEVERITY_WARNING,
                    "Dangerous interpreters enabled with weak policy",
                    f"enable_dangerous_interpreters=true with python.allow={python_allowed}, pip.allow={pip_allowed}. "
                    f"Policy lacks: require_workspace={has_workspace_requirement}, deny_flags={has_deny_flags}",
                    "Harden policy: set require_workspace=true, deny_flags=['-c','-m'], and use blocked_commands for additional restrictions",
                ))
            else:
                findings.append(Finding(
                    "config", SEVERITY_INFO,
                    "Dangerous interpreters enabled with guarded policy",
                    "enable_dangerous_interpreters=true but policy enforces workspace requirement and denies high-risk flags",
                    "Ensure blocked_commands is also used for defense in depth",
                ))
        else:
            findings.append(Finding(
                "config", SEVERITY_INFO,
                "Dangerous interpreters feature enabled but interpreters disallowed by policy",
                "enable_dangerous_interpreters=true but python.allow=false and pip.allow=false",
                "Feature is effectively disabled by policy - safe configuration",
            ))
    # Effective executability assessment: emit warning only when runnable.
    effectively_executable = []
    effectively_blocked = []
    for cmd in sorted(found_interpreters):
        if cmd in blocked_commands:
            effectively_blocked.append(cmd)
            continue
        base = "python" if cmd.startswith("python") else "pip"
        cmd_policy = py_policy if base == "python" else pip_policy
        cmd_allow = bool(cmd_policy.get("allow", False))
        if interpreters_enabled and cmd_allow:
            effectively_executable.append(cmd)
        else:
            effectively_blocked.append(cmd)

    if effectively_executable:
        findings.append(Finding(
            "config", SEVERITY_WARNING,
            "Dangerous interpreters effectively executable",
            f"Commands {effectively_executable} are in allowed_commands and currently executable under dangerous interpreter policy",
            "Prefer policy controls first: keep require_workspace + deny_flags strict, use blocked_commands for targeted denials, and keep audit logging. Avoid blanket allowlist removals unless capability impact is validated.",
        ))

    if effectively_blocked:
        findings.append(Finding(
            "config", SEVERITY_INFO,
            "Dangerous interpreters listed but effectively blocked",
            f"Commands {effectively_blocked} are in allowed_commands but blocked by global gate, per-command allow=false, and/or blocked_commands",
            "Current state provides defense-in-depth. Keep policy gates and blocked_commands as primary controls; allowlist removal is optional and may reduce autonomy/self-repair diagnostics.",
        ))

    return findings


def _check_filesystem(cfg: dict) -> list[Finding]:
    """Check filesystem permissions for security issues."""
    findings = []

    sensitive_files = [
        GHOST_HOME / "config.json",
        GHOST_HOME / "auth_profiles.json",
        GHOST_HOME / "credentials.json",
    ]

    # Unix permission bit checks are meaningless on Windows (NTFS uses ACLs)
    if not ghost_platform.IS_WIN:
        for fpath in sensitive_files:
            if not fpath.exists():
                continue
            try:
                st = fpath.stat()
                mode = st.st_mode
                if mode & stat.S_IROTH:
                    findings.append(Finding(
                        "filesystem", SEVERITY_CRITICAL,
                        f"World-readable sensitive file: {fpath.name}",
                        f"{fpath} is readable by all users (mode: {oct(mode)})",
                        f"Run: chmod 600 {fpath}",
                    ))
                elif mode & stat.S_IRGRP:
                    findings.append(Finding(
                        "filesystem", SEVERITY_WARNING,
                        f"Group-readable sensitive file: {fpath.name}",
                        f"{fpath} is readable by group (mode: {oct(mode)})",
                        f"Run: chmod 600 {fpath}",
                    ))
            except OSError:
                pass

        ghost_dir_stat = GHOST_HOME.stat() if GHOST_HOME.exists() else None
        if ghost_dir_stat:
            mode = ghost_dir_stat.st_mode
            if mode & stat.S_IWOTH:
                findings.append(Finding(
                    "filesystem", SEVERITY_CRITICAL,
                    "~/.ghost/ directory is world-writable",
                    f"Directory mode: {oct(mode)}",
                    "Run: chmod 700 ~/.ghost",
                ))

    return findings


def _check_api_key_exposure(cfg: dict) -> list[Finding]:
    """Check for API key exposure in logs and environment."""
    findings = []

    log_file = GHOST_HOME / "log.json"
    if log_file.exists():
        try:
            raw = log_file.read_text(encoding="utf-8")[:100_000]
            for pattern, desc in SECRET_PATTERNS:
                if re.search(pattern, raw):
                    findings.append(Finding(
                        "api_keys", SEVERITY_CRITICAL,
                        f"API key found in log file",
                        f"A {desc} was found in ~/.ghost/log.json",
                        "Purge the log file and ensure keys are never logged",
                    ))
                    break
        except Exception:
            pass

    debug_log = GHOST_HOME / "logs" / "tool_loop_debug.jsonl"
    if debug_log.exists():
        try:
            raw = debug_log.read_text(encoding="utf-8")[:100_000]
            for pattern, desc in SECRET_PATTERNS:
                if re.search(pattern, raw):
                    findings.append(Finding(
                        "api_keys", SEVERITY_WARNING,
                        f"API key found in debug log",
                        f"A {desc} was found in tool_loop_debug.jsonl",
                        f"Purge debug logs: delete {GHOST_HOME / 'logs' / 'tool_loop_debug.jsonl'}",
                    ))
                    break
        except Exception:
            pass

    return findings


def _check_tool_policy(cfg: dict) -> list[Finding]:
    """Check tool security policies."""
    findings = []

    allowed = set(cfg.get("allowed_commands", []))
    interpreters = {"python3", "python", "node", "ruby", "perl", "php"}
    active_interpreters = allowed & interpreters
    if active_interpreters:
        findings.append(Finding(
            "tools", SEVERITY_INFO,
            f"Script interpreters in allowlist: {active_interpreters}",
            "Interpreters can execute arbitrary code",
            "Consider removing interpreters if not needed for autonomy routines",
        ))

    roots = cfg.get("allowed_roots", [])
    if any(ghost_platform.is_root_path(r) for r in roots):
        findings.append(Finding(
            "tools", SEVERITY_CRITICAL,
            "Root filesystem in allowed_roots",
            "allowed_roots includes / — Ghost can read/write ANY file",
            "Restrict allowed_roots to user home directory",
        ))

    return findings


def _check_network_exposure(cfg: dict) -> list[Finding]:
    """Check for network exposure issues."""
    findings = []

    dash_host = cfg.get("dashboard_host", "127.0.0.1")
    if dash_host in ("0.0.0.0", "::"):
        findings.append(Finding(
            "network", SEVERITY_WARNING,
            "Dashboard bound to all interfaces",
            f"dashboard_host is {dash_host} — accessible from the network",
            "Set dashboard_host to 127.0.0.1 for local-only access",
        ))

    return findings


def _check_dependency_health() -> list[Finding]:
    """Check for outdated or vulnerable dependencies."""
    findings = []

    req_file = PROJECT_DIR / "requirements.txt"
    if not req_file.exists():
        return findings

    try:
        result = subprocess.run(
            [sys.executable, "-m", "pip", "list", "--outdated", "--format=json"],
            capture_output=True, text=True, timeout=30,
        )
        if result.returncode == 0:
            outdated = json.loads(result.stdout)
            req_text = req_file.read_text(encoding="utf-8").lower()
            for pkg in outdated:
                pkg_name = pkg.get("name", "").lower()
                if pkg_name in req_text:
                    current = pkg.get("version", "?")
                    latest = pkg.get("latest_version", "?")
                    findings.append(Finding(
                        "dependencies", SEVERITY_INFO,
                        f"Outdated dependency: {pkg_name}",
                        f"Current: {current}, Latest: {latest}",
                        f"Update with: pip install --upgrade {pkg_name}",
                    ))
    except (json.JSONDecodeError, OSError, subprocess.SubprocessError) as exc:
        log.warning("Dependency audit check failed: %s", exc)

    return findings


def sanitize_diagnostic_text(text: str, max_chars: int = 2000) -> str:
    """Redact sensitive tokens/paths/CLI args from diagnostic output and cap length."""
    if not isinstance(text, str):
        return ""

    sanitized = text
    token_patterns = [
        r"\b(?:sk|xai|xi|ghp|gho|github_pat)-[A-Za-z0-9_\-]{8,}\b",
        r"\bAKIA[0-9A-Z]{16}\b",
        r"\bAIza[0-9A-Za-z_\-]{20,}\b",
        r"\b(?:token|apikey|api_key|password|passwd|secret|bearer)=\S+",
        r"\bpostgres(?:ql)?://[^\s@]+:[^\s@]+@",
    ]
    for pattern in token_patterns:
        sanitized = re.sub(pattern, "[REDACTED]", sanitized, flags=re.IGNORECASE)

    sensitive_cli = re.compile(
        r"(?i)(--?(?:token|api[-_]?key|password|passwd|secret|bearer|authorization|access[-_]?token|refresh[-_]?token|dsn)\s*[=\s]+)([^\s,;]+)"
    )
    sanitized = sensitive_cli.sub(r"\1[REDACTED]", sanitized)
    sanitized = re.sub(r"(?i)(authorization:\s*bearer\s+)[^\s]+", r"\1[REDACTED]", sanitized)
    sanitized = re.sub(r"(?i)([?&](?:token|api[_-]?key|access[_-]?token|password|passwd|secret)=)[^&#\s]+", r"\1[REDACTED]", sanitized)

    sanitized = re.sub(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}", "[REDACTED_EMAIL]", sanitized)
    sanitized = re.sub(r"/(Users|home)/[^\s:/]+", r"/\1/[REDACTED_USER]", sanitized)
    sanitized = re.sub(r"\s-[A-Za-z][A-Za-z0-9_-]{1,30}\s+\S{20,}", " [REDACTED_ARG] ", sanitized)

    if len(sanitized) > max_chars:
        return sanitized[:max_chars] + "\n...[TRUNCATED]"
    return sanitized


def summarize_diagnostic_output(text: str, source: str = "") -> dict:
    """Return safe aggregate diagnostics instead of raw process/network blobs."""
    sanitized = sanitize_diagnostic_text(text or "", max_chars=4000)
    lines = [ln.strip() for ln in sanitized.splitlines() if ln.strip()]
    source_lower = (source or "").lower()

    summary = {
        "source": source or "unknown",
        "line_count": len(lines),
        "sample": lines[:8],
        "diagnostic_redaction_enabled": True,
        "redaction_applied": sanitized != (text or ""),
        "audit_note": "Sensitive arguments/tokens were redacted from diagnostics where detected.",
    }

    if "ps" in source_lower:
        proc_names = {}
        for ln in lines[:300]:
            parts = ln.split()
            if len(parts) >= 11:
                pname = Path(parts[10]).name[:48]
                proc_names[pname] = proc_names.get(pname, 0) + 1
        summary["top_processes"] = sorted(proc_names.items(), key=lambda kv: kv[1], reverse=True)[:10]

    if "lsof" in source_lower:
        ports = []
        for ln in lines[:300]:
            m = re.search(r":(\d+)(?:\s|$)", ln)
            if m:
                ports.append(int(m.group(1)))
        summary["listening_ports"] = sorted(set(ports))[:30]

    return summary


def run_security_audit(cfg: dict = None, categories: list = None) -> dict:
    """Run a full or targeted security audit.

    Args:
        cfg: Ghost config dict (loaded from config.json if None)
        categories: List of categories to audit. None = all.
                    Options: config, filesystem, api_keys, tools, network, dependencies

    Returns:
        Audit report dict with summary and findings.
    """
    if cfg is None:
        config_file = GHOST_HOME / "config.json"
        if config_file.exists():
            try:
                cfg = json.loads(config_file.read_text(encoding="utf-8"))
            except Exception:
                cfg = {}
        else:
            cfg = {}

    all_categories = {
        "config": _check_config_hygiene,
        "filesystem": _check_filesystem,
        "api_keys": _check_api_key_exposure,
        "tools": _check_tool_policy,
        "network": _check_network_exposure,
        "dependencies": _check_dependency_health,
    }

    cats = categories or list(all_categories.keys())
    findings = []

    for cat in cats:
        checker = all_categories.get(cat)
        if checker:
            try:
                if cat == "dependencies":
                    findings.extend(checker())
                else:
                    findings.extend(checker(cfg))
            except Exception as e:
                findings.append(Finding(
                    cat, SEVERITY_INFO,
                    f"Audit check failed: {cat}",
                    str(e),
                ))

    critical = sum(1 for f in findings if f.severity == SEVERITY_CRITICAL)
    warnings = sum(1 for f in findings if f.severity == SEVERITY_WARNING)
    info = sum(1 for f in findings if f.severity == SEVERITY_INFO)

    return {
        "timestamp": datetime.now().isoformat(),
        "categories_checked": cats,
        "summary": {
            "total": len(findings),
            "critical": critical,
            "warning": warnings,
            "info": info,
        },
        "findings": [f.to_dict() for f in findings],
        "status": "critical" if critical > 0 else "warning" if warnings > 0 else "clean",
    }


def auto_fix(findings: list) -> list[dict]:
    """Auto-remediate simple security issues. Returns list of actions taken."""
    actions = []

    for finding in findings:
        if finding.get("category") == "filesystem" and "World-readable" in finding.get("title", ""):
            fname = finding.get("title", "").split(": ")[-1]
            fpath = GHOST_HOME / fname
            if fpath.exists():
                try:
                    previous_mode = oct(stat.S_IMODE(fpath.stat().st_mode))
                    ghost_platform.chmod_safe(fpath, 0o600)
                    current_mode = oct(stat.S_IMODE(fpath.stat().st_mode))
                    actions.append({
                        "action": "chmod",
                        "file": str(fpath),
                        "previous_mode": previous_mode,
                        "current_mode": current_mode,
                        "fixed_at": datetime.now().isoformat(),
                        "result": "Fixed: set to mode 600",
                    })
                except OSError as e:
                    actions.append({
                        "action": "chmod",
                        "file": str(fpath),
                        "fixed_at": datetime.now().isoformat(),
                        "result": f"Failed: {e}",
                    })

        if finding.get("category") == "filesystem" and "world-writable" in finding.get("title", ""):
            try:
                previous_mode = oct(stat.S_IMODE(GHOST_HOME.stat().st_mode))
                ghost_platform.chmod_safe(GHOST_HOME, 0o700)
                current_mode = oct(stat.S_IMODE(GHOST_HOME.stat().st_mode))
                actions.append({
                    "action": "chmod",
                    "file": str(GHOST_HOME),
                    "previous_mode": previous_mode,
                    "current_mode": current_mode,
                    "fixed_at": datetime.now().isoformat(),
                    "result": "Fixed: set to mode 700",
                })
            except OSError as e:
                actions.append({
                    "action": "chmod",
                    "file": str(GHOST_HOME),
                    "fixed_at": datetime.now().isoformat(),
                    "result": f"Failed: {e}",
                })

    return actions


def build_security_audit_tools(cfg=None):
    """Build LLM-callable security audit tools for the tool registry."""

    def audit_exec(categories=None):
        report = run_security_audit(cfg=cfg, categories=categories)
        return json.dumps(report, indent=2)

    def fix_exec():
        report = run_security_audit(cfg=cfg)
        fixable = [f for f in report["findings"]
                   if f["severity"] in ("critical", "warning")
                   and f["category"] == "filesystem"]
        if not fixable:
            return "No auto-fixable issues found."
        actions = auto_fix(fixable)
        return json.dumps({
            "actions_taken": actions,
            "remaining_findings": report["summary"],
        }, indent=2)

    return [
        {
            "name": "security_audit",
            "description": (
                "Run a comprehensive security audit of Ghost's configuration, "
                "filesystem permissions, API key exposure, tool policies, network "
                "exposure, and dependency health. Returns findings with severity "
                "levels and remediation steps."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "categories": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": (
                            "Specific categories to audit. Options: "
                            "config, filesystem, api_keys, tools, network, dependencies. "
                            "Leave empty for all."
                        ),
                    },
                },
            },
            "execute": audit_exec,
        },
        {
            "name": "security_fix",
            "description": (
                "Auto-remediate simple security issues (file permissions, "
                "directory permissions). Runs audit first, then fixes what it can."
            ),
            "parameters": {
                "type": "object",
                "properties": {},
            },
            "execute": fix_exec,
        },
    ]
