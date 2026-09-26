"""TP/SL prompt, parsing, and display helpers shared by ``trade.py`` and
``_trade_multi.py`` (including the daily-ATR default stop-loss), plus the
limit-entry prompt used by ``trade --limit`` and the trailing-stop prompt
and display used by ``trade --trail``.
"""

from __future__ import annotations

from decimal import Decimal, InvalidOperation

import typer

from frmj.cli._display import _pl_str
from frmj.domain.pricing import (
    ExitLevels,
    LimitEntryKind,
    LimitEntrySpec,
    TPSLKind,
    TPSLSpec,
    TrailingStopLevels,
    compute_limit_price,
    compute_trailing_stop,
    planned_loss_home,
)
from frmj.domain.sizing import Direction, InstrumentSpec, PriceQuote


def _prompt_tpsl(label: str) -> TPSLSpec | None:
    """Prompt for a TP or SL value and return a TPSLSpec, or None to skip.

    Accepted formats:
      ``50``  or ``50p``  → 50 pips
      ``10%``             → 10% return on margin  (stored as fraction 0.10)
    """
    while True:
        raw = typer.prompt(
            f"{label} (pips, or 10% for %RoM, Enter to skip)",
            default="",
        ).strip()
        if not raw:
            return None
        try:
            return _parse_tpsl(raw)
        except ValueError as exc:
            typer.echo(f"  Invalid input: {exc}. Try '50' (pips) or '10%'.")


def _parse_tpsl(raw: str) -> TPSLSpec:
    """Parse a TP/SL string into a TPSLSpec.

    Raises ``ValueError`` on unrecognised format or non-positive value.
    """
    raw = raw.strip()
    try:
        if raw.endswith("%"):
            pct = Decimal(raw[:-1])
            return TPSLSpec(kind=TPSLKind.PERCENT_RETURN, value=pct / Decimal("100"))
        # Strip optional trailing 'p' for pips.
        return TPSLSpec(kind=TPSLKind.PIPS, value=Decimal(raw.rstrip("p")))
    except InvalidOperation:
        raise ValueError(f"{raw!r} is not a number") from None


def _fmt_multiple(multiple: Decimal) -> str:
    """Show an ATR multiple without trailing zeros: ``1.50`` -> ``1.5``."""
    return f"{multiple.normalize():f}"


def _parse_atr_multiple(raw: str) -> Decimal | None:
    """Parse an ATR-multiple stop such as ``2x`` or ``1.5atr``.

    Returns the multiple, or ``None`` when *raw* isn't in ATR form (so the
    caller can try the pips / %RoM forms instead).

    Raises ``ValueError`` when it is in ATR form but the number is missing,
    malformed, or not positive.
    """
    text = raw.strip().lower()
    for suffix in ("atr", "x"):
        if not text.endswith(suffix):
            continue
        number = text[: -len(suffix)].strip()
        try:
            multiple = Decimal(number)
        except InvalidOperation:
            raise ValueError(f"{raw!r} is not a valid ATR multiple") from None
        if not multiple.is_finite() or multiple <= 0:
            raise ValueError(f"ATR multiple must be positive; got {raw!r}")
        return multiple
    return None


