"""Tests for the pricing / exit-levels module.

Coverage strategy:
* Both TP and SL, independently and together.
* Both PIPS and PERCENT_RETURN kinds.
* Both LONG and SHORT directions (sign flips are the most common source of
  bugs in pricing math, so we explicitly assert both sides of every case).
* Round-trip: if you specify a TP in PIPS, the projected_profit_home should
  equal what you'd get specifying the same target in PERCENT_RETURN — and
  vice versa.
* Unrealistic-threshold warnings fire at the right boundary.
* Input validation rejects bad values early.

Fixture design: we use EUR_USD on a USD account with a round price (1.10)
so all intermediate values are easy to verify by hand. USD_JPY is added for
the JPY pip_location=-2 case.
"""

from decimal import Decimal

import pytest

from frmj.domain.pricing import (
    UNREALISTIC_PIP_THRESHOLD,
    UNREALISTIC_RETURN_THRESHOLD,
    ExitLevels,
    LimitEntryKind,
    LimitEntrySpec,
    TPSLKind,
    TPSLSpec,
    compute_exit_levels,
    compute_limit_price,
    pip_size,
    pip_value_home,
)
from frmj.domain.sizing import Direction, InstrumentSpec, PriceQuote


# ---------------------------------------------------------------------------
# Shared fixtures
# ---------------------------------------------------------------------------


def _eur_usd_spec() -> InstrumentSpec:
    return InstrumentSpec(
        name="EUR_USD",
        pip_location=-4,
        margin_rate=Decimal("0.02"),
        min_units=1,
        units_increment=1,
        display_precision=5,
    )


def _usd_jpy_spec() -> InstrumentSpec:
    return InstrumentSpec(
        name="USD_JPY",
        pip_location=-2,
        margin_rate=Decimal("0.04"),
        min_units=1,
        units_increment=1,
        display_precision=3,
    )


def _eur_usd_quote(price: Decimal = Decimal("1.10")) -> PriceQuote:
    # USD account; EUR_USD quoted as base=EUR, quote=USD.
    # quote_to_home = 1 (quote is already USD).
    # base_to_home  = EUR/USD price.
    return PriceQuote(
        bid=price - Decimal("0.0002"),
        ask=price + Decimal("0.0002"),
        quote_to_home=Decimal("1"),
        base_to_home=price,
    )


def _usd_jpy_quote(price: Decimal = Decimal("150.00")) -> PriceQuote:
    # USD account; USD_JPY quoted as base=USD, quote=JPY.
    # quote_to_home = 1/price (JPY → USD).
    # base_to_home  = 1 (base IS USD).
    return PriceQuote(
        bid=price - Decimal("0.01"),
        ask=price + Decimal("0.01"),
        quote_to_home=Decimal("1") / price,
        base_to_home=Decimal("1"),
    )


def _call(
    *,
    entry: Decimal = Decimal("1.10"),
    units: int = 10_000,
    direction: Direction = Direction.LONG,
    spec: InstrumentSpec | None = None,
    quote: PriceQuote | None = None,
    margin: Decimal = Decimal("220"),  # 10_000 units * 0.02 * 1.10 USD/EUR
    tp: TPSLSpec | None = None,
    sl: TPSLSpec | None = None,
) -> ExitLevels:
    """Thin wrapper that fills in EUR_USD defaults so individual tests only
    spell out the parameters that matter for their assertion."""
    return compute_exit_levels(
        entry_price=entry,
        units=units,
        direction=direction,
        spec=spec or _eur_usd_spec(),
        quote=quote or _eur_usd_quote(entry),
        margin_used=margin,
        take_profit=tp,
        stop_loss=sl,
    )


# ---------------------------------------------------------------------------
# pip_size helper
# ---------------------------------------------------------------------------


class TestPipSize:
    def test_eur_usd_pip(self) -> None:
        assert pip_size(_eur_usd_spec()) == Decimal("0.0001")

    def test_usd_jpy_pip(self) -> None:
        assert pip_size(_usd_jpy_spec()) == Decimal("0.01")


# ---------------------------------------------------------------------------
# Both sides None → empty result, no error
# ---------------------------------------------------------------------------


