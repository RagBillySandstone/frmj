"""``frmj note``, ``frmj tag``, and ``frmj journal`` — annotate and browse the local ledger."""

from __future__ import annotations

import sqlite3

import typer

from frmj.app import get_client, get_db
from frmj.cli import app
from frmj.cli._completion import _complete_instrument, _complete_txn_type
from frmj.cli._display import _display_transaction
from frmj.execution.sync import sync_incremental

# ---------------------------------------------------------------------------
# Tag helpers (shared by the tag command and trade's post-fill tag prompt)
# ---------------------------------------------------------------------------


def _validate_tag(raw: str) -> str | None:
    """Normalise *raw* to a lowercase tag string, or return None if invalid.

    Valid tags are non-empty and contain only ASCII letters, digits, hyphens,
    and underscores.  Spaces are NOT allowed (they act as delimiters in the
    CLI prompts).
    """
    t = raw.strip().lower()
    if not t:
        return None
    import re

    if not re.fullmatch(r"[a-z0-9_-]+", t):
        return None
    return t


def _attach_tags(
    conn: sqlite3.Connection,
    transaction_id: int,
    raw_tags: list[str],
) -> int:
    """Insert *raw_tags* for *transaction_id*, skipping duplicates and invalids.

    Returns the count of tags actually inserted (duplicates and invalids not
    counted).  Uses INSERT OR IGNORE so idempotent re-tagging is harmless.
    """
    attached = 0
    for raw in raw_tags:
        t = _validate_tag(raw)
        if t is None:
            typer.echo(
                f"  Skipped invalid tag {raw!r} "
                "(only letters, digits, hyphens, underscores allowed).",
                err=True,
            )
            continue
        try:
            conn.execute(
                "INSERT OR IGNORE INTO tags (transaction_id, tag) VALUES (?, ?)",
                (transaction_id, t),
            )
            if conn.execute("SELECT changes()").fetchone()[0]:
                attached += 1
        except Exception:
            pass
    conn.commit()
    return attached


def _complete_oanda_id(incomplete: str) -> list[str]:
    """Return locally-synced Oanda transaction IDs starting with *incomplete*.

    Filtered in SQL (rather than fetched in full like the other DB-backed
    completers) since the transactions table can grow much larger than the
    account/tag/group tables the other completers draw from.
    """
    conn = get_db()
    try:
        rows = conn.execute(
            "SELECT DISTINCT oanda_id FROM transactions WHERE oanda_id LIKE ? "
            "ORDER BY CAST(oanda_id AS INTEGER) DESC LIMIT 50",
            (f"{incomplete}%",),
        ).fetchall()
    finally:
        conn.close()
    return [r[0] for r in rows]


def _complete_tag(incomplete: str) -> list[str]:
    """Return distinct tags already attached to some transaction in the local DB."""
    conn = get_db()
    try:
        rows = conn.execute("SELECT DISTINCT tag FROM tags ORDER BY tag").fetchall()
    finally:
        conn.close()
    return [r[0] for r in rows if r[0].startswith(incomplete.lower())]


# ---------------------------------------------------------------------------
# note command
# ---------------------------------------------------------------------------


@app.command()
def note(
    oanda_id: str = typer.Argument(
        ...,
        help="Oanda transaction ID to annotate",
        autocompletion=_complete_oanda_id,
    ),
    text: str = typer.Argument(..., help="Note text"),
) -> None:
    """Attach a note to a locally-synced transaction."""
    conn = get_db()
    try:
        row = conn.execute(
            "SELECT id FROM transactions WHERE oanda_id = ?",
            (oanda_id,),
        ).fetchone()
        if not row:
            typer.echo(
                f"Transaction {oanda_id!r} not found in local database. "
                f"Run 'frmj sync' first.",
                err=True,
            )
            raise typer.Exit(1)
        conn.execute(
            "INSERT INTO notes (transaction_id, body) VALUES (?, ?)",
            (row["id"], text),
        )
        conn.commit()
    finally:
        conn.close()
    typer.echo(f"Note added to transaction {oanda_id}.")


# ---------------------------------------------------------------------------
# tag command
# ---------------------------------------------------------------------------


@app.command()
def tag(
    oanda_id: str = typer.Argument(
        ..., help="Oanda transaction ID to tag", autocompletion=_complete_oanda_id
    ),
    tags: list[str] = typer.Argument(
        ..., help="One or more tags to attach", autocompletion=_complete_tag
    ),
) -> None:
    """Attach one or more labels to a locally-synced transaction."""
    conn = get_db()
    try:
        row = conn.execute(
            "SELECT id FROM transactions WHERE oanda_id = ?",
            (oanda_id,),
        ).fetchone()
        if not row:
            typer.echo(
                f"Transaction {oanda_id!r} not found in local database. "
                f"Run 'frmj sync' first.",
                err=True,
            )
            raise typer.Exit(1)
        attached = _attach_tags(conn, row["id"], tags)
    finally:
        conn.close()
    label = "tag" if attached == 1 else "tags"
    typer.echo(f"{attached} {label} added to transaction {oanda_id}.")