def _prompt_stop_loss(
    label: str, atr_pips: Decimal | None, default_multiple: Decimal
) -> tuple[TPSLSpec | None, Decimal | None]:
    """Prompt for the stop-loss, defaulting to *default_multiple* x daily ATR.

    Returns ``(spec, atr_multiple)``: *atr_multiple* is the multiple used
    when the stop was ATR-based, or ``None`` for a pips / %RoM stop or no
    stop at all.

    Accepted input when the ATR is known:
      Enter               -> *default_multiple* x ATR
      ``2x`` / ``2atr``   -> 2 x ATR
      ``50`` / ``50p``    -> 50 pips
      ``10%``             -> 10% of margin
      ``-`` / ``none``    -> no stop-loss

    With no ATR (*atr_pips* is ``None``) this is the plain TP/SL prompt,
    where Enter skips.
    """
    # No ATR: nothing to default to, so keep the old Enter-to-skip prompt.
    if atr_pips is None:
        return _prompt_tpsl(label), None

    default_pips = atr_pips * default_multiple
    while True:
        raw = typer.prompt(
            f"{label} (pips, 10%, or 2x ATR; "
            f"Enter = {_fmt_multiple(default_multiple)}x ATR = {default_pips:.1f}p, "
            "'-' to skip)",
            default="",
            show_default=False,
        ).strip()
        # Enter takes the ATR default; skipping the stop must be explicit.
        if not raw:
            return TPSLSpec(kind=TPSLKind.PIPS, value=default_pips), default_multiple
        if raw.lower() in ("-", "none"):
            return None, None
        try:
            # An ATR multiple becomes a pip distance; anything else is the
            # ordinary pips / %RoM form.
            multiple = _parse_atr_multiple(raw)
            if multiple is not None:
                return TPSLSpec(kind=TPSLKind.PIPS, value=atr_pips * multiple), multiple
            return _parse_tpsl(raw), None
        except ValueError as exc:
            typer.echo(
                f"  Invalid input: {exc}. Try '2x' (ATR), '50' (pips), '10%', or '-'."
            )


def _display_daily_atr(period: int, atr_pips: Decimal | None) -> None:
    """Print the trade plan's daily-ATR line, or note that it's unavailable."""
    if atr_pips is None:
        typer.echo(f"  Daily ATR({period}): unavailable (no default stop-loss)")
    else:
        typer.echo(f"  Daily ATR({period}): {atr_pips:.1f}p")


def _prompt_limit_price(
    direction: Direction, spec: InstrumentSpec, quote: PriceQuote
) -> Decimal:
    """Prompt for a limit order's entry until it gives a valid limit price.

    Accepted formats (see ``_parse_limit_entry``): ``15``/``15p`` pips better
    than the market, ``@1.0950`` an absolute price, ``0.5%`` a percent of the
    current price. Offsets are measured from the ask for a long and the bid
    for a short, and a price that would fill immediately is re-prompted.
    """
    side = "ask" if direction is Direction.LONG else "bid"
    while True:
        raw = typer.prompt(
            f"Limit entry (pips from {side}, @price, or 0.5% of price)"
        ).strip()
        try:
            entry = _parse_limit_entry(raw)
            return compute_limit_price(
                entry=entry, direction=direction, spec=spec, quote=quote
            )
        except ValueError as exc:
            typer.echo(
                f"  Invalid input: {exc}. Try '15' (pips), '@1.0950', or '0.5%'."
            )


def _parse_limit_entry(raw: str) -> LimitEntrySpec:
    """Parse a limit-entry string into a LimitEntrySpec.

    ``@PRICE`` is an absolute price, a trailing ``%`` is a percent of the
    current price (stored as a fraction, so ``0.5%`` -> ``0.005``), and
    anything else is pips with an optional trailing ``p``.

    Raises ``ValueError`` on an unrecognised format or non-positive value.
    """
    raw = raw.strip()
    try:
        if raw.startswith("@"):
            return LimitEntrySpec(kind=LimitEntryKind.PRICE, value=Decimal(raw[1:]))
        if raw.endswith("%"):
            pct = Decimal(raw[:-1])
            return LimitEntrySpec(
                kind=LimitEntryKind.PERCENT_OF_PRICE, value=pct / Decimal("100")
            )
        return LimitEntrySpec(kind=LimitEntryKind.PIPS, value=Decimal(raw.rstrip("p")))
    except InvalidOperation:
        raise ValueError(f"{raw!r} is not a number") from None