class TestNothingSpecified:
    def test_no_tp_no_sl_is_valid(self) -> None:
        result = _call()
        assert result.take_profit_price is None
        assert result.stop_loss_price is None
        assert result.projected_profit_home is None
        assert result.projected_loss_home is None
        assert result.warnings == ()


# ---------------------------------------------------------------------------
# TPSLSpec validation
# ---------------------------------------------------------------------------


class TestTPSLSpecValidation:
    @pytest.mark.parametrize("bad", [Decimal("0"), Decimal("-1"), Decimal("-0.001")])
    def test_nonpositive_value_raises(self, bad: Decimal) -> None:
        with pytest.raises(ValueError):
            TPSLSpec(kind=TPSLKind.PIPS, value=bad)


# ---------------------------------------------------------------------------
# PIPS mode — take profit
# ---------------------------------------------------------------------------


class TestPipsTakeProfit:
    def test_long_tp_price_is_above_entry(self) -> None:
        result = _call(
            entry=Decimal("1.10000"),
            tp=TPSLSpec(TPSLKind.PIPS, Decimal("50")),
        )
        # 50 pips up from 1.10000 = 1.10500
        assert result.take_profit_price == Decimal("1.10500")

    def test_short_tp_price_is_below_entry(self) -> None:
        result = _call(
            entry=Decimal("1.10000"),
            direction=Direction.SHORT,
            tp=TPSLSpec(TPSLKind.PIPS, Decimal("50")),
        )
        # 50 pips down from 1.10000 = 1.09500
        assert result.take_profit_price == Decimal("1.09500")

    def test_projected_profit_is_positive(self) -> None:
        result = _call(tp=TPSLSpec(TPSLKind.PIPS, Decimal("50")))
        assert result.projected_profit_home is not None
        assert result.projected_profit_home > 0

    def test_profit_math_eur_usd(self) -> None:
        # 10_000 units * 50 pips * 0.0001 price/pip * 1 (quote_to_home)
        # = 10_000 * 0.005 = 50 USD
        result = _call(
            entry=Decimal("1.10000"),
            units=10_000,
            tp=TPSLSpec(TPSLKind.PIPS, Decimal("50")),
        )
        assert result.projected_profit_home == Decimal("50")

    def test_return_on_margin_at_tp(self) -> None:
        # $50 profit / $220 margin ≈ 22.72%
        result = _call(
            entry=Decimal("1.10000"),
            units=10_000,
            margin=Decimal("220"),
            tp=TPSLSpec(TPSLKind.PIPS, Decimal("50")),
        )
        expected_rom = Decimal("50") / Decimal("220")
        assert result.return_on_margin_at_tp == expected_rom
        assert result.return_on_margin_at_tp > 0

    def test_no_warning_within_threshold(self) -> None:
        result = _call(tp=TPSLSpec(TPSLKind.PIPS, Decimal("100")))
        assert result.warnings == ()

    def test_warning_beyond_pip_threshold(self) -> None:
        result = _call(tp=TPSLSpec(TPSLKind.PIPS, UNREALISTIC_PIP_THRESHOLD + 1))
        assert any("take-profit" in w and "pips" in w for w in result.warnings)


# ---------------------------------------------------------------------------
# PIPS mode — stop loss
# ---------------------------------------------------------------------------


class TestPipsStopLoss:
    def test_long_sl_price_is_below_entry(self) -> None:
        result = _call(
            entry=Decimal("1.10000"),
            sl=TPSLSpec(TPSLKind.PIPS, Decimal("30")),
        )
        assert result.stop_loss_price == Decimal("1.09700")

    def test_short_sl_price_is_above_entry(self) -> None:
        result = _call(
            entry=Decimal("1.10000"),
            direction=Direction.SHORT,
            sl=TPSLSpec(TPSLKind.PIPS, Decimal("30")),
        )
        assert result.stop_loss_price == Decimal("1.10300")

    def test_projected_loss_is_negative(self) -> None:
        result = _call(sl=TPSLSpec(TPSLKind.PIPS, Decimal("30")))
        assert result.projected_loss_home is not None
        assert result.projected_loss_home < 0

    def test_loss_math_eur_usd(self) -> None:
        # 10_000 units * 30 pips * 0.0001 * 1 = 30 USD → loss = -30
        result = _call(
            entry=Decimal("1.10000"),
            units=10_000,
            sl=TPSLSpec(TPSLKind.PIPS, Decimal("30")),
        )
        assert result.projected_loss_home == Decimal("-30")

    def test_return_on_margin_at_sl_is_negative(self) -> None:
        result = _call(
            units=10_000,
            margin=Decimal("220"),
            sl=TPSLSpec(TPSLKind.PIPS, Decimal("30")),
        )
        assert result.return_on_margin_at_sl is not None
        assert result.return_on_margin_at_sl < 0

    def test_warning_beyond_pip_threshold(self) -> None:
        result = _call(sl=TPSLSpec(TPSLKind.PIPS, UNREALISTIC_PIP_THRESHOLD + 1))
        assert any("stop-loss" in w and "pips" in w for w in result.warnings)


