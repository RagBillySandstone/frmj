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
``fetch_market_context`` + ``evaluate_trade_risk`` cover the market-data and
risk-check steps of the trade flow, ``execute_post_fill`` covers the
TP/SL-attach + sync + persist steps after an order is placed, and
``fetch_positions_view`` / ``execute_close`` cover the ``positions`` and
``close`` commands respectively. Order placement itself (with its
retry/save/abort prompt) stays in ``cli.py`` because the retry decision is
inherently interactive.
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
from frmj.domain.sizing import Direction, InstrumentSpec, PriceQuote
from frmj.execution.oanda import (
    AccountSummary,
    FinancingRate,
    OandaClient,
    OpenTrade,
    OrderFill,
)
from frmj.execution.sync import sync_incremental

# ---------------------------------------------------------------------------
# Trade planning: market data + risk/correlation evaluation
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class MarketContext:
    """Live account and instrument state needed to plan a trade."""

    summary: AccountSummary
    open_tickets_on_instrument: int
    spec: InstrumentSpec
    quote: PriceQuote
    open_trades: list[OpenTrade]
    financing_rate: FinancingRate | None


def fetch_market_context(client: OandaClient, instrument: str) -> MarketContext:
    """Fetch the account summary, open positions, instrument spec, live
    quote, and financing rate needed to plan a trade on *instrument*.

    The financing-rate fetch is best-effort: Oanda's instruments endpoint can
    fail independently of the rest, and a missing financing rate only means
    the trade-plan display omits that one line, so a failure there is
    swallowed rather than propagated.
    """
    summary = client.get_account_summary()
    open_tickets_on_instrument = client.get_open_tickets_on_instrument(instrument)
    spec = client.get_instrument(instrument)
    quote = client.get_price(instrument)
    open_trades = client.get_open_trades()
    try:
        financing_rate: FinancingRate | None = client.get_financing_rates([instrument])[
            0
        ]
    except Exception:
        financing_rate = None
    return MarketContext(
        summary=summary,
        open_tickets_on_instrument=open_tickets_on_instrument,
        spec=spec,
        quote=quote,
        open_trades=open_trades,
        financing_rate=financing_rate,
    )


@dataclass(frozen=True, slots=True)
class RiskEvaluation:
    """Result of running the max-trades/sizing and correlation checks."""

    sizing_decision: SizingDecision
    correlation_warnings: tuple[str, ...]


def evaluate_trade_risk(
    risk_config: RiskConfig,
    context: MarketContext,
    instrument: str,
    direction: Direction,
) -> RiskEvaluation:
    """Run the max-open-trades/sizing check and the correlated-position check
    for a trade on *instrument*/*direction* given already-fetched *context*.

    Raises ``MaxTradesExceeded`` or ``ScaleInForbidden`` (from
    ``evaluate_trade``) and ``CorrelatedPositionForbidden`` (from
    ``evaluate_correlation``) when the configured blocking mode forbids the
    trade — callers should catch these and surface them as user-facing
    errors rather than tracebacks.
    """
    sizing_decision = evaluate_trade(
        config=risk_config,
        open_trades=context.summary.open_trade_count,
        open_tickets_on_instrument=context.open_tickets_on_instrument,
        available_margin=context.summary.margin_available,
        equity=context.summary.nav,
    )
    correlation_warnings = evaluate_correlation(
        open_positions=[(t.instrument, t.direction) for t in context.open_trades],
        new_instrument=instrument,
        new_direction=direction,
        blocking_mode=risk_config.correlation_blocking_mode,
    )
    return RiskEvaluation(
        sizing_decision=sizing_decision, correlation_warnings=correlation_warnings
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


def execute_post_fill(
    conn: sqlite3.Connection,
    client: OandaClient,
    fill: OrderFill,
    tp_price: Decimal | None,
    sl_price: Decimal | None,
) -> PostFillResult:
    """Attach TP/SL to a filled trade, sync the fill into the local DB, and
    persist the trade plan.

    ``missing_trade_id`` is set when Oanda didn't return a trade ID and at
    least one of *tp_price*/*sl_price* was requested (so TP/SL could not be
    attached at all). TP/SL attach failures and sync failures are captured in
    the result fields rather than raised.
    """
    missing_trade_id = False
    tp_transaction_id: str | None = None
    tp_error: str | None = None
    sl_transaction_id: str | None = None
    sl_error: str | None = None

    if fill.trade_id is None:
        missing_trade_id = tp_price is not None or sl_price is not None
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

    sync_rows_ingested = 0
    sync_error: str | None = None
    try:
        sync_result = sync_incremental(conn, client)
        sync_rows_ingested = sync_result.rows_ingested
    except Exception as exc:
        sync_error = str(exc)

    _save_trade_plan(conn, fill.transaction_id, client.account_id, tp_price, sl_price)

    return PostFillResult(
        missing_trade_id=missing_trade_id,
        tp_transaction_id=tp_transaction_id,
        tp_error=tp_error,
        sl_transaction_id=sl_transaction_id,
        sl_error=sl_error,
        sync_rows_ingested=sync_rows_ingested,
        sync_error=sync_error,
    )


# ---------------------------------------------------------------------------
# positions command: live trade + quote fetch
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class PositionsView:
    """Live open trades and account summary for the ``positions`` command."""

    trades: list[OpenTrade]
    summary: AccountSummary
    quote_to_home: dict[str, Decimal]


def fetch_positions_view(client: OandaClient) -> PositionsView:
    """Fetch open trades, account summary, and one live quote per instrument
    that has a TP or SL set (so the caller can show projected dollar P/L at
    each exit level).

    The per-instrument quote fetch is best-effort: a failed fetch just means
    that instrument's trades display without a projected-P/L figure, rather
    than failing the whole command.
    """
    trades = client.get_open_trades()
    summary = client.get_account_summary()

    quote_to_home: dict[str, Decimal] = {}
    for trade in trades:
        if trade.instrument in quote_to_home:
            continue
        if trade.take_profit_price is None and trade.stop_loss_price is None:
            continue
        try:
            quote_to_home[trade.instrument] = client.get_price(
                trade.instrument
            ).quote_to_home
        except Exception:
            pass

    return PositionsView(trades=trades, summary=summary, quote_to_home=quote_to_home)


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
