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
from werkzeug.exceptions import HTTPException

import requests as http_requests
import shortuuid
from pymongo import MongoClient
from pymongo.errors import PyMongoError


# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

DB_PATH = os.environ.get("PED_DB_PATH", os.path.join(_BASE_DIR, "pedapp.db"))
# MongoDB — optional, used only by raw_mongo_* snippet helpers
MONGO_DB = os.environ.get("PED_MONGO_DB", "pedapp")
_mongo_user = os.environ.get("PED_MONGO_USER", "")
_mongo_pass = os.environ.get("PED_MONGO_PASS", "")
_mongo_uri_raw = os.environ.get("PED_MONGO_URI", "mongodb://localhost:27017")
if _mongo_user and _mongo_pass:
    from urllib.parse import quote_plus
    MONGO_URI = f"mongodb+srv://{quote_plus(_mongo_user)}:{quote_plus(_mongo_pass)}@" + _mongo_uri_raw.split("@", 1)[-1]
else:
    MONGO_URI = _mongo_uri_raw
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

# CORS — comma-separated origins; empty=disabled, *=allow all
CORS_ORIGINS = os.environ.get("PED_CORS_ORIGINS", "")
# Environment variable resolver prefix — only vars matching this prefix are exposed
MOCK_ENV_PREFIX = os.environ.get("PED_MOCK_ENV_PREFIX", "MOCK_")
# Max state snapshots per proxy
MAX_SNAPSHOTS_PER_PROXY = int(os.environ.get("PED_MAX_SNAPSHOTS", "20"))

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
app.config["SESSION_COOKIE_HTTPONLY"] = True
app.config["SESSION_COOKIE_SAMESITE"] = "Lax"
app.config["PERMANENT_SESSION_LIFETIME"] = timedelta(days=1)


@app.after_request
def _set_security_headers(response):
    response.headers.setdefault("X-Frame-Options", "DENY")
    response.headers.setdefault("X-Content-Type-Options", "nosniff")
    response.headers.setdefault("Referrer-Policy", "strict-origin-when-cross-origin")

    # CORS headers
    cors_origins = CORS_ORIGINS
    # Per-proxy override: check state key _cors_origins if we're in a proxy route
    if hasattr(g, "_proxy_cors_origins"):
        cors_origins = g._proxy_cors_origins
    if cors_origins:
        origin = request.headers.get("Origin", "")
        if cors_origins.strip() == "*":
            response.headers["Access-Control-Allow-Origin"] = "*"
        elif origin:
            allowed = [o.strip() for o in cors_origins.split(",") if o.strip()]
            if origin in allowed:
                response.headers["Access-Control-Allow-Origin"] = origin
                response.headers["Vary"] = "Origin"
        response.headers.setdefault(
            "Access-Control-Allow-Methods", "GET, POST, PUT, PATCH, DELETE, HEAD, OPTIONS"
        )
        response.headers.setdefault(
            "Access-Control-Allow-Headers", "Content-Type, Authorization, X-Mock-Method"
        )
        response.headers.setdefault("Access-Control-Max-Age", "86400")
    return response


@app.route("/proxy/<identifier>/<path:endpoint>", methods=["OPTIONS"])
def _cors_preflight(identifier, endpoint):
    """Handle CORS preflight requests with 204 No Content."""
    logger.debug("[CORS] Preflight for /%s/%s", identifier, endpoint)
    return Response(status=204)

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
# MongoDB client — used by raw_mongo_* helpers only (not for state/users)
# ---------------------------------------------------------------------------

_mongo_client: MongoClient | None = None


def _get_mongo() -> MongoClient:
    global _mongo_client
    if _mongo_client is None:
        logger.info("[MONGO] Connecting to %s db=%s", MONGO_URI, MONGO_DB)
        _mongo_client = MongoClient(MONGO_URI, serverSelectionTimeoutMS=5000)
        logger.info("[MONGO] Client ready")
    return _mongo_client


# ---------------------------------------------------------------------------
# User management helpers (proxy_users — SQLite)
# ---------------------------------------------------------------------------


def create_proxy_user(proxy_id: str, username: str, password: str) -> None:
    """Upsert a user credential for a proxy."""
    try:
        db = _get_db()
        db.execute(
            "INSERT INTO proxy_users(proxy_id, username, password) VALUES(?,?,?)"
            " ON CONFLICT(proxy_id, username) DO UPDATE SET password=excluded.password",
            (proxy_id, username, password),
        )
        db.commit()
        logger.info("[USERS] Upserted proxy='%s' username='%s'", proxy_id, username)
    except Exception as exc:
        logger.error("[USERS] create_proxy_user error: %s", exc)
        raise


def list_proxy_users(proxy_id: str) -> list[dict]:
    """Return all users for a proxy (password excluded)."""
    try:
        db = _get_db()
        rows = db.execute(
            "SELECT proxy_id, username FROM proxy_users WHERE proxy_id=?", (proxy_id,)
        ).fetchall()
        return [dict(r) for r in rows]
    except Exception as exc:
        logger.error("[USERS] list_proxy_users error: %s", exc)
        return []


def delete_proxy_user(proxy_id: str, username: str) -> bool:
    """Delete a user. Returns True if found and deleted."""
    try:
        db = _get_db()
        cur = db.execute(
            "DELETE FROM proxy_users WHERE proxy_id=? AND username=?", (proxy_id, username)
        )
        db.commit()
        deleted = cur.rowcount > 0
        if deleted:
            logger.info("[USERS] Deleted proxy='%s' username='%s'", proxy_id, username)
        return deleted
    except Exception as exc:
        logger.error("[USERS] delete_proxy_user error: %s", exc)
        raise


def verify_proxy_user(proxy_id: str, username: str, password: str) -> bool:
    """Return True if username+password match a stored credential."""
    try:
        db = _get_db()
        row = db.execute(
            "SELECT password FROM proxy_users WHERE proxy_id=? AND username=?",
            (proxy_id, username),
        ).fetchone()
        if not row:
            logger.debug("[USERS] verify: no user proxy='%s' username='%s'", proxy_id, username)
            return False
        match = row["password"] == password
        logger.debug("[USERS] verify: proxy='%s' username='%s' match=%s", proxy_id, username, match)
        return match
    except Exception as exc:
        logger.warning("[USERS] verify_proxy_user error: %s", exc)
        return False


# ---------------------------------------------------------------------------
# Raw query helpers — MongoDB + SQLite
# ---------------------------------------------------------------------------


def _clean_mongo_doc(doc: dict) -> dict:
    """Remove non-serialisable fields (ObjectId) from a Mongo document."""
    return {k: v for k, v in doc.items() if k != "_id"}


def raw_mongo_find(collection: str, query: dict, projection: dict | None = None,
                   limit: int = 100) -> list[dict]:
    """Execute a MongoDB find and return a list of plain dicts."""
    try:
        proj = {**(projection or {}), "_id": 0}
        cursor = _get_mongo()[MONGO_DB][collection].find(query, proj).limit(limit)
        results = [_clean_mongo_doc(d) for d in cursor]
        logger.debug("[MONGO_QUERY] find col=%s query=%s rows=%d", collection, query, len(results))
        return results
    except PyMongoError as exc:
        logger.warning("[MONGO_QUERY] find error col=%s: %s", collection, exc)
        return []


def raw_mongo_find_one(collection: str, query: dict,
                       projection: dict | None = None) -> dict | None:
    """Execute a MongoDB find_one and return a plain dict or None."""
    try:
        proj = {**(projection or {}), "_id": 0}
        doc = _get_mongo()[MONGO_DB][collection].find_one(query, proj)
        result = _clean_mongo_doc(doc) if doc else None
        logger.debug("[MONGO_QUERY] find_one col=%s query=%s found=%s", collection, query, result is not None)
        return result
    except PyMongoError as exc:
        logger.warning("[MONGO_QUERY] find_one error col=%s: %s", collection, exc)
        return None


def raw_mongo_count(collection: str, query: dict) -> int:
    """Return document count matching query."""
    try:
        count = _get_mongo()[MONGO_DB][collection].count_documents(query)
        logger.debug("[MONGO_QUERY] count col=%s query=%s count=%d", collection, query, count)
        return count
    except PyMongoError as exc:
        logger.warning("[MONGO_QUERY] count error col=%s: %s", collection, exc)
        return 0


def raw_mongo_aggregate(collection: str, pipeline: list) -> list[dict]:
    """Execute a MongoDB aggregation pipeline and return results."""
    try:
        cursor = _get_mongo()[MONGO_DB][collection].aggregate(pipeline)
        results = [_clean_mongo_doc(d) for d in cursor]
        logger.debug("[MONGO_QUERY] aggregate col=%s stages=%d rows=%d",
                     collection, len(pipeline), len(results))
        return results
    except PyMongoError as exc:
        logger.warning("[MONGO_QUERY] aggregate error col=%s: %s", collection, exc)
        return []


def raw_sql_query(query: str, params: tuple = ()) -> list[dict]:
    """Execute a raw SQLite SELECT and return a list of row dicts.

    Only SELECT statements are allowed; write operations raise ValueError.
    """
    normalised = query.strip().upper()
    if not normalised.startswith("SELECT"):
        raise ValueError(f"raw_sql_query only allows SELECT; got: {query[:40]!r}")
    try:
        conn = sqlite3.connect(DB_PATH)
        conn.row_factory = sqlite3.Row
        try:
            rows = conn.execute(query, params).fetchall()
            results = [dict(r) for r in rows]
        finally:
            conn.close()
        logger.debug("[SQL_QUERY] query=%s params=%s rows=%d", query[:80], params, len(results))
        return results
    except sqlite3.Error as exc:
        logger.warning("[SQL_QUERY] error query=%s: %s", query[:80], exc)
        return []


def raw_sql_one(query: str, params: tuple = ()) -> dict | None:
    """Execute a raw SQLite SELECT and return first row as dict or None."""
    rows = raw_sql_query(query, params)
    return rows[0] if rows else None


def raw_sql_count(query: str, params: tuple = ()) -> int:
    """Execute a COUNT query and return the integer result."""
    row = raw_sql_one(query, params)
    if not row:
        return 0
    return int(next(iter(row.values()), 0))


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


def _ensure_schema_ready() -> None:
    """Fail fast if the DB/schema is missing. Bootstrap is now explicit.

    Run `python bootstrap.py` (or `./run.sh`, which calls it) before starting
    the app. This keeps first-time setup out of import-time side effects.

    Also auto-migrates schema additions (tags column, new tables) so that
    existing deployments do not need a manual re-bootstrap.
    """
    must_exist = ("proxies", "mocks", "request_history", "mock_sequences")
    try:
        conn = sqlite3.connect(DB_PATH)
        try:
            rows = conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            ).fetchall()
        finally:
            conn.close()
    except sqlite3.Error as exc:
        logger.critical("[STARTUP] cannot open DB at %s: %s", DB_PATH, exc)
        raise RuntimeError(
            f"Cannot open DB at {DB_PATH}. Run `python bootstrap.py` first."
        ) from exc

    present = {r[0] for r in rows}
    missing = [t for t in must_exist if t not in present]
    if missing:
        logger.critical(
            "[STARTUP] DB at %s missing tables: %s", DB_PATH, ", ".join(missing)
        )
        raise RuntimeError(
            f"DB at {DB_PATH} is missing tables: {', '.join(missing)}. "
            f"Run `python bootstrap.py` first."
        )

    # Auto-migrate: add tags column to mocks if missing
    conn = sqlite3.connect(DB_PATH)
    try:
        cols = {r[1] for r in conn.execute("PRAGMA table_info(mocks)").fetchall()}
        if "tags" not in cols:
            conn.execute("ALTER TABLE mocks ADD COLUMN tags TEXT DEFAULT ''")
            conn.commit()
            logger.info("[STARTUP] Added 'tags' column to mocks table")
        # Auto-create state_snapshots table
        if "state_snapshots" not in present:
            conn.executescript("""
                CREATE TABLE IF NOT EXISTS state_snapshots (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    proxy_id TEXT NOT NULL,
                    name TEXT NOT NULL,
                    data TEXT NOT NULL,
                    created_at TEXT NOT NULL DEFAULT (datetime('now'))
                );
                CREATE INDEX IF NOT EXISTS idx_snapshots_proxy ON state_snapshots(proxy_id);
            """)
            logger.info("[STARTUP] Created state_snapshots table")
        # Auto-create mock_templates table
        if "mock_templates" not in present:
            conn.executescript("""
                CREATE TABLE IF NOT EXISTS mock_templates (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    name TEXT NOT NULL UNIQUE,
                    description TEXT,
                    template TEXT NOT NULL,
                    category TEXT NOT NULL DEFAULT 'general',
                    created_at TEXT NOT NULL DEFAULT (datetime('now'))
                );
            """)
            logger.info("[STARTUP] Created mock_templates table")
        # Auto-create suggestions table
        if "suggestions" not in present:
            conn.executescript("""
                CREATE TABLE IF NOT EXISTS suggestions (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    name TEXT NOT NULL DEFAULT 'Anonymous',
                    message TEXT NOT NULL,
                    created_at TEXT NOT NULL DEFAULT (datetime('now'))
                );
            """)
            logger.info("[STARTUP] Created suggestions table")
    finally:
        conn.close()

    logger.info("[STARTUP] schema ok db_path=%s tables=%d", DB_PATH, len(present))


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


