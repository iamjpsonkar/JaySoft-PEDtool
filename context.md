# PED Tools — AI Context Document

> **Maintenance rule:** Update this file after every code change. It is the authoritative machine-readable description of this codebase. Any AI reading this file should be able to understand the entire system without reading source code first.

---

## 1. Project Identity

**Name:** PED Tools  
**Type:** Flask web application (Python 3.9+)  
**Purpose:** HTTP proxy, mock server, and AES encryption utility for development and testing workflows.  
**Primary file:** `app.py` (~4830 lines)
**Database:** SQLite (proxies, mocks, history, sequences, state, proxy_users, state_snapshots, mock_templates) — MongoDB optional for raw_mongo_* helpers only  
**Frontend:** Server-rendered Jinja2 templates + vanilla JS  
**Auth:** Session cookie (UI) or Bearer token (API)

---

## 2. File Map

```
proxyapp/
├── app.py              Main Flask app — all routes, models, logic (~3090 lines)
├── bootstrap.py        SQLite schema creation (idempotent, run before first start)
├── requirements.txt    Python dependencies
├── run.sh              Start script (creates .venv, installs, runs bootstrap, starts server)
├── setup.sh            One-shot setup (venv, deps, DB backup, bootstrap)
├── .env                Live secrets (gitignored)
├── .env.example        Config template
├── .gitignore
├── README.md           User-facing documentation
├── context.md          This file — AI-facing documentation
├── pedapp.db           SQLite database (gitignored)
├── static/
│   ├── css/            Per-page stylesheets (common, index, login, proxy-helper, proxy-manage, proxy-server)
│   └── js/             Per-page JS (common, index, login, proxy-helper, proxy-manage, proxy-server)
└── templates/          Jinja2 HTML templates (index, login, proxy_helper, proxy_manage, proxy_server)
```

---

## 3. Dependencies

| Package | Version constraint | Role |
|---|---|---|
| `flask` | `>=2.3,<4` | Web framework |
| `requests` | `>=2.31,<3` | Upstream HTTP forwarding |
| `pycryptodome` | `>=3.19,<4` | AES-CBC encrypt/decrypt |
| `shortuuid` | `>=1.0.11,<2` | Proxy identifier generation |
| `python-dotenv` | `>=1.0,<2` | `.env` loading |
| `simpleeval` | `>=0.9.13,<2` | Sandboxed expression evaluation for `snippet()` |
| `pymongo` | `>=4.6,<5` | MongoDB client (state + proxy users) |

---

## 4. Configuration (Environment Variables)

All read at module import time from `.env` (via `python-dotenv`) then `os.environ`.

| Variable | Default | Notes |
|---|---|---|
| `PED_PORT` | `8000` | Server port |
| `PED_DEBUG` | `false` | Enables Flask debug mode and DEBUG logging. If `true`, missing `PED_SECRET_KEY` uses an insecure fallback (logs warning) instead of raising. |
| `PED_DB_PATH` | `<dir>/pedapp.db` | SQLite path, resolved relative to `app.py` |
| `PED_MONGO_URI` | `mongodb://localhost:27017` | MongoDB connection string |
| `PED_MONGO_DB` | `pedapp` | MongoDB database name |
| `PED_SECRET_KEY` | **required** | Flask session secret. Must be set in prod or startup raises `RuntimeError`. |
| `PED_UI_PASSWORD` | `""` | UI login password. Empty disables UI auth entirely. |
| `PED_API_TOKEN` | `""` | Bearer token. Empty disables API auth. If both empty, all auth is bypassed. |
| `PED_DEFAULT_SECRET` | `""` | Default AES-CBC key for encrypt/decrypt endpoints |
| `PED_DEFAULT_ENC_IV` | `""` | Default AES IV (base64) |
| `PED_ALLOWED_PROXY_DOMAINS` | `""` | Comma-separated hostnames. Empty = allow all. Matching: exact or dot-boundary suffix. |
| `PED_FORWARD_TIMEOUT` | `30` | Upstream HTTP timeout (seconds) |
| `PED_HISTORY_LIMIT` | `100` | Max history rows per proxy |
| `PED_RATE_LIMIT_MAX` | `0` | Per-proxy in-memory rate limit (0 = disabled) |
| `PED_RATE_LIMIT_WINDOW` | `60` | Rate limit window (seconds) |
| `PED_CORS_ORIGINS` | `""` | CORS origins (comma-separated; empty=disabled, `*`=allow all) |
| `PED_MOCK_ENV_PREFIX` | `MOCK_` | Prefix for `envget()` env var access restriction |
| `PED_MAX_SNAPSHOTS` | `20` | Max state snapshots per proxy |
| `PED_MOCK_CACHE_MAX` | `200` | Max in-memory mock response cache entries |

---

## 5. SQLite Schema (bootstrap.py)

```sql
proxies (identifier PK, api_domain, created_at)

mocks (id AUTOINCREMENT, proxy_id FK→proxies, endpoint, method, response TEXT,
       tags TEXT DEFAULT '', created_at, updated_at; UNIQUE(proxy_id, endpoint, method))

request_history (id, proxy_id, endpoint, method, request_headers, request_body,
                 query_params, response_status, response_body,
                 source TEXT ['forward'|'mock'|'redirect'|'mock_register'|'mock_miss'],
                 duration_ms, created_at)

mock_sequences (id, proxy_id, endpoint, method, call_count; UNIQUE(proxy_id, endpoint, method))

state_snapshots (id, proxy_id, name, data TEXT, created_at)

mock_templates (id, name UNIQUE, description, template TEXT, category, created_at)
```

Indices: `idx_mocks_proxy`, `idx_mocks_lookup`, `idx_history_proxy`, `idx_history_time`, `idx_snapshots_proxy`.

---

## 6. MongoDB Collections

### `proxy_state`

Per-proxy arbitrary key-value store. Unique index on `proxy_id`.

```json
{ "proxy_id": "myproxy", "tokens": { "alice": { "accessToken": "...", "refreshToken": "..." } } }
```

### `proxy_users`

Per-proxy user credentials. Unique index on `(proxy_id, username)`.

```json
{ "proxy_id": "myproxy", "username": "alice", "password": "plaintext" }
```

> **Known issue:** passwords stored in plaintext. Not changed because it is used for mock simulation, not real auth. Changing to hashed passwords would break the `verify_password()` snippet function which does string comparison.

---

## 7. Flask App Initialization (app.py lines 70–82)

```python
app = Flask(__name__)
app.secret_key = PED_SECRET_KEY
app.config["SESSION_COOKIE_HTTPONLY"] = True
app.config["SESSION_COOKIE_SAMESITE"] = "Lax"
app.config["PERMANENT_SESSION_LIFETIME"] = timedelta(days=1)
```

