"""Display/formatting helpers shared by two or more CLI commands.

Helpers used by only a single command live next to that command instead
(e.g. ``_display_stats`` in ``stats.py``, ``_display_financing_rates`` in
``financing.py``, ``_display_exits`` in ``_trade_helpers.py``).
"""

from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timezone
from decimal import Decimal

import typer

from frmj.domain.sizing import PriceQuote
from frmj.execution.oanda import (
    AccountSummary,
    FinancingRate,
    OpenTrade,
    PendingOrder,
)

# ---------------------------------------------------------------------------
# Financing-rate formatting (shared by financing.py and trade.py)
# ---------------------------------------------------------------------------


def _fmt_financing_pct(rate: Decimal) -> str:
    """Format an annualized financing-rate fraction as a signed percentage.

    E.g. ``Decimal("-0.0141")`` -> ``"-1.4100%"``. Four decimal places
    (rather than the app-wide 1dp convention used for trade-level
    percentages like win rate) because financing-rate differences that
    matter to a carry trader are often well under 1%.
    """
    return f"{rate * 100:+.4f}%"


def _color_financing_pct(rate: Decimal) -> str:
    """Color a formatted financing-rate percentage green (positive) or red (negative)."""
    text = _fmt_financing_pct(rate)
    if rate > 0:
        return typer.style(text, fg=typer.colors.GREEN)
    if rate < 0:
        return typer.style(text, fg=typer.colors.RED)
    return text


#: Divisor Oanda uses to turn its annualized financing rate into a daily
#: figure. We don't attempt to model the Wednesday triple-charge (weekend
#: rollover) — this is a plain daily estimate, not what Oanda will actually
#: post on any given day.
_FINANCING_DAYS_PER_YEAR = Decimal("365")


def _daily_financing_home(
    *, units: int, entry_price: Decimal, quote_to_home: Decimal, rate: Decimal
) -> Decimal:
    """Estimate one day's financing accrual for a position, in home currency.

    ``rate`` is Oanda's annualized financing fraction for the position's
    direction (``FinancingRate.long_rate`` or ``.short_rate``); it already
    carries the sign (positive = position earns financing, negative =
    position pays it). The notional position value is ``units * entry_price``
    in quote currency, converted to home currency via ``quote_to_home``.
    """
    notional_home = Decimal(units) * entry_price * quote_to_home
    return notional_home * rate / _FINANCING_DAYS_PER_YEAR


# ---------------------------------------------------------------------------
# P/L formatting (shared by close.py, trade.py, positions.py, stats.py,
# journal.py)
# ---------------------------------------------------------------------------


def _pl_str(amount: Decimal) -> str:
    """Return a sign-prefixed, coloured P/L string: green ≥0, red <0."""
    sign = "+" if amount >= 0 else ""
    color = typer.colors.GREEN if amount >= 0 else typer.colors.RED
    # Decimal preserves a negative zero's sign in formatting (e.g. "-0.00"),
    # which would double up with the "+" prefix above; normalize it away.
    if amount == 0:
        amount = abs(amount)
    return typer.style(f"{sign}${amount:,.2f}", fg=color)


def _color_pl(pl: Decimal) -> str:
    """Return a colored P/L string like '+$45.23' or '-$3.50' (no leading spaces)."""
    sign = "+" if pl > 0 else "-" if pl < 0 else ""
    text = f"{sign}${abs(pl):,.2f}"
    if pl > 0:
        return typer.style(text, fg=typer.colors.GREEN)
    if pl < 0:
        return typer.style(text, fg=typer.colors.RED)
    return text


def _pl_visible_width(pl: Decimal) -> int:
    """Return the visible (non-ANSI) character width of the string _color_pl produces."""
    sign = "+" if pl > 0 else "-" if pl < 0 else ""
    return len(f"{sign}${abs(pl):,.2f}")


def _color_pl_padded(pl: Decimal, width: int) -> str:
    """Return _color_pl(pl) right-justified to *width* visible chars.

    Leading spaces are prepended so the dollar sign and digits are flush-right
    within the column, regardless of the numeric magnitude of *pl*.
    """
    return " " * max(0, width - _pl_visible_width(pl)) + _color_pl(pl)


# ---------------------------------------------------------------------------
# Timestamp formatting (shared by journal.py and positions.py)
# ---------------------------------------------------------------------------


def _to_local_str(ts: str) -> str:
    """Convert an Oanda UTC timestamp to local wall-clock time (YYYY-MM-DD HH:MM:SS)."""
    dt = datetime.fromisoformat(ts[:19]).replace(tzinfo=timezone.utc).astimezone()
    return dt.strftime("%Y-%m-%d %H:%M:%S")


# ---------------------------------------------------------------------------
# Transaction row display (shared by journal.py and sync.py's _watch_loop)
# ---------------------------------------------------------------------------

