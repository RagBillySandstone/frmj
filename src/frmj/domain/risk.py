"""Risk model and position-sizing decisions.

This module is **pure**: it contains no I/O, no API calls, no CLI dependencies,
and no global state. Everything the decision needs is passed in by the caller;
everything it produces is either a plain data object (``SizingDecision``) or a
typed exception (``MaxTradesExceeded``, ``ScaleInForbidden``).

That purity is deliberate. The risk model is the most important piece of the
system to get right, so we want it:

* trivially unit-testable (no fixtures, no mocks, no DB)
* re-usable from a future GUI or automated trading bot without modification
* deterministic across runs (no clocks, no random number generators)

The whole rest of the application (CLI, services, Oanda adapter, persistence)
exists to feed this module the right inputs and act on its outputs.
"""

# ``from __future__ import annotations`` makes all annotations strings at import
# time. This lets us use ``X | None`` syntax on Python 3.11+ without paying any
# runtime cost, and it sidesteps forward-reference headaches inside dataclasses.
from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from enum import Enum
from fractions import Fraction


# ---------------------------------------------------------------------------
# Configuration enums
# ---------------------------------------------------------------------------
#
# Each enum value carries a stable string (the ``.value``) so it can survive
# round-tripping through TOML/JSON config files unchanged. We avoid bare strings
# in the rest of the codebase so a typo in config validation surfaces here
# rather than at trade-execution time.


class RiskStrategy(Enum):
    """How the user wants the next trade's capital allocation to be determined.

    Only ``REMAINING_MARGIN_FRACTION`` uses the ``1/(M+1-N)`` formula. The
    other two strategies are simpler — fixed % of equity or a fixed dollar
    amount — and exist for users (or test scenarios) where the
    breathing-room-aware formula is not desired.
    """

    # Stephen's preferred strategy: 1/(M+1-N) of currently available margin.
    REMAINING_MARGIN_FRACTION = "remaining_margin_fraction"
    # Constant fraction of total equity, regardless of how many trades are open.
    PERCENT_OF_EQUITY = "percent_of_equity"
    # Constant dollar amount, regardless of equity or open trades.
    FIXED_DOLLAR = "fixed_dollar"


class BlockingMode(Enum):
    """What should happen when the user tries to open trade #M+1.

    ``HARD_BLOCK`` raises an exception so the CLI/GUI can refuse to even ask
    the user for confirmation. ``WARNING_ONLY`` lets the trade proceed but
    surfaces a warning so the caller can scare-prompt before placing it.
    """

    HARD_BLOCK = "hard_block"
    WARNING_ONLY = "warning_only"


class ScaleInPolicy(Enum):
    """Whether to allow opening a second ticket on an instrument that already
    has at least one open ticket.

    Stephen has stated he *wants* to stop scaling in, so ``NEVER`` is the
    expected default in his config — but ``WARN`` and ``ALLOW`` are kept so
    he can deliberately override on a case-by-case basis without editing code.
    """

    # Refuse to size a second ticket on the same instrument.
    NEVER = "never"
    # Size it, but emit a warning so the CLI shows a scare-prompt.
    WARN = "warn"
    # Size it silently — old behaviour, no fuss.
    ALLOW = "allow"


# ---------------------------------------------------------------------------
# Data classes
# ---------------------------------------------------------------------------
#
# We use ``frozen=True, slots=True`` for every dataclass in this module:
#
# * ``frozen`` makes instances hashable and prevents accidental mutation —
#   important because ``RiskConfig`` is read by every trade decision and
#   ``SizingDecision`` is sometimes serialised for the "save plan" failure
#   option. Mutation in either case would be a bug.
# * ``slots`` cuts memory and makes accidental attribute typos raise instead of
#   silently sticking. Cheap discipline.


