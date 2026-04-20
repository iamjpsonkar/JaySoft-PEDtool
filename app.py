from __future__ import annotations

import collections
import copy
import json
import logging
import os
import random
import re
import sqlite3
import string
import time
import threading
import uuid as _uuid
from base64 import b64decode, b64encode
from datetime import datetime, timedelta, timezone
from functools import wraps
from urllib.parse import urlparse

from dotenv import load_dotenv

# Resolve paths relative to this file (needed for PythonAnywhere WSGI)
_BASE_DIR = os.path.dirname(os.path.abspath(__file__))

load_dotenv(os.path.join(_BASE_DIR, ".env"))

from Crypto.Cipher import AES
from Crypto.Util.Padding import pad, unpad
from flask import Flask, Response, g, jsonify, redirect, render_template, request, session, url_for
from simpleeval import EvalWithCompoundTypes

import requests as http_requests
import shortuuid


# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

DB_PATH = os.environ.get("PED_DB_PATH", os.path.join(_BASE_DIR, "pedapp.db"))
DEFAULT_ENC_IV = os.environ.get("PED_DEFAULT_ENC_IV", "")
DEFAULT_SECRET = os.environ.get("PED_DEFAULT_SECRET", "")
API_TOKEN = os.environ.get("PED_API_TOKEN", "")
ALLOWED_PROXY_DOMAINS = [
    d.strip()
    for d in os.environ.get("PED_ALLOWED_PROXY_DOMAINS", "").split(",")
    if d.strip()
]
DEBUG = os.environ.get("PED_DEBUG", "false").lower() == "true"
FORWARD_TIMEOUT = int(os.environ.get("PED_FORWARD_TIMEOUT", "30"))
REQUEST_HISTORY_LIMIT = int(os.environ.get("PED_HISTORY_LIMIT", "100"))
UI_PASSWORD = os.environ.get("PED_UI_PASSWORD", "")  # empty = no login required

_SECRET_KEY_DEV_FALLBACK = "ped-tools-dev-secret-DO-NOT-USE-IN-PROD"
_secret_key = os.environ.get("PED_SECRET_KEY", "")
if not _secret_key:
    if DEBUG:
        _secret_key = _SECRET_KEY_DEV_FALLBACK
    else:
        raise RuntimeError(
            "PED_SECRET_KEY must be set when PED_DEBUG is not 'true'. "
            "Generate one with: python -c 'import secrets; print(secrets.token_hex(32))'"
        )

app = Flask(__name__)
app.secret_key = _secret_key

# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------

logging.basicConfig(
    level=logging.DEBUG if DEBUG else logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
logger = logging.getLogger("pedapp")

if app.secret_key == _SECRET_KEY_DEV_FALLBACK:
    logger.warning(
        "[STARTUP] PED_SECRET_KEY not set; using insecure DEV fallback. "
        "Do NOT run this configuration in production."
    )


# ---------------------------------------------------------------------------
# SQLite storage
# ---------------------------------------------------------------------------


def _get_db() -> sqlite3.Connection:
    """Return a per-request DB connection stored on Flask's `g` object."""
    if "db" not in g:
        g.db = sqlite3.connect(DB_PATH)
        g.db.row_factory = sqlite3.Row
        g.db.execute("PRAGMA journal_mode=WAL")
        g.db.execute("PRAGMA foreign_keys=ON")
    return g.db


@app.teardown_appcontext
def _close_db(_exc):
    db = g.pop("db", None)
    if db is not None:
        db.close()


def init_db():
    """Create tables if they don't exist. Called once at startup."""
    conn = sqlite3.connect(DB_PATH)
    conn.executescript(
        """
        CREATE TABLE IF NOT EXISTS proxies (
            identifier  TEXT PRIMARY KEY,
            api_domain  TEXT NOT NULL,
            created_at  TEXT NOT NULL DEFAULT (datetime('now'))
        );

        CREATE TABLE IF NOT EXISTS mocks (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            proxy_id    TEXT NOT NULL REFERENCES proxies(identifier) ON DELETE CASCADE,
            endpoint    TEXT NOT NULL,
            method      TEXT NOT NULL,
            response    TEXT NOT NULL,
            created_at  TEXT NOT NULL DEFAULT (datetime('now')),
            updated_at  TEXT NOT NULL DEFAULT (datetime('now')),
            UNIQUE(proxy_id, endpoint, method)
        );

        CREATE INDEX IF NOT EXISTS idx_mocks_proxy ON mocks(proxy_id);
        CREATE INDEX IF NOT EXISTS idx_mocks_lookup ON mocks(proxy_id, endpoint, method);

        CREATE TABLE IF NOT EXISTS request_history (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            proxy_id    TEXT NOT NULL,
            endpoint    TEXT NOT NULL,
            method      TEXT NOT NULL,
            request_headers TEXT,
            request_body    TEXT,
            query_params    TEXT,
            response_status INTEGER,
            response_body   TEXT,
            source          TEXT NOT NULL DEFAULT 'forward',
            duration_ms     INTEGER,
            created_at  TEXT NOT NULL DEFAULT (datetime('now'))
        );

        CREATE INDEX IF NOT EXISTS idx_history_proxy ON request_history(proxy_id);
        CREATE INDEX IF NOT EXISTS idx_history_time ON request_history(created_at);

        CREATE TABLE IF NOT EXISTS mock_sequences (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            proxy_id    TEXT NOT NULL,
            endpoint    TEXT NOT NULL,
            method      TEXT NOT NULL,
            call_count  INTEGER NOT NULL DEFAULT 0,
            UNIQUE(proxy_id, endpoint, method)
        );

        CREATE TABLE IF NOT EXISTS mock_state (
            proxy_id   TEXT PRIMARY KEY,
            data       TEXT NOT NULL DEFAULT '{}',
            created_at TEXT NOT NULL DEFAULT (datetime('now')),
            updated_at TEXT NOT NULL DEFAULT (datetime('now'))
        );
        """
    )
    conn.commit()
    conn.close()
    logger.info("Database initialized at %s", DB_PATH)


# ---------------------------------------------------------------------------
# DB helper functions — Proxies & Mocks
# ---------------------------------------------------------------------------


def db_create_proxy(identifier: str, api_domain: str) -> dict | None:
    """Create or replace a proxy. Returns old mocks if proxy existed."""
    db = _get_db()
    old_mocks = db_get_mocks_for_proxy(identifier)

    db.execute(
        "INSERT INTO proxies (identifier, api_domain) VALUES (?, ?) "
        "ON CONFLICT(identifier) DO UPDATE SET api_domain = excluded.api_domain",
        (identifier, api_domain),
    )
    if old_mocks:
        db.execute("DELETE FROM mocks WHERE proxy_id = ?", (identifier,))
    db.commit()
    logger.info("[DB] Proxy '%s' -> %s (replaced=%s)", identifier, api_domain, bool(old_mocks))
    return old_mocks or None


def db_get_proxy(identifier: str) -> dict | None:
    """Return proxy info with all mocks, or None."""
    db = _get_db()
    row = db.execute(
        "SELECT identifier, api_domain FROM proxies WHERE identifier = ?",
        (identifier,),
    ).fetchone()
    if not row:
        return None
    return {
        "identifier": row["identifier"],
        "api_domain": row["api_domain"],
        "mocked_requests": db_get_mocks_for_proxy(identifier),
    }


def db_get_mocks_for_proxy(identifier: str) -> dict:
    """Return mocks as {endpoint: {method: response_json, ...}, ...}."""
    db = _get_db()
    rows = db.execute(
        "SELECT endpoint, method, response FROM mocks WHERE proxy_id = ?",
        (identifier,),
    ).fetchall()
    result: dict = {}
    for r in rows:
        result.setdefault(r["endpoint"], {})[r["method"]] = json.loads(r["response"])
    return result


def db_upsert_mock(
    proxy_id: str, endpoint: str, method: str, response: dict
) -> dict | None:
    """Insert or update a mock. Returns the old mock response if it existed."""
    db = _get_db()

    proxy = db.execute(
        "SELECT 1 FROM proxies WHERE identifier = ?", (proxy_id,)
    ).fetchone()
    if not proxy:
        return None

    old_row = db.execute(
        "SELECT response FROM mocks WHERE proxy_id = ? AND endpoint = ? AND method = ?",
        (proxy_id, endpoint, method),
    ).fetchone()
    old_mock = json.loads(old_row["response"]) if old_row else None

    db.execute(
        "INSERT INTO mocks (proxy_id, endpoint, method, response) VALUES (?, ?, ?, ?) "
        "ON CONFLICT(proxy_id, endpoint, method) DO UPDATE SET "
        "response = excluded.response, updated_at = datetime('now')",
        (proxy_id, endpoint, method, json.dumps(response)),
    )
    db.commit()
    logger.info("[DB] Mock upserted: %s %s %s", proxy_id, method, endpoint)
    return old_mock


def db_delete_mock(proxy_id: str, endpoint: str, method: str) -> dict | None:
    """Delete a specific mock. Returns the deleted mock data or None."""
    db = _get_db()
    old_row = db.execute(
        "SELECT response FROM mocks WHERE proxy_id = ? AND endpoint = ? AND method = ?",
        (proxy_id, endpoint, method),
    ).fetchone()
    if not old_row:
        return None
    deleted_mock = json.loads(old_row["response"])
    db.execute(
        "DELETE FROM mocks WHERE proxy_id = ? AND endpoint = ? AND method = ?",
        (proxy_id, endpoint, method),
    )
    db.commit()
    logger.info("[DB] Mock deleted: %s %s %s", proxy_id, method, endpoint)
    return deleted_mock


def db_delete_proxy(identifier: str) -> bool:
    """Delete a proxy and all its mocks (cascade). Returns True if found."""
    db = _get_db()
    cursor = db.execute(
        "DELETE FROM proxies WHERE identifier = ?", (identifier,)
    )
    db.commit()
    if cursor.rowcount > 0:
        logger.info("[DB] Proxy '%s' deleted with all mocks", identifier)
    return cursor.rowcount > 0


def db_list_proxies() -> list[dict]:
    """Return a list of all proxies with mock counts."""
    db = _get_db()
    rows = db.execute(
        """
        SELECT p.identifier, p.api_domain, p.created_at,
               COUNT(m.id) AS mock_count
        FROM proxies p
        LEFT JOIN mocks m ON m.proxy_id = p.identifier
        GROUP BY p.identifier
        ORDER BY p.created_at DESC
        """
    ).fetchall()
    return [dict(r) for r in rows]


def db_get_proxy_domain(identifier: str) -> str | None:
    """Quick lookup for just the api_domain."""
    db = _get_db()
    row = db.execute(
        "SELECT api_domain FROM proxies WHERE identifier = ?", (identifier,)
    ).fetchone()
    return row["api_domain"] if row else None


def db_clone_proxy(source_id: str, target_id: str) -> dict | None:
    """Clone a proxy and all its mocks. Returns new proxy info or None."""
    db = _get_db()
    source = db.execute(
        "SELECT api_domain FROM proxies WHERE identifier = ?", (source_id,)
    ).fetchone()
    if not source:
        return None

    db.execute(
        "INSERT INTO proxies (identifier, api_domain) VALUES (?, ?) "
        "ON CONFLICT(identifier) DO UPDATE SET api_domain = excluded.api_domain",
        (target_id, source["api_domain"]),
    )
    db.execute("DELETE FROM mocks WHERE proxy_id = ?", (target_id,))
    db.execute(
        "INSERT INTO mocks (proxy_id, endpoint, method, response) "
        "SELECT ?, endpoint, method, response FROM mocks WHERE proxy_id = ?",
        (target_id, source_id),
    )
    db.commit()

    mock_count = db.execute(
        "SELECT COUNT(*) as c FROM mocks WHERE proxy_id = ?", (target_id,)
    ).fetchone()["c"]
    logger.info("[DB] Cloned '%s' -> '%s' (%d mocks)", source_id, target_id, mock_count)
    return {
        "identifier": target_id,
        "api_domain": source["api_domain"],
        "mock_count": mock_count,
    }


# ---------------------------------------------------------------------------
# DB helper functions — Request History
# ---------------------------------------------------------------------------


def db_log_request(
    proxy_id: str, endpoint: str, method: str,
    req_headers: dict | None, req_body: str | None, query_params: str | None,
    resp_status: int | None, resp_body: str | None,
    source: str, duration_ms: int | None,
):
    """Log a proxy request to the history table."""
    db = _get_db()
    db.execute(
        "INSERT INTO request_history "
        "(proxy_id, endpoint, method, request_headers, request_body, query_params, "
        " response_status, response_body, source, duration_ms) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (
            proxy_id, endpoint, method,
            json.dumps(req_headers) if req_headers else None,
            req_body[:2000] if req_body else None,
            query_params,
            resp_status,
            resp_body[:2000] if resp_body else None,
            source, duration_ms,
        ),
    )
    # Trim old entries
    db.execute(
        "DELETE FROM request_history WHERE proxy_id = ? AND id NOT IN "
        "(SELECT id FROM request_history WHERE proxy_id = ? ORDER BY id DESC LIMIT ?)",
        (proxy_id, proxy_id, REQUEST_HISTORY_LIMIT),
    )
    db.commit()


