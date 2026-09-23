"""Tests for the sync module.

All tests use in-memory SQLite databases (via the ``db`` fixture) and a
``FakeClient`` test double in place of the real ``OandaClient``.

The ``FakeClient`` satisfies ``ClientProtocol`` structurally — it has the
same ``account_id`` attribute and ``get_transactions_since`` signature, with
no inheritance from any shared base.  mypy will verify this at type-check
time.

We test the public surface (``sync_cold`` and ``sync_incremental``) rather
than the private helpers, so the internal implementation can change without
breaking the test suite.  The one exception is that we DO inspect the
``sync_cursors`` and ``transactions`` tables directly to verify side effects,
which is correct because those side effects are part of the public contract.

Test classes
------------
TestFakeClientProtocol   — sanity-check that FakeClient satisfies the Protocol
TestSyncCold             — full-history ingestion, cursor writing, deduplication
TestSyncIncremental      — cursor reading, delta ingestion, no-new-rows case
TestParentChildLinking   — DAILY_FINANCING parent/child FK resolution
TestEntryOrderJournal    — notes/tags/plan move from an entry order to its fill
TestSyncCsv              — CSV import: ingestion, dedup, forward-only cursor
"""

from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass, field
from pathlib import Path

import pytest

from frmj.execution.oanda import TransactionRow
from frmj.execution.sync import SyncResult, sync_cold, sync_csv, sync_incremental
from frmj.persistence.schema import ensure_schema


# ---------------------------------------------------------------------------
# Test double
# ---------------------------------------------------------------------------


@dataclass
class FakeClient:
    """
    Minimal implementation of ClientProtocol for use in unit tests.

    ``responses`` is a list of lists: each call to ``get_transactions_since``
    pops the first element and returns it.  An empty outer list (or an
    exhausted one) returns [].  This lets tests simulate multiple sequential
    calls with different data.

    ``calls`` accumulates the ``from_id`` argument of each call so tests can
    assert on what was actually requested.
    """

    account_id: str
    responses: list[list[TransactionRow]] = field(default_factory=list)
    calls: list[str | None] = field(default_factory=list)

    def get_transactions_since(
        self, from_id: str | None = None
    ) -> list[TransactionRow]:
        self.calls.append(from_id)
        if not self.responses:
            return []
        return self.responses.pop(0)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _row(
    oanda_id: str,
    *,
    account_id: str = "acct-1",
    type_: str = "ORDER_FILL",
    time: str = "2026-04-25T12:00:00.000000Z",
    parent_oanda_id: str | None = None,
    raw_json: str = "{}",
) -> TransactionRow:
    """Build a TransactionRow with sensible defaults."""
    return TransactionRow(
        oanda_id=oanda_id,
        account_id=account_id,
        type=type_,
        time=time,
        parent_oanda_id=parent_oanda_id,
        raw_json=raw_json,
    )


def _count_transactions(conn: sqlite3.Connection) -> int:
    return conn.execute("SELECT COUNT(*) FROM transactions").fetchone()[0]


def _read_cursor(conn: sqlite3.Connection, account_id: str) -> str | None:
    row = conn.execute(
        "SELECT last_oanda_id FROM sync_cursors WHERE account_id = ?",
        (account_id,),
    ).fetchone()
    return row[0] if row else None


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture()
def db() -> sqlite3.Connection:
    """Fresh in-memory database with FRoMaJ schema applied."""
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    ensure_schema(conn)
    return conn


# ---------------------------------------------------------------------------
# Protocol satisfaction
# ---------------------------------------------------------------------------


