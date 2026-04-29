"""Tests for the persistence schema module.

We use in-memory SQLite databases throughout so tests are fast, leave no
files on disk, and are fully isolated from one another. Each test gets its
own fresh connection via the ``db`` fixture.

Coverage goals:

* ``ensure_schema`` creates all expected tables and indexes.
* ``ensure_schema`` is idempotent — safe to call twice on the same connection.
* Foreign key enforcement is active after ``ensure_schema``.
* Uniqueness constraints on ``transactions`` catch duplicate Oanda events.
* Parent/child FK relationship for DAILY_FINANCING rows works correctly.
* Notes enforce their FK to ``transactions.id``.
* ``sync_cursors`` PRIMARY KEY prevents duplicate account rows; REPLACE
  advances the cursor cleanly.
* ``config`` PRIMARY KEY prevents duplicate keys; REPLACE updates values.

We deliberately do NOT test WAL journal mode — SQLite silently falls back to
"memory" journal mode for in-memory databases, so the PRAGMA return value is
environment-dependent and not a meaningful assertion target in unit tests.
"""

from __future__ import annotations

import sqlite3

import pytest

from frmj.persistence.schema import ensure_schema


# ---------------------------------------------------------------------------
# Shared fixture
# ---------------------------------------------------------------------------


@pytest.fixture()
def db() -> sqlite3.Connection:
    """
    Fresh in-memory database with the FRoMaJ schema applied.

    ``row_factory = sqlite3.Row`` lets tests access columns by name (e.g.
    ``row["oanda_id"]``) rather than by positional index.
    """
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    ensure_schema(conn)
    return conn


# ---------------------------------------------------------------------------
# Helpers shared across test classes
# ---------------------------------------------------------------------------


def _insert_transaction(
    conn: sqlite3.Connection,
    *,
    oanda_id: str = "1001",
    account_id: str = "acct-1",
    type_: str = "ORDER_FILL",
    time: str = "2026-04-25T12:00:00.000000Z",
    parent_id: int | None = None,
    raw_json: str = "{}",
) -> int:
    """Insert one transaction row and return its synthetic id (lastrowid).

    All parameters have sensible defaults so individual tests only need to
    supply the field(s) they care about.
    """
    cur = conn.execute(
        """
        INSERT INTO transactions (oanda_id, account_id, type, time, parent_id, raw_json)
        VALUES (?, ?, ?, ?, ?, ?)
        """,
        (oanda_id, account_id, type_, time, parent_id, raw_json),
    )
    conn.commit()
    assert cur.lastrowid is not None
    return cur.lastrowid


# ---------------------------------------------------------------------------
# Schema creation and idempotency
# ---------------------------------------------------------------------------


class TestEnsureSchema:
    """Verify that ensure_schema creates the correct structure."""

    def test_creates_all_tables(self, db: sqlite3.Connection) -> None:
        """All five expected tables must be present after initialisation."""
        rows = db.execute(
            "SELECT name FROM sqlite_master WHERE type = 'table'"
        ).fetchall()
        found = {r["name"] for r in rows}
        assert {"transactions", "notes", "sync_cursors", "config", "trade_plans"} <= found

    def test_creates_all_indexes(self, db: sqlite3.Connection) -> None:
        """All named indexes must be present."""
        rows = db.execute(
            "SELECT name FROM sqlite_master WHERE type = 'index'"
        ).fetchall()
        found = {r["name"] for r in rows}
        assert {
            "idx_transactions_oanda_id",
            "idx_transactions_time",
            "idx_transactions_parent",
            "idx_notes_transaction",
            "idx_trade_plans_transaction",
        } <= found

    def test_idempotent(self, db: sqlite3.Connection) -> None:
        """Calling ensure_schema a second time on an initialised DB must not raise."""
        ensure_schema(db)  # db fixture already called it once

    def test_foreign_keys_enabled(self, db: sqlite3.Connection) -> None:
        """PRAGMA foreign_keys must be 1 (ON) for the connection."""
        row = db.execute("PRAGMA foreign_keys").fetchone()
        # Row is indexed positionally here (PRAGMA result has no column name).
        assert row[0] == 1


# ---------------------------------------------------------------------------
# transactions table
# ---------------------------------------------------------------------------