# ---------------------------------------------------------------------------
# PERCENT_RETURN mode — take profit
# ---------------------------------------------------------------------------


class TestPercentTakeProfit:
    def test_profit_equals_margin_times_fraction(self) -> None:
        # 10% return on $220 margin = $22 profit.
        result = _call(
            margin=Decimal("220"),
            tp=TPSLSpec(TPSLKind.PERCENT_RETURN, Decimal("0.10")),
        )
        assert result.projected_profit_home == Decimal("22")

    def test_rom_equals_fraction(self) -> None:
        # By definition the return on margin at TP should equal the spec value.
        result = _call(
            margin=Decimal("220"),
            tp=TPSLSpec(TPSLKind.PERCENT_RETURN, Decimal("0.10")),
        )
        assert result.return_on_margin_at_tp == Decimal("0.10")

    def test_long_tp_price_is_above_entry(self) -> None:
        result = _call(
            entry=Decimal("1.10000"),
            units=10_000,
            margin=Decimal("220"),
            tp=TPSLSpec(TPSLKind.PERCENT_RETURN, Decimal("0.10")),
        )
        # $22 profit / (10_000 units * 1 quote_to_home) = 0.0022 price move up
        assert result.take_profit_price == Decimal("1.10000") + Decimal("0.0022")

    def test_short_tp_price_is_below_entry(self) -> None:
        result = _call(
            entry=Decimal("1.10000"),
            units=10_000,
            margin=Decimal("220"),
            direction=Direction.SHORT,
            tp=TPSLSpec(TPSLKind.PERCENT_RETURN, Decimal("0.10")),
        )
        assert result.take_profit_price == Decimal("1.10000") - Decimal("0.0022")

    def test_warning_beyond_return_threshold(self) -> None:
        # 110% return — just over the 100% threshold.
        result = _call(
            tp=TPSLSpec(
                TPSLKind.PERCENT_RETURN, UNREALISTIC_RETURN_THRESHOLD + Decimal("0.1")
            )
        )
        assert any("take-profit" in w and "return" in w for w in result.warnings)

    def test_price_quantized_to_display_precision(self) -> None:
        """A percent-return TP that doesn't divide evenly must still land on
        a broker-legal price — regression test for the 400 Bad Request Oanda
        returns when a submitted price carries more decimals than
        ``displayPrecision`` allows."""
        result = _call(
            entry=Decimal("1.10000"),
            units=3333,
            margin=Decimal("221.34"),
            tp=TPSLSpec(TPSLKind.PERCENT_RETURN, Decimal("0.037")),
        )
        assert result.take_profit_price is not None
        # EUR_USD display_precision=5 in the fixture spec.
        assert -result.take_profit_price.as_tuple().exponent <= 5

    def test_no_warning_at_exactly_threshold(self) -> None:
        # Exactly 100% should NOT warn (boundary is strictly greater than).
        result = _call(
            tp=TPSLSpec(TPSLKind.PERCENT_RETURN, UNREALISTIC_RETURN_THRESHOLD)
        )
        assert not any("return" in w and "take-profit" in w for w in result.warnings)


# ---------------------------------------------------------------------------
# PERCENT_RETURN mode — stop loss
# ---------------------------------------------------------------------------


