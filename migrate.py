#!/usr/bin/env python3
"""
Database migration & management script for PED Tools.

Usage:
    python migrate.py init                  # Create tables (safe to re-run)
    python migrate.py status                # Show DB info & table counts
    python migrate.py import-json <file>    # Import proxy_server.json into DB
    python migrate.py import-db <file>      # Import from another pedapp.db
    python migrate.py export-json [file]    # Export all proxies to JSON (default: export.json)
    python migrate.py backup [file]         # Copy DB to backup file
    python migrate.py reset                 # Drop all data (keeps tables)
    python migrate.py reset --hard          # Drop and recreate all tables
    python migrate.py history-cleanup [days]# Delete history older than N days (default: 30)
"""

from __future__ import annotations

import json
import os
import shutil
import sqlite3
import sys
from datetime import datetime, timedelta

from dotenv import load_dotenv

_BASE_DIR = os.path.dirname(os.path.abspath(__file__))
load_dotenv(os.path.join(_BASE_DIR, ".env"))

DB_PATH = os.environ.get("PED_DB_PATH", os.path.join(_BASE_DIR, "pedapp.db"))


# ---------------------------------------------------------------------------
# Schema
# ---------------------------------------------------------------------------

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


def get_conn(path: str = DB_PATH) -> sqlite3.Connection:
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    return conn


# ---------------------------------------------------------------------------
# Commands
# ---------------------------------------------------------------------------


def cmd_init():
    """Create all tables."""
    conn = get_conn()
    conn.executescript(SCHEMA)
    conn.commit()
    conn.close()
    print(f"✓ Database initialized at {DB_PATH}")


def cmd_status():
    """Show DB info and row counts."""
    if not os.path.exists(DB_PATH):
        print(f"✗ Database not found at {DB_PATH}")
        return

    size_kb = os.path.getsize(DB_PATH) / 1024
    conn = get_conn()

    tables = conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name != 'sqlite_sequence'"
    ).fetchall()

    print(f"Database: {DB_PATH} ({size_kb:.1f} KB)")
    print(f"Tables: {len(tables)}")
    print()

    for t in tables:
        name = t["name"]
        count = conn.execute(f"SELECT COUNT(*) as c FROM [{name}]").fetchone()["c"]
        print(f"  {name:25s} {count:>6d} rows")

    # Show proxy summary
    proxies = conn.execute(
        """
        SELECT p.identifier, p.api_domain, COUNT(m.id) as mock_count
        FROM proxies p
        LEFT JOIN mocks m ON m.proxy_id = p.identifier
        GROUP BY p.identifier
        ORDER BY p.identifier
        """
    ).fetchall()

    if proxies:
        print(f"\nProxies ({len(proxies)}):")
        for p in proxies:
            print(f"  {p['identifier']:30s} -> {p['api_domain']:50s} ({p['mock_count']} mocks)")

    conn.close()


def cmd_import_json(json_path: str):
    """Import from proxy_server.json format."""
    if not os.path.exists(json_path):
        print(f"✗ File not found: {json_path}")
        sys.exit(1)

    with open(json_path, "r") as f:
        data = json.load(f)

    if not data:
        print("✗ JSON file is empty")
        return

    conn = get_conn()
    conn.executescript(SCHEMA)

    count_proxies = 0
    count_mocks = 0
    skipped_proxies = 0

    for identifier, entry in data.items():
        api_domain = entry.get("api_domain", "")

        # Check if proxy already exists
        existing = conn.execute(
            "SELECT 1 FROM proxies WHERE identifier = ?", (identifier,)
        ).fetchone()

        if existing:
            print(f"  ⚠ Proxy '{identifier}' already exists, merging mocks...")
            skipped_proxies += 1
        else:
            conn.execute(
                "INSERT INTO proxies (identifier, api_domain) VALUES (?, ?)",
                (identifier, api_domain),
            )
            count_proxies += 1

        for endpoint, methods in entry.get("mocked_requests", {}).items():
            for method, response in methods.items():
                conn.execute(
                    "INSERT INTO mocks (proxy_id, endpoint, method, response) "
                    "VALUES (?, ?, ?, ?) "
                    "ON CONFLICT(proxy_id, endpoint, method) DO UPDATE SET "
                    "response = excluded.response, updated_at = datetime('now')",
                    (identifier, endpoint, method, json.dumps(response)),
                )
                count_mocks += 1

    conn.commit()
    conn.close()

    print(f"✓ Imported {count_proxies} new proxies, {count_mocks} mocks "
          f"({skipped_proxies} existing proxies merged)")


