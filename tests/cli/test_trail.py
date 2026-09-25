"""Tests for ``frmj trail``.

The fake client quotes EUR_USD at bid 1.09990 / ask 1.10010 (5 dp, no
trailing-stop bounds), and ``_open_trade`` defaults to trade #6368: LONG
10,000 units @ 1.10050 with SL 1.09750 — so a 15-pip trail on it triggers
at 1.09990 - 0.00150 = 1.09840, which is -$21.00 from entry.
"""

from __future__ import annotations

from dataclasses import replace
from decimal import Decimal
from pathlib import Path

import pytest
from click.testing import Result
from typer.testing import CliRunner

from frmj.cli import app
from frmj.cli._completion import _complete_open_trade_id
from frmj.domain.sizing import InstrumentSpec

from .conftest import FakeFullClient, _completion_ctx, _open_trade

runner = CliRunner()


def _invoke(
    monkeypatch: pytest.MonkeyPatch,
    fake: FakeFullClient,
    args: list[str],
    inputs: str = "",
) -> Result:
    """Run ``frmj trail ARGS`` against *fake*, answering prompts with *inputs*."""
    monkeypatch.setattr(
        "frmj.cli.trail.get_client", lambda conn, account_name=None: fake
    )
    return runner.invoke(app, ["trail", *args], input=inputs)


