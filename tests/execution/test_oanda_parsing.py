"""Tests for the pure parsing helpers in ``oanda.py``.

These helpers convert raw Oanda API response dicts into domain types.  Testing
them directly (without HTTP) gives us confidence in the parsing logic
independently of the HTTP layer, which is only exercised in integration tests.

We feed in sample dicts that mirror the Oanda v20 API response shapes documented
in the module's docstring.  If Oanda ever changes a field name, the integration
tests would fail at the HTTP boundary and these unit tests would continue
passing — which is the right separation.
"""

from __future__ import annotations

from decimal import Decimal

import pytest

from frmj.execution.oanda import (
    AccountSummary,
    OpenTrade,
    _extract_bid_ask,
    _parse_account_summary,
    _parse_instrument_spec,
    _parse_open_trade,
    _parse_order_create_txn_id,
)
from frmj.domain.sizing import InstrumentSpec


# ---------------------------------------------------------------------------
# Helpers: sample Oanda response dicts
# ---------------------------------------------------------------------------


def _account_payload(
    nav: str = "12450.00",
    margin_available: str = "5000.00",
    open_trade_count: int = 2,
) -> dict:
    return {
        "account": {
            "NAV": nav,
            "marginAvailable": margin_available,
            "openTradeCount": open_trade_count,
        }
    }


def _instrument_payload(
    name: str = "EUR_USD",
    pip_location: int = -4,
    margin_rate: str = "0.02",
    min_trade_size: str = "1",
) -> dict:
    return {
        "name": name,
        "pipLocation": pip_location,
        "marginRate": margin_rate,
        "minimumTradeSize": min_trade_size,
    }


def _pricing_payload(bid: str, ask: str) -> dict:
    return {
        "prices": [
            {
                "bids": [{"price": bid, "liquidity": 10_000_000}],
                "asks": [{"price": ask, "liquidity": 10_000_000}],
            }
        ]
    }


# ---------------------------------------------------------------------------
# _parse_account_summary
# ---------------------------------------------------------------------------


class TestParseAccountSummary:
    def test_parses_nav(self) -> None:
        result = _parse_account_summary(_account_payload(nav="12450.00"))
        assert result.nav == Decimal("12450.00")

    def test_parses_margin_available(self) -> None:
        result = _parse_account_summary(_account_payload(margin_available="5000.00"))
        assert result.margin_available == Decimal("5000.00")

    def test_parses_open_trade_count(self) -> None:
        result = _parse_account_summary(_account_payload(open_trade_count=3))
        assert result.open_trade_count == 3

    def test_returns_account_summary_dataclass(self) -> None:
        result = _parse_account_summary(_account_payload())
        assert isinstance(result, AccountSummary)

    def test_zero_open_trades_is_valid(self) -> None:
        result = _parse_account_summary(_account_payload(open_trade_count=0))
        assert result.open_trade_count == 0

    def test_large_nav_preserved(self) -> None:
        result = _parse_account_summary(_account_payload(nav="1234567.89"))
        assert result.nav == Decimal("1234567.89")


# ---------------------------------------------------------------------------
# _parse_instrument_spec
# ---------------------------------------------------------------------------


class TestParseInstrumentSpec:
    def test_eur_usd_pip_location(self) -> None:
        result = _parse_instrument_spec(_instrument_payload(pip_location=-4))
        assert result.pip_location == -4

    def test_usd_jpy_pip_location(self) -> None:
        result = _parse_instrument_spec(
            _instrument_payload(name="USD_JPY", pip_location=-2, margin_rate="0.04")
        )
        assert result.pip_location == -2

    def test_margin_rate_as_decimal(self) -> None:
        result = _parse_instrument_spec(_instrument_payload(margin_rate="0.02"))
        assert result.margin_rate == Decimal("0.02")

    def test_instrument_name_preserved(self) -> None:
        result = _parse_instrument_spec(_instrument_payload(name="GBP_USD"))
        assert result.name == "GBP_USD"

    def test_min_units_from_string(self) -> None:
        result = _parse_instrument_spec(_instrument_payload(min_trade_size="1"))
        assert result.min_units == 1

    def test_large_min_trade_size(self) -> None:
        result = _parse_instrument_spec(_instrument_payload(min_trade_size="1000"))
        assert result.min_units == 1000

    def test_units_increment_defaults_to_one(self) -> None:
        result = _parse_instrument_spec(_instrument_payload())
        assert result.units_increment == 1

    def test_returns_instrument_spec_dataclass(self) -> None:
        result = _parse_instrument_spec(_instrument_payload())
        assert isinstance(result, InstrumentSpec)


# ---------------------------------------------------------------------------
# _extract_bid_ask
# ---------------------------------------------------------------------------


