"""``frmj positions`` — show open trades with live P/L and TP/SL levels."""

from __future__ import annotations

import typer

from frmj import services
from frmj.app import get_client, get_db
from frmj.cli import app
from frmj.cli._completion import _complete_account_name
from frmj.cli._display import _display_account_summary, _display_open_trade

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
    """Show all open trades with current P/L and TP/SL levels."""
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

    if not view.trades:
        typer.echo("No open positions.")
        conn.close()
        return

    label = "position" if len(view.trades) == 1 else "positions"
    typer.echo(f"{len(view.trades)} open {label}")
    typer.echo("─" * 56)

    for trade in view.trades:
        _display_open_trade(
            conn,
            trade,
            view.quote_to_home.get(trade.instrument),
            view.financing_rates.get(trade.instrument),
        )

    typer.echo("─" * 56)
    _display_account_summary(view.summary)

    conn.close()
