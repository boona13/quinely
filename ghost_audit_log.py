"""
GHOST Audit Logging System

Tracks sensitive operations across Ghost for security and compliance.
Stores audit entries in ~/.ghost/audit_log.jsonl (JSON Lines format).

Logged operations:
- Config changes (API keys, dangerous settings)
- Credential operations (save, get, list)
- Auth profile operations (set, remove API keys and OAuth tokens)
"""

import json
import logging
import os
import threading
import time
from datetime import datetime, timezone
from enum import Enum
from pathlib import Path
from typing import Any, Optional

log = logging.getLogger("quinely.audit")

GHOST_HOME = Path.home() / ".ghost"
AUDIT_LOG_FILE = GHOST_HOME / "audit_log.jsonl"
MAX_LOG_ENTRIES = 10000

# Thread-local storage for request context (actor info)
_request_context = threading.local()


class AuditAction(str, Enum):
    """Types of auditable actions."""
    # Config operations
    CONFIG_UPDATE = "config.update"
    CONFIG_DANGEROUS_ENABLE = "config.dangerous_enable"
    CLOUD_PROVIDER_UPDATE = "cloud_provider.update"
    
    # Credential operations
    CREDENTIAL_SAVE = "credential.save"
    CREDENTIAL_GET = "credential.get"
    CREDENTIAL_LIST = "credential.list"
    
    # Auth profile operations
    AUTH_PROFILE_SET = "auth_profile.set"
    AUTH_PROFILE_REMOVE = "auth_profile.remove"
    AUTH_PROFILE_SET_API_KEY = "auth_profile.set_api_key"
    AUTH_PROFILE_SET_OAUTH = "auth_profile.set_oauth"
    
    # Evolve operations
    EVOLVE_PLAN = "evolve.plan"
    EVOLVE_APPLY = "evolve.apply"
    EVOLVE_DEPLOY = "evolve.deploy"
    EVOLVE_ROLLBACK = "evolve.rollback"