@dataclass(frozen=True, slots=True)
class RiskConfig:
    """User-configurable risk parameters.

    Constructed once per process from the user's TOML config file (handled
    elsewhere) and then passed by reference into every ``evaluate_trade`` call.
    ``__post_init__`` performs cross-field validation so an invalid config
    fails fast at startup rather than at trade time.
    """

    # Maximum number of concurrently open tickets the user wants to hold.
    # Stephen's default is 6.
    max_open_trades: int

    # Which sizing formula to apply (see RiskStrategy docstring).
    strategy: RiskStrategy

    # What to do at the cap (see BlockingMode docstring).
    blocking_mode: BlockingMode

    # What to do when scaling into an instrument (see ScaleInPolicy docstring).
    scale_in: ScaleInPolicy

    # Fraction of equity that must never be deployed, regardless of strategy.
    # Expressed as a Decimal in [0, 1]. The reserve guards against margin calls
    # caused by adverse moves on existing positions; it's a "never touch this"
    # buffer that sits *outside* the 1/(M+1) breathing room baked into the
    # remaining-margin formula. Defaults to 0 (no extra reserve).
    safety_reserve_pct: Decimal = Decimal("0")

    # Strategy-specific parameters. Marked Optional and validated only when the
    # corresponding strategy is selected — saves the user from having to fill
    # in fields they don't use.
    percent_of_equity: Decimal | None = None  # required for PERCENT_OF_EQUITY
    fixed_dollar: Decimal | None = None  # required for FIXED_DOLLAR

    def __post_init__(self) -> None:
        # All validation happens here so callers can trust a constructed
        # RiskConfig is internally consistent. Anything that survives this
        # method is safe to feed into ``evaluate_trade``.

        # ``max_open_trades`` of 0 would mean "no trades ever," which is a
        # bizarre and almost certainly accidental config; reject explicitly.
        if self.max_open_trades < 1:
            raise ValueError("max_open_trades must be >= 1")

        # Reserve must be a sane fraction. Negatives are nonsensical; > 1 would
        # mean reserving more than the entire account, which is also nonsense.
        if not (Decimal("0") <= self.safety_reserve_pct <= Decimal("1")):
            raise ValueError("safety_reserve_pct must be in [0, 1]")

        # Strategy-specific required fields. We disallow 0% explicitly because
        # selecting PERCENT_OF_EQUITY with 0% is almost certainly a typo —
        # if the user truly wants no allocation, the right answer is to not
        # place the trade.
        if self.strategy is RiskStrategy.PERCENT_OF_EQUITY:
            if self.percent_of_equity is None or not (
                Decimal("0") < self.percent_of_equity <= Decimal("1")
            ):
                raise ValueError(
                    "percent_of_equity must be in (0, 1] for PERCENT_OF_EQUITY"
                )

        if self.strategy is RiskStrategy.FIXED_DOLLAR:
            if self.fixed_dollar is None or self.fixed_dollar <= Decimal("0"):
                raise ValueError("fixed_dollar must be > 0 for FIXED_DOLLAR")


@dataclass(frozen=True, slots=True)
class SizingDecision:
    """The output of ``evaluate_trade``.

    Designed so the CLI/GUI can render it directly without further computation:

    * ``capital_to_deploy`` — the margin amount, in account currency, that the
      caller should commit to this trade. The caller still needs to convert
      this into Oanda *units* using the instrument's pip value and margin rate
      (that's the job of the trade calculation engine, in a separate module).
    * ``strategy_used`` — echoes which strategy produced the number. Useful for
      the confirmation screen so the user can verify the system did what they
      expected.
    * ``size_fraction`` — populated only for ``REMAINING_MARGIN_FRACTION`` so
      the UI can display the literal "1/4" or "1/7" the user knows from their
      mental model. ``None`` for the other strategies, where the concept
      doesn't apply.
    * ``warnings`` — non-fatal messages the caller MUST surface to the user
      before execution. Hard blocks come back as exceptions, not warnings;
      anything in this tuple is "proceed if you want, but know this."
    """

    capital_to_deploy: Decimal
    strategy_used: RiskStrategy
    size_fraction: Fraction | None
    # Tuple (not list) so the dataclass remains hashable and immutable.
    warnings: tuple[str, ...]


# ---------------------------------------------------------------------------
# Typed exceptions
# ---------------------------------------------------------------------------
#
# We raise dedicated exception types (rather than ValueError or RuntimeError)
# so the service layer can catch each failure mode independently and give the
# user a tailored next-action menu (retry / save plan / abort / override).


class MaxTradesExceeded(Exception):
    """Raised when ``open_trades >= max_trades`` under ``HARD_BLOCK`` mode.

    Under ``WARNING_ONLY`` mode the same condition produces a warning in the
    ``SizingDecision`` instead — see ``evaluate_trade``.
    """


class ScaleInForbidden(Exception):
    """Raised when ``scale_in`` is ``NEVER`` and the target instrument already
    has at least one open ticket.

    Distinct from ``MaxTradesExceeded`` because the user-facing remediation is
    different: scale-in failures suggest "close the existing ticket first or
    relax the policy", whereas max-trade failures suggest "close some other
    ticket or relax the cap".
    """


# ---------------------------------------------------------------------------
# Pure functions
# ---------------------------------------------------------------------------


