"""``frmj shell`` — run frmj commands interactively, without the ``frmj`` prefix.

Each line typed at the shell prompt is split like a bash command line and
handed to the same Typer app the ``frmj`` executable runs, so ``trade``,
``positions``, ``tstop 123 off`` etc. behave exactly as ``frmj trade`` ...
would.  Every command already opens and closes the database itself, so the
shell holds no state of its own between commands.

Nothing a command does ends the shell: usage errors are shown, ``typer.Exit``
and Ctrl+C return to the prompt, and an unexpected exception prints its
traceback and returns to the prompt.  ``exit``, ``quit`` or Ctrl+D leave.

On a terminal, line editing and history come from the stdlib ``readline``
module.  History is kept in ``frmj_history`` next to the database; only the
command lines typed at the shell prompt go into it, never the answers given
to a command's own prompts (confirmations, notes, API tokens).
"""

from __future__ import annotations

import shlex
import sys
import traceback
from types import ModuleType

import click
import typer

from frmj.accounts import get_active_account, is_live_mode
from frmj.app import get_db, get_shell_history_path
from frmj.cli import app

# Lines that leave the shell.
_EXIT_WORDS: frozenset[str] = frozenset({"exit", "quit"})
# Lines that show the top-level help (``help trade`` shows ``trade --help``).
_HELP_WORDS: frozenset[str] = frozenset({"help", "?"})
# Exit code Typer returns when a command is interrupted with Ctrl+C.
_CTRL_C_EXIT_CODE: int = 130
# Most lines kept in the history file.
_HISTORY_LENGTH: int = 1000


def _load_readline() -> ModuleType | None:
    """Return the ``readline`` module, or ``None`` when it can't be used.

    Readline only applies to an interactive terminal; with piped input (or
    on a platform without it) the shell reads plain lines with ``input()``.
    """
    if not sys.stdin.isatty():
        return None
    try:
        import readline
    except ImportError:
        return None
    return readline


def _start_history(readline: ModuleType) -> None:
    """Load the history file and stop ``input()`` recording lines itself.

    With auto-history off, only lines the shell adds explicitly (its own
    command lines) are recorded, so answers to ``typer.prompt`` — including
    hidden API tokens — never reach the history file.
    """
    readline.set_auto_history(False)
    try:
        readline.read_history_file(str(get_shell_history_path()))
    except OSError:
        # No history yet (first run) or unreadable: start empty.
        pass


def _save_history(readline: ModuleType) -> None:
    """Write the most recent ``_HISTORY_LENGTH`` lines to the history file."""
    readline.set_history_length(_HISTORY_LENGTH)
    try:
        readline.write_history_file(str(get_shell_history_path()))
    except OSError as exc:
        typer.echo(f"Warning: shell history not saved: {exc}", err=True)


def _prompt(color: bool) -> str:
    """Return the shell prompt, e.g. ``frmj [practice PRACTICE]> ``.

    Re-read on every line so ``account use`` and ``mode`` show at once.  With
    *color*, LIVE is bold red (as in ``frmj status``) and the escape codes are
    wrapped in ``\\001``/``\\002`` so readline doesn't count them towards the
    line width.
    """
    conn = get_db()
    try:
        account = get_active_account(conn)
        live = is_live_mode(conn)
    finally:
        conn.close()

    name = account.name if account is not None else "no account"
    if not live:
        return f"frmj [{name} PRACTICE]> "
    if not color:
        return f"frmj [{name} LIVE]> "
    # Bold red on, then reset; each marked as zero-width for readline.
    bold_red = "\001\033[1;31m\002"
    reset = "\001\033[0m\002"
    return f"frmj [{name} {bold_red}LIVE{reset}]> "


def _run_line(cli: click.Command, line: str) -> bool:
    """Run one shell *line*; return ``False`` when the shell should exit.

    The line is split with ``shlex`` (POSIX rules, as bash would), a leading
    ``frmj`` is dropped so muscle memory still works, and the remaining words
    go to the Click command with ``standalone_mode=False`` so it raises or
    returns instead of calling ``sys.exit``.
    """
    try:
        words = shlex.split(line)
    except ValueError as exc:
        # e.g. an unclosed quote.
        typer.echo(f"Error: {exc}", err=True)
        return True

    # Muscle memory: "frmj trade" works as well as "trade".
    if words and words[0] == "frmj":
        words = words[1:]
    if not words:
        return True

    # Shell built-ins.
    if words[0] in _EXIT_WORDS:
        return False
    if words[0] in _HELP_WORDS:
        words = words[1:] + ["--help"]
    if words[0] == "shell":
        typer.echo("Already in the frmj shell.", err=True)
        return True

    try:
        exit_code = cli.main(args=words, prog_name="frmj", standalone_mode=False)
        # Typer turns Ctrl+C inside a command into exit code 130.
        if exit_code == _CTRL_C_EXIT_CODE:
            typer.echo("\nAborted.", err=True)
    except click.ClickException as exc:
        # Usage errors: unknown command, missing argument, bad option value.
        exc.show()
    except click.exceptions.Exit:
        # typer.Exit(n): the command has already reported any error.
        pass
    except (click.Abort, KeyboardInterrupt):
        # Ctrl+D at one of the command's prompts, or Ctrl+C that Typer
        # didn't catch.
        typer.echo("Aborted.", err=True)
    except SystemExit:
        pass
    except Exception:
        # A bug in a command shouldn't end the whole session.
        traceback.print_exc()
    return True


@app.command()
def shell() -> None:
    """Run frmj commands interactively: type `trade` instead of `frmj trade`."""
    cli = typer.main.get_command(app)
    color = sys.stdout.isatty()

    readline = _load_readline()
    if readline is not None:
        _start_history(readline)

    typer.echo("frmj shell — type a command without 'frmj'; 'help', or 'exit'.")
    try:
        while True:
            try:
                line = input(_prompt(color))
            except EOFError:
                # Ctrl+D: end the prompt line before leaving.
                typer.echo("")
                break
            except KeyboardInterrupt:
                # Ctrl+C at the prompt discards the line, as in bash.
                typer.echo("")
                continue
            if readline is not None and line.strip():
                readline.add_history(line)
            if not _run_line(cli, line):
                break
    finally:
        if readline is not None:
            _save_history(readline)