Security headers added via `@app.after_request` hook:
- `X-Frame-Options: DENY`
- `X-Content-Type-Options: nosniff`
- `Referrer-Policy: strict-origin-when-cross-origin`

---

## 8. Authentication System

**`_is_authenticated()` (app.py ~800):**
1. If both `UI_PASSWORD` and `API_TOKEN` are empty → always True (dev mode).
2. If `session["authenticated"]` is set → True.
3. If `Authorization: Bearer <API_TOKEN>` header matches → True.
4. Otherwise → False.

**`@require_auth` decorator:** Returns 401 JSON for API requests (JSON or `Accept: application/json`), redirects browser to `/login?next=<path>`.

**`@require_login` decorator:** Always redirects to login (UI-only pages).

**Login route `POST /login`:**
- Rate-limited: 10 attempts per IP per 60s using `_rate_limits` dict.
- `next=` parameter sanitized: rejects `://` and `//` prefixes to prevent open redirect.
- Sets `session.permanent = True` (1-day lifetime).

---

## 9. Route Inventory

### Unauthenticated (open) routes

| Method | Path | Handler | Notes |
|---|---|---|---|
| `GET` | `/health` | `health_check` | |
| `POST` | `/ped/encrypt` | `encrypt_endpoint` | |
| `POST` | `/ped/decrypt` | `decrypt_endpoint` | |
| `POST` | `/ped/prettify` | `prettify` | |
| `POST` | `/ped/minify` | `minify` | Compact JSON output |
| `POST` | `/ped/jsonpath` | `jsonpath_query` | Dot-path extraction from JSON |
| `POST` | `/ped/diff` | `json_diff` | Structured diff of two JSON documents |
| `POST` | `/ped/validate-schema` | `validate_json_schema` | Lightweight JSON Schema validation |
| `POST` | `/ped/transform` | `json_transform` | Pipeline of transform operations |
| `GET/POST` | `/login` | `login_page` | |
| `GET` | `/logout` | `logout` | |
| `GET` | `/` | `index` | `@require_login` redirect |
| `POST` | `/proxy/create/` | `create_proxy` | **No auth** — intended API endpoint |
| `GET` | `/proxy/get/<id>/` | `get_proxy` | **No auth** |
| `POST` | `/proxy/mock/create/` | `create_mock_proxy` | **No auth** |
| `POST` | `/proxy/mock/delete/` | `delete_mock_proxy` | **No auth** |
| `POST` | `/proxy/sequence/reset/` | `reset_sequence` | **No auth** |
| `GET/PUT/PATCH/DELETE` | `/proxy/state/<id>/` | state handlers | **No auth** |
| `GET` | `/proxy/ratelimit/<id>/` | `get_rate_limit` | **No auth** |
| `POST` | `/proxy/mock/validate/` | `validate_mock` | **No auth** — dry-run mock validation |
| `POST` | `/proxy/mock/batch/` | `batch_mock_ops` | **No auth** — bulk create/delete mocks |
| `POST` | `/proxy/mock/tags/` | `update_mock_tags` | **No auth** — set tags on a mock |
| `GET` | `/proxy/mocks/<id>/` | `list_mocks_with_tags` | **No auth** — list mocks with tags |
| `POST` | `/proxy/state/<id>/snapshot/` | `save_snapshot_route` | **No auth** |
| `GET` | `/proxy/state/<id>/snapshots/` | `list_snapshots_route` | **No auth** |
| `POST` | `/proxy/state/restore/<snap_id>/` | `restore_snapshot_route` | **No auth** |
| `DELETE` | `/proxy/state/snapshot/<snap_id>/` | `delete_snapshot_route` | **No auth** |
| `GET` | `/proxy/templates/` | `list_templates_route` | **No auth** |
| `GET` | `/proxy/templates/<id>/` | `get_template_route` | **No auth** |
| `GET` | `/proxy/health/<id>/` | `proxy_health` | **No auth** — upstream health check |
| `ANY` | `/proxy/<id>/<path>` | `proxy_request` | **No auth** — main proxy handler |
| `OPTIONS` | `/proxy/<id>/<path>` | `_cors_preflight` | **No auth** — CORS preflight |
| `POST` | `/mock/<id>/<path>` | `register_mock_by_url` | **No auth** |

### Authenticated (`@require_auth`) routes

| Method | Path | Handler |
|---|---|---|
| `GET` | `/proxy/list/` | `list_proxies` |
| `DELETE` | `/proxy/delete/<id>/` | `delete_proxy` |
| `POST` | `/proxy/clone/` | `clone_proxy` |
| `GET` | `/proxy/export/<id>/` | `export_proxy` |
| `GET` | `/proxy/export/<id>/postman/` | `export_postman` |
| `GET` | `/proxy/export/all/` | `export_all_proxies` |
| `POST` | `/proxy/import/` | `import_proxies` |
| `GET` | `/proxy/history/<id>/` | `get_history` |
| `POST` | `/proxy/history/<id>/clear/` | `clear_history` |
| `GET` | `/proxy/users/<id>/` | `get_proxy_users` |
| `POST` | `/proxy/users/<id>/` | `upsert_proxy_user` |
| `DELETE` | `/proxy/users/<id>/<user>/` | `delete_proxy_user_route` |
| `POST` | `/proxy/templates/` | `create_template_route` |
| `DELETE` | `/proxy/templates/<id>/` | `delete_template_route` |
| `GET` | `/proxy/analytics/<id>/` | `mock_analytics` |
| `GET` | `/proxy/storage/` | `storage_info` |
| `POST` | `/proxy/storage/cleanup/` | `storage_cleanup` |

### UI routes (`@require_login`)

| Path | Template |
|---|---|
| `/` | `index.html` |
| `/proxy-server` | `proxy_server.html` |
| `/proxy-manage` | `proxy_manage.html` |
| `/proxy-helper` | `proxy_helper.html` |

---

## 10. Core Classes

### `API` (app.py ~1002)

Captures a Flask request and forwards it to a target URL. Handles JSON, form-encoded, multipart, and raw body types. Logs the equivalent curl command.

```python
API(flask_request, api_url)
  .forward() → (flask_response, duration_ms, raw_requests_response)
```

**Important:** `API.__init__` always copies `flask_request.args` into `self.params`. Do NOT manually append `?query_string` to `api_url` — `requests` will double it.

Hop-by-hop headers stripped on both request and response sides.

### `MockMatcher` (app.py ~1136)

Finds the matching mock for a given endpoint+method by trying multiple path variants in priority order.

```python
MockMatcher(mock_requests_dict, endpoint, query_string, api_url)
  .find(method) → (matched_key, mock_data) or (None, None)
```