def db_get_request_history(
    proxy_id: str,
    limit: int = 50,
    method: str | None = None,
    endpoint: str | None = None,
    status_min: int | None = None,
    status_max: int | None = None,
    source: str | None = None,
    since: str | None = None,
    until: str | None = None,
) -> list[dict]:
    """Get recent request history for a proxy with optional filters."""
    db = _get_db()
    clauses = ["proxy_id = ?"]
    params: list = [proxy_id]
    if method:
        clauses.append("method = ?")
        params.append(method.upper())
    if endpoint:
        clauses.append("endpoint LIKE ?")
        params.append(f"%{endpoint}%")
    if status_min is not None:
        clauses.append("response_status >= ?")
        params.append(status_min)
    if status_max is not None:
        clauses.append("response_status <= ?")
        params.append(status_max)
    if source:
        clauses.append("source = ?")
        params.append(source)
    if since:
        clauses.append("created_at >= ?")
        params.append(since)
    if until:
        clauses.append("created_at <= ?")
        params.append(until)
    where = " AND ".join(clauses)
    params.append(limit)
    sql = f"SELECT * FROM request_history WHERE {where} ORDER BY id DESC LIMIT ?"
    logger.debug("[HISTORY] query=%s params=%s", sql[:120], params)
    rows = db.execute(sql, params).fetchall()
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
# Per-proxy State helpers — SQLite (proxy_state table)
# ---------------------------------------------------------------------------


def db_get_state(proxy_id: str) -> dict:
    """Return the per-proxy state dict from SQLite, or {} if none stored."""
    try:
        row = _get_db().execute(
            "SELECT data FROM proxy_state WHERE proxy_id=?", (proxy_id,)
        ).fetchone()
        if not row:
            return {}
        data = json.loads(row["data"])
        return data if isinstance(data, dict) else {}
    except Exception as exc:
        logger.warning("[STATE] db_get_state error proxy='%s': %s", proxy_id, exc)
        return {}


def db_set_state(proxy_id: str, data: dict) -> None:
    """Replace the per-proxy state in SQLite."""
    try:
        db = _get_db()
        db.execute(
            "INSERT INTO proxy_state(proxy_id, data) VALUES(?,?)"
            " ON CONFLICT(proxy_id) DO UPDATE SET data=excluded.data",
            (proxy_id, json.dumps(data)),
        )
        db.commit()
        logger.info("[STATE] Replaced proxy='%s' keys=%d", proxy_id, len(data))
    except Exception as exc:
        logger.error("[STATE] db_set_state error proxy='%s': %s", proxy_id, exc)
        raise


def db_merge_state(proxy_id: str, patch: dict) -> dict:
    """Shallow-merge `patch` into the per-proxy state. Returns merged result."""
    try:
        current = db_get_state(proxy_id)
        current.update(patch)
        db_set_state(proxy_id, current)
        logger.info("[STATE] Merged proxy='%s' patch_keys=%s", proxy_id, list(patch))
        return current
    except Exception as exc:
        logger.error("[STATE] db_merge_state error proxy='%s': %s", proxy_id, exc)
        raise


def db_clear_state(proxy_id: str) -> bool:
    """Delete the per-proxy state row. Returns True if something was deleted."""
    try:
        db = _get_db()
        cur = db.execute("DELETE FROM proxy_state WHERE proxy_id=?", (proxy_id,))
        db.commit()
        deleted = cur.rowcount > 0
        if deleted:
            logger.info("[STATE] Cleared proxy='%s'", proxy_id)
        return deleted
    except Exception as exc:
        logger.error("[STATE] db_clear_state error proxy='%s': %s", proxy_id, exc)
        raise


# ---------------------------------------------------------------------------
# DB helper functions — State Snapshots
# ---------------------------------------------------------------------------


def db_save_snapshot(proxy_id: str, name: str) -> dict:
    """Save current proxy state as a named snapshot. Enforces per-proxy cap."""
    db = _get_db()
    state = db_get_state(proxy_id)
    db.execute(
        "INSERT INTO state_snapshots (proxy_id, name, data) VALUES (?, ?, ?)",
        (proxy_id, name, json.dumps(state)),
    )
    # Enforce cap — delete oldest beyond limit
    db.execute(
        "DELETE FROM state_snapshots WHERE proxy_id = ? AND id NOT IN "
        "(SELECT id FROM state_snapshots WHERE proxy_id = ? ORDER BY id DESC LIMIT ?)",
        (proxy_id, proxy_id, MAX_SNAPSHOTS_PER_PROXY),
    )
    db.commit()
    snap_id = db.execute("SELECT last_insert_rowid()").fetchone()[0]
    logger.info("[SNAPSHOT] Saved proxy='%s' name='%s' id=%d", proxy_id, name, snap_id)
    return {"id": snap_id, "proxy_id": proxy_id, "name": name}


def db_list_snapshots(proxy_id: str) -> list[dict]:
    """List all snapshots for a proxy, newest first."""
    db = _get_db()
    rows = db.execute(
        "SELECT id, proxy_id, name, created_at FROM state_snapshots "
        "WHERE proxy_id = ? ORDER BY id DESC",
        (proxy_id,),
    ).fetchall()
    return [dict(r) for r in rows]


def db_restore_snapshot(snapshot_id: int) -> dict | None:
    """Restore proxy state from a snapshot. Returns the restored state or None."""
    db = _get_db()
    row = db.execute(
        "SELECT proxy_id, name, data FROM state_snapshots WHERE id = ?",
        (snapshot_id,),
    ).fetchone()
    if not row:
        return None
    proxy_id = row["proxy_id"]
    data = json.loads(row["data"])
    db_set_state(proxy_id, data)
    logger.info("[SNAPSHOT] Restored proxy='%s' snapshot_id=%d name='%s'",
                proxy_id, snapshot_id, row["name"])
    return {"proxy_id": proxy_id, "name": row["name"], "state": data}


def db_delete_snapshot(snapshot_id: int) -> bool:
    """Delete a snapshot. Returns True if found and deleted."""
    db = _get_db()
    cur = db.execute("DELETE FROM state_snapshots WHERE id = ?", (snapshot_id,))
    db.commit()
    deleted = cur.rowcount > 0
    if deleted:
        logger.info("[SNAPSHOT] Deleted snapshot_id=%d", snapshot_id)
    return deleted


# ---------------------------------------------------------------------------
# DB helper functions — Mock Templates
# ---------------------------------------------------------------------------


def db_list_templates(category: str | None = None) -> list[dict]:
    """List all mock templates, optionally filtered by category."""
    db = _get_db()
    if category:
        rows = db.execute(
            "SELECT id, name, description, category, created_at FROM mock_templates "
            "WHERE category = ? ORDER BY name",
            (category,),
        ).fetchall()
    else:
        rows = db.execute(
            "SELECT id, name, description, category, created_at FROM mock_templates "
            "ORDER BY category, name"
        ).fetchall()
    return [dict(r) for r in rows]


def db_get_template(template_id: int) -> dict | None:
    """Get a full template by ID."""
    db = _get_db()
    row = db.execute(
        "SELECT * FROM mock_templates WHERE id = ?", (template_id,)
    ).fetchone()
    if not row:
        return None
    result = dict(row)
    result["template"] = json.loads(result["template"])
    return result


def db_upsert_template(name: str, template: dict, description: str = "",
                       category: str = "general") -> int:
    """Create or update a mock template. Returns template ID."""
    db = _get_db()
    db.execute(
        "INSERT INTO mock_templates (name, template, description, category) VALUES (?, ?, ?, ?) "
        "ON CONFLICT(name) DO UPDATE SET template=excluded.template, "
        "description=excluded.description, category=excluded.category",
        (name, json.dumps(template), description, category),
    )
    db.commit()
    row = db.execute("SELECT id FROM mock_templates WHERE name = ?", (name,)).fetchone()
    logger.info("[TEMPLATE] Upserted name='%s' category='%s'", name, category)
    return row["id"]


def db_delete_template(template_id: int) -> bool:
    """Delete a template. Returns True if found."""
    db = _get_db()
    cur = db.execute("DELETE FROM mock_templates WHERE id = ?", (template_id,))
    db.commit()
    return cur.rowcount > 0


# ---------------------------------------------------------------------------
# Mock Response Cache (in-memory LRU)
# ---------------------------------------------------------------------------

_mock_cache: dict[str, tuple[float, object]] = {}
_mock_cache_lock = threading.Lock()
_MOCK_CACHE_MAX = int(os.environ.get("PED_MOCK_CACHE_MAX", "200"))


def _mock_cache_key(proxy_id: str, endpoint: str, method: str, params: str) -> str:
    return f"{proxy_id}:{method}:{endpoint}:{params}"


def _mock_cache_get(key: str, ttl: float) -> object | None:
    """Get a cached mock response if not expired."""
    with _mock_cache_lock:
        entry = _mock_cache.get(key)
        if entry and (time.time() - entry[0]) < ttl:
            logger.debug("[CACHE] Hit key=%s", key[:80])
            return entry[1]
        if entry:
            del _mock_cache[key]
    return None


def _mock_cache_set(key: str, value: object) -> None:
    """Store a mock response in cache with LRU eviction."""
    with _mock_cache_lock:
        if len(_mock_cache) >= _MOCK_CACHE_MAX:
            oldest = min(_mock_cache, key=lambda k: _mock_cache[k][0])
            del _mock_cache[oldest]
        _mock_cache[key] = (time.time(), value)
        logger.debug("[CACHE] Set key=%s entries=%d", key[:80], len(_mock_cache))


def _get_state_for_resolver(proxy_id: str | None) -> dict:
    """dbget() fetch helper. Returns {} on any error — never raises.

    When called from inside _apply_store_ops, returns the in-progress local
    state (thread-local) so sequential _store ops can see each other's writes.
    """
    if not proxy_id:
        return {}
    # Thread-local override set by _apply_store_ops for cross-op visibility
    pending = getattr(_store_pending_state, "entry", None)
    if pending is not None and pending.get("proxy_id") == proxy_id:
        return pending["state"]
    try:
        return db_get_state(proxy_id)
    except Exception as exc:
        logger.debug("[STATE] dbget resolver fallback proxy='%s': %s", proxy_id, exc)
        return {}


# Thread-local used by _apply_store_ops so each op sees previous ops' writes.
_store_pending_state = threading.local()


def mongo_get_any(collection: str, key: str, path: str | None = None, default=None):
    """General-purpose MongoDB lookup across any collection in PED_MONGO_DB.

    Looks up the document where ``proxy_id == key`` in ``collection``, then
    traverses ``path`` (dotted) inside its ``data`` field.

    mongoget(proxy_state, ajiocash, accessToken)     → token stored for ajiocash
    mongoget(proxy_state, juspay, user.tier, guest)  → nested path with default
    mongoget(sessions, sess_abc, expires)            → from a custom collection
    """
    try:
        doc = _get_mongo()[MONGO_DB][collection].find_one(
            {"proxy_id": key}, {"_id": 0, "data": 1}
        )
        if not doc:
            logger.debug("[MONGOGET] no doc col=%s key=%s", collection, key)
            return default
        data = doc.get("data", {})
        if not isinstance(data, dict):
            return default
        if path:
            resolved, found = _resolve_item_path(data, path)
            return resolved if found else default
        return data
    except PyMongoError as exc:
        logger.warning("[MONGOGET] error col=%s key=%s path=%s: %s", collection, key, path, exc)
        return default


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
    (re.compile(r'\bTrue\b'), "true"),
    (re.compile(r'\bFalse\b'), "false"),
    (re.compile(r'\bNone\b'), "null"),
]


