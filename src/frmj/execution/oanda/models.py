"""Dataclasses returned by the Oanda client and its parsing helpers.

These are the currency in which ``ClientProtocol`` deals — they are equally
produced by the real ``OandaClient`` (see ``client.py``) and by test doubles,
so changing their fields is a breaking change to both.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal


@dataclass(frozen=True, slots=True)
class TransactionRow:
    """
    One Oanda transaction, parsed and ready for insertion into ``transactions``.

    Fields
    ------
    oanda_id:
        Oanda's own transaction ID (a numeric string). Stored as TEXT so we
        never do arithmetic on it; the sync layer uses it only for
        deduplication and cursor tracking.
    account_id:
        The Oanda account this transaction belongs to.
    type:
        Oanda's transaction type string, e.g. ``"ORDER_FILL"``,
        ``"DAILY_FINANCING"``. Stored verbatim — we do not map to an Enum
        so that new types from Oanda don't require a code change.
    time:
        ISO-8601 timestamp from Oanda's ``time`` field, verbatim.
    parent_oanda_id:
        For DAILY_FINANCING children: the Oanda ID of the parent
        transaction in the same financing batch. ``None`` for everything
        else. The sync layer resolves this to a SQLite synthetic FK.
    raw_json:
        Compact JSON string of the full Oanda transaction object. Stored
        verbatim so we can add new parsed columns later without re-syncing.
    """

    oanda_id: str
    account_id: str
    type: str
    time: str
    parent_oanda_id: str | None
    raw_json: str


@dataclass(frozen=True, slots=True)
class OpenTrade:
    """One open trade as returned by GET /accounts/{id}/openTrades.

    ``direction`` is ``"LONG"`` or ``"SHORT"``.  ``units`` is always positive —
    direction is carried separately so callers never have to check sign.

    ``take_profit_price`` and ``stop_loss_price`` are ``None`` when no
    corresponding order is attached to the trade. Likewise for a trailing
    stop: ``trailing_stop_price`` is its current trigger price (Oanda's
    ``trailingStopValue``, which moves as the trade goes its way) and
    ``trailing_stop_distance`` how far behind the price it trails, in
    price units.

    ``open_time`` is the ISO-8601 timestamp from Oanda verbatim; the display
    layer trims it to seconds.
    """

    trade_id: str
    instrument: str
    direction: str
    units: int
    open_price: Decimal
    unrealised_pl: Decimal
    margin_used: Decimal
    take_profit_price: Decimal | None
    stop_loss_price: Decimal | None
    open_time: str
    trailing_stop_price: Decimal | None = None
    trailing_stop_distance: Decimal | None = None


@dataclass(frozen=True, slots=True)
class AccountSummary:
    """Account-level snapshot returned by GET /accounts/{id}/summary.

    ``nav`` (net asset value) is what the risk model calls *equity* — the
    total account value including unrealised P/L on open positions.

    ``margin_available`` is the margin currently available to open new
    positions. This is what the sizing model's safety-reserve calculation
    works from, not the raw NAV.

    ``open_trade_count`` is the total number of open tickets across all
    instruments, per Oanda's own count. We use it as the risk model's N.

    ``margin_closeout_percent`` is Oanda's margin closeout ratio as a
    fraction: at ``1`` or above the account is in margin closeout and Oanda
    starts closing positions. The Oanda web platform shows it as a percent.
    """

    nav: Decimal
    balance: Decimal
    unrealized_pl: Decimal
    realized_pl: Decimal
    position_value: Decimal
    margin_used: Decimal
    margin_available: Decimal
    open_trade_count: int
    margin_closeout_percent: Decimal


@dataclass(frozen=True, slots=True)
class FinancingRate:
    """Long/short financing rate for one instrument, from the ``financing``
    block of GET /accounts/{id}/instruments.

    Oanda quotes ``long_rate``/``short_rate`` as annualized decimal fractions
    (``Decimal("-0.03")`` means -3.00%/year) that it republishes daily — this
    is the same convention Oanda's own site uses for what it calls "daily
    financing rates" (the *rate* is annualized; it is the *publication* that
    is daily). A negative rate means you pay to hold that side overnight; a
    positive rate means you're paid.
    """

    instrument: str
    long_rate: Decimal
    short_rate: Decimal


@dataclass(frozen=True, slots=True)
class OrderFill:
    """Result of a successfully filled market order.

    ``transaction_id`` is Oanda's fill-transaction ID.  We attach the user's
    optional note to this ID via the ``notes`` table.

    ``fill_price`` is the actual execution price reported by Oanda.

    ``units_filled`` is signed: positive for long fills, negative for short.

    ``trade_id`` is the Oanda trade ID from ``tradeOpened.tradeID`` in the fill
    response.  Used to attach TP/SL orders to the newly-opened position.
    ``None`` in the rare case where the fill did not open a new trade (e.g.
    a partial close that is modelled as a fill — not currently reachable via the
    CLI, but defended against so callers don't have to guess).
    """

    transaction_id: str
    fill_price: Decimal
    units_filled: int
    trade_id: str | None = None


@dataclass(frozen=True, slots=True)
class LimitOrderResult:
    """Result of placing a limit (pending entry) order.

    ``order_id`` is the ID of the created order. In Oanda it is also the ID
    of the LIMIT_ORDER transaction that created it, which is what journal
    notes, tags, and the trade plan attach to until the order fills.

    ``fill`` is set only when the order filled as soon as it arrived (the
    market had already crossed the limit price by then). It is ``None`` in
    the normal case, where the order rests until price reaches it.
    """

    order_id: str
    fill: OrderFill | None = None


@dataclass(frozen=True, slots=True)
class PendingOrder:
    """One pending entry order from GET /accounts/{id}/pendingOrders.

    Only entry orders (LIMIT, STOP, MARKET_IF_TOUCHED) become a
    ``PendingOrder``. The TAKE_PROFIT/STOP_LOSS orders attached to open
    trades are also "pending" to Oanda, but they can't open a new position.

    ``direction`` is ``"LONG"`` or ``"SHORT"`` and ``units`` is always
    positive, matching ``OpenTrade``. ``take_profit_price`` and
    ``stop_loss_price`` come from the order's ``takeProfitOnFill`` /
    ``stopLossOnFill`` and are ``None`` when not set;
    ``trailing_stop_distance`` (price units) likewise comes from
    ``trailingStopLossOnFill``.
    """

    order_id: str
    order_type: str
    instrument: str
    direction: str
    units: int
    price: Decimal
    time_in_force: str
    create_time: str
    take_profit_price: Decimal | None
    stop_loss_price: Decimal | None
    trailing_stop_distance: Decimal | None = None


@dataclass(frozen=True, slots=True)
class CloseFill:
    """Result of closing an open trade via PUT /trades/{id}/close.

    ``transaction_id`` is Oanda's closing fill-transaction ID.

    ``close_price`` is the execution price at which the trade was closed.

    ``realised_pl`` is the net profit or loss on the trade in home currency,
    as reported by Oanda's ``pl`` field.  Negative for a losing trade.
    """

    transaction_id: str
    close_price: Decimal
    realised_pl: Decimal