def size_fraction(open_trades: int, max_trades: int) -> Fraction:
    """Return the REMAINING_MARGIN_FRACTION sizing fraction: ``1 / (M + 1 - N)``.

    Where ``N`` is the number of currently open trades and ``M`` is the
    configured cap. Each new trade deploys this fraction of the *currently
    available* margin (not the original margin).

    A pleasant invariant falls out of the formula and is verified by the test
    suite: across the full sequence of M permitted trades, each one consumes
    exactly ``1/(M+1)`` of the *original* margin, leaving a final buffer of
    ``1/(M+1)`` permanently untouched. That untouched slice is the "breathing
    room for margin calls" Stephen described in the original design brief —
    it's not added by us, it falls out of the formula automatically.

    Returns:
        ``Fraction`` (exact rational), never a float. Floats can't represent
        ``1/7`` exactly, and we don't want sizing drift from binary
        floating-point. Callers convert to Decimal at the money boundary.

    Raises:
        MaxTradesExceeded: when ``open_trades >= max_trades``. The caller is
            responsible for deciding whether to honour ``BlockingMode`` or
            re-raise — that decision happens in ``evaluate_trade``.
        ValueError: on negative ``open_trades`` or non-positive ``max_trades``.
    """

    # Validate inputs first so we always fail with a precise message rather
    # than a confusing arithmetic error downstream.
    if max_trades < 1:
        raise ValueError("max_trades must be >= 1")
    if open_trades < 0:
        raise ValueError("open_trades must be >= 0")

    # The formula's denominator is ``M + 1 - N``. When ``N == M`` the
    # denominator is 1 (the formula gives 1/1 = "deploy everything"), which is
    # exactly the wrong behaviour at the cap — it would tell us to bet the
    # whole remaining margin on what would be an over-cap trade. Callers who
    # want to permit that anyway must use WARNING_ONLY mode and ``evaluate_
    # trade`` clamps N appropriately before calling us.
    if open_trades >= max_trades:
        raise MaxTradesExceeded(
            f"open_trades ({open_trades}) >= max_trades ({max_trades})"
        )

    # Construct the Fraction directly with integer numerator/denominator so
    # there's no float intermediate. ``Fraction(1, 7)`` is exactly 1/7.
    return Fraction(1, max_trades + 1 - open_trades)


