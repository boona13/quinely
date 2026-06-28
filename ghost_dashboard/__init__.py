"""
Ghost Dashboard — Flask web app for controlling Ghost.

Can run standalone:     run_dashboard(port=3333)
Or embedded in daemon:  start_with_daemon(daemon, port=3333)
"""

import os, webbrowser, threading, logging, socket, secrets, hmac
from werkzeug.serving import make_server
from flask import Flask, request, jsonify, redirect, make_response
from pathlib import Path


def _get_dashboard_token() -> str:
    """Optional dashboard auth token. Env var wins; falls back to daemon config.

    When empty (default), the dashboard is unauthenticated as before — local-only
    usage is unchanged. Set GHOST_DASHBOARD_TOKEN (or config dashboard_auth_token)
    before binding to a non-loopback host."""
    tok = os.environ.get("GHOST_DASHBOARD_TOKEN", "").strip()
    if tok:
        return tok
    d = get_daemon()
    if d is not None:
        try:
            return (d.cfg.get("dashboard_auth_token") or "").strip()
        except Exception:
            return ""
    return ""


_AUTH_PUBLIC_PATHS = {"/auth", "/api/csrf-token"}
_AUTH_PUBLIC_PREFIXES = ("/static/",)


def _auth_login_html(error: bool = False) -> str:
    msg = '<p style="color:#e06c75">Invalid token.</p>' if error else ""
    return (
        "<!doctype html><html><head><meta charset='utf-8'><title>Ghost — Sign in</title>"
        "<style>body{background:#0d1117;color:#c9d1d9;font-family:system-ui,sans-serif;"
        "display:flex;height:100vh;align-items:center;justify-content:center;margin:0}"
        "form{background:#161b22;padding:32px;border:1px solid #30363d;border-radius:10px;width:300px}"
        "h1{font-size:18px;margin:0 0 16px}input{width:100%;padding:10px;margin:8px 0;"
        "background:#0d1117;border:1px solid #30363d;border-radius:6px;color:#c9d1d9;box-sizing:border-box}"
        "button{width:100%;padding:10px;background:#238636;color:#fff;border:0;border-radius:6px;"
        "cursor:pointer;margin-top:8px}</style></head><body>"
        "<form method='post' action='/auth'><h1>Quinely</h1>"
        f"{msg}<input type='password' name='token' placeholder='Access token' autofocus>"
        "<button type='submit'>Sign in</button></form></body></html>"
    )

# CSRF protection (optional - gracefully degrades if not installed)
try:
    from flask_wtf.csrf import CSRFProtect, generate_csrf
    _csrf_available = True
except ImportError:
    _csrf_available = False
    CSRFProtect = None
    generate_csrf = None


def _is_port_available(host: str, port: int) -> bool:
    """Check if a port is available by attempting to bind a test socket.
    
    This prevents calling make_server on an in-use port, which would trigger
    werkzeug's internal sys.exit(1) call (see BaseWSGIServer.__init__).
    """
    try:
        sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        sock.bind((host, port))
        sock.close()
        return True
    except OSError:
        return False

DASHBOARD_DIR = Path(__file__).resolve().parent

_daemon_ref = None
_server_ref = None


def get_daemon():
    """Return the live GhostDaemon instance (or None if running standalone)."""
    return _daemon_ref


