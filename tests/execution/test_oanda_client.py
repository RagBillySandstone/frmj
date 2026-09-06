"""Tests for OandaClient HTTP methods.

These tests exercise the client's logic by replacing the underlying
``httpx.Client`` with a lightweight mock that returns a pre-crafted response
dict — no real network traffic is made.

Separation from ``test_oanda_parsing.py``: that module tests the pure parsing
helpers in isolation; this module tests the OandaClient methods that wrap an
HTTP call around those helpers (or perform their own response interpretation).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from decimal import Decimal
from typing import Any

import httpx
import pytest

from frmj.execution import oanda
from frmj.execution.oanda import OandaClient


# ---------------------------------------------------------------------------
# Minimal HTTP mock
# ---------------------------------------------------------------------------
#
# ``_FakeHttp`` replaces ``OandaClient._http`` (a real ``httpx.Client``) with a
# stand-in that hands back pre-built responses in call order, one per
# get/post/put invocation the client makes. Most client methods make exactly
# one HTTP call, so a single response is enough; methods that page (the
# transaction-sync helpers) or that make follow-up calls (cross-pair price
# conversion) are tested by queuing up multiple responses.


@dataclass
class _FakeResponse:
    """Stand-in for a successful ``httpx.Response``."""

    _data: dict[str, Any]

    def raise_for_status(self) -> None:
        """No-op: this stand-in always represents a 2xx response."""

    def json(self) -> dict[str, Any]:
        """Return the pre-built response dict."""
        return self._data


@dataclass
class _ErrorResponse:
    """Stand-in for an ``httpx.Response`` whose status indicates failure.

    ``raise_for_status`` mirrors real httpx behaviour: it raises
    ``httpx.HTTPStatusError`` rather than returning normally. Building a real
    ``httpx.Request``/``httpx.Response`` pair keeps the exception faithful to
    what ``OandaClient`` actually has to handle (e.g. in
    ``_currency_to_home``'s fallback logic, which catches this exception type).
    """

    status_code: int

    def raise_for_status(self) -> None:
        """Raise ``httpx.HTTPStatusError``, as a real error response would."""
        request = httpx.Request("GET", "https://example.test/")
        response = httpx.Response(self.status_code, request=request)
        raise httpx.HTTPStatusError(
            f"HTTP {self.status_code}", request=request, response=response
        )

    def json(self) -> dict[str, Any]:
        """Never reached: callers must check ``raise_for_status`` first."""
        raise AssertionError("json() called on an error response")


@dataclass
class _RecordedCall:
    """One get/post/put call captured by ``_FakeHttp``, for assertions."""

    method: str
    url: str
    kwargs: dict[str, Any]


@dataclass
class _FakeHttp:
    """Replaces ``OandaClient._http``.

    Responses are consumed from ``_responses`` in the order the client makes
    HTTP calls, regardless of method (get/post/put share one queue — that
    matches how ``OandaClient`` issues calls sequentially, never concurrently).
    Every call is recorded in ``calls`` so tests can assert on the endpoint
    and parameters used.
    """

    _responses: list[_FakeResponse | _ErrorResponse]
    calls: list[_RecordedCall] = field(default_factory=list)
    closed: bool = False

    def _next(
        self, method: str, url: str, **kwargs: Any
    ) -> _FakeResponse | _ErrorResponse:
        """Record the call and pop the next queued response."""
        self.calls.append(_RecordedCall(method, url, kwargs))
        return self._responses.pop(0)

    def get(self, url: str, **kwargs: Any) -> _FakeResponse | _ErrorResponse:
        return self._next("GET", url, **kwargs)

    def post(self, url: str, **kwargs: Any) -> _FakeResponse | _ErrorResponse:
        return self._next("POST", url, **kwargs)

    def put(self, url: str, **kwargs: Any) -> _FakeResponse | _ErrorResponse:
        return self._next("PUT", url, **kwargs)

    def close(self) -> None:
        """Record that the connection pool was released."""
        self.closed = True


def _make_client(
    *responses: dict[str, Any] | _FakeResponse | _ErrorResponse,
) -> OandaClient:
    """Construct an OandaClient with a stubbed HTTP layer.

    Each positional argument is consumed by one HTTP call, in the order the
    client makes them. A bare ``dict`` is treated as a successful response
    body (wrapped in ``_FakeResponse``); pass an ``_ErrorResponse`` explicitly
    to simulate a 4xx/5xx.
    """
    wrapped = [_FakeResponse(r) if isinstance(r, dict) else r for r in responses]
    client = OandaClient(
        token="dummy-token",
        account_id="101-001-test-001",
        practice=True,
    )
    # Replace the real httpx.Client with our stub.
    client._http = _FakeHttp(wrapped)  # type: ignore[assignment]
    return client


# ---------------------------------------------------------------------------
# get_account_summary
# ---------------------------------------------------------------------------


class TestGetAccountSummary:
    def test_returns_parsed_summary(self) -> None:
        """A summary response is parsed into an ``AccountSummary`` and the
        request goes to the account's /summary endpoint."""
        response = {
            "account": {
                "NAV": "10500.25",
                "balance": "10000.00",
                "unrealizedPL": "500.25",
                "pl": "1200.00",
                "positionValue": "20000.00",
                "marginUsed": "400.00",
                "marginAvailable": "10100.25",
                "openTradeCount": 2,
            }
        }
        client = _make_client(response)
        summary = client.get_account_summary()
        assert summary.nav == Decimal("10500.25")
        assert summary.margin_available == Decimal("10100.25")
        assert summary.open_trade_count == 2

        http: _FakeHttp = client._http  # type: ignore[assignment]
        assert http.calls[0].method == "GET"
        assert http.calls[0].url.endswith("/accounts/101-001-test-001/summary")


# ---------------------------------------------------------------------------
# get_instrument
# ---------------------------------------------------------------------------


class TestGetInstrument:
    def test_returns_parsed_spec(self) -> None:
        """A matching instrument is parsed into an ``InstrumentSpec``, and the
        request filters to the single requested instrument name."""
        response = {
            "instruments": [
                {
                    "name": "EUR_USD",
                    "pipLocation": -4,
                    "marginRate": "0.02",
                    "minimumTradeSize": "1",
                    "displayPrecision": 5,
                }
            ]
        }
        client = _make_client(response)
        spec = client.get_instrument("EUR_USD")
        assert spec.name == "EUR_USD"
        assert spec.margin_rate == Decimal("0.02")

        http: _FakeHttp = client._http  # type: ignore[assignment]
        assert http.calls[0].kwargs["params"] == {"instruments": "EUR_USD"}

    def test_raises_value_error_when_not_found(self) -> None:
        """An empty ``instruments`` array means the account doesn't recognise
        the instrument name; this must raise rather than return garbage."""
        client = _make_client({"instruments": []})
        with pytest.raises(ValueError, match="not found for this account"):
            client.get_instrument("XAU_XAG")


# ---------------------------------------------------------------------------
# get_price
# ---------------------------------------------------------------------------


def _pricing_response(bid: str, ask: str) -> dict[str, Any]:
    """Build a minimal GET /pricing response for one instrument."""
    return {"prices": [{"bids": [{"price": bid}], "asks": [{"price": ask}]}]}


class TestGetPrice:
    def test_quote_equals_home_needs_no_conversion(self) -> None:
        """EUR_USD on a USD account: quote_to_home is 1, base_to_home is mid."""
        client = _make_client(_pricing_response("1.1000", "1.1002"))
        quote = client.get_price("EUR_USD", home_currency="USD")
        assert quote.bid == Decimal("1.1000")
        assert quote.ask == Decimal("1.1002")
        assert quote.quote_to_home == Decimal("1")
        assert quote.base_to_home == Decimal("1.1001")

    def test_base_equals_home_inverts_for_quote(self) -> None:
        """USD_JPY on a USD account: base_to_home is 1, quote_to_home is 1/mid."""
        client = _make_client(_pricing_response("150.00", "150.02"))
        quote = client.get_price("USD_JPY", home_currency="USD")
        assert quote.base_to_home == Decimal("1")
        assert quote.quote_to_home == Decimal("1") / Decimal("150.01")

    def test_cross_pair_resolves_both_legs_via_extra_calls(self) -> None:
        """EUR_GBP on a USD account: neither leg is home currency, so the
        client fetches EUR_USD and GBP_USD mids to resolve both conversion
        rates, in addition to the EUR_GBP price itself."""
        client = _make_client(
            _pricing_response("0.8550", "0.8552"),  # EUR_GBP
            _pricing_response("1.1000", "1.1002"),  # EUR_USD (base leg)
            _pricing_response("1.2700", "1.2702"),  # GBP_USD (quote leg)
        )
        quote = client.get_price("EUR_GBP", home_currency="USD")
        assert quote.base_to_home == Decimal("1.1001")
        assert quote.quote_to_home == Decimal("1.2701")

        http: _FakeHttp = client._http  # type: ignore[assignment]
        assert [c.kwargs["params"]["instruments"] for c in http.calls] == [
            "EUR_GBP",
            "EUR_USD",
            "GBP_USD",
        ]


# ---------------------------------------------------------------------------
# get_open_tickets_on_instrument
# ---------------------------------------------------------------------------


class TestGetOpenTicketsOnInstrument:
    def test_counts_open_trades(self) -> None:
        """Each element of the ``trades`` array is one open ticket."""
        client = _make_client({"trades": [{"id": "1"}, {"id": "2"}, {"id": "3"}]})
        assert client.get_open_tickets_on_instrument("EUR_USD") == 3

    def test_zero_when_no_open_trades(self) -> None:
        """No open trades on the instrument means zero tickets."""
        client = _make_client({"trades": []})
        assert client.get_open_tickets_on_instrument("EUR_USD") == 0


# ---------------------------------------------------------------------------
# get_open_trades
# ---------------------------------------------------------------------------


class TestGetOpenTrades:
    def test_returns_parsed_list(self) -> None:
        """Each element of the ``trades`` array is parsed into an ``OpenTrade``."""
        response = {
            "trades": [
                {
                    "id": "501",
                    "instrument": "EUR_USD",
                    "currentUnits": "10000",
                    "price": "1.10000",
                    "unrealizedPL": "12.50",
                    "marginUsed": "220.00",
                    "openTime": "2026-01-01T00:00:00.000000000Z",
                }
            ]
        }
        client = _make_client(response)
        trades = client.get_open_trades()
        assert len(trades) == 1
        assert trades[0].trade_id == "501"
        assert trades[0].direction == "LONG"
        assert trades[0].units == 10_000

    def test_empty_when_no_trades_key(self) -> None:
        """A response without a ``trades`` key means no open positions."""
        client = _make_client({})
        assert client.get_open_trades() == []


# ---------------------------------------------------------------------------
# get_transactions_since — cold path (_fetch_all_cold)
# ---------------------------------------------------------------------------


def _txn(txn_id: str, txn_type: str = "ORDER_FILL") -> dict[str, Any]:
    """Build a minimal Oanda transaction dict."""
    return {"id": txn_id, "type": txn_type, "time": "2026-01-01T00:00:00.000000000Z"}


class TestFetchAllCold:
    def test_empty_account_returns_empty_list(self) -> None:
        """An empty ``pages`` array means no history; no page fetches follow."""
        client = _make_client({"pages": []})
        rows = client.get_transactions_since(from_id=None)
        assert rows == []

    def test_fetches_and_concatenates_all_pages(self) -> None:
        """Each URL in ``pages`` is fetched in order and its transactions
        concatenated into the final row list."""
        client = _make_client(
            {"pages": ["https://x/page1", "https://x/page2"]},
            {"transactions": [_txn("1"), _txn("2")]},
            {"transactions": [_txn("3")]},
        )
        rows = client.get_transactions_since(from_id=None)
        assert [r.oanda_id for r in rows] == ["1", "2", "3"]

        http: _FakeHttp = client._http  # type: ignore[assignment]
        # First call discovers the pages index; the rest fetch each page URL.
        assert http.calls[1].url == "https://x/page1"
        assert http.calls[2].url == "https://x/page2"


# ---------------------------------------------------------------------------
# get_transactions_since — incremental path (_fetch_since)
# ---------------------------------------------------------------------------


class TestFetchSince:
    def test_stops_after_sub_limit_response(self) -> None:
        """A response with fewer rows than the per-call limit means there is
        no more data; the loop must not make a second call."""
        client = _make_client({"transactions": [_txn("10"), _txn("11")]})
        rows = client.get_transactions_since(from_id="9")
        assert [r.oanda_id for r in rows] == ["10", "11"]

        http: _FakeHttp = client._http  # type: ignore[assignment]
        assert len(http.calls) == 1
        assert http.calls[0].kwargs["params"] == {"id": "9"}

    def test_loops_and_advances_cursor_when_at_limit(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A full-limit response means there may be more; the client must
        loop, advancing ``from_id`` to the last received transaction's ID,
        until a sub-limit response is returned."""
        # Shrink the page limit so the test doesn't need 500 fake rows.
        monkeypatch.setattr(oanda, "_SINCEID_PAGE_LIMIT", 2)
        client = _make_client(
            {"transactions": [_txn("10"), _txn("11")]},  # at limit -> loop again
            {"transactions": [_txn("12")]},  # sub-limit -> stop
        )
        rows = client.get_transactions_since(from_id="9")
        assert [r.oanda_id for r in rows] == ["10", "11", "12"]

        http: _FakeHttp = client._http  # type: ignore[assignment]
        assert http.calls[0].kwargs["params"] == {"id": "9"}
        assert http.calls[1].kwargs["params"] == {"id": "11"}


# ---------------------------------------------------------------------------
# place_market_order
# ---------------------------------------------------------------------------


class TestPlaceMarketOrder:
    def test_fok_killed_raises_runtime_error(self) -> None:
        """When Oanda returns a response without ``orderFillTransaction``, the FOK
        order was killed (e.g., insufficient liquidity) and a RuntimeError must be
        raised so the caller can surface the retry / save / abort prompt."""
        # Oanda's FOK-killed response omits ``orderFillTransaction`` entirely;
        # it typically contains an ``orderCancelTransaction`` instead.
        killed_response = {
            "orderCancelTransaction": {
                "id": "12345",
                "type": "ORDER_CANCEL",
                "reason": "MARKET_HALTED",
            },
            "relatedTransactionIDs": ["12345"],
        }
        client = _make_client(killed_response)
        with pytest.raises(RuntimeError, match="Order not filled"):
            client.place_market_order("EUR_USD", 10_000)

    def test_successful_fill_returns_order_fill(self) -> None:
        """A response containing ``orderFillTransaction`` is parsed into an
        ``OrderFill`` with the correct transaction ID, fill price, and units."""
        filled_response = {
            "orderFillTransaction": {
                "id": "99001",
                "price": "1.10050",
                "units": "10000",
                "tradeOpened": {"tradeID": "88001"},
            },
            "relatedTransactionIDs": ["99001"],
        }
        client = _make_client(filled_response)
        fill = client.place_market_order("EUR_USD", 10_000)
        assert fill.transaction_id == "99001"
        assert fill.fill_price == Decimal("1.10050")
        assert fill.units_filled == 10_000
        assert fill.trade_id == "88001"


# ---------------------------------------------------------------------------
# close_trade
# ---------------------------------------------------------------------------


class TestCloseTrade:
    def test_returns_close_fill(self) -> None:
        """A successful close response is parsed into a ``CloseFill``, and
        the request PUTs to the trade's /close endpoint."""
        response = {
            "orderFillTransaction": {
                "id": "77002",
                "price": "1.10500",
                "pl": "-25.00",
            }
        }
        client = _make_client(response)
        fill = client.close_trade("501")
        assert fill.transaction_id == "77002"
        assert fill.close_price == Decimal("1.10500")
        assert fill.realised_pl == Decimal("-25.00")

        http: _FakeHttp = client._http  # type: ignore[assignment]
        assert http.calls[0].method == "PUT"
        assert http.calls[0].url.endswith("/trades/501/close")

    def test_raises_on_http_error(self) -> None:
        """A 404 (already-closed trade / bad ID) must surface as
        ``httpx.HTTPStatusError`` rather than being swallowed."""
        client = _make_client(_ErrorResponse(404))
        with pytest.raises(httpx.HTTPStatusError):
            client.close_trade("does-not-exist")


# ---------------------------------------------------------------------------
# attach_take_profit / attach_stop_loss
# ---------------------------------------------------------------------------


class TestAttachExitOrders:
    def test_attach_take_profit_posts_correct_order_type(self) -> None:
        """attach_take_profit must send order type TAKE_PROFIT and return the
        created transaction's ID."""
        response = {"orderCreateTransaction": {"id": "60001"}}
        client = _make_client(response)
        txn_id = client.attach_take_profit("501", Decimal("1.12000"))
        assert txn_id == "60001"

        http: _FakeHttp = client._http  # type: ignore[assignment]
        order = http.calls[0].kwargs["json"]["order"]
        assert order["type"] == "TAKE_PROFIT"
        assert order["tradeID"] == "501"
        assert order["price"] == "1.12000"
        assert order["timeInForce"] == "GTC"

    def test_attach_stop_loss_posts_correct_order_type(self) -> None:
        """attach_stop_loss must send order type STOP_LOSS."""
        response = {"orderCreateTransaction": {"id": "60002"}}
        client = _make_client(response)
        txn_id = client.attach_stop_loss("501", Decimal("1.08000"))
        assert txn_id == "60002"

        http: _FakeHttp = client._http  # type: ignore[assignment]
        order = http.calls[0].kwargs["json"]["order"]
        assert order["type"] == "STOP_LOSS"

    def test_raises_runtime_error_when_transaction_missing(self) -> None:
        """An undocumented response shape (no orderCreateTransaction) must
        surface loudly rather than returning a bogus ID."""
        client = _make_client({})
        with pytest.raises(RuntimeError, match="No orderCreateTransaction"):
            client.attach_take_profit("501", Decimal("1.12000"))


# ---------------------------------------------------------------------------
# close / __enter__ / __exit__
# ---------------------------------------------------------------------------


class TestLifecycle:
    def test_close_releases_the_connection_pool(self) -> None:
        """close() must delegate to the underlying HTTP client's close()."""
        client = _make_client()
        client.close()
        http: _FakeHttp = client._http  # type: ignore[assignment]
        assert http.closed is True

    def test_context_manager_closes_on_exit(self) -> None:
        """Using the client as a context manager closes it on exit, and
        __enter__ returns the client itself."""
        client = _make_client()
        http: _FakeHttp = client._http  # type: ignore[assignment]
        with client as ctx:
            assert ctx is client
            assert http.closed is False
        assert http.closed is True
