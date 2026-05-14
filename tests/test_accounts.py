"""
Tests for accounts.py: CRUD, active account helpers, live mode, and migration.

All tests use real SQLite databases (in-memory or tmp_path) — no mocks needed
for pure SQLite logic.  Migration tests additionally stub keyring operations
so the suite never touches the real OS keychain (the autouse fixture in
conftest.py handles this globally).
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from frmj.accounts import (
    AccountRecord,
    add_account,
    get_account,
    get_account_count,
    get_active_account,
    get_active_account_name,
    is_live_mode,
    list_accounts,
    remove_account,
    set_active_account,
    set_live_mode,
)
from frmj.app import (
    get_config,
    get_db,
    migrate_v1_accounts,
    set_config,
)
from frmj.persistence.schema import ensure_schema


# ---------------------------------------------------------------------------
# Helper: open an in-memory database with the schema applied.
# ---------------------------------------------------------------------------


@pytest.fixture()
def conn() -> sqlite3.Connection:
    """Return an in-memory SQLite connection with the full FRoMaJ schema."""
    c = sqlite3.connect(":memory:")
    c.row_factory = sqlite3.Row
    ensure_schema(c)
    yield c
    c.close()


# ---------------------------------------------------------------------------
# Account CRUD
# ---------------------------------------------------------------------------


class TestAddAccount:
    def test_add_creates_row(self, conn: sqlite3.Connection) -> None:
        """add_account inserts a row that get_account can retrieve."""
        add_account(conn, "funded", "101-001-12345-001", is_practice=False)
        record = get_account(conn, "funded")
        assert record is not None
        assert record.name == "funded"
        assert record.oanda_id == "101-001-12345-001"
        assert record.is_practice is False

    def test_add_practice_account(self, conn: sqlite3.Connection) -> None:
        """is_practice=True is preserved through the round-trip."""
        add_account(conn, "demo", "101-001-99999-001", is_practice=True)
        record = get_account(conn, "demo")
        assert record is not None
        assert record.is_practice is True

    def test_duplicate_name_raises(self, conn: sqlite3.Connection) -> None:
        """Inserting a duplicate name raises IntegrityError (PRIMARY KEY)."""
        add_account(conn, "dup", "acct-1", is_practice=True)
        with pytest.raises(sqlite3.IntegrityError):
            add_account(conn, "dup", "acct-2", is_practice=True)

    def test_get_unknown_returns_none(self, conn: sqlite3.Connection) -> None:
        """get_account returns None for a name that was never inserted."""
        assert get_account(conn, "ghost") is None

    def test_created_at_is_populated(self, conn: sqlite3.Connection) -> None:
        """The created_at timestamp default is filled in by SQLite."""
        add_account(conn, "ts-test", "acct-ts", is_practice=True)
        record = get_account(conn, "ts-test")
        assert record is not None
        assert record.created_at  # non-empty string


class TestListAccounts:
    def test_empty_returns_empty_list(self, conn: sqlite3.Connection) -> None:
        assert list_accounts(conn) == []

    def test_returns_all_sorted_by_name(self, conn: sqlite3.Connection) -> None:
        """list_accounts returns rows alphabetically regardless of insert order."""
        add_account(conn, "zebra", "z-acct", is_practice=False)
        add_account(conn, "alpha", "a-acct", is_practice=True)
        add_account(conn, "mid", "m-acct", is_practice=True)
        names = [r.name for r in list_accounts(conn)]
        assert names == ["alpha", "mid", "zebra"]

    def test_returns_account_record_instances(self, conn: sqlite3.Connection) -> None:
        add_account(conn, "rec-test", "acct-r", is_practice=True)
        records = list_accounts(conn)
        assert len(records) == 1
        assert isinstance(records[0], AccountRecord)


class TestRemoveAccount:
    def test_remove_existing_returns_true(self, conn: sqlite3.Connection) -> None:
        add_account(conn, "to-del", "d-acct", is_practice=True)
        assert remove_account(conn, "to-del") is True
        assert get_account(conn, "to-del") is None

    def test_remove_nonexistent_returns_false(self, conn: sqlite3.Connection) -> None:
        assert remove_account(conn, "ghost") is False

    def test_remove_does_not_affect_other_accounts(
        self, conn: sqlite3.Connection
    ) -> None:
        add_account(conn, "keep", "k-acct", is_practice=True)
        add_account(conn, "del", "d-acct", is_practice=False)
        remove_account(conn, "del")
        assert get_account(conn, "keep") is not None


class TestGetAccountCount:
    def test_empty_database_is_zero(self, conn: sqlite3.Connection) -> None:
        assert get_account_count(conn) == 0

    def test_count_increments_with_inserts(self, conn: sqlite3.Connection) -> None:
        add_account(conn, "a1", "id-1", is_practice=True)
        assert get_account_count(conn) == 1
        add_account(conn, "a2", "id-2", is_practice=False)
        assert get_account_count(conn) == 2

    def test_count_decrements_after_remove(self, conn: sqlite3.Connection) -> None:
        add_account(conn, "a", "id", is_practice=True)
        remove_account(conn, "a")
        assert get_account_count(conn) == 0


# ---------------------------------------------------------------------------
# Active account helpers
# ---------------------------------------------------------------------------


class TestActiveAccount:
    def test_get_active_name_returns_none_when_unset(
        self, conn: sqlite3.Connection
    ) -> None:
        assert get_active_account_name(conn) is None

    def test_set_and_get_active_name_roundtrip(self, conn: sqlite3.Connection) -> None:
        set_active_account(conn, "funded")
        assert get_active_account_name(conn) == "funded"

    def test_get_active_account_returns_none_when_no_account_set(
        self, conn: sqlite3.Connection
    ) -> None:
        assert get_active_account(conn) is None

    def test_get_active_account_returns_none_when_name_missing(
        self, conn: sqlite3.Connection
    ) -> None:
        """Active account name in config but no matching row → None (not an error)."""
        set_active_account(conn, "ghost")
        assert get_active_account(conn) is None

    def test_get_active_account_returns_correct_record(
        self, conn: sqlite3.Connection
    ) -> None:
        add_account(conn, "practice", "p-acct", is_practice=True)
        add_account(conn, "live", "l-acct", is_practice=False)
        set_active_account(conn, "live")
        record = get_active_account(conn)
        assert record is not None
        assert record.name == "live"
        assert record.is_practice is False

    def test_switching_active_account(self, conn: sqlite3.Connection) -> None:
        """set_active_account overwrites the previous selection."""
        add_account(conn, "a", "a-id", is_practice=True)
        add_account(conn, "b", "b-id", is_practice=False)
        set_active_account(conn, "a")
        set_active_account(conn, "b")
        assert get_active_account_name(conn) == "b"


# ---------------------------------------------------------------------------
# Live mode helpers
# ---------------------------------------------------------------------------


class TestLiveMode:
    def test_defaults_to_false(self, conn: sqlite3.Connection) -> None:
        """is_live_mode returns False when the key is absent — safe default."""
        assert is_live_mode(conn) is False

    def test_set_enabled_true(self, conn: sqlite3.Connection) -> None:
        set_live_mode(conn, enabled=True)
        assert is_live_mode(conn) is True

    def test_set_enabled_false(self, conn: sqlite3.Connection) -> None:
        set_live_mode(conn, enabled=True)
        set_live_mode(conn, enabled=False)
        assert is_live_mode(conn) is False

    def test_disabling_after_enabling(self, conn: sqlite3.Connection) -> None:
        """Toggling back to False resets to practice mode."""
        set_live_mode(conn, enabled=True)
        set_live_mode(conn, enabled=False)
        assert is_live_mode(conn) is False


# ---------------------------------------------------------------------------
# Migration: v1 flat config → named accounts
# ---------------------------------------------------------------------------


class TestMigrateV1Accounts:
    """migrate_v1_accounts converts old-style flat config to named account profiles.

    Each test exercises a distinct input state (practice only, live only,
    both, or neither).  Tests call migrate_v1_accounts directly on a
    pre-seeded connection so they can control exactly what old config is
    present.
    """

    def _fresh_conn(self) -> sqlite3.Connection:
        """Return an in-memory connection with schema but no data."""
        c = sqlite3.connect(":memory:")
        c.row_factory = sqlite3.Row
        ensure_schema(c)
        return c

    def test_no_old_config_is_noop(self) -> None:
        """Migration does nothing when no legacy config keys are present."""
        conn = self._fresh_conn()
        migrate_v1_accounts(conn)
        assert get_account_count(conn) == 0
        conn.close()

    def test_practice_only_creates_practice_account(self) -> None:
        """Legacy practice_account_id → account named 'practice' with is_practice=True."""
        conn = self._fresh_conn()
        set_config(conn, "practice_account_id", "practice-001")
        set_config(conn, "practice_mode", "true")
        migrate_v1_accounts(conn)
        record = get_account(conn, "practice")
        assert record is not None
        assert record.oanda_id == "practice-001"
        assert record.is_practice is True
        conn.close()

    def test_live_only_creates_live_account(self) -> None:
        """Legacy account_id → account named 'live' with is_practice=False."""
        conn = self._fresh_conn()
        set_config(conn, "account_id", "live-001")
        set_config(conn, "practice_mode", "false")
        migrate_v1_accounts(conn)
        record = get_account(conn, "live")
        assert record is not None
        assert record.oanda_id == "live-001"
        assert record.is_practice is False
        conn.close()

    def test_both_ids_creates_two_accounts(self) -> None:
        """Both account_id and practice_account_id present → two accounts created."""
        conn = self._fresh_conn()
        set_config(conn, "account_id", "live-001")
        set_config(conn, "practice_account_id", "practice-001")
        set_config(conn, "practice_mode", "true")
        migrate_v1_accounts(conn)
        assert get_account_count(conn) == 2
        assert get_account(conn, "practice") is not None
        assert get_account(conn, "live") is not None
        conn.close()

    def test_active_account_set_to_practice_when_practice_mode_true(self) -> None:
        """practice_mode=true → active account is the 'practice' profile."""
        conn = self._fresh_conn()
        set_config(conn, "account_id", "live-001")
        set_config(conn, "practice_account_id", "practice-001")
        set_config(conn, "practice_mode", "true")
        migrate_v1_accounts(conn)
        assert get_active_account_name(conn) == "practice"
        conn.close()

    def test_active_account_set_to_live_when_practice_mode_false(self) -> None:
        """practice_mode=false → active account is the 'live' profile."""
        conn = self._fresh_conn()
        set_config(conn, "account_id", "live-001")
        set_config(conn, "practice_account_id", "practice-001")
        set_config(conn, "practice_mode", "false")
        migrate_v1_accounts(conn)
        assert get_active_account_name(conn) == "live"
        conn.close()

    def test_live_mode_flag_set_correctly(self) -> None:
        """practice_mode=false sets live_mode=true; practice_mode=true sets live_mode=false."""
        conn_live = self._fresh_conn()
        set_config(conn_live, "account_id", "live-001")
        set_config(conn_live, "practice_mode", "false")
        migrate_v1_accounts(conn_live)
        assert is_live_mode(conn_live) is True
        conn_live.close()

        conn_prac = self._fresh_conn()
        set_config(conn_prac, "practice_account_id", "practice-001")
        set_config(conn_prac, "practice_mode", "true")
        migrate_v1_accounts(conn_prac)
        assert is_live_mode(conn_prac) is False
        conn_prac.close()

    def test_old_config_keys_removed_after_migration(self) -> None:
        """account_id, practice_account_id, and practice_mode are deleted post-migration."""
        conn = self._fresh_conn()
        set_config(conn, "account_id", "live-001")
        set_config(conn, "practice_account_id", "practice-001")
        set_config(conn, "practice_mode", "true")
        migrate_v1_accounts(conn)
        assert get_config(conn, "account_id") is None
        assert get_config(conn, "practice_account_id") is None
        assert get_config(conn, "practice_mode") is None
        conn.close()

    def test_already_migrated_is_noop(self) -> None:
        """When accounts already exist, migration does not overwrite them."""
        conn = self._fresh_conn()
        add_account(conn, "existing", "existing-id", is_practice=True)
        set_config(conn, "practice_account_id", "stale-001")
        # Migration should see accounts.count > 0 and skip.
        migrate_v1_accounts(conn)
        assert get_account_count(conn) == 1
        assert get_account(conn, "practice") is None  # would be created by migration
        assert get_account(conn, "existing") is not None
        conn.close()

    def test_migration_runs_on_get_db(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """get_db triggers migration automatically: old config → named account."""
        db_path = tmp_path / "migrate_test.db"
        # Pre-seed the database with legacy config BEFORE calling get_db.
        pre_conn = sqlite3.connect(str(db_path))
        pre_conn.row_factory = sqlite3.Row
        ensure_schema(pre_conn)
        set_config(pre_conn, "practice_account_id", "practice-migrated")
        set_config(pre_conn, "practice_mode", "true")
        pre_conn.close()

        # Now open via get_db — migration should run automatically.
        monkeypatch.setenv("FRMJ_DB_PATH", str(db_path))
        conn = get_db()
        try:
            record = get_account(conn, "practice")
            assert record is not None
            assert record.oanda_id == "practice-migrated"
            assert get_active_account_name(conn) == "practice"
            # Old keys must be gone.
            assert get_config(conn, "practice_account_id") is None
        finally:
            conn.close()


# ---------------------------------------------------------------------------
# Account switching
# ---------------------------------------------------------------------------


class TestAccountSwitching:
    """Integration-style tests: add multiple accounts, switch between them."""

    def test_switch_changes_active_account(self, conn: sqlite3.Connection) -> None:
        add_account(conn, "demo", "demo-001", is_practice=True)
        add_account(conn, "funded", "live-001", is_practice=False)
        set_active_account(conn, "demo")
        assert get_active_account(conn).name == "demo"  # type: ignore[union-attr]
        set_active_account(conn, "funded")
        assert get_active_account(conn).name == "funded"  # type: ignore[union-attr]

    def test_remove_inactive_account_leaves_active_intact(
        self, conn: sqlite3.Connection
    ) -> None:
        add_account(conn, "demo", "demo-001", is_practice=True)
        add_account(conn, "funded", "live-001", is_practice=False)
        set_active_account(conn, "demo")
        remove_account(conn, "funded")
        assert get_active_account(conn) is not None
        assert get_active_account(conn).name == "demo"  # type: ignore[union-attr]

    def test_list_shows_all_accounts_after_add(self, conn: sqlite3.Connection) -> None:
        add_account(conn, "a", "a-id", is_practice=True)
        add_account(conn, "b", "b-id", is_practice=False)
        names = [r.name for r in list_accounts(conn)]
        assert "a" in names
        assert "b" in names
