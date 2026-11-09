"""Thin httpx wrapper for the Oanda v20 REST API.

Endpoints implemented
---------------------
  GET  /accounts/{id}/transactions              (cold sync — pages index)
  GET  /accounts/{id}/transactions/sinceid      (incremental sync)
  GET  /accounts/{id}/summary                  (NAV, margin, open trade count)
  GET  /accounts/{id}/instruments              (InstrumentSpec / financing rates)
  GET  /accounts/{id}/pricing                  (live bid/ask + conversions)
  GET  /instruments/{name}/candles             (daily candles for ATR)
  GET  /instruments/{name}/orderBook           (order book snapshot)
  GET  /instruments/{name}/positionBook        (position book snapshot)
  GET  /accounts/{id}/trades                   (open ticket count per instrument)
  GET  /accounts/{id}/pendingOrders            (pending entry orders)
  POST /accounts/{id}/orders                   (place market / limit order)
  GET  /accounts/{id}/transactions/stream       (live transaction stream, on
                                                 the separate stream host)

We deliberately avoid the official v20 Python SDK so that:

  * We stay in full control of timeout and retry policy (no hidden waits).
  * The dependency surface stays tiny (httpx only).
  * We can add new fields to the parsed output without waiting on SDK updates.

Timeout configuration
---------------------
    connect = 5 s   fail fast if the network / DNS is broken
    read    = 15 s  give Oanda time to assemble a 1000-row cold page
    write   = 5 s   request upload; 5 s is generous for our tiny payloads
    pool    = 5 s   wait for a connection slot in the pool

Authentication
--------------
``Bearer {token}`` in the ``Authorization`` header. The token is supplied
at construction time — we never read the environment here. The CLI layer
reads ``OANDA_API_TOKEN`` and passes it in.

Base URLs
---------
    Practice : https://api-fxpractice.oanda.com/v3
    Live     : https://api-fxtrade.oanda.com/v3

Streaming endpoints live on their own hosts:

    Practice : https://stream-fxpractice.oanda.com/v3
    Live     : https://stream-fxtrade.oanda.com/v3

Retry policy
------------
None — deliberately. If a request fails we let the exception propagate to
the CLI, which can offer "retry / save plan / abort" options. Silent
automatic retries inside the client would obscure network problems and make
the "abort" path unreachable.
"""

from __future__ import annotations

import json
import re
from collections.abc import Generator, Iterator
from datetime import datetime, timezone
from decimal import Decimal
from typing import Any, Protocol

from frmj.domain.pricing import Candle
from frmj.domain.sizing import InstrumentSpec, PriceQuote

from .models import (
    AccountSummary,
    Book,
    CloseFill,
    FinancingRate,
    LimitOrderResult,
    OpenTrade,
    OrderFill,
    PendingOrder,
    TransactionRow,
)
from .parsing import (
    _compute_conversion_rate,
    _extract_bid_ask,
    _parse_account_summary,
    _parse_book,
    _parse_candles,
    _parse_close_fill,
    _parse_financing_rate,
    _parse_instrument_spec,
    _parse_open_trade,
    _parse_order_create_txn_id,
    _parse_order_fill,
    _parse_pending_order,
    _resolve_financing_parents,
)

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

PRACTICE_BASE_URL: str = "https://api-fxpractice.oanda.com/v3"
LIVE_BASE_URL: str = "https://api-fxtrade.oanda.com/v3"
PRACTICE_STREAM_URL: str = "https://stream-fxpractice.oanda.com/v3"
LIVE_STREAM_URL: str = "https://stream-fxtrade.oanda.com/v3"

# Oanda's documented per-call limit for the /sinceid endpoint.  When a
# response contains exactly this many rows there may be more; we loop.
_SINCEID_PAGE_LIMIT: int = 500

_CONNECT_TIMEOUT: float = 5.0  # seconds — fail fast on broken network
_READ_TIMEOUT: float = 15.0  # seconds — cold pages can be large
_WRITE_TIMEOUT: float = 5.0  # seconds — tiny uploads
_POOL_TIMEOUT: float = 5.0  # seconds — connection pool wait
# Oanda sends a stream heartbeat every 5 s; four missed in a row means the
# connection is dead and should be dropped (the caller reconnects).
_STREAM_READ_TIMEOUT: float = 20.0