class TestFakeClientProtocol:
    """Verify that FakeClient structurally satisfies ClientProtocol.

    These tests don't call sync functions — they just confirm that our
    test double looks right from the protocol's perspective, so a type-check
    failure in the sync tests means a real contract mismatch, not a test
    setup bug.
    """

    def test_has_account_id_attribute(self) -> None:
        client = FakeClient(account_id="acct-1")
        assert client.account_id == "acct-1"

    def test_get_transactions_since_returns_list(self) -> None:
        client = FakeClient(account_id="acct-1", responses=[[_row("1")]])
        result = client.get_transactions_since()
        assert isinstance(result, list)
        assert result[0].oanda_id == "1"

    def test_exhausted_responses_return_empty_list(self) -> None:
        client = FakeClient(account_id="acct-1", responses=[])
        assert client.get_transactions_since() == []

    def test_records_from_id_argument(self) -> None:
        client = FakeClient(account_id="acct-1")
        client.get_transactions_since(from_id="500")
        assert client.calls == ["500"]


# ---------------------------------------------------------------------------
# sync_cold
# ---------------------------------------------------------------------------


class TestSyncCold:
    """Full-history ingestion via sync_cold."""

    def test_empty_account_returns_zero_result(self, db: sqlite3.Connection) -> None:
        """No rows from client → SyncResult with zeros and no cursor written."""
        client = FakeClient(account_id="acct-1", responses=[[]])
        result = sync_cold(db, client)
        assert result == SyncResult(rows_ingested=0, rows_skipped=0, last_oanda_id=None)
        assert _count_transactions(db) == 0
        assert _read_cursor(db, "acct-1") is None

    def test_ingests_all_rows(self, db: sqlite3.Connection) -> None:
        """All rows from client must appear in the transactions table."""
        rows = [_row("1"), _row("2"), _row("3")]
        client = FakeClient(account_id="acct-1", responses=[rows])
        result = sync_cold(db, client)
        assert result.rows_ingested == 3
        assert result.rows_skipped == 0
        assert _count_transactions(db) == 3

    def test_writes_cursor_to_last_row_id(self, db: sqlite3.Connection) -> None:
        """Cursor must be set to the oanda_id of the last row in the batch."""
        rows = [_row("10"), _row("20"), _row("30")]
        client = FakeClient(account_id="acct-1", responses=[rows])
        result = sync_cold(db, client)
        assert result.last_oanda_id == "30"
        assert _read_cursor(db, "acct-1") == "30"

    def test_calls_get_transactions_since_with_no_from_id(
        self, db: sqlite3.Connection
    ) -> None:
        """Cold sync must request the full history (from_id=None)."""
        client = FakeClient(account_id="acct-1", responses=[[]])
        sync_cold(db, client)
        assert client.calls == [None]

    def test_duplicate_rows_skipped_not_raised(self, db: sqlite3.Connection) -> None:
        """Running cold sync twice on the same data must not raise.

        The second run should skip all rows via the unique index and return
        rows_skipped == len(rows), rows_ingested == 0.
        """
        rows = [_row("1"), _row("2")]
        client_1 = FakeClient(account_id="acct-1", responses=[rows])
        sync_cold(db, client_1)

        client_2 = FakeClient(account_id="acct-1", responses=[list(rows)])
        result = sync_cold(db, client_2)
        assert result.rows_ingested == 0
        assert result.rows_skipped == 2
        # Database must still have exactly 2 rows, not 4.
        assert _count_transactions(db) == 2

    def test_partial_overlap_ingests_new_rows_only(
        self, db: sqlite3.Connection
    ) -> None:
        """If the second cold sync contains both old and new rows, only new ones land."""
        first_batch = [_row("1"), _row("2")]
        second_batch = [_row("2"), _row("3")]  # row "2" is a duplicate

        sync_cold(db, FakeClient(account_id="acct-1", responses=[first_batch]))
        result = sync_cold(
            db, FakeClient(account_id="acct-1", responses=[second_batch])
        )

        assert result.rows_ingested == 1  # only "3"
        assert result.rows_skipped == 1  # "2" was already there
        assert _count_transactions(db) == 3

    def test_raw_json_stored_verbatim(self, db: sqlite3.Connection) -> None:
        """The raw_json field must be preserved exactly as supplied."""
        payload = '{"type":"ORDER_FILL","instrument":"EUR_USD"}'
        client = FakeClient(
            account_id="acct-1", responses=[[_row("1", raw_json=payload)]]
        )
        sync_cold(db, client)
        row = db.execute(
            "SELECT raw_json FROM transactions WHERE oanda_id = '1'"
        ).fetchone()
        assert row["raw_json"] == payload


