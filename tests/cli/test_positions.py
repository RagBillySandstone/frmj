"""Tests for ``frmj positions``."""

from __future__ import annotations

import sqlite3
from decimal import Decimal
from pathlib import Path

import pytest
from typer.testing import CliRunner

from frmj.app import get_db, set_config
from frmj.cli import app
from frmj.cli._display import _daily_financing_home
from frmj.domain.sizing import PriceQuote
from frmj.execution.oanda import FinancingRate, OpenTrade

from .conftest import FakeFullClient, _open_trade

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
    ) -> object:
        fake = FakeFullClient(open_trades=trades)
        monkeypatch.setattr("frmj.cli.positions.get_client", lambda conn: fake)
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
        monkeypatch.setattr("frmj.cli.positions.get_client", lambda conn: fake)
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
        monkeypatch.setattr("frmj.cli.positions.get_client", lambda conn: fake)
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
        monkeypatch.setattr("frmj.cli.positions.get_client", lambda conn: fake)
        result = runner.invoke(app, ["positions"])
        assert result.exit_code == 1
        assert "Error" in result.output + result.stderr

    def test_get_client_error_exits_1(
        self, pos_db: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A missing/invalid token surfaces as a RuntimeError from get_client
        itself (before any Oanda call is attempted)."""

        def _fail(conn: object) -> None:
            raise RuntimeError("No token configured for this account")

        monkeypatch.setattr("frmj.cli.positions.get_client", _fail)
        result = runner.invoke(app, ["positions"])
        assert result.exit_code == 1
        assert "Error" in result.output + result.stderr


# ---------------------------------------------------------------------------
# financing command
# ---------------------------------------------------------------------------