def normalize_escape_sequences(s: str) -> str:
    if not isinstance(s, str):
        return s
    for old, new in _ESCAPE_REPLACEMENTS:
        s = s.replace(old, new)
    for pattern, new in _PYTHON_LITERAL_MAP:
        s = pattern.sub(new, s)
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
            return {'json': flask_request.get_json(silent=True) or {}}
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


def safe_eval_snippet(snippet: str, names: dict | None = None,
                       functions: dict | None = None):
    """Evaluate a simpleeval expression with optional read-only request context.

    `names` — data surfaced as variables (body/headers/params/state/url/now_ts).
    `functions` — extra callables merged with the built-in safe set; callers
    wire resolver shortcuts (jsonget/dbget/now/...) so snippets can invoke
    them directly, e.g. ``snippet(dbget('user.tier', 'none') == 'gold')``.

    Return type is preserved — `int`, `float`, `list`, `dict` etc. all pass
    through.
    """
    snippet = snippet.strip()
    if not snippet:
        return ""
    if len(snippet) > _SNIPPET_MAX_LENGTH:
        raise ValueError(f"snippet() expression too long ({len(snippet)} chars)")

    fns = dict(_SNIPPET_FUNCTIONS)
    if functions:
        fns.update(functions)

    evaluator = EvalWithCompoundTypes(functions=fns, names=names or {})
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


