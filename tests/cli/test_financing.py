"""Tests for ``frmj financing`` (live rates, --date snapshot lookup, --quiet)."""

from __future__ import annotations

from datetime import date
from decimal import Decimal
from pathlib import Path

import httpx
import pytest
import typer
from click.testing import Result
from typer.testing import CliRunner

from frmj import queries
from frmj.app import get_db, set_config
from frmj.cli import app
from frmj.cli._completion import _FINANCING_PAIRS, _pair_tier
from frmj.cli.financing import (
    _fmt_financing_pct,
    _group_financing_rates,
)
from frmj.execution.oanda import FinancingRate

from .conftest import FakeFullClient

runner = CliRunner()


def _http_error(status: int) -> httpx.HTTPStatusError:
    """Build the error ``raise_for_status`` raises for an HTTP *status*."""
    request = httpx.Request("GET", "https://api-fxpractice.oanda.com/v3/x")
    response = httpx.Response(status, request=request)
    return httpx.HTTPStatusError(f"{status}", request=request, response=response)


class _FlakyFinancingClient(FakeFullClient):
    """Fake client that raises each of *failures* in turn, then returns *rates*."""

    def __init__(
        self,
        failures: list[Exception],
        rates: list[FinancingRate] | None = None,
    ) -> None:
        super().__init__(financing_rates=rates or [])
        self.failures = list(failures)
        self.calls = 0

    def get_financing_rates(self, instruments: list[str]) -> list[FinancingRate]:
        self.calls += 1
        if self.failures:
            raise self.failures.pop(0)
        return self.financing_rates


def _patch_sleep(monkeypatch: pytest.MonkeyPatch) -> list[float]:
    """Replace the retry sleep with a recorder; return the list of delays."""
    sleeps: list[float] = []
    monkeypatch.setattr("frmj.cli.financing.time.sleep", sleeps.append)
    return sleeps


class TestFinancingHelpers:
    """Pure-function tests for the major/minor/exotic classifier and formatters."""

    def test_pair_tier_major_is_usd_plus_one_other_major(self) -> None:
        assert _pair_tier("EUR_USD") == "major"
        assert _pair_tier("USD_JPY") == "major"

    def test_pair_tier_minor_is_two_majors_without_usd(self) -> None:
        assert _pair_tier("EUR_GBP") == "minor"
        assert _pair_tier("AUD_JPY") == "minor"

    def test_pair_tier_exotic_has_a_non_major_currency(self) -> None:
        assert _pair_tier("USD_TRY") == "exotic"
        assert _pair_tier("EUR_ZAR") == "exotic"
        assert _pair_tier("XAU_USD") == "exotic"

    def test_financing_pairs_excludes_metals(self) -> None:
        assert "XAU_USD" not in _FINANCING_PAIRS
        assert "XAG_USD" not in _FINANCING_PAIRS

    def test_financing_pairs_are_uppercase_with_no_duplicates(self) -> None:
        assert all(p == p.upper() for p in _FINANCING_PAIRS)
        assert len(_FINANCING_PAIRS) == len(set(_FINANCING_PAIRS))

    def test_fmt_financing_pct_shows_signed_four_decimals(self) -> None:
        assert _fmt_financing_pct(Decimal("-0.0141")) == "-1.4100%"
        assert _fmt_financing_pct(Decimal("0.0007")) == "+0.0700%"

    def test_group_financing_rates_buckets_and_sorts_alphabetically(self) -> None:
        rates = [
            FinancingRate("USD_JPY", Decimal("0"), Decimal("0")),
            FinancingRate("EUR_USD", Decimal("0"), Decimal("0")),
            FinancingRate("EUR_GBP", Decimal("0"), Decimal("0")),
            FinancingRate("USD_TRY", Decimal("0"), Decimal("0")),
        ]
        groups = _group_financing_rates(rates)
        assert [r.instrument for r in groups["major"]] == ["EUR_USD", "USD_JPY"]
        assert [r.instrument for r in groups["minor"]] == ["EUR_GBP"]
        assert [r.instrument for r in groups["exotic"]] == ["USD_TRY"]