# ---------------------------------------------------------------------------
# sync_incremental
# ---------------------------------------------------------------------------


class TestSyncIncremental:
    """Incremental sync: reads cursor, fetches delta, advances cursor."""

    def test_no_cursor_delegates_to_cold(self, db: sqlite3.Connection) -> None:
        """With no cursor row, incremental must behave identically to cold sync."""
        rows = [_row("1"), _row("2")]
        client = FakeClient(account_id="acct-1", responses=[rows])
        result = sync_incremental(db, client)
        assert result.rows_ingested == 2
        assert result.last_oanda_id == "2"
        # The delegation to cold sync calls get_transactions_since(from_id=None).
        assert client.calls == [None]

    def test_reads_cursor_and_passes_as_from_id(self, db: sqlite3.Connection) -> None:
        """With an existing cursor, the cursor value must be passed as from_id."""
        # Seed the cursor manually.
        db.execute(
            "INSERT INTO sync_cursors (account_id, last_oanda_id, synced_at) "
            "VALUES (?, ?, ?)",
            ("acct-1", "500", "2026-04-25T00:00:00Z"),
        )
        db.commit()

        client = FakeClient(account_id="acct-1", responses=[[]])
        sync_incremental(db, client)
        # from_id must equal the cursor value, not None.
        assert client.calls == ["500"]

    def test_ingests_new_rows_and_advances_cursor(self, db: sqlite3.Connection) -> None:
        """New rows since the cursor must be ingested and cursor advanced."""
        db.execute(
            "INSERT INTO sync_cursors (account_id, last_oanda_id, synced_at) "
            "VALUES (?, ?, ?)",
            ("acct-1", "10", "2026-04-25T00:00:00Z"),
        )
        db.commit()

        new_rows = [_row("11"), _row("12"), _row("13")]
        client = FakeClient(account_id="acct-1", responses=[new_rows])
        result = sync_incremental(db, client)

        assert result.rows_ingested == 3
        assert result.last_oanda_id == "13"
        assert _read_cursor(db, "acct-1") == "13"

    def test_no_new_rows_preserves_cursor(self, db: sqlite3.Connection) -> None:
        """When the client returns no rows, the cursor must not change."""
        db.execute(
            "INSERT INTO sync_cursors (account_id, last_oanda_id, synced_at) "
            "VALUES (?, ?, ?)",
            ("acct-1", "99", "2026-04-25T00:00:00Z"),
        )
        db.commit()

        client = FakeClient(account_id="acct-1", responses=[[]])
        result = sync_incremental(db, client)

        assert result.rows_ingested == 0
        assert result.rows_skipped == 0
        assert result.last_oanda_id == "99"  # cursor value echoed back
        assert _read_cursor(db, "acct-1") == "99"  # unchanged

    def test_duplicate_rows_skipped_in_incremental(
        self, db: sqlite3.Connection
    ) -> None:
        """Duplicate rows in an incremental batch are silently skipped."""
        # Seed the database with row "1" and a cursor.
        sync_cold(db, FakeClient(account_id="acct-1", responses=[[_row("1")]]))

        # Incremental batch overlaps: row "1" is a duplicate, row "2" is new.
        new_rows = [_row("1"), _row("2")]
        client = FakeClient(account_id="acct-1", responses=[new_rows])
        result = sync_incremental(db, client)

        assert result.rows_ingested == 1
        assert result.rows_skipped == 1
        assert _count_transactions(db) == 2  # not 3


# ---------------------------------------------------------------------------
# Parent/child linking
# ---------------------------------------------------------------------------