def _snippet_context(header, json_data, params, url, proxy_id):
    """Build names+functions dicts for safe_eval_snippet.

    Exposes request state (body/headers/params/state/url/now_ts) as variables
    AND wires the standard resolvers as callables, so snippets can do e.g.::

        snippet(jsonget('user.name', 'anon'))
        snippet(dbget('ver', 0) + 1)
        snippet(now() if jsonget('track') else '')
    """
    _body = json_data or {}
    _header = header or {}
    _params = params or {}
    _state = _get_state_for_resolver(proxy_id)
    _url = url or ""

    def _fn_jsonget(path, default=None):
        v, ok = _resolve_item_path(_body, path)
        return v if ok else default

    def _fn_dbget(path, default=None):
        v, ok = _resolve_item_path(_state, path)
        return v if ok else default

    def _fn_mongoget(collection, key, path=None, default=None):
        return mongo_get_any(collection, key, path, default)

    def _fn_verify_password(username, password):
        """Check username+password against proxy_users collection for this proxy."""
        if not proxy_id:
            return False
        return verify_proxy_user(proxy_id, str(username), str(password))

    def _fn_valid_token(token):
        """Return True if token matches any stored accessToken in state.tokens.<user>."""
        tokens = _state.get("tokens", {})
        return any(
            isinstance(v, dict) and v.get("accessToken") == str(token)
            for v in tokens.values()
        )

    def _fn_valid_refresh_token(token):
        """Return True if token matches any stored refreshToken in state.tokens.<user>."""
        tokens = _state.get("tokens", {})
        return any(
            isinstance(v, dict) and v.get("refreshToken") == str(token)
            for v in tokens.values()
        )

    def _fn_token_user(token):
        """Return the username associated with the given accessToken, or None."""
        tokens = _state.get("tokens", {})
        for username, v in tokens.items():
            if isinstance(v, dict) and v.get("accessToken") == str(token):
                return username
        return None

    def _fn_refresh_token_user(token):
        """Return the username associated with the given refreshToken, or None."""
        tokens = _state.get("tokens", {})
        for username, v in tokens.items():
            if isinstance(v, dict) and v.get("refreshToken") == str(token):
                return username
        return None

    def _fn_bearer_token():
        """Extract the token from 'Authorization: Bearer <token>' header."""
        auth = _header.get("Authorization") or _header.get("authorization") or ""
        return auth[7:] if auth.startswith("Bearer ") else auth

    def _fn_headerget(name, default=None):
        return _header.get(name, default)

    def _fn_paramget(name, default=None):
        return _params.get(name, default)

    def _fn_pathparamget(prefix, default=None):
        v = extract_path_param(prefix, _url)
        return v if v is not None else default

    def _fn_now(offset=0):
        return (
            datetime.now(timezone.utc) + timedelta(seconds=int(offset or 0))
        ).isoformat().replace("+00:00", "Z")

    def _fn_now_epoch(offset=0):
        return time.time() + int(offset or 0)

    # --- Random-string generators (the same ones the non-snippet resolvers
    # expose: upper(N) / lower(N) / chars(N) / digit(N) / alnum(...)). In
    # snippet-land they take ordinary Python args instead of a parsed string.
    def _fn_upper(n):
        return "".join(random.choices(string.ascii_uppercase, k=int(n)))

    def _fn_lower(n):
        return "".join(random.choices(string.ascii_lowercase, k=int(n)))

    def _fn_chars(n):
        return "".join(random.choices(string.ascii_letters, k=int(n)))

    def _fn_digit(n):
        return "".join(random.choices(string.digits, k=int(n)))

    def _fn_alnum(*parts):
        """alnum(3, 4, 2, 1) → 3 letters + 4 digits + 2 letters + 1 digit."""
        if len(parts) % 2 != 0:
            raise ValueError("alnum() requires an even number of arguments")
        out = []
        for i in range(0, len(parts), 2):
            out.append("".join(random.choices(string.ascii_letters, k=int(parts[i]))))
            out.append("".join(random.choices(string.digits, k=int(parts[i + 1]))))
        return "".join(out)

    def _fn_body():
        return _body

    def _fn_state_all():
        return _state

    def _fn_envget(var_name, default=None):
        """Return env var value if it matches the allowed prefix, else default."""
        if not isinstance(var_name, str):
            return default
        if not var_name.startswith(MOCK_ENV_PREFIX):
            logger.warning("[ENVGET] Blocked access to env var '%s' (prefix '%s' required)",
                           var_name, MOCK_ENV_PREFIX)
            return default
        return os.environ.get(var_name, default)

    return {
        "names": {
            "body": _body,
            "headers": _header,
            "params": _params,
            "state": _state,
            "url": _url,
            "now_ts": time.time(),
        },
        "functions": {
            # Request accessors
            "jsonget": _fn_jsonget,
            "headerget": _fn_headerget,
            "paramget": _fn_paramget,
            "pathparamget": _fn_pathparamget,
            # State accessors
            "dbget": _fn_dbget,
            "mongoget": _fn_mongoget,
            # Auth helpers
            "verify_password": _fn_verify_password,
            "valid_token": _fn_valid_token,
            "valid_refresh_token": _fn_valid_refresh_token,
            "token_user": _fn_token_user,
            "refresh_token_user": _fn_refresh_token_user,
            "bearer_token": _fn_bearer_token,
            # Raw MongoDB queries
            "mongo_find": lambda col, q, proj=None, limit=100: raw_mongo_find(col, q, proj, limit),
            "mongo_one": lambda col, q, proj=None: raw_mongo_find_one(col, q, proj),
            "mongo_count": lambda col, q: raw_mongo_count(col, q),
            "mongo_aggregate": lambda col, pipeline: raw_mongo_aggregate(col, pipeline),
            # Raw SQLite queries (SELECT only)
            "sql": lambda q, *params: raw_sql_query(q, params),
            "sql_one": lambda q, *params: raw_sql_one(q, params),
            "sql_count": lambda q, *params: raw_sql_count(q, params),
            # Whole-payload accessors
            "body": _fn_body,
            "state_all": _fn_state_all,
            # Environment variable accessor
            "envget": _fn_envget,
            # Time + identity
            "now": _fn_now,
            "now_epoch": _fn_now_epoch,
            "uuid": lambda: str(_uuid.uuid4()),
            "uuid_short": lambda: shortuuid.uuid(),
            # Random-string generators
            "upper": _fn_upper,
            "lower": _fn_lower,
            "chars": _fn_chars,
            "digit": _fn_digit,
            "alnum": _fn_alnum,
        },
    }


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

    # --- General MongoDB lookup: mongoget(collection, key, path, default) ---
    if value.startswith("mongoget(") and value.endswith(")"):
        raw = value[9:-1].strip()
        # Split into up to 4 positional args respecting no nested parens
        parts = [p.strip().strip("'\"") for p in raw.split(",")]
        if len(parts) < 2:
            logger.warning("[RESOLVER] mongoget() needs at least 2 args: %s", value)
            return value
        col = parts[0]
        key = parts[1]
        path = parts[2] if len(parts) > 2 else None
        default = parts[3] if len(parts) > 3 else None
        return mongo_get_any(col, key, path, default)

    # --- Environment variable resolver (restricted prefix) ---
    if value.startswith("envget(") and value.endswith(")"):
        field, default = _parse_resolver_args(value[7:-1], value)
        if not field.startswith(MOCK_ENV_PREFIX):
            logger.warning("[RESOLVER] envget blocked: '%s' (prefix '%s' required)", field, MOCK_ENV_PREFIX)
            return default
        return os.environ.get(field, default)

    # --- Generators (random) ---
    if value.startswith("alnum(") and value.endswith(")"):
        return _safe_alnum(value[6:-1])
    if value.startswith("snippet(") and value.endswith(")"):
        return safe_eval_snippet(
            value[8:-1],
            **_snippet_context(header, json_data, params, url, proxy_id),
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


def _set_item_path(root, path: str, value) -> bool:
    """Set `value` at a dotted path in `root` dict, creating intermediate
    dicts as needed. Returns True on success."""
    if not isinstance(root, dict) or not path:
        return False
    parts = path.split(".")
    cur = root
    for part in parts[:-1]:
        if part not in cur or not isinstance(cur[part], dict):
            cur[part] = {}
        cur = cur[part]
    cur[parts[-1]] = value
    return True


def _delete_item_path(root, path: str) -> bool:
    """Delete the value at a dotted path. Returns True if something was deleted."""
    if not isinstance(root, dict) or not path:
        return False
    parts = path.split(".")
    cur = root
    for part in parts[:-1]:
        if not isinstance(cur, dict) or part not in cur:
            return False
        cur = cur[part]
    if isinstance(cur, dict) and parts[-1] in cur:
        del cur[parts[-1]]
        return True
    return False


def _resolve_path_template(template: str, header, json_data, params, url, proxy_id) -> str:
    """Resolve each dot-separated segment of a path template through the resolver
    pipeline, then re-join.

    ``"orders.jsonget(orderId)"`` with body ``{"orderId": "O_123"}`` becomes
    ``"orders.O_123"`` — the pattern most useful for mongo-style keys.
    """
    parts = template.split(".")
    out = []
    for p in parts:
        r = _resolve_value(p, header, json_data, params, url, proxy_id)
        out.append("null" if r is None else str(r))
    return ".".join(out)


def _apply_store_ops(ops, proxy_id, header, json_body, params, url):
    """Apply ``_store`` side-effects: write resolved values into per-proxy state.

    Accepted op shapes (or a list of them):

        {"path": "a.b.c", "value": <anything>}              # set
        {"collection": "orders", "key": "...", "value": ..} # set (mongo-style)
        {"path": "a.b.c", "delete": true}                   # delete

    ``path`` / ``collection`` / ``key`` segments pass through the resolver
    pipeline so expressions like ``"orders.jsonget(orderId)"`` work. ``value``
    is resolved too — strings go through ``_resolve_value``, compound
    structures through ``resolve_mock_data`` (so nested placeholders, _foreach,
    _random all work inside the stored value).
    """
    if ops is None or not proxy_id:
        return
    if isinstance(ops, dict):
        ops = [ops]
    if not isinstance(ops, list):
        logger.warning("[STORE] unsupported shape %r — ignoring", type(ops).__name__)
        return

    state = db_get_state(proxy_id)
    # Expose in-progress state so sequential ops can read each other's writes
    # via dbget() without waiting for the final db_set_state commit.
    _store_pending_state.entry = {"proxy_id": proxy_id, "state": state}
    dirty = False
    try:
        for op in ops:
            if not isinstance(op, dict):
                continue
            if "collection" in op and "key" in op:
                col = op["collection"]
                key = op["key"]
                col_r = (
                    _resolve_value(col, header, json_body, params, url, proxy_id)
                    if isinstance(col, str) else col
                )
                key_r = (
                    _resolve_value(key, header, json_body, params, url, proxy_id)
                    if isinstance(key, str) else key
                )
                path = f"{col_r}.{key_r}"
            elif "path" in op:
                tmpl = op["path"]
                if not isinstance(tmpl, str):
                    continue
                path = _resolve_path_template(tmpl, header, json_body, params, url, proxy_id)
            else:
                logger.warning("[STORE] op missing collection/key or path: %r", op)
                continue

            if op.get("delete"):
                if _delete_item_path(state, path):
                    dirty = True
                    logger.info("[STORE] delete path=%r", path)
            else:
                raw = op.get("value")
                if isinstance(raw, str):
                    resolved_value = _resolve_value(raw, header, json_body, params, url, proxy_id)
                elif isinstance(raw, (dict, list)):
                    resolved_value = resolve_mock_data(
                        copy.deepcopy(raw),
                        header=header, json_body=json_body, params=params,
                        url=url, proxy_id=proxy_id,
                    )
                else:
                    resolved_value = raw
                _set_item_path(state, path, resolved_value)
                dirty = True
                logger.info("[STORE] set path=%r", path)
    finally:
        _store_pending_state.entry = None

    if dirty:
        db_set_state(proxy_id, state)


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
    proxy_id: str | None = None,
) -> bool:
    """Check if all conditions match.

    Each condition is {field, source?, operator, value?}.

    Supported sources (aliases accepted for UI compatibility):
      json / json_body  — field from request JSON body
      header            — named request header
      param / query_param — query parameter
      path              — URL path ('field' ignored)
      method            — HTTP method ('field' ignored)
      snippet           — evaluate 'value' as snippet expression; truthy = pass

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

        # snippet source: evaluate value expression; truthy = condition passes
        if source_type == "snippet":
            expr = cond.get("value", "")
            # strip outer snippet(...) wrapper if present
            if expr.startswith("snippet(") and expr.endswith(")"):
                expr = expr[8:-1]
            try:
                result = safe_eval_snippet(
                    expr,
                    **_snippet_context(headers, json_body, params, path or "", proxy_id),
                )
                if not result:
                    logger.debug("[CONDITIONAL] snippet condition false: %r", expr)
                    return False
            except Exception as exc:
                logger.warning("[CONDITIONAL] snippet condition error %r: %s", expr, exc)
                return False
            continue

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
        # Brute-force protection: max 10 attempts per IP per 60 seconds
        _login_key = f"__login__:{request.remote_addr}"
        _login_window = 60
        _login_max = 10
        _now = time.time()
        with _rate_lock:
            _ts = _rate_limits.get(_login_key, [])
            _ts = [t for t in _ts if t > _now - _login_window]
            if len(_ts) >= _login_max:
                _rate_limits[_login_key] = _ts
                logger.warning("[AUTH] Login rate limit hit from %s", request.remote_addr)
                return render_template("login.html", error="Too many attempts. Try again later."), 429
            _ts.append(_now)
            _rate_limits[_login_key] = _ts

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
            # Reject absolute URLs to prevent open redirect
            if next_url and (next_url.startswith("//") or "://" in next_url):
                logger.warning("[AUTH] Rejected suspicious next_url=%s from %s", next_url, request.remote_addr)
                next_url = "/"
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


@app.route("/ped/minify", methods=["POST"])
@log_access
def minify():
    """Minify JSON — strip all whitespace and produce compact output."""
    data = request.get_json(silent=True)
    if not data:
        return jsonify({"error": "Request body must be JSON"}), 400
    raw = data.get("data", "")
    if not raw:
        return jsonify({"error": "data field is required"}), 400

    # If the input is already a parsed object (caller sent JSON directly)
    if isinstance(raw, (dict, list)):
        minified = json.dumps(raw, separators=(",", ":"))
        logger.info("[MINIFY] Direct object minified len=%d", len(minified))
        return jsonify({"minified": minified, "original_length": len(json.dumps(raw)),
                        "minified_length": len(minified)})

    # String input — try to parse, then minify
    raw = str(raw).strip()
    try:
        parsed = json.loads(raw)
        minified = json.dumps(parsed, separators=(",", ":"))
        logger.info("[MINIFY] String minified len=%d -> %d", len(raw), len(minified))
        return jsonify({"minified": minified, "original_length": len(raw),
                        "minified_length": len(minified)})
    except json.JSONDecodeError as exc:
        logger.warning("[MINIFY] Invalid JSON: %s", exc)
        return jsonify({"error": f"Invalid JSON: {exc}"}), 400


@app.route("/ped/jsonpath", methods=["POST"])
@log_access
def jsonpath_query():
    """Extract a value from a JSON document using a dot-separated path.

    Supports:
      - Dotted paths: ``user.address.city``
      - Array indices: ``items.0.name``
      - Multiple paths: pass ``paths`` as a list to query several at once
    """
    data = request.get_json(silent=True)
    if not data:
        return jsonify({"error": "Request body must be JSON"}), 400

    document = data.get("data")
    if document is None:
        return jsonify({"error": "data field is required"}), 400

    # If input is a string, try to parse it
    if isinstance(document, str):
        try:
            document = json.loads(document)
        except json.JSONDecodeError as exc:
            return jsonify({"error": f"data is not valid JSON: {exc}"}), 400

    # Single path query
    single_path = data.get("path")
    # Multi-path query
    paths = data.get("paths")

    if single_path:
        resolved, found = _resolve_item_path(document, single_path)
        logger.info("[JSONPATH] path=%r found=%s", single_path, found)
        return jsonify({
            "path": single_path,
            "found": found,
            "value": resolved,
            "type": type(resolved).__name__ if found else None,
        })

    if paths and isinstance(paths, list):
        results = {}
        for p in paths:
            if not isinstance(p, str):
                continue
            resolved, found = _resolve_item_path(document, p)
            results[p] = {"found": found, "value": resolved,
                          "type": type(resolved).__name__ if found else None}
        logger.info("[JSONPATH] multi-path count=%d", len(results))
        return jsonify({"results": results})

    return jsonify({"error": "path or paths field is required"}), 400


@app.route("/ped/diff", methods=["POST"])
@log_access
def json_diff():
    """Compare two JSON documents and return a structured diff.

    Input: ``{"a": {...}, "b": {...}}``
    Output: list of changes with path, type (added/removed/changed), old/new values.
    """
    data = request.get_json(silent=True)
    if not data:
        return jsonify({"error": "Request body must be JSON"}), 400

    a = data.get("a")
    b = data.get("b")
    if a is None or b is None:
        return jsonify({"error": "Both a and b fields are required"}), 400

    # Parse if strings
    if isinstance(a, str):
        try:
            a = json.loads(a)
        except json.JSONDecodeError as exc:
            return jsonify({"error": f"a is not valid JSON: {exc}"}), 400
    if isinstance(b, str):
        try:
            b = json.loads(b)
        except json.JSONDecodeError as exc:
            return jsonify({"error": f"b is not valid JSON: {exc}"}), 400

    diffs = _json_diff_recursive(a, b, "")
    logger.info("[DIFF] changes=%d", len(diffs))
    return jsonify({
        "identical": len(diffs) == 0,
        "change_count": len(diffs),
        "changes": diffs,
    })


def _json_diff_recursive(a, b, path: str) -> list[dict]:
    """Recursively diff two JSON-compatible values. Returns a list of change dicts."""
    diffs = []
    if a == b:
        return diffs

    # Type mismatch or non-dict/list → atomic change
    if (type(a) != type(b)
            or a is None or b is None
            or not isinstance(a, (dict, list))):
        diffs.append({
            "path": path or "(root)",
            "type": "changed",
            "old": a,
            "new": b,
        })
        return diffs

    if isinstance(a, list):
        max_len = max(len(a), len(b))
        for i in range(max_len):
            p = f"{path}[{i}]" if path else f"[{i}]"
            if i >= len(a):
                diffs.append({"path": p, "type": "added", "new": b[i]})
            elif i >= len(b):
                diffs.append({"path": p, "type": "removed", "old": a[i]})
            else:
                diffs.extend(_json_diff_recursive(a[i], b[i], p))
        return diffs

    # Both are dicts
    all_keys = sorted(set(list(a.keys()) + list(b.keys())))
    for key in all_keys:
        p = f"{path}.{key}" if path else key
        if key not in a:
            diffs.append({"path": p, "type": "added", "new": b[key]})
        elif key not in b:
            diffs.append({"path": p, "type": "removed", "old": a[key]})
        else:
            diffs.extend(_json_diff_recursive(a[key], b[key], p))
    return diffs


@app.route("/ped/validate-schema", methods=["POST"])
@log_access
def validate_json_schema():
    """Validate a JSON document against a JSON Schema (draft-compatible subset).

    Input: ``{"data": {...}, "schema": {...}}``

    This implements a lightweight schema validator without external dependencies.
    Supports: type, required, properties, items, enum, minimum, maximum,
    minLength, maxLength, pattern, minItems, maxItems.
    """
    req = request.get_json(silent=True)
    if not req:
        return jsonify({"error": "Request body must be JSON"}), 400

    document = req.get("data")
    schema = req.get("schema")
    if document is None:
        return jsonify({"error": "data field is required"}), 400
    if not schema or not isinstance(schema, dict):
        return jsonify({"error": "schema field must be a JSON object"}), 400

    if isinstance(document, str):
        try:
            document = json.loads(document)
        except json.JSONDecodeError as exc:
            return jsonify({"error": f"data is not valid JSON: {exc}"}), 400

    errors = _validate_schema(document, schema, "")
    valid = len(errors) == 0
    logger.info("[SCHEMA] valid=%s errors=%d", valid, len(errors))
    return jsonify({"valid": valid, "errors": errors})


def _validate_schema(value, schema: dict, path: str) -> list[dict]:
    """Lightweight JSON Schema validator. No external deps."""
    errors = []
    loc = path or "(root)"

    # --- type ---
    expected_type = schema.get("type")
    if expected_type:
        _TYPE_MAP = {
            "string": str, "number": (int, float), "integer": int,
            "boolean": bool, "array": list, "object": dict, "null": type(None),
        }
        if isinstance(expected_type, str):
            py_type = _TYPE_MAP.get(expected_type)
            if py_type and not isinstance(value, py_type):
                # JSON has no int/float distinction — accept both for "number"
                if not (expected_type == "number" and isinstance(value, (int, float))):
                    errors.append({"path": loc, "message": f"Expected type {expected_type}, got {type(value).__name__}"})
                    return errors  # skip further checks if type is wrong
        elif isinstance(expected_type, list):
            matched = False
            for et in expected_type:
                py_type = _TYPE_MAP.get(et)
                if py_type and isinstance(value, py_type):
                    matched = True
                    break
            if not matched:
                errors.append({"path": loc, "message": f"Expected one of types {expected_type}, got {type(value).__name__}"})
                return errors

    # --- enum ---
    enum_values = schema.get("enum")
    if enum_values is not None and isinstance(enum_values, list):
        if value not in enum_values:
            errors.append({"path": loc, "message": f"Value not in enum: {enum_values}"})

    # --- string constraints ---
    if isinstance(value, str):
        min_len = schema.get("minLength")
        if min_len is not None and len(value) < int(min_len):
            errors.append({"path": loc, "message": f"String too short: {len(value)} < {min_len}"})
        max_len = schema.get("maxLength")
        if max_len is not None and len(value) > int(max_len):
            errors.append({"path": loc, "message": f"String too long: {len(value)} > {max_len}"})
        pattern = schema.get("pattern")
        if pattern:
            try:
                if not re.search(pattern, value):
                    errors.append({"path": loc, "message": f"String does not match pattern: {pattern}"})
            except re.error:
                errors.append({"path": loc, "message": f"Invalid regex pattern: {pattern}"})

    # --- number constraints ---
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        minimum = schema.get("minimum")
        if minimum is not None and value < minimum:
            errors.append({"path": loc, "message": f"Value {value} < minimum {minimum}"})
        maximum = schema.get("maximum")
        if maximum is not None and value > maximum:
            errors.append({"path": loc, "message": f"Value {value} > maximum {maximum}"})

    # --- object constraints ---
    if isinstance(value, dict):
        # required
        required = schema.get("required", [])
        for field in required:
            if field not in value:
                errors.append({"path": f"{loc}.{field}" if loc != "(root)" else field,
                               "message": f"Required field missing: {field}"})
        # properties
        properties = schema.get("properties", {})
        for prop_name, prop_schema in properties.items():
            if prop_name in value:
                child_path = f"{loc}.{prop_name}" if loc != "(root)" else prop_name
                errors.extend(_validate_schema(value[prop_name], prop_schema, child_path))

    # --- array constraints ---
    if isinstance(value, list):
        min_items = schema.get("minItems")
        if min_items is not None and len(value) < int(min_items):
            errors.append({"path": loc, "message": f"Array too short: {len(value)} < {min_items}"})
        max_items = schema.get("maxItems")
        if max_items is not None and len(value) > int(max_items):
            errors.append({"path": loc, "message": f"Array too long: {len(value)} > {max_items}"})
        items_schema = schema.get("items")
        if items_schema and isinstance(items_schema, dict):
            for i, item in enumerate(value):
                errors.extend(_validate_schema(item, items_schema, f"{loc}[{i}]"))

    return errors


@app.route("/ped/transform", methods=["POST"])
@log_access
def json_transform():
    """Apply a sequence of transform operations to a JSON document.

    Input: ``{"data": {...}, "operations": [...]}``

    Supported operations:
      - ``{"op": "pick", "fields": ["a", "b"]}`` — keep only listed keys
      - ``{"op": "omit", "fields": ["x", "y"]}`` — remove listed keys
      - ``{"op": "rename", "from": "old", "to": "new"}`` — rename a key
      - ``{"op": "set", "path": "a.b", "value": ...}`` — set a value at path
      - ``{"op": "delete", "path": "a.b"}`` — delete a value at path
      - ``{"op": "flatten", "separator": "."}`` — flatten nested object
      - ``{"op": "unflatten", "separator": "."}`` — unflatten dotted keys
      - ``{"op": "wrap", "key": "data"}`` — wrap in ``{"data": <original>}``
      - ``{"op": "unwrap", "key": "data"}`` — extract value at key
      - ``{"op": "sort_keys"}`` — recursively sort object keys
      - ``{"op": "defaults", "values": {...}}`` — set missing keys only
      - ``{"op": "map", "path": "items", "set": {"processed": true}}`` — set fields on each array element
    """
    req_data = request.get_json(silent=True)
    if not req_data:
        return jsonify({"error": "Request body must be JSON"}), 400

    document = req_data.get("data")
    operations = req_data.get("operations", [])
    if document is None:
        return jsonify({"error": "data field is required"}), 400
    if not isinstance(operations, list) or not operations:
        return jsonify({"error": "operations must be a non-empty list"}), 400

    if isinstance(document, str):
        try:
            document = json.loads(document)
        except json.JSONDecodeError as exc:
            return jsonify({"error": f"data is not valid JSON: {exc}"}), 400

    result = copy.deepcopy(document)
    applied = []
    errors = []

    for i, op in enumerate(operations):
        if not isinstance(op, dict):
            errors.append({"index": i, "error": "Operation must be an object"})
            continue
        op_type = op.get("op", "")
        try:
            result = _apply_transform(result, op)
            applied.append({"index": i, "op": op_type})
        except Exception as exc:
            errors.append({"index": i, "op": op_type, "error": str(exc)})

    logger.info("[TRANSFORM] applied=%d errors=%d", len(applied), len(errors))
    return jsonify({
        "result": result,
        "applied": len(applied),
        "errors": errors,
    })


def _apply_transform(data, op: dict):
    """Apply a single transform operation. Returns transformed data."""
    op_type = op.get("op", "")

    if op_type == "pick" and isinstance(data, dict):
        fields = op.get("fields", [])
        return {k: v for k, v in data.items() if k in fields}

    if op_type == "omit" and isinstance(data, dict):
        fields = op.get("fields", [])
        return {k: v for k, v in data.items() if k not in fields}

    if op_type == "rename" and isinstance(data, dict):
        old_key = op.get("from", "")
        new_key = op.get("to", "")
        if old_key in data:
            data[new_key] = data.pop(old_key)
        return data

    if op_type == "set":
        path = op.get("path", "")
        value = op.get("value")
        if isinstance(data, dict) and path:
            _set_item_path(data, path, value)
        return data

    if op_type == "delete":
        path = op.get("path", "")
        if isinstance(data, dict) and path:
            _delete_item_path(data, path)
        return data

    if op_type == "flatten" and isinstance(data, dict):
        sep = op.get("separator", ".")
        return _flatten_dict(data, sep)

    if op_type == "unflatten" and isinstance(data, dict):
        sep = op.get("separator", ".")
        return _unflatten_dict(data, sep)

    if op_type == "wrap":
        key = op.get("key", "data")
        return {key: data}

    if op_type == "unwrap" and isinstance(data, dict):
        key = op.get("key", "data")
        return data.get(key, data)

    if op_type == "sort_keys":
        return _sort_keys_recursive(data)

    if op_type == "defaults" and isinstance(data, dict):
        defaults = op.get("values", {})
        for k, v in defaults.items():
            if k not in data:
                data[k] = v
        return data

    if op_type == "map" and isinstance(data, dict):
        path = op.get("path", "")
        set_fields = op.get("set", {})
        resolved, found = _resolve_item_path(data, path)
        if found and isinstance(resolved, list):
            for item in resolved:
                if isinstance(item, dict):
                    item.update(set_fields)
        return data

    raise ValueError(f"Unknown or incompatible operation: {op_type}")


def _flatten_dict(d: dict, sep: str = ".", prefix: str = "") -> dict:
    """Flatten a nested dict into dotted keys."""
    out = {}
    for k, v in d.items():
        full_key = f"{prefix}{sep}{k}" if prefix else k
        if isinstance(v, dict):
            out.update(_flatten_dict(v, sep, full_key))
        else:
            out[full_key] = v
    return out


def _unflatten_dict(d: dict, sep: str = ".") -> dict:
    """Unflatten dotted keys back into nested dicts."""
    out: dict = {}
    for key, value in d.items():
        parts = key.split(sep)
        cur = out
        for part in parts[:-1]:
            if part not in cur or not isinstance(cur[part], dict):
                cur[part] = {}
            cur = cur[part]
        cur[parts[-1]] = value
    return out


def _sort_keys_recursive(data):
    """Recursively sort dict keys."""
    if isinstance(data, dict):
        return {k: _sort_keys_recursive(v) for k, v in sorted(data.items())}
    if isinstance(data, list):
        return [_sort_keys_recursive(item) for item in data]
    return data


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

    if not api_domain:
        return jsonify({"error": "api_domain is required"}), 400

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
        try:
            new_mock = json.loads(new_mock)
        except json.JSONDecodeError as exc:
            logger.warning("[MOCK CREATE] Invalid JSON in mock body: %s", exc)
            return jsonify({"error": f"mock field is not valid JSON: {exc}"}), 400

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


@app.route("/proxy/export/<identifier>/postman/", methods=["GET"])
@log_access
def export_postman(identifier):
    """Export a Postman v2.1 collection for all mocks of a proxy.

    Each mock endpoint+method becomes a request item. The collection uses
    a ``base_url`` variable so the user can switch between environments.
    """
    proxy = db_get_proxy(identifier)
    if not proxy:
        logger.warning("[EXPORT] Postman export failed — proxy '%s' not found", identifier)
        return jsonify({"error": "Proxy not found"}), 404

    mocked = proxy.get("mocked_requests", {})
    base_url = request.host_url.rstrip("/")
    proxy_base = f"{base_url}/proxy/{identifier}"
    collection_id = str(_uuid.uuid4())

    parsed_base = urlparse(base_url)
    host_part = parsed_base.netloc
    protocol = parsed_base.scheme or "https"

    items = []
    for endpoint, methods in sorted(mocked.items()):
        endpoint_clean = endpoint.lstrip("/")
        for method_name, mock_body in sorted(methods.items()):
            # Build a readable name
            item_name = f"{method_name} /{endpoint_clean}"

            # Determine if the mock expects a JSON body (POST/PUT/PATCH/DELETE with conditions or _store)
            has_body = method_name in ("POST", "PUT", "PATCH", "DELETE")

            # Build request URL parts
            raw_url = f"{proxy_base}/{endpoint_clean}"
            path_parts = ["proxy", identifier] + endpoint_clean.split("/")

            req_url = {
                "raw": raw_url,
                "protocol": protocol,
                "host": host_part.split("."),
                "path": path_parts,
            }

            # Build example body from mock structure hints
            example_body = _postman_example_body(mock_body)
            logger.debug("[EXPORT] Postman item: %s %s body_keys=%s",
                         method_name, endpoint, list(example_body.keys()) if isinstance(example_body, dict) else "N/A")

            item = {
                "name": item_name,
                "request": {
                    "method": method_name,
                    "header": [
                        {"key": "Content-Type", "value": "application/json", "type": "text"},
                    ],
                    "url": req_url,
                },
                "response": [],
            }

            if has_body and example_body:
                item["request"]["body"] = {
                    "mode": "raw",
                    "raw": json.dumps(example_body, indent=2),
                    "options": {"raw": {"language": "json"}},
                }

            items.append(item)

    # Also add the State API endpoints for this proxy
    state_url_base = f"{base_url}/proxy/state/{identifier}/"
    state_url_obj = {"raw": state_url_base, "protocol": protocol,
                     "host": host_part.split("."), "path": ["proxy", "state", identifier, ""]}
    state_items = [
        {
            "name": f"GET State ({identifier})",
            "request": {"method": "GET", "header": [], "url": state_url_obj},
            "response": [],
        },
        {
            "name": f"PUT State ({identifier})",
            "request": {
                "method": "PUT",
                "header": [{"key": "Content-Type", "value": "application/json", "type": "text"}],
                "url": state_url_obj,
                "body": {"mode": "raw", "raw": json.dumps({"key": "value"}, indent=2), "options": {"raw": {"language": "json"}}},
            },
            "response": [],
        },
        {
            "name": f"PATCH State ({identifier})",
            "request": {
                "method": "PATCH",
                "header": [{"key": "Content-Type", "value": "application/json", "type": "text"}],
                "url": state_url_obj,
                "body": {"mode": "raw", "raw": json.dumps({"key": "value"}, indent=2), "options": {"raw": {"language": "json"}}},
            },
            "response": [],
        },
    ]

    collection = {
        "info": {
            "_postman_id": collection_id,
            "name": f"PED Mock — {identifier}",
            "description": f"Auto-generated Postman collection for proxy '{identifier}'.\n\nAPI Domain: {proxy['api_domain']}\nGenerated: {datetime.now(timezone.utc).isoformat().replace('+00:00', 'Z')}",
            "schema": "https://schema.getpostman.com/json/collection/v2.1.0/collection.json",
        },
        "item": [
            {
                "name": "Mock Endpoints",
                "item": items,
            },
            {
                "name": "State Management",
                "item": state_items,
            },
        ],
        "variable": [
            {"key": "base_url", "value": base_url, "type": "string"},
        ],
    }

    logger.info("[EXPORT] Postman collection for '%s': %d mock items, %d state items",
                identifier, len(items), len(state_items))

    resp = Response(
        json.dumps(collection, indent=2),
        mimetype="application/json",
        headers={
            "Content-Disposition": f'attachment; filename="ped-{identifier}-postman.json"',
        },
    )
    return resp


def _postman_example_body(mock_data) -> dict:
    """Build a reasonable example request body from mock structure hints.

    Scans conditions, jsonget() references, and _store paths to infer
    which fields the endpoint expects in the request body.
    """
    fields = {}

    if not isinstance(mock_data, dict):
        return fields

    # Scan conditions for json-source fields
    for resp in mock_data.get("responses", []):
        for cond in resp.get("when", []):
            source = cond.get("source", "json")
            if source in ("json", "json_body") and cond.get("field"):
                field_name = cond["field"]
                example = cond.get("value", "")
                if cond.get("operator") in ("exists", "not_exists"):
                    example = f"example_{field_name}"
                fields[field_name] = example or f"example_{field_name}"

            # snippet conditions: extract jsonget('field') references
            if source == "snippet":
                expr = cond.get("value", "")
                for match in re.finditer(r"jsonget\(['\"]([^'\"]+)['\"]", expr):
                    fname = match.group(1)
                    if fname not in ("__NO__",):
                        fields[fname] = f"example_{fname}"

    # Scan _store for jsonget references in paths and values
    store_ops = mock_data.get("_store", [])
    if isinstance(store_ops, dict):
        store_ops = [store_ops]
    if isinstance(store_ops, list):
        for op in store_ops:
            if not isinstance(op, dict):
                continue
            for key in ("path", "value", "key"):
                val = op.get(key, "")
                if isinstance(val, str):
                    for match in re.finditer(r"jsonget\(([^,)]+)", val):
                        fname = match.group(1).strip("'\" ")
                        if fname and fname not in ("__NO__",):
                            fields[fname] = f"example_{fname}"

    # Scan the then block too
    for resp in mock_data.get("responses", []):
        then = resp.get("then", {})
        if isinstance(then, dict):
            for sops in [then.get("_store", [])]:
                if isinstance(sops, dict):
                    sops = [sops]
                if isinstance(sops, list):
                    for op in sops:
                        if not isinstance(op, dict):
                            continue
                        for key in ("path", "value", "key"):
                            val = op.get(key, "")
                            if isinstance(val, str):
                                for match in re.finditer(r"jsonget\(([^,)]+)", val):
                                    fname = match.group(1).strip("'\" ")
                                    if fname and fname not in ("__NO__",):
                                        fields[fname] = f"example_{fname}"

    # Also scan top-level value strings for jsonget
    for val in mock_data.values():
        if isinstance(val, str):
            for match in re.finditer(r"jsonget\(([^,)]+)", val):
                fname = match.group(1).strip("'\" ")
                if fname and fname not in ("__NO__", "_store", "conditions", "responses", "default"):
                    fields[fname] = f"example_{fname}"

    return fields


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
    """Get recent request history for a proxy with optional filters."""
    limit = request.args.get("limit", 50, type=int)
    method_filter = request.args.get("method")
    endpoint_filter = request.args.get("endpoint")
    status_min = request.args.get("status_min", type=int)
    status_max = request.args.get("status_max", type=int)
    source_filter = request.args.get("source")
    since = request.args.get("since")
    until = request.args.get("until")

    history = db_get_request_history(
        identifier, limit,
        method=method_filter, endpoint=endpoint_filter,
        status_min=status_min, status_max=status_max,
        source=source_filter, since=since, until=until,
    )
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
# Routes — User Management (proxy_users collection)
# ---------------------------------------------------------------------------


@app.route("/proxy/users/<identifier>/", methods=["GET"])
@require_auth
@log_access
def get_proxy_users(identifier: str):
    """List all users for a proxy (passwords excluded)."""
    users = list_proxy_users(identifier)
    logger.info("[USERS] Listed proxy='%s' count=%d", identifier, len(users))
    return jsonify({"proxy_id": identifier, "users": users})


@app.route("/proxy/users/<identifier>/", methods=["POST"])
@require_auth
@log_access
def upsert_proxy_user(identifier: str):
    """Create or update a user credential."""
    data = request.get_json(silent=True) or {}
    username = data.get("username", "").strip()
    password = data.get("password", "")
    if not username:
        return jsonify({"error": "username is required"}), 400
    if not password:
        return jsonify({"error": "password is required"}), 400
    create_proxy_user(identifier, username, password)
    return jsonify({"proxy_id": identifier, "username": username, "status": "ok"})


@app.route("/proxy/users/<identifier>/<username>/", methods=["DELETE"])
@require_auth
def remove_proxy_user(identifier: str, username: str):
    """Delete a user credential."""
    deleted = delete_proxy_user(identifier, username)
    if not deleted:
        return jsonify({"error": "user not found"}), 404
    return jsonify({"proxy_id": identifier, "username": username, "status": "deleted"})


# ---------------------------------------------------------------------------
# Routes — Mock Validation & Dry-Run (Feature 3)
# ---------------------------------------------------------------------------


@app.route("/proxy/mock/validate/", methods=["POST"])
@log_access
def validate_mock():
    """Validate a mock payload and optionally dry-run against a test request."""
    data = request.get_json(silent=True)
    if not data:
        return jsonify({"error": "Request body must be JSON"}), 400

    mock_payload = data.get("mock")
    if mock_payload is None:
        return jsonify({"error": "mock field is required"}), 400

    test_request = data.get("test_request", {})
    proxy_id = data.get("proxy_identifier", "__validate__")

    errors = []
    resolved_output = None
    store_ops_preview = []

    try:
        mock_copy = copy.deepcopy(mock_payload)

        t_headers = test_request.get("headers", {})
        t_body = test_request.get("body", {})
        t_params = test_request.get("params", {})
        t_method = test_request.get("method", "GET")
        t_url = test_request.get("url", "/test")

        if isinstance(mock_copy, list):
            if not mock_copy:
                errors.append("Sequence mock is empty (no steps)")
            else:
                mock_copy = mock_copy[0]

        if isinstance(mock_copy, dict) and "conditions" in mock_copy and "responses" in mock_copy:
            responses = mock_copy.get("responses", [])
            if not responses:
                errors.append("Conditional mock has no responses")
            selected = None
            for case in responses:
                case_conditions = case.get("when", [])
                try:
                    if _check_conditions(case_conditions, t_headers, t_body, t_params,
                                         path=t_url, method=t_method, proxy_id=proxy_id):
                        selected = case.get("then", {})
                        break
                except Exception as exc:
                    errors.append(f"Condition check error: {exc}")
            if selected is None:
                selected = mock_copy.get("default", {})
            mock_copy = selected

        if isinstance(mock_copy, dict):
            store_raw = mock_copy.pop("_store", None)
            if store_raw:
                store_ops_preview = [store_raw] if isinstance(store_raw, dict) else store_raw
            mock_copy.pop("_delay_ms", None)
            mock_copy.pop("_delay_profile", None)
            mock_copy.pop("_callback", None)
            mock_copy.pop("_cache_ttl", None)

        if isinstance(mock_copy, dict) and "status_code" in mock_copy and "body" in mock_copy:
            resolved_output = resolve_mock_data(
                copy.deepcopy(mock_copy["body"]), header=t_headers, json_body=t_body,
                params=t_params, url=t_url, proxy_id=proxy_id,
            )
        else:
            resolved_output = resolve_mock_data(
                copy.deepcopy(mock_copy), header=t_headers, json_body=t_body,
                params=t_params, url=t_url, proxy_id=proxy_id,
            )

    except Exception as exc:
        errors.append(f"Resolution error: {exc}")
        logger.warning("[VALIDATE] Mock validation failed: %s", exc)

    valid = len(errors) == 0
    logger.info("[VALIDATE] valid=%s errors=%d", valid, len(errors))
    result = {"valid": valid, "errors": errors}
    if resolved_output is not None:
        result["resolved_output"] = resolved_output
    if store_ops_preview:
        result["store_ops_preview"] = store_ops_preview
    return jsonify(result)


# ---------------------------------------------------------------------------
# Routes — Batch Mock Operations (Feature 7)
# ---------------------------------------------------------------------------


@app.route("/proxy/mock/batch/", methods=["POST"])
@log_access
def batch_mock_ops():
    """Execute multiple mock operations in a single request."""
    data = request.get_json(silent=True)
    if not data:
        return jsonify({"error": "Request body must be JSON"}), 400

    proxy_id = data.get("proxy_identifier")
    operations = data.get("operations", [])
    if not proxy_id:
        return jsonify({"error": "proxy_identifier is required"}), 400
    if not operations or not isinstance(operations, list):
        return jsonify({"error": "operations must be a non-empty list"}), 400

    results = []
    errors = []
    logger.info("[BATCH] Starting %d operations for proxy='%s'", len(operations), proxy_id)

    for i, op in enumerate(operations):
        action = op.get("action", "create")
        ep = op.get("end_point") or op.get("endpoint")
        mth = op.get("method", "*")
        mock_data = op.get("mock")

        if not ep:
            errors.append({"index": i, "error": "end_point required"})
            continue
        try:
            if action in ("create", "update"):
                if mock_data is None:
                    errors.append({"index": i, "error": "mock required for create/update"})
                    continue
                if isinstance(mock_data, str):
                    mock_data = json.loads(mock_data)
                old = db_upsert_mock(proxy_id, ep, mth, mock_data)
                results.append({"index": i, "action": action, "end_point": ep,
                                "method": mth, "replaced": old is not None})
            elif action == "delete":
                deleted = db_delete_mock(proxy_id, ep, mth)
                results.append({"index": i, "action": "delete", "end_point": ep,
                                "method": mth, "found": deleted is not None})
            else:
                errors.append({"index": i, "error": f"Unknown action: {action}"})
        except Exception as exc:
            errors.append({"index": i, "error": str(exc)})

    logger.info("[BATCH] Done proxy='%s' ok=%d errors=%d", proxy_id, len(results), len(errors))
    return jsonify({
        "proxy_identifier": proxy_id,
        "results": results,
        "errors": errors,
        "total_processed": len(results),
        "total_errors": len(errors),
    })


# ---------------------------------------------------------------------------
# Routes — State Snapshots (Feature 6)
# ---------------------------------------------------------------------------


@app.route("/proxy/state/<identifier>/snapshot/", methods=["POST"])
@log_access
def save_snapshot_route(identifier):
    """Save current proxy state as a named snapshot."""
    data = request.get_json(silent=True) or {}
    name = data.get("name", "").strip()
    if not name:
        name = f"snapshot_{datetime.now(timezone.utc).strftime('%Y%m%d_%H%M%S')}"
    result = db_save_snapshot(identifier, name)
    return jsonify(result)


@app.route("/proxy/state/<identifier>/snapshots/", methods=["GET"])
@log_access
def list_snapshots_route(identifier):
    """List all snapshots for a proxy."""
    snapshots = db_list_snapshots(identifier)
    return jsonify({"proxy_id": identifier, "snapshots": snapshots})


@app.route("/proxy/state/restore/<int:snapshot_id>/", methods=["POST"])
@log_access
def restore_snapshot_route(snapshot_id):
    """Restore proxy state from a snapshot."""
    result = db_restore_snapshot(snapshot_id)
    if result is None:
        return jsonify({"error": "Snapshot not found"}), 404
    return jsonify({"message": "State restored", **result})


@app.route("/proxy/state/snapshot/<int:snapshot_id>/", methods=["DELETE"])
@log_access
def delete_snapshot_route(snapshot_id):
    """Delete a state snapshot."""
    deleted = db_delete_snapshot(snapshot_id)
    if not deleted:
        return jsonify({"error": "Snapshot not found"}), 404
    return jsonify({"message": "Snapshot deleted", "id": snapshot_id})


# ---------------------------------------------------------------------------
# Routes — Mock Templates (Feature 13)
# ---------------------------------------------------------------------------


@app.route("/proxy/templates/", methods=["GET"])
@log_access
def list_templates_route():
    """List all mock templates."""
    category = request.args.get("category")
    templates = db_list_templates(category)
    return jsonify({"templates": templates})


@app.route("/proxy/templates/<int:template_id>/", methods=["GET"])
@log_access
def get_template_route(template_id):
    """Get a full mock template by ID."""
    tmpl = db_get_template(template_id)
    if not tmpl:
        return jsonify({"error": "Template not found"}), 404
    return jsonify(tmpl)


@app.route("/proxy/templates/", methods=["POST"])
@log_access
@require_auth
def create_template_route():
    """Create or update a mock template."""
    data = request.get_json(silent=True)
    if not data:
        return jsonify({"error": "Request body must be JSON"}), 400
    name = data.get("name", "").strip()
    if not name:
        return jsonify({"error": "name is required"}), 400
    template = data.get("template")
    if template is None:
        return jsonify({"error": "template is required"}), 400
    tid = db_upsert_template(
        name, template,
        data.get("description", ""),
        data.get("category", "general"),
    )
    return jsonify({"id": tid, "name": name, "message": "Template saved"})


@app.route("/proxy/templates/<int:template_id>/", methods=["DELETE"])
@log_access
@require_auth
def delete_template_route(template_id):
    """Delete a mock template."""
    if not db_delete_template(template_id):
        return jsonify({"error": "Template not found"}), 404
    return jsonify({"message": "Template deleted", "id": template_id})


# ---------------------------------------------------------------------------
# Routes — Mock Tagging (Feature 12)
# ---------------------------------------------------------------------------


@app.route("/proxy/mock/tags/", methods=["POST"])
@log_access
def update_mock_tags():
    """Update tags for a specific mock."""
    data = request.get_json(silent=True)
    if not data:
        return jsonify({"error": "Request body must be JSON"}), 400
    proxy_id = data.get("proxy_identifier")
    endpoint = data.get("end_point")
    method = data.get("method")
    tags = data.get("tags", "")
    if not proxy_id or not endpoint or not method:
        return jsonify({"error": "proxy_identifier, end_point, and method are required"}), 400
    if isinstance(tags, list):
        tags = ",".join(str(t).strip() for t in tags)
    db = _get_db()
    cur = db.execute(
        "UPDATE mocks SET tags = ? WHERE proxy_id = ? AND endpoint = ? AND method = ?",
        (tags, proxy_id, endpoint, method),
    )
    db.commit()
    if cur.rowcount == 0:
        return jsonify({"error": "Mock not found"}), 404
    logger.info("[TAGS] Updated proxy='%s' ep='%s' method='%s' tags='%s'",
                proxy_id, endpoint, method, tags)
    return jsonify({"proxy_identifier": proxy_id, "end_point": endpoint,
                    "method": method, "tags": tags})


@app.route("/proxy/mocks/<identifier>/", methods=["GET"])
@log_access
def list_mocks_with_tags(identifier):
    """List mocks for a proxy with tag info, optionally filtered by tag."""
    tag_filter = request.args.get("tag", "").strip()
    db = _get_db()
    rows = db.execute(
        "SELECT endpoint, method, response, tags FROM mocks WHERE proxy_id = ? ORDER BY endpoint",
        (identifier,),
    ).fetchall()
    mocks = []
    for r in rows:
        mock_tags = [t.strip() for t in (r["tags"] or "").split(",") if t.strip()]
        if tag_filter and tag_filter not in mock_tags:
            continue
        mocks.append({
            "endpoint": r["endpoint"],
            "method": r["method"],
            "tags": mock_tags,
            "response_preview": r["response"][:200],
        })
    return jsonify({"proxy_id": identifier, "mocks": mocks, "count": len(mocks)})


# ---------------------------------------------------------------------------
# Routes — Mock Analytics (Feature 14)
# ---------------------------------------------------------------------------


@app.route("/proxy/analytics/<identifier>/", methods=["GET"])
@log_access
@require_auth
def mock_analytics(identifier):
    """Compute analytics from request history for a proxy."""
    db = _get_db()
    total = db.execute(
        "SELECT COUNT(*) as c FROM request_history WHERE proxy_id = ?", (identifier,)
    ).fetchone()["c"]
    source_counts = {}
    for row in db.execute(
        "SELECT source, COUNT(*) as c FROM request_history WHERE proxy_id = ? GROUP BY source",
        (identifier,),
    ).fetchall():
        source_counts[row["source"]] = row["c"]
    method_counts = {}
    for row in db.execute(
        "SELECT method, COUNT(*) as c FROM request_history WHERE proxy_id = ? GROUP BY method",
        (identifier,),
    ).fetchall():
        method_counts[row["method"]] = row["c"]
    avg_lat = db.execute(
        "SELECT AVG(duration_ms) as avg_ms FROM request_history "
        "WHERE proxy_id = ? AND duration_ms IS NOT NULL", (identifier,),
    ).fetchone()["avg_ms"]
    error_count = db.execute(
        "SELECT COUNT(*) as c FROM request_history WHERE proxy_id = ? AND response_status >= 400",
        (identifier,),
    ).fetchone()["c"]
    top_endpoints = []
    for row in db.execute(
        "SELECT endpoint, method, COUNT(*) as hits FROM request_history "
        "WHERE proxy_id = ? GROUP BY endpoint, method ORDER BY hits DESC LIMIT 10",
        (identifier,),
    ).fetchall():
        top_endpoints.append(dict(row))
    # Stale mocks
    mock_eps = set()
    for row in db.execute(
        "SELECT endpoint, method FROM mocks WHERE proxy_id = ?", (identifier,)
    ).fetchall():
        mock_eps.add((row["endpoint"], row["method"]))
    hit_eps = set()
    for row in db.execute(
        "SELECT DISTINCT endpoint, method FROM request_history WHERE proxy_id = ? AND source = 'mock'",
        (identifier,),
    ).fetchall():
        hit_eps.add((row["endpoint"], row["method"]))
    stale = [{"endpoint": e, "method": m} for e, m in mock_eps - hit_eps]

    logger.info("[ANALYTICS] proxy='%s' total=%d", identifier, total)
    return jsonify({
        "proxy_id": identifier, "total_requests": total,
        "by_source": source_counts, "by_method": method_counts,
        "avg_latency_ms": round(avg_lat, 1) if avg_lat else None,
        "error_rate": round(error_count / total * 100, 1) if total > 0 else 0,
        "error_count": error_count,
        "top_endpoints": top_endpoints, "stale_mocks": stale,
    })


# ---------------------------------------------------------------------------
# Routes — Proxy Health Dashboard (Feature 17)
# ---------------------------------------------------------------------------


@app.route("/proxy/health/<identifier>/", methods=["GET"])
@log_access
def proxy_health(identifier):
    """Check health of a proxy's upstream domain."""
    api_domain = db_get_proxy_domain(identifier)
    if api_domain is None:
        return jsonify({"error": "Proxy not found"}), 404

    status = "unknown"
    latency_ms = None
    error_msg = None
    try:
        parsed = urlparse(api_domain if "://" in api_domain else f"https://{api_domain}")
        health_url = f"{parsed.scheme}://{parsed.netloc}/"
        start = time.time()
        resp = http_requests.head(health_url, timeout=5, allow_redirects=True)
        latency_ms = int((time.time() - start) * 1000)
        if resp.status_code < 400:
            status = "healthy"
        elif resp.status_code < 500:
            status = "degraded"
        else:
            status = "unhealthy"
    except http_requests.exceptions.Timeout:
        status = "unhealthy"
        error_msg = "Connection timed out"
    except http_requests.exceptions.ConnectionError as exc:
        status = "unhealthy"
        error_msg = f"Connection failed: {exc}"
    except Exception as exc:
        status = "unhealthy"
        error_msg = str(exc)

    db = _get_db()
    mock_count = db.execute(
        "SELECT COUNT(*) as c FROM mocks WHERE proxy_id = ?", (identifier,)
    ).fetchone()["c"]
    history_count = db.execute(
        "SELECT COUNT(*) as c FROM request_history WHERE proxy_id = ?", (identifier,)
    ).fetchone()["c"]

    logger.info("[HEALTH] proxy='%s' domain=%s status=%s latency=%s",
                identifier, api_domain, status, latency_ms)
    result = {
        "proxy_id": identifier, "api_domain": api_domain,
        "upstream_status": status, "upstream_latency_ms": latency_ms,
        "mock_count": mock_count, "history_count": history_count,
    }
    if error_msg:
        result["error"] = error_msg
    return jsonify(result)