def cmd_import_db(source_path: str):
    """Import from another pedapp.db file."""
    if not os.path.exists(source_path):
        print(f"✗ File not found: {source_path}")
        sys.exit(1)

    source = get_conn(source_path)
    target = get_conn()
    target.executescript(SCHEMA)

    # Import proxies
    proxies = source.execute("SELECT identifier, api_domain FROM proxies").fetchall()
    count_proxies = 0
    count_mocks = 0

    for p in proxies:
        target.execute(
            "INSERT INTO proxies (identifier, api_domain) VALUES (?, ?) "
            "ON CONFLICT(identifier) DO UPDATE SET api_domain = excluded.api_domain",
            (p["identifier"], p["api_domain"]),
        )
        count_proxies += 1

        # Import mocks for this proxy
        mocks = source.execute(
            "SELECT endpoint, method, response FROM mocks WHERE proxy_id = ?",
            (p["identifier"],),
        ).fetchall()

        for m in mocks:
            target.execute(
                "INSERT INTO mocks (proxy_id, endpoint, method, response) "
                "VALUES (?, ?, ?, ?) "
                "ON CONFLICT(proxy_id, endpoint, method) DO UPDATE SET "
                "response = excluded.response, updated_at = datetime('now')",
                (p["identifier"], m["endpoint"], m["method"], m["response"]),
            )
            count_mocks += 1

    target.commit()
    source.close()
    target.close()

    print(f"✓ Imported {count_proxies} proxies and {count_mocks} mocks from {source_path}")


def cmd_export_json(output_path: str = "export.json"):
    """Export all proxies to JSON."""
    conn = get_conn()
    proxies = conn.execute("SELECT identifier, api_domain FROM proxies").fetchall()

    result = {}
    for p in proxies:
        mocks = conn.execute(
            "SELECT endpoint, method, response FROM mocks WHERE proxy_id = ?",
            (p["identifier"],),
        ).fetchall()

        mocked_requests = {}
        for m in mocks:
            mocked_requests.setdefault(m["endpoint"], {})[m["method"]] = json.loads(m["response"])

        result[p["identifier"]] = {
            "api_domain": p["api_domain"],
            "mocked_requests": mocked_requests,
        }

    conn.close()

    with open(output_path, "w") as f:
        json.dump(result, f, indent=2)

    print(f"✓ Exported {len(result)} proxies to {output_path}")


def cmd_backup(backup_path: str | None = None):
    """Copy DB to a backup file."""
    if not os.path.exists(DB_PATH):
        print(f"✗ Database not found at {DB_PATH}")
        sys.exit(1)

    if not backup_path:
        ts = datetime.now().strftime("%Y%m%d_%H%M%S")
        backup_path = f"pedapp_backup_{ts}.db"

    shutil.copy2(DB_PATH, backup_path)
    size_kb = os.path.getsize(backup_path) / 1024
    print(f"✓ Backed up to {backup_path} ({size_kb:.1f} KB)")


def cmd_reset(hard: bool = False):
    """Reset all data."""
    if not os.path.exists(DB_PATH):
        print(f"✗ Database not found at {DB_PATH}")
        sys.exit(1)

    confirm = input(f"{'DROP and recreate' if hard else 'Delete all data from'} "
                    f"{DB_PATH}? (yes/no): ")
    if confirm.lower() != "yes":
        print("Cancelled.")
        return

    conn = get_conn()

    if hard:
        conn.executescript("""
            DROP TABLE IF EXISTS mock_sequences;
            DROP TABLE IF EXISTS request_history;
            DROP TABLE IF EXISTS mocks;
            DROP TABLE IF EXISTS proxies;
        """)
        conn.executescript(SCHEMA)
        print("✓ All tables dropped and recreated")
    else:
        conn.execute("DELETE FROM mock_sequences")
        conn.execute("DELETE FROM request_history")
        conn.execute("DELETE FROM mocks")
        conn.execute("DELETE FROM proxies")
        conn.commit()
        print("✓ All data deleted (tables preserved)")

    conn.close()


def cmd_history_cleanup(days: int = 30):
    """Delete request history older than N days."""
    conn = get_conn()
    cutoff = (datetime.utcnow() - timedelta(days=days)).isoformat()
    cursor = conn.execute(
        "DELETE FROM request_history WHERE created_at < ?", (cutoff,)
    )
    conn.commit()
    conn.close()
    print(f"✓ Deleted {cursor.rowcount} history entries older than {days} days")


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def main():
    if len(sys.argv) < 2:
        print(__doc__)
        sys.exit(0)

    command = sys.argv[1]

    if command == "init":
        cmd_init()
    elif command == "status":
        cmd_status()
    elif command == "import-json":
        if len(sys.argv) < 3:
            print("Usage: python migrate.py import-json <file.json>")
            sys.exit(1)
        cmd_import_json(sys.argv[2])
    elif command == "import-db":
        if len(sys.argv) < 3:
            print("Usage: python migrate.py import-db <source.db>")
            sys.exit(1)
        cmd_import_db(sys.argv[2])
    elif command == "export-json":
        output = sys.argv[2] if len(sys.argv) > 2 else "export.json"
        cmd_export_json(output)
    elif command == "backup":
        backup = sys.argv[2] if len(sys.argv) > 2 else None
        cmd_backup(backup)
    elif command == "reset":
        hard = "--hard" in sys.argv
        cmd_reset(hard)
    elif command == "history-cleanup":
        days = int(sys.argv[2]) if len(sys.argv) > 2 else 30
        cmd_history_cleanup(days)
    else:
        print(f"Unknown command: {command}")
        print(__doc__)
        sys.exit(1)


if __name__ == "__main__":
    main()
