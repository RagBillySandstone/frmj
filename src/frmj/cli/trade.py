"""``frmj trade`` — plan and (optionally) execute a trade, single or multi-account."""

from __future__ import annotations

from decimal import Decimal

import httpx
import typer

from frmj import services
from frmj.accounts import get_active_account, is_live_mode, list_group_members
from frmj.app import (
    clear_draft_plan,
    get_client,
    get_db,
    get_risk_config,
    load_draft_plan,
    save_draft_plan,
)
from frmj.cli import app
from frmj.cli._completion import (
    _complete_account_group,
    _complete_direction,
    _complete_instrument,
)
from frmj.cli._display import _color_financing_pct, _daily_financing_home, _pl_str
from frmj.cli._trade_helpers import (
    _display_exits,
    _prompt_retry_save_abort,
    _prompt_tpsl,
)
from frmj.cli._trade_multi import _trade_multi_account
from frmj.cli.journal import _attach_tags
from frmj.domain.pricing import compute_exit_levels, pip_value_home
from frmj.domain.risk import (
    CorrelatedPositionForbidden,
    MaxTradesExceeded,
    ScaleInForbidden,
)
from frmj.domain.sizing import Direction

# ---------------------------------------------------------------------------
# trade command
# ---------------------------------------------------------------------------