# ---------------------------------------------------------------------------
# Routes — Storage / Space Optimization
# ---------------------------------------------------------------------------


@app.route("/proxy/storage/", methods=["GET"])
@log_access
@require_auth
def storage_info():
    """Return database size and storage usage info."""
    db_size = 0
    try:
        db_size = os.path.getsize(DB_PATH)
    except OSError:
        pass
    db = _get_db()
    history_count = db.execute("SELECT COUNT(*) as c FROM request_history").fetchone()["c"]
    mock_count = db.execute("SELECT COUNT(*) as c FROM mocks").fetchone()["c"]
    proxy_count = db.execute("SELECT COUNT(*) as c FROM proxies").fetchone()["c"]
    snapshot_count = 0
    try:
        snapshot_count = db.execute("SELECT COUNT(*) as c FROM state_snapshots").fetchone()["c"]
    except Exception:
        pass
    return jsonify({
        "db_size_bytes": db_size,
        "db_size_mb": round(db_size / (1024 * 1024), 2),
        "history_count": history_count, "mock_count": mock_count,
        "proxy_count": proxy_count, "snapshot_count": snapshot_count,
        "history_limit": REQUEST_HISTORY_LIMIT,
    })


@app.route("/proxy/storage/cleanup/", methods=["POST"])
@log_access
@require_auth
def storage_cleanup():
    """Clean up old history entries and vacuum the database."""
    data = request.get_json(silent=True) or {}
    keep_days = data.get("keep_days", 7)
    vacuum = data.get("vacuum", True)

    db = _get_db()
    cutoff = (datetime.now(timezone.utc) - timedelta(days=keep_days)).strftime("%Y-%m-%d %H:%M:%S")
    cur = db.execute("DELETE FROM request_history WHERE created_at < ?", (cutoff,))
    deleted_history = cur.rowcount
    db.commit()

    deleted_snapshots = 0
    try:
        cur2 = db.execute(
            "DELETE FROM state_snapshots WHERE proxy_id NOT IN (SELECT identifier FROM proxies)"
        )
        deleted_snapshots = cur2.rowcount
        db.commit()
    except Exception:
        pass

    size_before = os.path.getsize(DB_PATH)
    if vacuum:
        conn = sqlite3.connect(DB_PATH)
        conn.execute("VACUUM")
        conn.close()
    size_after = os.path.getsize(DB_PATH)

    logger.info("[CLEANUP] deleted_history=%d deleted_snapshots=%d vacuum=%s "
                "size_before=%d size_after=%d saved=%d",
                deleted_history, deleted_snapshots, vacuum,
                size_before, size_after, size_before - size_after)
    return jsonify({
        "deleted_history": deleted_history,
        "deleted_snapshots": deleted_snapshots,
        "vacuumed": vacuum,
        "size_before_bytes": size_before,
        "size_after_bytes": size_after,
        "saved_bytes": size_before - size_after,
        "saved_mb": round((size_before - size_after) / (1024 * 1024), 2),
    })


