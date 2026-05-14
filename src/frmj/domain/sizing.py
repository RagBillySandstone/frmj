"""
Position sizing: convert a *capital amount* (in home/account currency) into a
concrete *number of units* of an instrument, respecting Oanda margin rules,
the broker's minimum trade size, and the lot/unit increment.

This is the second half of the trade-calculation engine. The first half
(``risk.py``) decides *how much capital* we are willing to commit to this
trade. This module decides *how many units* of the instrument that capital
buys at the current price under the broker's leverage rules.

Design rules (deliberately re-stated for future readers):

* **Pure domain.** No I/O, no clocks, no globals, no logging. Every
  dependency is passed in as an argument or attached to ``InstrumentSpec`` /
  ``PriceQuote``.
* **Decimal money, integer units.** Currency math uses ``decimal.Decimal``
  to avoid float drift. Unit counts are integers because Oanda accepts only
  whole units (and many instruments require larger increments still).
* **Round DOWN, never up.** Stephen explicitly chose conservative sizing:
  if the capital buys 17,432.7 units and the increment is 1, we ship 17,432.
  This guarantees the *actual* margin used is ≤ the *requested* capital.
* **Black-box pip handling.** ``InstrumentSpec.pip_location`` is taken from
  Oanda's instrument metadata verbatim. We do *not* special-case JPY pairs
  here; the integration layer is responsible for populating the spec
  correctly per instrument.

References to Oanda's margin formula:

    margin_required_home_ccy = |units| * margin_rate * base_ccy_to_home_ccy

So ``capital_to_deploy / (margin_rate * base_to_home)`` is the maximum
number of units that fits in our capital budget. We then floor to the
broker's increment.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from enum import Enum


# ---------------------------------------------------------------------------
# Inputs: instrument metadata and a live price/conversion snapshot
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class InstrumentSpec:
    """
    Static-ish metadata for a tradable instrument.

    These values come from Oanda's ``/v3/accounts/{id}/instruments`` endpoint
    and rarely change intra-day. We treat the spec as immutable input; the
    integration layer is responsible for refreshing it when Oanda changes
    margin requirements or rolls out new contracts.

    Fields
    ------
    name:
        Oanda instrument name, e.g. ``"EUR_USD"`` or ``"USD_JPY"``. Used only
        for display and for error messages — domain logic does not parse it.
    pip_location:
        Exponent such that ``one_pip_price = 10 ** pip_location``. For most
        FX pairs this is ``-4`` (1 pip = 0.0001). For JPY-quoted pairs this
        is ``-2`` (1 pip = 0.01). Treating it as a plain integer means we
        never branch on currency code in this module.
    margin_rate:
        Fraction of notional required as margin, expressed in *base*
        currency. ``Decimal("0.02")`` means 50:1 leverage.
    min_units:
        Smallest legal trade size the broker accepts. Below this the order
        is rejected outright; we surface this as ``BelowMinimumUnits``.
    units_increment:
        Granularity. Most FX pairs accept any integer (increment = 1), but
        some instruments (CFDs, metals) require 10 / 100 / 1000 unit lots.
        We always round *down* to a multiple of this.
    """

    name: str
    pip_location: int
    margin_rate: Decimal
    min_units: int
    units_increment: int

    def __post_init__(self) -> None:
        # Cheap structural validation — catches typos in tests and bad data
        # from Oanda before it propagates into a trade decision.
        if self.margin_rate <= 0 or self.margin_rate > 1:
            raise ValueError(f"margin_rate must be in (0, 1]; got {self.margin_rate!r}")
        if self.min_units < 1:
            raise ValueError(f"min_units must be >= 1; got {self.min_units!r}")
        if self.units_increment < 1:
            raise ValueError(
                f"units_increment must be >= 1; got {self.units_increment!r}"
            )


@dataclass(frozen=True, slots=True)
class PriceQuote:
    """
    Live pricing snapshot used to convert between currencies.

    For an instrument quoted ``BASE/QUOTE`` we hold:

    * ``bid`` / ``ask`` — current top-of-book prices, in *quote* currency.
    * ``quote_to_home`` — multiplier converting *quote-ccy values* into
      account/home-ccy values. For ``EUR_USD`` on a USD account this is 1.
      For ``USD_JPY`` on a USD account this is ``1 / mid_price``.
    * ``base_to_home`` — multiplier converting *base-ccy values* into
      account-ccy values. For ``EUR_USD`` on a USD account this is the
      current EUR/USD price (≈ ``mid * quote_to_home``).

    We carry both because Oanda's margin formula uses ``base_to_home`` while
    pip P/L uses ``quote_to_home`` (the instrument's price ticks live in
    quote currency).
    """

    bid: Decimal
    ask: Decimal
    quote_to_home: Decimal
    base_to_home: Decimal

    def __post_init__(self) -> None:
        if self.bid <= 0 or self.ask <= 0:
            raise ValueError("bid/ask must be positive")
        if self.ask < self.bid:
            # Crossed book — should never happen in normal markets and
            # almost certainly indicates a feed / parsing bug upstream.
            raise ValueError(f"ask {self.ask} < bid {self.bid}")
        if self.quote_to_home <= 0 or self.base_to_home <= 0:
            raise ValueError("conversion rates must be positive")

    @property
    def mid(self) -> Decimal:
        return (self.bid + self.ask) / 2

    def entry_price(self, direction: Direction) -> Decimal:
        """
        Best-effort entry fill for plan display.

        Stephen explicitly chose 'use the relevant side' over 'use mid'
        because mid is misleading on wide spreads. Buy at ask, sell at bid
        — the worst-case fill assuming no slippage past the visible book.
        """
        return self.ask if direction is Direction.LONG else self.bid


class Direction(Enum):
    """Trade direction. We model long/short separately rather than using a
    signed unit count — it makes downstream pricing math read more naturally
    and keeps the units field non-negative."""

    LONG = "long"
    SHORT = "short"


# ---------------------------------------------------------------------------
# Output and exception types
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class UnitsCalc:
    """
    Result of sizing a trade.

    ``units`` is unsigned; combine with ``Direction`` at the call site to
    decide buy vs sell. ``margin_used`` is the *actual* margin the broker
    will reserve given our rounded-down unit count, so it is always ≤ the
    capital we intended to deploy. ``capital_unused`` is that slack — it is
    informational and surfaces when the rounding step throws away a
    meaningful amount (e.g. fractional-lot brokers).
    """

    units: int
    margin_used: Decimal
    capital_unused: Decimal


class BelowMinimumUnits(Exception):
    """Raised when the requested capital cannot afford even one minimum
    trade ticket on this instrument. Carries the computed unit count and
    minimum so the CLI can surface an actionable message."""

    def __init__(self, computed_units: int, min_units: int, instrument: str) -> None:
        self.computed_units = computed_units
        self.min_units = min_units
        self.instrument = instrument
        super().__init__(
            f"computed {computed_units} units for {instrument}, "
            f"below broker minimum of {min_units}"
        )


# ---------------------------------------------------------------------------
# Core sizing function
# ---------------------------------------------------------------------------


def margin_per_unit(spec: InstrumentSpec, base_to_home: Decimal) -> Decimal:
    """
    Margin (in home ccy) required to hold *one unit* of the instrument.

    Pulled out as a named function because it shows up in two places (sizing
    and unused-capital reporting) and because writing it once means the
    margin formula has exactly one place to break if Oanda ever changes it.
    """
    return spec.margin_rate * base_to_home


def compute_units(
    *,
    capital_to_deploy: Decimal,
    spec: InstrumentSpec,
    quote: PriceQuote,
    direction: Direction,  # noqa: ARG001  -- kept for future asymmetric margin rules
) -> UnitsCalc:
    """
    Convert a capital budget into a concrete unit count.

    ``direction`` is currently unused because Oanda's standard margin model
    is symmetric (long and short carry identical margin). We accept it
    anyway so that:

    1. The call site is self-documenting ("size this LONG trade").
    2. If we ever support brokers with asymmetric short-margin (some CFD
       venues do this), we can add the logic without changing callers.

    Steps:

    1. Validate inputs. We want fail-fast behaviour rather than silent zero
       sizing — a negative or zero capital almost certainly indicates a bug
       in the risk layer, not a legitimate "skip this trade" signal.
    2. Compute per-unit margin via the Oanda formula.
    3. Divide and floor to the broker's unit increment.
    4. Reject if below the broker minimum (raise, don't silently clamp —
       the user needs to know they tried to trade too small).
    5. Compute the *actual* margin used and the slack capital.
    """
    # --- step 1: validation ------------------------------------------------
    if capital_to_deploy <= 0:
        raise ValueError(
            f"capital_to_deploy must be positive; got {capital_to_deploy!r}"
        )

    # --- step 2: per-unit margin ------------------------------------------
    per_unit = margin_per_unit(spec, quote.base_to_home)
    # per_unit is guaranteed > 0 by spec/quote validation, so no divide-by-
    # zero check needed here. We keep the assertion to document the
    # invariant for future readers.
    assert per_unit > 0, "InstrumentSpec/PriceQuote validation should prevent this"

    # --- step 3: floor to unit increment ----------------------------------
    # ``raw`` is the fractional unit count we *could* afford if Oanda
    # accepted fractional units. We floor to the increment (always rounding
    # *down*, never to-nearest) per Stephen's stated preference: better to
    # under-deploy by a few units than to exceed the capital budget.
    raw_units = capital_to_deploy / per_unit
    increment = spec.units_increment
    # ``//`` on Decimals gives integer division; multiplying by increment
    # snaps down to the nearest legal lot size.
    units = int(raw_units // increment) * increment

    # --- step 4: minimum-size gate ----------------------------------------
    if units < spec.min_units:
        raise BelowMinimumUnits(
            computed_units=units,
            min_units=spec.min_units,
            instrument=spec.name,
        )

    # --- step 5: actual margin and slack ----------------------------------
    margin_used = Decimal(units) * per_unit
    capital_unused = capital_to_deploy - margin_used
    return UnitsCalc(
        units=units,
        margin_used=margin_used,
        capital_unused=capital_unused,
    )
