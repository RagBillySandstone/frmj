"""Tests for app.py: database factory, config helpers, client factory, risk config.

We use ``tmp_path`` (pytest's built-in temporary directory fixture) for any
test that creates a real SQLite file, and ``monkeypatch`` to control env vars.
No network calls are made — ``get_client`` is tested up to the point of
constructing an ``OandaClient`` object (which has no side effects on
construction).
"""

from __future__ import annotations

import sqlite3
import sys
from decimal import Decimal
from pathlib import Path

import pytest

from frmj.accounts import add_account, set_active_account
from frmj.app import (
    _resolve_default_data_dir,
    clear_draft_plan,
    delete_token,
    get_all_config,
    get_client,
    get_config,
    get_db,
    get_risk_config,
    get_token,
    load_draft_plan,
    save_draft_plan,
    set_config,
    store_token,
)
from frmj.domain.risk import (
    BlockingMode,
    RiskConfig,
    RiskStrategy,
    ScaleInPolicy,
)
from frmj.execution.oanda import OandaClient


# ---------------------------------------------------------------------------
# _resolve_default_data_dir
# ---------------------------------------------------------------------------


class TestResolveDefaultDataDir:
    """Tests for the platform-aware data directory resolver.

    ``Path.home`` is replaced with a staticmethod returning ``tmp_path`` so the
    test never touches the real home directory.  ``sys.platform`` is patched on
    the global ``sys`` object, which is the same reference ``frmj.app`` holds.
    """

    @pytest.fixture()
    def fake_home(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
        """Redirect Path.home() to a controlled temp directory."""
        monkeypatch.setattr(Path, "home", staticmethod(lambda: tmp_path))
        return tmp_path

    def test_linux_returns_xdg_path(
        self, fake_home: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """On Linux the XDG convention is always used regardless of what is on disk."""
        monkeypatch.setattr(sys, "platform", "linux")
        assert _resolve_default_data_dir() == fake_home / ".local" / "share" / "frmj"

    def test_darwin_fresh_install_returns_library_path(
        self, fake_home: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """On macOS the canonical ~/Library/Application Support path is used for new installs."""
        monkeypatch.setattr(sys, "platform", "darwin")
        assert (
            _resolve_default_data_dir()
            == fake_home / "Library" / "Application Support" / "frmj"
        )

    def test_darwin_existing_install_keeps_legacy_xdg_path(
        self, fake_home: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """On macOS, the XDG path is preserved when frmj.db already lives there."""
        monkeypatch.setattr(sys, "platform", "darwin")
        legacy_db = fake_home / ".local" / "share" / "frmj" / "frmj.db"
        legacy_db.parent.mkdir(parents=True)
        legacy_db.touch()
        assert _resolve_default_data_dir() == fake_home / ".local" / "share" / "frmj"

    def test_darwin_legacy_dir_without_db_does_not_trigger_fallback(
        self, fake_home: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """On macOS, the legacy directory alone is not enough — frmj.db must be present."""
        monkeypatch.setattr(sys, "platform", "darwin")
        (fake_home / ".local" / "share" / "frmj").mkdir(parents=True)
        assert (
            _resolve_default_data_dir()
            == fake_home / "Library" / "Application Support" / "frmj"
        )

    def test_windows_fresh_install_returns_appdata_path(
        self, fake_home: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """On Windows with no legacy db the canonical AppData\\Roaming path is used."""
        monkeypatch.setattr(sys, "platform", "win32")
        assert _resolve_default_data_dir() == fake_home / "AppData" / "Roaming" / "frmj"

    def test_windows_existing_install_keeps_legacy_xdg_path(
        self, fake_home: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """On Windows, the XDG path is preserved when frmj.db already lives there."""
        monkeypatch.setattr(sys, "platform", "win32")
        # Simulate a pre-existing database at the nonstandard location.
        legacy_db = fake_home / ".local" / "share" / "frmj" / "frmj.db"
        legacy_db.parent.mkdir(parents=True)
        legacy_db.touch()
        assert _resolve_default_data_dir() == fake_home / ".local" / "share" / "frmj"

    def test_windows_legacy_dir_without_db_does_not_trigger_fallback(
        self, fake_home: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The legacy directory existing on its own is not enough — the db file must be present."""
        monkeypatch.setattr(sys, "platform", "win32")
        # Directory exists but contains no frmj.db.
        (fake_home / ".local" / "share" / "frmj").mkdir(parents=True)
        assert _resolve_default_data_dir() == fake_home / "AppData" / "Roaming" / "frmj"


# ---------------------------------------------------------------------------
# get_db
# ---------------------------------------------------------------------------


class TestGetDb:
    def test_creates_file_at_frmj_db_path(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        db_path = tmp_path / "test.db"
        monkeypatch.setenv("FRMJ_DB_PATH", str(db_path))
        conn = get_db()
        conn.close()
        assert db_path.exists()

    def test_creates_parent_directories(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        db_path = tmp_path / "a" / "b" / "c" / "test.db"
        monkeypatch.setenv("FRMJ_DB_PATH", str(db_path))
        conn = get_db()
        conn.close()
        assert db_path.exists()

    def test_applies_schema(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        db_path = tmp_path / "test.db"
        monkeypatch.setenv("FRMJ_DB_PATH", str(db_path))
        conn = get_db()
        tables = {
            row[0]
            for row in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            ).fetchall()
        }
        conn.close()
        assert {"transactions", "notes", "sync_cursors", "config", "accounts"} <= tables

    def test_explicit_path_overrides_env(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("FRMJ_DB_PATH", str(tmp_path / "env.db"))
        explicit = tmp_path / "explicit.db"
        conn = get_db(path=explicit)
        conn.close()
        assert explicit.exists()
        assert not (tmp_path / "env.db").exists()

    def test_row_factory_set(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        db_path = tmp_path / "test.db"
        monkeypatch.setenv("FRMJ_DB_PATH", str(db_path))
        conn = get_db()
        assert conn.row_factory == sqlite3.Row
        conn.close()


# ---------------------------------------------------------------------------
# get_config / set_config
# ---------------------------------------------------------------------------


class TestConfigHelpers:
    @pytest.fixture()
    def db(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> sqlite3.Connection:
        monkeypatch.setenv("FRMJ_DB_PATH", str(tmp_path / "test.db"))
        conn = get_db()
        yield conn
        conn.close()

    def test_get_config_returns_none_for_missing_key(
        self, db: sqlite3.Connection
    ) -> None:
        assert get_config(db, "nonexistent") is None

    def test_set_and_get_roundtrip(self, db: sqlite3.Connection) -> None:
        set_config(db, "account_id", "101-001-12345-001")
        assert get_config(db, "account_id") == "101-001-12345-001"

    def test_set_config_upserts(self, db: sqlite3.Connection) -> None:
        set_config(db, "practice_mode", "true")
        set_config(db, "practice_mode", "false")
        assert get_config(db, "practice_mode") == "false"

    def test_multiple_keys_are_independent(self, db: sqlite3.Connection) -> None:
        set_config(db, "key_a", "value_a")
        set_config(db, "key_b", "value_b")
        assert get_config(db, "key_a") == "value_a"
        assert get_config(db, "key_b") == "value_b"

    def test_get_all_config_empty(self, db: sqlite3.Connection) -> None:
        assert get_all_config(db) == []

    def test_get_all_config_returns_all_sorted(self, db: sqlite3.Connection) -> None:
        set_config(db, "zzz", "last")
        set_config(db, "aaa", "first")
        set_config(db, "mmm", "middle")
        pairs = get_all_config(db)
        assert pairs == [("aaa", "first"), ("mmm", "middle"), ("zzz", "last")]

    def test_get_all_config_reflects_upsert(self, db: sqlite3.Connection) -> None:
        set_config(db, "k", "v1")
        set_config(db, "k", "v2")
        pairs = get_all_config(db)
        assert pairs == [("k", "v2")]


# ---------------------------------------------------------------------------
# get_client
# ---------------------------------------------------------------------------


class TestGetClient:
    @pytest.fixture()
    def db(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> sqlite3.Connection:
        monkeypatch.setenv("FRMJ_DB_PATH", str(tmp_path / "test.db"))
        conn = get_db()
        yield conn
        conn.close()

    def test_raises_without_active_account(
        self, db: sqlite3.Connection, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """get_client raises a clear error when no active account is configured."""
        monkeypatch.setenv("OANDA_API_TOKEN", "test-token")
        with pytest.raises(RuntimeError, match="No active account"):
            get_client(db)

    def test_raises_without_token(
        self, db: sqlite3.Connection, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """get_client raises when the active account has no resolvable token."""
        monkeypatch.delenv("OANDA_API_TOKEN", raising=False)
        monkeypatch.delenv("OANDA_API_TOKEN_PRACTICE", raising=False)
        add_account(db, "practice", "101-001-12345-001", is_practice=True)
        set_active_account(db, "practice")
        with pytest.raises(RuntimeError, match="No API token"):
            get_client(db)

    def test_returns_oanda_client_for_practice_account(
        self, db: sqlite3.Connection, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A practice account profile yields an OandaClient with the correct account_id."""
        monkeypatch.setenv("OANDA_API_TOKEN", "test-token")
        add_account(db, "my-practice", "101-001-12345-001", is_practice=True)
        set_active_account(db, "my-practice")
        client = get_client(db)
        assert isinstance(client, OandaClient)
        assert client.account_id == "101-001-12345-001"
        client.close()

    def test_practice_account_uses_practice_url(
        self, db: sqlite3.Connection, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A practice account profile points OandaClient at the practice API host."""
        monkeypatch.setenv("OANDA_API_TOKEN", "test-token")
        add_account(db, "practice", "101-001-12345-001", is_practice=True)
        set_active_account(db, "practice")
        client = get_client(db)
        from frmj.execution.oanda import PRACTICE_BASE_URL

        assert client._base_url == PRACTICE_BASE_URL
        client.close()

    def test_live_account_uses_live_url(
        self, db: sqlite3.Connection, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A live account profile points OandaClient at the live API host."""
        monkeypatch.setenv("OANDA_API_TOKEN", "test-token")
        add_account(db, "live", "101-001-99999-001", is_practice=False)
        set_active_account(db, "live")
        client = get_client(db)
        from frmj.execution.oanda import LIVE_BASE_URL

        assert client._base_url == LIVE_BASE_URL
        client.close()


# ---------------------------------------------------------------------------
# get_risk_config
# ---------------------------------------------------------------------------


class TestGetRiskConfig:
    @pytest.fixture()
    def db(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> sqlite3.Connection:
        monkeypatch.setenv("FRMJ_DB_PATH", str(tmp_path / "test.db"))
        conn = get_db()
        yield conn
        conn.close()

    def test_raises_without_max_open_trades(self, db: sqlite3.Connection) -> None:
        with pytest.raises(RuntimeError, match="max_open_trades"):
            get_risk_config(db)

    def test_minimal_config_uses_defaults(self, db: sqlite3.Connection) -> None:
        """Only max_open_trades required; everything else has sensible defaults."""
        set_config(db, "max_open_trades", "5")
        cfg = get_risk_config(db)
        assert cfg.max_open_trades == 5
        assert cfg.strategy is RiskStrategy.REMAINING_MARGIN_FRACTION
        assert cfg.blocking_mode is BlockingMode.HARD_BLOCK
        assert cfg.scale_in is ScaleInPolicy.NEVER
        assert cfg.safety_reserve_pct == Decimal("0")

    def test_strategy_override(self, db: sqlite3.Connection) -> None:
        set_config(db, "max_open_trades", "5")
        set_config(db, "risk_strategy", "percent_of_equity")
        set_config(db, "percent_of_equity", "0.02")
        cfg = get_risk_config(db)
        assert cfg.strategy is RiskStrategy.PERCENT_OF_EQUITY
        assert cfg.percent_of_equity == Decimal("0.02")

    def test_blocking_mode_override(self, db: sqlite3.Connection) -> None:
        set_config(db, "max_open_trades", "5")
        set_config(db, "blocking_mode", "warning_only")
        cfg = get_risk_config(db)
        assert cfg.blocking_mode is BlockingMode.WARNING_ONLY

    def test_scale_in_warn(self, db: sqlite3.Connection) -> None:
        set_config(db, "max_open_trades", "5")
        set_config(db, "scale_in", "warn")
        cfg = get_risk_config(db)
        assert cfg.scale_in is ScaleInPolicy.WARN

    def test_safety_reserve_set(self, db: sqlite3.Connection) -> None:
        set_config(db, "max_open_trades", "5")
        set_config(db, "safety_reserve_pct", "0.05")
        cfg = get_risk_config(db)
        assert cfg.safety_reserve_pct == Decimal("0.05")

    def test_returns_risk_config_instance(self, db: sqlite3.Connection) -> None:
        set_config(db, "max_open_trades", "6")
        cfg = get_risk_config(db)
        assert isinstance(cfg, RiskConfig)


# ---------------------------------------------------------------------------
# get_token / store_token / delete_token
# ---------------------------------------------------------------------------


class TestGetToken:
    def test_live_env_var_takes_priority_over_keyring(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """OANDA_API_TOKEN env var wins over keyring in live mode."""
        monkeypatch.setenv("OANDA_API_TOKEN", "env-token")
        monkeypatch.setattr(
            "frmj.app.keyring.get_password", lambda s, u: "keyring-token"
        )
        assert get_token(practice=False) == "env-token"

    def test_live_falls_back_to_keyring_when_env_absent(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.delenv("OANDA_API_TOKEN", raising=False)
        monkeypatch.setattr(
            "frmj.app.keyring.get_password", lambda s, u: "keyring-token"
        )
        assert get_token(practice=False) == "keyring-token"

    def test_live_returns_none_when_both_absent(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.delenv("OANDA_API_TOKEN", raising=False)
        assert get_token(practice=False) is None

    def test_practice_env_var_takes_priority(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """OANDA_API_TOKEN_PRACTICE wins over other sources in practice mode."""
        monkeypatch.setenv("OANDA_API_TOKEN_PRACTICE", "practice-env")
        monkeypatch.setenv("OANDA_API_TOKEN", "live-env")
        monkeypatch.setattr(
            "frmj.app.keyring.get_password", lambda s, u: "keyring-token"
        )
        assert get_token(practice=True) == "practice-env"

    def test_practice_falls_back_to_legacy_env(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """OANDA_API_TOKEN is used as legacy fallback when no practice-specific token set."""
        monkeypatch.delenv("OANDA_API_TOKEN_PRACTICE", raising=False)
        monkeypatch.setenv("OANDA_API_TOKEN", "legacy-token")
        # _no_real_keyring autouse fixture returns None from keyring.
        assert get_token(practice=True) == "legacy-token"

    def test_returns_none_on_keyring_error(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Any KeyringError (including NoKeyringError) is treated as 'not found'."""
        monkeypatch.delenv("OANDA_API_TOKEN", raising=False)
        monkeypatch.delenv("OANDA_API_TOKEN_PRACTICE", raising=False)
        import keyring.errors

        monkeypatch.setattr(
            "frmj.app.keyring.get_password",
            lambda s, u: (_ for _ in ()).throw(keyring.errors.KeyringError()),
        )
        assert get_token(practice=False) is None
        assert get_token(practice=True) is None


class TestStoreToken:
    def test_live_writes_to_correct_keyring_key(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        calls: list[tuple[str, str, str]] = []
        monkeypatch.setattr(
            "frmj.app.keyring.set_password",
            lambda s, u, p: calls.append((s, u, p)),
        )
        store_token("my-live-token", practice=False)
        assert len(calls) == 1
        service, username, token = calls[0]
        assert service == "frmj"
        assert username == "oanda_api_token_live"
        assert token == "my-live-token"

    def test_practice_writes_to_correct_keyring_key(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        calls: list[tuple[str, str, str]] = []
        monkeypatch.setattr(
            "frmj.app.keyring.set_password",
            lambda s, u, p: calls.append((s, u, p)),
        )
        store_token("my-practice-token", practice=True)
        assert len(calls) == 1
        service, username, token = calls[0]
        assert service == "frmj"
        assert username == "oanda_api_token_practice"
        assert token == "my-practice-token"

    def test_raises_runtime_error_on_no_keyring(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        import keyring.errors

        monkeypatch.setattr(
            "frmj.app.keyring.set_password",
            lambda s, u, p: (_ for _ in ()).throw(keyring.errors.NoKeyringError()),
        )
        with pytest.raises(RuntimeError, match="No system keyring"):
            store_token("my-secret-token")


# ---------------------------------------------------------------------------
# save_draft_plan / load_draft_plan / clear_draft_plan
# ---------------------------------------------------------------------------


class TestDraftPlan:
    @pytest.fixture()
    def plan_path(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
        """Redirect _DRAFT_PLAN_PATH to a temp location for isolation."""
        path = tmp_path / "saved_plan.json"
        monkeypatch.setattr("frmj.app._DRAFT_PLAN_PATH", path)
        return path

    def test_save_creates_file(self, plan_path: Path) -> None:
        save_draft_plan({"instrument": "EUR_USD"})
        assert plan_path.exists()

    def test_save_returns_path(self, plan_path: Path) -> None:
        result = save_draft_plan({"instrument": "EUR_USD"})
        assert result == plan_path

    def test_save_and_load_roundtrip(self, plan_path: Path) -> None:
        data = {
            "instrument": "EUR_USD",
            "direction": "long",
            "units_signed": 10000,
            "tp_price": "1.10550",
            "sl_price": None,
        }
        save_draft_plan(data)
        loaded = load_draft_plan()
        assert loaded == data

    def test_load_returns_none_when_absent(self, plan_path: Path) -> None:
        assert load_draft_plan() is None

    def test_clear_removes_file(self, plan_path: Path) -> None:
        save_draft_plan({"instrument": "EUR_USD"})
        clear_draft_plan()
        assert not plan_path.exists()

    def test_clear_is_noop_when_absent(self, plan_path: Path) -> None:
        """clear_draft_plan must not raise when no file exists."""
        clear_draft_plan()  # must not raise

    def test_load_returns_none_on_corrupt_file(self, plan_path: Path) -> None:
        plan_path.write_text("not valid json{{{")
        assert load_draft_plan() is None

    def test_save_overwrites_existing_plan(self, plan_path: Path) -> None:
        save_draft_plan({"instrument": "EUR_USD"})
        save_draft_plan({"instrument": "GBP_USD"})
        loaded = load_draft_plan()
        assert loaded is not None
        assert loaded["instrument"] == "GBP_USD"

    def test_save_creates_parent_directory(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        nested = tmp_path / "a" / "b" / "saved_plan.json"
        monkeypatch.setattr("frmj.app._DRAFT_PLAN_PATH", nested)
        save_draft_plan({"instrument": "EUR_USD"})
        assert nested.exists()


class TestDeleteToken:
    def test_calls_delete_password(self, monkeypatch: pytest.MonkeyPatch) -> None:
        deleted: list[bool] = []
        monkeypatch.setattr(
            "frmj.app.keyring.delete_password",
            lambda s, u: deleted.append(True),
        )
        delete_token()
        assert deleted == [True]

    def test_swallows_password_delete_error(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Deleting a token that was never stored must not raise."""
        import keyring.errors

        monkeypatch.setattr(
            "frmj.app.keyring.delete_password",
            lambda s, u: (_ for _ in ()).throw(keyring.errors.PasswordDeleteError()),
        )
        delete_token()  # must not raise

    def test_raises_runtime_error_on_no_keyring(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        import keyring.errors

        monkeypatch.setattr(
            "frmj.app.keyring.delete_password",
            lambda s, u: (_ for _ in ()).throw(keyring.errors.NoKeyringError()),
        )
        with pytest.raises(RuntimeError, match="No system keyring"):
            delete_token()
