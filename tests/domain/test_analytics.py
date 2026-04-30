"""Tests for domain/analytics.py — pure functions over ClosedTrade lists."""

from __future__ import annotations

from decimal import Decimal

import pytest

from frmj.domain.analytics import (
    ClosedTrade,
    TradeSummary,
    compute_summary,
    pl_by_hour,
    pl_by_instrument,
    pl_by_weekday,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _trade(
    oanda_id: str = "1",
    instrument: str = "EUR_USD",
    time: str = "2026-04-25T09:30:00.000000Z",
    pl: str = "10.00",
    units: int = 10_000,
    direction: str = "LONG",
) -> ClosedTrade:
    return ClosedTrade(
        oanda_id=oanda_id,
        instrument=instrument,
        time=time,
        pl=Decimal(pl),
        units=units,
        direction=direction,
    )


# ---------------------------------------------------------------------------
# compute_summary
# ---------------------------------------------------------------------------


class TestComputeSummary:
    def test_empty_list_returns_none(self) -> None:
        assert compute_summary([]) is None

    def test_single_winning_trade(self) -> None:
        result = compute_summary([_trade(pl="50.00")])
        assert result is not None
        assert result.total == 1
        assert result.wins == 1
        assert result.losses == 0
        assert result.breakeven == 0
        assert result.total_pl == Decimal("50.00")
        assert result.best_pl == Decimal("50.00")
        assert result.worst_pl == Decimal("50.00")

    def test_single_losing_trade(self) -> None:
        result = compute_summary([_trade(pl="-30.00")])
        assert result is not None
        assert result.total == 1
        assert result.wins == 0
        assert result.losses == 1
        assert result.total_pl == Decimal("-30.00")

    def test_win_rate_fraction(self) -> None:
        trades = [
            _trade("1", pl="20.00"),
            _trade("2", pl="15.00"),
            _trade("3", pl="-10.00"),
        ]
        result = compute_summary(trades)
        assert result is not None
        assert result.wins == 2
        assert result.losses == 1
        # 2/3 ≈ 0.666...
        assert result.win_rate == Decimal(2) / Decimal(3)

    def test_avg_pl_over_mixed_trades(self) -> None:
        trades = [_trade("1", pl="30.00"), _trade("2", pl="-10.00")]
        result = compute_summary(trades)
        assert result is not None
        assert result.avg_pl == Decimal("10.00")

    def test_total_pl_is_sum(self) -> None:
        trades = [_trade("1", pl="25.00"), _trade("2", pl="-8.50"), _trade("3", pl="12.00")]
        result = compute_summary(trades)
        assert result is not None
        assert result.total_pl == Decimal("28.50")

    def test_best_and_worst_pl(self) -> None:
        trades = [
            _trade("1", pl="100.00"),
            _trade("2", pl="-50.00"),
            _trade("3", pl="30.00"),
        ]
        result = compute_summary(trades)
        assert result is not None
        assert result.best_pl == Decimal("100.00")
        assert result.worst_pl == Decimal("-50.00")

    def test_breakeven_trade_counted(self) -> None:
        # A trade with pl exactly zero is neither win nor loss.
        trades = [
            _trade("1", pl="20.00"),
            _trade("2", pl="0.00"),
            _trade("3", pl="-5.00"),
        ]
        result = compute_summary(trades)
        assert result is not None
        assert result.wins == 1
        assert result.losses == 1
        assert result.breakeven == 1
        assert result.total == 3

    def test_all_losing_trades(self) -> None:
        trades = [_trade("1", pl="-10.00"), _trade("2", pl="-20.00")]
        result = compute_summary(trades)
        assert result is not None
        assert result.win_rate == Decimal(0)
        assert result.wins == 0
        assert result.losses == 2

    def test_returns_trade_summary_type(self) -> None:
        result = compute_summary([_trade()])
        assert isinstance(result, TradeSummary)


# ---------------------------------------------------------------------------
# pl_by_instrument
# ---------------------------------------------------------------------------


class TestPlByInstrument:
    def test_empty_list_returns_empty(self) -> None:
        assert pl_by_instrument([]) == []

    def test_single_instrument(self) -> None:
        trades = [_trade("1", instrument="EUR_USD", pl="20.00"),
                  _trade("2", instrument="EUR_USD", pl="15.00")]
        rows = pl_by_instrument(trades)
        assert len(rows) == 1
        instr, count, total, avg = rows[0]
        assert instr == "EUR_USD"
        assert count == 2
        assert total == Decimal("35.00")
        assert avg == Decimal("17.50")

    def test_multiple_instruments_sorted_by_total_desc(self) -> None:
        trades = [
            _trade("1", instrument="GBP_USD", pl="10.00"),
            _trade("2", instrument="EUR_USD", pl="50.00"),
            _trade("3", instrument="EUR_USD", pl="-5.00"),
        ]
        rows = pl_by_instrument(trades)
        assert rows[0][0] == "EUR_USD"   # 45.00 total
        assert rows[1][0] == "GBP_USD"   # 10.00 total

    def test_losing_instrument_sorted_last(self) -> None:
        trades = [
            _trade("1", instrument="USD_JPY", pl="-20.00"),
            _trade("2", instrument="EUR_USD", pl="5.00"),
        ]
        rows = pl_by_instrument(trades)
        assert rows[-1][0] == "USD_JPY"

    def test_avg_pl_computed_correctly(self) -> None:
        trades = [
            _trade("1", instrument="EUR_USD", pl="30.00"),
            _trade("2", instrument="EUR_USD", pl="10.00"),
            _trade("3", instrument="EUR_USD", pl="-4.00"),
        ]
        rows = pl_by_instrument(trades)
        _, _, total, avg = rows[0]
        assert total == Decimal("36.00")
        assert avg == Decimal("12.00")


# ---------------------------------------------------------------------------
# pl_by_hour
# ---------------------------------------------------------------------------


class TestPlByHour:
    def test_empty_list_returns_empty(self) -> None:
        assert pl_by_hour([]) == []

    def test_single_trade_at_known_hour(self) -> None:
        rows = pl_by_hour([_trade(time="2026-04-25T14:30:00.000000Z", pl="25.00")])
        assert len(rows) == 1
        hour, count, total = rows[0]
        assert hour == 14
        assert count == 1
        assert total == Decimal("25.00")

    def test_multiple_trades_same_hour_aggregated(self) -> None:
        trades = [
            _trade("1", time="2026-04-25T09:15:00.000000Z", pl="10.00"),
            _trade("2", time="2026-04-25T09:45:00.000000Z", pl="20.00"),
        ]
        rows = pl_by_hour(trades)
        assert len(rows) == 1
        hour, count, total = rows[0]
        assert hour == 9
        assert count == 2
        assert total == Decimal("30.00")

    def test_different_hours_kept_separate(self) -> None:
        trades = [
            _trade("1", time="2026-04-25T08:00:00.000000Z", pl="10.00"),
            _trade("2", time="2026-04-25T10:00:00.000000Z", pl="20.00"),
        ]
        rows = pl_by_hour(trades)
        assert len(rows) == 2
        hours = [r[0] for r in rows]
        assert 8 in hours
        assert 10 in hours

    def test_hours_ordered_0_to_23(self) -> None:
        trades = [
            _trade("1", time="2026-04-25T16:00:00.000000Z", pl="5.00"),
            _trade("2", time="2026-04-25T08:00:00.000000Z", pl="5.00"),
        ]
        rows = pl_by_hour(trades)
        assert rows[0][0] == 8
        assert rows[1][0] == 16

    def test_empty_hours_omitted(self) -> None:
        rows = pl_by_hour([_trade(time="2026-04-25T12:00:00.000000Z", pl="1.00")])
        hours = [r[0] for r in rows]
        assert hours == [12]

    def test_invalid_time_skipped_gracefully(self) -> None:
        trades = [
            ClosedTrade("1", "EUR_USD", "not-a-date", Decimal("10.00"), 1000, "LONG"),
            _trade("2", time="2026-04-25T09:00:00.000000Z", pl="5.00"),
        ]
        rows = pl_by_hour(trades)
        assert len(rows) == 1


# ---------------------------------------------------------------------------
# pl_by_weekday
# ---------------------------------------------------------------------------


class TestPlByWeekday:
    def test_empty_list_returns_empty(self) -> None:
        assert pl_by_weekday([]) == []

    def test_monday_trade(self) -> None:
        # 2026-04-27 is a Monday
        rows = pl_by_weekday([_trade(time="2026-04-27T10:00:00.000000Z", pl="15.00")])
        assert len(rows) == 1
        day, count, total = rows[0]
        assert day == "Mon"
        assert count == 1
        assert total == Decimal("15.00")

    def test_friday_trade(self) -> None:
        # 2026-04-24 is a Friday
        rows = pl_by_weekday([_trade(time="2026-04-24T14:00:00.000000Z", pl="8.00")])
        day, _, _ = rows[0]
        assert day == "Fri"

    def test_days_ordered_mon_to_sun(self) -> None:
        trades = [
            _trade("1", time="2026-04-24T10:00:00.000000Z", pl="5.00"),  # Fri
            _trade("2", time="2026-04-27T10:00:00.000000Z", pl="5.00"),  # Mon
        ]
        rows = pl_by_weekday(trades)
        assert rows[0][0] == "Mon"
        assert rows[1][0] == "Fri"

    def test_empty_days_omitted(self) -> None:
        # Only Monday trade
        rows = pl_by_weekday([_trade(time="2026-04-27T10:00:00.000000Z", pl="5.00")])
        days = [r[0] for r in rows]
        assert days == ["Mon"]

    def test_multiple_trades_same_day_aggregated(self) -> None:
        trades = [
            _trade("1", time="2026-04-27T09:00:00.000000Z", pl="20.00"),
            _trade("2", time="2026-04-27T14:00:00.000000Z", pl="-5.00"),
        ]
        rows = pl_by_weekday(trades)
        assert len(rows) == 1
        day, count, total = rows[0]
        assert day == "Mon"
        assert count == 2
        assert total == Decimal("15.00")

    def test_invalid_time_skipped_gracefully(self) -> None:
        trades = [
            ClosedTrade("1", "EUR_USD", "bad-date", Decimal("10.00"), 1000, "LONG"),
            _trade("2", time="2026-04-27T10:00:00.000000Z", pl="5.00"),
        ]
        rows = pl_by_weekday(trades)
        assert len(rows) == 1