class AuditLog:
    """Thread-safe audit logging with JSONL storage."""
    
    def __init__(self, log_file: Path = None):
        self._log_file = log_file or AUDIT_LOG_FILE
        self._lock = threading.Lock()
        self._ensure_dir()
    
    def _ensure_dir(self):
        """Ensure the log directory exists."""
        self._log_file.parent.mkdir(parents=True, exist_ok=True)
    
    def _get_timestamp(self) -> str:
        """Get ISO format timestamp with timezone."""
        return datetime.now(timezone.utc).isoformat(timespec="seconds")
    
    def _get_actor(self) -> dict:
        """Get current actor info from request context."""
        return {
            "type": getattr(_request_context, "actor_type", "system"),
            "id": getattr(_request_context, "actor_id", None),
            "ip": getattr(_request_context, "actor_ip", None),
        }
    
    def log(
        self,
        action: AuditAction,
        resource_type: str,
        resource_id: str,
        success: bool = True,
        details: Optional[dict] = None,
        error: Optional[str] = None,
    ) -> dict:
        """
        Log an audit event.
        
        Args:
            action: The type of action performed
            resource_type: Category of resource (config, credential, extension, auth_profile)
            resource_id: Identifier for the specific resource
            success: Whether the operation succeeded
            details: Additional sanitized details (no secrets!)
            error: Error message if operation failed
        
        Returns:
            The audit entry that was logged
        """
        entry = {
            "timestamp": self._get_timestamp(),
            "action": action.value,
            "resource_type": resource_type,
            "resource_id": str(resource_id) if resource_id else None,
            "actor": self._get_actor(),
            "success": success,
            "details": self._sanitize_details(details or {}),
        }
        
        if error:
            entry["error"] = str(error)[:500]  # Limit error length
        
        with self._lock:
            self._append_entry(entry)
        
        # Also log to Python logger at debug level
        log.debug("Audit: %s %s/%s success=%s", action.value, resource_type, resource_id, success)
        
        return entry
    
    def _sanitize_details(self, details: dict) -> dict:
        """Remove any potentially sensitive fields from details."""
        sensitive_keys = {"password", "api_key", "secret_key", "access_token", 
                         "refresh_token", "token", "key", "secret", "credential"}
        sanitized = {}
        for key, value in details.items():
            if any(s in key.lower() for s in sensitive_keys):
                sanitized[key] = "***REDACTED***"
            elif isinstance(value, dict):
                sanitized[key] = self._sanitize_details(value)
            elif isinstance(value, list):
                sanitized[key] = [
                    self._sanitize_details(item) if isinstance(item, dict) else item
                    for item in value
                ]
            else:
                sanitized[key] = value
        return sanitized
    
    def _append_entry(self, entry: dict):
        """Append entry to log file with rotation - uses true append-only JSONL."""
        try:
            # Ensure directory exists
            self._log_file.parent.mkdir(parents=True, exist_ok=True)
            
            # Append-only write (O(1) operation, not O(n))
            with open(self._log_file, "a", encoding="utf-8") as f:
                f.write(json.dumps(entry, default=str) + "\n")
            
            # Check if rotation needed (every 1000 writes, check file size)
            # This is much cheaper than reading entire file on every write
            self._maybe_rotate()
            
        except Exception as e:
            log.error("Failed to write audit log entry: %s", e)
    
    def _maybe_rotate(self):
        """Rotate log file if it exceeds size limit. Called periodically."""
        try:
            if not self._log_file.exists():
                return
            
            # Check size (10MB limit)
            size = self._log_file.stat().st_size
            if size < 10 * 1024 * 1024:  # 10MB
                return
            
            # Perform rotation
            self._rotate_log()
        except Exception as e:
            log.warning("Audit log rotation check failed: %s", e)
    
    def _rotate_log(self):
        """Rotate log files, keeping 5 archives."""
        try:
            # Remove oldest archive if exists
            oldest = self._log_file.parent / "audit_log.5.jsonl"
            if oldest.exists():
                oldest.unlink()
            
            # Shift archives
            for i in range(4, 0, -1):
                src = self._log_file.parent / f"audit_log.{i}.jsonl"
                dst = self._log_file.parent / f"audit_log.{i+1}.jsonl"
                if src.exists():
                    src.rename(dst)
            
            # Move current to .1
            self._log_file.rename(self._log_file.parent / "audit_log.1.jsonl")
            
            log.info("Audit log rotated")
        except Exception as e:
            log.error("Audit log rotation failed: %s", e)
    
    def query(
        self,
        action: Optional[str] = None,
        resource_type: Optional[str] = None,
        resource_id: Optional[str] = None,
        success: Optional[bool] = None,
        since: Optional[str] = None,
        until: Optional[str] = None,
        limit: int = 100,
        offset: int = 0,
    ) -> list[dict]:
        """
        Query audit log entries with filters.
        
        Args:
            action: Filter by action type
            resource_type: Filter by resource type
            resource_id: Filter by resource ID
            success: Filter by success status
            since: ISO timestamp to start from
            until: ISO timestamp to end at
            limit: Maximum entries to return
            offset: Number of entries to skip
        
        Returns:
            List of matching audit entries (newest first)
        """
        entries = []
        
        if not self._log_file.exists():
            return entries
        
        try:
            with open(self._log_file, "r", encoding="utf-8") as f:
                for line in f:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        entry = json.loads(line)
                    except json.JSONDecodeError:
                        continue
                    
                    # Apply filters
                    if action and entry.get("action") != action:
                        continue
                    if resource_type and entry.get("resource_type") != resource_type:
                        continue
                    if resource_id and entry.get("resource_id") != resource_id:
                        continue
                    if success is not None and entry.get("success") != success:
                        continue
                    if since and entry.get("timestamp", "") < since:
                        continue
                    if until and entry.get("timestamp", "") > until:
                        continue
                    
                    entries.append(entry)
        except OSError as e:
            log.warning("Failed to read audit log: %s", e)
            return []
        
        # Sort by timestamp descending (newest first)
        entries.sort(key=lambda x: x.get("timestamp", ""), reverse=True)
        
        # Apply pagination
        return entries[offset:offset + limit]
    
    def get_stats(self) -> dict:
        """Get summary statistics of the audit log."""
        if not self._log_file.exists():
            return {
                "total_entries": 0,
                "oldest": None,
                "newest": None,
                "today_entries": 0,
                "successful_entries": 0,
                "failed_entries": 0,
            }
        
        total = 0
        oldest = None
        newest = None
        today_entries = 0
        successful_entries = 0
        failed_entries = 0
        
        # Get today's date for comparison
        today = datetime.now(timezone.utc).date().isoformat()
        
        try:
            with open(self._log_file, "r", encoding="utf-8") as f:
                for line in f:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        entry = json.loads(line)
                        total += 1
                        ts = entry.get("timestamp")
                        if ts:
                            if oldest is None or ts < oldest:
                                oldest = ts
                            if newest is None or ts > newest:
                                newest = ts
                            # Check if entry is from today
                            if ts.startswith(today):
                                today_entries += 1
                        # Count success/failure
                        if entry.get("success") is True:
                            successful_entries += 1
                        elif entry.get("success") is False:
                            failed_entries += 1
                    except json.JSONDecodeError:
                        continue
        except OSError as e:
            log.warning("Failed to read audit log for stats: %s", e)
        
        return {
            "total_entries": total,
            "oldest": oldest,
            "newest": newest,
            "today_entries": today_entries,
            "successful_entries": successful_entries,
            "failed_entries": failed_entries,
        }


