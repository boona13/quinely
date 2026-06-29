"""
GHOST OAuth — OpenAI Codex PKCE Flow

Implements the OAuth Authorization Code + PKCE flow for ChatGPT subscription users.
Uses the same client_id and endpoints as the Codex CLI.

Flow:
  1. Generate PKCE verifier + challenge
  2. Start local HTTP callback server on port 1455
  3. Open browser to auth.openai.com
  4. User logs in via ChatGPT
  5. Capture authorization code from callback
  6. Exchange code for tokens
  7. Save to auth profiles
"""

import base64
import hashlib
import json
import logging
import os
import secrets
import threading
import time

from http.server import HTTPServer, BaseHTTPRequestHandler
from urllib.parse import urlencode, urlparse, parse_qs

import requests

log = logging.getLogger("quinely.oauth")

CODEX_CLIENT_ID = "app_EMoamEEZ73f0CkXaXp7hrann"
CODEX_AUTH_URL = "https://auth.openai.com/oauth/authorize"
CODEX_TOKEN_URL = "https://auth.openai.com/oauth/token"
CODEX_REDIRECT_URI = "http://localhost:1455/auth/callback"
CODEX_SCOPE = "openid profile email offline_access"
CALLBACK_PORT = 1455
CALLBACK_TIMEOUT = 300  # 5 minutes to complete login


class _OAuthState:
    """Mutable state shared between the callback server and the main flow."""
    def __init__(self):
        self.code: str = ""
        self.error: str = ""
        self.state: str = ""
        self.received = threading.Event()


class _CallbackHandler(BaseHTTPRequestHandler):
    """HTTP handler for the localhost OAuth callback."""

    oauth_state: _OAuthState = None
    expected_state: str = ""

    def do_GET(self):
        parsed = urlparse(self.path)
        if parsed.path != "/auth/callback":
            self.send_response(404)
            self.end_headers()
            return

        params = parse_qs(parsed.query)
        code = params.get("code", [""])[0]
        state = params.get("state", [""])[0]
        error = params.get("error", [""])[0]

        if error:
            self.oauth_state.error = error
            body = self._html("Authentication Failed",
                              f"Error: {error}. You can close this tab.")
        elif state != self.expected_state:
            self.oauth_state.error = "state_mismatch"
            body = self._html("Security Error",
                              "State mismatch — possible CSRF. Please try again.")
        elif code:
            self.oauth_state.code = code
            self.oauth_state.state = state
            body = self._html("Success!",
                              "Ghost is now connected to your ChatGPT account. You can close this tab.")
        else:
            self.oauth_state.error = "no_code"
            body = self._html("Error", "No authorization code received.")

        self.send_response(200)
        self.send_header("Content-Type", "text/html")
        self.end_headers()
        self.wfile.write(body.encode())
        self.oauth_state.received.set()

    def log_message(self, format, *args):
        pass

    @staticmethod
    def _html(title, message):
        return f"""<!DOCTYPE html><html><head><title>Ghost Auth</title>
<style>body{{font-family:system-ui;background:#0a0a0a;color:#e4e4e7;
display:flex;align-items:center;justify-content:center;min-height:100vh;margin:0}}
.card{{background:#18181b;border:1px solid #27272a;border-radius:12px;padding:2rem;
max-width:400px;text-align:center}}
h1{{font-size:1.5rem;margin-bottom:0.5rem}}
p{{color:#a1a1aa;font-size:0.9rem}}</style></head>
<body><div class="card"><h1>{title}</h1><p>{message}</p></div></body></html>"""


def _generate_pkce() -> tuple[str, str]:
    """Generate PKCE code_verifier and code_challenge (S256)."""
    verifier = secrets.token_hex(32)
    digest = hashlib.sha256(verifier.encode()).digest()
    challenge = base64.urlsafe_b64encode(digest).rstrip(b"=").decode()
    return verifier, challenge


