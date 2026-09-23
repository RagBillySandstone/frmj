"""TP/SL prompt, parsing, and display helpers shared by ``trade.py`` and
``_trade_multi.py``, plus the limit-entry prompt used by ``trade --limit``.
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
    compute_limit_price,
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


def _display_exits(exits: ExitLevels, margin_used: Decimal) -> None:
    """Print the exit-levels table and any warnings."""
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
