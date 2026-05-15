"""
Trade analytics: pure functions over a list of ClosedTrade records.

The data source is closing ORDER_FILL transactions (pl != "0") from the
local DB.  All arithmetic stays in Decimal to match the rest of the domain
layer; the CLI is responsible for display rounding and formatting.

Pipeline
--------
    DB rows  →  parse_closed_trades()  →  list[ClosedTrade]
                                              │
                         ┌────────────────────┼──────────────────────┐
                         ▼                    ▼                      ▼
               compute_summary()     pl_by_instrument()     pl_by_hour()
                                     pl_by_direction()      pl_by_weekday()
                                     pl_by_instrument_direction()
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone, tzinfo
from decimal import Decimal

_DAY_NAMES: tuple[str, ...] = ("Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun")


# ---------------------------------------------------------------------------
# Input record
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class ClosedTrade:
    """One closed trade extracted from an ORDER_FILL transaction.

    ``direction`` is the direction of the *original* open trade:
      - LONG  → closing fill has negative units  (sold to close)
      - SHORT → closing fill has positive units  (bought to close)

    ``pl`` is signed: positive for profit, negative for loss.
    """

    oanda_id: str
    instrument: str
    time: str  # ISO-8601, verbatim from Oanda
    pl: Decimal
    units: int  # absolute value
    direction: str  # "LONG" or "SHORT"


# ---------------------------------------------------------------------------
# Summary
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class TradeSummary:
    """Aggregate statistics over a collection of closed trades."""

    total: int
    wins: int
    losses: int
    breakeven: int
    win_rate: Decimal  # fraction, e.g. Decimal("0.571")
    avg_pl: Decimal
    total_pl: Decimal
    best_pl: Decimal
    worst_pl: Decimal


def compute_summary(trades: list[ClosedTrade]) -> TradeSummary | None:
    """Compute aggregate stats; returns None when *trades* is empty."""
    if not trades:
        return None
    total = len(trades)
    wins = 0
    losses = 0
    total_pl = Decimal(0)
    best_pl = trades[0].pl
    worst_pl = trades[0].pl
    for t in trades:
        if t.pl > 0:
            wins += 1
        elif t.pl < 0:
            losses += 1
        total_pl += t.pl
        if t.pl > best_pl:
            best_pl = t.pl
        if t.pl < worst_pl:
            worst_pl = t.pl
    return TradeSummary(
        total=total,
        wins=wins,
        losses=losses,
        breakeven=total - wins - losses,
        win_rate=Decimal(wins) / Decimal(total),
        avg_pl=total_pl / Decimal(total),
        total_pl=total_pl,
        best_pl=best_pl,
        worst_pl=worst_pl,
    )


# ---------------------------------------------------------------------------
# Breakdowns
# ---------------------------------------------------------------------------


def pl_by_instrument(
    trades: list[ClosedTrade],
) -> list[tuple[str, int, Decimal, Decimal]]:
    """Return ``(instrument, count, total_pl, avg_pl)`` sorted by total_pl desc."""
    groups: dict[str, tuple[int, Decimal]] = {}
    for t in trades:
        count, total = groups.get(t.instrument, (0, Decimal(0)))
        groups[t.instrument] = (count + 1, total + t.pl)
    rows: list[tuple[str, int, Decimal, Decimal]] = [
        (instr, count, total, total / Decimal(count))
        for instr, (count, total) in groups.items()
    ]
    rows.sort(key=lambda r: r[2], reverse=True)
    return rows


@dataclass(frozen=True, slots=True)
class DirectionStats:
    """Per-direction aggregate statistics.

    Holds the same shape of numbers as :class:`TradeSummary` but trimmed to
    the columns that are useful when comparing LONG vs SHORT performance
    side-by-side.  The CLI renders one of these per row.
    """

    direction: str  # "LONG" or "SHORT"
    count: int
    wins: int
    losses: int
    win_rate: Decimal  # fraction in [0, 1]; Decimal(0) when count == 0
    total_pl: Decimal
    avg_pl: Decimal  # Decimal(0) when count == 0


def _stats_for(direction: str, group: list[Decimal]) -> DirectionStats:
    """Build a :class:`DirectionStats` from a list of P/L values.

    Helper shared by :func:`pl_by_direction` and
    :func:`pl_by_instrument_direction`.  An empty *group* is allowed and
    yields zeroed statistics so callers can pre-seed both sides without
    having to special-case the missing one.
    """
    # Tally wins/losses and aggregate P/L in a single pass; breakeven trades
    # (pl == 0) are counted in `count` but contribute to neither wins nor losses.
    count = len(group)
    wins = 0
    losses = 0
    total_pl = Decimal(0)
    for pl in group:
        if pl > 0:
            wins += 1
        elif pl < 0:
            losses += 1
        total_pl += pl

    # Guard against ZeroDivisionError when `group` is empty — return a row of
    # zeros rather than raising, since callers may always-show both sides.
    if count == 0:
        return DirectionStats(
            direction=direction,
            count=0,
            wins=0,
            losses=0,
            win_rate=Decimal(0),
            total_pl=Decimal(0),
            avg_pl=Decimal(0),
        )

    return DirectionStats(
        direction=direction,
        count=count,
        wins=wins,
        losses=losses,
        win_rate=Decimal(wins) / Decimal(count),
        total_pl=total_pl,
        avg_pl=total_pl / Decimal(count),
    )


def pl_by_direction(trades: list[ClosedTrade]) -> list[DirectionStats]:
    """Return aggregate stats split by trade direction.

    Output is a list of :class:`DirectionStats`, one per direction that has
    at least one trade, ordered LONG before SHORT.  Returns an empty list
    when *trades* is empty so the CLI can omit the section entirely.
    """
    # Bucket P/Ls by direction first; defer arithmetic to `_stats_for` so
    # the win/loss/avg logic stays in one place.
    long_pls: list[Decimal] = []
    short_pls: list[Decimal] = []
    for t in trades:
        if t.direction == "LONG":
            long_pls.append(t.pl)
        elif t.direction == "SHORT":
            short_pls.append(t.pl)
        # Any other value (shouldn't occur given parse logic) is silently dropped.

    rows: list[DirectionStats] = []
    if long_pls:
        rows.append(_stats_for("LONG", long_pls))
    if short_pls:
        rows.append(_stats_for("SHORT", short_pls))
    return rows


def pl_by_instrument_direction(
    trades: list[ClosedTrade],
) -> list[tuple[str, DirectionStats]]:
    """Return per-(instrument, direction) stats.

    The result is sorted by instrument name, with LONG listed before SHORT
    inside each instrument.  Sides with zero trades are omitted, matching
    the design choice for the CLI's "By instrument & direction" table.
    """
    # Two-level bucketing: instrument → direction → list[Decimal].
    groups: dict[str, dict[str, list[Decimal]]] = {}
    for t in trades:
        if t.direction not in ("LONG", "SHORT"):
            continue  # defensive: ignore malformed rows
        per_instr = groups.setdefault(t.instrument, {"LONG": [], "SHORT": []})
        per_instr[t.direction].append(t.pl)

    rows: list[tuple[str, DirectionStats]] = []
    # Sort instruments alphabetically for stable, predictable output; LONG
    # is emitted before SHORT inside each pair so visual scanning groups them.
    for instrument in sorted(groups):
        per_instr = groups[instrument]
        for direction in ("LONG", "SHORT"):
            pls = per_instr[direction]
            if not pls:
                continue
            rows.append((instrument, _stats_for(direction, pls)))
    return rows


def pl_by_hour(
    trades: list[ClosedTrade],
    tz: tzinfo | None = None,
) -> list[tuple[int, int, Decimal]]:
    """Return ``(hour, count, total_pl)`` for hours 0-23 that have trades.

    The Oanda timestamps stored on ``ClosedTrade.time`` are UTC.  When *tz*
    is provided, each timestamp is converted to that timezone before its
    hour is extracted, so the buckets reflect wall-clock hours in *tz*.
    When *tz* is None, the parsed datetime's hour is used verbatim (UTC
    for the typical Oanda payload, preserving the original behaviour).

    Hours with no trades are omitted to keep the table compact.
    """
    groups: dict[int, tuple[int, Decimal]] = {}
    for t in trades:
        try:
            # Oanda emits ISO-8601 with a trailing "Z"; trim to the seconds
            # field for compatibility with Python < 3.11 fromisoformat().
            dt = datetime.fromisoformat(t.time[:19])
        except ValueError:
            continue
        if tz is not None:
            # The DB stores naive UTC timestamps; tag and shift to *tz*
            # so the resulting hour reflects wall-clock time in *tz*.
            dt = dt.replace(tzinfo=timezone.utc).astimezone(tz)
        h = dt.hour
        count, total = groups.get(h, (0, Decimal(0)))
        groups[h] = (count + 1, total + t.pl)
    return [(h, groups[h][0], groups[h][1]) for h in range(24) if h in groups]


def pl_by_weekday(
    trades: list[ClosedTrade],
    tz: tzinfo | None = None,
) -> list[tuple[str, int, Decimal]]:
    """Return ``(weekday_name, count, total_pl)`` Mon-Sun for days that have trades.

    The Oanda timestamps stored on ``ClosedTrade.time`` are UTC.  When *tz*
    is provided, each timestamp is converted to that timezone before its
    calendar day is extracted, so the buckets reflect the local date in *tz*.
    When *tz* is None, the raw UTC date is used (original behaviour).

    Days with no trades are omitted to keep the table compact.
    """
    groups: dict[int, tuple[int, Decimal]] = {}
    for t in trades:
        try:
            # Trim to seconds for Python < 3.11 compat (same approach as pl_by_hour).
            dt = datetime.fromisoformat(t.time[:19])
        except ValueError:
            continue
        if tz is not None:
            # Tag as naive UTC then shift; the resulting date reflects wall-clock
            # calendar day in *tz*, so day-boundary crossings are handled correctly.
            dt = dt.replace(tzinfo=timezone.utc).astimezone(tz)
        d = dt.weekday()
        count, total = groups.get(d, (0, Decimal(0)))
        groups[d] = (count + 1, total + t.pl)
    return [
        (_DAY_NAMES[d], groups[d][0], groups[d][1]) for d in range(7) if d in groups
    ]
