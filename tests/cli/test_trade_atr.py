"""Tests for the daily-ATR default stop-loss in ``frmj trade``."""

from __future__ import annotations

from collections.abc import Iterator
from decimal import Decimal
from typing import Any

import pytest

from frmj import services
from frmj.cli._trade_helpers import _parse_atr_multiple, _prompt_stop_loss
from frmj.domain.pricing import TPSLKind, TPSLSpec

from .conftest import FakeFullClient, _atr_candles


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
