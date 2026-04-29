# PED Tools — Stateful Mock Storage Guide

This guide explains how to use `_store` and `dbget` to create mocks that remember data between requests — turning flat static mocks into stateful simulations (wallets, order systems, login flows, etc.).

---

## Table of Contents

1. [Concepts](#1-concepts)
2. [State API (CRUD)](#2-state-api-crud)
3. [Writing to State: `_store`](#3-writing-to-state-_store)
4. [Reading from State: `dbget`](#4-reading-from-state-dbget)
5. [The Staging Pattern (lastId)](#5-the-staging-pattern-lastid)
6. [Resolvers You Can Use Inside `_store`](#6-resolvers-you-can-use-inside-_store)
7. [Conditional Mocks + State](#7-conditional-mocks--state)
8. [Gotchas and Rules](#8-gotchas-and-rules)
9. [Recipes](#9-recipes)

---

## 1. Concepts

Each proxy has a **state** — a JSON object stored in SQLite. Think of it as a per-proxy database.

```
state = {
  "tokens": {},
  "transactions": {},
  "wallet": { "bonus": 2000 }
}
```

- **`_store`** — a special key in your mock response that **writes** to state as a side-effect before returning the response.
- **`dbget(path)`** — a resolver that **reads** from state and inserts the value into your mock response.

**Execution order in a request:**

```
1. Condition matching (if conditional mock)
2. _store runs  →  writes to state, commits to DB
3. Body resolves  →  dbget() reads from DB (sees what _store just wrote)
4. Response returned to caller
```

This order is critical — the body always sees the state AFTER `_store` has committed.

---

## 2. State API (CRUD)

Manage state directly via HTTP. No auth required.

| Action | Method | URL | Body |
|--------|--------|-----|------|
| Read | GET | `/proxy/state/<proxy_id>/` | — |
| Replace | PUT | `/proxy/state/<proxy_id>/` | `{"wallet": {"bonus": 2000}}` |
| Merge | PATCH | `/proxy/state/<proxy_id>/` | `{"wallet": {"bonus": 5000}}` |
| Clear | DELETE | `/proxy/state/<proxy_id>/` | — |

**Seed state before first use:**

```bash
curl -X PUT 'https://yourserver.com/proxy/state/myproxy/' \
  -H 'Content-Type: application/json' \
  -d '{"wallet": {"bonus": 2000}, "orders": {}, "tokens": {}}'
```

---

## 3. Writing to State: `_store`

Add a `_store` key to any mock response. It is popped (removed) before the response is sent — the caller never sees it.

### 3.1 Single operation

```json
{
  "_store": { "path": "counter", "value": 42 },
  "message": "stored"
}
```

### 3.2 Multiple operations (list)

```json
{
  "_store": [
    { "path": "user.name", "value": "alice" },
    { "path": "user.role", "value": "admin" }
  ],
  "result": "ok"
}
```

### 3.3 Operation shapes

#### SET by dotted path (most common)

```json
{ "path": "orders.ORD123.status", "value": "CREATED" }
```

Creates nested structure automatically: `state.orders.ORD123.status = "CREATED"`

#### SET by collection + key

```json
{ "collection": "orders", "key": "ORD123", "value": { "status": "CREATED" } }
```

Same result as: `state.orders.ORD123 = { "status": "CREATED" }`

#### DELETE

```json
{ "path": "tokens.alice", "delete": true }
```

Removes `state.tokens.alice` entirely.

### 3.4 Dynamic paths (using resolvers)

Paths are resolved through the resolver pipeline. Use resolvers **without quotes** in path templates:

```json
{ "path": "orders.jsonget(orderId).status", "value": "CREATED" }
```

If the request body is `{"orderId": "ORD_456"}`, the path becomes `orders.ORD_456.status`.

### 3.5 Dynamic values

Values are also resolved:

```json
{ "path": "user.lastLogin", "value": "now()" }
{ "path": "user.name", "value": "jsonget(name)" }
{ "path": "user.id", "value": "alnum(8,4)" }
{ "path": "wallet.bonus", "value": "snippet(dbget('wallet.bonus', 0) + jsonget('amount', 0))" }
```

If the value is a dict or list, every string inside it is also resolved recursively:

```json
{
  "path": "orders.jsonget(orderId)",
  "value": {
    "amount": "jsonget(amount)",
    "status": "CREATED",
    "createdAt": "now()"
  }
}
```

---

## 4. Reading from State: `dbget`

### 4.1 In mock response values (standalone resolver)

```json
{
  "balance": "dbget(wallet.bonus, 0)",
  "username": "dbget(currentUser, anonymous)"
}
```

Syntax: `dbget(dotted.path, default)`

- **No quotes** around the path or default (they become part of the key if included).
- Default is optional. If omitted and path not found, returns the literal string `dbget(...)`.

### 4.2 In snippet expressions

```json
{
  "balance": "snippet(dbget('wallet.bonus', 0))",
  "enough": "snippet(dbget('wallet.bonus', 0) >= jsonget('amount', 0))",
  "total": "snippet(dbget('wallet.bonus', 0) + dbget('wallet.rcs', 0))"
}
```

Inside `snippet()`, `dbget` is a Python function — **use quotes** around string arguments (standard Python syntax). Defaults preserve their type (int, bool, None, etc.).

### 4.3 Standalone vs Snippet — when to use which

| Use case | Syntax | Example |
|----------|--------|---------|
| Simple read, string is fine | standalone | `"dbget(user.name, guest)"` |
| Need a number | snippet | `"snippet(dbget('wallet.bonus', 0))"` |
| Need arithmetic | snippet | `"snippet(dbget('balance', 0) - jsonget('amount', 0))"` |
| Need comparison | snippet | `"snippet(dbget('balance', 0) >= 100)"` |
| String concatenation | snippet | `"snippet('Hello ' + dbget('user.name', 'world'))"` |

**Why?** The standalone `dbget()` resolver always returns a string for the default. `snippet()` preserves Python types.

---

## 5. The Staging Pattern (lastId)

The most important pattern for stateful mocks. Use it whenever you need to:
- Generate a random ID
- Store data under that ID
- Return the ID in the response

### The problem

You can't use the same `alnum()` in both the path and the response — it generates a **different** random value each time.

### The solution

Store the generated ID in a temporary state key first. Later ops and the body read it back.

```json
{
  "_store": [
    { "path": "lastOrderId", "value": "alnum(8,4)" },
    { "path": "orders.dbget(lastOrderId).status", "value": "CREATED" },
    { "path": "orders.dbget(lastOrderId).amount", "value": "jsonget(amount)" }
  ],
  "orderId": "snippet(dbget('lastOrderId'))"
}
```

**How it works:**

1. Op 1: `alnum(8,4)` generates e.g. `"AbCdEfGh1234"`, stores at `state.lastOrderId`
2. Op 2: `dbget(lastOrderId)` resolves to `"AbCdEfGh1234"`, path becomes `orders.AbCdEfGh1234.status`
3. Op 3: same — stores `orders.AbCdEfGh1234.amount`
4. After `_store` commits, body resolves: `snippet(dbget('lastOrderId'))` reads `"AbCdEfGh1234"` from DB

**Sequential ops see each other's writes** — within a single `_store` list, each op can read values written by previous ops via `dbget()`.

### Nested object path workaround

`_resolve_path_template` splits on `.` naively. So `jsonget(giftcard.cardNumber)` in a path **breaks** (the dot inside the arg is treated as a path separator).

**Fix:** stage it first:

```json
"_store": [
  { "path": "lastCardNum", "value": "snippet(str(jsonget('giftcard.cardNumber')))" },
  { "path": "cards.dbget(lastCardNum).redeemed", "value": true }
]
```

---

## 6. Resolvers You Can Use Inside `_store`

These work in both `path` and `value` fields:

| Resolver | What it does | Example |
|----------|-------------|---------|
| `jsonget(field)` | Read from request body | `jsonget(amount)` |
| `headerget(name)` | Read request header | `headerget(Authorization)` |
| `paramget(name)` | Read query parameter | `paramget(page)` |
| `dbget(path)` | Read from current state | `dbget(lastOrderId)` |
| `alnum(L,D)` | Random L letters + D digits | `alnum(8,4)` → `"AbCdEfGh1234"` |
| `digit(N)` | Random N digits | `digit(6)` → `"482910"` |
| `upper(N)` | Random N uppercase letters | `upper(4)` → `"XKQM"` |
| `lower(N)` | Random N lowercase letters | `lower(4)` → `"abcd"` |
| `now()` | ISO timestamp | `"2026-04-29T12:00:00Z"` |
| `now(+3600)` | Timestamp + offset (seconds) | future timestamp |
| `snippet(expr)` | Python expression | `snippet(dbget('x', 0) + 1)` |

### Snippet-only functions (available inside `snippet()`)

`abs`, `int`, `float`, `str`, `len`, `min`, `max`, `round`, `sum`, `sorted`, `list`, `dict`, `bool`, `range`

Plus context functions: `jsonget()`, `dbget()`, `headerget()`, `paramget()`, `now()`, `now_epoch()`, `upper()`, `lower()`, `digit()`, `alnum()`, `valid_token()`, `valid_refresh_token()`, `token_user()`, `refresh_token_user()`, `verify_password()`, `bearer_token()`

---

## 7. Conditional Mocks + State

Combine conditions with `_store` for if-this-then-that logic.

### Structure

```json
{
  "conditions": [],
  "responses": [
    {
      "when": [ ...conditions... ],
      "then": {
        "_store": [ ...ops... ],
        "body": { ... },
        "status_code": 200
      }
    },
    {
      "when": [ ...conditions... ],
      "then": { ... }
    }
  ],
  "default": {
    "body": { "error": "No match" },
    "status_code": 400
  }
}
```

Responses are checked in order. First match wins. If none match, `default` is used.

### Condition types

**Check request body field:**
```json
{ "field": "username", "operator": "exists", "source": "json" }
{ "field": "amount", "operator": "gt", "source": "json", "value": "0" }
```

**Check state via snippet (most powerful):**
```json
{ "source": "snippet", "value": "dbget('wallet.bonus', 0) >= jsonget('amount', 0)" }
```

**Lookup-or-404 pattern:**
```json
{
  "source": "snippet",
  "value": "dbget('orders.' + jsonget('orderId', '__NO__'), '__NO__') != '__NO__'"
}
```

### Operators

`eq`, `neq`, `contains`, `exists`, `not_exists`, `gt`, `lt`, `starts_with`, `ends_with`, `regex`

---

## 8. Gotchas and Rules

### Path templates: NO quotes around resolver args

```
WRONG:  tokens.dbget('lastUser').accessToken     ← quotes become part of the key
RIGHT:  tokens.dbget(lastUser).accessToken        ← resolves correctly
```

### Snippet strings: YES quotes (Python syntax)

```
RIGHT:  snippet(dbget('wallet.bonus', 0))         ← Python string argument
RIGHT:  snippet(dbget('tokens.' + dbget('lastUser', '') + '.accessToken'))
```

### Dotted args in path templates break

```
WRONG:  giftcards.jsonget(giftcard.cardNumber)    ← splits on inner dot
RIGHT:  Stage it first, then use dbget(lastCardNum)
```

### `_store` runs BEFORE body resolution

- Body `dbget()` always sees what `_store` just wrote.
- But `_store` cannot see what the body will return (it runs first).

### Sequential `_store` ops see each other

```json
"_store": [
  { "path": "tempId", "value": "alnum(8,4)" },
  { "path": "items.dbget(tempId).name", "value": "test" }
]
```

Op 2 sees `tempId` written by Op 1.

### Don't re-lookup rotated values

If `_store` overwrites a token, the body can't look it up by the OLD value anymore.

```
WRONG:  _store rotates refreshToken, body calls refresh_token_user(old_token) → None → crash
RIGHT:  Stage the username first, reference it in body via dbget(lastRefreshUser)
```

### Standalone `dbget` default is always a string

`dbget(balance, 0)` → if not found, returns `"0"` (string), not `0` (int).
Use `snippet(dbget('balance', 0))` when you need a number.

---

## 9. Recipes

### 9.1 Login with token storage

**Seed:** `PUT /proxy/state/myproxy/ {"tokens": {}}`

```json
{
  "conditions": [],
  "responses": [{
    "when": [
      { "field": "username", "operator": "exists", "source": "json" },
      { "field": "password", "operator": "exists", "source": "json" }
    ],
    "then": {
      "_store": [
        { "path": "tokens.jsonget(username).accessToken", "value": "alnum(16,16)" },
        { "path": "tokens.jsonget(username).refreshToken", "value": "alnum(16,16)" }
      ],
      "body": {
        "accessToken": "snippet(dbget('tokens.' + jsonget('username') + '.accessToken'))",
        "refreshToken": "snippet(dbget('tokens.' + jsonget('username') + '.refreshToken'))",
        "expiresIn": 3600
      },
      "status_code": 200
    }
  }],
  "default": { "body": { "error": "Missing credentials" }, "status_code": 400 }
}
```

### 9.2 Token refresh with rotation

**Key:** stage the username before rotating, so body can find it after.

```json
{
  "conditions": [],
  "responses": [{
    "when": [{ "source": "snippet", "value": "valid_refresh_token(jsonget('refreshToken', ''))" }],
    "then": {
      "_store": [
        { "path": "lastRefreshUser", "value": "snippet(str(refresh_token_user(jsonget('refreshToken'))))" },
        { "path": "tokens.dbget(lastRefreshUser).accessToken", "value": "alnum(16,16)" },
        { "path": "tokens.dbget(lastRefreshUser).refreshToken", "value": "alnum(16,16)" }
      ],
      "body": {
        "accessToken": "snippet(dbget('tokens.' + dbget('lastRefreshUser', '') + '.accessToken'))",
        "refreshToken": "snippet(dbget('tokens.' + dbget('lastRefreshUser', '') + '.refreshToken'))",
        "expiresIn": 3600
      },
      "status_code": 200
    }
  }],
  "default": { "body": { "error": "Invalid refresh token" }, "status_code": 401 }
}
```

### 9.3 Create + Lookup (order system)

**Seed:** `PUT /proxy/state/myproxy/ {"orders": {}}`

**POST (create order):**

```json
{
  "_store": [
    { "path": "lastOrderId", "value": "alnum(8,4)" },
    { "path": "orders.dbget(lastOrderId).amount", "value": "jsonget(amount, 0)" },
    { "path": "orders.dbget(lastOrderId).status", "value": "CREATED" }
  ],
  "orderId": "snippet(dbget('lastOrderId'))",
  "status": "CREATED"
}
```

**POST (get order status) — lookup or 404:**

```json
{
  "conditions": [],
  "responses": [{
    "when": [{
      "source": "snippet",
      "value": "dbget('orders.' + jsonget('orderId', '__NO__'), '__NO__') != '__NO__'"
    }],
    "then": {
      "orderId": "jsonget(orderId)",
      "amount": "snippet(dbget('orders.' + jsonget('orderId') + '.amount', 0))",
      "status": "snippet(dbget('orders.' + jsonget('orderId') + '.status', 'UNKNOWN'))"
    }
  }],
  "default": { "body": { "error": "Order not found" }, "status_code": 404 }
}
```

### 9.4 Wallet with balance tracking

**Seed:** `PUT /proxy/state/myproxy/ {"wallet": {"bonus": 2000}}`

**Read balance (userWallet):**

```json
{
  "balance": "snippet(dbget('wallet.bonus', 0))",
  "status": "SUCCESS"
}
```

**Deduct (payment):**

```json
{
  "_store": [
    { "path": "wallet.bonus", "value": "snippet(max(0, dbget('wallet.bonus', 0) - jsonget('amount', 0)))" }
  ],
  "newBalance": "snippet(dbget('wallet.bonus', 0))",
  "status": "SUCCESS"
}
```

**Add back (refund):**

```json
{
  "_store": [
    { "path": "wallet.bonus", "value": "snippet(dbget('wallet.bonus', 0) + jsonget('amount', 0))" }
  ],
  "newBalance": "snippet(dbget('wallet.bonus', 0))",
  "status": "REFUNDED"
}
```

### 9.5 Gift card with double-redeem prevention

**Seed:** `PUT /proxy/state/myproxy/ {"giftcards": {}, "wallet": {"bonus": 2000}}`

```json
{
  "conditions": [],
  "responses": [
    {
      "when": [{
        "source": "snippet",
        "value": "dbget('giftcards.' + str(jsonget('cardNumber', '__NONE__')) + '.redeemed', False) == True"
      }],
      "then": {
        "body": { "error": "Card already redeemed" },
        "status_code": 400
      }
    },
    {
      "when": [{ "field": "cardNumber", "operator": "exists", "source": "json" }],
      "then": {
        "_store": [
          { "path": "lastCard", "value": "snippet(str(jsonget('cardNumber')))" },
          { "path": "giftcards.dbget(lastCard).redeemed", "value": true },
          { "path": "wallet.bonus", "value": "snippet(dbget('wallet.bonus', 0) + 500)" }
        ],
        "body": { "amount": 500, "message": "Redeemed successfully" },
        "status_code": 200
      }
    }
  ],
  "default": { "body": { "error": "Invalid card" }, "status_code": 400 }
}
```

### 9.6 Status mutation (undo/cancel)

```json
{
  "conditions": [],
  "responses": [{
    "when": [{ "field": "transactionId", "operator": "exists", "source": "json" }],
    "then": {
      "_store": [
        { "path": "transactions.jsonget(transactionId).status", "value": "REVERSED" },
        { "path": "wallet.bonus", "value": "snippet(dbget('wallet.bonus', 0) + dbget('transactions.' + jsonget('transactionId') + '.amount', 0))" }
      ]
    }
  }],
  "default": {}
}
```

Note: the `then` has only `_store` and no `body`/`status_code` — returns `{}` with 200.

---

## Quick Reference

```
PATH TEMPLATES (in _store path)       SNIPPET EXPRESSIONS (in values)
─────────────────────────────────     ──────────────────────────────────
No quotes:  dbget(myKey)              Quotes:  dbget('myKey', 0)
No quotes:  jsonget(orderId)          Quotes:  jsonget('orderId', '')
No dots:    dbget(lastId)             Dots OK: dbget('orders.ABC.status')
Stage first if arg has dots            Arithmetic: dbget('x', 0) + 1

_store op shapes:
  { "path": "a.b.c", "value": X }          # set
  { "collection": "a", "key": "b", "value": X }  # set (alt)
  { "path": "a.b.c", "delete": true }      # delete
```