def evaluate_trade(
    *,
    config: RiskConfig,
    open_trades: int,
    open_tickets_on_instrument: int,
    available_margin: Decimal,
    equity: Decimal,
) -> SizingDecision:
    """Decide how much capital to deploy for a proposed trade.

    The single public entry point for the risk model. Service-layer code
    assembles the inputs (by querying Oanda and the local DB) and the CLI/GUI
    presents the resulting decision to the user.

    Args:
        config: User's risk configuration. Already validated.
        open_trades: Current count of open tickets across all instruments.
            Counted *per ticket*, not per instrument — this matches Oanda's
            own model and Stephen's stated preference. If three AUD_USD
            tickets are open, that's 3 trades, not 1.
        open_tickets_on_instrument: How many of those open tickets belong to
            the instrument this proposed trade would target. Used only for
            the scale-in policy check.
        available_margin: Margin currently available to open new positions,
            in account currency. From Oanda's account summary.
        equity: Total account equity (NAV), in account currency. Also from
            Oanda. Used as the basis for ``safety_reserve_pct`` and for the
            ``PERCENT_OF_EQUITY`` strategy.

    Returns:
        A ``SizingDecision`` containing the recommended capital to deploy and
        any non-fatal warnings the caller must surface before execution.

    Raises:
        ScaleInForbidden: scale_in is ``NEVER`` and the instrument already
            has at least one open ticket.
        MaxTradesExceeded: ``open_trades >= max_open_trades`` under
            ``HARD_BLOCK`` mode.
        ValueError: on negative inputs.
    """

    # ----- Argument sanity ----------------------------------------------------
    # Catch obviously-bad inputs before doing anything else. These would
    # almost always indicate a bug in the service layer (e.g., a failed Oanda
    # call returning -1), so a loud ValueError is the right response.
    if open_trades < 0 or open_tickets_on_instrument < 0:
        raise ValueError("trade counts must be >= 0")
    if available_margin < Decimal(0) or equity < Decimal(0):
        raise ValueError("money values must be >= 0")

    # ----- Warnings collection ------------------------------------------------
    # We accumulate non-fatal warnings here as we evaluate each rule. They are
    # bundled into the returned SizingDecision so the caller can show them all
    # at once rather than dribbling them out one rule at a time.
    warnings: list[str] = []

    # ----- Scale-in policy ----------------------------------------------------
    # Checked before the max-trades cap because scale-in is a more specific
    # failure: telling the user "you can't scale in" is more actionable than
    # the generic "you're at the cap". A user with a NEVER policy who is also
    # at the cap should hear about scale-in first.
    if open_tickets_on_instrument > 0:
        if config.scale_in is ScaleInPolicy.NEVER:
            # Hard rejection — let the service layer translate to a CLI prompt.
            raise ScaleInForbidden(
                f"instrument already has {open_tickets_on_instrument} "
                f"open ticket(s); scale_in policy is NEVER"
            )
        if config.scale_in is ScaleInPolicy.WARN:
            warnings.append(
                f"scaling in: instrument already has "
                f"{open_tickets_on_instrument} open ticket(s)"
            )
        # ScaleInPolicy.ALLOW: silent fall-through, no warning.

    # ----- Max-trades cap -----------------------------------------------------
    # When at or over the cap, behaviour depends on BlockingMode:
    #   HARD_BLOCK    -> raise immediately, the trade cannot proceed.
    #   WARNING_ONLY  -> attach a warning and continue to sizing, with N
    #                    clamped below.
    at_or_over_cap = open_trades >= config.max_open_trades
    if at_or_over_cap:
        if config.blocking_mode is BlockingMode.HARD_BLOCK:
            raise MaxTradesExceeded(
                f"{open_trades} open >= max {config.max_open_trades} "
                f"(HARD_BLOCK)"
            )
        warnings.append(
            f"{open_trades} open trades >= max {config.max_open_trades}"
        )

    # ----- Safety reserve -----------------------------------------------------
    # Compute the "untouchable" portion of equity and subtract it from the
    # available margin to get the actual margin we're allowed to deploy from.
    # If reserve > available_margin (e.g., user is already over-extended on
    # existing positions), usable_margin clamps to 0 and every strategy will
    # return 0 capital_to_deploy — the user effectively can't open new trades
    # until they close some existing ones or the market moves favourably.
    reserve = equity * config.safety_reserve_pct
    usable_margin = max(Decimal(0), available_margin - reserve)

    # ----- Strategy dispatch --------------------------------------------------
    # ``frac`` stays None unless we use the remaining-margin-fraction formula;
    # the other strategies don't have a meaningful "fraction" to display.
    frac: Fraction | None = None

    if config.strategy is RiskStrategy.REMAINING_MARGIN_FRACTION:
        # Under WARNING_ONLY we may have arrived here with N >= M. The
        # formula ``1/(M+1-N)`` would divide by zero (at N=M) or flip
        # negative (at N>M); neither is meaningful as a fraction of remaining
        # margin. The least-bad fallback is to size the override trade as if
        # it were the last permitted one — i.e., clamp the effective N to
        # M-1, giving a fraction of 1/2. The user has already been warned,
        # so we just pick a defensible behaviour rather than blowing up.
        effective_n = min(open_trades, config.max_open_trades - 1)
        frac = size_fraction(effective_n, config.max_open_trades)

        # Mix Fraction and Decimal carefully: Decimal division gets rounded
        # by the current decimal context, but multiplying by an integer
        # numerator first preserves precision. Since our numerators are
        # always 1 today this is somewhat academic, but the code stays
        # correct if we ever generalise to non-unit numerators.
        capital = (
            usable_margin * Decimal(frac.numerator) / Decimal(frac.denominator)
        )

    elif config.strategy is RiskStrategy.PERCENT_OF_EQUITY:
        # ``percent_of_equity`` is guaranteed non-None by RiskConfig validation;
        # the assert documents the invariant for type-checkers and human
        # readers.
        assert config.percent_of_equity is not None
        requested = equity * config.percent_of_equity

        # Cap by usable_margin so we never ask the user to commit more margin
        # than they actually have. If we hit the cap, surface a warning so the
        # user knows their configured % is being downgraded — better than
        # silently deploying less than requested.
        capital = min(requested, usable_margin)
        if capital < requested:
            warnings.append(
                f"capped by usable margin: requested {requested}, "
                f"deploying {capital}"
            )

    elif config.strategy is RiskStrategy.FIXED_DOLLAR:
        assert config.fixed_dollar is not None
        requested = config.fixed_dollar
        capital = min(requested, usable_margin)
        if capital < requested:
            warnings.append(
                f"capped by usable margin: requested {requested}, "
                f"deploying {capital}"
            )

    else:  # pragma: no cover - defensive against future enum additions
        # If a new RiskStrategy is added without updating this dispatch we
        # want a loud failure, not silent zero-sizing.
        raise AssertionError(f"unknown strategy: {config.strategy!r}")

    return SizingDecision(
        capital_to_deploy=capital,
        strategy_used=config.strategy,
        size_fraction=frac,
        # Convert the working list to a tuple so the dataclass stays
        # hashable/immutable.
        warnings=tuple(warnings),
    )
