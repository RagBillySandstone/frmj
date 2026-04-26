"""
FRoMaJ CLI — typer application.

Commands
--------
``frmj sync [--cold]``
    Sync transactions from Oanda. Incremental by default; ``--cold`` fetches
    the full account history.

``frmj config set <key> <value>``
    Write a config key/value to the database.

``frmj config get <key>``
    Read a config key from the database.

``frmj trade <INSTRUMENT> <long|short> [--dry-run]``
    Interactive trade flow: risk → sizing → TP/SL → confirm → execute → note.
    ``--dry-run`` shows the full plan (including exit levels) without placing
    the order or prompting for confirmation.

``frmj note <OANDA_ID> <TEXT>``
    Attach a free-text note to any locally-synced transaction by its Oanda
    transaction ID.  Run ``frmj sync`` first if the transaction is not yet
    in the local database.

``frmj journal [--n N]``
    Show the most recent N transactions (default 20) with any attached notes.
    Reads only from the local database — no network call.

All commands open the database, perform their work, and close.  Network errors
propagate as plain RuntimeError or httpx exceptions and are caught at the
outermost level to show a clean one-line message before exiting non-zero.

Display units: all prices show the number of decimal places Oanda's pip
location implies (4dp for most FX, 2dp for JPY pairs), P/L in home currency
to 2dp, percentages to 1dp.
"""

from __future__ import annotations

import json
import sqlite3
from decimal import Decimal

import typer

from frmj.app import get_client, get_config, get_db, get_risk_config, set_config
from frmj.domain.pricing import (
    ExitLevels,
    TPSLKind,
    TPSLSpec,
    compute_exit_levels,
)
from frmj.domain.risk import MaxTradesExceeded, ScaleInForbidden, evaluate_trade
from frmj.domain.sizing import Direction, compute_units
from frmj.execution.sync import sync_cold, sync_incremental

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


# ---------------------------------------------------------------------------
# sync command
# ---------------------------------------------------------------------------


@app.command()
def sync(
    cold: bool = typer.Option(
        False,
        "--cold",
        help="Full history re-fetch instead of incremental.",
    ),
) -> None:
    """Sync transactions from Oanda."""
    conn = get_db()
    try:
        client = get_client(conn)
        result = sync_cold(conn, client) if cold else sync_incremental(conn, client)
    except RuntimeError as exc:
        typer.echo(f"Error: {exc}", err=True)
        raise typer.Exit(1)
    finally:
        conn.close()

    mode = "cold" if cold else "incremental"
    typer.echo(f"Sync ({mode}): {result.rows_ingested} ingested, {result.rows_skipped} skipped")
    if result.last_oanda_id:
        typer.echo(f"Cursor: transaction {result.last_oanda_id}")
    else:
        typer.echo("No transactions returned.")


# ---------------------------------------------------------------------------
# config sub-commands
# ---------------------------------------------------------------------------


@config_app.command("set")
def config_set(
    key: str = typer.Argument(..., help="Config key, e.g. account_id"),
    value: str = typer.Argument(..., help="Config value"),
) -> None:
    """Set a configuration value."""
    conn = get_db()
    try:
        set_config(conn, key, value)
    finally:
        conn.close()
    typer.echo(f"Set {key} = {value}")


@config_app.command("get")
def config_get(
    key: str = typer.Argument(..., help="Config key to retrieve"),
) -> None:
    """Read a configuration value."""
    conn = get_db()
    try:
        value = get_config(conn, key)
    finally:
        conn.close()
    if value is None:
        typer.echo(f"{key} is not set.")
        raise typer.Exit(1)
    typer.echo(value)


# ---------------------------------------------------------------------------
# trade command
# ---------------------------------------------------------------------------


