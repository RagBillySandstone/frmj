from decimal import Decimal
from fractions import Fraction

import pytest

from frmj.domain.risk import (
    BlockingMode,
    MaxTradesExceeded,
    RiskConfig,
    RiskStrategy,
    ScaleInForbidden,
    ScaleInPolicy,
    evaluate_trade,
    size_fraction,
)


def _cfg(**overrides) -> RiskConfig:
    defaults = dict(
        max_open_trades=6,
        strategy=RiskStrategy.REMAINING_MARGIN_FRACTION,
        blocking_mode=BlockingMode.HARD_BLOCK,
        scale_in=ScaleInPolicy.NEVER,
        safety_reserve_pct=Decimal("0"),
    )
    defaults.update(overrides)
    return RiskConfig(**defaults)


class TestSizeFraction:
    @pytest.mark.parametrize(
        "n, m, expected",
        [
            (0, 6, Fraction(1, 7)),
            (1, 6, Fraction(1, 6)),
            (2, 6, Fraction(1, 5)),
            (3, 6, Fraction(1, 4)),
            (4, 6, Fraction(1, 3)),
            (5, 6, Fraction(1, 2)),
            (0, 1, Fraction(1, 2)),
            (0, 10, Fraction(1, 11)),
        ],
    )
    def test_canonical_cases(self, n: int, m: int, expected: Fraction) -> None:
        assert size_fraction(n, m) == expected

    def test_returns_exact_rational_not_float(self) -> None:
        assert size_fraction(0, 6) == Fraction(1, 7)
        assert size_fraction(0, 6) * 7 == 1

    def test_at_cap_raises(self) -> None:
        with pytest.raises(MaxTradesExceeded):
            size_fraction(6, 6)

    def test_over_cap_raises(self) -> None:
        with pytest.raises(MaxTradesExceeded):
            size_fraction(7, 6)

    @pytest.mark.parametrize("n, m", [(-1, 6), (0, 0), (0, -1)])
    def test_invalid_inputs(self, n: int, m: int) -> None:
        with pytest.raises(ValueError):
            size_fraction(n, m)

    @pytest.mark.parametrize("m", [1, 2, 3, 6, 20, 100])
    def test_each_trade_consumes_equal_share_of_original(self, m: int) -> None:
        # Walk the full sequence of M trades; each should deploy exactly
        # 1/(M+1) of the *original* margin, leaving 1/(M+1) as buffer.
        remaining = Fraction(1)
        committed = Fraction(0)
        for n in range(m):
            deploy = remaining * size_fraction(n, m)
            assert deploy == Fraction(1, m + 1)
            committed += deploy
            remaining -= deploy
        assert committed == Fraction(m, m + 1)
        assert remaining == Fraction(1, m + 1)


class TestRemainingMarginFraction:
    def test_first_trade_uses_one_seventh_of_usable_margin(self) -> None:
        d = evaluate_trade(
            config=_cfg(),
            open_trades=0,
            open_tickets_on_instrument=0,
            available_margin=Decimal("7000"),
            equity=Decimal("10000"),
        )
        assert d.capital_to_deploy == Decimal("1000")
        assert d.size_fraction == Fraction(1, 7)
        assert d.warnings == ()

    def test_sixth_trade_uses_half(self) -> None:
        d = evaluate_trade(
            config=_cfg(),
            open_trades=5,
            open_tickets_on_instrument=0,
            available_margin=Decimal("1000"),
            equity=Decimal("10000"),
        )
        assert d.capital_to_deploy == Decimal("500")
        assert d.size_fraction == Fraction(1, 2)


class TestSafetyReserve:
    def test_reserve_subtracted_from_available_margin(self) -> None:
        d = evaluate_trade(
            config=_cfg(safety_reserve_pct=Decimal("0.2")),
            open_trades=0,
            open_tickets_on_instrument=0,
            available_margin=Decimal("7000"),
            equity=Decimal("10000"),
        )
        # reserve = 10_000 * 0.2 = 2_000; usable = 7_000 - 2_000 = 5_000; * 1/7
        assert d.capital_to_deploy == Decimal("5000") / Decimal("7")

    def test_reserve_larger_than_margin_yields_zero(self) -> None:
        d = evaluate_trade(
            config=_cfg(safety_reserve_pct=Decimal("0.5")),
            open_trades=0,
            open_tickets_on_instrument=0,
            available_margin=Decimal("3000"),
            equity=Decimal("10000"),
        )
        assert d.capital_to_deploy == Decimal("0")


class TestScaleIn:
    def test_never_raises_when_instrument_has_open_ticket(self) -> None:
        with pytest.raises(ScaleInForbidden):
            evaluate_trade(
                config=_cfg(scale_in=ScaleInPolicy.NEVER),
                open_trades=2,
                open_tickets_on_instrument=1,
                available_margin=Decimal("5000"),
                equity=Decimal("10000"),
            )

    def test_never_proceeds_when_instrument_is_fresh(self) -> None:
        d = evaluate_trade(
            config=_cfg(scale_in=ScaleInPolicy.NEVER),
            open_trades=2,
            open_tickets_on_instrument=0,
            available_margin=Decimal("5000"),
            equity=Decimal("10000"),
        )
        assert d.warnings == ()

    def test_warn_proceeds_with_warning(self) -> None:
        d = evaluate_trade(
            config=_cfg(scale_in=ScaleInPolicy.WARN),
            open_trades=2,
            open_tickets_on_instrument=1,
            available_margin=Decimal("5000"),
            equity=Decimal("10000"),
        )
        assert any("scal" in w.lower() for w in d.warnings)
        assert d.capital_to_deploy > Decimal("0")

    def test_allow_is_silent(self) -> None:
        d = evaluate_trade(
            config=_cfg(scale_in=ScaleInPolicy.ALLOW),
            open_trades=2,
            open_tickets_on_instrument=1,
            available_margin=Decimal("5000"),
            equity=Decimal("10000"),
        )
        assert d.warnings == ()


