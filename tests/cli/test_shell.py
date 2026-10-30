"""Tests for ``frmj shell``."""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

import click
import pytest
import typer
from typer._completion_classes import BashComplete
from typer.testing import CliRunner

from frmj.accounts import (
    add_account,
    add_group_member,
    get_default_account_name,
    in_shell_session,
    set_live_mode,
)
from frmj.app import get_db, get_shell_history_path
from frmj.cli import app
from frmj.cli.shell import (
    _complete_path,
    _make_completer,
    _prompt,
    _shell_completions,
)

from .conftest import FakeFullClient, _open_trade

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


class TestShellAccount:
    """Each shell has its own active account; the shared default is untouched."""

    @pytest.fixture()
    def funded(self, db_path: Path) -> None:
        """A second account, "funded", alongside the default "practice"."""
        conn = get_db()
        add_account(conn, "funded", "acct-2", is_practice=True)
        conn.close()

    @staticmethod
    def _default_account() -> str | None:
        conn = get_db()
        try:
            return get_default_account_name(conn)
        finally:
            conn.close()

    def test_starts_on_the_default_account(self, funded: None) -> None:
        assert "Account: practice" in _shell("status\n")

    def test_account_option_picks_the_shells_account(self, funded: None) -> None:
        result = runner.invoke(app, ["shell", "--account", "funded"], input="status\n")
        assert result.exit_code == 0, result.output
        assert "Account: funded" in result.output
        assert "frmj [funded PRACTICE]> " in result.output
        assert self._default_account() == "practice"

    def test_unknown_account_option_is_an_error(self, funded: None) -> None:
        result = runner.invoke(app, ["shell", "-a", "nope"], input="status\n")
        assert result.exit_code == 1
        assert "account 'nope' not found" in result.output

    def test_account_use_switches_only_this_shell(self, funded: None) -> None:
        output = _shell("account use funded\nstatus\naccount list\n")
        assert "Active account for this shell set to 'funded'." in output
        assert "Account: funded" in output
        assert "* funded" in output
        assert "frmj [funded PRACTICE]> " in output
        # Outside the shell, and in the next shell, the default still applies.
        assert self._default_account() == "practice"
        assert "Account: practice" in _shell("status\n")

    def test_account_use_unknown_keeps_current_account(self, funded: None) -> None:
        output = _shell("account use nope\nstatus\n")
        assert "account 'nope' not found" in output
        assert "Account: practice" in output

    def test_session_ends_with_the_shell(self, funded: None) -> None:
        _shell("account use funded\n")
        assert not in_shell_session()
        result = runner.invoke(app, ["status"])
        assert "Account: practice" in result.output

    def test_cannot_remove_the_default_from_another_account(self, funded: None) -> None:
        """Removing the shared default would leave other terminals pointing nowhere."""
        output = _shell("account use funded\naccount remove practice\n")
        assert "'practice' is the active account" in output
        assert self._default_account() == "practice"

    def test_cannot_remove_this_shells_account(self, funded: None) -> None:
        output = _shell("account use funded\naccount remove funded\n")
        assert "'funded' is the active account" in output


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

    # Completion setup, a no-op here.
    def set_completer(self, completer: object) -> None:
        pass

    def set_completer_delims(self, delims: str) -> None:
        pass

    def parse_and_bind(self, binding: str) -> None:
        pass


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


# ---------------------------------------------------------------------------
# Tab completion
# ---------------------------------------------------------------------------


def _bash_completions(line: str, monkeypatch: pytest.MonkeyPatch) -> list[str]:
    """Return what ``frmj <line><TAB>`` offers in bash.

    Reproduces the installed completion script (``frmj --install-completion``):
    bash splits the line into COMP_WORDS (with an empty last word after a
    trailing space), joins them with newlines, and runs Typer's
    ``BashComplete``; COMPREPLY is its output split on newlines. An empty
    COMPREPLY makes bash fall back to filename completion (``-o default``).
    """
    words = ["frmj", *line.split()]
    if not line or line[-1].isspace():
        words.append("")
    monkeypatch.setenv("COMP_WORDS", "\n".join(words))
    monkeypatch.setenv("COMP_CWORD", str(len(words) - 1))
    cli = typer.main.get_command(app)
    output = BashComplete(cli, {}, "frmj", "_FRMJ_COMPLETE").complete()
    return [value for value in output.split("\n") if value]


def _all_command_lines() -> list[str]:
    """Return partial lines reaching every command, argument and option.

    For every command and sub-app: the line just after its name (first
    argument or subcommand), after ``--`` (its options), and after each
    option that takes a value (that option's completer).
    """
    lines: list[str] = []

    def walk(cmd: click.Command, path: list[str]) -> None:
        prefix = " ".join(path)
        lines.append(f"{prefix} " if path else "")
        lines.append(f"{prefix} --" if path else "--")
        for param in cmd.params:
            if isinstance(param, click.Option) and not param.is_flag:
                lines.append(f"{prefix} {param.opts[0]} ".lstrip())
        if isinstance(cmd, click.Group):
            for name, sub in cmd.commands.items():
                walk(sub, path + [name])

    walk(typer.main.get_command(app), [])
    return lines


# Hand-picked partial words, on top of _all_command_lines().
_PARTIAL_LINES: list[str] = [
    "t",
    "tr",
    "acc",
    "account g",
    "account group ",
    "account group show ",
    "account use pra",
    "config get ",
    "config set risk_strategy ",
    "trade eur_u",
    "trade EUR_US",
    "trade EUR_USD ",
    "trade EUR_USD lo",
    "trade EUR_USD long --account ",
    "tstop ",
    "tstop 63",
    "tstop --account practice ",
    "close ",
    "close eu",
    "export --format j",
    "journal --instrument gbp",
    "mode ",
    "mode l",
]