# ---------------------------------------------------------------------------
# Routes — Suggestions
# ---------------------------------------------------------------------------


@app.route("/suggestions/", methods=["POST"])
@log_access
def submit_suggestion():
    """Submit a suggestion (no auth required — anyone can suggest)."""
    data = request.get_json(silent=True)
    if not data:
        return jsonify({"error": "Request body must be JSON"}), 400
    message = (data.get("message") or "").strip()
    if not message:
        return jsonify({"error": "message is required"}), 400
    if len(message) > 2000:
        return jsonify({"error": "message too long (max 2000 chars)"}), 400
    name = (data.get("name") or "Anonymous").strip()[:100]

    db = _get_db()
    db.execute(
        "INSERT INTO suggestions (name, message) VALUES (?, ?)",
        (name, message),
    )
    db.commit()
    logger.info("[SUGGESTION] New from '%s': %s", name, message[:80])
    return jsonify({"message": "Thank you for your suggestion!", "name": name})


@app.route("/suggestions/", methods=["GET"])
@log_access
@require_auth
def list_suggestions():
    """List all suggestions (auth required)."""
    db = _get_db()
    rows = db.execute(
        "SELECT id, name, message, created_at FROM suggestions ORDER BY id DESC"
    ).fetchall()
    suggestions = [dict(r) for r in rows]
    logger.info("[SUGGESTION] Listed count=%d", len(suggestions))
    return jsonify({"suggestions": suggestions, "count": len(suggestions)})