# Fixed widths for the variable-length "extra" (instrument/direction/units)
# and P/L segments of a journal row, so the trailing time column lands in the
# same place regardless of how long those segments are for a given row.
_JOURNAL_EXTRA_W = 46
_JOURNAL_PL_W = 12


def _display_transaction(txn: sqlite3.Row, account_label: str | None = None) -> None:
    """Format one transaction row for journal display.

    *account_label*, when given, is printed as a column after the transaction
    number so rows from different accounts can be told apart (used by
    ``frmj journal --all-accounts``).  Callers pass it pre-padded to a common
    width so the remaining columns stay aligned.
    """
    # Trim the ISO-8601 timestamp to seconds for readability.
    time_short = _to_local_str(txn["time"])

    extra = ""
    pl: Decimal | None = None

    if txn["type"] == "ORDER_FILL":
        try:
            data = json.loads(txn["raw_json"])
            instrument = data.get("instrument", "")
            units = int(Decimal(data.get("units", "0")))
            reason = data.get("reason", "")
            side = "BUY" if units >= 0 else "SELL"
            if reason == "TAKE_PROFIT_ORDER":
                direction = "TP"
            elif reason == "STOP_LOSS_ORDER":
                direction = "SL"
            else:
                direction = "LONG" if units >= 0 else "SHORT"
            extra = f"  {instrument} {direction} {side} {abs(units):,} units"
            price = data.get("price")
            if price:
                extra += f" @ {price}"
            # pl is non-zero only on closing fills; opening fills carry "0".
            pl_val = Decimal(data.get("pl", "0") or "0")
            if pl_val != 0:
                pl = pl_val
        except Exception:
            pass

    elif txn["type"] == "DAILY_FINANCING":
        try:
            data = json.loads(txn["raw_json"])
            instrument = data.get("instrument", "")
            if instrument:
                extra = f"  {instrument}"
            # Parent has "financing"; per-instrument children have "amount".
            raw_amount = data.get("amount") or data.get("financing") or "0"
            amount_val = Decimal(raw_amount)
            if amount_val != 0:
                pl = amount_val
        except Exception:
            pass

    extra = extra.ljust(_JOURNAL_EXTRA_W)
    pl_str = (
        f"  {_color_pl_padded(pl, _JOURNAL_PL_W)}"
        if pl is not None
        else " " * (_JOURNAL_PL_W + 2)
    )
    account_col = f"{account_label}  " if account_label is not None else ""
    typer.echo(
        f"#{txn['oanda_id']:<12}  {account_col}{txn['type']:<24}"
        f"{extra}{pl_str}  {time_short}"
    )


# ---------------------------------------------------------------------------
# Open-position display (positions.py)
# ---------------------------------------------------------------------------


def _projected_pl_at_price(
    trade: OpenTrade, exit_price: Decimal, quote_to_home: Decimal
) -> Decimal:
    """Home-currency P/L if *trade* were closed at *exit_price* right now.

    Positive when *exit_price* is favorable for the trade's direction
    (up for LONG, down for SHORT), matching the sign convention used by
    ``compute_exit_levels``'s ``projected_profit_home``/``projected_loss_home``.
    """
    favor_sign = Decimal(1) if trade.direction == "LONG" else Decimal(-1)
    return favor_sign * (exit_price - trade.open_price) * trade.units * quote_to_home


