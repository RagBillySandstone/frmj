"""Tests for OandaClient HTTP methods.

These tests exercise the client's logic by replacing the underlying
``httpx.Client`` with a lightweight mock that returns a pre-crafted response
dict — no real network traffic is made.

Separation from ``test_oanda_parsing.py``: that module tests the pure parsing
helpers in isolation; this module tests the OandaClient methods that wrap an
HTTP call around those helpers (or perform their own response interpretation).
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal

import pytest

from frmj.execution.oanda import OandaClient


# ---------------------------------------------------------------------------
# Minimal HTTP mock
# ---------------------------------------------------------------------------


@dataclass
class _FakeResponse:
    """Minimal stand-in for an httpx.Response used by OandaClient methods."""

    _data: dict

    def raise_for_status(self) -> None:
        """No-op: test doubles always return 200."""

    def json(self) -> dict:
        """Return the pre-built response dict."""
        return self._data


@dataclass
class _FakeHttp:
    """Replaces ``OandaClient._http``; stores the last request for inspection."""

    response_data: dict

    def post(self, url: str, **kwargs: object) -> _FakeResponse:
        """Return the configured response regardless of URL or body."""
        return _FakeResponse(self.response_data)


def _make_client(response_data: dict) -> OandaClient:
    """Construct an OandaClient with a stubbed HTTP layer."""
    client = OandaClient(
        token="dummy-token",
        account_id="101-001-test-001",
        practice=True,
    )
    # Replace the real httpx.Client with our stub.
    client._http = _FakeHttp(response_data)  # type: ignore[assignment]
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
