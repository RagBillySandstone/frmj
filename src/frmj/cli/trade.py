"""``frmj trade`` — plan and (optionally) execute a trade, single or multi-account."""

from __future__ import annotations

import sqlite3
from decimal import Decimal

import httpx
import typer

from frmj import services
from frmj.accounts import (
    is_live_mode,
    list_accounts,
    list_group_members,
    resolve_account,
)
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
    _complete_account_name,
    _complete_direction,
    _complete_instrument,
)
from frmj.cli._display import _color_financing_pct, _daily_financing_home, _pl_str
from frmj.cli._trade_helpers import (
    _display_exits,
    _prompt_limit_price,
    _prompt_retry_save_abort,
    _prompt_tpsl,
)
from frmj.cli._trade_multi import _trade_multi_account
from frmj.cli.journal import _attach_tags
from frmj.domain.pricing import compute_exit_levels, pip_size, pip_value_home
from frmj.domain.risk import (
    CorrelatedPositionForbidden,
    MaxTradesExceeded,
    ScaleInForbidden,
)
from frmj.domain.sizing import Direction
from frmj.execution.oanda import LimitOrderResult, OandaClient, OrderFill

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


def _resolve_draft_account(conn: sqlite3.Connection, plan: dict) -> str | None:
    """Return the profile name a resumed draft should be placed on.

    Drafts record both the profile name and its Oanda account ID.  The ID is
    what identifies the account: a profile can be renamed after the draft is
    saved, and a removed profile's name can be reused for a *different* Oanda
    account, which must never receive this order.  So the draft is matched by
    ID, preferring the saved name when it still belongs to that ID.

    Drafts saved before the ID was recorded fall back to the saved name, or
    to the active account (``None``) when they predate recording an account
    at all.

    Raises ``ValueError`` with a user-facing message when no profile, or
    more than one, has the saved ID.
    """
    saved_name: str | None = plan.get("account")
    oanda_id: str | None = plan.get("account_oanda_id")
    if oanda_id is None:
        return saved_name

    # Step 1: every profile pointing at the saved Oanda account.
    matches = [a for a in list_accounts(conn) if a.oanda_id == oanda_id]

    # Step 2: the saved name still refers to the same account — the usual case.
    if any(a.name == saved_name for a in matches):
        return saved_name

    # Step 3: renamed since the draft was saved — follow the ID.
    if len(matches) == 1:
        typer.echo(
            f"Note: the plan was saved for account '{saved_name}', "
            f"now named '{matches[0].name}'."
        )
        return matches[0].name

    # Step 4: removed, or ambiguous — refuse rather than guess.
    if not matches:
        raise ValueError(
            f"The saved plan is for account '{saved_name}' (Oanda ID {oanda_id}), "
            "which is no longer configured. Add it back with "
            "'frmj account add NAME', or plan a new trade."
        )
    names = ", ".join(sorted(a.name for a in matches))
    raise ValueError(
        f"The saved plan is for Oanda account {oanda_id}, which several profiles "
        f"share ({names}). Remove the duplicates or plan a new trade."
    )


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
    account: str | None = typer.Option(
        None,
        "--account",
        "-a",
        help="Use this account instead of the active one (see 'frmj account list').",
        autocompletion=_complete_account_name,
    ),
    limit: bool = typer.Option(
        False,
        "--limit",
        "-l",
        help="Place a GTC limit (pending) entry order instead of a market order. "
        "Prompts for the entry as pips better than the market, @price, or a "
        "percent of the current price.",
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
        if account is not None:
            typer.echo(
                "Error: --account is not used with --resume "
                "(the saved plan records its account).",
                err=True,
            )
            raise typer.Exit(1)
        if limit:
            typer.echo(
                "Error: --limit is not used with --resume "
                "(the saved plan records its order type).",
                err=True,
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

    # --- Multi-account dispatch: resolve the group and hand off entirely -----
    # Kept as a separate flow rather than unifying with the single-account path
    # below: risk/sizing/correlation must run independently per account (each
    # has its own NAV and open positions), which changes enough of the
    # planning logic that sharing it here would risk the single-account path's
    # behavior for a feature most trades never touch.
    if multi is not None and account is not None:
        typer.echo("Error: --account cannot be combined with --multi.", err=True)
        raise typer.Exit(1)
    if multi is not None and limit:
        typer.echo("Error: --limit is not supported with --multi.", err=True)
        raise typer.Exit(1)
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

    # A resumed plan is placed on the account it was planned for, which may
    # not be the active account any more (or ever, with --account), and may
    # have been renamed since — _resolve_draft_account finds it by Oanda ID.
    plan: dict | None = None
    if resume:
        plan = load_draft_plan()
        if plan is None:
            typer.echo(
                "No saved plan found. "
                "Run 'frmj trade <INSTRUMENT> <DIRECTION>' to create one.",
                err=True,
            )
            raise typer.Exit(1)

    conn = get_db()
    if plan is not None:
        try:
            account = _resolve_draft_account(conn, plan)
        except ValueError as exc:
            typer.echo(f"Error: {exc}", err=True)
            conn.close()
            raise typer.Exit(1)
    try:
        client = get_client(conn, account)
    except RuntimeError as exc:
        typer.echo(f"Error: {exc}", err=True)
        conn.close()
        raise typer.Exit(1)
    # The profile the order goes to — drives the live-mode gate and is recorded
    # in a saved draft. None only when get_client is stubbed in tests.
    target_account = resolve_account(conn, account)

    # These are set by either the normal or resume path before the shared section.
    units_signed: int
    tp_price: Decimal | None
    sl_price: Decimal | None
    # Set only for a limit order; None means a market order.
    limit_price: Decimal | None = None

    if resume:
        # --- Resume path: skip planning; confirm the draft loaded above ------
        assert plan is not None

        instrument = plan["instrument"]
        direction_str = plan["direction"]
        units_signed = plan["units_signed"]
        tp_price = Decimal(plan["tp_price"]) if plan.get("tp_price") else None
        sl_price = Decimal(plan["sl_price"]) if plan.get("sl_price") else None
        # Plans saved before --limit existed have no "limit_price": market.
        limit_price = Decimal(plan["limit_price"]) if plan.get("limit_price") else None

        typer.echo(f"Resuming saved plan: {instrument} {direction_str.upper()}")
        typer.echo("─" * 40)
        if account is not None:
            typer.echo(f"  Account:   {account}")
        direction_label = "LONG" if units_signed > 0 else "SHORT"
        typer.echo(f"  Units:     {abs(units_signed):,} ({direction_label})")
        if limit_price is not None:
            typer.echo(f"  Limit price: {limit_price} (GTC)")
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

        # A limit order enters at the price the user picks; TP/SL, R:R, and
        # financing below are all computed from it. Unit sizing above still
        # used today's conversion rates, which is what the order is sized at.
        if limit:
            typer.echo(f"Market: bid {quote.bid} / ask {quote.ask}")
            limit_price = _prompt_limit_price(direction, spec, quote)
            entry_price = limit_price
        else:
            entry_price = quote.entry_price(direction)

        # Trade plan header
        typer.echo("")
        typer.echo(f"Trade plan: {instrument} {direction_str.upper()}")
        typer.echo("─" * 40)
        if account is not None:
            typer.echo(f"  Account:         {account}")
        typer.echo(f"  Account NAV:     ${summary.nav:,.2f}")
        # Pending entry orders count toward the cap, so show them alongside.
        pending_count = len(account_ctx.pending_orders)
        pending_note = f" (+{pending_count} pending)" if pending_count else ""
        typer.echo(
            f"  Open trades:     {summary.open_trade_count} / "
            f"{risk_config.max_open_trades}{pending_note}"
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
        if limit_price is not None:
            # Show how far the limit sits from the side it would fill against.
            reference_label = "ask" if direction is Direction.LONG else "bid"
            distance_pips = abs(quote.entry_price(direction) - limit_price) / pip_size(
                spec
            )
            typer.echo(
                f"  Entry:   {entry_price} ({direction_str} limit, GTC — "
                f"{distance_pips:.1f} pips from {reference_label})"
            )
        else:
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
    # Checks the account the order actually goes to, so --account can't be
    # used to slip a live order past practice mode.
    if target_account is not None and not target_account.is_practice:
        if not is_live_mode(conn):
            typer.echo(
                f"Error: Account '{target_account.name}' is a live account, "
                "but live trading mode is not enabled.\n"
                "Run: frmj mode live",
                err=True,
            )
            conn.close()
            raise typer.Exit(1)

    # --- Place order with retry loop -----------------------------------------
    # Exactly one of these is set once the loop exits: a market order yields
    # a fill, a limit order a LimitOrderResult (which may itself hold a fill).
    fill: OrderFill | None = None
    limit_result: LimitOrderResult | None = None
    while True:
        try:
            if limit_price is not None:
                limit_result = client.place_limit_order(
                    instrument, units_signed, limit_price, tp_price, sl_price
                )
            else:
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
                    "limit_price": (
                        str(limit_price) if limit_price is not None else None
                    ),
                    "account": (
                        target_account.name if target_account is not None else None
                    ),
                    # The ID survives a rename; see _resolve_draft_account.
                    "account_oanda_id": (
                        target_account.oanda_id if target_account is not None else None
                    ),
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

    if limit_result is not None:
        journal_oanda_id = _report_limit_order(
            conn, client, limit_result, limit_price, tp_price, sl_price
        )
    else:
        assert fill is not None
        _report_market_fill(conn, client, fill, tp_price, sl_price)
        journal_oanda_id = fill.transaction_id

    _prompt_note_and_tags(conn, client.account_id, journal_oanda_id)
    conn.close()


def _report_limit_order(
    conn: sqlite3.Connection,
    client: OandaClient,
    result: LimitOrderResult,
    limit_price: Decimal | None,
    tp_price: Decimal | None,
    sl_price: Decimal | None,
) -> str:
    """Report a placed limit order, sync it, and save its trade plan.

    TP/SL went in the order body, so there is nothing to attach — Oanda
    sets them when the order fills. Returns the Oanda transaction ID the
    entry's note and tags should attach to (see ``execute_post_limit``).
    """
    if result.fill is not None:
        # The market crossed the limit before the order arrived.
        typer.echo(
            f"Limit order filled immediately at {result.fill.fill_price} — "
            f"transaction #{result.fill.transaction_id}"
        )
        when = "set"
    else:
        typer.echo(f"Limit order #{result.order_id} placed at {limit_price} (GTC)")
        when = "will be set when it fills"
    if tp_price is not None:
        typer.echo(f"Take-profit {tp_price} {when}")
    if sl_price is not None:
        typer.echo(f"Stop-loss {sl_price} {when}")

    post = services.execute_post_limit(conn, client, result, tp_price, sl_price)
    if post.sync_error is not None:
        typer.echo(
            f"[sync] Warning: post-order sync failed — {post.sync_error}", err=True
        )
    return post.journal_oanda_id


def _report_market_fill(
    conn: sqlite3.Connection,
    client: OandaClient,
    fill: OrderFill,
    tp_price: Decimal | None,
    sl_price: Decimal | None,
) -> None:
    """Report a market fill, attach TP/SL, sync, and save the trade plan."""
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


def _prompt_note_and_tags(
    conn: sqlite3.Connection, account_id: str, journal_oanda_id: str
) -> None:
    """Prompt for an optional entry note and tags and attach them to the
    transaction *journal_oanda_id* (a fill, or a pending limit order's
    LIMIT_ORDER transaction).

    If that transaction isn't in the local DB yet (post-order sync failed),
    nothing is saved and the user is told how to add them later.
    """
    # Resolve the transaction's synthetic DB id once; used for note and tags.
    txn_row = conn.execute(
        "SELECT id FROM transactions WHERE oanda_id = ? AND account_id = ?",
        (journal_oanda_id, account_id),
    ).fetchone()

    note_text = typer.prompt("Add a note (Enter to skip)", default="").strip()
    if note_text:
        if txn_row:
            conn.execute(
                "INSERT INTO notes (transaction_id, body) VALUES (?, ?)",
                (txn_row["id"], note_text),
            )
            conn.commit()
            typer.echo("Note saved.")
        else:
            typer.echo(
                "Note not saved: transaction not yet in local DB. "
                "Run 'frmj sync' then add the note manually.",
                err=True,
            )

    tags_raw = typer.prompt("Tags (space-separated, Enter to skip)", default="").strip()
    if tags_raw and txn_row:
        attached = _attach_tags(conn, txn_row["id"], tags_raw.split())
        label = "tag" if attached == 1 else "tags"
        if attached:
            typer.echo(f"{attached} {label} saved.")
    elif tags_raw and not txn_row:
        typer.echo(
            "Tags not saved: transaction not yet in local DB. "
            "Run 'frmj sync' then add tags with 'frmj tag'.",
            err=True,
        )
