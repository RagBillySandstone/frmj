"""Tests for ``frmj account`` and ``frmj account group`` sub-commands."""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest
from typer.testing import CliRunner

from frmj.cli import app
from frmj.cli._completion import _complete_account_group, _complete_group_member

runner = CliRunner()


class TestAccountCommands:
    """Tests for ``frmj account`` sub-commands."""

    def test_account_add_creates_account(
        self, db_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """account add NAME prompts for oanda_id and type then creates the profile."""
        monkeypatch.setattr("frmj.app.keyring.set_password", lambda s, u, p: None)
        # Input: oanda_id, type=practice
        result = runner.invoke(
            app, ["account", "add", "demo"], input="101-001-99999-001\npractice\n"
        )
        assert result.exit_code == 0, result.output
        assert "demo" in result.output

    def test_account_add_duplicate_name_exits_1(
        self, db_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Adding an account with a name that already exists → exit 1."""
        # "practice" already exists from db_path fixture.
        result = runner.invoke(
            app, ["account", "add", "practice"], input="acct-dup\npractice\n"
        )
        assert result.exit_code == 1
        assert "already exists" in result.output + result.stderr

    def test_account_list_shows_active_marker(self, db_path: Path) -> None:
        """account list marks the active account with '*'."""
        result = runner.invoke(app, ["account", "list"])
        assert result.exit_code == 0, result.output
        # The db_path fixture creates and activates 'practice'.
        assert "*" in result.output
        assert "practice" in result.output

    def test_account_list_empty_db_shows_message(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """account list on a DB with no accounts prints guidance."""
        path = tmp_path / "empty.db"
        monkeypatch.setenv("FRMJ_DB_PATH", str(path))
        result = runner.invoke(app, ["account", "list"])
        assert result.exit_code == 0
        assert "No accounts" in result.output

    def test_account_use_switches_active_account(
        self, db_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """account use NAME changes the active account."""
        monkeypatch.setattr("frmj.app.keyring.set_password", lambda s, u, p: None)
        # Add a second account first.
        runner.invoke(app, ["account", "add", "funded"], input="live-001\nlive\n")
        result = runner.invoke(app, ["account", "use", "funded"])
        assert result.exit_code == 0, result.output
        assert "funded" in result.output
        # Verify via current.
        current = runner.invoke(app, ["account", "current"])
        assert "funded" in current.output

    def test_account_use_nonexistent_exits_1(self, db_path: Path) -> None:
        """Switching to an account that doesn't exist → exit 1."""
        result = runner.invoke(app, ["account", "use", "ghost"])
        assert result.exit_code == 1
        assert "not found" in result.output + result.stderr

    def test_account_current_shows_active(self, db_path: Path) -> None:
        """account current prints the active account name."""
        result = runner.invoke(app, ["account", "current"])
        assert result.exit_code == 0, result.output
        assert "practice" in result.output

    def test_account_current_no_active_exits_1(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """account current with no active account → exit 1."""
        path = tmp_path / "no-active.db"
        monkeypatch.setenv("FRMJ_DB_PATH", str(path))
        result = runner.invoke(app, ["account", "current"])
        assert result.exit_code == 1

    def test_account_remove_removes_account(
        self, db_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """account remove NAME deletes the profile (when it is not active)."""
        # Add a second account; keep 'practice' active.
        monkeypatch.setattr("frmj.app.keyring.set_password", lambda s, u, p: None)
        runner.invoke(app, ["account", "add", "to-del"], input="del-001\npractice\n")
        result = runner.invoke(app, ["account", "remove", "to-del"])
        assert result.exit_code == 0, result.output
        assert "removed" in result.output

    def test_account_remove_active_account_exits_1(self, db_path: Path) -> None:
        """Removing the currently active account is refused → exit 1."""
        result = runner.invoke(app, ["account", "remove", "practice"])
        assert result.exit_code == 1
        assert "active account" in result.output + result.stderr

    def test_account_remove_nonexistent_exits_1(self, db_path: Path) -> None:
        """Removing an account that doesn't exist → exit 1."""
        result = runner.invoke(app, ["account", "remove", "ghost"])
        assert result.exit_code == 1

    def test_account_set_token_for_active(
        self, db_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """account set-token with no argument uses the active account."""
        stored: list[str] = []
        monkeypatch.setattr(
            "frmj.app.keyring.set_password",
            lambda s, u, p: stored.append(p),
        )
        result = runner.invoke(app, ["account", "set-token"], input="my-tok\n")
        assert result.exit_code == 0, result.output
        assert stored == ["my-tok"]

    def test_account_set_token_for_env_type(
        self, db_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """account set-token practice stores token under the practice keychain key."""
        stored: list[tuple[str, str]] = []
        monkeypatch.setattr(
            "frmj.app.keyring.set_password",
            lambda s, u, p: stored.append((u, p)),
        )
        result = runner.invoke(
            app, ["account", "set-token", "practice"], input="practice-tok\n"
        )
        assert result.exit_code == 0, result.output
        assert any(
            u == "oanda_api_token_practice" and tok == "practice-tok"
            for u, tok in stored
        )

    def test_account_rename_success(self, db_path: Path) -> None:
        """account rename OLD NEW exits 0 and prints a confirmation message."""
        result = runner.invoke(app, ["account", "rename", "practice", "demo"])
        assert result.exit_code == 0, result.output
        assert "practice" in result.output
        assert "demo" in result.output

    def test_account_rename_account_is_listed_under_new_name(
        self, db_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """After rename, account list shows only the new name as an account entry."""
        runner.invoke(app, ["account", "rename", "practice", "funded"])
        result = runner.invoke(app, ["account", "list"])
        assert result.exit_code == 0, result.output
        # Parse the account name from each list row (first double-space-delimited token
        # after stripping the active marker) so the type label "practice" does not
        # produce a false positive.
        listed_names = [
            line.strip().lstrip("* ").split("  ")[0]
            for line in result.output.splitlines()
            if line.strip()
        ]
        assert "funded" in listed_names
        assert "practice" not in listed_names

    def test_account_rename_active_pointer_updated(self, db_path: Path) -> None:
        """Renaming the active account updates the active pointer."""
        runner.invoke(app, ["account", "rename", "practice", "funded"])
        # account current should report the new name.
        current = runner.invoke(app, ["account", "current"])
        assert current.exit_code == 0, current.output
        assert "funded" in current.output

    def test_account_rename_old_name_not_found_exits_1(self, db_path: Path) -> None:
        """Renaming an account that doesn't exist → exit 1 with 'not found'."""
        result = runner.invoke(app, ["account", "rename", "ghost", "new"])
        assert result.exit_code == 1
        assert "not found" in result.output + result.stderr

    def test_account_rename_new_name_collision_exits_1(
        self, db_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Renaming to an existing account name → exit 1 with 'already exists'."""
        monkeypatch.setattr("frmj.app.keyring.set_password", lambda s, u, p: None)
        # Add a second account to collide with.
        runner.invoke(app, ["account", "add", "funded"], input="live-001\nlive\n")
        result = runner.invoke(app, ["account", "rename", "practice", "funded"])
        assert result.exit_code == 1
        assert "already exists" in result.output + result.stderr

    def test_account_rename_same_name_exits_1(self, db_path: Path) -> None:
        """Renaming an account to its current name → exit 1."""
        result = runner.invoke(app, ["account", "rename", "practice", "practice"])
        assert result.exit_code == 1
        assert "same" in result.output + result.stderr

    def test_account_add_empty_oanda_id_exits_1(
        self, db_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Supplying an all-whitespace Oanda account ID exits 1.

        Typer re-prompts on a bare newline (no default set), so a space is
        used — it passes the prompt but strips to empty, triggering the guard.
        """
        result = runner.invoke(app, ["account", "add", "new-acct"], input="   \n")
        assert result.exit_code == 1
        assert "cannot be empty" in result.output + result.stderr

    def test_account_add_invalid_type_exits_1(
        self, db_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Supplying an account type other than 'practice' or 'live' exits 1."""
        # Input: valid oanda_id, invalid type
        result = runner.invoke(
            app, ["account", "add", "new-acct"], input="101-001-99999-001\nfoo\n"
        )
        assert result.exit_code == 1
        assert (
            "practice" in result.output + result.stderr
            or "type" in result.output + result.stderr
        )

    def test_account_add_empty_name_exits_1(self, db_path: Path) -> None:
        """A whitespace-only account name exits 1 before any prompting."""
        result = runner.invoke(app, ["account", "add", "   "])
        assert result.exit_code == 1
        assert "cannot be empty" in result.output + result.stderr

    def test_account_add_integrity_error_exits_1(
        self, db_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A duplicate-name race at insert time (past the pre-check) surfaces
        the same 'already exists' error rather than an unhandled traceback."""
        import sqlite3

        def _raise(
            conn: object, name: str, oanda_id: str, *, is_practice: bool
        ) -> None:
            raise sqlite3.IntegrityError("UNIQUE constraint failed")

        monkeypatch.setattr("frmj.cli.accounts.add_account", _raise)
        result = runner.invoke(
            app, ["account", "add", "new-acct"], input="101-001-99999-001\npractice\n"
        )
        assert result.exit_code == 1
        assert "already exists" in result.output + result.stderr

    def test_account_add_first_account_auto_activates(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Adding an account to an empty database auto-activates it."""
        monkeypatch.setenv("FRMJ_DB_PATH", str(tmp_path / "fresh.db"))
        result = runner.invoke(
            app, ["account", "add", "first"], input="101-001-99999-001\npractice\n"
        )
        assert result.exit_code == 0, result.output
        assert "set as active" in result.output

    def test_account_add_no_token_shows_reminder(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """When no token is stored for the new account's environment, a
        reminder to run 'account set-token' is printed."""
        monkeypatch.setenv("FRMJ_DB_PATH", str(tmp_path / "fresh.db"))
        monkeypatch.delenv("OANDA_API_TOKEN", raising=False)
        result = runner.invoke(
            app, ["account", "add", "first"], input="101-001-99999-001\npractice\n"
        )
        assert result.exit_code == 0, result.output
        assert "No practice token stored" in result.output

    def test_account_set_token_invalid_env_type_exits_1(self, db_path: Path) -> None:
        """An env-type argument other than 'practice'/'live' exits 1."""
        result = runner.invoke(app, ["account", "set-token", "foo"])
        assert result.exit_code == 1
        assert "must be 'practice' or 'live'" in result.output + result.stderr

    def test_account_set_token_no_active_account_exits_1(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Calling set-token with no argument and no active account exits 1."""
        monkeypatch.setenv("FRMJ_DB_PATH", str(tmp_path / "no-active.db"))
        result = runner.invoke(app, ["account", "set-token"])
        assert result.exit_code == 1
        assert "No active account" in result.output + result.stderr

    def test_account_set_token_store_failure_exits_1(
        self, db_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A keychain-unavailable error from store_token surfaces as exit 1."""

        def _raise(token: str, *, practice: bool) -> None:
            raise RuntimeError("No keyring backend available")

        monkeypatch.setattr("frmj.cli.accounts.store_token", _raise)
        result = runner.invoke(app, ["account", "set-token"], input="my-tok\n")
        assert result.exit_code == 1
        assert "Error" in result.output + result.stderr

    def test_account_rename_empty_new_name_exits_1(self, db_path: Path) -> None:
        """A whitespace-only new name exits 1 before touching the database."""
        result = runner.invoke(app, ["account", "rename", "practice", "   "])
        assert result.exit_code == 1
        assert "cannot be empty" in result.output + result.stderr


# ---------------------------------------------------------------------------
# account group sub-commands
# ---------------------------------------------------------------------------


class TestAccountGroupCommands:
    """Tests for ``frmj account group`` sub-commands. db_path seeds 'practice'."""

    def test_add_creates_membership(self, db_path: Path) -> None:
        result = runner.invoke(app, ["account", "group", "add", "g1", "practice"])
        assert result.exit_code == 0, result.output
        show = runner.invoke(app, ["account", "group", "show", "g1"])
        assert "practice" in show.output

    def test_add_unknown_account_exits_1(self, db_path: Path) -> None:
        result = runner.invoke(app, ["account", "group", "add", "g1", "ghost"])
        assert result.exit_code == 1
        assert "not found" in result.output + result.stderr

    def test_add_duplicate_member_exits_1(self, db_path: Path) -> None:
        runner.invoke(app, ["account", "group", "add", "g1", "practice"])
        result = runner.invoke(app, ["account", "group", "add", "g1", "practice"])
        assert result.exit_code == 1
        assert "already" in result.output + result.stderr

    def test_remove_member(self, db_path: Path) -> None:
        runner.invoke(app, ["account", "group", "add", "g1", "practice"])
        result = runner.invoke(app, ["account", "group", "remove", "g1", "practice"])
        assert result.exit_code == 0, result.output
        show = runner.invoke(app, ["account", "group", "show", "g1"])
        assert show.exit_code == 1

    def test_remove_nonmember_exits_1(self, db_path: Path) -> None:
        result = runner.invoke(app, ["account", "group", "remove", "g1", "practice"])
        assert result.exit_code == 1
        assert "not in group" in result.output + result.stderr

    def test_delete_group_removes_all_members(
        self, db_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr("frmj.app.keyring.set_password", lambda s, u, p: None)
        runner.invoke(app, ["account", "add", "funded"], input="live-001\nlive\n")
        runner.invoke(app, ["account", "group", "add", "g1", "practice"])
        runner.invoke(app, ["account", "group", "add", "g1", "funded"])
        result = runner.invoke(app, ["account", "group", "delete", "g1"])
        assert result.exit_code == 0, result.output
        assert "2" in result.output
        assert (
            runner.invoke(app, ["account", "group", "list"])
            .output.strip()
            .startswith("No account groups")
        )

    def test_delete_nonexistent_group_exits_1(self, db_path: Path) -> None:
        result = runner.invoke(app, ["account", "group", "delete", "ghost"])
        assert result.exit_code == 1
        assert "not found" in result.output + result.stderr

    def test_list_shows_groups_and_members(
        self, db_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr("frmj.app.keyring.set_password", lambda s, u, p: None)
        runner.invoke(app, ["account", "add", "funded"], input="live-001\nlive\n")
        runner.invoke(app, ["account", "group", "add", "g1", "practice"])
        runner.invoke(app, ["account", "group", "add", "g1", "funded"])
        result = runner.invoke(app, ["account", "group", "list"])
        assert result.exit_code == 0, result.output
        assert "g1" in result.output
        assert "practice" in result.output
        assert "funded" in result.output

    def test_complete_account_group_filters_by_prefix(
        self, db_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The shell tab-completion callback returns only matching group names,
        opening its own DB connection independently of any CLI invocation."""
        runner.invoke(app, ["account", "group", "add", "prop-firms", "practice"])
        runner.invoke(app, ["account", "group", "add", "personal", "practice"])
        assert _complete_account_group("prop") == ["prop-firms"]
        assert set(_complete_account_group("")) == {"prop-firms", "personal"}

    def test_complete_group_member_scoped_to_group(
        self, db_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Only accounts already in the named group are offered for removal."""
        monkeypatch.setattr("frmj.app.keyring.set_password", lambda s, u, p: None)
        runner.invoke(app, ["account", "add", "funded"], input="live-001\nlive\n")
        runner.invoke(app, ["account", "group", "add", "g1", "practice"])
        ctx = SimpleNamespace(params={"group_name": "g1"})
        assert _complete_group_member(ctx, "") == ["practice"]
        assert _complete_group_member(ctx, "fun") == []

    def test_complete_group_member_unknown_group_returns_empty(
        self, db_path: Path
    ) -> None:
        ctx = SimpleNamespace(params={"group_name": "ghost"})
        assert _complete_group_member(ctx, "") == []

    def test_list_empty_shows_message(self, db_path: Path) -> None:
        result = runner.invoke(app, ["account", "group", "list"])
        assert result.exit_code == 0
        assert "No account groups" in result.output

    def test_show_unknown_group_exits_1(self, db_path: Path) -> None:
        result = runner.invoke(app, ["account", "group", "show", "ghost"])
        assert result.exit_code == 1
        assert "not found" in result.output + result.stderr
