"""Tests for the daily-ATR default stop-loss in ``frmj trade``."""

from __future__ import annotations

from decimal import Decimal

from frmj import services

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
