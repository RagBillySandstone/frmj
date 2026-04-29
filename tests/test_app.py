"""Tests for app.py: database factory, config helpers, client factory, risk config.

We use ``tmp_path`` (pytest's built-in temporary directory fixture) for any
test that creates a real SQLite file, and ``monkeypatch`` to control env vars.
No network calls are made — ``get_client`` is tested up to the point of
constructing an ``OandaClient`` object (which has no side effects on
construction).
"""

from __future__ import annotations

import sqlite3
from decimal import Decimal
from pathlib import Path

import pytest

from frmj.app import (
    delete_token,
    get_all_config,
    get_client,
    get_config,
    get_db,
    get_risk_config,
    get_token,
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
        assert {"transactions", "notes", "sync_cursors", "config"} <= tables

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

    def test_raises_without_token(
        self, db: sqlite3.Connection, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.delenv("OANDA_API_TOKEN", raising=False)
        set_config(db, "account_id", "101-001-12345-001")
        with pytest.raises(RuntimeError, match="OANDA_API_TOKEN"):
            get_client(db)

    def test_raises_without_account_id(
        self, db: sqlite3.Connection, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("OANDA_API_TOKEN", "test-token")
        with pytest.raises(RuntimeError, match="account_id"):
            get_client(db)

    def test_returns_oanda_client(
        self, db: sqlite3.Connection, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("OANDA_API_TOKEN", "test-token")
        set_config(db, "account_id", "101-001-12345-001")
        client = get_client(db)
        assert isinstance(client, OandaClient)
        assert client.account_id == "101-001-12345-001"
        client.close()

    def test_practice_mode_default_is_true(
        self, db: sqlite3.Connection, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """When practice_mode is absent, the client should use the practice URL."""
        monkeypatch.setenv("OANDA_API_TOKEN", "test-token")
        set_config(db, "account_id", "101-001-12345-001")
        client = get_client(db)
        from frmj.execution.oanda import PRACTICE_BASE_URL
        assert client._base_url == PRACTICE_BASE_URL
        client.close()

    def test_practice_mode_false_uses_live_url(
        self, db: sqlite3.Connection, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("OANDA_API_TOKEN", "test-token")
        set_config(db, "account_id", "101-001-12345-001")
        set_config(db, "practice_mode", "false")
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
    def test_env_var_takes_priority_over_keyring(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Env var wins even when keyring also has a value."""
        monkeypatch.setenv("OANDA_API_TOKEN", "env-token")
        monkeypatch.setattr("frmj.app.keyring.get_password", lambda s, u: "keyring-token")
        assert get_token() == "env-token"

    def test_falls_back_to_keyring_when_env_absent(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.delenv("OANDA_API_TOKEN", raising=False)
        monkeypatch.setattr("frmj.app.keyring.get_password", lambda s, u: "keyring-token")
        assert get_token() == "keyring-token"

    def test_returns_none_when_both_absent(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.delenv("OANDA_API_TOKEN", raising=False)
        # _no_real_keyring autouse fixture already returns None from get_password.
        assert get_token() is None

    def test_returns_none_on_keyring_error(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Any KeyringError (including NoKeyringError) is treated as 'not found'."""
        monkeypatch.delenv("OANDA_API_TOKEN", raising=False)
        import keyring.errors
        monkeypatch.setattr(
            "frmj.app.keyring.get_password",
            lambda s, u: (_ for _ in ()).throw(keyring.errors.KeyringError()),
        )
        assert get_token() is None


class TestStoreToken:
    def test_writes_to_keyring_with_correct_args(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        calls: list[tuple[str, str, str]] = []
        monkeypatch.setattr(
            "frmj.app.keyring.set_password",
            lambda s, u, p: calls.append((s, u, p)),
        )
        store_token("my-secret-token")
        assert len(calls) == 1
        service, username, token = calls[0]
        assert service == "frmj"
        assert username == "oanda_api_token"
        assert token == "my-secret-token"

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


class TestDeleteToken:
    def test_calls_delete_password(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
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
