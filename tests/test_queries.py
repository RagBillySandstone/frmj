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


# ---------------------------------------------------------------------------
# Transaction listings
# ---------------------------------------------------------------------------


def _ids(rows: list[sqlite3.Row]) -> list[str]:
    """The Oanda IDs of *rows*, in order."""
    return [row["oanda_id"] for row in rows]


class TestListRecentTransactions:
    def test_single_account_newest_id_first_with_limit(
        self, conn: sqlite3.Connection
    ) -> None:
        for oanda_id in ("9", "10", "11"):
            _add_txn(conn, oanda_id, "acct-1")
        _add_txn(conn, "50", "acct-2")
        rows = queries.list_recent_transactions(conn, 2, account_id="acct-1")
        # Numeric, not string, ordering: 11 > 10 > 9.
        assert _ids(rows) == ["11", "10"]

    def test_all_accounts_ordered_by_time(self, conn: sqlite3.Connection) -> None:
        # The highest ID is the oldest, so ID ordering would put it first.
        _add_txn(conn, "999", "acct-2", time="2026-01-01T00:00:00Z")
        _add_txn(conn, "5", "acct-1", time="2026-03-01T00:00:00Z")
        rows = queries.list_recent_transactions(conn, 10)
        assert _ids(rows) == ["5", "999"]

    def test_type_since_and_instrument_filters(self, conn: sqlite3.Connection) -> None:
        _add_txn(conn, "1", raw_json='{"instrument":"EUR_USD"}')
        _add_txn(conn, "2", raw_json='{"instrument":"GBP_USD"}')
        _add_txn(conn, "3", txn_type="DAILY_FINANCING")
        _add_txn(conn, "4", raw_json='{"instrument":"EUR_USD"}', time="2025-01-01")
        rows = queries.list_recent_transactions(
            conn,
            10,
            account_id="acct-1",
            txn_type="ORDER_FILL",
            since="2026-01-01",
            instrument="eur_usd",
        )
        assert _ids(rows) == ["1"]

    def test_notes_and_tag_filters(self, conn: sqlite3.Connection) -> None:
        noted = _add_txn(conn, "1")
        tagged = _add_txn(conn, "2")
        _add_txn(conn, "3")
        queries.add_note(conn, noted, "x")
        queries.add_tags(conn, tagged, ["breakout"])
        assert _ids(queries.list_recent_transactions(conn, 10, with_notes=True)) == [
            "1"
        ]
        # Tag matching is case-insensitive; tags are stored lowercase.
        assert _ids(queries.list_recent_transactions(conn, 10, tag="Breakout")) == ["2"]


class TestListTransactionsChronological:
    def test_every_account_oldest_first(self, conn: sqlite3.Connection) -> None:
        _add_txn(conn, "1", "acct-1", time="2026-03-01T00:00:00Z")
        _add_txn(conn, "2", "acct-2", time="2026-01-01T00:00:00Z")
        assert _ids(queries.list_transactions_chronological(conn)) == ["2", "1"]

    def test_filters(self, conn: sqlite3.Connection) -> None:
        _add_txn(conn, "1", raw_json='{"instrument":"EUR_USD"}')
        _add_txn(conn, "2", raw_json='{"instrument":"GBP_USD"}')
        rows = queries.list_transactions_chronological(conn, instrument="EUR_USD")
        assert _ids(rows) == ["1"]


# ---------------------------------------------------------------------------
# Per-transaction annotations
# ---------------------------------------------------------------------------


class TestAnnotations:
    def test_trade_plan(self, conn: sqlite3.Connection) -> None:
        txn = _add_txn(conn, "1")
        assert queries.get_trade_plan(conn, txn) is None
        conn.execute(
            "INSERT INTO trade_plans (transaction_id, tp_price, sl_price) "
            "VALUES (?, '1.1', '1.0')",
            (txn,),
        )
        plan = queries.get_trade_plan(conn, txn)
        assert plan is not None
        assert (plan["tp_price"], plan["sl_price"]) == ("1.1", "1.0")

    def test_notes_oldest_first_and_tags_sorted(self, conn: sqlite3.Connection) -> None:
        txn = _add_txn(conn, "1")
        queries.add_note(conn, txn, "first")
        queries.add_note(conn, txn, "second")
        queries.add_tags(conn, txn, ["zeta", "alpha"])
        assert queries.list_notes(conn, txn) == ["first", "second"]
        assert queries.list_transaction_tags(conn, txn) == ["alpha", "zeta"]

    def test_notes_by_transaction(self, conn: sqlite3.Connection) -> None:
        a = _add_txn(conn, "1")
        b = _add_txn(conn, "2")
        queries.add_note(conn, a, "one")
        queries.add_note(conn, a, "two")
        assert queries.notes_by_transaction(conn, [a, b]) == {a: ["one", "two"]}
        assert queries.notes_by_transaction(conn, []) == {}

    def test_notes_by_transaction_beyond_sqlite_parameter_limit(
        self, conn: sqlite3.Connection
    ) -> None:
        """More IDs than SQLite allows bound parameters in one statement
        (32766 on current builds) must not raise "too many SQL variables"."""
        first = _add_txn(conn, "1")
        last = _add_txn(conn, "2")
        queries.add_note(conn, first, "first")
        queries.add_note(conn, last, "last")
        # Pad with IDs that have no notes, putting the real ones at both ends
        # so they land in different batches.
        ids = [first, *range(10_000, 50_000), last]
        assert queries.notes_by_transaction(conn, ids) == {
            first: ["first"],
            last: ["last"],
        }


