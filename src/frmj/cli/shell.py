"""``frmj shell`` — run frmj commands interactively, without the ``frmj`` prefix.

Each line typed at the shell prompt is split like a bash command line and
handed to the same Typer app the ``frmj`` executable runs, so ``trade``,
``positions``, ``tstop 123 off`` etc. behave exactly as ``frmj trade`` ...
would.  Every command already opens and closes the database itself, so the
shell holds no state of its own between commands.

Each shell has its own active account, so several terminals can each work
on a different one.  A shell starts on the default account (the one ``frmj
account use`` sets outside a shell) or on ``--account NAME``; ``account use``
inside the shell switches only that shell.  Live/practice mode stays shared.

Nothing a command does ends the shell: usage errors are shown, ``typer.Exit``
and Ctrl+C return to the prompt, and an unexpected exception prints its
traceback and returns to the prompt.  ``exit``, ``quit`` or Ctrl+D leave.

On a terminal, line editing and history come from the stdlib ``readline``
module.  History is kept in ``frmj_history`` next to the database; only the
command lines typed at the shell prompt go into it, never the answers given
to a command's own prompts (confirmations, notes, API tokens).

Tab completion matches ``frmj <TAB>`` in bash.  The installed bash script
(``frmj --install-completion``) asks Click for candidates and falls back to
filename completion when there are none (``complete -o default``); the shell
asks Click the same way, with the same word splitting, so every existing
completer (instruments, trade IDs, accounts, tags, config keys, ...) works
unchanged and the same filename fallback applies (``sync --csv ~/Dow<TAB>``).
"""

from __future__ import annotations

import os
import shlex
import sys
import traceback
from collections.abc import Callable
from types import ModuleType

import click
import typer
from click.shell_completion import ShellComplete, split_arg_string

from frmj.accounts import (
    begin_shell_session,
    end_shell_session,
    get_account,
    get_active_account,
    get_default_account_name,
    is_live_mode,
)
from frmj.app import get_db, get_shell_history_path
from frmj.cli import app
from frmj.cli._completion import _complete_account_name

# Lines that leave the shell.
_EXIT_WORDS: frozenset[str] = frozenset({"exit", "quit"})
# Lines that show the top-level help (``help trade`` shows ``trade --help``).
_HELP_WORDS: frozenset[str] = frozenset({"help", "?"})
# Exit code Typer returns when a command is interrupted with Ctrl+C.
_CTRL_C_EXIT_CODE: int = 130
# Most lines kept in the history file.
_HISTORY_LENGTH: int = 1000
# Readline splits words for completion on whitespace only, as bash's
# COMP_WORDS does here, so eur_usd, --flag and paths stay whole words.
_COMPLETER_DELIMS: str = " \t\n"


def _complete_path(incomplete: str) -> list[str]:
    """Return filenames completing *incomplete*, as bash's default completion.

    The directory part is kept exactly as typed (so ``~/Dow`` completes to
    ``~/Downloads/``, not the expanded home path), directories get a trailing
    ``/``, and dotfiles are offered only when the typed name starts with ``.``.
    """
    # Split into the directory as typed and the partial name within it.
    head, _, prefix = incomplete.rpartition("/")
    if head or incomplete.startswith("/"):
        head += "/"
    directory = os.path.expanduser(head) if head else "."

    try:
        names = os.listdir(directory)
    except OSError:
        return []

    matches: list[str] = []
    for name in sorted(names):
        if not name.startswith(prefix):
            continue
        # Hidden files only when asked for, as in bash.
        if name.startswith(".") and not prefix.startswith("."):
            continue
        is_dir = os.path.isdir(os.path.join(directory, name))
        matches.append(head + name + ("/" if is_dir else ""))
    return matches


def _shell_completions(cli: click.Command, line: str) -> list[str]:
    """Return the tab-completion candidates for *line* (the text before the cursor).

    Mirrors the bash path: the line is split with Click's
    ``split_arg_string`` (as Typer's ``BashComplete`` splits ``COMP_WORDS``),
    the word under the cursor is the incomplete one (``""`` right after a
    space), and Click's ``get_completions`` produces the candidates.  When it
    has none, filenames are offered instead (bash's ``-o default``).
    """
    words = split_arg_string(line)
    # The word being completed: "" right after whitespace, else the last word.
    incomplete = words.pop() if words and not line[-1:].isspace() else ""
    # "frmj trade ..." completes the same as "trade ...".
    if words and words[0] == "frmj":
        words = words[1:]

    try:
        completer = ShellComplete(cli, {}, "frmj", "_FRMJ_COMPLETE")
        items = completer.get_completions(words, incomplete)
    except Exception:
        # As in bash, a failing completer just offers nothing.
        return []

    values = [item.value for item in items]
    return values if values else _complete_path(incomplete)


def _make_completer(
    cli: click.Command, readline: ModuleType
) -> Callable[[str, int], str | None]:
    """Return a readline completer function backed by ``_shell_completions``.

    Readline calls it with ``state`` 0, 1, 2, ... until it returns ``None``;
    the candidates are computed once, at ``state`` 0.  Python's readline
    appends nothing after a unique match, so the completer adds the space
    bash would — except after a directory, where the next TAB continues
    into it.
    """
    matches: list[str] = []

    def complete(text: str, state: int) -> str | None:
        if state == 0:
            # Complete against the line up to the cursor, as bash does.
            line = readline.get_line_buffer()[: readline.get_endidx()]
            matches[:] = _shell_completions(cli, line)
            if len(matches) == 1 and not matches[0].endswith("/"):
                matches[0] += " "
        return matches[state] if state < len(matches) else None

    return complete


def _start_completion(cli: click.Command, readline: ModuleType) -> None:
    """Bind TAB to the shell's completer."""
    readline.set_completer(_make_completer(cli, readline))
    readline.set_completer_delims(_COMPLETER_DELIMS)
    # macOS's libedit takes a different binding syntax from GNU readline.
    if "libedit" in (readline.__doc__ or ""):
        readline.parse_and_bind("bind ^I rl_complete")
    else:
        readline.parse_and_bind("tab: complete")


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

    Re-read on every line so ``account use`` and ``mode`` (which may be
    changed from another terminal) show at once.  With
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
def shell(
    account: str | None = typer.Option(
        None,
        "--account",
        "-a",
        help="Start on this account instead of the default (see 'frmj account list').",
        autocompletion=_complete_account_name,
        show_default=False,
    ),
) -> None:
    """Run frmj commands interactively: type `trade` instead of `frmj trade`.

    Each shell keeps its own active account, so several terminals can trade
    different accounts at once.
    """
    # Pin this shell's account now, so a later `frmj account use` in another
    # terminal doesn't move it.
    conn = get_db()
    try:
        if account is not None and get_account(conn, account) is None:
            typer.echo(
                f"Error: account '{account}' not found. "
                "Run 'frmj account list' to see available accounts.",
                err=True,
            )
            raise typer.Exit(1)
        start_account = account or get_default_account_name(conn)
    finally:
        conn.close()

    cli = typer.main.get_command(app)
    color = sys.stdout.isatty()

    readline = _load_readline()
    if readline is not None:
        _start_history(readline)
        _start_completion(cli, readline)

    typer.echo("frmj shell — type a command without 'frmj'; 'help', or 'exit'.")
    begin_shell_session(start_account)
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
        end_shell_session()
        if readline is not None:
            _save_history(readline)
