"""``frmj mode`` — switch between practice and live execution modes."""

from __future__ import annotations

import typer

from frmj.accounts import get_active_account, set_live_mode
from frmj.app import get_db
from frmj.cli import mode_app

# ---------------------------------------------------------------------------
# mode sub-commands
# ---------------------------------------------------------------------------


@mode_app.command("practice")
def mode_practice() -> None:
    """Switch to practice mode — live order execution is disabled."""
    conn = get_db()
    try:
        set_live_mode(conn, enabled=False)
    finally:
        conn.close()
    typer.echo("Mode set to PRACTICE. Live order execution is disabled.")


@mode_app.command("live")
def mode_live() -> None:
    """Enable live trading mode (requires explicit confirmation)."""
    conn = get_db()
    try:
        account = get_active_account(conn)
        account_name = account.name if account else "(none)"

        typer.echo(
            typer.style(
                "WARNING: Live trading mode enables real order execution.",
                fg=typer.colors.YELLOW,
                bold=True,
            )
        )
        typer.echo("")
        typer.echo(f"Active account: {account_name}")
        typer.echo("")

        confirmation = typer.prompt("Type ENABLE LIVE to continue")
        if confirmation != "ENABLE LIVE":
            typer.echo("Cancelled. Live mode not enabled.")
            conn.close()
            raise typer.Exit(0)

        set_live_mode(conn, enabled=True)
    finally:
        conn.close()

    typer.echo(
        typer.style("Live trading mode ENABLED.", fg=typer.colors.RED, bold=True)
    )
