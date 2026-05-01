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
    CloseFill,
    OpenTrade,
    TransactionRow,
    _compute_conversion_rate,
    _extract_bid_ask,
    _parse_account_summary,
    _parse_close_fill,
    _parse_instrument_spec,
    _parse_open_trade,
    _parse_order_create_txn_id,
    _resolve_financing_parents,
)
from frmj.domain.sizing import InstrumentSpec


# ---------------------------------------------------------------------------
# Helpers: sample Oanda response dicts
# ---------------------------------------------------------------------------


def _account_payload(
    nav: str = "12450.00",
    balance: str = "10000.00",
    unrealized_pl: str = "2450.00",
    realized_pl: str = "800.00",
    position_value: str = "220000.00",
    margin_used: str = "2200.00",
    margin_available: str = "5000.00",
    open_trade_count: int = 2,
) -> dict:
    return {
        "account": {
            "NAV": nav,
            "balance": balance,
            "unrealizedPL": unrealized_pl,
            "pl": realized_pl,
            "positionValue": position_value,
            "marginUsed": margin_used,
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
# _parse_close_fill
# ---------------------------------------------------------------------------


def _close_payload(
    txn_id: str = "6372",
    price: str = "1.10095",
    pl: str = "45.23",
) -> dict:
    return {
        "orderFillTransaction": {
            "id": txn_id,
            "price": price,
            "pl": pl,
        }
    }


class TestParseCloseFill:
    def test_parses_transaction_id(self) -> None:
        result = _parse_close_fill(_close_payload(txn_id="6372"))
        assert result.transaction_id == "6372"

    def test_parses_close_price(self) -> None:
        result = _parse_close_fill(_close_payload(price="1.10095"))
        assert result.close_price == Decimal("1.10095")

    def test_parses_realised_pl(self) -> None:
        result = _parse_close_fill(_close_payload(pl="45.23"))
        assert result.realised_pl == Decimal("45.23")

    def test_negative_pl_for_losing_trade(self) -> None:
        result = _parse_close_fill(_close_payload(pl="-32.10"))
        assert result.realised_pl == Decimal("-32.10")

    def test_returns_close_fill_dataclass(self) -> None:
        result = _parse_close_fill(_close_payload())
        assert isinstance(result, CloseFill)


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


# ---------------------------------------------------------------------------
# _resolve_financing_parents
# ---------------------------------------------------------------------------


def _financing_row(
    oanda_id: str,
    related: list[str] | None = None,
    parent_oanda_id: str | None = None,
) -> TransactionRow:
    """Build a DAILY_FINANCING TransactionRow with optional relatedTransactionIDs."""
    raw: dict = {"id": oanda_id, "type": "DAILY_FINANCING"}
    if related is not None:
        raw["relatedTransactionIDs"] = related
    import json as _json
    return TransactionRow(
        oanda_id=oanda_id,
        account_id="acct-1",
        type="DAILY_FINANCING",
        time="2026-04-25T22:00:00.000000Z",
        parent_oanda_id=parent_oanda_id,
        raw_json=_json.dumps(raw),
    )


def _fill_row(oanda_id: str) -> TransactionRow:
    """Build a non-financing TransactionRow."""
    import json as _json
    return TransactionRow(
        oanda_id=oanda_id,
        account_id="acct-1",
        type="ORDER_FILL",
        time="2026-04-25T10:00:00.000000Z",
        parent_oanda_id=None,
        raw_json=_json.dumps({"id": oanda_id, "type": "ORDER_FILL"}),
    )


class TestResolveFinancingParents:
    def test_empty_list_returns_empty(self) -> None:
        assert _resolve_financing_parents([]) == []

    def test_no_financing_rows_returns_list_unchanged(self) -> None:
        rows = [_fill_row("1"), _fill_row("2")]
        result = _resolve_financing_parents(rows)
        assert result == rows

    def test_parent_and_children_linked(self) -> None:
        """Children listed in parent's relatedTransactionIDs get parent_oanda_id set."""
        parent = _financing_row("1000", related=["1001", "1002"])
        child_a = _financing_row("1001")
        child_b = _financing_row("1002")
        result = _resolve_financing_parents([parent, child_a, child_b])
        by_id = {r.oanda_id: r for r in result}
        assert by_id["1001"].parent_oanda_id == "1000"
        assert by_id["1002"].parent_oanda_id == "1000"

    def test_parent_row_itself_unchanged(self) -> None:
        """The parent's own parent_oanda_id must remain None."""
        parent = _financing_row("1000", related=["1001"])
        child = _financing_row("1001")
        result = _resolve_financing_parents([parent, child])
        by_id = {r.oanda_id: r for r in result}
        assert by_id["1000"].parent_oanda_id is None

    def test_multiple_financing_batches_in_one_list(self) -> None:
        """Two independent financing batches on the same day link correctly."""
        parent_a = _financing_row("100", related=["101"])
        child_a = _financing_row("101")
        parent_b = _financing_row("200", related=["201", "202"])
        child_b1 = _financing_row("201")
        child_b2 = _financing_row("202")
        result = _resolve_financing_parents(
            [parent_a, child_a, parent_b, child_b1, child_b2]
        )
        by_id = {r.oanda_id: r for r in result}
        assert by_id["101"].parent_oanda_id == "100"
        assert by_id["201"].parent_oanda_id == "200"
        assert by_id["202"].parent_oanda_id == "200"

    def test_non_financing_rows_interspersed_are_unchanged(self) -> None:
        """ORDER_FILL rows in a mixed batch must pass through untouched."""
        fill = _fill_row("999")
        parent = _financing_row("1000", related=["1001"])
        child = _financing_row("1001")
        result = _resolve_financing_parents([fill, parent, child])
        by_id = {r.oanda_id: r for r in result}
        assert by_id["999"].parent_oanda_id is None
        assert by_id["999"].type == "ORDER_FILL"

    def test_financing_row_without_related_ids_left_alone(self) -> None:
        """A DAILY_FINANCING row with no relatedTransactionIDs is treated as a child
        from a prior batch — parent_oanda_id stays None."""
        child_only = _financing_row("500")  # no related list → cross-batch case
        result = _resolve_financing_parents([child_only])
        assert result[0].parent_oanda_id is None

    def test_row_count_unchanged(self) -> None:
        parent = _financing_row("1000", related=["1001", "1002"])
        rows = [parent, _financing_row("1001"), _financing_row("1002")]
        result = _resolve_financing_parents(rows)
        assert len(result) == len(rows)


# ---------------------------------------------------------------------------
# _compute_conversion_rate
# ---------------------------------------------------------------------------


class TestComputeConversionRate:
    # ---- trivial path -------------------------------------------------------

    def test_same_currency_returns_one(self) -> None:
        """No lookup needed when currency already is home."""
        result = _compute_conversion_rate("USD", "USD", {})
        assert result == Decimal("1")

    def test_same_currency_ignores_mids_dict(self) -> None:
        """The mids dict is irrelevant when currency == home."""
        result = _compute_conversion_rate("GBP", "GBP", {"GBP_GBP": Decimal("99")})
        assert result == Decimal("1")

    # ---- direct quote -------------------------------------------------------

    def test_direct_quote_eur_usd(self) -> None:
        """EUR_USD on a USD account: direct pair exists."""
        mids = {"EUR_USD": Decimal("1.0800")}
        result = _compute_conversion_rate("EUR", "USD", mids)
        assert result == Decimal("1.0800")

    def test_direct_quote_gbp_usd(self) -> None:
        mids = {"GBP_USD": Decimal("1.2700")}
        result = _compute_conversion_rate("GBP", "USD", mids)
        assert result == Decimal("1.2700")

    def test_direct_quote_returns_decimal(self) -> None:
        mids = {"AUD_USD": Decimal("0.6500")}
        result = _compute_conversion_rate("AUD", "USD", mids)
        assert isinstance(result, Decimal)

    # ---- inverted quote -----------------------------------------------------

    def test_inverted_quote_usd_chf(self) -> None:
        """CHF→USD when only USD_CHF is available: invert the rate."""
        mids = {"USD_CHF": Decimal("0.8900")}
        result = _compute_conversion_rate("CHF", "USD", mids)
        assert result == Decimal("1") / Decimal("0.8900")

    def test_inverted_quote_usd_jpy(self) -> None:
        """JPY→USD when only USD_JPY is available."""
        mids = {"USD_JPY": Decimal("150.00")}
        result = _compute_conversion_rate("JPY", "USD", mids)
        assert result == Decimal("1") / Decimal("150.00")

    def test_direct_takes_priority_over_inverted(self) -> None:
        """When both direct and inverted are present, direct wins."""
        mids = {
            "EUR_USD": Decimal("1.0800"),
            "USD_EUR": Decimal("0.9259"),
        }
        result = _compute_conversion_rate("EUR", "USD", mids)
        assert result == Decimal("1.0800")

    # ---- missing pair → ValueError -----------------------------------------

    def test_neither_pair_raises_value_error(self) -> None:
        with pytest.raises(ValueError, match="Cannot convert"):
            _compute_conversion_rate("EUR", "USD", {})

    def test_error_message_names_both_pairs(self) -> None:
        """The error text should name the two pairs that were tried."""
        with pytest.raises(ValueError, match="GBP_USD") as exc_info:
            _compute_conversion_rate("GBP", "USD", {})
        assert "USD_GBP" in str(exc_info.value)

    def test_irrelevant_pairs_do_not_help(self) -> None:
        """Having GBP_EUR in mids does not satisfy a GBP→USD lookup."""
        with pytest.raises(ValueError):
            _compute_conversion_rate("GBP", "USD", {"GBP_EUR": Decimal("1.15")})

    # ---- cross-pair scenario (USD account, EUR_GBP instrument) -------------

    def test_cross_pair_base_leg(self) -> None:
        """EUR→USD conversion for the base leg of EUR_GBP on a USD account."""
        mids = {"EUR_USD": Decimal("1.0800")}
        assert _compute_conversion_rate("EUR", "USD", mids) == Decimal("1.0800")

    def test_cross_pair_quote_leg(self) -> None:
        """GBP→USD conversion for the quote leg of EUR_GBP on a USD account."""
        mids = {"GBP_USD": Decimal("1.2700")}
        assert _compute_conversion_rate("GBP", "USD", mids) == Decimal("1.2700")

    def test_cross_pair_quote_leg_via_inversion(self) -> None:
        """JPY→USD for USD_JPY instrument as the quote leg (inverted)."""
        mids = {"USD_JPY": Decimal("150.00")}
        result = _compute_conversion_rate("JPY", "USD", mids)
        assert result == Decimal("1") / Decimal("150.00")
