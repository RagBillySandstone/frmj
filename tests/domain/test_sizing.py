"""Tests for the position sizing module.

We aim to lock down the *contract* of ``compute_units``: floor-to-increment,
correct Oanda margin formula, and clear failure modes for sub-minimum
sizing or invalid inputs. We use realistic-looking fixtures (EUR_USD on a
USD account, USD_JPY on a USD account) to catch unit-of-measure bugs that
toy values would mask.
"""

from decimal import Decimal

import pytest

from frmj.domain.sizing import (
    BelowMinimumUnits,
    Direction,
    InstrumentSpec,
    PriceQuote,
    compute_units,
    margin_per_unit,
)


# ---------------------------------------------------------------------------
# Fixtures: instrument + quote builders
# ---------------------------------------------------------------------------


def _eur_usd() -> InstrumentSpec:
    """Vanilla 50:1 FX pair on a USD account."""
    return InstrumentSpec(
        name="EUR_USD",
        pip_location=-4,
        margin_rate=Decimal("0.02"),
        min_units=1,
        units_increment=1,
    )


def _eur_usd_quote(price: Decimal = Decimal("1.10")) -> PriceQuote:
    # On a USD account, a USD-quoted pair has quote_to_home = 1 and
    # base_to_home = current EUR/USD price.
    return PriceQuote(
        bid=price - Decimal("0.0001"),
        ask=price + Decimal("0.0001"),
        quote_to_home=Decimal("1"),
        base_to_home=price,
    )


def _usd_jpy() -> InstrumentSpec:
    return InstrumentSpec(
        name="USD_JPY",
        pip_location=-2,
        margin_rate=Decimal("0.04"),
        min_units=1,
        units_increment=1,
    )


def _usd_jpy_quote(price: Decimal = Decimal("150.00")) -> PriceQuote:
    # On a USD account: base = USD so base_to_home = 1; quote = JPY so
    # quote_to_home = 1 / price.
    return PriceQuote(
        bid=price - Decimal("0.01"),
        ask=price + Decimal("0.01"),
        quote_to_home=Decimal("1") / price,
        base_to_home=Decimal("1"),
    )


# ---------------------------------------------------------------------------
# InstrumentSpec / PriceQuote validation
# ---------------------------------------------------------------------------


class TestInstrumentSpecValidation:
    @pytest.mark.parametrize("rate", [Decimal("0"), Decimal("-0.01"), Decimal("1.5")])
    def test_rejects_bad_margin_rate(self, rate: Decimal) -> None:
        with pytest.raises(ValueError):
            InstrumentSpec(
                name="X",
                pip_location=-4,
                margin_rate=rate,
                min_units=1,
                units_increment=1,
            )

    def test_rejects_zero_min_units(self) -> None:
        with pytest.raises(ValueError):
            InstrumentSpec(
                name="X",
                pip_location=-4,
                margin_rate=Decimal("0.02"),
                min_units=0,
                units_increment=1,
            )

    def test_rejects_zero_increment(self) -> None:
        with pytest.raises(ValueError):
            InstrumentSpec(
                name="X",
                pip_location=-4,
                margin_rate=Decimal("0.02"),
                min_units=1,
                units_increment=0,
            )


class TestPriceQuoteValidation:
    def test_rejects_crossed_book(self) -> None:
        with pytest.raises(ValueError):
            PriceQuote(
                bid=Decimal("1.10"),
                ask=Decimal("1.09"),
                quote_to_home=Decimal("1"),
                base_to_home=Decimal("1.10"),
            )

    def test_rejects_nonpositive_prices(self) -> None:
        with pytest.raises(ValueError):
            PriceQuote(
                bid=Decimal("0"),
                ask=Decimal("0.0001"),
                quote_to_home=Decimal("1"),
                base_to_home=Decimal("1.10"),
            )

    def test_rejects_nonpositive_conversion(self) -> None:
        with pytest.raises(ValueError):
            PriceQuote(
                bid=Decimal("1.10"),
                ask=Decimal("1.11"),
                quote_to_home=Decimal("0"),
                base_to_home=Decimal("1.10"),
            )

    def test_mid_is_average(self) -> None:
        q = _eur_usd_quote(Decimal("1.20"))
        assert q.mid == Decimal("1.20")

    def test_entry_price_uses_relevant_side(self) -> None:
        q = _eur_usd_quote(Decimal("1.20"))
        # Buying lifts the offer; selling hits the bid.
        assert q.entry_price(Direction.LONG) == q.ask
        assert q.entry_price(Direction.SHORT) == q.bid


# ---------------------------------------------------------------------------
# margin_per_unit and compute_units happy paths
# ---------------------------------------------------------------------------


class TestMarginPerUnit:
    def test_eur_usd_margin_is_2pct_of_eur_price(self) -> None:
        # 1 EUR at 1.10 USD/EUR is worth 1.10 USD; 2% margin = 0.022 USD.
        assert margin_per_unit(_eur_usd(), Decimal("1.10")) == Decimal("0.022")

    def test_usd_jpy_margin_uses_base_to_home(self) -> None:
        # 1 USD has base_to_home 1.0 on a USD account; 4% margin = 0.04 USD.
        assert margin_per_unit(_usd_jpy(), Decimal("1")) == Decimal("0.04")


