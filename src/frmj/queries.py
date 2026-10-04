"""
Local-ledger queries shared by the CLI (and, eventually, other front ends).

Plain functions over an open SQLite connection: each runs one query (or a
small, fixed group of them) against the tables defined in
``persistence/schema.py`` and returns plain data. Nothing here prompts,
prints, or exits; validation messages and output formatting stay in the
``frmj.cli`` package.

``accounts.py`` is the equivalent for account profiles and their config;
``services.py`` holds the multi-step flows that also call Oanda. This module
covers everything else the CLI reads from or writes to the ledger:
transactions, notes, tags, trade plans, and financing snapshots.

Oanda transaction IDs are only unique within one account, so functions that
look a transaction up by its Oanda ID take the account's Oanda ID too —
``None`` where noted means "any account".
"""

from __future__ import annotations

import sqlite3

# ---------------------------------------------------------------------------
# Transaction lookup
# ---------------------------------------------------------------------------


def find_transaction_ids(
    conn: sqlite3.Connection, oanda_id: str, account_id: str | None
) -> list[int]:
    """
    Return the local ``transactions.id`` of every row with *oanda_id*.

    Scoped to *account_id* when given; with ``None`` every account is
    searched, so the result can hold several IDs (one per account that has
    a transaction with that Oanda ID). Callers decide what to do with zero
    or several matches.
    """
    if account_id is not None:
        rows = conn.execute(
            "SELECT id FROM transactions WHERE oanda_id = ? AND account_id = ?",
            (oanda_id, account_id),
        ).fetchall()
    else:
        rows = conn.execute(
            "SELECT id FROM transactions WHERE oanda_id = ?", (oanda_id,)
        ).fetchall()
    return [int(row[0]) for row in rows]


def get_transaction_id(
    conn: sqlite3.Connection, oanda_id: str, account_id: str
) -> int | None:
    """Return the local ``transactions.id`` for *oanda_id* in *account_id*,
    or ``None`` if it hasn't been synced yet."""
    ids = find_transaction_ids(conn, oanda_id, account_id)
    return ids[0] if ids else None


# ---------------------------------------------------------------------------
# Notes and tags
# ---------------------------------------------------------------------------


def add_note(conn: sqlite3.Connection, transaction_id: int, body: str) -> None:
    """Attach a note with *body* to the local transaction *transaction_id*."""
    conn.execute(
        "INSERT INTO notes (transaction_id, body) VALUES (?, ?)",
        (transaction_id, body),
    )
    conn.commit()


def add_tags(conn: sqlite3.Connection, transaction_id: int, tags: list[str]) -> int:
    """
    Attach each of *tags* (already validated and normalised) to the local
    transaction *transaction_id*.

    Returns how many were actually inserted: a tag the transaction already
    has is skipped (``INSERT OR IGNORE``), so re-tagging is harmless. A
    database error on one tag is swallowed and that tag skipped, so one bad
    insert can't lose the others.
    """
    attached = 0
    for tag in tags:
        try:
            conn.execute(
                "INSERT OR IGNORE INTO tags (transaction_id, tag) VALUES (?, ?)",
                (transaction_id, tag),
            )
            # changes() is 0 when the tag was already there and was ignored.
            if conn.execute("SELECT changes()").fetchone()[0]:
                attached += 1
        except Exception:
            pass
    conn.commit()
    return attached


# ---------------------------------------------------------------------------
# Completion sources
# ---------------------------------------------------------------------------


def list_oanda_ids_with_prefix(
    conn: sqlite3.Connection, prefix: str, account_id: str | None, limit: int = 50
) -> list[str]:
    """
    Return up to *limit* distinct Oanda transaction IDs starting with
    *prefix*, newest (highest ID) first.

    Scoped to *account_id* when given. Filtered in SQL rather than in
    Python because the transactions table can grow large.
    """
    account_sql = "AND account_id = ? " if account_id is not None else ""
    params: list[str | int] = [f"{prefix}%"]
    if account_id is not None:
        params.append(account_id)
    params.append(limit)
    rows = conn.execute(
        "SELECT DISTINCT oanda_id FROM transactions WHERE oanda_id LIKE ? "
        f"{account_sql}ORDER BY CAST(oanda_id AS INTEGER) DESC LIMIT ?",
        params,
    ).fetchall()
    return [row[0] for row in rows]


def list_tags(conn: sqlite3.Connection) -> list[str]:
    """Return every distinct tag in use on any transaction, alphabetically."""
    rows = conn.execute("SELECT DISTINCT tag FROM tags ORDER BY tag").fetchall()
    return [row[0] for row in rows]


def list_transaction_types(conn: sqlite3.Connection) -> list[str]:
    """Return every distinct transaction type in the ledger, alphabetically."""
    rows = conn.execute(
        "SELECT DISTINCT type FROM transactions ORDER BY type"
    ).fetchall()
    return [row[0] for row in rows]