class TestPercentStopLoss:
    def test_loss_equals_minus_margin_times_fraction(self) -> None:
        result = _call(
            margin=Decimal("220"),
            sl=TPSLSpec(TPSLKind.PERCENT_RETURN, Decimal("0.05")),
        )
        assert result.projected_loss_home == Decimal("-11")

    def test_rom_is_negative_fraction(self) -> None:
        result = _call(
            margin=Decimal("220"),
            sl=TPSLSpec(TPSLKind.PERCENT_RETURN, Decimal("0.05")),
        )
        assert result.return_on_margin_at_sl == Decimal("-0.05")

    def test_long_sl_price_is_below_entry(self) -> None:
        result = _call(
            entry=Decimal("1.10000"),
            units=10_000,
            margin=Decimal("220"),
            sl=TPSLSpec(TPSLKind.PERCENT_RETURN, Decimal("0.05")),
        )
        # $11 loss / (10_000 * 1) = 0.0011 price move down
        assert result.stop_loss_price == Decimal("1.10000") - Decimal("0.0011")

    def test_short_sl_price_is_above_entry(self) -> None:
        result = _call(
            entry=Decimal("1.10000"),
            units=10_000,
            margin=Decimal("220"),
            direction=Direction.SHORT,
            sl=TPSLSpec(TPSLKind.PERCENT_RETURN, Decimal("0.05")),
        )
        assert result.stop_loss_price == Decimal("1.10000") + Decimal("0.0011")

    def test_warning_beyond_return_threshold(self) -> None:
        result = _call(
            sl=TPSLSpec(
                TPSLKind.PERCENT_RETURN, UNREALISTIC_RETURN_THRESHOLD + Decimal("0.1")
            )
        )
        assert any("stop-loss" in w and "return" in w for w in result.warnings)


# ---------------------------------------------------------------------------
# Round-trip: PIPS ↔ PERCENT_RETURN should agree on P/L
# ---------------------------------------------------------------------------


class TestRoundTrip:
    def test_pips_and_percent_tp_give_same_profit(self) -> None:
        """Express the same TP in both modes; the projected profit must agree.

        We pick numbers that produce an exact, terminating Decimal so the
        two computation paths land on the same value without any rounding
        divergence. 50 pips on 10_000 EUR_USD (quote_to_home=1) = $50
        exactly; $50 / $500 margin = 0.10 exactly (terminates).
        """
        result_pips = _call(
            units=10_000,
            margin=Decimal("500"),
            tp=TPSLSpec(TPSLKind.PIPS, Decimal("50")),
        )
        result_pct = _call(
            units=10_000,
            margin=Decimal("500"),
            tp=TPSLSpec(TPSLKind.PERCENT_RETURN, Decimal("0.10")),
        )
        assert result_pips.projected_profit_home == result_pct.projected_profit_home

    def test_pips_and_percent_sl_give_same_loss(self) -> None:
        # 30 pips on 10_000 EUR_USD = $30; $30 / $300 margin = 0.10 exactly.
        result_pips = _call(
            units=10_000,
            margin=Decimal("300"),
            sl=TPSLSpec(TPSLKind.PIPS, Decimal("30")),
        )
        result_pct = _call(
            units=10_000,
            margin=Decimal("300"),
            sl=TPSLSpec(TPSLKind.PERCENT_RETURN, Decimal("0.10")),
        )
        assert result_pips.projected_loss_home == result_pct.projected_loss_home


# ---------------------------------------------------------------------------
# JPY pair: pip_location=-2 black-box treatment
# ---------------------------------------------------------------------------


class TestJPYPair:
    def test_usd_jpy_long_tp_pips(self) -> None:
        # Use price=100 so quote_to_home = 1/100 = 0.01 exactly (terminates).
        # 30 pips * 0.01/pip * 10_000 units * 0.01 = 30 USD exactly.
        spec = _usd_jpy_spec()
        quote = _usd_jpy_quote(Decimal("100.00"))
        result = compute_exit_levels(
            entry_price=Decimal("100.00"),
            units=10_000,
            direction=Direction.LONG,
            spec=spec,
            quote=quote,
            margin_used=Decimal("400"),
            take_profit=TPSLSpec(TPSLKind.PIPS, Decimal("30")),
            stop_loss=None,
        )
        # TP price = 100.00 + 30 * 0.01 = 100.30
        assert result.take_profit_price == Decimal("100.30")
        # Profit = 30 * 0.01 * 10_000 * (1/100) = 30 USD exactly
        assert result.projected_profit_home == Decimal("30")

    def test_usd_jpy_pip_is_one_cent(self) -> None:
        assert pip_size(_usd_jpy_spec()) == Decimal("0.01")


