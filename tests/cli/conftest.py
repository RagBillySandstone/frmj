"""Shared fixtures and fake Oanda clients for the ``frmj.cli`` test package.

``FakeClient`` satisfies the minimal ``ClientProtocol`` needed by sync-only
tests; ``FakeFullClient`` additionally provides account_summary, instrument,
price, open_tickets, place_market_order, and close_trade — used by every
command that touches live market/account data (trade, positions, financing,
close, config check --connectivity, stats/journal auto-sync).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from decimal import Decimal
from pathlib import Path

import click
import pytest
import typer

from frmj.accounts import add_account, set_active_account
from frmj.app import get_db
from frmj.domain.sizing import InstrumentSpec, PriceQuote
from frmj.execution.oanda import (
    AccountSummary,
    CloseFill,
    FinancingRate,
    LimitOrderResult,
    OpenTrade,
    OrderFill,
    PendingOrder,
    TransactionRow,
)

# ---------------------------------------------------------------------------
# Fake clients
# ---------------------------------------------------------------------------


@dataclass
class FakeClient:
    account_id: str
    responses: list[list[TransactionRow]] = field(default_factory=list)

    def get_transactions_since(
        self, from_id: str | None = None
    ) -> list[TransactionRow]:
        if not self.responses:
            return []
        return self.responses.pop(0)


def _row(oanda_id: str, account_id: str = "acct-1") -> TransactionRow:
    return TransactionRow(
        oanda_id=oanda_id,
        account_id=account_id,
        type="ORDER_FILL",
        time="2026-04-25T12:00:00.000000Z",
        parent_oanda_id=None,
        raw_json="{}",
    )


# ---------------------------------------------------------------------------
# FakeFullClient — satisfies all OandaClient methods used by the trade command
# ---------------------------------------------------------------------------


@dataclass
class FakeFullClient:
    """Test double for the full OandaClient interface needed by ``trade``.

    All methods are no-ops or return safe default values.  ``order_placed``
    is set to True when ``place_market_order`` is called, letting tests
    assert that dry-run skips order placement.

    ``tp_should_fail`` / ``sl_should_fail`` / ``trail_should_fail`` cause the
    attach methods to raise,
    simulating a network error after the fill.
    """

    account_id: str = "acct-1"
    order_placed: bool = False
    tp_attached: str | None = None  # price string passed to attach_take_profit
    sl_attached: str | None = None  # price string passed to attach_stop_loss
    tp_should_fail: bool = False
    sl_should_fail: bool = False
    # Distance string passed to attach_trailing_stop.
    trail_attached: str | None = None
    trail_should_fail: bool = False
    sync_rows: list[TransactionRow] = field(default_factory=list)
    sync_should_fail: bool = False

    # --- ClientProtocol (for the auto-sync step) ----------------------------
    def get_transactions_since(self, from_id: str | None = None) -> list:
        if self.sync_should_fail:
            raise RuntimeError("Oanda unreachable during sync")
        return self.sync_rows

    # --- Trade-flow methods --------------------------------------------------
    def get_account_summary(self) -> AccountSummary:
        return AccountSummary(
            nav=Decimal("10000.00"),
            balance=Decimal("9500.00"),
            unrealized_pl=Decimal("500.00"),
            realized_pl=Decimal("1200.00"),
            position_value=Decimal("220000.00"),
            margin_used=Decimal("2000.00"),
            margin_available=Decimal("8000.00"),
            open_trade_count=2,
            margin_closeout_percent=Decimal("0.0558"),
        )

    def get_open_tickets_on_instrument(self, instrument: str) -> int:
        return 0

    def get_instrument(self, name: str) -> InstrumentSpec:
        return InstrumentSpec(
            name=name,
            pip_location=-4,
            margin_rate=Decimal("0.02"),
            min_units=1,
            units_increment=1,
            display_precision=5,
        )

    def get_price(self, instrument: str, home_currency: str = "USD") -> PriceQuote:
        return PriceQuote(
            bid=Decimal("1.09990"),
            ask=Decimal("1.10010"),
            quote_to_home=Decimal("1"),
            base_to_home=Decimal("1.10"),
        )

    def place_market_order(self, instrument: str, units_signed: int) -> OrderFill:
        self.order_placed = True
        return OrderFill(
            transaction_id="99999",
            fill_price=Decimal("1.10005"),
            units_filled=units_signed,
            trade_id="99999",
        )

    # --- Limit orders -------------------------------------------------------
    # Each placed limit order's arguments, in call order.
    limit_orders: list[dict] = field(default_factory=list)
    # Simulate the market crossing the limit before the order arrives.
    limit_fills_immediately: bool = False
    # Number of place_limit_order calls that raise before one succeeds.
    limit_fail_count: int = 0

    def place_limit_order(
        self,
        instrument: str,
        units_signed: int,
        price: Decimal,
        take_profit_price: Decimal | None = None,
        stop_loss_price: Decimal | None = None,
        trailing_stop_distance: Decimal | None = None,
    ) -> LimitOrderResult:
        if self.limit_fail_count > 0:
            self.limit_fail_count -= 1
            raise RuntimeError("Network error")
        self.limit_orders.append(
            {
                "instrument": instrument,
                "units_signed": units_signed,
                "price": price,
                "take_profit_price": take_profit_price,
                "stop_loss_price": stop_loss_price,
                "trailing_stop_distance": trailing_stop_distance,
            }
        )
        fill = None
        if self.limit_fills_immediately:
            fill = OrderFill(
                transaction_id="88889",
                fill_price=price,
                units_filled=units_signed,
                trade_id="88889",
            )
        return LimitOrderResult(order_id="88888", fill=fill)

    def attach_take_profit(self, trade_id: str, price: Decimal) -> str:
        if self.tp_should_fail:
            raise RuntimeError("TP order rejected by Oanda")
        self.tp_attached = str(price)
        return "100001"

    def attach_stop_loss(self, trade_id: str, price: Decimal) -> str:
        if self.sl_should_fail:
            raise RuntimeError("SL order rejected by Oanda")
        self.sl_attached = str(price)
        return "100002"

    def attach_trailing_stop(self, trade_id: str, distance: Decimal) -> str:
        if self.trail_should_fail:
            raise RuntimeError("Trailing stop rejected by Oanda")
        self.trail_attached = str(distance)
        return "100003"

    open_trades: list[OpenTrade] = field(default_factory=list)
    close_should_fail: bool = False
    closed_trade_ids: list[str] = field(default_factory=list)
    financing_rates: list[FinancingRate] = field(default_factory=list)
    financing_should_fail: bool = False

    pending_orders: list[PendingOrder] = field(default_factory=list)

    def get_open_trades(self) -> list[OpenTrade]:
        return self.open_trades

    def get_pending_orders(self) -> list[PendingOrder]:
        return self.pending_orders

    def get_financing_rates(self, instruments: list[str]) -> list[FinancingRate]:
        if self.financing_should_fail:
            raise RuntimeError("Oanda unreachable")
        return self.financing_rates

    def close_trade(self, trade_id: str) -> CloseFill:
        if self.close_should_fail:
            raise RuntimeError("Close rejected by Oanda")
        self.closed_trade_ids.append(trade_id)
        return CloseFill(
            transaction_id=str(int(trade_id) + 1000),
            close_price=Decimal("1.10095"),
            realised_pl=Decimal("45.23"),
        )


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


def _open_trade(
    trade_id: str = "6368",
    instrument: str = "EUR_USD",
    direction: str = "LONG",
    units: int = 10_000,
    open_price: str = "1.10050",
    unrealised_pl: str = "45.23",
    margin_used: str = "220.10",
    tp_price: str | None = "1.10550",
    sl_price: str | None = "1.09750",
) -> OpenTrade:
    return OpenTrade(
        trade_id=trade_id,
        instrument=instrument,
        direction=direction,
        units=units,
        open_price=Decimal(open_price),
        unrealised_pl=Decimal(unrealised_pl),
        margin_used=Decimal(margin_used),
        take_profit_price=Decimal(tp_price) if tp_price else None,
        stop_loss_price=Decimal(sl_price) if sl_price else None,
        open_time="2026-04-25T14:30:00.000000Z",
    )


def _completion_ctx(params: dict[str, object]) -> typer.Context:
    """Return a real ``typer.Context`` carrying *params*.

    Stands in for the context a shell-completion callback receives: the
    completers only read ``ctx.params`` (the options already typed on the
    command line), so a bare context around a dummy command is enough.
    """
    ctx = typer.Context(click.Command("test"))
    ctx.params = dict(params)
    return ctx


def _pending_order(
    order_id: str = "7001",
    instrument: str = "EUR_USD",
    direction: str = "LONG",
    units: int = 10_000,
    price: str = "1.09500",
) -> PendingOrder:
    return PendingOrder(
        order_id=order_id,
        order_type="LIMIT",
        instrument=instrument,
        direction=direction,
        units=units,
        price=Decimal(price),
        time_in_force="GTC",
        create_time="2026-04-25T14:30:00.000000Z",
        take_profit_price=None,
        stop_loss_price=None,
    )


@pytest.fixture()
def db_path(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Set FRMJ_DB_PATH to a temp location and seed a practice account.

    OANDA_API_TOKEN is set so that get_token falls back to it for practice
    accounts, keeping tests independent of the OS keychain.
    """
    path = tmp_path / "frmj_test.db"
    monkeypatch.setenv("FRMJ_DB_PATH", str(path))
    monkeypatch.setenv("OANDA_API_TOKEN", "test-token-123")
    # Add a named practice account and activate it.
    conn = get_db(path=path)
    add_account(conn, "practice", "acct-1", is_practice=True)
    set_active_account(conn, "practice")
    conn.close()
    return path