class TestParentChildLinking:
    """DAILY_FINANCING parent/child rows are linked correctly via parent_id."""

    def test_within_batch_parent_child(self, db: sqlite3.Connection) -> None:
        """A parent and its child in the same batch are linked by parent_id."""
        parent = _row("100", type_="DAILY_FINANCING")
        child = _row("101", type_="DAILY_FINANCING", parent_oanda_id="100")

        client = FakeClient(account_id="acct-1", responses=[[parent, child]])
        sync_cold(db, client)

        parent_row = db.execute(
            "SELECT id FROM transactions WHERE oanda_id = '100'"
        ).fetchone()
        child_row = db.execute(
            "SELECT parent_id FROM transactions WHERE oanda_id = '101'"
        ).fetchone()

        assert child_row["parent_id"] == parent_row["id"]

    def test_cross_batch_parent_child(self, db: sqlite3.Connection) -> None:
        """A child whose parent was ingested in a prior sync run is linked correctly."""
        # First sync: parent only.
        client_1 = FakeClient(
            account_id="acct-1", responses=[[_row("200", type_="DAILY_FINANCING")]]
        )
        sync_cold(db, client_1)

        # Second sync: child references the parent from the first run.
        child = _row("201", type_="DAILY_FINANCING", parent_oanda_id="200")
        client_2 = FakeClient(account_id="acct-1", responses=[[child]])
        sync_incremental(db, client_2)

        parent_id_in_db = db.execute(
            "SELECT id FROM transactions WHERE oanda_id = '200'"
        ).fetchone()["id"]
        child_parent_id = db.execute(
            "SELECT parent_id FROM transactions WHERE oanda_id = '201'"
        ).fetchone()["parent_id"]

        assert child_parent_id == parent_id_in_db

    def test_orphan_child_inserted_with_null_parent(
        self, db: sqlite3.Connection
    ) -> None:
        """A child referencing a missing parent is inserted with parent_id=NULL.

        This should not happen with correct Oanda data, but we degrade
        gracefully rather than aborting the whole batch.
        """
        child = _row("999", parent_oanda_id="no-such-parent")
        client = FakeClient(account_id="acct-1", responses=[[child]])
        result = sync_cold(db, client)

        # The row must have been ingested (not skipped or raised).
        assert result.rows_ingested == 1

        row = db.execute(
            "SELECT parent_id FROM transactions WHERE oanda_id = '999'"
        ).fetchone()
        assert row["parent_id"] is None

    def test_multiple_children_same_parent(self, db: sqlite3.Connection) -> None:
        """Multiple children in one batch all link to the same parent row."""
        parent = _row("300", type_="DAILY_FINANCING")
        children = [
            _row(f"30{i}", type_="DAILY_FINANCING", parent_oanda_id="300")
            for i in range(1, 4)
        ]
        client = FakeClient(account_id="acct-1", responses=[[parent] + children])
        sync_cold(db, client)

        parent_synthetic_id = db.execute(
            "SELECT id FROM transactions WHERE oanda_id = '300'"
        ).fetchone()["id"]
        linked = db.execute(
            "SELECT COUNT(*) FROM transactions WHERE parent_id = ?",
            (parent_synthetic_id,),
        ).fetchone()[0]
        assert linked == 3


# ---------------------------------------------------------------------------
# sync_csv
# ---------------------------------------------------------------------------

_CSV_HEADER = (
    "TICKET,TRANSACTION DATE,TRANSACTION TYPE,DETAILS,INSTRUMENT,PRICE,"
    "UNITS,DIRECTION,ESTIMATED SPREAD COST,STOP LOSS,TAKE PROFIT,"
    "TRAILING STOP,FINANCING,FUNDING RATE,COMMISSION,CONVERSION RATE,"
    "CONVERSION FEE,PL,AMOUNT,BALANCE"
)


def _csv_row(oanda_id: str, time: str = "2026-02-03 00:00:00 UTC") -> str:
    """A minimal, valid MARGIN_CALL_ENTER-style CSV data line for *oanda_id*."""
    return (
        f'"{oanda_id}-0","{time}","MARGIN_CALL_ENTER","",'
        '"","","","","","","","","","","","","","","",""'
    )


