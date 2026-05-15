"""Tests for domain/analytics.py — pure functions over ClosedTrade lists."""

from __future__ import annotations

from datetime import timedelta, timezone
from decimal import Decimal


from frmj.domain.analytics import (
    ClosedTrade,
    DirectionStats,
    TradeSummary,
    _stats_for,
    compute_summary,
    pl_by_direction,
    pl_by_hour,
    pl_by_instrument,
    pl_by_instrument_direction,
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
        trades = [
            _trade("1", pl="25.00"),
            _trade("2", pl="-8.50"),
            _trade("3", pl="12.00"),
        ]
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
        trades = [
            _trade("1", instrument="EUR_USD", pl="20.00"),
            _trade("2", instrument="EUR_USD", pl="15.00"),
        ]
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
        assert rows[0][0] == "EUR_USD"  # 45.00 total
        assert rows[1][0] == "GBP_USD"  # 10.00 total

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

    def test_tz_shifts_bucket_to_local_wall_clock(self) -> None:
        # 14:30 UTC seen from a fixed UTC-6 zone should bucket as hour 8.
        # Using a hard-coded offset keeps the assertion deterministic
        # regardless of the system timezone the tests run under.
        fixed_utc_minus_6 = timezone(timedelta(hours=-6))
        rows = pl_by_hour(
            [_trade(time="2026-04-25T14:30:00.000000Z", pl="25.00")],
            tz=fixed_utc_minus_6,
        )
        assert len(rows) == 1
        hour, count, total = rows[0]
        assert hour == 8
        assert count == 1
        assert total == Decimal("25.00")

    def test_tz_can_roll_hour_across_midnight(self) -> None:
        # 02:00 UTC in UTC-5 is 21:00 the previous day → hour 21.
        fixed_utc_minus_5 = timezone(timedelta(hours=-5))
        rows = pl_by_hour(
            [_trade(time="2026-04-25T02:00:00.000000Z", pl="1.00")],
            tz=fixed_utc_minus_5,
        )
        assert rows[0][0] == 21


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

    def test_tz_rolls_day_forward_across_midnight(self) -> None:
        # 22:00 UTC Sunday (2026-04-26) = 08:00 AEST Monday (2026-04-27).
        # Without a tz the UTC date (Sunday) would be used; with UTC+10 the
        # trade must land on Monday.
        fixed_aest = timezone(timedelta(hours=10))
        rows = pl_by_weekday(
            [_trade(time="2026-04-26T22:00:00.000000Z", pl="10.00")],
            tz=fixed_aest,
        )
        assert len(rows) == 1
        day, count, total = rows[0]
        assert day == "Mon"
        assert count == 1
        assert total == Decimal("10.00")

    def test_tz_trade_before_midnight_stays_on_same_day(self) -> None:
        # 13:00 UTC Sunday (2026-04-26) = 23:00 AEST Sunday — still Sunday.
        fixed_aest = timezone(timedelta(hours=10))
        rows = pl_by_weekday(
            [_trade(time="2026-04-26T13:00:00.000000Z", pl="5.00")],
            tz=fixed_aest,
        )
        assert len(rows) == 1
        day, _, _ = rows[0]
        assert day == "Sun"


# ---------------------------------------------------------------------------
# pl_by_direction
# ---------------------------------------------------------------------------


class TestPlByDirection:
    def test_empty_list_returns_empty(self) -> None:
        assert pl_by_direction([]) == []

    def test_only_longs_returns_single_row(self) -> None:
        trades = [
            _trade("1", direction="LONG", pl="20.00"),
            _trade("2", direction="LONG", pl="-5.00"),
        ]
        rows = pl_by_direction(trades)
        assert len(rows) == 1
        row = rows[0]
        assert isinstance(row, DirectionStats)
        assert row.direction == "LONG"
        assert row.count == 2
        assert row.wins == 1
        assert row.losses == 1
        assert row.total_pl == Decimal("15.00")
        assert row.avg_pl == Decimal("7.50")
        assert row.win_rate == Decimal(1) / Decimal(2)

    def test_only_shorts_returns_single_row(self) -> None:
        trades = [_trade("1", direction="SHORT", pl="40.00")]
        rows = pl_by_direction(trades)
        assert len(rows) == 1
        assert rows[0].direction == "SHORT"
        assert rows[0].total_pl == Decimal("40.00")

    def test_long_listed_before_short(self) -> None:
        trades = [
            _trade("1", direction="SHORT", pl="10.00"),
            _trade("2", direction="LONG", pl="5.00"),
        ]
        rows = pl_by_direction(trades)
        assert [r.direction for r in rows] == ["LONG", "SHORT"]

    def test_breakeven_counted_in_total_not_wins_or_losses(self) -> None:
        trades = [
            _trade("1", direction="LONG", pl="10.00"),
            _trade("2", direction="LONG", pl="0.00"),
            _trade("3", direction="LONG", pl="-5.00"),
        ]
        rows = pl_by_direction(trades)
        assert rows[0].count == 3
        assert rows[0].wins == 1
        assert rows[0].losses == 1
        # win_rate is wins/total, not wins/(wins+losses)
        assert rows[0].win_rate == Decimal(1) / Decimal(3)

    def test_mixed_directions_split_correctly(self) -> None:
        trades = [
            _trade("1", direction="LONG", pl="30.00"),
            _trade("2", direction="LONG", pl="-10.00"),
            _trade("3", direction="SHORT", pl="50.00"),
            _trade("4", direction="SHORT", pl="-20.00"),
            _trade("5", direction="SHORT", pl="-5.00"),
        ]
        rows = pl_by_direction(trades)
        assert len(rows) == 2
        long_row, short_row = rows
        assert long_row.count == 2
        assert long_row.total_pl == Decimal("20.00")
        assert short_row.count == 3
        assert short_row.total_pl == Decimal("25.00")

    def test_unknown_direction_silently_dropped(self) -> None:
        # Defensive: malformed rows are tolerated (they shouldn't occur in
        # practice, but the parser elsewhere already swallows oddities).
        trades = [
            ClosedTrade(
                "1",
                "EUR_USD",
                "2026-04-25T09:00:00.000000Z",
                Decimal("10.00"),
                1000,
                "FLAT",
            ),
            _trade("2", direction="LONG", pl="5.00"),
        ]
        rows = pl_by_direction(trades)
        assert len(rows) == 1
        assert rows[0].direction == "LONG"
        assert rows[0].count == 1


# ---------------------------------------------------------------------------
# pl_by_instrument_direction
# ---------------------------------------------------------------------------


class TestPlByInstrumentDirection:
    def test_empty_list_returns_empty(self) -> None:
        assert pl_by_instrument_direction([]) == []

    def test_single_pair_long_only(self) -> None:
        trades = [
            _trade("1", instrument="EUR_USD", direction="LONG", pl="20.00"),
            _trade("2", instrument="EUR_USD", direction="LONG", pl="-5.00"),
        ]
        rows = pl_by_instrument_direction(trades)
        assert len(rows) == 1
        instrument, stats = rows[0]
        assert instrument == "EUR_USD"
        assert stats.direction == "LONG"
        assert stats.count == 2
        assert stats.total_pl == Decimal("15.00")

    def test_long_emitted_before_short_within_instrument(self) -> None:
        trades = [
            _trade("1", instrument="EUR_USD", direction="SHORT", pl="10.00"),
            _trade("2", instrument="EUR_USD", direction="LONG", pl="5.00"),
        ]
        rows = pl_by_instrument_direction(trades)
        directions = [stats.direction for _, stats in rows]
        assert directions == ["LONG", "SHORT"]

    def test_instruments_sorted_alphabetically(self) -> None:
        trades = [
            _trade("1", instrument="USD_JPY", direction="LONG", pl="5.00"),
            _trade("2", instrument="EUR_USD", direction="LONG", pl="10.00"),
            _trade("3", instrument="GBP_USD", direction="SHORT", pl="3.00"),
        ]
        rows = pl_by_instrument_direction(trades)
        instruments = [instr for instr, _ in rows]
        assert instruments == ["EUR_USD", "GBP_USD", "USD_JPY"]

    def test_empty_side_omitted(self) -> None:
        # EUR_USD has only LONG trades — no SHORT row should be emitted.
        trades = [_trade("1", instrument="EUR_USD", direction="LONG", pl="20.00")]
        rows = pl_by_instrument_direction(trades)
        assert len(rows) == 1
        assert rows[0][1].direction == "LONG"

    def test_both_sides_emitted_when_present(self) -> None:
        trades = [
            _trade("1", instrument="EUR_USD", direction="LONG", pl="20.00"),
            _trade("2", instrument="EUR_USD", direction="SHORT", pl="-5.00"),
        ]
        rows = pl_by_instrument_direction(trades)
        assert len(rows) == 2
        assert rows[0][0] == "EUR_USD"
        assert rows[0][1].direction == "LONG"
        assert rows[1][0] == "EUR_USD"
        assert rows[1][1].direction == "SHORT"

    def test_stats_per_row_are_correct(self) -> None:
        trades = [
            _trade("1", instrument="EUR_USD", direction="LONG", pl="30.00"),
            _trade("2", instrument="EUR_USD", direction="LONG", pl="-10.00"),
            _trade("3", instrument="EUR_USD", direction="LONG", pl="20.00"),
            _trade("4", instrument="EUR_USD", direction="SHORT", pl="-15.00"),
        ]
        rows = pl_by_instrument_direction(trades)
        long_row = next(s for _, s in rows if s.direction == "LONG")
        short_row = next(s for _, s in rows if s.direction == "SHORT")
        assert long_row.count == 3
        assert long_row.wins == 2
        assert long_row.losses == 1
        assert long_row.total_pl == Decimal("40.00")
        assert long_row.avg_pl == Decimal("40.00") / Decimal(3)
        assert short_row.count == 1
        assert short_row.total_pl == Decimal("-15.00")

    def test_unknown_direction_silently_skipped(self) -> None:
        """Trades with a direction other than LONG/SHORT are silently ignored."""
        trades = [
            ClosedTrade(
                "1",
                "EUR_USD",
                "2026-04-25T09:00:00.000000Z",
                Decimal("10.00"),
                1000,
                "FLAT",
            ),
            _trade("2", instrument="EUR_USD", direction="LONG", pl="5.00"),
        ]
        rows = pl_by_instrument_direction(trades)
        # Only the LONG trade should appear; FLAT is dropped.
        assert len(rows) == 1
        assert rows[0][1].direction == "LONG"
        assert rows[0][1].count == 1


# ---------------------------------------------------------------------------
# _stats_for — direct unit tests for the private helper
# ---------------------------------------------------------------------------


class TestStatsForHelper:
    def test_empty_group_returns_zeroed_stats(self) -> None:
        """``_stats_for`` with an empty list returns a zeroed DirectionStats.

        Both callers (``pl_by_direction`` and ``pl_by_instrument_direction``)
        guard against empty lists via ``if not pls: continue``, so this branch
        is dead in normal usage — but the guard is there to prevent a
        ZeroDivisionError, and this test exercises it directly.
        """
        result = _stats_for("LONG", [])
        assert isinstance(result, DirectionStats)
        assert result.direction == "LONG"
        assert result.count == 0
        assert result.wins == 0
        assert result.losses == 0
        assert result.win_rate == Decimal(0)
        assert result.total_pl == Decimal(0)
        assert result.avg_pl == Decimal(0)


# ---------------------------------------------------------------------------
# compute_summary — best_pl update branch
# ---------------------------------------------------------------------------


class TestComputeSummaryBestPlUpdate:
    def test_best_pl_updates_when_later_trade_exceeds_first(self) -> None:
        """``best_pl`` must be updated when a trade after index 0 has a higher P/L.

        The existing tests all start with the highest P/L trade at index 0, so
        the ``if t.pl > best_pl: best_pl = t.pl`` branch is never reached by
        them.  This test places the best trade last to exercise that update.
        """
        trades = [
            _trade("1", pl="10.00"),
            _trade("2", pl="50.00"),  # highest — appears after index 0
        ]
        result = compute_summary(trades)
        assert result is not None
        assert result.best_pl == Decimal("50.00")