# ---------------------------------------------------------------------------
# Statistics inputs
# ---------------------------------------------------------------------------


class TestStatsInputs:
    def test_fill_open_time_from_full_close(self, conn: sqlite3.Connection) -> None:
        _add_txn(conn, "10", time="2026-04-01T08:00:00Z")
        _add_txn(
            conn,
            "11",
            time="2026-04-02T08:00:00Z",
            raw_json='{"pl":"5","tradesClosed":[{"tradeID":"10"}]}',
        )
        rows = {r["oanda_id"]: r for r in queries.list_fills_with_open_time(conn, None)}
        assert rows["11"]["open_time"] == "2026-04-01T08:00:00Z"
        assert rows["10"]["open_time"] is None

    def test_fill_open_time_from_partial_reduce(self, conn: sqlite3.Connection) -> None:
        _add_txn(conn, "10", time="2026-04-01T08:00:00Z")
        _add_txn(conn, "11", raw_json='{"pl":"5","tradeReduced":{"tradeID":"10"}}')
        rows = {r["oanda_id"]: r for r in queries.list_fills_with_open_time(conn, None)}
        assert rows["11"]["open_time"] == "2026-04-01T08:00:00Z"

    def test_open_fill_only_matched_within_same_account(
        self, conn: sqlite3.Connection
    ) -> None:
        # Trade 10 was opened on acct-2; acct-1's close of "its" trade 10
        # must not pick that up.
        _add_txn(conn, "10", "acct-2", time="2026-04-01T08:00:00Z")
        _add_txn(conn, "11", "acct-1", raw_json='{"tradesClosed":[{"tradeID":"10"}]}')
        rows = queries.list_fills_with_open_time(conn, "acct-1")
        assert [(r["oanda_id"], r["open_time"]) for r in rows] == [("11", None)]

    def test_fills_exclude_other_types(self, conn: sqlite3.Connection) -> None:
        _add_txn(conn, "1")
        _add_txn(conn, "2", txn_type="DAILY_FINANCING")
        assert _ids(queries.list_fills_with_open_time(conn, None)) == ["1"]

    def test_tagged_fills_scoped(self, conn: sqlite3.Connection) -> None:
        mine = _add_txn(conn, "1", "acct-1", raw_json='{"pl":"5"}')
        other = _add_txn(conn, "1", "acct-2")
        financing = _add_txn(conn, "2", "acct-1", txn_type="DAILY_FINANCING")
        queries.add_tags(conn, mine, ["breakout"])
        queries.add_tags(conn, other, ["other"])
        queries.add_tags(conn, financing, ["ignored"])
        rows = queries.list_tagged_fills(conn, "acct-1")
        assert [(r["tag"], r["raw_json"]) for r in rows] == [("breakout", '{"pl":"5"}')]
        assert len(queries.list_tagged_fills(conn, None)) == 2

    def test_daily_financing_scoped(self, conn: sqlite3.Connection) -> None:
        _add_txn(conn, "1", "acct-1", txn_type="DAILY_FINANCING", raw_json='{"a":1}')
        _add_txn(conn, "1", "acct-2", txn_type="DAILY_FINANCING", raw_json='{"b":2}')
        _add_txn(conn, "2", "acct-1")
        assert queries.list_daily_financing(conn, "acct-1") == ['{"a":1}']
        assert sorted(queries.list_daily_financing(conn, None)) == [
            '{"a":1}',
            '{"b":2}',
        ]


# ---------------------------------------------------------------------------
# Positions and sync --watch
# ---------------------------------------------------------------------------


class TestPositionsAndWatch:
    def test_count_notes_for_oanda_id(self, conn: sqlite3.Connection) -> None:
        txn = _add_txn(conn, "100", "acct-1")
        assert queries.count_notes_for_oanda_id(conn, "100", "acct-1") == 0
        queries.add_note(conn, txn, "a")
        queries.add_note(conn, txn, "b")
        assert queries.count_notes_for_oanda_id(conn, "100", "acct-1") == 2

    def test_count_notes_ignores_other_accounts(self, conn: sqlite3.Connection) -> None:
        """Oanda IDs repeat across accounts: another account's note on the
        same ID must not count."""
        _add_txn(conn, "100", "acct-1")
        other = _add_txn(conn, "100", "acct-2")
        queries.add_note(conn, other, "elsewhere")
        assert queries.count_notes_for_oanda_id(conn, "100", "acct-1") == 0
        assert queries.count_notes_for_oanda_id(conn, "100", "acct-2") == 1

    def test_transactions_after_cursor(self, conn: sqlite3.Connection) -> None:
        _add_txn(conn, "9", time="2026-04-01T00:00:00Z")
        _add_txn(conn, "11", time="2026-04-03T00:00:00Z")
        _add_txn(conn, "10", time="2026-04-02T00:00:00Z")
        _add_txn(conn, "12", "acct-2")
        rows = queries.list_transactions_after(conn, "acct-1", "9")
        # Numeric comparison (11 > 9), ordered by time.
        assert _ids(rows) == ["10", "11"]