def db_get_request_history(proxy_id: str, limit: int = 50) -> list[dict]:
    """Get recent request history for a proxy."""
    db = _get_db()
    rows = db.execute(
        "SELECT * FROM request_history WHERE proxy_id = ? ORDER BY id DESC LIMIT ?",
        (proxy_id, limit),
    ).fetchall()
    return [dict(r) for r in rows]


def db_clear_request_history(proxy_id: str) -> int:
    """Clear all history for a proxy. Returns count deleted."""
    db = _get_db()
    cursor = db.execute(
        "DELETE FROM request_history WHERE proxy_id = ?", (proxy_id,)
    )
    db.commit()
    return cursor.rowcount


# ---------------------------------------------------------------------------
# DB helper functions — Mock Sequences
# ---------------------------------------------------------------------------


def db_get_and_increment_sequence(proxy_id: str, endpoint: str, method: str) -> int:
    """Get current call count and increment it. Returns the count BEFORE increment."""
    db = _get_db()
    row = db.execute(
        "SELECT call_count FROM mock_sequences "
        "WHERE proxy_id = ? AND endpoint = ? AND method = ?",
        (proxy_id, endpoint, method),
    ).fetchone()

    if row:
        count = row["call_count"]
        db.execute(
            "UPDATE mock_sequences SET call_count = call_count + 1 "
            "WHERE proxy_id = ? AND endpoint = ? AND method = ?",
            (proxy_id, endpoint, method),
        )
    else:
        count = 0
        db.execute(
            "INSERT INTO mock_sequences (proxy_id, endpoint, method, call_count) "
            "VALUES (?, ?, ?, 1)",
            (proxy_id, endpoint, method),
        )
    db.commit()
    return count


def db_reset_sequence(proxy_id: str, endpoint: str | None = None) -> int:
    """Reset sequence counters. If endpoint is None, reset all for proxy."""
    db = _get_db()
    if endpoint:
        cursor = db.execute(
            "DELETE FROM mock_sequences WHERE proxy_id = ? AND endpoint = ?",
            (proxy_id, endpoint),
        )
    else:
        cursor = db.execute(
            "DELETE FROM mock_sequences WHERE proxy_id = ?", (proxy_id,)
        )
    db.commit()
    return cursor.rowcount


# ---------------------------------------------------------------------------
# DB helper functions — Per-proxy State (for dbget resolver)
# ---------------------------------------------------------------------------


def db_get_state(proxy_id: str) -> dict:
    """Return the per-proxy state dict, or {} if none stored."""
    db = _get_db()
    row = db.execute(
        "SELECT data FROM mock_state WHERE proxy_id = ?", (proxy_id,)
    ).fetchone()
    if not row:
        return {}
    try:
        parsed = json.loads(row["data"])
        return parsed if isinstance(parsed, dict) else {}
    except (json.JSONDecodeError, TypeError):
        logger.warning("[STATE] Corrupt JSON for proxy '%s' — returning {}", proxy_id)
        return {}


def db_set_state(proxy_id: str, data: dict) -> None:
    """Replace the per-proxy state with `data`."""
    db = _get_db()
    db.execute(
        "INSERT INTO mock_state (proxy_id, data) VALUES (?, ?) "
        "ON CONFLICT(proxy_id) DO UPDATE SET "
        "data = excluded.data, updated_at = datetime('now')",
        (proxy_id, json.dumps(data)),
    )
    db.commit()
    logger.info("[STATE] Replaced for proxy '%s' (%d top-level keys)", proxy_id, len(data))


def db_merge_state(proxy_id: str, patch: dict) -> dict:
    """Shallow-merge `patch` into the per-proxy state. Returns the merged result.

    Race-safe within a single SQLite process via the immediate-transaction
    semantics; concurrent callers still race across read/write — last writer
    wins at the key level.
    """
    current = db_get_state(proxy_id)
    merged = {**current, **patch}
    db_set_state(proxy_id, merged)
    return merged


def db_clear_state(proxy_id: str) -> bool:
    """Drop the per-proxy state row. Returns True if something was deleted."""
    db = _get_db()
    cursor = db.execute(
        "DELETE FROM mock_state WHERE proxy_id = ?", (proxy_id,)
    )
    db.commit()
    if cursor.rowcount > 0:
        logger.info("[STATE] Cleared for proxy '%s'", proxy_id)
    return cursor.rowcount > 0


def _get_state_for_resolver(proxy_id: str | None) -> dict:
    """dbget() fetch helper. Robust to being called outside a Flask request
    context (e.g. unit tests) — returns {} rather than raising."""
    if not proxy_id:
        return {}
    try:
        return db_get_state(proxy_id)
    except (RuntimeError, sqlite3.Error) as exc:
        logger.debug("[STATE] dbget: no DB context (%s)", exc)
        return {}


# ---------------------------------------------------------------------------
# Rate Limiting (in-memory, thread-safe)
# ---------------------------------------------------------------------------

_rate_limits: dict[str, list[float]] = {}
_rate_lock = threading.Lock()

RATE_LIMIT_WINDOW = int(os.environ.get("PED_RATE_LIMIT_WINDOW", "60"))  # seconds
RATE_LIMIT_MAX = int(os.environ.get("PED_RATE_LIMIT_MAX", "0"))  # 0 = disabled


def check_rate_limit(identifier: str) -> bool:
    """Returns True if the request is allowed, False if rate limited."""
    if RATE_LIMIT_MAX <= 0:
        return True

    now = time.time()
    cutoff = now - RATE_LIMIT_WINDOW

    with _rate_lock:
        timestamps = _rate_limits.get(identifier, [])
        timestamps = [t for t in timestamps if t > cutoff]

        if len(timestamps) >= RATE_LIMIT_MAX:
            _rate_limits[identifier] = timestamps
            return False

        timestamps.append(now)
        _rate_limits[identifier] = timestamps
        return True


def get_rate_limit_info(identifier: str) -> dict:
    """Return current rate limit status for an identifier."""
    now = time.time()
    cutoff = now - RATE_LIMIT_WINDOW
    with _rate_lock:
        timestamps = _rate_limits.get(identifier, [])
        timestamps = [t for t in timestamps if t > cutoff]
    return {
        "identifier": identifier,
        "window_seconds": RATE_LIMIT_WINDOW,
        "max_requests": RATE_LIMIT_MAX if RATE_LIMIT_MAX > 0 else "unlimited",
        "current_count": len(timestamps),
        "remaining": max(0, RATE_LIMIT_MAX - len(timestamps)) if RATE_LIMIT_MAX > 0 else "unlimited",
    }


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def log_access(f):
    @wraps(f)
    def decorated(*args, **kwargs):
        logger.info(
            "%-6s %s  from %s", request.method, request.path, request.remote_addr
        )
        return f(*args, **kwargs)

    return decorated


def _is_authenticated() -> bool:
    """Check if the current request is authenticated via session or Bearer token."""
    # No password set = everything open
    if not UI_PASSWORD and not API_TOKEN:
        return True
    # Session-based auth (UI)
    if session.get("authenticated"):
        return True
    # Bearer token auth (API)
    if API_TOKEN:
        auth = request.headers.get("Authorization", "")
        if auth == f"Bearer {API_TOKEN}":
            return True
    return False


def require_auth(f):
    """Gate that checks session cookie OR Bearer token."""

    @wraps(f)
    def decorated(*args, **kwargs):
        if _is_authenticated():
            return f(*args, **kwargs)
        # For API requests, return 401 JSON
        if request.is_json or request.headers.get("Accept") == "application/json":
            return jsonify({"error": "Unauthorized"}), 401
        # For browser requests, redirect to login
        return redirect(url_for("login_page", next=request.path))

    return decorated


