"""
SQLite storage for the healthcare front-desk agent.

A single file-based DB (`clinic.db`) is shared by two processes:
  - the LiveKit agent worker (agent.py), which writes appointments + summaries
  - the FastAPI server (server.py), which reads them for the UI

WAL mode is enabled so the worker and the API can read/write concurrently
without locking each other out.
"""

import os
import sqlite3
from pathlib import Path

DB_PATH = os.getenv("CLINIC_DB_PATH", str(Path(__file__).resolve().parent.parent / "clinic.db"))

_SCHEMA = """
CREATE TABLE IF NOT EXISTS users (
    phone       TEXT PRIMARY KEY,
    name        TEXT,
    created_at  TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS appointments (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    phone       TEXT NOT NULL,
    name        TEXT,
    date        TEXT NOT NULL,            -- ISO date, e.g. 2026-06-23
    time        TEXT NOT NULL,            -- e.g. 10:00 AM
    reason      TEXT,                     -- intent / reason for visit
    status      TEXT NOT NULL DEFAULT 'booked',  -- booked | cancelled
    created_at  TEXT NOT NULL DEFAULT (datetime('now')),
    updated_at  TEXT NOT NULL DEFAULT (datetime('now'))
);

-- A given (date, time) slot can only have one active booking (single front desk).
CREATE UNIQUE INDEX IF NOT EXISTS uniq_active_slot
    ON appointments(date, time)
    WHERE status = 'booked';

CREATE TABLE IF NOT EXISTS call_summaries (
    room        TEXT PRIMARY KEY,
    phone       TEXT,
    name        TEXT,
    summary     TEXT,
    intent      TEXT,
    preferences TEXT,
    extracted_date TEXT,             -- date the patient asked about (extracted)
    extracted_time TEXT,             -- time the patient asked about (extracted)
    appointments_json TEXT,
    created_at  TEXT NOT NULL DEFAULT (datetime('now'))
);
"""

# Columns added after the initial release — applied to pre-existing DBs.
_MIGRATIONS = {
    "call_summaries": {
        "extracted_date": "TEXT",
        "extracted_time": "TEXT",
    },
}


def connect() -> sqlite3.Connection:
    """Open a connection with sane defaults for concurrent access."""
    conn = sqlite3.connect(DB_PATH, timeout=10)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL;")
    conn.execute("PRAGMA busy_timeout=5000;")
    conn.execute("PRAGMA foreign_keys=ON;")
    return conn


def init_db() -> None:
    """Create tables/indexes if they do not exist, then apply column migrations.
    Safe to call repeatedly."""
    conn = connect()
    try:
        conn.executescript(_SCHEMA)
        for table, columns in _MIGRATIONS.items():
            existing = {row["name"] for row in conn.execute(f"PRAGMA table_info({table})")}
            for col, coltype in columns.items():
                if col not in existing:
                    conn.execute(f"ALTER TABLE {table} ADD COLUMN {col} {coltype}")
        conn.commit()
    finally:
        conn.close()
