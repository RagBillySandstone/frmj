"""``frmj sync`` — sync transactions from Oanda (incremental, cold, csv, or watch)."""

from __future__ import annotations

import sqlite3
import time
from collections.abc import Callable
from datetime import datetime, timezone
from pathlib import Path

import typer

from frmj.accounts import list_accounts, resolve_account
from frmj.app import get_client, get_db
from frmj.cli import app
from frmj.cli._completion import _complete_account_name
from frmj.cli._display import _display_account_header, _display_transaction
from frmj.execution.oanda import ClientProtocol
from frmj.execution.sync import sync_cold, sync_csv, sync_incremental

# ---------------------------------------------------------------------------
# Auto-sync shared by the local-journal reports (journal, stats)
# ---------------------------------------------------------------------------


def _auto_sync(
    conn: sqlite3.Connection,
    account_names: list[str | None],
    client_factory: Callable[[sqlite3.Connection, str | None], ClientProtocol],
) -> None:
    """
    Best-effort incremental sync of each account in *account_names* before a
    report is read from the local database.

    Each entry is passed to *client_factory* as the ``--account`` override,
    so ``None`` means the active account.  Callers pass their module's
    ``get_client`` so tests can swap it per command.

    A failure (no token, network error, ...) is printed as a warning and the
    remaining accounts are still synced: the report then shows whatever was
    already stored locally.  When more than one account is synced, every
    line names the account it is about.
    """
    labelled = len(account_names) > 1
    for name in account_names:
        # " [name]" / " name:" fragments only when several accounts are synced,
        # keeping the single-account output unchanged.
        tag = f" [{name}]" if labelled else ""
        prefix = f" {name}:" if labelled else ""
        try:
            client = client_factory(conn, name)
            result = sync_incremental(conn, client)
            if result.rows_ingested:
                typer.echo(f"[sync]{prefix} +{result.rows_ingested} transactions")
        except RuntimeError as exc:
            typer.echo(f"[sync] Warning{tag}: {exc}", err=True)
        except Exception as exc:
            typer.echo(f"[sync] Warning{tag}: sync failed — {exc}", err=True)


def _report_sync_targets(
    conn: sqlite3.Connection, account_name: str | None, covers_all: bool
) -> list[str | None]:
    """
    Return the accounts a journal/stats report should auto-sync.

    A report that covers every account (*covers_all*: ``--all-accounts``, or
    no active account to scope to) syncs every configured account, so none
    is shown stale.  Otherwise only *account_name* (``None`` = the active
    account) is synced.  With no accounts configured at all, ``[None]`` lets
    the client factory report the missing account as a warning.
    """
    if covers_all:
        return [rec.name for rec in list_accounts(conn)] or [None]
    return [account_name]


# ---------------------------------------------------------------------------
# sync command
# ---------------------------------------------------------------------------