**Lookup order per variant:**
1. Exact path + exact method
2. Exact path + `*`
3. Pattern path (`<param>`) + exact method
4. Pattern path + `*`

Variants tried (deduped): `endpoint+qs`, `endpoint`, `/endpoint+qs`, `/endpoint`, `api_url+qs`, `api_url`.

### `EncryptHelper` (app.py ~871)

AES-CBC encrypt and decrypt. `convert_json_to_string()` sorts keys for deterministic serialization before encryption.

---

## 11. Proxy Request Flow (`proxy_request`, app.py ~2840)

```
ANY /proxy/<identifier>/<endpoint>

1. check_rate_limit(identifier) → 429 if exceeded

2. db_get_proxy_domain(identifier) → 404 if not found
   api_url = api_domain + "/" + endpoint

3. if identifier.endswith("_REDIRECT"):
     API(request, api_url).forward() → return
     (query params forwarded via self.params, NOT manually appended to URL)

4. mock_requests = db_get_mocks_for_proxy(identifier)
   _, mock_data = MockMatcher(...).find(method)

5. if mock_data:
     a. resolve_mock_data() — walks the mock dict/list
     b. if list → cycling sequence (mock_sequences table)
     c. if {conditions, responses, default} → evaluate conditions
     d. pop _store → _apply_store_ops()
     e. pop _delay_ms → sleep (capped 30s)
     f. if {status_code, body, headers} wrapper → return custom status
     g. else return 200 JSON

6. else:
     _is_domain_allowed(api_domain) → 403 if blocked
     API(request, api_url).forward() → return
```

---

## 12. Mock Resolution Pipeline

`resolve_mock_data(data, header, json_body, params, url, proxy_id)`

Walks any JSON structure (dict/list) and applies resolvers to all string values. Also handles `_foreach`/`_template` expansion.

`_resolve_value(value_str, header, json_data, params, url, proxy_id)`

Single-value resolver. Checks value string against all known resolver patterns (exact match or `startswith`). Returns resolved value (preserving type for non-string resolvers like `body()`, `dbget()`, `snippet()`). Unmatched strings returned as-is.

`_resolve_to_int(raw, default, ...)` — resolves a value then coerces to `int`. Used by `_delay_ms` and `status_code`.

---

## 13. Snippet System (`safe_eval_snippet`, app.py ~1247)

Uses `simpleeval.EvalWithCompoundTypes`. Max 2000 chars.

`_snippet_context(header, json_data, params, url, proxy_id)` builds the `names` and `functions` dicts passed to `safe_eval_snippet`. Uses the already-captured `_state` variable (one DB read per resolver call, not per snippet function call).

**Key invariant:** `_fn_valid_refresh_token`, `_fn_token_user`, `_fn_refresh_token_user` all use the captured `_state` (set once at `_snippet_context` entry) — they do NOT call `_get_state_for_resolver()` again.

---

## 14. State System

### Storage

MongoDB `proxy_state` collection, unique index on `proxy_id`.

### Access

- **Read:** `_get_state_for_resolver(proxy_id)` — returns the state dict or `{}`. Also checks `_store_pending_state` thread-local for in-flight `_store` ops.
- **Write:** `db_set_state(proxy_id, state_dict)` — full replace via `$set`.

### `_store_pending_state`

`threading.local()` object with `.entry` field. While `_apply_store_ops` is executing, it stores the in-flight state so that sequential `_store` ops can read each other's writes via `dbget()` without waiting for the final MongoDB commit.

### `_apply_store_ops`

Processes a list of `{path, value}` or `{collection, key, value}` or `{path, delete: true}` operations. Resolves all paths and values through the resolver pipeline. Commits once after all ops if dirty.

---

## 15. Path Pattern Matching

`path_to_regex(pattern)` — converts `<param>` placeholders to `[^/]+` regex segments.

`match_path(mock_endpoints, actual_path)` — tries each registered pattern against the actual path.

`extract_path_param(prefix, url)` — extracts a path segment by:
- Named prefix: first `/prefix/<value>` match
- Named prefix + `_N` suffix: Nth match
- Numeric prefix: Nth `/`-separated segment (1-indexed)

---

## 16. Rate Limiting

In-memory, thread-safe (`threading.Lock`). Dict `_rate_limits` maps identifier to list of float timestamps.

`check_rate_limit(identifier)` — slides the window, checks count against `RATE_LIMIT_MAX`. Returns `True` = allowed.

Used for:
- Per-proxy request limiting at `proxy_request` entry
- Login brute-force limiting (key `__login__:<ip>`, hardcoded 10/60s)

---

## 17. `normalize_escape_sequences` (app.py ~931)

Called only by `/ped/prettify`. Normalises raw strings containing escaped/Python-style JSON before parsing.

**Order:**
1. `_ESCAPE_REPLACEMENTS`: literal escape sequences (`\/`, `\'`, `\"`, `"{`, etc.)
2. `_PYTHON_LITERAL_MAP`: `\bTrue\b` → `true`, `\bFalse\b` → `false`, `\bNone\b` → `null` (word-boundary regex — does NOT mangle words like `TrueBlood`)
3. Single-quote → double-quote

---

## 18. `extract_jsons_from_string` (app.py ~902)

Used by `/ped/prettify` to extract embedded JSON structures from raw text. Uses a single bracket counter for both `{}`/`[]`. Known limitation: mismatched bracket types (e.g., `{"a": [1}`) can produce false positives. Only affects the prettify endpoint.

---

## 19. Security Properties

| Property | Status | Implementation |
|---|---|---|
| Session cookie `HttpOnly` | ✅ | `app.config` |
| Session cookie `SameSite=Lax` | ✅ | `app.config` |
| Session lifetime limit | ✅ | `PERMANENT_SESSION_LIFETIME = 1 day` |
| `X-Frame-Options: DENY` | ✅ | `@after_request` hook |
| `X-Content-Type-Options: nosniff` | ✅ | `@after_request` hook |
| `Referrer-Policy` | ✅ | `@after_request` hook |
| Open redirect prevention on `/login` | ✅ | Rejects `://` / `//` in `next=` param |
| Login brute-force protection | ✅ | 10 attempts/60s per IP |
| `_delay_ms` DoS prevention | ✅ | Capped at 30,000 ms |
| Empty `api_domain` prevention | ✅ | Explicit 400 before allowlist check |
| Binary response decode safety | ✅ | `.content.decode(errors="replace")` |
| `JSONDecodeError` on bad mock body | ✅ | Returns 400 with error message |
| CSRF protection | ❌ | No CSRF tokens — not implemented |
| Content-Security-Policy | ❌ | Not set — would need UI audit |
| Plaintext proxy user passwords | ⚠️ | Intentional: used for mock simulation only |
| Unauthenticated state-mutating endpoints | ⚠️ | By design — see route inventory above |
| CORS headers | ✅ | Controlled by `PED_CORS_ORIGINS`; per-proxy override via state `_cors_origins` |
| `envget()` secret leak prevention | ✅ | Restricted to env vars matching `PED_MOCK_ENV_PREFIX` |
| `_callback` SSRF prevention | ✅ | Callback URLs checked via `_is_domain_allowed()` |
| `_callback` delay cap | ✅ | Capped at 30,000 ms |