class TestMaxTradesEnforcement:
    def test_hard_block_raises_at_cap(self) -> None:
        with pytest.raises(MaxTradesExceeded):
            evaluate_trade(
                config=_cfg(blocking_mode=BlockingMode.HARD_BLOCK),
                open_trades=6,
                open_tickets_on_instrument=0,
                available_margin=Decimal("1000"),
                equity=Decimal("10000"),
            )

    def test_hard_block_raises_over_cap(self) -> None:
        with pytest.raises(MaxTradesExceeded):
            evaluate_trade(
                config=_cfg(blocking_mode=BlockingMode.HARD_BLOCK),
                open_trades=7,
                open_tickets_on_instrument=0,
                available_margin=Decimal("1000"),
                equity=Decimal("10000"),
            )

    def test_warning_only_proceeds_at_cap(self) -> None:
        d = evaluate_trade(
            config=_cfg(blocking_mode=BlockingMode.WARNING_ONLY),
            open_trades=6,
            open_tickets_on_instrument=0,
            available_margin=Decimal("1000"),
            equity=Decimal("10000"),
        )
        assert any("max" in w.lower() for w in d.warnings)
        # Clamped to N=M-1=5 → fraction 1/2
        assert d.size_fraction == Fraction(1, 2)
        assert d.capital_to_deploy == Decimal("500")


class TestPercentOfEquity:
    def test_simple_percent(self) -> None:
        d = evaluate_trade(
            config=_cfg(
                strategy=RiskStrategy.PERCENT_OF_EQUITY,
                percent_of_equity=Decimal("0.1"),
            ),
            open_trades=0,
            open_tickets_on_instrument=0,
            available_margin=Decimal("10000"),
            equity=Decimal("10000"),
        )
        assert d.capital_to_deploy == Decimal("1000")
        assert d.size_fraction is None
        assert d.strategy_used is RiskStrategy.PERCENT_OF_EQUITY
        assert d.warnings == ()

    def test_capped_by_usable_margin_emits_warning(self) -> None:
        d = evaluate_trade(
            config=_cfg(
                strategy=RiskStrategy.PERCENT_OF_EQUITY,
                percent_of_equity=Decimal("0.5"),
            ),
            open_trades=0,
            open_tickets_on_instrument=0,
            available_margin=Decimal("3000"),
            equity=Decimal("10000"),
        )
        assert d.capital_to_deploy == Decimal("3000")
        assert any("cap" in w.lower() for w in d.warnings)


class TestFixedDollar:
    def test_simple_fixed(self) -> None:
        d = evaluate_trade(
            config=_cfg(
                strategy=RiskStrategy.FIXED_DOLLAR,
                fixed_dollar=Decimal("250"),
            ),
            open_trades=0,
            open_tickets_on_instrument=0,
            available_margin=Decimal("5000"),
            equity=Decimal("10000"),
        )
        assert d.capital_to_deploy == Decimal("250")
        assert d.size_fraction is None

    def test_capped_by_usable_margin(self) -> None:
        d = evaluate_trade(
            config=_cfg(
                strategy=RiskStrategy.FIXED_DOLLAR,
                fixed_dollar=Decimal("500"),
            ),
            open_trades=0,
            open_tickets_on_instrument=0,
            available_margin=Decimal("200"),
            equity=Decimal("10000"),
        )
        assert d.capital_to_deploy == Decimal("200")
        assert any("cap" in w.lower() for w in d.warnings)


class TestRiskConfigValidation:
    def test_rejects_zero_max_trades(self) -> None:
        with pytest.raises(ValueError):
            _cfg(max_open_trades=0)

    def test_rejects_reserve_above_one(self) -> None:
        with pytest.raises(ValueError):
            _cfg(safety_reserve_pct=Decimal("1.5"))

    def test_rejects_negative_reserve(self) -> None:
        with pytest.raises(ValueError):
            _cfg(safety_reserve_pct=Decimal("-0.1"))

    def test_percent_of_equity_requires_value(self) -> None:
        with pytest.raises(ValueError):
            RiskConfig(
                max_open_trades=6,
                strategy=RiskStrategy.PERCENT_OF_EQUITY,
                blocking_mode=BlockingMode.HARD_BLOCK,
                scale_in=ScaleInPolicy.NEVER,
            )

    def test_fixed_dollar_requires_positive_value(self) -> None:
        with pytest.raises(ValueError):
            RiskConfig(
                max_open_trades=6,
                strategy=RiskStrategy.FIXED_DOLLAR,
                blocking_mode=BlockingMode.HARD_BLOCK,
                scale_in=ScaleInPolicy.NEVER,
                fixed_dollar=Decimal("0"),
            )


class TestInputValidation:
    def test_negative_open_trades(self) -> None:
        with pytest.raises(ValueError):
            evaluate_trade(
                config=_cfg(),
                open_trades=-1,
                open_tickets_on_instrument=0,
                available_margin=Decimal("1000"),
                equity=Decimal("10000"),
            )

    def test_negative_margin(self) -> None:
        with pytest.raises(ValueError):
            evaluate_trade(
                config=_cfg(),
                open_trades=0,
                open_tickets_on_instrument=0,
                available_margin=Decimal("-1"),
                equity=Decimal("10000"),
            )
