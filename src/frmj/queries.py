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
from decimal import Decimal

from frmj.execution.oanda.models import FinancingRate

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


# ---------------------------------------------------------------------------
# Transaction listings (journal, export)
# ---------------------------------------------------------------------------


def _transaction_filter_sql(
    *,
    account_id: str | None = None,
    txn_type: str | None = None,
    since: str | None = None,
    instrument: str | None = None,
    with_notes: bool = False,
    tag: str | None = None,
) -> tuple[str, list[object]]:
    """
    Build the ``WHERE`` clause (possibly empty) and its parameters for a
    transactions listing. Every filter is optional and they combine with
    ``AND``.
    """
    where: list[str] = []
    params: list[object] = []

    # Plain column filters.
    if account_id is not None:
        where.append("account_id = ?")
        params.append(account_id)
    if txn_type:
        where.append("type = ?")
        params.append(txn_type)
    if since:
        where.append("time >= ?")
        params.append(since)

    # Instruments live in the raw JSON. json_extract needs SQLite >= 3.9
    # (2015). Uppercased so 'eur_usd' matches 'EUR_USD' as Oanda stores it.
    if instrument:
        where.append("json_extract(raw_json, '$.instrument') = ?")
        params.append(instrument.upper())

    # Annotation filters: transactions with any note, or with a given tag
    # (tags are stored lowercase).
    if with_notes:
        where.append("id IN (SELECT DISTINCT transaction_id FROM notes)")
    if tag:
        where.append("id IN (SELECT DISTINCT transaction_id FROM tags WHERE tag = ?)")
        params.append(tag.lower())

    where_sql = ("WHERE " + " AND ".join(where)) if where else ""
    return where_sql, params


def list_recent_transactions(
    conn: sqlite3.Connection,
    limit: int,
    *,
    account_id: str | None = None,
    txn_type: str | None = None,
    since: str | None = None,
    instrument: str | None = None,
    with_notes: bool = False,
    tag: str | None = None,
) -> list[sqlite3.Row]:
    """
    Return up to *limit* transactions matching the filters, newest first.

    Rows have ``id``, ``oanda_id``, ``account_id``, ``type``, ``time`` and
    ``raw_json``. Within one account (*account_id* given) they're ordered by
    Oanda ID, which is sequential per account. Across accounts IDs aren't
    comparable, so they're ordered by time, with the ID breaking ties
    between events stamped in the same instant.
    """
    where_sql, params = _transaction_filter_sql(
        account_id=account_id,
        txn_type=txn_type,
        since=since,
        instrument=instrument,
        with_notes=with_notes,
        tag=tag,
    )
    order_sql = (
        "CAST(oanda_id AS INTEGER) DESC"
        if account_id is not None
        else "time DESC, CAST(oanda_id AS INTEGER) DESC"
    )
    return conn.execute(
        f"""
        SELECT id, oanda_id, account_id, type, time, raw_json
        FROM transactions
        {where_sql}
        ORDER BY {order_sql}
        LIMIT ?
        """,
        [*params, limit],
    ).fetchall()


def list_transactions_chronological(
    conn: sqlite3.Connection,
    *,
    txn_type: str | None = None,
    since: str | None = None,
    instrument: str | None = None,
) -> list[sqlite3.Row]:
    """
    Return every transaction (all accounts) matching the filters, oldest
    first, with the same columns as ``list_recent_transactions``.
    """
    where_sql, params = _transaction_filter_sql(
        txn_type=txn_type, since=since, instrument=instrument
    )
    return conn.execute(
        f"""
        SELECT id, oanda_id, account_id, type, time, raw_json
        FROM transactions
        {where_sql}
        ORDER BY time ASC
        """,
        params,
    ).fetchall()


# ---------------------------------------------------------------------------
# Per-transaction annotations (journal, export)
# ---------------------------------------------------------------------------


