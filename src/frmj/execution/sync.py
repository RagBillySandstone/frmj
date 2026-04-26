"""
Sync logic: ingest Oanda transaction rows into the local SQLite database.

This module is the bridge between the Oanda HTTP client (``oanda.py``) and
the persistence schema (``persistence/schema.py``).  It is responsible for:

  * Pagination-free ingestion — the client already handles pagination and
    returns a flat list of ``TransactionRow`` objects.
  * Deduplication — the unique index on ``(account_id, oanda_id)`` catches
    duplicate rows if a re-sync overlaps with a previous window.  We catch
    ``sqlite3.IntegrityError`` and count skipped rows rather than aborting.
  * Parent/child FK resolution — rows where ``parent_oanda_id`` is set must
    be inserted after their parent so the SQLite FK constraint is satisfied.
    We separate each batch into parent-first, children-second before writing.
  * Cursor management — after a successful ingest we write (or advance) the
    ``sync_cursors`` row so the next incremental sync knows where to resume.

Two public entry points
-----------------------
``sync_cold(conn, client)``
    Fetches the full account history (no cursor required).  Safe to call on a
    database that already has rows — duplicates are silently skipped.

``sync_incremental(conn, client)``
    Reads the cursor for ``client.account_id`` and fetches only transactions
    after the last ingested ID.  Delegates to ``sync_cold`` automatically when
    no cursor exists (first run).

Design notes
------------
* Both functions accept ``conn`` with FK enforcement already on (set by
  ``ensure_schema``).  Callers must not disable it between calls.
* We commit once per batch (all rows from one ``get_transactions_since``
  call), not row-by-row.  A partial failure (e.g. network error mid-page)
  therefore leaves the database in a consistent state: either the whole batch
  landed or none of it did.  The next sync run will re-fetch and skip
  duplicates via the unique index.
* The sync layer deliberately has no I/O beyond SQLite — it does not read
  env vars, print, or log.  The CLI layer handles all user interaction.
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass

from frmj.execution.oanda import ClientProtocol, TransactionRow


# ---------------------------------------------------------------------------
# Output type
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class SyncResult:
    """
    Summary of one sync run.

    ``rows_ingested``:  rows successfully written to ``transactions``.
    ``rows_skipped``:   rows already present (duplicate unique key); these
                        are silently skipped, not treated as errors.
    ``last_oanda_id``:  the Oanda ID of the last row in the batch, or
                        ``None`` if no rows were returned at all (account
                        is empty, or incremental sync found nothing new).
    """

    rows_ingested: int
    rows_skipped: int
    last_oanda_id: str | None


# ---------------------------------------------------------------------------
# Cursor helpers
# ---------------------------------------------------------------------------


def _read_cursor(conn: sqlite3.Connection, account_id: str) -> str | None:
    """
    Return the ``last_oanda_id`` from ``sync_cursors`` for *account_id*, or
    ``None`` if no cursor row exists yet (first run).
    """
    row = conn.execute(
        "SELECT last_oanda_id FROM sync_cursors WHERE account_id = ?",
        (account_id,),
    ).fetchone()
    return row[0] if row else None


def _write_cursor(
    conn: sqlite3.Connection,
    account_id: str,
    last_oanda_id: str,
) -> None:
    """
    Upsert the sync cursor for *account_id* to *last_oanda_id*.

    REPLACE INTO is safe here because ``account_id`` is the PRIMARY KEY:
    SQLite atomically deletes the old row (if any) and inserts the new one.
    The ``synced_at`` timestamp is set to the database's current wall clock,
    which is adequate for display purposes.
    """
    conn.execute(
        """
        REPLACE INTO sync_cursors (account_id, last_oanda_id, synced_at)
        VALUES (?, ?, strftime('%Y-%m-%dT%H:%M:%fZ', 'now'))
        """,
        (account_id, last_oanda_id),
    )
    conn.commit()


# ---------------------------------------------------------------------------
# Row ingestion
# ---------------------------------------------------------------------------


def _resolve_parent_id(
    conn: sqlite3.Connection,
    account_id: str,
    parent_oanda_id: str,
    page_index: dict[str, int],
) -> int | None:
    """
    Look up the synthetic SQLite ``id`` for a parent row, returning ``None``
    if the parent cannot be found.

    Search order:
    1. ``page_index`` — rows inserted earlier in the *current* batch. This
       handles within-page parent/child pairs without a round-trip to SQLite.
    2. The ``transactions`` table — handles cross-batch parents (parent arrived
       in an earlier sync run or an earlier page).

    A ``None`` return means the parent genuinely doesn't exist in the database.
    This should not happen with correct Oanda data (the parent always arrives
    before its children), but we degrade gracefully by inserting the child with
    a NULL ``parent_id`` rather than aborting the whole batch.
    """
    if parent_oanda_id in page_index:
        return page_index[parent_oanda_id]
    row = conn.execute(
        "SELECT id FROM transactions WHERE account_id = ? AND oanda_id = ?",
        (account_id, parent_oanda_id),
    ).fetchone()
    return row[0] if row else None


def _ingest_rows(
    conn: sqlite3.Connection,
    rows: list[TransactionRow],
) -> tuple[int, int]:
    """
    Insert *rows* into ``transactions``, returning ``(ingested, skipped)``.

    Insertion order within the batch:
    1. Rows with ``parent_oanda_id is None`` first (the vast majority).
    2. Rows with a ``parent_oanda_id`` second, so the FK can be resolved.

    Within each group the original list order is preserved, which matches
    Oanda's delivery order (chronological, parent before children).

    Duplicate rows (same ``account_id`` + ``oanda_id``) are skipped via the
    unique index rather than raising — this makes re-sync idempotent.

    We do NOT commit inside this function.  The caller commits once the whole
    batch is written, giving atomic batch semantics.
    """
    # Separate into parents-first order without mutating the original list.
    parents = [r for r in rows if r.parent_oanda_id is None]
    children = [r for r in rows if r.parent_oanda_id is not None]

    # Maps oanda_id → synthetic SQLite id for rows inserted in this batch.
    # Used by child rows to resolve parent_id without an extra SELECT.
    page_index: dict[str, int] = {}

    ingested = 0
    skipped = 0

    for row in parents + children:
        # Resolve parent FK (None for the vast majority of rows).
        parent_id: int | None = None
        if row.parent_oanda_id is not None:
            parent_id = _resolve_parent_id(
                conn, row.account_id, row.parent_oanda_id, page_index
            )

        try:
            cur = conn.execute(
                """
                INSERT INTO transactions
                    (oanda_id, account_id, type, time, parent_id, raw_json)
                VALUES (?, ?, ?, ?, ?, ?)
                """,
                (
                    row.oanda_id,
                    row.account_id,
                    row.type,
                    row.time,
                    parent_id,
                    row.raw_json,
                ),
            )
            # Record the synthetic id so child rows in this batch can find it.
            assert cur.lastrowid is not None
            page_index[row.oanda_id] = cur.lastrowid
            ingested += 1
        except sqlite3.IntegrityError:
            # Unique constraint violation — row already in the database from a
            # previous sync run.  Skip silently; don't update page_index (the
            # existing row's synthetic id is already in the DB if needed).
            skipped += 1

    return ingested, skipped


# ---------------------------------------------------------------------------
# Public entry points
# ---------------------------------------------------------------------------


def sync_cold(
    conn: sqlite3.Connection,
    client: ClientProtocol,
) -> SyncResult:
    """
    Fetch the full transaction history for ``client.account_id`` and ingest it.

    Safe to call on a database that already has rows — duplicates are silently
    skipped via the unique index.  This means re-running a cold sync after a
    partial failure is always safe: the successful rows from the first attempt
    are skipped and the new rows are inserted.

    Cursor behaviour:
    * If rows are returned, the cursor is written (or overwritten) with the
      Oanda ID of the last row in the batch.
    * If no rows are returned (empty account), no cursor is written.

    Raises whatever ``client.get_transactions_since`` raises on network errors.
    """
    rows = client.get_transactions_since(from_id=None)

    ingested, skipped = _ingest_rows(conn, rows)
    conn.commit()

    last_oanda_id: str | None = rows[-1].oanda_id if rows else None
    if last_oanda_id is not None:
        _write_cursor(conn, client.account_id, last_oanda_id)

    return SyncResult(
        rows_ingested=ingested,
        rows_skipped=skipped,
        last_oanda_id=last_oanda_id,
    )


def sync_incremental(
    conn: sqlite3.Connection,
    client: ClientProtocol,
) -> SyncResult:
    """
    Fetch only transactions after the last-known cursor and ingest them.

    If no cursor exists for ``client.account_id`` (first run), delegates to
    ``sync_cold`` to fetch the full history.

    If the cursor exists but no new transactions arrive (the account is quiet),
    returns a ``SyncResult`` with zero ingested/skipped and the cursor unchanged.

    Cursor behaviour:
    * Advances to the Oanda ID of the last row in the new batch.
    * Unchanged if no new rows were returned.

    Raises whatever ``client.get_transactions_since`` raises on network errors.
    """
    cursor = _read_cursor(conn, client.account_id)

    if cursor is None:
        # First run — no cursor yet; do a full cold sync.
        return sync_cold(conn, client)

    rows = client.get_transactions_since(from_id=cursor)

    if not rows:
        # Nothing new since last sync.
        return SyncResult(rows_ingested=0, rows_skipped=0, last_oanda_id=cursor)

    ingested, skipped = _ingest_rows(conn, rows)
    conn.commit()

    last_oanda_id = rows[-1].oanda_id
    _write_cursor(conn, client.account_id, last_oanda_id)

    return SyncResult(
        rows_ingested=ingested,
        rows_skipped=skipped,
        last_oanda_id=last_oanda_id,
    )