class TestTransactions:
    """Contract tests for the append-only transaction event ledger."""

    def test_basic_insert_and_retrieve(self, db: sqlite3.Connection) -> None:
        """A freshly inserted row can be read back with all fields intact."""
        rowid = _insert_transaction(
            db, oanda_id="42", raw_json='{"type": "ORDER_FILL"}'
        )
        row = db.execute(
            "SELECT * FROM transactions WHERE id = ?", (rowid,)
        ).fetchone()
        assert row["oanda_id"] == "42"
        assert row["account_id"] == "acct-1"
        assert row["type"] == "ORDER_FILL"
        assert row["raw_json"] == '{"type": "ORDER_FILL"}'
        # created_at is populated by the DEFAULT expression.
        assert row["created_at"] is not None

    def test_unique_constraint_blocks_duplicate_oanda_id(
        self, db: sqlite3.Connection
    ) -> None:
        """Inserting the same (account_id, oanda_id) pair twice must fail.

        This is the deduplication guard that prevents a re-sync from inserting
        duplicate rows if the new poll window overlaps with a previous one.
        """
        _insert_transaction(db, oanda_id="999", account_id="acct-1")
        with pytest.raises(sqlite3.IntegrityError):
            _insert_transaction(db, oanda_id="999", account_id="acct-1")

    def test_same_oanda_id_allowed_across_different_accounts(
        self, db: sqlite3.Connection
    ) -> None:
        """The unique index is on (account_id, oanda_id) — not oanda_id alone.

        Two different Oanda accounts can legitimately share the same transaction
        ID namespace.
        """
        _insert_transaction(db, oanda_id="999", account_id="acct-1")
        _insert_transaction(db, oanda_id="999", account_id="acct-2")  # must not raise

    def test_parent_id_fk_enforced(self, db: sqlite3.Connection) -> None:
        """Inserting a child row referencing a non-existent parent must fail.

        This catches ingestion bugs where a financing child arrives before its
        parent (which Oanda should not do, but defensive enforcement is cheap).
        """
        with pytest.raises(sqlite3.IntegrityError):
            _insert_transaction(db, oanda_id="child-orphan", parent_id=99999)

    def test_financing_parent_child_link(self, db: sqlite3.Connection) -> None:
        """A DAILY_FINANCING child correctly references its parent's synthetic id."""
        parent_id = _insert_transaction(
            db, oanda_id="parent-1", type_="DAILY_FINANCING"
        )
        child_rowid = _insert_transaction(
            db,
            oanda_id="child-1",
            type_="DAILY_FINANCING",
            parent_id=parent_id,
        )
        row = db.execute(
            "SELECT parent_id FROM transactions WHERE id = ?", (child_rowid,)
        ).fetchone()
        assert row["parent_id"] == parent_id

    def test_null_parent_id_is_allowed(self, db: sqlite3.Connection) -> None:
        """The vast majority of rows have no parent — NULL must be accepted."""
        rowid = _insert_transaction(db, parent_id=None)
        row = db.execute(
            "SELECT parent_id FROM transactions WHERE id = ?", (rowid,)
        ).fetchone()
        assert row["parent_id"] is None

    def test_multiple_children_share_same_parent(
        self, db: sqlite3.Connection
    ) -> None:
        """A single DAILY_FINANCING parent can have multiple child rows."""
        parent_id = _insert_transaction(
            db, oanda_id="fin-parent", type_="DAILY_FINANCING"
        )
        for i in range(3):
            _insert_transaction(
                db,
                oanda_id=f"fin-child-{i}",
                type_="DAILY_FINANCING",
                parent_id=parent_id,
            )
        rows = db.execute(
            "SELECT id FROM transactions WHERE parent_id = ?", (parent_id,)
        ).fetchall()
        assert len(rows) == 3


# ---------------------------------------------------------------------------
# notes table
# ---------------------------------------------------------------------------


