"""Tests for CLI commands: sync and config sub-commands.

We use typer's CliRunner to invoke commands in-process.  To avoid real network
calls we monkeypatch ``frmj.app.get_client`` to return a ``FakeClient`` (same
double used in the sync tests) seeded with whatever rows each test needs.

The ``trade`` command requires live Oanda calls (account summary, price, etc.)
and interactive prompts — it is not unit-tested here.  Testing it requires an
integration environment; see the project README for how to run integration tests.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

import pytest
from typer.testing import CliRunner

from frmj.app import get_db, set_config
from frmj.cli import app
from frmj.execution.oanda import TransactionRow

runner = CliRunner()


# ---------------------------------------------------------------------------
# Fake client reused from sync tests
# ---------------------------------------------------------------------------


@dataclass
class FakeClient:
    account_id: str
    responses: list[list[TransactionRow]] = field(default_factory=list)

    def get_transactions_since(
        self, from_id: str | None = None
    ) -> list[TransactionRow]:
        if not self.responses:
            return []
        return self.responses.pop(0)


def _row(oanda_id: str, account_id: str = "acct-1") -> TransactionRow:
    return TransactionRow(
        oanda_id=oanda_id,
        account_id=account_id,
        type="ORDER_FILL",
        time="2026-04-25T12:00:00.000000Z",
        parent_oanda_id=None,
        raw_json="{}",
    )


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture()
def db_path(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Set FRMJ_DB_PATH to a temp location and seed account_id + OANDA_API_TOKEN."""
    path = tmp_path / "frmj_test.db"
    monkeypatch.setenv("FRMJ_DB_PATH", str(path))
    monkeypatch.setenv("OANDA_API_TOKEN", "test-token-123")
    # Seed account_id so get_client doesn't raise.
    conn = get_db(path=path)
    set_config(conn, "account_id", "acct-1")
    conn.close()
    return path


# ---------------------------------------------------------------------------
# sync command
# ---------------------------------------------------------------------------


class TestSyncCommand:
    def test_sync_incremental_success(
        self,
        db_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """``frmj sync`` exits 0 and reports ingested count."""
        rows = [_row("1"), _row("2"), _row("3")]
        monkeypatch.setattr(
            "frmj.cli.get_client",
            lambda conn: FakeClient(account_id="acct-1", responses=[rows]),
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
            "frmj.cli.get_client",
            lambda conn: FakeClient(account_id="acct-1", responses=[[]]),
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
            "frmj.cli.get_client",
            lambda conn: FakeClient(account_id="acct-1", responses=[[]]),
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
            "frmj.cli.get_client",
            lambda conn: FakeClient(account_id="acct-1", responses=[rows]),
        )
        result = runner.invoke(app, ["sync"])
        assert result.exit_code == 0
        assert "42" in result.output

    def test_sync_exits_1_on_missing_token(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Missing OANDA_API_TOKEN → exit code 1, error message."""
        path = tmp_path / "no_token.db"
        monkeypatch.setenv("FRMJ_DB_PATH", str(path))
        monkeypatch.delenv("OANDA_API_TOKEN", raising=False)
        result = runner.invoke(app, ["sync"])
        assert result.exit_code == 1
        assert "OANDA_API_TOKEN" in result.stderr


# ---------------------------------------------------------------------------
# config sub-commands
# ---------------------------------------------------------------------------


class TestConfigCommands:
    def test_config_set_and_get_roundtrip(
        self, db_path: Path
    ) -> None:
        """``frmj config set`` writes, ``frmj config get`` reads back."""
        set_result = runner.invoke(app, ["config", "set", "max_open_trades", "6"])
        assert set_result.exit_code == 0, set_result.output
        assert "max_open_trades" in set_result.output
        assert "6" in set_result.output

        get_result = runner.invoke(app, ["config", "get", "max_open_trades"])
        assert get_result.exit_code == 0, get_result.output
        assert get_result.output.strip() == "6"

    def test_config_set_overwrites_existing_value(
        self, db_path: Path
    ) -> None:
        runner.invoke(app, ["config", "set", "scale_in", "warn"])
        runner.invoke(app, ["config", "set", "scale_in", "allow"])
        result = runner.invoke(app, ["config", "get", "scale_in"])
        assert result.output.strip() == "allow"

    def test_config_get_missing_key_exits_1(self, db_path: Path) -> None:
        result = runner.invoke(app, ["config", "get", "nonexistent_key"])
        assert result.exit_code == 1
        assert "not set" in result.output

    def test_config_set_multiple_keys(self, db_path: Path) -> None:
        """Multiple independent keys can be set without interference."""
        for key, val in [("k1", "v1"), ("k2", "v2"), ("k3", "v3")]:
            runner.invoke(app, ["config", "set", key, val])
        for key, val in [("k1", "v1"), ("k2", "v2"), ("k3", "v3")]:
            result = runner.invoke(app, ["config", "get", key])
            assert result.output.strip() == val
