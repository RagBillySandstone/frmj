"""Tests for ``frmj book orders`` / ``frmj book positions``.

The pure helpers (time rounding, trimming, serialising, error messages) are
tested directly; the commands are run through Typer with a fake client that
records what it was asked for, so no request reaches Oanda.
"""

from __future__ import annotations

import csv
import io
import json
import time
from collections.abc import Iterator
from dataclasses import dataclass, field
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path

import httpx
import pytest
from click.testing import Result
from typer.testing import CliRunner

from frmj.cli import app
from frmj.cli.book import (
    _book_to_csv,
    _book_to_json,
    _complete_book_instrument,
    _oanda_error_message,
    _snapshot_time,
    _trim_buckets,
)
from frmj.domain.sizing import InstrumentSpec
from frmj.execution.oanda import Book, BookBucket

runner = CliRunner()


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _bucket(
    price: str, long_pct: str = "0.1000", short_pct: str = "0.2000"
) -> BookBucket:
    """A bucket at *price* with the given (string) percentages."""
    return BookBucket(Decimal(price), Decimal(long_pct), Decimal(short_pct))


def _eur_usd_book() -> Book:
    """A small EUR_USD book around 1.10000 with 5-pip buckets.

    Buckets sit at 0 (a stray order), 50 pips below the price, at the price,
    10 pips above, 200 pips above (the default window's edge), 205 pips
    above (just outside it), and at an absurd price, as Oanda's real book has.
    """
    return Book(
        instrument="EUR_USD",
        time="2026-11-06T14:20:00Z",
        price=Decimal("1.10000"),
        bucket_width=Decimal("0.00050"),
        buckets=(
            _bucket("0.00000"),
            _bucket("1.09500"),
            _bucket("1.10000", "1.5541", "0.4010"),
            _bucket("1.10100"),
            _bucket("1.12000"),
            _bucket("1.12050"),
            _bucket("5555555555.00000"),
        ),
    )


def _http_error(status: int, body: bytes = b"") -> httpx.HTTPStatusError:
    """The error ``raise_for_status`` raises for *status* with response *body*."""
    request = httpx.Request("GET", "https://api-fxpractice.oanda.com/v3/x")
    response = httpx.Response(status, request=request, content=body)
    return httpx.HTTPStatusError(f"{status}", request=request, response=response)


@dataclass
class _FakeBookClient:
    """Stands in for ``OandaClient``: serves *book*, or raises *error*.

    Every book request is recorded in ``calls`` as (endpoint, instrument,
    time), and every ``get_instrument`` call in ``instrument_calls``.
    """

    book: Book = field(default_factory=_eur_usd_book)
    error: Exception | None = None
    calls: list[tuple[str, str, datetime | None]] = field(default_factory=list)
    instrument_calls: list[str] = field(default_factory=list)

    def _serve(self, endpoint: str, instrument: str, at: datetime | None) -> Book:
        """Record the request, then raise the queued error or return the book."""
        self.calls.append((endpoint, instrument, at))
        if self.error is not None:
            raise self.error
        return self.book

    def get_order_book(self, instrument: str, time: datetime | None = None) -> Book:
        return self._serve("orderBook", instrument, time)

    def get_position_book(self, instrument: str, time: datetime | None = None) -> Book:
        return self._serve("positionBook", instrument, time)

    def get_instrument(self, name: str) -> InstrumentSpec:
        """Only reached for an instrument missing from the pip table."""
        self.instrument_calls.append(name)
        return InstrumentSpec(
            name=name,
            pip_location=-4,
            margin_rate=Decimal("0.05"),
            min_units=1,
            units_increment=1,
            display_precision=5,
        )


