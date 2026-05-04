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
                                                             pl_by_weekday()
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
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
    time: str        # ISO-8601, verbatim from Oanda
    pl: Decimal
    units: int       # absolute value
    direction: str   # "LONG" or "SHORT"


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
    win_rate: Decimal    # fraction, e.g. Decimal("0.571")
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


def pl_by_hour(
    trades: list[ClosedTrade],
) -> list[tuple[int, int, Decimal]]:
    """Return ``(hour_utc, count, total_pl)`` for hours 0-23 that have trades.

    Hours with no trades are omitted to keep the table compact.
    """
    groups: dict[int, tuple[int, Decimal]] = {}
    for t in trades:
        try:
            dt = datetime.fromisoformat(t.time)
        except ValueError:
            continue
        h = dt.hour
        count, total = groups.get(h, (0, Decimal(0)))
        groups[h] = (count + 1, total + t.pl)
    return [(h, groups[h][0], groups[h][1]) for h in range(24) if h in groups]


def pl_by_weekday(
    trades: list[ClosedTrade],
) -> list[tuple[str, int, Decimal]]:
    """Return ``(weekday_name, count, total_pl)`` Mon-Sun for days that have trades.

    Days with no trades are omitted to keep the table compact.
    """
    groups: dict[int, tuple[int, Decimal]] = {}
    for t in trades:
        try:
            dt = datetime.fromisoformat(t.time)
        except ValueError:
            continue
        d = dt.weekday()
        count, total = groups.get(d, (0, Decimal(0)))
        groups[d] = (count + 1, total + t.pl)
    return [(_DAY_NAMES[d], groups[d][0], groups[d][1]) for d in range(7) if d in groups]
