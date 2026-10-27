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

from frmj.accounts import resolve_account
from frmj.domain.pricing import pip_size
from frmj.domain.sizing import InstrumentSpec, PriceQuote
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

# Indent for the detail lines under a transaction's header line, shared with
# the Plan/Note/Tags lines journal.py prints beneath it.
_JOURNAL_INDENT = "    "

# Oanda fill reasons for a market order that closed a trade or a whole
# position by hand (e.g. ``frmj close`` or the Oanda web portal).
_MANUAL_CLOSE_REASONS = frozenset(
    {"MARKET_ORDER_TRADE_CLOSE", "MARKET_ORDER_POSITION_CLOSEOUT"}
)


def _display_transaction(
    txn: sqlite3.Row, account_label: str | None = None, id_width: int = 0
) -> None:
    """Print one transaction for the journal as a header line plus details.

    The header is ``#ID  TIME  [ACCOUNT  ]TYPE`` — short enough to fit an
    80-column terminal.  Fills and financing rows add one indented detail
    line beneath it (instrument, direction, units, price, and any P/L or
    financing amount); other transaction types are header-only.

    *account_label*, when given, is printed after the time so rows from
    different accounts can be told apart (used by ``frmj journal
    --all-accounts``).  Callers pass it pre-padded to a common width so the
    type column stays aligned.  *id_width* pads the ID the same way, so the
    time column lines up when IDs in one listing differ in length.
    """
    time_short = _to_local_str(txn["time"])

    detail = ""
    if txn["type"] == "ORDER_FILL":
        try:
            data = json.loads(txn["raw_json"])
            instrument = data.get("instrument", "")
            units = int(Decimal(data.get("units", "0")))
            reason = data.get("reason", "")
            side = "BUY" if units >= 0 else "SELL"
            # A closing fill is labelled by what closed it, not by its own
            # units' sign — closing a short is a BUY but not a LONG.
            if reason == "TAKE_PROFIT_ORDER":
                direction = "TP"
            elif reason == "STOP_LOSS_ORDER":
                direction = "SL"
            elif reason == "TRAILING_STOP_LOSS_ORDER":
                direction = "TS"
            elif reason in _MANUAL_CLOSE_REASONS:
                direction = "CLOSE"
            else:
                direction = "LONG" if units >= 0 else "SHORT"
            detail = f"{instrument} {direction} {side} {abs(units):,} units"
            price = data.get("price")
            if price:
                detail += f" @ {price}"
            # pl is non-zero only on closing fills; opening fills carry "0".
            pl_val = Decimal(data.get("pl", "0") or "0")
            if pl_val != 0:
                detail += f"  P/L {_color_pl(pl_val)}"
        except Exception:
            pass

    elif txn["type"] == "DAILY_FINANCING":
        try:
            data = json.loads(txn["raw_json"])
            # Parent has "financing"; per-instrument children have "amount".
            raw_amount = data.get("amount") or data.get("financing") or "0"
            amount_val = Decimal(raw_amount)
            instrument = data.get("instrument", "")
            prefix = f"{instrument}  " if instrument else ""
            if amount_val != 0:
                detail = f"{prefix}Financing {_color_pl(amount_val)}"
            else:
                detail = instrument
        except Exception:
            pass

    oanda_id = f"#{txn['oanda_id']}".ljust(id_width + 1)
    account_col = f"{account_label}  " if account_label is not None else ""
    typer.echo(f"{oanda_id}  {time_short}  {account_col}{txn['type']}")
    if detail:
        typer.echo(f"{_JOURNAL_INDENT}{detail}")


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


def _profit_pips_and_pct(
    trade: OpenTrade, quote: PriceQuote, spec: InstrumentSpec | None
) -> tuple[Decimal | None, Decimal]:
    """Return the trade's open profit as ``(pips, percent)``, as Oanda's web
    portal shows them in its Trades list.

    Both measure the price move from entry to the price the trade would
    close at now — the bid for a LONG, the ask for a SHORT — signed so a
    favorable move is positive. Percent is that move relative to the entry
    price (e.g. ``Decimal("-0.32")`` for -0.32%), independent of position
    size. Pips is ``None`` when *spec* (needed for pip size) is unavailable.
    """
    # Close side of the book, and the move signed in the trade's favor.
    close_price = quote.bid if trade.direction == "LONG" else quote.ask
    favor_sign = Decimal(1) if trade.direction == "LONG" else Decimal(-1)
    move = favor_sign * (close_price - trade.open_price)

    pips = move / pip_size(spec) if spec is not None else None
    pct = move / trade.open_price * 100
    return pips, pct


def _signed_colored(text: str, value: Decimal) -> str:
    """Color *text* green when *value* ≥ 0, red otherwise (as ``_pl_str``)."""
    color = typer.colors.GREEN if value >= 0 else typer.colors.RED
    return typer.style(text, fg=color)


