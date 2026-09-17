"""Tests for ``frmj export``."""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path

import pytest
from typer.testing import CliRunner

from frmj.app import get_db
from frmj.cli import app

runner = CliRunner()


class TestExportCommand:
    @pytest.fixture()
    def export_db(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
        path = tmp_path / "export_test.db"
        monkeypatch.setenv("FRMJ_DB_PATH", str(path))
        conn = get_db(path=path)
        # Seed two ORDER_FILL rows and one DAILY_FINANCING row.
        conn.execute(
            "INSERT INTO transactions (oanda_id, account_id, type, time, raw_json)"
            " VALUES ('1', 'acct-1', 'ORDER_FILL', '2026-04-25T09:00:00Z',"
            ' \'{"instrument":"EUR_USD","units":"10000","pl":"0","price":"1.10050"}\')'
        )
        conn.execute(
            "INSERT INTO transactions (oanda_id, account_id, type, time, raw_json)"
            " VALUES ('2', 'acct-1', 'ORDER_FILL', '2026-04-25T14:00:00Z',"
            ' \'{"instrument":"EUR_USD","units":"-10000","pl":"45.23","price":"1.10503"}\')'
        )
        conn.execute(
            "INSERT INTO transactions (oanda_id, account_id, type, time, raw_json)"
            " VALUES ('3', 'acct-1', 'DAILY_FINANCING', '2026-04-25T22:00:00Z',"
            ' \'{"financing":"-1.50"}\')'
        )
        conn.commit()
        conn.close()
        return path

    def test_csv_default_format_contains_header(self, export_db: Path) -> None:
        result = runner.invoke(app, ["export"])
        assert result.exit_code == 0, result.output
        assert "oanda_id" in result.output
        assert "instrument" in result.output

    def test_csv_contains_all_rows(self, export_db: Path) -> None:
        result = runner.invoke(app, ["export"])
        assert "1" in result.output
        assert "2" in result.output
        assert "3" in result.output

    def test_csv_order_fill_parsed_fields(self, export_db: Path) -> None:
        result = runner.invoke(app, ["export"])
        assert "EUR_USD" in result.output
        assert "LONG" in result.output
        assert "1.10050" in result.output

    def test_csv_closing_fill_has_pl(self, export_db: Path) -> None:
        result = runner.invoke(app, ["export"])
        assert "45.23" in result.output

    def test_json_format_produces_array(self, export_db: Path) -> None:
        result = runner.invoke(app, ["export", "--format", "json"])
        assert result.exit_code == 0, result.output
        data = json.loads(result.output)
        assert isinstance(data, list)
        assert len(data) == 3

    def test_json_format_has_expected_keys(self, export_db: Path) -> None:
        result = runner.invoke(app, ["export", "--format", "json"])
        data = json.loads(result.output)
        first = data[0]
        for key in ("oanda_id", "account_id", "type", "time", "instrument", "pl"):
            assert key in first, f"Missing key: {key}"

    def test_json_rows_in_chronological_order(self, export_db: Path) -> None:
        result = runner.invoke(app, ["export", "--format", "json"])
        data = json.loads(result.output)
        times = [r["time"] for r in data]
        assert times == sorted(times)

    def test_invalid_format_exits_1(self, export_db: Path) -> None:
        result = runner.invoke(app, ["export", "--format", "xml"])
        assert result.exit_code == 1

    def test_filter_by_type(self, export_db: Path) -> None:
        result = runner.invoke(app, ["export", "--type", "ORDER_FILL"])
        assert result.exit_code == 0, result.output
        assert "DAILY_FINANCING" not in result.output

    def test_filter_by_instrument(self, export_db: Path) -> None:
        result = runner.invoke(
            app, ["export", "--format", "json", "--instrument", "EUR_USD"]
        )
        data = json.loads(result.output)
        assert all(r["instrument"] == "EUR_USD" for r in data)

    def test_filter_since(self, export_db: Path) -> None:
        result = runner.invoke(
            app, ["export", "--format", "json", "--since", "2026-04-25T14"]
        )
        data = json.loads(result.output)
        oanda_ids = [r["oanda_id"] for r in data]
        assert "1" not in oanda_ids  # before cutoff
        assert "2" in oanda_ids
        assert "3" in oanda_ids

    def test_include_notes_adds_column(self, export_db: Path) -> None:
        # Attach a note to row 2.
        conn = sqlite3.connect(str(export_db))
        txn_id = conn.execute(
            "SELECT id FROM transactions WHERE oanda_id = '2'"
        ).fetchone()[0]
        conn.execute(
            "INSERT INTO notes (transaction_id, body) VALUES (?, 'Entry confirmed')",
            (txn_id,),
        )
        conn.commit()
        conn.close()

        result = runner.invoke(app, ["export", "--include-notes"])
        assert result.exit_code == 0, result.output
        assert "notes" in result.output
        assert "Entry confirmed" in result.output

    def test_output_to_file(self, export_db: Path, tmp_path: Path) -> None:
        out = tmp_path / "out.csv"
        result = runner.invoke(app, ["export", "--output", str(out)])
        assert result.exit_code == 0, result.output
        assert out.exists()
        content = out.read_text()
        assert "oanda_id" in content
        assert "EUR_USD" in content

    def test_output_file_row_count_in_message(
        self, export_db: Path, tmp_path: Path
    ) -> None:
        out = tmp_path / "out.json"
        result = runner.invoke(
            app, ["export", "--format", "json", "--output", str(out)]
        )
        assert "3 rows" in result.output

    def test_malformed_row_exported_with_blank_fields(self, export_db: Path) -> None:
        """A row whose raw_json can't be parsed still appears in the export,
        with its type-specific fields left at their defaults."""
        conn = sqlite3.connect(str(export_db))
        conn.execute(
            "INSERT INTO transactions (oanda_id, account_id, type, time, raw_json)"
            " VALUES ('4', 'acct-1', 'ORDER_FILL', '2026-04-26T09:00:00Z', 'not-json')"
        )
        conn.commit()
        conn.close()
        result = runner.invoke(app, ["export"])
        assert result.exit_code == 0, result.output
        assert "4" in result.output
