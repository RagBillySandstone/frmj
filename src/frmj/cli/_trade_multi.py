"""Multi-account trade flow (``frmj trade ... --multi GROUP``).

Split out of ``trade.py`` because it mirrors, but does not share code with,
the single-account flow: risk, sizing, and correlation must run
independently per account (each has its own NAV and open positions), while
the instrument, direction, TP/SL choice, and final confirmation are shared.
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass

import httpx
import typer

from frmj import services
from frmj.accounts import AccountRecord, is_live_mode
from frmj.app import get_client_for_account, get_risk_config
from frmj.cli._display import _daily_financing_home, _pl_str
from frmj.cli._trade_helpers import _display_exits, _prompt_tpsl
from frmj.cli.journal import _attach_tags
from frmj.domain.pricing import ExitLevels, compute_exit_levels, pip_value_home
from frmj.domain.risk import (
    CorrelatedPositionForbidden,
    MaxTradesExceeded,
    ScaleInForbidden,
    SizingDecision,
)
from frmj.domain.sizing import Direction, UnitsCalc
from frmj.execution.oanda import OandaClient, OrderFill


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