class TestNotes:
    """Contract tests for the mutable notes table."""

    def test_basic_insert_and_retrieve(self, db: sqlite3.Connection) -> None:
        """A note can be attached to a transaction and read back."""
        txn_id = _insert_transaction(db)
        db.execute(
            "INSERT INTO notes (transaction_id, body) VALUES (?, ?)",
            (txn_id, "Entry rationale: breakout above weekly pivot"),
        )
        db.commit()
        row = db.execute(
            "SELECT body FROM notes WHERE transaction_id = ?", (txn_id,)
        ).fetchone()
        assert row["body"] == "Entry rationale: breakout above weekly pivot"

    def test_fk_enforced_on_insert(self, db: sqlite3.Connection) -> None:
        """Attaching a note to a non-existent transaction must fail."""
        with pytest.raises(sqlite3.IntegrityError):
            db.execute(
                "INSERT INTO notes (transaction_id, body) VALUES (?, ?)",
                (99999, "This should not persist"),
            )
            db.commit()

    def test_multiple_notes_per_transaction(self, db: sqlite3.Connection) -> None:
        """Multiple notes can be attached to a single transaction."""
        txn_id = _insert_transaction(db)
        for body in ("First note", "Second note", "Third note"):
            db.execute(
                "INSERT INTO notes (transaction_id, body) VALUES (?, ?)",
                (txn_id, body),
            )
        db.commit()
        rows = db.execute(
            "SELECT body FROM notes WHERE transaction_id = ? ORDER BY id",
            (txn_id,),
        ).fetchall()
        assert [r["body"] for r in rows] == [
            "First note",
            "Second note",
            "Third note",
        ]

    def test_timestamps_populated_by_default(self, db: sqlite3.Connection) -> None:
        """Both created_at and updated_at are set by the DEFAULT expression."""
        txn_id = _insert_transaction(db)
        db.execute(
            "INSERT INTO notes (transaction_id, body) VALUES (?, ?)",
            (txn_id, "timestamped note"),
        )
        db.commit()
        row = db.execute("SELECT created_at, updated_at FROM notes").fetchone()
        assert row["created_at"] is not None
        assert row["updated_at"] is not None

    def test_notes_for_different_transactions_are_independent(
        self, db: sqlite3.Connection
    ) -> None:
        """A note query for one transaction does not return another's notes."""
        txn_a = _insert_transaction(db, oanda_id="txn-a")
        txn_b = _insert_transaction(db, oanda_id="txn-b")
        db.execute(
            "INSERT INTO notes (transaction_id, body) VALUES (?, ?)",
            (txn_a, "note for A"),
        )
        db.execute(
            "INSERT INTO notes (transaction_id, body) VALUES (?, ?)",
            (txn_b, "note for B"),
        )
        db.commit()
        a_notes = db.execute(
            "SELECT body FROM notes WHERE transaction_id = ?", (txn_a,)
        ).fetchall()
        assert len(a_notes) == 1
        assert a_notes[0]["body"] == "note for A"


# ---------------------------------------------------------------------------
# sync_cursors table
# ---------------------------------------------------------------------------


class TestSyncCursors:
    """Contract tests for the incremental-sync cursor table."""

    def test_insert_and_retrieve(self, db: sqlite3.Connection) -> None:
        """A sync cursor can be written and read back."""
        db.execute(
            "INSERT INTO sync_cursors (account_id, last_oanda_id, synced_at) "
            "VALUES (?, ?, ?)",
            ("acct-1", "5000", "2026-04-25T12:00:00Z"),
        )
        db.commit()
        row = db.execute(
            "SELECT * FROM sync_cursors WHERE account_id = ?", ("acct-1",)
        ).fetchone()
        assert row["last_oanda_id"] == "5000"
        assert row["synced_at"] == "2026-04-25T12:00:00Z"

    def test_replace_advances_cursor(self, db: sqlite3.Connection) -> None:
        """REPLACE INTO correctly overwrites the cursor after a new sync run."""
        db.execute(
            "INSERT INTO sync_cursors (account_id, last_oanda_id, synced_at) "
            "VALUES (?, ?, ?)",
            ("acct-1", "5000", "2026-04-25T12:00:00Z"),
        )
        db.commit()
        db.execute(
            "REPLACE INTO sync_cursors (account_id, last_oanda_id, synced_at) "
            "VALUES (?, ?, ?)",
            ("acct-1", "6000", "2026-04-25T13:00:00Z"),
        )
        db.commit()
        row = db.execute(
            "SELECT last_oanda_id FROM sync_cursors WHERE account_id = ?",
            ("acct-1",),
        ).fetchone()
        assert row["last_oanda_id"] == "6000"

    def test_primary_key_blocks_duplicate_account(
        self, db: sqlite3.Connection
    ) -> None:
        """Plain INSERT of the same account_id twice must raise — use REPLACE."""
        db.execute(
            "INSERT INTO sync_cursors (account_id, last_oanda_id, synced_at) "
            "VALUES (?, ?, ?)",
            ("acct-1", "5000", "2026-04-25T12:00:00Z"),
        )
        db.commit()
        with pytest.raises(sqlite3.IntegrityError):
            db.execute(
                "INSERT INTO sync_cursors (account_id, last_oanda_id, synced_at) "
                "VALUES (?, ?, ?)",
                ("acct-1", "5001", "2026-04-25T12:01:00Z"),
            )
            db.commit()

    def test_multiple_accounts_coexist(self, db: sqlite3.Connection) -> None:
        """Cursors for different accounts must not interfere with each other."""
        for acct in ("practice-1", "live-1"):
            db.execute(
                "INSERT INTO sync_cursors (account_id, last_oanda_id, synced_at) "
                "VALUES (?, ?, ?)",
                (acct, "1", "2026-04-25T00:00:00Z"),
            )
        db.commit()
        count = db.execute("SELECT COUNT(*) FROM sync_cursors").fetchone()[0]
        assert count == 2


# ---------------------------------------------------------------------------
# config table
# ---------------------------------------------------------------------------