@app.route("/suggestions/<int:suggestion_id>/", methods=["DELETE"])
@log_access
@require_auth
def delete_suggestion(suggestion_id):
    """Delete a suggestion (auth required)."""
    db = _get_db()
    cur = db.execute("DELETE FROM suggestions WHERE id = ?", (suggestion_id,))
    db.commit()
    if cur.rowcount == 0:
        return jsonify({"error": "Suggestion not found"}), 404
    logger.info("[SUGGESTION] Deleted id=%d", suggestion_id)
    return jsonify({"message": "Suggestion deleted", "id": suggestion_id})


# ---------------------------------------------------------------------------
# Routes — Proxy Passthrough
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# Latency Simulation Profiles (Feature 8)
# ---------------------------------------------------------------------------

_DELAY_PROFILE_RE = re.compile(r"^(\w+)\(([^)]*)\)$")


def _resolve_delay_profile(profile: str) -> int:
    """Parse a delay profile string and return a delay in ms.

    Supported profiles:
      uniform(min, max) — uniform random between min and max ms
      normal(mean, stddev) — normal distribution, clamped to [0, 30000]
      spike(base, spike, pct) — base ms most of the time, spike ms pct% of the time
    """
    m = _DELAY_PROFILE_RE.match(profile.strip())
    if not m:
        logger.warning("[DELAY] Invalid delay profile: %r", profile)
        return 0
    name = m.group(1).lower()
    args = [a.strip() for a in m.group(2).split(",") if a.strip()]
    try:
        if name == "uniform" and len(args) == 2:
            lo, hi = int(args[0]), int(args[1])
            delay = random.randint(min(lo, hi), max(lo, hi))
            logger.debug("[DELAY] Profile uniform(%d,%d) -> %dms", lo, hi, delay)
            return delay
        if name == "normal" and len(args) == 2:
            mean, stddev = float(args[0]), float(args[1])
            delay = max(0, int(random.gauss(mean, stddev)))
            logger.debug("[DELAY] Profile normal(%.0f,%.0f) -> %dms", mean, stddev, delay)
            return delay
        if name == "spike" and len(args) == 3:
            base, spike, pct = int(args[0]), int(args[1]), float(args[2])
            delay = spike if random.random() * 100 < pct else base
            logger.debug("[DELAY] Profile spike(%d,%d,%.1f%%) -> %dms", base, spike, pct, delay)
            return delay
    except (ValueError, TypeError) as exc:
        logger.warning("[DELAY] Profile parse error %r: %s", profile, exc)
    logger.warning("[DELAY] Unknown delay profile: %r", profile)
    return 0