def create_app():
    app = Flask(
        __name__,
        template_folder=str(DASHBOARD_DIR / "templates"),
        static_folder=str(DASHBOARD_DIR / "static"),
        static_url_path="/static",
    )
    app.config["JSON_SORT_KEYS"] = False
    app.config["SEND_FILE_MAX_AGE_DEFAULT"] = 0

    from .routes import register_routes
    register_routes(app)

    # Initialize CSRF protection (if available)
    csrf_obj = None
    if _csrf_available:
        app.config["SECRET_KEY"] = os.environ.get("GHOST_SECRET_KEY", secrets.token_hex(32))
        app.config["WTF_CSRF_TIME_LIMIT"] = 3600  # 1 hour token validity
        csrf = CSRFProtect(app)
        csrf_obj = csrf
        # Exempt webhook endpoints that use Bearer token auth (all paths under /api/webhooks/)
        csrf.exempt("/api/webhooks/")
        # Exempt CSRF token endpoint itself
        csrf.exempt("/api/csrf-token")
    else:
        logging.getLogger("ghost_dashboard").warning(
            "CSRF protection not available - install flask-wtf: pip install flask-wtf"
        )

    # Context processor to provide csrf_token() in templates (fallback when flask-wtf not installed)
    @app.context_processor
    def inject_csrf_token():
        if _csrf_available and generate_csrf:
            return dict(csrf_token=generate_csrf)
        return dict(csrf_token=lambda: "")


    @app.after_request
    def add_no_cache(response):
        if "text/javascript" in response.content_type or "text/css" in response.content_type:
            response.headers["Cache-Control"] = "no-cache, no-store, must-revalidate"
            response.headers["Pragma"] = "no-cache"
            response.headers["Expires"] = "0"
        return response

    @app.route("/api/csrf-token", methods=["GET"])
    def get_csrf_token():
        """Return a fresh CSRF token for the frontend."""
        if _csrf_available and generate_csrf:
            token = generate_csrf()
            return jsonify({"csrf_token": token})
        return jsonify({"csrf_token": ""})

    # ── Optional dashboard authentication ────────────────────────────
    @app.before_request
    def _require_dashboard_token():
        token = _get_dashboard_token()
        if not token:
            return  # auth disabled (default) — behaviour unchanged
        path = request.path or "/"
        if (path in _AUTH_PUBLIC_PATHS
                or path.startswith("/api/webhooks/")
                or any(path.startswith(pre) for pre in _AUTH_PUBLIC_PREFIXES)):
            return
        auth_header = request.headers.get("Authorization", "")
        bearer = auth_header[7:] if auth_header.startswith("Bearer ") else ""
        provided = (
            request.headers.get("X-Ghost-Token", "")
            or bearer
            or request.args.get("token", "")
            or request.cookies.get("ghost_token", "")
        )
        if provided and hmac.compare_digest(provided, token):
            return
        if path.startswith("/api/"):
            return jsonify({"error": "unauthorized",
                            "detail": "Dashboard auth token required"}), 401
        return redirect("/auth")

    @app.route("/auth", methods=["GET", "POST"])
    def _auth_page():
        token = _get_dashboard_token()
        if not token:
            return redirect("/")
        if request.method == "POST":
            provided = request.form.get("token", "")
            if not provided:
                provided = (request.get_json(silent=True) or {}).get("token", "")
            if provided and hmac.compare_digest(provided, token):
                resp = make_response(redirect("/"))
                resp.set_cookie("ghost_token", provided, httponly=True,
                                samesite="Lax", max_age=30 * 24 * 3600)
                return resp
            return _auth_login_html(error=True), 401
        return _auth_login_html()

    if csrf_obj is not None:
        csrf_obj.exempt(_auth_page)

    return app


def start_with_daemon(daemon, port=3333, open_browser=False):
    """Start dashboard as a background thread inside the Ghost daemon."""
    global _daemon_ref, _server_ref
    _daemon_ref = daemon

    app = create_app()

    log = logging.getLogger("werkzeug")
    log.setLevel(logging.WARNING)

    bind_host = os.environ.get("GHOST_BIND_HOST", "127.0.0.1")

    if not _is_port_available(bind_host, port):
        print(f"  ⚠ Dashboard port {port} is already in use — refusing to start a second instance.")
        print(f"    Run ./stop.sh first, or let ./start.sh handle cleanup automatically.")
        return None

    try:
        _server_ref = make_server(bind_host, port, app, threaded=True)
    except OSError as e:
        print(f"  ⚠ Dashboard bind failed on {bind_host}:{port}: {e}")
        return None

    t = threading.Thread(target=_server_ref.serve_forever, daemon=True, name="ghost-dashboard")
    t.start()

    if open_browser:
        threading.Timer(1.0, lambda: webbrowser.open(f"http://localhost:{port}")).start()

    return port


def stop_dashboard():
    """Shut down the background dashboard server."""
    global _server_ref, _daemon_ref
    if _server_ref:
        _server_ref.shutdown()
        _server_ref = None
    _daemon_ref = None


def run_dashboard(port=3333, open_browser=True):
    """Run dashboard as standalone (blocking). For `python ghost.py dashboard`.

    Uses resilient port binding (same strategy as start_with_daemon) so a busy
    default port does not crash the process with exit code 1.
    """
    app = create_app()

    log = logging.getLogger("werkzeug")
    log.setLevel(logging.WARNING)

    bind_host = os.environ.get("GHOST_BIND_HOST", "127.0.0.1")

    if not _is_port_available(bind_host, port):
        print(f"\n  ⚠ Dashboard port {port} is already in use.")
        print(f"    Another Ghost instance may be running. Use ./stop.sh first.\n")
        return

    try:
        server = make_server(bind_host, port, app, threaded=True)
    except OSError as e:
        print(f"\n  ⚠ Dashboard bind failed on {bind_host}:{port}: {e}\n")
        return

    url = f"http://localhost:{port}"
    print(f"\n  👻 Quinely → {url}\n")
    if open_browser:
        threading.Timer(1.0, lambda: webbrowser.open(url)).start()

    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        try:
            server.shutdown()
        except (OSError, RuntimeError) as exc:
            logging.getLogger("ghost_dashboard").warning("Server shutdown error: %s", exc)
