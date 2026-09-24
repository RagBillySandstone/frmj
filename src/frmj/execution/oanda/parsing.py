"""Pure functions that turn raw Oanda API dicts into our own types.

Separating parsing from HTTP means tests can feed sample dicts without
spinning up an HTTP server, while ``OandaClient`` methods (``client.py``)
stay thin.
"""

from __future__ import annotations

import json
from dataclasses import replace
from decimal import Decimal
from typing import Any

from frmj.domain.sizing import InstrumentSpec

from .models import (
    AccountSummary,
    CloseFill,
    FinancingRate,
    OpenTrade,
    OrderFill,
    PendingOrder,
    TransactionRow,
)

# ---------------------------------------------------------------------------
# Response parsing
# ---------------------------------------------------------------------------


def _parse_close_fill(payload: dict[str, Any]) -> CloseFill:
    """Parse PUT /trades/{id}/close response into a CloseFill.

    Oanda returns the closing fill under ``orderFillTransaction``.  The ``pl``
    field is the net realised P/L for this trade in the account's home currency.
    """
    fill = payload["orderFillTransaction"]
    return CloseFill(
        transaction_id=str(fill["id"]),
        close_price=Decimal(fill["price"]),
        realised_pl=Decimal(fill["pl"]),
    )


def _parse_order_fill(fill: dict[str, Any]) -> OrderFill:
    """Parse an ``orderFillTransaction`` for an order that opened a trade.

    ``tradeOpened`` is absent when the fill only reduced or closed existing
    trades, in which case ``trade_id`` is ``None`` (see ``OrderFill``).
    """
    trade_opened = fill.get("tradeOpened")
    return OrderFill(
        transaction_id=str(fill["id"]),
        fill_price=Decimal(fill["price"]),
        units_filled=int(Decimal(fill["units"])),
        trade_id=str(trade_opened["tradeID"]) if trade_opened else None,
    )


def _parse_open_trade(trade: dict[str, Any]) -> OpenTrade:
    """Parse one element of the ``trades`` array from GET /openTrades.

    ``currentUnits`` is signed (positive=long, negative=short); we normalise to
    a direction string + positive unit count so callers never have to check sign.

    ``takeProfitOrder`` and ``stopLossOrder`` are optional keys — absent when no
    exit order is attached.
    """
    units_raw = int(Decimal(trade["currentUnits"]))
    tp_order = trade.get("takeProfitOrder")
    sl_order = trade.get("stopLossOrder")
    return OpenTrade(
        trade_id=str(trade["id"]),
        instrument=trade["instrument"],
        direction="LONG" if units_raw >= 0 else "SHORT",
        units=abs(units_raw),
        open_price=Decimal(trade["price"]),
        unrealised_pl=Decimal(trade["unrealizedPL"]),
        margin_used=Decimal(trade["marginUsed"]),
        take_profit_price=Decimal(tp_order["price"]) if tp_order else None,
        stop_loss_price=Decimal(sl_order["price"]) if sl_order else None,
        open_time=trade["openTime"],
    )


def _parse_pending_order(order: dict[str, Any]) -> PendingOrder:
    """Parse one entry order from the ``orders`` array of GET /pendingOrders.

    Callers filter out non-entry order types first; see ``PendingOrder``.
    ``units`` is signed in Oanda's payload and normalised here to a direction
    string plus a positive count, as ``_parse_open_trade`` does.
    """
    units_raw = int(Decimal(order["units"]))
    tp_on_fill = order.get("takeProfitOnFill")
    sl_on_fill = order.get("stopLossOnFill")
    return PendingOrder(
        order_id=str(order["id"]),
        order_type=order["type"],
        instrument=order["instrument"],
        direction="LONG" if units_raw >= 0 else "SHORT",
        units=abs(units_raw),
        price=Decimal(order["price"]),
        time_in_force=order["timeInForce"],
        create_time=order["createTime"],
        take_profit_price=Decimal(tp_on_fill["price"]) if tp_on_fill else None,
        stop_loss_price=Decimal(sl_on_fill["price"]) if sl_on_fill else None,
    )


def _parse_account_summary(payload: dict[str, Any]) -> AccountSummary:
    """Parse GET /accounts/{id}/summary response."""
    acct = payload["account"]
    return AccountSummary(
        nav=Decimal(acct["NAV"]),
        balance=Decimal(acct["balance"]),
        unrealized_pl=Decimal(acct.get("unrealizedPL", "0")),
        realized_pl=Decimal(acct.get("pl", "0")),
        position_value=Decimal(acct.get("positionValue", "0")),
        margin_used=Decimal(acct.get("marginUsed", "0")),
        margin_available=Decimal(acct["marginAvailable"]),
        open_trade_count=int(acct["openTradeCount"]),
        margin_closeout_percent=Decimal(acct.get("marginCloseoutPercent", "0")),
    )


def _parse_order_create_txn_id(payload: dict[str, Any]) -> str:
    """Extract the transaction ID from a POST /orders success response.

    Oanda wraps the created-order transaction under ``orderCreateTransaction``.
    Returns its ``id`` as a string.  Raises ``RuntimeError`` when the key is
    absent — that would mean an undocumented response shape and should surface
    loudly rather than silently swallowing.
    """
    txn = payload.get("orderCreateTransaction")
    if txn is None:
        raise RuntimeError(
            f"No orderCreateTransaction in Oanda response: {json.dumps(payload)}"
        )
    return str(txn["id"])


