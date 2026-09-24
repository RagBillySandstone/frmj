"""Tests for ``frmj config`` (set/get/unset/check/set-token/unset-token)."""

from __future__ import annotations

from pathlib import Path

import pytest
from typer.testing import CliRunner

from frmj.cli import app
from frmj.cli.config import (
    VALID_CONFIG_KEYS,
    _complete_config_key,
    _complete_config_value,
)

from .conftest import FakeFullClient, _completion_ctx

runner = CliRunner()


class TestConfigCommands:
    def test_config_set_and_get_roundtrip(self, db_path: Path) -> None:
        """``frmj config set`` writes, ``frmj config get`` reads back."""
        set_result = runner.invoke(app, ["config", "set", "max_open_trades", "6"])
        assert set_result.exit_code == 0, set_result.output
        assert "max_open_trades" in set_result.output
        assert "6" in set_result.output

        get_result = runner.invoke(app, ["config", "get", "max_open_trades"])
        assert get_result.exit_code == 0, get_result.output
        assert get_result.output.strip() == "6"

    def test_config_set_overwrites_existing_value(self, db_path: Path) -> None:
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
        pairs = [
            ("max_open_trades", "5"),
            ("scale_in", "warn"),
            ("blocking_mode", "warning_only"),
        ]
        for key, val in pairs:
            runner.invoke(app, ["config", "set", key, val])
        for key, val in pairs:
            result = runner.invoke(app, ["config", "get", key])
            assert result.output.strip() == val

    def test_config_set_rejects_account_id(self, db_path: Path) -> None:
        """account_id is no longer a valid config key — must use 'frmj account' instead."""
        result = runner.invoke(app, ["config", "set", "account_id", "101-001"])
        assert result.exit_code == 1
        assert "not a valid config key" in result.output

    def test_complete_config_key_matches_prefix(self) -> None:
        """Tab completion suggests only valid keys starting with the prefix."""
        assert _complete_config_key("max") == ["max_open_trades"]
        assert _complete_config_key("") == sorted(VALID_CONFIG_KEYS)
        assert _complete_config_key("nonexistent") == []

    def test_complete_config_key_case_insensitive(self) -> None:
        """Completion matches regardless of the case the user typed."""
        assert _complete_config_key("SCALE") == ["scale_in"]

    def test_complete_config_value_suggests_enum_settings(self) -> None:
        """Tab completion on VALUE suggests the enum settings for the typed key."""
        ctx = _completion_ctx({"key": "blocking_mode"})
        assert _complete_config_value(ctx, "") == ["hard_block", "warning_only"]
        assert _complete_config_value(ctx, "hard") == ["hard_block"]

    def test_complete_config_value_empty_for_freeform_key(self) -> None:
        """Keys with no fixed set of settings (e.g. numeric ones) get no suggestions."""
        ctx = _completion_ctx({"key": "max_open_trades"})
        assert _complete_config_value(ctx, "") == []

    def test_complete_config_value_empty_before_key_is_typed(self) -> None:
        """With no key parsed yet, there is nothing to suggest for VALUE."""
        ctx = _completion_ctx({})
        assert _complete_config_value(ctx, "") == []

    def test_config_get_all_shows_all_keys(self, db_path: Path) -> None:
        """``frmj config get`` with no argument shows every configured key."""
        for key, val in [("max_open_trades", "6"), ("scale_in", "never")]:
            runner.invoke(app, ["config", "set", key, val])
        result = runner.invoke(app, ["config", "get"])
        assert result.exit_code == 0, result.output
        assert "max_open_trades" in result.output
        assert "6" in result.output
        assert "scale_in" in result.output
        assert "never" in result.output

    def test_config_get_all_shows_active_account(self, db_path: Path) -> None:
        """``frmj config get`` shows the active_account config key set by the fixture."""
        result = runner.invoke(app, ["config", "get"])
        assert result.exit_code == 0, result.output
        # The db_path fixture adds a practice account and activates it.
        assert "active_account" in result.output

    def test_config_get_all_shows_token_status(self, db_path: Path) -> None:
        """``frmj config get`` always prints an API token status line."""
        result = runner.invoke(app, ["config", "get"])
        assert result.exit_code == 0, result.output
        assert "API token" in result.output

    def test_config_get_all_token_not_set(
        self, db_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """When no token is configured, the status line says 'not set'."""
        monkeypatch.delenv("OANDA_API_TOKEN", raising=False)
        result = runner.invoke(app, ["config", "get"])
        assert "not set" in result.output

    def test_config_get_all_token_from_practice_env(
        self, db_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """OANDA_API_TOKEN_PRACTICE is detected and reported as its own
        source for a practice account, ahead of the legacy env var."""
        monkeypatch.setenv("OANDA_API_TOKEN_PRACTICE", "practice-tok")
        result = runner.invoke(app, ["config", "get"])
        assert result.exit_code == 0, result.output
        assert "OANDA_API_TOKEN_PRACTICE" in result.output
        assert "practice-tok" not in result.output  # value must not be printed

    def test_config_get_all_token_from_env(
        self, db_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """OANDA_API_TOKEN detected as legacy env var source — value never printed."""
        monkeypatch.setenv("OANDA_API_TOKEN", "env-tok")
        result = runner.invoke(app, ["config", "get"])
        # Displayed as "legacy env var" since it's the old format.
        assert "env var" in result.output
        assert "env-tok" not in result.output  # value must not be printed

    def test_config_get_all_token_from_keyring(
        self, db_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.delenv("OANDA_API_TOKEN", raising=False)
        monkeypatch.setattr("frmj.app.keyring.get_password", lambda s, u: "kr-tok")
        result = runner.invoke(app, ["config", "get"])
        assert "keychain" in result.output
        assert "kr-tok" not in result.output  # value must not be printed

    def test_config_get_all_no_active_account_shows_guidance(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """When no active account exists, config get shows 'no active account'."""
        path = tmp_path / "empty_account.db"
        monkeypatch.setenv("FRMJ_DB_PATH", str(path))
        result = runner.invoke(app, ["config", "get"])
        assert result.exit_code == 0
        assert "no active account" in result.output.lower()

    def test_config_unset_removes_existing_key(self, db_path: Path) -> None:
        runner.invoke(app, ["config", "set", "max_open_trades", "5"])
        result = runner.invoke(app, ["config", "unset", "max_open_trades"])
        assert result.exit_code == 0, result.output
        assert "Unset max_open_trades" in result.output
        get_result = runner.invoke(app, ["config", "get", "max_open_trades"])
        assert get_result.exit_code == 1

    def test_config_unset_missing_key_exits_1(self, db_path: Path) -> None:
        result = runner.invoke(app, ["config", "unset", "never_set_key"])
        assert result.exit_code == 1
        assert "was not set" in result.output


# ---------------------------------------------------------------------------
# config check
# ---------------------------------------------------------------------------


class TestConfigCheck:
    """Tests for ``frmj config check``."""

    def test_all_checks_pass_with_full_config(
        self, db_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Fully configured setup exits 0 and reports all checks passed."""
        for key, val in [
            ("max_open_trades", "5"),
            ("risk_strategy", "remaining_margin_fraction"),
            ("blocking_mode", "hard_block"),
            ("scale_in", "never"),
            ("safety_reserve_pct", "0.05"),
        ]:
            runner.invoke(app, ["config", "set", key, val])

        result = runner.invoke(app, ["config", "check"])
        assert result.exit_code == 0, result.output
        assert "All checks passed" in result.output

    def test_missing_token_exits_1(
        self, db_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Missing token shows MISSING status and exits 1."""
        monkeypatch.delenv("OANDA_API_TOKEN", raising=False)
        monkeypatch.delenv("OANDA_API_TOKEN_PRACTICE", raising=False)
        monkeypatch.setattr("frmj.app.keyring.get_password", lambda s, u: None)
        result = runner.invoke(app, ["config", "check"])
        assert result.exit_code == 1
        assert "MISSING" in result.output

    def test_missing_active_account_exits_1(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """No active account shows MISSING and exits 1."""
        path = tmp_path / "check_test.db"
        monkeypatch.setenv("FRMJ_DB_PATH", str(path))
        monkeypatch.setenv("OANDA_API_TOKEN", "tok")
        result = runner.invoke(app, ["config", "check"])
        assert result.exit_code == 1
        assert "active account" in result.output
        assert "MISSING" in result.output

    def test_missing_max_open_trades_is_warning(
        self, db_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Missing max_open_trades shows WARN but exits 0 (trading is optional)."""
        result = runner.invoke(app, ["config", "check"])
        assert result.exit_code == 0
        assert "WARN" in result.output
        assert "max_open_trades" in result.output

    def test_invalid_risk_strategy_exits_1(self, db_path: Path) -> None:
        """An unknown risk_strategy value shows INVALID and exits 1."""
        runner.invoke(app, ["config", "set", "risk_strategy", "bogus_strategy"])
        result = runner.invoke(app, ["config", "check"])
        assert result.exit_code == 1
        assert "INVALID" in result.output
        assert "risk_strategy" in result.output

    def test_invalid_blocking_mode_exits_1(self, db_path: Path) -> None:
        """An unknown blocking_mode value shows INVALID and exits 1."""
        runner.invoke(app, ["config", "set", "blocking_mode", "not_valid"])
        result = runner.invoke(app, ["config", "check"])
        assert result.exit_code == 1
        assert "blocking_mode" in result.output

    def test_invalid_scale_in_exits_1(self, db_path: Path) -> None:
        """An unknown scale_in value shows INVALID and exits 1."""
        runner.invoke(app, ["config", "set", "scale_in", "sometimes"])
        result = runner.invoke(app, ["config", "check"])
        assert result.exit_code == 1
        assert "scale_in" in result.output

    def test_safety_reserve_out_of_range_exits_1(self, db_path: Path) -> None:
        """safety_reserve_pct >= 1 shows INVALID and exits 1."""
        runner.invoke(app, ["config", "set", "safety_reserve_pct", "1.5"])
        result = runner.invoke(app, ["config", "check"])
        assert result.exit_code == 1
        assert "safety_reserve_pct" in result.output

    def test_percent_of_equity_strategy_requires_field(self, db_path: Path) -> None:
        """risk_strategy=percent_of_equity without percent_of_equity → MISSING."""
        runner.invoke(app, ["config", "set", "risk_strategy", "percent_of_equity"])
        result = runner.invoke(app, ["config", "check"])
        assert result.exit_code == 1
        assert "percent_of_equity" in result.output
        assert "MISSING" in result.output

    def test_percent_of_equity_with_field_is_ok(self, db_path: Path) -> None:
        """risk_strategy=percent_of_equity with percent_of_equity set → no error."""
        runner.invoke(app, ["config", "set", "risk_strategy", "percent_of_equity"])
        runner.invoke(app, ["config", "set", "percent_of_equity", "0.02"])
        runner.invoke(app, ["config", "set", "max_open_trades", "5"])
        result = runner.invoke(app, ["config", "check"])
        # Should pass (no errors), may have no warnings
        assert "MISSING" not in result.output
        assert "INVALID" not in result.output

    def test_fixed_dollar_strategy_requires_field(self, db_path: Path) -> None:
        """risk_strategy=fixed_dollar without fixed_dollar → MISSING."""
        runner.invoke(app, ["config", "set", "risk_strategy", "fixed_dollar"])
        result = runner.invoke(app, ["config", "check"])
        assert result.exit_code == 1
        assert "fixed_dollar" in result.output

    def test_fixed_dollar_with_field_is_ok(self, db_path: Path) -> None:
        """risk_strategy=fixed_dollar with fixed_dollar set → OK line shown."""
        runner.invoke(app, ["config", "set", "max_open_trades", "5"])
        runner.invoke(app, ["config", "set", "risk_strategy", "fixed_dollar"])
        runner.invoke(app, ["config", "set", "fixed_dollar", "100"])
        result = runner.invoke(app, ["config", "check"])
        assert "fixed_dollar" in result.output
        assert "OK" in result.output

    def test_connectivity_flag_skipped_without_token(
        self, db_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """--connectivity is skipped with a WARN when token is missing."""
        monkeypatch.delenv("OANDA_API_TOKEN", raising=False)
        monkeypatch.setattr("frmj.app.keyring.get_password", lambda s, u: None)
        result = runner.invoke(app, ["config", "check", "--connectivity"])
        # Connectivity check itself is WARN (skipped), not causing an extra error
        assert "skipped" in result.output or "connectivity" in result.output

    def test_connectivity_flag_with_working_api(
        self, db_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """--connectivity shows NAV when API responds successfully."""
        runner.invoke(app, ["config", "set", "max_open_trades", "5"])

        fake = FakeFullClient()
        monkeypatch.setattr("frmj.cli.config.get_client", lambda conn: fake)

        result = runner.invoke(app, ["config", "check", "--connectivity"])
        assert result.exit_code == 0, result.output
        assert "connectivity" in result.output
        assert "NAV" in result.output or "OK" in result.output

    def test_connectivity_flag_with_api_error(
        self, db_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """--connectivity shows INVALID when the API call fails."""
        runner.invoke(app, ["config", "set", "max_open_trades", "5"])

        class BadClient:
            account_id = "acct-1"

            def get_account_summary(self):
                raise RuntimeError("connection refused")

        monkeypatch.setattr("frmj.cli.config.get_client", lambda conn: BadClient())

        result = runner.invoke(app, ["config", "check", "--connectivity"])
        assert result.exit_code == 1
        assert "INVALID" in result.output
        assert "connection refused" in result.output

    def test_token_from_env_shows_ok(self, db_path: Path) -> None:
        """When OANDA_API_TOKEN is set, config check reports the token as OK."""
        result = runner.invoke(app, ["config", "check"])
        assert "token" in result.output
        # env var source is displayed (new format or legacy fallback format)
        assert "env var" in result.output or "keychain" in result.output

    def test_default_values_shown_when_not_set(self, db_path: Path) -> None:
        """Unset optional keys display their defaults."""
        result = runner.invoke(app, ["config", "check"])
        assert "remaining_margin_fraction (default)" in result.output
        assert "hard_block (default)" in result.output
        assert "never (default)" in result.output

    def test_token_practice_env_var_shows_ok(
        self, db_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """OANDA_API_TOKEN_PRACTICE takes priority over the legacy env var for
        a practice account, and is reported as its own source."""
        monkeypatch.setenv("OANDA_API_TOKEN_PRACTICE", "practice-tok")
        result = runner.invoke(app, ["config", "check"])
        assert result.exit_code == 0, result.output
        assert "OANDA_API_TOKEN_PRACTICE" in result.output

    def test_token_from_keychain_shows_ok(
        self, db_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """With no token env vars set, a keychain-stored token is reported as
        an OS keychain source."""
        monkeypatch.delenv("OANDA_API_TOKEN", raising=False)
        monkeypatch.delenv("OANDA_API_TOKEN_PRACTICE", raising=False)
        monkeypatch.setattr("frmj.app.keyring.get_password", lambda s, u: "kr-tok")
        result = runner.invoke(app, ["config", "check"])
        assert result.exit_code == 0, result.output
        assert "OS keychain" in result.output

    def test_max_open_trades_non_positive_is_invalid(self, db_path: Path) -> None:
        """max_open_trades <= 0 shows INVALID and exits 1."""
        runner.invoke(app, ["config", "set", "max_open_trades", "0"])
        result = runner.invoke(app, ["config", "check"])
        assert result.exit_code == 1
        assert "INVALID" in result.output
        assert "max_open_trades" in result.output

    def test_correlation_blocking_mode_explicit_valid_value_is_ok(
        self, db_path: Path
    ) -> None:
        """An explicitly-set, recognised correlation_blocking_mode shows OK
        with its own value (not the default label)."""
        runner.invoke(app, ["config", "set", "correlation_blocking_mode", "hard_block"])
        result = runner.invoke(app, ["config", "check"])
        assert "MISSING" not in result.output
        assert "INVALID" not in result.output
        assert "correlation_blocking_mode" in result.output

    def test_invalid_correlation_blocking_mode_exits_1(self, db_path: Path) -> None:
        """An unknown correlation_blocking_mode value shows INVALID and exits 1."""
        runner.invoke(app, ["config", "set", "correlation_blocking_mode", "not_valid"])
        result = runner.invoke(app, ["config", "check"])
        assert result.exit_code == 1
        assert "INVALID" in result.output
        assert "correlation_blocking_mode" in result.output


# ---------------------------------------------------------------------------
# config set-token / config unset-token
# ---------------------------------------------------------------------------


class TestConfigTokenCommands:
    def test_set_token_stores_and_confirms(
        self, db_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        stored: list[str] = []
        monkeypatch.setattr(
            "frmj.app.keyring.set_password",
            lambda s, u, p: stored.append(p),
        )
        result = runner.invoke(app, ["config", "set-token"], input="my-api-key\n")
        assert result.exit_code == 0, result.output
        assert "stored" in result.output.lower()
        assert stored == ["my-api-key"]

    def test_set_token_hides_input(
        self, db_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The token value must not appear in the command output."""
        monkeypatch.setattr("frmj.app.keyring.set_password", lambda s, u, p: None)
        result = runner.invoke(app, ["config", "set-token"], input="super-secret\n")
        assert "super-secret" not in result.output

    def test_set_token_no_active_account_exits_1(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("FRMJ_DB_PATH", str(tmp_path / "no-active.db"))
        result = runner.invoke(app, ["config", "set-token"])
        assert result.exit_code == 1
        assert "No active account" in result.output + result.stderr

    def test_set_token_exits_1_on_no_keyring(
        self, db_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        import keyring.errors

        monkeypatch.setattr(
            "frmj.app.keyring.set_password",
            lambda s, u, p: (_ for _ in ()).throw(keyring.errors.NoKeyringError()),
        )
        result = runner.invoke(app, ["config", "set-token"], input="tok\n")
        assert result.exit_code == 1
        assert "No system keyring" in result.output + result.stderr

    def test_unset_token_confirms(
        self, db_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        deleted: list[bool] = []
        monkeypatch.setattr(
            "frmj.app.keyring.delete_password",
            lambda s, u: deleted.append(True),
        )
        result = runner.invoke(app, ["config", "unset-token"])
        assert result.exit_code == 0, result.output
        assert "removed" in result.output.lower()
        assert deleted == [True]

    def test_unset_token_no_active_account_exits_1(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("FRMJ_DB_PATH", str(tmp_path / "no-active.db"))
        result = runner.invoke(app, ["config", "unset-token"])
        assert result.exit_code == 1
        assert "No active account" in result.output + result.stderr

    def test_unset_token_exits_1_on_no_keyring(
        self, db_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        import keyring.errors

        monkeypatch.setattr(
            "frmj.app.keyring.delete_password",
            lambda s, u: (_ for _ in ()).throw(keyring.errors.NoKeyringError()),
        )
        result = runner.invoke(app, ["config", "unset-token"])
        assert result.exit_code == 1
