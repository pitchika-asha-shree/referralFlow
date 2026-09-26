"""SQLite storage. Swapping to Postgres only touches this file and repository.py."""
from __future__ import annotations

import sqlite3
from pathlib import Path

SCHEMA = """
CREATE TABLE IF NOT EXISTS referrals (
    id               TEXT PRIMARY KEY,
    client_id        TEXT NOT NULL,
    filename         TEXT NOT NULL,
    document_sha256  TEXT NOT NULL,
    status           TEXT NOT NULL,
    text_method      TEXT,
    raw_text         TEXT NOT NULL DEFAULT '',
    fields_json      TEXT NOT NULL DEFAULT '{}',
    issues_json      TEXT NOT NULL DEFAULT '[]',
    queue            TEXT,
    priority         TEXT,
    routing_rule     TEXT,
    created_at       TEXT NOT NULL,
    updated_at       TEXT NOT NULL,
    UNIQUE (client_id, document_sha256)          -- idempotent ingestion
);
CREATE INDEX IF NOT EXISTS ix_referrals_status ON referrals (status);
CREATE INDEX IF NOT EXISTS ix_referrals_client ON referrals (client_id, created_at);

CREATE TABLE IF NOT EXISTS referral_events (       -- append-only audit trail
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    referral_id  TEXT NOT NULL REFERENCES referrals(id),
    at           TEXT NOT NULL,
    from_status  TEXT,
    to_status    TEXT,
    actor        TEXT NOT NULL,
    note         TEXT NOT NULL DEFAULT ''
);
CREATE INDEX IF NOT EXISTS ix_events_referral ON referral_events (referral_id, id);

CREATE TABLE IF NOT EXISTS tickets (
    id               TEXT PRIMARY KEY,
    referral_id      TEXT NOT NULL REFERENCES referrals(id),
    client_id        TEXT NOT NULL,
    category         TEXT NOT NULL,
    summary          TEXT NOT NULL,
    details          TEXT NOT NULL DEFAULT '',
    status           TEXT NOT NULL,
    created_at       TEXT NOT NULL,
    resolved_at      TEXT,
    resolution_note  TEXT
);
CREATE INDEX IF NOT EXISTS ix_tickets_open ON tickets (status, referral_id, category);

CREATE TABLE IF NOT EXISTS deliveries (            -- transactional outbox for webhooks
    id                TEXT PRIMARY KEY,
    referral_id       TEXT NOT NULL REFERENCES referrals(id),
    client_id         TEXT NOT NULL,
    url               TEXT NOT NULL,
    payload_json      TEXT NOT NULL,
    status            TEXT NOT NULL,
    attempts          INTEGER NOT NULL DEFAULT 0,
    max_attempts      INTEGER NOT NULL DEFAULT 5,
    next_attempt_at   TEXT NOT NULL,
    last_status_code  INTEGER,
    last_error        TEXT,
    created_at        TEXT NOT NULL,
    updated_at        TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS ix_deliveries_due ON deliveries (status, next_attempt_at);
"""


def connect(path: str | Path) -> sqlite3.Connection:
    """Open a connection with sane defaults. Use ':memory:' in tests."""
    if str(path) != ":memory:":
        Path(path).parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(path), check_same_thread=False, timeout=10)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    if str(path) != ":memory:":
        conn.execute("PRAGMA journal_mode = WAL")  # readers don't block the retry worker
    conn.executescript(SCHEMA)
    return conn