def require_login(f):
    """Gate for UI pages — redirects to login if not authenticated."""

    @wraps(f)
    def decorated(*args, **kwargs):
        if _is_authenticated():
            return f(*args, **kwargs)
        return redirect(url_for("login_page", next=request.path))

    return decorated


def _is_domain_allowed(domain: str) -> bool:
    """Return True when the domain is on the allowlist (or no allowlist is set).

    Matching is exact-host OR dot-boundary suffix so that an allowlist entry of
    'example.com' matches 'example.com' and 'api.example.com' but NOT
    'evil-example.com'.
    """
    if not ALLOWED_PROXY_DOMAINS:
        return True
    parsed = urlparse(domain if "://" in (domain or "") else f"//{domain}", scheme="")
    host = (parsed.hostname or "").lower()
    if not host:
        logger.warning("[ALLOWLIST] Rejecting domain with no parseable host: %r", domain)
        return False
    for allowed in ALLOWED_PROXY_DOMAINS:
        a = allowed.lower().lstrip(".")
        if not a:
            continue
        if host == a or host.endswith("." + a):
            return True
    logger.warning("[ALLOWLIST] Host %r not in allowlist %s", host, ALLOWED_PROXY_DOMAINS)
    return False


# ---------------------------------------------------------------------------
# Encryption helper
# ---------------------------------------------------------------------------


class EncryptHelper:
    @staticmethod
    def convert_json_to_string(data: dict) -> str:
        ordered = collections.OrderedDict(sorted(data.items()))
        return json.dumps(ordered, separators=(",", ":"))

    @staticmethod
    def encrypt(secret: str, enc_iv: str, msg: str) -> dict:
        key = secret.encode("utf-8")
        iv = b64decode(enc_iv)
        cipher = AES.new(key, AES.MODE_CBC, iv)
        ct_bytes = cipher.encrypt(pad(msg.encode("utf-8"), AES.block_size))
        return {
            "iv": b64encode(cipher.iv).decode("utf-8"),
            "gateway_secret": b64encode(ct_bytes).decode("utf-8"),
        }

    @staticmethod
    def decrypt(secret: str, enc_iv: str, data: dict) -> dict:
        iv = b64decode(data.get("iv", enc_iv))
        ct = b64decode(data["gateway_secret"])
        cipher = AES.new(secret.encode("utf-8"), AES.MODE_CBC, iv)
        dec_data = unpad(cipher.decrypt(ct), AES.block_size)
        return json.loads(dec_data.decode("utf-8"))


# ---------------------------------------------------------------------------
# JSON extraction / normalisation
# ---------------------------------------------------------------------------


def extract_jsons_from_string(s: str) -> list[str]:
    jsons = []
    start_positions = []
    open_count = 0
    openers = {"{", "["}
    closers = {"}", "]"}

    for i, c in enumerate(s):
        if c in openers:
            if open_count == 0:
                start_positions.append(i)
            open_count += 1
        elif c in closers:
            open_count -= 1
            if open_count == 0 and start_positions:
                start_idx = start_positions.pop()
                jsons.append(s[start_idx : i + 1])
    return jsons


_ESCAPE_REPLACEMENTS = [
    ("\\/", "/"),
    ("\\'", "'"),
    ('\\"', '"'),
    ('"{', "{"),
    ('}"', "}"),
    ("'{", "{"),
    ("}'", "}"),
]

_PYTHON_LITERAL_MAP = [
    ("True", "true"),
    ("False", "false"),
    ("None", "null"),
]


def normalize_escape_sequences(s: str) -> str:
    if not isinstance(s, str):
        return s
    for old, new in _ESCAPE_REPLACEMENTS:
        s = s.replace(old, new)
    for old, new in _PYTHON_LITERAL_MAP:
        s = s.replace(old, new)
    s = s.replace("'", '"')
    return s


# ---------------------------------------------------------------------------
# Path matching helpers
# ---------------------------------------------------------------------------


def extract_path_param(prefix: str, url: str | None) -> str | None:
    if url is None:
        return None
    try:
        if any(c.isalpha() for c in prefix):
            if "_" in prefix:
                base_prefix, idx_str = prefix.split("_", 1)
                try:
                    index = int(idx_str) - 1
                except ValueError:
                    return None
                matches = re.findall(rf"/{re.escape(base_prefix)}/([^/]+)", url)
                return matches[index] if 0 <= index < len(matches) else None
            else:
                matches = re.findall(rf"/{re.escape(prefix)}/([^/]+)", url)
                return matches[0] if matches else None
        elif prefix.isdigit():
            parsed = urlparse(url)
            segments = parsed.path.strip("/").split("/")
            index = int(prefix) - 1
            return segments[index] if 0 <= index < len(segments) else None
        return None
    except Exception as exc:
        logger.warning("extract_path_param error: %s", exc)
        return None


def path_to_regex(path_pattern: str) -> re.Pattern:
    placeholder = "\x00PARAM\x00"
    temp = re.sub(r"<\w+>", placeholder, path_pattern)
    temp = re.escape(temp)
    path_regex = temp.replace(re.escape(placeholder), r"[^/]+")
    return re.compile(f"^{path_regex}$")


def match_path(mock_endpoints: dict, actual_path: str) -> str | None:
    for pattern in mock_endpoints:
        if path_to_regex(pattern).match(actual_path):
            return pattern
    return None


# ---------------------------------------------------------------------------
# API class — multi-content-type forwarding with curl logging
# ---------------------------------------------------------------------------


class API:
    """Captures a Flask request and forwards it to a target URL,
    handling JSON, form-encoded, multipart, and raw body types.
    Logs the equivalent curl command for debugging."""

    _HOP_BY_HOP = frozenset({
        'host', 'content-length', 'transfer-encoding',
        'connection', 'keep-alive', 'upgrade',
    })
    _RESPONSE_HOP = frozenset({
        'content-encoding', 'content-length', 'transfer-encoding',
        'connection', 'keep-alive',
    })

    def __init__(self, flask_request, api_url):
        self.method = flask_request.method
        self.url = api_url
        self.headers = {
            k: v for k, v in flask_request.headers.items()
            if k.lower() not in self._HOP_BY_HOP
        }
        if 'Accept' not in self.headers:
            self.headers['Accept'] = '*/*'
        self.params = flask_request.args.to_dict(flat=False)
        self.params = {
            k: v[0] if len(v) == 1 else v
            for k, v in self.params.items()
        }
        self.content_type = flask_request.content_type or ''
        self.body = self._parse_body(flask_request)

    def _parse_body(self, flask_request):
        raw = flask_request.get_data()
        if not raw:
            return None
        if 'application/json' in self.content_type:
            return {'json': flask_request.json or {}}
        if 'application/x-www-form-urlencoded' in self.content_type:
            return {'data': flask_request.form.to_dict()}
        if 'multipart/form-data' in self.content_type:
            files = {}
            for k, f in flask_request.files.items():
                files[k] = (f.filename, f.stream, f.content_type)
            return {'files': files, 'data': flask_request.form.to_dict()}
        return {'data': raw, 'headers': {**self.headers, 'Content-Type': self.content_type}}

    def _to_curl(self):
        parts = [f"curl -X {self.method}"]
        url = self.url
        if self.params:
            qs = '&'.join(
                f"{k}={v}" if isinstance(v, str) else '&'.join(f"{k}={i}" for i in v)
                for k, v in self.params.items()
            )
            url += ('&' if '?' in url else '?') + qs
        parts.append(f"'{url}'")
        for k, v in self.headers.items():
            parts.append(f"-H '{k}: {v}'")
        if self.body:
            if 'json' in self.body:
                parts.append(f"-d '{json.dumps(self.body['json'])}'")
            elif 'data' in self.body and isinstance(self.body['data'], dict):
                parts.append(f"-d '{json.dumps(self.body['data'])}'")
            elif 'data' in self.body and isinstance(self.body['data'], bytes):
                parts.append(f"-d '<{len(self.body['data'])} bytes>'")
            if 'files' in self.body:
                for k in self.body['files']:
                    parts.append(f"-F '{k}=@...'")
        return ' \\\n  '.join(parts)

    def forward(self):
        kwargs = {
            'headers': self.headers,
            'params': self.params,
            'timeout': FORWARD_TIMEOUT,
            'allow_redirects': True,
        }
        if self.body:
            kwargs.update(self.body)

        logger.info("[FORWARD] %s %s content-type=%s", self.method, self.url, self.content_type)
        logger.info("[CURL] %s", self._to_curl())
        if self.body and 'json' in self.body:
            logger.debug("[FORWARD] Request body: %s", json.dumps(self.body['json'])[:500])

        start = time.time()
        response = http_requests.request(self.method, self.url, **kwargs)
        duration_ms = int((time.time() - start) * 1000)

        logger.info("[FORWARD] %s %s -> %s (%s bytes, %dms) response-type=%s",
                     self.method, self.url, response.status_code,
                     len(response.content), duration_ms,
                     response.headers.get('Content-Type', 'unknown'))
        if response.status_code >= 400:
            logger.warning("[FORWARD] Upstream error %s: %s", response.status_code, response.text[:300])

        return self._build_response(response), duration_ms, response

    def _build_response(self, response):
        pass_headers = {
            k: v for k, v in response.headers.items()
            if k.lower() not in self._RESPONSE_HOP
        }

        if not response.content:
            resp = Response(status=response.status_code)
            for k, v in pass_headers.items():
                resp.headers[k] = v
            return resp

        content_type = response.headers.get('Content-Type', 'text/plain')

        if 'application/json' in content_type:
            try:
                return jsonify(response.json()), response.status_code
            except ValueError:
                pass

        resp = Response(
            response.content,
            status=response.status_code,
            content_type=content_type,
        )
        for k, v in pass_headers.items():
            if k.lower() != 'content-type':
                resp.headers[k] = v
        return resp


# ---------------------------------------------------------------------------
# MockMatcher — structured mock lookup with query string variants
# ---------------------------------------------------------------------------