def get_trade_plan(conn: sqlite3.Connection, transaction_id: int) -> sqlite3.Row | None:
    """
    Return the trade plan recorded for *transaction_id* (``tp_price``,
    ``sl_price``, ``trail_pips``, ``sl_atr_multiple``), or ``None`` if the
    transaction has no plan.
    """
    return conn.execute(
        "SELECT tp_price, sl_price, trail_pips, sl_atr_multiple "
        "FROM trade_plans "
        "WHERE transaction_id = ?",
        (transaction_id,),
    ).fetchone()


def list_notes(conn: sqlite3.Connection, transaction_id: int) -> list[str]:
    """Return the bodies of *transaction_id*'s notes, oldest first."""
    rows = conn.execute(
        "SELECT body FROM notes WHERE transaction_id = ? ORDER BY id",
        (transaction_id,),
    ).fetchall()
    return [row[0] for row in rows]


def list_transaction_tags(conn: sqlite3.Connection, transaction_id: int) -> list[str]:
    """Return *transaction_id*'s tags, alphabetically."""
    rows = conn.execute(
        "SELECT tag FROM tags WHERE transaction_id = ? ORDER BY tag",
        (transaction_id,),
    ).fetchall()
    return [row[0] for row in rows]


def notes_by_transaction(
    conn: sqlite3.Connection, transaction_ids: list[int]
) -> dict[int, list[str]]:
    """
    Return the note bodies for each of *transaction_ids* that has notes,
    keyed by transaction ID, each list oldest first. Transactions without
    notes are absent from the result.
    """
    if not transaction_ids:
        return {}
    placeholders = ",".join("?" * len(transaction_ids))
    result: dict[int, list[str]] = {}
    for row in conn.execute(
        f"SELECT transaction_id, body FROM notes "
        f"WHERE transaction_id IN ({placeholders}) ORDER BY id",
        transaction_ids,
    ).fetchall():
        result.setdefault(row[0], []).append(row[1])
    return result


# ---------------------------------------------------------------------------
# Statistics inputs (stats)
# ---------------------------------------------------------------------------


def list_fills_with_open_time(
    conn: sqlite3.Connection, account_id: str | None
) -> list[sqlite3.Row]:
    """
    Return every ORDER_FILL (``id``, ``oanda_id``, ``time``, ``raw_json``)
    plus ``open_time``: the time of the fill that opened the trade it
    closes, or ``None`` if that opening fill isn't in the ledger (or the
    fill opened a trade rather than closing one).

    Scoped to *account_id* when given, otherwise every account. Opening and
    closing fills are both returned; callers tell them apart by P/L.
    """
    account_sql = " AND t.account_id = ?" if account_id is not None else ""
    params = (account_id,) if account_id is not None else ()
    return conn.execute(
        """
        SELECT t.id, t.oanda_id, t.time, t.raw_json,
               open_t.time AS open_time
        FROM transactions t
        -- Resolve the opening fill so callers can bucket by open time.
        -- COALESCE covers both full closes (tradesClosed array) and
        -- partial reduces (tradeReduced object), each carrying tradeID.
        LEFT JOIN transactions open_t
            ON  open_t.account_id = t.account_id
            AND open_t.type       = 'ORDER_FILL'
            AND open_t.oanda_id   = COALESCE(
                    json_extract(t.raw_json, '$.tradesClosed[0].tradeID'),
                    json_extract(t.raw_json, '$.tradeReduced.tradeID')
                )
        WHERE t.type = 'ORDER_FILL'
        """
        + account_sql,
        params,
    ).fetchall()


def list_tagged_fills(
    conn: sqlite3.Connection, account_id: str | None
) -> list[sqlite3.Row]:
    """
    Return one row (``tag``, ``raw_json``) per tag on each ORDER_FILL,
    scoped to *account_id* when given, otherwise every account.
    """
    account_sql = " AND tx.account_id = ?" if account_id is not None else ""
    params = (account_id,) if account_id is not None else ()
    return conn.execute(
        """
        SELECT tg.tag, tx.raw_json
        FROM tags tg
        JOIN transactions tx ON tg.transaction_id = tx.id
        WHERE tx.type = 'ORDER_FILL'
        """
        + account_sql,
        params,
    ).fetchall()