# Pending order types that open a position when triggered. Everything else
# in /pendingOrders (TAKE_PROFIT, STOP_LOSS, TRAILING_STOP_LOSS, ...) is an
# exit order attached to an existing trade.
_ENTRY_ORDER_TYPES: frozenset[str] = frozenset({"LIMIT", "STOP", "MARKET_IF_TOUCHED"})


# Values placed in a URL path (account ID, instrument, trade ID). Oanda's own
# IDs use only these characters; anything else — "/", "..", "?", "#" — could
# point the request at a different endpoint than the one intended.
_PATH_SEGMENT_RE: re.Pattern[str] = re.compile(r"[A-Za-z0-9_-]+")


def _path_segment(value: str, what: str) -> str:
    """Return *value* if it is safe to place in a URL path, else raise.

    *what* names the value in the ``ValueError`` message, e.g. "trade ID".
    """
    if not _PATH_SEGMENT_RE.fullmatch(value):
        raise ValueError(f"Invalid {what}: {value!r}")
    return value


# ---------------------------------------------------------------------------
# Protocol — the abstraction boundary
# ---------------------------------------------------------------------------


class ClientProtocol(Protocol):
    """
    Structural interface for any object that can supply transaction rows.

    The sync functions in ``sync.py`` accept a ``ClientProtocol``, not a
    concrete ``OandaClient``.  Any class that has an ``account_id`` attribute
    and a ``get_transactions_since`` method with this exact signature satisfies
    the Protocol — no inheritance required.

    This lets test doubles be plain dataclasses or simple classes with no
    shared ancestor. It also means adding a new implementation (e.g. a CSV
    importer) requires zero changes to the sync layer.
    """

    account_id: str

    def get_transactions_since(
        self,
        from_id: str | None = None,
    ) -> list[TransactionRow]:
        """
        Return all transactions after *from_id* (exclusive), or all
        transactions ever if *from_id* is ``None``.

        Callers must not assume anything about the order within the returned
        list beyond: if two transactions have a parent/child relationship, the
        parent appears before its children (Oanda guarantees this in its own
        delivery order, and implementations must preserve it).

        On any network or API error, raises rather than returning a partial
        list.  Retry decisions belong to the caller.
        """
        ...


class StreamingClientProtocol(ClientProtocol, Protocol):
    """A ``ClientProtocol`` that can also stream transactions as they happen.

    Needed by ``follow_transactions`` (``sync.py``), which syncs on each
    streamed event; ``OandaClient`` satisfies it.
    """

    def stream_transactions(self) -> Iterator[dict[str, Any]]:
        """Yield each message on the account's transaction stream, in order.

        Messages are Oanda's JSON objects: transactions, plus a
        ``{"type": "HEARTBEAT", "lastTransactionID": ...}`` every few
        seconds. Blocks between messages; raises when the connection fails
        or goes quiet for too long, and never retries itself.
        """
        ...


# ---------------------------------------------------------------------------
# Real HTTP client
# ---------------------------------------------------------------------------


