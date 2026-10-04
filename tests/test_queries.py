"""
Tests for queries.py: local-ledger reads and writes behind the CLI.

Each test runs against a real SQLite database created by ``get_db`` (so the
full schema and migrations apply) and seeds only the rows it needs.  No
mocks: these are plain SQL functions.
"""

from __future__ import annotations

import sqlite3
from collections.abc import Iterator
from pathlib import Path

import pytest

from frmj import queries
from frmj.app import get_db


@pytest.fixture()
def conn(tmp_path: Path) -> Iterator[sqlite3.Connection]:
    """A fresh, schema-initialised database connection."""
    db = get_db(path=tmp_path / "queries.db")
    try:
        yield db
    finally:
        db.close()


def _add_txn(
    conn: sqlite3.Connection,
    oanda_id: str,
    account_id: str = "acct-1",
    txn_type: str = "ORDER_FILL",
    time: str = "2026-04-25T12:00:00Z",
    raw_json: str = "{}",
) -> int:
    """Insert one transaction row and return its local ``id``."""
    cur = conn.execute(
        "INSERT INTO transactions (oanda_id, account_id, type, time, raw_json)"
        " VALUES (?, ?, ?, ?, ?)",
        (oanda_id, account_id, txn_type, time, raw_json),
    )
    conn.commit()
    assert cur.lastrowid is not None
    return cur.lastrowid


# ---------------------------------------------------------------------------
# Transaction lookup
# ---------------------------------------------------------------------------


class TestTransactionLookup:
    def test_scoped_lookup_finds_only_that_account(
        self, conn: sqlite3.Connection
    ) -> None:
        mine = _add_txn(conn, "100", "acct-1")
        _add_txn(conn, "100", "acct-2")
        assert queries.find_transaction_ids(conn, "100", "acct-1") == [mine]

    def test_unscoped_lookup_finds_every_account(
        self, conn: sqlite3.Connection
    ) -> None:
        a = _add_txn(conn, "100", "acct-1")
        b = _add_txn(conn, "100", "acct-2")
        assert sorted(queries.find_transaction_ids(conn, "100", None)) == [a, b]

    def test_missing_transaction_gives_empty_list(
        self, conn: sqlite3.Connection
    ) -> None:
        assert queries.find_transaction_ids(conn, "999", None) == []

    def test_get_transaction_id(self, conn: sqlite3.Connection) -> None:
        txn = _add_txn(conn, "100", "acct-1")
        assert queries.get_transaction_id(conn, "100", "acct-1") == txn
        assert queries.get_transaction_id(conn, "100", "acct-2") is None


# ---------------------------------------------------------------------------
# Notes and tags
# ---------------------------------------------------------------------------


class TestNotesAndTags:
    def test_add_note_persists(self, conn: sqlite3.Connection) -> None:
        txn = _add_txn(conn, "100")
        queries.add_note(conn, txn, "breakout entry")
        rows = conn.execute(
            "SELECT body FROM notes WHERE transaction_id = ?", (txn,)
        ).fetchall()
        assert [r[0] for r in rows] == ["breakout entry"]

    def test_add_tags_counts_only_new_tags(self, conn: sqlite3.Connection) -> None:
        txn = _add_txn(conn, "100")
        assert queries.add_tags(conn, txn, ["breakout", "london"]) == 2
        # Re-adding one existing tag plus one new one counts only the new one.
        assert queries.add_tags(conn, txn, ["breakout", "trend"]) == 1
        assert queries.list_tags(conn) == ["breakout", "london", "trend"]

    def test_add_tags_with_no_tags_is_a_no_op(self, conn: sqlite3.Connection) -> None:
        txn = _add_txn(conn, "100")
        assert queries.add_tags(conn, txn, []) == 0


# ---------------------------------------------------------------------------
# Completion sources
# ---------------------------------------------------------------------------


class TestCompletionSources:
    def test_prefix_match_newest_first(self, conn: sqlite3.Connection) -> None:
        for oanda_id in ("12", "120", "125", "300"):
            _add_txn(conn, oanda_id)
        assert queries.list_oanda_ids_with_prefix(conn, "12", None) == [
            "125",
            "120",
            "12",
        ]

    def test_prefix_match_scoped_to_account(self, conn: sqlite3.Connection) -> None:
        _add_txn(conn, "120", "acct-1")
        _add_txn(conn, "125", "acct-2")
        assert queries.list_oanda_ids_with_prefix(conn, "12", "acct-2") == ["125"]

    def test_prefix_match_respects_limit(self, conn: sqlite3.Connection) -> None:
        for n in range(5):
            _add_txn(conn, f"1{n}")
        assert len(queries.list_oanda_ids_with_prefix(conn, "1", None, limit=3)) == 3

    def test_transaction_types_are_distinct_and_sorted(
        self, conn: sqlite3.Connection
    ) -> None:
        _add_txn(conn, "1", txn_type="ORDER_FILL")
        _add_txn(conn, "2", txn_type="DAILY_FINANCING")
        _add_txn(conn, "3", txn_type="ORDER_FILL")
        assert queries.list_transaction_types(conn) == [
            "DAILY_FINANCING",
            "ORDER_FILL",
        ]