class TestSetTrail:
    def test_long_shows_trigger_and_pl_then_sets(
        self, db_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        fake = FakeFullClient(open_trades=[_open_trade()])
        result = _invoke(monkeypatch, fake, ["6368", "15"], "y\n")
        assert result.exit_code == 0, result.output
        assert "15.0p (0.00150)" in result.output
        assert "triggers now at 1.09840" in result.output
        assert "$-21.00 from entry" in result.output
        assert fake.trail_changes == [("6368", Decimal("0.00150"))]
        assert "Trailing stop set (txn #100004)" in result.output

    def test_short_trigger_is_above_ask(
        self, db_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # 1.10010 + 0.00150 = 1.10160; a short from 1.10050 loses $11 there.
        fake = FakeFullClient(open_trades=[_open_trade(direction="SHORT")])
        result = _invoke(monkeypatch, fake, ["6368", "15"], "n\n")
        assert "triggers now at 1.10160" in result.output
        assert "$-11.00 from entry" in result.output

    def test_winning_trade_shows_locked_in_profit(
        self, db_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # Entry 1.09000, trigger 1.09840: +$84.00 locked in.
        fake = FakeFullClient(open_trades=[_open_trade(open_price="1.09000")])
        result = _invoke(monkeypatch, fake, ["6368", "15"], "n\n")
        assert "+$84.00 from entry" in result.output

    def test_accepts_p_suffix(
        self, db_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        fake = FakeFullClient(open_trades=[_open_trade()])
        result = _invoke(monkeypatch, fake, ["6368", "15p"], "y\n")
        assert result.exit_code == 0, result.output
        assert fake.trail_changes == [("6368", Decimal("0.00150"))]

    def test_cancel_sends_nothing(
        self, db_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        fake = FakeFullClient(open_trades=[_open_trade()])
        result = _invoke(monkeypatch, fake, ["6368", "15"], "n\n")
        assert result.exit_code == 0, result.output
        assert "Cancelled." in result.output
        assert fake.trail_changes == []

    def test_existing_trail_is_shown_and_replaced(
        self, db_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        trade = _open_trade(trail_distance="0.00200", trail_price="1.09800")
        fake = FakeFullClient(open_trades=[trade])
        result = _invoke(monkeypatch, fake, ["6368", "15"], "y\n")
        assert "Trail: 1.09800 [0.00200 behind]" in result.output
        assert "Replace the trailing stop on #6368?" in result.output
        assert fake.trail_changes == [("6368", Decimal("0.00150"))]

    def test_below_instrument_minimum_rejected_before_prompt(
        self, db_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        fake = FakeFullClient(open_trades=[_open_trade()])
        bounded = replace(
            fake.get_instrument("EUR_USD"),
            min_trailing_stop_distance=Decimal("0.00050"),
        )

        def _get_instrument(name: str) -> InstrumentSpec:
            return bounded

        monkeypatch.setattr(fake, "get_instrument", _get_instrument)
        result = _invoke(monkeypatch, fake, ["6368", "4"])
        assert result.exit_code == 1
        assert "at least 5.0 pips" in result.output
        assert fake.trail_changes == []


class TestRemoveTrail:
    def test_off_removes_trail(
        self, db_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        fake = FakeFullClient(open_trades=[_open_trade(trail_distance="0.00200")])
        result = _invoke(monkeypatch, fake, ["6368", "off"], "y\n")
        assert result.exit_code == 0, result.output
        assert "Remove the trailing stop from #6368?" in result.output
        assert fake.trail_changes == [("6368", None)]
        assert "Trailing stop removed (txn #100005)" in result.output

    def test_off_is_case_insensitive(
        self, db_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        fake = FakeFullClient(open_trades=[_open_trade(trail_distance="0.00200")])
        result = _invoke(monkeypatch, fake, ["6368", "OFF"], "y\n")
        assert fake.trail_changes == [("6368", None)], result.output

    def test_off_without_trail_is_a_no_op(
        self, db_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        fake = FakeFullClient(open_trades=[_open_trade()])
        result = _invoke(monkeypatch, fake, ["6368", "off"])
        assert result.exit_code == 0, result.output
        assert "has no trailing stop" in result.output
        assert fake.trail_changes == []

    def test_off_warns_when_no_fixed_stop_remains(
        self, db_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        trade = _open_trade(sl_price=None, trail_distance="0.00200")
        fake = FakeFullClient(open_trades=[trade])
        result = _invoke(monkeypatch, fake, ["6368", "off"], "y\n")
        assert "no stop at all" in result.output
        # A warning, not a refusal: the removal still goes through.
        assert fake.trail_changes == [("6368", None)]

    def test_off_with_fixed_stop_has_no_warning(
        self, db_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        fake = FakeFullClient(open_trades=[_open_trade(trail_distance="0.00200")])
        result = _invoke(monkeypatch, fake, ["6368", "off"], "n\n")
        assert "no stop at all" not in result.output


class TestErrors:
    @pytest.mark.parametrize("pips", ["abc", "1.2.3"])
    def test_malformed_pips_rejected(
        self, db_path: Path, monkeypatch: pytest.MonkeyPatch, pips: str
    ) -> None:
        fake = FakeFullClient(open_trades=[_open_trade()])
        result = _invoke(monkeypatch, fake, ["6368", pips])
        assert result.exit_code == 1
        assert "pip distance or 'off'" in result.output

    def test_non_positive_pips_rejected(
        self, db_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        fake = FakeFullClient(open_trades=[_open_trade()])
        result = _invoke(monkeypatch, fake, ["6368", "0"])
        assert result.exit_code == 1
        assert "positive" in result.output

    def test_unknown_trade_id(
        self, db_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        fake = FakeFullClient(open_trades=[_open_trade()])
        result = _invoke(monkeypatch, fake, ["9999", "15"])
        assert result.exit_code == 1
        assert "no open trade #9999" in result.output

    def test_oanda_rejection_exits_nonzero(
        self, db_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        fake = FakeFullClient(open_trades=[_open_trade()], trail_should_fail=True)
        result = _invoke(monkeypatch, fake, ["6368", "15"], "y\n")
        assert result.exit_code == 1
        assert "trailing stop not changed" in result.output
        assert "rejected by Oanda" in result.output

    def test_sync_failure_is_a_warning(
        self, db_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The change already happened on Oanda, so a failed sync must not
        make the command look like it failed."""
        fake = FakeFullClient(open_trades=[_open_trade()], sync_should_fail=True)
        result = _invoke(monkeypatch, fake, ["6368", "15"], "y\n")
        assert result.exit_code == 0, result.output
        assert "Trailing stop set" in result.output
        assert "[sync] Warning" in result.output


class TestAccountOption:
    def test_uses_named_account_and_shows_it_first(
        self, db_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        requested: list[str | None] = []
        fake = FakeFullClient(open_trades=[_open_trade()])

        def _get_client(conn: object, account_name: str | None = None) -> object:
            requested.append(account_name)
            return fake

        monkeypatch.setattr("frmj.cli.trail.get_client", _get_client)
        result = runner.invoke(
            app, ["trail", "6368", "15", "--account", "other"], input="n\n"
        )
        assert result.exit_code == 0, result.output
        assert requested == ["other"]
        assert result.output.index("Account: other") < result.output.index("Set the")


class TestTradeIdCompletion:
    def test_lists_open_trade_ids_numerically(
        self, db_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        fake = FakeFullClient(
            open_trades=[
                _open_trade(trade_id="1000"),
                _open_trade(trade_id="999"),
                _open_trade(trade_id="1050"),
            ]
        )
        monkeypatch.setattr(
            "frmj.cli._completion.get_client", lambda conn, account_name=None: fake
        )
        ctx = _completion_ctx({})
        assert _complete_open_trade_id(ctx, "") == ["999", "1000", "1050"]
        assert _complete_open_trade_id(ctx, "10") == ["1000", "1050"]

    def test_swallows_client_errors(
        self, db_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        def _fail(conn: object, account_name: str | None = None) -> object:
            raise RuntimeError("no active account")

        monkeypatch.setattr("frmj.cli._completion.get_client", _fail)
        assert _complete_open_trade_id(_completion_ctx({}), "") == []
