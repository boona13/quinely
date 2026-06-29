"""
GHOST Credential Management

Structured storage for service credentials (email accounts, social media, etc.).
Credentials are persisted in ~/.ghost/credentials.json as a JSON array.
"""

import json
import logging
import os
from pathlib import Path
from datetime import datetime
from typing import Optional, List, Dict, Any

import ghost_secret_store as secret_store

GHOST_HOME = Path.home() / ".ghost"
CREDENTIALS_FILE = GHOST_HOME / "credentials.json"


def _load_credentials() -> List[Dict[str, Any]]:
    if CREDENTIALS_FILE.exists():
        try:
            creds = json.loads(CREDENTIALS_FILE.read_text(encoding="utf-8"))
            # Decrypt the password field back to plaintext for in-memory use.
            for c in creds:
                if isinstance(c, dict) and "password" in c:
                    c["password"] = secret_store.decrypt(c["password"])
            return creds
        except (json.JSONDecodeError, OSError):
            return []
    return []


def _save_credentials(creds: List[Dict[str, Any]]):
    GHOST_HOME.mkdir(parents=True, exist_ok=True)
    # Encrypt the password field at rest without mutating the caller's list.
    enc = []
    for c in creds:
        if isinstance(c, dict) and c.get("password"):
            c = {**c, "password": secret_store.encrypt(c["password"])}
        enc.append(c)
    CREDENTIALS_FILE.write_text(json.dumps(enc, indent=2), encoding="utf-8")
    try:
        os.chmod(CREDENTIALS_FILE, 0o600)
    except Exception:
        pass


def _migrate_credentials():
    """One-time: re-encrypt any legacy plaintext passwords on disk."""
    if not CREDENTIALS_FILE.exists() or not secret_store.is_available():
        return
    try:
        raw = json.loads(CREDENTIALS_FILE.read_text(encoding="utf-8"))
        if any(isinstance(c, dict) and c.get("password")
               and not secret_store.is_encrypted(c["password"]) for c in raw):
            _save_credentials(_load_credentials())  # load decrypts, save encrypts
            logging.getLogger("quinely.credentials").info(
                "Migrated credentials to encrypted-at-rest")
    except Exception as e:
        logging.getLogger("quinely.credentials").warning(
            "Credential encryption migration failed: %s", e)


def build_credential_tools() -> list:
    """Build credential management tools for the ghost tool registry."""
    _migrate_credentials()

    def credential_save(
        service: str,
        username: str,
        password: str,
        email: str = "",
        notes: str = "",
        metadata: Optional[dict] = None,
    ) -> str:
        from ghost_audit_log import get_audit_log, AuditAction
        creds = _load_credentials()
        entry = {
            "service": service.strip().lower(),
            "username": username.strip(),
            "email": email.strip(),
            "password": password,
            "created_at": datetime.now().isoformat(timespec="seconds"),
            "notes": notes,
            "metadata": metadata or {},
        }
        creds.append(entry)
        _save_credentials(creds)
        # Audit log
        try:
            audit = get_audit_log()
            audit.log(
                action=AuditAction.CREDENTIAL_SAVE,
                resource_type="credential",
                resource_id=entry["service"],
                success=True,
                details={"username": username, "email": email},
            )
        except Exception as e:
            logging.getLogger("quinely.audit").warning("Audit log failed: %s", e)
        display_email = entry["email"] or entry["username"]
        return f"OK: credentials saved for {entry['service']} ({display_email})"

    def credential_get(service: str, show_password: bool = False) -> str:
        creds = _load_credentials()
        service_lower = service.strip().lower()
        matches = [
            c for c in creds
            if service_lower in c.get("service", "")
            or service_lower in c.get("email", "")
            or service_lower in c.get("username", "")
        ]
        if not matches:
            return f"No credentials found matching '{service}'."
        parts = []
        for c in matches:
            pwd = c["password"] if show_password else "********"
            line = (
                f"Service: {c['service']}\n"
                f"  Username: {c['username']}\n"
                f"  Email:    {c.get('email', 'N/A')}\n"
                f"  Password: {pwd}\n"
                f"  Created:  {c.get('created_at', 'unknown')}"
            )
            if c.get("notes"):
                line += f"\n  Notes:    {c['notes']}"
            parts.append(line)
        return "\n---\n".join(parts)

    def credential_list(show_password: bool = False) -> str:
        creds = _load_credentials()
        if not creds:
            return "No credentials stored yet."
        parts = []
        for i, c in enumerate(creds, 1):
            pwd = c["password"] if show_password else "********"
            parts.append(
                f"{i}. {c['service']} — {c.get('email') or c['username']} "
                f"(pwd: {pwd}, created: {c.get('created_at', '?')})"
            )
        return "\n".join(parts)

    return [
        {
            "name": "credential_save",
            "description": (
                "Save login credentials for a service (email provider, social media, etc.) "
                "to Ghost's secure credential store. Use after creating an account."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "service": {
                        "type": "string",
                        "description": "Service name, e.g. 'mail.com', 'twitter', 'instagram'",
                    },
                    "username": {
                        "type": "string",
                        "description": "Username or login handle",
                    },
                    "password": {
                        "type": "string",
                        "description": "Account password",
                    },
                    "email": {
                        "type": "string",
                        "description": "Full email address if applicable",
                        "default": "",
                    },
                    "notes": {
                        "type": "string",
                        "description": "Optional notes about this account",
                        "default": "",
                    },
                    "metadata": {
                        "type": "object",
                        "description": "Optional extra key-value data (recovery codes, security questions, etc.)",
                        "default": {},
                    },
                },
                "required": ["service", "username", "password"],
            },
            "execute": credential_save,
        },
        {
            "name": "credential_get",
            "description": (
                "Retrieve saved credentials for a service. "
                "Searches by service name, email, or username."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "service": {
                        "type": "string",
                        "description": "Service name, email, or username to search for",
                    },
                    "show_password": {
                        "type": "boolean",
                        "description": "Whether to include the password in the result (default false for safety)",
                        "default": False,
                    },
                },
                "required": ["service"],
            },
            "execute": credential_get,
        },
        {
            "name": "credential_list",
            "description": (
                "List all saved credentials. "
                "Passwords are hidden by default — set show_password=true to reveal them."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "show_password": {
                        "type": "boolean",
                        "description": "Show passwords in the listing",
                        "default": False,
                    },
                },
                "required": [],
            },
            "execute": credential_list,
        },
    ]
