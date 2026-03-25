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
from base64 import b64decode, b64encode
from functools import wraps
from urllib.parse import urlparse

from dotenv import load_dotenv

# Resolve paths relative to this file (needed for PythonAnywhere WSGI)
_BASE_DIR = os.path.dirname(os.path.abspath(__file__))

load_dotenv(os.path.join(_BASE_DIR, ".env"))

from Crypto.Cipher import AES
from Crypto.Util.Padding import pad, unpad
from flask import Flask, Response, g, jsonify, render_template, request
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

app = Flask(__name__)

# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------

logging.basicConfig(
    level=logging.DEBUG if DEBUG else logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
logger = logging.getLogger("pedapp")


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
        """
    )
    conn.commit()
    conn.close()
    logger.info("Database initialized at %s", DB_PATH)


# --- DB helper functions ---


def db_create_proxy(identifier: str, api_domain: str) -> dict | None:
    """Create or replace a proxy. Returns old mocks if proxy existed."""
    db = _get_db()
    old_mocks = db_get_mocks_for_proxy(identifier)

    db.execute(
        "INSERT INTO proxies (identifier, api_domain) VALUES (?, ?) "
        "ON CONFLICT(identifier) DO UPDATE SET api_domain = excluded.api_domain",
        (identifier, api_domain),
    )
    # Clear old mocks on re-register
    if old_mocks:
        db.execute("DELETE FROM mocks WHERE proxy_id = ?", (identifier,))
    db.commit()
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

    # Check proxy exists
    proxy = db.execute(
        "SELECT 1 FROM proxies WHERE identifier = ?", (proxy_id,)
    ).fetchone()
    if not proxy:
        return None  # sentinel: proxy not found

    # Get old mock
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
    return deleted_mock


def db_delete_proxy(identifier: str) -> bool:
    """Delete a proxy and all its mocks (cascade). Returns True if found."""
    db = _get_db()
    cursor = db.execute(
        "DELETE FROM proxies WHERE identifier = ?", (identifier,)
    )
    db.commit()
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


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def try_or(fn, default=None):
    """One-liner try/except.  Usage: try_or(lambda: obj.attr, None)"""
    try:
        return fn()
    except Exception:
        return default


def log_access(f):
    @wraps(f)
    def decorated(*args, **kwargs):
        logger.info(
            "%-6s %s  from %s", request.method, request.path, request.remote_addr
        )
        return f(*args, **kwargs)

    return decorated


def require_auth(f):
    """Simple Bearer-token gate. Skipped when API_TOKEN is empty."""

    @wraps(f)
    def decorated(*args, **kwargs):
        if not API_TOKEN:
            return f(*args, **kwargs)
        auth = request.headers.get("Authorization", "")
        if auth != f"Bearer {API_TOKEN}":
            return jsonify({"error": "Unauthorized"}), 401
        return f(*args, **kwargs)

    return decorated


def _is_domain_allowed(domain: str) -> bool:
    """Return True when the domain is on the allowlist (or no allowlist is set)."""
    if not ALLOWED_PROXY_DOMAINS:
        return True
    parsed = urlparse(domain)
    host = parsed.hostname or parsed.path
    return any(host.endswith(allowed) for allowed in ALLOWED_PROXY_DOMAINS)


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
    # Temporarily replace <param> placeholders, escape the rest, then restore
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

FORWARD_TIMEOUT = int(os.environ.get("PED_FORWARD_TIMEOUT", "30"))


class API:
    """Captures a Flask request and forwards it to a target URL,
    handling JSON, form-encoded, multipart, and raw body types.
    Logs the equivalent curl command for debugging."""

    # Headers to strip from the incoming request before forwarding
    _HOP_BY_HOP = frozenset({
        'host', 'content-length', 'transfer-encoding',
        'connection', 'keep-alive', 'upgrade',
    })
    # Headers to exclude from the upstream response when building our response
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
        # Ensure Accept */* like Postman if not explicitly set
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
        response = http_requests.request(self.method, self.url, **kwargs)
        logger.info("[FORWARD] %s %s -> %s (%s bytes) response-type=%s",
                     self.method, self.url, response.status_code,
                     len(response.content), response.headers.get('Content-Type', 'unknown'))
        if response.status_code >= 400:
            logger.warning("[FORWARD] Upstream error %s: %s", response.status_code, response.text[:300])
        return self._build_response(response)

    def _build_response(self, response):
        # Collect passthrough headers from upstream (skip hop-by-hop)
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
            if k.lower() != 'content-type':  # already set above
                resp.headers[k] = v
        return resp


# ---------------------------------------------------------------------------
# MockMatcher — structured mock lookup with query string variants
# ---------------------------------------------------------------------------


class MockMatcher:
    """Finds matching mock responses by trying multiple endpoint variants
    (with/without leading slash, with/without query string, full URL)
    and pattern matching for <param> placeholders."""

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
        """Return (mock_key, mock_data) or (None, None)."""
        # Exact match first
        for variant in self.variants:
            mock_methods = self.mock_requests.get(variant)
            if mock_methods and mock_methods.get(method):
                logger.info("[MOCK HIT] %s matched exact key '%s'", method, variant)
                return variant, mock_methods[method]

        # Pattern match (keys with <param> placeholders)
        for mock_key, mock_methods in self.mock_requests.items():
            if '<' not in mock_key:
                continue
            regex = path_to_regex(mock_key)
            for variant in self.variants:
                if regex.match(variant) and mock_methods.get(method):
                    logger.info("[MOCK HIT] %s matched pattern key '%s' via '%s'", method, mock_key, variant)
                    return mock_key, mock_methods[method]

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
    """Generate alphanumeric string from pairs like '[2,3,4,1]' or '2,3,4,1'."""
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

_SNIPPET_FUNCTIONS = {
    "abs": abs,
    "int": int,
    "float": float,
    "str": str,
    "len": len,
    "min": min,
    "max": max,
    "round": round,
    "sum": sum,
    "sorted": sorted,
    "list": list,
    "dict": dict,
    "tuple": tuple,
    "bool": bool,
    "enumerate": enumerate,
    "zip": zip,
    "range": range,
    "map": map,
    "filter": filter,
    "any": any,
    "all": all,
    "isinstance": isinstance,
    "type": type,
}

_SNIPPET_MAX_LENGTH = 2000


def safe_eval_snippet(snippet: str) -> str:
    """Evaluate a Python expression in a sandboxed environment."""
    snippet = snippet.strip()
    if not snippet:
        return ""
    if len(snippet) > _SNIPPET_MAX_LENGTH:
        raise ValueError(
            f"snippet() expression too long ({len(snippet)} chars, max {_SNIPPET_MAX_LENGTH})"
        )

    evaluator = EvalWithCompoundTypes(
        functions=_SNIPPET_FUNCTIONS,
        names={},
    )

    try:
        result = evaluator.eval(snippet)
        return result if isinstance(result, str) else str(result)
    except Exception as exc:
        logger.warning("snippet() evaluation failed: %s — expression: %s", exc, snippet[:200])
        raise ValueError(f"snippet() evaluation error: {exc}") from exc


def _resolve_value(value: str, header: dict, json_data: dict, params: dict, url: str | None) -> str | None:
    """Resolve a single template-style string value from mock data."""
    if value.startswith("headerget(") and value.endswith(")"):
        return header.get(value[10:-1], value)
    if value.startswith("jsonget(") and value.endswith(")"):
        return json_data.get(value[8:-1], value)
    if value.startswith("paramget(") and value.endswith(")"):
        return params.get(value[9:-1], value)
    if value.startswith("pathparamget(") and value.endswith(")"):
        return extract_path_param(value[13:-1], url)
    if value.startswith("alnum(") and value.endswith(")"):
        return _safe_alnum(value[6:-1])
    if value.startswith("snippet(") and value.endswith(")"):
        return safe_eval_snippet(value[8:-1])
    for name, gen in _SAFE_GENERATORS.items():
        if value.startswith(f"{name}(") and value.endswith(")"):
            return gen(value[len(name) + 1 : -1])
    return value


def resolve_mock_data(data, header=None, json_body=None, params=None, url=None):
    """Walk a mock response dict/list and resolve all placeholder values."""
    header = header or {}
    json_data = json_body or {}
    params = params or {}

    def process(node):
        if isinstance(node, dict):
            for key, value in node.items():
                if isinstance(value, str):
                    node[key] = _resolve_value(value, header, json_data, params, url)
                elif isinstance(value, (dict, list)):
                    process(value)
        elif isinstance(node, list):
            for i, item in enumerate(node):
                if isinstance(item, str):
                    node[i] = _resolve_value(item, header, json_data, params, url)
                elif isinstance(item, (dict, list)):
                    process(item)
        return node

    logger.debug("resolve_mock_data called: url=%s", url)
    return process(data)


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
# Routes — Encrypt / Decrypt / Prettify
# ---------------------------------------------------------------------------


@app.route("/")
@log_access
def index():
    return render_template("index.html")


@app.route("/ped/encrypt", methods=["POST"])
@log_access
@require_auth
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

    encrypted = EncryptHelper.encrypt(secret, enc_iv, normal_data)
    return jsonify({"encrypted": json.dumps(encrypted)})


@app.route("/ped/decrypt", methods=["POST"])
@log_access
@require_auth
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
        encrypted_data = json.loads(encrypted_data)

    decrypted = EncryptHelper.decrypt(secret, enc_iv, encrypted_data)
    return jsonify({"decrypted": json.dumps(decrypted, indent=4)})


@app.route("/ped/prettify", methods=["POST"])
@log_access
@require_auth
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
@require_auth
def create_proxy():
    request_data = request.json
    if not request_data:
        return jsonify({"error": "Request body must be JSON"}), 400
    api_domain = request_data.get("api_domain", "")

    if not _is_domain_allowed(api_domain):
        return jsonify({"error": "Domain not in allowlist"}), 403

    identifier = request_data.get("identifier") or shortuuid.uuid()
    old_mocks = db_create_proxy(identifier, api_domain)

    return jsonify(
        {
            "identifier": identifier,
            "message": "api mocker created successfully",
            "old_mocks": old_mocks,
        }
    )


@app.route("/proxy/mock/create/", methods=["POST"])
@log_access
@require_auth
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

    return jsonify(
        {
            "proxy_identifier": proxy_identifier,
            "end_point": end_point,
            "method": method,
            "new_mock": new_mock,
            "old_mock": old_mock,
        }
    )


@app.route("/proxy/mock/delete/", methods=["POST"])
@log_access
@require_auth
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

    logger.info("[MOCK DELETE] %s %s %s", proxy_id, method, endpoint)

    return jsonify({
        "proxy_identifier": proxy_id,
        "end_point": endpoint,
        "method": method,
        "deleted_mock": deleted_mock
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
@require_auth
def get_proxy(identifier):
    proxy = db_get_proxy(identifier)
    if not proxy:
        return jsonify({}), 200
    return jsonify(
        {
            "api_domain": proxy["api_domain"],
            "mocked_requests": proxy["mocked_requests"],
        }
    ), 200


# ---------------------------------------------------------------------------
# Routes — Proxy passthrough
# ---------------------------------------------------------------------------


@app.route(
    "/proxy/<identifier>/<path:endpoint>",
    methods=["GET", "POST", "PUT", "PATCH", "DELETE", "HEAD", "OPTIONS"],
)
@log_access
@require_auth
def proxy_request(identifier, endpoint):
    try:
        api_domain = db_get_proxy_domain(identifier)
        if api_domain is None:
            return jsonify({"error": f"Proxy '{identifier}' not found"}), 404

        api_url = f"{api_domain.rstrip('/')}/{endpoint}"
        method = request.method
        query_string = request.query_string.decode('utf-8')

        logger.info("[PROXY] %s /%s/%s%s", method, identifier, endpoint,
                     '?' + query_string if query_string else '')

        # --- Redirect mode (server-side forward, preserves headers/body) ---
        if identifier.endswith("_REDIRECT"):
            if not _is_domain_allowed(api_domain):
                return jsonify({"error": "Redirect target domain not allowed"}), 403
            target_url = api_url
            if request.query_string:
                target_url += f"?{request.query_string.decode()}"
            try:
                fwd_api = API(request, target_url)
                return fwd_api.forward()
            except http_requests.exceptions.RequestException as exc:
                logger.error("Redirect forward failed: %s", exc)
                return jsonify({"error": "Redirect forward failed"}), 502

        # --- Mock lookup using MockMatcher ---
        mock_requests = db_get_mocks_for_proxy(identifier)
        matcher = MockMatcher(mock_requests, endpoint, query_string, api_url)
        _, mock_data = matcher.find(method)

        if mock_data:
            headers = {k: v for k, v in request.headers.items() if k.lower() != 'host'}
            json_body = request.get_json(silent=True) or {}
            params = request.args.to_dict()

            mock_data_copy = copy.deepcopy(mock_data)

            # Support status_code + body mock structure
            if isinstance(mock_data_copy, dict) and "status_code" in mock_data_copy and "body" in mock_data_copy:
                status_code = mock_data_copy["status_code"]
                body = mock_data_copy["body"]
                processed = resolve_mock_data(
                    body, header=headers, json_body=json_body, params=params, url=api_url
                )
                return jsonify(processed), status_code

            processed = resolve_mock_data(
                mock_data_copy, header=headers, json_body=json_body, params=params, url=api_url
            )
            return jsonify(processed), 200

        # --- SSRF guard ---
        if not _is_domain_allowed(api_domain):
            return jsonify({"error": "Target domain not allowed"}), 403

        # --- Forward to real API using API class ---
        try:
            api = API(request, api_url)
            return api.forward()
        except http_requests.exceptions.Timeout:
            logger.error("[PROXY] Timeout forwarding %s %s", method, api_url)
            return jsonify({"error": f"Upstream timeout after {FORWARD_TIMEOUT}s"}), 504
        except http_requests.exceptions.ConnectionError as exc:
            logger.error("[PROXY] Connection error forwarding %s %s: %s", method, api_url, exc)
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


# ---------------------------------------------------------------------------
# Error handler
# ---------------------------------------------------------------------------


@app.errorhandler(Exception)
def handle_exception(exc):
    logger.exception("Unhandled exception: %s", exc)
    return jsonify({"error": "An unexpected error occurred"}), 500


# ---------------------------------------------------------------------------
# Migrate from proxy_server.json (one-time)
# ---------------------------------------------------------------------------


def migrate_from_json(json_path: str):
    """Import existing proxy_server.json data into SQLite."""
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

        # Rename old file so migration doesn't run again
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

# Initialize DB on import (safe to call multiple times)
init_db()

# Auto-migrate old JSON data if present
migrate_from_json(os.path.join(_BASE_DIR, "proxy_server.json"))

if __name__ == "__main__":
    port = int(os.environ.get("PED_PORT", 8000))
    app.run(host="0.0.0.0", port=port, debug=DEBUG)
