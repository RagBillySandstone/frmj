"""
Multi-step application flows shared by the CLI (and, eventually, other
front ends).

This module contains no Typer dependency and performs no terminal I/O — it
only fetches data from ``OandaClient``, calls the domain layer, and writes to
the local database. Interactive parts of a flow (prompting for TP/SL,
confirming an order, formatting output for the terminal) stay in ``cli.py``;
that module calls into the functions here for the parts of a flow that don't
require user interaction.

This is the split described in TODO item 6 ("Service layer extraction"):
``fetch_instrument_context``/``fetch_account_context`` + ``plan_account_sizing``
cover the market-data and risk-check steps of the trade flow,
``execute_post_fill`` covers the TP/SL-attach + sync + persist steps after an
order is placed (``execute_post_limit`` is its limit-order counterpart), and ``fetch_positions_view`` / ``execute_close`` cover the
``positions`` and ``close`` commands respectively. Order placement itself
(with its retry/save/abort prompt) stays in ``cli.py`` because the retry
decision is inherently interactive.

``plan_account_sizing`` is also the shared per-account planning step
described in TODO item 9 ("Unify single- and multi-account trade planning"):
the single-account ``trade()`` command calls it once, and
``_trade_multi_account()`` calls it once per group member, each with its own
``AccountContext`` but the same shared ``InstrumentContext``.
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from decimal import Decimal

from frmj.domain.risk import (
    RiskConfig,
    SizingDecision,
    evaluate_correlation,
    evaluate_trade,
)
from frmj.domain.sizing import (
    Direction,
    InstrumentSpec,
    PriceQuote,
    UnitsCalc,
    compute_units,
    margin_per_unit,
)
from frmj.execution.oanda import (
    AccountSummary,
    FinancingRate,
    LimitOrderResult,
    OandaClient,
    OpenTrade,
    OrderFill,
    PendingOrder,
)
from frmj.execution.sync import sync_incremental

# ---------------------------------------------------------------------------
# Trade planning: market data + risk/correlation/sizing evaluation
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class InstrumentContext:
    """Live instrument spec, quote, and financing rate for a planned trade.

    Independent of which account is trading — one instance is fetched and
    shared across every account in a multi-account trade.
    """

    spec: InstrumentSpec
    quote: PriceQuote
    financing_rate: FinancingRate | None


def fetch_instrument_context(client: OandaClient, instrument: str) -> InstrumentContext:
    """Fetch the instrument spec, live quote, and financing rate for *instrument*.

    Any account's client can be used to fetch this — the data doesn't depend
    on which account is trading. The financing-rate fetch is best-effort:
    Oanda's instruments endpoint can fail independently of the rest, and a
    missing financing rate only means the trade-plan display omits that one
    line, so a failure there is swallowed rather than propagated.
    """
    spec = client.get_instrument(instrument)
    quote = client.get_price(instrument)
    try:
        financing_rate: FinancingRate | None = client.get_financing_rates([instrument])[
            0
        ]
    except Exception:
        financing_rate = None
    return InstrumentContext(spec=spec, quote=quote, financing_rate=financing_rate)


@dataclass(frozen=True, slots=True)
class AccountContext:
    """Live account state needed to risk-check and size a trade for one account.

    ``pending_orders`` are the account's unfilled entry orders (limit, stop,
    market-if-touched). The risk model treats each one as if it had already
    filled: it counts toward the open-trade cap and the correlation check,
    and ``pending_margin`` — its estimated margin at current prices — is
    deducted from available margin before sizing. Oanda itself reserves no
    margin for a pending order, so without this deduction a trade sized now
    could leave too little margin for the order to fill later.
    """

    summary: AccountSummary
    open_tickets_on_instrument: int
    open_trades: list[OpenTrade]
    pending_orders: list[PendingOrder]
    pending_margin: Decimal


def _estimate_pending_margin(
    client: OandaClient, pending_orders: list[PendingOrder]
) -> Decimal:
    """Estimate the margin *pending_orders* would use if they all filled now.

    Uses the same formula as unit sizing (``units * margin_rate *
    base_to_home``), with each instrument's current margin rate and
    base-to-home conversion — one spec and one quote fetch per distinct
    instrument. This is an estimate: the real margin is set at fill time.
    Fetch errors propagate, since silently treating a pending order as
    margin-free would over-size the new trade.
    """
    total = Decimal(0)
    # Cache the per-unit margin by instrument so several pending orders on
    # the same instrument cost only one spec/quote round trip.
    per_unit_by_instrument: dict[str, Decimal] = {}
    for order in pending_orders:
        per_unit = per_unit_by_instrument.get(order.instrument)
        if per_unit is None:
            spec = client.get_instrument(order.instrument)
            quote = client.get_price(order.instrument)
            per_unit = margin_per_unit(spec, quote.base_to_home)
            per_unit_by_instrument[order.instrument] = per_unit
        total += Decimal(order.units) * per_unit
    return total


def fetch_account_context(client: OandaClient, instrument: str) -> AccountContext:
    """Fetch one account's summary, open-ticket count on *instrument*, open
    trades, and pending entry orders (with their estimated margin) — the
    account-specific state needed to risk-check and size a trade on this
    account.
    """
    pending_orders = client.get_pending_orders()
    return AccountContext(
        summary=client.get_account_summary(),
        open_tickets_on_instrument=client.get_open_tickets_on_instrument(instrument),
        open_trades=client.get_open_trades(),
        pending_orders=pending_orders,
        pending_margin=_estimate_pending_margin(client, pending_orders),
    )


@dataclass(frozen=True, slots=True)
class AccountSizing:
    """Per-account outcome of the risk/correlation checks and unit sizing."""

    sizing_decision: SizingDecision
    correlation_warnings: tuple[str, ...]
    units_calc: UnitsCalc


def plan_account_sizing(
    risk_config: RiskConfig,
    account: AccountContext,
    instrument_ctx: InstrumentContext,
    instrument: str,
    direction: Direction,
) -> AccountSizing:
    """Run the max-open-trades/sizing check, the correlated-position check,
    and unit sizing for one account's leg of a trade on *instrument*.

    This is the one per-account planning step shared by the single- and
    multi-account trade flows. Pending entry orders are treated as already
    filled: each counts toward the open-trade cap and the correlation check,
    and their estimated margin is deducted from available margin (see
    ``AccountContext``), and pending orders on *instrument* count toward the
    scale-in check.

    Raises ``MaxTradesExceeded`` or ``ScaleInForbidden`` (from
    ``evaluate_trade``), ``CorrelatedPositionForbidden`` (from
    ``evaluate_correlation``), or ``BelowMinimumUnits``/``ValueError`` (from
    ``compute_units``) when the trade can't proceed on this account —
    callers should catch these and surface them as user-facing errors rather
    than tracebacks.
    """
    # Pending orders reserve a slot and margin as if already filled; clamp at
    # zero since the estimate can exceed what Oanda currently reports free.
    available_margin = max(
        Decimal(0), account.summary.margin_available - account.pending_margin
    )
    sizing_decision = evaluate_trade(
        config=risk_config,
        open_trades=account.summary.open_trade_count + len(account.pending_orders),
        open_tickets_on_instrument=account.open_tickets_on_instrument,
        available_margin=available_margin,
        equity=account.summary.nav,
        pending_orders_on_instrument=sum(
            1 for o in account.pending_orders if o.instrument == instrument
        ),
    )
    # Correlation checks pending orders too, since a pending order becomes
    # the same directional exposure once it fills; they're passed separately
    # so the warning can say "pending" rather than "open".
    correlation_warnings = evaluate_correlation(
        open_positions=[(t.instrument, t.direction) for t in account.open_trades],
        pending_positions=[(o.instrument, o.direction) for o in account.pending_orders],
        new_instrument=instrument,
        new_direction=direction,
        blocking_mode=risk_config.correlation_blocking_mode,
    )
    units_calc = compute_units(
        capital_to_deploy=sizing_decision.capital_to_deploy,
        spec=instrument_ctx.spec,
        quote=instrument_ctx.quote,
        direction=direction,
    )
    return AccountSizing(
        sizing_decision=sizing_decision,
        correlation_warnings=correlation_warnings,
        units_calc=units_calc,
    )


# ---------------------------------------------------------------------------
# Post-fill: attach TP/SL, sync, persist the trade plan
# ---------------------------------------------------------------------------


def _save_trade_plan(
    conn: sqlite3.Connection,
    fill_oanda_id: str,
    account_id: str,
    tp_price: Decimal | None,
    sl_price: Decimal | None,
    trail_pips: Decimal | None,
) -> None:
    """Persist the intended TP/SL/trailing stop for a fill transaction if any
    was set.

    For a limit order that hasn't filled yet, *fill_oanda_id* is the
    LIMIT_ORDER transaction that created it; sync moves the plan to the
    ORDER_FILL once the order fills.

    Silent no-op when no exit was specified, or when the fill
    transaction is not yet in the local DB (post-fill sync may have failed).
    Uses INSERT OR IGNORE so a duplicate call (e.g. from a retry) is harmless.
    """
    if tp_price is None and sl_price is None and trail_pips is None:
        return
    row = conn.execute(
        "SELECT id FROM transactions WHERE oanda_id = ? AND account_id = ?",
        (fill_oanda_id, account_id),
    ).fetchone()
    if not row:
        return
    tp_str = str(tp_price) if tp_price is not None else None
    sl_str = str(sl_price) if sl_price is not None else None
    trail_str = str(trail_pips) if trail_pips is not None else None
    conn.execute(
        "INSERT OR IGNORE INTO trade_plans "
        "(transaction_id, tp_price, sl_price, trail_pips) VALUES (?, ?, ?, ?)",
        (row["id"], tp_str, sl_str, trail_str),
    )
    conn.commit()


@dataclass(frozen=True, slots=True)
class PostFillResult:
    """Outcome of attaching TP/SL and syncing after an order fill.

    Every failure mode is reported here rather than raised, matching the
    CLI's existing warn-and-continue behavior: a failed TP/SL attach or a
    failed post-fill sync should never be treated as the trade itself having
    failed, since the order is already filled.
    """

    missing_trade_id: bool
    tp_transaction_id: str | None
    tp_error: str | None
    sl_transaction_id: str | None
    sl_error: str | None
    sync_rows_ingested: int
    sync_error: str | None
    trail_transaction_id: str | None = None
    trail_error: str | None = None


def execute_post_fill(
    conn: sqlite3.Connection,
    client: OandaClient,
    fill: OrderFill,
    tp_price: Decimal | None,
    sl_price: Decimal | None,
    trail_distance: Decimal | None = None,
    trail_pips: Decimal | None = None,
) -> PostFillResult:
    """Attach TP/SL (and optionally a trailing stop) to a filled trade, sync
    the fill into the local DB, and persist the trade plan.

    *trail_distance* is the trailing stop's distance in price units, or
    ``None`` for no trailing stop; *trail_pips* is the same distance in pips,
    for the trade plan.

    ``missing_trade_id`` is set when Oanda didn't return a trade ID and at
    least one exit order was requested (so none could be attached at all).
    Attach failures and sync failures are captured in the result fields
    rather than raised.
    """
    missing_trade_id = False
    tp_transaction_id: str | None = None
    tp_error: str | None = None
    sl_transaction_id: str | None = None
    sl_error: str | None = None
    trail_transaction_id: str | None = None
    trail_error: str | None = None

    if fill.trade_id is None:
        missing_trade_id = any(
            level is not None for level in (tp_price, sl_price, trail_distance)
        )
    else:
        if tp_price is not None:
            try:
                tp_transaction_id = client.attach_take_profit(fill.trade_id, tp_price)
            except Exception as exc:
                tp_error = str(exc)
        if sl_price is not None:
            try:
                sl_transaction_id = client.attach_stop_loss(fill.trade_id, sl_price)
            except Exception as exc:
                sl_error = str(exc)
        # Attached independently of the fixed SL: a trade can hold both,
        # and Oanda closes it on whichever triggers first.
        if trail_distance is not None:
            try:
                trail_transaction_id = client.attach_trailing_stop(
                    fill.trade_id, trail_distance
                )
            except Exception as exc:
                trail_error = str(exc)

    sync_rows_ingested = 0
    sync_error: str | None = None
    try:
        sync_result = sync_incremental(conn, client)
        sync_rows_ingested = sync_result.rows_ingested
    except Exception as exc:
        sync_error = str(exc)

    _save_trade_plan(
        conn, fill.transaction_id, client.account_id, tp_price, sl_price, trail_pips
    )

    return PostFillResult(
        missing_trade_id=missing_trade_id,
        tp_transaction_id=tp_transaction_id,
        tp_error=tp_error,
        sl_transaction_id=sl_transaction_id,
        sl_error=sl_error,
        sync_rows_ingested=sync_rows_ingested,
        sync_error=sync_error,
        trail_transaction_id=trail_transaction_id,
        trail_error=trail_error,
    )


@dataclass(frozen=True, slots=True)
class PostLimitResult:
    """Outcome of syncing and persisting the trade plan after a limit order.

    ``journal_oanda_id`` is the Oanda transaction the entry's note, tags, and
    trade plan belong to: the ORDER_FILL when the order filled on arrival,
    otherwise the LIMIT_ORDER transaction that created it (whose ID is the
    order ID). As with ``PostFillResult``, a sync failure is reported here
    rather than raised, since the order itself was accepted.
    """

    journal_oanda_id: str
    sync_rows_ingested: int
    sync_error: str | None


def execute_post_limit(
    conn: sqlite3.Connection,
    client: OandaClient,
    result: LimitOrderResult,
    tp_price: Decimal | None,
    sl_price: Decimal | None,
    trail_pips: Decimal | None = None,
) -> PostLimitResult:
    """Sync a just-placed limit order into the local DB and persist its
    trade plan.

    Unlike ``execute_post_fill`` there is no TP/SL attach step: the limit
    order carried TP/SL (and any trailing stop) as ``takeProfitOnFill``/
    ``stopLossOnFill``/``trailingStopLossOnFill``, so Oanda
    applies them itself when the order fills — including an immediate fill.
    Attaching them again would fail against the TP/SL Oanda already set.
    """
    # Key the journal on the fill if there is one; otherwise on the order.
    journal_oanda_id = (
        result.fill.transaction_id if result.fill is not None else result.order_id
    )

    sync_rows_ingested = 0
    sync_error: str | None = None
    try:
        sync_result = sync_incremental(conn, client)
        sync_rows_ingested = sync_result.rows_ingested
    except Exception as exc:
        sync_error = str(exc)

    _save_trade_plan(
        conn, journal_oanda_id, client.account_id, tp_price, sl_price, trail_pips
    )

    return PostLimitResult(
        journal_oanda_id=journal_oanda_id,
        sync_rows_ingested=sync_rows_ingested,
        sync_error=sync_error,
    )


# ---------------------------------------------------------------------------
# positions command: live trade + quote fetch
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class PositionsView:
    """Live open trades, pending entry orders, and account summary for the
    ``positions`` command.

    ``quotes`` holds one live quote per instrument with an open trade or a
    pending order. ``pending_orders`` is ``None`` (with ``pending_error``
    set) when the pending-orders fetch failed, so the caller can say so
    rather than implying there are none.
    """

    trades: list[OpenTrade]
    summary: AccountSummary
    quotes: dict[str, PriceQuote]
    financing_rates: dict[str, FinancingRate]
    pending_orders: list[PendingOrder] | None
    pending_error: str | None


def fetch_positions_view(client: OandaClient) -> PositionsView:
    """Fetch open trades, pending entry orders, account summary, one live
    quote per instrument, and financing rates for open instruments (so the
    caller can show projected dollar P/L at each exit level, the estimated
    daily financing charge for each position, and how far each pending
    order is from the market).

    Everything after the trades and summary is best-effort: a failed quote
    or financing fetch just means that instrument displays without those
    figures, and a failed pending-orders fetch is reported in
    ``pending_error``, rather than failing the whole command.
    """
    trades = client.get_open_trades()
    summary = client.get_account_summary()

    pending_orders: list[PendingOrder] | None
    pending_error: str | None = None
    try:
        pending_orders = client.get_pending_orders()
    except Exception as exc:
        pending_orders = None
        pending_error = str(exc)

    instruments = {trade.instrument for trade in trades}
    # Pending orders need a quote too (to show the current price), but not a
    # financing rate — they aren't paying financing yet.
    quote_instruments = instruments | {o.instrument for o in pending_orders or []}

    quotes: dict[str, PriceQuote] = {}
    for instrument in quote_instruments:
        try:
            quotes[instrument] = client.get_price(instrument)
        except Exception:
            pass

    financing_rates: dict[str, FinancingRate] = {}
    if instruments:
        try:
            for rate in client.get_financing_rates(list(instruments)):
                financing_rates[rate.instrument] = rate
        except Exception:
            pass

    return PositionsView(
        trades=trades,
        summary=summary,
        quotes=quotes,
        financing_rates=financing_rates,
        pending_orders=pending_orders,
        pending_error=pending_error,
    )


# ---------------------------------------------------------------------------
# close command: close tickets + sync
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class CloseTicketResult:
    """Outcome of closing a single open ticket."""

    trade_id: str
    close_price: Decimal | None
    realised_pl: Decimal | None
    transaction_id: str | None
    error: str | None


@dataclass(frozen=True, slots=True)
class CloseResult:
    """Outcome of closing every requested ticket, plus the post-close sync."""

    ticket_results: list[CloseTicketResult]
    sync_rows_ingested: int
    sync_error: str | None


def execute_close(
    conn: sqlite3.Connection, client: OandaClient, trades: list[OpenTrade]
) -> CloseResult:
    """Close every ticket in *trades* and, if at least one closed
    successfully, run an incremental sync so the local journal reflects the
    closing transactions immediately.

    A ticket that fails to close is recorded in its ``CloseTicketResult``
    rather than raised, so one failure doesn't stop the remaining tickets
    from being closed.
    """
    ticket_results: list[CloseTicketResult] = []
    closed = 0
    for t in trades:
        try:
            result = client.close_trade(t.trade_id)
            ticket_results.append(
                CloseTicketResult(
                    trade_id=t.trade_id,
                    close_price=result.close_price,
                    realised_pl=result.realised_pl,
                    transaction_id=result.transaction_id,
                    error=None,
                )
            )
            closed += 1
        except Exception as exc:
            ticket_results.append(
                CloseTicketResult(
                    trade_id=t.trade_id,
                    close_price=None,
                    realised_pl=None,
                    transaction_id=None,
                    error=str(exc),
                )
            )

    sync_rows_ingested = 0
    sync_error: str | None = None
    if closed:
        try:
            sync_result = sync_incremental(conn, client)
            sync_rows_ingested = sync_result.rows_ingested
        except Exception as exc:
            sync_error = str(exc)

    return CloseResult(
        ticket_results=ticket_results,
        sync_rows_ingested=sync_rows_ingested,
        sync_error=sync_error,
    )
