"""
FRoMaJ CLI — typer application.

Commands
--------
``frmj sync [--cold] [--watch [--interval N]] [--csv PATH]``
    Sync transactions from Oanda. Incremental by default; ``--cold`` fetches
    the full account history.  ``--watch`` enters a polling loop that runs
    ``sync_incremental`` every *N* seconds (default 60) and prints new
    transactions as they arrive.  Exits cleanly on Ctrl+C.  ``--csv`` imports
    an Oanda Hub transaction-history CSV export instead of hitting the API
    (Reports -> Transaction History -> Export to csv, with Timezone set to
    UTC) — useful for backfilling history the API can no longer return.
    Cannot be combined with ``--cold`` or ``--watch``.

``frmj config set <key> <value>``
    Write a config key/value to the database.

``frmj config unset <key>``
    Remove a config key from the database.  Exits 1 if the key was not set.

``frmj config get [<key>]``
    Read a config key from the database.  Omit the key to display all
    currently configured values (token status is shown but never the value).

``frmj config check [--connectivity]``
    Validate all configuration keys and report missing or invalid values.
    ``--connectivity`` additionally calls the Oanda API to verify the token
    and account_id are accepted.  Exits 0 on success, 1 if any errors.

``frmj config set-token``
    Securely store the Oanda API token in the OS keychain (prompted, hidden).

``frmj config unset-token``
    Remove the stored token from the OS keychain.

``frmj account rename OLD_NAME NEW_NAME``
    Rename a configured account profile.  The Oanda account ID and all other
    settings are preserved; only the friendly name changes.  If *OLD_NAME* is
    the active account the active pointer is updated atomically.

``frmj trade <INSTRUMENT> <long|short> [--dry-run] [--multi GROUP]``
    Interactive trade flow: risk → sizing → TP/SL → confirm → execute →
    attach TP/SL on Oanda → note.  ``--dry-run`` shows the full plan
    (including exit levels) without placing the order or prompting for
    confirmation.  ``--multi GROUP`` fans the same trade out to every account
    in a saved group (see ``frmj account group``) instead of just the active
    account; risk, sizing, and correlation are evaluated independently per
    account. Not supported together with ``--resume``.

``frmj positions``
    Show all open trades fetched live from Oanda: instrument, direction,
    units, entry price, unrealised P/L, margin, TP/SL levels with the
    projected dollar P/L if each level is hit.  Trades that have journal
    notes in the local DB are flagged with ``[note]``.

``frmj financing [--date YYYY-MM-DD] [--quiet]``
    Show current long/short financing rates (Oanda's annualized daily
    financing percentages) for every tradable FX pair, grouped major /
    minor / exotic, alphabetical within each group. Each live fetch also
    records a snapshot locally, since Oanda has no historical-rate endpoint;
    ``--date`` looks up a previously recorded snapshot instead of fetching
    live. ``--quiet`` fetches and records with no output on success, for a
    daily cron job.

``frmj close <INSTRUMENT>``
    Close all open tickets for an instrument.  Shows each ticket's current
    P/L and prompts for confirmation before sending any close requests.
    Runs an incremental sync after closing so the local journal reflects
    the closing transactions immediately.

``frmj tag <OANDA_ID> <TAG> [<TAG2>...]``
    Attach one or more short labels to a transaction.  Tags are normalised
    to lowercase and must be non-empty tokens (alphanumeric, hyphens, or
    underscores).  Duplicate tags on the same transaction are silently
    ignored.

``frmj note <OANDA_ID> <TEXT>``
    Attach a free-text note to any locally-synced transaction by its Oanda
    transaction ID.  Run ``frmj sync`` first if the transaction is not yet
    in the local database.

``frmj export [--format csv|json] [--output FILE]``
    Export transactions to a flat file.  Supports the same --instrument,
    --type, --since filters as ``journal``.  ``--include-notes`` joins the
    notes table as an extra column.  Defaults to CSV on stdout.

``frmj stats``
    Show trade performance: win rate, avg P/L, total P/L, best/worst, and
    breakdowns by instrument, weekday, and hour (local time).  Auto-syncs
    before displaying.

``frmj journal [--number N]``
    Show the most recent N transactions (default 20) with any attached notes.
    Auto-syncs before displaying.

All commands open the database, perform their work, and close.  Network errors
propagate as plain RuntimeError or httpx exceptions and are caught at the
outermost level to show a clean one-line message before exiting non-zero.

Display units: all prices show the number of decimal places Oanda's pip
location implies (4dp for most FX, 2dp for JPY pairs), P/L in home currency
to 2dp, percentages to 1dp.
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