@app.command()
def trade(
    instrument: str = typer.Argument(..., help="Oanda instrument, e.g. EUR_USD"),
    direction_str: str = typer.Argument(..., metavar="DIRECTION", help="long or short"),
    dry_run: bool = typer.Option(
        False,
        "--dry-run",
        help="Show the full trade plan without placing an order.",
    ),
) -> None:
    """Plan and (optionally) execute a trade."""
    # --- Parse direction -----------------------------------------------------
    direction_str = direction_str.lower()
    if direction_str not in ("long", "short"):
        typer.echo("DIRECTION must be 'long' or 'short'.", err=True)
        raise typer.Exit(1)
    direction = Direction.LONG if direction_str == "long" else Direction.SHORT

    conn = get_db()
    try:
        client = get_client(conn)
        risk_config = get_risk_config(conn)
    except RuntimeError as exc:
        typer.echo(f"Error: {exc}", err=True)
        conn.close()
        raise typer.Exit(1)

    # --- Auto-sync (silent unless error) -------------------------------------
    try:
        sync_result = sync_incremental(conn, client)
        if sync_result.rows_ingested:
            typer.echo(f"[sync] +{sync_result.rows_ingested} transactions")
    except Exception as exc:
        typer.echo(f"[sync] Warning: sync failed — {exc}", err=True)
        # Non-fatal: proceed with potentially stale data.

    # --- Fetch live account state --------------------------------------------
    try:
        summary = client.get_account_summary()
        open_on_instr = client.get_open_tickets_on_instrument(instrument)
        spec = client.get_instrument(instrument)
        quote = client.get_price(instrument)
    except Exception as exc:
        typer.echo(f"Error fetching market data: {exc}", err=True)
        conn.close()
        raise typer.Exit(1)

    # --- Risk model ----------------------------------------------------------
    try:
        sizing_decision = evaluate_trade(
            config=risk_config,
            open_trades=summary.open_trade_count,
            open_tickets_on_instrument=open_on_instr,
            available_margin=summary.margin_available,
            equity=summary.nav,
        )
    except MaxTradesExceeded as exc:
        typer.echo(f"Cannot trade: {exc}", err=True)
        conn.close()
        raise typer.Exit(1)
    except ScaleInForbidden as exc:
        typer.echo(f"Cannot trade: {exc}", err=True)
        conn.close()
        raise typer.Exit(1)

    # --- Display any risk warnings -------------------------------------------
    for warn in sizing_decision.warnings:
        typer.echo(f"Warning: {warn}", err=True)

    # --- Sizing --------------------------------------------------------------
    try:
        units_calc = compute_units(
            capital_to_deploy=sizing_decision.capital_to_deploy,
            spec=spec,
            quote=quote,
            direction=direction,
        )
    except Exception as exc:
        typer.echo(f"Error computing units: {exc}", err=True)
        conn.close()
        raise typer.Exit(1)

    entry_price = quote.entry_price(direction)

    # --- Trade plan header ---------------------------------------------------
    typer.echo("")
    typer.echo(f"Trade plan: {instrument} {direction_str.upper()}")
    typer.echo("─" * 40)
    typer.echo(f"  Account NAV:     ${summary.nav:,.2f}")
    typer.echo(
        f"  Open trades:     {summary.open_trade_count} / {risk_config.max_open_trades}"
    )
    if sizing_decision.size_fraction is not None:
        frac = sizing_decision.size_fraction
        typer.echo(
            f"  Size fraction:   {frac.numerator}/{frac.denominator}"
        )
    typer.echo(f"  Capital at risk: ${sizing_decision.capital_to_deploy:,.2f}")
    typer.echo("")
    typer.echo(f"  Units:   {units_calc.units:,}")
    typer.echo(f"  Margin:  ${units_calc.margin_used:,.2f}")
    typer.echo(f"  Entry:   {entry_price} ({direction_str})")
    typer.echo(f"  Unused:  ${units_calc.capital_unused:,.2f}")
    typer.echo("")

    # --- TP/SL prompts -------------------------------------------------------
    tp_spec = _prompt_tpsl("Take-profit")
    sl_spec = _prompt_tpsl("Stop-loss  ")

    # --- Exit levels ---------------------------------------------------------
    exits = compute_exit_levels(
        entry_price=entry_price,
        units=units_calc.units,
        direction=direction,
        spec=spec,
        quote=quote,
        margin_used=units_calc.margin_used,
        take_profit=tp_spec,
        stop_loss=sl_spec,
    )

    _display_exits(exits, units_calc.margin_used)

    # Display R:R when both sides are available.
    if exits.projected_profit_home is not None and exits.projected_loss_home is not None:
        if exits.projected_loss_home != 0:
            rr = abs(exits.projected_profit_home / exits.projected_loss_home)
            typer.echo(f"  R:R  {rr:.2f}")
    typer.echo("")

    # --- Dry-run exit --------------------------------------------------------
    if dry_run:
        typer.echo("[DRY RUN] Plan complete. No order placed.")
        conn.close()
        return

    # --- Confirm -------------------------------------------------------------
    while True:
        answer = typer.prompt("Confirm order? [y/N/e=edit]").strip().lower()
        if answer in ("n", ""):
            typer.echo("Order cancelled.")
            conn.close()
            return
        if answer == "y":
            break
        if answer == "e":
            tp_spec = _prompt_tpsl("Take-profit (new)")
            sl_spec = _prompt_tpsl("Stop-loss   (new)")
            exits = compute_exit_levels(
                entry_price=entry_price,
                units=units_calc.units,
                direction=direction,
                spec=spec,
                quote=quote,
                margin_used=units_calc.margin_used,
                take_profit=tp_spec,
                stop_loss=sl_spec,
            )
            _display_exits(exits, units_calc.margin_used)

    # --- Place order ---------------------------------------------------------
    units_signed = units_calc.units if direction is Direction.LONG else -units_calc.units
    try:
        fill = client.place_market_order(instrument, units_signed)
    except Exception as exc:
        typer.echo(f"Error placing order: {exc}", err=True)
        conn.close()
        raise typer.Exit(1)

    typer.echo(f"Order filled at {fill.fill_price} — transaction #{fill.transaction_id}")

    # --- Optional entry note -------------------------------------------------
    note_text = typer.prompt("Add a note (Enter to skip)", default="").strip()
    if note_text:
        # Resolve fill transaction to our synthetic DB id so we can attach.
        row = conn.execute(
            "SELECT id FROM transactions WHERE oanda_id = ? AND account_id = ?",
            (fill.transaction_id, client.account_id),
        ).fetchone()
        if row:
            conn.execute(
                "INSERT INTO notes (transaction_id, body) VALUES (?, ?)",
                (row["id"], note_text),
            )
            conn.commit()
            typer.echo("Note saved.")
        else:
            typer.echo(
                "Note not saved: fill transaction not yet in local DB. "
                "Run 'frmj sync' then add the note manually.",
                err=True,
            )

    conn.close()