def _write_csv(path: Path, *rows: str) -> Path:
    path.write_text("\n".join((_CSV_HEADER, *rows)), encoding="utf-8")
    return path


def _txn_id(conn: sqlite3.Connection, oanda_id: str) -> int:
    return conn.execute(
        "SELECT id FROM transactions WHERE oanda_id = ?", (oanda_id,)
    ).fetchone()[0]


def _journal_owner(conn: sqlite3.Connection, table: str) -> list[str]:
    """Oanda IDs of the transactions that *table*'s rows point at."""
    return [
        r[0]
        for r in conn.execute(
            f"SELECT t.oanda_id FROM {table} x "
            "JOIN transactions t ON x.transaction_id = t.id ORDER BY x.id"
        ).fetchall()
    ]


def _fill(oanda_id: str, order_id: str, **kwargs: str) -> TransactionRow:
    return _row(
        oanda_id,
        raw_json=json.dumps({"type": "ORDER_FILL", "orderID": order_id}),
        **kwargs,
    )


class TestEntryOrderJournal:
    """When a pending entry order fills, its journal moves to the ORDER_FILL."""

    def _seed_order_with_journal(
        self, db: sqlite3.Connection, order_id: str = "100", type_: str = "LIMIT_ORDER"
    ) -> None:
        """Sync an order transaction and attach a note, tag, and plan to it."""
        client = FakeClient("acct-1", responses=[[_row(order_id, type_=type_)]])
        sync_incremental(db, client)
        txn = _txn_id(db, order_id)
        db.execute("INSERT INTO notes (transaction_id, body) VALUES (?, 'n')", (txn,))
        db.execute("INSERT INTO tags (transaction_id, tag) VALUES (?, 't')", (txn,))
        db.execute(
            "INSERT INTO trade_plans (transaction_id, tp_price, sl_price) "
            "VALUES (?, '1.1', '1.0')",
            (txn,),
        )
        db.commit()

    def test_fill_in_later_sync_takes_over_journal(
        self, db: sqlite3.Connection
    ) -> None:
        self._seed_order_with_journal(db)
        client = FakeClient("acct-1", responses=[[_fill("105", "100")]])
        sync_incremental(db, client)
        assert _journal_owner(db, "notes") == ["105"]
        assert _journal_owner(db, "tags") == ["105"]
        assert _journal_owner(db, "trade_plans") == ["105"]

    @pytest.mark.parametrize("type_", ["STOP_ORDER", "MARKET_IF_TOUCHED_ORDER"])
    def test_other_entry_order_types_link_too(
        self, db: sqlite3.Connection, type_: str
    ) -> None:
        self._seed_order_with_journal(db, type_=type_)
        sync_incremental(db, FakeClient("acct-1", responses=[[_fill("105", "100")]]))
        assert _journal_owner(db, "notes") == ["105"]

    def test_non_entry_order_journal_stays_put(self, db: sqlite3.Connection) -> None:
        """A note on a TP order must not move to the fill that closes the trade."""
        self._seed_order_with_journal(db, type_="TAKE_PROFIT_ORDER")
        sync_incremental(db, FakeClient("acct-1", responses=[[_fill("105", "100")]]))
        assert _journal_owner(db, "notes") == ["100"]
        assert _journal_owner(db, "trade_plans") == ["100"]

    def test_fill_for_other_order_leaves_journal(self, db: sqlite3.Connection) -> None:
        self._seed_order_with_journal(db)
        sync_incremental(db, FakeClient("acct-1", responses=[[_fill("105", "999")]]))
        assert _journal_owner(db, "notes") == ["100"]

    def test_same_order_id_on_other_account_not_linked(
        self, db: sqlite3.Connection
    ) -> None:
        self._seed_order_with_journal(db)
        other = FakeClient(
            "acct-2", responses=[[_fill("105", "100", account_id="acct-2")]]
        )
        sync_incremental(db, other)
        assert _journal_owner(db, "notes") == ["100"]

    def test_second_partial_fill_does_not_steal_journal(
        self, db: sqlite3.Connection
    ) -> None:
        self._seed_order_with_journal(db)
        sync_incremental(db, FakeClient("acct-1", responses=[[_fill("105", "100")]]))
        sync_incremental(db, FakeClient("acct-1", responses=[[_fill("110", "100")]]))
        assert _journal_owner(db, "notes") == ["105"]
        assert _journal_owner(db, "trade_plans") == ["105"]

    def test_resynced_duplicate_fill_is_harmless(self, db: sqlite3.Connection) -> None:
        self._seed_order_with_journal(db)
        sync_incremental(db, FakeClient("acct-1", responses=[[_fill("105", "100")]]))
        sync_cold(db, FakeClient("acct-1", responses=[[_fill("105", "100")]]))
        assert _journal_owner(db, "notes") == ["105"]

    def test_fill_without_order_id_or_bad_json_is_ignored(
        self, db: sqlite3.Connection
    ) -> None:
        self._seed_order_with_journal(db)
        client = FakeClient(
            "acct-1",
            responses=[[_row("105", raw_json="{}"), _row("106", raw_json="not json")]],
        )
        result = sync_incremental(db, client)
        assert result.rows_ingested == 2
        assert _journal_owner(db, "notes") == ["100"]


