"""SQLite schema. See SUBMISSION.md for the rationale."""
from __future__ import annotations

import sqlite3

from .pii import PiiVault

DDL = """
CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT NOT NULL);

-- One row per source file ever ingested. Makes ingest idempotent and carries
-- the per-file counters that feed data_quality.json.
CREATE TABLE IF NOT EXISTS ingested_files (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,   -- ingestion order
    drop_name     TEXT NOT NULL,
    relpath       TEXT NOT NULL,
    kind          TEXT NOT NULL CHECK (kind IN ('leads','events')),
    sha256        TEXT NOT NULL,
    rows_total    INTEGER NOT NULL DEFAULT 0,   -- non-blank rows / non-empty lines
    rows_rejected INTEGER NOT NULL DEFAULT 0,   -- leads only
    rows_merged   INTEGER NOT NULL DEFAULT 0,   -- leads only
    field_warnings INTEGER NOT NULL DEFAULT 0,  -- leads only
    malformed     INTEGER NOT NULL DEFAULT 0,   -- events only
    duplicates    INTEGER NOT NULL DEFAULT 0,   -- events only
    UNIQUE (drop_name, relpath)
);

-- Current state of each lead. Identity = (workflow_id, phone_hash).
CREATE TABLE IF NOT EXISTS leads (
    id          INTEGER PRIMARY KEY,
    workflow_id TEXT NOT NULL,
    phone_hash  BLOB NOT NULL,          -- HMAC-SHA256(canonical phone)
    phone_last4 TEXT NOT NULL,          -- needed for the masked report column
    batch_id    TEXT NOT NULL,          -- file stem of the first valid row; never changes
    external_ref TEXT NOT NULL,         -- schema field marked "reference"
    attrs       TEXT NOT NULL,          -- JSON of the other non-PII fields
    pii_enc     BLOB NOT NULL,          -- Fernet token: name, full phone, ...
    UNIQUE (workflow_id, phone_hash)
);

CREATE TABLE IF NOT EXISTS rejections (
    file_id     INTEGER NOT NULL REFERENCES ingested_files(id),
    workflow_id TEXT NOT NULL,
    batch_id    TEXT NOT NULL,
    row_number  INTEGER NOT NULL,
    reason      TEXT NOT NULL,
    PRIMARY KEY (file_id, row_number)
);

CREATE TABLE IF NOT EXISTS dispositions (
    workflow_id TEXT NOT NULL, code TEXT NOT NULL,
    is_terminal INTEGER NOT NULL, is_success INTEGER NOT NULL,
    PRIMARY KEY (workflow_id, code)
);

-- The interaction ledger: append-only. Every well-formed, first-seen event is
-- stored exactly as received (minus raw PII), whether or not it links to a
-- lead *yet*. Linking is derived at read time, so an event that arrives before
-- its lead resolves by itself when the lead shows up.
CREATE TABLE IF NOT EXISTS events (
    seq              INTEGER PRIMARY KEY AUTOINCREMENT,
    event_id         TEXT NOT NULL UNIQUE,
    event_type       TEXT NOT NULL,
    workflow_id      TEXT,
    call_id          TEXT,
    occurred_at_us   INTEGER NOT NULL,   -- UTC microseconds since epoch
    phone_hash       BLOB,               -- NULL when phone missing/invalid
    disposition_code TEXT,               -- extracted from payload for disposition.*
    payload          TEXT NOT NULL,      -- JSON, as received
    file_id          INTEGER NOT NULL REFERENCES ingested_files(id)
);
CREATE INDEX IF NOT EXISTS ix_events_link ON events (workflow_id, phone_hash);
CREATE INDEX IF NOT EXISTS ix_events_call ON events (call_id);

CREATE TRIGGER IF NOT EXISTS events_no_update BEFORE UPDATE ON events
BEGIN SELECT RAISE(ABORT, 'events ledger is append-only'); END;
CREATE TRIGGER IF NOT EXISTS events_no_delete BEFORE DELETE ON events
BEGIN SELECT RAISE(ABORT, 'events ledger is append-only'); END;

-- Quarantine = events that currently link to no lead (phone events only; an
-- override is judged by report logic through its call_id).
CREATE VIEW IF NOT EXISTS v_unlinked_events AS
SELECT e.* FROM events e
LEFT JOIN leads l ON l.workflow_id = e.workflow_id AND l.phone_hash = e.phone_hash
WHERE l.id IS NULL;
"""


def connect(path: str) -> sqlite3.Connection:
    conn = sqlite3.connect(path, isolation_level=None)  # explicit BEGIN/COMMIT
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA synchronous=NORMAL")
    conn.execute("PRAGMA foreign_keys=ON")
    conn.execute("PRAGMA cache_size=-200000")
    conn.executescript(DDL)
    return conn


def check_key(conn: sqlite3.Connection, vault: PiiVault) -> None:
    """Refuse to run with a different PII key than the one that built the DB."""
    row = conn.execute("SELECT value FROM meta WHERE key='key_fingerprint'").fetchone()
    if row is None:
        conn.execute("INSERT INTO meta VALUES ('key_fingerprint', ?)", (vault.fingerprint,))
    elif row[0] != vault.fingerprint:
        raise SystemExit("PII key does not match this database (wrong LEDGER_PII_KEY / .key file).")


def sync_dispositions(conn: sqlite3.Connection, rows) -> None:
    conn.execute("BEGIN")
    conn.executemany("INSERT OR REPLACE INTO dispositions VALUES (?,?,?,?)", rows)
    conn.execute("COMMIT")
