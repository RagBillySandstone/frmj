"""``frmj positions`` — show open trades with live P/L and TP/SL levels, and
pending entry orders."""

from __future__ import annotations

import typer

from frmj import services
from frmj.app import get_client, get_db
from frmj.cli import app
from frmj.cli._completion import _complete_account_name
from frmj.cli._display import (
    _display_account_summary,
    _display_open_trade,
    _display_pending_order,
)

# ---------------------------------------------------------------------------
# positions command
# ---------------------------------------------------------------------------


@app.command()
def positions(
    account: str | None = typer.Option(
        None,
        "--account",
        "-a",
        help="Use this account instead of the active one (see 'frmj account list').",
        autocompletion=_complete_account_name,
    ),
) -> None:
    """Show all open trades with current P/L and TP/SL levels, and pending
    entry orders."""
    conn = get_db()
    try:
        client = get_client(conn, account)
    except RuntimeError as exc:
        typer.echo(f"Error: {exc}", err=True)
        conn.close()
        raise typer.Exit(1)

    try:
        view = services.fetch_positions_view(client)
    except Exception as exc:
        typer.echo(f"Error fetching open positions: {exc}", err=True)
        conn.close()
        raise typer.Exit(1)

    # Name the overridden account up front so its output can't be mistaken
    # for the active account's.
    if account is not None:
        typer.echo(f"Account: {account}")

    # --- Open trades ---------------------------------------------------------
    if view.trades:
        label = "position" if len(view.trades) == 1 else "positions"
        typer.echo(f"{len(view.trades)} open {label}")
        typer.echo("─" * 56)
        for trade in view.trades:
            quote = view.quotes.get(trade.instrument)
            _display_open_trade(
                conn,
                trade,
                quote.quote_to_home if quote is not None else None,
                view.financing_rates.get(trade.instrument),
            )
        typer.echo("─" * 56)
    else:
        typer.echo("No open positions.")

    # --- Pending entry orders ------------------------------------------------
    # A failed fetch is a warning, not "no pending orders": the user should
    # know the list may be incomplete.
    if view.pending_error is not None:
        typer.echo(
            f"Warning: could not fetch pending orders — {view.pending_error}",
            err=True,
        )
    pending = view.pending_orders or []
    if pending:
        typer.echo("")
        label = "order" if len(pending) == 1 else "orders"
        typer.echo(f"{len(pending)} pending {label}")
        typer.echo("─" * 56)
        for order in pending:
            _display_pending_order(conn, order, view.quotes.get(order.instrument))
        typer.echo("─" * 56)

    # The account summary only adds something when there's exposure to see.
    if view.trades or pending:
        _display_account_summary(view.summary)

    conn.close()
