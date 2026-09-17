"""Tests for ``frmj stats``."""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest
from typer.testing import CliRunner

from frmj.app import get_db, set_config
from frmj.cli import app

from .conftest import FakeClient, _row

runner = CliRunner()


def _closing_fill_json(
    instrument: str = "EUR_USD",
    units: str = "-10000",
    pl: str = "45.23",
) -> str:
    """Build a compact raw_json string for a closing ORDER_FILL."""
    import json as _json

    return _json.dumps({"instrument": instrument, "units": units, "pl": pl})


class TestStatsCommand:
    @pytest.fixture()
    def stats_db(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
        path = tmp_path / "stats_test.db"
        monkeypatch.setenv("FRMJ_DB_PATH", str(path))
        monkeypatch.setenv("OANDA_API_TOKEN", "test-token-123")
        monkeypatch.setattr(
            "frmj.cli.stats.get_client",
            lambda conn: FakeClient(account_id="acct-1", responses=[[]]),
        )
        conn = get_db(path=path)
        set_config(conn, "account_id", "acct-1")
        conn.close()
        return path

    def _seed_fills(self, path: Path, fills: list[tuple[str, str, str, str]]) -> None:
        """Seed ORDER_FILL rows: each tuple is (oanda_id, time, units, pl)."""
        conn = sqlite3.connect(str(path))
        conn.execute("PRAGMA foreign_keys = ON")
        for oanda_id, time, units, pl in fills:
            conn.execute(
                "INSERT INTO transactions (oanda_id, account_id, type, time, raw_json) "
                "VALUES (?, 'acct-1', 'ORDER_FILL', ?, ?)",
                (oanda_id, time, _closing_fill_json(units=units, pl=pl)),
            )
        conn.commit()
        conn.close()

    def test_no_closed_trades_shows_message(self, stats_db: Path) -> None:
        result = runner.invoke(app, ["stats"])
        assert result.exit_code == 0, result.output
        assert "No closed trades" in result.output

    def test_opening_fills_excluded(self, stats_db: Path) -> None:
        """ORDER_FILL rows with pl=0 (opening fills) must not count as trades."""
        conn = sqlite3.connect(str(stats_db))
        conn.execute(
            "INSERT INTO transactions (oanda_id, account_id, type, time, raw_json) "
            "VALUES ('1', 'acct-1', 'ORDER_FILL', '2026-04-25T09:00:00Z', "
            '\'{"instrument":"EUR_USD","units":"10000","pl":"0"}\')'
        )
        conn.commit()
        conn.close()
        result = runner.invoke(app, ["stats"])
        assert result.exit_code == 0, result.output
        assert "No closed trades" in result.output

    def test_shows_trade_count(self, stats_db: Path) -> None:
        self._seed_fills(
            stats_db,
            [
                ("1", "2026-04-25T09:00:00Z", "-10000", "30.00"),
                ("2", "2026-04-26T10:00:00Z", "-10000", "-15.00"),
            ],
        )
        result = runner.invoke(app, ["stats"])
        assert result.exit_code == 0, result.output
        assert "2 closed trades" in result.output

    def test_shows_win_rate(self, stats_db: Path) -> None:
        self._seed_fills(
            stats_db,
            [
                ("1", "2026-04-25T09:00:00Z", "-10000", "30.00"),
                ("2", "2026-04-26T10:00:00Z", "-10000", "20.00"),
                ("3", "2026-04-27T11:00:00Z", "-10000", "-10.00"),
            ],
        )
        result = runner.invoke(app, ["stats"])
        assert "Win rate" in result.output
        # 2 wins / 3 total = 66.7%
        assert "66.7%" in result.output

    def test_shows_instrument_breakdown(self, stats_db: Path) -> None:
        self._seed_fills(
            stats_db,
            [
                ("1", "2026-04-25T09:00:00Z", "-10000", "30.00"),
                ("2", "2026-04-26T10:00:00Z", "5000", "-10.00"),  # GBP_USD short close
            ],
        )
        # Override instrument for second fill
        conn = sqlite3.connect(str(stats_db))
        conn.execute(
            "UPDATE transactions SET raw_json = ? WHERE oanda_id = '2'",
            (_closing_fill_json(instrument="GBP_USD", units="5000", pl="-10.00"),),
        )
        conn.commit()
        conn.close()
        result = runner.invoke(app, ["stats"])
        assert "EUR_USD" in result.output
        assert "GBP_USD" in result.output
        assert "By instrument" in result.output

    def test_shows_weekday_breakdown(self, stats_db: Path) -> None:
        # 2026-04-27 is Monday
        self._seed_fills(
            stats_db,
            [
                ("1", "2026-04-27T09:00:00Z", "-10000", "25.00"),
            ],
        )
        result = runner.invoke(app, ["stats"])
        assert "By weekday" in result.output
        assert "Mon" in result.output

    def test_shows_hour_breakdown(self, stats_db: Path) -> None:
        # The hour bucket is rendered in the system local timezone, so we
        # only assert on the section header rather than a specific hour.
        self._seed_fills(
            stats_db,
            [
                ("1", "2026-04-25T09:30:00Z", "-10000", "15.00"),
            ],
        )
        result = runner.invoke(app, ["stats"])
        assert "By hour (local)" in result.output

    def test_total_pl_shown(self, stats_db: Path) -> None:
        self._seed_fills(
            stats_db,
            [
                ("1", "2026-04-25T09:00:00Z", "-10000", "50.00"),
                ("2", "2026-04-26T10:00:00Z", "-10000", "-20.00"),
            ],
        )
        result = runner.invoke(app, ["stats"])
        assert "Total P/L" in result.output
        assert "30.00" in result.output

    def test_total_financing_zero_when_no_financing_data(self, stats_db: Path) -> None:
        """No DAILY_FINANCING rows synced yet -> a $0.00 line, not an omitted one."""
        self._seed_fills(
            stats_db,
            [("1", "2026-04-25T09:00:00Z", "-10000", "30.00")],
        )
        result = runner.invoke(app, ["stats"])
        assert result.exit_code == 0, result.output
        assert "Financing:" in result.output
        assert "0.00" in result.output

    def test_zero_total_pl_shows_unsigned_amount(self, stats_db: Path) -> None:
        """Two perfectly offsetting trades (total P/L = 0) exercise the
        ``_color_pl(Decimal(0))`` code path that returns plain text without
        ANSI color styling."""
        self._seed_fills(
            stats_db,
            [
                ("1", "2026-04-25T09:00:00Z", "-10000", "10.00"),
                ("2", "2026-04-26T10:00:00Z", "-10000", "-10.00"),
            ],
        )
        result = runner.invoke(app, ["stats"])
        assert result.exit_code == 0, result.output
        # total_pl=0 and avg_pl=0 both pass through _color_pl(Decimal("0")),
        # which returns plain "$0.00" without a + prefix or ANSI color codes.
        assert "$0.00" in result.output

    def test_shows_instrument_and_direction_breakdown(self, stats_db: Path) -> None:
        """When one instrument has closed trades on both sides, an extra
        'By instrument & direction' section breaks out each side."""
        self._seed_fills(
            stats_db,
            [
                ("1", "2026-04-25T09:00:00Z", "-10000", "30.00"),  # LONG close
                ("2", "2026-04-26T10:00:00Z", "10000", "-15.00"),  # SHORT close
            ],
        )
        result = runner.invoke(app, ["stats"])
        assert result.exit_code == 0, result.output
        assert "By instrument & direction" in result.output

    def test_auto_sync_ingested_count_shown(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """When auto-sync brings in new rows, the count is printed to stdout."""
        path = tmp_path / "stats_auto_sync.db"
        monkeypatch.setenv("FRMJ_DB_PATH", str(path))
        monkeypatch.setenv("OANDA_API_TOKEN", "test-token-123")
        conn = get_db(path=path)
        set_config(conn, "account_id", "acct-1")
        conn.close()
        new_row = _row("9002")
        monkeypatch.setattr(
            "frmj.cli.stats.get_client",
            lambda conn: FakeClient(account_id="acct-1", responses=[[new_row]]),
        )
        result = runner.invoke(app, ["stats"])
        assert result.exit_code == 0, result.output
        assert "[sync] +1 transactions" in result.output

    def test_auto_sync_runtime_error_shows_warning(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A missing token (RuntimeError from get_client) surfaces as a
        warning; stats still runs against existing local data."""
        path = tmp_path / "stats_no_token.db"
        monkeypatch.setenv("FRMJ_DB_PATH", str(path))
        monkeypatch.delenv("OANDA_API_TOKEN", raising=False)
        conn = get_db(path=path)
        set_config(conn, "account_id", "acct-1")
        conn.close()
        result = runner.invoke(app, ["stats"])
        assert result.exit_code == 0, result.output
        assert "[sync] Warning:" in result.output + result.stderr

    def test_auto_sync_non_runtime_error_shows_warning(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A non-RuntimeError raised during auto-sync is caught by the
        generic handler and surfaces as a warning, not a crash."""

        class ExplodingClient:
            account_id = "acct-1"

            def get_transactions_since(self, from_id: str | None = None) -> list:
                raise ValueError("unexpected sync failure")

        path = tmp_path / "stats_explode_sync.db"
        monkeypatch.setenv("FRMJ_DB_PATH", str(path))
        monkeypatch.setenv("OANDA_API_TOKEN", "test-token-123")
        conn = get_db(path=path)
        set_config(conn, "account_id", "acct-1")
        conn.close()
        monkeypatch.setattr("frmj.cli.stats.get_client", lambda conn: ExplodingClient())
        result = runner.invoke(app, ["stats"])
        assert result.exit_code == 0, result.output
        assert "[sync] Warning: sync failed" in result.output + result.stderr

    def test_malformed_transaction_row_skipped(self, stats_db: Path) -> None:
        """A row with a non-numeric ``pl`` field raises when converted to
        Decimal; it must be skipped rather than crashing the whole command."""
        conn = sqlite3.connect(str(stats_db))
        conn.execute(
            "INSERT INTO transactions (oanda_id, account_id, type, time, raw_json) "
            "VALUES ('1', 'acct-1', 'ORDER_FILL', '2026-04-25T09:00:00Z', "
            '\'{"instrument":"EUR_USD","units":"-10000","pl":"not-a-number"}\')'
        )
        conn.commit()
        conn.close()
        result = runner.invoke(app, ["stats"])
        assert result.exit_code == 0, result.output
        assert "No closed trades" in result.output

    def test_malformed_tag_row_skipped(self, stats_db: Path) -> None:
        """A tagged row with a non-numeric ``pl`` field is skipped when
        building the by-tag P/L breakdown, rather than crashing stats."""
        self._seed_fills(
            stats_db,
            [("1", "2026-04-25T09:00:00Z", "-10000", "30.00")],
        )
        conn = sqlite3.connect(str(stats_db))
        conn.execute(
            "INSERT INTO transactions (oanda_id, account_id, type, time, raw_json) "
            "VALUES ('2', 'acct-1', 'ORDER_FILL', '2026-04-26T10:00:00Z', "
            '\'{"instrument":"EUR_USD","units":"-10000","pl":"not-a-number"}\')'
        )
        conn.commit()
        bad_txn_id = conn.execute(
            "SELECT id FROM transactions WHERE oanda_id = '2'"
        ).fetchone()[0]
        conn.execute(
            "INSERT INTO tags (transaction_id, tag) VALUES (?, 'breakout')",
            (bad_txn_id,),
        )
        conn.commit()
        conn.close()
        result = runner.invoke(app, ["stats"])
        assert result.exit_code == 0, result.output
        assert "1 closed trades" in result.output

    def _insert_financing(
        self,
        path: Path,
        oanda_id: str,
        raw_json: str,
    ) -> None:
        conn = sqlite3.connect(str(path))
        conn.execute(
            "INSERT INTO transactions (oanda_id, account_id, type, time, raw_json) "
            "VALUES (?, 'acct-1', 'DAILY_FINANCING', '2026-04-25T22:00:00Z', ?)",
            (oanda_id, raw_json),
        )
        conn.commit()
        conn.close()

    def test_total_financing_shown_in_trade_summary(self, stats_db: Path) -> None:
        """The Trade summary block totals financing across all instruments,
        not just the per-instrument breakdown further down."""
        self._seed_fills(
            stats_db,
            [("1", "2026-04-25T09:00:00Z", "-10000", "30.00")],
        )
        self._insert_financing(
            stats_db,
            "500",
            '{"financing":"-0.75","positionFinancings":['
            '{"instrument":"EUR_USD","financing":"-1.25"},'
            '{"instrument":"GBP_USD","financing":"0.50"}]}',
        )
        result = runner.invoke(app, ["stats"])
        assert result.exit_code == 0, result.output
        summary_block = result.output.split("Financing by instrument")[0]
        assert "Financing:" in summary_block
        # -1.25 + 0.50 = -0.75
        assert "-$0.75" in summary_block

    def test_financing_by_instrument_section_shown(self, stats_db: Path) -> None:
        """Each DAILY_FINANCING row's positionFinancings entries are summed
        per instrument and shown under a 'Financing by instrument' section."""
        self._seed_fills(
            stats_db,
            [("1", "2026-04-25T09:00:00Z", "-10000", "30.00")],
        )
        self._insert_financing(
            stats_db,
            "500",
            '{"financing":"-0.75","positionFinancings":['
            '{"instrument":"EUR_USD","financing":"-1.25"},'
            '{"instrument":"GBP_USD","financing":"0.50"}]}',
        )
        result = runner.invoke(app, ["stats"])
        assert result.exit_code == 0, result.output
        assert "Financing by instrument" in result.output
        assert "EUR_USD" in result.output
        assert "GBP_USD" in result.output
        assert "1.25" in result.output
        assert "0.50" in result.output

    def test_financing_entries_summed_per_instrument(self, stats_db: Path) -> None:
        """Multiple positionFinancings entries for the same instrument,
        across different days, are summed."""
        self._seed_fills(
            stats_db,
            [("1", "2026-04-25T09:00:00Z", "-10000", "30.00")],
        )
        self._insert_financing(
            stats_db,
            "500",
            '{"financing":"-1.25","positionFinancings":['
            '{"instrument":"EUR_USD","financing":"-1.25"}]}',
        )
        self._insert_financing(
            stats_db,
            "501",
            '{"financing":"-0.75","positionFinancings":['
            '{"instrument":"EUR_USD","financing":"-0.75"}]}',
        )
        result = runner.invoke(app, ["stats"])
        assert result.exit_code == 0, result.output
        assert "2.00" in result.output

    def test_financing_entry_without_instrument_skipped(self, stats_db: Path) -> None:
        """A positionFinancings entry missing its 'instrument' field is
        skipped rather than producing a spurious breakdown entry."""
        self._seed_fills(
            stats_db,
            [("1", "2026-04-25T09:00:00Z", "-10000", "30.00")],
        )
        self._insert_financing(
            stats_db,
            "500",
            '{"financing":"-1.25","positionFinancings":[{"financing":"-1.25"}]}',
        )
        result = runner.invoke(app, ["stats"])
        assert result.exit_code == 0, result.output
        assert "Financing by instrument" not in result.output

    def test_financing_row_without_position_breakdown_excluded(
        self, stats_db: Path
    ) -> None:
        """A DAILY_FINANCING row with no positionFinancings entries (e.g. no
        open positions that day) contributes nothing to the breakdown."""
        self._seed_fills(
            stats_db,
            [("1", "2026-04-25T09:00:00Z", "-10000", "30.00")],
        )
        self._insert_financing(stats_db, "500", '{"financing":"0.00"}')
        result = runner.invoke(app, ["stats"])
        assert result.exit_code == 0, result.output
        assert "Financing by instrument" not in result.output

    def test_malformed_financing_row_skipped(self, stats_db: Path) -> None:
        """A positionFinancings entry with a non-numeric amount is skipped
        rather than crashing stats."""
        self._seed_fills(
            stats_db,
            [("1", "2026-04-25T09:00:00Z", "-10000", "30.00")],
        )
        self._insert_financing(
            stats_db,
            "500",
            '{"financing":"0","positionFinancings":['
            '{"instrument":"EUR_USD","financing":"not-a-number"}]}',
        )
        result = runner.invoke(app, ["stats"])
        assert result.exit_code == 0, result.output
        assert "Financing by instrument" not in result.output


# ---------------------------------------------------------------------------
# export command
# ---------------------------------------------------------------------------