@pytest.fixture()
def book_db(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Point frmj at an empty throwaway database (the client is faked)."""
    path = tmp_path / "book_test.db"
    monkeypatch.setenv("FRMJ_DB_PATH", str(path))
    return path


@pytest.fixture()
def kolkata_tz(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    """Make local time India's (UTC+05:30, no DST) for the duration of a test.

    A half-hour offset shows that a local time is converted to UTC *before*
    it is rounded down: rounding first would land off Oanda's UTC grid.
    """
    monkeypatch.setenv("TZ", "Asia/Kolkata")
    time.tzset()
    yield
    monkeypatch.undo()
    time.tzset()


def _invoke(
    monkeypatch: pytest.MonkeyPatch, fake: _FakeBookClient, args: list[str]
) -> Result:
    """Run ``frmj book ...args`` against *fake*; return the Typer result."""
    monkeypatch.setattr("frmj.cli.book.get_client", lambda conn: fake)
    return runner.invoke(app, ["book", *args])


# ---------------------------------------------------------------------------
# Pure helpers
# ---------------------------------------------------------------------------


class TestSnapshotTime:
    @pytest.mark.parametrize(
        ("value", "expected"),
        [
            ("2026-11-06T14:07Z", datetime(2026, 11, 6, 14, 0, tzinfo=timezone.utc)),
            (
                "2026-11-06T14:59:59Z",
                datetime(2026, 11, 6, 14, 40, tzinfo=timezone.utc),
            ),
            (
                "2026-11-06T14:20:00Z",
                datetime(2026, 11, 6, 14, 20, tzinfo=timezone.utc),
            ),
            # An explicit offset is converted: 00:25 at UTC+10 is 14:25 UTC.
            (
                "2026-11-07T00:25+10:00",
                datetime(2026, 11, 6, 14, 20, tzinfo=timezone.utc),
            ),
        ],
    )
    def test_rounds_down_to_the_20_minute_utc_grid(
        self, value: str, expected: datetime
    ) -> None:
        assert _snapshot_time(value) == expected

    def test_seconds_and_microseconds_are_dropped(self) -> None:
        moment = _snapshot_time("2026-11-06T14:41:30.123456Z")
        assert (moment.minute, moment.second, moment.microsecond) == (40, 0, 0)

    def test_a_time_without_a_zone_is_local(self, kolkata_tz: None) -> None:
        """20:07 in Kolkata is 14:37 UTC, which rounds down to 14:20 UTC."""
        assert _snapshot_time("2026-11-06 20:07") == datetime(
            2026, 11, 6, 14, 20, tzinfo=timezone.utc
        )

    def test_a_bare_date_is_local_midnight(self, kolkata_tz: None) -> None:
        """Midnight in Kolkata is 18:30 UTC the day before, rounded to 18:20."""
        assert _snapshot_time("2026-11-06") == datetime(
            2026, 11, 5, 18, 20, tzinfo=timezone.utc
        )

    @pytest.mark.parametrize("bad", ["yesterday", "2026-13-01", "14:20", ""])
    def test_not_a_date_raises(self, bad: str) -> None:
        with pytest.raises(ValueError):
            _snapshot_time(bad)


class TestTrimBuckets:
    def test_keeps_buckets_within_the_window_inclusive(self) -> None:
        """At +/-200 pips the 200-pip bucket stays and the 205-pip one goes,
        as do the stray orders at 0 and at an absurd price."""
        kept = _trim_buckets(_eur_usd_book(), 200, Decimal("0.0001"))
        assert [str(b.price) for b in kept] == [
            "1.09500",
            "1.10000",
            "1.10100",
            "1.12000",
        ]

    def test_window_is_measured_in_the_instruments_pips(self) -> None:
        """A JPY pip is 0.01, so 20 pips reaches 0.20 either side."""
        book = Book(
            instrument="USD_JPY",
            time="2026-11-06T14:20:00Z",
            price=Decimal("158.240"),
            bucket_width=Decimal("0.050"),
            buckets=(_bucket("158.000"), _bucket("158.050"), _bucket("158.450")),
        )
        kept = _trim_buckets(book, 20, Decimal("0.01"))
        assert [str(b.price) for b in kept] == ["158.050"]


class TestSerialising:
    def test_csv_is_one_row_per_bucket_with_oandas_exact_numbers(self) -> None:
        book = _eur_usd_book()
        text = _book_to_csv(book, book.buckets[2:3])
        assert text == (
            "instrument,time,price,bucket_width,bucket_price,long_pct,short_pct\n"
            "EUR_USD,2026-11-06T14:20:00Z,1.10000,0.00050,1.10000,1.5541,0.4010\n"
        )

    def test_csv_with_no_buckets_is_just_the_header(self) -> None:
        assert _book_to_csv(_eur_usd_book(), ()).count("\n") == 1

    def test_json_keeps_numbers_as_exact_strings(self) -> None:
        book = _eur_usd_book()
        document = json.loads(_book_to_json(book, book.buckets[2:3]))
        assert document == {
            "instrument": "EUR_USD",
            "time": "2026-11-06T14:20:00Z",
            "price": "1.10000",
            "bucket_width": "0.00050",
            "buckets": [
                {"price": "1.10000", "long_pct": "1.5541", "short_pct": "0.4010"}
            ],
        }


class TestOandaErrorMessage:
    def test_uses_oandas_error_message(self) -> None:
        exc = _http_error(400, b'{"errorMessage":"EUR_CAD is not a valid instrument."}')
        assert _oanda_error_message(exc) == "EUR_CAD is not a valid instrument."

    @pytest.mark.parametrize("body", [b"", b"<html>Bad gateway</html>", b"{}"])
    def test_falls_back_to_the_status(self, body: bytes) -> None:
        assert _oanda_error_message(_http_error(502, body)) == (
            "Oanda returned HTTP 502."
        )


class TestCompleteBookInstrument:
    def test_lists_only_instruments_with_books_in_the_case_typed(self) -> None:
        assert _complete_book_instrument("eur_") == [
            "eur_aud",
            "eur_chf",
            "eur_gbp",
            "eur_jpy",
            "eur_usd",
        ]
        assert _complete_book_instrument("XA") == ["XAG_USD", "XAU_USD"]

    def test_pairs_without_a_book_are_not_offered(self) -> None:
        assert _complete_book_instrument("EUR_CA") == []


# ---------------------------------------------------------------------------
# Commands
# ---------------------------------------------------------------------------


class TestBookCommands:
    @pytest.mark.parametrize(
        ("command", "endpoint"),
        [("orders", "orderBook"), ("positions", "positionBook")],
    )
    def test_latest_snapshot_as_trimmed_csv(
        self,
        book_db: Path,
        monkeypatch: pytest.MonkeyPatch,
        command: str,
        endpoint: str,
    ) -> None:
        """Each command hits its own endpoint for the latest snapshot and
        writes the buckets within the default 200 pips as CSV on stdout."""
        fake = _FakeBookClient()
        result = _invoke(monkeypatch, fake, [command, "eur_usd"])
        assert result.exit_code == 0, result.output
        assert fake.calls == [(endpoint, "EUR_USD", None)]
        rows = list(csv.DictReader(io.StringIO(result.stdout)))
        assert [r["bucket_price"] for r in rows] == [
            "1.09500",
            "1.10000",
            "1.10100",
            "1.12000",
        ]
        # The pip size comes from the table, not a second request.
        assert fake.instrument_calls == []

    def test_at_sends_the_snapshot_time_rounded_down(
        self, book_db: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        fake = _FakeBookClient()
        result = _invoke(
            monkeypatch, fake, ["orders", "EUR_USD", "--at", "2026-11-06T14:07Z"]
        )
        assert result.exit_code == 0, result.output
        assert fake.calls[0][2] == datetime(2026, 11, 6, 14, 0, tzinfo=timezone.utc)

    def test_pips_narrows_the_window(
        self, book_db: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        result = _invoke(
            monkeypatch, _FakeBookClient(), ["orders", "EUR_USD", "--pips", "10"]
        )
        assert result.exit_code == 0, result.output
        rows = list(csv.DictReader(io.StringIO(result.stdout)))
        assert [r["bucket_price"] for r in rows] == ["1.10000", "1.10100"]

    def test_pips_must_be_positive(
        self, book_db: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        fake = _FakeBookClient()
        result = _invoke(monkeypatch, fake, ["orders", "EUR_USD", "--pips", "0"])
        assert result.exit_code == 2
        assert fake.calls == []

    def test_json_format(self, book_db: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        result = _invoke(
            monkeypatch, _FakeBookClient(), ["positions", "EUR_USD", "-f", "json"]
        )
        assert result.exit_code == 0, result.output
        document = json.loads(result.stdout)
        assert document["time"] == "2026-11-06T14:20:00Z"
        assert len(document["buckets"]) == 4

    def test_output_writes_the_file_and_says_what_it_wrote(
        self, book_db: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        out = tmp_path / "book.csv"
        result = _invoke(
            monkeypatch, _FakeBookClient(), ["orders", "EUR_USD", "-o", str(out)]
        )
        assert result.exit_code == 0, result.output
        assert out.read_text().startswith("instrument,time,")
        assert result.stdout == (
            f"Wrote 4 buckets (EUR_USD order book at 2026-11-06T14:20:00Z) to {out}\n"
        )

    def test_instrument_missing_from_the_pip_table_asks_the_account(
        self, book_db: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A book Oanda starts publishing later still works, using the
        account's instrument spec for its pip size."""
        fake = _FakeBookClient()
        result = _invoke(monkeypatch, fake, ["orders", "EUR_CAD"])
        assert result.exit_code == 0, result.output
        assert fake.instrument_calls == ["EUR_CAD"]

    def test_instrument_without_a_book_lists_the_ones_that_have_one(
        self, book_db: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        fake = _FakeBookClient(
            error=_http_error(
                400, b'{"errorMessage":"EUR_CAD is not a valid instrument."}'
            )
        )
        result = _invoke(monkeypatch, fake, ["orders", "EUR_CAD"])
        assert result.exit_code == 1
        assert result.stdout == ""
        assert "Error: EUR_CAD is not a valid instrument." in result.stderr
        assert "only for: AUD_JPY, AUD_USD," in result.stderr

    def test_missing_snapshot_shows_oandas_message_without_the_hint(
        self, book_db: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        message = (
            b'{"errorMessage":"The snapshot for EUR_USD at ... is not yet available."}'
        )
        fake = _FakeBookClient(error=_http_error(404, message))
        result = _invoke(
            monkeypatch, fake, ["positions", "EUR_USD", "--at", "2027-01-01"]
        )
        assert result.exit_code == 1
        assert "is not yet available" in result.stderr
        assert "only for:" not in result.stderr

    def test_network_error_is_one_line(
        self, book_db: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        fake = _FakeBookClient(error=httpx.ConnectError("connection refused"))
        result = _invoke(monkeypatch, fake, ["positions", "EUR_USD"])
        assert result.exit_code == 1
        assert result.stderr == "Error fetching the position book: connection refused\n"

    def test_failed_fetch_leaves_no_output_file(
        self, book_db: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        out = tmp_path / "book.csv"
        fake = _FakeBookClient(error=_http_error(502))
        result = _invoke(monkeypatch, fake, ["orders", "EUR_USD", "-o", str(out)])
        assert result.exit_code == 1
        assert not out.exists()

    @pytest.mark.parametrize(
        ("args", "message"),
        [
            (["--at", "yesterday"], "'yesterday' is not a valid time"),
            (["--format", "xml"], "--format must be 'csv' or 'json'"),
        ],
    )
    def test_bad_options_fail_before_any_request(
        self,
        book_db: Path,
        monkeypatch: pytest.MonkeyPatch,
        args: list[str],
        message: str,
    ) -> None:
        fake = _FakeBookClient()
        result = _invoke(monkeypatch, fake, ["orders", "EUR_USD", *args])
        assert result.exit_code == 1
        assert message in result.stderr
        assert fake.calls == []

    def test_missing_token_is_reported(
        self, book_db: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        def no_client(conn: object) -> None:
            raise RuntimeError("No API token found for the practice environment.")

        monkeypatch.setattr("frmj.cli.book.get_client", no_client)
        result = runner.invoke(app, ["book", "orders", "EUR_USD"])
        assert result.exit_code == 1
        assert "Error: No API token found" in result.stderr
