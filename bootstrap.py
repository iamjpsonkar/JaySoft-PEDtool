#!/usr/bin/env python3
"""
First-time setup for PED Tools.

Creates the SQLite schema. Idempotent — safe to re-run.

Usage:
    python bootstrap.py

setup.sh / run.sh invoke this before starting the app.
"""

from __future__ import annotations

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

CREATE TABLE IF NOT EXISTS proxy_state (
    proxy_id    TEXT PRIMARY KEY,
    data        TEXT NOT NULL DEFAULT '{}'
);

CREATE TABLE IF NOT EXISTS proxy_users (
    proxy_id    TEXT NOT NULL,
    username    TEXT NOT NULL,
    password    TEXT NOT NULL,
    PRIMARY KEY (proxy_id, username)
);

CREATE TABLE IF NOT EXISTS state_snapshots (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    proxy_id    TEXT NOT NULL,
    name        TEXT NOT NULL,
    data        TEXT NOT NULL,
    created_at  TEXT NOT NULL DEFAULT (datetime('now'))
);
CREATE INDEX IF NOT EXISTS idx_snapshots_proxy ON state_snapshots(proxy_id);

CREATE TABLE IF NOT EXISTS mock_templates (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    name        TEXT NOT NULL UNIQUE,
    description TEXT,
    template    TEXT NOT NULL,
    category    TEXT NOT NULL DEFAULT 'general',
    created_at  TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS suggestions (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    name        TEXT NOT NULL DEFAULT 'Anonymous',
    message     TEXT NOT NULL,
    created_at  TEXT NOT NULL DEFAULT (datetime('now'))
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


def main() -> int:
    init_db()
    return 0


if __name__ == "__main__":
    sys.exit(main())