---

## 20. Known Limitations / Future Work

1. **CSRF:** No CSRF tokens on session-authenticated endpoints. Adding Flask-WTF would require updating all JS `fetch()` calls to include the token.
2. **Proxy user passwords:** Stored in plaintext MongoDB. Changing to hashed storage requires updating `verify_proxy_user()` and all callers.
3. **`extract_jsons_from_string`:** Single bracket counter — mismatched brackets can produce false positives in prettify output.
4. **No `Content-Security-Policy`:** Would require a full audit of inline scripts/styles in templates.
5. **`_mongo_client` not closed on shutdown:** No `atexit`/`teardown_appcontext` hook to call `_mongo_client.close()`.
6. **`db_create_proxy` not atomic:** Three separate SQLite operations without a transaction. Race-safe for typical single-user dev use; not production-grade.
7. **Login rate limiter is in-memory only:** Resets on server restart. Fine for dev; not sufficient for multi-worker deployments.

---

## 21. Data Flow Diagrams

### Mock registration via `POST /proxy/mock/create/`

```
Request body {proxy_identifier, end_point, method, mock}
    ↓
Parse JSON mock (str → json.loads, 400 on invalid)
    ↓
db_upsert_mock(proxy_id, endpoint, method, mock_dict)
    → SQLite upsert INTO mocks(proxy_id, endpoint, method, response=json.dumps(mock))
    ↓
Return {proxy_identifier, end_point, method, new_mock, old_mock}
```

### Proxy request (mock hit)

```
ANY /proxy/<id>/<path>?<qs>
    ↓
check_rate_limit → 429
    ↓
db_get_proxy_domain → 404
    ↓
db_get_mocks_for_proxy → dict[endpoint][method] = parsed mock
    ↓
MockMatcher.find(method) → (key, mock_data)
    ↓
resolve_mock_data(mock_data)          ← walks dict/list, resolves string values
    ↓
[if list] → advance mock_sequences counter, pick element
    ↓
[if conditional] → _check_conditions → pick matching response / default
    ↓
pop _store → _apply_store_ops()       ← writes to proxy_state MongoDB
    ↓
pop _delay_ms → time.sleep()          ← max 30s
    ↓
[if {status_code, body}] → Response(body, status_code, headers)
[else] → jsonify(mock_data), 200
    ↓
db_log_request(...)                   ← SQLite INSERT into request_history
```

---

## 22. Logging Convention

Logger name: `pedapp`. Format: `%(asctime)s [%(levelname)s] %(name)s: %(message)s`.

Common log prefixes:

| Prefix | Meaning |
|---|---|
| `[MONGO]` | MongoDB connection events |
| `[USERS]` | Proxy user CRUD |
| `[MONGO_QUERY]` | Raw MongoDB queries from snippet helpers |
| `[PROXY]` | Proxy request entry/exit |
| `[MOCK HIT]` | Mock matched |
| `[MOCK MISS]` | No mock, forwarding upstream |
| `[FORWARD]` | Upstream HTTP call |
| `[CURL]` | Equivalent curl command for the upstream call |
| `[STORE]` | `_store` side-effect ops |
| `[DELAY]` | Sleep before response |
| `[RATE LIMIT]` | Rate limit exceeded |
| `[ALLOWLIST]` | Domain allowlist check |
| `[AUTH]` | Login / logout events |
| `[CONDITIONAL]` | Condition evaluation |
| `[FOREACH]` | Template expansion |
| `[RESOLVER]` | Placeholder resolver warnings |
| `[STARTUP]` | Config warnings at startup |
| `[BOOTSTRAP]` | DB schema init |
| `[MOCK CREATE]` | Mock registration |
| `[MOCK REGISTER]` | URL-mirroring registration |

---

## 23. Changelog (recent)

### 2026-05-01 — JSON Toolbox (5 new /ped/ endpoints)

**Files changed:** `app.py`, `static/js/index.js`, `templates/index.html`, `context.md`

**New endpoints:**

1. **`POST /ped/minify`** — Strips all whitespace from JSON, returns compact output with original/minified length stats. Accepts both string and pre-parsed JSON input.

2. **`POST /ped/jsonpath`** — Dot-path extraction from JSON documents. Accepts single `path` or multiple `paths` (array). Uses the existing `_resolve_item_path` engine that powers `jsonget()` / `dbget()`. Supports dict key traversal and array index access (e.g. `items.0.name`).

3. **`POST /ped/diff`** — Recursive structural diff of two JSON documents (`a` vs `b`). Returns list of changes with path, type (added/removed/changed), old/new values. Sorted keys for deterministic output. Backend function `_json_diff_recursive` handles nested dicts, lists, and type mismatches.

4. **`POST /ped/validate-schema`** — Lightweight JSON Schema validation with zero external dependencies. Supports: `type` (string/number/integer/boolean/array/object/null, single or union), `required`, `properties` (recursive), `items` (recursive), `enum`, `minimum`/`maximum`, `minLength`/`maxLength`, `pattern` (regex), `minItems`/`maxItems`.

5. **`POST /ped/transform`** — Pipeline of JSON transform operations. Supported ops: `pick`, `omit`, `rename`, `set` (dot-path), `delete` (dot-path), `flatten`, `unflatten`, `wrap`, `unwrap`, `sort_keys`, `defaults`, `map` (set fields on array elements). Operations applied sequentially; individual failures reported without aborting the pipeline.

**UI changes (index.html):**
- New toolbar group "JSON Tools" with 4 buttons: Path Query, Diff, Validate Schema, Transform
- Minify button added next to Prettify

**JS handlers (index.js):**
- `minifyJson()` — sends to `/ped/minify`, shows saved chars
- `jsonPathQuery()` — prompts for path(s), supports comma-separated multi-path
- `jsonDiffTool()` — uses Input pane as `a`, Output pane as `b`
- `jsonSchemaValidate()` — prompt for schema or use Output pane contents
- `jsonTransform()` — prompt for operations JSON array with examples

**Helper functions added to app.py:**
- `_json_diff_recursive(a, b, path)` — recursive diff engine
- `_validate_schema(value, schema, path)` — lightweight schema validator
- `_apply_transform(data, op)` — single transform dispatcher
- `_flatten_dict(d, sep, prefix)` — nested dict to flat dotted keys
- `_unflatten_dict(d, sep)` — flat dotted keys to nested dict
- `_sort_keys_recursive(data)` — recursive key sorting

