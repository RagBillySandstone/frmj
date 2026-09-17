"""Tests for ``frmj mode practice`` and ``frmj mode live``."""

from __future__ import annotations

from pathlib import Path

from typer.testing import CliRunner

from frmj.cli import app

runner = CliRunner()


class TestModeCommands:
    """Tests for ``frmj mode practice`` and ``frmj mode live``."""

    def test_mode_practice_sets_practice_mode(self, db_path: Path) -> None:
        """mode practice sets live_mode to false."""
        result = runner.invoke(app, ["mode", "practice"])
        assert result.exit_code == 0, result.output
        assert "PRACTICE" in result.output

    def test_mode_live_requires_exact_phrase(self, db_path: Path) -> None:
        """Entering the wrong phrase cancels live mode enablement."""
        result = runner.invoke(app, ["mode", "live"], input="wrong phrase\n")
        assert result.exit_code == 0
        assert "Cancelled" in result.output

    def test_mode_live_with_correct_phrase_enables(self, db_path: Path) -> None:
        """Entering 'ENABLE LIVE' exactly enables live mode."""
        result = runner.invoke(app, ["mode", "live"], input="ENABLE LIVE\n")
        assert result.exit_code == 0, result.output
        assert "ENABLED" in result.output

    def test_mode_live_shows_active_account_name(self, db_path: Path) -> None:
        """The live mode warning displays the current active account name."""
        result = runner.invoke(app, ["mode", "live"], input="wrong\n")
        assert "practice" in result.output  # the account name from the fixture
