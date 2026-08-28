"""
Product Sale Website - Main Backend
Run locally (Termux/Android or any machine):
    python main.py
Then open http://127.0.0.1:8000 in a browser.

For production, run behind a real WSGI server (gunicorn/waitress) and HTTPS,
and set a real SECRET_KEY environment variable.
"""

import os
import json
import time
import secrets
import string
import threading
import functools
from datetime import datetime, timedelta

from flask import (
    Flask, request, jsonify, render_template, redirect,
    url_for, session, abort, flash, send_from_directory
)
from werkzeug.security import generate_password_hash, check_password_hash

# --------------------------------------------------------------------------
# Paths / constants
# --------------------------------------------------------------------------

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
ASSETS_DIR = os.path.join(BASE_DIR, "assets")

# --------------------------------------------------------------------------
# Storage backend
# --------------------------------------------------------------------------
# On Render's free plan (and similar free hosts) the whole app folder is
# rebuilt from git on every restart/redeploy - there is no persistent disk,
# so plain local JSON files always get wiped and reset to whatever is in
# the repo. To fix that, this app can optionally store its JSON "database"
# in Upstash Redis instead (Upstash has a free-forever tier, no card
# needed: https://upstash.com -> Create Database -> copy the REST URL and
# REST TOKEN it gives you).
#
# Set these two environment variables on Render to enable it:
#   UPSTASH_REDIS_REST_URL
#   UPSTASH_REDIS_REST_TOKEN
#
# If they're not set, the app falls back to local JSON files under DATA_DIR
# (fine for Termux / a VPS / Docker with a real persistent volume).
UPSTASH_URL = os.environ.get("UPSTASH_REDIS_REST_URL", "").rstrip("/")
UPSTASH_TOKEN = os.environ.get("UPSTASH_REDIS_REST_TOKEN", "")
USE_REDIS = bool(UPSTASH_URL and UPSTASH_TOKEN)

DATA_DIR = os.environ.get("DATA_DIR", BASE_DIR)
os.makedirs(DATA_DIR, exist_ok=True)

DATA_FILES = {
    "products": os.path.join(DATA_DIR, "products.json"),
    "tokens": os.path.join(DATA_DIR, "tokens.json"),
    "keys": os.path.join(DATA_DIR, "keys.json"),
    "settings": os.path.join(DATA_DIR, "settings.json"),
    "admin": os.path.join(DATA_DIR, "admin.json"),
    "logs": os.path.join(DATA_DIR, "logs.json"),
}

if USE_REDIS:
    import urllib.request

    def _redis_command(*args):
        """Send a single Redis command via Upstash's body-style REST API:
        POST the command + args as a JSON array in the request body. This
        avoids URL-encoding entirely, which is important because our values
        are JSON blobs (braces, quotes, spaces) that don't survive being
        encoded into a URL path reliably."""
        body = json.dumps(list(args)).encode("utf-8")
        req = urllib.request.Request(
            UPSTASH_URL,
            data=body,
            method="POST",
            headers={
                "Authorization": f"Bearer {UPSTASH_TOKEN}",
                "Content-Type": "application/json",
            },
        )
        with urllib.request.urlopen(req, timeout=10) as resp:
            return json.loads(resp.read().decode("utf-8"))

    # Self-test at startup so connection problems show up immediately in the
    # logs instead of silently making every load_json() call fall back to
    # "empty", which looks exactly like "admin not found" and is confusing.
    try:
        _test = _redis_command("SET", "page-main:_selftest", "ok")
        _test_get = _redis_command("GET", "page-main:_selftest")
        if _test_get.get("result") == "ok":
            print("Redis backend (Upstash): connected OK - data will persist across restarts.")
        else:
            print("Redis backend (Upstash): connected but GET did not return the expected value.")
            print(f"  SET response: {_test}")
            print(f"  GET response: {_test_get}")
    except Exception as e:
        print("=" * 60)
        print("Redis backend (Upstash): CONNECTION FAILED.")
        print(f"  Error: {type(e).__name__}: {e}")
        print("  Check UPSTASH_REDIS_REST_URL and UPSTASH_REDIS_REST_TOKEN are")
        print("  copied exactly (no extra spaces/quotes) from the Upstash REST API")
        print("  panel. Falling back to local files, which will NOT persist here.")
        print("=" * 60)