@app.command()
def sync(
    cold: bool = typer.Option(
        False,
        "--cold",
        "-c",
        help="Full history re-fetch instead of incremental.",
    ),
    watch: bool = typer.Option(
        False,
        "--watch",
        "-w",
        help="Poll for new transactions continuously (incremental only).",
    ),
    interval: int = typer.Option(
        60,
        "--interval",
        "-i",
        help="Polling interval in seconds when --watch is active.",
    ),
    csv_path: Path | None = typer.Option(
        None,
        "--csv",
        help=(
            "Import an Oanda Hub transaction-history CSV export instead of "
            "hitting the API (Reports -> Transaction History -> Export to "
            "csv, with Timezone set to UTC)."
        ),
    ),
    account: str | None = typer.Option(
        None,
        "--account",
        "-a",
        help="Use this account instead of the active one (see 'frmj account list').",
        autocompletion=_complete_account_name,
    ),
) -> None:
    """Sync transactions from Oanda."""
    if csv_path is not None and (cold or watch):
        typer.echo("Error: --csv cannot be combined with --cold or --watch.", err=True)
        raise typer.Exit(1)

    if watch and cold:
        typer.echo("Error: --watch and --cold cannot be used together.", err=True)
        raise typer.Exit(1)

    if watch:
        _watch_loop(interval, account)
        return

    conn = get_db()
    # Name the account up front so the results can't be mistaken for another
    # account's. An unknown account prints nothing here and errors below.
    _display_account_header(conn, account)
    try:
        if csv_path is not None:
            # CSV rows carry no account ID, so they're filed under the target
            # account's Oanda ID — an unknown --account must not fall through.
            record = resolve_account(conn, account)
            if record is None and account is not None:
                typer.echo(f"Error: no account named '{account}'.", err=True)
                raise typer.Exit(1)
            if record is None:
                typer.echo("Error: no active account configured.", err=True)
                raise typer.Exit(1)
            result = sync_csv(conn, record.oanda_id, csv_path)
        else:
            client = get_client(conn, account)
            result = sync_cold(conn, client) if cold else sync_incremental(conn, client)
    except (RuntimeError, ValueError, OSError) as exc:
        typer.echo(f"Error: {exc}", err=True)
        raise typer.Exit(1)
    finally:
        conn.close()

    mode = "csv" if csv_path is not None else ("cold" if cold else "incremental")
    typer.echo(
        f"Sync ({mode}): {result.rows_ingested} ingested, {result.rows_skipped} skipped"
    )
    if result.last_oanda_id:
        typer.echo(f"Cursor: transaction {result.last_oanda_id}")
    else:
        typer.echo("No transactions returned.")


def _watch_loop(interval: int, account: str | None = None) -> None:
    """Poll ``sync_incremental`` every *interval* seconds until Ctrl+C.

    Watches *account* (the ``--account`` override), or the active account
    when it is ``None``.

    New transactions are printed as they arrive using ``_display_transaction``.
    When no cursor exists (first run), only the count is reported to avoid
    flooding the terminal with historical rows.
    Sync errors are printed to stderr but the loop continues.
    """
    conn = get_db()
    try:
        client = get_client(conn, account)
    except RuntimeError as exc:
        typer.echo(f"Error: {exc}", err=True)
        conn.close()
        raise typer.Exit(1)

    _display_account_header(conn, account)
    typer.echo(f"Watching for new transactions (every {interval}s) — Ctrl+C to stop.")

    try:
        while True:
            now_str = datetime.now(tz=timezone.utc).strftime("%H:%M:%S")
            # Read cursor before sync so we can identify new rows afterwards.
            cursor_row = conn.execute(
                "SELECT last_oanda_id FROM sync_cursors WHERE account_id = ?",
                (client.account_id,),
            ).fetchone()
            prev_id: str | None = cursor_row[0] if cursor_row else None

            try:
                result = sync_incremental(conn, client)
            except Exception as exc:
                typer.echo(f"[{now_str}] Sync error: {exc}", err=True)
                time.sleep(interval)
                continue

            if result.rows_ingested:
                if prev_id is not None:
                    new_txns = conn.execute(
                        """
                        SELECT id, oanda_id, type, time, raw_json
                        FROM transactions
                        WHERE account_id = ?
                          AND CAST(oanda_id AS INTEGER) > CAST(? AS INTEGER)
                        ORDER BY time ASC
                        """,
                        (client.account_id, prev_id),
                    ).fetchall()
                    typer.echo(f"[{now_str}] +{result.rows_ingested} new:")
                    for txn in new_txns:
                        _display_transaction(txn)
                else:
                    # First run was a cold sync — don't flood the terminal.
                    typer.echo(
                        f"[{now_str}] Initial sync: {result.rows_ingested} "
                        "transactions loaded. Run 'frmj journal' to view."
                    )

            time.sleep(interval)
    except KeyboardInterrupt:
        typer.echo("\nStopped.")
    finally:
        conn.close()
