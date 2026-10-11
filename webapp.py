"""Production WSGI entrypoint for Atlas Research.

Run locally with: flask --app webapp run
Deploy with: gunicorn --workers 2 --threads 4 --timeout 100 webapp:app
"""
from collections import defaultdict, deque
from datetime import datetime, timezone
from functools import wraps
from pathlib import Path
from threading import BoundedSemaphore, Lock
from urllib.parse import urlparse
import json
import os
import time

from flask import Flask, jsonify, make_response, render_template, request
from werkzeug.middleware.proxy_fix import ProxyFix

import engine

APP_VERSION = "2.0.0"
BASE_DIR = Path(__file__).resolve().parent

app = Flask(__name__, template_folder=str(BASE_DIR / "templates"))
app.config.update(
    MAX_CONTENT_LENGTH=24 * 1024,
    JSON_SORT_KEYS=False,
    PROPAGATE_EXCEPTIONS=False,
)
# Trust one proxy hop (typical managed-hosting reverse proxy) for client IP/scheme.
app.wsgi_app = ProxyFix(app.wsgi_app, x_for=1, x_proto=1, x_host=1)

# Best-effort process-local controls. For a high-traffic public deployment, move
# limits to a shared store/service (e.g. Redis) and add authentication/quotas.
_RATE_LOCK = Lock()
_REQUESTS = defaultdict(deque)
_SEARCH_SLOTS = BoundedSemaphore(value=2)
RATE_WINDOW_SECONDS = 60
RATE_LIMIT_PER_WINDOW = int(os.environ.get("ATLAS_RATE_LIMIT_PER_MINUTE", "10"))


def _rate_limited(client_key: str) -> bool:
    now = time.monotonic()
    with _RATE_LOCK:
        bucket = _REQUESTS[client_key]
        while bucket and now - bucket[0] > RATE_WINDOW_SECONDS:
            bucket.popleft()
        if len(bucket) >= RATE_LIMIT_PER_WINDOW:
            return True
        bucket.append(now)
        # Stop the dictionary growing forever when many addresses hit the service.
        if len(_REQUESTS) > 4000:
            stale = [key for key, values in _REQUESTS.items() if not values or now - values[-1] > RATE_WINDOW_SECONDS * 3]
            for key in stale[:2000]:
                _REQUESTS.pop(key, None)
        return False


@app.after_request
def secure_headers(response):
    response.headers["X-Content-Type-Options"] = "nosniff"
    response.headers["Referrer-Policy"] = "strict-origin-when-cross-origin"
    response.headers["X-Frame-Options"] = "DENY"
    response.headers["Permissions-Policy"] = "camera=(), microphone=(), geolocation=()"
    response.headers["Content-Security-Policy"] = (
        "default-src 'self'; base-uri 'self'; object-src 'none'; frame-ancestors 'none'; "
        "form-action 'self'; connect-src 'self'; img-src 'self' data:; "
        "style-src 'self' 'unsafe-inline'; script-src 'self' 'unsafe-inline'"
    )
    if request.is_secure:
        response.headers["Strict-Transport-Security"] = "max-age=31536000; includeSubDomains"
    if request.path.startswith("/api/"):
        response.headers["Cache-Control"] = "no-store"
    return response


@app.get("/")
def home():
    return render_template("index.html", version=APP_VERSION)


@app.get("/api/health")
def health():
    return jsonify({
        "ok": True,
        "version": APP_VERSION,
        "service": "Atlas Research",
        "sources": [{"key": key, "name": name} for key, (name, _fn) in engine.SOURCE_FUNCTIONS.items()],
        "timestamp": datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z"),
    })


@app.get("/robots.txt")
def robots():
    # Search results are dynamic; do not ask crawlers to index query responses.
    return make_response("User-agent: *\nDisallow: /api/\n", 200, {"Content-Type": "text/plain; charset=utf-8"})


@app.post("/api/search")
def search():
    origin = request.headers.get("Origin")
    if origin:
        parsed = urlparse(origin)
        if not parsed.netloc or parsed.netloc.lower() != request.host.lower():
            return jsonify({"error": "Cross-origin API requests are not allowed."}), 403

    if not request.is_json:
        return jsonify({"error": "Send a JSON request with Content-Type: application/json."}), 415

    client_key = request.remote_addr or "unknown"
    if _rate_limited(client_key):
        response = jsonify({"error": "Search rate limit reached. Please wait a minute before trying again."})
        response.status_code = 429
        response.headers["Retry-After"] = "60"
        return response

    payload = request.get_json(silent=True)
    if not isinstance(payload, dict):
        return jsonify({"error": "Invalid JSON request. Expected an object."}), 400

    if not _SEARCH_SLOTS.acquire(blocking=False):
        return jsonify({"error": "The research service is busy. Please retry in a few seconds."}), 429
    try:
        body, status = engine.build_search_response(payload)
        return jsonify(body), status
    except Exception:
        app.logger.exception("Unhandled search error")
        return jsonify({"error": "The research request failed unexpectedly. Please retry."}), 500
    finally:
        _SEARCH_SLOTS.release()


@app.errorhandler(413)
def too_large(_error):
    return jsonify({"error": "Request body is too large. Keep it under 24 KB."}), 413


@app.errorhandler(404)
def not_found(_error):
    if request.path.startswith("/api/"):
        return jsonify({"error": "API endpoint not found."}), 404
    return make_response("Page not found", 404)


if __name__ == "__main__":
    # Development only. Public hosting should use Gunicorn (see README).
    app.run(host="127.0.0.1", port=int(os.environ.get("PORT", "5055")), debug=False)