---

### 2026-05-01 — 18 Feature Mega Release

**Files changed:** `app.py` (3335→4371 lines), `bootstrap.py`, `static/js/proxy-manage.js`, `static/js/proxy-server.js`, `static/css/proxy-manage.css`, `static/css/proxy-server.css`, `templates/proxy_manage.html`, `templates/proxy_server.html`, `context.md`

**New features implemented (18 total):**

**P0 — Quick Wins:**
1. **CORS Headers Support** — `PED_CORS_ORIGINS` env var (comma-separated or `*`). Per-proxy override via state key `_cors_origins`. OPTIONS preflight handled with 204. Headers set in `_set_security_headers` after_request hook.
2. **Request Replay from History** — Replay button per history row in manage UI. Constructs fetch() from stored method/endpoint/headers/body, sends to `/proxy/<id>/<endpoint>`.

**P1 — High-Impact Features:**
3. **Mock Validation & Dry-Run** — `POST /proxy/mock/validate/` accepts mock payload + optional `test_request`. Runs resolve_mock_data in try/except, returns `{valid, errors, resolved_output, store_ops_preview}`. UI "Validate" button in mock builder.
4. **History Search & Filtering** — `GET /proxy/history/<id>/` now accepts query params: `method`, `endpoint` (substring), `status_min`/`status_max`, `source`, `since`/`until`. Filter inputs in manage UI.
5. **Webhook / Callback Simulation** — New `_callback` key in mock responses: `{"url", "method", "body", "headers", "delay_ms"}`. Scheduled via `threading.Timer`. Body/URL pass through resolver pipeline. SSRF guard via `_is_domain_allowed()`. Delay capped at 30s.
6. **State Snapshots & Restore** — New `state_snapshots` table. Routes: `POST .../snapshot/` (save), `GET .../snapshots/` (list), `POST .../restore/<id>/` (restore), `DELETE .../snapshot/<id>/` (delete). Cap at 20 per proxy (PED_MAX_SNAPSHOTS). Snapshot list + restore in manage UI.

**P2 — Nice-to-Have:**
7. **Batch Mock Operations** — `POST /proxy/mock/batch/` accepts `{proxy_identifier, operations: [{action, end_point, method, mock}]}`. Bulk delete checkbox mode in mocks table UI.
8. **Latency Simulation Profiles** — New `_delay_profile` key: `uniform(min,max)`, `normal(mean,stddev)`, `spike(base,spike,pct)`. Uses Python random module. Cap at 30s.
9. **Environment Variable Resolver** — `envget(VAR_NAME, default)` in both `_resolve_value` and `_snippet_context`. Restricted to env vars matching `PED_MOCK_ENV_PREFIX` (default `MOCK_`).
10. **Mock Diff View** — Client-side recursive JSON diff in proxy-server.js. Renders inline diff with color-coded additions/removals when updating an existing mock.
11. **Shareable Mock Playground Links** — URL pattern: `/proxy/?proxy=<id>&endpoint=<b64>&method=<m>`. On load, detects params, loads mocks, opens editor. Share button copies URL to clipboard.

**P3 — Future Consideration (all implemented):**
12. **Mock Tagging & Filtering** — `tags` TEXT column added to mocks table (auto-migrated). `POST /proxy/mock/tags/` to set, `GET /proxy/mocks/<id>/` lists with tags. UI tag filter support.
13. **Mock Templates Library** — New `mock_templates` table. CRUD routes at `/proxy/templates/`. Template dropdown in mock builder UI.
14. **Mock Analytics Dashboard** — `GET /proxy/analytics/<id>/` computes: total requests, by source/method, avg latency, error rate, top endpoints, stale mocks. Stat cards in manage UI.
15. **Request/Response Transform on Forward** — Per-proxy `_request_transforms` and `_response_transforms` state keys with `add_headers` support. Applied during upstream forwarding.
16. **Mock Inheritance / Proxy Chaining** — `_parent_proxy` state key. Child inherits parent's mocks, child takes precedence on conflict. Merged at mock lookup time.
17. **Proxy Health Dashboard** — `GET /proxy/health/<id>/` pings upstream domain via HEAD request. Returns status (healthy/degraded/unhealthy), latency, mock count, history count.
18. **Mock Response Caching** — `_cache_ttl` key in mock responses (seconds). In-memory LRU cache keyed by proxy+endpoint+method+params. Max entries via PED_MOCK_CACHE_MAX.

**Space Optimization (bonus):**
- `GET /proxy/storage/` — DB size, row counts per table
- `POST /proxy/storage/cleanup/` — delete old history (keep_days), orphaned snapshots, VACUUM. Returns bytes saved.
- Storage info and cleanup buttons in manage UI.

**Schema migrations (auto-applied in `_ensure_schema_ready`):**
- `tags` column added to `mocks` table
- `state_snapshots` table auto-created
- `mock_templates` table auto-created

**New env vars:** `PED_CORS_ORIGINS`, `PED_MOCK_ENV_PREFIX`, `PED_MAX_SNAPSHOTS`, `PED_MOCK_CACHE_MAX`

---

### 2026-04-30 — Mock-only mode (skip upstream forwarding)

**Problem:** Proxy server IP is not whitelisted at upstream APIs (e.g., Juspay). When no mock matches, the proxy forwards the request from its own IP → upstream blocks it.

**Solution:** Mock-only mode. When enabled, unmatched requests return `501 Not Implemented` with a structured JSON response instead of forwarding upstream. The calling service detects the 501 and falls back to calling the upstream API directly (from its own whitelisted IP).

**How to enable (two options):**
1. **State flag:** `PATCH /proxy/state/<id>/ {"_mock_only": true}` — toggle on any existing proxy
2. **Suffix:** register identifier as `<name>_MOCKONLY` — like `_REDIRECT` convention

**501 response shape:**
```json
{
  "error": "No mock registered for this endpoint",
  "mock_only": true,
  "proxy_id": "juspay",
  "method": "POST",
  "endpoint": "/orders",
  "api_domain": "https://api.juspay.in"
}
```

**History source:** logged as `mock_miss` in request history.

**Execution order in proxy_request:** mock-only check runs after mock lookup fails, before SSRF guard and upstream forward.

---

### 2026-04-29 — Postman collection export + UI font contrast fix

**New endpoint:** `GET /proxy/export/<identifier>/postman/`
- Generates a Postman v2.1 collection JSON from proxy mocks
- Each mock endpoint+method becomes a Postman request item
- Auto-infers example request bodies from `jsonget()` refs in conditions, `_store` paths, and values
- Includes State Management folder (GET/PUT/PATCH state)
- Returns as downloadable file (`Content-Disposition: attachment`)
- Helper function `_postman_example_body()` scans mock structure for jsonget references

