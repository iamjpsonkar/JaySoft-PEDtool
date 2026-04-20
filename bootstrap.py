#!/usr/bin/env python3
"""
First-time setup for PED Tools.

- Creates SQLite tables (idempotent; safe to re-run)
- Migrates legacy proxy_server.json -> SQLite (one-shot; renames source to .migrated)

Run explicitly:
    python bootstrap.py

run.sh invokes this before starting the app.
"""

from __future__ import annotations

import json
import logging
import os
import sqlite3
import sys

from dotenv import load_dotenv

_BASE_DIR = os.path.dirname(os.path.abspath(__file__))
load_dotenv(os.path.join(_BASE_DIR, ".env"))

DB_PATH = os.environ.get("PED_DB_PATH", os.path.join(_BASE_DIR, "pedapp.db"))

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
logger = logging.getLogger("pedapp.bootstrap")


SCHEMA = """
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


def init_db(db_path: str = DB_PATH) -> None:
    """Create all tables. Idempotent."""
    logger.info("[BOOTSTRAP] init_db start db_path=%s", db_path)
    conn = sqlite3.connect(db_path)
    try:
        conn.executescript(SCHEMA)
        conn.commit()
    finally:
        conn.close()
    logger.info("[BOOTSTRAP] init_db ok db_path=%s", db_path)


def migrate_from_json(json_path: str, db_path: str = DB_PATH) -> None:
    """One-shot migration of legacy proxy_server.json into SQLite.

    Renames the source file to ``<path>.migrated`` on success to ensure a
    single-run behaviour. No-op when the file doesn't exist or is empty.
    """
    if not os.path.exists(json_path):
        logger.debug("[BOOTSTRAP] migrate_from_json skip (no file) path=%s", json_path)
        return

    logger.info("[BOOTSTRAP] migrate_from_json start path=%s", json_path)
    try:
        with open(json_path, "r") as f:
            data = json.load(f)

        if not data:
            logger.info("[BOOTSTRAP] migrate_from_json skip (empty) path=%s", json_path)
            return

        conn = sqlite3.connect(db_path)
        try:
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
                            "INSERT OR IGNORE INTO mocks "
                            "(proxy_id, endpoint, method, response) VALUES (?, ?, ?, ?)",
                            (identifier, endpoint, method, json.dumps(response)),
                        )
                        count_mocks += 1
            conn.commit()
        finally:
            conn.close()

        backup = json_path + ".migrated"
        os.rename(json_path, backup)
        logger.info(
            "[BOOTSTRAP] migrate_from_json ok proxies=%d mocks=%d backup=%s",
            count_proxies, count_mocks, backup,
        )
    except Exception:
        logger.exception("[BOOTSTRAP] migrate_from_json failed path=%s", json_path)
        raise


def main() -> int:
    init_db()
    migrate_from_json(os.path.join(_BASE_DIR, "proxy_server.json"))
    return 0


if __name__ == "__main__":
    sys.exit(main())