class MockMatcher:
    """Finds matching mock responses by trying multiple endpoint variants."""

    def __init__(self, mock_requests, endpoint, query_string, api_url):
        self.mock_requests = mock_requests
        self.variants = self._build_variants(endpoint, query_string, api_url)

    def _build_variants(self, endpoint, query_string, api_url):
        qs_suffix = '?' + query_string if query_string else ''
        variants = [
            endpoint + qs_suffix,
            endpoint,
            '/' + endpoint + qs_suffix,
            '/' + endpoint,
            api_url + qs_suffix,
            api_url,
        ]
        seen = set()
        deduped = []
        for v in variants:
            if v and v not in seen:
                seen.add(v)
                deduped.append(v)
        return deduped

    def find(self, method):
        """Return (mock_key, mock_data) or (None, None).

        Lookup order per variant:
          1. exact path + exact method
          2. exact path + '*'  (any-method registration, e.g. via POST /mock/<id>/<path>)
          3. pattern path + exact method
          4. pattern path + '*'
        """
        methods_to_try = [method, "*"] if method != "*" else ["*"]

        for variant in self.variants:
            mock_methods = self.mock_requests.get(variant)
            if not mock_methods:
                continue
            for m in methods_to_try:
                if mock_methods.get(m):
                    logger.info("[MOCK HIT] %s matched exact key '%s' (stored method=%s)",
                                method, variant, m)
                    return variant, mock_methods[m]

        for mock_key, mock_methods in self.mock_requests.items():
            if '<' not in mock_key:
                continue
            regex = path_to_regex(mock_key)
            for variant in self.variants:
                if not regex.match(variant):
                    continue
                for m in methods_to_try:
                    if mock_methods.get(m):
                        logger.info("[MOCK HIT] %s matched pattern key '%s' via '%s' (stored method=%s)",
                                    method, mock_key, variant, m)
                        return mock_key, mock_methods[m]

        logger.info("[MOCK MISS] %s no match found, tried: %s", method, self.variants)
        return None, None


# ---------------------------------------------------------------------------
# Safe mock-data value generators
# ---------------------------------------------------------------------------

_SAFE_GENERATORS = {
    "upper": lambda arg: "".join(random.choices(string.ascii_uppercase, k=int(arg))),
    "lower": lambda arg: "".join(random.choices(string.ascii_lowercase, k=int(arg))),
    "chars": lambda arg: "".join(random.choices(string.ascii_letters, k=int(arg))),
    "digit": lambda arg: "".join(random.choices(string.digits, k=int(arg))),
}


def _safe_alnum(arg: str) -> str:
    arg = arg.strip()
    if arg.startswith("[") and arg.endswith("]"):
        try:
            parts = json.loads(arg)
        except json.JSONDecodeError:
            raise ValueError(f"alnum() invalid list literal: {arg}")
    else:
        parts = [int(x.strip()) for x in arg.split(",")]

    if len(parts) % 2 != 0:
        raise ValueError("alnum() requires an even number of arguments")
    result = []
    for i in range(0, len(parts), 2):
        result.append("".join(random.choices(string.ascii_letters, k=int(parts[i]))))
        result.append("".join(random.choices(string.digits, k=int(parts[i + 1]))))
    return "".join(result)


# ---------------------------------------------------------------------------
# Safe snippet evaluator (sandboxed via simpleeval)
# ---------------------------------------------------------------------------

# Note: isinstance/type are rejected by current simpleeval versions
# ("really bad idea") even in compound mode; keep them out.
_SNIPPET_FUNCTIONS = {
    "abs": abs, "int": int, "float": float, "str": str, "len": len,
    "min": min, "max": max, "round": round, "sum": sum, "sorted": sorted,
    "list": list, "dict": dict, "tuple": tuple, "bool": bool,
    "enumerate": enumerate, "zip": zip, "range": range,
    "map": map, "filter": filter, "any": any, "all": all,
}

_SNIPPET_MAX_LENGTH = 2000


def safe_eval_snippet(snippet: str, names: dict | None = None):
    """Evaluate a simpleeval expression with optional read-only request context.

    Caller-provided `names` (body/headers/params/url/now_ts) are surfaced to the
    expression so mocks can do e.g. ``snippet(body['amount'] * 1.18)``.

    Return type is preserved — `int`, `float`, `list`, `dict` etc. all pass
    through (previously everything was stringified). Mocks that relied on
    stringification should wrap explicitly: ``snippet(str(...))``.
    """
    snippet = snippet.strip()
    if not snippet:
        return ""
    if len(snippet) > _SNIPPET_MAX_LENGTH:
        raise ValueError(f"snippet() expression too long ({len(snippet)} chars)")

    evaluator = EvalWithCompoundTypes(functions=_SNIPPET_FUNCTIONS, names=names or {})
    try:
        return evaluator.eval(snippet)
    except Exception as exc:
        logger.warning("snippet() failed: %s — expr: %s", exc, snippet[:200])
        raise ValueError(f"snippet() evaluation error: {exc}") from exc


def _parse_resolver_args(arg_str: str, miss_fallback):
    """Split a resolver argument string into (field, default).

    Forms:
        "x"               -> ("x", miss_fallback)
        "x, y"            -> ("x", "y")
        'x, "value,comma"' -> ("x", "value,comma")
        "x, '123'"        -> ("x", "123")

    Default is always a string (surrounding quotes stripped). When no comma is
    present, `miss_fallback` is used — typically the original literal
    expression, so unresolved placeholders stay visible for debuggability.
    """
    idx = arg_str.find(",")
    if idx == -1:
        return arg_str.strip(), miss_fallback
    field = arg_str[:idx].strip()
    default = arg_str[idx + 1:].strip()
    if len(default) >= 2 and default[0] == default[-1] and default[0] in ('"', "'"):
        default = default[1:-1]
    return field, default


def _parse_int_arg(arg_str: str, default: int = 0) -> int:
    """Parse an optional signed integer argument (e.g. ``now(+3600)``)."""
    arg_str = arg_str.strip()
    if not arg_str:
        return default
    try:
        return int(arg_str)
    except ValueError:
        return default


def _resolve_value(value: str, header: dict, json_data: dict, params: dict,
                   url: str | None, proxy_id: str | None = None):
    """Resolve a single placeholder expression. Non-matching strings are returned as-is.

    Returned type depends on the resolver: dotted paths / ``body()`` /
    ``dbget()`` can return dicts / lists / numbers so templates compose
    naturally. ``proxy_id`` scopes ``dbget()`` lookups to the current proxy.
    """
    # --- Zero-arg resolvers (fast path) ---
    if value == "body()":
        return json_data
    if value == "now()":
        return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
    if value == "now_epoch()":
        return time.time()
    if value == "uuid()":
        return str(_uuid.uuid4())
    if value == "uuid_short()":
        return shortuuid.uuid()

    # --- Time-with-offset: now(+3600), now(-60) ---
    if value.startswith("now(") and value.endswith(")"):
        offset = _parse_int_arg(value[4:-1])
        return (
            datetime.now(timezone.utc) + timedelta(seconds=offset)
        ).isoformat().replace("+00:00", "Z")
    if value.startswith("now_epoch(") and value.endswith(")"):
        return time.time() + _parse_int_arg(value[10:-1])

    # --- Request accessors (dotted path + optional default) ---
    if value.startswith("jsonget(") and value.endswith(")"):
        field, default = _parse_resolver_args(value[8:-1], value)
        resolved, found = _resolve_item_path(json_data, field)
        return resolved if found else default
    if value.startswith("headerget(") and value.endswith(")"):
        field, default = _parse_resolver_args(value[10:-1], value)
        return header.get(field, default)
    if value.startswith("paramget(") and value.endswith(")"):
        field, default = _parse_resolver_args(value[9:-1], value)
        return params.get(field, default)
    if value.startswith("pathparamget(") and value.endswith(")"):
        field, default = _parse_resolver_args(value[13:-1], value)
        result = extract_path_param(field, url)
        return result if result is not None else default

    # --- State accessor (per-proxy mock_state, dotted path + optional default) ---
    if value.startswith("dbget(") and value.endswith(")"):
        field, default = _parse_resolver_args(value[6:-1], value)
        state = _get_state_for_resolver(proxy_id)
        resolved, found = _resolve_item_path(state, field)
        return resolved if found else default

    # --- Generators (random) ---
    if value.startswith("alnum(") and value.endswith(")"):
        return _safe_alnum(value[6:-1])
    if value.startswith("snippet(") and value.endswith(")"):
        return safe_eval_snippet(
            value[8:-1],
            names={
                "body": json_data or {},
                "headers": header or {},
                "params": params or {},
                "state": _get_state_for_resolver(proxy_id),
                "url": url or "",
                "now_ts": time.time(),
            },
        )
    for name, gen in _SAFE_GENERATORS.items():
        if value.startswith(f"{name}(") and value.endswith(")"):
            return gen(value[len(name) + 1:-1])
    return value


def _resolve_to_int(raw, default: int, header: dict, json_data: dict,
                    params: dict, url: str | None, label: str,
                    proxy_id: str | None = None) -> int:
    """Resolve a raw value — literal or placeholder string — to an int.

    Used by ``_delay_ms`` and ``status_code`` so they can reference request
    fields, e.g. ``"_delay_ms": "jsonget(delay_ms)"`` or
    ``"status_code": "snippet(400 if body['fail'] else 200)"``.
    """
    if raw is None:
        return default
    if isinstance(raw, bool):
        return int(raw)
    if isinstance(raw, (int, float)):
        return int(raw)
    if isinstance(raw, str):
        resolved = _resolve_value(raw, header, json_data, params, url, proxy_id)
        if isinstance(resolved, bool):
            return int(resolved)
        if isinstance(resolved, (int, float)):
            return int(resolved)
        try:
            return int(str(resolved))
        except (TypeError, ValueError):
            logger.warning("[%s] cannot resolve %r to int — using %d", label, raw, default)
            return default
    return default


_FOREACH_TOKEN_RE = re.compile(r"\$(?:item|value)(?:\.[\w.]+)?|\$key|\$index")


def _resolve_item_path(item, path: str):
    """Walk a dotted path on item. Dict segments match keys; list segments must be
    numeric indices. Returns (value, found)."""
    if not path:
        return item, True
    cur = item
    for part in path.split("."):
        if isinstance(cur, dict):
            if part in cur:
                cur = cur[part]
            else:
                return None, False
        elif isinstance(cur, list):
            try:
                cur = cur[int(part)]
            except (ValueError, IndexError):
                return None, False
        else:
            return None, False
    return cur, True


