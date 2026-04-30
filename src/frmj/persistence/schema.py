"""
SQLite persistence schema for FRoMaJ.

Design principles:

* **Immutable event log.** Oanda is the system of record. We never UPDATE or
  DELETE rows in ``transactions``; the table is append-only. If Oanda corrects
  a transaction it sends a new corrective event, which we ingest as a fresh
  row just like any other.

* **Synthetic primary key.** Oanda's own transaction IDs have gaps, can arrive
  out of order during a cold sync, and for DAILY_FINANCING events the parent
  row arrives before its children. Using our own INTEGER rowid PK means we can
  model the parent→child FK relationship with a proper column reference rather
  than a dangling string.

* **Raw JSON preserved verbatim.** ``raw_json`` stores the full Oanda payload
  alongside the parsed index columns. This lets us add new parsed columns in
  future migrations without re-fetching from Oanda, and makes incident
  investigation straightforward.

* **Financing parent/child linking.** Oanda emits a DAILY_FINANCING parent row
  whose child rows follow it in the same transaction stream. Each child carries
  a reference to its parent via ``parent_id`` (our synthetic PK, not the Oanda
  ID). Insertion order must be parent-before-children; the FK constraint
  enforces this at the database level when ``PRAGMA foreign_keys = ON``.

* **Single database, account_id column.** All Oanda accounts are stored in one
  SQLite file with an ``account_id`` discriminator column. Simpler to back up
  than multiple files; lets us do cross-account aggregates later if needed.

* **ISO-8601 timestamps as TEXT.** SQLite has no native timestamp type. RFC-3339
  strings (which Oanda already provides) sort correctly lexicographically, are
  easy to read in a debugger, and are trivially parsed in Python with
  ``datetime.fromisoformat``. The micro-optimisation of storing Unix
  milliseconds as INTEGER is not worth the loss in debuggability at this scale.

* **WAL journal mode.** Write-Ahead Logging allows concurrent readers during a
  write — relevant once the CLI and a background sync process might touch the
  same file. WAL also survives power-loss without corruption on the platforms
  we target (Linux, macOS). The pragma is a no-op on in-memory databases and
  harmless in that case.

* **Foreign keys enforced.** SQLite disables FK enforcement by default for
  backwards-compatibility reasons. ``ensure_schema`` always sets
  ``PRAGMA foreign_keys = ON`` so that referential integrity violations
  (e.g. a note that references a non-existent transaction) are caught
  immediately rather than silently stored.
"""

from __future__ import annotations

import sqlite3

# ---------------------------------------------------------------------------
# DDL: all CREATE TABLE / CREATE INDEX statements
# ---------------------------------------------------------------------------
# Kept as a single string executed via executescript so the ordering is
# explicit and obvious. CREATE IF NOT EXISTS on every statement means the
# whole block is safe to re-run on an already-initialised database.