def _complete_multi_opposite(ctx: typer.Context, incomplete: str) -> list[str]:
    """Return accounts in the group named by ``--multi``, for ``--opposite``.

    Single-use completer (only ``trade`` has an ``--opposite`` option), so it
    lives here rather than in ``_completion.py``. Falls back to an empty list
    when ``--multi`` hasn't resolved to a group yet, mirroring
    ``_complete_group_member``.
    """
    conn = get_db()
    try:
        members = list_group_members(conn, ctx.params.get("multi", ""))
    finally:
        conn.close()
    return [m.name for m in members if m.name.startswith(incomplete)]


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
        "-d",
        help="Show the full trade plan without placing an order.",
    ),
    resume: bool = typer.Option(
        False,
        "--resume",
        "-r",
        help="Execute the previously saved draft plan (after a failed order attempt).",
    ),
    multi: str | None = typer.Option(
        None,
        "--multi",
        "-m",
        help="Fan this trade out to every account in the named group "
        "(see 'frmj account group').",
        autocompletion=_complete_account_group,
    ),
    opposite: list[str] | None = typer.Option(
        None,
        "--opposite",
        "-o",
        help="Accounts in the --multi group that take the opposite side of this "
        "trade (short if the dialog's direction is long, long if short). "
        "Take-profit/stop-loss are mirrored automatically. Repeat for multiple "
        "accounts.",
        autocompletion=_complete_multi_opposite,
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
        if multi is not None:
            typer.echo("Error: --multi is not supported with --resume.", err=True)
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

    # --- Multi-account dispatch: resolve the group and hand off entirely -----
    # Kept as a separate flow rather than unifying with the single-account path
    # below: risk/sizing/correlation must run independently per account (each
    # has its own NAV and open positions), which changes enough of the
    # planning logic that sharing it here would risk the single-account path's
    # behavior for a feature most trades never touch.
    if multi is not None:
        conn = get_db()
        accounts = list_group_members(conn, multi)
        if not accounts:
            typer.echo(
                f"Error: group '{multi}' not found or has no members. "
                f"Add one with: frmj account group add {multi} ACCOUNT",
                err=True,
            )
            conn.close()
            raise typer.Exit(1)
        opposite_names = frozenset(opposite) if opposite else frozenset()
        unknown = opposite_names - {acct.name for acct in accounts}
        if unknown:
            typer.echo(
                "Error: --opposite account(s) not in group "
                f"'{multi}': {', '.join(sorted(unknown))}",
                err=True,
            )
            conn.close()
            raise typer.Exit(1)
        assert instrument is not None and direction_str is not None
        _trade_multi_account(
            conn,
            accounts,
            instrument,
            direction,
            direction_str,
            dry_run,
            opposite_names,
        )
        return
    elif opposite:
        typer.echo("Error: --opposite requires --multi.", err=True)
        raise typer.Exit(1)

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
        # instrument/direction_str were already validated non-None above.
        assert instrument is not None and direction_str is not None
        # --- Normal path: risk + sizing + TP/SL prompts + confirmation -------
        try:
            risk_config = get_risk_config(conn)
        except RuntimeError as exc:
            typer.echo(f"Error: {exc}", err=True)
            conn.close()
            raise typer.Exit(1)

        # Fetch instrument spec/quote/financing rate and live account state.
        try:
            instrument_ctx = services.fetch_instrument_context(client, instrument)
            account_ctx = services.fetch_account_context(client, instrument)
        except Exception as exc:
            typer.echo(f"Error fetching market data: {exc}", err=True)
            conn.close()
            raise typer.Exit(1)
        summary = account_ctx.summary
        spec = instrument_ctx.spec
        quote = instrument_ctx.quote
        financing_rate = instrument_ctx.financing_rate

        # Risk model, correlated-position check, and unit sizing.
        try:
            account_sizing = services.plan_account_sizing(
                risk_config, account_ctx, instrument_ctx, instrument, direction
            )
        except MaxTradesExceeded as exc:
            typer.echo(f"Cannot trade: {exc}", err=True)
            conn.close()
            raise typer.Exit(1)
        except ScaleInForbidden as exc:
            typer.echo(f"Cannot trade: {exc}", err=True)
            conn.close()
            raise typer.Exit(1)
        except CorrelatedPositionForbidden as exc:
            typer.echo(f"Cannot trade: {exc}", err=True)
            conn.close()
            raise typer.Exit(1)
        except Exception as exc:
            typer.echo(f"Error computing units: {exc}", err=True)
            conn.close()
            raise typer.Exit(1)
        sizing_decision = account_sizing.sizing_decision
        correlation_warnings = account_sizing.correlation_warnings
        units_calc = account_sizing.units_calc

        for warn in sizing_decision.warnings:
            typer.echo(f"Warning: {warn}", err=True)
        for warn in correlation_warnings:
            typer.echo(f"Warning: {warn}", err=True)

        # Correlated-exposure warnings must be explicitly acknowledged rather
        # than scrolling past unread — HARD_BLOCK already aborted above via
        # CorrelatedPositionForbidden, so reaching here means warning_only.
        if correlation_warnings and not typer.confirm("Proceed anyway?", default=False):
            typer.echo("Order cancelled.")
            conn.close()
            raise typer.Exit(0)

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
        if financing_rate is not None:
            rate = (
                financing_rate.long_rate
                if direction is Direction.LONG
                else financing_rate.short_rate
            )
            daily_financing = _daily_financing_home(
                units=units_calc.units,
                entry_price=entry_price,
                quote_to_home=quote.quote_to_home,
                rate=rate,
            )
            typer.echo(
                f"  Financing: {_pl_str(daily_financing)}/day"
                f"  ({_color_financing_pct(rate)} ann.)"
            )
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

        if (
            exits.projected_profit_home is not None
            and exits.projected_loss_home is not None
        ):
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

        units_signed = (
            units_calc.units if direction is Direction.LONG else -units_calc.units
        )
        tp_price = exits.take_profit_price
        sl_price = exits.stop_loss_price

    # =========================================================================
    # Shared post-planning section: place order, attach TP/SL, sync, note
    # =========================================================================
    # Both branches above set instrument to a concrete value before reaching here.
    assert instrument is not None

    # --- Live mode gate: block live orders when mode is practice -------------
    active_account = get_active_account(conn)
    if active_account is not None and not active_account.is_practice:
        if not is_live_mode(conn):
            typer.echo(
                "Error: Active account is a live account, "
                "but live trading mode is not enabled.\n"
                "Run: frmj mode live",
                err=True,
            )
            conn.close()
            raise typer.Exit(1)

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
            plan_path = save_draft_plan(
                {
                    "instrument": instrument,
                    "direction": direction_str,
                    "units_signed": units_signed,
                    "tp_price": str(tp_price) if tp_price is not None else None,
                    "sl_price": str(sl_price) if sl_price is not None else None,
                }
            )
            typer.echo(f"Plan saved to {plan_path}.")
            typer.echo("Resume later with:  frmj trade --resume")
            conn.close()
            return
        else:  # "a"
            typer.echo("Order aborted.")
            conn.close()
            return

    typer.echo(
        f"Order filled at {fill.fill_price} — transaction #{fill.transaction_id}"
    )

    # --- Attach TP/SL, post-fill sync, and save the trade plan ---------------
    post_fill = services.execute_post_fill(conn, client, fill, tp_price, sl_price)

    if post_fill.missing_trade_id:
        typer.echo(
            "Warning: Oanda did not return a trade ID — cannot attach TP/SL. "
            "Set them manually in the Oanda interface.",
            err=True,
        )
    if tp_price is not None and not post_fill.missing_trade_id:
        if post_fill.tp_error is not None:
            typer.echo(
                f"Warning: failed to attach take-profit — {post_fill.tp_error}",
                err=True,
            )
        else:
            typer.echo(
                f"Take-profit set at {tp_price} — order #{post_fill.tp_transaction_id}"
            )
    if sl_price is not None and not post_fill.missing_trade_id:
        if post_fill.sl_error is not None:
            typer.echo(
                f"Warning: failed to attach stop-loss — {post_fill.sl_error}", err=True
            )
            typer.echo(
                "  Position is unprotected — set SL in Oanda immediately.",
                err=True,
            )
        else:
            typer.echo(
                f"Stop-loss set at {sl_price} — order #{post_fill.sl_transaction_id}"
            )

    if post_fill.sync_error is not None:
        typer.echo(
            f"[sync] Warning: post-fill sync failed — {post_fill.sync_error}", err=True
        )

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