class TestComputeUnitsHappyPath:
    def test_eur_usd_basic(self) -> None:
        # $220 of capital at 0.022 USD/unit → 10,000 units exactly.
        result = compute_units(
            capital_to_deploy=Decimal("220"),
            spec=_eur_usd(),
            quote=_eur_usd_quote(Decimal("1.10")),
            direction=Direction.LONG,
        )
        assert result.units == 10_000
        assert result.margin_used == Decimal("220.000")
        assert result.capital_unused == Decimal("0.000")

    def test_floors_when_capital_doesnt_divide_evenly(self) -> None:
        # $220.05 / 0.022 = 10_002.27...  → floor to 10_002.
        result = compute_units(
            capital_to_deploy=Decimal("220.05"),
            spec=_eur_usd(),
            quote=_eur_usd_quote(Decimal("1.10")),
            direction=Direction.LONG,
        )
        assert result.units == 10_002
        # margin_used should be ≤ capital_to_deploy by construction
        assert result.margin_used <= Decimal("220.05")
        assert result.capital_unused >= 0

    def test_short_uses_same_units_as_long(self) -> None:
        # Margin is symmetric for FX on Oanda; flipping direction shouldn't
        # change the unit count.
        common = dict(
            capital_to_deploy=Decimal("220"),
            spec=_eur_usd(),
            quote=_eur_usd_quote(Decimal("1.10")),
        )
        long_result = compute_units(direction=Direction.LONG, **common)
        short_result = compute_units(direction=Direction.SHORT, **common)
        assert long_result.units == short_result.units

    def test_usd_jpy_sizing(self) -> None:
        # $400 at 0.04 USD/unit (base = USD on a USD account) → 10_000 units.
        result = compute_units(
            capital_to_deploy=Decimal("400"),
            spec=_usd_jpy(),
            quote=_usd_jpy_quote(Decimal("150")),
            direction=Direction.LONG,
        )
        assert result.units == 10_000
        assert result.margin_used == Decimal("400.00")


class TestUnitsIncrement:
    def test_increment_floors_to_lot_size(self) -> None:
        # Increment 1000 means we must trade in 1000-unit lots. 17,432.7
        # affordable units → 17,000 actual.
        spec = InstrumentSpec(
            name="XAU_USD",
            pip_location=-2,
            margin_rate=Decimal("0.05"),
            min_units=1000,
            units_increment=1000,
        )
        # Per-unit margin = 0.05 * 2_000 = 100; $1_750_000 / 100 = 17_500
        # Pick a number that doesn't divide cleanly: $1_743_270.
        quote = PriceQuote(
            bid=Decimal("1999"),
            ask=Decimal("2001"),
            quote_to_home=Decimal("1"),
            base_to_home=Decimal("2000"),
        )
        result = compute_units(
            capital_to_deploy=Decimal("1743270"),
            spec=spec,
            quote=quote,
            direction=Direction.LONG,
        )
        # Per-unit = 100; raw = 17432.7; floor to 1000 → 17000.
        assert result.units == 17_000
        assert result.margin_used == Decimal("1700000")


# ---------------------------------------------------------------------------
# Error paths
# ---------------------------------------------------------------------------


class TestErrors:
    def test_below_minimum_units_raises(self) -> None:
        # Tiny capital on a min-1000-unit instrument.
        spec = InstrumentSpec(
            name="XAU_USD",
            pip_location=-2,
            margin_rate=Decimal("0.05"),
            min_units=1000,
            units_increment=1000,
        )
        quote = PriceQuote(
            bid=Decimal("1999"),
            ask=Decimal("2001"),
            quote_to_home=Decimal("1"),
            base_to_home=Decimal("2000"),
        )
        with pytest.raises(BelowMinimumUnits) as ei:
            compute_units(
                capital_to_deploy=Decimal("50"),
                spec=spec,
                quote=quote,
                direction=Direction.LONG,
            )
        assert ei.value.min_units == 1000
        assert ei.value.computed_units == 0
        assert ei.value.instrument == "XAU_USD"

    @pytest.mark.parametrize("bad", [Decimal("0"), Decimal("-1")])
    def test_nonpositive_capital_raises(self, bad: Decimal) -> None:
        with pytest.raises(ValueError):
            compute_units(
                capital_to_deploy=bad,
                spec=_eur_usd(),
                quote=_eur_usd_quote(),
                direction=Direction.LONG,
            )


# ---------------------------------------------------------------------------
# Invariants
# ---------------------------------------------------------------------------


class TestInvariants:
    @pytest.mark.parametrize(
        "capital",
        [Decimal("100"), Decimal("220.50"), Decimal("999.99"), Decimal("12345.67")],
    )
    def test_margin_used_never_exceeds_capital(self, capital: Decimal) -> None:
        result = compute_units(
            capital_to_deploy=capital,
            spec=_eur_usd(),
            quote=_eur_usd_quote(Decimal("1.10")),
            direction=Direction.LONG,
        )
        assert result.margin_used <= capital
        assert result.capital_unused >= 0
        # And the two should reconcile exactly.
        assert result.margin_used + result.capital_unused == capital
