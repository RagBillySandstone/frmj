"""Tests for the daily-ATR default stop-loss in ``frmj trade``."""

from __future__ import annotations

import json
from collections.abc import Iterator
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest
from typer.testing import CliRunner

from frmj import services
from frmj.accounts import add_account, add_group_member, set_active_account
from frmj.app import get_db
from frmj.cli import app
from frmj.cli._trade_helpers import _parse_atr_multiple, _prompt_stop_loss
from frmj.domain.pricing import TPSLKind, TPSLSpec

from .conftest import _config_all_accounts, FakeFullClient, _atr_candles

runner = CliRunner()


@pytest.fixture()
def trade_db(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """DB with the config the trade command needs."""
    path = tmp_path / "trade_atr.db"
    monkeypatch.setenv("FRMJ_DB_PATH", str(path))
    monkeypatch.setenv("OANDA_API_TOKEN", "test-token-123")
    conn = get_db(path=path)
    add_account(conn, "practice", "acct-1", is_practice=True)
    set_active_account(conn, "practice")
    _config_all_accounts(conn, "max_open_trades", "5")
    conn.close()
    return path


def _use_client(monkeypatch: pytest.MonkeyPatch, fake: FakeFullClient) -> None:
    monkeypatch.setattr(
        "frmj.cli.trade.get_client", lambda conn, account_name=None: fake
    )


class TestFetchInstrumentContextAtr:
    """``services.fetch_instrument_context`` computes the daily ATR on request."""

    def test_computes_atr_from_candles(self) -> None:
        fake = FakeFullClient(candles=_atr_candles(range_pips=50))
        ctx = services.fetch_instrument_context(fake, "EUR_USD", atr_period=14)  # type: ignore[arg-type]
        assert ctx.daily_atr == Decimal("0.0050")
        # 100 bars of history minimum, plus one for the first previous close.
        assert fake.candles_requested == ("EUR_USD", 101)

    def test_long_period_fetches_more_history(self) -> None:
        fake = FakeFullClient(candles=_atr_candles(count=151))
        services.fetch_instrument_context(fake, "EUR_USD", atr_period=30)  # type: ignore[arg-type]
        assert fake.candles_requested == ("EUR_USD", 151)

    def test_candle_failure_gives_none(self) -> None:
        """A failed candle fetch doesn't break the context — ATR is just None."""
        fake = FakeFullClient(candles=None)
        ctx = services.fetch_instrument_context(fake, "EUR_USD", atr_period=14)  # type: ignore[arg-type]
        assert ctx.daily_atr is None
        assert ctx.spec.name == "EUR_USD"

    def test_too_little_history_gives_none(self) -> None:
        fake = FakeFullClient(candles=_atr_candles(count=10))
        ctx = services.fetch_instrument_context(fake, "EUR_USD", atr_period=14)  # type: ignore[arg-type]
        assert ctx.daily_atr is None

    def test_no_period_skips_candle_fetch(self) -> None:
        """Callers that don't need ATR (e.g. ``frmj trail``) make no request."""
        fake = FakeFullClient(candles=_atr_candles())
        ctx = services.fetch_instrument_context(fake, "EUR_USD")  # type: ignore[arg-type]
        assert ctx.daily_atr is None
        assert fake.candles_requested is None


class TestParseAtrMultiple:
    @pytest.mark.parametrize(
        ("raw", "expected"),
        [
            ("2x", Decimal("2")),
            ("1.5X", Decimal("1.5")),
            ("1.5atr", Decimal("1.5")),
            (" 3 ATR ", Decimal("3")),
        ],
    )
    def test_atr_forms(self, raw: str, expected: Decimal) -> None:
        assert _parse_atr_multiple(raw) == expected

    @pytest.mark.parametrize("raw", ["50", "50p", "10%"])
    def test_other_forms_are_none(self, raw: str) -> None:
        assert _parse_atr_multiple(raw) is None

    @pytest.mark.parametrize("raw", ["x", "0x", "-1x", "abcatr", "nanx"])
    def test_bad_multiple_raises(self, raw: str) -> None:
        with pytest.raises(ValueError):
            _parse_atr_multiple(raw)


def _feed_prompts(monkeypatch: pytest.MonkeyPatch, answers: list[str]) -> list[str]:
    """Answer successive ``typer.prompt`` calls with *answers*; return the
    prompt texts shown."""
    shown: list[str] = []
    it: Iterator[str] = iter(answers)

    def _prompt(text: str, **_: Any) -> str:
        shown.append(text)
        return next(it)

    monkeypatch.setattr("frmj.cli._trade_helpers.typer.prompt", _prompt)
    return shown


class TestPromptStopLoss:
    def test_enter_takes_atr_default(self, monkeypatch: pytest.MonkeyPatch) -> None:
        shown = _feed_prompts(monkeypatch, [""])
        spec, multiple = _prompt_stop_loss("Stop-loss", Decimal("41.6"), Decimal("1.5"))
        assert spec == TPSLSpec(kind=TPSLKind.PIPS, value=Decimal("62.40"))
        assert multiple == Decimal("1.5")
        assert "Enter = 1.5x ATR = 62.4p" in shown[0]

    @pytest.mark.parametrize("raw", ["-", "none", "NONE"])
    def test_dash_skips(self, monkeypatch: pytest.MonkeyPatch, raw: str) -> None:
        _feed_prompts(monkeypatch, [raw])
        assert _prompt_stop_loss("SL", Decimal("40"), Decimal("1.5")) == (None, None)

    def test_inline_multiple(self, monkeypatch: pytest.MonkeyPatch) -> None:
        _feed_prompts(monkeypatch, ["2x"])
        spec, multiple = _prompt_stop_loss("SL", Decimal("40"), Decimal("1.5"))
        assert spec == TPSLSpec(kind=TPSLKind.PIPS, value=Decimal("80"))
        assert multiple == Decimal("2")

    def test_pips_is_manual(self, monkeypatch: pytest.MonkeyPatch) -> None:
        _feed_prompts(monkeypatch, ["35"])
        spec, multiple = _prompt_stop_loss("SL", Decimal("40"), Decimal("1.5"))
        assert spec == TPSLSpec(kind=TPSLKind.PIPS, value=Decimal("35"))
        assert multiple is None

    def test_percent_is_manual(self, monkeypatch: pytest.MonkeyPatch) -> None:
        _feed_prompts(monkeypatch, ["5%"])
        spec, multiple = _prompt_stop_loss("SL", Decimal("40"), Decimal("1.5"))
        assert spec == TPSLSpec(kind=TPSLKind.PERCENT_RETURN, value=Decimal("0.05"))
        assert multiple is None

    def test_invalid_reprompts(
        self, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
    ) -> None:
        shown = _feed_prompts(monkeypatch, ["0x", "abc", "2x"])
        _, multiple = _prompt_stop_loss("SL", Decimal("40"), Decimal("1.5"))
        assert multiple == Decimal("2")
        assert len(shown) == 3
        assert "Invalid input" in capsys.readouterr().out

    def test_no_atr_enter_skips(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Without an ATR the prompt is the old one: Enter means no stop."""
        shown = _feed_prompts(monkeypatch, [""])
        assert _prompt_stop_loss("SL", None, Decimal("1.5")) == (None, None)
        assert "ATR" not in shown[0]


class TestTradeAtrStopLoss:
    """End-to-end: ``frmj trade`` with a 50-pip daily ATR on EUR_USD.

    FakeFullClient quotes ask 1.10010 / bid 1.09990, so a long enters at
    1.10010 and a short at 1.09990.
    """

    def test_header_shows_daily_atr(
        self, trade_db: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _use_client(monkeypatch, FakeFullClient(candles=_atr_candles(50)))
        result = runner.invoke(
            app, ["trade", "EUR_USD", "long", "--dry-run"], input="\n\n"
        )
        assert result.exit_code == 0, result.output
        assert "Daily ATR(14): 50.0p" in result.output
        assert "Enter = 1.5x ATR = 75.0p" in result.output

    def test_enter_sets_default_atr_stop_long(
        self, trade_db: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Enter at the SL prompt = 1.5 x 50 = 75 pips below the ask."""
        _use_client(monkeypatch, FakeFullClient(candles=_atr_candles(50)))
        result = runner.invoke(
            app, ["trade", "EUR_USD", "long", "--dry-run"], input="\n\n"
        )
        assert result.exit_code == 0, result.output
        assert "SL: 1.09260" in result.output
        assert "[1.5× ATR]" in result.output

    def test_enter_sets_default_atr_stop_short(
        self, trade_db: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """For a short the ATR stop sits 75 pips above the bid."""
        _use_client(monkeypatch, FakeFullClient(candles=_atr_candles(50)))
        result = runner.invoke(
            app, ["trade", "EUR_USD", "short", "--dry-run"], input="\n\n"
        )
        assert result.exit_code == 0, result.output
        assert "SL: 1.10740" in result.output

    def test_dash_means_no_stop(
        self, trade_db: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _use_client(monkeypatch, FakeFullClient(candles=_atr_candles(50)))
        result = runner.invoke(
            app, ["trade", "EUR_USD", "long", "--dry-run"], input="\n-\n"
        )
        assert result.exit_code == 0, result.output
        assert "SL:" not in result.output

    def test_inline_multiple(
        self, trade_db: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """``2x`` = 100 pips below the ask."""
        _use_client(monkeypatch, FakeFullClient(candles=_atr_candles(50)))
        result = runner.invoke(
            app, ["trade", "EUR_USD", "long", "--dry-run"], input="\n2x\n"
        )
        assert result.exit_code == 0, result.output
        assert "SL: 1.09010" in result.output
        assert "[2× ATR]" in result.output

    def test_manual_pips_has_no_atr_tag(
        self, trade_db: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _use_client(monkeypatch, FakeFullClient(candles=_atr_candles(50)))
        result = runner.invoke(
            app, ["trade", "EUR_USD", "long", "--dry-run"], input="\n30\n"
        )
        assert result.exit_code == 0, result.output
        assert "SL: 1.09710" in result.output
        assert "× ATR]" not in result.output

    def test_config_multiple_and_period(
        self, trade_db: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """sl_atr_multiple / atr_period from config drive the default."""
        conn = get_db(path=trade_db)
        _config_all_accounts(conn, "sl_atr_multiple", "2")
        _config_all_accounts(conn, "atr_period", "20")
        conn.close()
        _use_client(monkeypatch, FakeFullClient(candles=_atr_candles(50)))
        result = runner.invoke(
            app, ["trade", "EUR_USD", "long", "--dry-run"], input="\n\n"
        )
        assert result.exit_code == 0, result.output
        assert "Daily ATR(20): 50.0p" in result.output
        assert "SL: 1.09010" in result.output

    def test_invalid_atr_config_exits_1(
        self, trade_db: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        conn = get_db(path=trade_db)
        _config_all_accounts(conn, "sl_atr_multiple", "zero")
        conn.close()
        _use_client(monkeypatch, FakeFullClient(candles=_atr_candles(50)))
        result = runner.invoke(app, ["trade", "EUR_USD", "long", "--dry-run"])
        assert result.exit_code == 1
        assert "sl_atr_multiple" in result.output

    def test_atr_unavailable_enter_skips(
        self, trade_db: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """No candles: the plan says so and Enter leaves the trade without SL."""
        _use_client(monkeypatch, FakeFullClient(candles=None))
        result = runner.invoke(
            app, ["trade", "EUR_USD", "long", "--dry-run"], input="\n\n"
        )
        assert result.exit_code == 0, result.output
        assert "Daily ATR(14): unavailable" in result.output
        assert "SL:" not in result.output

    def test_edit_reprompts_with_atr_default(
        self, trade_db: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """``e`` at the confirm prompt re-offers the ATR default for the SL."""
        fake = FakeFullClient(candles=_atr_candles(50))
        _use_client(monkeypatch, fake)
        # Plan with a 30-pip SL, edit to the ATR default, then decline.
        result = runner.invoke(
            app, ["trade", "EUR_USD", "long"], input="\n30\ne\n\n\nn\n"
        )
        assert result.exit_code == 0, result.output
        assert "SL: 1.09710" in result.output
        assert "SL: 1.09260" in result.output
        assert not fake.order_placed


def _seed_fill(db: Path, oanda_id: str = "99999") -> None:
    """Insert the fill transaction as if the post-fill sync had brought it in."""
    conn = get_db(path=db)
    conn.execute(
        "INSERT INTO transactions (oanda_id, account_id, type, time, raw_json) "
        "VALUES (?, 'acct-1', 'ORDER_FILL', '2026-09-26T12:00:00Z', '{}')",
        (oanda_id,),
    )
    conn.commit()
    conn.close()


def _plan_row(db: Path, oanda_id: str = "99999") -> dict[str, Any] | None:
    conn = get_db(path=db)
    row = conn.execute(
        "SELECT sl_price, atr_pips, sl_atr_multiple FROM trade_plans "
        "JOIN transactions ON trade_plans.transaction_id = transactions.id "
        "WHERE transactions.oanda_id = ?",
        (oanda_id,),
    ).fetchone()
    conn.close()
    return dict(row) if row else None


class TestTradeAtrPlanRecord:
    """The ATR and the stop's ATR multiple are recorded in trade_plans."""

    def test_atr_stop_recorded(
        self, trade_db: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _seed_fill(trade_db)
        fake = FakeFullClient(candles=_atr_candles(50))
        _use_client(monkeypatch, fake)
        # TP skip, SL = ATR default, confirm, note skip, tags skip.
        result = runner.invoke(app, ["trade", "EUR_USD", "long"], input="\n\ny\n\n\n")
        assert result.exit_code == 0, result.output
        assert fake.sl_attached == "1.09260"
        assert _plan_row(trade_db) == {
            "sl_price": "1.09260",
            "atr_pips": "50.0",
            "sl_atr_multiple": "1.5",
        }

    def test_manual_stop_records_atr_only(
        self, trade_db: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _seed_fill(trade_db)
        _use_client(monkeypatch, FakeFullClient(candles=_atr_candles(50)))
        result = runner.invoke(app, ["trade", "EUR_USD", "long"], input="\n30\ny\n\n\n")
        assert result.exit_code == 0, result.output
        assert _plan_row(trade_db) == {
            "sl_price": "1.09710",
            "atr_pips": "50.0",
            "sl_atr_multiple": None,
        }

    def test_no_atr_records_nulls(
        self, trade_db: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _seed_fill(trade_db)
        _use_client(monkeypatch, FakeFullClient(candles=None))
        result = runner.invoke(app, ["trade", "EUR_USD", "long"], input="\n30\ny\n\n\n")
        assert result.exit_code == 0, result.output
        assert _plan_row(trade_db) == {
            "sl_price": "1.09710",
            "atr_pips": None,
            "sl_atr_multiple": None,
        }

    def test_no_exits_saves_no_plan(
        self, trade_db: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The ATR alone isn't a plan: with no stop and no TP, no row."""
        _seed_fill(trade_db)
        _use_client(monkeypatch, FakeFullClient(candles=_atr_candles(50)))
        result = runner.invoke(app, ["trade", "EUR_USD", "long"], input="\n-\ny\n\n\n")
        assert result.exit_code == 0, result.output
        assert _plan_row(trade_db) is None


class TestTradeAtrDraft:
    """A saved draft keeps the ATR fields; --resume shows and records them."""

    @pytest.fixture()
    def plan_file(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
        path = tmp_path / "saved_plan.json"
        monkeypatch.setattr("frmj.app._DRAFT_PLAN_PATH", path)
        return path

    def test_save_then_resume(
        self, trade_db: Path, plan_file: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # First attempt: the order fails and the plan is saved.
        failing = FakeFullClient(candles=_atr_candles(50))

        def _fail(instrument: str, units_signed: int) -> Any:
            raise RuntimeError("fail")

        failing.place_market_order = _fail  # type: ignore[method-assign]
        _use_client(monkeypatch, failing)
        result = runner.invoke(app, ["trade", "EUR_USD", "long"], input="\n\ny\ns\n")
        assert result.exit_code == 0, result.output
        saved = json.loads(plan_file.read_text())
        assert saved["atr_pips"] == "50.0"
        assert saved["sl_atr_multiple"] == "1.5"

        # Resume: the stop is shown as ATR-based and recorded as such.
        _seed_fill(trade_db)
        _use_client(monkeypatch, FakeFullClient())
        result = runner.invoke(app, ["trade", "--resume"], input="y\n\n\n")
        assert result.exit_code == 0, result.output
        assert "Stop-loss:   1.09260  [1.5× ATR]" in result.output
        assert _plan_row(trade_db) == {
            "sl_price": "1.09260",
            "atr_pips": "50.0",
            "sl_atr_multiple": "1.5",
        }


class TestTradeMultiAtr:
    """``--multi``: one ATR default for the group, mirrored for --opposite."""

    @pytest.fixture()
    def multi_db(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
        """Two practice accounts ('alpha', 'beta') in group 'grp'."""
        path = tmp_path / "multi_atr.db"
        monkeypatch.setenv("FRMJ_DB_PATH", str(path))
        monkeypatch.setenv("OANDA_API_TOKEN", "test-token-123")
        conn = get_db(path=path)
        add_account(conn, "alpha", "alpha-acct", is_practice=True)
        add_account(conn, "beta", "beta-acct", is_practice=True)
        add_group_member(conn, "grp", "alpha")
        add_group_member(conn, "grp", "beta")
        _config_all_accounts(conn, "max_open_trades", "5")
        # Each account's fill, as the post-fill sync would bring it in.
        for acct_id in ("alpha-acct", "beta-acct"):
            conn.execute(
                "INSERT INTO transactions (oanda_id, account_id, type, time, "
                "raw_json) VALUES ('99999', ?, 'ORDER_FILL', "
                "'2026-09-26T12:00:00Z', '{}')",
                (acct_id,),
            )
        conn.commit()
        conn.close()
        return path

    def test_enter_sets_mirrored_atr_stops(
        self, multi_db: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # Only the first account's client fetches the shared market data.
        alpha = FakeFullClient(account_id="alpha-acct", candles=_atr_candles(50))
        beta = FakeFullClient(account_id="beta-acct")
        fakes = {"alpha": alpha, "beta": beta}
        monkeypatch.setattr(
            "frmj.cli._trade_multi.get_client_for_account",
            lambda account: fakes[account.name],
        )
        # TP skip, SL = ATR default, confirm, then note/tags skip.
        result = runner.invoke(
            app,
            ["trade", "EUR_USD", "long", "--multi", "grp", "--opposite", "beta"],
            input="\n\ny\n\n\n\n\n",
        )
        assert result.exit_code == 0, result.output
        assert "Daily ATR(14): 50.0p" in result.output
        # 75 pips below alpha's long entry (ask), above beta's short (bid).
        assert alpha.sl_attached == "1.09260"
        assert beta.sl_attached == "1.10740"
        assert result.output.count("[1.5× ATR]") == 2

        conn = get_db(path=multi_db)
        rows = conn.execute(
            "SELECT t.account_id, p.sl_price, p.atr_pips, p.sl_atr_multiple "
            "FROM trade_plans p JOIN transactions t ON p.transaction_id = t.id "
            "ORDER BY t.account_id"
        ).fetchall()
        conn.close()
        assert [tuple(r) for r in rows] == [
            ("alpha-acct", "1.09260", "50.0", "1.5"),
            ("beta-acct", "1.10740", "50.0", "1.5"),
        ]

    def test_atr_unavailable_keeps_old_prompt(
        self, multi_db: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        fakes = {
            "alpha": FakeFullClient(account_id="alpha-acct"),
            "beta": FakeFullClient(account_id="beta-acct"),
        }
        monkeypatch.setattr(
            "frmj.cli._trade_multi.get_client_for_account",
            lambda account: fakes[account.name],
        )
        result = runner.invoke(
            app,
            ["trade", "EUR_USD", "long", "--multi", "grp", "--dry-run"],
            input="\n\n",
        )
        assert result.exit_code == 0, result.output
        assert "Daily ATR(14): unavailable" in result.output
        assert "SL:" not in result.output