def _match_foreach_where(conditions: list, item, key, index) -> bool:
    """Evaluate _foreach _where filters against the current element.

    Each condition is {source?, field?, operator, value?}.
    Supported sources: item / value (current element, optionally with dotted
    sub-field), key (dict key), index (iteration counter). Operators match the
    conditional-mock set (eq/neq/contains/exists/not_exists/gt/lt/
    starts_with/ends_with/regex plus equals/not_equals aliases).
    """
    if not conditions:
        return True
    _op_aliases = {"equals": "eq", "not_equals": "neq"}
    for cond in conditions:
        source = cond.get("source", "item")
        field = cond.get("field", "")
        op = _op_aliases.get(cond.get("operator", "eq"), cond.get("operator", "eq"))
        expected = cond.get("value")

        if source in ("item", "value"):
            if field:
                resolved, found = _resolve_item_path(item, field)
                actual = resolved if found else None
            else:
                actual = item
        elif source == "key":
            actual = key
        elif source == "index":
            actual = index
        else:
            logger.warning("[FOREACH] _where: unsupported source %r — rejecting item", source)
            return False

        try:
            if op == "eq" and str(actual) != str(expected):
                return False
            elif op == "neq" and str(actual) == str(expected):
                return False
            elif op == "contains" and (actual is None or str(expected) not in str(actual)):
                return False
            elif op == "exists" and actual is None:
                return False
            elif op == "not_exists" and actual is not None:
                return False
            elif op == "gt" and (actual is None or float(actual) <= float(expected)):
                return False
            elif op == "lt" and (actual is None or float(actual) >= float(expected)):
                return False
            elif op == "starts_with" and (actual is None or not str(actual).startswith(str(expected))):
                return False
            elif op == "ends_with" and (actual is None or not str(actual).endswith(str(expected))):
                return False
            elif op == "regex":
                if actual is None:
                    return False
                if not re.search(str(expected), str(actual)):
                    return False
        except (ValueError, TypeError, re.error) as exc:
            logger.warning("[FOREACH] _where: operator %s eval error (%s) — rejecting item", op, exc)
            return False
    return True


def _foreach_token_value(token: str, item, key, index, fallback):
    """Resolve one foreach token. Returns ``fallback`` if unresolvable so that
    typos (e.g. referencing a missing sub-field) stay visible in the output."""
    if token == "$item" or token == "$value":
        return item
    if token == "$key":
        return key if key is not None else fallback
    if token == "$index":
        return index if index is not None else fallback
    if token.startswith("$item."):
        path = token[6:]
    elif token.startswith("$value."):
        path = token[7:]
    else:
        return fallback
    val, found = _resolve_item_path(item, path)
    return val if found else fallback


def _substitute_item(node, item, key=None, index=None):
    """Walk node and substitute foreach placeholder tokens.

    Tokens (bare strings preserve type; embedded tokens are stringified):
      $item / $value                — current element
      $item.a.b.c / $value.a.b.c    — dotted sub-field on dict items (numeric
                                      segments index into list items)
      $key                          — current dict key (dict sources only)
      $index                        — 0-based iteration counter
    """
    if isinstance(node, dict):
        for k, v in list(node.items()):
            node[k] = _substitute_item(v, item, key, index)
        return node
    if isinstance(node, list):
        return [_substitute_item(x, item, key, index) for x in node]
    if isinstance(node, str):
        m = _FOREACH_TOKEN_RE.fullmatch(node)
        if m:
            return _foreach_token_value(m.group(0), item, key, index, fallback=node)

        def _repl(match):
            return str(_foreach_token_value(match.group(0), item, key, index, fallback=match.group(0)))

        return _FOREACH_TOKEN_RE.sub(_repl, node)
    return node


def resolve_mock_data(data, header=None, json_body=None, params=None, url=None, proxy_id=None):
    """Walk a mock response dict/list and resolve all placeholder values.

    Supports a special '_foreach'/'_template' dict marker that expands into a
    list — one entry per element in a named JSON-body field. Source may be a
    list OR a dict. Inside the template:

      $item / $value  — current element's value (type-preserving when whole
                        string, string interpolation when embedded)
      $key            — current dict key (dict sources only)

    Normal resolvers (jsonget/headerget/paramget/alnum/...) still run on the
    rendered template afterwards.

    List example:
        "balanceDetails": {
            "_foreach":  "filterKeys",
            "_template": {"availableBalance": "", "instrumentId": "$item"}
        }
    Request body {"filterKeys": ["RONE", "EGV"]} →
        "balanceDetails": [
            {"availableBalance": "", "instrumentId": "RONE"},
            {"availableBalance": "", "instrumentId": "EGV"}
        ]

    Dict example:
        "users": {
            "_foreach":  "userMap",
            "_template": {"id": "$key", "name": "$value"}
        }
    Request body {"userMap": {"u1": "Alice", "u2": "Bob"}} →
        "users": [
            {"id": "u1", "name": "Alice"},
            {"id": "u2", "name": "Bob"}
        ]

    If the named field is missing or not a list/dict the expansion yields [].
    """
    header = header or {}
    json_data = json_body or {}
    params = params or {}

    def process(node):
        # _random: pick one branch (optionally weighted). Evaluated before
        # _foreach so both can compose when nested inside each other.
        if isinstance(node, dict) and "_random" in node:
            choices = node.get("_random")
            if not isinstance(choices, list) or not choices:
                logger.warning("[RANDOM] no choices — returning {}")
                return {}
            weights = []
            for c in choices:
                if isinstance(c, dict) and "weight" in c:
                    try:
                        w = max(0.0, float(c["weight"]))
                    except (TypeError, ValueError):
                        w = 1.0
                else:
                    w = 1.0
                weights.append(w)
            total = sum(weights)
            picked_idx = len(choices) - 1
            if total > 0:
                r = random.uniform(0, total)
                cum = 0.0
                for i, w in enumerate(weights):
                    cum += w
                    if r <= cum:
                        picked_idx = i
                        break
            picked = choices[picked_idx]
            payload = picked.get("then") if isinstance(picked, dict) and "then" in picked else picked
            logger.info(
                "[RANDOM] picked index=%d of %d weight=%.3f",
                picked_idx, len(choices), weights[picked_idx] if weights else 0.0,
            )
            return process(copy.deepcopy(payload))

        # _foreach expansion runs before other processing so that token
        # substitution happens on a fresh copy of the template per element.
        if (
            isinstance(node, dict)
            and "_foreach" in node
            and "_template" in node
        ):
            template = node.get("_template")
            where_conditions = node.get("_where") or []
            limit_raw = node.get("_limit")
            as_dict = node.get("_as") == "dict"
            key_template = node.get("_key")
            source_raw = node.get("_foreach", "")

            # Parse limit defensively — bad value logs + ignores.
            limit = None
            if limit_raw is not None:
                try:
                    limit = int(limit_raw)
                    if limit < 0:
                        logger.warning("[FOREACH] negative _limit %r — ignoring", limit_raw)
                        limit = None
                except (TypeError, ValueError):
                    logger.warning("[FOREACH] invalid _limit %r — ignoring", limit_raw)

            # Resolve source:
            #   • list/dict value → already resolved (from nested $item.X substitution)
            #   • string → dotted path on json_body, then literal-key fallback
            if isinstance(source_raw, (list, dict)):
                source = source_raw
                found = True
            elif isinstance(source_raw, str):
                source, found = (
                    _resolve_item_path(json_data, source_raw)
                    if source_raw else (None, False)
                )
                if not found and source_raw in json_data:
                    source = json_data[source_raw]
                    found = True
            else:
                source, found = None, False

            if isinstance(source, list):
                kind = "list"
                raw_entries = list(enumerate(source))
                has_key = False
            elif isinstance(source, dict):
                kind = "dict"
                raw_entries = list(enumerate(source.items()))
                has_key = True
            else:
                empty = {} if as_dict else []
                logger.info(
                    "[FOREACH] %r missing or not a list/dict — returning %s",
                    source_raw, "{}" if as_dict else "[]",
                )
                return empty

            # _where filter
            filtered = []
            skipped = 0
            for idx, entry in raw_entries:
                if has_key:
                    k, v = entry
                else:
                    k, v = None, entry
                if where_conditions and not _match_foreach_where(where_conditions, v, k, idx):
                    skipped += 1
                    continue
                filtered.append((idx, k, v))

            # _limit slice
            if limit is not None:
                filtered = filtered[:limit]

            # Build output (list or dict)
            if as_dict:
                out_dict = {}
                for idx, k, v in filtered:
                    rendered = _substitute_item(copy.deepcopy(template), v, key=k, index=idx)
                    rendered = process(rendered)
                    if key_template is not None:
                        rk = _substitute_item(copy.deepcopy(key_template), v, key=k, index=idx)
                    elif has_key:
                        rk = k
                    else:
                        rk = idx
                    # JSON object keys must be strings.
                    out_dict[str(rk)] = rendered
                logger.info(
                    "[FOREACH] source=%s field=%r count=%d skipped=%d limit=%s out=dict",
                    kind, source_raw if isinstance(source_raw, str) else "<resolved>",
                    len(filtered), skipped, limit,
                )
                return out_dict

            out_list = []
            for idx, k, v in filtered:
                rendered = _substitute_item(copy.deepcopy(template), v, key=k, index=idx)
                out_list.append(process(rendered))
            logger.info(
                "[FOREACH] source=%s field=%r count=%d skipped=%d limit=%s out=list",
                kind, source_raw if isinstance(source_raw, str) else "<resolved>",
                len(filtered), skipped, limit,
            )
            return out_list

        if isinstance(node, dict):
            for key, value in list(node.items()):
                if isinstance(value, str):
                    node[key] = _resolve_value(value, header, json_data, params, url, proxy_id)
                elif isinstance(value, (dict, list)):
                    node[key] = process(value)
        elif isinstance(node, list):
            for i, item in enumerate(node):
                if isinstance(item, str):
                    node[i] = _resolve_value(item, header, json_data, params, url, proxy_id)
                elif isinstance(item, (dict, list)):
                    node[i] = process(item)
        return node

    return process(data)


# ---------------------------------------------------------------------------
# Mock conditionals
# ---------------------------------------------------------------------------


