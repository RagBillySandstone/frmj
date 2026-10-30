"""Tests for ``frmj shell``."""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

import pytest
from typer.testing import CliRunner

from frmj.accounts import set_live_mode
from frmj.app import get_db, get_shell_history_path
from frmj.cli import app
from frmj.cli.shell import _prompt

runner = CliRunner()


def _shell(stdin: str) -> str:
    """Run ``frmj shell`` with *stdin* as the typed lines; return its output.

    Asserts the shell itself always exits cleanly, whatever the commands did.
    """
    result = runner.invoke(app, ["shell"], input=stdin)
    assert result.exit_code == 0, result.output
    return result.output


class TestShellLoop:
    def test_runs_command_without_prefix(self, db_path: Path) -> None:
        output = _shell("status\nexit\n")
        assert "Account: practice" in output

    def test_frmj_prefix_is_accepted(self, db_path: Path) -> None:
        assert "Account: practice" in _shell("frmj status\n")

    def test_eof_exits_cleanly(self, db_path: Path) -> None:
        """Ctrl+D (end of input) leaves the shell with exit code 0."""
        _shell("")

    def test_exit_stops_reading_lines(self, db_path: Path) -> None:
        assert "Account:" not in _shell("quit\nstatus\n")

    def test_blank_line_is_ignored(self, db_path: Path) -> None:
        assert "Account: practice" in _shell("\n   \nstatus\n")

    def test_unknown_command_reports_and_continues(self, db_path: Path) -> None:
        output = _shell("bogus\nstatus\n")
        assert "No such command 'bogus'" in output
        assert "Account: practice" in output

    def test_missing_argument_reports_and_continues(self, db_path: Path) -> None:
        output = _shell("tstop\nstatus\n")
        assert "Missing argument" in output
        assert "Account: practice" in output

    def test_unclosed_quote_reports_and_continues(self, db_path: Path) -> None:
        output = _shell('note 1 "oops\nstatus\n')
        assert "No closing quotation" in output
        assert "Account: practice" in output

    def test_help_shows_top_level_commands(self, db_path: Path) -> None:
        output = _shell("help\n")
        assert "trade" in output and "positions" in output

    def test_help_command_shows_that_commands_help(self, db_path: Path) -> None:
        assert "TRADE_ID" in _shell("help tstop\n")

    def test_nested_shell_is_refused(self, db_path: Path) -> None:
        assert "Already in the frmj shell" in _shell("shell\nstatus\n")

    def test_command_exception_does_not_end_shell(
        self, db_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A crashing command prints its traceback; the next line still runs."""

        def boom(conn: object) -> None:
            raise RuntimeError("kaboom")

        monkeypatch.setattr("frmj.cli.get_active_account", boom)
        output = _shell("status\nhelp\n")
        assert "kaboom" in output
        assert "positions" in output

    def test_ctrl_c_in_command_returns_to_prompt(
        self, db_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        def interrupted(conn: object) -> None:
            raise KeyboardInterrupt

        monkeypatch.setattr("frmj.cli.get_active_account", interrupted)
        output = _shell("status\nhelp\n")
        assert "Aborted." in output
        assert "positions" in output


class TestShellPrompt:
    def test_prompt_shows_account_and_practice(self, db_path: Path) -> None:
        assert _prompt(color=False) == "frmj [practice PRACTICE]> "

    def test_prompt_follows_live_mode(self, db_path: Path) -> None:
        conn = get_db()
        set_live_mode(conn, enabled=True)
        conn.close()
        assert _prompt(color=False) == "frmj [practice LIVE]> "
        # Colour codes are wrapped as zero-width for readline.
        assert "\001\033[1;31m\002LIVE\001\033[0m\002" in _prompt(color=True)


@dataclass
class FakeReadline:
    """Records the calls ``frmj shell`` makes on the ``readline`` module."""

    history: list[str] = field(default_factory=list)
    auto_history: bool = True
    read_from: str | None = None
    written_to: str | None = None

    def set_auto_history(self, enabled: bool) -> None:
        self.auto_history = enabled

    def read_history_file(self, path: str) -> None:
        self.read_from = path

    def write_history_file(self, path: str) -> None:
        self.written_to = path

    def set_history_length(self, length: int) -> None:
        pass

    def add_history(self, line: str) -> None:
        self.history.append(line)


class TestShellHistory:
    @pytest.fixture()
    def fake_readline(self, monkeypatch: pytest.MonkeyPatch) -> FakeReadline:
        fake = FakeReadline()
        monkeypatch.setattr("frmj.cli.shell._load_readline", lambda: fake)
        return fake

    def test_history_file_is_next_to_the_database(self, db_path: Path) -> None:
        assert get_shell_history_path() == db_path.parent / "frmj_history"

    def test_history_is_loaded_and_saved(
        self, db_path: Path, fake_readline: FakeReadline
    ) -> None:
        _shell("status\n")
        history_file = str(db_path.parent / "frmj_history")
        assert fake_readline.read_from == history_file
        assert fake_readline.written_to == history_file

    def test_only_command_lines_are_recorded(
        self, db_path: Path, fake_readline: FakeReadline
    ) -> None:
        """Answers to a command's own prompts never reach the history."""
        _shell("status\n\nmode live\nnope\nexit\n")
        assert fake_readline.auto_history is False
        assert fake_readline.history == ["status", "mode live", "exit"]
