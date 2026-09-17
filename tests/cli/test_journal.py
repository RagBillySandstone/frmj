"""Tests for ``frmj note``, ``frmj tag``, and ``frmj journal``."""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path

import pytest
from typer.testing import CliRunner

from frmj.app import get_db, set_config
from frmj.cli import app
from frmj.cli._completion import _complete_txn_type
from frmj.cli.journal import (
    _attach_tags,
    _complete_oanda_id,
    _complete_tag,
    _validate_tag,
)

from .conftest import FakeClient, _row

runner = CliRunner()


def _seed_transaction(
    path: Path,
    oanda_id: str = "12345",
    account_id: str = "acct-1",
) -> None:
    """Insert one transaction row directly into the DB for test setup."""
    conn = sqlite3.connect(str(path))
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute(
        """
        INSERT INTO transactions (oanda_id, account_id, type, time, raw_json)
        VALUES (?, ?, 'ORDER_FILL', '2026-04-25T12:00:00.000000Z', '{}')
        """,
        (oanda_id, account_id),
    )
    conn.commit()
    conn.close()


class TestNoteCommand:
    @pytest.fixture()
    def note_db(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
        path = tmp_path / "note_test.db"
        monkeypatch.setenv("FRMJ_DB_PATH", str(path))
        get_db(path=path).close()  # create and apply schema
        _seed_transaction(path, oanda_id="12345")
        return path

    def test_adds_note_to_transaction(self, note_db: Path) -> None:
        result = runner.invoke(app, ["note", "12345", "Entry at weekly pivot"])
        assert result.exit_code == 0, result.output
        assert "12345" in result.output

    def test_note_persisted_in_db(self, note_db: Path) -> None:
        """The note body must appear in the notes table after the command."""
        runner.invoke(app, ["note", "12345", "My trade rationale"])
        conn = sqlite3.connect(str(note_db))
        row = conn.execute("SELECT body FROM notes LIMIT 1").fetchone()
        conn.close()
        assert row is not None
        assert row[0] == "My trade rationale"

    def test_note_on_missing_transaction_exits_1(self, note_db: Path) -> None:
        """Referencing an Oanda ID not in the local DB must exit 1."""
        result = runner.invoke(app, ["note", "99999", "This should fail"])
        assert result.exit_code == 1

    def test_multiple_notes_on_same_transaction(self, note_db: Path) -> None:
        runner.invoke(app, ["note", "12345", "First note"])
        runner.invoke(app, ["note", "12345", "Second note"])
        conn = sqlite3.connect(str(note_db))
        rows = conn.execute("SELECT body FROM notes ORDER BY id").fetchall()
        conn.close()
        assert len(rows) == 2
        assert rows[0][0] == "First note"
        assert rows[1][0] == "Second note"

    def test_complete_oanda_id_filters_by_prefix(self, note_db: Path) -> None:
        """Opens its own DB connection independently of any CLI invocation."""
        _seed_transaction(note_db, oanda_id="12399")
        assert set(_complete_oanda_id("123")) == {"12345", "12399"}
        assert _complete_oanda_id("1239") == ["12399"]
        assert _complete_oanda_id("999") == []


# ---------------------------------------------------------------------------
# tag command
# ---------------------------------------------------------------------------


class TestTagCommand:
    """Tests for ``frmj tag``."""

    @pytest.fixture()
    def tag_db(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
        path = tmp_path / "tag_test.db"
        monkeypatch.setenv("FRMJ_DB_PATH", str(path))
        get_db(path=path).close()
        _seed_transaction(path, oanda_id="99")
        return path

    def test_tag_added_to_transaction(self, tag_db: Path) -> None:
        """A valid tag is persisted and confirmation is printed."""
        result = runner.invoke(app, ["tag", "99", "breakout"])
        assert result.exit_code == 0, result.output
        assert "99" in result.output

        conn = sqlite3.connect(str(tag_db))
        row = conn.execute("SELECT tag FROM tags LIMIT 1").fetchone()
        conn.close()
        assert row is not None
        assert row[0] == "breakout"

    def test_tag_normalised_to_lowercase(self, tag_db: Path) -> None:
        """Tags are stored in lowercase regardless of input case."""
        runner.invoke(app, ["tag", "99", "Momentum"])
        conn = sqlite3.connect(str(tag_db))
        row = conn.execute("SELECT tag FROM tags LIMIT 1").fetchone()
        conn.close()
        assert row is not None
        assert row[0] == "momentum"

    def test_multiple_tags_in_one_call(self, tag_db: Path) -> None:
        """Multiple space-separated tags can be attached in one command."""
        result = runner.invoke(app, ["tag", "99", "breakout", "momentum"])
        assert result.exit_code == 0, result.output
        conn = sqlite3.connect(str(tag_db))
        rows = conn.execute("SELECT tag FROM tags ORDER BY tag").fetchall()
        conn.close()
        assert {r[0] for r in rows} == {"breakout", "momentum"}

    def test_complete_tag_filters_by_prefix(self, tag_db: Path) -> None:
        """Opens its own DB connection independently of any CLI invocation."""
        runner.invoke(app, ["tag", "99", "breakout", "momentum"])
        assert _complete_tag("bre") == ["breakout"]
        assert set(_complete_tag("")) == {"breakout", "momentum"}

    def test_complete_txn_type_filters_by_prefix(self, tag_db: Path) -> None:
        assert _complete_txn_type("order") == ["ORDER_FILL"]
        assert _complete_txn_type("ORDER") == ["ORDER_FILL"]
        assert _complete_txn_type("zzz") == []

    def test_duplicate_tag_silently_ignored(self, tag_db: Path) -> None:
        """Attaching the same tag twice leaves only one row in the DB."""
        runner.invoke(app, ["tag", "99", "breakout"])
        runner.invoke(app, ["tag", "99", "breakout"])
        conn = sqlite3.connect(str(tag_db))
        count = conn.execute("SELECT COUNT(*) FROM tags").fetchone()[0]
        conn.close()
        assert count == 1

    def test_tag_on_missing_transaction_exits_1(self, tag_db: Path) -> None:
        """Referencing an unknown Oanda ID must exit 1."""
        result = runner.invoke(app, ["tag", "99999", "breakout"])
        assert result.exit_code == 1

    def test_invalid_tag_skipped_with_warning(self, tag_db: Path) -> None:
        """A tag containing invalid characters is skipped with a warning."""
        result = runner.invoke(app, ["tag", "99", "bad tag"])
        # "bad tag" is two args here; let's test a single arg with a space via the CLI
        # Actually CLI splits on whitespace, so "bad tag" would be two args.
        # Test an invalid character in a single token instead.
        result = runner.invoke(app, ["tag", "99", "bad@tag"])
        assert "Skipped" in result.output + result.stderr

    def test_validate_tag_rejects_blank(self) -> None:
        """Whitespace-only input normalises to None (no valid tag)."""
        assert _validate_tag("   ") is None

    def test_attach_tags_swallows_insert_errors(self, tag_db: Path) -> None:
        """A DB error on one tag insert is swallowed and skipped, not raised."""

        class ExplodingConn:
            def execute(self, sql: str, params: tuple[object, ...] = ()) -> object:
                raise sqlite3.OperationalError("boom")

            def commit(self) -> None:
                pass

        attached = _attach_tags(
            ExplodingConn(), transaction_id=1, raw_tags=["breakout"]
        )
        assert attached == 0

    def test_journal_shows_tags(
        self, tag_db: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Tags appear under the transaction in journal output."""
        runner.invoke(app, ["tag", "99", "breakout"])
        monkeypatch.setattr(
            "frmj.cli.journal.get_client",
            lambda conn: FakeClient(account_id="acct-1"),
        )
        result = runner.invoke(app, ["journal"])
        assert "Tags: breakout" in result.output

    def test_journal_tag_filter(
        self, tag_db: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """--tag filter shows only transactions with that tag."""
        _seed_transaction(tag_db, oanda_id="100")
        runner.invoke(app, ["tag", "99", "breakout"])
        # oanda_id 100 has no tag
        monkeypatch.setattr(
            "frmj.cli.journal.get_client",
            lambda conn: FakeClient(account_id="acct-1"),
        )
        result = runner.invoke(app, ["journal", "--tag", "breakout"])
        assert result.exit_code == 0
        assert "#99" in result.output
        assert "#100" not in result.output

    def test_stats_by_tag_section(
        self, tag_db: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """When closed trades have tags, the stats output includes a 'By tag' section."""
        # Seed a closing ORDER_FILL with P/L.
        conn = get_db(path=tag_db)
        conn.execute(
            "INSERT INTO transactions (oanda_id, account_id, type, time, raw_json) "
            "VALUES ('200', 'acct-1', 'ORDER_FILL', '2026-04-25T10:00:00.000000Z', "
            """'{"instrument":"EUR_USD","units":"-10000","pl":"30.00"}')"""
        )
        conn.commit()
        # Tag the closing fill with "breakout".
        row = conn.execute(
            "SELECT id FROM transactions WHERE oanda_id='200'"
        ).fetchone()
        conn.execute(
            "INSERT INTO tags (transaction_id, tag) VALUES (?, 'breakout')", (row[0],)
        )
        conn.commit()
        conn.close()

        monkeypatch.setattr(
            "frmj.cli.stats.get_client", lambda conn: FakeClient(account_id="acct-1")
        )
        result = runner.invoke(app, ["stats"])
        assert result.exit_code == 0, result.output
        assert "By tag" in result.output
        assert "breakout" in result.output


# ---------------------------------------------------------------------------
# journal command
# ---------------------------------------------------------------------------


class TestJournalCommand:
    @pytest.fixture()
    def journal_db(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
        path = tmp_path / "journal_test.db"
        monkeypatch.setenv("FRMJ_DB_PATH", str(path))
        monkeypatch.setenv("OANDA_API_TOKEN", "test-token-123")
        # Auto-sync is a no-op: returns 0 new rows so existing test output is stable.
        monkeypatch.setattr(
            "frmj.cli.journal.get_client",
            lambda conn: FakeClient(account_id="acct-1", responses=[[]]),
        )
        conn = get_db(path=path)
        set_config(conn, "account_id", "acct-1")
        conn.close()
        # Seed 5 transactions with distinct IDs and times.
        conn = sqlite3.connect(str(path))
        conn.execute("PRAGMA foreign_keys = ON")
        for i in range(1, 6):
            conn.execute(
                """
                INSERT INTO transactions (oanda_id, account_id, type, time, raw_json)
                VALUES (?, 'acct-1', 'ORDER_FILL',
                        ?, '{"instrument":"EUR_USD","units":"1000"}')
                """,
                (str(1000 + i), f"2026-04-25T{10 + i:02d}:00:00.000000Z"),
            )
        conn.commit()
        conn.close()
        return path

    def test_shows_transactions(self, journal_db: Path) -> None:
        result = runner.invoke(app, ["journal"])
        assert result.exit_code == 0, result.output
        # All 5 transaction IDs should appear.
        for i in range(1, 6):
            assert str(1000 + i) in result.output

    def test_n_flag_limits_output(self, journal_db: Path) -> None:
        """``--number 2`` should show only the 2 most recent transactions."""
        result = runner.invoke(app, ["journal", "--number", "2"])
        assert result.exit_code == 0
        # The 2 most recent are 1005 and 1004 (ordered DESC by time).
        assert "1005" in result.output
        assert "1004" in result.output
        assert "1001" not in result.output

    def test_empty_db_shows_helpful_message(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        path = tmp_path / "empty.db"
        monkeypatch.setenv("FRMJ_DB_PATH", str(path))
        monkeypatch.setenv("OANDA_API_TOKEN", "test-token-123")
        monkeypatch.setattr(
            "frmj.cli.journal.get_client",
            lambda conn: FakeClient(account_id="acct-1", responses=[[]]),
        )
        get_db(path=path).close()
        result = runner.invoke(app, ["journal"])
        assert result.exit_code == 0
        assert "No transactions" in result.output

    def test_notes_appear_under_their_transaction(self, journal_db: Path) -> None:
        """A note seeded for transaction 1001 must appear indented below it."""
        conn = sqlite3.connect(str(journal_db))
        conn.execute("PRAGMA foreign_keys = ON")
        txn_id = conn.execute(
            "SELECT id FROM transactions WHERE oanda_id = '1001'"
        ).fetchone()[0]
        conn.execute(
            "INSERT INTO notes (transaction_id, body) VALUES (?, ?)",
            (txn_id, "Confirmed breakout"),
        )
        conn.commit()
        conn.close()

        result = runner.invoke(app, ["journal"])
        assert "Confirmed breakout" in result.output

    def test_instrument_direction_shown_for_order_fill(self, journal_db: Path) -> None:
        """ORDER_FILL rows must show instrument and direction parsed from JSON."""
        result = runner.invoke(app, ["journal"])
        assert "EUR_USD" in result.output
        assert "LONG" in result.output

    def test_buy_shown_for_long_opening_fill(self, journal_db: Path) -> None:
        """An opening fill with positive units shows 'BUY' alongside LONG."""
        result = runner.invoke(app, ["journal"])
        assert result.exit_code == 0, result.output
        line = next(line for line in result.output.splitlines() if "1001" in line)
        assert "EUR_USD LONG BUY" in line

    def test_sell_shown_for_short_opening_fill(self, journal_db: Path) -> None:
        """An opening fill with negative units shows 'SELL' alongside SHORT."""
        conn = sqlite3.connect(str(journal_db))
        conn.execute(
            "UPDATE transactions SET raw_json = ? WHERE oanda_id = '1001'",
            ('{"instrument":"EUR_USD","units":"-1000","reason":"MARKET_ORDER"}',),
        )
        conn.commit()
        conn.close()
        result = runner.invoke(app, ["journal"])
        assert result.exit_code == 0, result.output
        line = next(line for line in result.output.splitlines() if "1001" in line)
        assert "EUR_USD SHORT SELL" in line

    def test_fill_price_shown_for_order_fill(self, journal_db: Path) -> None:
        """An ORDER_FILL with a 'price' field shows it after the units."""
        conn = sqlite3.connect(str(journal_db))
        conn.execute(
            "UPDATE transactions SET raw_json = ? WHERE oanda_id = '1001'",
            (
                '{"instrument":"EUR_USD","units":"1000",'
                '"reason":"MARKET_ORDER","price":"1.08542"}',
            ),
        )
        conn.commit()
        conn.close()
        result = runner.invoke(app, ["journal"])
        assert result.exit_code == 0, result.output
        line = next(line for line in result.output.splitlines() if "1001" in line)
        assert "EUR_USD LONG BUY 1,000 units @ 1.08542" in line

    def test_no_price_suffix_when_price_missing(self, journal_db: Path) -> None:
        """ORDER_FILL rows without a 'price' field show no ' @ ' suffix."""
        result = runner.invoke(app, ["journal"])
        assert result.exit_code == 0, result.output
        line = next(line for line in result.output.splitlines() if "1001" in line)
        assert " @ " not in line

    def test_take_profit_fill_shows_tp_label(self, journal_db: Path) -> None:
        """An ORDER_FILL closed by a take-profit order shows 'TP', not LONG/SHORT."""
        conn = sqlite3.connect(str(journal_db))
        conn.execute(
            "UPDATE transactions SET raw_json = ? WHERE oanda_id = '1001'",
            (
                '{"instrument":"EUR_USD","units":"-1000",'
                '"reason":"TAKE_PROFIT_ORDER","pl":"12.00"}',
            ),
        )
        conn.commit()
        conn.close()
        result = runner.invoke(app, ["journal"])
        assert result.exit_code == 0, result.output
        line = next(line for line in result.output.splitlines() if "1001" in line)
        assert "EUR_USD TP" in line
        assert "LONG" not in line
        assert "SHORT" not in line
        assert "EUR_USD TP SELL" in line

    def test_stop_loss_fill_shows_sl_label(self, journal_db: Path) -> None:
        """An ORDER_FILL closed by a stop-loss order shows 'SL', not LONG/SHORT."""
        conn = sqlite3.connect(str(journal_db))
        conn.execute(
            "UPDATE transactions SET raw_json = ? WHERE oanda_id = '1001'",
            (
                '{"instrument":"EUR_USD","units":"-1000",'
                '"reason":"STOP_LOSS_ORDER","pl":"-8.00"}',
            ),
        )
        conn.commit()
        conn.close()
        result = runner.invoke(app, ["journal"])
        assert result.exit_code == 0, result.output
        line = next(line for line in result.output.splitlines() if "1001" in line)
        assert "EUR_USD SL" in line
        assert "LONG" not in line
        assert "SHORT" not in line
        assert "EUR_USD SL SELL" in line

    def test_plan_shown_under_order_fill(self, journal_db: Path) -> None:
        """A trade plan row is shown as '    Plan: TP ...  SL ...' under its fill."""
        conn = sqlite3.connect(str(journal_db))
        conn.execute("PRAGMA foreign_keys = ON")
        txn_id = conn.execute(
            "SELECT id FROM transactions WHERE oanda_id = '1001'"
        ).fetchone()[0]
        conn.execute(
            "INSERT INTO trade_plans (transaction_id, tp_price, sl_price) "
            "VALUES (?, ?, ?)",
            (txn_id, "1.10550", "1.09750"),
        )
        conn.commit()
        conn.close()

        result = runner.invoke(app, ["journal"])
        assert result.exit_code == 0, result.output
        assert "Plan:" in result.output
        assert "TP 1.10550" in result.output
        assert "SL 1.09750" in result.output

    def test_plan_not_shown_when_absent(self, journal_db: Path) -> None:
        """Transactions without a plan must not show a 'Plan:' line."""
        result = runner.invoke(app, ["journal"])
        assert "Plan:" not in result.output

    def test_auto_sync_ingested_count_shown(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """When auto-sync brings in new rows, the count is printed to stdout."""
        path = tmp_path / "auto_sync.db"
        monkeypatch.setenv("FRMJ_DB_PATH", str(path))
        monkeypatch.setenv("OANDA_API_TOKEN", "test-token-123")
        conn = get_db(path=path)
        set_config(conn, "account_id", "acct-1")
        conn.close()
        new_row = _row("9001")
        monkeypatch.setattr(
            "frmj.cli.journal.get_client",
            lambda conn: FakeClient(account_id="acct-1", responses=[[new_row]]),
        )
        result = runner.invoke(app, ["journal"])
        assert result.exit_code == 0, result.output
        assert "[sync] +1" in result.output
        assert "9001" in result.output

    def test_auto_sync_failure_still_shows_journal(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Existing journal data is displayed even when auto-sync cannot run."""
        path = tmp_path / "fail_sync.db"
        monkeypatch.setenv("FRMJ_DB_PATH", str(path))
        monkeypatch.delenv("OANDA_API_TOKEN", raising=False)
        conn = get_db(path=path)
        conn.execute(
            "INSERT INTO transactions (oanda_id, account_id, type, time, raw_json) "
            "VALUES ('777', 'acct-1', 'ORDER_FILL', '2026-04-25T12:00:00.000000Z', '{}')"
        )
        conn.commit()
        conn.close()
        result = runner.invoke(app, ["journal"])
        assert result.exit_code == 0, result.output
        assert "777" in result.output

    def test_auto_sync_non_runtime_error_shows_warning(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A non-RuntimeError raised during auto-sync is caught by the
        generic handler and surfaces as a warning, not a crash."""

        class ExplodingClient:
            account_id = "acct-1"

            def get_transactions_since(self, from_id: str | None = None) -> list:
                raise ValueError("unexpected sync failure")

        path = tmp_path / "explode_sync.db"
        monkeypatch.setenv("FRMJ_DB_PATH", str(path))
        monkeypatch.setenv("OANDA_API_TOKEN", "test-token-123")
        conn = get_db(path=path)
        set_config(conn, "account_id", "acct-1")
        conn.close()
        monkeypatch.setattr(
            "frmj.cli.journal.get_client", lambda conn: ExplodingClient()
        )
        result = runner.invoke(app, ["journal"])
        assert result.exit_code == 0, result.output
        assert "[sync] Warning: sync failed" in result.output + result.stderr

    def test_pl_shown_for_closing_order_fill(self, journal_db: Path) -> None:
        """A closing ORDER_FILL (non-zero pl) shows the realised P/L amount."""
        conn = sqlite3.connect(str(journal_db))
        conn.execute(
            "UPDATE transactions SET raw_json = ? WHERE oanda_id = '1001'",
            ('{"instrument":"EUR_USD","units":"1000","pl":"45.23"}',),
        )
        conn.commit()
        conn.close()
        result = runner.invoke(app, ["journal"])
        assert result.exit_code == 0, result.output
        assert "45.23" in result.output

    def test_pl_not_shown_for_opening_order_fill(self, journal_db: Path) -> None:
        """An opening ORDER_FILL (pl == 0) shows no P/L amount."""
        result = runner.invoke(app, ["journal"])
        assert result.exit_code == 0, result.output
        # Seeded transactions have no pl / pl="0"; no dollar amounts in output.
        assert "$" not in result.output

    def test_pl_negative_shown_for_losing_trade(self, journal_db: Path) -> None:
        """A losing closing fill (negative pl) shows the negative P/L amount."""
        conn = sqlite3.connect(str(journal_db))
        conn.execute(
            "UPDATE transactions SET raw_json = ? WHERE oanda_id = '1002'",
            ('{"instrument":"GBP_USD","units":"-2000","pl":"-18.40"}',),
        )
        conn.commit()
        conn.close()
        result = runner.invoke(app, ["journal"])
        assert result.exit_code == 0, result.output
        assert "18.40" in result.output

    def test_columns_align_across_rows_of_differing_content(
        self, journal_db: Path
    ) -> None:
        """The time column must start at the same offset on every row, even
        when the instrument/direction/units/P-L text differs in length."""
        conn = sqlite3.connect(str(journal_db))
        # Give the seeded rows deliberately mismatched extra/P-L lengths:
        # a bare fill, a long instrument+direction+units combo with P/L, and
        # a financing row with no direction/units at all.
        conn.execute(
            "UPDATE transactions SET raw_json = ? WHERE oanda_id = '1001'",
            ('{"instrument":"EUR_USD","units":"1000"}',),
        )
        conn.execute(
            "UPDATE transactions SET raw_json = ? WHERE oanda_id = '1002'",
            (
                '{"instrument":"GBP_USD","units":"-250000",'
                '"reason":"STOP_LOSS_ORDER","pl":"-1234.56"}',
            ),
        )
        conn.execute(
            "UPDATE transactions SET type = ?, raw_json = ? WHERE oanda_id = '1003'",
            ("DAILY_FINANCING", '{"instrument":"EUR_USD","amount":"-1.23"}'),
        )
        conn.commit()
        conn.close()

        result = runner.invoke(app, ["journal"])
        assert result.exit_code == 0, result.output
        lines = [
            line
            for line in result.output.splitlines()
            if line.startswith("#1001")
            or line.startswith("#1002")
            or line.startswith("#1003")
        ]
        assert len(lines) == 3
        # The trailing "YYYY-MM-DD HH:MM:SS" timestamp is a fixed-width token,
        # so if every row's time column starts at the same index the rows
        # must all be the same total length.
        assert len({len(line) for line in lines}) == 1

    def test_daily_financing_amount_shown(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """DAILY_FINANCING rows display the financing amount."""
        path = tmp_path / "fin_test.db"
        monkeypatch.setenv("FRMJ_DB_PATH", str(path))
        monkeypatch.setenv("OANDA_API_TOKEN", "test-token-123")
        monkeypatch.setattr(
            "frmj.cli.journal.get_client",
            lambda conn: FakeClient(account_id="acct-1", responses=[[]]),
        )
        conn = get_db(path=path)
        set_config(conn, "account_id", "acct-1")
        conn.execute(
            "INSERT INTO transactions (oanda_id, account_id, type, time, raw_json) "
            "VALUES ('5001', 'acct-1', 'DAILY_FINANCING', "
            "'2026-04-25T22:00:00.000000Z', "
            '\'{"financing":"-3.50"}\')'
        )
        conn.commit()
        conn.close()
        result = runner.invoke(app, ["journal"])
        assert result.exit_code == 0, result.output
        assert "3.50" in result.output

    def test_daily_financing_child_shows_instrument(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Per-instrument DAILY_FINANCING children show instrument and amount."""
        path = tmp_path / "fin_child.db"
        monkeypatch.setenv("FRMJ_DB_PATH", str(path))
        monkeypatch.setenv("OANDA_API_TOKEN", "test-token-123")
        monkeypatch.setattr(
            "frmj.cli.journal.get_client",
            lambda conn: FakeClient(account_id="acct-1", responses=[[]]),
        )
        conn = get_db(path=path)
        set_config(conn, "account_id", "acct-1")
        conn.execute(
            "INSERT INTO transactions (oanda_id, account_id, type, time, raw_json) "
            "VALUES ('5002', 'acct-1', 'DAILY_FINANCING', "
            "'2026-04-25T22:00:00.000000Z', "
            '\'{"instrument":"EUR_USD","amount":"-1.25"}\')'
        )
        conn.commit()
        conn.close()
        result = runner.invoke(app, ["journal"])
        assert result.exit_code == 0, result.output
        assert "EUR_USD" in result.output
        assert "1.25" in result.output

    def test_daily_financing_invalid_amount_handled_gracefully(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """DAILY_FINANCING rows with a non-numeric amount are displayed without
        a P/L figure — the ``except Exception: pass`` block suppresses the error."""
        path = tmp_path / "fin_bad.db"
        monkeypatch.setenv("FRMJ_DB_PATH", str(path))
        monkeypatch.setenv("OANDA_API_TOKEN", "test-token-123")
        monkeypatch.setattr(
            "frmj.cli.journal.get_client",
            lambda conn: FakeClient(account_id="acct-1", responses=[[]]),
        )
        conn = get_db(path=path)
        set_config(conn, "account_id", "acct-1")
        conn.execute(
            "INSERT INTO transactions (oanda_id, account_id, type, time, raw_json) "
            "VALUES ('5099', 'acct-1', 'DAILY_FINANCING', "
            "'2026-04-25T22:00:00.000000Z', "
            '\'{"financing":"not-a-number"}\')'
        )
        conn.commit()
        conn.close()
        result = runner.invoke(app, ["journal"])
        assert result.exit_code == 0, result.output
        # The transaction row must appear; no crash despite the bad amount.
        assert "5099" in result.output

    def test_order_fill_invalid_units_handled_gracefully(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """ORDER_FILL rows with a non-numeric ``units`` field are displayed
        without instrument/direction detail — the ``except Exception: pass``
        block suppresses the error rather than crashing journal."""
        path = tmp_path / "fill_bad.db"
        monkeypatch.setenv("FRMJ_DB_PATH", str(path))
        monkeypatch.setenv("OANDA_API_TOKEN", "test-token-123")
        monkeypatch.setattr(
            "frmj.cli.journal.get_client",
            lambda conn: FakeClient(account_id="acct-1", responses=[[]]),
        )
        conn = get_db(path=path)
        set_config(conn, "account_id", "acct-1")
        conn.execute(
            "INSERT INTO transactions (oanda_id, account_id, type, time, raw_json) "
            "VALUES ('5100', 'acct-1', 'ORDER_FILL', "
            "'2026-04-25T22:00:00.000000Z', "
            '\'{"instrument":"EUR_USD","units":"not-a-number"}\')'
        )
        conn.commit()
        conn.close()
        result = runner.invoke(app, ["journal"])
        assert result.exit_code == 0, result.output
        assert "5100" in result.output


# ---------------------------------------------------------------------------
# trade — confirmed execution path with TP/SL attachment
# ---------------------------------------------------------------------------


class TestJournalFiltering:
    """Tests for --instrument, --type, --since, and --with-notes filters."""

    @pytest.fixture()
    def filter_db(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
        path = tmp_path / "filter_test.db"
        monkeypatch.setenv("FRMJ_DB_PATH", str(path))
        monkeypatch.setenv("OANDA_API_TOKEN", "test-token-123")
        monkeypatch.setattr(
            "frmj.cli.journal.get_client",
            lambda conn: FakeClient(account_id="acct-1", responses=[[]]),
        )
        conn = get_db(path=path)
        set_config(conn, "account_id", "acct-1")
        # Three fills: EUR_USD on Apr-25, GBP_USD on Apr-26, EUR_USD on Apr-27
        rows = [
            ("101", "2026-04-25T09:00:00Z", "EUR_USD"),
            ("102", "2026-04-26T10:00:00Z", "GBP_USD"),
            ("103", "2026-04-27T11:00:00Z", "EUR_USD"),
        ]
        raw_conn = sqlite3.connect(str(path))
        raw_conn.execute("PRAGMA foreign_keys = ON")
        for oid, ts, instr in rows:
            raw_conn.execute(
                "INSERT INTO transactions (oanda_id, account_id, type, time, raw_json)"
                " VALUES (?, 'acct-1', 'ORDER_FILL', ?, ?)",
                (oid, ts, json.dumps({"instrument": instr, "units": "10000"})),
            )
        # One DAILY_FINANCING row
        raw_conn.execute(
            "INSERT INTO transactions (oanda_id, account_id, type, time, raw_json)"
            " VALUES ('200', 'acct-1', 'DAILY_FINANCING', '2026-04-25T22:00:00Z',"
            ' \'{"financing":"-1.50"}\')'
        )
        raw_conn.commit()
        raw_conn.close()
        return path

    def test_filter_by_instrument_shows_only_matching(self, filter_db: Path) -> None:
        result = runner.invoke(app, ["journal", "--instrument", "EUR_USD"])
        assert result.exit_code == 0, result.output
        assert "101" in result.output
        assert "103" in result.output
        assert "102" not in result.output  # GBP_USD row excluded

    def test_filter_by_instrument_shows_filter_label(self, filter_db: Path) -> None:
        result = runner.invoke(app, ["journal", "--instrument", "EUR_USD"])
        assert "instrument=EUR_USD" in result.output

    def test_filter_by_type_shows_only_matching(self, filter_db: Path) -> None:
        result = runner.invoke(app, ["journal", "--type", "DAILY_FINANCING"])
        assert result.exit_code == 0, result.output
        assert "200" in result.output
        assert "101" not in result.output  # ORDER_FILL excluded

    def test_filter_by_type_shows_filter_label(self, filter_db: Path) -> None:
        result = runner.invoke(app, ["journal", "--type", "ORDER_FILL"])
        assert "type=ORDER_FILL" in result.output

    def test_filter_since_excludes_earlier_rows(self, filter_db: Path) -> None:
        result = runner.invoke(app, ["journal", "--since", "2026-04-26"])
        assert result.exit_code == 0, result.output
        assert "102" in result.output
        assert "103" in result.output
        assert "101" not in result.output  # Apr-25 before cutoff

    def test_filter_since_shows_filter_label(self, filter_db: Path) -> None:
        result = runner.invoke(app, ["journal", "--since", "2026-04-26"])
        assert "since=2026-04-26" in result.output

    def test_filter_with_notes_shows_only_annotated(self, filter_db: Path) -> None:
        # Attach a note to row 102
        conn = sqlite3.connect(str(filter_db))
        txn_id = conn.execute(
            "SELECT id FROM transactions WHERE oanda_id = '102'"
        ).fetchone()[0]
        conn.execute(
            "INSERT INTO notes (transaction_id, body) VALUES (?, 'test note')",
            (txn_id,),
        )
        conn.commit()
        conn.close()

        result = runner.invoke(app, ["journal", "--with-notes"])
        assert result.exit_code == 0, result.output
        assert "102" in result.output
        assert "101" not in result.output
        assert "103" not in result.output

    def test_filter_with_notes_label_shown(self, filter_db: Path) -> None:
        result = runner.invoke(app, ["journal", "--with-notes"])
        assert "with-notes" in result.output

    def test_combined_filters(self, filter_db: Path) -> None:
        """--instrument + --since together narrow results to intersection."""
        result = runner.invoke(
            app,
            ["journal", "--instrument", "EUR_USD", "--since", "2026-04-26"],
        )
        assert result.exit_code == 0, result.output
        assert "103" in result.output  # EUR_USD on Apr-27 ✓
        assert "101" not in result.output  # EUR_USD on Apr-25 before since
        assert "102" not in result.output  # GBP_USD excluded by instrument

    def test_no_matching_rows_shows_no_transactions_message(
        self, filter_db: Path
    ) -> None:
        result = runner.invoke(app, ["journal", "--instrument", "USD_JPY"])
        assert result.exit_code == 0, result.output
        assert "No transactions" in result.output

    def test_n_still_limits_after_filter(self, filter_db: Path) -> None:
        """--number 1 with matching rows returns only the most recent match."""
        result = runner.invoke(
            app, ["journal", "--instrument", "EUR_USD", "--number", "1"]
        )
        assert result.exit_code == 0, result.output
        assert "103" in result.output  # most recent EUR_USD
        assert "101" not in result.output
