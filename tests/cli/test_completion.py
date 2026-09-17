"""Tests for shell tab-completion helpers and the completion-coverage gate."""

from __future__ import annotations

from pathlib import Path

import click
import pytest
import typer
from typer.testing import CliRunner

from frmj.cli import app
from frmj.cli._completion import (
    _complete_account_name,
    _complete_direction,
    _complete_env_type,
    _complete_instrument,
)
from frmj.cli.export import _complete_export_format

runner = CliRunner()


class TestCompletionHelpers:
    def test_complete_instrument_matches_prefix_case_insensitively(self) -> None:
        assert "eur_usd" in _complete_instrument("EUR")
        assert all(p.startswith("eur") for p in _complete_instrument("eur"))

    def test_complete_instrument_no_match_returns_empty(self) -> None:
        assert _complete_instrument("zzz") == []

    def test_complete_direction_matches_prefix(self) -> None:
        assert _complete_direction("lo") == ["long"]
        assert _complete_direction("s") == ["short"]

    def test_complete_direction_no_match_returns_empty(self) -> None:
        assert _complete_direction("x") == []

    def test_complete_env_type_matches_prefix(self) -> None:
        assert _complete_env_type("pr") == ["practice"]
        assert _complete_env_type("li") == ["live"]

    def test_complete_env_type_no_match_returns_empty(self) -> None:
        assert _complete_env_type("x") == []

    def test_complete_export_format_matches_prefix(self) -> None:
        assert _complete_export_format("cs") == ["csv"]
        assert _complete_export_format("") == ["csv", "json"]

    def test_complete_account_name_filters_by_prefix(
        self, db_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Opens its own DB connection independently of any CLI invocation."""
        monkeypatch.setattr("frmj.app.keyring.set_password", lambda s, u, p: None)
        runner.invoke(app, ["account", "add", "funded"], input="live-001\nlive\n")
        assert _complete_account_name("pra") == ["practice"]
        assert set(_complete_account_name("")) == {"practice", "funded"}


# ---------------------------------------------------------------------------
# Completion coverage gate
#
# Every string/text CLI argument or option that is drawn from a bounded set
# of values (an instrument, an account name, a config key, ...) should offer
# shell tab-completion. Rather than relying on someone remembering to wire it
# up whenever a new command or parameter is added, this test walks the whole
# Typer command tree and fails on any string parameter that has neither a
# completion callback nor an explicit, commented exemption below. Adding a
# new parameter therefore forces a conscious choice: give it a completer, or
# add it to _COMPLETION_EXEMPT with a reason.
# ---------------------------------------------------------------------------

#: (command path as space-joined string, parameter name) -> reason no
#: completion is offered. Only genuinely free-form values (new names being
#: created, arbitrary text, numbers, file paths, dates) belong here.
_COMPLETION_EXEMPT: dict[tuple[str, str], str] = {
    ("sync", "interval"): "numeric, no fixed set of values",
    ("sync", "csv_path"): "arbitrary input file path",
    ("export", "output"): "arbitrary output file path",
    ("export", "since"): "free-form date, no fixed set of values",
    ("financing", "date_str"): "free-form date, no fixed set of values",
    ("note", "text"): "arbitrary free text",
    ("journal", "n"): "numeric, no fixed set of values",
    ("journal", "since"): "free-form date, no fixed set of values",
    ("account add", "name"): "new account name being created",
    ("account rename", "new_name"): "new account name being created",
}


def _iter_completable_params() -> list[tuple[str, click.Parameter]]:
    """Walk the Typer command tree, yielding every non-flag string parameter.

    Boolean flags and counters are excluded since they take no free-form
    value to complete.
    """
    root = typer.main.get_command(app)
    found: list[tuple[str, click.Parameter]] = []

    def walk(cmd: click.Command, path: list[str]) -> None:
        if isinstance(cmd, click.Group):
            for name, sub in cmd.commands.items():
                walk(sub, path + [name])
            return
        for param in cmd.params:
            is_flag_like = isinstance(param, click.Option) and (
                param.is_flag or param.count
            )
            if isinstance(param, click.Argument) or (
                isinstance(param, click.Option) and not is_flag_like
            ):
                found.append((" ".join(path), param))

    walk(root, [])
    return found


class TestCompletionCoverage:
    def test_every_bounded_param_offers_completion_or_is_exempt(self) -> None:
        missing = [
            f"{path} --{param.name}"
            if isinstance(param, click.Option)
            else f"{path} {param.name}"
            for path, param in _iter_completable_params()
            if getattr(param, "_custom_shell_complete", None) is None
            and (path, param.name) not in _COMPLETION_EXEMPT
        ]
        assert missing == [], (
            "These parameters have no tab-completion and no exemption in "
            "_COMPLETION_EXEMPT: " + ", ".join(missing)
        )

    def test_exemptions_reference_real_parameters(self) -> None:
        """Catches stale entries left behind after a command/param is renamed or removed."""
        real = {(path, param.name) for path, param in _iter_completable_params()}
        stale = set(_COMPLETION_EXEMPT) - real
        assert stale == set(), f"Stale entries in _COMPLETION_EXEMPT: {stale}"