def start_codex_oauth(on_complete=None) -> dict:
    """Start the Codex OAuth PKCE flow.

    Opens the browser for user login.
    Returns immediately with the auth URL and state.
    The on_complete callback is called with (tokens_dict) when done.
    """
    verifier, challenge = _generate_pkce()
    state = secrets.token_hex(16)

    params = {
        "response_type": "code",
        "client_id": CODEX_CLIENT_ID,
        "redirect_uri": CODEX_REDIRECT_URI,
        "scope": CODEX_SCOPE,
        "code_challenge": challenge,
        "code_challenge_method": "S256",
        "state": state,
        "id_token_add_organizations": "true",
        "codex_cli_simplified_flow": "true",
        "originator": "ghost",
    }
    auth_url = f"{CODEX_AUTH_URL}?{urlencode(params)}"

    oauth_state = _OAuthState()

    def _run_server():
        _CallbackHandler.oauth_state = oauth_state
        _CallbackHandler.expected_state = state

        try:
            server = HTTPServer(("127.0.0.1", CALLBACK_PORT), _CallbackHandler)
            server.timeout = CALLBACK_TIMEOUT

            while not oauth_state.received.is_set():
                server.handle_request()

            server.server_close()
        except OSError as e:
            oauth_state.error = f"Cannot start callback server: {e}"
            oauth_state.received.set()
            return

        if oauth_state.code and not oauth_state.error:
            try:
                tokens = exchange_code(oauth_state.code, verifier)
                if on_complete:
                    on_complete(tokens)
            except Exception as e:
                oauth_state.error = str(e)

    thread = threading.Thread(target=_run_server, daemon=True)
    thread.start()

    return {
        "auth_url": auth_url,
        "state": state,
        "port": CALLBACK_PORT,
    }


def exchange_code(code: str, verifier: str) -> dict:
    """Exchange authorization code for access + refresh tokens."""
    resp = requests.post(CODEX_TOKEN_URL, json={
        "grant_type": "authorization_code",
        "client_id": CODEX_CLIENT_ID,
        "code": code,
        "redirect_uri": CODEX_REDIRECT_URI,
        "code_verifier": verifier,
    }, headers={"Content-Type": "application/json"}, timeout=30)

    resp.raise_for_status()
    data = resp.json()

    access_token = data.get("access_token", "")
    refresh_token = data.get("refresh_token", "")
    expires_in = data.get("expires_in", 3600)
    expires_at = time.time() + expires_in

    account_id = _extract_account_id(access_token)

    return {
        "access_token": access_token,
        "refresh_token": refresh_token,
        "expires_at": expires_at,
        "account_id": account_id,
    }


def refresh_codex_token(refresh_token: str) -> dict:
    """Refresh an expired Codex access token."""
    resp = requests.post(CODEX_TOKEN_URL, json={
        "grant_type": "refresh_token",
        "client_id": CODEX_CLIENT_ID,
        "refresh_token": refresh_token,
    }, headers={"Content-Type": "application/json"}, timeout=30)

    resp.raise_for_status()
    data = resp.json()

    access_token = data.get("access_token", "")
    new_refresh = data.get("refresh_token", refresh_token)
    expires_in = data.get("expires_in", 3600)
    expires_at = time.time() + expires_in
    account_id = _extract_account_id(access_token)

    return {
        "access_token": access_token,
        "refresh_token": new_refresh,
        "expires_at": expires_at,
        "account_id": account_id,
    }


def ensure_fresh_token(store) -> str | None:
    """Ensure the Codex OAuth token is fresh. Refresh if needed.
    Returns the access token or None if not configured."""
    profile = store.get_provider_profile("openai-codex")
    if not profile or profile.get("type") != "oauth":
        return None

    access = profile.get("access_token", "")
    refresh = profile.get("refresh_token", "")
    expires = profile.get("expires_at", 0)

    if not access:
        return None

    if expires and time.time() > (expires - 300):
        if not refresh:
            log.warning("Codex token expired and no refresh token available")
            return None
        try:
            tokens = refresh_codex_token(refresh)
            store.set_oauth(
                "openai-codex",
                tokens["access_token"],
                tokens["refresh_token"],
                expires_at=tokens["expires_at"],
                account_id=tokens.get("account_id", profile.get("account_id", "")),
            )
            log.info("Refreshed Codex OAuth token")
            return tokens["access_token"]
        except Exception as e:
            log.error("Failed to refresh Codex token: %s", e)
            return None

    return access


def _extract_account_id(token: str) -> str:
    """Extract chatgpt_account_id from JWT payload."""
    try:
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
            or ""
        )
    except Exception:
        return ""


def get_codex_oauth_status() -> dict:
    """Check the current state of Codex OAuth."""
    from ghost_auth_profiles import get_auth_store
    store = get_auth_store()
    profile = store.get_provider_profile("openai-codex")

    if not profile or profile.get("type") != "oauth":
        synced = store.sync_codex_cli()
        if synced:
            profile = store.get_provider_profile("openai-codex")
        else:
            return {"configured": False, "source": None}

    expires = profile.get("expires_at", 0)
    return {
        "configured": True,
        "account_id": profile.get("account_id", ""),
        "expired": expires > 0 and time.time() > expires,
        "has_refresh": bool(profile.get("refresh_token")),
        "source": "oauth",
    }