_DDL: str = """
-- -------------------------------------------------------------------------
-- Core event ledger — append-only, never UPDATE or DELETE
-- -------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS transactions (
    -- Synthetic local PK. We never expose this to the user; it exists so
    -- the notes table and financing children can FK-reference a row without
    -- relying on Oanda's ID, which has gaps and ordering quirks.
    id          INTEGER PRIMARY KEY,

    -- Oanda's own transaction ID. Stored for deduplication on re-sync and
    -- for human-readable display, but NOT used as a PK or FK target.
    oanda_id    TEXT    NOT NULL,

    -- Which Oanda account this event belongs to. Supports multi-account in
    -- a single database file.
    account_id  TEXT    NOT NULL,

    -- Oanda's transaction type string, e.g. "ORDER_FILL", "DAILY_FINANCING",
    -- "STOP_LOSS_ORDER". Stored verbatim — we do not map to an Enum here
    -- because Oanda sometimes adds new types and we want to ingest them
    -- without a schema migration.
    type        TEXT    NOT NULL,

    -- ISO-8601 timestamp from Oanda's "time" field, stored verbatim (no
    -- conversion to UTC or epoch). RFC-3339 strings sort lexicographically
    -- so ORDER BY time gives correct chronological order.
    time        TEXT    NOT NULL,

    -- For DAILY_FINANCING children: the synthetic id of the parent row.
    -- NULL for all other transaction types. The FK constraint (enforced when
    -- foreign_keys = ON) requires the parent to already exist — insertion
    -- order must be parent before children.
    parent_id   INTEGER REFERENCES transactions(id),

    -- Full Oanda JSON payload, verbatim. Lets us add new parsed columns in
    -- future without re-fetching from the API.
    raw_json    TEXT    NOT NULL,

    -- Local wall-clock time of ingestion. Useful for auditing sync runs.
    created_at  TEXT    NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now'))
);

-- Deduplication guard: the same Oanda event must never appear twice for the
-- same account, even if we re-sync an overlapping time window.
CREATE UNIQUE INDEX IF NOT EXISTS idx_transactions_oanda_id
    ON transactions (account_id, oanda_id);

-- Primary access pattern: "show me all events for account X in time order."
CREATE INDEX IF NOT EXISTS idx_transactions_time
    ON transactions (account_id, time);

-- Secondary pattern: "show me all financing children of this parent row."
-- Partial index (WHERE parent_id IS NOT NULL) keeps it lean — the vast
-- majority of rows have no parent.
CREATE INDEX IF NOT EXISTS idx_transactions_parent
    ON transactions (parent_id)
    WHERE parent_id IS NOT NULL;


-- -------------------------------------------------------------------------
-- Mutable notes — attachable to any transaction
-- -------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS notes (
    id              INTEGER PRIMARY KEY,

    -- References transactions.id (our synthetic PK, not Oanda's ID).
    -- ON DELETE RESTRICT is the implicit SQLite default when foreign_keys=ON
    -- and no ON DELETE clause is specified, which is what we want: deleting
    -- a transaction row while notes reference it is an error we want to
    -- surface loudly (even though we don't actually delete transactions).
    transaction_id  INTEGER NOT NULL REFERENCES transactions(id),

    body            TEXT    NOT NULL,

    -- Both timestamps default to now at insert time. Callers that update a
    -- note body are responsible for also updating updated_at.
    created_at      TEXT    NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),
    updated_at      TEXT    NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now'))
);

-- "Show me all notes for this transaction" — the primary use-case.
CREATE INDEX IF NOT EXISTS idx_notes_transaction
    ON notes (transaction_id);


-- -------------------------------------------------------------------------
-- Sync cursors — one row per account, tracks incremental poll progress
-- -------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS sync_cursors (
    account_id      TEXT    PRIMARY KEY,

    -- The highest Oanda transaction ID we have successfully ingested for this
    -- account. The next incremental sync will ask Oanda for transactions with
    -- ID > last_oanda_id. Stored as TEXT because Oanda IDs are numeric strings
    -- and we never do arithmetic on them — lexicographic ordering is sufficient
    -- since they are monotonically increasing integers with no zero-padding.
    last_oanda_id   TEXT    NOT NULL,

    -- ISO-8601 wall-clock time of the last completed sync, for display only.
    synced_at       TEXT    NOT NULL
);


-- -------------------------------------------------------------------------
-- Trade plans — intended TP/SL captured at entry time
-- -------------------------------------------------------------------------
-- One row per ORDER_FILL.  Stores the prices the trader intended when they
-- placed the order so the journal and stats layer can compare intent with
-- outcome.  tp_price / sl_price are NULL when the user did not specify that
-- side.  Prices are stored as TEXT Decimal strings (same pattern as raw_json
-- field values) to preserve exact representation.
CREATE TABLE IF NOT EXISTS trade_plans (
    id              INTEGER PRIMARY KEY,
    transaction_id  INTEGER NOT NULL UNIQUE REFERENCES transactions(id),
    tp_price        TEXT,
    sl_price        TEXT,
    created_at      TEXT    NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now'))
);

-- "What was the plan for this fill?" — the only access pattern.
CREATE INDEX IF NOT EXISTS idx_trade_plans_transaction
    ON trade_plans (transaction_id);


-- -------------------------------------------------------------------------
-- Tags — short labels attachable to transactions for grouping in stats
-- -------------------------------------------------------------------------
-- One tag per row; a transaction can have many tags. Tags are stored
-- lowercase (normalised at write time by the CLI) so lookups are
-- case-insensitive without needing COLLATE NOCASE.
CREATE TABLE IF NOT EXISTS tags (
    id              INTEGER PRIMARY KEY,
    transaction_id  INTEGER NOT NULL REFERENCES transactions(id),
    tag             TEXT    NOT NULL,
    created_at      TEXT    NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now'))
);

-- Prevent the same tag being attached twice to the same transaction.
CREATE UNIQUE INDEX IF NOT EXISTS idx_tags_transaction_tag
    ON tags (transaction_id, tag);

-- "Show all tags for this transaction" — journal display.
CREATE INDEX IF NOT EXISTS idx_tags_transaction
    ON tags (transaction_id);

-- "Show all transactions with this tag" — stats / filter queries.
CREATE INDEX IF NOT EXISTS idx_tags_tag
    ON tags (tag);


-- -------------------------------------------------------------------------
-- Config — flat key/value store for account settings
-- -------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS config (
    -- Examples: "account_id", "practice_mode", "default_risk_fraction"
    key     TEXT    PRIMARY KEY,

    -- All values stored as TEXT; callers are responsible for serialising and
    -- deserialising (e.g. "true"/"false" for booleans, decimal strings for
    -- numbers). This avoids having to migrate the schema when we add new
    -- config keys with different types.
    value   TEXT    NOT NULL
);
"""


# ---------------------------------------------------------------------------
# Public initialisation function
# ---------------------------------------------------------------------------


def ensure_schema(conn: sqlite3.Connection) -> None:
    """
    Apply the FRoMaJ schema to *conn*, creating any missing tables and indexes.

    Safe to call on every application startup because every DDL statement uses
    CREATE IF NOT EXISTS — re-running against an already-initialised database
    is a complete no-op.

    Side effects beyond DDL:

    * ``PRAGMA journal_mode = WAL`` — enables Write-Ahead Logging for better
      read/write concurrency and crash safety. Returns "memory" on in-memory
      databases (expected and harmless).

    * ``PRAGMA foreign_keys = ON`` — activates FK enforcement for this
      connection. SQLite disables FKs by default for legacy reasons; we
      always want them on so that referential integrity violations are caught
      at insert time rather than silently accepted.

    ``conn`` must already be open and writable. The caller is responsible for
    closing it when done.
    """
    # Enable WAL before creating tables so that the DB file is in WAL mode
    # from its first write (relevant for new files; no-op for existing ones
    # already in WAL mode or for :memory: databases).
    conn.execute("PRAGMA journal_mode = WAL")

    # FK enforcement is connection-scoped, not persistent. Must be set on
    # every new connection — including this one.
    conn.execute("PRAGMA foreign_keys = ON")

    # executescript issues an implicit COMMIT before running, which is fine
    # here since DDL statements are auto-committed in SQLite anyway.
    conn.executescript(_DDL)
