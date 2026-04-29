# PED Tools

A Flask-based HTTP proxy, mock server, and AES encryption utility with a full web UI. Designed for development and testing workflows where you need to intercept, replay, stub, or inspect HTTP traffic.

---

## Table of Contents

1. [Features](#features)
2. [Quick Start](#quick-start)
3. [Configuration](#configuration)
4. [Authentication](#authentication)
5. [API Reference](#api-reference)
6. [Mock Response Shapes](#mock-response-shapes)
7. [Placeholder Resolvers](#placeholder-resolvers)
8. [Snippet Expressions](#snippet-expressions)
9. [State Management](#state-management)
10. [Conditional Mocks](#conditional-mocks)
11. [Foreach / Template Expansion](#foreach--template-expansion)
12. [Store Side-Effects](#store-side-effects)
13. [Proxy Users](#proxy-users)
14. [Import / Export / Clone](#import--export--clone)
15. [Tutorial](#tutorial)
16. [Deployment](#deployment)
17. [Security Notes](#security-notes)

---

## Features

| Feature | Description |
|---|---|
| **HTTP Proxy** | Forward requests to any target domain; per-proxy history and rate limits |
| **Mock Server** | Register canned responses per path+method; cycling sequences, delays, custom status/headers |
| **URL-mirroring registration** | POST to `/mock/<id>/<path>` — no separate body needed |
| **Conditional mocks** | Return different responses based on request body, headers, params, path, method, or snippet expression |
| **Placeholder resolvers** | Dynamic values injected at request time: `jsonget`, `headerget`, `dbget`, `now()`, `snippet()`, and more |
| **State management** | Per-proxy key-value store backed by SQLite; readable and writable from mock responses |
| **Foreach / template expansion** | Build response arrays from request body fields |
| **_store side-effects** | Persist request fields to state in the same response that returns a mock |
| **Redirect mode** | Proxies ending in `_REDIRECT` bypass mock lookup and always forward upstream |
| **AES encryption** | Encrypt and decrypt payloads via UI or API (CBC mode) |
| **Request history** | Per-proxy log of recent requests with headers, body, status, source, and latency |
| **Rate limiting** | In-memory, per-proxy, configurable via env vars |
| **Domain allowlist** | Restrict proxy targets to approved hostnames (exact-host or suffix match) |
| **Import / Export / Clone** | Move proxy configs and mocks between environments as JSON |
| **Postman export** | One-click download of a Postman v2.1 collection for any proxy's mocks |
| **Dark / Light mode** | System-preference-aware theme toggle, persisted across sessions |
| **UI + API auth** | Session login (browser) or Bearer token (API) |

---

## Quick Start

```bash
# Clone and run
git clone <repo>
cd proxyapp
./setup.sh        # creates .venv, installs deps, bootstraps SQLite schema
./run.sh          # starts on http://localhost:8000

# Or directly
pip install -r requirements.txt
python bootstrap.py
python app.py
```

Copy `.env.example` to `.env` and set at minimum:

```dotenv
PED_SECRET_KEY=<output of: python -c 'import secrets; print(secrets.token_hex(32))'>
PED_UI_PASSWORD=yourpassword
```

---

## Configuration

All configuration is via environment variables (loaded from `.env`).

| Variable | Default | Description |
|---|---|---|
| `PED_PORT` | `8000` | Server port |
| `PED_DEBUG` | `false` | Enable Flask debug mode and DEBUG-level logging |
| `PED_DB_PATH` | `pedapp.db` | SQLite database file path |
| `PED_SECRET_KEY` | **required in prod** | Flask session secret. Raises at startup unless `PED_DEBUG=true`. Generate: `python -c 'import secrets; print(secrets.token_hex(32))'` |
| `PED_UI_PASSWORD` | _(empty)_ | Password for browser login. Empty disables UI auth |
| `PED_API_TOKEN` | _(empty)_ | Bearer token for API auth. Empty disables API auth |
| `PED_MONGO_URI` | `mongodb://localhost:27017` | MongoDB URI — **optional**, only used by `mongo_*` snippet helpers |
| `PED_MONGO_DB` | `pedapp` | MongoDB database name — only relevant when `PED_MONGO_URI` is in use |
| `PED_DEFAULT_SECRET` | — | Default AES key for encrypt/decrypt endpoints |
| `PED_DEFAULT_ENC_IV` | — | Default AES IV (base64) for encrypt/decrypt endpoints |
| `PED_ALLOWED_PROXY_DOMAINS` | _(empty)_ | Comma-separated allowed proxy target hostnames. Empty = allow all. Matching is exact-host or dot-boundary suffix |
| `PED_FORWARD_TIMEOUT` | `30` | Upstream request timeout in seconds |
| `PED_HISTORY_LIMIT` | `100` | Max history entries retained per proxy |
| `PED_RATE_LIMIT_MAX` | `0` | Max requests per window per proxy (`0` = disabled) |
| `PED_RATE_LIMIT_WINDOW` | `60` | Rate limit window in seconds |

---

## Authentication

When either `PED_UI_PASSWORD` or `PED_API_TOKEN` is set, requests to management endpoints require auth.

**Browser:** Log in at `/login`. A session cookie is set for 1 day.

**API:** Send `Authorization: Bearer <PED_API_TOKEN>` on every request.

If both env vars are empty, auth is bypassed (development only).

**Login brute-force protection:** 10 failed attempts per IP per 60 seconds triggers a lockout.

---

## API Reference

### Health

```
GET /health
```
Returns `{status, database, timestamp, version}`.

---

### Encrypt / Decrypt / Prettify

```
POST /ped/encrypt     body: {secret, enc_iv, data}           → {encrypted}
POST /ped/decrypt     body: {secret, enc_iv, encryptedData}  → {decrypted}
POST /ped/prettify    body: {data, processEscape?}           → {prettified}
```

AES-CBC encryption. If `secret`/`enc_iv` are omitted, falls back to `PED_DEFAULT_SECRET` / `PED_DEFAULT_ENC_IV`.

`/ped/prettify` attempts to extract and pretty-print JSON from a raw string. Set `processEscape: true` to also normalise Python-style literals (`True/False/None`) and escape sequences before parsing.

---

### Proxy CRUD

```
POST   /proxy/create/              {identifier?, api_domain}  → {identifier}
GET    /proxy/list/
GET    /proxy/get/<id>/
DELETE /proxy/delete/<id>/
POST   /proxy/clone/               {source_identifier, target_identifier?}
```

`identifier` is auto-generated (shortuuid) when omitted. Deleting a proxy cascades to all its mocks and history.

---

### Mock Management

**Structured registration — `POST /proxy/mock/create/`**

```json
{
  "proxy_identifier": "myproxy",
  "end_point": "/users/<user_id>/profile",
  "method": "GET",
  "mock": { "id": "jsonget(user_id)", "name": "Test User" }
}
```

**URL-mirroring — `POST /mock/<id>/<path>?<qs>`**

Swap `/proxy/` for `/mock/` in any retrieval URL and POST the desired body. Stored with method `*` (matches any HTTP method). Re-POST overwrites.

```bash
# Register
curl -X POST 'http://localhost:8000/mock/myproxy/users/123/profile' \
  -H 'Content-Type: application/json' \
  -d '{"id": "123", "name": "Test User"}'

# Retrieve (any method)
curl 'http://localhost:8000/proxy/myproxy/users/123/profile'
```

Specific-method mocks take precedence over `*` wildcard mocks for the same path.

**Other mock endpoints:**

```
POST /proxy/mock/delete/    {proxy_identifier, end_point, method}
POST /proxy/sequence/reset/ {proxy_identifier, end_point?}
```

---

### Proxy Passthrough

```
ANY /proxy/<id>/<path>?<qs>
```

Per-request flow:

1. **Rate limit check** — `429` if exceeded.
2. **Redirect mode** — if `id` ends with `_REDIRECT`, forward to `api_domain + path` (no mock lookup).
3. **Mock lookup** — try path variants in order: with QS, without QS, with leading slash, full URL. For each: exact method, then `*`. Then try parameterized patterns (`<param>`).
4. **Mock hit** — apply sequencing, conditionals, delay, status wrapper, placeholder resolution. Return response.
5. **Mock miss** — forward to `api_domain + path` (subject to domain allowlist).

---

### Request History

```
GET  /proxy/history/<id>/?limit=50
POST /proxy/history/<id>/clear/
```

History entries contain: `endpoint`, `method`, `request_headers`, `request_body`, `query_params`, `response_status`, `response_body`, `source` (`mock` / `forward` / `redirect` / `mock_register`), `duration_ms`, `created_at`.

---

### Rate Limit Status

```
GET /proxy/ratelimit/<id>/
```

Returns `{identifier, window_seconds, max_requests, current_count, remaining}`.

---

### Per-Proxy State

```
GET    /proxy/state/<id>/
PUT    /proxy/state/<id>/    body: {key: value, ...}   (merge)
PATCH  /proxy/state/<id>/    body: {key: value, ...}   (merge)
DELETE /proxy/state/<id>/                              (clear all)
```

State is stored in SQLite (`proxy_state` table). Used by `dbget()`, `_store` side-effects, and snippet functions.

---

### Import / Export / Postman

```
GET  /proxy/export/<id>/            single proxy (mocks included)
GET  /proxy/export/<id>/postman/    Postman v2.1 collection (downloadable)
GET  /proxy/export/all/             all proxies
POST /proxy/import/                 accepts {identifier,...} or {proxies:{...}} bulk format
```

The Postman export generates a ready-to-import collection with:
- A request item for every registered mock (method + endpoint)
- Auto-inferred example request bodies from `jsonget()` references
- State Management folder (GET/PUT/PATCH state endpoints)
- Available from the **Postman** button in the Manage dashboard and Mock Builder

---

## Mock Response Shapes

The `mock` field (or the body POSTed to `/mock/<id>/<path>`) can be any of:

### 1. Plain dict — returned as-is with `200`

```json
{ "status": "ok", "result": [] }
```

### 2. List — cycling sequence

Each call returns the next element. Counter is per proxy/endpoint/method and resets via `/proxy/sequence/reset/`.

```json
[
  { "attempt": 1, "status": "pending" },
  { "attempt": 2, "status": "processing" },
  { "attempt": 3, "status": "success" }
]
```

### 3. `{status_code, body, headers?}` — custom status

```json
{
  "status_code": 404,
  "body": { "error": "not found" },
  "headers": { "X-Request-Id": "uuid()" }
}
```

### 4. Conditional mock

```json
{
  "conditions": [
    { "field": "amount", "source": "json", "operator": "gt", "value": "1000" }
  ],
  "responses": [
    {
      "when": [{ "field": "currency", "operator": "eq", "value": "USD" }],
      "then": { "status": "approved", "limit": "high" }
    }
  ],
  "default": { "status": "review" }
}
```

### 5. `_delay_ms` — artificial latency

Add `"_delay_ms": 500` to any mock dict. Supports placeholder resolvers. Capped at 30,000 ms.

---

Shapes compose freely: a list of conditional mocks is valid; a conditional's `then` can be a `{status_code, body}` wrapper.

---

## Placeholder Resolvers

String values inside mock responses are resolved at request time.

| Syntax | Source | Resolves to |
|---|---|---|
| `jsonget(field)` | Request JSON body | `body["field"]` (dotted path supported) |
| `jsonget(field, default)` | Request JSON body | `body["field"]` or `default` |
| `headerget(X-Header)` | Request headers | Value of header `X-Header` |
| `paramget(key)` | Query parameters | `?key=value` |
| `pathparamget(users)` | URL path | First segment after `/users/` |
| `pathparamget(users_2)` | URL path | Second `/users/<X>/.../users/<Y>` match |
| `pathparamget(3)` | URL path | Third `/`-separated path segment (1-indexed) |
| `dbget(key)` | Per-proxy state | `state["key"]` (dotted path) |
| `mongoget(col, key, path, default)` | MongoDB | Arbitrary collection lookup |
| `body()` | Request body | Entire JSON body (as dict) |
| `now()` | Server time | UTC ISO-8601 timestamp |
| `now(+3600)` | Server time | UTC ISO-8601 timestamp + offset seconds |
| `now_epoch()` | Server time | Unix epoch float |
| `now_epoch(+60)` | Server time | Unix epoch + offset |
| `uuid()` | Generated | Random UUID v4 |
| `uuid_short()` | Generated | Short UUID |
| `upper(N)` | Generated | N random uppercase letters |
| `lower(N)` | Generated | N random lowercase letters |
| `chars(N)` | Generated | N random mixed-case letters |
| `digit(N)` | Generated | N random digits |
| `alnum(a,b,c,d,...)` | Generated | Alternating letter/digit blocks of given lengths |
| `snippet(<expr>)` | Evaluated | Result of sandboxed expression (see below) |

Resolvers work in any string value, including nested dicts and lists. Non-matching strings are returned as-is.

---

## Snippet Expressions

`snippet(<expr>)` evaluates a sandboxed Python-like expression (via `simpleeval`, max 2000 chars) with full access to request context.

Available variables:

| Variable | Type | Value |
|---|---|---|
| `body` | dict | Request JSON body |
| `header` | dict | Request headers |
| `params` | dict | Query parameters |
| `state` | dict | Per-proxy state |
| `url` | str | Full request URL |
| `now_ts` | float | Current epoch time |

Available functions within snippets:

| Function | Equivalent to |
|---|---|
| `jsonget(path, default?)` | `jsonget()` resolver |
| `dbget(path, default?)` | `dbget()` resolver |
| `headerget(name)` | `headerget()` resolver |
| `paramget(name)` | `paramget()` resolver |
| `pathparamget(prefix)` | `pathparamget()` resolver |
| `now()` | Current UTC ISO string |
| `now_epoch()` | Current epoch float |
| `uuid()` | UUID v4 string |
| `state_all()` | Entire state dict |
| `mongo_find(col, query)` | MongoDB find |
| `mongo_one(col, query)` | MongoDB find_one |
| `mongo_count(col, query)` | MongoDB count |
| `mongo_aggregate(col, pipeline)` | MongoDB aggregate |
| `sql(query, *params)` | SQLite SELECT |
| `sql_one(query, *params)` | SQLite SELECT (first row) |
| `sql_count(query, *params)` | SQLite SELECT (scalar) |
| `valid_token(token)` | Check if token is valid access token |
| `valid_refresh_token(token)` | Check if token is valid refresh token |
| `token_user(token)` | Username for access token |
| `refresh_token_user(token)` | Username for refresh token |
| `bearer_token()` | Extract Bearer token from Authorization header |
| `verify_password(user, pass)` | Verify proxy user credentials |
| `upper/lower/chars/digit/alnum` | Random string generators |

Standard Python builtins available: `abs`, `int`, `float`, `str`, `len`, `min`, `max`, `round`, `sum`, `sorted`, `list`, `dict`, `tuple`, `bool`, `enumerate`, `zip`, `range`, `map`, `filter`, `any`, `all`.

**Example:**

```json
{ "eligible": "snippet(jsonget('amount', 0) > 500 and dbget('user.tier') == 'gold')" }
```

---

## State Management

Each proxy has a persistent key-value store in SQLite (`proxy_state` table).

**Read state:** `dbget(path)` resolver, `state_all()` snippet function, or `GET /proxy/state/<id>/`.

**Write state:** `_store` side-effects in mock responses (see below), `PUT/PATCH /proxy/state/<id>/`.

**State schema example:**

```json
{
  "tokens": {
    "alice": { "accessToken": "abc123", "refreshToken": "xyz789" }
  },
  "orders": {
    "ORD_001": { "status": "shipped" }
  }
}
```

Token helpers (`valid_token`, `token_user`, `bearer_token`) are built around the `tokens.<username>` shape.

> **Comprehensive storage guide:** See [`STORAGE_GUIDE.md`](STORAGE_GUIDE.md) for detailed recipes, gotchas, and patterns (login flows, wallets, order systems, token rotation, double-redeem prevention).

---

## Conditional Mocks

```json
{
  "conditions": [...],     // optional: ALL must match for this mock to apply
  "responses": [
    { "when": [...], "then": {...} },
    { "when": [...], "then": {...} }
  ],
  "default": {...}         // returned when no "when" block matches
}
```

Each condition: `{field, source?, operator, value?}`

**Sources:** `json` (default), `header`, `param`, `path`, `method`, `snippet`

**Operators:** `eq`, `neq`, `contains`, `exists`, `not_exists`, `gt`, `lt`, `starts_with`, `ends_with`, `regex`

`snippet` source: `value` is evaluated as a snippet expression; truthy = condition passes.

---

## Foreach / Template Expansion

Build a response list from a field in the request body:

```json
{
  "items": {
    "_foreach": "cartItems",
    "_template": { "sku": "$item.sku", "qty": "$item.qty", "id": "$index" },
    "_where": [{ "field": "qty", "operator": "gt", "value": "0" }]
  }
}
```

With request body `{"cartItems": [{"sku": "A", "qty": 2}, {"sku": "B", "qty": 0}]}`, this expands to:

```json
{ "items": [{ "sku": "A", "qty": 2, "id": 0 }] }
```

**Tokens:** `$item` / `$value` (element), `$item.field` / `$value.field` (dotted sub-field), `$key` (dict iteration), `$index` (0-based counter).

**`_foreach` source:** field name in the request body (list or dict). For dict sources, iteration is over key-value pairs.

**`_where`** (optional): filter conditions using the same operator set as conditional mocks.

---

## Store Side-Effects

Write values into per-proxy state as a side-effect of returning a mock response.

```json
{
  "_store": [
    { "path": "tokens.jsonget(username).accessToken", "value": "upper(32)" },
    { "path": "tokens.jsonget(username).refreshToken", "value": "upper(32)" }
  ],
  "accessToken": "dbget(tokens.jsonget(username).accessToken)",
  "refreshToken": "dbget(tokens.jsonget(username).refreshToken)"
}
```

Store ops run before the response is built, so `dbget()` in the same response body immediately sees the new value.

**Op shapes:**

```jsonc
{ "path": "a.b.c", "value": <any> }          // set at dotted path
{ "collection": "orders", "key": "O_123", "value": {...} }  // mongo-style namespace
{ "path": "a.b.c", "delete": true }          // remove key
```

`path`, `collection`, and `key` support resolver expressions. `value` supports string resolvers and nested structures (including `_foreach`).

---

## Proxy Users

Per-proxy user credentials (stored in SQLite `proxy_users` table). Used by `verify_password()` and `valid_token()`/`token_user()` snippet functions to simulate auth flows.

```
GET  /proxy/users/<id>/           list users (passwords excluded)
POST /proxy/users/<id>/           {username, password}  upsert user
DELETE /proxy/users/<id>/<user>/  delete user
```

---

## Import / Export / Clone

**Export single proxy:**
```bash
curl -H 'Authorization: Bearer $TOKEN' http://localhost:8000/proxy/export/myproxy/
```

**Export as Postman collection:**
```bash
curl -o myproxy-postman.json http://localhost:8000/proxy/export/myproxy/postman/
# Import into Postman: File → Import → drop the JSON file
```

**Export all:**
```bash
curl -H 'Authorization: Bearer $TOKEN' http://localhost:8000/proxy/export/all/
```

**Import:**
```bash
curl -X POST http://localhost:8000/proxy/import/ \
  -H 'Content-Type: application/json' \
  -H 'Authorization: Bearer $TOKEN' \
  -d @export.json
```

Accepts both single-proxy format `{identifier, api_domain, mocks: {...}}` and bulk format `{proxies: {...}}`.

**Clone:**
```bash
curl -X POST http://localhost:8000/proxy/clone/ \
  -H 'Content-Type: application/json' \
  -d '{"source_identifier": "myproxy", "target_identifier": "myproxy_copy"}'
```

---

## Tutorial

### Scenario 1: Stub a simple API

```bash
# 1. Create a proxy pointing at a real API (or any domain)
curl -X POST http://localhost:8000/proxy/create/ \
  -H 'Content-Type: application/json' \
  -d '{"identifier": "payments", "api_domain": "https://api.example.com"}'

# 2. Register a mock for GET /v1/orders/123
curl -X POST http://localhost:8000/proxy/mock/create/ \
  -H 'Content-Type: application/json' \
  -d '{
    "proxy_identifier": "payments",
    "end_point": "/v1/orders/123",
    "method": "GET",
    "mock": { "id": "123", "status": "shipped" }
  }'

# 3. Hit the proxy — gets the mock, not the real API
curl http://localhost:8000/proxy/payments/v1/orders/123
# → {"id": "123", "status": "shipped"}

# 4. Hit an unstubbed path — forwarded to api.example.com
curl http://localhost:8000/proxy/payments/v1/orders/456
```

---

### Scenario 2: Cycle through states (sequencing)

```bash
curl -X POST http://localhost:8000/proxy/mock/create/ \
  -H 'Content-Type: application/json' \
  -d '{
    "proxy_identifier": "payments",
    "end_point": "/v1/orders/poll",
    "method": "GET",
    "mock": [
      { "status": "pending" },
      { "status": "processing" },
      { "status": "success" }
    ]
  }'

# Call 1 → {"status": "pending"}
# Call 2 → {"status": "processing"}
# Call 3 → {"status": "success"}
# Call 4 → {"status": "pending"}  (wraps)

# Reset the counter
curl -X POST http://localhost:8000/proxy/sequence/reset/ \
  -d '{"proxy_identifier": "payments", "end_point": "/v1/orders/poll"}'
```

---

### Scenario 3: Echo request fields back

```bash
curl -X POST http://localhost:8000/proxy/mock/create/ \
  -H 'Content-Type: application/json' \
  -d '{
    "proxy_identifier": "payments",
    "end_point": "/v1/echo",
    "method": "POST",
    "mock": {
      "received_user": "jsonget(user.id)",
      "received_token": "headerget(Authorization)",
      "received_page": "paramget(page)",
      "timestamp": "now()"
    }
  }'

curl -X POST 'http://localhost:8000/proxy/payments/v1/echo?page=2' \
  -H 'Authorization: Bearer mytoken' \
  -H 'Content-Type: application/json' \
  -d '{"user": {"id": "alice"}}'
# → {"received_user": "alice", "received_token": "Bearer mytoken", "received_page": "2", "timestamp": "2026-04-21T..."}
```

---

### Scenario 4: Conditional responses based on request content

```bash
curl -X POST http://localhost:8000/proxy/mock/create/ \
  -H 'Content-Type: application/json' \
  -d '{
    "proxy_identifier": "payments",
    "end_point": "/v1/charge",
    "method": "POST",
    "mock": {
      "responses": [
        {
          "when": [
            { "field": "amount", "operator": "gt", "value": "10000" }
          ],
          "then": { "status_code": 422, "body": { "error": "amount_too_large" } }
        },
        {
          "when": [
            { "field": "currency", "operator": "eq", "value": "USD" }
          ],
          "then": { "status": "approved", "currency": "USD" }
        }
      ],
      "default": { "status": "approved" }
    }
  }'
```

---

### Scenario 5: Stateful mock login flow

```bash
# Add a proxy user
curl -X POST http://localhost:8000/proxy/users/payments/ \
  -H 'Authorization: Bearer $TOKEN' \
  -H 'Content-Type: application/json' \
  -d '{"username": "alice", "password": "secret123"}'

# Register login mock — verifies credentials, stores token, returns it
curl -X POST http://localhost:8000/proxy/mock/create/ \
  -H 'Content-Type: application/json' \
  -d '{
    "proxy_identifier": "payments",
    "end_point": "/v1/login",
    "method": "POST",
    "mock": {
      "_store": [
        {
          "path": "snippet(\"tokens.\" + jsonget(\"username\") + \".accessToken\")",
          "value": "upper(32)"
        }
      ],
      "responses": [
        {
          "when": [{ "field": "snippet(verify_password(jsonget(\"username\"), jsonget(\"password\")))", "source": "snippet", "operator": "eq", "value": "True" }],
          "then": {
            "accessToken": "snippet(dbget(\"tokens.\" + jsonget(\"username\") + \".accessToken\"))",
            "user": "jsonget(username)"
          }
        }
      ],
      "default": { "status_code": 401, "body": { "error": "invalid_credentials" } }
    }
  }'
```

---

### Scenario 6: URL-mirroring (fastest registration)

```bash
# Register by POSTing to the same path you'll GET from, swapping /proxy/ → /mock/
curl -X POST 'http://localhost:8000/mock/payments/v1/products?category=electronics' \
  -H 'Content-Type: application/json' \
  -d '{"products": [{"id": 1, "name": "Laptop"}]}'

# Retrieve
curl 'http://localhost:8000/proxy/payments/v1/products?category=electronics'
```

---

### Scenario 7: Transparent redirect proxy

```bash
# Create a proxy with _REDIRECT suffix — always forwards, never mocks
curl -X POST http://localhost:8000/proxy/create/ \
  -H 'Content-Type: application/json' \
  -d '{"identifier": "live_REDIRECT", "api_domain": "https://api.example.com"}'

# All requests forwarded straight to api.example.com; history still recorded
curl http://localhost:8000/proxy/live_REDIRECT/v1/products
```

---

### Scenario 8: Inspect what was sent upstream

```bash
# View last 10 requests
curl 'http://localhost:8000/proxy/history/payments/?limit=10' \
  -H 'Authorization: Bearer $TOKEN'

# Clear history
curl -X POST http://localhost:8000/proxy/history/payments/clear/ \
  -H 'Authorization: Bearer $TOKEN'
```

---

## Deployment

### PythonAnywhere

The app resolves all paths relative to `app.py`, so it works out-of-the-box with PythonAnywhere WSGI.

1. Upload project files.
2. Run `python bootstrap.py` once to create the SQLite schema.
3. Point the WSGI config at `app.py`.
4. Set environment variables (or keep `.env` beside `app.py`).
5. Ensure `PED_SECRET_KEY` is set — the app refuses to start without it.

### Docker / general WSGI

Use any WSGI server (gunicorn, uWSGI). Example:

```bash
gunicorn -w 4 -b 0.0.0.0:8000 'app:app'
```

State and proxy users are stored in SQLite — no external database required. MongoDB is optional and only needed if you use `mongo_*` snippet helpers.

---

## Security Notes

- Set `PED_SECRET_KEY` to a strong random value (64 hex chars).
- Set `PED_UI_PASSWORD` and/or `PED_API_TOKEN` — if both are empty, auth is bypassed.
- Set `PED_ALLOWED_PROXY_DOMAINS` to the specific hostnames you proxy to, preventing use as an open SSRF relay.
- The session cookie has `HttpOnly=True`, `SameSite=Lax`, and a 1-day lifetime.
- Security headers (`X-Frame-Options: DENY`, `X-Content-Type-Options: nosniff`, `Referrer-Policy`) are set on every response.
- `_delay_ms` is capped at 30,000 ms to prevent worker thread exhaustion.
- Login is rate-limited to 10 attempts per IP per 60 seconds.
- Rotating `PED_SECRET_KEY` invalidates all existing session cookies — users will be bounced to `/login`.
