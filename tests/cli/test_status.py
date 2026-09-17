"""Tests for ``frmj status``."""

from __future__ import annotations

from pathlib import Path

import pytest
from typer.testing import CliRunner

from frmj.cli import app

runner = CliRunner()


class TestStatusCommand:
    def test_status_shows_account_and_mode(self, db_path: Path) -> None:
        """status prints the active account name and mode."""
        result = runner.invoke(app, ["status"])
        assert result.exit_code == 0, result.output
        assert "Account:" in result.output
        assert "Mode:" in result.output
        assert "practice" in result.output

    def test_status_no_active_account(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """status with no active account shows guidance."""
        path = tmp_path / "empty-status.db"
        monkeypatch.setenv("FRMJ_DB_PATH", str(path))
        result = runner.invoke(app, ["status"])
        assert result.exit_code == 0
        assert "none" in result.output.lower()

    def test_status_practice_mode_shown(self, db_path: Path) -> None:
        """Default mode is PRACTICE (live mode not enabled)."""
        result = runner.invoke(app, ["status"])
        assert "PRACTICE" in result.output
