"""Tests for ``frmj sync`` (incremental/cold/csv) and ``frmj sync --watch``."""

from __future__ import annotations

from collections.abc import Iterator
from dataclasses import dataclass, field, replace
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from typer.testing import CliRunner

from frmj.accounts import add_account
from frmj.app import get_db
from frmj.cli import app
from frmj.execution.oanda import TransactionRow

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

    def test_sync_header_names_active_account(
        self,
        db_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Without --account the output still opens with the active account."""
        monkeypatch.setattr(
            "frmj.cli.sync.get_client",
            lambda conn, account_name=None: FakeClient(
                account_id="acct-1", responses=[[]]
            ),
        )
        result = runner.invoke(app, ["sync"])
        assert result.exit_code == 0, result.output
        first_line = result.output.splitlines()[0]
        assert first_line == "Account: practice  [practice, acct-1]"

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
        assert "Account: other  [practice, acct-2]" in result.output

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

        def _get_client(conn: object, account_name: str | None = None) -> WatchClient:
            requested.append(account_name)
            return WatchClient(account_id="acct-2")

        monkeypatch.setattr("frmj.cli.sync.get_client", _get_client)
        result = runner.invoke(app, ["sync", "--watch", "--account", "other"])
        assert result.exit_code == 0, result.output
        assert requested == ["other"]


# ---------------------------------------------------------------------------
# sync --watch
# ---------------------------------------------------------------------------


@dataclass
class WatchClient(FakeClient):
    """``FakeClient`` with a scripted transaction stream, for ``--watch``.

    Each ``stream_transactions`` call plays the next script: its messages,
    then its exception if any (else the stream ends). Once the scripts run
    out it raises ``KeyboardInterrupt``, standing in for the user's Ctrl+C.
    """

    streams: list[tuple[list[dict[str, Any]], Exception | None]] = field(
        default_factory=list
    )
    # The from_id of every sync, in order.
    calls: list[str | None] = field(default_factory=list)

    def get_transactions_since(
        self, from_id: str | None = None
    ) -> list[TransactionRow]:
        self.calls.append(from_id)
        return super().get_transactions_since(from_id)

    def stream_transactions(self) -> Iterator[dict[str, Any]]:
        if not self.streams:
            raise KeyboardInterrupt
        messages, error = self.streams.pop(0)
        yield from messages
        if error is not None:
            raise error


def _seed_cursor(db_path: Path, last_oanda_id: str = "100") -> None:
    """Give acct-1 a cursor, so --watch starts in incremental mode."""
    conn = get_db(path=db_path)
    conn.execute(
        "INSERT INTO transactions (oanda_id, account_id, type, time, raw_json) "
        "VALUES (?, 'acct-1', 'ORDER_FILL', '2026-04-25T09:00:00.000000Z', '{}')",
        (last_oanda_id,),
    )
    conn.execute(
        "INSERT INTO sync_cursors (account_id, last_oanda_id, synced_at) "
        "VALUES ('acct-1', ?, '2026-04-25T09:00:00.000000Z')",
        (last_oanda_id,),
    )
    conn.commit()
    conn.close()


def _watch(monkeypatch: pytest.MonkeyPatch, client: WatchClient, *args: str) -> Any:
    """Run ``frmj sync --watch`` against *client*; waits are skipped."""
    monkeypatch.setattr(
        "frmj.cli.sync.get_client", lambda conn, account_name=None: client
    )
    waits: list[float] = []
    monkeypatch.setattr(
        "threading.Event.wait", lambda self, timeout=None: waits.append(timeout)
    )
    result = runner.invoke(app, ["sync", "--watch", *args])
    return result, waits


class TestSyncWatch:
    """Tests for ``frmj sync --watch``: streaming, with polling fallback."""

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
        self, db_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Ctrl+C prints 'Stopped.' and exits 0."""
        _seed_cursor(db_path)
        result, waits = _watch(monkeypatch, WatchClient(account_id="acct-1"))
        assert result.exit_code == 0, result.output
        assert "Stopped" in result.output

    def test_watch_header_mentions_streaming_and_interval(
        self, db_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _seed_cursor(db_path)
        result, waits = _watch(
            monkeypatch, WatchClient(account_id="acct-1"), "-i", "30"
        )
        assert "streaming" in result.output
        assert "polling every 30s" in result.output

    def test_watch_silent_on_heartbeats(
        self, db_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Heartbeats at the cursor print nothing."""
        _seed_cursor(db_path, "100")
        heartbeat = {"type": "HEARTBEAT", "lastTransactionID": "100"}
        client = WatchClient(account_id="acct-1", streams=[([heartbeat] * 3, None)])
        result, waits = _watch(monkeypatch, client)
        body = [
            ln
            for ln in result.output.splitlines()
            if ln.strip()
            and not ln.startswith("Account:")
            and "Watching" not in ln
            and "Stopped" not in ln
        ]
        assert body == []

    def test_watch_shows_streamed_transactions(
        self, db_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A streamed transaction is synced and displayed straight away."""
        _seed_cursor(db_path, "100")
        new_fill = replace(
            _row("101"),
            time="2026-04-25T10:00:00.000000Z",
            raw_json='{"instrument":"EUR_USD","units":"10000","pl":"25.00"}',
        )
        client = WatchClient(
            account_id="acct-1",
            # Catch-up sync finds nothing; the streamed fill's sync gets it.
            responses=[[], [new_fill]],
            streams=[([{"id": "101", "type": "ORDER_FILL"}], None)],
        )
        result, waits = _watch(monkeypatch, client)
        assert result.exit_code == 0, result.output
        assert "+1 new" in result.output
        assert "EUR_USD" in result.output

    def test_watch_initial_sync_message(
        self, db_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """First run (no cursor) shows a count + journal hint instead of all rows."""
        rows = [_row(str(i)) for i in range(1, 51)]
        client = WatchClient(account_id="acct-1", responses=[rows])
        result, waits = _watch(monkeypatch, client)
        assert result.exit_code == 0, result.output
        assert "Initial sync: 50 transactions" in result.output
        assert "frmj journal" in result.output
        assert "new:" not in result.output

    def test_watch_stream_error_polls_after_interval(
        self, db_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A dropped stream is reported; after --interval it syncs and reconnects."""
        _seed_cursor(db_path, "100")
        client = WatchClient(
            account_id="acct-1",
            # Second round's catch-up sync (the poll) finds a transaction.
            responses=[[], [_row("101")]],
            streams=[([], ConnectionError("stream dropped"))],
        )
        result, waits = _watch(monkeypatch, client, "--interval", "30")
        assert result.exit_code == 0, result.output
        assert "Connection error: stream dropped" in result.output + result.stderr
        assert "retrying in 30s" in result.output + result.stderr
        assert waits == [30]
        assert "+1 new" in result.output
        # Polled twice from the cursor: before each stream connection.
        assert client.calls == ["100", "100"]


# ---------------------------------------------------------------------------
# config sub-commands
# ---------------------------------------------------------------------------