def _check_conditions(
    conditions: list[dict],
    headers: dict,
    json_body: dict,
    params: dict,
    path: str | None = None,
    method: str | None = None,
) -> bool:
    """Check if all conditions match.

    Each condition is {field, source?, operator, value?}.

    Supported sources (aliases accepted for UI compatibility):
      json / json_body  — field from request JSON body
      header            — named request header
      param / query_param — query parameter
      path              — URL path ('field' ignored)
      method            — HTTP method ('field' ignored)

    Supported operators:
      eq / equals, neq / not_equals, contains, exists, not_exists,
      gt, lt, starts_with, ends_with, regex.
    """
    _SOURCE_ALIASES = {
        "json_body": "json",
        "query_param": "param",
    }
    _OP_ALIASES = {
        "equals": "eq",
        "not_equals": "neq",
    }

    for cond in conditions:
        raw_source = cond.get("source", "json")
        source_type = _SOURCE_ALIASES.get(raw_source, raw_source)
        field = cond.get("field", "")
        raw_op = cond.get("operator", "eq")
        operator = _OP_ALIASES.get(raw_op, raw_op)
        expected = cond.get("value")

        if source_type == "header":
            actual = headers.get(field)
        elif source_type == "param":
            actual = params.get(field)
        elif source_type == "path":
            actual = path
        elif source_type == "method":
            actual = method
        else:
            actual = json_body.get(field)

        if operator == "eq" and str(actual) != str(expected):
            return False
        elif operator == "neq" and str(actual) == str(expected):
            return False
        elif operator == "contains" and (actual is None or str(expected) not in str(actual)):
            return False
        elif operator == "exists" and actual is None:
            return False
        elif operator == "not_exists" and actual is not None:
            return False
        elif operator == "gt" and (actual is None or float(actual) <= float(expected)):
            return False
        elif operator == "lt" and (actual is None or float(actual) >= float(expected)):
            return False
        elif operator == "starts_with" and (actual is None or not str(actual).startswith(str(expected))):
            return False
        elif operator == "ends_with" and (actual is None or not str(actual).endswith(str(expected))):
            return False
        elif operator == "regex":
            if actual is None:
                return False
            try:
                if not re.search(str(expected), str(actual)):
                    return False
            except re.error as exc:
                logger.warning("[CONDITIONAL] Invalid regex %r: %s", expected, exc)
                return False
    return True


# ---------------------------------------------------------------------------
# Resolve default secrets helper
# ---------------------------------------------------------------------------


def _resolve_secret(value: str) -> str:
    if value == "gringotts":
        if not DEFAULT_SECRET:
            raise ValueError("Default secret not configured (set PED_DEFAULT_SECRET)")
        return DEFAULT_SECRET
    return value


def _resolve_iv(value: str) -> str:
    if value == "gringotts":
        if not DEFAULT_ENC_IV:
            raise ValueError("Default IV not configured (set PED_DEFAULT_ENC_IV)")
        return DEFAULT_ENC_IV
    return value


# ---------------------------------------------------------------------------
# Routes — Login / Logout
# ---------------------------------------------------------------------------


@app.route("/login", methods=["GET", "POST"])
def login_page():
    """Login page. GET shows form, POST validates password."""
    # If no password set, no login needed
    if not UI_PASSWORD and not API_TOKEN:
        return redirect(url_for("index"))

    if request.method == "POST":
        password = request.form.get("password", "")
        # Accept UI_PASSWORD or API_TOKEN as valid password
        valid = False
        if UI_PASSWORD and password == UI_PASSWORD:
            valid = True
        if API_TOKEN and password == API_TOKEN:
            valid = True
        if valid:
            session["authenticated"] = True
            session.permanent = True
            next_url = request.args.get("next", "/")
            logger.info("[AUTH] Login successful from %s", request.remote_addr)
            return redirect(next_url)
        else:
            logger.warning("[AUTH] Failed login attempt from %s", request.remote_addr)
            return render_template("login.html", error="Invalid password"), 401

    return render_template("login.html", error=None)


@app.route("/logout")
def logout():
    session.clear()
    logger.info("[AUTH] Logout from %s", request.remote_addr)
    return redirect(url_for("login_page"))


# ---------------------------------------------------------------------------
# Routes — Health Check
# ---------------------------------------------------------------------------


@app.route("/health")
def health_check():
    """Health check endpoint for monitoring."""
    try:
        db = _get_db()
        db.execute("SELECT 1").fetchone()
        db_ok = True
    except Exception:
        db_ok = False

    status = "healthy" if db_ok else "degraded"
    code = 200 if db_ok else 503
    return jsonify({
        "status": status,
        "timestamp": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
        "database": "ok" if db_ok else "error",
        "version": "2.0.0",
    }), code


# ---------------------------------------------------------------------------
# Routes — Encrypt / Decrypt / Prettify
# ---------------------------------------------------------------------------


@app.route("/")
@log_access
def index():
    return render_template("index.html")


@app.route("/ped/encrypt", methods=["POST"])
@log_access
def encrypt_endpoint():
    try:
        data = request.get_json(force=True)
    except Exception:
        return jsonify({"error": "Invalid JSON provided"}), 400

    secret = data.get("secret", "")
    if not secret:
        return jsonify({"error": "Secret key is missing"}), 400
    secret = _resolve_secret(secret)

    enc_iv = data.get("enc_iv", "")
    if not enc_iv:
        return jsonify({"error": "enc_iv key is missing"}), 400
    enc_iv = _resolve_iv(enc_iv)

    normal_data = data.get("data", "")
    if not normal_data:
        return jsonify({"error": "data key is missing"}), 400

    try:
        encrypted = EncryptHelper.encrypt(secret, enc_iv, normal_data)
    except Exception as e:
        return jsonify({"error": f"Encryption failed: {e}"}), 400
    return jsonify({"encrypted": json.dumps(encrypted)})


@app.route("/ped/decrypt", methods=["POST"])
@log_access
def decrypt_endpoint():
    try:
        data = request.get_json(force=True)
    except Exception:
        return jsonify({"error": "Invalid JSON provided"}), 400

    secret = data.get("secret", "")
    if not secret:
        return jsonify({"error": "Secret key is missing"}), 400
    secret = _resolve_secret(secret)

    enc_iv = data.get("enc_iv", "")
    if not enc_iv:
        return jsonify({"error": "enc_iv key is missing"}), 400
    enc_iv = _resolve_iv(enc_iv)

    encrypted_data = data.get("encryptedData", "")
    if not encrypted_data:
        return jsonify({"error": "encryptedData key is missing"}), 400

    if isinstance(encrypted_data, str):
        try:
            encrypted_data = json.loads(encrypted_data)
        except json.JSONDecodeError:
            return jsonify({"error": "encryptedData is not valid JSON"}), 400

    try:
        decrypted = EncryptHelper.decrypt(secret, enc_iv, encrypted_data)
    except Exception as e:
        return jsonify({"error": f"Decryption failed: {e}"}), 400
    return jsonify({"decrypted": json.dumps(decrypted, indent=4)})


@app.route("/ped/prettify", methods=["POST"])
@log_access
def prettify():
    request_json = request.json
    if not request_json:
        return jsonify({"error": "Request body must be JSON"}), 400
    data_string = request_json.get("data", "")
    process_escape = request_json.get("processEscape", False)

    if process_escape:
        data_string = normalize_escape_sequences(data_string)

    json_strings = extract_jsons_from_string(data_string)
    parsed_results = []

    for json_str in json_strings:
        try:
            parsed_results.append(json.loads(json_str))
        except json.JSONDecodeError:
            pass

    output = "\n\n".join(json.dumps(item, indent=4) for item in parsed_results)
    return jsonify({"prettified": output})


# ---------------------------------------------------------------------------
# Routes — Proxy CRUD
# ---------------------------------------------------------------------------


@app.route("/proxy/create/", methods=["POST"])
@log_access
def create_proxy():
    request_data = request.json
    if not request_data:
        return jsonify({"error": "Request body must be JSON"}), 400
    api_domain = request_data.get("api_domain", "")

    if not _is_domain_allowed(api_domain):
        return jsonify({"error": "Domain not in allowlist"}), 403

    identifier = request_data.get("identifier") or shortuuid.uuid()
    old_mocks = db_create_proxy(identifier, api_domain)

    return jsonify({
        "identifier": identifier,
        "message": "api mocker created successfully",
        "old_mocks": old_mocks,
    })


@app.route("/proxy/mock/create/", methods=["POST"])
@log_access
def create_mock_proxy():
    request_data = request.json
    if not request_data:
        return jsonify({"error": "Request body must be JSON"}), 400
    proxy_identifier = request_data.get("proxy_identifier")
    end_point = request_data.get("end_point") or request_data.get("api_url")
    method = request_data.get("method")
    new_mock = request_data.get("mock")

    if not proxy_identifier or not end_point or not method:
        return jsonify({"error": "proxy_identifier, end_point, and method are required"}), 400

    if isinstance(new_mock, str):
        new_mock = json.loads(new_mock)

    old_mock = db_upsert_mock(proxy_identifier, end_point, method, new_mock)
    if old_mock is None and not db_get_proxy(proxy_identifier):
        return jsonify({"error": f"Proxy server not found for {proxy_identifier}"}), 404

    return jsonify({
        "proxy_identifier": proxy_identifier,
        "end_point": end_point,
        "method": method,
        "new_mock": new_mock,
        "old_mock": old_mock,
    })


@app.route("/proxy/mock/delete/", methods=["POST"])
@log_access
def delete_mock():
    request_data = request.json
    if not request_data:
        return jsonify({"error": "Request body must be JSON"}), 400
    proxy_id = request_data.get("proxy_identifier")
    endpoint = request_data.get("end_point")
    method = request_data.get("method")

    if not proxy_id or not endpoint or not method:
        return jsonify({"error": "proxy_identifier, end_point, and method are required"}), 400

    deleted_mock = db_delete_mock(proxy_id, endpoint, method)
    if deleted_mock is None:
        return jsonify({"error": "Mock not found"}), 404

    return jsonify({
        "proxy_identifier": proxy_id,
        "end_point": endpoint,
        "method": method,
        "deleted_mock": deleted_mock,
    })


@app.route("/proxy/delete/<identifier>/", methods=["DELETE"])
@log_access
@require_auth
def delete_proxy(identifier):
    deleted = db_delete_proxy(identifier)
    if not deleted:
        return jsonify({"error": "Proxy not found"}), 404
    return jsonify({"message": f"Proxy '{identifier}' and all its mocks deleted"})


