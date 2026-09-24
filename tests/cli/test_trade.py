"""Tests for ``frmj trade``: dry-run, execute, error paths, retry/resume, and multi-account."""

from __future__ import annotations

import json
from decimal import Decimal
from pathlib import Path

import httpx
import pytest
from click.testing import Result
from typer.testing import CliRunner

from frmj.accounts import (
    AccountRecord,
    add_account,
    add_group_member,
    set_active_account,
)
from frmj.app import get_db, set_config
from frmj.cli import app
from frmj.domain.sizing import InstrumentSpec
from frmj.execution.oanda import AccountSummary, FinancingRate, OrderFill

from .conftest import FakeFullClient, _open_trade, _pending_order

runner = CliRunner()


def _failing_market_order(instrument: str, units_signed: int) -> OrderFill:
    """Stand-in for ``place_market_order`` that always fails, to reach the
    retry / save / abort prompt."""
    raise RuntimeError("fail")


class TestDryRun:
    """The --dry-run flag shows the plan and exits without placing an order."""

    @pytest.fixture()
    def trade_db(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
        """DB with all required config for the trade command."""
        path = tmp_path / "trade_test.db"
        monkeypatch.setenv("FRMJ_DB_PATH", str(path))
        monkeypatch.setenv("OANDA_API_TOKEN", "test-token-123")
        conn = get_db(path=path)
        set_config(conn, "account_id", "acct-1")
        set_config(conn, "max_open_trades", "5")
        conn.close()
        return path

    def test_dry_run_exits_zero(
        self, trade_db: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """``frmj trade EUR_USD long --dry-run`` must exit 0."""
        fake = FakeFullClient()
        monkeypatch.setattr(
            "frmj.cli.trade.get_client", lambda conn, account_name=None: fake
        )
        # Provide TP and SL input (50 pips, 30 pips), then dry-run exits.
        result = runner.invoke(
            app, ["trade", "EUR_USD", "long", "--dry-run"], input="50\n30\n"
        )
        assert result.exit_code == 0, result.output

    def test_dry_run_prints_dry_run_message(
        self, trade_db: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Output must contain the [DRY RUN] marker."""
        monkeypatch.setattr(
            "frmj.cli.trade.get_client",
            lambda conn, account_name=None: FakeFullClient(),
        )
        result = runner.invoke(
            app, ["trade", "EUR_USD", "long", "--dry-run"], input="50\n30\n"
        )
        assert "[DRY RUN]" in result.output

    def test_dry_run_does_not_place_order(
        self, trade_db: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """``place_market_order`` must NOT be called in dry-run mode."""
        fake = FakeFullClient()
        monkeypatch.setattr(
            "frmj.cli.trade.get_client", lambda conn, account_name=None: fake
        )
        runner.invoke(app, ["trade", "EUR_USD", "long", "--dry-run"], input="50\n30\n")
        assert not fake.order_placed

    def test_dry_run_shows_exit_levels(
        self, trade_db: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Exit levels table (TP and SL) appears in dry-run output."""
        monkeypatch.setattr(
            "frmj.cli.trade.get_client",
            lambda conn, account_name=None: FakeFullClient(),
        )
        result = runner.invoke(
            app, ["trade", "EUR_USD", "long", "--dry-run"], input="50\n30\n"
        )
        assert "TP:" in result.output
        assert "SL:" in result.output

    def test_dry_run_skip_tpsl_shows_no_exit_levels(
        self, trade_db: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Pressing Enter for both TP and SL yields no exit levels line."""
        monkeypatch.setattr(
            "frmj.cli.trade.get_client",
            lambda conn, account_name=None: FakeFullClient(),
        )
        # Empty input for both TP and SL prompts.
        result = runner.invoke(
            app, ["trade", "EUR_USD", "long", "--dry-run"], input="\n\n"
        )
        assert result.exit_code == 0
        # Neither TP nor SL was supplied, so no exit levels table is printed.
        assert "TP:" not in result.output
        assert "SL:" not in result.output

    def test_dry_run_shows_daily_financing(
        self, trade_db: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Trade plan shows the estimated daily financing cost/credit."""
        fake = FakeFullClient(
            financing_rates=[
                FinancingRate("EUR_USD", Decimal("-0.0141"), Decimal("0.0021"))
            ]
        )
        monkeypatch.setattr(
            "frmj.cli.trade.get_client", lambda conn, account_name=None: fake
        )
        result = runner.invoke(
            app, ["trade", "EUR_USD", "long", "--dry-run"], input="50\n30\n"
        )
        assert result.exit_code == 0, result.output
        assert "Financing:" in result.output
        assert "/day" in result.output

    def test_dry_run_omits_financing_line_when_rate_unavailable(
        self, trade_db: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """No financing line is shown when the rate fetch fails or is empty."""
        fake = FakeFullClient(financing_should_fail=True)
        monkeypatch.setattr(
            "frmj.cli.trade.get_client", lambda conn, account_name=None: fake
        )
        result = runner.invoke(
            app, ["trade", "EUR_USD", "long", "--dry-run"], input="50\n30\n"
        )
        assert result.exit_code == 0, result.output
        assert "Financing:" not in result.output


class TestTradeExecute:
    """Confirmed trade path (non-dry-run) with TP/SL attachment."""

    @pytest.fixture()
    def trade_db(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
        path = tmp_path / "trade_exec_test.db"
        monkeypatch.setenv("FRMJ_DB_PATH", str(path))
        monkeypatch.setenv("OANDA_API_TOKEN", "test-token-123")
        conn = get_db(path=path)
        set_config(conn, "account_id", "acct-1")
        set_config(conn, "max_open_trades", "5")
        conn.close()
        return path

    def _invoke(
        self,
        monkeypatch: pytest.MonkeyPatch,
        fake: FakeFullClient,
        inputs: str,
    ) -> Result:
        monkeypatch.setattr(
            "frmj.cli.trade.get_client", lambda conn, account_name=None: fake
        )
        return runner.invoke(app, ["trade", "EUR_USD", "long"], input=inputs)

    def test_tpsl_both_attached_after_fill(
        self, trade_db: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Both TP and SL are sent to Oanda after a confirmed fill."""
        fake = FakeFullClient()
        # TP=50 pips, SL=30 pips, confirm=y, note=skip, tags=skip
        result = self._invoke(monkeypatch, fake, "50\n30\ny\n\n\n")
        assert result.exit_code == 0, result.output
        assert fake.tp_attached is not None
        assert fake.sl_attached is not None
        assert "Take-profit set" in result.output
        assert "Stop-loss set" in result.output

    def test_no_tpsl_skipped_means_no_attachment(
        self, trade_db: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Skipping both TP and SL means neither attach method is called."""
        fake = FakeFullClient()
        # skip TP, skip SL, confirm=y, note=skip, tags=skip
        result = self._invoke(monkeypatch, fake, "\n\ny\n\n\n")
        assert result.exit_code == 0, result.output
        assert fake.tp_attached is None
        assert fake.sl_attached is None
        # The prompt labels contain these words, so check for the post-fill confirmation.
        assert "Take-profit set" not in result.output
        assert "Stop-loss set" not in result.output

    def test_invalid_tpsl_input_reprompts(
        self, trade_db: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """An unparseable TP value re-prompts instead of crashing; a valid
        value on retry proceeds normally."""
        fake = FakeFullClient()
        # invalid TP, then valid TP=50 pips; skip SL, confirm=y, note/tags skip
        result = self._invoke(monkeypatch, fake, "abc\n50\n\ny\n\n\n")
        assert result.exit_code == 0, result.output
        assert "Invalid input" in result.output
        assert fake.tp_attached is not None

    def test_percent_return_tpsl_format_accepted(
        self, trade_db: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A '10%' TP value is parsed as a percent-return-on-margin spec."""
        fake = FakeFullClient()
        result = self._invoke(monkeypatch, fake, "10%\n\ny\n\n\n")
        assert result.exit_code == 0, result.output
        assert fake.tp_attached is not None

    def test_unrealistic_tp_shows_warning(
        self, trade_db: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A TP set beyond the sanity threshold (500 pips) surfaces a warning
        from the exit-levels display."""
        fake = FakeFullClient()
        result = self._invoke(monkeypatch, fake, "600\n\ny\n\n\n")
        assert result.exit_code == 0, result.output
        assert "check the value" in result.output + result.stderr

    def test_declining_confirm_cancels_order(
        self, trade_db: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Answering 'n' (or Enter) at the final confirm prompt cancels the
        order without calling place_market_order."""
        fake = FakeFullClient()
        # skip TP, skip SL, decline confirm
        result = self._invoke(monkeypatch, fake, "\n\nn\n")
        assert result.exit_code == 0, result.output
        assert "Order cancelled" in result.output
        assert fake.order_placed is False

    def test_sl_failure_warns_unprotected(
        self, trade_db: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """SL attachment failure prints an 'unprotected' warning but does not crash."""
        fake = FakeFullClient(sl_should_fail=True)
        result = self._invoke(monkeypatch, fake, "50\n30\ny\n\n\n")
        assert result.exit_code == 0, result.output
        # TP still goes through
        assert fake.tp_attached is not None
        assert "Take-profit set" in result.output
        # SL warning is emitted
        assert "unprotected" in result.output + result.stderr

    def test_tp_failure_does_not_block_sl(
        self, trade_db: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """TP failure is a warning only; SL attachment still proceeds."""
        fake = FakeFullClient(tp_should_fail=True)
        result = self._invoke(monkeypatch, fake, "50\n30\ny\n\n\n")
        assert result.exit_code == 0, result.output
        assert fake.sl_attached is not None
        assert "Stop-loss set" in result.output

    def test_missing_trade_id_warns_gracefully(
        self, trade_db: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """If Oanda returns no trade_id, a warning is shown instead of a crash."""
        fake = FakeFullClient()

        # Override place_market_order to return fill with no trade_id.
        def _no_trade_id_fill(instrument: str, units_signed: int) -> OrderFill:
            fake.order_placed = True
            return OrderFill(
                transaction_id="99999",
                fill_price=Decimal("1.10005"),
                units_filled=units_signed,
                trade_id=None,
            )

        fake.place_market_order = _no_trade_id_fill  # type: ignore[method-assign]
        result = self._invoke(monkeypatch, fake, "50\n30\ny\n\n\n")
        assert result.exit_code == 0, result.output
        assert "trade ID" in result.output + result.stderr

    def test_trade_plan_saved_to_db(
        self, trade_db: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """TP and SL prices are persisted in trade_plans after a confirmed fill."""
        # Seed the fill transaction as if post-fill sync brought it in.
        conn = get_db(path=trade_db)
        conn.execute(
            "INSERT INTO transactions (oanda_id, account_id, type, time, raw_json) "
            "VALUES ('99999', 'acct-1', 'ORDER_FILL', '2026-04-29T12:00:00Z', '{}')"
        )
        conn.commit()
        conn.close()

        fake = FakeFullClient()
        result = self._invoke(monkeypatch, fake, "50\n30\ny\n\n\n")
        assert result.exit_code == 0, result.output

        conn = get_db(path=trade_db)
        plan = conn.execute(
            "SELECT tp_price, sl_price FROM trade_plans "
            "JOIN transactions ON trade_plans.transaction_id = transactions.id "
            "WHERE transactions.oanda_id = '99999'"
        ).fetchone()
        conn.close()
        assert plan is not None
        assert plan["tp_price"] is not None
        assert plan["sl_price"] is not None

    def test_no_trade_plan_when_tpsl_skipped(
        self, trade_db: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Skipping both TP and SL leaves no row in trade_plans."""
        fake = FakeFullClient()
        # skip TP, skip SL, confirm=y, note=skip, tags=skip
        result = self._invoke(monkeypatch, fake, "\n\ny\n\n\n")
        assert result.exit_code == 0, result.output

        conn = get_db(path=trade_db)
        count = conn.execute("SELECT COUNT(*) FROM trade_plans").fetchone()[0]
        conn.close()
        assert count == 0

    def test_note_and_tags_saved_when_fill_row_present(
        self, trade_db: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """When the fill is already in the local DB, a note and tags entered
        at the post-fill prompts are persisted against it."""
        conn = get_db(path=trade_db)
        conn.execute(
            "INSERT INTO transactions (oanda_id, account_id, type, time, raw_json) "
            "VALUES ('99999', 'acct-1', 'ORDER_FILL', '2026-04-29T12:00:00Z', '{}')"
        )
        conn.commit()
        conn.close()

        fake = FakeFullClient()
        # skip TP, skip SL, confirm=y, note="Entered on breakout", tags="breakout momentum"
        result = self._invoke(
            monkeypatch, fake, "\n\ny\nEntered on breakout\nbreakout momentum\n"
        )
        assert result.exit_code == 0, result.output
        assert "Note saved." in result.output
        assert "2 tags saved." in result.output

        conn = get_db(path=trade_db)
        txn_id = conn.execute(
            "SELECT id FROM transactions WHERE oanda_id = '99999'"
        ).fetchone()[0]
        note = conn.execute(
            "SELECT body FROM notes WHERE transaction_id = ?", (txn_id,)
        ).fetchone()
        tags = {
            r[0]
            for r in conn.execute(
                "SELECT tag FROM tags WHERE transaction_id = ?", (txn_id,)
            ).fetchall()
        }
        conn.close()
        assert note is not None
        assert note[0] == "Entered on breakout"
        assert tags == {"breakout", "momentum"}

    def test_note_and_tags_not_saved_when_fill_row_missing(
        self, trade_db: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """When post-fill sync hasn't brought the fill in yet, note/tags
        prompts warn instead of silently discarding the input."""
        fake = FakeFullClient()
        # skip TP, skip SL, confirm=y, note="lost note", tags="lost-tag"
        result = self._invoke(monkeypatch, fake, "\n\ny\nlost note\nlost-tag\n")
        assert result.exit_code == 0, result.output
        combined = result.output + result.stderr
        assert "Note not saved" in combined
        assert "Tags not saved" in combined


class TestTradeErrors:
    """Error-path and edge-case tests for the ``frmj trade`` command."""

    @pytest.fixture()
    def trade_db(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
        """Fully configured practice DB for normal trade command invocations."""
        path = tmp_path / "trade_err.db"
        monkeypatch.setenv("FRMJ_DB_PATH", str(path))
        monkeypatch.setenv("OANDA_API_TOKEN", "test-token-123")
        conn = get_db(path=path)
        add_account(conn, "practice", "acct-1", is_practice=True)
        set_active_account(conn, "practice")
        set_config(conn, "max_open_trades", "5")
        conn.close()
        return path

    @pytest.fixture()
    def live_trade_db(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
        """DB with a live account and max_open_trades; live mode NOT enabled."""
        path = tmp_path / "live_trade_err.db"
        monkeypatch.setenv("FRMJ_DB_PATH", str(path))
        monkeypatch.setenv("OANDA_API_TOKEN", "test-token-123")
        conn = get_db(path=path)
        add_account(conn, "my-live", "101-001-live-001", is_practice=False)
        set_active_account(conn, "my-live")
        set_config(conn, "max_open_trades", "5")
        conn.close()
        return path

    def test_resume_with_instrument_exits_1(self, trade_db: Path) -> None:
        """--resume rejects a positional instrument/direction argument."""
        result = runner.invoke(app, ["trade", "EUR_USD", "long", "--resume"])
        assert result.exit_code == 1
        assert "not used with --resume" in result.output + result.stderr

    def test_resume_with_multi_exits_1(self, trade_db: Path) -> None:
        """--resume and --multi are mutually exclusive."""
        result = runner.invoke(app, ["trade", "--resume", "--multi", "some-group"])
        assert result.exit_code == 1
        assert "--multi is not supported with --resume" in result.output + result.stderr

    def test_missing_instrument_and_direction_exits_1(self, trade_db: Path) -> None:
        """Without --resume, instrument and direction are required."""
        result = runner.invoke(app, ["trade"])
        assert result.exit_code == 1
        assert "instrument and direction are required" in result.output + result.stderr

    def test_invalid_direction_exits_1(self, trade_db: Path) -> None:
        """A direction other than long/short is rejected."""
        result = runner.invoke(app, ["trade", "EUR_USD", "sideways"])
        assert result.exit_code == 1
        assert "must be 'long' or 'short'" in result.output + result.stderr

    def test_get_client_failure_exits_1(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """When get_client raises (no active account), trade exits 1."""
        path = tmp_path / "no_account.db"
        monkeypatch.setenv("FRMJ_DB_PATH", str(path))
        monkeypatch.delenv("OANDA_API_TOKEN", raising=False)
        result = runner.invoke(app, ["trade", "EUR_USD", "long"])
        assert result.exit_code == 1
        assert "Error" in result.output + result.stderr

    def test_get_risk_config_failure_exits_1(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """When get_risk_config raises (no max_open_trades), trade exits 1."""
        path = tmp_path / "no_risk.db"
        monkeypatch.setenv("FRMJ_DB_PATH", str(path))
        monkeypatch.setenv("OANDA_API_TOKEN", "test-token-123")
        conn = get_db(path=path)
        add_account(conn, "practice", "acct-1", is_practice=True)
        set_active_account(conn, "practice")
        conn.close()
        monkeypatch.setattr(
            "frmj.cli.trade.get_client",
            lambda conn, account_name=None: FakeFullClient(),
        )
        result = runner.invoke(app, ["trade", "EUR_USD", "long"])
        assert result.exit_code == 1
        assert "Error" in result.output + result.stderr

    def test_market_data_failure_exits_1(
        self, trade_db: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """When fetching market data raises, trade exits 1."""

        class FailingClient(FakeFullClient):
            """Raises on the first call that fetches live account data."""

            def get_account_summary(self) -> None:  # type: ignore[override]
                raise RuntimeError("network down")

        monkeypatch.setattr(
            "frmj.cli.trade.get_client", lambda conn, account_name=None: FailingClient()
        )
        result = runner.invoke(app, ["trade", "EUR_USD", "long"])
        assert result.exit_code == 1
        assert "Error fetching market data" in result.output + result.stderr

    def test_max_trades_exceeded_exits_1(
        self, trade_db: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """MaxTradesExceeded from evaluate_trade exits 1."""
        from frmj.domain.risk import MaxTradesExceeded

        monkeypatch.setattr(
            "frmj.cli.trade.get_client",
            lambda conn, account_name=None: FakeFullClient(),
        )
        monkeypatch.setattr(
            "frmj.services.evaluate_trade",
            lambda **kw: (_ for _ in ()).throw(
                MaxTradesExceeded("too many open trades")
            ),
        )
        result = runner.invoke(app, ["trade", "EUR_USD", "long"])
        assert result.exit_code == 1
        assert "Cannot trade" in result.output + result.stderr

    def test_scale_in_forbidden_exits_1(
        self, trade_db: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """ScaleInForbidden from evaluate_trade exits 1."""
        from frmj.domain.risk import ScaleInForbidden

        monkeypatch.setattr(
            "frmj.cli.trade.get_client",
            lambda conn, account_name=None: FakeFullClient(),
        )
        monkeypatch.setattr(
            "frmj.services.evaluate_trade",
            lambda **kw: (_ for _ in ()).throw(
                ScaleInForbidden("scale-in not allowed")
            ),
        )
        result = runner.invoke(app, ["trade", "EUR_USD", "long"])
        assert result.exit_code == 1
        assert "Cannot trade" in result.output + result.stderr

    def test_correlated_position_warns_by_default(
        self, trade_db: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """WARNING_ONLY (default): a correlated open position emits a warning
        and requires acknowledgement, but does not block the trade once
        acknowledged."""
        fake = FakeFullClient(
            open_trades=[_open_trade(instrument="GBP_USD", direction="LONG")]
        )
        monkeypatch.setattr(
            "frmj.cli.trade.get_client", lambda conn, account_name=None: fake
        )
        # "y" acknowledges the "Proceed anyway?" prompt; then TP/SL are skipped.
        result = runner.invoke(
            app, ["trade", "EUR_USD", "long", "--dry-run"], input="y\n\n\n"
        )
        assert result.exit_code == 0, result.output
        assert "shares USD exposure" in result.output + result.stderr
        assert "Proceed anyway?" in result.output + result.stderr

    def test_correlated_position_decline_cancels(
        self, trade_db: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """WARNING_ONLY: declining the acknowledgement prompt cancels the
        trade before any TP/SL prompts or order placement."""
        fake = FakeFullClient(
            open_trades=[_open_trade(instrument="GBP_USD", direction="LONG")]
        )
        monkeypatch.setattr(
            "frmj.cli.trade.get_client", lambda conn, account_name=None: fake
        )
        result = runner.invoke(app, ["trade", "EUR_USD", "long"], input="n\n")
        assert result.exit_code == 0, result.output
        assert "Order cancelled" in result.output + result.stderr

    def test_correlated_position_hard_block_exits_1(
        self, trade_db: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """HARD_BLOCK: a correlated open position refuses the trade outright."""
        conn = get_db(path=trade_db)
        set_config(conn, "correlation_blocking_mode", "hard_block")
        conn.close()

        fake = FakeFullClient(
            open_trades=[_open_trade(instrument="GBP_USD", direction="LONG")]
        )
        monkeypatch.setattr(
            "frmj.cli.trade.get_client", lambda conn, account_name=None: fake
        )
        result = runner.invoke(app, ["trade", "EUR_USD", "long"])
        assert result.exit_code == 1
        assert "Cannot trade" in result.output + result.stderr

    def test_uncorrelated_position_no_warning(
        self, trade_db: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """No shared-currency exposure means no correlation warning."""
        fake = FakeFullClient(
            open_trades=[_open_trade(instrument="USD_JPY", direction="LONG")]
        )
        monkeypatch.setattr(
            "frmj.cli.trade.get_client", lambda conn, account_name=None: fake
        )
        # EUR_USD long is net-short USD; USD_JPY long is net-long USD — opposite
        # bets on USD, so no conflict is flagged.
        result = runner.invoke(
            app, ["trade", "EUR_USD", "long", "--dry-run"], input="\n\n"
        )
        assert result.exit_code == 0, result.output
        assert "shares" not in result.output + result.stderr

    def test_sizing_warnings_shown(
        self, trade_db: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Non-fatal warnings from evaluate_trade are echoed before the trade plan."""
        from frmj.domain.risk import RiskStrategy, SizingDecision

        decision = SizingDecision(
            capital_to_deploy=Decimal("500"),
            strategy_used=RiskStrategy.REMAINING_MARGIN_FRACTION,
            size_fraction=None,
            warnings=("near max open trades",),
        )
        monkeypatch.setattr(
            "frmj.cli.trade.get_client",
            lambda conn, account_name=None: FakeFullClient(),
        )
        monkeypatch.setattr("frmj.services.evaluate_trade", lambda **kw: decision)
        # Just show the plan (dry-run avoids needing confirmation input).
        result = runner.invoke(
            app, ["trade", "EUR_USD", "long", "--dry-run"], input="\n\n"
        )
        assert result.exit_code == 0, result.output
        assert "Warning" in result.output + result.stderr
        assert "near max open trades" in result.output + result.stderr

    def test_units_compute_failure_exits_1(
        self, trade_db: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """When compute_units raises, trade exits 1."""
        monkeypatch.setattr(
            "frmj.cli.trade.get_client",
            lambda conn, account_name=None: FakeFullClient(),
        )
        monkeypatch.setattr(
            "frmj.services.compute_units",
            lambda **kw: (_ for _ in ()).throw(RuntimeError("sizing error")),
        )
        result = runner.invoke(app, ["trade", "EUR_USD", "long"])
        assert result.exit_code == 1
        assert "Error computing units" in result.output + result.stderr

    def test_edit_confirmation_loop(
        self, trade_db: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Pressing 'e' at the confirm prompt re-prompts for TP/SL and
        re-displays exit levels before asking for confirmation again."""
        monkeypatch.setattr(
            "frmj.cli.trade.get_client",
            lambda conn, account_name=None: FakeFullClient(),
        )
        # Sequence: TP=50, SL=30, answer=e (edit), new TP=20, new SL=15,
        # answer=y (confirm order), note=skip, tags=skip.
        result = runner.invoke(
            app,
            ["trade", "EUR_USD", "long"],
            input="50\n30\ne\n20\n15\ny\n\n\n",
        )
        assert result.exit_code == 0, result.output
        assert "Order filled" in result.output

    def test_live_account_practice_mode_gate_exits_1(
        self, live_trade_db: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A live account without live mode enabled is blocked after confirming."""
        monkeypatch.setattr(
            "frmj.cli.trade.get_client",
            lambda conn, account_name=None: FakeFullClient(),
        )
        # TP=50, SL=30, confirm=y → live mode gate fires before place_market_order.
        result = runner.invoke(
            app,
            ["trade", "EUR_USD", "long"],
            input="50\n30\ny\n",
        )
        assert result.exit_code == 1
        assert "live trading mode is not enabled" in result.output + result.stderr

    def test_post_fill_sync_failure_warns_but_exits_0(
        self, trade_db: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A sync error after a successful fill emits a warning but exits 0."""
        monkeypatch.setattr(
            "frmj.cli.trade.get_client",
            lambda conn, account_name=None: FakeFullClient(),
        )

        def _counting_sync(conn: object, client: object) -> object:
            """The only sync call is the post-fill sync; always raise."""
            raise RuntimeError("sync exploded after fill")

        monkeypatch.setattr("frmj.services.sync_incremental", _counting_sync)
        # TP=50, SL=30, confirm=y, note=skip, tags=skip.
        result = runner.invoke(
            app,
            ["trade", "EUR_USD", "long"],
            input="50\n30\ny\n\n\n",
        )
        assert result.exit_code == 0, result.output
        assert "post-fill sync failed" in result.output + result.stderr

    def test_note_skipped_when_fill_not_in_db(
        self, trade_db: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """When the fill transaction is absent from the local DB and the user
        types a note, a 'not yet in local DB' warning is shown instead of
        inserting the note."""
        monkeypatch.setattr(
            "frmj.cli.trade.get_client",
            lambda conn, account_name=None: FakeFullClient(),
        )
        # TP=skip, SL=skip, confirm=y, note="my note" (non-empty), tags=skip.
        # FakeFullClient returns transaction_id="99999" which is never pre-inserted.
        result = runner.invoke(
            app,
            ["trade", "EUR_USD", "long"],
            input="\n\ny\nmy note\n\n",
        )
        assert result.exit_code == 0, result.output
        assert "not yet in local DB" in result.output + result.stderr


class TestTradePendingOrders:
    """Pending entry orders are risk-checked as if they had already filled."""

    @pytest.fixture()
    def trade_db(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
        """Practice DB with max_open_trades=5 (default hard_block mode)."""
        path = tmp_path / "trade_pending.db"
        monkeypatch.setenv("FRMJ_DB_PATH", str(path))
        monkeypatch.setenv("OANDA_API_TOKEN", "test-token-123")
        conn = get_db(path=path)
        add_account(conn, "practice", "acct-1", is_practice=True)
        set_active_account(conn, "practice")
        set_config(conn, "max_open_trades", "5")
        conn.close()
        return path

    def test_pending_order_counts_toward_n_and_reduces_margin(
        self, trade_db: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """One pending order moves N from 2 to 3 and deducts its margin.

        FakeFullClient: 8000 margin available, 2 open trades; pending order
        margin = 10,000 units * 0.02 * 1.10 = 220. Sizing is therefore
        (8000 - 220) * 1/(5+1-3) = 2593.33.
        """
        fake = FakeFullClient(
            pending_orders=[_pending_order(instrument="USD_JPY", direction="LONG")]
        )
        monkeypatch.setattr(
            "frmj.cli.trade.get_client", lambda conn, account_name=None: fake
        )
        result = runner.invoke(
            app, ["trade", "EUR_USD", "long", "--dry-run"], input="\n\n"
        )
        assert result.exit_code == 0, result.output
        assert "2 / 5 (+1 pending)" in result.output
        assert "Size fraction:   1/3" in result.output
        assert "Capital at risk: $2,593.33" in result.output

    def test_no_pending_orders_shows_no_pending_note(
        self, trade_db: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Without pending orders the plan is unchanged: 1/4 of 8000."""
        fake = FakeFullClient()
        monkeypatch.setattr(
            "frmj.cli.trade.get_client", lambda conn, account_name=None: fake
        )
        result = runner.invoke(
            app, ["trade", "EUR_USD", "long", "--dry-run"], input="\n\n"
        )
        assert result.exit_code == 0, result.output
        assert "pending" not in result.output
        assert "Capital at risk: $2,000.00" in result.output

    def test_pending_orders_hit_max_trades_cap(
        self, trade_db: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """2 open + 3 pending reaches max_open_trades=5, so HARD_BLOCK refuses."""
        fake = FakeFullClient(
            pending_orders=[
                _pending_order(order_id=str(i), instrument="USD_JPY") for i in range(3)
            ]
        )
        monkeypatch.setattr(
            "frmj.cli.trade.get_client", lambda conn, account_name=None: fake
        )
        result = runner.invoke(app, ["trade", "EUR_USD", "long"])
        assert result.exit_code == 1
        assert "Cannot trade" in result.output + result.stderr

    def test_pending_order_on_same_instrument_blocks_scale_in(
        self, trade_db: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Default scale_in=never refuses a trade on an instrument with a
        pending order, naming the pending order in the error."""
        fake = FakeFullClient(pending_orders=[_pending_order(instrument="EUR_USD")])
        monkeypatch.setattr(
            "frmj.cli.trade.get_client", lambda conn, account_name=None: fake
        )
        result = runner.invoke(app, ["trade", "EUR_USD", "long"])
        assert result.exit_code == 1
        assert "1 pending order(s)" in result.output + result.stderr

    def test_correlated_pending_order_warns(
        self, trade_db: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A pending GBP_USD long shares USD exposure with a new EUR_USD long."""
        fake = FakeFullClient(
            pending_orders=[_pending_order(instrument="GBP_USD", direction="LONG")]
        )
        monkeypatch.setattr(
            "frmj.cli.trade.get_client", lambda conn, account_name=None: fake
        )
        result = runner.invoke(app, ["trade", "EUR_USD", "long"], input="n\n")
        assert result.exit_code == 0, result.output
        assert (
            "shares USD exposure with pending GBP_USD LONG"
            in result.output + result.stderr
        )
        assert "Order cancelled" in result.output + result.stderr

    def test_pending_order_fetch_failure_exits_1(
        self, trade_db: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A failed pendingOrders fetch aborts rather than sizing without it."""

        def _fail() -> list:
            raise RuntimeError("Oanda unreachable")

        fake = FakeFullClient()
        monkeypatch.setattr(fake, "get_pending_orders", _fail)
        monkeypatch.setattr(
            "frmj.cli.trade.get_client", lambda conn, account_name=None: fake
        )
        result = runner.invoke(app, ["trade", "EUR_USD", "long"])
        assert result.exit_code == 1
        assert "Error fetching market data" in result.output + result.stderr


class TestTradeLimit:
    """``trade --limit`` places a GTC limit entry order.

    FakeFullClient quotes EUR_USD at bid 1.09990 / ask 1.10010, so a long
    limit 15 pips better than the market is 1.09860.
    """

    @pytest.fixture()
    def trade_db(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
        path = tmp_path / "trade_limit_test.db"
        monkeypatch.setenv("FRMJ_DB_PATH", str(path))
        monkeypatch.setenv("OANDA_API_TOKEN", "test-token-123")
        conn = get_db(path=path)
        set_config(conn, "account_id", "acct-1")
        set_config(conn, "max_open_trades", "5")
        conn.close()
        return path

    @pytest.fixture()
    def plan_file(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
        """Redirect draft plan writes to a temp path."""
        path = tmp_path / "saved_plan.json"
        monkeypatch.setattr("frmj.app._DRAFT_PLAN_PATH", path)
        return path

    def _invoke(
        self,
        monkeypatch: pytest.MonkeyPatch,
        fake: FakeFullClient,
        inputs: str,
        *extra: str,
        direction: str = "long",
    ) -> Result:
        monkeypatch.setattr(
            "frmj.cli.trade.get_client", lambda conn, account_name=None: fake
        )
        return runner.invoke(
            app, ["trade", "EUR_USD", direction, "--limit", *extra], input=inputs
        )

    def _seed_txn(self, db: Path, oanda_id: str, txn_type: str) -> None:
        """Insert a transaction as if the post-order sync had brought it in."""
        conn = get_db(path=db)
        conn.execute(
            "INSERT INTO transactions (oanda_id, account_id, type, time, raw_json) "
            "VALUES (?, 'acct-1', ?, '2026-04-29T12:00:00Z', '{}')",
            (oanda_id, txn_type),
        )
        conn.commit()
        conn.close()

    def test_dry_run_plans_at_limit_price(
        self, trade_db: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Entry, TP, and SL are all computed from the limit price."""
        fake = FakeFullClient()
        # limit=15 pips, TP=50 pips, SL=30 pips
        result = self._invoke(monkeypatch, fake, "15\n50\n30\n", "--dry-run")
        assert result.exit_code == 0, result.output
        assert "Market: bid 1.09990 / ask 1.10010" in result.output
        assert "pips from ask" in result.output
        assert "Entry:   1.09860 (long limit, GTC — 15.0 pips from ask)" in (
            result.output
        )
        assert "TP: 1.10360" in result.output
        assert "SL: 1.09560" in result.output
        assert fake.limit_orders == []

    def test_short_offset_measured_from_bid(
        self, trade_db: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        fake = FakeFullClient()
        result = self._invoke(
            monkeypatch, fake, "15\n\n\n", "--dry-run", direction="short"
        )
        assert result.exit_code == 0, result.output
        assert "Entry:   1.10140 (short limit, GTC — 15.0 pips from bid)" in (
            result.output
        )

    @pytest.mark.parametrize(
        "entry, expected",
        [("@1.0950", "1.09500"), ("1%", "1.08910"), ("15p", "1.09860")],
    )
    def test_entry_formats(
        self,
        trade_db: Path,
        monkeypatch: pytest.MonkeyPatch,
        entry: str,
        expected: str,
    ) -> None:
        """@price is absolute; % is a percent of price (1.10010 * 0.99)."""
        fake = FakeFullClient()
        result = self._invoke(monkeypatch, fake, f"{entry}\n\n\n", "--dry-run")
        assert result.exit_code == 0, result.output
        assert f"Entry:   {expected} (long limit" in result.output

    def test_limit_that_would_fill_immediately_reprompts(
        self, trade_db: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        fake = FakeFullClient()
        # @1.2 is above the ask for a long; "abc" isn't a number; then 15 pips.
        result = self._invoke(monkeypatch, fake, "@1.2\nabc\n15\n\n\n", "--dry-run")
        assert result.exit_code == 0, result.output
        assert "would fill immediately" in result.output
        assert "is not a number" in result.output
        assert "Entry:   1.09860" in result.output

    def test_places_limit_order_with_tpsl_on_fill(
        self, trade_db: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """TP/SL go in the order; nothing is attached and no market order sent."""
        fake = FakeFullClient()
        # limit, TP, SL, confirm=y, note=skip, tags=skip
        result = self._invoke(monkeypatch, fake, "15\n50\n30\ny\n\n\n")
        assert result.exit_code == 0, result.output
        assert len(fake.limit_orders) == 1
        order = fake.limit_orders[0]
        assert order["instrument"] == "EUR_USD"
        assert order["units_signed"] > 0
        assert order["price"] == Decimal("1.09860")
        assert order["take_profit_price"] == Decimal("1.10360")
        assert order["stop_loss_price"] == Decimal("1.09560")
        assert fake.order_placed is False
        assert fake.tp_attached is None
        assert fake.sl_attached is None
        assert "Limit order #88888 placed at 1.09860 (GTC)" in result.output
        assert "Take-profit 1.10360 will be set when it fills" in result.output
        assert "Stop-loss 1.09560 will be set when it fills" in result.output

    def test_short_limit_sends_negative_units(
        self, trade_db: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        fake = FakeFullClient()
        result = self._invoke(monkeypatch, fake, "15\n\n\ny\n\n\n", direction="short")
        assert result.exit_code == 0, result.output
        assert fake.limit_orders[0]["units_signed"] < 0

    def test_immediate_fill_reported_without_attaching(
        self, trade_db: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        fake = FakeFullClient(limit_fills_immediately=True)
        result = self._invoke(monkeypatch, fake, "15\n50\n30\ny\n\n\n")
        assert result.exit_code == 0, result.output
        assert "Limit order filled immediately at 1.09860" in result.output
        assert "Take-profit 1.10360 set" in result.output
        assert fake.tp_attached is None
        assert fake.sl_attached is None

    def test_note_tags_and_plan_attach_to_limit_order_txn(
        self, trade_db: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """An unfilled order's journal entries key on its LIMIT_ORDER txn."""
        self._seed_txn(trade_db, "88888", "LIMIT_ORDER")
        fake = FakeFullClient()
        result = self._invoke(
            monkeypatch, fake, "15\n50\n30\ny\nWaiting for pullback\npullback\n"
        )
        assert result.exit_code == 0, result.output
        assert "Note saved." in result.output
        assert "1 tag saved." in result.output

        conn = get_db(path=trade_db)
        txn_id = conn.execute(
            "SELECT id FROM transactions WHERE oanda_id = '88888'"
        ).fetchone()[0]
        note = conn.execute(
            "SELECT body FROM notes WHERE transaction_id = ?", (txn_id,)
        ).fetchone()
        tag = conn.execute(
            "SELECT tag FROM tags WHERE transaction_id = ?", (txn_id,)
        ).fetchone()
        plan = conn.execute(
            "SELECT tp_price, sl_price FROM trade_plans WHERE transaction_id = ?",
            (txn_id,),
        ).fetchone()
        conn.close()
        assert note[0] == "Waiting for pullback"
        assert tag[0] == "pullback"
        assert (plan["tp_price"], plan["sl_price"]) == ("1.10360", "1.09560")

    def test_immediate_fill_journals_on_fill_txn(
        self, trade_db: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """An order that filled on arrival keys its journal on the ORDER_FILL."""
        self._seed_txn(trade_db, "88888", "LIMIT_ORDER")
        self._seed_txn(trade_db, "88889", "ORDER_FILL")
        fake = FakeFullClient(limit_fills_immediately=True)
        result = self._invoke(monkeypatch, fake, "15\n50\n30\ny\nFilled fast\n\n")
        assert result.exit_code == 0, result.output

        conn = get_db(path=trade_db)
        row = conn.execute(
            "SELECT t.oanda_id FROM notes n "
            "JOIN transactions t ON n.transaction_id = t.id"
        ).fetchone()
        plan_row = conn.execute(
            "SELECT t.oanda_id FROM trade_plans p "
            "JOIN transactions t ON p.transaction_id = t.id"
        ).fetchone()
        conn.close()
        assert row[0] == "88889"
        assert plan_row[0] == "88889"

    def test_note_not_saved_when_order_txn_missing(
        self, trade_db: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        fake = FakeFullClient()
        result = self._invoke(monkeypatch, fake, "15\n\n\ny\nsome note\n\n")
        assert result.exit_code == 0, result.output
        assert "not yet in local DB" in result.output + result.stderr

    def test_post_order_sync_failure_warns(
        self, trade_db: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        fake = FakeFullClient(sync_should_fail=True)
        result = self._invoke(monkeypatch, fake, "15\n\n\ny\n\n\n")
        assert result.exit_code == 0, result.output
        assert "post-order sync failed" in result.output + result.stderr

    def test_failed_order_saves_limit_price_in_draft(
        self, trade_db: Path, plan_file: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        fake = FakeFullClient(limit_fail_count=1)
        # limit, TP, SL, confirm=y, retry prompt=s
        result = self._invoke(monkeypatch, fake, "15\n50\n30\ny\ns\n")
        assert result.exit_code == 0, result.output
        plan = json.loads(plan_file.read_text())
        assert plan["limit_price"] == "1.09860"
        assert plan["tp_price"] == "1.10360"

    def test_retry_places_limit_order(
        self, trade_db: Path, plan_file: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        fake = FakeFullClient(limit_fail_count=1)
        result = self._invoke(monkeypatch, fake, "15\n\n\ny\nr\n\n\n")
        assert result.exit_code == 0, result.output
        assert len(fake.limit_orders) == 1

    def test_resume_places_saved_limit_order(
        self, trade_db: Path, plan_file: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        plan_file.write_text(
            json.dumps(
                {
                    "instrument": "EUR_USD",
                    "direction": "long",
                    "units_signed": 1000,
                    "tp_price": "1.10360",
                    "sl_price": None,
                    "limit_price": "1.09860",
                    "account": None,
                }
            )
        )
        fake = FakeFullClient()
        monkeypatch.setattr(
            "frmj.cli.trade.get_client", lambda conn, account_name=None: fake
        )
        result = runner.invoke(app, ["trade", "--resume"], input="y\n\n\n")
        assert result.exit_code == 0, result.output
        assert "Limit price: 1.09860 (GTC)" in result.output
        assert fake.limit_orders[0]["price"] == Decimal("1.09860")
        assert fake.limit_orders[0]["take_profit_price"] == Decimal("1.10360")
        assert fake.order_placed is False

    def test_limit_with_resume_exits_1(self, trade_db: Path) -> None:
        result = runner.invoke(app, ["trade", "--resume", "--limit"])
        assert result.exit_code == 1
        assert "--limit is not used with --resume" in result.output + result.stderr

    def test_limit_with_multi_exits_1(self, trade_db: Path) -> None:
        result = runner.invoke(
            app, ["trade", "EUR_USD", "long", "--limit", "--multi", "grp"]
        )
        assert result.exit_code == 1
        assert "--limit is not supported with --multi" in (
            result.output + result.stderr
        )


class TestTradeTrail:
    """``trade --trail`` adds a trailing stop-loss to the plan and the order.

    FakeFullClient quotes EUR_USD at bid 1.09990 / ask 1.10010 (2-pip
    spread), so a 20-pip trail on a long starts at 1.09790: 20 pips below
    the bid, 22 pips below the ask it entered at.
    """

    @pytest.fixture()
    def trade_db(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
        path = tmp_path / "trade_trail_test.db"
        monkeypatch.setenv("FRMJ_DB_PATH", str(path))
        monkeypatch.setenv("OANDA_API_TOKEN", "test-token-123")
        conn = get_db(path=path)
        set_config(conn, "account_id", "acct-1")
        set_config(conn, "max_open_trades", "5")
        conn.close()
        return path

    def _invoke(
        self,
        monkeypatch: pytest.MonkeyPatch,
        fake: FakeFullClient,
        inputs: str,
        *extra: str,
    ) -> Result:
        monkeypatch.setattr(
            "frmj.cli.trade.get_client", lambda conn, account_name=None: fake
        )
        return runner.invoke(
            app, ["trade", "EUR_USD", "long", "--trail", *extra], input=inputs
        )

    def test_dry_run_shows_trail_row_including_spread(
        self, trade_db: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        fake = FakeFullClient()
        # TP skip, SL skip, trail=20
        result = self._invoke(monkeypatch, fake, "\n\n20\n", "--dry-run")
        assert result.exit_code == 0, result.output
        assert "Trail: 20.0p" in result.output
        assert "starts at 1.09790" in result.output
        assert "incl. spread" in result.output
        assert fake.order_placed is False

    def test_rr_uses_tighter_of_sl_and_trail(
        self, trade_db: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """TP 50 pips vs. a 100-pip SL and a 20-pip trail (22 pips with the
        spread): the trail is tighter, so R:R = 50 / 22."""
        fake = FakeFullClient()
        result = self._invoke(monkeypatch, fake, "50\n100\n20\n", "--dry-run")
        assert result.exit_code == 0, result.output
        assert "R:R  2.27" in result.output

    def test_trail_attached_after_market_fill(
        self, trade_db: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        fake = FakeFullClient()
        # TP 50, SL 30, trail 20, confirm, note skip, tags skip
        result = self._invoke(monkeypatch, fake, "50\n30\n20\ny\n\n\n")
        assert result.exit_code == 0, result.output
        assert fake.trail_attached == "0.00200"
        assert fake.sl_attached is not None
        assert "Trailing stop set at 20.0 pips" in result.output

    def test_skipping_trail_attaches_none(
        self, trade_db: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        fake = FakeFullClient()
        result = self._invoke(monkeypatch, fake, "\n30\n\ny\n\n\n")
        assert result.exit_code == 0, result.output
        assert fake.trail_attached is None
        assert "Trailing stop set" not in result.output

    def test_no_trail_prompt_without_flag(
        self, trade_db: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        fake = FakeFullClient()
        monkeypatch.setattr(
            "frmj.cli.trade.get_client", lambda conn, account_name=None: fake
        )
        result = runner.invoke(
            app, ["trade", "EUR_USD", "long", "--dry-run"], input="\n\n"
        )
        assert result.exit_code == 0, result.output
        assert "Trailing stop" not in result.output

    def test_invalid_trail_reprompts(
        self, trade_db: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        fake = FakeFullClient()
        # "abc" and "-5" are rejected before 20 is accepted.
        result = self._invoke(monkeypatch, fake, "\n\nabc\n-5\n20\ny\n\n\n")
        assert result.exit_code == 0, result.output
        assert result.output.count("Invalid input") == 2
        assert fake.trail_attached == "0.00200"

    def test_trail_below_instrument_minimum_reprompts(
        self, trade_db: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        fake = FakeFullClient()
        spec = InstrumentSpec(
            name="EUR_USD",
            pip_location=-4,
            margin_rate=Decimal("0.02"),
            min_units=1,
            units_increment=1,
            display_precision=5,
            min_trailing_stop_distance=Decimal("0.00050"),
        )
        monkeypatch.setattr(fake, "get_instrument", lambda name: spec)
        result = self._invoke(monkeypatch, fake, "\n\n3\n5\ny\n\n\n")
        assert result.exit_code == 0, result.output
        assert "at least 5.0 pips" in result.output
        assert fake.trail_attached == "0.00050"

    def test_edit_reprompts_trail(
        self, trade_db: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        fake = FakeFullClient()
        # First pass trail 20; edit to TP 50, SL 30, trail 25; confirm.
        inputs = "50\n30\n20\ne\n50\n30\n25\ny\n\n\n"
        result = self._invoke(monkeypatch, fake, inputs)
        assert result.exit_code == 0, result.output
        assert "Trailing stop (new)" in result.output
        assert fake.trail_attached == "0.00250"

    def test_trail_failure_without_sl_warns_unprotected(
        self, trade_db: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        fake = FakeFullClient(trail_should_fail=True)
        result = self._invoke(monkeypatch, fake, "\n\n20\ny\n\n\n")
        assert result.exit_code == 0, result.output
        output = result.output + result.stderr
        assert "failed to attach trailing stop" in output
        assert "unprotected" in output

    def test_trail_failure_with_sl_set_is_not_unprotected(
        self, trade_db: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        fake = FakeFullClient(trail_should_fail=True)
        result = self._invoke(monkeypatch, fake, "\n30\n20\ny\n\n\n")
        assert result.exit_code == 0, result.output
        output = result.output + result.stderr
        assert "failed to attach trailing stop" in output
        assert "unprotected" not in output

    def test_limit_order_carries_trail_on_fill(
        self, trade_db: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        fake = FakeFullClient()
        # limit 15 pips, TP skip, SL skip, trail 20, confirm, note/tags skip
        result = self._invoke(monkeypatch, fake, "15\n\n\n20\ny\n\n\n", "--limit")
        assert result.exit_code == 0, result.output
        assert fake.limit_orders[0]["trailing_stop_distance"] == Decimal("0.00200")
        # Nothing is attached after placement; Oanda sets it on fill.
        assert fake.trail_attached is None
        assert "Trailing stop 20.0 pips will be set when it fills" in result.output

    def _seed_txn(self, db: Path, oanda_id: str, txn_type: str) -> None:
        """Insert a transaction as if the post-order sync had brought it in."""
        conn = get_db(path=db)
        conn.execute(
            "INSERT INTO transactions (oanda_id, account_id, type, time, raw_json) "
            "VALUES (?, 'acct-1', ?, '2026-04-29T12:00:00Z', '{}')",
            (oanda_id, txn_type),
        )
        conn.commit()
        conn.close()

    def _plan_trail(self, db: Path, oanda_id: str) -> str | None:
        conn = get_db(path=db)
        row = conn.execute(
            "SELECT trail_pips FROM trade_plans "
            "JOIN transactions ON trade_plans.transaction_id = transactions.id "
            "WHERE transactions.oanda_id = ?",
            (oanda_id,),
        ).fetchone()
        conn.close()
        return row["trail_pips"] if row else None

    def test_trail_saved_in_trade_plan_after_fill(
        self, trade_db: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A trail-only plan (no TP/SL) still gets a trade_plans row."""
        self._seed_txn(trade_db, "99999", "ORDER_FILL")
        fake = FakeFullClient()
        result = self._invoke(monkeypatch, fake, "\n\n20\ny\n\n\n")
        assert result.exit_code == 0, result.output
        assert self._plan_trail(trade_db, "99999") == "20.0"

    def test_trail_saved_in_limit_order_plan(
        self, trade_db: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        self._seed_txn(trade_db, "88888", "LIMIT_ORDER")
        fake = FakeFullClient()
        result = self._invoke(monkeypatch, fake, "15\n\n\n20\ny\n\n\n", "--limit")
        assert result.exit_code == 0, result.output
        assert self._plan_trail(trade_db, "88888") == "20.0"

    @pytest.fixture()
    def plan_file(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
        """Redirect draft plan writes to a temp path."""
        path = tmp_path / "saved_plan.json"
        monkeypatch.setattr("frmj.app._DRAFT_PLAN_PATH", path)
        return path

    def test_saved_draft_keeps_trail(
        self, trade_db: Path, plan_file: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        fake = FakeFullClient()
        monkeypatch.setattr(fake, "place_market_order", _failing_market_order)
        # TP/SL skip, trail 20, confirm, order fails, save.
        result = self._invoke(monkeypatch, fake, "\n\n20\ny\ns\n")
        assert result.exit_code == 0, result.output
        plan = json.loads(plan_file.read_text())
        assert plan["trail_distance"] == "0.00200"
        assert plan["trail_pips"] == "20.0"

    def test_resume_attaches_saved_trail(
        self, trade_db: Path, plan_file: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        plan_file.write_text(
            json.dumps(
                {
                    "instrument": "EUR_USD",
                    "direction": "long",
                    "units_signed": 10000,
                    "tp_price": None,
                    "sl_price": None,
                    "trail_distance": "0.00200",
                    "trail_pips": "20.0",
                }
            )
        )
        fake = FakeFullClient()
        monkeypatch.setattr(
            "frmj.cli.trade.get_client", lambda conn, account_name=None: fake
        )
        result = runner.invoke(app, ["trade", "--resume"], input="y\n\n\n")
        assert result.exit_code == 0, result.output
        assert "Trailing stop: 20.0 pips" in result.output
        assert fake.trail_attached == "0.00200"

    def test_trail_rejected_with_resume(
        self, trade_db: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        result = runner.invoke(app, ["trade", "--resume", "--trail"])
        assert result.exit_code == 1
        assert "--trail is not used with --resume" in result.output + result.stderr


class TestTradeFailureAndRetry:
    """Tests for the retry loop triggered when place_market_order raises."""

    @pytest.fixture()
    def trade_db(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
        path = tmp_path / "trade_fail_test.db"
        monkeypatch.setenv("FRMJ_DB_PATH", str(path))
        monkeypatch.setenv("OANDA_API_TOKEN", "test-token-123")
        conn = get_db(path=path)
        set_config(conn, "account_id", "acct-1")
        set_config(conn, "max_open_trades", "5")
        conn.close()
        return path

    @pytest.fixture()
    def plan_file(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
        """Redirect draft plan writes to a temp path."""
        path = tmp_path / "saved_plan.json"
        monkeypatch.setattr("frmj.app._DRAFT_PLAN_PATH", path)
        return path

    def _invoke_with_failure(
        self,
        monkeypatch: pytest.MonkeyPatch,
        action_input: str,
        *,
        fail_count: int = 1,
        use_timeout: bool = False,
    ) -> Result:
        """Invoke ``frmj trade EUR_USD long`` where place_market_order fails
        *fail_count* times before succeeding.  *action_input* is the retry
        prompt response (r/s/a).
        """
        fake = FakeFullClient()
        calls: list[int] = []

        def _flaky_order(instrument: str, units_signed: int) -> "OrderFill":
            calls.append(1)
            if len(calls) <= fail_count:
                if use_timeout:
                    raise httpx.TimeoutException("timed out")
                raise RuntimeError("Network error")
            fake.order_placed = True
            return OrderFill(
                transaction_id="99999",
                fill_price=Decimal("1.10005"),
                units_filled=units_signed,
                trade_id="99999",
            )

        fake.place_market_order = _flaky_order  # type: ignore[method-assign]
        monkeypatch.setattr(
            "frmj.cli.trade.get_client", lambda conn, account_name=None: fake
        )

        # TP=50p, SL=30p, confirm=y, retry prompt=action_input, note=skip, tags=skip
        inputs = f"50\n30\ny\n{action_input}\n\n\n"
        return runner.invoke(app, ["trade", "EUR_USD", "long"], input=inputs)

    def test_failure_shows_retry_prompt(
        self, trade_db: Path, plan_file: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        result = self._invoke_with_failure(monkeypatch, "a")
        assert "[R]etry" in result.output + result.stderr
        assert "[S]ave" in result.output + result.stderr

    def test_abort_exits_0(
        self, trade_db: Path, plan_file: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        result = self._invoke_with_failure(monkeypatch, "a")
        assert result.exit_code == 0, result.output
        assert "aborted" in result.output.lower()

    def test_abort_does_not_fill(
        self, trade_db: Path, plan_file: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        fake = FakeFullClient()
        fake.place_market_order = _failing_market_order  # type: ignore[method-assign]
        monkeypatch.setattr(
            "frmj.cli.trade.get_client", lambda conn, account_name=None: fake
        )
        runner.invoke(app, ["trade", "EUR_USD", "long"], input="50\n30\ny\na\n")
        assert not fake.order_placed

    def test_retry_places_order_again(
        self, trade_db: Path, plan_file: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Choosing R loops back and the second attempt succeeds."""
        result = self._invoke_with_failure(monkeypatch, "r", fail_count=1)
        assert result.exit_code == 0, result.output
        assert "filled" in result.output

    def test_save_writes_plan_file(
        self, trade_db: Path, plan_file: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        self._invoke_with_failure(monkeypatch, "s")
        assert plan_file.exists()

    def test_saved_plan_contains_expected_fields(
        self, trade_db: Path, plan_file: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        self._invoke_with_failure(monkeypatch, "s")
        plan = json.loads(plan_file.read_text())
        assert plan["instrument"] == "EUR_USD"
        assert plan["direction"] == "long"
        assert isinstance(plan["units_signed"], int)
        assert plan["tp_price"] is not None
        assert plan["sl_price"] is not None

    def test_save_exits_0_and_prints_resume_hint(
        self, trade_db: Path, plan_file: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        result = self._invoke_with_failure(monkeypatch, "s")
        assert result.exit_code == 0, result.output
        assert "--resume" in result.output

    def test_timeout_shows_double_fill_warning(
        self, trade_db: Path, plan_file: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """httpx.TimeoutException prints an extra caution about possible double fill."""
        result = self._invoke_with_failure(monkeypatch, "a", use_timeout=True)
        combined = result.output + result.stderr
        assert "double fill" in combined or "may have been placed" in combined

    def test_invalid_retry_choice_reprompts(
        self, trade_db: Path, plan_file: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """An unrecognised retry-prompt answer re-prompts instead of crashing."""
        result = self._invoke_with_failure(monkeypatch, "x\na")
        assert result.exit_code == 0, result.output
        assert "Enter R, S, or A." in result.output + result.stderr

    def test_successful_retry_clears_draft_plan(
        self, trade_db: Path, plan_file: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """When the retry eventually succeeds, any stale plan file is cleared."""
        # Pre-seed a plan file to simulate a prior failed attempt.
        plan_file.write_text('{"instrument": "EUR_USD"}')
        result = self._invoke_with_failure(monkeypatch, "r", fail_count=1)
        assert result.exit_code == 0, result.output
        assert not plan_file.exists()


class TestTradeResume:
    """Tests for ``frmj trade --resume`` executing a saved draft plan."""

    @pytest.fixture()
    def resume_db(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
        path = tmp_path / "resume_test.db"
        monkeypatch.setenv("FRMJ_DB_PATH", str(path))
        monkeypatch.setenv("OANDA_API_TOKEN", "test-token-123")
        conn = get_db(path=path)
        set_config(conn, "account_id", "acct-1")
        # Note: max_open_trades is NOT set — resume skips risk eval.
        conn.close()
        return path

    @pytest.fixture()
    def plan_file(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
        path = tmp_path / "saved_plan.json"
        monkeypatch.setattr("frmj.app._DRAFT_PLAN_PATH", path)
        return path

    def _seed_plan(
        self,
        plan_file: Path,
        instrument: str = "EUR_USD",
        direction: str = "long",
        units_signed: int = 10000,
        tp_price: str | None = "1.10550",
        sl_price: str | None = "1.09750",
    ) -> None:
        plan_file.write_text(
            json.dumps(
                {
                    "instrument": instrument,
                    "direction": direction,
                    "units_signed": units_signed,
                    "tp_price": tp_price,
                    "sl_price": sl_price,
                }
            )
        )

    def test_no_plan_exits_1(
        self, resume_db: Path, plan_file: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        fake = FakeFullClient()
        monkeypatch.setattr(
            "frmj.cli.trade.get_client", lambda conn, account_name=None: fake
        )
        result = runner.invoke(app, ["trade", "--resume"], input="")
        assert result.exit_code == 1
        assert "No saved plan" in result.output + result.stderr

    def test_resume_shows_plan_details(
        self, resume_db: Path, plan_file: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        self._seed_plan(plan_file, instrument="GBP_USD", tp_price="1.25500")
        fake = FakeFullClient()
        monkeypatch.setattr(
            "frmj.cli.trade.get_client", lambda conn, account_name=None: fake
        )
        result = runner.invoke(app, ["trade", "--resume"], input="n\n")
        assert "GBP_USD" in result.output
        assert "1.25500" in result.output

    def test_resume_cancel_does_not_place_order(
        self, resume_db: Path, plan_file: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        self._seed_plan(plan_file)
        fake = FakeFullClient()
        monkeypatch.setattr(
            "frmj.cli.trade.get_client", lambda conn, account_name=None: fake
        )
        runner.invoke(app, ["trade", "--resume"], input="n\n")
        assert not fake.order_placed

    def test_resume_confirm_places_order(
        self, resume_db: Path, plan_file: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        self._seed_plan(plan_file)
        fake = FakeFullClient()
        monkeypatch.setattr(
            "frmj.cli.trade.get_client", lambda conn, account_name=None: fake
        )
        result = runner.invoke(app, ["trade", "--resume"], input="y\n\n\n")
        assert result.exit_code == 0, result.output
        assert fake.order_placed

    def test_resume_attaches_tp_and_sl(
        self, resume_db: Path, plan_file: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        self._seed_plan(plan_file, tp_price="1.10550", sl_price="1.09750")
        fake = FakeFullClient()
        monkeypatch.setattr(
            "frmj.cli.trade.get_client", lambda conn, account_name=None: fake
        )
        runner.invoke(app, ["trade", "--resume"], input="y\n\n")
        assert fake.tp_attached is not None
        assert fake.sl_attached is not None

    def test_resume_with_no_instrument_arg_works(
        self, resume_db: Path, plan_file: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """--resume with no positional args must not raise a validation error."""
        self._seed_plan(plan_file)
        fake = FakeFullClient()
        monkeypatch.setattr(
            "frmj.cli.trade.get_client", lambda conn, account_name=None: fake
        )
        result = runner.invoke(app, ["trade", "--resume"], input="n\n")
        assert result.exit_code == 0, result.output

    def test_resume_clears_plan_after_success(
        self, resume_db: Path, plan_file: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """After a successful fill via --resume, the plan file must be removed."""
        self._seed_plan(plan_file)
        fake = FakeFullClient()
        monkeypatch.setattr(
            "frmj.cli.trade.get_client", lambda conn, account_name=None: fake
        )
        runner.invoke(app, ["trade", "--resume"], input="y\n\n")
        assert not plan_file.exists()

    def test_resume_skips_risk_eval(
        self, resume_db: Path, plan_file: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """--resume must succeed even when max_open_trades is not configured."""
        self._seed_plan(plan_file)
        # resume_db fixture deliberately omits max_open_trades config.
        fake = FakeFullClient()
        monkeypatch.setattr(
            "frmj.cli.trade.get_client", lambda conn, account_name=None: fake
        )
        result = runner.invoke(app, ["trade", "--resume"], input="y\n\n\n")
        assert result.exit_code == 0, result.output

    def test_instrument_arg_with_resume_errors(
        self, resume_db: Path, plan_file: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Passing instrument alongside --resume must exit 1 with a clear message."""
        self._seed_plan(plan_file)
        fake = FakeFullClient()
        monkeypatch.setattr(
            "frmj.cli.trade.get_client", lambda conn, account_name=None: fake
        )
        result = runner.invoke(app, ["trade", "EUR_USD", "--resume"])
        assert result.exit_code == 1
        assert "not used with --resume" in result.output + result.stderr


class TestTradeAccountOption:
    """Tests for ``frmj trade ... --account NAME``."""

    @pytest.fixture()
    def account_db(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
        """Active practice account 'practice', plus 'other' (practice) and
        'my-live' (live); live mode NOT enabled."""
        path = tmp_path / "account_option.db"
        monkeypatch.setenv("FRMJ_DB_PATH", str(path))
        monkeypatch.setenv("OANDA_API_TOKEN", "test-token-123")
        conn = get_db(path=path)
        add_account(conn, "practice", "acct-1", is_practice=True)
        add_account(conn, "other", "acct-2", is_practice=True)
        add_account(conn, "my-live", "101-001-live-001", is_practice=False)
        set_active_account(conn, "practice")
        set_config(conn, "max_open_trades", "5")
        conn.close()
        return path

    @pytest.fixture()
    def plan_file(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
        path = tmp_path / "saved_plan.json"
        monkeypatch.setattr("frmj.app._DRAFT_PLAN_PATH", path)
        return path

    @staticmethod
    def _patch_client(
        monkeypatch: pytest.MonkeyPatch, fake: FakeFullClient
    ) -> list[str | None]:
        """Stub get_client with *fake*; return the list of requested names."""
        requested: list[str | None] = []

        def _get_client(conn: object, account_name: str | None = None) -> object:
            requested.append(account_name)
            return fake

        monkeypatch.setattr("frmj.cli.trade.get_client", _get_client)
        return requested

    def test_account_with_multi_exits_1(self, account_db: Path) -> None:
        result = runner.invoke(
            app, ["trade", "EUR_USD", "long", "--multi", "grp", "--account", "other"]
        )
        assert result.exit_code == 1
        assert "cannot be combined with --multi" in result.output + result.stderr

    def test_account_with_resume_exits_1(self, account_db: Path) -> None:
        result = runner.invoke(app, ["trade", "--resume", "--account", "other"])
        assert result.exit_code == 1
        assert "not used with --resume" in result.output + result.stderr

    def test_unknown_account_exits_1(self, account_db: Path) -> None:
        result = runner.invoke(
            app, ["trade", "EUR_USD", "long", "--dry-run", "--account", "ghost"]
        )
        assert result.exit_code == 1
        assert "No account named 'ghost'" in result.output + result.stderr

    def test_dry_run_uses_and_names_account(
        self, account_db: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        requested = self._patch_client(monkeypatch, FakeFullClient())
        result = runner.invoke(
            app,
            ["trade", "EUR_USD", "long", "--dry-run", "--account", "other"],
            input="\n\n",
        )
        assert result.exit_code == 0, result.output
        assert requested == ["other"]
        assert "Account:         other" in result.output

    def test_live_gate_checks_target_account(
        self, account_db: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The active account is practice, but the order goes to a live one:
        practice mode must still block it."""
        fake = FakeFullClient()
        self._patch_client(monkeypatch, fake)
        result = runner.invoke(
            app,
            ["trade", "EUR_USD", "long", "--account", "my-live"],
            input="50\n30\ny\n",
        )
        assert result.exit_code == 1
        assert "Account 'my-live' is a live account" in result.output + result.stderr
        assert not fake.order_placed

    def test_saved_draft_records_account(
        self, account_db: Path, plan_file: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        fake = FakeFullClient()
        fake.place_market_order = _failing_market_order  # type: ignore[method-assign]
        self._patch_client(monkeypatch, fake)
        # TP=50, SL=30, confirm=y, then Save at the retry prompt.
        runner.invoke(
            app,
            ["trade", "EUR_USD", "long", "--account", "other"],
            input="50\n30\ny\ns\n",
        )
        assert json.loads(plan_file.read_text())["account"] == "other"

    def test_saved_draft_records_active_account_by_default(
        self, account_db: Path, plan_file: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Without --account the resolved active account is recorded, so a
        later 'frmj account use' doesn't redirect the resumed order."""
        fake = FakeFullClient()
        fake.place_market_order = _failing_market_order  # type: ignore[method-assign]
        self._patch_client(monkeypatch, fake)
        runner.invoke(app, ["trade", "EUR_USD", "long"], input="50\n30\ny\ns\n")
        assert json.loads(plan_file.read_text())["account"] == "practice"

    def test_saved_draft_records_oanda_id(
        self, account_db: Path, plan_file: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The draft records the account's Oanda ID alongside its name, so a
        resume can follow the account through a rename."""
        fake = FakeFullClient()
        fake.place_market_order = _failing_market_order  # type: ignore[method-assign]
        self._patch_client(monkeypatch, fake)
        runner.invoke(
            app,
            ["trade", "EUR_USD", "long", "--account", "other"],
            input="50\n30\ny\ns\n",
        )
        saved = json.loads(plan_file.read_text())
        assert saved["account"] == "other"
        assert saved["account_oanda_id"] == "acct-2"

    @staticmethod
    def _write_plan(plan_file: Path, account: str, oanda_id: str) -> None:
        """Write a market-order draft for *account* / *oanda_id*."""
        plan_file.write_text(
            json.dumps(
                {
                    "instrument": "EUR_USD",
                    "direction": "long",
                    "units_signed": 10000,
                    "tp_price": None,
                    "sl_price": None,
                    "account": account,
                    "account_oanda_id": oanda_id,
                }
            )
        )

    def test_resume_follows_renamed_account(
        self, account_db: Path, plan_file: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A draft saved for 'other' resumes on it after it's renamed."""
        self._write_plan(plan_file, "other", "acct-2")
        runner.invoke(app, ["account", "rename", "other", "renamed"])
        fake = FakeFullClient()
        requested = self._patch_client(monkeypatch, fake)
        result = runner.invoke(app, ["trade", "--resume"], input="y\n\n\n")
        assert result.exit_code == 0, result.output
        assert requested == ["renamed"]
        assert "now named 'renamed'" in result.output
        assert "Account:   renamed" in result.output
        assert fake.order_placed

    def test_resume_refuses_reused_name_on_other_account(
        self, account_db: Path, plan_file: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """If the saved name now belongs to a different Oanda account (removed,
        then re-added), the order must not go there."""
        self._write_plan(plan_file, "other", "acct-gone")
        fake = FakeFullClient()
        requested = self._patch_client(monkeypatch, fake)
        result = runner.invoke(app, ["trade", "--resume"], input="y\n\n\n")
        assert result.exit_code == 1
        assert "no longer configured" in result.output + result.stderr
        assert requested == []
        assert not fake.order_placed

    def test_resume_prefers_saved_name_among_shared_ids(
        self, account_db: Path, plan_file: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Two profiles on one Oanda account: the saved name picks between them."""
        conn = get_db(path=account_db)
        add_account(conn, "other-alias", "acct-2", is_practice=True)
        conn.close()
        self._write_plan(plan_file, "other", "acct-2")
        requested = self._patch_client(monkeypatch, FakeFullClient())
        result = runner.invoke(app, ["trade", "--resume"], input="y\n\n\n")
        assert result.exit_code == 0, result.output
        assert requested == ["other"]

    def test_resume_ambiguous_shared_id_exits_1(
        self, account_db: Path, plan_file: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Saved name gone and several profiles share the ID: refuse to guess."""
        conn = get_db(path=account_db)
        add_account(conn, "other-alias", "acct-2", is_practice=True)
        conn.close()
        self._write_plan(plan_file, "old-name", "acct-2")
        fake = FakeFullClient()
        requested = self._patch_client(monkeypatch, fake)
        result = runner.invoke(app, ["trade", "--resume"], input="y\n\n\n")
        assert result.exit_code == 1
        assert "other, other-alias" in result.output + result.stderr
        assert requested == []
        assert not fake.order_placed

    def test_resume_targets_saved_account(
        self, account_db: Path, plan_file: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        plan_file.write_text(
            json.dumps(
                {
                    "instrument": "EUR_USD",
                    "direction": "long",
                    "units_signed": 10000,
                    "tp_price": None,
                    "sl_price": None,
                    "account": "other",
                }
            )
        )
        fake = FakeFullClient()
        requested = self._patch_client(monkeypatch, fake)
        result = runner.invoke(app, ["trade", "--resume"], input="y\n\n\n")
        assert result.exit_code == 0, result.output
        assert requested == ["other"]
        assert "Account:   other" in result.output
        assert fake.order_placed

    def test_resume_legacy_plan_uses_active_account(
        self, account_db: Path, plan_file: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A draft saved before plans recorded an account has no 'account'
        key and resumes on the active account, as it always did."""
        plan_file.write_text(
            json.dumps(
                {
                    "instrument": "EUR_USD",
                    "direction": "long",
                    "units_signed": 10000,
                    "tp_price": None,
                    "sl_price": None,
                }
            )
        )
        requested = self._patch_client(monkeypatch, FakeFullClient())
        result = runner.invoke(app, ["trade", "--resume"], input="y\n\n\n")
        assert result.exit_code == 0, result.output
        assert requested == [None]


class TestTradeMultiAccount:
    """Tests for ``frmj trade ... --multi GROUP`` fanning a trade out to a group."""

    @pytest.fixture()
    def multi_db(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
        """DB with two practice accounts ('alpha', 'beta') in group 'grp'."""
        path = tmp_path / "multi_trade_test.db"
        monkeypatch.setenv("FRMJ_DB_PATH", str(path))
        monkeypatch.setenv("OANDA_API_TOKEN", "test-token-123")
        conn = get_db(path=path)
        add_account(conn, "alpha", "alpha-acct", is_practice=True)
        add_account(conn, "beta", "beta-acct", is_practice=True)
        add_group_member(conn, "grp", "alpha")
        add_group_member(conn, "grp", "beta")
        set_config(conn, "max_open_trades", "5")
        conn.close()
        return path

    @staticmethod
    def _fake(
        account_id: str, margin_available: Decimal = Decimal("8000.00")
    ) -> FakeFullClient:
        """A FakeFullClient with a distinct account_id and margin_available."""
        fake = FakeFullClient(account_id=account_id)
        base_summary = fake.get_account_summary

        def _summary() -> AccountSummary:
            s = base_summary()
            return AccountSummary(
                nav=s.nav,
                balance=s.balance,
                unrealized_pl=s.unrealized_pl,
                realized_pl=s.realized_pl,
                position_value=s.position_value,
                margin_used=s.margin_used,
                margin_available=margin_available,
                open_trade_count=s.open_trade_count,
                margin_closeout_percent=s.margin_closeout_percent,
            )

        fake.get_account_summary = _summary  # type: ignore[method-assign]
        return fake

    def _invoke(
        self,
        monkeypatch: pytest.MonkeyPatch,
        fakes: dict[str, FakeFullClient],
        inputs: str,
        args: list[str] | None = None,
    ) -> Result:
        monkeypatch.setattr(
            "frmj.cli._trade_multi.get_client_for_account",
            lambda account: fakes[account.name],
        )
        return runner.invoke(
            app, args or ["trade", "EUR_USD", "long", "--multi", "grp"], input=inputs
        )

    def test_dry_run_shows_both_accounts(
        self, multi_db: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        fakes = {"alpha": self._fake("alpha-acct"), "beta": self._fake("beta-acct")}
        result = self._invoke(
            monkeypatch,
            fakes,
            "\n\n",
            ["trade", "EUR_USD", "long", "--multi", "grp", "--dry-run"],
        )
        assert result.exit_code == 0, result.output
        assert "alpha" in result.output
        assert "beta" in result.output
        assert "2 accounts" in result.output
        assert not fakes["alpha"].order_placed
        assert not fakes["beta"].order_placed

    def test_independent_sizing_differs_by_account(
        self, multi_db: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Accounts with different margin_available get different unit counts."""
        fakes = {
            "alpha": self._fake("alpha-acct", margin_available=Decimal("8000.00")),
            "beta": self._fake("beta-acct", margin_available=Decimal("16000.00")),
        }
        result = self._invoke(
            monkeypatch,
            fakes,
            "\n\n",
            ["trade", "EUR_USD", "long", "--multi", "grp", "--dry-run"],
        )
        assert result.exit_code == 0, result.output
        lines = [
            line for line in result.output.splitlines() if "Capital at risk" in line
        ]
        assert len(lines) == 2
        assert lines[0] != lines[1]

    def test_execute_places_orders_on_both_accounts(
        self, multi_db: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        fakes = {"alpha": self._fake("alpha-acct"), "beta": self._fake("beta-acct")}
        # TP=50, SL=30, confirm=y, note=skip, tags=skip.
        result = self._invoke(monkeypatch, fakes, "50\n30\ny\n\n\n")
        assert result.exit_code == 0, result.output
        assert fakes["alpha"].order_placed
        assert fakes["beta"].order_placed
        assert "2/2 accounts" in result.output

    def test_group_not_found_exits_1(
        self, multi_db: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        result = runner.invoke(
            app, ["trade", "EUR_USD", "long", "--multi", "ghost-group"]
        )
        assert result.exit_code == 1
        assert "not found" in result.output + result.stderr

    def test_resume_with_multi_exits_1(self, multi_db: Path) -> None:
        result = runner.invoke(app, ["trade", "--resume", "--multi", "grp"])
        assert result.exit_code == 1
        assert "not supported" in result.output + result.stderr

    def test_live_mode_gate_blocks_mixed_group(
        self, multi_db: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A live account in the group blocks the whole trade when live mode is off."""
        conn = get_db(path=multi_db)
        add_account(conn, "gamma", "gamma-acct", is_practice=False)
        add_group_member(conn, "grp", "gamma")
        conn.close()
        fakes = {
            "alpha": self._fake("alpha-acct"),
            "beta": self._fake("beta-acct"),
            "gamma": self._fake("gamma-acct"),
        }
        # TP=50, SL=30, confirm=y → live mode gate fires before any order is placed.
        result = self._invoke(monkeypatch, fakes, "50\n30\ny\n")
        assert result.exit_code == 1
        assert "live trading mode is not enabled" in result.output + result.stderr
        assert not fakes["alpha"].order_placed
        assert not fakes["gamma"].order_placed

    def test_note_and_tags_applied_to_every_filled_account(
        self, multi_db: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        conn = get_db(path=multi_db)
        for account_id in ("alpha-acct", "beta-acct"):
            conn.execute(
                "INSERT INTO transactions (oanda_id, account_id, type, time, raw_json) "
                "VALUES ('99999', ?, 'ORDER_FILL', '2026-04-29T12:00:00Z', '{}')",
                (account_id,),
            )
        conn.commit()
        conn.close()

        fakes = {"alpha": self._fake("alpha-acct"), "beta": self._fake("beta-acct")}
        # TP=50, SL=30, confirm=y, note='shared note', tags='tag1 tag2'.
        result = self._invoke(monkeypatch, fakes, "50\n30\ny\nshared note\ntag1 tag2\n")
        assert result.exit_code == 0, result.output

        conn = get_db(path=multi_db)
        note_count = conn.execute("SELECT COUNT(*) FROM notes").fetchone()[0]
        tag_count = conn.execute(
            "SELECT COUNT(DISTINCT transaction_id) FROM tags"
        ).fetchone()[0]
        conn.close()
        assert note_count == 2
        assert tag_count == 2

    def test_skip_failing_account_continues_with_rest(
        self, multi_db: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        alpha = self._fake("alpha-acct")
        beta = self._fake("beta-acct")

        def _always_fail(instrument: str, units_signed: int) -> OrderFill:
            raise RuntimeError("Order rejected by Oanda")

        alpha.place_market_order = _always_fail  # type: ignore[method-assign]
        fakes = {"alpha": alpha, "beta": beta}
        # TP=50, SL=30, confirm=y, [alpha fails] skip='s', note=skip, tags=skip.
        result = self._invoke(monkeypatch, fakes, "50\n30\ny\ns\n\n\n")
        assert result.exit_code == 0, result.output
        assert not alpha.order_placed
        assert beta.order_placed
        assert "1/2 accounts" in result.output
        assert "alpha" in result.output

    def test_hard_block_on_one_account_aborts_before_any_order(
        self, multi_db: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A HARD_BLOCK risk failure on any account aborts the whole group."""
        from frmj.domain.risk import MaxTradesExceeded

        fakes = {"alpha": self._fake("alpha-acct"), "beta": self._fake("beta-acct")}
        monkeypatch.setattr(
            "frmj.cli._trade_multi.get_client_for_account",
            lambda account: fakes[account.name],
        )
        monkeypatch.setattr(
            "frmj.services.evaluate_trade",
            lambda **kw: (_ for _ in ()).throw(
                MaxTradesExceeded("too many open trades")
            ),
        )
        result = runner.invoke(app, ["trade", "EUR_USD", "long", "--multi", "grp"])
        assert result.exit_code == 1
        assert "Cannot trade on 'alpha'" in result.output + result.stderr
        assert not fakes["alpha"].order_placed
        assert not fakes["beta"].order_placed

    def test_correlation_warning_decline_cancels_whole_group(
        self, multi_db: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A correlation warning (WARNING_ONLY) needs one combined acknowledgement;
        declining it cancels the order on every account."""
        fakes = {"alpha": self._fake("alpha-acct"), "beta": self._fake("beta-acct")}
        monkeypatch.setattr(
            "frmj.cli._trade_multi.get_client_for_account",
            lambda account: fakes[account.name],
        )
        monkeypatch.setattr(
            "frmj.services.evaluate_correlation",
            lambda **kw: ["shares USD exposure with an existing GBP_USD position"],
        )
        result = runner.invoke(
            app, ["trade", "EUR_USD", "long", "--multi", "grp"], input="n\n"
        )
        assert result.exit_code == 0
        assert "Proceed anyway?" in result.output + result.stderr
        assert "Order cancelled" in result.output + result.stderr
        assert not fakes["alpha"].order_placed
        assert not fakes["beta"].order_placed

    def test_missing_token_for_one_account_aborts_before_market_data(
        self, multi_db: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A missing token for any group member aborts before fetching market data."""

        def _get_client(account: AccountRecord) -> FakeFullClient:
            if account.name == "beta":
                raise RuntimeError("No API token found for the practice environment.")
            return self._fake(account.oanda_id)

        monkeypatch.setattr("frmj.cli._trade_multi.get_client_for_account", _get_client)
        result = runner.invoke(app, ["trade", "EUR_USD", "long", "--multi", "grp"])
        assert result.exit_code == 1
        assert "Error [beta]" in result.output + result.stderr

    def test_missing_risk_config_exits_1(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """No max_open_trades configured aborts before building any clients."""
        path = tmp_path / "multi_no_risk.db"
        monkeypatch.setenv("FRMJ_DB_PATH", str(path))
        monkeypatch.setenv("OANDA_API_TOKEN", "test-token-123")
        conn = get_db(path=path)
        add_account(conn, "alpha", "alpha-acct", is_practice=True)
        add_account(conn, "beta", "beta-acct", is_practice=True)
        add_group_member(conn, "grp", "alpha")
        add_group_member(conn, "grp", "beta")
        conn.close()
        result = runner.invoke(app, ["trade", "EUR_USD", "long", "--multi", "grp"])
        assert result.exit_code == 1
        assert "Error" in result.output + result.stderr

    def test_market_data_failure_exits_1(
        self, multi_db: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A market-data fetch failure on the primary account aborts the group."""

        class FailingClient(FakeFullClient):
            def get_instrument(self, name: str) -> InstrumentSpec:
                raise RuntimeError("network down")

        fakes = {
            "alpha": FailingClient(account_id="alpha-acct"),
            "beta": self._fake("beta-acct"),
        }
        result = self._invoke(monkeypatch, fakes, "")
        assert result.exit_code == 1
        assert "Error fetching market data" in result.output + result.stderr

    def test_account_data_failure_exits_1(
        self, multi_db: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """An account-data fetch failure on any account aborts the group."""

        class FailingClient(FakeFullClient):
            def get_account_summary(self) -> AccountSummary:
                raise RuntimeError("account data unavailable")

        fakes = {
            "alpha": self._fake("alpha-acct"),
            "beta": FailingClient(account_id="beta-acct"),
        }
        result = self._invoke(monkeypatch, fakes, "")
        assert result.exit_code == 1
        assert "Error fetching account data [beta]" in result.output + result.stderr

    def test_sizing_warnings_shown_per_account(
        self, multi_db: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Non-fatal sizing warnings are echoed per account before the plan table."""
        from frmj.domain.risk import RiskStrategy, SizingDecision

        decision = SizingDecision(
            capital_to_deploy=Decimal("500"),
            strategy_used=RiskStrategy.REMAINING_MARGIN_FRACTION,
            size_fraction=None,
            warnings=("near max open trades",),
        )
        fakes = {"alpha": self._fake("alpha-acct"), "beta": self._fake("beta-acct")}
        monkeypatch.setattr("frmj.services.evaluate_trade", lambda **kw: decision)
        result = self._invoke(
            monkeypatch,
            fakes,
            "\n\n",
            ["trade", "EUR_USD", "long", "--multi", "grp", "--dry-run"],
        )
        assert result.exit_code == 0, result.output
        assert "near max open trades" in result.output + result.stderr

    def test_correlation_hard_block_on_one_account_aborts(
        self, multi_db: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """CorrelatedPositionForbidden (HARD_BLOCK) on any account aborts the group."""
        from frmj.domain.risk import CorrelatedPositionForbidden

        fakes = {"alpha": self._fake("alpha-acct"), "beta": self._fake("beta-acct")}
        monkeypatch.setattr(
            "frmj.cli._trade_multi.get_client_for_account",
            lambda account: fakes[account.name],
        )
        monkeypatch.setattr(
            "frmj.services.evaluate_correlation",
            lambda **kw: (_ for _ in ()).throw(
                CorrelatedPositionForbidden("blocked: correlated exposure")
            ),
        )
        result = runner.invoke(app, ["trade", "EUR_USD", "long", "--multi", "grp"])
        assert result.exit_code == 1
        assert "Cannot trade on 'alpha'" in result.output + result.stderr

    def test_compute_units_failure_exits_1(
        self, multi_db: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """An exception from compute_units for any account aborts the group."""
        fakes = {"alpha": self._fake("alpha-acct"), "beta": self._fake("beta-acct")}
        monkeypatch.setattr(
            "frmj.cli._trade_multi.get_client_for_account",
            lambda account: fakes[account.name],
        )
        monkeypatch.setattr(
            "frmj.services.compute_units",
            lambda **kw: (_ for _ in ()).throw(ValueError("bad sizing")),
        )
        result = runner.invoke(app, ["trade", "EUR_USD", "long", "--multi", "grp"])
        assert result.exit_code == 1
        assert "Error computing units [alpha]" in result.output + result.stderr

    def test_declining_confirm_cancels_group_order(
        self, multi_db: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Answering 'n' at the group confirm prompt cancels without placing
        any orders."""
        fakes = {"alpha": self._fake("alpha-acct"), "beta": self._fake("beta-acct")}
        result = self._invoke(monkeypatch, fakes, "\n\nn\n")
        assert result.exit_code == 0, result.output
        assert "Order cancelled" in result.output
        assert not fakes["alpha"].order_placed
        assert not fakes["beta"].order_placed

    def test_edit_at_confirm_reprompts_tpsl(
        self, multi_db: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Answering 'e' at the group confirm prompt re-prompts for new TP/SL
        and redisplays exit levels before confirming again."""
        fakes = {"alpha": self._fake("alpha-acct"), "beta": self._fake("beta-acct")}
        # TP=50, SL=30, edit=e, new TP=60, new SL=40, confirm=y, note/tags skip.
        result = self._invoke(monkeypatch, fakes, "50\n30\ne\n60\n40\ny\n\n\n")
        assert result.exit_code == 0, result.output
        assert fakes["alpha"].order_placed
        assert fakes["beta"].order_placed

    def test_invalid_retry_choice_then_retry_succeeds(
        self, multi_db: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """An unrecognised retry-prompt answer re-prompts; choosing R after
        that retries the failed account, which then succeeds."""
        alpha = self._fake("alpha-acct")
        beta = self._fake("beta-acct")
        calls: list[int] = []

        def _flaky_order(instrument: str, units_signed: int) -> OrderFill:
            calls.append(1)
            if len(calls) == 1:
                raise RuntimeError("Network error")
            alpha.order_placed = True
            return OrderFill(
                transaction_id="99999",
                fill_price=Decimal("1.10005"),
                units_filled=units_signed,
                trade_id="99999",
            )

        alpha.place_market_order = _flaky_order  # type: ignore[method-assign]
        fakes = {"alpha": alpha, "beta": beta}
        # TP=50, SL=30, confirm=y, [alpha fails] invalid='x', retry='r', note/tags skip.
        result = self._invoke(monkeypatch, fakes, "50\n30\ny\nx\nr\n\n\n")
        assert result.exit_code == 0, result.output
        assert "Enter R or S." in result.output + result.stderr
        assert alpha.order_placed
        assert beta.order_placed
        assert "2/2 accounts" in result.output

    def test_timeout_shows_double_fill_warning(
        self, multi_db: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """httpx.TimeoutException on one account's order prints a double-fill
        caution and offers retry/skip."""
        alpha = self._fake("alpha-acct")
        beta = self._fake("beta-acct")

        def _timeout_order(instrument: str, units_signed: int) -> OrderFill:
            raise httpx.TimeoutException("timed out")

        alpha.place_market_order = _timeout_order  # type: ignore[method-assign]
        fakes = {"alpha": alpha, "beta": beta}
        # TP=50, SL=30, confirm=y, [alpha times out] skip='s', note/tags skip.
        result = self._invoke(monkeypatch, fakes, "50\n30\ny\ns\n\n\n")
        assert result.exit_code == 0, result.output
        combined = result.output + result.stderr
        assert "double fill" in combined or "may have been placed" in combined
        assert not alpha.order_placed
        assert beta.order_placed

    def test_missing_trade_id_warns_per_account(
        self, multi_db: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """If Oanda returns no trade_id for a filled account, a warning is
        shown instead of attempting to attach TP/SL."""
        alpha = self._fake("alpha-acct")
        beta = self._fake("beta-acct")

        def _no_trade_id_fill(instrument: str, units_signed: int) -> OrderFill:
            alpha.order_placed = True
            return OrderFill(
                transaction_id="99999",
                fill_price=Decimal("1.10005"),
                units_filled=units_signed,
                trade_id=None,
            )

        alpha.place_market_order = _no_trade_id_fill  # type: ignore[method-assign]
        fakes = {"alpha": alpha, "beta": beta}
        result = self._invoke(monkeypatch, fakes, "50\n30\ny\n\n\n")
        assert result.exit_code == 0, result.output
        assert "trade ID" in result.output + result.stderr

    def test_tp_attach_failure_warns_per_account(
        self, multi_db: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A take-profit attach failure on one account is a warning only."""
        alpha = self._fake("alpha-acct")
        alpha.tp_should_fail = True
        beta = self._fake("beta-acct")
        fakes = {"alpha": alpha, "beta": beta}
        result = self._invoke(monkeypatch, fakes, "50\n30\ny\n\n\n")
        assert result.exit_code == 0, result.output
        assert "failed to attach take-profit" in result.output + result.stderr
        assert alpha.order_placed
        assert beta.order_placed

    def test_sl_attach_failure_warns_unprotected_per_account(
        self, multi_db: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A stop-loss attach failure on one account warns that the position
        is unprotected."""
        alpha = self._fake("alpha-acct")
        alpha.sl_should_fail = True
        beta = self._fake("beta-acct")
        fakes = {"alpha": alpha, "beta": beta}
        result = self._invoke(monkeypatch, fakes, "50\n30\ny\n\n\n")
        assert result.exit_code == 0, result.output
        combined = result.output + result.stderr
        assert "failed to attach stop-loss" in combined
        assert "unprotected" in combined

    def test_post_fill_sync_failure_warns_per_account(
        self, multi_db: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A post-fill sync failure on one account is a warning only."""
        alpha = self._fake("alpha-acct")
        alpha.sync_should_fail = True
        beta = self._fake("beta-acct")
        fakes = {"alpha": alpha, "beta": beta}
        result = self._invoke(monkeypatch, fakes, "50\n30\ny\n\n\n")
        assert result.exit_code == 0, result.output
        assert "post-fill sync failed" in result.output + result.stderr
        assert alpha.order_placed

    def test_note_and_tags_not_saved_when_fill_row_missing(
        self, multi_db: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """When post-fill sync hasn't brought a fill into the local DB yet,
        the note/tags prompt warns for that account rather than discarding
        the input silently."""
        fakes = {"alpha": self._fake("alpha-acct"), "beta": self._fake("beta-acct")}
        # TP=50, SL=30, confirm=y, note='lost note', tags='lost-tag'.
        result = self._invoke(monkeypatch, fakes, "50\n30\ny\nlost note\nlost-tag\n")
        assert result.exit_code == 0, result.output
        combined = result.output + result.stderr
        assert "Note/tags not saved" in combined


class TestTradeMultiOpposite:
    """Tests for ``frmj trade ... --multi GROUP --opposite ACCOUNT``."""

    @pytest.fixture()
    def multi_db(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
        """DB with two practice accounts ('alpha', 'beta') in group 'grp'."""
        path = tmp_path / "multi_opposite_test.db"
        monkeypatch.setenv("FRMJ_DB_PATH", str(path))
        monkeypatch.setenv("OANDA_API_TOKEN", "test-token-123")
        conn = get_db(path=path)
        add_account(conn, "alpha", "alpha-acct", is_practice=True)
        add_account(conn, "beta", "beta-acct", is_practice=True)
        add_group_member(conn, "grp", "alpha")
        add_group_member(conn, "grp", "beta")
        set_config(conn, "max_open_trades", "5")
        conn.close()
        return path

    @staticmethod
    def _fake(account_id: str) -> FakeFullClient:
        return FakeFullClient(account_id=account_id)

    def _invoke(
        self,
        monkeypatch: pytest.MonkeyPatch,
        fakes: dict[str, FakeFullClient],
        inputs: str,
        args: list[str],
    ) -> Result:
        monkeypatch.setattr(
            "frmj.cli._trade_multi.get_client_for_account",
            lambda account: fakes[account.name],
        )
        return runner.invoke(app, args, input=inputs)

    def test_opposite_requires_multi(self, multi_db: Path) -> None:
        result = runner.invoke(app, ["trade", "EUR_USD", "long", "--opposite", "alpha"])
        assert result.exit_code == 1
        assert "--opposite requires --multi" in result.output + result.stderr

    def test_unknown_opposite_account_exits_1(
        self, multi_db: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        fakes = {"alpha": self._fake("alpha-acct"), "beta": self._fake("beta-acct")}
        result = self._invoke(
            monkeypatch,
            fakes,
            "",
            ["trade", "EUR_USD", "long", "--multi", "grp", "--opposite", "ghost"],
        )
        assert result.exit_code == 1
        combined = result.output + result.stderr
        assert "not in group" in combined
        assert "ghost" in combined

    def test_dry_run_shows_flipped_direction_and_entry(
        self, multi_db: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        fakes = {"alpha": self._fake("alpha-acct"), "beta": self._fake("beta-acct")}
        result = self._invoke(
            monkeypatch,
            fakes,
            "\n\n",
            [
                "trade",
                "EUR_USD",
                "long",
                "--multi",
                "grp",
                "--opposite",
                "beta",
                "--dry-run",
            ],
        )
        assert result.exit_code == 0, result.output
        assert "LONG" in result.output
        assert "SHORT" in result.output
        # Long fills at ask (1.10010), short at bid (1.09990) — the
        # FakeFullClient's fixed quote — so the flipped account shows a
        # different entry price in the plan table.
        assert "1.10010" in result.output
        assert "1.09990" in result.output

    def test_execute_places_opposite_direction_and_mirrors_tpsl(
        self, multi_db: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        alpha = self._fake("alpha-acct")
        beta = self._fake("beta-acct")
        alpha_units: list[int] = []
        beta_units: list[int] = []
        base_alpha_order = alpha.place_market_order
        base_beta_order = beta.place_market_order

        def _alpha_order(instrument: str, units_signed: int) -> OrderFill:
            alpha_units.append(units_signed)
            return base_alpha_order(instrument, units_signed)

        def _beta_order(instrument: str, units_signed: int) -> OrderFill:
            beta_units.append(units_signed)
            return base_beta_order(instrument, units_signed)

        alpha.place_market_order = _alpha_order  # type: ignore[method-assign]
        beta.place_market_order = _beta_order  # type: ignore[method-assign]
        fakes = {"alpha": alpha, "beta": beta}
        # TP=50, SL=30, confirm=y, note/tags skip.
        result = self._invoke(
            monkeypatch,
            fakes,
            "50\n30\ny\n\n\n",
            ["trade", "EUR_USD", "long", "--multi", "grp", "--opposite", "beta"],
        )
        assert result.exit_code == 0, result.output
        assert alpha_units and alpha_units[0] > 0  # alpha stayed long
        assert beta_units and beta_units[0] < 0  # beta flipped short
        assert alpha.tp_attached is not None
        assert beta.tp_attached is not None
        # alpha (long) TP sits above its entry; beta (short) TP sits below
        # its entry — mirrored around each account's own entry price.
        assert Decimal(alpha.tp_attached) > Decimal("1.10010")
        assert Decimal(beta.tp_attached) < Decimal("1.09990")


class TestTradeMultiTrail:
    """``trade ... --multi GROUP --trail``: one trail distance on every account."""

    @pytest.fixture()
    def multi_db(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
        """DB with two practice accounts ('alpha', 'beta') in group 'grp'."""
        path = tmp_path / "multi_trail_test.db"
        monkeypatch.setenv("FRMJ_DB_PATH", str(path))
        monkeypatch.setenv("OANDA_API_TOKEN", "test-token-123")
        conn = get_db(path=path)
        add_account(conn, "alpha", "alpha-acct", is_practice=True)
        add_account(conn, "beta", "beta-acct", is_practice=True)
        add_group_member(conn, "grp", "alpha")
        add_group_member(conn, "grp", "beta")
        set_config(conn, "max_open_trades", "5")
        conn.close()
        return path

    def _invoke(
        self,
        monkeypatch: pytest.MonkeyPatch,
        fakes: dict[str, FakeFullClient],
        inputs: str,
        *extra: str,
    ) -> Result:
        monkeypatch.setattr(
            "frmj.cli._trade_multi.get_client_for_account",
            lambda account: fakes[account.name],
        )
        args = ["trade", "EUR_USD", "long", "--multi", "grp", "--trail", *extra]
        return runner.invoke(app, args, input=inputs)

    def test_dry_run_mirrors_trail_for_opposite_account(
        self, multi_db: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Alpha's long trail starts below the bid, beta's short above the ask."""
        fakes = {
            "alpha": FakeFullClient(account_id="alpha-acct"),
            "beta": FakeFullClient(account_id="beta-acct"),
        }
        result = self._invoke(
            monkeypatch, fakes, "\n\n20\n", "--opposite", "beta", "--dry-run"
        )
        assert result.exit_code == 0, result.output
        assert "starts at 1.09790" in result.output
        assert "starts at 1.10210" in result.output

    def test_trail_attached_on_every_account(
        self, multi_db: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        fakes = {
            "alpha": FakeFullClient(account_id="alpha-acct"),
            "beta": FakeFullClient(account_id="beta-acct"),
        }
        # TP/SL skip, trail 20, confirm, note/tags skip
        result = self._invoke(monkeypatch, fakes, "\n\n20\ny\n\n\n")
        assert result.exit_code == 0, result.output
        assert fakes["alpha"].trail_attached == "0.00200"
        assert fakes["beta"].trail_attached == "0.00200"
        assert "[alpha] Trailing stop set at 20.0 pips" in result.output
        assert "[beta] Trailing stop set at 20.0 pips" in result.output

    def test_edit_reprompts_trail(
        self, multi_db: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        fakes = {
            "alpha": FakeFullClient(account_id="alpha-acct"),
            "beta": FakeFullClient(account_id="beta-acct"),
        }
        result = self._invoke(monkeypatch, fakes, "\n\n20\ne\n\n\n30\ny\n\n\n")
        assert result.exit_code == 0, result.output
        assert fakes["alpha"].trail_attached == "0.00300"
        assert fakes["beta"].trail_attached == "0.00300"

    def test_trail_failure_on_one_account_warns_unprotected(
        self, multi_db: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        fakes = {
            "alpha": FakeFullClient(account_id="alpha-acct"),
            "beta": FakeFullClient(account_id="beta-acct", trail_should_fail=True),
        }
        result = self._invoke(monkeypatch, fakes, "\n\n20\ny\n\n\n")
        assert result.exit_code == 0, result.output
        output = result.output + result.stderr
        assert "[alpha] Trailing stop set" in output
        assert "Warning [beta]: failed to attach trailing stop" in output
        assert "[beta] Position is unprotected" in output