class TestExtractBidAsk:
    def test_extracts_bid_and_ask(self) -> None:
        payload = _pricing_payload(bid="1.09990", ask="1.10010")
        bid, ask = _extract_bid_ask(payload)
        assert bid == Decimal("1.09990")
        assert ask == Decimal("1.10010")

    def test_uses_best_price_band(self) -> None:
        """Index 0 is always the tightest band — we must use it."""
        payload = {
            "prices": [
                {
                    "bids": [
                        {"price": "1.09995", "liquidity": 5_000_000},
                        {"price": "1.09990", "liquidity": 10_000_000},
                    ],
                    "asks": [
                        {"price": "1.10005", "liquidity": 5_000_000},
                        {"price": "1.10010", "liquidity": 10_000_000},
                    ],
                }
            ]
        }
        bid, ask = _extract_bid_ask(payload)
        assert bid == Decimal("1.09995")
        assert ask == Decimal("1.10005")

    def test_jpy_pair_prices(self) -> None:
        payload = _pricing_payload(bid="149.990", ask="150.010")
        bid, ask = _extract_bid_ask(payload)
        assert bid == Decimal("149.990")
        assert ask == Decimal("150.010")


# ---------------------------------------------------------------------------
# _parse_open_trade
# ---------------------------------------------------------------------------


def _open_trade_payload(
    trade_id: str = "6368",
    instrument: str = "EUR_USD",
    current_units: str = "10000",
    price: str = "1.10050",
    unrealised_pl: str = "45.23",
    margin_used: str = "220.10",
    open_time: str = "2026-04-25T14:30:00.000000Z",
    tp_price: str | None = "1.10550",
    sl_price: str | None = "1.09750",
) -> dict:
    trade: dict = {
        "id": trade_id,
        "instrument": instrument,
        "currentUnits": current_units,
        "price": price,
        "unrealizedPL": unrealised_pl,
        "marginUsed": margin_used,
        "openTime": open_time,
    }
    if tp_price is not None:
        trade["takeProfitOrder"] = {"id": "6369", "price": tp_price}
    if sl_price is not None:
        trade["stopLossOrder"] = {"id": "6370", "price": sl_price}
    return trade


class TestParseOpenTrade:
    def test_long_direction_from_positive_units(self) -> None:
        result = _parse_open_trade(_open_trade_payload(current_units="10000"))
        assert result.direction == "LONG"
        assert result.units == 10000

    def test_short_direction_from_negative_units(self) -> None:
        result = _parse_open_trade(_open_trade_payload(current_units="-5000"))
        assert result.direction == "SHORT"
        assert result.units == 5000

    def test_units_always_positive(self) -> None:
        result = _parse_open_trade(_open_trade_payload(current_units="-1"))
        assert result.units > 0

    def test_parses_open_price(self) -> None:
        result = _parse_open_trade(_open_trade_payload(price="1.10050"))
        assert result.open_price == Decimal("1.10050")

    def test_parses_unrealised_pl(self) -> None:
        result = _parse_open_trade(_open_trade_payload(unrealised_pl="-12.50"))
        assert result.unrealised_pl == Decimal("-12.50")

    def test_parses_tp_and_sl_prices(self) -> None:
        result = _parse_open_trade(
            _open_trade_payload(tp_price="1.10550", sl_price="1.09750")
        )
        assert result.take_profit_price == Decimal("1.10550")
        assert result.stop_loss_price == Decimal("1.09750")

    def test_tp_sl_none_when_absent(self) -> None:
        result = _parse_open_trade(_open_trade_payload(tp_price=None, sl_price=None))
        assert result.take_profit_price is None
        assert result.stop_loss_price is None

    def test_returns_open_trade_dataclass(self) -> None:
        result = _parse_open_trade(_open_trade_payload())
        assert isinstance(result, OpenTrade)

    def test_instrument_and_trade_id_preserved(self) -> None:
        result = _parse_open_trade(
            _open_trade_payload(trade_id="9999", instrument="USD_JPY")
        )
        assert result.trade_id == "9999"
        assert result.instrument == "USD_JPY"


# ---------------------------------------------------------------------------
# _parse_order_create_txn_id
# ---------------------------------------------------------------------------


class TestParseOrderCreateTxnId:
    def test_extracts_id_from_order_create_transaction(self) -> None:
        payload = {"orderCreateTransaction": {"id": "12345", "type": "TAKE_PROFIT_ORDER"}}
        assert _parse_order_create_txn_id(payload) == "12345"

    def test_id_coerced_to_string(self) -> None:
        """Oanda IDs are numeric strings; an int value must still work."""
        payload = {"orderCreateTransaction": {"id": 12345}}
        result = _parse_order_create_txn_id(payload)
        assert isinstance(result, str)
        assert result == "12345"

    def test_raises_on_missing_key(self) -> None:
        with pytest.raises(RuntimeError, match="No orderCreateTransaction"):
            _parse_order_create_txn_id({})

    def test_raises_with_unrelated_keys_present(self) -> None:
        """A response with orderFillTransaction but no orderCreateTransaction raises."""
        payload = {"orderFillTransaction": {"id": "99"}, "lastTransactionID": "99"}
        with pytest.raises(RuntimeError, match="No orderCreateTransaction"):
            _parse_order_create_txn_id(payload)