class TestConfig:
    """Contract tests for the flat key/value config store."""

    def test_insert_and_retrieve(self, db: sqlite3.Connection) -> None:
        """A config entry can be written and read back."""
        db.execute(
            "INSERT INTO config (key, value) VALUES (?, ?)",
            ("practice_mode", "true"),
        )
        db.commit()
        row = db.execute(
            "SELECT value FROM config WHERE key = ?", ("practice_mode",)
        ).fetchone()
        assert row["value"] == "true"

    def test_replace_updates_value(self, db: sqlite3.Connection) -> None:
        """REPLACE INTO correctly overwrites an existing config entry."""
        db.execute("INSERT INTO config (key, value) VALUES (?, ?)", ("k", "v1"))
        db.commit()
        db.execute("REPLACE INTO config (key, value) VALUES (?, ?)", ("k", "v2"))
        db.commit()
        row = db.execute("SELECT value FROM config WHERE key = ?", ("k",)).fetchone()
        assert row["value"] == "v2"

    def test_primary_key_blocks_duplicate_key(self, db: sqlite3.Connection) -> None:
        """Plain INSERT of a duplicate key must raise — use REPLACE to update."""
        db.execute("INSERT INTO config (key, value) VALUES (?, ?)", ("k", "v1"))
        db.commit()
        with pytest.raises(sqlite3.IntegrityError):
            db.execute(
                "INSERT INTO config (key, value) VALUES (?, ?)", ("k", "v2")
            )
            db.commit()

    def test_multiple_keys_independent(self, db: sqlite3.Connection) -> None:
        """Multiple distinct config keys can coexist without interference."""
        entries = [
            ("account_id", "101-001-12345678-001"),
            ("practice_mode", "false"),
            ("default_risk_fraction", "0.02"),
        ]
        for key, value in entries:
            db.execute(
                "INSERT INTO config (key, value) VALUES (?, ?)", (key, value)
            )
        db.commit()
        count = db.execute("SELECT COUNT(*) FROM config").fetchone()[0]
        assert count == 3


# ---------------------------------------------------------------------------
# trade_plans table
# ---------------------------------------------------------------------------


class TestTradePlans:
    """Contract tests for the trade intent / plan table."""

    def test_insert_and_retrieve(self, db: sqlite3.Connection) -> None:
        """TP and SL prices survive a roundtrip."""
        txn_id = _insert_transaction(db)
        db.execute(
            "INSERT INTO trade_plans (transaction_id, tp_price, sl_price) VALUES (?, ?, ?)",
            (txn_id, "1.10550", "1.09750"),
        )
        db.commit()
        row = db.execute(
            "SELECT tp_price, sl_price FROM trade_plans WHERE transaction_id = ?",
            (txn_id,),
        ).fetchone()
        assert row["tp_price"] == "1.10550"
        assert row["sl_price"] == "1.09750"

    def test_null_prices_allowed(self, db: sqlite3.Connection) -> None:
        """Either or both of tp_price / sl_price can be NULL."""
        txn_id = _insert_transaction(db)
        db.execute(
            "INSERT INTO trade_plans (transaction_id, tp_price, sl_price) VALUES (?, ?, ?)",
            (txn_id, None, "1.09750"),
        )
        db.commit()
        row = db.execute(
            "SELECT tp_price FROM trade_plans WHERE transaction_id = ?", (txn_id,)
        ).fetchone()
        assert row["tp_price"] is None

    def test_fk_enforced(self, db: sqlite3.Connection) -> None:
        """Inserting a plan for a non-existent transaction must fail."""
        with pytest.raises(sqlite3.IntegrityError):
            db.execute(
                "INSERT INTO trade_plans (transaction_id, tp_price, sl_price) VALUES (?, ?, ?)",
                (99999, "1.10550", "1.09750"),
            )
            db.commit()

    def test_unique_per_transaction(self, db: sqlite3.Connection) -> None:
        """Only one plan row is allowed per fill transaction."""
        txn_id = _insert_transaction(db)
        db.execute(
            "INSERT INTO trade_plans (transaction_id, tp_price, sl_price) VALUES (?, ?, ?)",
            (txn_id, "1.10550", "1.09750"),
        )
        db.commit()
        with pytest.raises(sqlite3.IntegrityError):
            db.execute(
                "INSERT INTO trade_plans (transaction_id, tp_price, sl_price) VALUES (?, ?, ?)",
                (txn_id, "1.11000", "1.09000"),
            )
            db.commit()

    def test_created_at_populated_by_default(self, db: sqlite3.Connection) -> None:
        txn_id = _insert_transaction(db)
        db.execute(
            "INSERT INTO trade_plans (transaction_id, tp_price, sl_price) VALUES (?, ?, ?)",
            (txn_id, "1.10550", None),
        )
        db.commit()
        row = db.execute("SELECT created_at FROM trade_plans").fetchone()
        assert row["created_at"] is not None
