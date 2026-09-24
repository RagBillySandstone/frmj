"""Tests for ``frmj close``."""

from __future__ import annotations

from pathlib import Path

import pytest
from click.testing import Result
from typer.testing import CliRunner

from frmj.app import get_db, set_config
from frmj.cli import app
from frmj.cli._completion import _complete_open_instrument

from .conftest import FakeFullClient, _completion_ctx, _open_trade, _row

runner = CliRunner()

# Completion context with no --account on the command line.
_CTX = _completion_ctx({})


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
    ) -> Result:
        monkeypatch.setattr(
            "frmj.cli.close.get_client", lambda conn, account_name=None: fake
        )
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

    def test_completion_only_offers_open_instruments(
        self, close_db: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Tab completion for ``close`` must be scoped to live open trades,
        not the full static FX pair list used by ``trade``."""
        fake = FakeFullClient(
            open_trades=[
                _open_trade(trade_id="200", instrument="EUR_USD"),
                _open_trade(trade_id="201", instrument="USD_JPY"),
            ]
        )
        monkeypatch.setattr(
            "frmj.cli._completion.get_client", lambda conn, account_name=None: fake
        )
        assert _complete_open_instrument(_CTX, "") == ["EUR_USD", "USD_JPY"]
        assert _complete_open_instrument(_CTX, "eur") == ["EUR_USD"]
        assert _complete_open_instrument(_CTX, "gbp") == []

    def test_completion_empty_when_no_open_trades(
        self, close_db: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(
            "frmj.cli._completion.get_client",
            lambda conn, account_name=None: FakeFullClient(open_trades=[]),
        )
        assert _complete_open_instrument(_CTX, "") == []

    def test_completion_swallows_client_errors(
        self, close_db: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """No active account, auth failure, network error, etc. should yield
        no completions rather than raising inside the user's shell."""

        def _fail(conn: object, account_name: str | None = None) -> object:
            raise RuntimeError("no active account")

        monkeypatch.setattr("frmj.cli._completion.get_client", _fail)
        assert _complete_open_instrument(_CTX, "") == []

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
        monkeypatch.setattr(
            "frmj.cli.close.get_client", lambda conn, account_name=None: fake
        )
        result = runner.invoke(app, ["close", "EUR_USD"])
        assert result.exit_code == 1
        assert "Error" in result.output + result.stderr

    def test_get_client_error_exits_1(
        self, close_db: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        def _fail(conn: object, account_name: str | None = None) -> None:
            raise RuntimeError("No token configured for this account")

        monkeypatch.setattr("frmj.cli.close.get_client", _fail)
        result = runner.invoke(app, ["close", "EUR_USD"])
        assert result.exit_code == 1
        assert "Error" in result.output + result.stderr

    def test_post_close_sync_reports_ingested_count(
        self, close_db: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """After closing at least one ticket, the CLI auto-syncs; new rows
        pulled in during that sync are reported."""
        fake = FakeFullClient(
            open_trades=[_open_trade(trade_id="6368")],
            sync_rows=[_row("9001", account_id="acct-1")],
        )
        result = self._invoke(monkeypatch, fake, inputs="y\n")
        assert result.exit_code == 0, result.output
        assert "[sync] +1 transactions" in result.output

    def test_post_close_sync_failure_shows_warning(
        self, close_db: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A sync failure after a successful close must not crash the command
        — it surfaces as a warning instead."""
        fake = FakeFullClient(
            open_trades=[_open_trade(trade_id="6368")],
            sync_should_fail=True,
        )
        result = self._invoke(monkeypatch, fake, inputs="y\n")
        assert result.exit_code == 0, result.output
        assert "[sync] Warning: sync failed" in result.output + result.stderr

    def test_account_option_targets_named_account(
        self, close_db: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """--account is handed to get_client and named before the confirm."""
        requested: list[str | None] = []
        fake = FakeFullClient(open_trades=[_open_trade(trade_id="6368")])

        def _get_client(conn: object, account_name: str | None = None) -> object:
            requested.append(account_name)
            return fake

        monkeypatch.setattr("frmj.cli.close.get_client", _get_client)
        result = runner.invoke(
            app, ["close", "EUR_USD", "--account", "other"], input="n\n"
        )
        assert result.exit_code == 0, result.output
        assert requested == ["other"]
        assert result.output.index("Account: other") < result.output.index("Close 1")

    def test_completion_uses_account_option(
        self, close_db: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Completion lists the --account's open trades, not the active one's."""
        requested: list[str | None] = []
        fake = FakeFullClient(open_trades=[_open_trade(instrument="EUR_USD")])

        def _get_client(conn: object, account_name: str | None = None) -> object:
            requested.append(account_name)
            return fake

        monkeypatch.setattr("frmj.cli._completion.get_client", _get_client)
        ctx = _completion_ctx({"account": "other"})
        assert _complete_open_instrument(ctx, "") == ["EUR_USD"]
        assert requested == ["other"]


# ---------------------------------------------------------------------------
# trade — order failure with retry / save / abort
# ---------------------------------------------------------------------------
