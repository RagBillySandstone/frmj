"""``frmj note``, ``frmj tag``, and ``frmj journal`` — annotate and browse the local ledger."""

from __future__ import annotations

import sqlite3

import typer

from frmj.accounts import get_active_account, list_accounts, resolve_account
from frmj.app import get_client, get_db
from frmj.cli import app
from frmj.cli._completion import (
    _complete_account_name,
    _complete_instrument,
    _complete_txn_type,
)
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
        # Match _resolve_transaction: only suggest the active account's IDs.
        account = get_active_account(conn)
        account_sql = "AND account_id = ? " if account is not None else ""
        params: list[str] = [f"{incomplete}%"]
        if account is not None:
            params.append(account.oanda_id)
        rows = conn.execute(
            "SELECT DISTINCT oanda_id FROM transactions WHERE oanda_id LIKE ? "
            f"{account_sql}ORDER BY CAST(oanda_id AS INTEGER) DESC LIMIT 50",
            params,
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


def _resolve_transaction(conn: sqlite3.Connection, oanda_id: str) -> int:
    """Return the local ``transactions.id`` for *oanda_id*, or exit 1.

    Oanda transaction IDs are only unique within one account, so the lookup
    is scoped to the active account.  With no active account there is
    nothing to scope to: a unique match is accepted, but an ID present in
    several accounts is rejected as ambiguous rather than guessed at.
    """
    account = get_active_account(conn)

    # Step 1: find candidate rows, scoped to the active account if any.
    if account is not None:
        rows = conn.execute(
            "SELECT id FROM transactions WHERE oanda_id = ? AND account_id = ?",
            (oanda_id, account.oanda_id),
        ).fetchall()
        where = f" for account {account.name!r}"
    else:
        rows = conn.execute(
            "SELECT id FROM transactions WHERE oanda_id = ?", (oanda_id,)
        ).fetchall()
        where = ""

    # Step 2: exactly one match is the only acceptable outcome.
    if not rows:
        typer.echo(
            f"Transaction {oanda_id!r} not found in local database{where}. "
            f"Run 'frmj sync' first.",
            err=True,
        )
        raise typer.Exit(1)
    if len(rows) > 1:
        typer.echo(
            f"Transaction {oanda_id!r} exists in {len(rows)} accounts. "
            "Select one first with 'frmj account use NAME'.",
            err=True,
        )
        raise typer.Exit(1)
    return int(rows[0]["id"])


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
        txn_id = _resolve_transaction(conn, oanda_id)
        conn.execute(
            "INSERT INTO notes (transaction_id, body) VALUES (?, ?)",
            (txn_id, text),
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
        txn_id = _resolve_transaction(conn, oanda_id)
        attached = _attach_tags(conn, txn_id, tags)
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
    all_accounts: bool = typer.Option(
        False,
        "--all-accounts",
        "-a",
        help="Show transactions from every account, not just the active one.",
    ),
    account_name: str | None = typer.Option(
        None,
        "--account",
        help="Use this account instead of the active one (see 'frmj account list').",
        autocompletion=_complete_account_name,
        show_default=False,
    ),
) -> None:
    """Show recent transactions with their notes and tags.

    By default only the active account's transactions are shown; pass
    ``--account NAME`` to show another account's instead, or
    ``--all-accounts`` to include every account in the local database.  When
    no active account is configured there is nothing to scope to, so all
    accounts are shown.
    """
    # The two scoping options contradict each other; refuse rather than
    # silently letting one win.
    if account_name is not None and all_accounts:
        typer.echo(
            "Error: --account and --all-accounts cannot be used together.", err=True
        )
        raise typer.Exit(1)

    conn = get_db()

    # Resolve the scope locally (no token needed) so the listing works even
    # when the auto-sync below fails.  A typo in --account must fail here
    # rather than fall through to showing every account.
    account = None if all_accounts else resolve_account(conn, account_name)
    if account is None and account_name is not None:
        typer.echo(
            f"Error: No account named '{account_name}'. List accounts with:\n"
            "  frmj account list",
            err=True,
        )
        conn.close()
        raise typer.Exit(1)

    # Auto-sync: best-effort; journal display proceeds even if sync fails.
    try:
        client = get_client(conn, account_name)
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

        # Scope to the resolved account unless the user asked for everything.
        if account is not None:
            where.append("account_id = ?")
            params.append(account.oanda_id)
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

        # Oanda transaction IDs are only sequential within one account, so
        # a multi-account listing is ordered by time instead (ID breaks ties
        # between events stamped in the same instant).
        order_sql = (
            "CAST(oanda_id AS INTEGER) DESC"
            if account is not None
            else "time DESC, CAST(oanda_id AS INTEGER) DESC"
        )
        txns = conn.execute(
            f"""
            SELECT id, oanda_id, account_id, type, time, raw_json
            FROM transactions
            {where_sql}
            ORDER BY {order_sql}
            LIMIT ?
            """,
            params,
        ).fetchall()

        # When several accounts are shown, label each row with its profile
        # name.  IDs with no profile (e.g. a removed account) fall back to
        # the raw Oanda ID.  If two profiles share an Oanda ID, the first
        # alphabetically wins — list_accounts is ordered by name.
        labels: dict[str, str] = {}
        label_w = 0
        if account is None:
            for rec in list_accounts(conn):
                labels.setdefault(rec.oanda_id, rec.name)
            label_w = max(
                (len(labels.get(t["account_id"], t["account_id"])) for t in txns),
                default=0,
            )

        active_filters = [
            f
            for f in [
                f"account={account.name}" if account else "",
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
            label = (
                None
                if account is not None
                else labels.get(txn["account_id"], txn["account_id"]).ljust(label_w)
            )
            _display_transaction(txn, label)
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