class OandaClient:
    """
    HTTP client for the Oanda v3 REST API.

    Instantiate once and either use as a context manager (recommended for CLI
    flows) or call ``.close()`` explicitly when done.

    Usage::

        with OandaClient(token=token, account_id=account_id) as client:
            rows = client.get_transactions_since()
    """

    def __init__(
        self,
        *,
        token: str,
        account_id: str,
        practice: bool = True,
    ) -> None:
        # httpx is imported here rather than at module level: it costs ~50ms
        # to import, and commands that never build a client shouldn't pay it.
        import httpx

        self.account_id = _path_segment(account_id, "Oanda account ID")
        self._base_url = PRACTICE_BASE_URL if practice else LIVE_BASE_URL
        self._stream_url = PRACTICE_STREAM_URL if practice else LIVE_STREAM_URL
        self._http = httpx.Client(
            headers={"Authorization": f"Bearer {token}"},
            timeout=httpx.Timeout(
                connect=_CONNECT_TIMEOUT,
                read=_READ_TIMEOUT,
                write=_WRITE_TIMEOUT,
                pool=_POOL_TIMEOUT,
            ),
        )

    # ------------------------------------------------------------------
    # Public interface (satisfies ClientProtocol)
    # ------------------------------------------------------------------

    def get_transactions_since(
        self,
        from_id: str | None = None,
    ) -> list[TransactionRow]:
        """
        Fetch all transactions after *from_id*, handling Oanda pagination.

        When *from_id* is ``None`` (cold sync), fetches the full account
        history via the paginated ``/transactions`` endpoint.

        When *from_id* is provided (incremental sync), uses the faster
        ``/transactions/sinceid`` endpoint, looping until a sub-limit
        response signals no more data.

        Raises ``httpx.HTTPStatusError`` on 4xx / 5xx responses.
        Raises ``httpx.TimeoutException`` if a request exceeds the timeout.
        """
        rows = self._fetch_all_cold() if from_id is None else self._fetch_since(from_id)
        return _resolve_financing_parents(rows)

    def get_account_summary(self) -> AccountSummary:
        """Fetch NAV, available margin, and open trade count.

        Called at the start of the trade flow so the risk model has fresh
        account state. Raises ``httpx.HTTPStatusError`` on Oanda errors.
        """
        resp = self._http.get(f"{self._base_url}/accounts/{self.account_id}/summary")
        resp.raise_for_status()
        return _parse_account_summary(resp.json())

    def get_instrument(self, name: str) -> InstrumentSpec:
        """Fetch metadata for one instrument (pip location, margin rate, etc.).

        Uses GET /accounts/{id}/instruments filtered to a single name so we
        get the account-specific margin rate (which differs between practice
        and live accounts and can vary per-account based on account type).
        """
        resp = self._http.get(
            f"{self._base_url}/accounts/{self.account_id}/instruments",
            params={"instruments": name},
        )
        resp.raise_for_status()
        instruments = resp.json().get("instruments", [])
        if not instruments:
            raise ValueError(f"Instrument {name!r} not found for this account")
        return _parse_instrument_spec(instruments[0])

    def get_financing_rates(self, instruments: list[str]) -> list[FinancingRate]:
        """Fetch long/short financing rates for *instruments* in one request.

        Uses GET /accounts/{id}/instruments with a comma-separated
        ``instruments`` filter, so displaying rates for the whole tradable
        pairs list costs one HTTP call rather than one per pair. If *any*
        instrument in the list isn't tradeable for this account, Oanda
        returns 404 for the whole request rather than filtering it out —
        every name passed in must be valid.
        """
        resp = self._http.get(
            f"{self._base_url}/accounts/{self.account_id}/instruments",
            params={"instruments": ",".join(instruments)},
        )
        resp.raise_for_status()
        return [_parse_financing_rate(i) for i in resp.json().get("instruments", [])]

    def get_price(
        self,
        instrument: str,
        home_currency: str = "USD",
    ) -> PriceQuote:
        """Fetch live bid/ask and compute currency conversion rates.

        Conversion rates (``quote_to_home`` and ``base_to_home``) are what
        the sizing and pricing models need to translate pip moves and margin
        requirements into home-currency (account-currency) amounts.

        Handles three cases:
        1. ``quote == home`` (e.g., EUR_USD on USD account):
           ``quote_to_home = 1``, ``base_to_home = mid``.
        2. ``base == home`` (e.g., USD_JPY on USD account):
           ``base_to_home = 1``, ``quote_to_home = 1 / mid``.
        3. Cross-pair (e.g., EUR_GBP on USD account): fetches one additional
           price per side via ``_currency_to_home`` to resolve both rates.

        The cross-pair path makes extra HTTP calls. For the instruments
        Stephen typically trades (major USD pairs), path 1 or 2 is always
        taken.
        """
        resp = self._http.get(
            f"{self._base_url}/accounts/{self.account_id}/pricing",
            params={"instruments": instrument},
        )
        resp.raise_for_status()
        bid, ask = _extract_bid_ask(resp.json())
        mid = (bid + ask) / 2

        base, quote = instrument.split("_")

        if quote == home_currency:
            # e.g. EUR_USD on USD account: quote IS home, no conversion needed.
            quote_to_home = Decimal("1")
            base_to_home = mid
        elif base == home_currency:
            # e.g. USD_JPY on USD account: base IS home.
            base_to_home = Decimal("1")
            quote_to_home = Decimal("1") / mid
        else:
            # Cross-pair: resolve each currency's home value separately.
            base_to_home = self._currency_to_home(base, home_currency)
            quote_to_home = self._currency_to_home(quote, home_currency)

        return PriceQuote(
            bid=bid,
            ask=ask,
            quote_to_home=quote_to_home,
            base_to_home=base_to_home,
        )

    def get_daily_candles(self, instrument: str, count: int) -> list[Candle]:
        """Fetch up to *count* recent daily mid-price candles, oldest first.

        Days are aligned to the New York 17:00 close — the forex convention
        charting platforms use — so an ATR computed from these matches what
        the user sees on their daily chart. Only completed candles are
        returned, so the result can be one shorter than *count*.
        """
        resp = self._http.get(
            f"{self._base_url}/instruments/{_path_segment(instrument, 'instrument')}"
            "/candles",
            params={
                "granularity": "D",
                "price": "M",
                "count": count,
                "dailyAlignment": 17,
                "alignmentTimezone": "America/New_York",
            },
        )
        resp.raise_for_status()
        return _parse_candles(resp.json())

    def get_order_book(self, instrument: str, time: datetime | None = None) -> Book:
        """Fetch Oanda's order book for *instrument*: the latest snapshot, or
        the one at *time*.

        See ``_get_book`` for how *time* is sent and what Oanda rejects.
        """
        return self._get_book(instrument, "orderBook", time)

    def get_position_book(self, instrument: str, time: datetime | None = None) -> Book:
        """Fetch Oanda's position book for *instrument*: the latest snapshot,
        or the one at *time*.

        See ``_get_book`` for how *time* is sent and what Oanda rejects.
        """
        return self._get_book(instrument, "positionBook", time)

    def get_open_tickets_on_instrument(self, instrument: str) -> int:
        """Count open trade tickets for *instrument* (for the scale-in check).

        Uses GET /accounts/{id}/trades?instrument={name}&state=OPEN.  Each
        element in ``trades`` is one open ticket regardless of units size,
        which is what the risk model's ``open_tickets_on_instrument`` expects.
        """
        resp = self._http.get(
            f"{self._base_url}/accounts/{self.account_id}/trades",
            params={"instrument": instrument, "state": "OPEN"},
        )
        resp.raise_for_status()
        return len(resp.json().get("trades", []))

    def get_open_trades(self) -> list[OpenTrade]:
        """Fetch all currently open trades for the account.

        Uses GET /accounts/{id}/openTrades which returns the full trade detail
        including unrealised P/L, margin used, and any attached TP/SL orders.
        Returns an empty list when there are no open positions.
        Raises ``httpx.HTTPStatusError`` on Oanda errors.
        """
        resp = self._http.get(f"{self._base_url}/accounts/{self.account_id}/openTrades")
        resp.raise_for_status()
        return [_parse_open_trade(t) for t in resp.json().get("trades", [])]

    def get_pending_orders(self) -> list[PendingOrder]:
        """Fetch the account's pending *entry* orders.

        Uses GET /accounts/{id}/pendingOrders and keeps only order types that
        can open a position (``_ENTRY_ORDER_TYPES``), dropping the TP/SL
        orders attached to open trades. Returns an empty list when there are
        none. Raises ``httpx.HTTPStatusError`` on Oanda errors.
        """
        resp = self._http.get(
            f"{self._base_url}/accounts/{self.account_id}/pendingOrders"
        )
        resp.raise_for_status()
        return [
            _parse_pending_order(o)
            for o in resp.json().get("orders", [])
            if o.get("type") in _ENTRY_ORDER_TYPES
        ]

    def close_trade(self, trade_id: str) -> CloseFill:
        """Close an open trade in full.

        Uses PUT /accounts/{id}/trades/{tradeID}/close with no body, which
        always closes the complete position.  Returns the closing fill details.
        Raises ``httpx.HTTPStatusError`` on Oanda errors (e.g. 404 if the
        trade is already closed or the ID is wrong).
        """
        resp = self._http.put(
            f"{self._base_url}/accounts/{self.account_id}"
            f"/trades/{_path_segment(trade_id, 'trade ID')}/close"
        )
        resp.raise_for_status()
        return _parse_close_fill(resp.json())

    def place_market_order(
        self,
        instrument: str,
        units_signed: int,
    ) -> OrderFill:
        """Place a market order and return the fill details.

        ``units_signed`` follows Oanda's sign convention: positive = long,
        negative = short.  We use FOK (Fill-Or-Kill) so the order either fills
        immediately at the visible price or is rejected — no partial fills, no
        resting orders left behind.

        Raises:
            RuntimeError: if Oanda does not return an ``orderFillTransaction``
                (i.e., the order was killed, usually due to insufficient
                liquidity — extremely rare for major FX pairs).
            httpx.HTTPStatusError: on 4xx/5xx from the orders endpoint.
        """
        resp = self._http.post(
            f"{self._base_url}/accounts/{self.account_id}/orders",
            json={
                "order": {
                    "type": "MARKET",
                    "instrument": instrument,
                    "units": str(units_signed),
                    "timeInForce": "FOK",
                }
            },
        )
        resp.raise_for_status()
        payload = resp.json()

        if "orderFillTransaction" not in payload:
            # FOK was killed — the order did not execute.
            raise RuntimeError(
                f"Order not filled (instrument={instrument}, "
                f"units={units_signed}). "
                f"Oanda response: {json.dumps(payload)}"
            )

        return _parse_order_fill(payload["orderFillTransaction"])

    def place_limit_order(
        self,
        instrument: str,
        units_signed: int,
        price: Decimal,
        take_profit_price: Decimal | None = None,
        stop_loss_price: Decimal | None = None,
        trailing_stop_distance: Decimal | None = None,
    ) -> LimitOrderResult:
        """Place a GTC limit entry order, with TP/SL set to apply on fill.

        ``units_signed`` follows Oanda's sign convention (positive = long).
        TP/SL go in the order body as ``takeProfitOnFill`` /
        ``stopLossOnFill``: Oanda attaches them the moment the order fills,
        so there is no attach-after-fill step and no unprotected window. A
        trailing stop likewise rides as ``trailingStopLossOnFill``, given as
        a price-unit *distance* rather than a price.
        Prices must already be rounded to the instrument's display precision;
        Oanda rejects extra decimals.

        Usually the order rests and ``fill`` is ``None``. If the market has
        already crossed *price* when the order arrives, Oanda fills it at
        once and the fill is returned too.

        Raises:
            RuntimeError: if Oanda cancels the order on arrival (reported in
                ``orderCancelTransaction``), or the response has no
                ``orderCreateTransaction``.
            httpx.HTTPStatusError: on 4xx/5xx, including a rejected order.
        """
        # Build the order body; TP/SL/trail are optional and only sent when set.
        order: dict[str, Any] = {
            "type": "LIMIT",
            "instrument": instrument,
            "units": str(units_signed),
            "price": str(price),
            "timeInForce": "GTC",
            "positionFill": "DEFAULT",
        }
        if take_profit_price is not None:
            order["takeProfitOnFill"] = {"price": str(take_profit_price)}
        if stop_loss_price is not None:
            order["stopLossOnFill"] = {"price": str(stop_loss_price)}
        if trailing_stop_distance is not None:
            order["trailingStopLossOnFill"] = {"distance": str(trailing_stop_distance)}

        resp = self._http.post(
            f"{self._base_url}/accounts/{self.account_id}/orders",
            json={"order": order},
        )
        resp.raise_for_status()
        payload = resp.json()

        # A cancel with no fill means the order never went live (e.g. a
        # price outside Oanda's allowed bounds) — surface it, don't hide it.
        if (
            "orderCancelTransaction" in payload
            and "orderFillTransaction" not in payload
        ):
            reason = payload["orderCancelTransaction"].get("reason", "unknown")
            raise RuntimeError(
                f"Limit order cancelled by Oanda (reason: {reason}). "
                f"Oanda response: {json.dumps(payload)}"
            )

        order_id = _parse_order_create_txn_id(payload)
        fill_txn = payload.get("orderFillTransaction")
        return LimitOrderResult(
            order_id=order_id,
            fill=_parse_order_fill(fill_txn) if fill_txn is not None else None,
        )

    def attach_take_profit(self, trade_id: str, price: Decimal) -> str:
        """Attach a GTC take-profit order to an existing open trade.

        Returns the ``orderCreateTransaction.id`` from Oanda's response.
        Raises ``httpx.HTTPStatusError`` on 4xx / 5xx responses.
        """
        return self._attach_exit_order("TAKE_PROFIT", trade_id, {"price": str(price)})

    def attach_stop_loss(self, trade_id: str, price: Decimal) -> str:
        """Attach a GTC stop-loss order to an existing open trade.

        Returns the ``orderCreateTransaction.id`` from Oanda's response.
        Raises ``httpx.HTTPStatusError`` on 4xx / 5xx responses.
        """
        return self._attach_exit_order("STOP_LOSS", trade_id, {"price": str(price)})

    def attach_trailing_stop(self, trade_id: str, distance: Decimal) -> str:
        """Attach a GTC trailing stop-loss order to an existing open trade.

        *distance* is in price units (not pips) and must already be rounded
        to the instrument's display precision. Oanda keeps the trigger that
        far behind the bid (long) or ask (short) and only moves it in the
        trade's favour.

        Returns the ``orderCreateTransaction.id`` from Oanda's response.
        Raises ``httpx.HTTPStatusError`` on 4xx / 5xx responses.
        """
        return self._attach_exit_order(
            "TRAILING_STOP_LOSS", trade_id, {"distance": str(distance)}
        )

    def set_trade_trailing_stop(self, trade_id: str, distance: Decimal | None) -> str:
        """Add, replace, or remove the trailing stop on an open trade.

        Uses PUT /accounts/{id}/trades/{tradeID}/orders, which modifies a
        trade's dependent orders in place: only the ``trailingStopLoss`` field
        is sent, so any take-profit or fixed stop-loss is left untouched.
        *distance* is in price units, already rounded to the instrument's
        display precision; ``None`` sends ``trailingStopLoss: null``, which
        cancels the existing trailing stop.

        Returns the ID of the transaction that did the work: the new
        trailing-stop order's (``trailingStopLossOrderTransaction``) when
        setting one — Oanda also cancels any previous trail in the same call —
        or the cancellation's (``trailingStopLossOrderCancelTransaction``)
        when removing.

        Raises ``httpx.HTTPStatusError`` on Oanda errors (e.g. a distance
        outside the instrument's bounds, or an unknown trade ID), and
        ``RuntimeError`` if the expected transaction is missing from an
        otherwise successful response.
        """
        # A null trailingStopLoss is Oanda's documented way to cancel one.
        trailing: dict[str, str] | None = None
        if distance is not None:
            trailing = {"distance": str(distance), "timeInForce": "GTC"}
        resp = self._http.put(
            f"{self._base_url}/accounts/{self.account_id}"
            f"/trades/{_path_segment(trade_id, 'trade ID')}/orders",
            json={"trailingStopLoss": trailing},
        )
        resp.raise_for_status()

        # Pick the transaction that matches what was asked for.
        key = (
            "trailingStopLossOrderCancelTransaction"
            if distance is None
            else "trailingStopLossOrderTransaction"
        )
        txn = resp.json().get(key)
        if not txn or "id" not in txn:
            raise RuntimeError(f"No {key} in Oanda response for trade {trade_id}")
        return str(txn["id"])

    def stream_transactions(self) -> Generator[dict[str, Any], None, None]:
        """
        Yield each message on this account's transaction stream as a dict.

        Opens ``GET /accounts/{id}/transactions/stream`` on the stream host
        and yields every newline-delimited JSON message: a transaction as
        soon as Oanda records it, or a ``HEARTBEAT`` (with
        ``lastTransactionID``) every 5 seconds when nothing is happening.

        The stream never ends on its own. It raises
        ``httpx.HTTPStatusError`` on a 4xx / 5xx when opening, and
        ``httpx.ReadTimeout`` after ``_STREAM_READ_TIMEOUT`` seconds without
        a message (several missed heartbeats). Stopping early (``break`` or
        closing the generator) closes the connection.
        """
        import httpx

        url = f"{self._stream_url}/accounts/{self.account_id}/transactions/stream"
        # Same limits as other calls, except reads wait out heartbeat gaps.
        timeout = httpx.Timeout(
            connect=_CONNECT_TIMEOUT,
            read=_STREAM_READ_TIMEOUT,
            write=_WRITE_TIMEOUT,
            pool=_POOL_TIMEOUT,
        )
        with self._http.stream("GET", url, timeout=timeout) as resp:
            resp.raise_for_status()
            for line in resp.iter_lines():
                # Skip keep-alive blank lines between messages.
                if line.strip():
                    yield json.loads(line)

    def close(self) -> None:
        """Release the underlying httpx connection pool."""
        self._http.close()

    def __enter__(self) -> OandaClient:
        return self

    def __exit__(self, *_: object) -> None:
        self.close()

    # ------------------------------------------------------------------
    # Private helpers
    # ------------------------------------------------------------------

    def _attach_exit_order(
        self,
        order_type: str,
        trade_id: str,
        level: dict[str, str],
    ) -> str:
        """POST a TAKE_PROFIT, STOP_LOSS, or TRAILING_STOP_LOSS order linked
        to *trade_id*.

        The order types share identical request/response shapes apart from
        the ``type`` field and how the exit level is given: *level* is
        ``{"price": ...}`` for TP/SL and ``{"distance": ...}`` for a
        trailing stop.  GTC (good-till-cancelled) is the
        only time-in-force that makes sense for exit orders on an open trade —
        DAY orders would expire at session end, leaving the position unprotected.
        """
        resp = self._http.post(
            f"{self._base_url}/accounts/{self.account_id}/orders",
            json={
                "order": {
                    "type": order_type,
                    "tradeID": trade_id,
                    **level,
                    "timeInForce": "GTC",
                }
            },
        )
        resp.raise_for_status()
        return _parse_order_create_txn_id(resp.json())

    def _get_book(self, instrument: str, endpoint: str, time: datetime | None) -> Book:
        """GET /instruments/{instrument}/{endpoint} and parse the snapshot.

        *endpoint* is ``"orderBook"`` or ``"positionBook"``, which is also the
        response's top-level key. Oanda takes a snapshot every 20 minutes and
        rejects a *time* off that grid with a 400 ("misaligned"), so callers
        must round *time* down to :00, :20 or :40 first. A *time* with no
        snapshot (in the future, or before Oanda's history starts) gets a 404,
        and an instrument Oanda keeps no book for gets a 400. Each error's
        ``errorMessage`` says which; this method raises
        ``httpx.HTTPStatusError`` and leaves reporting to the caller.
        """
        params: dict[str, str] = {}
        if time is not None:
            # Oanda wants RFC 3339 in UTC; an aware datetime in any zone is
            # converted, so the same instant is asked for whatever the zone.
            params["time"] = time.astimezone(timezone.utc).strftime(
                "%Y-%m-%dT%H:%M:%SZ"
            )
        resp = self._http.get(
            f"{self._base_url}/instruments/"
            f"{_path_segment(instrument, 'instrument')}/{endpoint}",
            params=params,
        )
        resp.raise_for_status()
        return _parse_book(resp.json(), endpoint)

    def _fetch_all_cold(self) -> list[TransactionRow]:
        """
        Cold path: GET /transactions to discover pages, then fetch each page.

        Oanda responds with a ``pages`` array of full URLs.  Each URL returns
        a ``transactions`` array.  We fetch pages in sequence and concatenate
        the results.  Empty accounts return an empty pages list.
        """
        # Step 1: get the pages index.
        resp = self._http.get(
            f"{self._base_url}/accounts/{self.account_id}/transactions"
        )
        resp.raise_for_status()
        pages: list[str] = resp.json().get("pages", [])

        # Step 2: fetch each page in order and collect rows.
        all_rows: list[TransactionRow] = []
        for page_url in pages:
            page_resp = self._http.get(page_url)
            page_resp.raise_for_status()
            for txn in page_resp.json().get("transactions", []):
                all_rows.append(self._parse_transaction(txn))

        return all_rows

    def _fetch_since(self, from_id: str) -> list[TransactionRow]:
        """
        Incremental path: GET /transactions/sinceid, looping until done.

        Oanda returns up to ``_SINCEID_PAGE_LIMIT`` rows per call.  When a
        response contains exactly that many rows, we advance ``from_id`` to
        the last received ID and repeat.  A sub-limit response means no more
        data.
        """
        all_rows: list[TransactionRow] = []
        current_from = from_id

        while True:
            resp = self._http.get(
                f"{self._base_url}/accounts/{self.account_id}/transactions/sinceid",
                params={"id": current_from},
            )
            resp.raise_for_status()
            txns: list[dict[str, Any]] = resp.json().get("transactions", [])

            for txn in txns:
                all_rows.append(self._parse_transaction(txn))

            # Fewer than the per-call limit → no more pages to fetch.
            if len(txns) < _SINCEID_PAGE_LIMIT:
                break

            # Advance the cursor to the last received ID and loop.
            current_from = str(txns[-1]["id"])

        return all_rows

    def _fetch_mid(self, instrument: str) -> Decimal:
        """Fetch the mid price for *instrument* (helper for cross-pair conversions)."""
        resp = self._http.get(
            f"{self._base_url}/accounts/{self.account_id}/pricing",
            params={"instruments": instrument},
        )
        resp.raise_for_status()
        bid, ask = _extract_bid_ask(resp.json())
        return (bid + ask) / 2

    def _currency_to_home(self, currency: str, home: str) -> Decimal:
        """HTTP: fetch mid price(s) and delegate to ``_compute_conversion_rate``.

        Fetches whichever of ``{currency}_{home}`` or ``{home}_{currency}``
        Oanda knows about, then calls the pure conversion helper.  Raises
        ``ValueError`` when neither pair is available on this account.
        """
        import httpx  # lazy: see OandaClient.__init__

        if currency == home:
            return Decimal("1")
        direct = f"{currency}_{home}"
        inverted = f"{home}_{currency}"
        mids: dict[str, Decimal] = {}
        try:
            mids[direct] = self._fetch_mid(direct)
        except httpx.HTTPStatusError:
            pass
        if direct not in mids:
            try:
                mids[inverted] = self._fetch_mid(inverted)
            except httpx.HTTPStatusError:
                raise ValueError(
                    f"Cannot determine {currency}/{home} conversion rate: "
                    f"neither {direct} nor {inverted} "
                    f"is a recognised Oanda pair for this account"
                ) from None
        return _compute_conversion_rate(currency, home, mids)

    def _parse_transaction(self, txn: dict[str, Any]) -> TransactionRow:
        """Extract the fields we index from one Oanda transaction dict.

        ``raw_json`` is the compact-serialised full dict so we never lose
        fields we don't yet parse.

        ``parent_oanda_id`` is always ``None`` here; the caller
        (``get_transactions_since``) applies ``_resolve_financing_parents``
        to the full batch afterward to stamp parent links on DAILY_FINANCING
        children.
        """
        return TransactionRow(
            oanda_id=str(txn["id"]),
            account_id=self.account_id,
            type=txn["type"],
            time=txn["time"],
            parent_oanda_id=None,
            raw_json=json.dumps(txn, separators=(",", ":")),
        )
