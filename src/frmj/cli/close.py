"""``frmj close`` — close all open tickets for an instrument."""

from __future__ import annotations

from decimal import Decimal

import typer

from frmj import services
from frmj.app import get_client, get_db
from frmj.cli import app
from frmj.cli._completion import _complete_open_instrument
from frmj.cli._display import _pl_str

# ---------------------------------------------------------------------------
# close command
# ---------------------------------------------------------------------------


@app.command()
def close(
    instrument: str = typer.Argument(
        ...,
        help="Instrument to close, e.g. EUR_USD",
        autocompletion=_complete_open_instrument,
    ),
) -> None:
    """Close all open tickets for an instrument."""
    instrument = instrument.upper()
    conn = get_db()
    try:
        client = get_client(conn)
    except RuntimeError as exc:
        typer.echo(f"Error: {exc}", err=True)
        conn.close()
        raise typer.Exit(1)

    try:
        all_trades = client.get_open_trades()
    except Exception as exc:
        typer.echo(f"Error fetching open trades: {exc}", err=True)
        conn.close()
        raise typer.Exit(1)

    trades = [t for t in all_trades if t.instrument == instrument]

    if not trades:
        typer.echo(f"No open positions for {instrument}.")
        conn.close()
        return

    label = "ticket" if len(trades) == 1 else "tickets"
    typer.echo(f"{len(trades)} open {label} for {instrument}:")
    typer.echo("─" * 40)
    for t in trades:
        typer.echo(
            f"  #{t.trade_id}  {t.direction}  {t.units:,} units"
            f"  @ {t.open_price}  P/L: {_pl_str(t.unrealised_pl)}"
        )

    if len(trades) > 1:
        total_pl = sum((t.unrealised_pl for t in trades), Decimal("0"))
        typer.echo(f"\n  Total P/L: {_pl_str(total_pl)}")

    typer.echo("")
    if not typer.confirm(f"Close {len(trades)} {label}?", default=False):
        typer.echo("Cancelled.")
        conn.close()
        return

    close_result = services.execute_close(conn, client, trades)
    for ticket in close_result.ticket_results:
        if ticket.error is not None:
            typer.echo(
                f"  #{ticket.trade_id} failed to close: {ticket.error}", err=True
            )
        else:
            assert ticket.realised_pl is not None
            typer.echo(
                f"  #{ticket.trade_id} closed at {ticket.close_price}"
                f"  P/L: {_pl_str(ticket.realised_pl)}"
                f"  (txn #{ticket.transaction_id})"
            )

    if close_result.sync_error is not None:
        typer.echo(f"[sync] Warning: sync failed — {close_result.sync_error}", err=True)
    elif close_result.sync_rows_ingested:
        typer.echo(f"[sync] +{close_result.sync_rows_ingested} transactions")

    conn.close()