@app.route("/proxy/list/", methods=["GET"])
@log_access
@require_auth
def list_proxies():
    proxies = db_list_proxies()
    return jsonify({"proxies": proxies})


@app.route("/proxy/get/<identifier>/", methods=["GET"])
@log_access
def get_proxy(identifier):
    proxy = db_get_proxy(identifier)
    if not proxy:
        return jsonify({}), 200
    return jsonify({
        "api_domain": proxy["api_domain"],
        "mocked_requests": proxy["mocked_requests"],
    }), 200


# ---------------------------------------------------------------------------
# Routes — Proxy Clone
# ---------------------------------------------------------------------------


@app.route("/proxy/clone/", methods=["POST"])
@log_access
@require_auth
def clone_proxy():
    """Clone a proxy and all its mocks to a new identifier."""
    request_data = request.json
    if not request_data:
        return jsonify({"error": "Request body must be JSON"}), 400

    # Accept both long-form (source_identifier/target_identifier) and short-form
    # (source/target) field names for compatibility with the management UI.
    source_id = request_data.get("source_identifier") or request_data.get("source")
    target_id = (
        request_data.get("target_identifier")
        or request_data.get("target")
        or shortuuid.uuid()
    )

    if not source_id:
        return jsonify({"error": "source_identifier is required"}), 400

    result = db_clone_proxy(source_id, target_id)
    if result is None:
        return jsonify({"error": f"Source proxy '{source_id}' not found"}), 404

    return jsonify({
        "message": f"Proxy '{source_id}' cloned to '{target_id}'",
        **result,
    })


# ---------------------------------------------------------------------------
# Routes — Import / Export
# ---------------------------------------------------------------------------


@app.route("/proxy/export/<identifier>/", methods=["GET"])
@log_access
@require_auth
def export_proxy(identifier):
    """Export a proxy and all its mocks as JSON."""
    proxy = db_get_proxy(identifier)
    if not proxy:
        return jsonify({"error": "Proxy not found"}), 404

    export_data = {
        "identifier": identifier,
        "api_domain": proxy["api_domain"],
        "mocked_requests": proxy["mocked_requests"],
        "exported_at": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
    }
    return jsonify(export_data)


@app.route("/proxy/export/all/", methods=["GET"])
@log_access
@require_auth
def export_all_proxies():
    """Export all proxies and their mocks."""
    proxies = db_list_proxies()
    result = {}
    for p in proxies:
        proxy = db_get_proxy(p["identifier"])
        if proxy:
            result[p["identifier"]] = {
                "api_domain": proxy["api_domain"],
                "mocked_requests": proxy["mocked_requests"],
            }
    return jsonify({
        "proxies": result,
        "count": len(result),
        "exported_at": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
    })


@app.route("/proxy/import/", methods=["POST"])
@log_access
@require_auth
def import_proxies():
    """Import proxies and mocks from JSON. Accepts single or bulk format."""
    request_data = request.json
    if not request_data:
        return jsonify({"error": "Request body must be JSON"}), 400

    imported = []
    errors = []

    # Single proxy format: {identifier, api_domain, mocked_requests}
    if "identifier" in request_data:
        items = {request_data["identifier"]: request_data}
    # Bulk format: {proxies: {id: {api_domain, mocked_requests}, ...}}
    elif "proxies" in request_data:
        items = request_data["proxies"]
    else:
        return jsonify({"error": "Expected 'identifier' or 'proxies' key"}), 400

    for identifier, data in items.items():
        try:
            api_domain = data.get("api_domain", "")
            if not _is_domain_allowed(api_domain):
                errors.append({"identifier": identifier, "error": "Domain not allowed"})
                continue

            db_create_proxy(identifier, api_domain)
            mock_count = 0
            for endpoint, methods in data.get("mocked_requests", {}).items():
                for method, response in methods.items():
                    db_upsert_mock(identifier, endpoint, method, response)
                    mock_count += 1

            imported.append({"identifier": identifier, "mock_count": mock_count})
        except Exception as e:
            errors.append({"identifier": identifier, "error": str(e)})

    return jsonify({
        "imported": imported,
        "errors": errors,
        "total_imported": len(imported),
    })


# ---------------------------------------------------------------------------
# Routes — Request History
# ---------------------------------------------------------------------------


@app.route("/proxy/history/<identifier>/", methods=["GET"])
@log_access
@require_auth
def get_history(identifier):
    """Get recent request history for a proxy."""
    limit = request.args.get("limit", 50, type=int)
    history = db_get_request_history(identifier, limit)
    # Parse JSON strings back to objects for readability
    for h in history:
        for field in ("request_headers", "request_body", "response_body"):
            if h.get(field):
                try:
                    h[field] = json.loads(h[field])
                except (json.JSONDecodeError, TypeError):
                    pass
    return jsonify({"proxy_id": identifier, "count": len(history), "history": history})


@app.route("/proxy/history/<identifier>/clear/", methods=["POST"])
@log_access
@require_auth
def clear_history(identifier):
    """Clear request history for a proxy."""
    count = db_clear_request_history(identifier)
    return jsonify({"message": f"Cleared {count} history entries for '{identifier}'"})


# ---------------------------------------------------------------------------
# Routes — Mock Sequences
# ---------------------------------------------------------------------------


@app.route("/proxy/sequence/reset/", methods=["POST"])
@log_access
def reset_sequence():
    """Reset mock sequence counters."""
    request_data = request.json
    if not request_data:
        return jsonify({"error": "Request body must be JSON"}), 400
    proxy_id = request_data.get("proxy_identifier")
    endpoint = request_data.get("end_point")  # optional
    if not proxy_id:
        return jsonify({"error": "proxy_identifier is required"}), 400
    count = db_reset_sequence(proxy_id, endpoint)
    return jsonify({"message": f"Reset {count} sequence counters"})


# ---------------------------------------------------------------------------
# Routes — Rate Limit Info
# ---------------------------------------------------------------------------


@app.route("/proxy/ratelimit/<identifier>/", methods=["GET"])
@log_access
def rate_limit_info(identifier):
    """Get current rate limit status for a proxy."""
    return jsonify(get_rate_limit_info(identifier))


# ---------------------------------------------------------------------------
# Routes — Per-proxy State (backing for the dbget() resolver)
# ---------------------------------------------------------------------------


@app.route("/proxy/state/<identifier>/", methods=["GET"])
@log_access
def get_proxy_state(identifier):
    """Return the current per-proxy state dict (empty if nothing stored)."""
    return jsonify({
        "proxy_id": identifier,
        "state": db_get_state(identifier),
    })


@app.route("/proxy/state/<identifier>/", methods=["PUT"])
@log_access
def set_proxy_state(identifier):
    """Replace the per-proxy state with the request body (must be a JSON object)."""
    data = request.get_json(silent=True)
    if not isinstance(data, dict):
        return jsonify({"error": "Request body must be a JSON object"}), 400
    db_set_state(identifier, data)
    return jsonify({
        "message": "State replaced",
        "proxy_id": identifier,
        "state": data,
    })


@app.route("/proxy/state/<identifier>/", methods=["PATCH"])
@log_access
def merge_proxy_state(identifier):
    """Shallow-merge the request body into the per-proxy state."""
    patch = request.get_json(silent=True)
    if not isinstance(patch, dict):
        return jsonify({"error": "Request body must be a JSON object"}), 400
    merged = db_merge_state(identifier, patch)
    return jsonify({
        "message": "State merged",
        "proxy_id": identifier,
        "state": merged,
    })


@app.route("/proxy/state/<identifier>/", methods=["DELETE"])
@log_access
def clear_proxy_state(identifier):
    """Drop the entire per-proxy state row."""
    cleared = db_clear_state(identifier)
    return jsonify({
        "message": "State cleared" if cleared else "No state to clear",
        "proxy_id": identifier,
    })


# ---------------------------------------------------------------------------
# Routes — Proxy Passthrough
# ---------------------------------------------------------------------------


_SUPPORTED_MOCK_METHODS = {"*", "GET", "POST", "PUT", "PATCH", "DELETE", "HEAD", "OPTIONS"}


@app.route(
    "/mock/<identifier>/<path:endpoint>",
    methods=["POST"],
)
@log_access
def register_mock_by_url(identifier, endpoint):
    """Register/update a mock by URL mirroring.

    Swap '/proxy/' for '/mock/' in the retrieval URL and POST the desired response
    body to register a static mock for that URL. Re-POST with a new body to update.

    Method pinning (optional):
      - Default: stored with method '*' — matches any retrieval method.
      - Override: send 'X-Mock-Method: GET' (or POST/PUT/PATCH/DELETE/HEAD/OPTIONS)
        to pin the mock to a specific HTTP method. A method-specific mock takes
        precedence over '*' at the same path during retrieval.

    The request body becomes the mock response. All existing mock shapes are
    supported (plain dict, list for sequencing, {status_code,body,headers},
    {conditions,responses,default}, placeholder resolvers inside values).
    """
    start_time = time.time()
    try:
        api_domain = db_get_proxy_domain(identifier)
        if api_domain is None:
            logger.warning("[MOCK REGISTER] Unknown proxy '%s'", identifier)
            return jsonify({"error": f"Proxy '{identifier}' not found"}), 404

        try:
            mock_body = request.get_json(force=True)
        except Exception as exc:
            logger.warning("[MOCK REGISTER] Invalid JSON (identifier=%s): %s", identifier, exc)
            return jsonify({"error": "Request body must be valid JSON"}), 400
        if mock_body is None:
            return jsonify({"error": "Request body must be JSON"}), 400

        # Method pinning via optional X-Mock-Method header; defaults to '*'
        # so a single registration matches any retrieval method.
        raw_method = (request.headers.get("X-Mock-Method") or "*").strip().upper()
        if raw_method not in _SUPPORTED_MOCK_METHODS:
            logger.warning(
                "[MOCK REGISTER] Unsupported X-Mock-Method: %r (identifier=%s)",
                raw_method, identifier,
            )
            return jsonify({
                "error": f"Unsupported X-Mock-Method: {raw_method!r}",
                "supported": sorted(_SUPPORTED_MOCK_METHODS),
            }), 400
        stored_method = raw_method

        query_string = request.query_string.decode("utf-8")

        # Storage key mirrors the /proxy/mock/create/ convention:
        # leading-slash path, plus ?query-string suffix when present.
        mock_key = endpoint if endpoint.startswith("/") else "/" + endpoint
        if query_string:
            mock_key = f"{mock_key}?{query_string}"

        old_mock = db_upsert_mock(identifier, mock_key, stored_method, mock_body)

        req_headers = {k: v for k, v in request.headers.items() if k.lower() != "host"}
        req_body = request.get_data(as_text=True)[:2000] or None
        duration_ms = int((time.time() - start_time) * 1000)
        db_log_request(
            identifier, endpoint, "POST", req_headers, req_body, query_string,
            200, json.dumps(mock_body)[:2000], "mock_register", duration_ms,
        )

        logger.info(
            "[MOCK REGISTER] identifier=%s key='%s' method=%s replaced_existing=%s bytes=%d",
            identifier, mock_key, stored_method, old_mock is not None,
            len(json.dumps(mock_body)),
        )
        return jsonify({
            "message": "Mock registered",
            "proxy_identifier": identifier,
            "end_point": mock_key,
            "method": stored_method,
            "new_mock": mock_body,
            "old_mock": old_mock,
        }), 200

    except Exception:
        logger.exception("[MOCK REGISTER] Unhandled error for identifier=%s", identifier)
        return jsonify({"error": "Internal server error"}), 500