class TestFinancingCommand:
    @pytest.fixture()
    def fin_db(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
        path = tmp_path / "fin_test.db"
        monkeypatch.setenv("FRMJ_DB_PATH", str(path))
        monkeypatch.setenv("OANDA_API_TOKEN", "test-token-123")
        conn = get_db(path=path)
        set_config(conn, "account_id", "acct-1")
        conn.close()
        return path

    def _invoke(
        self,
        monkeypatch: pytest.MonkeyPatch,
        rates: list[FinancingRate],
    ) -> Result:
        fake = FakeFullClient(financing_rates=rates)
        monkeypatch.setattr("frmj.cli.financing.get_client", lambda conn: fake)
        return runner.invoke(app, ["financing"])

    def test_no_rates_returned_message(
        self, fin_db: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        result = self._invoke(monkeypatch, [])
        assert result.exit_code == 0, result.output
        assert "No financing rates" in result.output

    def test_shows_majors_minors_exotics_sections(
        self, fin_db: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        result = self._invoke(
            monkeypatch,
            [
                FinancingRate("EUR_USD", Decimal("-0.0049"), Decimal("0.0009")),
                FinancingRate("EUR_GBP", Decimal("-0.0021"), Decimal("0.0005")),
                FinancingRate("USD_TRY", Decimal("-0.4521"), Decimal("0.4102")),
            ],
        )
        assert result.exit_code == 0, result.output
        assert "Majors" in result.output
        assert "Minors" in result.output
        assert "Exotics" in result.output
        assert "EUR_USD" in result.output
        assert "-0.4900%" in result.output
        assert "+0.0900%" in result.output

    def test_omits_empty_tier_sections(
        self, fin_db: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Only major pairs supplied: no Minors/Exotics headers should print."""
        result = self._invoke(
            monkeypatch, [FinancingRate("EUR_USD", Decimal("0"), Decimal("0"))]
        )
        assert result.exit_code == 0, result.output
        assert "Majors" in result.output
        assert "Minors" not in result.output
        assert "Exotics" not in result.output

    def test_exotics_sit_beside_majors_and_minors(
        self, fin_db: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Exotics share lines with the first column instead of following it."""
        result = self._invoke(
            monkeypatch,
            [
                FinancingRate("EUR_USD", Decimal("-0.0049"), Decimal("0.0009")),
                FinancingRate("EUR_GBP", Decimal("-0.0021"), Decimal("0.0005")),
                FinancingRate("USD_TRY", Decimal("-0.4521"), Decimal("0.4102")),
            ],
        )
        assert result.exit_code == 0, result.output
        lines = result.output.splitlines()

        # The two column headers and first rows of each column share a line.
        header = next(line for line in lines if "Majors" in line)
        assert "Exotics" in header
        assert header.index("Majors") < header.index("Exotics")
        row = next(line for line in lines if "EUR_USD" in line)
        assert "USD_TRY" in row
        assert row.index("EUR_USD") < row.index("USD_TRY")

        # Minors stay below Majors in the first column, not beside them.
        minors = next(line for line in lines if "Minors" in line)
        assert minors.index("Minors") == header.index("Majors")
        assert lines.index(minors) > lines.index(row)

    def test_second_column_aligned_despite_color_codes(
        self, fin_db: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Color escapes in the first column must not shift the second one."""
        # One positive, one negative, one uncolored zero: each first-column row
        # has a different number of invisible escape characters. GBP_USD's wider
        # rates force padding onto the narrower rows, so padding measured on the
        # colored text instead of the visible text would shift the second column.
        fake = FakeFullClient(
            financing_rates=[
                FinancingRate("AUD_USD", Decimal("0.0100"), Decimal("-0.0100")),
                FinancingRate("EUR_USD", Decimal("0"), Decimal("0")),
                FinancingRate("GBP_USD", Decimal("-0.4521"), Decimal("0.4102")),
                FinancingRate("USD_MXN", Decimal("0.0100"), Decimal("0.0100")),
                FinancingRate("USD_NOK", Decimal("0.0100"), Decimal("0.0100")),
                FinancingRate("USD_PLN", Decimal("0.0100"), Decimal("0.0100")),
            ]
        )
        monkeypatch.setattr("frmj.cli.financing.get_client", lambda conn: fake)
        result = runner.invoke(app, ["financing"], color=True)
        assert result.exit_code == 0, result.output
        # Guard: without real escapes this test would pass vacuously.
        assert "\x1b[" in result.output

        lines = [typer.unstyle(line) for line in result.output.splitlines()]
        # Check each row exactly: "Exotics" is flush with the column and rows are
        # indented by two spaces, so a two-char shift must not pass as an indent.
        column_start = next(line for line in lines if "Exotics" in line).index(
            "Exotics"
        )
        for exotic in ("USD_MXN", "USD_NOK", "USD_PLN"):
            row = next(line for line in lines if exotic in line)
            assert row.index(exotic) == column_start + 2, row

    def test_second_column_continues_below_a_shorter_first_column(
        self, fin_db: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Exotic rows past the end of the first column keep their indent."""
        result = self._invoke(
            monkeypatch,
            [
                FinancingRate("EUR_USD", Decimal("0.0100"), Decimal("0.0100")),
                FinancingRate("USD_MXN", Decimal("0.0100"), Decimal("0.0100")),
                FinancingRate("USD_NOK", Decimal("0.0100"), Decimal("0.0100")),
            ],
        )
        assert result.exit_code == 0, result.output
        lines = result.output.splitlines()
        beside = next(line for line in lines if "USD_MXN" in line)
        below = next(line for line in lines if "USD_NOK" in line)
        assert "EUR_USD" in beside
        assert "EUR_USD" not in below
        assert below.index("USD_NOK") == beside.index("USD_MXN")

    def test_second_column_aligned_beside_the_blank_line_between_tiers(
        self, fin_db: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The blank line between Majors and Minors still holds the column's width."""
        # Majors takes four lines (label, rule, heading, one row), so the blank
        # separator is line five, which is where the second exotic row lands.
        result = self._invoke(
            monkeypatch,
            [
                FinancingRate("EUR_USD", Decimal("0.0100"), Decimal("0.0100")),
                FinancingRate("EUR_GBP", Decimal("0.0100"), Decimal("0.0100")),
                FinancingRate("USD_MXN", Decimal("0.0100"), Decimal("0.0100")),
                FinancingRate("USD_NOK", Decimal("0.0100"), Decimal("0.0100")),
            ],
        )
        assert result.exit_code == 0, result.output
        lines = result.output.splitlines()
        above = next(line for line in lines if "USD_MXN" in line)
        beside_blank = next(line for line in lines if "USD_NOK" in line)
        # Guard: this row must really be sitting beside the separator.
        assert beside_blank.lstrip().startswith("USD_NOK")
        assert beside_blank.index("USD_NOK") == above.index("USD_MXN")

    def test_exotics_only_table_is_flush_left(
        self, fin_db: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """With no majors or minors there is no blank column to indent behind."""
        result = self._invoke(
            monkeypatch,
            [FinancingRate("USD_TRY", Decimal("-0.4521"), Decimal("0.4102"))],
        )
        assert result.exit_code == 0, result.output
        lines = result.output.splitlines()
        assert any(line.startswith("Exotics") for line in lines)
        assert any(line.startswith("  USD_TRY") for line in lines)

    def test_full_pair_list_fits_one_screen(
        self, fin_db: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Every pair at a realistic worst-case rate fits 80 columns, no blank tail."""
        result = self._invoke(
            monkeypatch,
            [
                FinancingRate(pair, Decimal("-0.4521"), Decimal("-0.4102"))
                for pair in _FINANCING_PAIRS
            ],
        )
        assert result.exit_code == 0, result.output
        lines = result.output.splitlines()
        assert max(len(line) for line in lines) <= 80
        # Height is set by the first column: title (3 lines), then Majors (3 + 7),
        # a blank line, and Minors (3 + 21).
        assert len(lines) == 3 + 10 + 1 + 24
        assert all(line == line.rstrip() for line in lines)

    def test_requests_the_full_financing_pairs_list(
        self, fin_db: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The command asks Oanda for every non-metal instrument in one call."""
        requested: list[list[str]] = []

        class _RecordingClient(FakeFullClient):
            def get_financing_rates(self, instruments: list[str]) -> list:
                requested.append(instruments)
                return []

        fake = _RecordingClient()
        monkeypatch.setattr("frmj.cli.financing.get_client", lambda conn: fake)
        runner.invoke(app, ["financing"])
        assert requested == [list(_FINANCING_PAIRS)]

    def test_api_error_exits_1(
        self, fin_db: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        fake = FakeFullClient(financing_should_fail=True)
        monkeypatch.setattr("frmj.cli.financing.get_client", lambda conn: fake)
        result = runner.invoke(app, ["financing"])
        assert result.exit_code == 1
        assert "Error" in result.output + result.stderr

    def test_get_client_error_exits_1(
        self, fin_db: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        def _fail(conn: object) -> None:
            raise RuntimeError("No token configured for this account")

        monkeypatch.setattr("frmj.cli.financing.get_client", _fail)
        result = runner.invoke(app, ["financing"])
        assert result.exit_code == 1
        assert "Error" in result.output + result.stderr

    def test_interactive_run_does_not_retry(
        self, fin_db: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Without --quiet a transient error fails at once — the user is there
        to rerun it, and a silent multi-minute wait would look like a hang."""
        fake = _FlakyFinancingClient(failures=[_http_error(401)])
        sleeps = _patch_sleep(monkeypatch)
        monkeypatch.setattr("frmj.cli.financing.get_client", lambda conn: fake)

        result = runner.invoke(app, ["financing"])
        assert result.exit_code == 1
        assert fake.calls == 1
        assert sleeps == []


class TestFinancingSnapshotRecording:
    """A live ``frmj financing`` fetch should record today's rates for later
    lookup via ``--date``, since Oanda has no historical-rate endpoint."""

    @pytest.fixture()
    def fin_db(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
        path = tmp_path / "fin_snapshot_test.db"
        monkeypatch.setenv("FRMJ_DB_PATH", str(path))
        monkeypatch.setenv("OANDA_API_TOKEN", "test-token-123")
        conn = get_db(path=path)
        set_config(conn, "account_id", "acct-1")
        conn.close()
        return path

    def test_live_fetch_records_todays_snapshot(
        self, fin_db: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        fake = FakeFullClient(
            financing_rates=[
                FinancingRate("EUR_USD", Decimal("-0.0049"), Decimal("0.0009"))
            ]
        )
        monkeypatch.setattr("frmj.cli.financing.get_client", lambda conn: fake)
        result = runner.invoke(app, ["financing"])
        assert result.exit_code == 0, result.output

        conn = get_db(path=fin_db)
        snapshot = queries.load_financing_snapshot(
            conn, "acct-1", date.today().isoformat()
        )
        conn.close()
        assert snapshot == [
            FinancingRate("EUR_USD", Decimal("-0.0049"), Decimal("0.0009"))
        ]

    def test_no_rates_returned_records_nothing(
        self, fin_db: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        fake = FakeFullClient(financing_rates=[])
        monkeypatch.setattr("frmj.cli.financing.get_client", lambda conn: fake)
        result = runner.invoke(app, ["financing"])
        assert result.exit_code == 0, result.output

        conn = get_db(path=fin_db)
        count = conn.execute(
            "SELECT COUNT(*) FROM financing_rate_snapshots"
        ).fetchone()[0]
        conn.close()
        assert count == 0

    def test_rerunning_same_day_overwrites_rather_than_duplicates(
        self, fin_db: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        first = FakeFullClient(
            financing_rates=[
                FinancingRate("EUR_USD", Decimal("-0.0049"), Decimal("0.0009"))
            ]
        )
        monkeypatch.setattr("frmj.cli.financing.get_client", lambda conn: first)
        runner.invoke(app, ["financing"])

        second = FakeFullClient(
            financing_rates=[
                FinancingRate("EUR_USD", Decimal("-0.0100"), Decimal("0.0050"))
            ]
        )
        monkeypatch.setattr("frmj.cli.financing.get_client", lambda conn: second)
        runner.invoke(app, ["financing"])

        conn = get_db(path=fin_db)
        rows = conn.execute("SELECT * FROM financing_rate_snapshots").fetchall()
        snapshot = queries.load_financing_snapshot(
            conn, "acct-1", date.today().isoformat()
        )
        conn.close()
        assert len(rows) == 1
        assert snapshot == [
            FinancingRate("EUR_USD", Decimal("-0.0100"), Decimal("0.0050"))
        ]


class TestFinancingDateOption:
    """``frmj financing --date`` looks up a previously recorded snapshot
    instead of calling Oanda, since there is no historical-rate endpoint."""

    @pytest.fixture()
    def fin_db(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
        path = tmp_path / "fin_date_test.db"
        monkeypatch.setenv("FRMJ_DB_PATH", str(path))
        monkeypatch.setenv("OANDA_API_TOKEN", "test-token-123")
        conn = get_db(path=path)
        set_config(conn, "account_id", "acct-1")
        conn.close()
        return path

    def test_shows_recorded_snapshot_without_calling_the_api(
        self, fin_db: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        conn = get_db(path=fin_db)
        queries.record_financing_snapshot(
            conn,
            "acct-1",
            [FinancingRate("EUR_USD", Decimal("-0.0049"), Decimal("0.0009"))],
            "2026-01-15",
        )
        conn.close()

        def _fail_if_called(instruments: list[str]) -> list[FinancingRate]:
            raise AssertionError("--date must not call the live financing API")

        fake = FakeFullClient()
        fake.get_financing_rates = _fail_if_called  # type: ignore[method-assign]
        monkeypatch.setattr("frmj.cli.financing.get_client", lambda conn: fake)

        result = runner.invoke(app, ["financing", "--date", "2026-01-15"])
        assert result.exit_code == 0, result.output
        assert "snapshot from 2026-01-15" in result.output
        assert "EUR_USD" in result.output
        assert "-0.4900%" in result.output

    def test_no_snapshot_recorded_for_date_shows_message(
        self, fin_db: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        fake = FakeFullClient()
        monkeypatch.setattr("frmj.cli.financing.get_client", lambda conn: fake)
        result = runner.invoke(app, ["financing", "--date", "2020-06-01"])
        assert result.exit_code == 0, result.output
        assert "No financing snapshot recorded for 2020-06-01." in result.output

    def test_invalid_date_format_exits_1(
        self, fin_db: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        fake = FakeFullClient()
        monkeypatch.setattr("frmj.cli.financing.get_client", lambda conn: fake)
        result = runner.invoke(app, ["financing", "--date", "not-a-date"])
        assert result.exit_code == 1
        assert "not a valid date" in result.output + result.stderr

    def test_get_client_error_exits_1(
        self, fin_db: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        def _fail(conn: object) -> None:
            raise RuntimeError("No token configured for this account")

        monkeypatch.setattr("frmj.cli.financing.get_client", _fail)
        result = runner.invoke(app, ["financing", "--date", "2026-01-15"])
        assert result.exit_code == 1
        assert "Error" in result.output + result.stderr

    def test_snapshot_scoped_to_recording_account(
        self, fin_db: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A snapshot recorded under a different account_id is invisible to
        the currently active account."""
        conn = get_db(path=fin_db)
        queries.record_financing_snapshot(
            conn,
            "some-other-account",
            [FinancingRate("EUR_USD", Decimal("-0.0049"), Decimal("0.0009"))],
            "2026-01-15",
        )
        conn.close()

        fake = FakeFullClient()  # account_id defaults to "acct-1"
        monkeypatch.setattr("frmj.cli.financing.get_client", lambda conn: fake)
        result = runner.invoke(app, ["financing", "--date", "2026-01-15"])
        assert result.exit_code == 0, result.output
        assert "No financing snapshot recorded for 2026-01-15." in result.output


class TestFinancingQuietOption:
    """``frmj financing --quiet`` fetches and records live rates with no
    stdout output on success, for use as a daily cron job."""

    @pytest.fixture()
    def fin_db(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
        path = tmp_path / "fin_quiet_test.db"
        monkeypatch.setenv("FRMJ_DB_PATH", str(path))
        monkeypatch.setenv("OANDA_API_TOKEN", "test-token-123")
        conn = get_db(path=path)
        set_config(conn, "account_id", "acct-1")
        conn.close()
        return path

    def test_no_output_on_success(
        self, fin_db: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        fake = FakeFullClient(
            financing_rates=[
                FinancingRate("EUR_USD", Decimal("-0.0049"), Decimal("0.0009"))
            ]
        )
        monkeypatch.setattr("frmj.cli.financing.get_client", lambda conn: fake)
        result = runner.invoke(app, ["financing", "--quiet"])
        assert result.exit_code == 0, result.output
        assert result.output == ""

    def test_still_records_snapshot(
        self, fin_db: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        fake = FakeFullClient(
            financing_rates=[
                FinancingRate("EUR_USD", Decimal("-0.0049"), Decimal("0.0009"))
            ]
        )
        monkeypatch.setattr("frmj.cli.financing.get_client", lambda conn: fake)
        runner.invoke(app, ["financing", "--quiet"])

        conn = get_db(path=fin_db)
        snapshot = queries.load_financing_snapshot(
            conn, "acct-1", date.today().isoformat()
        )
        conn.close()
        assert snapshot == [
            FinancingRate("EUR_USD", Decimal("-0.0049"), Decimal("0.0009"))
        ]

    def test_no_output_when_no_rates_returned(
        self, fin_db: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        fake = FakeFullClient(financing_rates=[])
        monkeypatch.setattr("frmj.cli.financing.get_client", lambda conn: fake)
        result = runner.invoke(app, ["financing", "--quiet"])
        assert result.exit_code == 0, result.output
        assert result.output == ""

    def test_fetch_error_still_prints_and_exits_1(
        self, fin_db: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        fake = FakeFullClient(financing_should_fail=True)
        monkeypatch.setattr("frmj.cli.financing.get_client", lambda conn: fake)
        result = runner.invoke(app, ["financing", "--quiet"])
        assert result.exit_code == 1
        assert "Error" in result.output + result.stderr

    def test_get_client_error_still_prints_and_exits_1(
        self, fin_db: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        def _fail(conn: object) -> None:
            raise RuntimeError("No token configured for this account")

        monkeypatch.setattr("frmj.cli.financing.get_client", _fail)
        result = runner.invoke(app, ["financing", "--quiet"])
        assert result.exit_code == 1
        assert "Error" in result.output + result.stderr

    def test_retries_transient_error_then_records_silently(
        self, fin_db: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A one-off 401 (seen from Oanda's practice API) is retried after the
        first delay; the eventual success records the snapshot with no output."""
        rates = [FinancingRate("EUR_USD", Decimal("-0.0049"), Decimal("0.0009"))]
        fake = _FlakyFinancingClient(failures=[_http_error(401)], rates=rates)
        sleeps = _patch_sleep(monkeypatch)
        monkeypatch.setattr("frmj.cli.financing.get_client", lambda conn: fake)

        result = runner.invoke(app, ["financing", "--quiet"])
        assert result.exit_code == 0, result.output
        assert result.output == ""
        assert fake.calls == 2
        assert sleeps == [60]

        conn = get_db(path=fin_db)
        snapshot = queries.load_financing_snapshot(
            conn, "acct-1", date.today().isoformat()
        )
        conn.close()
        assert snapshot == rates

    def test_gives_up_after_all_retries_and_exits_1(
        self, fin_db: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        fake = _FlakyFinancingClient(failures=[_http_error(503)] * 4)
        sleeps = _patch_sleep(monkeypatch)
        monkeypatch.setattr("frmj.cli.financing.get_client", lambda conn: fake)

        result = runner.invoke(app, ["financing", "--quiet"])
        assert result.exit_code == 1
        assert "503" in result.output + result.stderr
        assert fake.calls == 4
        assert sleeps == [60, 300, 900]

    def test_retries_network_error(
        self, fin_db: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        fake = _FlakyFinancingClient(failures=[httpx.ConnectError("refused")])
        _patch_sleep(monkeypatch)
        monkeypatch.setattr("frmj.cli.financing.get_client", lambda conn: fake)

        result = runner.invoke(app, ["financing", "--quiet"])
        assert result.exit_code == 0, result.output
        assert fake.calls == 2

    def test_permanent_http_error_is_not_retried(
        self, fin_db: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A 404 (an untradeable instrument in the list) won't fix itself."""
        fake = _FlakyFinancingClient(failures=[_http_error(404)])
        sleeps = _patch_sleep(monkeypatch)
        monkeypatch.setattr("frmj.cli.financing.get_client", lambda conn: fake)

        result = runner.invoke(app, ["financing", "--quiet"])
        assert result.exit_code == 1
        assert fake.calls == 1
        assert sleeps == []

    def test_combined_with_date_exits_1(
        self, fin_db: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        fake = FakeFullClient()
        monkeypatch.setattr("frmj.cli.financing.get_client", lambda conn: fake)
        result = runner.invoke(app, ["financing", "--quiet", "--date", "2026-01-15"])
        assert result.exit_code == 1
        assert "cannot be combined" in result.output + result.stderr


# ---------------------------------------------------------------------------
# close command
# ---------------------------------------------------------------------------
