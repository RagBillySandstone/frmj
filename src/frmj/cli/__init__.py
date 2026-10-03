"""
FRoMaJ CLI — typer application.

This package defines the Typer ``app`` and its sub-apps (``config``,
``account``, ``account group``, ``mode``) plus the small ``status`` command;
every other command lives in its own module, imported at the bottom of this
file to register it.  The full command and option reference is
``docs/commands.md`` — it is deliberately not repeated here, so there is one
place to keep in sync with the code.

All commands open the database, perform their work, and close.  Network and
configuration errors (``RuntimeError`` or httpx exceptions) are caught in
each command and reported as a one-line message before exiting non-zero.

Display units: prices are shown as Oanda quotes them, and computed exit
prices are rounded to the instrument's ``displayPrecision`` (5dp for most FX,
3dp for JPY pairs); P/L is shown in home currency to 2dp.
"""

from __future__ import annotations

import typer

from frmj.accounts import get_active_account, is_live_mode
from frmj.app import get_db

# ---------------------------------------------------------------------------
# Typer app and sub-app
# ---------------------------------------------------------------------------

app = typer.Typer(
    name="frmj",
    help="FRoMaJ — Forex Risk Operations, Management & Journal",
    no_args_is_help=True,
)

config_app = typer.Typer(
    name="config",
    help="Read and write configuration values.",
    no_args_is_help=True,
)
app.add_typer(config_app, name="config")

account_app = typer.Typer(
    name="account",
    help="Manage named Oanda account profiles.",
    no_args_is_help=True,
)
app.add_typer(account_app, name="account")

group_app = typer.Typer(
    name="group",
    help="Manage named account groups for multi-account trades.",
    no_args_is_help=True,
)
account_app.add_typer(group_app, name="group")

mode_app = typer.Typer(
    name="mode",
    help="Switch between practice and live execution modes.",
    no_args_is_help=True,
)
app.add_typer(mode_app, name="mode")


# ---------------------------------------------------------------------------
# status command
# ---------------------------------------------------------------------------


@app.command()
def status() -> None:
    """Show the active account and current execution mode."""
    conn = get_db()
    try:
        account = get_active_account(conn)
        live = is_live_mode(conn)
    finally:
        conn.close()

    if account is None:
        typer.echo("Account: (none — run: frmj account add NAME)")
    else:
        acct_type = "practice" if account.is_practice else "live"
        typer.echo(f"Account: {account.name}  [{acct_type}, {account.oanda_id}]")

    mode_label = (
        typer.style("LIVE", fg=typer.colors.RED, bold=True) if live else "PRACTICE"
    )
    typer.echo(f"Mode:    {mode_label}")


# ---------------------------------------------------------------------------
# Import each command module for its side effect of registering commands on
# app/config_app/account_app/group_app/mode_app above. Must come after those
# are defined, since each module does `from frmj.cli import app` (etc.) at
# import time.
# ---------------------------------------------------------------------------

from frmj.cli import accounts as accounts  # noqa: E402  (see comment above)
from frmj.cli import close as close  # noqa: E402
from frmj.cli import config as config  # noqa: E402
from frmj.cli import export as export  # noqa: E402
from frmj.cli import financing as financing  # noqa: E402
from frmj.cli import journal as journal  # noqa: E402
from frmj.cli import mode as mode  # noqa: E402
from frmj.cli import positions as positions  # noqa: E402
from frmj.cli import stats as stats  # noqa: E402
from frmj.cli import sync as sync  # noqa: E402
from frmj.cli import trade as trade  # noqa: E402
from frmj.cli import trail as trail  # noqa: E402