class TestShellCompletion:
    @pytest.fixture()
    def completion_data(
        self, db_path: Path, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        """Give the dynamic completers something to offer.

        Open trades for ``tstop``/``close``, a saved group for ``account
        group``, and a known working directory for the filename fallback.
        """
        fake = FakeFullClient(
            open_trades=[
                _open_trade("6368", "EUR_USD"),
                _open_trade("6371", "GBP_USD"),
            ]
        )
        monkeypatch.setattr(
            "frmj.cli._completion.get_client", lambda conn, account_name=None: fake
        )
        conn = get_db()
        add_group_member(conn, "majors", "practice")
        conn.close()
        (tmp_path / "work").mkdir()
        (tmp_path / "work" / "fills.csv").write_text("")
        monkeypatch.chdir(tmp_path / "work")

    @pytest.mark.parametrize("line", _all_command_lines() + _PARTIAL_LINES)
    def test_matches_bash(
        self,
        line: str,
        completion_data: None,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """The shell offers exactly what ``frmj <line><TAB>`` offers in bash."""
        cli = typer.main.get_command(app)
        expected = _bash_completions(line, monkeypatch)
        if not expected:
            # Bash falls back to filename completion of the current word.
            incomplete = "" if not line or line[-1].isspace() else line.split()[-1]
            expected = _complete_path(incomplete)
        assert _shell_completions(cli, line) == expected

    def test_frmj_prefix_completes_the_same(self, completion_data: None) -> None:
        cli = typer.main.get_command(app)
        assert _shell_completions(cli, "frmj tr") == ["trade"]
        assert _shell_completions(cli, "frmj tstop ") == ["6368", "6371"]

    def test_dynamic_completers_run(self, completion_data: None) -> None:
        """Sanity check that the parity cases really exercise live data."""
        cli = typer.main.get_command(app)
        assert _shell_completions(cli, "tstop ") == ["6368", "6371"]
        assert _shell_completions(cli, "close g") == ["gbp_usd"]
        assert _shell_completions(cli, "account group show ") == ["majors"]

    def test_failing_completer_offers_nothing_but_files(
        self, db_path: Path, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        """A completer that raises is swallowed, as in bash."""

        def boom(*args: object, **kwargs: object) -> list[str]:
            raise RuntimeError("kaboom")

        monkeypatch.setattr(
            "click.shell_completion.ShellComplete.get_completions", boom
        )
        monkeypatch.chdir(tmp_path)
        cli = typer.main.get_command(app)
        assert _shell_completions(cli, "tr") == []


class TestCompletePath:
    def test_lists_matching_files_and_marks_directories(self, tmp_path: Path) -> None:
        (tmp_path / "fills.csv").write_text("")
        (tmp_path / "fx").mkdir()
        (tmp_path / "other.txt").write_text("")
        assert _complete_path(f"{tmp_path}/f") == [
            f"{tmp_path}/fills.csv",
            f"{tmp_path}/fx/",
        ]

    def test_hidden_files_only_when_asked_for(self, tmp_path: Path) -> None:
        (tmp_path / ".hidden").write_text("")
        (tmp_path / "shown").write_text("")
        assert _complete_path(f"{tmp_path}/") == [f"{tmp_path}/shown"]
        assert _complete_path(f"{tmp_path}/.h") == [f"{tmp_path}/.hidden"]

    def test_keeps_tilde_as_typed(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("HOME", str(tmp_path))
        (tmp_path / "Downloads").mkdir()
        assert _complete_path("~/Dow") == ["~/Downloads/"]

    def test_relative_to_working_directory(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        (tmp_path / "fills.csv").write_text("")
        monkeypatch.chdir(tmp_path)
        assert _complete_path("fi") == ["fills.csv"]

    def test_missing_directory_offers_nothing(self, tmp_path: Path) -> None:
        assert _complete_path(f"{tmp_path}/nope/x") == []


@dataclass
class FakeLineBuffer:
    """The parts of ``readline`` the completer reads: line and cursor."""

    line: str

    def get_line_buffer(self) -> str:
        return self.line

    def get_endidx(self) -> int:
        return len(self.line)


def _tab(line: str) -> list[str]:
    """Return every candidate readline would get from the completer for *line*."""
    cli = typer.main.get_command(app)
    complete = _make_completer(cli, FakeLineBuffer(line))  # type: ignore[arg-type]
    text = "" if not line or line[-1].isspace() else line.split()[-1]
    results: list[str] = []
    while (candidate := complete(text, len(results))) is not None:
        results.append(candidate)
    return results


class TestReadlineCompleter:
    def test_unique_match_gets_a_trailing_space(self, db_path: Path) -> None:
        """As in bash; Python's readline doesn't add it itself."""
        assert _tab("tr") == ["trade "]

    def test_several_matches_are_left_bare(self, db_path: Path) -> None:
        assert _tab("export --format ") == ["csv", "json"]

    def test_unique_directory_gets_no_space(
        self, db_path: Path, tmp_path: Path
    ) -> None:
        """So the next TAB continues into the directory, as in bash."""
        (tmp_path / "statements").mkdir()
        assert _tab(f"sync --csv {tmp_path}/sta") == [f"{tmp_path}/statements/"]

    def test_completes_up_to_the_cursor(self, db_path: Path) -> None:
        """Text after the cursor is ignored, as bash does."""
        buffer = FakeLineBuffer("tr status")
        buffer.get_endidx = lambda: 2  # type: ignore[method-assign]
        cli = typer.main.get_command(app)
        complete = _make_completer(cli, buffer)  # type: ignore[arg-type]
        assert complete("tr", 0) == "trade "
