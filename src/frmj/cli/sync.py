"""``frmj sync`` — sync transactions from Oanda (incremental, cold, csv, or watch)."""

from __future__ import annotations

import time
from datetime import datetime, timezone
from pathlib import Path

import typer

from frmj.accounts import get_active_account
from frmj.app import get_client, get_db
from frmj.cli import app
from frmj.cli._display import _display_transaction
from frmj.execution.sync import sync_cold, sync_csv, sync_incremental

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
) -> None:
    """Sync transactions from Oanda."""
    if csv_path is not None and (cold or watch):
        typer.echo("Error: --csv cannot be combined with --cold or --watch.", err=True)
        raise typer.Exit(1)

    if watch and cold:
        typer.echo("Error: --watch and --cold cannot be used together.", err=True)
        raise typer.Exit(1)

    if watch:
        _watch_loop(interval)
        return

    conn = get_db()
    try:
        if csv_path is not None:
            account = get_active_account(conn)
            if account is None:
                typer.echo("Error: no active account configured.", err=True)
                raise typer.Exit(1)
            result = sync_csv(conn, account.oanda_id, csv_path)
        else:
            client = get_client(conn)
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


def _watch_loop(interval: int) -> None:
    """Poll ``sync_incremental`` every *interval* seconds until Ctrl+C.

    New transactions are printed as they arrive using ``_display_transaction``.
    When no cursor exists (first run), only the count is reported to avoid
    flooding the terminal with historical rows.
    Sync errors are printed to stderr but the loop continues.
    """
    conn = get_db()
    try:
        client = get_client(conn)
    except RuntimeError as exc:
        typer.echo(f"Error: {exc}", err=True)
        conn.close()
        raise typer.Exit(1)

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
