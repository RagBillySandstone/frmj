"""``frmj shell`` — run frmj commands interactively, without the ``frmj`` prefix.

Each line typed at the shell prompt is split like a bash command line and
handed to the same Typer app the ``frmj`` executable runs, so ``trade``,
``positions``, ``tstop 123 off`` etc. behave exactly as ``frmj trade`` ...
would.  Every command already opens and closes the database itself, so the
shell holds no state of its own between commands.

Nothing a command does ends the shell: usage errors are shown, ``typer.Exit``
and Ctrl+C return to the prompt, and an unexpected exception prints its
traceback and returns to the prompt.  ``exit``, ``quit`` or Ctrl+D leave.
"""

from __future__ import annotations

import shlex
import sys
import traceback

import click
import typer

from frmj.accounts import get_active_account, is_live_mode
from frmj.app import get_db
from frmj.cli import app

# Lines that leave the shell.
_EXIT_WORDS: frozenset[str] = frozenset({"exit", "quit"})
# Lines that show the top-level help (``help trade`` shows ``trade --help``).
_HELP_WORDS: frozenset[str] = frozenset({"help", "?"})
# Exit code Typer returns when a command is interrupted with Ctrl+C.
_CTRL_C_EXIT_CODE: int = 130


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

    typer.echo("frmj shell — type a command without 'frmj'; 'help', or 'exit'.")
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
        if not _run_line(cli, line):
            break
