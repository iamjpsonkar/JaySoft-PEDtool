# PED Tools

A Flask-based proxy, mock server, and AES encryption utility with a web UI.

## Features

- **HTTP Proxy** — forward requests to target URLs, with per-proxy mock overrides
- **Mock Server** — register canned JSON responses; supports sequencing, conditionals, delays, custom status/headers, and dynamic value resolvers
- **URL-mirroring mock registration** — swap `/proxy/` → `/mock/` in the retrieval URL and `POST` to register a mock for that URL in one call
- **AES Encryption/Decryption** — encrypt and decrypt payloads via the UI or API
- **Request History** — per-proxy log of recent requests (configurable limit)
- **UI Authentication** — session login screen protecting management pages
- **API Token Auth** — bearer token for programmatic access
- **Rate Limiting** — per-proxy request rate limits
- **Domain Allowlist** — restrict proxy targets to approved domains
- **Import / Export / Clone** — move mocks between environments

## Requirements

- Python 3.9+
- Dependencies in [requirements.txt](requirements.txt)

```bash
pip install -r requirements.txt
```

## Configuration

Copy [.env](.env) and adjust values as needed:

| Variable | Default | Description |
|---|---|---|
| `PED_PORT` | `8000` | Server port |
| `PED_DEBUG` | `false` | Enable Flask debug mode & debug-level logging |
| `PED_DB_PATH` | `pedapp.db` | SQLite database path |
| `PED_UI_PASSWORD` | _(empty)_ | UI login password (empty disables UI auth) |
| `PED_API_TOKEN` | _(empty)_ | Bearer token for API auth (empty disables API auth) |
| `PED_SECRET_KEY` | **required in prod** | Flask session secret. Missing value raises at startup unless `PED_DEBUG=true` |
| `PED_DEFAULT_SECRET` | — | Default AES key |
| `PED_DEFAULT_ENC_IV` | — | Default AES IV (base64) |
| `PED_ALLOWED_PROXY_DOMAINS` | _(empty)_ | Comma-separated allowed target hostnames. Empty = allow all. Match is exact-host or dot-boundary suffix |
| `PED_FORWARD_TIMEOUT` | `30` | Upstream request timeout (seconds) |
| `PED_HISTORY_LIMIT` | `100` | Max history entries retained per proxy |
| `PED_RATE_LIMIT_MAX` | `0` | Max requests per window per proxy (`0` = disabled) |
| `PED_RATE_LIMIT_WINDOW` | `60` | Rate limit window (seconds) |

> **Production checklist**
> - Set a strong `PED_SECRET_KEY` (e.g. `python -c 'import secrets; print(secrets.token_hex(32))'`).
> - Set `PED_UI_PASSWORD` and/or `PED_API_TOKEN`.
> - Set `PED_ALLOWED_PROXY_DOMAINS` to the hostnames you actually proxy to, so the server cannot be used as an open SSRF relay.
> - Rotating `PED_SECRET_KEY` invalidates all existing session cookies — users will be bounced to `/login`.

## Running

```bash
./run.sh            # creates .venv, installs, runs app.py
# or
python app.py
```

The app listens at `http://localhost:8000` (or `PED_PORT`).

### First-time setup

```bash
./setup.sh
```

Runs the venv + deps install, backs up any existing DB, and applies the schema via `bootstrap.py`. Idempotent — safe to re-run after pulling changes.

## Authentication

Requests must be authenticated if **either** `PED_UI_PASSWORD` or `PED_API_TOKEN` is set:

- **Browser** — log in at `/login`; session cookie is used automatically.
- **API** — send `Authorization: Bearer <PED_API_TOKEN>`.

If both env vars are empty, auth is bypassed (local dev only).

## API reference

### Health

```
GET /health            # {status, database, timestamp, version}
```

### Encrypt / Decrypt / Prettify

```
POST /ped/encrypt      # {secret, enc_iv, data}        → {encrypted}
POST /ped/decrypt      # {secret, enc_iv, encryptedData} → {decrypted}
POST /ped/prettify     # {data, processEscape?}        → {prettified}
```

### Proxy CRUD

```
POST   /proxy/create/              # {identifier?, api_domain}
GET    /proxy/list/
GET    /proxy/get/<id>/
DELETE /proxy/delete/<id>/
POST   /proxy/clone/               # {source_identifier, target_identifier?}
```

If `identifier` is omitted on create, a shortuuid is generated.

### Mock management

Two ways to register a mock:

**1. Structured — `POST /proxy/mock/create/`**