def _prompt_trailing_stop(
    *,
    entry_price: Decimal,
    units: int,
    direction: Direction,
    spec: InstrumentSpec,
    quote: PriceQuote,
    margin_used: Decimal,
    label: str = "Trailing stop",
) -> TrailingStopLevels | None:
    """Prompt for a trailing stop in pips; return its levels, or None to skip.

    Accepts ``20`` or ``20p``. Re-prompts on anything that isn't a positive
    number or that ``compute_trailing_stop`` rejects (e.g. a distance below
    the instrument's minimum), showing the reason.
    """
    while True:
        raw = typer.prompt(f"{label} (pips, Enter to skip)", default="").strip()
        if not raw:
            return None
        try:
            # Strip an optional trailing 'p', as the TP/SL prompts do.
            pips = Decimal(raw.rstrip("p"))
        except InvalidOperation:
            typer.echo(f"  Invalid input: {raw!r} is not a number. Try '20' (pips).")
            continue
        try:
            return compute_trailing_stop(
                pips=pips,
                entry_price=entry_price,
                units=units,
                direction=direction,
                spec=spec,
                quote=quote,
                margin_used=margin_used,
            )
        except ValueError as exc:
            typer.echo(f"  Invalid input: {exc}.")


def _display_trail(trail: TrailingStopLevels) -> None:
    """Print a trailing stop's row of the exit table, and any warnings.

    The loss shown is at the initial trigger, which sits behind the bid
    (long) / ask (short), so it includes the spread.
    """
    typer.echo(
        f"  Trail: {trail.distance_pips:.1f}p"
        f"  →  starts at {trail.initial_trigger_price}"
        f"  →  {_pl_str(trail.projected_loss_home)}"
        f"  ({trail.return_on_margin * 100:+.1f}% RoM, incl. spread)"
    )
    for warn in trail.warnings:
        typer.echo(f"  ! {warn}", err=True)


def _display_risk_reward(exits: ExitLevels, trail: TrailingStopLevels | None) -> None:
    """Print the plan's R:R, measured against the tighter of SL and trail.

    Prints nothing without both a take-profit and some stop.
    """
    loss = planned_loss_home(exits, trail)
    if exits.projected_profit_home is None or loss is None or loss == 0:
        return
    rr = abs(exits.projected_profit_home / loss)
    typer.echo(f"  R:R  {rr:.2f}")


def _display_exits(
    exits: ExitLevels, margin_used: Decimal, sl_atr_multiple: Decimal | None = None
) -> None:
    """Print the exit-levels table and any warnings.

    *sl_atr_multiple* tags the SL row with the ATR multiple it came from.
    """
    typer.echo("Exit levels:")
    # compute_exit_levels sets projected_profit_home/return_on_margin_at_tp
    # together with take_profit_price — never independently None.
    if exits.take_profit_price is not None:
        assert exits.projected_profit_home is not None
        assert exits.return_on_margin_at_tp is not None
        typer.echo(
            f"  TP: {exits.take_profit_price}"
            f"  →  {_pl_str(exits.projected_profit_home)}"
            f"  ({exits.return_on_margin_at_tp * 100:+.1f}% RoM)"
        )
    if exits.stop_loss_price is not None:
        assert exits.projected_loss_home is not None
        assert exits.return_on_margin_at_sl is not None
        typer.echo(
            f"  SL: {exits.stop_loss_price}"
            f"  →  {_pl_str(exits.projected_loss_home)}"
            f"  ({exits.return_on_margin_at_sl * 100:+.1f}% RoM)"
            + (
                f"  [{_fmt_multiple(sl_atr_multiple)}× ATR]"
                if sl_atr_multiple is not None
                else ""
            )
        )
    for warn in exits.warnings:
        typer.echo(f"  ! {warn}", err=True)


def _prompt_retry_save_abort() -> str:
    """Prompt the user after a failed order attempt.

    Returns 'r' (retry), 's' (save plan), or 'a' (abort).
    """
    while True:
        raw = typer.prompt("[R]etry / [S]ave plan / [A]bort").strip().lower()
        if raw in ("r", "s", "a"):
            return raw
        typer.echo("  Enter R, S, or A.")
