"""``frmj book orders`` / ``frmj book positions`` — Oanda's order and position books.

Every 20 minutes Oanda publishes two snapshots per instrument, each a list
of price buckets with the percentage of all longs and shorts that sit in
each one:

* the **order book**: where its clients have orders waiting — entry
  orders, take-profits and stop-losses;
* the **position book**: the prices at which their open positions were
  entered.

These commands fetch one snapshot (the latest, or a past one with ``--at``),
keep the buckets within ``--pips`` of the snapshot price, and write them as
CSV or JSON for the analytics stack. Nothing is stored locally: Oanda keeps
the history itself, back to at least late 2017. The data covers only Oanda's
own clients, and counts orders/positions rather than units.
"""

from __future__ import annotations

import csv
import io
import json
from datetime import datetime, timezone
from decimal import Decimal

import httpx
import typer

from frmj.app import get_client, get_db
from frmj.cli import book_app
from frmj.cli._completion import _complete_output_format, _match_typed_case
from frmj.execution.oanda import Book, BookBucket

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

# The instruments Oanda publishes books for, as found by probing the practice
# API on 2026-11-08, each with Oanda's pipLocation (one pip = 10 ** it). Every
# other pair frmj knows (EUR_CAD, the exotics, the other metals) gets a 400
# "not a valid instrument". Used for TAB completion, the hint after that
# error, and the pip size --pips is measured in. The pip size can't come from
# Oanda at run time: its instruments endpoint is per account, and refuses an
# instrument the account can't trade (metals on a US account) even though the
# book endpoint serves it. The FX values were read from that endpoint; the
# metals' are Oanda's published ones, matching the 3 and 5 decimals their
# prices are quoted to.
_BOOK_PIP_LOCATIONS: dict[str, int] = {
    "AUD_JPY": -2,
    "AUD_USD": -4,
    "EUR_AUD": -4,
    "EUR_CHF": -4,
    "EUR_GBP": -4,
    "EUR_JPY": -2,
    "EUR_USD": -4,
    "GBP_CHF": -4,
    "GBP_JPY": -2,
    "GBP_USD": -4,
    "NZD_USD": -4,
    "USD_CAD": -4,
    "USD_CHF": -4,
    "USD_JPY": -2,
    "XAG_USD": -4,
    "XAU_USD": -2,
}

# Oanda snapshots each book at :00, :20 and :40 past the hour, and rejects a
# requested time off that grid with a 400 ("misaligned").
_SNAPSHOT_MINUTES: int = 20

# How far either side of the snapshot price to keep buckets, by default. The
# full order book runs to thousands of buckets, out to stray orders at absurd
# prices (0 and 5,555,555,555 for EUR_USD); +/-200 pips keeps everything a
# chart of the market around the price needs.
_DEFAULT_PIPS: int = 200

# CSV columns: one row per bucket, with the snapshot's own fields repeated on
# every row so the file loads as a single tidy table.
_CSV_FIELDS: tuple[str, ...] = (
    "instrument",
    "time",
    "price",
    "bucket_width",
    "bucket_price",
    "long_pct",
    "short_pct",
)


# ---------------------------------------------------------------------------
# Pure helpers (tested directly)
# ---------------------------------------------------------------------------


def _complete_book_instrument(incomplete: str) -> list[str]:
    """Return the instruments Oanda keeps books for, starting with *incomplete*.

    Matching and the case of the result follow ``_match_typed_case``, as for
    every other instrument argument.
    """
    return _match_typed_case(_BOOK_PIP_LOCATIONS, incomplete)


def _snapshot_time(value: str) -> datetime:
    """Parse an ``--at`` value and round it down to Oanda's snapshot grid.

    *value* is any ISO-8601 date or date-time ``datetime.fromisoformat``
    accepts: ``2026-11-06``, ``2026-11-06 14:07``, ``2026-11-06T14:07Z``,
    ``2026-11-06T14:07+10:00``. A value with no zone is local time, as times
    are everywhere else in frmj. The result is UTC, rounded *down* to the
    last :00, :20 or :40 — the latest snapshot taken at or before the moment
    asked for, so looking up the book at a trade's fill time never returns a
    snapshot taken after the fill.

    Raises ``ValueError`` if *value* isn't a date or date-time.
    """
    moment = datetime.fromisoformat(value)
    # A naive datetime is local time; astimezone() attaches the local zone.
    if moment.tzinfo is None:
        moment = moment.astimezone()
    moment = moment.astimezone(timezone.utc)
    # Drop the minutes past the last grid line, and anything finer than that.
    return moment.replace(
        minute=moment.minute - moment.minute % _SNAPSHOT_MINUTES,
        second=0,
        microsecond=0,
    )


