"""Tests for ``frmj positions``."""

from __future__ import annotations

import sqlite3
from decimal import Decimal
from pathlib import Path

import pytest
from click.testing import Result
from typer.testing import CliRunner

from frmj.app import get_db, set_config
from frmj.cli import app
from frmj.cli._display import _daily_financing_home
from frmj.domain.sizing import PriceQuote
from frmj.execution.oanda import FinancingRate, OpenTrade, PendingOrder

from .conftest import FakeFullClient, _open_trade, _pending_order

runner = CliRunner()


class TestPositionsCommand:
    @pytest.fixture()
    def pos_db(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
        path = tmp_path / "pos_test.db"
        monkeypatch.setenv("FRMJ_DB_PATH", str(path))
        monkeypatch.setenv("OANDA_API_TOKEN", "test-token-123")
        conn = get_db(path=path)
        set_config(conn, "account_id", "acct-1")
        conn.close()
        return path

    def _invoke(
        self,
        monkeypatch: pytest.MonkeyPatch,
        trades: list[OpenTrade],
    ) -> Result:
        fake = FakeFullClient(open_trades=trades)
        monkeypatch.setattr(
            "frmj.cli.positions.get_client", lambda conn, account_name=None: fake
        )
        return runner.invoke(app, ["positions"])

    def test_no_open_positions_message(
        self, pos_db: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        result = self._invoke(monkeypatch, [])
        assert result.exit_code == 0, result.output
        assert "No open positions" in result.output

    def test_shows_instrument_and_direction(
        self, pos_db: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        result = self._invoke(
            monkeypatch, [_open_trade(instrument="EUR_USD", direction="LONG")]
        )
        assert result.exit_code == 0, result.output
        assert "EUR_USD" in result.output
        assert "LONG" in result.output

    def test_shows_units_and_entry_price(
        self, pos_db: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        result = self._invoke(
            monkeypatch, [_open_trade(units=10_000, open_price="1.10050")]
        )
        assert "10,000" in result.output
        assert "1.10050" in result.output

    def test_shows_tp_and_sl(
        self, pos_db: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        result = self._invoke(
            monkeypatch,
            [_open_trade(tp_price="1.10550", sl_price="1.09750")],
        )
        assert "TP: 1.10550" in result.output
        assert "SL: 1.09750" in result.output

    def test_no_tpsl_shows_fallback_text(
        self, pos_db: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        result = self._invoke(monkeypatch, [_open_trade(tp_price=None, sl_price=None)])
        assert "no TP/SL set" in result.output

    def test_shows_dollar_amount_at_tp_and_sl(
        self, pos_db: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """TP/SL lines include the projected home-currency P/L if hit.

        FakeFullClient.get_price returns quote_to_home=1, so for this LONG
        10,000-unit EUR_USD trade: TP is 500 pips (+$50.00) above entry,
        SL is 300 pips ($-30.00) below entry.
        """
        result = self._invoke(
            monkeypatch,
            [
                _open_trade(
                    direction="LONG",
                    units=10_000,
                    open_price="1.10050",
                    tp_price="1.10550",
                    sl_price="1.09750",
                )
            ],
        )
        assert "TP: 1.10550 (+$50.00)" in result.output
        assert "SL: 1.09750 ($-30.00)" in result.output

    def test_shows_daily_financing_charge(
        self, pos_db: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Financing shows as a per-day dollar figure, not the raw annualized
        rate, derived from the instrument's long/short financing rate."""
        trade = _open_trade(
            instrument="EUR_USD", direction="LONG", units=10_000, open_price="1.10050"
        )
        fake = FakeFullClient(
            open_trades=[trade],
            financing_rates=[
                FinancingRate("EUR_USD", Decimal("-0.0365"), Decimal("0.0135"))
            ],
        )
        monkeypatch.setattr(
            "frmj.cli.positions.get_client", lambda conn, account_name=None: fake
        )
        result = runner.invoke(app, ["positions"])
        assert result.exit_code == 0, result.output

        expected = _daily_financing_home(
            units=10_000,
            entry_price=Decimal("1.10050"),
            quote_to_home=Decimal("1"),
            rate=Decimal("-0.0365"),
        )
        assert f"financing: ${expected:,.2f}/day" in result.output

    def test_financing_omitted_when_no_rate_available(
        self, pos_db: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """FakeFullClient's default empty financing_rates list mimics a
        fetch that returned nothing for this instrument."""
        result = self._invoke(monkeypatch, [_open_trade()])
        assert "financing:" not in result.output

    def test_dollar_amount_omitted_when_price_fetch_fails(
        self, pos_db: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """If the live quote can't be fetched, TP/SL prices still show but
        without a dollar amount, rather than failing the whole command."""
        fake = FakeFullClient(open_trades=[_open_trade()])

        def _fail(instrument: str, home_currency: str = "USD") -> PriceQuote:
            raise RuntimeError("pricing endpoint unavailable")

        fake.get_price = _fail  # type: ignore[method-assign]
        monkeypatch.setattr(
            "frmj.cli.positions.get_client", lambda conn, account_name=None: fake
        )
        result = runner.invoke(app, ["positions"])
        assert result.exit_code == 0, result.output
        assert "TP: 1.10550" in result.output
        assert "$" not in result.output.split("TP: 1.10550")[1].split("\n")[0]

    def test_shows_position_count(
        self, pos_db: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        result = self._invoke(
            monkeypatch,
            [_open_trade(trade_id="1"), _open_trade(trade_id="2")],
        )
        assert "2 open positions" in result.output

    def test_note_flag_shown_when_notes_exist(
        self, pos_db: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A trade whose fill transaction has notes shows [note] in output."""
        # Seed the fill transaction and a note directly in the DB.
        conn = sqlite3.connect(str(pos_db))
        conn.execute(
            "INSERT INTO transactions (oanda_id, account_id, type, time, raw_json) "
            "VALUES ('6368', 'acct-1', 'ORDER_FILL', '2026-04-25T14:30:00Z', '{}')"
        )
        txn_id = conn.execute(
            "SELECT id FROM transactions WHERE oanda_id='6368'"
        ).fetchone()[0]
        conn.execute(
            "INSERT INTO notes (transaction_id, body) VALUES (?, 'Test note')",
            (txn_id,),
        )
        conn.commit()
        conn.close()

        result = self._invoke(monkeypatch, [_open_trade(trade_id="6368")])
        assert "[note]" in result.output

    def test_no_note_flag_when_no_notes(
        self, pos_db: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        result = self._invoke(monkeypatch, [_open_trade(trade_id="9999")])
        assert "[note]" not in result.output

    def test_api_error_exits_1(
        self, pos_db: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        fake = FakeFullClient()

        def _fail() -> list:
            raise RuntimeError("Oanda API unavailable")

        fake.get_open_trades = _fail  # type: ignore[method-assign]
        monkeypatch.setattr(
            "frmj.cli.positions.get_client", lambda conn, account_name=None: fake
        )
        result = runner.invoke(app, ["positions"])
        assert result.exit_code == 1
        assert "Error" in result.output + result.stderr

    def test_get_client_error_exits_1(
        self, pos_db: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A missing/invalid token surfaces as a RuntimeError from get_client
        itself (before any Oanda call is attempted)."""

        def _fail(conn: object, account_name: str | None = None) -> None:
            raise RuntimeError("No token configured for this account")

        monkeypatch.setattr("frmj.cli.positions.get_client", _fail)
        result = runner.invoke(app, ["positions"])
        assert result.exit_code == 1
        assert "Error" in result.output + result.stderr

    def test_account_option_targets_named_account(
        self, pos_db: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """--account is handed to get_client and named in the output."""
        requested: list[str | None] = []
        fake = FakeFullClient(open_trades=[_open_trade()])

        def _get_client(conn: object, account_name: str | None = None) -> object:
            requested.append(account_name)
            return fake

        monkeypatch.setattr("frmj.cli.positions.get_client", _get_client)
        result = runner.invoke(app, ["positions", "--account", "other"])
        assert result.exit_code == 0, result.output
        assert requested == ["other"]
        assert "Account: other" in result.output

    def test_unknown_account_exits_1(self, pos_db: Path) -> None:
        """A typo in --account fails before any Oanda call, with a clear hint."""
        result = runner.invoke(app, ["positions", "--account", "ghost"])
        assert result.exit_code == 1
        assert "No account named 'ghost'" in result.output + result.stderr


class TestPositionsPendingOrders:
    """``frmj positions`` lists pending entry orders after open trades.

    FakeFullClient quotes every instrument at bid 1.09990 / ask 1.10010.
    """

    @pytest.fixture()
    def pos_db(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
        path = tmp_path / "pos_pending_test.db"
        monkeypatch.setenv("FRMJ_DB_PATH", str(path))
        monkeypatch.setenv("OANDA_API_TOKEN", "test-token-123")
        conn = get_db(path=path)
        set_config(conn, "account_id", "acct-1")
        conn.close()
        return path

    def _invoke(self, monkeypatch: pytest.MonkeyPatch, fake: FakeFullClient) -> Result:
        monkeypatch.setattr(
            "frmj.cli.positions.get_client", lambda conn, account_name=None: fake
        )
        return runner.invoke(app, ["positions"])

    def test_pending_only_shows_section_and_summary(
        self, pos_db: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        fake = FakeFullClient(pending_orders=[_pending_order()])
        result = self._invoke(monkeypatch, fake)
        assert result.exit_code == 0, result.output
        assert "No open positions." in result.output
        assert "1 pending order" in result.output
        assert "#7001  EUR_USD  LONG LIMIT  10,000 units  @ 1.09500  GTC" in (
            result.output
        )
        assert "market: ask 1.10010" in result.output
        assert "no TP/SL set" in result.output
        assert "Margin Available" in result.output
        # Oanda's fraction 0.0558 is shown as a percent.
        assert "Margin Closeout" in result.output
        assert "5.58%" in result.output

    def test_short_order_shows_bid_and_tpsl(
        self, pos_db: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        order = PendingOrder(
            order_id="7002",
            order_type="MARKET_IF_TOUCHED",
            instrument="EUR_USD",
            direction="SHORT",
            units=5_000,
            price=Decimal("1.10500"),
            time_in_force="GTC",
            create_time="2026-04-25T14:30:00.000000Z",
            take_profit_price=Decimal("1.10000"),
            stop_loss_price=Decimal("1.10800"),
        )
        result = self._invoke(monkeypatch, FakeFullClient(pending_orders=[order]))
        assert result.exit_code == 0, result.output
        assert "SHORT MARKET IF TOUCHED" in result.output
        assert "market: bid 1.09990  TP: 1.10000  SL: 1.10800" in result.output

    def test_open_trades_and_pending_orders_both_listed(
        self, pos_db: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        fake = FakeFullClient(
            open_trades=[_open_trade()],
            pending_orders=[_pending_order(order_id="7001"), _pending_order("7003")],
        )
        result = self._invoke(monkeypatch, fake)
        assert result.exit_code == 0, result.output
        assert "1 open position" in result.output
        assert "2 pending orders" in result.output
        # Open trades come first, then pending orders.
        assert result.output.index("#6368") < result.output.index("#7001")

    def test_no_pending_section_when_none(
        self, pos_db: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        result = self._invoke(monkeypatch, FakeFullClient(open_trades=[_open_trade()]))
        assert result.exit_code == 0, result.output
        assert "pending" not in result.output

    def test_note_flag_on_pending_order(
        self, pos_db: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Notes live on the LIMIT_ORDER transaction until the order fills."""
        conn = sqlite3.connect(str(pos_db))
        conn.execute(
            "INSERT INTO transactions (oanda_id, account_id, type, time, raw_json) "
            "VALUES ('7001', 'acct-1', 'LIMIT_ORDER', '2026-04-25T14:30:00Z', '{}')"
        )
        txn_id = conn.execute(
            "SELECT id FROM transactions WHERE oanda_id='7001'"
        ).fetchone()[0]
        conn.execute(
            "INSERT INTO notes (transaction_id, body) VALUES (?, 'Waiting')",
            (txn_id,),
        )
        conn.commit()
        conn.close()

        fake = FakeFullClient(pending_orders=[_pending_order(order_id="7001")])
        result = self._invoke(monkeypatch, fake)
        assert "[note]" in result.output

    def test_quote_failure_omits_market_price(
        self, pos_db: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        fake = FakeFullClient(pending_orders=[_pending_order()])

        def _fail(instrument: str, home_currency: str = "USD") -> PriceQuote:
            raise RuntimeError("no price")

        fake.get_price = _fail  # type: ignore[method-assign]
        result = self._invoke(monkeypatch, fake)
        assert result.exit_code == 0, result.output
        assert "#7001" in result.output
        assert "market:" not in result.output

    def test_pending_fetch_failure_warns_and_still_shows_trades(
        self, pos_db: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        fake = FakeFullClient(open_trades=[_open_trade()])

        def _fail() -> list:
            raise RuntimeError("Oanda unreachable")

        fake.get_pending_orders = _fail  # type: ignore[method-assign]
        result = self._invoke(monkeypatch, fake)
        assert result.exit_code == 0, result.output
        assert "#6368" in result.output
        assert "could not fetch pending orders" in result.output + result.stderr


# ---------------------------------------------------------------------------
# financing command
# ---------------------------------------------------------------------------
