"""
Exit-price pricing: convert a take-profit / stop-loss specification (in pips
*or* percent-return-on-margin) into concrete exit prices, projected P/L in
home currency, and return-on-margin figures at each exit.

This is the third pure-domain module. The pipeline is now:

    risk.evaluate_trade        -> capital_to_deploy   (home ccy)
    sizing.compute_units       -> units, margin_used  (units, home ccy)
    pricing.compute_exit_levels-> tp/sl prices, projected P/L, RoM warnings

Stephen specified that we should *warn* on unrealistic TP/SL rather than
reject them. Two thresholds (both tunable in one place):

    UNREALISTIC_PIP_THRESHOLD     -- 500 pips on either side
    UNREALISTIC_RETURN_THRESHOLD  -- 100% return on margin

Crossing either threshold attaches a warning to ``ExitLevels.warnings``;
the CLI surfaces them but the user is free to proceed.

Convention for TP/SL magnitudes: the user supplies *positive* values for
both TP and SL. The function knows TP is favorable and SL is adverse for
the given direction; signs in the output (P/L, return) are derived.

    take_profit = TPSLSpec(PIPS, 50)              -> 50 pips in our favor
    stop_loss   = TPSLSpec(PERCENT_RETURN, 0.05)  -> 5% loss of margin

This keeps the user-facing surface symmetric and avoids the "did I have to
negate the SL?" foot-gun.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from enum import Enum

from frmj.domain.sizing import Direction, InstrumentSpec, PriceQuote


# ---------------------------------------------------------------------------
# Tunable thresholds for the "unrealistic" warning
# ---------------------------------------------------------------------------

# Pip distance on either TP or SL beyond which we warn the user. 500 pips is
# huge for intraday FX (a typical day's range on EUR_USD is ~50-80 pips);
# Stephen's strategies rarely exceed 200, so 500 is a generous tripwire that
# mostly catches typos like "5000 pips" instead of "500".
UNREALISTIC_PIP_THRESHOLD: Decimal = Decimal("500")

# Return-on-margin magnitude beyond which we warn. 100% means the trade
# either doubles the margin used (TP) or loses all of it (SL). For a 50:1
# leveraged FX position this is a ~2% price move, which is plausible but
# usually indicates either an unintentionally large risk or a misconfigured
# percent input.
UNREALISTIC_RETURN_THRESHOLD: Decimal = Decimal("1.0")


# ---------------------------------------------------------------------------
# Inputs: TP/SL specification
# ---------------------------------------------------------------------------


class TPSLKind(Enum):
    """How the user expressed their target — distance in pips or fraction
    of margin returned. Both forms are first-class; we convert internally."""

    PIPS = "pips"
    PERCENT_RETURN = "percent_return"


@dataclass(frozen=True, slots=True)
class TPSLSpec:
    """A take-profit *or* stop-loss target.

    ``value`` is always a positive magnitude — the function infers sign
    from whether it is the TP or SL slot and from ``Direction``. We
    validate at construction so a typo (negative pips, zero percent)
    fails immediately rather than silently picking the wrong side of
    entry.
    """

    kind: TPSLKind
    value: Decimal

    def __post_init__(self) -> None:
        if self.value <= 0:
            raise ValueError(
                f"TPSLSpec.value must be positive (a magnitude); "
                f"got {self.value!r}"
            )


# ---------------------------------------------------------------------------
# Output type
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class ExitLevels:
    """
    Concrete exit prices and projected outcomes.

    Any of the price / P/L / return fields can be ``None`` if the user
    omitted that side (e.g. SL only, no TP). ``warnings`` is a tuple
    accumulating sanity-check messages — same convention as the risk
    module so the CLI handles both uniformly.
    """

    take_profit_price: Decimal | None
    stop_loss_price: Decimal | None
    projected_profit_home: Decimal | None  # at TP, positive
    projected_loss_home: Decimal | None    # at SL, negative
    return_on_margin_at_tp: Decimal | None  # fraction, e.g. Decimal("0.10")
    return_on_margin_at_sl: Decimal | None  # fraction, e.g. Decimal("-0.05")
    warnings: tuple[str, ...]


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def pip_size(spec: InstrumentSpec) -> Decimal:
    """One pip in *price* units (quote currency).

    Computed as ``10 ** pip_location``. Decimal exponentiation with a
    negative integer exponent is exact, so ``pip_size`` for
    ``pip_location=-4`` is exactly ``Decimal("0.0001")`` and for
    ``pip_location=-2`` exactly ``Decimal("0.01")``.
    """
    return Decimal(10) ** spec.pip_location


def pip_value_home(units: int, spec: InstrumentSpec, quote: PriceQuote) -> Decimal:
    """Home-currency P&L of a one-pip move on a position of *units*.

    Equivalent to the dollar gain/loss per pip tick:

        pip_value = units × pip_size × quote_to_home

    Always positive — direction (gain vs loss) depends on whether price moved
    with or against the trade, which the caller handles separately.
    """
    return Decimal(units) * pip_size(spec) * quote.quote_to_home


def _favorable_offset_in_quote(
    *,
    target: TPSLSpec,
    units: int,
    spec: InstrumentSpec,
    quote: PriceQuote,
    margin_used: Decimal,
) -> Decimal:
    """
    The magnitude of price movement (in quote currency) that achieves the
    target. Always positive. Caller flips the sign for SL or for SHORT
    direction.

    For ``PIPS`` targets this is simply ``value * pip_size``.
    For ``PERCENT_RETURN`` targets we invert the home-ccy P/L formula:

        profit_home = price_offset_quote * units * quote_to_home
        =>  price_offset_quote = profit_home / (units * quote_to_home)
                              = (margin_used * value) / (units * quote_to_home)

    ``units > 0`` is guaranteed by sizing (we'd have raised
    BelowMinimumUnits otherwise) and ``quote_to_home > 0`` by PriceQuote
    validation, so the division is always safe here.
    """
    if target.kind is TPSLKind.PIPS:
        return target.value * pip_size(spec)
    # PERCENT_RETURN branch: solve for the price move that moves margin by
    # the requested fraction. We deliberately do this in quote-ccy first
    # (rather than home-ccy) because the entry/exit prices we ultimately
    # display are quote-ccy values.
    profit_home = margin_used * target.value
    return profit_home / (Decimal(units) * quote.quote_to_home)


def _profit_home_for_offset(
    *,
    favorable_offset_quote: Decimal,
    units: int,
    quote: PriceQuote,
) -> Decimal:
    """Convert a favorable price offset (quote ccy) into home-ccy P/L.

    Always positive — the caller negates for the loss side.
    """
    return favorable_offset_quote * Decimal(units) * quote.quote_to_home


# ---------------------------------------------------------------------------
# Main entry point
# ---------------------------------------------------------------------------


def compute_exit_levels(
    *,
    entry_price: Decimal,
    units: int,
    direction: Direction,
    spec: InstrumentSpec,
    quote: PriceQuote,
    margin_used: Decimal,
    take_profit: TPSLSpec | None,
    stop_loss: TPSLSpec | None,
) -> ExitLevels:
    """
    Compute exit prices and projected P/L for the supplied TP/SL.

    Either ``take_profit`` or ``stop_loss`` can be ``None``; the
    corresponding output fields are then ``None``. If both are ``None``
    we still return a valid ExitLevels (no prices, empty warnings) — the
    CLI may want to display the trade plan before the user has chosen
    levels.

    Sign handling:

    * For LONG, favorable means UP, adverse means DOWN.
    * For SHORT, favorable means DOWN, adverse means UP.
    * TP is favorable, SL is adverse.

    All arithmetic stays in Decimal to avoid float drift; we never round
    here (the caller handles display rounding to broker-quoted decimals).
    """
    # --- input validation -------------------------------------------------
    # We trust callers from within the domain layer but still guard the
    # public boundary because pricing decisions feed directly into orders.
    if entry_price <= 0:
        raise ValueError(f"entry_price must be positive; got {entry_price!r}")
    if units <= 0:
        raise ValueError(f"units must be positive; got {units!r}")
    if margin_used <= 0:
        raise ValueError(f"margin_used must be positive; got {margin_used!r}")

    warnings: list[str] = []

    # The price-direction multiplier: +1 for LONG (favorable = up),
    # -1 for SHORT (favorable = down). Pulling this out keeps the four
    # branches below readable and avoids repeated direction checks.
    favor_sign: Decimal = Decimal(1) if direction is Direction.LONG else Decimal(-1)

    # ------------------------------------------------------------------
    # Take-profit branch
    # ------------------------------------------------------------------
    tp_price: Decimal | None = None
    profit_home: Decimal | None = None
    rom_tp: Decimal | None = None
    if take_profit is not None:
        tp_offset_quote = _favorable_offset_in_quote(
            target=take_profit,
            units=units,
            spec=spec,
            quote=quote,
            margin_used=margin_used,
        )
        tp_price = entry_price + favor_sign * tp_offset_quote
        profit_home = _profit_home_for_offset(
            favorable_offset_quote=tp_offset_quote,
            units=units,
            quote=quote,
        )
        rom_tp = profit_home / margin_used

        # --- TP sanity warnings --------------------------------------
        # Pip-equivalent of the TP, regardless of how it was specified.
        tp_pips_equiv = tp_offset_quote / pip_size(spec)
        if tp_pips_equiv > UNREALISTIC_PIP_THRESHOLD:
            warnings.append(
                f"take-profit is {tp_pips_equiv:.0f} pips away "
                f"(threshold {UNREALISTIC_PIP_THRESHOLD}); "
                f"check the value"
            )
        if rom_tp > UNREALISTIC_RETURN_THRESHOLD:
            warnings.append(
                f"take-profit return on margin is "
                f"{rom_tp * 100:.1f}% (threshold "
                f"{UNREALISTIC_RETURN_THRESHOLD * 100:.0f}%); "
                f"check the value"
            )

    # ------------------------------------------------------------------
    # Stop-loss branch (mirror of TP, with sign flips)
    # ------------------------------------------------------------------
    sl_price: Decimal | None = None
    loss_home: Decimal | None = None
    rom_sl: Decimal | None = None
    if stop_loss is not None:
        sl_offset_quote = _favorable_offset_in_quote(
            target=stop_loss,
            units=units,
            spec=spec,
            quote=quote,
            margin_used=margin_used,
        )
        # SL sits on the *adverse* side of entry, so we subtract a
        # favorable offset.
        sl_price = entry_price - favor_sign * sl_offset_quote
        # Loss is negative by convention so the CLI doesn't have to
        # remember to flip the sign.
        loss_home = -_profit_home_for_offset(
            favorable_offset_quote=sl_offset_quote,
            units=units,
            quote=quote,
        )
        rom_sl = loss_home / margin_used

        # --- SL sanity warnings --------------------------------------
        sl_pips_equiv = sl_offset_quote / pip_size(spec)
        if sl_pips_equiv > UNREALISTIC_PIP_THRESHOLD:
            warnings.append(
                f"stop-loss is {sl_pips_equiv:.0f} pips away "
                f"(threshold {UNREALISTIC_PIP_THRESHOLD}); "
                f"check the value"
            )
        # rom_sl is negative; compare its magnitude.
        if -rom_sl > UNREALISTIC_RETURN_THRESHOLD:
            warnings.append(
                f"stop-loss return on margin is "
                f"{rom_sl * 100:.1f}% (threshold "
                f"-{UNREALISTIC_RETURN_THRESHOLD * 100:.0f}%); "
                f"check the value"
            )

    return ExitLevels(
        take_profit_price=tp_price,
        stop_loss_price=sl_price,
        projected_profit_home=profit_home,
        projected_loss_home=loss_home,
        return_on_margin_at_tp=rom_tp,
        return_on_margin_at_sl=rom_sl,
        warnings=tuple(warnings),
    )
