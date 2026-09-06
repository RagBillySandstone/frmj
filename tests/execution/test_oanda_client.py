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