_LIST_TYPES = {"logs"}
_locks = {name: threading.Lock() for name in DATA_FILES}

# --------------------------------------------------------------------------
# Safe JSON storage helpers
# --------------------------------------------------------------------------

def _empty_value(name):
    return [] if name in _LIST_TYPES else {}


def load_json(name):
    if USE_REDIS:
        try:
            result = _redis_command("GET", f"page-main:{name}")
            raw = result.get("result")
            if raw is None:
                return _empty_value(name)
            return json.loads(raw)
        except Exception as e:
            print(f"Redis GET failed for '{name}': {type(e).__name__}: {e}")
            return _empty_value(name)

    path = DATA_FILES[name]
    if not os.path.exists(path):
        return _empty_value(name)
    with _locks[name]:
        try:
            with open(path, "r", encoding="utf-8") as f:
                return json.load(f)
        except (json.JSONDecodeError, OSError):
            return _empty_value(name)


def save_json(name, data):
    if USE_REDIS:
        encoded = json.dumps(data, ensure_ascii=False)
        _redis_command("SET", f"page-main:{name}", encoded)
        return

    path = DATA_FILES[name]
    with _locks[name]:
        tmp_path = path + ".tmp"
        with open(tmp_path, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
        os.replace(tmp_path, path)  # atomic on POSIX, avoids corruption


def load_products():
    data = load_json("products")
    return data if isinstance(data, list) else []


def save_products(items):
    save_json("products", items)


def load_settings():
    data = load_json("settings")
    return data if isinstance(data, dict) and data else DEFAULT_SETTINGS


def save_settings(data):
    save_json("settings", data)


DEFAULT_SETTINGS = {
    "site": {
        "site_title": "My Product Shop",
        "index_title": "Home - My Product Shop",
        "step_title": "Continue - My Product Shop",
        "key_title": "Get Your Key",
        "logo": "logo.png",
        "favicon": "favicon.png",
        "maintenance_mode": False,
    },
    "step": {
        "rules_html": "<p>Follow the steps to continue.</p>",
        "destination_url": "https://example.com/continue",
        "token_validity_minutes": 15,
        "enabled": True,
    },
    "key": {
        "token_param_name": "token",
        "key_validity_hours": 24,
        "bind_ip": True,
    },
    "admin_session_hours": 12,
}


def log_event(event_type, details=None):
    logs = load_json("logs")
    if not isinstance(logs, list):
        logs = []
    logs.append({
        "type": event_type,
        "details": details or {},
        "time": datetime.utcnow().isoformat() + "Z",
        "ip": get_client_ip(),
    })
    logs = logs[-1000:]  # keep the log file bounded
    save_json("logs", logs)


# --------------------------------------------------------------------------
# Admin bootstrap (create a default admin account on first run)
# --------------------------------------------------------------------------

def ensure_admin_exists():
    admin = load_json("admin")
    if not admin or not admin.get("username"):
        default_password = os.environ.get("ADMIN_BOOTSTRAP_PASSWORD") or secrets.token_urlsafe(9)
        admin = {
            "username": "admin",
            "password_hash": generate_password_hash(default_password),
        }
        save_json("admin", admin)
        print("=" * 60)
        print("No admin account found - created one automatically:")
        print(f"  username: admin")
        print(f"  password: {default_password}")
        print("Log in at /admin/login and change this immediately")
        print("(there's no in-app 'change password' UI yet - edit admin.json")
        print(" with a new werkzeug password hash, or delete it to regenerate).")
        print("=" * 60)
    return admin


# --------------------------------------------------------------------------
# App setup
# --------------------------------------------------------------------------

app = Flask(__name__)
app.secret_key = os.environ.get("SECRET_KEY", secrets.token_hex(32))

if not os.environ.get("SECRET_KEY"):
    print("WARNING: SECRET_KEY env var not set - using a random key for this run.")
    print("Admin sessions will be invalidated every restart. Set SECRET_KEY in production.")

ensure_admin_exists()


def get_client_ip():
    forwarded = request.headers.get("X-Forwarded-For", "")
    if forwarded:
        return forwarded.split(",")[0].strip()
    return request.remote_addr or "unknown"


# --------------------------------------------------------------------------
# Token / key helpers
# --------------------------------------------------------------------------

def generate_token():
    return secrets.token_urlsafe(24)


def generate_key():
    alphabet = string.ascii_uppercase + string.digits
    groups = ["".join(secrets.choice(alphabet) for _ in range(4)) for _ in range(4)]
    return "-".join(groups)


def cleanup_tokens():
    """Remove tokens once they're past their expiry time."""
    tokens = load_json("tokens")
    if not isinstance(tokens, dict):
        tokens = {}
    now = time.time()
    changed = False
    for tok, info in list(tokens.items()):
        if info.get("expires_at", 0) < now:
            del tokens[tok]
            changed = True
    if changed:
        save_json("tokens", tokens)
    return tokens


def find_active_token_for_ip(tokens, ip):
    """Return (token_value, info) for the most recently issued, still-valid
    token bound to this IP, or (None, None) if there isn't one."""
    now = time.time()
    candidates = [
        (tok, info) for tok, info in tokens.items()
        if info.get("ip") == ip and info.get("expires_at", 0) >= now
    ]
    if not candidates:
        return None, None
    candidates.sort(key=lambda item: item[1].get("created_at", 0), reverse=True)
    return candidates[0]


def cleanup_keys():
    keys = load_json("keys")
    if not isinstance(keys, dict):
        keys = {}
    now = time.time()
    changed = False
    for k, info in list(keys.items()):
        if info.get("status") == "active" and info.get("expires_at", 0) < now:
            info["status"] = "expired"
            changed = True
    if changed:
        save_json("keys", keys)
    return keys


# --------------------------------------------------------------------------
# Admin auth decorator
# --------------------------------------------------------------------------

def admin_required(view):
    @functools.wraps(view)
    def wrapped(*args, **kwargs):
        if not session.get("admin_logged_in"):
            return redirect(url_for("admin_login", next=request.path))
        return view(*args, **kwargs)
    return wrapped


# --------------------------------------------------------------------------
# Public routes
# --------------------------------------------------------------------------

@app.route("/assets/<path:filename>")
def serve_asset(filename):
    return send_from_directory(ASSETS_DIR, filename)


@app.route("/")
def index():
    settings = load_settings()
    if settings["site"].get("maintenance_mode"):
        return render_template("index.html", maintenance=True, settings=settings, products=[])
    products = [p for p in load_products() if p.get("enabled", True)]
    products.sort(key=lambda p: p.get("position", 0))
    return render_template("index.html", maintenance=False, settings=settings, products=products)


@app.route("/buy/<product_id>")
def buy_now(product_id):
    products = load_products()
    product = next((p for p in products if p["id"] == product_id), None)
    if not product or not product.get("enabled", True):
        abort(404)
    log_event("buy_click", {"product_id": product_id})
    return redirect(product["buy_now_url"])


@app.route("/step")
def step_page():
    settings = load_settings()
    if not settings["step"].get("enabled", True):
        return render_template("step.html", settings=settings, disabled=True)
    return render_template("step.html", settings=settings, disabled=False)


@app.route("/step/continue", methods=["POST"])
def step_continue():
    settings = load_settings()
    if not settings["step"].get("enabled", True):
        abort(403)

    tokens = cleanup_tokens()
    token = generate_token()
    validity_minutes = int(settings["step"].get("token_validity_minutes", 15))
    now = time.time()
    tokens[token] = {
        "ip": get_client_ip(),
        "created_at": now,
        "expires_at": now + validity_minutes * 60,
        "status": "active",
    }
    save_json("tokens", tokens)
    log_event("token_generated", {"token_prefix": token[:8]})

    param_name = settings["key"].get("token_param_name", "token")
    dest = settings["step"].get("destination_url", "/")
    sep = "&" if "?" in dest else "?"
    return redirect(f"{dest}{sep}{param_name}={token}")


@app.route("/key")
def key_page():
    settings = load_settings()
    param_name = settings["key"].get("token_param_name", "token")
    # Only checks that the parameter KEY is present in the URL at all -
    # "?pid" or "?pid=" or "?pid=anything" all count. The value is never
    # compared to anything; only presence of the name matters here.
    param_present = param_name in request.args
    client_ip = get_client_ip()

    tokens = cleanup_tokens()
    matched_token, info = find_active_token_for_ip(tokens, client_ip)

    # Single generic message on purpose: we don't want to reveal *why* a
    # link failed (missing param / no token for this IP / expired) since
    # that helps someone probe the token system.
    generic_error = "This link is invalid, expired, or was already used. Please start over."

    valid = param_present and matched_token is not None

    if not valid:
        log_event("key_page_invalid", {"ip": client_ip, "param_present": param_present})
        return render_template("key.html", settings=settings, error=generic_error, key=None)

    # Matched: consume this IP's token immediately so it can never be
    # reused, and drop it from tokens.json entirely.
    del tokens[matched_token]
    save_json("tokens", tokens)

    keys = load_json("keys")
    if not isinstance(keys, dict):
        keys = {}
    new_key = generate_key()
    validity_hours = float(settings["key"].get("key_validity_hours", 24))
    now = time.time()
    keys[new_key] = {
        "ip": client_ip if settings["key"].get("bind_ip", True) else None,
        "created_at": now,
        "expires_at": now + validity_hours * 3600,
        "status": "active",
    }
    save_json("keys", keys)
    log_event("key_generated", {"key": new_key})

    return render_template("key.html", settings=settings, error=None, key=new_key)


# --------------------------------------------------------------------------
# Public verification API (used by an external app/website to check a key)
# --------------------------------------------------------------------------

@app.route("/api/verify", methods=["POST"])
def api_verify():
    payload = request.get_json(silent=True) or request.form
    key = (payload.get("key") or "").strip()
    ip = (payload.get("ip") or "").strip()

    if not key:
        return jsonify({"success": False, "reason": "missing_key"}), 400

    keys = cleanup_keys()
    info = keys.get(key)
    settings = load_settings()

    if not info:
        log_event("key_verify_fail", {"reason": "not_found"})
        return jsonify({"success": False, "reason": "invalid_key"}), 404

    if info.get("status") != "active":
        log_event("key_verify_fail", {"reason": "inactive", "key": key})
        return jsonify({"success": False, "reason": "key_inactive_or_expired"}), 403

    if settings["key"].get("bind_ip", True) and info.get("ip"):
        if not ip or ip != info["ip"]:
            log_event("key_verify_fail", {"reason": "ip_mismatch", "key": key})
            return jsonify({"success": False, "reason": "ip_mismatch"}), 403

    log_event("key_verify_success", {"key": key})
    return jsonify({
        "success": True,
        "expires_at": info.get("expires_at"),
    })


# --------------------------------------------------------------------------
# Admin: auth
# --------------------------------------------------------------------------

@app.route("/admin/login", methods=["GET", "POST"])
def admin_login():
    if request.method == "POST":
        username = request.form.get("username", "")
        password = request.form.get("password", "")
        admin = load_json("admin")
        if admin and username == admin.get("username") and check_password_hash(admin.get("password_hash", ""), password):
            session.clear()
            session["admin_logged_in"] = True
            session["admin_username"] = username
            session.permanent = True
            log_event("admin_login", {"username": username})
            next_url = request.args.get("next") or url_for("admin_dashboard")
            return redirect(next_url)
        flash("Invalid username or password.")
    return render_template("login.html")


@app.route("/admin/logout")
def admin_logout():
    session.clear()
    return redirect(url_for("admin_login"))


# --------------------------------------------------------------------------
# Admin: dashboard
# --------------------------------------------------------------------------

@app.route("/admin")
@admin_required
def admin_dashboard():
    products = load_products()
    tokens = cleanup_tokens()
    keys = cleanup_keys()
    logs = load_json("logs")
    if not isinstance(logs, list):
        logs = []

    stats = {
        "total_products": len(products),
        "active_products": sum(1 for p in products if p.get("enabled", True)),
        "total_keys": len(keys),
        "active_keys": sum(1 for k in keys.values() if k.get("status") == "active"),
        "expired_keys": sum(1 for k in keys.values() if k.get("status") != "active"),
        "active_tokens": sum(1 for t in tokens.values() if t.get("status") == "active"),
        "recent_logs": list(reversed(logs))[:20],
    }
    return render_template("admin.html", section="dashboard", stats=stats)


# --------------------------------------------------------------------------
# Admin: products
# --------------------------------------------------------------------------

@app.route("/admin/products")
@admin_required
def admin_products():
    products = load_products()
    products.sort(key=lambda p: p.get("position", 0))
    return render_template("admin.html", section="products", products=products)


@app.route("/admin/products/add", methods=["POST"])
@admin_required
def admin_products_add():
    products = load_products()
    new_id = "prod_" + secrets.token_hex(4)
    max_pos = max([p.get("position", 0) for p in products], default=0)
    products.append({
        "id": new_id,
        "name": request.form.get("name", "New Product"),
        "description": request.form.get("description", ""),
        "image": request.form.get("image", ""),
        "price": request.form.get("price", ""),
        "buy_now_url": request.form.get("buy_now_url", "#"),
        "enabled": True,
        "position": max_pos + 1,
    })
    save_products(products)
    log_event("product_added", {"id": new_id})
    return redirect(url_for("admin_products"))


@app.route("/admin/products/<product_id>/edit", methods=["POST"])
@admin_required
def admin_products_edit(product_id):
    products = load_products()
    for p in products:
        if p["id"] == product_id:
            p["name"] = request.form.get("name", p["name"])
            p["description"] = request.form.get("description", p["description"])
            p["image"] = request.form.get("image", p["image"])
            p["price"] = request.form.get("price", p["price"])
            p["buy_now_url"] = request.form.get("buy_now_url", p["buy_now_url"])
            break
    save_products(products)
    log_event("product_edited", {"id": product_id})
    return redirect(url_for("admin_products"))


@app.route("/admin/products/<product_id>/delete", methods=["POST"])
@admin_required
def admin_products_delete(product_id):
    products = [p for p in load_products() if p["id"] != product_id]
    save_products(products)
    log_event("product_deleted", {"id": product_id})
    return redirect(url_for("admin_products"))


@app.route("/admin/products/<product_id>/toggle", methods=["POST"])
@admin_required
def admin_products_toggle(product_id):
    products = load_products()
    for p in products:
        if p["id"] == product_id:
            p["enabled"] = not p.get("enabled", True)
            break
    save_products(products)
    log_event("product_toggled", {"id": product_id})
    return redirect(url_for("admin_products"))


@app.route("/admin/products/<product_id>/move/<direction>", methods=["POST"])
@admin_required
def admin_products_move(product_id, direction):
    products = load_products()
    products.sort(key=lambda p: p.get("position", 0))
    idx = next((i for i, p in enumerate(products) if p["id"] == product_id), None)
    if idx is not None:
        swap_idx = idx - 1 if direction == "up" else idx + 1
        if 0 <= swap_idx < len(products):
            products[idx]["position"], products[swap_idx]["position"] = (
                products[swap_idx].get("position", 0), products[idx].get("position", 0)
            )
    save_products(products)
    return redirect(url_for("admin_products"))


# --------------------------------------------------------------------------
# Admin: keys
# --------------------------------------------------------------------------

@app.route("/admin/keys")
@admin_required
def admin_keys():
    keys = cleanup_keys()
    q = request.args.get("q", "").strip().upper()
    items = [{"key": k, **v} for k, v in keys.items()]
    if q:
        items = [i for i in items if q in i["key"]]
    items.sort(key=lambda i: i.get("created_at", 0), reverse=True)
    return render_template("admin.html", section="keys", keys=items, query=request.args.get("q", ""))


@app.route("/admin/keys/generate", methods=["POST"])
@admin_required
def admin_keys_generate():
    settings = load_settings()
    keys = load_json("keys")
    if not isinstance(keys, dict):
        keys = {}
    new_key = generate_key()
    hours = float(request.form.get("validity_hours") or settings["key"].get("key_validity_hours", 24))
    now = time.time()
    keys[new_key] = {
        "ip": None,
        "created_at": now,
        "expires_at": now + hours * 3600,
        "status": "active",
    }
    save_json("keys", keys)
    log_event("admin_key_generated", {"key": new_key})
    return redirect(url_for("admin_keys"))


@app.route("/admin/keys/<key>/toggle", methods=["POST"])
@admin_required
def admin_keys_toggle(key):
    keys = load_json("keys")
    if key in keys:
        keys[key]["status"] = "disabled" if keys[key].get("status") == "active" else "active"
        save_json("keys", keys)
        log_event("admin_key_toggled", {"key": key})
    return redirect(url_for("admin_keys"))


@app.route("/admin/keys/<key>/unbind", methods=["POST"])
@admin_required
def admin_keys_unbind(key):
    keys = load_json("keys")
    if key in keys:
        keys[key]["ip"] = None
        save_json("keys", keys)
        log_event("admin_key_unbound", {"key": key})
    return redirect(url_for("admin_keys"))


@app.route("/admin/keys/<key>/delete", methods=["POST"])
@admin_required
def admin_keys_delete(key):
    keys = load_json("keys")
    if key in keys:
        del keys[key]
        save_json("keys", keys)
        log_event("admin_key_deleted", {"key": key})
    return redirect(url_for("admin_keys"))


# --------------------------------------------------------------------------
# Admin: settings (site / step / key / token)
# --------------------------------------------------------------------------

@app.route("/admin/settings", methods=["GET", "POST"])
@admin_required
def admin_settings():
    settings = load_settings()
    if request.method == "POST":
        form = request.form
        settings["site"]["site_title"] = form.get("site_title", settings["site"]["site_title"])
        settings["site"]["index_title"] = form.get("index_title", settings["site"]["index_title"])
        settings["site"]["step_title"] = form.get("step_title", settings["site"]["step_title"])
        settings["site"]["key_title"] = form.get("key_title", settings["site"]["key_title"])
        settings["site"]["logo"] = form.get("logo", settings["site"]["logo"])
        settings["site"]["favicon"] = form.get("favicon", settings["site"]["favicon"])
        settings["site"]["maintenance_mode"] = form.get("maintenance_mode") == "on"

        settings["step"]["rules_html"] = form.get("rules_html", settings["step"]["rules_html"])
        settings["step"]["destination_url"] = form.get("destination_url", settings["step"]["destination_url"])
        settings["step"]["token_validity_minutes"] = int(form.get("token_validity_minutes") or settings["step"]["token_validity_minutes"])
        settings["step"]["enabled"] = form.get("step_enabled") == "on"

        settings["key"]["token_param_name"] = form.get("token_param_name", settings["key"]["token_param_name"]) or "token"
        settings["key"]["key_validity_hours"] = float(form.get("key_validity_hours") or settings["key"]["key_validity_hours"])
        settings["key"]["bind_ip"] = form.get("bind_ip") == "on"

        save_settings(settings)
        log_event("settings_updated", {})
        flash("Settings saved.")
        return redirect(url_for("admin_settings"))

    return render_template("admin.html", section="settings", settings=settings)


# --------------------------------------------------------------------------
# Admin: logs
# --------------------------------------------------------------------------

@app.route("/admin/logs")
@admin_required
def admin_logs():
    logs = load_json("logs")
    if not isinstance(logs, list):
        logs = []
    return render_template("admin.html", section="logs", logs=list(reversed(logs))[:200])


# --------------------------------------------------------------------------
# Entrypoint
# --------------------------------------------------------------------------

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 8000))
    debug = os.environ.get("DEBUG", "1") == "1"
    print(f"Starting server on http://127.0.0.1:{port}  (Ctrl+C to stop)")
    app.run(host="0.0.0.0", port=port, debug=debug)
