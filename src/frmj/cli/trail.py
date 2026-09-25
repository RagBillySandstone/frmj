"""``frmj trail`` — add, change, or remove the trailing stop on an open trade."""

from __future__ import annotations

from decimal import Decimal, InvalidOperation

import typer

from frmj import services
from frmj.app import get_client, get_db
from frmj.cli import app
from frmj.cli._completion import _complete_account_name, _complete_open_trade_id
from frmj.cli._display import _pl_str, _projected_pl_at_price
from frmj.domain.pricing import trailing_stop_distance, trailing_trigger_now
from frmj.domain.sizing import Direction
from frmj.execution.oanda import OpenTrade

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _parse_trail_pips(value: str) -> Decimal | None:
    """Parse the PIPS argument: ``off`` → ``None``, else a pip count.

    Accepts an optional trailing ``p`` (``15p``), as the ``trade`` prompts
    do. Only the syntax is checked here; positivity and the instrument's
    bounds are checked by ``trailing_stop_distance`` once the spec is known.

    Raises:
        ValueError: if *value* is neither ``off`` nor a number.
    """
    text = value.strip().lower()
    if text == "off":
        return None
    try:
        return Decimal(text.removesuffix("p"))
    except InvalidOperation:
        raise ValueError(f"expected a pip distance or 'off'; got {value!r}") from None


def _current_exits_str(trade: OpenTrade) -> str:
    """One line summarising the trade's current TP / SL / trailing stop."""
    tp = trade.take_profit_price if trade.take_profit_price is not None else "none"
    sl = trade.stop_loss_price if trade.stop_loss_price is not None else "none"
    trail = "none"
    if trade.trailing_stop_distance is not None:
        # The trigger price moves with the market; show it when Oanda gave it.
        trail = f"{trade.trailing_stop_distance} behind"
        if trade.trailing_stop_price is not None:
            trail = f"{trade.trailing_stop_price} [{trail}]"
    return f"TP: {tp}  SL: {sl}  Trail: {trail}"


# ---------------------------------------------------------------------------
# trail command
# ---------------------------------------------------------------------------


@app.command()
def trail(
    trade_id: str = typer.Argument(
        ...,
        help="Open trade ID, as shown by 'frmj positions'.",
        autocompletion=_complete_open_trade_id,
    ),
    pips: str = typer.Argument(
        ...,
        help="Trailing distance in pips (e.g. 15 or 15p), or 'off' to remove it.",
    ),
    account: str | None = typer.Option(
        None,
        "--account",
        "-a",
        help="Use this account instead of the active one (see 'frmj account list').",
        autocompletion=_complete_account_name,
    ),
) -> None:
    """Add, change, or remove the trailing stop on an open trade."""
    # Reject a malformed PIPS before touching the network.
    try:
        trail_pips = _parse_trail_pips(pips)
    except ValueError as exc:
        typer.echo(f"Error: {exc}", err=True)
        raise typer.Exit(1)

    conn = get_db()
    try:
        client = get_client(conn, account)
    except RuntimeError as exc:
        typer.echo(f"Error: {exc}", err=True)
        conn.close()
        raise typer.Exit(1)

    try:
        # --- locate the trade --------------------------------------------
        try:
            open_trades = client.get_open_trades()
        except Exception as exc:
            typer.echo(f"Error fetching open trades: {exc}", err=True)
            raise typer.Exit(1)
        trade = next((t for t in open_trades if t.trade_id == trade_id), None)
        if trade is None:
            typer.echo(f"Error: no open trade #{trade_id}.", err=True)
            raise typer.Exit(1)

        # Name an overridden account up front, as `close` does, so the
        # confirmation can't be mistaken for the active account's.
        if account is not None:
            typer.echo(f"Account: {account}")
        typer.echo(
            f"#{trade.trade_id}  {trade.instrument}  {trade.direction}"
            f"  {trade.units:,} units  @ {trade.open_price}"
            f"  P/L: {_pl_str(trade.unrealised_pl)}"
        )
        typer.echo(f"  {_current_exits_str(trade)}")
        typer.echo("")

        # --- build the change and ask for confirmation --------------------
        distance: Decimal | None = None
        if trail_pips is None:
            # Removing: nothing to do without a trail; warn if unprotected after.
            if trade.trailing_stop_distance is None:
                typer.echo(f"Trade #{trade_id} has no trailing stop.")
                return
            if trade.stop_loss_price is None:
                typer.secho(
                    "  Warning: no fixed stop-loss — this trade will have no "
                    "stop at all.",
                    fg=typer.colors.YELLOW,
                )
            question = f"Remove the trailing stop from #{trade_id}?"
        else:
            # Setting: validate against the instrument, then preview the trigger.
            try:
                context = services.fetch_instrument_context(client, trade.instrument)
            except Exception as exc:
                typer.echo(f"Error fetching {trade.instrument} data: {exc}", err=True)
                raise typer.Exit(1)
            try:
                distance = trailing_stop_distance(trail_pips, context.spec)
            except ValueError as exc:
                typer.echo(f"Error: {exc}", err=True)
                raise typer.Exit(1)
            direction = Direction(trade.direction.lower())
            trigger = trailing_trigger_now(
                distance, direction, context.quote, context.spec
            )
            # P/L from entry: a trail on a winning trade can lock in profit.
            trigger_pl = _projected_pl_at_price(
                trade, trigger, context.quote.quote_to_home
            )
            action = "Replace" if trade.trailing_stop_distance is not None else "Set"
            typer.echo(
                f"  New trail: {trail_pips:.1f}p ({distance})"
                f"  →  triggers now at {trigger}"
                f"  →  {_pl_str(trigger_pl)} from entry"
            )
            question = f"{action} the trailing stop on #{trade_id}?"

        typer.echo("")
        if not typer.confirm(question, default=False):
            typer.echo("Cancelled.")
            return

        # --- send it, then report the outcome and the follow-up sync -------
        result = services.execute_trail(conn, client, trade_id, distance)
        if result.error is not None:
            typer.echo(f"Error: trailing stop not changed: {result.error}", err=True)
            raise typer.Exit(1)
        verb = "removed" if distance is None else "set"
        typer.echo(f"Trailing stop {verb} (txn #{result.transaction_id}).")

        if result.sync_error is not None:
            typer.echo(f"[sync] Warning: sync failed — {result.sync_error}", err=True)
        elif result.sync_rows_ingested:
            typer.echo(f"[sync] +{result.sync_rows_ingested} transactions")
    finally:
        conn.close()
