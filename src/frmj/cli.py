"""
FRoMaJ CLI — typer application.

Commands
--------
``frmj sync [--cold]``
    Sync transactions from Oanda. Incremental by default; ``--cold`` fetches
    the full account history.

``frmj config set <key> <value>``
    Write a config key/value to the database.

``frmj config get [<key>]``
    Read a config key from the database.  Omit the key to display all
    currently configured values (token status is shown but never the value).

``frmj config set-token``
    Securely store the Oanda API token in the OS keychain (prompted, hidden).

``frmj config unset-token``
    Remove the stored token from the OS keychain.

``frmj trade <INSTRUMENT> <long|short> [--dry-run]``
    Interactive trade flow: risk → sizing → TP/SL → confirm → execute →
    attach TP/SL on Oanda → note.  ``--dry-run`` shows the full plan
    (including exit levels) without placing the order or prompting for
    confirmation.

``frmj positions``
    Show all open trades fetched live from Oanda: instrument, direction,
    units, entry price, unrealised P/L, margin, TP/SL levels.  Trades that
    have journal notes in the local DB are flagged with ``[note]``.

``frmj close <INSTRUMENT>``
    Close all open tickets for an instrument.  Shows each ticket's current
    P/L and prompts for confirmation before sending any close requests.
    Runs an incremental sync after closing so the local journal reflects
    the closing transactions immediately.

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
import os
import sqlite3
from decimal import Decimal

import httpx
import typer

from frmj.app import (
    clear_draft_plan,
    delete_token,
    get_all_config,
    get_client,
    get_config,
    get_db,
    get_risk_config,
    get_token,
    load_draft_plan,
    save_draft_plan,
    set_config,
    store_token,
)
from frmj.domain.pricing import (
    ExitLevels,
    TPSLKind,
    TPSLSpec,
    compute_exit_levels,
    pip_value_home,
)
from frmj.domain.risk import MaxTradesExceeded, ScaleInForbidden, evaluate_trade
from frmj.domain.sizing import Direction, compute_units
from frmj.execution.oanda import CloseFill, OpenTrade
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
# Shell completion helpers
# ---------------------------------------------------------------------------

# All Oanda FX instruments used to drive tab completion.  This list does not
# gate input — any instrument string is accepted regardless of whether it
# appears here.  Sorted alphabetically within each group.
_FX_PAIRS: tuple[str, ...] = (
    # Majors
    "AUD_USD", "EUR_USD", "GBP_USD", "NZD_USD",
    "USD_CAD", "USD_CHF", "USD_JPY",
    # Euro crosses
    "EUR_AUD", "EUR_CAD", "EUR_CHF", "EUR_GBP",
    "EUR_JPY", "EUR_NZD",
    # Sterling crosses
    "GBP_AUD", "GBP_CAD", "GBP_CHF", "GBP_JPY", "GBP_NZD",
    # Antipodean / commodity crosses
    "AUD_CAD", "AUD_CHF", "AUD_JPY", "AUD_NZD",
    "CAD_CHF", "CAD_JPY",
    "CHF_JPY",
    "NZD_CAD", "NZD_CHF", "NZD_JPY",
    # USD exotics
    "USD_CNH", "USD_CZK", "USD_DKK", "USD_HKD", "USD_HUF",
    "USD_MXN", "USD_NOK", "USD_PLN", "USD_SAR", "USD_SEK",
    "USD_SGD", "USD_THB", "USD_TRY", "USD_ZAR",
    # EUR exotics
    "EUR_CZK", "EUR_DKK", "EUR_HUF", "EUR_NOK",
    "EUR_PLN", "EUR_SEK", "EUR_TRY", "EUR_ZAR",
    # Metals / spot commodities
    "XAG_USD", "XAU_USD", "XCU_USD", "XPD_USD", "XPT_USD",
)


def _complete_instrument(incomplete: str) -> list[str]:
    """Return FX pairs whose names start with *incomplete* (case-insensitive)."""
    return [p for p in _FX_PAIRS if p.startswith(incomplete.upper())]


def _complete_direction(incomplete: str) -> list[str]:
    return [d for d in ("long", "short") if d.startswith(incomplete.lower())]


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
# positions command
# ---------------------------------------------------------------------------


@app.command()
def positions() -> None:
    """Show all open trades with current P/L and TP/SL levels."""
    conn = get_db()
    try:
        client = get_client(conn)
    except RuntimeError as exc:
        typer.echo(f"Error: {exc}", err=True)
        conn.close()
        raise typer.Exit(1)

    try:
        trades = client.get_open_trades()
    except Exception as exc:
        typer.echo(f"Error fetching open positions: {exc}", err=True)
        conn.close()
        raise typer.Exit(1)

    if not trades:
        typer.echo("No open positions.")
        conn.close()
        return

    label = "position" if len(trades) == 1 else "positions"
    typer.echo(f"{len(trades)} open {label}")
    typer.echo("─" * 56)

    for trade in trades:
        _display_open_trade(conn, trade)

    conn.close()


# ---------------------------------------------------------------------------
# close command
# ---------------------------------------------------------------------------


@app.command()
def close(
    instrument: str = typer.Argument(
        ...,
        help="Instrument to close, e.g. EUR_USD",
        autocompletion=_complete_instrument,
    ),
) -> None:
    """Close all open tickets for an instrument."""
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
        pl_sign = "+" if t.unrealised_pl >= 0 else ""
        typer.echo(
            f"  #{t.trade_id}  {t.direction}  {t.units:,} units"
            f"  @ {t.open_price}  P/L: {pl_sign}${t.unrealised_pl:,.2f}"
        )

    if len(trades) > 1:
        total_pl = sum(t.unrealised_pl for t in trades)
        pl_sign = "+" if total_pl >= 0 else ""
        typer.echo(f"\n  Total P/L: {pl_sign}${total_pl:,.2f}")

    typer.echo("")
    if not typer.confirm(f"Close {len(trades)} {label}?", default=False):
        typer.echo("Cancelled.")
        conn.close()
        return

    closed = 0
    for t in trades:
        try:
            result = client.close_trade(t.trade_id)
            pl_sign = "+" if result.realised_pl >= 0 else ""
            typer.echo(
                f"  #{t.trade_id} closed at {result.close_price}"
                f"  P/L: {pl_sign}${result.realised_pl:,.2f}"
                f"  (txn #{result.transaction_id})"
            )
            closed += 1
        except Exception as exc:
            typer.echo(f"  #{t.trade_id} failed to close: {exc}", err=True)

    if closed:
        try:
            sync_result = sync_incremental(conn, client)
            if sync_result.rows_ingested:
                typer.echo(f"[sync] +{sync_result.rows_ingested} transactions")
        except Exception as exc:
            typer.echo(f"[sync] Warning: sync failed — {exc}", err=True)

    conn.close()


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
    key: str | None = typer.Argument(
        None,
        help="Config key to retrieve. Omit to show all configured values.",
    ),
) -> None:
    """Read a configuration value, or show all values if no key is given."""
    conn = get_db()
    try:
        if key is None:
            pairs = get_all_config(conn)
        else:
            value = get_config(conn, key)
    finally:
        conn.close()

    if key is None:
        if not pairs:
            typer.echo("No configuration values set.")
        else:
            width = max(len(k) for k, _ in pairs)
            for k, v in pairs:
                typer.echo(f"{k:<{width}}  =  {v}")
        _print_token_status()
        return

    if value is None:
        typer.echo(f"{key} is not set.")
        raise typer.Exit(1)
    typer.echo(value)


@config_app.command("set-token")
def config_set_token() -> None:
    """Store the Oanda API token securely in the OS keychain."""
    token = typer.prompt("Oanda API token", hide_input=True)
    try:
        store_token(token)
    except RuntimeError as exc:
        typer.echo(f"Error: {exc}", err=True)
        raise typer.Exit(1)
    typer.echo("Token stored in OS keychain.")


@config_app.command("unset-token")
def config_unset_token() -> None:
    """Remove the Oanda API token from the OS keychain."""
    try:
        delete_token()
    except RuntimeError as exc:
        typer.echo(f"Error: {exc}", err=True)
        raise typer.Exit(1)
    typer.echo("Token removed from OS keychain.")


# ---------------------------------------------------------------------------
# trade command
# ---------------------------------------------------------------------------


@app.command()
def trade(
    instrument: str | None = typer.Argument(
        None,
        help="Oanda instrument, e.g. EUR_USD (omit with --resume)",
        autocompletion=_complete_instrument,
    ),
    direction_str: str | None = typer.Argument(
        None,
        metavar="DIRECTION",
        help="long or short (omit with --resume)",
        autocompletion=_complete_direction,
    ),
    dry_run: bool = typer.Option(
        False,
        "--dry-run",
        help="Show the full trade plan without placing an order.",
    ),
    resume: bool = typer.Option(
        False,
        "--resume",
        help="Execute the previously saved draft plan (after a failed order attempt).",
    ),
) -> None:
    """Plan and (optionally) execute a trade."""
    # --- Validate argument combinations --------------------------------------
    if resume:
        if instrument is not None or direction_str is not None:
            typer.echo(
                "Error: instrument and direction are not used with --resume.", err=True
            )
            raise typer.Exit(1)
    else:
        if instrument is None or direction_str is None:
            typer.echo("Error: instrument and direction are required.", err=True)
            raise typer.Exit(1)
        direction_str = direction_str.lower()
        if direction_str not in ("long", "short"):
            typer.echo("DIRECTION must be 'long' or 'short'.", err=True)
            raise typer.Exit(1)
        direction = Direction.LONG if direction_str == "long" else Direction.SHORT

    conn = get_db()
    try:
        client = get_client(conn)
    except RuntimeError as exc:
        typer.echo(f"Error: {exc}", err=True)
        conn.close()
        raise typer.Exit(1)

    # These are set by either the normal or resume path before the shared section.
    units_signed: int
    tp_price: Decimal | None
    sl_price: Decimal | None

    if resume:
        # --- Resume path: skip planning; load the saved draft and confirm ----
        plan = load_draft_plan()
        if plan is None:
            typer.echo(
                "No saved plan found. "
                "Run 'frmj trade <INSTRUMENT> <DIRECTION>' to create one.",
                err=True,
            )
            conn.close()
            raise typer.Exit(1)

        instrument = plan["instrument"]
        direction_str = plan["direction"]
        units_signed = plan["units_signed"]
        tp_price = Decimal(plan["tp_price"]) if plan.get("tp_price") else None
        sl_price = Decimal(plan["sl_price"]) if plan.get("sl_price") else None

        typer.echo(f"Resuming saved plan: {instrument} {direction_str.upper()}")
        typer.echo("─" * 40)
        direction_label = "LONG" if units_signed > 0 else "SHORT"
        typer.echo(f"  Units:     {abs(units_signed):,} ({direction_label})")
        if tp_price is not None:
            typer.echo(f"  Take-profit: {tp_price}")
        if sl_price is not None:
            typer.echo(f"  Stop-loss:   {sl_price}")
        typer.echo("")

        if not typer.confirm("Place order?", default=False):
            typer.echo("Cancelled.")
            conn.close()
            return

    else:
        # --- Normal path: risk + sizing + TP/SL prompts + confirmation -------
        try:
            risk_config = get_risk_config(conn)
        except RuntimeError as exc:
            typer.echo(f"Error: {exc}", err=True)
            conn.close()
            raise typer.Exit(1)

        # Auto-sync (silent unless error)
        try:
            sync_result = sync_incremental(conn, client)
            if sync_result.rows_ingested:
                typer.echo(f"[sync] +{sync_result.rows_ingested} transactions")
        except Exception as exc:
            typer.echo(f"[sync] Warning: sync failed — {exc}", err=True)

        # Fetch live account state
        try:
            summary = client.get_account_summary()
            open_on_instr = client.get_open_tickets_on_instrument(instrument)
            spec = client.get_instrument(instrument)
            quote = client.get_price(instrument)
        except Exception as exc:
            typer.echo(f"Error fetching market data: {exc}", err=True)
            conn.close()
            raise typer.Exit(1)

        # Risk model
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

        for warn in sizing_decision.warnings:
            typer.echo(f"Warning: {warn}", err=True)

        # Sizing
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

        # Trade plan header
        typer.echo("")
        typer.echo(f"Trade plan: {instrument} {direction_str.upper()}")
        typer.echo("─" * 40)
        typer.echo(f"  Account NAV:     ${summary.nav:,.2f}")
        typer.echo(
            f"  Open trades:     {summary.open_trade_count} / {risk_config.max_open_trades}"
        )
        if sizing_decision.size_fraction is not None:
            frac = sizing_decision.size_fraction
            typer.echo(f"  Size fraction:   {frac.numerator}/{frac.denominator}")
        typer.echo(f"  Capital at risk: ${sizing_decision.capital_to_deploy:,.2f}")
        typer.echo("")
        pv = pip_value_home(units_calc.units, spec, quote)
        pip_pct = pv / units_calc.margin_used * Decimal("100")
        typer.echo(f"  Units:   {units_calc.units:,}")
        typer.echo(f"  Margin:  ${units_calc.margin_used:,.2f}")
        typer.echo(f"  Pip:     ${pv:.2f}  ({pip_pct:.2f}% of margin)")
        typer.echo(f"  Entry:   {entry_price} ({direction_str})")
        typer.echo(f"  Unused:  ${units_calc.capital_unused:,.2f}")
        typer.echo("")

        # TP/SL prompts
        tp_spec = _prompt_tpsl("Take-profit")
        sl_spec = _prompt_tpsl("Stop-loss  ")

        # Exit levels
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

        if exits.projected_profit_home is not None and exits.projected_loss_home is not None:
            if exits.projected_loss_home != 0:
                rr = abs(exits.projected_profit_home / exits.projected_loss_home)
                typer.echo(f"  R:R  {rr:.2f}")
        typer.echo("")

        # Dry-run exit
        if dry_run:
            typer.echo("[DRY RUN] Plan complete. No order placed.")
            conn.close()
            return

        # Confirm
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

        units_signed = units_calc.units if direction is Direction.LONG else -units_calc.units
        tp_price = exits.take_profit_price
        sl_price = exits.stop_loss_price

    # =========================================================================
    # Shared post-planning section: place order, attach TP/SL, sync, note
    # =========================================================================

    # --- Place order with retry loop -----------------------------------------
    while True:
        try:
            fill = client.place_market_order(instrument, units_signed)
            clear_draft_plan()
            break
        except httpx.TimeoutException as exc:
            typer.echo(
                f"Warning: request timed out ({exc}). "
                "The order may have been placed — check Oanda before retrying "
                "to avoid a double fill.",
                err=True,
            )
        except Exception as exc:
            typer.echo(f"Error placing order: {exc}", err=True)

        action = _prompt_retry_save_abort()
        if action == "r":
            continue
        elif action == "s":
            plan_path = save_draft_plan({
                "instrument": instrument,
                "direction": direction_str,
                "units_signed": units_signed,
                "tp_price": str(tp_price) if tp_price is not None else None,
                "sl_price": str(sl_price) if sl_price is not None else None,
            })
            typer.echo(f"Plan saved to {plan_path}.")
            typer.echo("Resume later with:  frmj trade --resume")
            conn.close()
            return
        else:  # "a"
            typer.echo("Order aborted.")
            conn.close()
            return

    typer.echo(f"Order filled at {fill.fill_price} — transaction #{fill.transaction_id}")

    # --- Attach TP/SL to the open trade on Oanda -----------------------------
    if fill.trade_id is None:
        if tp_price is not None or sl_price is not None:
            typer.echo(
                "Warning: Oanda did not return a trade ID — cannot attach TP/SL. "
                "Set them manually in the Oanda interface.",
                err=True,
            )
    else:
        if tp_price is not None:
            try:
                tp_txn = client.attach_take_profit(fill.trade_id, tp_price)
                typer.echo(f"Take-profit set at {tp_price} — order #{tp_txn}")
            except Exception as exc:
                typer.echo(f"Warning: failed to attach take-profit — {exc}", err=True)

        if sl_price is not None:
            try:
                sl_txn = client.attach_stop_loss(fill.trade_id, sl_price)
                typer.echo(f"Stop-loss set at {sl_price} — order #{sl_txn}")
            except Exception as exc:
                typer.echo(f"Warning: failed to attach stop-loss — {exc}", err=True)
                typer.echo(
                    "  Position is unprotected — set SL in Oanda immediately.",
                    err=True,
                )

    # --- Post-fill sync -------------------------------------------------------
    try:
        sync_incremental(conn, client)
    except Exception as exc:
        typer.echo(f"[sync] Warning: post-fill sync failed — {exc}", err=True)

    # --- Save trade plan to DB ------------------------------------------------
    _save_trade_plan(conn, fill.transaction_id, client.account_id, tp_price, sl_price)

    # --- Optional entry note -------------------------------------------------
    note_text = typer.prompt("Add a note (Enter to skip)", default="").strip()
    if note_text:
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

    # Auto-sync: best-effort; journal display proceeds even if sync fails.
    try:
        client = get_client(conn)
        sync_result = sync_incremental(conn, client)
        if sync_result.rows_ingested:
            typer.echo(f"[sync] +{sync_result.rows_ingested} transactions")
    except RuntimeError as exc:
        typer.echo(f"[sync] Warning: {exc}", err=True)
    except Exception as exc:
        typer.echo(f"[sync] Warning: sync failed — {exc}", err=True)

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
            typer.echo("No transactions in local database.")
            return

        for txn in txns:
            _display_transaction(txn)
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
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _print_token_status() -> None:
    """Print a single line showing where (or whether) the API token is set.

    Called by ``config_get`` when showing all values.  The token value itself
    is never printed — only its source.
    """
    token_label = "API token"
    if os.environ.get("OANDA_API_TOKEN"):
        typer.echo(f"{token_label}  =  (set via OANDA_API_TOKEN env var)")
    elif get_token() is not None:
        typer.echo(f"{token_label}  =  (stored in OS keychain)")
    else:
        typer.echo(f"{token_label}  =  (not set — run: frmj config set-token)")


def _color_pl(pl: Decimal) -> str:
    """Return a P/L string colored green (profit) or red (loss)."""
    sign = "+" if pl > 0 else ""
    text = f"  {sign}${pl:,.2f}"
    if pl > 0:
        return typer.style(text, fg=typer.colors.GREEN)
    if pl < 0:
        return typer.style(text, fg=typer.colors.RED)
    return text


def _display_transaction(txn: sqlite3.Row) -> None:
    """Format one transaction row for journal display."""
    # Trim the ISO-8601 timestamp to seconds for readability.
    time_short = txn["time"][:19].replace("T", " ")

    extra = ""
    pl: Decimal | None = None

    if txn["type"] == "ORDER_FILL":
        try:
            data = json.loads(txn["raw_json"])
            instrument = data.get("instrument", "")
            units = int(Decimal(data.get("units", "0")))
            direction = "LONG" if units >= 0 else "SHORT"
            extra = f"  {instrument} {direction} {abs(units):,} units"
            # pl is non-zero only on closing fills; opening fills carry "0".
            pl_val = Decimal(data.get("pl", "0") or "0")
            if pl_val != 0:
                pl = pl_val
        except Exception:
            pass

    elif txn["type"] == "DAILY_FINANCING":
        try:
            data = json.loads(txn["raw_json"])
            instrument = data.get("instrument", "")
            if instrument:
                extra = f"  {instrument}"
            # Parent has "financing"; per-instrument children have "amount".
            raw_amount = data.get("amount") or data.get("financing") or "0"
            amount_val = Decimal(raw_amount)
            if amount_val != 0:
                pl = amount_val
        except Exception:
            pass

    pl_str = _color_pl(pl) if pl is not None else ""
    typer.echo(f"{time_short}  {txn['type']:<24}  #{txn['oanda_id']}{extra}{pl_str}")


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


def _display_open_trade(conn: sqlite3.Connection, trade: OpenTrade) -> None:
    """Print one open trade in the positions view."""
    note_count = conn.execute(
        """
        SELECT COUNT(*) FROM notes n
        JOIN transactions t ON n.transaction_id = t.id
        WHERE t.oanda_id = ?
        """,
        (trade.trade_id,),
    ).fetchone()[0]
    note_flag = "  [note]" if note_count else ""

    time_short = trade.open_time[:19].replace("T", " ")
    pl_sign = "+" if trade.unrealised_pl >= 0 else ""

    typer.echo(
        f"  #{trade.trade_id}  {trade.instrument}  {trade.direction}"
        f"  {trade.units:,} units  @ {trade.open_price}"
        f"  (opened {time_short}){note_flag}"
    )

    exits_parts: list[str] = []
    if trade.take_profit_price is not None:
        exits_parts.append(f"TP: {trade.take_profit_price}")
    if trade.stop_loss_price is not None:
        exits_parts.append(f"SL: {trade.stop_loss_price}")
    exits_str = "  ".join(exits_parts) if exits_parts else "no TP/SL set"

    typer.echo(
        f"         P/L: {pl_sign}${trade.unrealised_pl:,.2f}"
        f"  margin: ${trade.margin_used:,.2f}"
        f"  {exits_str}"
    )
    typer.echo("")


def _prompt_retry_save_abort() -> str:
    """Prompt the user after a failed order attempt.

    Returns 'r' (retry), 's' (save plan), or 'a' (abort).
    """
    while True:
        raw = typer.prompt("[R]etry / [S]ave plan / [A]bort").strip().lower()
        if raw in ("r", "s", "a"):
            return raw
        typer.echo("  Enter R, S, or A.")


def _save_trade_plan(
    conn: sqlite3.Connection,
    fill_oanda_id: str,
    account_id: str,
    tp_price: Decimal | None,
    sl_price: Decimal | None,
) -> None:
    """Persist the intended TP/SL for a fill transaction if either side was set.

    Silent no-op when neither TP nor SL was specified, or when the fill
    transaction is not yet in the local DB (post-fill sync may have failed).
    Uses INSERT OR IGNORE so a duplicate call (e.g. from a retry) is harmless.
    """
    if tp_price is None and sl_price is None:
        return
    row = conn.execute(
        "SELECT id FROM transactions WHERE oanda_id = ? AND account_id = ?",
        (fill_oanda_id, account_id),
    ).fetchone()
    if not row:
        return
    tp_str = str(tp_price) if tp_price is not None else None
    sl_str = str(sl_price) if sl_price is not None else None
    conn.execute(
        "INSERT OR IGNORE INTO trade_plans (transaction_id, tp_price, sl_price) "
        "VALUES (?, ?, ?)",
        (row["id"], tp_str, sl_str),
    )
    conn.commit()
