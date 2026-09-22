"""Tests for ``frmj sync`` (incremental/cold/csv) and ``frmj sync --watch``."""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest
from typer.testing import CliRunner

from frmj.accounts import add_account
from frmj.app import get_db
from frmj.cli import app

from .conftest import FakeClient, _row

runner = CliRunner()


class TestSyncCommand:
    def test_sync_incremental_success(
        self,
        db_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """``frmj sync`` exits 0 and reports ingested count."""
        rows = [_row("1"), _row("2"), _row("3")]
        monkeypatch.setattr(
            "frmj.cli.sync.get_client",
            lambda conn, account_name=None: FakeClient(
                account_id="acct-1", responses=[rows]
            ),
        )
        result = runner.invoke(app, ["sync"])
        assert result.exit_code == 0, result.output
        assert "3 ingested" in result.output

    def test_sync_cold_flag(
        self,
        db_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """``frmj sync --cold`` reports 'cold' in the output."""
        monkeypatch.setattr(
            "frmj.cli.sync.get_client",
            lambda conn, account_name=None: FakeClient(
                account_id="acct-1", responses=[[]]
            ),
        )
        result = runner.invoke(app, ["sync", "--cold"])
        assert result.exit_code == 0, result.output
        assert "cold" in result.output

    def test_sync_no_rows_reports_zero(
        self,
        db_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Empty response prints 0 ingested."""
        monkeypatch.setattr(
            "frmj.cli.sync.get_client",
            lambda conn, account_name=None: FakeClient(
                account_id="acct-1", responses=[[]]
            ),
        )
        result = runner.invoke(app, ["sync"])
        assert result.exit_code == 0
        assert "0 ingested" in result.output

    def test_sync_reports_cursor(
        self,
        db_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """The cursor transaction ID appears in the output."""
        rows = [_row("42")]
        monkeypatch.setattr(
            "frmj.cli.sync.get_client",
            lambda conn, account_name=None: FakeClient(
                account_id="acct-1", responses=[rows]
            ),
        )
        result = runner.invoke(app, ["sync"])
        assert result.exit_code == 0
        assert "42" in result.output

    def test_sync_exits_1_on_missing_account(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """No active account → exit code 1, actionable error message."""
        path = tmp_path / "no_account.db"
        monkeypatch.setenv("FRMJ_DB_PATH", str(path))
        monkeypatch.delenv("OANDA_API_TOKEN", raising=False)
        result = runner.invoke(app, ["sync"])
        assert result.exit_code == 1
        assert "No active account" in result.stderr


class TestSyncAccountOption:
    """``frmj sync --account NAME`` targets a profile other than the active one."""

    @pytest.fixture()
    def two_account_db(self, db_path: Path) -> Path:
        """``db_path`` (active: 'practice' / acct-1) plus 'other' / acct-2."""
        conn = get_db(path=db_path)
        add_account(conn, "other", "acct-2", is_practice=True)
        conn.close()
        return db_path

    def test_account_passed_to_get_client(
        self, two_account_db: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        requested: list[str | None] = []

        def _get_client(conn: object, account_name: str | None = None) -> FakeClient:
            requested.append(account_name)
            return FakeClient(account_id="acct-2", responses=[[]])

        monkeypatch.setattr("frmj.cli.sync.get_client", _get_client)
        result = runner.invoke(app, ["sync", "--account", "other"])
        assert result.exit_code == 0, result.output
        assert requested == ["other"]
        assert "Account: other" in result.output

    def test_csv_files_rows_under_named_account(
        self, two_account_db: Path, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        """CSV rows have no account ID, so --account decides where they land."""
        seen: list[str] = []

        def _fake_sync_csv(conn: object, account_id: str, csv_path: Path) -> object:
            seen.append(account_id)
            return SimpleNamespace(rows_ingested=0, rows_skipped=0, last_oanda_id=None)

        monkeypatch.setattr("frmj.cli.sync.sync_csv", _fake_sync_csv)
        csv_file = tmp_path / "export.csv"
        csv_file.write_text("")
        result = runner.invoke(
            app, ["sync", "--csv", str(csv_file), "--account", "other"]
        )
        assert result.exit_code == 0, result.output
        assert seen == ["acct-2"]

    def test_csv_unknown_account_exits_1(
        self, two_account_db: Path, tmp_path: Path
    ) -> None:
        """An unknown --account must not fall back to the active account."""
        csv_file = tmp_path / "export.csv"
        csv_file.write_text("")
        result = runner.invoke(
            app, ["sync", "--csv", str(csv_file), "--account", "ghost"]
        )
        assert result.exit_code == 1
        assert "no account named 'ghost'" in result.stderr

    def test_watch_passes_account_to_get_client(
        self, two_account_db: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        requested: list[str | None] = []

        def _get_client(conn: object, account_name: str | None = None) -> FakeClient:
            requested.append(account_name)
            return FakeClient(account_id="acct-2")

        def _interrupt(conn: object, client: object) -> None:
            raise KeyboardInterrupt

        monkeypatch.setattr("frmj.cli.sync.get_client", _get_client)
        monkeypatch.setattr("frmj.cli.sync.sync_incremental", _interrupt)
        result = runner.invoke(app, ["sync", "--watch", "--account", "other"])
        assert result.exit_code == 0, result.output
        assert requested == ["other"]


# ---------------------------------------------------------------------------
# sync --watch
# ---------------------------------------------------------------------------


class TestSyncWatch:
    """Tests for ``frmj sync --watch`` continuous polling mode."""

    def test_watch_cold_incompatible(
        self,
        db_path: Path,
    ) -> None:
        """--watch and --cold together must exit 1 with a clear error."""
        result = runner.invoke(app, ["sync", "--watch", "--cold"])
        assert result.exit_code == 1
        assert "cannot be used together" in result.output + result.stderr

    def test_watch_missing_token_exits_1(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """--watch exits 1 when OANDA_API_TOKEN is not set."""
        path = tmp_path / "no_token.db"
        monkeypatch.setenv("FRMJ_DB_PATH", str(path))
        monkeypatch.delenv("OANDA_API_TOKEN", raising=False)
        result = runner.invoke(app, ["sync", "--watch"])
        assert result.exit_code == 1

    def test_watch_exits_cleanly_on_keyboard_interrupt(
        self,
        db_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """KeyboardInterrupt causes the loop to print 'Stopped.' and exit 0."""

        def fake_sync(conn, client):
            raise KeyboardInterrupt

        monkeypatch.setattr(
            "frmj.cli.sync.get_client",
            lambda conn, account_name=None: FakeClient(account_id="acct-1"),
        )
        monkeypatch.setattr("frmj.cli.sync.sync_incremental", fake_sync)
        monkeypatch.setattr("frmj.cli.sync.time.sleep", lambda _: None)

        result = runner.invoke(app, ["sync", "--watch"])
        assert result.exit_code == 0
        assert "Stopped" in result.output

    def test_watch_header_mentions_interval(
        self,
        db_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """The opening message includes the configured interval."""

        def fake_sync(conn, client):
            raise KeyboardInterrupt

        monkeypatch.setattr(
            "frmj.cli.sync.get_client",
            lambda conn, account_name=None: FakeClient(account_id="acct-1"),
        )
        monkeypatch.setattr("frmj.cli.sync.sync_incremental", fake_sync)
        monkeypatch.setattr("frmj.cli.sync.time.sleep", lambda _: None)

        result = runner.invoke(app, ["sync", "--watch", "--interval", "30"])
        assert result.exit_code == 0
        assert "30" in result.output

    def test_watch_silent_when_no_new_transactions(
        self,
        db_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """When rows_ingested == 0, no transaction lines appear after the header."""
        # Seed a cursor so the loop is in incremental mode.
        conn = get_db(path=db_path)
        conn.execute(
            "INSERT INTO sync_cursors (account_id, last_oanda_id, synced_at) "
            "VALUES ('acct-1', '100', '2026-04-25T09:00:00.000000Z')"
        )
        conn.commit()
        conn.close()

        call_count = 0

        def fake_sync(conn, client):
            nonlocal call_count
            call_count += 1
            if call_count == 1:
                from frmj.execution.sync import SyncResult

                return SyncResult(rows_ingested=0, rows_skipped=0, last_oanda_id="100")
            raise KeyboardInterrupt

        monkeypatch.setattr(
            "frmj.cli.sync.get_client",
            lambda conn, account_name=None: FakeClient(account_id="acct-1"),
        )
        monkeypatch.setattr("frmj.cli.sync.sync_incremental", fake_sync)
        monkeypatch.setattr("frmj.cli.sync.time.sleep", lambda _: None)

        result = runner.invoke(app, ["sync", "--watch"])
        assert result.exit_code == 0
        # Only the header and "Stopped." line; no transaction rows.
        body_lines = [
            ln
            for ln in result.output.splitlines()
            if ln.strip() and "Watching" not in ln and "Stopped" not in ln
        ]
        assert body_lines == []

    def test_watch_shows_new_transactions(
        self,
        db_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """When rows_ingested > 0, newly arrived transactions are displayed."""
        # Seed: existing transaction at id=100, cursor=100.
        conn = get_db(path=db_path)
        conn.execute(
            "INSERT INTO transactions (oanda_id, account_id, type, time, raw_json) "
            "VALUES ('100', 'acct-1', 'ORDER_FILL', '2026-04-25T09:00:00.000000Z', '{}')"
        )
        conn.execute(
            "INSERT INTO sync_cursors (account_id, last_oanda_id, synced_at) "
            "VALUES ('acct-1', '100', '2026-04-25T09:00:00.000000Z')"
        )
        # Pre-insert the "new" transaction the watch loop should display.
        conn.execute(
            "INSERT INTO transactions (oanda_id, account_id, type, time, raw_json) "
            "VALUES ('101', 'acct-1', 'ORDER_FILL', '2026-04-25T10:00:00.000000Z', "
            """'{"instrument":"EUR_USD","units":"10000","pl":"25.00"}')"""
        )
        conn.commit()
        conn.close()

        call_count = 0

        def fake_sync(conn, client):
            nonlocal call_count
            call_count += 1
            if call_count == 1:
                from frmj.execution.sync import SyncResult

                return SyncResult(rows_ingested=1, rows_skipped=0, last_oanda_id="101")
            raise KeyboardInterrupt

        monkeypatch.setattr(
            "frmj.cli.sync.get_client",
            lambda conn, account_name=None: FakeClient(account_id="acct-1"),
        )
        monkeypatch.setattr("frmj.cli.sync.sync_incremental", fake_sync)
        monkeypatch.setattr("frmj.cli.sync.time.sleep", lambda _: None)

        result = runner.invoke(app, ["sync", "--watch"])
        assert result.exit_code == 0
        assert "EUR_USD" in result.output
        assert "+1 new" in result.output

    def test_watch_initial_sync_message(
        self,
        db_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """First run (no cursor) shows a count + journal hint instead of all rows."""
        call_count = 0

        def fake_sync(conn, client):
            nonlocal call_count
            call_count += 1
            if call_count == 1:
                from frmj.execution.sync import SyncResult

                return SyncResult(rows_ingested=50, rows_skipped=0, last_oanda_id="50")
            raise KeyboardInterrupt

        monkeypatch.setattr(
            "frmj.cli.sync.get_client",
            lambda conn, account_name=None: FakeClient(account_id="acct-1"),
        )
        monkeypatch.setattr("frmj.cli.sync.sync_incremental", fake_sync)
        monkeypatch.setattr("frmj.cli.sync.time.sleep", lambda _: None)

        result = runner.invoke(app, ["sync", "--watch"])
        assert result.exit_code == 0
        assert "Initial sync" in result.output
        assert "50" in result.output
        assert "frmj journal" in result.output

    def test_watch_sync_error_continues_loop(
        self,
        db_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """A sync exception is printed to stderr and the loop keeps running."""
        call_count = 0

        def fake_sync(conn, client):
            nonlocal call_count
            call_count += 1
            if call_count == 1:
                raise ValueError("network blip")
            raise KeyboardInterrupt

        monkeypatch.setattr(
            "frmj.cli.sync.get_client",
            lambda conn, account_name=None: FakeClient(account_id="acct-1"),
        )
        monkeypatch.setattr("frmj.cli.sync.sync_incremental", fake_sync)
        monkeypatch.setattr("frmj.cli.sync.time.sleep", lambda _: None)

        result = runner.invoke(app, ["sync", "--watch"])
        assert result.exit_code == 0
        assert "Sync error" in result.output + result.stderr
        assert call_count == 2  # loop continued after error

    def test_watch_custom_interval_passed_to_sleep(
        self,
        db_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """--interval N causes time.sleep(N) to be called between polls."""
        sleep_calls: list[int] = []

        call_count = 0

        def fake_sync(conn, client):
            nonlocal call_count
            call_count += 1
            if call_count == 1:
                from frmj.execution.sync import SyncResult

                return SyncResult(rows_ingested=0, rows_skipped=0, last_oanda_id="100")
            raise KeyboardInterrupt

        monkeypatch.setattr(
            "frmj.cli.sync.get_client",
            lambda conn, account_name=None: FakeClient(account_id="acct-1"),
        )
        monkeypatch.setattr("frmj.cli.sync.sync_incremental", fake_sync)
        monkeypatch.setattr("frmj.cli.sync.time.sleep", lambda s: sleep_calls.append(s))

        runner.invoke(app, ["sync", "--watch", "--interval", "30"])
        assert sleep_calls == [30]


# ---------------------------------------------------------------------------
# config sub-commands
# ---------------------------------------------------------------------------