# Global audit log instance
_audit_log: Optional[AuditLog] = None
_audit_log_lock = threading.Lock()


def get_audit_log() -> AuditLog:
    """Get or create the global audit log instance."""
    global _audit_log
    if _audit_log is None:
        with _audit_log_lock:
            if _audit_log is None:
                _audit_log = AuditLog()
    return _audit_log


def set_request_context(actor_type: str = "system", actor_id: str = None, actor_ip: str = None):
    """
    Set the actor context for the current request/thread.
    Call this at the start of request handling.
    """
    _request_context.actor_type = actor_type
    _request_context.actor_id = actor_id
    _request_context.actor_ip = actor_ip


def clear_request_context():
    """Clear the request context. Call at the end of request handling."""
    _request_context.actor_type = "system"
    _request_context.actor_id = None
    _request_context.actor_ip = None


# Convenience functions for common audit patterns

def audit_config_update(changed_keys: list, success: bool = True, error: str = None):
    """Log a config update."""
    return get_audit_log().log(
        action=AuditAction.CONFIG_UPDATE,
        resource_type="config",
        resource_id="global",
        success=success,
        details={"changed_keys": changed_keys},
        error=error,
    )


def audit_dangerous_interpreters_enabled(success: bool = True):
    """Log dangerous interpreters being enabled."""
    return get_audit_log().log(
        action=AuditAction.CONFIG_DANGEROUS_ENABLE,
        resource_type="config",
        resource_id="enable_dangerous_interpreters",
        success=success,
        details={"severity": "high", "confirmation_required": True},
    )


def audit_credential_save(service: str, success: bool = True, error: str = None):
    """Log credential save operation."""
    return get_audit_log().log(
        action=AuditAction.CREDENTIAL_SAVE,
        resource_type="credential",
        resource_id=service,
        success=success,
        error=error,
    )


def audit_auth_profile_set(provider: str, profile_type: str, success: bool = True):
    """Log auth profile set."""
    return get_audit_log().log(
        action=AuditAction.AUTH_PROFILE_SET,
        resource_type="auth_profile",
        resource_id=f"{provider}:{profile_type}",
        success=success,
        details={"provider": provider, "profile_type": profile_type},
    )


def audit_auth_profile_remove(profile_id: str, success: bool = True):
    """Log auth profile removal."""
    return get_audit_log().log(
        action=AuditAction.AUTH_PROFILE_REMOVE,
        resource_type="auth_profile",
        resource_id=profile_id,
        success=success,
    )


def audit_evolve_plan(evolution_id: str, description: str = None, files: list = None, success: bool = True, error: str = None):
    """Log evolution plan creation."""
    return get_audit_log().log(
        action=AuditAction.EVOLVE_PLAN,
        resource_type="evolution",
        resource_id=evolution_id,
        success=success,
        details={
            "description": description[:100] if description else None,
            "files_count": len(files) if files else 0,
        },
        error=error,
    )


def audit_evolve_apply(evolution_id: str, file_path: str, success: bool = True, error: str = None):
    """Log evolution apply (code change)."""
    return get_audit_log().log(
        action=AuditAction.EVOLVE_APPLY,
        resource_type="evolution",
        resource_id=evolution_id,
        success=success,
        details={"file_path": file_path},
        error=error,
    )


def audit_evolve_deploy(evolution_id: str, success: bool = True, error: str = None):
    """Log evolution deploy."""
    return get_audit_log().log(
        action=AuditAction.EVOLVE_DEPLOY,
        resource_type="evolution",
        resource_id=evolution_id,
        success=success,
        error=error,
    )


def audit_evolve_rollback(evolution_id: str, backup_path: str = None, success: bool = True, error: str = None):
    """Log evolution rollback."""
    return get_audit_log().log(
        action=AuditAction.EVOLVE_ROLLBACK,
        resource_type="evolution",
        resource_id=evolution_id,
        success=success,
        details={"backup_path": backup_path},
        error=error,
    )
