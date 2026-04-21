# PED Tools — Claude Instructions

## context.md maintenance (MANDATORY)

**After every code change in this project, update `context.md`.**

Specifically update:
- Section 23 (Changelog) — add a dated entry describing what changed and why
- Any section whose content was affected by the change (routes, resolvers, security properties, known limitations, data flow, etc.)

Do not skip this even for small changes. `context.md` is the single source of truth for any AI working on this project.

## Project summary

This is a Flask-based HTTP proxy + mock server. The main file is `app.py` (~3090 lines). Read `context.md` at the start of any session for full architecture context before making changes.

## Code style rules

- No unused imports
- No placeholder stubs or TODO code
- Log at all important execution points (see global CLAUDE.md for logging standards)
- Match existing patterns for route handlers: `@app.route` → `@require_auth` (if needed) → `@log_access` → function
- Do not add `background=True` to pymongo index creation (deprecated in pymongo 4.x)
- Do not call `flask_request.json` directly — use `flask_request.get_json(silent=True)`

## Key architectural invariants

1. `API.__init__` copies `flask_request.args` into `self.params`. Never also append `?qs` to the URL passed to `API()` — it will double the query string.
2. `_snippet_context` captures `_state` once. All inner functions (`_fn_valid_refresh_token`, `_fn_token_user`, etc.) must use the captured `_state`, not call `_get_state_for_resolver()` again.
3. `_apply_store_ops` must set `_store_pending_state.entry` before any `dbget()` resolvers run on later ops in the same batch. This enables sequential ops to read each other's writes.
4. `_delay_ms` is capped at 30,000 ms. Never remove this cap.
5. The `next=` parameter on `/login` must always reject values containing `://` or `//` to prevent open redirect.
