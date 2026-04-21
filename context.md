# PED Tools — AI Context Document

> **Maintenance rule:** Update this file after every code change. It is the authoritative machine-readable description of this codebase. Any AI reading this file should be able to understand the entire system without reading source code first.

---

## 1. Project Identity

**Name:** PED Tools  
**Type:** Flask web application (Python 3.9+)  
**Purpose:** HTTP proxy, mock server, and AES encryption utility for development and testing workflows.  
**Primary file:** `app.py` (~3090 lines)  
**Database:** SQLite (proxies, mocks, history, sequences) + MongoDB (state, proxy_users)  
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

---

## 5. SQLite Schema (bootstrap.py)

```sql
proxies (identifier PK, api_domain, created_at)

mocks (id AUTOINCREMENT, proxy_id FK→proxies, endpoint, method, response TEXT,
       created_at, updated_at; UNIQUE(proxy_id, endpoint, method))

request_history (id, proxy_id, endpoint, method, request_headers, request_body,
                 query_params, response_status, response_body,
                 source TEXT ['forward'|'mock'|'redirect'|'mock_register'],
                 duration_ms, created_at)

mock_sequences (id, proxy_id, endpoint, method, call_count; UNIQUE(proxy_id, endpoint, method))
```

Indices: `idx_mocks_proxy`, `idx_mocks_lookup`, `idx_history_proxy`, `idx_history_time`.

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
| `ANY` | `/proxy/<id>/<path>` | `proxy_request` | **No auth** — main proxy handler |
| `POST` | `/mock/<id>/<path>` | `register_mock_by_url` | **No auth** |

### Authenticated (`@require_auth`) routes

| Method | Path | Handler |
|---|---|---|
| `GET` | `/proxy/list/` | `list_proxies` |
| `DELETE` | `/proxy/delete/<id>/` | `delete_proxy` |
| `POST` | `/proxy/clone/` | `clone_proxy` |
| `GET` | `/proxy/export/<id>/` | `export_proxy` |
| `GET` | `/proxy/export/all/` | `export_all_proxies` |
| `POST` | `/proxy/import/` | `import_proxies` |
| `GET` | `/proxy/history/<id>/` | `get_history` |
| `POST` | `/proxy/history/<id>/clear/` | `clear_history` |
| `GET` | `/proxy/users/<id>/` | `get_proxy_users` |
| `POST` | `/proxy/users/<id>/` | `upsert_proxy_user` |
| `DELETE` | `/proxy/users/<id>/<user>/` | `delete_proxy_user_route` |

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