**Visual mock generator improvements:**
- Resolver dropdown now uses `<optgroup>` with categories: Request Data, State, Random, Timestamps, Expression
- Added new types: `dbget`, `mongoget`, `now`, `now_epoch`
- Placeholder pill bar reorganized with section labels
- Select width increased to 200px for grouped options

**Documentation updates (proxy_helper.html):**
- Added 5 new sections: State API, _store (Write), dbget (Read), Staging Pattern, Recipes
- TOC updated with State & Storage section
- Dynamic Placeholders table updated with dbget, now, now_epoch entries
- snippet() docs expanded with full available function list

**Font contrast fix:**
- `--text` darkened from `#1e293b` to `#0f172a`
- `--text-secondary` darkened from `#64748b` to `#374151`
- `--text-muted` darkened from `#94a3b8` to `#6b7280`
- `--nav-link` darkened from `#64748b` to `#374151`
- Labels bumped from 0.85rem to 0.875rem
- Hints changed from --text-muted to --text-secondary
- Body font-size set to 0.9375rem (15px)
- Nav links weight bumped from 500 to 600

---

### 2026-04-29 — Proxy Manage & Proxy Helper pages modern UI refresh

**Files changed:**
- `static/css/proxy-manage.css` — full rewrite to use CSS variable system from `common.css`
- `static/css/proxy-helper.css` — full rewrite to use CSS variable system from `common.css`
- `templates/proxy_manage.html` — added Google Fonts (Inter), theme toggle button, nav-spacer, theme detection/toggle script
- `templates/proxy_helper.html` — added Google Fonts (Inter), theme toggle button, nav-spacer, theme detection/toggle script

**CSS changes (proxy-manage.css):**
- All hardcoded colors replaced with CSS variables
- `.status-bar`: --surface bg, --border, --text-muted; transition for smooth theme switching
- `.status-dot`: .ok=--success, .err=--danger, .loading=--warning; border-radius uses --radius-full
- `.history-detail`: --surface-inset bg, --border; pre uses --code-bg/--code-text with --border
- `.history-detail-label`: --primary color instead of hardcoded --blue-dark
- `.source-badge`: mock=--purple, forward=--primary, redirect=--warning, error=--danger; --radius-sm
- `.section-chevron`: --text-muted, transition uses var(--transition)
- `.hidden-section`: --surface-inset bg, --border, --radius-sm; fadeSlideIn animation on .visible
- `.clickable-row`: hover uses --primary-light instead of --blue-light

