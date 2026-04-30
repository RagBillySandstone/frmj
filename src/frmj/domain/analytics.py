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
    wins = sum(1 for t in trades if t.pl > 0)
    losses = sum(1 for t in trades if t.pl < 0)
    breakeven = total - wins - losses
    total_pl = sum((t.pl for t in trades), Decimal(0))
    avg_pl = total_pl / Decimal(total)
    best_pl = max(t.pl for t in trades)
    worst_pl = min(t.pl for t in trades)
    win_rate = Decimal(wins) / Decimal(total)
    return TradeSummary(
        total=total,
        wins=wins,
        losses=losses,
        breakeven=breakeven,
        win_rate=win_rate,
        avg_pl=avg_pl,
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
    groups: dict[str, list[Decimal]] = {}
    for t in trades:
        groups.setdefault(t.instrument, []).append(t.pl)
    rows: list[tuple[str, int, Decimal, Decimal]] = []
    for instr, pls in groups.items():
        count = len(pls)
        total = sum(pls, Decimal(0))
        avg = total / Decimal(count)
        rows.append((instr, count, total, avg))
    rows.sort(key=lambda r: r[2], reverse=True)
    return rows


def pl_by_hour(
    trades: list[ClosedTrade],
) -> list[tuple[int, int, Decimal]]:
    """Return ``(hour_utc, count, total_pl)`` for hours 0-23 that have trades.

    Hours with no trades are omitted to keep the table compact.
    """
    groups: dict[int, list[Decimal]] = {}
    for t in trades:
        try:
            dt = datetime.fromisoformat(t.time.replace("Z", "+00:00"))
        except ValueError:
            continue
        groups.setdefault(dt.hour, []).append(t.pl)
    return [
        (h, len(groups[h]), sum(groups[h], Decimal(0)))
        for h in range(24)
        if h in groups
    ]


def pl_by_weekday(
    trades: list[ClosedTrade],
) -> list[tuple[str, int, Decimal]]:
    """Return ``(weekday_name, count, total_pl)`` Mon-Sun for days that have trades.

    Days with no trades are omitted to keep the table compact.
    """
    _DAY_NAMES = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"]
    groups: dict[int, list[Decimal]] = {}
    for t in trades:
        try:
            dt = datetime.fromisoformat(t.time.replace("Z", "+00:00"))
        except ValueError:
            continue
        groups.setdefault(dt.weekday(), []).append(t.pl)
    return [
        (_DAY_NAMES[d], len(groups[d]), sum(groups[d], Decimal(0)))
        for d in range(7)
        if d in groups
    ]
