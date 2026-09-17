"""``frmj trade`` — plan and (optionally) execute a trade, single or multi-account."""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation

import httpx
import typer

from frmj import services
from frmj.accounts import (
    AccountRecord,
    get_active_account,
    is_live_mode,
    list_group_members,
)
from frmj.app import (
    clear_draft_plan,
    get_client,
    get_client_for_account,
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
from frmj.cli.journal import _attach_tags
from frmj.domain.pricing import (
    ExitLevels,
    TPSLKind,
    TPSLSpec,
    compute_exit_levels,
    pip_value_home,
)
from frmj.domain.risk import (
    CorrelatedPositionForbidden,
    MaxTradesExceeded,
    ScaleInForbidden,
    SizingDecision,
)
from frmj.domain.sizing import Direction, UnitsCalc
from frmj.execution.oanda import OandaClient, OrderFill

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
        assert instrument is not None and direction_str is not None
        _trade_multi_account(
            conn, accounts, instrument, direction, direction_str, dry_run
        )
        return

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
    try:
        if raw.endswith("%"):
            pct = Decimal(raw[:-1])
            return TPSLSpec(kind=TPSLKind.PERCENT_RETURN, value=pct / Decimal("100"))
        # Strip optional trailing 'p' for pips.
        return TPSLSpec(kind=TPSLKind.PIPS, value=Decimal(raw.rstrip("p")))
    except InvalidOperation:
        raise ValueError(f"{raw!r} is not a number") from None


def _display_exits(exits: ExitLevels, margin_used: Decimal) -> None:
    """Print the exit-levels table and any warnings."""
    typer.echo("Exit levels:")
    # compute_exit_levels sets projected_profit_home/return_on_margin_at_tp
    # together with take_profit_price — never independently None.
    if exits.take_profit_price is not None:
        assert exits.projected_profit_home is not None
        assert exits.return_on_margin_at_tp is not None
        typer.echo(
            f"  TP: {exits.take_profit_price}"
            f"  →  {_pl_str(exits.projected_profit_home)}"
            f"  ({exits.return_on_margin_at_tp * 100:+.1f}% RoM)"
        )
    if exits.stop_loss_price is not None:
        assert exits.projected_loss_home is not None
        assert exits.return_on_margin_at_sl is not None
        typer.echo(
            f"  SL: {exits.stop_loss_price}"
            f"  →  {_pl_str(exits.projected_loss_home)}"
            f"  ({exits.return_on_margin_at_sl * 100:+.1f}% RoM)"
        )
    for warn in exits.warnings:
        typer.echo(f"  ! {warn}", err=True)


def _prompt_retry_save_abort() -> str:
    """Prompt the user after a failed order attempt.

    Returns 'r' (retry), 's' (save plan), or 'a' (abort).
    """
    while True:
        raw = typer.prompt("[R]etry / [S]ave plan / [A]bort").strip().lower()
        if raw in ("r", "s", "a"):
            return raw
        typer.echo("  Enter R, S, or A.")


# ---------------------------------------------------------------------------
# Multi-account trade flow (frmj trade ... --multi GROUP)
# ---------------------------------------------------------------------------


def _prompt_retry_or_skip() -> str:
    """Prompt after a failed order attempt on one account in a multi-account trade.

    Returns 'r' (retry this account) or 's' (skip this account and continue
    with the rest of the group). Unlike the single-account
    ``_prompt_retry_save_abort``, there is no "abort" option here — other
    accounts in the group may already have filled, so the only choices are to
    keep trying this one account or move on to the next.
    """
    while True:
        raw = typer.prompt("[R]etry / [S]kip this account").strip().lower()
        if raw in ("r", "s"):
            return raw
        typer.echo("  Enter R or S.")


@dataclass(slots=True)
class _AccountPlan:
    """Per-account risk/sizing result within a multi-account trade."""

    account: AccountRecord
    client: OandaClient
    sizing_decision: SizingDecision
    units_calc: UnitsCalc


def _trade_multi_account(
    conn: sqlite3.Connection,
    accounts: list[AccountRecord],
    instrument: str,
    direction: Direction,
    direction_str: str,
    dry_run: bool,
) -> None:
    """Plan and execute the same trade across every account in *accounts*.

    Mirrors the single-account flow in ``trade()`` — risk model, sizing,
    TP/SL prompt, confirm, execute, attach TP/SL, sync, note/tags — but risk,
    sizing, and correlation are evaluated independently per account (each has
    its own NAV, margin, and open positions), while the instrument,
    direction, TP/SL choice, and final confirmation are shared, since it's
    the same intended trade replicated across accounts.
    """
    try:
        risk_config = get_risk_config(conn)
    except RuntimeError as exc:
        typer.echo(f"Error: {exc}", err=True)
        conn.close()
        raise typer.Exit(1)

    # --- Build one client per account, up front -------------------------------
    clients: dict[str, OandaClient] = {}
    for acct in accounts:
        try:
            clients[acct.name] = get_client_for_account(acct)
        except RuntimeError as exc:
            typer.echo(f"Error [{acct.name}]: {exc}", err=True)
            conn.close()
            raise typer.Exit(1)

    # --- Shared market snapshot -------------------------------------------------
    # Fetched once, from the first account's client. Used only for the plan
    # display and to translate TP/SL into absolute prices — the actual fill
    # price for each account is whatever Oanda returns when that account's
    # own order is placed.
    primary_client = clients[accounts[0].name]
    try:
        instrument_ctx = services.fetch_instrument_context(primary_client, instrument)
    except Exception as exc:
        typer.echo(f"Error fetching market data: {exc}", err=True)
        conn.close()
        raise typer.Exit(1)
    spec = instrument_ctx.spec
    quote = instrument_ctx.quote
    financing_rate = instrument_ctx.financing_rate
    entry_price = quote.entry_price(direction)

    # --- Per-account risk model, sizing, and correlation check ----------------
    plans: list[_AccountPlan] = []
    any_correlation_warnings = False
    for acct in accounts:
        client = clients[acct.name]
        try:
            account_ctx = services.fetch_account_context(client, instrument)
        except Exception as exc:
            typer.echo(f"Error fetching account data [{acct.name}]: {exc}", err=True)
            conn.close()
            raise typer.Exit(1)

        try:
            account_sizing = services.plan_account_sizing(
                risk_config, account_ctx, instrument_ctx, instrument, direction
            )
        except (MaxTradesExceeded, ScaleInForbidden) as exc:
            typer.echo(f"Cannot trade on '{acct.name}': {exc}", err=True)
            conn.close()
            raise typer.Exit(1)
        except CorrelatedPositionForbidden as exc:
            typer.echo(f"Cannot trade on '{acct.name}': {exc}", err=True)
            conn.close()
            raise typer.Exit(1)
        except Exception as exc:
            typer.echo(f"Error computing units [{acct.name}]: {exc}", err=True)
            conn.close()
            raise typer.Exit(1)
        sizing_decision = account_sizing.sizing_decision
        correlation_warnings = account_sizing.correlation_warnings

        for warn in sizing_decision.warnings:
            typer.echo(f"Warning [{acct.name}]: {warn}", err=True)
        for warn in correlation_warnings:
            typer.echo(f"Warning [{acct.name}]: {warn}", err=True)
        if correlation_warnings:
            any_correlation_warnings = True

        plans.append(
            _AccountPlan(
                account=acct,
                client=client,
                sizing_decision=sizing_decision,
                units_calc=account_sizing.units_calc,
            )
        )

    # Correlated-exposure warnings must be explicitly acknowledged rather than
    # scrolling past unread — one combined prompt covers the whole group,
    # mirroring the single-account behavior for the same underlying check.
    if any_correlation_warnings and not typer.confirm("Proceed anyway?", default=False):
        typer.echo("Order cancelled.")
        conn.close()
        raise typer.Exit(0)

    # --- Trade plan table -------------------------------------------------------
    typer.echo("")
    typer.echo(
        f"Trade plan: {instrument} {direction_str.upper()}  ({len(plans)} accounts)"
    )
    typer.echo("─" * 60)
    typer.echo(f"  Entry:   {entry_price} ({direction_str})")
    if financing_rate is not None:
        financing_ann_rate = (
            financing_rate.long_rate
            if direction is Direction.LONG
            else financing_rate.short_rate
        )
    typer.echo("")
    for plan in plans:
        acct_type = "practice" if plan.account.is_practice else "live"
        pv = pip_value_home(plan.units_calc.units, spec, quote)
        line = (
            f"  {plan.account.name}  [{acct_type}]  "
            f"Capital at risk ${plan.sizing_decision.capital_to_deploy:,.2f}  "
            f"Units {plan.units_calc.units:,}  "
            f"Margin ${plan.units_calc.margin_used:,.2f}  "
            f"Pip ${pv:.2f}"
        )
        if financing_rate is not None:
            daily_financing = _daily_financing_home(
                units=plan.units_calc.units,
                entry_price=entry_price,
                quote_to_home=quote.quote_to_home,
                rate=financing_ann_rate,
            )
            line += f"  Financing {_pl_str(daily_financing)}/day"
        typer.echo(line)
    typer.echo("")

    # --- TP/SL prompt (once) and per-account exit levels -----------------------
    # A %RoM target translates to a different absolute price per account
    # (each has its own margin_used from independent sizing) — that's
    # expected: it holds the risk/reward ratio constant per account rather
    # than the raw price. A pip target is identical across accounts since
    # entry_price is shared.
    tp_spec = _prompt_tpsl("Take-profit")
    sl_spec = _prompt_tpsl("Stop-loss  ")

    def _compute_all_exits() -> list[ExitLevels]:
        return [
            compute_exit_levels(
                entry_price=entry_price,
                units=plan.units_calc.units,
                direction=direction,
                spec=spec,
                quote=quote,
                margin_used=plan.units_calc.margin_used,
                take_profit=tp_spec,
                stop_loss=sl_spec,
            )
            for plan in plans
        ]

    exits_list = _compute_all_exits()
    for plan, exits in zip(plans, exits_list):
        typer.echo(f"{plan.account.name}:")
        _display_exits(exits, plan.units_calc.margin_used)
    typer.echo("")

    if dry_run:
        typer.echo("[DRY RUN] Plan complete. No orders placed.")
        conn.close()
        return

    # --- Confirm ------------------------------------------------------------
    while True:
        answer = (
            typer.prompt(f"Confirm order on {len(plans)} accounts? [y/N/e=edit]")
            .strip()
            .lower()
        )
        if answer in ("n", ""):
            typer.echo("Order cancelled.")
            conn.close()
            return
        if answer == "y":
            break
        if answer == "e":
            tp_spec = _prompt_tpsl("Take-profit (new)")
            sl_spec = _prompt_tpsl("Stop-loss   (new)")
            exits_list = _compute_all_exits()
            for plan, exits in zip(plans, exits_list):
                typer.echo(f"{plan.account.name}:")
                _display_exits(exits, plan.units_calc.margin_used)
            typer.echo("")

    # --- Live mode gate: check every target account before placing anything ---
    blocked = [
        plan.account.name
        for plan in plans
        if not plan.account.is_practice and not is_live_mode(conn)
    ]
    if blocked:
        typer.echo(
            "Error: the following accounts are live accounts, but live trading "
            f"mode is not enabled: {', '.join(blocked)}\n"
            "Run: frmj mode live",
            err=True,
        )
        conn.close()
        raise typer.Exit(1)

    # --- Place orders, one account at a time ------------------------------------
    # A failure on one account does not roll back accounts that already
    # filled — nothing to roll back, Oanda is the system of record for each
    # account independently. Each failure is reported and the operator
    # chooses retry-this-account or skip-this-account; the rest of the group
    # proceeds regardless.
    results: list[tuple[_AccountPlan, OrderFill | None]] = []
    for plan, exits in zip(plans, exits_list):
        units_signed = (
            plan.units_calc.units
            if direction is Direction.LONG
            else -plan.units_calc.units
        )
        fill: OrderFill | None = None
        while True:
            try:
                fill = plan.client.place_market_order(instrument, units_signed)
                break
            except httpx.TimeoutException as exc:
                typer.echo(
                    f"Warning [{plan.account.name}]: request timed out ({exc}). "
                    "The order may have been placed — check Oanda before retrying "
                    "to avoid a double fill.",
                    err=True,
                )
            except Exception as exc:
                typer.echo(
                    f"Error placing order [{plan.account.name}]: {exc}", err=True
                )

            if _prompt_retry_or_skip() == "r":
                continue
            typer.echo(f"Skipped '{plan.account.name}'.")
            fill = None
            break

        if fill is None:
            results.append((plan, None))
            continue

        typer.echo(
            f"[{plan.account.name}] Order filled at {fill.fill_price} "
            f"— transaction #{fill.transaction_id}"
        )

        post_fill = services.execute_post_fill(
            conn, plan.client, fill, exits.take_profit_price, exits.stop_loss_price
        )

        if post_fill.missing_trade_id:
            typer.echo(
                f"Warning [{plan.account.name}]: Oanda did not return a trade ID "
                "— cannot attach TP/SL. Set them manually in the Oanda interface.",
                err=True,
            )
        if exits.take_profit_price is not None and not post_fill.missing_trade_id:
            if post_fill.tp_error is not None:
                typer.echo(
                    f"Warning [{plan.account.name}]: failed to attach "
                    f"take-profit — {post_fill.tp_error}",
                    err=True,
                )
            else:
                typer.echo(
                    f"[{plan.account.name}] Take-profit set at "
                    f"{exits.take_profit_price} — order #{post_fill.tp_transaction_id}"
                )
        if exits.stop_loss_price is not None and not post_fill.missing_trade_id:
            if post_fill.sl_error is not None:
                typer.echo(
                    f"Warning [{plan.account.name}]: failed to attach "
                    f"stop-loss — {post_fill.sl_error}",
                    err=True,
                )
                typer.echo(
                    f"  [{plan.account.name}] Position is unprotected "
                    "— set SL in Oanda immediately.",
                    err=True,
                )
            else:
                typer.echo(
                    f"[{plan.account.name}] Stop-loss set at "
                    f"{exits.stop_loss_price} — order #{post_fill.sl_transaction_id}"
                )

        if post_fill.sync_error is not None:
            typer.echo(
                f"[sync] Warning [{plan.account.name}]: post-fill sync failed — "
                f"{post_fill.sync_error}",
                err=True,
            )
        results.append((plan, fill))

    # --- Summary -----------------------------------------------------------
    filled = [(plan, fill) for plan, fill in results if fill is not None]
    skipped = [plan for plan, fill in results if fill is None]
    typer.echo("")
    typer.echo(f"Order placed on {len(filled)}/{len(plans)} accounts.")
    if skipped:
        typer.echo(f"  Skipped: {', '.join(p.account.name for p in skipped)}", err=True)

    # --- Optional entry note and tags, applied to every filled account --------
    # Asked once rather than per account — it's the same trade rationale.
    note_text = typer.prompt("Add a note (Enter to skip)", default="").strip()
    tags_raw = typer.prompt("Tags (space-separated, Enter to skip)", default="").strip()
    for plan, fill in filled:
        assert fill is not None
        fill_row = conn.execute(
            "SELECT id FROM transactions WHERE oanda_id = ? AND account_id = ?",
            (fill.transaction_id, plan.client.account_id),
        ).fetchone()
        if not fill_row:
            if note_text or tags_raw:
                typer.echo(
                    f"[{plan.account.name}] Note/tags not saved: fill transaction "
                    "not yet in local DB. Run 'frmj sync' then add them manually.",
                    err=True,
                )
            continue
        if note_text:
            conn.execute(
                "INSERT INTO notes (transaction_id, body) VALUES (?, ?)",
                (fill_row["id"], note_text),
            )
            conn.commit()
        if tags_raw:
            _attach_tags(conn, fill_row["id"], tags_raw.split())
    if note_text or tags_raw:
        typer.echo("Note/tags saved.")

    conn.close()