def _parse_instrument_spec(instr: dict[str, Any]) -> InstrumentSpec:
    """Parse one element of the ``instruments`` array from GET /instruments.

    ``units_increment`` defaults to 1 because Oanda FX pairs accept any
    integer unit count.  The API does not expose a dedicated increment field
    for FX; ``tradeUnitsPrecision == 0`` means whole units only, which maps
    to increment = 1. Instruments with non-standard increments (some metals /
    CFDs) will need explicit overrides — add them when we encounter them.

    ``min_units`` comes from Oanda's ``minimumTradeSize`` (a string like
    ``"1"``).  We convert via Decimal to handle any decimal-valued minimums
    safely before truncating to int.

    ``display_precision`` comes from Oanda's ``displayPrecision`` — the
    number of decimal places the API accepts for prices on this instrument.
    We need it to quantize TP/SL prices before submitting them; sending a
    price with more decimals than Oanda expects is rejected with a 400.

    The trailing-stop distance bounds are optional in the payload; a missing
    field leaves the bound as ``None`` (unchecked locally).
    """
    # Read the optional trailing-stop bounds without assuming they exist.
    min_trail = instr.get("minimumTrailingStopDistance")
    max_trail = instr.get("maximumTrailingStopDistance")
    return InstrumentSpec(
        name=instr["name"],
        pip_location=int(instr["pipLocation"]),
        margin_rate=Decimal(instr["marginRate"]),
        min_units=int(Decimal(instr["minimumTradeSize"])),
        units_increment=1,
        display_precision=int(instr["displayPrecision"]),
        min_trailing_stop_distance=Decimal(min_trail) if min_trail else None,
        max_trailing_stop_distance=Decimal(max_trail) if max_trail else None,
    )


def _parse_financing_rate(instr: dict[str, Any]) -> FinancingRate:
    """Parse one element of the ``instruments`` array into a ``FinancingRate``.

    Pulls only the ``financing`` sub-object; the rest of *instr* (margin
    rate, pip location, etc.) is handled by ``_parse_instrument_spec``.
    """
    financing = instr["financing"]
    return FinancingRate(
        instrument=instr["name"],
        long_rate=Decimal(financing["longRate"]),
        short_rate=Decimal(financing["shortRate"]),
    )


def _extract_bid_ask(payload: dict[str, Any]) -> tuple[Decimal, Decimal]:
    """Pull the best bid and ask from a GET /pricing response.

    Oanda returns ``bids`` and ``asks`` as arrays (multiple liquidity bands).
    Index 0 is always the best (tightest) price — the one we would receive
    for a market order of typical size.
    """
    price_data = payload["prices"][0]
    bid = Decimal(price_data["bids"][0]["price"])
    ask = Decimal(price_data["asks"][0]["price"])
    return bid, ask


# ---------------------------------------------------------------------------
# Conversion rates and parent/child resolution (pure — tested directly)
# ---------------------------------------------------------------------------


def _compute_conversion_rate(
    currency: str,
    home: str,
    mids: dict[str, Decimal],
) -> Decimal:
    """Pure: convert one unit of *currency* into *home* currency.

    Consults *mids* (a ``{instrument_name: mid_price}`` dict) to find the
    rate.  Tries the direct quote ``{currency}_{home}`` first; falls back to
    the inverted quote ``{home}_{currency}``.  Returns ``Decimal("1")`` when
    ``currency == home`` — no lookup required.

    Raises ``ValueError`` when neither pair is present in *mids*.  The HTTP
    layer (``OandaClient._currency_to_home``) is responsible for populating
    the dict before calling this function.
    """
    if currency == home:
        return Decimal("1")
    direct = f"{currency}_{home}"
    if direct in mids:
        return mids[direct]
    inverted = f"{home}_{currency}"
    if inverted in mids:
        return Decimal("1") / mids[inverted]
    raise ValueError(
        f"Cannot convert {currency} to {home}: "
        f"neither {direct} nor {inverted} in provided mid prices"
    )


def _resolve_financing_parents(rows: list[TransactionRow]) -> list[TransactionRow]:
    """Stamp ``parent_oanda_id`` on DAILY_FINANCING child rows.

    Oanda emits each DAILY_FINANCING batch as one summary parent (which
    carries ``relatedTransactionIDs`` listing its per-instrument children)
    followed by the children themselves.  The children have no back-reference.

    We build a ``{child_oanda_id: parent_oanda_id}`` map from every parent in
    *rows*, then return a new list where each child row has its
    ``parent_oanda_id`` set.  All other rows are returned unchanged.

    The fast path (no DAILY_FINANCING parents in the batch) returns *rows*
    unmodified so callers bear zero overhead on typical batches that contain
    only trade transactions.

    Cross-batch case: if a parent arrived in a prior sync run it will not be
    in *rows*, so its children's ``parent_oanda_id`` will remain ``None`` here.
    The sync layer's ``_resolve_parent_id`` DB lookup handles that case — it
    finds the parent's synthetic id from the transactions table.
    """
    child_to_parent: dict[str, str] = {}
    for row in rows:
        if row.type != "DAILY_FINANCING":
            continue
        raw: dict[str, Any] = json.loads(row.raw_json)
        for child_id in raw.get("relatedTransactionIDs", []):
            child_to_parent[str(child_id)] = row.oanda_id

    if not child_to_parent:
        return rows

    return [
        replace(row, parent_oanda_id=child_to_parent[row.oanda_id])
        if row.oanda_id in child_to_parent
        else row
        for row in rows
    ]