def list_daily_financing(conn: sqlite3.Connection, account_id: str | None) -> list[str]:
    """
    Return the raw JSON of every DAILY_FINANCING transaction, scoped to
    *account_id* when given, otherwise every account.
    """
    account_sql = " AND account_id = ?" if account_id is not None else ""
    params = (account_id,) if account_id is not None else ()
    rows = conn.execute(
        "SELECT raw_json FROM transactions WHERE type = 'DAILY_FINANCING'"
        + account_sql,
        params,
    ).fetchall()
    return [row[0] for row in rows]


# ---------------------------------------------------------------------------
# Positions and sync --watch
# ---------------------------------------------------------------------------


def count_notes_for_oanda_id(
    conn: sqlite3.Connection, oanda_id: str, account_id: str
) -> int:
    """
    Return how many notes are attached to *account_id*'s transaction with
    *oanda_id* (0 if it has none or isn't in the ledger).

    Scoped to the account because Oanda IDs are only unique within one:
    another account's transaction with the same ID is a different event.
    """
    return int(
        conn.execute(
            """
            SELECT COUNT(*) FROM notes n
            JOIN transactions t ON n.transaction_id = t.id
            WHERE t.oanda_id = ? AND t.account_id = ?
            """,
            (oanda_id, account_id),
        ).fetchone()[0]
    )


def list_transactions_after(
    conn: sqlite3.Connection, account_id: str, after_oanda_id: str
) -> list[sqlite3.Row]:
    """
    Return *account_id*'s transactions with an Oanda ID numerically greater
    than *after_oanda_id* (``id``, ``oanda_id``, ``type``, ``time``,
    ``raw_json``), oldest first: the rows a sync just added after that
    cursor.
    """
    return conn.execute(
        """
        SELECT id, oanda_id, type, time, raw_json
        FROM transactions
        WHERE account_id = ?
          AND CAST(oanda_id AS INTEGER) > CAST(? AS INTEGER)
        ORDER BY time ASC
        """,
        (account_id, after_oanda_id),
    ).fetchall()


# ---------------------------------------------------------------------------
# Financing-rate snapshots (financing)
# ---------------------------------------------------------------------------


def record_financing_snapshot(
    conn: sqlite3.Connection,
    account_id: str,
    rates: list[FinancingRate],
    rate_date: str,
) -> None:
    """
    Upsert *rates* as *account_id*'s snapshot for *rate_date* in
    ``financing_rate_snapshots``.

    Oanda's API exposes only the current rate, so these snapshots are the
    only history ``frmj financing --date`` can show. Re-recording the same
    date overwrites its rows (latest fetch wins) instead of adding
    duplicates.
    """
    conn.executemany(
        """
        INSERT OR REPLACE INTO financing_rate_snapshots
            (account_id, instrument, rate_date, long_rate, short_rate)
        VALUES (?, ?, ?, ?, ?)
        """,
        [
            (account_id, r.instrument, rate_date, str(r.long_rate), str(r.short_rate))
            for r in rates
        ],
    )
    conn.commit()


def load_financing_snapshot(
    conn: sqlite3.Connection, account_id: str, rate_date: str
) -> list[FinancingRate]:
    """
    Return *account_id*'s recorded financing-rate snapshot for *rate_date*,
    or an empty list if none was captured that day (e.g. it predates the
    first ``frmj financing`` run, or the command wasn't run that day).
    """
    rows = conn.execute(
        """
        SELECT instrument, long_rate, short_rate
        FROM financing_rate_snapshots
        WHERE account_id = ? AND rate_date = ?
        """,
        (account_id, rate_date),
    ).fetchall()
    return [FinancingRate(row[0], Decimal(row[1]), Decimal(row[2])) for row in rows]