def _display_open_trade(
    conn: sqlite3.Connection,
    trade: OpenTrade,
    quote_to_home: Decimal | None,
    financing_rate: FinancingRate | None,
) -> None:
    """Print one open trade in the positions view.

    ``quote_to_home`` is the live conversion rate for the trade's instrument,
    used to show the dollar P/L expected if the trade hits TP or SL, and to
    convert the estimated daily financing charge into home currency. ``None``
    when the live quote couldn't be fetched, in which case only the raw
    TP/SL prices are shown and no financing figure is shown.

    ``financing_rate`` is the instrument's current long/short annualized
    financing rate. ``None`` when it couldn't be fetched, in which case no
    financing figure is shown.
    """
    note_count = conn.execute(
        """
        SELECT COUNT(*) FROM notes n
        JOIN transactions t ON n.transaction_id = t.id
        WHERE t.oanda_id = ?
        """,
        (trade.trade_id,),
    ).fetchone()[0]
    note_flag = "  [note]" if note_count else ""

    time_short = _to_local_str(trade.open_time)

    typer.echo(
        f"  #{trade.trade_id}  {trade.instrument}  {trade.direction}"
        f"  {trade.units:,} units  @ {trade.open_price}"
        f"  (opened {time_short}){note_flag}"
    )

    exits_parts: list[str] = []
    if trade.take_profit_price is not None:
        tp_str = f"TP: {trade.take_profit_price}"
        if quote_to_home is not None:
            tp_pl = _projected_pl_at_price(
                trade, trade.take_profit_price, quote_to_home
            )
            tp_str += f" ({_pl_str(tp_pl)})"
        exits_parts.append(tp_str)
    if trade.stop_loss_price is not None:
        sl_str = f"SL: {trade.stop_loss_price}"
        if quote_to_home is not None:
            sl_pl = _projected_pl_at_price(trade, trade.stop_loss_price, quote_to_home)
            sl_str += f" ({_pl_str(sl_pl)})"
        exits_parts.append(sl_str)
    if trade.trailing_stop_distance is not None:
        # The trigger price moves, so show where it is now and the P/L
        # there, plus the fixed distance it trails by (in price units).
        trail_str = "Trail:"
        if trade.trailing_stop_price is not None:
            trail_str += f" {trade.trailing_stop_price}"
            if quote_to_home is not None:
                trail_pl = _projected_pl_at_price(
                    trade, trade.trailing_stop_price, quote_to_home
                )
                trail_str += f" ({_pl_str(trail_pl)})"
        trail_str += f" [{trade.trailing_stop_distance} behind]"
        exits_parts.append(trail_str)
    exits_str = "  ".join(exits_parts) if exits_parts else "no TP/SL set"

    financing_str = ""
    if financing_rate is not None and quote_to_home is not None:
        rate = (
            financing_rate.long_rate
            if trade.direction == "LONG"
            else financing_rate.short_rate
        )
        daily_financing = _daily_financing_home(
            units=trade.units,
            entry_price=trade.open_price,
            quote_to_home=quote_to_home,
            rate=rate,
        )
        financing_str = f"  financing: {_pl_str(daily_financing)}/day"

    typer.echo(
        f"         P/L: {_pl_str(trade.unrealised_pl)}"
        f"  margin: ${trade.margin_used:,.2f}"
        f"  {exits_str}"
        f"{financing_str}"
    )
    typer.echo("")


def _display_pending_order(
    conn: sqlite3.Connection, order: PendingOrder, quote: PriceQuote | None
) -> None:
    """Print one pending entry order in the positions view.

    ``quote`` is the instrument's live quote, used to show the current price
    on the side the order would fill against (ask for a long, bid for a
    short) next to the order's price. ``None`` when the quote couldn't be
    fetched, in which case that figure is omitted.

    The ``[note]`` flag reflects notes on the order's own transaction (the
    order ID), where ``frmj trade --limit`` puts them until the order fills.
    """
    note_count = conn.execute(
        """
        SELECT COUNT(*) FROM notes n
        JOIN transactions t ON n.transaction_id = t.id
        WHERE t.oanda_id = ?
        """,
        (order.order_id,),
    ).fetchone()[0]
    note_flag = "  [note]" if note_count else ""

    # "MARKET_IF_TOUCHED" reads better as "MARKET IF TOUCHED".
    order_type = order.order_type.replace("_", " ")
    typer.echo(
        f"  #{order.order_id}  {order.instrument}  {order.direction} {order_type}"
        f"  {order.units:,} units  @ {order.price}  {order.time_in_force}"
        f"  (placed {_to_local_str(order.create_time)}){note_flag}"
    )

    parts: list[str] = []
    if quote is not None:
        # Show the side of the book the order fills against.
        if order.direction == "LONG":
            parts.append(f"market: ask {quote.ask}")
        else:
            parts.append(f"market: bid {quote.bid}")
    if order.take_profit_price is not None:
        parts.append(f"TP: {order.take_profit_price}")
    if order.stop_loss_price is not None:
        parts.append(f"SL: {order.stop_loss_price}")
    if order.trailing_stop_distance is not None:
        parts.append(f"Trail: {order.trailing_stop_distance} behind")
    if (
        order.take_profit_price is None
        and order.stop_loss_price is None
        and order.trailing_stop_distance is None
    ):
        parts.append("no TP/SL set")
    typer.echo("         " + "  ".join(parts))
    typer.echo("")


def _display_account_summary(summary: AccountSummary) -> None:
    """Print account-level summary rows beneath the positions table."""
    rows: list[tuple[str, str]] = [
        ("NAV", f"${summary.nav:,.2f}"),
        ("Unrealized P/L", _pl_str(summary.unrealized_pl)),
        ("Balance", f"${summary.balance:,.2f}"),
        ("Realized P/L", _pl_str(summary.realized_pl)),
        ("Position Value", f"${summary.position_value:,.2f}"),
        ("Margin Used", f"${summary.margin_used:,.2f}"),
        ("Margin Available", f"${summary.margin_available:,.2f}"),
        # Oanda reports a fraction; 100% means margin closeout begins.
        ("Margin Closeout", f"{summary.margin_closeout_percent * 100:.2f}%"),
    ]
    label_width = max(len(label) for label, _ in rows)
    for label, value in rows:
        typer.echo(f"  {label:<{label_width}}  {value}")
    typer.echo("")