# ---------------------------------------------------------------------------
# journal command
# ---------------------------------------------------------------------------


@app.command()
def journal(
    n: int = typer.Option(
        20, "--number", "-n", help="Number of recent transactions to show."
    ),
    instrument: str | None = typer.Option(
        None,
        "--instrument",
        "-i",
        help="Filter to one instrument, e.g. EUR_USD.",
        autocompletion=_complete_instrument,
        show_default=False,
    ),
    txn_type: str | None = typer.Option(
        None,
        "--type",
        "-t",
        help="Filter by transaction type, e.g. ORDER_FILL.",
        autocompletion=_complete_txn_type,
        show_default=False,
    ),
    since: str | None = typer.Option(
        None,
        "--since",
        "-s",
        help="Show transactions on or after this date, e.g. 2026-04-01.",
        show_default=False,
    ),
    with_notes: bool = typer.Option(
        False,
        "--with-notes",
        "-w",
        help="Only show transactions that have at least one note.",
    ),
    filter_tag: str | None = typer.Option(
        None,
        "--tag",
        "-T",
        help="Filter to transactions tagged with this label.",
        autocompletion=_complete_tag,
        show_default=False,
    ),
) -> None:
    """Show recent transactions with their notes and tags."""
    conn = get_db()

    # Auto-sync: best-effort; journal display proceeds even if sync fails.
    try:
        client = get_client(conn)
        sync_result = sync_incremental(conn, client)
        if sync_result.rows_ingested:
            typer.echo(f"[sync] +{sync_result.rows_ingested} transactions")
    except RuntimeError as exc:
        typer.echo(f"[sync] Warning: {exc}", err=True)
    except Exception as exc:
        typer.echo(f"[sync] Warning: sync failed — {exc}", err=True)

    try:
        where: list[str] = []
        params: list[object] = []

        if txn_type:
            where.append("type = ?")
            params.append(txn_type)
        if since:
            where.append("time >= ?")
            params.append(since)
        if instrument:
            # json_extract is available in SQLite ≥ 3.9 (2015); safe on all
            # target platforms.  Normalise to uppercase so 'eur_usd' matches
            # 'EUR_USD' as stored by Oanda.
            where.append("json_extract(raw_json, '$.instrument') = ?")
            params.append(instrument.upper())
        if with_notes:
            where.append("id IN (SELECT DISTINCT transaction_id FROM notes)")
        if filter_tag:
            where.append(
                "id IN (SELECT DISTINCT transaction_id FROM tags WHERE tag = ?)"
            )
            params.append(filter_tag.lower())

        where_sql = ("WHERE " + " AND ".join(where)) if where else ""
        params.append(n)

        txns = conn.execute(
            f"""
            SELECT id, oanda_id, type, time, raw_json
            FROM transactions
            {where_sql}
            ORDER BY CAST(oanda_id AS INTEGER) DESC
            LIMIT ?
            """,
            params,
        ).fetchall()

        active_filters = [
            f
            for f in [
                f"instrument={instrument}" if instrument else "",
                f"type={txn_type}" if txn_type else "",
                f"since={since}" if since else "",
                "with-notes" if with_notes else "",
                f"tag={filter_tag}" if filter_tag else "",
            ]
            if f
        ]
        if active_filters:
            typer.echo(f"Filter: {', '.join(active_filters)}")

        if not txns:
            typer.echo("No transactions in local database.")
            return

        for txn in txns:
            _display_transaction(txn)
            if txn["type"] == "ORDER_FILL":
                plan = conn.execute(
                    "SELECT tp_price, sl_price FROM trade_plans WHERE transaction_id = ?",
                    (txn["id"],),
                ).fetchone()
                if plan:
                    parts: list[str] = []
                    if plan["tp_price"]:
                        parts.append(f"TP {plan['tp_price']}")
                    if plan["sl_price"]:
                        parts.append(f"SL {plan['sl_price']}")
                    if parts:
                        typer.echo(f"    Plan: {'  '.join(parts)}")
            notes = conn.execute(
                "SELECT body FROM notes WHERE transaction_id = ? ORDER BY id",
                (txn["id"],),
            ).fetchall()
            for note_row in notes:
                typer.echo(f"    Note: {note_row['body']}")
            txn_tags = conn.execute(
                "SELECT tag FROM tags WHERE transaction_id = ? ORDER BY tag",
                (txn["id"],),
            ).fetchall()
            if txn_tags:
                tag_list = "  ".join(r["tag"] for r in txn_tags)
                typer.echo(f"    Tags: {tag_list}")
    finally:
        conn.close()