def _trim_buckets(book: Book, pips: int, pip: Decimal) -> tuple[BookBucket, ...]:
    """Return *book*'s buckets whose price is within *pips* pips of its price.

    A bucket's price is its lower edge. *pip* is the instrument's pip size
    (``0.0001`` for EUR_USD, ``0.01`` for USD_JPY); the window is measured in
    pips rather than buckets so it means the same whatever Oanda's bucket
    width is. Order is preserved.
    """
    reach = pip * pips
    return tuple(b for b in book.buckets if abs(b.price - book.price) <= reach)


def _book_to_csv(book: Book, buckets: tuple[BookBucket, ...]) -> str:
    """Serialise *buckets* of *book* as CSV text, one row per bucket.

    Numbers are written exactly as Oanda sent them. Every cell is either a
    number or an Oanda instrument name or timestamp, so none can start a
    spreadsheet formula and no neutralizing (as ``export`` does) is needed.
    """
    buf = io.StringIO()
    writer = csv.writer(buf, lineterminator="\n")
    writer.writerow(_CSV_FIELDS)
    for bucket in buckets:
        writer.writerow(
            (
                book.instrument,
                book.time,
                book.price,
                book.bucket_width,
                bucket.price,
                bucket.long_pct,
                bucket.short_pct,
            )
        )
    return buf.getvalue()


def _book_to_json(book: Book, buckets: tuple[BookBucket, ...]) -> str:
    """Serialise *buckets* of *book* as one JSON object.

    Numbers are strings, exactly as Oanda sends them, so no precision is lost
    to floats; field names match the CSV columns.
    """
    document = {
        "instrument": book.instrument,
        "time": book.time,
        "price": str(book.price),
        "bucket_width": str(book.bucket_width),
        "buckets": [
            {
                "price": str(b.price),
                "long_pct": str(b.long_pct),
                "short_pct": str(b.short_pct),
            }
            for b in buckets
        ],
    }
    return json.dumps(document, indent=2) + "\n"


def _oanda_error_message(exc: httpx.HTTPStatusError) -> str:
    """Return Oanda's own ``errorMessage`` for *exc*, or its HTTP status.

    Oanda's messages for these endpoints say exactly what went wrong ("EUR_CAD
    is not a valid instrument.", "The snapshot for EUR_USD at ... is not yet
    available. The most recent snapshot was at ..."), so they're shown as-is.
    """
    try:
        message = exc.response.json().get("errorMessage")
    except ValueError:
        # Not JSON (a proxy's HTML error page, an empty body): no message.
        message = None
    return message or f"Oanda returned HTTP {exc.response.status_code}."


# ---------------------------------------------------------------------------
# Shared command body
# ---------------------------------------------------------------------------