class TestSyncCsv:
    """CSV import: ingestion, deduplication, forward-only cursor advance."""

    def test_ingests_rows_and_writes_cursor(
        self, db: sqlite3.Connection, tmp_path: Path
    ) -> None:
        csv_path = _write_csv(tmp_path / "history.csv", _csv_row("10"), _csv_row("20"))
        result = sync_csv(db, "acct-1", csv_path)
        assert result == SyncResult(rows_ingested=2, rows_skipped=0, last_oanda_id="20")
        assert _count_transactions(db) == 2
        assert _read_cursor(db, "acct-1") == "20"

    def test_empty_csv_returns_zero_result_no_cursor(
        self, db: sqlite3.Connection, tmp_path: Path
    ) -> None:
        csv_path = _write_csv(tmp_path / "history.csv")
        result = sync_csv(db, "acct-1", csv_path)
        assert result == SyncResult(rows_ingested=0, rows_skipped=0, last_oanda_id=None)
        assert _read_cursor(db, "acct-1") is None

    def test_reimporting_same_csv_skips_all_rows(
        self, db: sqlite3.Connection, tmp_path: Path
    ) -> None:
        csv_path = _write_csv(tmp_path / "history.csv", _csv_row("10"), _csv_row("20"))
        sync_csv(db, "acct-1", csv_path)
        result = sync_csv(db, "acct-1", csv_path)
        assert result.rows_ingested == 0
        assert result.rows_skipped == 2
        assert _count_transactions(db) == 2

    def test_cursor_does_not_regress_on_older_csv(
        self, db: sqlite3.Connection, tmp_path: Path
    ) -> None:
        """Importing an older CSV after a newer cursor already exists must
        leave the cursor at the newer value, even though the older rows are
        still ingested.
        """
        newer = _write_csv(tmp_path / "newer.csv", _csv_row("500"))
        sync_csv(db, "acct-1", newer)
        assert _read_cursor(db, "acct-1") == "500"

        older = _write_csv(tmp_path / "older.csv", _csv_row("100"))
        result = sync_csv(db, "acct-1", older)
        assert result.rows_ingested == 1
        assert _read_cursor(db, "acct-1") == "500"

    def test_separate_accounts_do_not_share_a_cursor(
        self, db: sqlite3.Connection, tmp_path: Path
    ) -> None:
        csv_path = _write_csv(tmp_path / "history.csv", _csv_row("10"))
        sync_csv(db, "acct-1", csv_path)
        sync_csv(db, "acct-2", csv_path)
        assert _read_cursor(db, "acct-1") == "10"
        assert _read_cursor(db, "acct-2") == "10"
        assert _count_transactions(db) == 2