**CSS changes (proxy-helper.css):**
- All hardcoded colors replaced with CSS variables
- `.card p`: --text-secondary; `.card h2/h3`: --text
- `code` inline: --primary-light bg, #c084fc purple text (dark mode: #d8b4fe via `[data-theme="dark"]` selector)
- `pre` code blocks: --code-bg, --code-text, --border, --radius; `pre code` resets inline-code styles
- `.accordion`: --surface bg, --shadow, --border; hover uses --primary-light; arrow uses --primary; transitions added
- `.method-ANY`: --warning with dark text
- `.flow-step`: --primary-light bg, --text color; `.flow-step.alt`: success green with alpha bg
- `.note`: --primary-light bg, --primary left-border, --text color (was hardcoded #004080)
- `.toc a`: --primary color with transition
- `.ref-table th`: --surface-inset bg, --text-secondary color, --border bottom
- `.ref-table td`: --border bottom instead of hardcoded #eee
- All components have transition properties for smooth theme switching

**HTML changes (proxy_manage.html):**
- Added Google Fonts preconnect + Inter link (400/500/600/700)
- Replaced `style="margin-left:auto;opacity:0.8;"` on Logout with `<span class="nav-spacer"></span>` + removed inline style
- Added `<button class="theme-toggle">` before Logout
- Added theme detection IIFE + toggleTheme() script at end of body

**HTML changes (proxy_helper.html):**
- Added Google Fonts preconnect + Inter link (400/500/600/700)
- Replaced `style="margin-left:auto;"` / `style="margin-left:auto;opacity:0.8;"` on Login/Logout with `<span class="nav-spacer"></span>`
- Added `<button class="theme-toggle">` before Login/Logout in both auth branches
- Manage link moved before the nav-spacer; auth-conditional block split so nav-spacer + theme toggle are outside it
- Added theme detection IIFE + toggleTheme() script at end of body

**Not changed:** IDs, onclick handlers, Jinja2 form logic, JS files, page content/structure.

---

### 2026-04-29 — Proxy Server page modern UI refresh

**Files changed:**
- `static/css/proxy-server.css` — full rewrite to use new CSS variable system from `common.css`
- `templates/proxy_server.html` — added Google Fonts (Inter), theme toggle button, nav-spacer, theme detection/toggle script

**CSS changes (proxy-server.css):**
- All hardcoded colors replaced with CSS variables (--surface, --border, --text-muted, --primary, --success, --danger, --warning, --purple, --code-bg, --code-text, etc.)
- `.status-bar`: --surface bg, --border, --text-muted
- `.status-dot`: .ok=--success, .err=--danger, .loading=--warning
- `.tabs`/`.tab`: inactive uses --surface-inset, active uses --surface with --primary accent bottom border
- `.tab-content`: --border, --surface background
- `.pattern-help`: --primary-light bg, --text color; dark-mode code bg override
- `.toggle .slider`: --border track, --primary when checked, smooth --transition
- `.condition-row`/`.sequence-step`: --surface-inset bg, --border, hover with --border-hover
- `.pill`: --border, --surface bg, hover to --primary-light/--primary
- `.history-detail`: --surface-inset bg, pre uses --code-bg/--code-text
- `.source-badge`: mock=--purple, forward=--primary, redirect=--warning, error=--danger
- `.section-chevron`: --text-muted, transition uses var(--transition)
- Added transition properties throughout for smooth theme switching
- No class names or selectors removed or renamed

**HTML changes (proxy_server.html):**
- Added Google Fonts preconnect + Inter link (400/500/600/700)
- Replaced `style="margin-left:auto;"` on Login/Logout with `<span class="nav-spacer"></span>`
- Added `<button class="theme-toggle">` before Login/Logout in both auth branches
- Added theme detection IIFE + toggleTheme() script at end of body

**Not changed:** IDs, onclick handlers, Jinja2 conditionals, proxy-server.js dependencies.

---

### 2026-04-29 — Login page modern UI refresh

**Files changed:**
- `static/css/login.css` — fully rewritten to use the new CSS variable design system from `common.css`
- `templates/login.html` — minimal template updates (no form/logic changes)

**`login.css` changes:**
- Card uses `var(--surface)`, `var(--border)`, `var(--radius-lg)`, `var(--shadow-xl)` instead of hardcoded values
- Subtle dual radial-gradient background using `var(--primary-glow)` over `var(--bg)`
- Card entrance animation (`cardAppear`: fade + slide-up + scale)
- Button uses `var(--primary)` / `var(--primary-hover)` with lift-on-hover effect
- Error message restyled: left accent border (`var(--danger)`), `color-mix()` tinted background, left-aligned text
- Footer links extracted to `.login-footer` class (was inline styles with old `--blue` / `--gray-border` vars)
- Footer link hover uses `var(--primary-light)` background pill
- Input focus ring uses `var(--primary-glow)`
- All colors/borders use CSS variables for full dark mode support
- Responsive breakpoint at 480px reduces card padding

**`login.html` changes:**
- Added Google Fonts preconnect + Inter font link (400/500/600/700 weights)
- Footer links `div` changed from inline styles to `class="login-footer"`
- Added theme detection script at end of body: reads `ped-theme` from localStorage or falls back to `prefers-color-scheme` media query, sets `data-theme` attribute on `<html>`

**Not changed:** Form action, field names, Jinja2 conditionals, `login.js` script, lock emoji icon.

---

### 2026-04-21 — Security hardening + bug fixes (commit ea49df3 + follow-up)

**Bugs fixed:**
- XSS in `proxy-server.js` method badge — `e.method` was injected raw into HTML; now uses `escapeAttr()`/`escapeHtml()`
- Open redirect on `/login` — `next=` parameter now rejects values containing `://` or `//`
- Double query string in `_REDIRECT` mode — manual `?qs` append removed; `API` class already forwards via `self.params`
- `json.loads(new_mock)` crash — wrapped in `try/except json.JSONDecodeError` → 400
- `UnicodeDecodeError` on binary upstream responses — switched to `.content[:2000].decode("utf-8", errors="replace")`
- `_fn_valid_refresh_token`, `_fn_token_user`, `_fn_refresh_token_user` — stopped re-calling `_get_state_for_resolver()`, now use captured `_state`
- `normalize_escape_sequences` — `True/False/None` replacement now uses `\b` word-boundary regex (was mangling words like `TrueBlood`)
- `flask_request.json` in `API._parse_body` — changed to `get_json(silent=True)` to avoid `BadRequest` on malformed JSON
- Empty `api_domain` — explicit 400 added before allowlist check

**Security hardened:**
- Removed deprecated `background=True` from pymongo index creation
- Moved `werkzeug` import to top of file
- Added `SESSION_COOKIE_HTTPONLY=True`, `SESSION_COOKIE_SAMESITE=Lax`, `PERMANENT_SESSION_LIFETIME=1 day`
- Added `@after_request` hook setting `X-Frame-Options`, `X-Content-Type-Options`, `Referrer-Policy`
- Added login brute-force rate limiting (10 attempts/60s per IP, reuses `_rate_limits` dict with `__login__:<ip>` key)
- Capped `_delay_ms` at 30,000 ms
- Added `@log_access` to `get_proxy_users` and `upsert_proxy_user`

---

### 2026-04-21 — Proxy export redesign with MongoDB state integration

**File added:** `improved_proxies.json`

A fully redesigned 14-proxy export replacing the original flat-mock patterns with MongoDB state integration (`_store` / `dbget`). Key changes per proxy:

**ajiocashwallet**
- Login stores `tokens.<username>.accessToken` + `tokens.<username>.refreshToken` via `alnum(16,16)` in `_store`
- Refresh validates via `valid_refresh_token(jsonget('refreshToken'))` snippet condition, rotates both tokens
- Payments stores transaction under `transactions.<lastTxnId>` using staged `lastId` variable pattern
- transactionStatus / refundTransactionStatus look up stored entries; return 404 if not found
- undoPayments updates `transactions.<txnId>.status` to REVERSED
- cartBenefit reads configurable benefit amounts from state

**deadlock (Razorpay)**
- POST /v1/orders stores order under `orders.<lastOrderId>` and returns dynamic ID via `dbget`
- GET /v1/orders/<order_id> looks up stored order; returns 404 if not found
- GET /v1/orders/<order_id>/payments returns payment with dynamic `pay_*` ID via `snippet('pay_' + upper(14))`
- Fixed 20+ broken `snippet(return/import ...)` expressions (simpleeval supports expressions only)
- Fixed duplicate Razorpay customer token IDs and expired `expired_at` timestamps (updated to 2027)

**jioprimewallet**
- `getActualPoint` returns live `snippet(dbget('points.balance', 1500))`
- `redeemActivePoints` validates balance ≥ requested amount via snippet condition; deducts on success; 422 with available balance on failure
- `addActivePoints` adds to balance, returns new total

**mahacashback** — Same balance tracking pattern as jioprimewallet

**pinelabs**
- `UploadBilledTransaction` generates `digit(7)` reference, stores under `transactions.<ref>`, returns as `PlutusTransactionReferenceID`
- `GetCloudBasedTxnStatus` looks up by `PlutusTransactionReferenceID`; returns 404 if not found
- `CancelTransaction` updates stored status to CANCELLED

**Other proxies** (personal, test, gokwik, jiocinema, kotak, paymentpayload, paymentpayloadupi, ixigo):
- Removed real phone numbers from personal proxy
- Fixed all broken `snippet(return ...)` patterns in test proxy
- Retained existing flat-mock structure where state management was not applicable

**State seeding required** for stateful proxies before first use:
```
PUT /proxy/state/ajiocashwallet/   {"tokens": {}, "transactions": {}}
PUT /proxy/state/deadlock/         {"orders": {}}
PUT /proxy/state/jioprimewallet/   {"points": {"balance": 1500}}
PUT /proxy/state/mahacashback/     {"cashback": {"balance": 5000}}
PUT /proxy/state/pinelabs/         {"transactions": {}}
```

**`lastId` staging pattern** (established in this redesign):
In `_store` ops, write the generated ID to a temporary key first (e.g., `lastOrderId`), then subsequent ops in the same batch read it via `_store_pending_state.entry` (exposed as `dbget('lastOrderId')` in resolver context) to build the full nested key for the actual record.

### 2026-04-21 — Migrate state + users to SQLite; MongoDB now optional

**Problem:** MongoDB Atlas Data API was EOL'd September 2025; PythonAnywhere free plan blocks TCP port 27017 (direct pymongo).

**Solution:** Move `proxy_state` and `proxy_users` to SQLite (already present, zero new deps).

**`bootstrap.py`** — added two new tables (idempotent `CREATE TABLE IF NOT EXISTS`):
- `proxy_state(proxy_id TEXT PRIMARY KEY, data TEXT)` — JSON blob per proxy
- `proxy_users(proxy_id TEXT, username TEXT, password TEXT, PRIMARY KEY(proxy_id, username))`

**`app.py` changes:**
- `db_get_state` / `db_set_state` / `db_merge_state` / `db_clear_state` — now use `_get_db()` (SQLite)
- `create_proxy_user` / `list_proxy_users` / `delete_proxy_user` / `verify_proxy_user` — now use `_get_db()` (SQLite)
- Removed `_atlas_request()`, `_USE_ATLAS`, `ATLAS_API_KEY`, `ATLAS_APP_ID`, `ATLAS_CLUSTER` config vars
- `_get_mongo()` and `raw_mongo_*` helpers kept as-is — MongoDB remains optional for snippet-level queries
- Removed unused `ASCENDING` import from pymongo

**`.env.example`** — MongoDB section updated to clarify it's optional (raw helpers only).

**On PythonAnywhere:** run `python bootstrap.py` once to create the new tables, then reload the web app. No MongoDB config needed.

---

### 2026-04-21 — MongoDB Atlas Data API backend (attempted, superseded)

**Problem:** PythonAnywhere free plan blocks outbound TCP on non-80/443 ports, so `mongodb+srv://` connections (port 27017) always fail.

**Solution:** MongoDB Atlas Data API — pure HTTPS REST interface to Atlas, always on port 443.

**Config vars added** (`.env` / `.env.example`):
- `PED_ATLAS_API_KEY` — Atlas Data API key
- `PED_ATLAS_APP_ID` — Atlas App Services App ID (from Data API settings)
- `PED_ATLAS_CLUSTER` — cluster name, defaults to `Cluster0`

When `PED_ATLAS_API_KEY` and `PED_ATLAS_APP_ID` are both set, `_USE_ATLAS=True` and all state/user DB operations go through `_atlas_request()`. Otherwise they fall back to direct pymongo (for local dev).

**`_atlas_request(action, collection, body)`** — POSTs to `https://data.mongodb-api.com/app/{ATLAS_APP_ID}/endpoint/data/v1/action/{action}` with the Atlas Data API key. Supports `findOne`, `find`, `insertOne`, `updateOne`, `deleteOne`, `aggregate`.

**Functions updated** (dual-backend `_USE_ATLAS` branch added):
- `db_get_state`, `db_set_state`, `db_merge_state`, `db_clear_state`
- `create_proxy_user`, `list_proxy_users`, `delete_proxy_user`, `verify_proxy_user`

**Raw mongo helpers** (`raw_mongo_find`, `raw_mongo_find_one`, etc.) continue using `_get_mongo()` directly — Atlas Data API is optional for those.

**`_get_mongo()`** — index creation removed (was only needed for correctness guarantees; Atlas handles uniqueness differently; raw queries still work fine without enforced unique index in dev).

**`_state_col()` and `_users_col()`** — removed; callers now inline `_get_mongo()[MONGO_DB]["collection"]` in the pymongo branch.

---

### 2026-04-21 — improved_proxies.json: bug fixes + juspay state + import_and_seed.sh

**Bugs fixed in improved_proxies.json:**
- `jioprimewallet addActivePoints.newBalance`: was re-adding `jsonget('amount')` to the already-committed new balance → double-add. Fixed to `snippet(dbget('points.balance', 0))` (reads committed value).
- `mahacashback PERFORM_REDEMPTION.remainingBalance`: same double-deduct pattern. Fixed to `snippet(dbget('cashback.balance', 0))`.
- `mahacashback PERFORM_REFUND.newBalance`: same double-add. Fixed to `snippet(dbget('cashback.balance', 0))`.

**Root cause:** `_apply_store_ops` clears `_store_pending_state.entry` and commits to MongoDB before returning. Body resolvers then call `_get_state_for_resolver` → reads from MongoDB (already updated). Re-running `old_value ± request_amount` in the body produced wrong results.

**juspay enhanced with MongoDB state:**
- `POST /orders/` — stores order under `orders.<merchant_order_id>` using `lastJuspayId` staging variable. Returns Juspay-format response with `payment_links`.
- `GET /orders/<order_id>` — reads stored order from MongoDB; returns 404 if not found.
- `POST /v2/upi/verify-vpa` — conditional mock: `fail@upi` returns `is_valid: false`, all other VPAs return `is_valid: true`.
- State seeding: `PUT /proxy/state/juspay/ {"orders": {}}`

**import_and_seed.sh updated:**
- Added `juspay` seed
- `ajiocashwallet` seed now includes full initial state with `benefits`

---

### 2026-04-29 — Index page UI modernization (CSS + template)

**`static/css/index.css`** — fully rewritten to use the new CSS variable design system from `common.css`:
- Replaced all hardcoded colors (`#fafafa`, `#f8f9fa`, `#333`, `#aaa`, `var(--white)`, `var(--gray-border)`, `var(--gray-text)`, `var(--blue)`, `var(--blue-light)`) with design-system variables (`var(--surface)`, `var(--surface-inset)`, `var(--border)`, `var(--text)`, `var(--text-secondary)`, `var(--text-muted)`, `var(--primary)`, `var(--primary-light)`, `var(--primary-glow)`)
- Added `backdrop-filter` glass effect to `.toolbar`
- Added `transition` properties throughout for smooth light/dark theme switching
- Pane-header buttons now use `var(--font)`, `var(--primary-light)` hover, and subtle `translateY(-1px)` lift
- Checkbox accent color changed from `var(--blue)` to `var(--primary)`
- Input focus ring uses `var(--primary-glow)` box-shadow instead of simple border color change
- Layout structure (full-height body, toolbar, split editors, status bar) and 700px responsive breakpoint preserved

**`templates/index.html`** — minimal template changes:
- Added Google Fonts preconnect + Inter font link in `<head>`
- Added `class="active"` Home link in topbar
- Replaced `style="margin-left:auto;"` with `<span class="nav-spacer"></span>` (uses `.nav-spacer { flex: 1 }` from common.css)
- Added theme toggle button (`<button class="theme-toggle">`) before Login/Logout link
- Added inline theme init script at end of body (reads `ped-theme` from localStorage or `prefers-color-scheme`, sets `data-theme` attribute, exposes `toggleTheme()` global function)

No IDs, JS-referenced class names, onclick handlers, or JS files were changed.

---

## 24. How to Update This File

After any code change that affects:
- A route (added, removed, auth changed)
- A resolver or snippet function
- Configuration variables
- Database schema
- Security properties
- Core class behavior (API, MockMatcher, etc.)
- Known limitations (fixed or new ones found)

Update the relevant section(s) above and add an entry to section 23 (Changelog).