# ---------------------------------------------------------------------------
# note command
# ---------------------------------------------------------------------------


@app.command()
def note(
    oanda_id: str = typer.Argument(..., help="Oanda transaction ID to annotate"),
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
# journal command
# ---------------------------------------------------------------------------


@app.command()
def journal(
    n: int = typer.Option(
        20, "--n", "-n", help="Number of recent transactions to show."
    ),
) -> None:
    """Show recent transactions with their notes."""
    conn = get_db()
    try:
        txns = conn.execute(
            """
            SELECT id, oanda_id, type, time, raw_json
            FROM transactions
            ORDER BY time DESC
            LIMIT ?
            """,
            (n,),
        ).fetchall()

        if not txns:
            typer.echo("No transactions in local database. Run 'frmj sync' first.")
            return

        for txn in txns:
            _display_transaction(txn)
            notes = conn.execute(
                "SELECT body FROM notes WHERE transaction_id = ? ORDER BY id",
                (txn["id"],),
            ).fetchall()
            for note_row in notes:
                typer.echo(f"    Note: {note_row['body']}")
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _display_transaction(txn: sqlite3.Row) -> None:
    """Format one transaction row for journal display."""
    # Trim the ISO-8601 timestamp to seconds for readability.
    time_short = txn["time"][:19].replace("T", " ")

    extra = ""
    if txn["type"] == "ORDER_FILL":
        try:
            data = json.loads(txn["raw_json"])
            instrument = data.get("instrument", "")
            units = int(Decimal(data.get("units", "0")))
            direction = "LONG" if units >= 0 else "SHORT"
            extra = f"  {instrument} {direction} {abs(units):,} units"
        except Exception:
            pass

    typer.echo(f"{time_short}  {txn['type']:<24}  #{txn['oanda_id']}{extra}")


def _prompt_tpsl(label: str) -> TPSLSpec | None:
    """Prompt for a TP or SL value and return a TPSLSpec, or None to skip.

    Accepted formats:
      ``50``  or ``50p``  → 50 pips
      ``10%``             → 10% return on margin  (stored as fraction 0.10)
    """
    while True:
        raw = typer.prompt(
            f"{label} (pips, or 10% for %RoM, Enter to skip)",
            default="",
        ).strip()
        if not raw:
            return None
        try:
            return _parse_tpsl(raw)
        except ValueError as exc:
            typer.echo(f"  Invalid input: {exc}. Try '50' (pips) or '10%'.")


def _parse_tpsl(raw: str) -> TPSLSpec:
    """Parse a TP/SL string into a TPSLSpec.

    Raises ``ValueError`` on unrecognised format or non-positive value.
    """
    raw = raw.strip()
    if raw.endswith("%"):
        pct = Decimal(raw[:-1])
        return TPSLSpec(kind=TPSLKind.PERCENT_RETURN, value=pct / Decimal("100"))
    # Strip optional trailing 'p' for pips.
    return TPSLSpec(kind=TPSLKind.PIPS, value=Decimal(raw.rstrip("p")))


def _display_exits(exits: ExitLevels, margin_used: Decimal) -> None:
    """Print the exit-levels table and any warnings."""
    typer.echo("Exit levels:")
    if exits.take_profit_price is not None:
        typer.echo(
            f"  TP: {exits.take_profit_price}"
            f"  →  +${exits.projected_profit_home:,.2f}"
            f"  ({exits.return_on_margin_at_tp * 100:+.1f}% RoM)"
        )
    if exits.stop_loss_price is not None:
        typer.echo(
            f"  SL: {exits.stop_loss_price}"
            f"  →  ${exits.projected_loss_home:,.2f}"
            f"  ({exits.return_on_margin_at_sl * 100:+.1f}% RoM)"
        )
    for warn in exits.warnings:
        typer.echo(f"  ! {warn}", err=True)
