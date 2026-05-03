"""
FRoMaJ CLI — typer application.

Commands
--------
``frmj sync [--cold] [--watch [--interval N]]``
    Sync transactions from Oanda. Incremental by default; ``--cold`` fetches
    the full account history.  ``--watch`` enters a polling loop that runs
    ``sync_incremental`` every *N* seconds (default 60) and prints new
    transactions as they arrive.  Exits cleanly on Ctrl+C.

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
    breakdowns by instrument, weekday, and hour (UTC).  Auto-syncs before
    displaying.

``frmj journal [--n N]``
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

import csv as _csv
import io
import json
import os
import sqlite3
import time
from datetime import datetime, timezone
from decimal import Decimal

import httpx
import typer

from frmj.app import (
    clear_draft_plan,
    delete_config,
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
from frmj.domain.analytics import (
    ClosedTrade,
    compute_summary,
    pl_by_hour,
    pl_by_instrument,
    pl_by_weekday,
)
from frmj.domain.pricing import (
    ExitLevels,
    TPSLKind,
    TPSLSpec,
    compute_exit_levels,
    pip_value_home,
)
from frmj.domain.risk import (
    BlockingMode,
    MaxTradesExceeded,
    RiskStrategy,
    ScaleInForbidden,
    ScaleInPolicy,
    evaluate_trade,
)
from frmj.domain.sizing import Direction, compute_units
from frmj.execution.oanda import AccountSummary, CloseFill, OpenTrade
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
    "aud_usd", "eur_usd", "gbp_usd", "nzd_usd",
    "usd_cad", "usd_chf", "usd_jpy",
    # Euro crosses
    "eur_aud", "eur_cad", "eur_chf", "eur_gbp",
    "eur_jpy", "eur_nzd",
    # Sterling crosses
    "gbp_aud", "gbp_cad", "gbp_chf", "gbp_jpy", "gbp_nzd",
    # Antipodean / commodity crosses
    "aud_cad", "aud_chf", "aud_jpy", "aud_nzd",
    "cad_chf", "cad_jpy",
    "chf_jpy",
    "nzd_cad", "nzd_chf", "nzd_jpy",
    # USD exotics
    "usd_cnh", "usd_czk", "usd_dkk", "usd_hkd", "usd_huf",
    "usd_mxn", "usd_nok", "usd_pln", "usd_sar", "usd_sek",
    "usd_sgd", "usd_thb", "usd_try", "usd_zar",
    # EUR exotics
    "eur_czk", "eur_dkk", "eur_huf", "eur_nok",
    "eur_pln", "eur_sek", "eur_try", "eur_zar",
    # Metals / spot commodities
    "xag_usd", "xau_usd", "xcu_usd", "xpd_usd", "xpt_usd",
)


def _complete_instrument(incomplete: str) -> list[str]:
    """Return FX pairs whose names start with *incomplete* (case-insensitive)."""
    return [p for p in _FX_PAIRS if p.startswith(incomplete.lower())]


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
    watch: bool = typer.Option(
        False,
        "--watch",
        "-w",
        help="Poll for new transactions continuously (incremental only).",
    ),
    interval: int = typer.Option(
        60,
        "--interval",
        help="Polling interval in seconds when --watch is active.",
    ),
) -> None:
    """Sync transactions from Oanda."""
    if watch and cold:
        typer.echo("Error: --watch and --cold cannot be used together.", err=True)
        raise typer.Exit(1)

    if watch:
        _watch_loop(interval)
        return

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
        summary = client.get_account_summary()
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

    typer.echo("─" * 56)
    _display_account_summary(summary)

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
        total_pl = sum(t.unrealised_pl for t in trades)
        typer.echo(f"\n  Total P/L: {_pl_str(total_pl)}")

    typer.echo("")
    if not typer.confirm(f"Close {len(trades)} {label}?", default=False):
        typer.echo("Cancelled.")
        conn.close()
        return

    closed = 0
    for t in trades:
        try:
            result = client.close_trade(t.trade_id)
            typer.echo(
                f"  #{t.trade_id} closed at {result.close_price}"
                f"  P/L: {_pl_str(result.realised_pl)}"
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


@config_app.command("unset")
def config_unset(
    key: str = typer.Argument(..., help="Config key to remove, e.g. account_id"),
) -> None:
    """Remove a configuration key from the database."""
    conn = get_db()
    try:
        removed = delete_config(conn, key)
    finally:
        conn.close()
    if removed:
        typer.echo(f"Unset {key}.")
    else:
        typer.echo(f"{key} was not set.")
        raise typer.Exit(1)


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


@config_app.command("check")
def config_check(
    connectivity: bool = typer.Option(
        False,
        "--connectivity",
        help="Also verify the token and account_id are accepted by Oanda.",
    ),
) -> None:
    """Validate configuration and report any issues."""
    conn = get_db()
    try:
        all_cfg = dict(get_all_config(conn))
        account_id_val = all_cfg.get("account_id")

        # Each item is (label, status, detail).
        # status: "OK" | "WARN" | "MISSING" | "INVALID"
        checks: list[tuple[str, str, str]] = []

        # --- Token -----------------------------------------------------------
        token_val = get_token()
        if os.environ.get("OANDA_API_TOKEN"):
            checks.append(("token", "OK", "env var"))
        elif token_val:
            checks.append(("token", "OK", "OS keychain"))
        else:
            checks.append(("token", "MISSING", "run: frmj config set-token"))

        # --- account_id ------------------------------------------------------
        if account_id_val:
            checks.append(("account_id", "OK", account_id_val))
        else:
            checks.append(("account_id", "MISSING",
                           "run: frmj config set account_id <ID>"))

        # --- max_open_trades (required for trading) --------------------------
        mot = all_cfg.get("max_open_trades")
        if mot is None:
            checks.append(("max_open_trades", "WARN",
                           "not set — trading disabled; run: frmj config set max_open_trades <N>"))
        else:
            try:
                if int(mot) <= 0:
                    raise ValueError
                checks.append(("max_open_trades", "OK", mot))
            except ValueError:
                checks.append(("max_open_trades", "INVALID",
                               f"{mot!r} — must be a positive integer"))

        # --- risk_strategy ---------------------------------------------------
        rs_val = all_cfg.get("risk_strategy")
        valid_strategies = [s.value for s in RiskStrategy]
        if rs_val is None:
            checks.append(("risk_strategy", "OK",
                           f"remaining_margin_fraction (default)"))
        elif rs_val in valid_strategies:
            checks.append(("risk_strategy", "OK", rs_val))
        else:
            checks.append(("risk_strategy", "INVALID",
                           f"{rs_val!r} — must be one of: {', '.join(valid_strategies)}"))

        # --- percent_of_equity (required when strategy=percent_of_equity) ----
        effective_strategy = rs_val or "remaining_margin_fraction"
        if effective_strategy == RiskStrategy.PERCENT_OF_EQUITY.value:
            poe = all_cfg.get("percent_of_equity")
            if poe is None:
                checks.append(("percent_of_equity", "MISSING",
                               "required when risk_strategy = percent_of_equity"))
            else:
                checks.append(("percent_of_equity", "OK", poe))

        # --- fixed_dollar (required when strategy=fixed_dollar) --------------
        if effective_strategy == RiskStrategy.FIXED_DOLLAR.value:
            fd = all_cfg.get("fixed_dollar")
            if fd is None:
                checks.append(("fixed_dollar", "MISSING",
                               "required when risk_strategy = fixed_dollar"))
            else:
                checks.append(("fixed_dollar", "OK", fd))

        # --- blocking_mode ---------------------------------------------------
        bm_val = all_cfg.get("blocking_mode")
        valid_modes = [m.value for m in BlockingMode]
        if bm_val is None:
            checks.append(("blocking_mode", "OK", "hard_block (default)"))
        elif bm_val in valid_modes:
            checks.append(("blocking_mode", "OK", bm_val))
        else:
            checks.append(("blocking_mode", "INVALID",
                           f"{bm_val!r} — must be one of: {', '.join(valid_modes)}"))

        # --- scale_in --------------------------------------------------------
        si_val = all_cfg.get("scale_in")
        valid_si = [p.value for p in ScaleInPolicy]
        if si_val is None:
            checks.append(("scale_in", "OK", "never (default)"))
        elif si_val in valid_si:
            checks.append(("scale_in", "OK", si_val))
        else:
            checks.append(("scale_in", "INVALID",
                           f"{si_val!r} — must be one of: {', '.join(valid_si)}"))

        # --- safety_reserve_pct ----------------------------------------------
        sr_val = all_cfg.get("safety_reserve_pct")
        if sr_val is None:
            checks.append(("safety_reserve_pct", "OK", "0 (default)"))
        else:
            try:
                sr = Decimal(sr_val)
                if not (0 <= sr < 1):
                    raise ValueError
                checks.append(("safety_reserve_pct", "OK", sr_val))
            except Exception:
                checks.append(("safety_reserve_pct", "INVALID",
                               f"{sr_val!r} — must be a decimal in [0, 1)"))

        # --- practice_mode ---------------------------------------------------
        pm_val = all_cfg.get("practice_mode")
        if pm_val is None:
            checks.append(("practice_mode", "OK", "true (default)"))
        elif pm_val.lower() in ("true", "false", "1", "0", "yes", "no"):
            checks.append(("practice_mode", "OK", pm_val))
        else:
            checks.append(("practice_mode", "INVALID",
                           f"{pm_val!r} — must be true or false"))

        # --- Connectivity (opt-in) -------------------------------------------
        if connectivity:
            if token_val and account_id_val:
                try:
                    client = get_client(conn)
                    summary = client.get_account_summary()
                    checks.append(("connectivity", "OK",
                                   f"Oanda responded — NAV ${summary.nav:,.2f}"))
                except Exception as exc:
                    checks.append(("connectivity", "INVALID",
                                   f"API call failed: {exc}"))
            else:
                checks.append(("connectivity", "WARN",
                               "skipped — token or account_id not configured"))

    finally:
        conn.close()

    # --- Render --------------------------------------------------------------
    typer.echo("Configuration check")
    typer.echo("─" * 56)

    label_w = max(len(c[0]) for c in checks)
    status_w = max(len(c[1]) for c in checks)

    for label, status, detail in checks:
        if status == "OK":
            badge = typer.style(f"{status:<{status_w}}", fg=typer.colors.GREEN)
        elif status == "WARN":
            badge = typer.style(f"{status:<{status_w}}", fg=typer.colors.YELLOW)
        else:  # MISSING / INVALID
            badge = typer.style(f"{status:<{status_w}}", fg=typer.colors.RED)

        typer.echo(f"  {label:<{label_w}}  {badge}  {detail}")

    errors = [c for c in checks if c[1] in ("MISSING", "INVALID")]
    warnings = [c for c in checks if c[1] == "WARN"]

    typer.echo("")
    if not errors and not warnings:
        typer.echo(typer.style("All checks passed.", fg=typer.colors.GREEN))
    elif not errors:
        typer.echo(f"{len(warnings)} warning(s). Configuration is usable but incomplete.")
    else:
        count = len(errors) + len(warnings)
        typer.echo(
            typer.style(f"{count} issue(s) found.", fg=typer.colors.RED)
        )
        raise typer.Exit(1)


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
        instrument = instrument.upper()
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

    # --- Optional entry note and tags ----------------------------------------
    # Resolve the fill's synthetic DB id once; used for both note and tags.
    fill_row = conn.execute(
        "SELECT id FROM transactions WHERE oanda_id = ? AND account_id = ?",
        (fill.transaction_id, client.account_id),
    ).fetchone()

    note_text = typer.prompt("Add a note (Enter to skip)", default="").strip()
    if note_text:
        if fill_row:
            conn.execute(
                "INSERT INTO notes (transaction_id, body) VALUES (?, ?)",
                (fill_row["id"], note_text),
            )
            conn.commit()
            typer.echo("Note saved.")
        else:
            typer.echo(
                "Note not saved: fill transaction not yet in local DB. "
                "Run 'frmj sync' then add the note manually.",
                err=True,
            )

    tags_raw = typer.prompt("Tags (space-separated, Enter to skip)", default="").strip()
    if tags_raw and fill_row:
        attached = _attach_tags(conn, fill_row["id"], tags_raw.split())
        label = "tag" if attached == 1 else "tags"
        if attached:
            typer.echo(f"{attached} {label} saved.")
    elif tags_raw and not fill_row:
        typer.echo(
            "Tags not saved: fill transaction not yet in local DB. "
            "Run 'frmj sync' then add tags with 'frmj tag'.",
            err=True,
        )

    conn.close()


# ---------------------------------------------------------------------------
# export command
# ---------------------------------------------------------------------------

# Ordered columns for both CSV and JSON export.
_EXPORT_FIELDS = (
    "oanda_id", "account_id", "type", "time",
    "instrument", "units", "direction", "pl", "price",
)


@app.command()
def export(
    fmt: str = typer.Option(
        "csv",
        "--format",
        "-f",
        help="Output format: csv or json.",
    ),
    output: str | None = typer.Option(
        None,
        "--output",
        "-o",
        help="Write to this file path instead of stdout.",
        show_default=False,
    ),
    instrument: str | None = typer.Option(
        None,
        "--instrument",
        "-i",
        help="Filter to one instrument, e.g. EUR_USD.",
        autocompletion=_complete_instrument,
        show_default=False,
    ),
    txn_type: str | None = typer.Option(
        None,
        "--type",
        "-t",
        help="Filter by transaction type, e.g. ORDER_FILL.",
        show_default=False,
    ),
    since: str | None = typer.Option(
        None,
        "--since",
        help="Export transactions on or after this date, e.g. 2026-04-01.",
        show_default=False,
    ),
    include_notes: bool = typer.Option(
        False,
        "--include-notes",
        help="Join notes as an extra column in the export.",
    ),
) -> None:
    """Export transactions to CSV or JSON for external analysis."""
    if fmt not in ("csv", "json"):
        typer.echo("Error: --format must be 'csv' or 'json'.", err=True)
        raise typer.Exit(1)

    conn = get_db()
    try:
        where: list[str] = []
        params: list[object] = []

        if txn_type:
            where.append("type = ?")
            params.append(txn_type)
        if since:
            where.append("time >= ?")
            params.append(since)
        if instrument:
            where.append("json_extract(raw_json, '$.instrument') = ?")
            params.append(instrument)

        where_sql = ("WHERE " + " AND ".join(where)) if where else ""

        txns = conn.execute(
            f"""
            SELECT id, oanda_id, account_id, type, time, raw_json
            FROM transactions
            {where_sql}
            ORDER BY time ASC
            """,
            params,
        ).fetchall()

        notes_by_id: dict[int, list[str]] = {}
        if include_notes and txns:
            txn_ids = [t["id"] for t in txns]
            placeholders = ",".join("?" * len(txn_ids))
            for nr in conn.execute(
                f"SELECT transaction_id, body FROM notes "
                f"WHERE transaction_id IN ({placeholders}) ORDER BY id",
                txn_ids,
            ).fetchall():
                notes_by_id.setdefault(nr["transaction_id"], []).append(nr["body"])
    finally:
        conn.close()

    records = [_make_export_record(t, notes_by_id, include_notes) for t in txns]

    if fmt == "csv":
        content = _records_to_csv(records, include_notes)
    else:
        content = _records_to_json(records)

    if output:
        with open(output, "w", encoding="utf-8", newline="") as fh:
            fh.write(content)
        typer.echo(f"Exported {len(records)} rows to {output}")
    else:
        typer.echo(content, nl=False)


# ---------------------------------------------------------------------------
# stats command
# ---------------------------------------------------------------------------


@app.command()
def stats() -> None:
    """Show trade performance statistics from the local journal."""
    conn = get_db()

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
        rows = conn.execute(
            """
            SELECT t.id, t.oanda_id, t.time, t.raw_json
            FROM transactions t
            WHERE t.type = 'ORDER_FILL'
            """
        ).fetchall()
        # Tag breakdown: for each tag, collect P/L values of tagged closing fills.
        tag_rows = conn.execute(
            """
            SELECT tg.tag, tx.raw_json
            FROM tags tg
            JOIN transactions tx ON tg.transaction_id = tx.id
            WHERE tx.type = 'ORDER_FILL'
            """
        ).fetchall()
    finally:
        conn.close()

    trades: list[ClosedTrade] = []
    for row in rows:
        try:
            data = json.loads(row["raw_json"])
            pl_val = Decimal(data.get("pl", "0") or "0")
            if pl_val == 0:
                continue  # opening fill — no realised P/L
            units_raw = int(Decimal(data.get("units", "0")))
            trades.append(ClosedTrade(
                oanda_id=row["oanda_id"],
                instrument=data.get("instrument", ""),
                time=row["time"],
                pl=pl_val,
                units=abs(units_raw),
                # Closing a long = negative units in close txn; short = positive.
                direction="LONG" if units_raw < 0 else "SHORT",
            ))
        except Exception:
            continue

    if not trades:
        typer.echo("No closed trades in local database.")
        return

    # Build tag → list[Decimal] map from tag_rows (skip opening fills).
    tag_pl: dict[str, list[Decimal]] = {}
    for tr in tag_rows:
        try:
            data = json.loads(tr["raw_json"])
            pl_val = Decimal(data.get("pl", "0") or "0")
            if pl_val != 0:
                tag_pl.setdefault(tr["tag"], []).append(pl_val)
        except Exception:
            continue

    _display_stats(trades, tag_pl)


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
# tag command
# ---------------------------------------------------------------------------


@app.command()
def tag(
    oanda_id: str = typer.Argument(..., help="Oanda transaction ID to tag"),
    tags: list[str] = typer.Argument(..., help="One or more tags to attach"),
) -> None:
    """Attach one or more labels to a locally-synced transaction."""
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
        attached = _attach_tags(conn, row["id"], tags)
    finally:
        conn.close()
    label = "tag" if attached == 1 else "tags"
    typer.echo(f"{attached} {label} added to transaction {oanda_id}.")


# ---------------------------------------------------------------------------
# journal command
# ---------------------------------------------------------------------------


@app.command()
def journal(
    n: int = typer.Option(
        20, "--n", "-n", help="Number of recent transactions to show."
    ),
    instrument: str | None = typer.Option(
        None,
        "--instrument",
        "-i",
        help="Filter to one instrument, e.g. EUR_USD.",
        autocompletion=_complete_instrument,
        show_default=False,
    ),
    txn_type: str | None = typer.Option(
        None,
        "--type",
        "-t",
        help="Filter by transaction type, e.g. ORDER_FILL.",
        show_default=False,
    ),
    since: str | None = typer.Option(
        None,
        "--since",
        help="Show transactions on or after this date, e.g. 2026-04-01.",
        show_default=False,
    ),
    with_notes: bool = typer.Option(
        False,
        "--with-notes",
        help="Only show transactions that have at least one note.",
    ),
    filter_tag: str | None = typer.Option(
        None,
        "--tag",
        help="Filter to transactions tagged with this label.",
        show_default=False,
    ),
) -> None:
    """Show recent transactions with their notes and tags."""
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
        where: list[str] = []
        params: list[object] = []

        if txn_type:
            where.append("type = ?")
            params.append(txn_type)
        if since:
            where.append("time >= ?")
            params.append(since)
        if instrument:
            # json_extract is available in SQLite ≥ 3.9 (2015); safe on all
            # target platforms.
            where.append("json_extract(raw_json, '$.instrument') = ?")
            params.append(instrument)
        if with_notes:
            where.append("id IN (SELECT DISTINCT transaction_id FROM notes)")
        if filter_tag:
            where.append("id IN (SELECT DISTINCT transaction_id FROM tags WHERE tag = ?)")
            params.append(filter_tag.lower())

        where_sql = ("WHERE " + " AND ".join(where)) if where else ""
        params.append(n)

        txns = conn.execute(
            f"""
            SELECT id, oanda_id, type, time, raw_json
            FROM transactions
            {where_sql}
            ORDER BY time DESC
            LIMIT ?
            """,
            params,
        ).fetchall()

        active_filters = [
            f for f in [
                f"instrument={instrument}" if instrument else "",
                f"type={txn_type}" if txn_type else "",
                f"since={since}" if since else "",
                "with-notes" if with_notes else "",
                f"tag={filter_tag}" if filter_tag else "",
            ] if f
        ]
        if active_filters:
            typer.echo(f"Filter: {', '.join(active_filters)}")

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
            txn_tags = conn.execute(
                "SELECT tag FROM tags WHERE transaction_id = ? ORDER BY tag",
                (txn["id"],),
            ).fetchall()
            if txn_tags:
                tag_list = "  ".join(r["tag"] for r in txn_tags)
                typer.echo(f"    Tags: {tag_list}")
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _validate_tag(raw: str) -> str | None:
    """Normalise *raw* to a lowercase tag string, or return None if invalid.

    Valid tags are non-empty and contain only ASCII letters, digits, hyphens,
    and underscores.  Spaces are NOT allowed (they act as delimiters in the
    CLI prompts).
    """
    t = raw.strip().lower()
    if not t:
        return None
    import re
    if not re.fullmatch(r"[a-z0-9_-]+", t):
        return None
    return t


def _attach_tags(
    conn: sqlite3.Connection,
    transaction_id: int,
    raw_tags: list[str],
) -> int:
    """Insert *raw_tags* for *transaction_id*, skipping duplicates and invalids.

    Returns the count of tags actually inserted (duplicates and invalids not
    counted).  Uses INSERT OR IGNORE so idempotent re-tagging is harmless.
    """
    attached = 0
    for raw in raw_tags:
        t = _validate_tag(raw)
        if t is None:
            typer.echo(
                f"  Skipped invalid tag {raw!r} "
                "(only letters, digits, hyphens, underscores allowed).",
                err=True,
            )
            continue
        try:
            conn.execute(
                "INSERT OR IGNORE INTO tags (transaction_id, tag) VALUES (?, ?)",
                (transaction_id, t),
            )
            if conn.execute("SELECT changes()").fetchone()[0]:
                attached += 1
        except Exception:
            pass
    conn.commit()
    return attached


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

    typer.echo(
        f"Watching for new transactions (every {interval}s) — Ctrl+C to stop."
    )

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


def _display_stats(
    trades: list[ClosedTrade],
    tag_pl: dict[str, list[Decimal]] | None = None,
) -> None:
    """Render the full stats report for the given closed trades."""
    summary = compute_summary(trades)
    assert summary is not None  # trades is guaranteed non-empty by caller

    typer.echo(f"Trade summary  ({summary.total} closed trades)")
    typer.echo("─" * 50)
    typer.echo(
        f"  Win rate:   {summary.win_rate * 100:.1f}%"
        f"  ({summary.wins}W / {summary.losses}L)"
    )
    typer.echo(f"  Avg P/L:    {_color_pl(summary.avg_pl)}")
    typer.echo(f"  Total P/L:  {_color_pl(summary.total_pl)}")
    typer.echo(f"  Best:       {_color_pl(summary.best_pl)}")
    typer.echo(f"  Worst:      {_color_pl(summary.worst_pl)}")

    by_instr = pl_by_instrument(trades)
    if by_instr:
        typer.echo("")
        typer.echo("By instrument")
        typer.echo("─" * 50)
        iw = max(len(r[0]) for r in by_instr)
        for instr, count, total, avg in by_instr:
            typer.echo(
                f"  {instr:<{iw}}  {count:>4}  {_color_pl(total)}"
                f"    avg {_color_pl(avg)}"
            )

    by_day = pl_by_weekday(trades)
    if by_day:
        typer.echo("")
        typer.echo("By weekday")
        typer.echo("─" * 50)
        for day, count, total in by_day:
            typer.echo(f"  {day}  {count:>4}  {_color_pl(total)}")

    by_hour = pl_by_hour(trades)
    if by_hour:
        typer.echo("")
        typer.echo("By hour (UTC)")
        typer.echo("─" * 50)
        for hour, count, total in by_hour:
            typer.echo(f"  {hour:02d}:00  {count:>4}  {_color_pl(total)}")

    if tag_pl:
        by_tag: list[tuple[str, int, Decimal]] = []
        for t, pls in tag_pl.items():
            by_tag.append((t, len(pls), sum(pls, Decimal(0))))
        by_tag.sort(key=lambda r: r[2], reverse=True)
        typer.echo("")
        typer.echo("By tag")
        typer.echo("─" * 50)
        tw = max(len(r[0]) for r in by_tag)
        for t, count, total in by_tag:
            typer.echo(f"  {t:<{tw}}  {count:>4}  {_color_pl(total)}")


def _make_export_record(
    txn: sqlite3.Row,
    notes_by_id: dict[int, list[str]],
    include_notes: bool,
) -> dict:
    """Build one export record dict from a transactions row."""
    rec: dict = {
        "oanda_id": txn["oanda_id"],
        "account_id": txn["account_id"],
        "type": txn["type"],
        "time": txn["time"],
        "instrument": "",
        "units": None,
        "direction": "",
        "pl": None,
        "price": "",
    }
    try:
        data = json.loads(txn["raw_json"])
        if txn["type"] == "ORDER_FILL":
            units_raw = int(Decimal(data.get("units", "0")))
            rec["instrument"] = data.get("instrument", "")
            rec["units"] = abs(units_raw)
            rec["direction"] = "LONG" if units_raw >= 0 else "SHORT"
            rec["pl"] = data.get("pl", "") or ""
            rec["price"] = data.get("price", "") or ""
        elif txn["type"] == "DAILY_FINANCING":
            rec["instrument"] = data.get("instrument", "")
            rec["pl"] = data.get("amount") or data.get("financing") or ""
    except Exception:
        pass

    if include_notes:
        rec["notes"] = "; ".join(notes_by_id.get(txn["id"], []))

    return rec


def _records_to_csv(records: list[dict], include_notes: bool) -> str:
    """Serialise export records to CSV text."""
    fields = list(_EXPORT_FIELDS)
    if include_notes:
        fields.append("notes")

    buf = io.StringIO()
    writer = _csv.DictWriter(
        buf, fieldnames=fields, extrasaction="ignore", lineterminator="\n"
    )
    writer.writeheader()
    for rec in records:
        writer.writerow({k: ("" if rec.get(k) is None else rec[k]) for k in fields})
    return buf.getvalue()


def _records_to_json(records: list[dict]) -> str:
    """Serialise export records to JSON text (array of objects)."""
    return json.dumps(records, indent=2, default=str) + "\n"


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
    """Return a colored P/L string like '+$45.23' or '-$3.50' (no leading spaces)."""
    sign = "+" if pl > 0 else ""
    text = f"{sign}${pl:,.2f}"
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

    pl_str = f"  {_color_pl(pl)}" if pl is not None else ""
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


def _pl_str(amount: Decimal) -> str:
    """Return a sign-prefixed, coloured P/L string: green ≥0, red <0."""
    sign = "+" if amount >= 0 else ""
    color = typer.colors.GREEN if amount >= 0 else typer.colors.RED
    return typer.style(f"{sign}${amount:,.2f}", fg=color)


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
        f"         P/L: {_pl_str(trade.unrealised_pl)}"
        f"  margin: ${trade.margin_used:,.2f}"
        f"  {exits_str}"
    )
    typer.echo("")


def _display_account_summary(summary: AccountSummary) -> None:
    """Print account-level summary rows beneath the positions table."""
    rows: list[tuple[str, str]] = [
        ("NAV",              f"${summary.nav:,.2f}"),
        ("Unrealized P/L",  _pl_str(summary.unrealized_pl)),
        ("Balance",         f"${summary.balance:,.2f}"),
        ("Realized P/L",    _pl_str(summary.realized_pl)),
        ("Position Value",  f"${summary.position_value:,.2f}"),
        ("Margin Used",     f"${summary.margin_used:,.2f}"),
        ("Margin Available", f"${summary.margin_available:,.2f}"),
    ]
    label_width = max(len(label) for label, _ in rows)
    for label, value in rows:
        typer.echo(f"  {label:<{label_width}}  {value}")
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