# ---------------------------------------------------------------------------
# Webhook / Callback Simulation (Feature 5)
# ---------------------------------------------------------------------------


def _schedule_callback(callback: dict, headers: dict, json_body: dict,
                       params: dict, url: str, proxy_id: str) -> None:
    """Schedule an async HTTP callback from a mock response _callback key.

    Shape: {"url": "...", "method": "POST", "body": {...}, "headers": {...}, "delay_ms": 2000}
    """
    cb_url = callback.get("url")
    if not cb_url or not isinstance(cb_url, str):
        logger.warning("[CALLBACK] Missing or invalid url in _callback")
        return

    # Resolve the URL through the resolver pipeline
    cb_url = str(_resolve_value(cb_url, headers, json_body, params, url, proxy_id))

    # SSRF guard — restrict callback URLs
    parsed = urlparse(cb_url)
    if not parsed.scheme or not parsed.hostname:
        logger.warning("[CALLBACK] Invalid callback URL: %s", cb_url)
        return
    if not _is_domain_allowed(parsed.hostname):
        logger.warning("[CALLBACK] Callback URL domain not allowed: %s", cb_url)
        return

    cb_method = (callback.get("method") or "POST").upper()
    cb_delay_ms = min(int(callback.get("delay_ms", 2000)), 30_000)
    cb_headers = callback.get("headers", {})
    cb_body = callback.get("body", {})

    # Resolve body through mock data pipeline
    if isinstance(cb_body, (dict, list)):
        cb_body = resolve_mock_data(
            copy.deepcopy(cb_body),
            header=headers, json_body=json_body, params=params,
            url=url, proxy_id=proxy_id,
        )
    elif isinstance(cb_body, str):
        cb_body = _resolve_value(cb_body, headers, json_body, params, url, proxy_id)

    # Resolve header values
    resolved_headers = {}
    for k, v in cb_headers.items():
        if isinstance(v, str):
            resolved_headers[k] = str(_resolve_value(v, headers, json_body, params, url, proxy_id))
        else:
            resolved_headers[k] = v
    if "Content-Type" not in resolved_headers:
        resolved_headers["Content-Type"] = "application/json"

    def _fire():
        try:
            logger.info("[CALLBACK] Firing %s %s (delay=%dms proxy=%s)",
                        cb_method, cb_url, cb_delay_ms, proxy_id)
            resp = http_requests.request(
                cb_method, cb_url,
                json=cb_body if isinstance(cb_body, (dict, list)) else None,
                data=str(cb_body) if not isinstance(cb_body, (dict, list)) else None,
                headers=resolved_headers,
                timeout=FORWARD_TIMEOUT,
            )
            logger.info("[CALLBACK] Response %s %s status=%d", cb_method, cb_url, resp.status_code)
        except Exception as exc:
            logger.error("[CALLBACK] Failed %s %s: %s", cb_method, cb_url, exc)

    delay_s = max(0, cb_delay_ms) / 1000.0
    timer = threading.Timer(delay_s, _fire)
    timer.daemon = True
    timer.start()
    logger.info("[CALLBACK] Scheduled %s %s in %dms", cb_method, cb_url, cb_delay_ms)


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

        # Per-proxy CORS override via state key
        try:
            _proxy_st = db_get_state(identifier)
            _cors_override = _proxy_st.get("_cors_origins")
            if _cors_override:
                g._proxy_cors_origins = _cors_override
        except Exception:
            pass

        # Capture request info for history
        req_headers = {k: v for k, v in request.headers.items() if k.lower() != 'host'}
        req_body = request.get_data(as_text=True)[:2000] or None

        # --- Redirect mode ---
        if identifier.endswith("_REDIRECT"):
            if not _is_domain_allowed(api_domain):
                return jsonify({"error": "Redirect target domain not allowed"}), 403
            # API.__init__ captures flask_request.args as self.params; do NOT also
            # append the query string to the URL or requests will duplicate it.
            try:
                fwd_api = API(request, api_url)
                flask_resp, duration_ms, raw_resp = fwd_api.forward()
                db_log_request(
                    identifier, endpoint, method, req_headers, req_body, query_string,
                    raw_resp.status_code, raw_resp.content[:2000].decode("utf-8", errors="replace"), "redirect", duration_ms,
                )
                return flask_resp
            except http_requests.exceptions.RequestException as exc:
                logger.error("Redirect forward failed: %s", exc)
                return jsonify({"error": "Redirect forward failed"}), 502

        # --- Mock lookup (with inheritance from parent proxy) ---
        mock_requests = db_get_mocks_for_proxy(identifier)
        # Feature 16: parent proxy inheritance — child overrides parent
        try:
            _p_state = db_get_state(identifier)
            parent_proxy = _p_state.get("_parent_proxy")
        except Exception:
            parent_proxy = None
        if parent_proxy and isinstance(parent_proxy, str):
            parent_mocks = db_get_mocks_for_proxy(parent_proxy)
            # Merge: parent first, child overrides
            merged = {}
            for ep, methods_d in parent_mocks.items():
                merged.setdefault(ep, {}).update(methods_d)
            for ep, methods_d in mock_requests.items():
                merged.setdefault(ep, {}).update(methods_d)
            mock_requests = merged
            logger.debug("[INHERIT] Merged mocks from parent '%s' for '%s'",
                         parent_proxy, identifier)
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
                    if _check_conditions(case_conditions, headers, json_body, params, path=endpoint, method=method, proxy_id=identifier):
                        selected = case.get("then", {})
                        logger.info("[CONDITIONAL] Matched condition: %s", case_conditions)
                        break
                if selected is None:
                    selected = mock_data_copy.get("default", {"error": "No condition matched"})
                    logger.info("[CONDITIONAL] No condition matched, using default")
                mock_data_copy = selected

            # --- Apply _store side-effects (mongo-like per-proxy state) ---
            # Popped before delay so it runs exactly once per response, regardless
            # of whether we hit the plain-dict path or the {status_code, body} path.
            # Placed after sequence + conditional selection so _store inside a
            # sequence step or conditional "then" still applies.
            if isinstance(mock_data_copy, dict):
                _apply_store_ops(
                    mock_data_copy.pop("_store", None),
                    identifier, headers, json_body, params, api_url,
                )

            # --- Response delay (supports placeholder strings, or _delay_profile
            #     for randomized latency: uniform(min,max), normal(mean,stddev),
            #     spike(base,spike,pct)) ---
            delay_raw = None
            delay_profile = None
            if isinstance(mock_data_copy, dict):
                delay_raw = mock_data_copy.pop("_delay_ms", None)
                delay_profile = mock_data_copy.pop("_delay_profile", None)
            if delay_profile and isinstance(delay_profile, str):
                delay_ms = _resolve_delay_profile(delay_profile)
            else:
                delay_ms = _resolve_to_int(
                    delay_raw, 0, headers, json_body, params, api_url, "DELAY",
                    proxy_id=identifier,
                )
            _MAX_DELAY_MS = 30_000
            if delay_ms > _MAX_DELAY_MS:
                logger.warning("[DELAY] Clamping delay from %dms to %dms", delay_ms, _MAX_DELAY_MS)
                delay_ms = _MAX_DELAY_MS
            if delay_ms > 0:
                logger.info("[DELAY] Sleeping %dms before responding", delay_ms)
                time.sleep(delay_ms / 1000.0)

            # --- Webhook / Callback simulation ---
            if isinstance(mock_data_copy, dict):
                _callback = mock_data_copy.pop("_callback", None)
                if _callback and isinstance(_callback, dict):
                    _schedule_callback(
                        _callback, headers, json_body, params, api_url, identifier
                    )

            # --- Mock response caching: pop _cache_ttl for later use ---
            _cache_ttl_raw = None
            if isinstance(mock_data_copy, dict):
                _cache_ttl_raw = mock_data_copy.pop("_cache_ttl", None)

            # Check cache if _cache_ttl is set on the original mock
            if _cache_ttl_raw is not None:
                cache_ttl_s = max(0, int(_cache_ttl_raw))
                if cache_ttl_s > 0:
                    ckey = _mock_cache_key(identifier, endpoint, method, query_string)
                    cached = _mock_cache_get(ckey, cache_ttl_s)
                    if cached is not None:
                        duration_ms = int((time.time() - start_time) * 1000)
                        db_log_request(
                            identifier, endpoint, method, req_headers, req_body,
                            query_string, 200, json.dumps(cached)[:2000],
                            "mock", duration_ms,
                        )
                        logger.info("[CACHE] Serving cached response for %s %s/%s",
                                    method, identifier, endpoint)
                        return jsonify(cached), 200

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
                # Store in cache if _cache_ttl is set
                if _cache_ttl_raw is not None and int(_cache_ttl_raw) > 0:
                    _mock_cache_set(
                        _mock_cache_key(identifier, endpoint, method, query_string),
                        processed,
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
            # Store in cache if _cache_ttl is set
            if _cache_ttl_raw is not None and int(_cache_ttl_raw) > 0:
                _mock_cache_set(
                    _mock_cache_key(identifier, endpoint, method, query_string),
                    processed,
                )
            duration_ms = int((time.time() - start_time) * 1000)
            db_log_request(
                identifier, endpoint, method, req_headers, req_body, query_string,
                200, json.dumps(processed)[:2000], "mock", duration_ms,
            )
            return jsonify(processed), 200

        # --- Mock-only mode ---
        # When enabled, unmatched requests return 501 instead of forwarding
        # upstream. The caller should fall back to calling the real API directly
        # (from its own IP, which may be whitelisted at the target).
        # Enable via: identifier suffix _MOCKONLY, or state flag _mock_only: true.
        _is_mock_only = identifier.endswith("_MOCKONLY")
        if not _is_mock_only:
            proxy_state = db_get_state(identifier)
            _is_mock_only = proxy_state.get("_mock_only", False) is True

        if _is_mock_only:
            duration_ms = int((time.time() - start_time) * 1000)
            logger.info("[MOCK MISS] mock_only=true — returning 501 for %s %s/%s",
                        method, identifier, endpoint)
            db_log_request(
                identifier, endpoint, method, req_headers, req_body, query_string,
                501, '{"error":"not_mocked"}', "mock_miss", duration_ms,
            )
            return jsonify({
                "error": "No mock registered for this endpoint",
                "mock_only": True,
                "proxy_id": identifier,
                "method": method,
                "endpoint": f"/{endpoint}",
                "api_domain": api_domain,
            }), 501

        # --- SSRF guard ---
        if not _is_domain_allowed(api_domain):
            return jsonify({"error": "Target domain not allowed"}), 403

        # --- Forward to real API (with optional request/response transforms) ---
        try:
            fwd_api = API(request, api_url)
            # Feature 15: Request/Response transforms from state
            _transforms_state = None
            try:
                _transforms_state = db_get_state(identifier)
                req_transforms = _transforms_state.get("_request_transforms")
                if req_transforms and isinstance(req_transforms, dict):
                    if "add_headers" in req_transforms:
                        for k, v in req_transforms["add_headers"].items():
                            fwd_api.headers[k] = v
                    logger.debug("[TRANSFORM] Applied request transforms for '%s'", identifier)
            except Exception:
                pass

            flask_resp, duration_ms, raw_resp = fwd_api.forward()

            try:
                if _transforms_state:
                    resp_transforms = _transforms_state.get("_response_transforms")
                    if resp_transforms and isinstance(resp_transforms, dict):
                        if "add_headers" in resp_transforms:
                            resp_obj = flask_resp[0] if isinstance(flask_resp, tuple) else flask_resp
                            for k, v in resp_transforms["add_headers"].items():
                                resp_obj.headers[k] = v
                        logger.debug("[TRANSFORM] Applied response transforms for '%s'", identifier)
            except Exception:
                pass

            db_log_request(
                identifier, endpoint, method, req_headers, req_body, query_string,
                raw_resp.status_code, raw_resp.content[:2000].decode("utf-8", errors="replace"), "forward", duration_ms,
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


@app.route("/suggestions/view/")
@log_access
@require_login
def suggestions_page():
    """View all user suggestions (auth required)."""
    return render_template("suggestions.html")


# ---------------------------------------------------------------------------
# Error handler
# ---------------------------------------------------------------------------


@app.errorhandler(Exception)
def handle_exception(exc):
    if isinstance(exc, HTTPException):
        return exc
    logger.exception("Unhandled exception: %s", exc)
    return jsonify({"error": "An unexpected error occurred"}), 500


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

_ensure_schema_ready()

if __name__ == "__main__":
    port = int(os.environ.get("PED_PORT", 8000))
    app.run(host="0.0.0.0", port=port, debug=DEBUG)