# ---------------------------------------------------------------------------
# Multiple warnings can accumulate
# ---------------------------------------------------------------------------


class TestMultipleWarnings:
    def test_both_tp_and_sl_can_warn(self) -> None:
        big = UNREALISTIC_PIP_THRESHOLD + 1
        result = _call(
            tp=TPSLSpec(TPSLKind.PIPS, big),
            sl=TPSLSpec(TPSLKind.PIPS, big),
        )
        # Expect at least two warnings — one for TP, one for SL.
        assert len(result.warnings) >= 2
        assert any("take-profit" in w for w in result.warnings)
        assert any("stop-loss" in w for w in result.warnings)


# ---------------------------------------------------------------------------
# Input validation
# ---------------------------------------------------------------------------


class TestInputValidation:
    @pytest.mark.parametrize("bad", [Decimal("0"), Decimal("-1")])
    def test_nonpositive_entry_raises(self, bad: Decimal) -> None:
        with pytest.raises(ValueError):
            _call(entry=bad)

    def test_zero_entry_with_explicit_quote_reaches_entry_validation(self) -> None:
        """Cover the ``entry_price <= 0`` guard inside ``compute_exit_levels``.

        ``_call(entry=Decimal("0"))`` constructs ``_eur_usd_quote(Decimal("0"))``
        which itself raises from ``PriceQuote.__post_init__`` (bid becomes
        -0.0002).  Supplying an explicit valid quote bypasses that so the guard
        on line 231 of pricing.py is actually reached.
        """
        with pytest.raises(ValueError, match="entry_price must be positive"):
            compute_exit_levels(
                entry_price=Decimal("0"),
                units=10_000,
                direction=Direction.LONG,
                spec=_eur_usd_spec(),
                quote=_eur_usd_quote(),  # valid; constructed at price 1.10
                margin_used=Decimal("220"),
                take_profit=None,
                stop_loss=None,
            )

    @pytest.mark.parametrize("bad_units", [0, -1])
    def test_nonpositive_units_raises(self, bad_units: int) -> None:
        with pytest.raises(ValueError):
            compute_exit_levels(
                entry_price=Decimal("1.10"),
                units=bad_units,
                direction=Direction.LONG,
                spec=_eur_usd_spec(),
                quote=_eur_usd_quote(),
                margin_used=Decimal("220"),
                take_profit=None,
                stop_loss=None,
            )

    @pytest.mark.parametrize("bad", [Decimal("0"), Decimal("-1")])
    def test_nonpositive_margin_raises(self, bad: Decimal) -> None:
        with pytest.raises(ValueError):
            _call(margin=bad)


# ---------------------------------------------------------------------------
# pip_value_home
# ---------------------------------------------------------------------------


class TestPipValueHome:
    def test_eur_usd_usd_account(self) -> None:
        # 10_000 units EUR_USD, quote_to_home=1: pip = 10_000 * 0.0001 * 1 = $1.00
        result = pip_value_home(10_000, _eur_usd_spec(), _eur_usd_quote())
        assert result == Decimal("1")

    def test_scales_linearly_with_units(self) -> None:
        # Doubling units doubles pip value.
        small = pip_value_home(5_000, _eur_usd_spec(), _eur_usd_quote())
        large = pip_value_home(10_000, _eur_usd_spec(), _eur_usd_quote())
        assert large == 2 * small

    def test_usd_jpy_inverted_quote_to_home(self) -> None:
        # USD_JPY at 100.00: quote_to_home = 1/100, pip_size = 0.01
        # pip_value = 10_000 * 0.01 * (1/100) = $1.00
        spec = _usd_jpy_spec()
        quote = _usd_jpy_quote(Decimal("100.00"))
        result = pip_value_home(10_000, spec, quote)
        assert result == Decimal("1")

    def test_returns_decimal(self) -> None:
        result = pip_value_home(1_000, _eur_usd_spec(), _eur_usd_quote())
        assert isinstance(result, Decimal)

    def test_pip_pct_of_margin_eur_usd(self) -> None:
        # 10_000 EUR_USD units at price 1.10, margin_rate 0.02:
        #   margin = 10_000 * 0.02 * 1.10 = $220
        #   pip_value = $1.00
        #   pip_pct = 1/220 * 100 ≈ 0.4545%
        pv = pip_value_home(10_000, _eur_usd_spec(), _eur_usd_quote(Decimal("1.10")))
        margin = Decimal("10000") * Decimal("0.02") * Decimal("1.10")
        pip_pct = pv / margin * Decimal("100")
        assert round(pip_pct, 4) == round(Decimal("1") / margin * 100, 4)

    def test_always_positive(self) -> None:
        result = pip_value_home(1_000, _eur_usd_spec(), _eur_usd_quote())
        assert result > 0


