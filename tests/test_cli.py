"""Tests for CLI commands: sync, config, note, journal, and trade --dry-run.

We use typer's CliRunner to invoke commands in-process.  Network calls are
avoided by monkeypatching ``frmj.cli.get_client`` with lightweight fakes:

* ``FakeClient``      — satisfies ClientProtocol for sync tests.
* ``FakeFullClient``  — additionally provides account_summary, instrument,
                        price, open_tickets, and place_market_order; used for
                        the trade --dry-run test.

The ``trade`` command in normal (non-dry-run) mode requires interactive
confirmation and live Oanda data; those paths are covered by integration tests.
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass, field
from decimal import Decimal
from pathlib import Path

import pytest
from typer.testing import CliRunner

from frmj.app import get_db, set_config
from frmj.cli import app
from frmj.domain.sizing import Direction, InstrumentSpec, PriceQuote
from frmj.execution.oanda import AccountSummary, CloseFill, OpenTrade, OrderFill, TransactionRow

runner = CliRunner()


# ---------------------------------------------------------------------------
# Fake clients
# ---------------------------------------------------------------------------


@dataclass
class FakeClient:
    account_id: str
    responses: list[list[TransactionRow]] = field(default_factory=list)

    def get_transactions_since(
        self, from_id: str | None = None
    ) -> list[TransactionRow]:
        if not self.responses:
            return []
        return self.responses.pop(0)


def _row(oanda_id: str, account_id: str = "acct-1") -> TransactionRow:
    return TransactionRow(
        oanda_id=oanda_id,
        account_id=account_id,
        type="ORDER_FILL",
        time="2026-04-25T12:00:00.000000Z",
        parent_oanda_id=None,
        raw_json="{}",
    )


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture()
def db_path(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Set FRMJ_DB_PATH to a temp location and seed account_id + OANDA_API_TOKEN."""
    path = tmp_path / "frmj_test.db"
    monkeypatch.setenv("FRMJ_DB_PATH", str(path))
    monkeypatch.setenv("OANDA_API_TOKEN", "test-token-123")
    # Seed account_id so get_client doesn't raise.
    conn = get_db(path=path)
    set_config(conn, "account_id", "acct-1")
    conn.close()
    return path


# ---------------------------------------------------------------------------
# sync command
# ---------------------------------------------------------------------------