def _display_open_trade(
    trade: OpenTrade,
    quote: PriceQuote | None,
    spec: InstrumentSpec | None,
    financing_rate: FinancingRate | None,
    has_note: bool,
) -> None:
    """Print one open trade in the positions view, sized to fit an 80-column
    terminal: the trade header (ID, instrument, direction, size, entry),
    then P/L (with pips and percent) and margin, then daily financing and
    open time, then each exit level (TP, SL, trailing stop) on its own line.

    ``quote`` is the live quote for the trade's instrument. Its close-side
    price gives the trade's profit in pips and percent, and its
    ``quote_to_home`` rate is used to show the dollar P/L expected if the
    trade hits TP or SL, and to convert the estimated daily financing charge
    into home currency. ``None`` when the live quote couldn't be fetched, in
    which case only the dollar P/L and raw TP/SL prices are shown.

    ``spec`` is the instrument's spec, needed for pip size. ``None`` when it
    couldn't be fetched, in which case the pip figure is omitted.

    ``financing_rate`` is the instrument's current long/short annualized
    financing rate. ``None`` when it couldn't be fetched, in which case no
    financing figure is shown.

    ``has_note`` adds a ``[note]`` flag: the trade's opening fill has notes
    in the local journal.
    """
    note_flag = "  [note]" if has_note else ""

    time_short = _to_local_str(trade.open_time)
    quote_to_home = quote.quote_to_home if quote is not None else None

    typer.echo(
        f"  #{trade.trade_id}  {trade.instrument}  {trade.direction}"
        f"  {trade.units:,} units  @ {trade.open_price}{note_flag}"
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
    if not exits_parts:
        exits_parts.append("no TP/SL set")

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
        financing_str = f"financing: {_pl_str(daily_financing)}/day  "

    # Profit in pips and percent, alongside the dollar P/L. Decimal keeps a
    # negative zero's sign ("-0.0"), so normalize zero before formatting.
    profit_str = ""
    if quote is not None:
        pips, pct = _profit_pips_and_pct(trade, quote, spec)
        profit_parts: list[str] = []
        if pips is not None:
            pips = abs(pips) if pips == 0 else pips
            profit_parts.append(_signed_colored(f"{pips:+,.1f} pips", pips))
        pct = abs(pct) if pct == 0 else pct
        profit_parts.append(_signed_colored(f"{pct:+.2f}%", pct))
        profit_str = f" ({', '.join(profit_parts)})"

    # Short lines under the header so the trade fits an 80-column terminal:
    # P/L and margin, then financing and open time, then one exit per line.
    typer.echo(
        f"         P/L: {_pl_str(trade.unrealised_pl)}{profit_str}"
        f"  margin: ${trade.margin_used:,.2f}"
    )
    typer.echo(f"         {financing_str}opened {time_short}")
    for exit_str in exits_parts:
        typer.echo(f"         {exit_str}")
    typer.echo("")


def _display_pending_order(
    order: PendingOrder, quote: PriceQuote | None, has_note: bool
) -> None:
    """Print one pending entry order in the positions view, sized to fit an
    80-column terminal: the order header (ID, instrument, direction, type,
    size, price), then the market price, time in force, and placement time,
    then each exit level (TP, SL, trailing stop) on its own line.

    ``quote`` is the instrument's live quote, used to show the current price
    on the side the order would fill against (ask for a long, bid for a
    short) next to the order's price. ``None`` when the quote couldn't be
    fetched, in which case that figure is omitted.

    ``has_note`` adds a ``[note]`` flag: the order's own transaction (the
    order ID) has notes, which is where ``frmj trade --limit`` puts them
    until the order fills.
    """
    note_flag = "  [note]" if has_note else ""

    # "MARKET_IF_TOUCHED" reads better as "MARKET IF TOUCHED".
    order_type = order.order_type.replace("_", " ")
    typer.echo(
        f"  #{order.order_id}  {order.instrument}  {order.direction} {order_type}"
        f"  {order.units:,} units  @ {order.price}{note_flag}"
    )

    # Show the side of the book the order fills against.
    market_str = ""
    if quote is not None:
        if order.direction == "LONG":
            market_str = f"market: ask {quote.ask}  "
        else:
            market_str = f"market: bid {quote.bid}  "
    typer.echo(
        f"         {market_str}{order.time_in_force}"
        f"  placed {_to_local_str(order.create_time)}"
    )

    parts: list[str] = []
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
    for part in parts:
        typer.echo(f"         {part}")
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


# ---------------------------------------------------------------------------
# Account header (shared by close, positions, sync, and tstop)
# ---------------------------------------------------------------------------


def _display_account_header(conn: sqlite3.Connection, account_name: str | None) -> None:
    """
    Print ``Account: NAME  [practice|live, OANDA_ID]`` for the target account.

    *account_name* is the command's ``--account`` override, or ``None`` for the
    active account. Commands print this before any results or confirmation
    prompt so their output can't be mistaken for another account's.

    Prints nothing when the account can't be resolved: callers have either
    already failed on that (``get_client``) or will report it themselves.
    """
    record = resolve_account(conn, account_name)
    if record is None:
        return
    acct_type = "practice" if record.is_practice else "live"
    typer.echo(f"Account: {record.name}  [{acct_type}, {record.oanda_id}]")