```json
{
  "proxy_identifier": "juspay",
  "end_point": "/merchants/<merchant_id>/paymentmethods?options.add_outage=true",
  "method": "GET",
  "mock": { "outage": [ ... ] }
}
```

**2. URL-mirroring — `POST /mock/<id>/<path>?<query>`**

Swap `/proxy/` → `/mock/` in the retrieval URL and POST the desired response body. Method, path, and query are derived from the URL. Stored with method `*` so any retrieval method matches it. Re-POST to overwrite.

```bash
# Register
curl -X POST 'http://localhost:8000/mock/juspay/merchants/test_id/paymentmethods?options.add_outage=true' \
  -H 'Content-Type: application/json' \
  -H 'Authorization: Bearer $PED_API_TOKEN' \
  -d '{"outage": [ ... ]}'

# Retrieve
curl 'http://localhost:8000/proxy/juspay/merchants/test_id/paymentmethods?options.add_outage=true'
```

Specific-method mocks (registered via `/proxy/mock/create/`) take precedence over `*` fallbacks for the same path key.

**Other mock endpoints:**

```
POST /proxy/mock/delete/           # {proxy_identifier, end_point, method}
POST /proxy/sequence/reset/        # {proxy_identifier, end_point?}
```

### Proxy passthrough

```
ANY /proxy/<id>/<path>?<query>
```

Per-request flow:
1. Rate limit check → `429` if exceeded.
2. If the identifier ends with `_REDIRECT`, forward unconditionally to `api_domain + path` (skipping mock lookup).
3. Otherwise: match against registered mocks (exact path first, then `<param>` pattern regex; for each, exact method then `*`).
4. On mock hit — apply sequencing / conditionals / delay / status_code wrapper / placeholder resolvers, return the response.
5. On mock miss — forward to `api_domain + path` (subject to allowlist).

### Request history

```
GET  /proxy/history/<id>/?limit=50
POST /proxy/history/<id>/clear/
```

Entries record `source`: `"mock"`, `"forward"`, `"redirect"`, or `"mock_register"` (URL-mirroring registrations).

### Rate limit status

```
GET /proxy/ratelimit/<id>/
```

Returns current count, window, and remaining quota.

### Import / Export

```
GET  /proxy/export/<id>/           # one proxy
GET  /proxy/export/all/            # everything
POST /proxy/import/                # accepts single {identifier,...} or bulk {proxies:{...}} format
```

## Mock response shapes

The value stored in `mock` (or the body POSTed to `/mock/<id>/<path>`) can be any of:

- **Plain dict** — returned as-is with `200`.
- **List** — cycled on each call (sequencing); counter is per proxy/endpoint/method and resettable via `/proxy/sequence/reset/`.
- **`{status_code, body, headers?}`** — custom response status and headers.
- **`{conditions, responses: [{when:[...], then:{...}}], default}`** — first matching `when` block's `then` is returned; falls back to `default`. Each condition has `{field, source?, operator, value}` where `source` ∈ `json|header|param` (default `json`) and `operator` ∈ `eq|neq|contains|exists|not_exists|gt|lt`.
- **`_delay_ms` key** — artificial latency in milliseconds, consumed before response.

Shapes compose: a list of conditional mocks is valid, as is a conditional whose branches use `{status_code, body}` wrappers.

## Placeholder resolvers

String values inside a mock response are resolved at request time:

| Syntax | Resolves to |
|---|---|
| `headerget(X)` | request header `X` |
| `jsonget(X)` | top-level field `X` of the JSON request body |
| `paramget(X)` | query parameter `X` |
| `pathparamget(users)` | first segment after `/users/` in the URL |
| `pathparamget(users_2)` | second `/users/<X>/.../users/<Y>/...` match |
| `pathparamget(3)` | third `/`-separated path segment (1-indexed) |
| `upper(N)` / `lower(N)` / `chars(N)` / `digit(N)` | random string of length `N` |
| `alnum(a,b,c,d,...)` | alternating letters/digits blocks |
| `snippet(<expr>)` | safe `simpleeval` expression (2000 char limit) |

## Special identifiers

- **`<name>_REDIRECT`** — proxies whose identifier ends with `_REDIRECT` bypass all mock lookup and forward every request upstream. Useful as a transparent pass-through while keeping the domain allowlist / history / rate-limit features.

## Deployment (PythonAnywhere)

The app resolves paths relative to `app.py`, so it works out-of-the-box with PythonAnywhere WSGI. Point your WSGI config at `app.py`, keep `.env` beside it, and ensure `PED_SECRET_KEY` is set — the app refuses to start without it unless `PED_DEBUG=true`.