class TestSyncCommand:
    def test_sync_incremental_success(
        self,
        db_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """``frmj sync`` exits 0 and reports ingested count."""
        rows = [_row("1"), _row("2"), _row("3")]
        monkeypatch.setattr(
            "frmj.cli.get_client",
            lambda conn: FakeClient(account_id="acct-1", responses=[rows]),
        )
        result = runner.invoke(app, ["sync"])
        assert result.exit_code == 0, result.output
        assert "3 ingested" in result.output

    def test_sync_cold_flag(
        self,
        db_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """``frmj sync --cold`` reports 'cold' in the output."""
        monkeypatch.setattr(
            "frmj.cli.get_client",
            lambda conn: FakeClient(account_id="acct-1", responses=[[]]),
        )
        result = runner.invoke(app, ["sync", "--cold"])
        assert result.exit_code == 0, result.output
        assert "cold" in result.output

    def test_sync_no_rows_reports_zero(
        self,
        db_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Empty response prints 0 ingested."""
        monkeypatch.setattr(
            "frmj.cli.get_client",
            lambda conn: FakeClient(account_id="acct-1", responses=[[]]),
        )
        result = runner.invoke(app, ["sync"])
        assert result.exit_code == 0
        assert "0 ingested" in result.output

    def test_sync_reports_cursor(
        self,
        db_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """The cursor transaction ID appears in the output."""
        rows = [_row("42")]
        monkeypatch.setattr(
            "frmj.cli.get_client",
            lambda conn: FakeClient(account_id="acct-1", responses=[rows]),
        )
        result = runner.invoke(app, ["sync"])
        assert result.exit_code == 0
        assert "42" in result.output

    def test_sync_exits_1_on_missing_token(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Missing OANDA_API_TOKEN → exit code 1, error message."""
        path = tmp_path / "no_token.db"
        monkeypatch.setenv("FRMJ_DB_PATH", str(path))
        monkeypatch.delenv("OANDA_API_TOKEN", raising=False)
        result = runner.invoke(app, ["sync"])
        assert result.exit_code == 1
        assert "OANDA_API_TOKEN" in result.stderr


# ---------------------------------------------------------------------------
# config sub-commands
# ---------------------------------------------------------------------------


class TestConfigCommands:
    def test_config_set_and_get_roundtrip(
        self, db_path: Path
    ) -> None:
        """``frmj config set`` writes, ``frmj config get`` reads back."""
        set_result = runner.invoke(app, ["config", "set", "max_open_trades", "6"])
        assert set_result.exit_code == 0, set_result.output
        assert "max_open_trades" in set_result.output
        assert "6" in set_result.output

        get_result = runner.invoke(app, ["config", "get", "max_open_trades"])
        assert get_result.exit_code == 0, get_result.output
        assert get_result.output.strip() == "6"

    def test_config_set_overwrites_existing_value(
        self, db_path: Path
    ) -> None:
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
        for key, val in [("k1", "v1"), ("k2", "v2"), ("k3", "v3")]:
            runner.invoke(app, ["config", "set", key, val])
        for key, val in [("k1", "v1"), ("k2", "v2"), ("k3", "v3")]:
            result = runner.invoke(app, ["config", "get", key])
            assert result.output.strip() == val

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

    def test_config_get_all_empty_db_shows_message(self, db_path: Path) -> None:
        """``frmj config get`` on a fresh DB (only account_id seeded) shows it."""
        result = runner.invoke(app, ["config", "get"])
        assert result.exit_code == 0, result.output
        # The db_path fixture seeds account_id, so it must appear.
        assert "account_id" in result.output

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

    def test_config_get_all_token_from_env(
        self, db_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("OANDA_API_TOKEN", "env-tok")
        result = runner.invoke(app, ["config", "get"])
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


# ---------------------------------------------------------------------------
# FakeFullClient — satisfies all OandaClient methods used by the trade command
# ---------------------------------------------------------------------------


@dataclass
class FakeFullClient:
    """Test double for the full OandaClient interface needed by ``trade``.

    All methods are no-ops or return safe default values.  ``order_placed``
    is set to True when ``place_market_order`` is called, letting tests
    assert that dry-run skips order placement.

    ``tp_should_fail`` / ``sl_should_fail`` cause the attach methods to raise,
    simulating a network error after the fill.
    """

    account_id: str = "acct-1"
    order_placed: bool = False
    tp_attached: str | None = None   # price string passed to attach_take_profit
    sl_attached: str | None = None   # price string passed to attach_stop_loss
    tp_should_fail: bool = False
    sl_should_fail: bool = False

    # --- ClientProtocol (for the auto-sync step) ----------------------------
    def get_transactions_since(self, from_id: str | None = None) -> list:
        return []

    # --- Trade-flow methods --------------------------------------------------
    def get_account_summary(self) -> AccountSummary:
        return AccountSummary(
            nav=Decimal("10000.00"),
            margin_available=Decimal("8000.00"),
            open_trade_count=2,
        )

    def get_open_tickets_on_instrument(self, instrument: str) -> int:
        return 0

    def get_instrument(self, name: str) -> InstrumentSpec:
        return InstrumentSpec(
            name=name,
            pip_location=-4,
            margin_rate=Decimal("0.02"),
            min_units=1,
            units_increment=1,
        )

    def get_price(self, instrument: str, home_currency: str = "USD") -> PriceQuote:
        return PriceQuote(
            bid=Decimal("1.09990"),
            ask=Decimal("1.10010"),
            quote_to_home=Decimal("1"),
            base_to_home=Decimal("1.10"),
        )

    def place_market_order(self, instrument: str, units_signed: int) -> OrderFill:
        self.order_placed = True
        return OrderFill(
            transaction_id="99999",
            fill_price=Decimal("1.10005"),
            units_filled=units_signed,
            trade_id="99999",
        )

    def attach_take_profit(self, trade_id: str, price: Decimal) -> str:
        if self.tp_should_fail:
            raise RuntimeError("TP order rejected by Oanda")
        self.tp_attached = str(price)
        return "100001"

    def attach_stop_loss(self, trade_id: str, price: Decimal) -> str:
        if self.sl_should_fail:
            raise RuntimeError("SL order rejected by Oanda")
        self.sl_attached = str(price)
        return "100002"

    open_trades: list[OpenTrade] = field(default_factory=list)
    close_should_fail: bool = False
    closed_trade_ids: list[str] = field(default_factory=list)

    def get_open_trades(self) -> list[OpenTrade]:
        return self.open_trades

    def close_trade(self, trade_id: str) -> CloseFill:
        if self.close_should_fail:
            raise RuntimeError("Close rejected by Oanda")
        self.closed_trade_ids.append(trade_id)
        return CloseFill(
            transaction_id=str(int(trade_id) + 1000),
            close_price=Decimal("1.10095"),
            realised_pl=Decimal("45.23"),
        )


# ---------------------------------------------------------------------------
# trade --dry-run
# ---------------------------------------------------------------------------


class TestDryRun:
    """The --dry-run flag shows the plan and exits without placing an order."""

    @pytest.fixture()
    def trade_db(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
        """DB with all required config for the trade command."""
        path = tmp_path / "trade_test.db"
        monkeypatch.setenv("FRMJ_DB_PATH", str(path))
        monkeypatch.setenv("OANDA_API_TOKEN", "test-token-123")
        conn = get_db(path=path)
        set_config(conn, "account_id", "acct-1")
        set_config(conn, "max_open_trades", "5")
        conn.close()
        return path

    def test_dry_run_exits_zero(
        self, trade_db: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """``frmj trade EUR_USD long --dry-run`` must exit 0."""
        fake = FakeFullClient()
        monkeypatch.setattr("frmj.cli.get_client", lambda conn: fake)
        # Provide TP and SL input (50 pips, 30 pips), then dry-run exits.
        result = runner.invoke(app, ["trade", "EUR_USD", "long", "--dry-run"],
                               input="50\n30\n")
        assert result.exit_code == 0, result.output

    def test_dry_run_prints_dry_run_message(
        self, trade_db: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Output must contain the [DRY RUN] marker."""
        monkeypatch.setattr("frmj.cli.get_client", lambda conn: FakeFullClient())
        result = runner.invoke(app, ["trade", "EUR_USD", "long", "--dry-run"],
                               input="50\n30\n")
        assert "[DRY RUN]" in result.output

    def test_dry_run_does_not_place_order(
        self, trade_db: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """``place_market_order`` must NOT be called in dry-run mode."""
        fake = FakeFullClient()
        monkeypatch.setattr("frmj.cli.get_client", lambda conn: fake)
        runner.invoke(app, ["trade", "EUR_USD", "long", "--dry-run"],
                      input="50\n30\n")
        assert not fake.order_placed

    def test_dry_run_shows_exit_levels(
        self, trade_db: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Exit levels table (TP and SL) appears in dry-run output."""
        monkeypatch.setattr("frmj.cli.get_client", lambda conn: FakeFullClient())
        result = runner.invoke(app, ["trade", "EUR_USD", "long", "--dry-run"],
                               input="50\n30\n")
        assert "TP:" in result.output
        assert "SL:" in result.output

    def test_dry_run_skip_tpsl_shows_no_exit_levels(
        self, trade_db: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Pressing Enter for both TP and SL yields no exit levels line."""
        monkeypatch.setattr("frmj.cli.get_client", lambda conn: FakeFullClient())
        # Empty input for both TP and SL prompts.
        result = runner.invoke(app, ["trade", "EUR_USD", "long", "--dry-run"],
                               input="\n\n")
        assert result.exit_code == 0
        # Neither TP nor SL was supplied, so no exit levels table is printed.
        assert "TP:" not in result.output
        assert "SL:" not in result.output


# ---------------------------------------------------------------------------
# note command
# ---------------------------------------------------------------------------


def _seed_transaction(
    path: Path,
    oanda_id: str = "12345",
    account_id: str = "acct-1",
) -> None:
    """Insert one transaction row directly into the DB for test setup."""
    conn = sqlite3.connect(str(path))
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute(
        """
        INSERT INTO transactions (oanda_id, account_id, type, time, raw_json)
        VALUES (?, ?, 'ORDER_FILL', '2026-04-25T12:00:00.000000Z', '{}')
        """,
        (oanda_id, account_id),
    )
    conn.commit()
    conn.close()


class TestNoteCommand:
    @pytest.fixture()
    def note_db(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
        path = tmp_path / "note_test.db"
        monkeypatch.setenv("FRMJ_DB_PATH", str(path))
        get_db(path=path).close()  # create and apply schema
        _seed_transaction(path, oanda_id="12345")
        return path

    def test_adds_note_to_transaction(self, note_db: Path) -> None:
        result = runner.invoke(app, ["note", "12345", "Entry at weekly pivot"])
        assert result.exit_code == 0, result.output
        assert "12345" in result.output

    def test_note_persisted_in_db(self, note_db: Path) -> None:
        """The note body must appear in the notes table after the command."""
        runner.invoke(app, ["note", "12345", "My trade rationale"])
        conn = sqlite3.connect(str(note_db))
        row = conn.execute("SELECT body FROM notes LIMIT 1").fetchone()
        conn.close()
        assert row is not None
        assert row[0] == "My trade rationale"

    def test_note_on_missing_transaction_exits_1(self, note_db: Path) -> None:
        """Referencing an Oanda ID not in the local DB must exit 1."""
        result = runner.invoke(app, ["note", "99999", "This should fail"])
        assert result.exit_code == 1

    def test_multiple_notes_on_same_transaction(self, note_db: Path) -> None:
        runner.invoke(app, ["note", "12345", "First note"])
        runner.invoke(app, ["note", "12345", "Second note"])
        conn = sqlite3.connect(str(note_db))
        rows = conn.execute("SELECT body FROM notes ORDER BY id").fetchall()
        conn.close()
        assert len(rows) == 2
        assert rows[0][0] == "First note"
        assert rows[1][0] == "Second note"


# ---------------------------------------------------------------------------
# journal command
# ---------------------------------------------------------------------------


class TestJournalCommand:
    @pytest.fixture()
    def journal_db(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
        path = tmp_path / "journal_test.db"
        monkeypatch.setenv("FRMJ_DB_PATH", str(path))
        get_db(path=path).close()
        # Seed 5 transactions with distinct IDs and times.
        conn = sqlite3.connect(str(path))
        conn.execute("PRAGMA foreign_keys = ON")
        for i in range(1, 6):
            conn.execute(
                """
                INSERT INTO transactions (oanda_id, account_id, type, time, raw_json)
                VALUES (?, 'acct-1', 'ORDER_FILL',
                        ?, '{"instrument":"EUR_USD","units":"1000"}')
                """,
                (str(1000 + i), f"2026-04-25T{10 + i:02d}:00:00.000000Z"),
            )
        conn.commit()
        conn.close()
        return path

    def test_shows_transactions(self, journal_db: Path) -> None:
        result = runner.invoke(app, ["journal"])
        assert result.exit_code == 0, result.output
        # All 5 transaction IDs should appear.
        for i in range(1, 6):
            assert str(1000 + i) in result.output

    def test_n_flag_limits_output(self, journal_db: Path) -> None:
        """``--n 2`` should show only the 2 most recent transactions."""
        result = runner.invoke(app, ["journal", "--n", "2"])
        assert result.exit_code == 0
        # The 2 most recent are 1005 and 1004 (ordered DESC by time).
        assert "1005" in result.output
        assert "1004" in result.output
        assert "1001" not in result.output

    def test_empty_db_shows_helpful_message(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        path = tmp_path / "empty.db"
        monkeypatch.setenv("FRMJ_DB_PATH", str(path))
        get_db(path=path).close()
        result = runner.invoke(app, ["journal"])
        assert result.exit_code == 0
        assert "sync" in result.output.lower()

    def test_notes_appear_under_their_transaction(self, journal_db: Path) -> None:
        """A note seeded for transaction 1001 must appear indented below it."""
        conn = sqlite3.connect(str(journal_db))
        conn.execute("PRAGMA foreign_keys = ON")
        txn_id = conn.execute(
            "SELECT id FROM transactions WHERE oanda_id = '1001'"
        ).fetchone()[0]
        conn.execute(
            "INSERT INTO notes (transaction_id, body) VALUES (?, ?)",
            (txn_id, "Confirmed breakout"),
        )
        conn.commit()
        conn.close()

        result = runner.invoke(app, ["journal"])
        assert "Confirmed breakout" in result.output

    def test_instrument_direction_shown_for_order_fill(
        self, journal_db: Path
    ) -> None:
        """ORDER_FILL rows must show instrument and direction parsed from JSON."""
        result = runner.invoke(app, ["journal"])
        assert "EUR_USD" in result.output
        assert "LONG" in result.output

    def test_plan_shown_under_order_fill(self, journal_db: Path) -> None:
        """A trade plan row is shown as '    Plan: TP ...  SL ...' under its fill."""
        conn = sqlite3.connect(str(journal_db))
        conn.execute("PRAGMA foreign_keys = ON")
        txn_id = conn.execute(
            "SELECT id FROM transactions WHERE oanda_id = '1001'"
        ).fetchone()[0]
        conn.execute(
            "INSERT INTO trade_plans (transaction_id, tp_price, sl_price) "
            "VALUES (?, ?, ?)",
            (txn_id, "1.10550", "1.09750"),
        )
        conn.commit()
        conn.close()

        result = runner.invoke(app, ["journal"])
        assert result.exit_code == 0, result.output
        assert "Plan:" in result.output
        assert "TP 1.10550" in result.output
        assert "SL 1.09750" in result.output

    def test_plan_not_shown_when_absent(self, journal_db: Path) -> None:
        """Transactions without a plan must not show a 'Plan:' line."""
        result = runner.invoke(app, ["journal"])
        assert "Plan:" not in result.output


# ---------------------------------------------------------------------------
# trade — confirmed execution path with TP/SL attachment
# ---------------------------------------------------------------------------


class TestTradeExecute:
    """Confirmed trade path (non-dry-run) with TP/SL attachment."""

    @pytest.fixture()
    def trade_db(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
        path = tmp_path / "trade_exec_test.db"
        monkeypatch.setenv("FRMJ_DB_PATH", str(path))
        monkeypatch.setenv("OANDA_API_TOKEN", "test-token-123")
        conn = get_db(path=path)
        set_config(conn, "account_id", "acct-1")
        set_config(conn, "max_open_trades", "5")
        conn.close()
        return path

    def _invoke(
        self,
        monkeypatch: pytest.MonkeyPatch,
        fake: FakeFullClient,
        inputs: str,
    ) -> object:
        monkeypatch.setattr("frmj.cli.get_client", lambda conn: fake)
        return runner.invoke(app, ["trade", "EUR_USD", "long"], input=inputs)

    def test_tpsl_both_attached_after_fill(
        self, trade_db: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Both TP and SL are sent to Oanda after a confirmed fill."""
        fake = FakeFullClient()
        # TP=50 pips, SL=30 pips, confirm=y, note=skip
        result = self._invoke(monkeypatch, fake, "50\n30\ny\n\n")
        assert result.exit_code == 0, result.output
        assert fake.tp_attached is not None
        assert fake.sl_attached is not None
        assert "Take-profit set" in result.output
        assert "Stop-loss set" in result.output

    def test_no_tpsl_skipped_means_no_attachment(
        self, trade_db: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Skipping both TP and SL means neither attach method is called."""
        fake = FakeFullClient()
        # skip TP, skip SL, confirm=y, note=skip
        result = self._invoke(monkeypatch, fake, "\n\ny\n\n")
        assert result.exit_code == 0, result.output
        assert fake.tp_attached is None
        assert fake.sl_attached is None
        # The prompt labels contain these words, so check for the post-fill confirmation.
        assert "Take-profit set" not in result.output
        assert "Stop-loss set" not in result.output

    def test_sl_failure_warns_unprotected(
        self, trade_db: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """SL attachment failure prints an 'unprotected' warning but does not crash."""
        fake = FakeFullClient(sl_should_fail=True)
        result = self._invoke(monkeypatch, fake, "50\n30\ny\n\n")
        assert result.exit_code == 0, result.output
        # TP still goes through
        assert fake.tp_attached is not None
        assert "Take-profit set" in result.output
        # SL warning is emitted
        assert "unprotected" in result.output + result.stderr

    def test_tp_failure_does_not_block_sl(
        self, trade_db: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """TP failure is a warning only; SL attachment still proceeds."""
        fake = FakeFullClient(tp_should_fail=True)
        result = self._invoke(monkeypatch, fake, "50\n30\ny\n\n")
        assert result.exit_code == 0, result.output
        assert fake.sl_attached is not None
        assert "Stop-loss set" in result.output

    def test_missing_trade_id_warns_gracefully(
        self, trade_db: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """If Oanda returns no trade_id, a warning is shown instead of a crash."""
        fake = FakeFullClient()

        # Override place_market_order to return fill with no trade_id.
        def _no_trade_id_fill(instrument: str, units_signed: int) -> OrderFill:
            fake.order_placed = True
            return OrderFill(
                transaction_id="99999",
                fill_price=Decimal("1.10005"),
                units_filled=units_signed,
                trade_id=None,
            )

        fake.place_market_order = _no_trade_id_fill  # type: ignore[method-assign]
        result = self._invoke(monkeypatch, fake, "50\n30\ny\n\n")
        assert result.exit_code == 0, result.output
        assert "trade ID" in result.output + result.stderr

    def test_trade_plan_saved_to_db(
        self, trade_db: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """TP and SL prices are persisted in trade_plans after a confirmed fill."""
        # Seed the fill transaction as if post-fill sync brought it in.
        conn = get_db(path=trade_db)
        conn.execute(
            "INSERT INTO transactions (oanda_id, account_id, type, time, raw_json) "
            "VALUES ('99999', 'acct-1', 'ORDER_FILL', '2026-04-29T12:00:00Z', '{}')"
        )
        conn.commit()
        conn.close()

        fake = FakeFullClient()
        result = self._invoke(monkeypatch, fake, "50\n30\ny\n\n")
        assert result.exit_code == 0, result.output

        conn = get_db(path=trade_db)
        plan = conn.execute(
            "SELECT tp_price, sl_price FROM trade_plans "
            "JOIN transactions ON trade_plans.transaction_id = transactions.id "
            "WHERE transactions.oanda_id = '99999'"
        ).fetchone()
        conn.close()
        assert plan is not None
        assert plan["tp_price"] is not None
        assert plan["sl_price"] is not None

    def test_no_trade_plan_when_tpsl_skipped(
        self, trade_db: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Skipping both TP and SL leaves no row in trade_plans."""
        fake = FakeFullClient()
        # skip TP, skip SL, confirm=y, note=skip
        result = self._invoke(monkeypatch, fake, "\n\ny\n\n")
        assert result.exit_code == 0, result.output

        conn = get_db(path=trade_db)
        count = conn.execute("SELECT COUNT(*) FROM trade_plans").fetchone()[0]
        conn.close()
        assert count == 0


# ---------------------------------------------------------------------------
# positions command
# ---------------------------------------------------------------------------


def _open_trade(
    trade_id: str = "6368",
    instrument: str = "EUR_USD",
    direction: str = "LONG",
    units: int = 10_000,
    open_price: str = "1.10050",
    unrealised_pl: str = "45.23",
    margin_used: str = "220.10",
    tp_price: str | None = "1.10550",
    sl_price: str | None = "1.09750",
) -> OpenTrade:
    return OpenTrade(
        trade_id=trade_id,
        instrument=instrument,
        direction=direction,
        units=units,
        open_price=Decimal(open_price),
        unrealised_pl=Decimal(unrealised_pl),
        margin_used=Decimal(margin_used),
        take_profit_price=Decimal(tp_price) if tp_price else None,
        stop_loss_price=Decimal(sl_price) if sl_price else None,
        open_time="2026-04-25T14:30:00.000000Z",
    )


class TestPositionsCommand:
    @pytest.fixture()
    def pos_db(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
        path = tmp_path / "pos_test.db"
        monkeypatch.setenv("FRMJ_DB_PATH", str(path))
        monkeypatch.setenv("OANDA_API_TOKEN", "test-token-123")
        conn = get_db(path=path)
        set_config(conn, "account_id", "acct-1")
        conn.close()
        return path

    def _invoke(
        self,
        monkeypatch: pytest.MonkeyPatch,
        trades: list[OpenTrade],
    ) -> object:
        fake = FakeFullClient(open_trades=trades)
        monkeypatch.setattr("frmj.cli.get_client", lambda conn: fake)
        return runner.invoke(app, ["positions"])

    def test_no_open_positions_message(
        self, pos_db: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        result = self._invoke(monkeypatch, [])
        assert result.exit_code == 0, result.output
        assert "No open positions" in result.output

    def test_shows_instrument_and_direction(
        self, pos_db: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        result = self._invoke(monkeypatch, [_open_trade(instrument="EUR_USD", direction="LONG")])
        assert result.exit_code == 0, result.output
        assert "EUR_USD" in result.output
        assert "LONG" in result.output

    def test_shows_units_and_entry_price(
        self, pos_db: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        result = self._invoke(monkeypatch, [_open_trade(units=10_000, open_price="1.10050")])
        assert "10,000" in result.output
        assert "1.10050" in result.output

    def test_shows_tp_and_sl(
        self, pos_db: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        result = self._invoke(
            monkeypatch,
            [_open_trade(tp_price="1.10550", sl_price="1.09750")],
        )
        assert "TP: 1.10550" in result.output
        assert "SL: 1.09750" in result.output

    def test_no_tpsl_shows_fallback_text(
        self, pos_db: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        result = self._invoke(monkeypatch, [_open_trade(tp_price=None, sl_price=None)])
        assert "no TP/SL set" in result.output

    def test_shows_position_count(
        self, pos_db: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        result = self._invoke(
            monkeypatch,
            [_open_trade(trade_id="1"), _open_trade(trade_id="2")],
        )
        assert "2 open positions" in result.output

    def test_note_flag_shown_when_notes_exist(
        self, pos_db: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A trade whose fill transaction has notes shows [note] in output."""
        # Seed the fill transaction and a note directly in the DB.
        conn = sqlite3.connect(str(pos_db))
        conn.execute(
            "INSERT INTO transactions (oanda_id, account_id, type, time, raw_json) "
            "VALUES ('6368', 'acct-1', 'ORDER_FILL', '2026-04-25T14:30:00Z', '{}')"
        )
        txn_id = conn.execute(
            "SELECT id FROM transactions WHERE oanda_id='6368'"
        ).fetchone()[0]
        conn.execute(
            "INSERT INTO notes (transaction_id, body) VALUES (?, 'Test note')",
            (txn_id,),
        )
        conn.commit()
        conn.close()

        result = self._invoke(monkeypatch, [_open_trade(trade_id="6368")])
        assert "[note]" in result.output

    def test_no_note_flag_when_no_notes(
        self, pos_db: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        result = self._invoke(monkeypatch, [_open_trade(trade_id="9999")])
        assert "[note]" not in result.output

    def test_api_error_exits_1(
        self, pos_db: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        fake = FakeFullClient()

        def _fail() -> list:
            raise RuntimeError("Oanda API unavailable")

        fake.get_open_trades = _fail  # type: ignore[method-assign]
        monkeypatch.setattr("frmj.cli.get_client", lambda conn: fake)
        result = runner.invoke(app, ["positions"])
        assert result.exit_code == 1
        assert "Error" in result.output + result.stderr


# ---------------------------------------------------------------------------
# close command
# ---------------------------------------------------------------------------


class TestCloseCommand:
    @pytest.fixture()
    def close_db(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
        path = tmp_path / "close_test.db"
        monkeypatch.setenv("FRMJ_DB_PATH", str(path))
        monkeypatch.setenv("OANDA_API_TOKEN", "test-token-123")
        conn = get_db(path=path)
        set_config(conn, "account_id", "acct-1")
        conn.close()
        return path

    def _invoke(
        self,
        monkeypatch: pytest.MonkeyPatch,
        fake: FakeFullClient,
        inputs: str = "",
    ) -> object:
        monkeypatch.setattr("frmj.cli.get_client", lambda conn: fake)
        return runner.invoke(app, ["close", "EUR_USD"], input=inputs)

    def test_no_open_positions_message(
        self, close_db: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        result = self._invoke(monkeypatch, FakeFullClient(open_trades=[]))
        assert result.exit_code == 0, result.output
        assert "No open positions" in result.output

    def test_shows_ticket_details_before_confirm(
        self, close_db: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        fake = FakeFullClient(open_trades=[_open_trade(trade_id="6368", units=10_000)])
        result = self._invoke(monkeypatch, fake, inputs="n\n")
        assert "6368" in result.output
        assert "10,000" in result.output

    def test_cancel_does_not_close(
        self, close_db: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        fake = FakeFullClient(open_trades=[_open_trade(trade_id="6368")])
        result = self._invoke(monkeypatch, fake, inputs="n\n")
        assert result.exit_code == 0, result.output
        assert fake.closed_trade_ids == []
        assert "Cancelled" in result.output

    def test_confirm_calls_close_trade(
        self, close_db: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        fake = FakeFullClient(open_trades=[_open_trade(trade_id="6368")])
        result = self._invoke(monkeypatch, fake, inputs="y\n")
        assert result.exit_code == 0, result.output
        assert "6368" in fake.closed_trade_ids
        assert "closed at" in result.output

    def test_multiple_tickets_all_closed(
        self, close_db: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        fake = FakeFullClient(
            open_trades=[
                _open_trade(trade_id="100"),
                _open_trade(trade_id="101"),
            ]
        )
        result = self._invoke(monkeypatch, fake, inputs="y\n")
        assert result.exit_code == 0, result.output
        assert fake.closed_trade_ids == ["100", "101"]

    def test_multiple_tickets_shows_total_pl(
        self, close_db: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        fake = FakeFullClient(
            open_trades=[
                _open_trade(trade_id="100", unrealised_pl="20.00"),
                _open_trade(trade_id="101", unrealised_pl="30.00"),
            ]
        )
        result = self._invoke(monkeypatch, fake, inputs="n\n")
        assert "Total P/L" in result.output

    def test_only_closes_matching_instrument(
        self, close_db: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Trades on other instruments must not be touched."""
        fake = FakeFullClient(
            open_trades=[
                _open_trade(trade_id="200", instrument="EUR_USD"),
                _open_trade(trade_id="201", instrument="USD_JPY"),
            ]
        )
        result = self._invoke(monkeypatch, fake, inputs="y\n")
        assert result.exit_code == 0, result.output
        assert fake.closed_trade_ids == ["200"]

    def test_close_failure_reports_error_and_continues(
        self, close_db: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A failed close on one ticket should not prevent closing the next."""
        fake = FakeFullClient(
            open_trades=[
                _open_trade(trade_id="300"),
                _open_trade(trade_id="301"),
            ],
            close_should_fail=True,
        )
        result = self._invoke(monkeypatch, fake, inputs="y\n")
        assert result.exit_code == 0, result.output
        assert "failed to close" in result.output + result.stderr

    def test_api_error_fetching_trades_exits_1(
        self, close_db: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        fake = FakeFullClient()

        def _fail() -> list:
            raise RuntimeError("Oanda unreachable")

        fake.get_open_trades = _fail  # type: ignore[method-assign]
        monkeypatch.setattr("frmj.cli.get_client", lambda conn: fake)
        result = runner.invoke(app, ["close", "EUR_USD"])
        assert result.exit_code == 1
        assert "Error" in result.output + result.stderr