def _run_book(
    kind: str,
    instrument: str,
    at: str | None,
    pips: int,
    fmt: str,
    output: str | None,
) -> None:
    """Fetch, trim and write one book snapshot; *kind* is "order" or "position".

    Errors are reported on stderr with exit status 1, before any output is
    written, so a failed run never leaves a partial file behind.
    """
    # --- validate the options before touching the network ------------------
    if fmt not in ("csv", "json"):
        typer.echo("Error: --format must be 'csv' or 'json'.", err=True)
        raise typer.Exit(1)
    try:
        time = _snapshot_time(at) if at is not None else None
    except ValueError:
        typer.echo(
            f"Error: '{at}' is not a valid time. Use e.g. '2026-11-06 14:20' "
            "(local time) or '2026-11-06T14:20Z' (UTC).",
            err=True,
        )
        raise typer.Exit(1)
    instrument = instrument.upper()

    # --- fetch the snapshot, then the pip size to trim it by ---------------
    # The account only supplies the API token: book data isn't per-account.
    conn = get_db()
    try:
        client = get_client(conn)
    except RuntimeError as exc:
        typer.echo(f"Error: {exc}", err=True)
        raise typer.Exit(1)
    finally:
        conn.close()
    try:
        if kind == "order":
            book = client.get_order_book(instrument, time)
        else:
            book = client.get_position_book(instrument, time)
        # An instrument missing from the table is one Oanda has started
        # keeping a book for since; ask the account for its pip location,
        # which works if the account can trade it. Done after the book
        # fetch, so a bad instrument is reported by the book endpoint, whose
        # messages are the clearer ones.
        pip_location = _BOOK_PIP_LOCATIONS.get(instrument)
        if pip_location is None:
            pip_location = client.get_instrument(instrument).pip_location
    except httpx.HTTPStatusError as exc:
        typer.echo(f"Error: {_oanda_error_message(exc)}", err=True)
        # Oanda's "not a valid instrument" is misleading for a pair you can
        # trade but that has no book, so say which ones have one.
        if instrument not in _BOOK_PIP_LOCATIONS:
            typer.echo(
                "Oanda keeps order and position books only for: "
                + ", ".join(_BOOK_PIP_LOCATIONS),
                err=True,
            )
        raise typer.Exit(1)
    except Exception as exc:
        typer.echo(f"Error fetching the {kind} book: {exc}", err=True)
        raise typer.Exit(1)

    # --- trim and write ------------------------------------------------------
    # One pip is 10 ** pip_location: 0.0001 for EUR_USD, 0.01 for USD_JPY.
    buckets = _trim_buckets(book, pips, Decimal(10) ** pip_location)
    content = (
        _book_to_csv(book, buckets) if fmt == "csv" else _book_to_json(book, buckets)
    )
    if output:
        with open(output, "w", encoding="utf-8", newline="") as fh:
            fh.write(content)
        typer.echo(
            f"Wrote {len(buckets)} buckets ({book.instrument} {kind} book at "
            f"{book.time}) to {output}"
        )
    else:
        typer.echo(content, nl=False)


# ---------------------------------------------------------------------------
# Commands
# ---------------------------------------------------------------------------

# Help text shared by both commands' options.
_INSTRUMENT_HELP = "Instrument, e.g. EUR_USD. TAB lists the ones Oanda keeps books for."
_AT_HELP = (
    "Fetch the snapshot at this time instead of the latest, e.g. "
    "'2026-11-06 14:07' (local time) or '2026-11-06T14:07Z' (UTC). "
    "Rounded down to Oanda's 20-minute snapshot grid."
)
_PIPS_HELP = "Keep only buckets within this many pips of the snapshot price."
_FORMAT_HELP = "Output format: csv or json."
_OUTPUT_HELP = "Write to this file path instead of stdout."


@book_app.command("orders")
def book_orders(
    instrument: str = typer.Argument(
        ..., help=_INSTRUMENT_HELP, autocompletion=_complete_book_instrument
    ),
    at: str | None = typer.Option(None, "--at", help=_AT_HELP, show_default=False),
    pips: int = typer.Option(_DEFAULT_PIPS, "--pips", min=1, help=_PIPS_HELP),
    fmt: str = typer.Option(
        "csv",
        "--format",
        "-f",
        help=_FORMAT_HELP,
        autocompletion=_complete_output_format,
    ),
    output: str | None = typer.Option(
        None, "--output", "-o", help=_OUTPUT_HELP, show_default=False
    ),
) -> None:
    """Where Oanda's clients have orders waiting, by price (the order book).

    Each bucket gives the percentage of all buy (long) and sell (short) orders
    in the book that sit in it. The book includes take-profits and
    stop-losses, so sells clustered just below the price usually mark longs'
    stops. One snapshot every 20 minutes; written as CSV or JSON.
    """
    _run_book("order", instrument, at, pips, fmt, output)


@book_app.command("positions")
def book_positions(
    instrument: str = typer.Argument(
        ..., help=_INSTRUMENT_HELP, autocompletion=_complete_book_instrument
    ),
    at: str | None = typer.Option(None, "--at", help=_AT_HELP, show_default=False),
    pips: int = typer.Option(_DEFAULT_PIPS, "--pips", min=1, help=_PIPS_HELP),
    fmt: str = typer.Option(
        "csv",
        "--format",
        "-f",
        help=_FORMAT_HELP,
        autocompletion=_complete_output_format,
    ),
    output: str | None = typer.Option(
        None, "--output", "-o", help=_OUTPUT_HELP, show_default=False
    ),
) -> None:
    """Where Oanda's clients' open positions were entered, by price (the
    position book).

    Each bucket gives the percentage of all open long and short positions
    that were entered at a price in it. One snapshot every 20 minutes;
    written as CSV or JSON.
    """
    _run_book("position", instrument, at, pips, fmt, output)