# ---------------------------------------------------------------------------
# Limit-order entry price
# ---------------------------------------------------------------------------
#
# _eur_usd_quote() has bid 1.0998 / ask 1.1002, so a long's offsets are
# measured from 1.1002 and a short's from 1.0998.


def _limit(
    kind: LimitEntryKind,
    value: str,
    direction: Direction,
    spec: InstrumentSpec | None = None,
    quote: PriceQuote | None = None,
) -> Decimal:
    return compute_limit_price(
        entry=LimitEntrySpec(kind=kind, value=Decimal(value)),
        direction=direction,
        spec=spec or _eur_usd_spec(),
        quote=quote or _eur_usd_quote(),
    )


class TestLimitEntrySpecValidation:
    @pytest.mark.parametrize("value", ["0", "-5"])
    def test_non_positive_value_rejected(self, value: str) -> None:
        with pytest.raises(ValueError):
            LimitEntrySpec(kind=LimitEntryKind.PIPS, value=Decimal(value))


class TestComputeLimitPrice:
    def test_long_pips_below_ask(self) -> None:
        assert _limit(LimitEntryKind.PIPS, "15", Direction.LONG) == Decimal("1.09870")

    def test_short_pips_above_bid(self) -> None:
        assert _limit(LimitEntryKind.PIPS, "15", Direction.SHORT) == Decimal("1.10130")

    def test_long_percent_of_price(self) -> None:
        # 1.1002 * 0.99 = 1.089198 -> rounded to 5 dp.
        assert _limit(
            LimitEntryKind.PERCENT_OF_PRICE, "0.01", Direction.LONG
        ) == Decimal("1.08920")

    def test_short_percent_of_price(self) -> None:
        # 1.0998 * 1.01 = 1.110798 -> rounded to 5 dp.
        assert _limit(
            LimitEntryKind.PERCENT_OF_PRICE, "0.01", Direction.SHORT
        ) == Decimal("1.11080")

    def test_absolute_price_is_rounded(self) -> None:
        assert _limit(LimitEntryKind.PRICE, "1.095004", Direction.LONG) == Decimal(
            "1.09500"
        )

    def test_absolute_inside_spread_is_accepted(self) -> None:
        """Below the ask is enough for a long, even if above the bid."""
        assert _limit(LimitEntryKind.PRICE, "1.1000", Direction.LONG) == Decimal(
            "1.10000"
        )

    def test_jpy_pair_uses_its_pip_size(self) -> None:
        # ask 150.01 - 20 pips of 0.01 = 149.81
        price = _limit(
            LimitEntryKind.PIPS,
            "20",
            Direction.LONG,
            spec=_usd_jpy_spec(),
            quote=_usd_jpy_quote(),
        )
        assert price == Decimal("149.810")

    @pytest.mark.parametrize("value", ["1.1002", "1.1100"])
    def test_long_at_or_above_ask_rejected(self, value: str) -> None:
        with pytest.raises(ValueError, match="fill immediately"):
            _limit(LimitEntryKind.PRICE, value, Direction.LONG)

    @pytest.mark.parametrize("value", ["1.0998", "1.0900"])
    def test_short_at_or_below_bid_rejected(self, value: str) -> None:
        with pytest.raises(ValueError, match="fill immediately"):
            _limit(LimitEntryKind.PRICE, value, Direction.SHORT)

    def test_offset_that_rounds_back_to_ask_rejected(self) -> None:
        """0.001 pips is below the display precision, so it rounds to the ask."""
        with pytest.raises(ValueError, match="fill immediately"):
            _limit(LimitEntryKind.PIPS, "0.001", Direction.LONG)

    def test_offset_past_zero_rejected(self) -> None:
        with pytest.raises(ValueError, match="positive"):
            _limit(LimitEntryKind.PIPS, "20000", Direction.LONG)
