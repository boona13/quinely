"""
ghost_secret_store.py — Transparent encryption-at-rest for local secrets.

Ghost stores provider API keys, OAuth tokens, and service passwords on disk.
Previously these sat in plaintext JSON. This module adds symmetric encryption so
those values are no longer readable in the file itself — defending against
accidental git commits, backup/log leakage, and casual disk inspection.

Design
------
• Uses Fernet (AES-128-CBC + HMAC) from the ``cryptography`` package when
  available. If ``cryptography`` is missing, every function degrades gracefully
  to a no-op pass-through so Ghost keeps working (values stay plaintext) — this
  preserves the cross-platform "no hard system deps" guarantee.
• The data key lives at ``~/.ghost/.secret_key`` (chmod 600), or can be supplied
  via the ``GHOST_SECRET_KEY`` environment variable for externalized key
  management. Encrypted values are prefixed with ``enc:v1:`` so plaintext and
  ciphertext can coexist during migration.

Threat model: this protects secrets *at rest in the data files*. The key sits on
the same machine, so it does not defend against a full local compromise — it
raises the bar from "plaintext in a JSON file" to "needs the local key too".
"""

import logging
import os
from pathlib import Path

log = logging.getLogger("quinely.secrets")

GHOST_HOME = Path.home() / ".ghost"
KEY_FILE = GHOST_HOME / ".secret_key"
ENC_PREFIX = "enc:v1:"

_fernet = None
_init_done = False
_unavailable_reason = ""


def _init():
    global _fernet, _init_done, _unavailable_reason
    if _init_done:
        return _fernet
    _init_done = True
    try:
        from cryptography.fernet import Fernet
    except Exception as e:  # pragma: no cover - depends on env
        _unavailable_reason = f"cryptography not installed ({e})"
        log.info("Secret encryption disabled: %s", _unavailable_reason)
        return None
    try:
        GHOST_HOME.mkdir(parents=True, exist_ok=True)
        env_key = os.environ.get("GHOST_SECRET_KEY", "").strip()
        if env_key:
            key = env_key.encode()
        elif KEY_FILE.exists():
            key = KEY_FILE.read_bytes().strip()
        else:
            key = Fernet.generate_key()
            KEY_FILE.write_bytes(key)
            try:
                os.chmod(KEY_FILE, 0o600)
            except Exception:
                pass
        _fernet = Fernet(key)
    except Exception as e:
        _unavailable_reason = str(e)
        log.warning("Secret encryption unavailable: %s", e)
        _fernet = None
    return _fernet


def is_available() -> bool:
    """True if encryption is active (cryptography present + key usable)."""
    return _init() is not None


def status() -> dict:
    avail = is_available()
    return {
        "available": avail,
        "reason": "" if avail else (_unavailable_reason or "unknown"),
        "key_file": str(KEY_FILE),
        "env_key": bool(os.environ.get("GHOST_SECRET_KEY")),
    }


def is_encrypted(value) -> bool:
    return isinstance(value, str) and value.startswith(ENC_PREFIX)


def encrypt(value):
    """Encrypt a string value. Non-strings, empty strings, and already-encrypted
    values are returned unchanged. Falls back to plaintext if unavailable."""
    if not isinstance(value, str) or not value or is_encrypted(value):
        return value
    f = _init()
    if f is None:
        return value
    try:
        token = f.encrypt(value.encode("utf-8")).decode("ascii")
        return ENC_PREFIX + token
    except Exception as e:
        log.warning("Encrypt failed, storing plaintext: %s", e)
        return value


def decrypt(value):
    """Decrypt an ``enc:v1:`` value. Plaintext values pass through unchanged."""
    if not is_encrypted(value):
        return value
    f = _init()
    if f is None:
        log.warning("Cannot decrypt secret — encryption unavailable")
        return value
    try:
        token = value[len(ENC_PREFIX):]
        return f.decrypt(token.encode("ascii")).decode("utf-8")
    except Exception as e:
        log.warning("Decrypt failed for a stored secret: %s", e)
        return value


def encrypt_fields(data: dict, fields) -> dict:
    """Return a shallow copy of ``data`` with the named fields encrypted."""
    out = dict(data)
    for f in fields:
        if f in out and isinstance(out[f], str) and out[f]:
            out[f] = encrypt(out[f])
    return out


def decrypt_fields(data: dict, fields) -> dict:
    """Return a shallow copy of ``data`` with the named fields decrypted."""
    out = dict(data)
    for f in fields:
        if f in out and is_encrypted(out[f]):
            out[f] = decrypt(out[f])
    return out