@app.route(
    "/proxy/<identifier>/<path:endpoint>",
    methods=["GET", "POST", "PUT", "PATCH", "DELETE", "HEAD", "OPTIONS"],
)
@log_access
def proxy_request(identifier, endpoint):
    start_time = time.time()

    try:
        # --- Rate limiting ---
        if not check_rate_limit(identifier):
            logger.warning("[RATE LIMIT] %s exceeded rate limit", identifier)
            return jsonify({
                "error": "Rate limit exceeded",
                **get_rate_limit_info(identifier),
            }), 429

        api_domain = db_get_proxy_domain(identifier)
        if api_domain is None:
            return jsonify({"error": f"Proxy '{identifier}' not found"}), 404

        api_url = f"{api_domain.rstrip('/')}/{endpoint}"
        method = request.method
        query_string = request.query_string.decode('utf-8')

        logger.info("[PROXY] %s /%s/%s%s", method, identifier, endpoint,
                     '?' + query_string if query_string else '')

        # Capture request info for history
        req_headers = {k: v for k, v in request.headers.items() if k.lower() != 'host'}
        req_body = request.get_data(as_text=True)[:2000] or None

        # --- Redirect mode ---
        if identifier.endswith("_REDIRECT"):
            if not _is_domain_allowed(api_domain):
                return jsonify({"error": "Redirect target domain not allowed"}), 403
            target_url = api_url
            if request.query_string:
                target_url += f"?{request.query_string.decode()}"
            try:
                fwd_api = API(request, target_url)
                flask_resp, duration_ms, raw_resp = fwd_api.forward()
                db_log_request(
                    identifier, endpoint, method, req_headers, req_body, query_string,
                    raw_resp.status_code, raw_resp.text[:2000], "redirect", duration_ms,
                )
                return flask_resp
            except http_requests.exceptions.RequestException as exc:
                logger.error("Redirect forward failed: %s", exc)
                return jsonify({"error": "Redirect forward failed"}), 502

        # --- Mock lookup ---
        mock_requests = db_get_mocks_for_proxy(identifier)
        matcher = MockMatcher(mock_requests, endpoint, query_string, api_url)
        _, mock_data = matcher.find(method)

        if mock_data:
            headers = req_headers
            json_body = request.get_json(silent=True) or {}
            params = request.args.to_dict()

            mock_data_copy = copy.deepcopy(mock_data)

            # --- Mock sequencing ---
            # If mock is a list, pick response based on call count
            if isinstance(mock_data_copy, list):
                call_count = db_get_and_increment_sequence(identifier, endpoint, method)
                idx = call_count % len(mock_data_copy)
                logger.info("[SEQUENCE] %s %s call #%d -> index %d/%d",
                            method, endpoint, call_count + 1, idx, len(mock_data_copy))
                mock_data_copy = mock_data_copy[idx]

            # --- Conditional mocks ---
            # If mock has "conditions" + "responses", pick first matching
            if isinstance(mock_data_copy, dict) and "conditions" in mock_data_copy and "responses" in mock_data_copy:
                selected = None
                for case in mock_data_copy["responses"]:
                    case_conditions = case.get("when", [])
                    if _check_conditions(case_conditions, headers, json_body, params, path=endpoint, method=method):
                        selected = case.get("then", {})
                        logger.info("[CONDITIONAL] Matched condition: %s", case_conditions)
                        break
                if selected is None:
                    selected = mock_data_copy.get("default", {"error": "No condition matched"})
                    logger.info("[CONDITIONAL] No condition matched, using default")
                mock_data_copy = selected

            # --- Response delay (supports placeholder strings — e.g.
            #     "_delay_ms": "jsonget(delay_ms)" or "snippet(...)"). ---
            delay_raw = None
            if isinstance(mock_data_copy, dict):
                delay_raw = mock_data_copy.pop("_delay_ms", None)
            delay_ms = _resolve_to_int(
                delay_raw, 0, headers, json_body, params, api_url, "DELAY",
                proxy_id=identifier,
            )
            if delay_ms > 0:
                logger.info("[DELAY] Sleeping %dms before responding", delay_ms)
                time.sleep(delay_ms / 1000.0)

            # --- status_code + body structure ---
            if isinstance(mock_data_copy, dict) and "status_code" in mock_data_copy and "body" in mock_data_copy:
                status_code = _resolve_to_int(
                    mock_data_copy["status_code"], 200,
                    headers, json_body, params, api_url, "STATUS",
                    proxy_id=identifier,
                )
                # Response headers from mock
                resp_headers = mock_data_copy.get("headers", {})
                body = mock_data_copy["body"]
                processed = resolve_mock_data(
                    body, header=headers, json_body=json_body, params=params,
                    url=api_url, proxy_id=identifier,
                )
                duration_ms = int((time.time() - start_time) * 1000)
                db_log_request(
                    identifier, endpoint, method, req_headers, req_body, query_string,
                    status_code, json.dumps(processed)[:2000], "mock", duration_ms,
                )
                response = jsonify(processed), status_code
                # Apply custom response headers
                if resp_headers:
                    resp_obj = response[0] if isinstance(response, tuple) else response
                    for k, v in resp_headers.items():
                        resp_obj.headers[k] = v
                return response

            processed = resolve_mock_data(
                mock_data_copy, header=headers, json_body=json_body, params=params,
                url=api_url, proxy_id=identifier,
            )
            duration_ms = int((time.time() - start_time) * 1000)
            db_log_request(
                identifier, endpoint, method, req_headers, req_body, query_string,
                200, json.dumps(processed)[:2000], "mock", duration_ms,
            )
            return jsonify(processed), 200

        # --- SSRF guard ---
        if not _is_domain_allowed(api_domain):
            return jsonify({"error": "Target domain not allowed"}), 403

        # --- Forward to real API ---
        try:
            api = API(request, api_url)
            flask_resp, duration_ms, raw_resp = api.forward()
            db_log_request(
                identifier, endpoint, method, req_headers, req_body, query_string,
                raw_resp.status_code, raw_resp.text[:2000], "forward", duration_ms,
            )
            return flask_resp
        except http_requests.exceptions.Timeout:
            logger.error("[PROXY] Timeout forwarding %s %s", method, api_url)
            return jsonify({"error": f"Upstream timeout after {FORWARD_TIMEOUT}s"}), 504
        except http_requests.exceptions.ConnectionError as exc:
            logger.error("[PROXY] Connection error %s %s: %s", method, api_url, exc)
            return jsonify({"error": "Upstream connection failed"}), 502
        except http_requests.exceptions.RequestException as exc:
            logger.error("[PROXY] Request failed: %s", exc)
            return jsonify({"error": "Upstream request failed"}), 502

    except Exception:
        logger.exception("Unhandled error in proxy_request")
        return jsonify({"error": "Internal server error"}), 500


# ---------------------------------------------------------------------------
# Routes — UI
# ---------------------------------------------------------------------------


@app.route("/proxy/helper")
@log_access
def proxy_helper():
    return render_template("proxy_helper.html")


@app.route("/proxy/")
@log_access
def proxy_server_page():
    return render_template("proxy_server.html")


@app.route("/proxy/manage/")
@log_access
@require_login
def proxy_manage_page():
    """Protected management dashboard — proxy list, clone, import/export, history."""
    return render_template("proxy_manage.html")


# ---------------------------------------------------------------------------
# Error handler
# ---------------------------------------------------------------------------


from werkzeug.exceptions import HTTPException


@app.errorhandler(Exception)
def handle_exception(exc):
    if isinstance(exc, HTTPException):
        return exc
    logger.exception("Unhandled exception: %s", exc)
    return jsonify({"error": "An unexpected error occurred"}), 500


# ---------------------------------------------------------------------------
# Migrate from proxy_server.json (one-time)
# ---------------------------------------------------------------------------


def migrate_from_json(json_path: str):
    if not os.path.exists(json_path):
        return
    try:
        with open(json_path, "r") as f:
            data = json.load(f)
        if not data:
            return

        conn = sqlite3.connect(DB_PATH)
        count_proxies = 0
        count_mocks = 0
        for identifier, entry in data.items():
            api_domain = entry.get("api_domain", "")
            conn.execute(
                "INSERT OR IGNORE INTO proxies (identifier, api_domain) VALUES (?, ?)",
                (identifier, api_domain),
            )
            count_proxies += 1
            for endpoint, methods in entry.get("mocked_requests", {}).items():
                for method, response in methods.items():
                    conn.execute(
                        "INSERT OR IGNORE INTO mocks (proxy_id, endpoint, method, response) "
                        "VALUES (?, ?, ?, ?)",
                        (identifier, endpoint, method, json.dumps(response)),
                    )
                    count_mocks += 1
        conn.commit()
        conn.close()

        backup = json_path + ".migrated"
        os.rename(json_path, backup)
        logger.info(
            "Migrated %d proxies and %d mocks from %s → SQLite (backup: %s)",
            count_proxies, count_mocks, json_path, backup,
        )
    except Exception:
        logger.exception("Migration from %s failed", json_path)


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

init_db()
migrate_from_json(os.path.join(_BASE_DIR, "proxy_server.json"))

if __name__ == "__main__":
    port = int(os.environ.get("PED_PORT", 8000))
    app.run(host="0.0.0.0", port=port, debug=DEBUG)
