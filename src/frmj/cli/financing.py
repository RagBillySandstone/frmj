"""``frmj financing`` — show/record Oanda long/short financing rates."""

from __future__ import annotations

import time
from datetime import date
from decimal import Decimal
from itertools import zip_longest
from typing import Any

import httpx
import typer

from frmj import queries
from frmj.app import get_client, get_db
from frmj.cli import app
from frmj.cli._completion import _FINANCING_PAIRS, _pair_tier
from frmj.cli._display import _color_financing_pct, _fmt_financing_pct
from frmj.execution.oanda import FinancingRate

# ---------------------------------------------------------------------------
# --quiet retry
# ---------------------------------------------------------------------------

# Seconds to wait before each retry of a failed --quiet fetch: up to four
# attempts spread over ~21 minutes. A missed day can never be backfilled
# (Oanda has no historical-rate API), so an unattended run is worth waiting
# for; interactive runs fail immediately since the user can just rerun.
_QUIET_RETRY_DELAYS: tuple[int, ...] = (60, 300, 900)


def _is_transient_fetch_error(exc: Exception) -> bool:
    """Return True if *exc* is a fetch failure worth retrying.

    Retried: network/timeout errors, 5xx, 429 (rate limited), and 401 —
    Oanda's practice API has been seen to reject a valid token with 401 for
    a single request and accept it again later. Everything else (e.g. the
    404 Oanda returns for an untradeable instrument in the list) is
    permanent, so retrying would only delay the error.
    """
    # Connection refused, DNS failure, timeouts, etc.
    if isinstance(exc, httpx.TransportError):
        return True
    # Only specific HTTP statuses are transient; anything else is final.
    if isinstance(exc, httpx.HTTPStatusError):
        status = exc.response.status_code
        return status in (401, 429) or status >= 500
    return False


def _fetch_with_retry(client: Any, delays: tuple[int, ...]) -> list[FinancingRate]:
    """Fetch financing rates, retrying transient failures after each of *delays*.

    Makes ``len(delays) + 1`` attempts at most. Retries are silent so a
    ``--quiet`` run that eventually succeeds still prints nothing; the
    last error (or the first non-transient one) is re-raised to the caller.
    """
    # Each delay is the wait *before* the next attempt; the final attempt
    # has no delay after it, so it is made outside the loop.
    for delay in delays:
        try:
            return client.get_financing_rates(list(_FINANCING_PAIRS))
        except Exception as exc:
            if not _is_transient_fetch_error(exc):
                raise
            time.sleep(delay)
    return client.get_financing_rates(list(_FINANCING_PAIRS))


# ---------------------------------------------------------------------------
# financing command
# ---------------------------------------------------------------------------

# Separates the Majors/Minors column from the Exotics column. Eight spaces still
# keep the table within 80 columns: two 33-char columns (rates up to +/-99%) make
# 74, and two 35-char columns (rates of 100% or more on both sides) make 78.
_FINANCING_COLUMN_GAP: str = " " * 8


def _color_financing_pct_padded(rate: Decimal, width: int) -> str:
    """Return _color_financing_pct(rate) right-justified to *width* visible chars."""
    return " " * max(0, width - len(_fmt_financing_pct(rate))) + _color_financing_pct(
        rate
    )


def _group_financing_rates(
    rates: list[FinancingRate],
) -> dict[str, list[FinancingRate]]:
    """Bucket *rates* into major/minor/exotic tiers, alphabetical within each."""
    groups: dict[str, list[FinancingRate]] = {"major": [], "minor": [], "exotic": []}
    for rate in sorted(rates, key=lambda r: r.instrument):
        groups[_pair_tier(rate.instrument)].append(rate)
    return groups


def _financing_column(
    tiers: list[tuple[str, list[FinancingRate]]],
    instr_w: int,
    long_w: int,
    short_w: int,
) -> list[str]:
    """Stack *tiers* into one display column, omitting tiers with no rates.

    Every returned line is padded to the same visible width (color escapes
    excluded), so a second column can be placed beside this one by plain
    string concatenation. Returns an empty list if every tier is empty.
    """
    heading = f"  {'':<{instr_w}}  {'Long':>{long_w}}  {'Short':>{short_w}}"
    # The heading carries no color escapes, so its length is the visible width
    # shared by every row (rows use the same indent and field widths).
    col_w = len(heading)

    lines: list[str] = []
    for label, tier_rates in tiers:
        if not tier_rates:
            continue
        # A blank line separates tiers stacked in the same column. It is padded
        # like everything else so the next column stays aligned beside it.
        if lines:
            lines.append(" " * col_w)
        lines.extend((label.ljust(col_w), "─" * col_w, heading))
        for rate in tier_rates:
            lines.append(
                f"  {rate.instrument:<{instr_w}}  "
                f"{_color_financing_pct_padded(rate.long_rate, long_w)}  "
                f"{_color_financing_pct_padded(rate.short_rate, short_w)}"
            )
    return lines


def _display_financing_rates(
    rates: list[FinancingRate], rate_date: str | None = None
) -> None:
    """Render the financing-rate table for *rates* in two side-by-side columns.

    Majors and minors stack in the first column and exotics fill the second,
    so the full pair list fits on one screen. A column with no rates is left
    out rather than drawn empty. *rate_date* customizes the header for a stored
    snapshot (``--date``); ``None`` renders the header for a live fetch.
    """
    groups = _group_financing_rates(rates)
    # Widths are measured across all rates, not per column, so both columns
    # come out the same width and the table stays regular.
    long_w = max(len(_fmt_financing_pct(r.long_rate)) for r in rates)
    short_w = max(len(_fmt_financing_pct(r.short_rate)) for r in rates)
    instr_w = max(9, max(len(r.instrument) for r in rates))

    candidate_columns: tuple[list[str], list[str]] = (
        _financing_column(
            [("Majors", groups["major"]), ("Minors", groups["minor"])],
            instr_w,
            long_w,
            short_w,
        ),
        _financing_column([("Exotics", groups["exotic"])], instr_w, long_w, short_w),
    )
    # Dropping an empty column keeps an exotics-only table flush left instead
    # of indented behind a blank first column. *rates* is never empty here (the
    # width calculations above require that), so at least one column remains.
    columns: list[list[str]] = [c for c in candidate_columns if c]

    # A column's first line is its tier label: padded and never colored, so its
    # length is the column's visible width.
    blank_cell: str = " " * len(columns[0][0])
    table_w: int = len(_FINANCING_COLUMN_GAP.join(c[0] for c in columns))

    typer.echo("─" * table_w)
    if rate_date is None:
        typer.echo("Annualized financing rates (updated daily)")
    else:
        typer.echo(f"Annualized financing rates — snapshot from {rate_date}")
    typer.echo("")

    # The columns differ in height, so the shorter one is filled out with blank
    # cells. rstrip drops the padding left at the end of a line with nothing
    # beside it; a colored row ends in an escape code, so it is unaffected.
    for cells in zip_longest(*columns, fillvalue=blank_cell):
        typer.echo(_FINANCING_COLUMN_GAP.join(cells).rstrip())


@app.command()
def financing(
    date_str: str | None = typer.Option(
        None,
        "--date",
        help=(
            "Show a previously recorded snapshot for this date (YYYY-MM-DD) "
            "instead of fetching live rates. Only dates `frmj financing` was "
            "actually run on have data — Oanda has no historical-rate API."
        ),
        show_default=False,
    ),
    quiet: bool = typer.Option(
        False,
        "--quiet",
        "-q",
        help=(
            "Fetch and record today's snapshot with no output on success "
            "(errors still print to stderr and exit 1) — for a daily cron job."
        ),
    ),
) -> None:
    """Show long/short financing rates for every tradable FX pair.

    Rates are Oanda's annualized long/short financing percentages — what
    Oanda's own site calls "daily financing rates" (republished daily, but
    quoted per year, not per day). A negative rate means you pay to hold
    that side overnight; a positive rate means you're paid. Pairs are
    grouped major/minor/exotic, alphabetical within each group, with majors
    and minors in the first column and exotics in the second.

    With no options, fetches live rates from Oanda and also records them as
    today's snapshot. With ``--date``, skips the API call and instead looks
    up whatever snapshot was recorded for that date — Oanda's API has no
    historical-rate endpoint, so only dates this command has previously run
    on will have data. ``--quiet`` still fetches and records live but prints
    nothing on success, for unattended use (e.g. a daily cron job that just
    wants the snapshot recorded); it cannot be combined with ``--date``.
    Because a missed day can't be backfilled, ``--quiet`` also retries
    network errors, 5xx, 429, and 401 responses up to three times (after
    1, 5, and 15 minutes) before giving up.
    """
    conn = get_db()

    if quiet and date_str is not None:
        typer.echo("Error: --quiet cannot be combined with --date.", err=True)
        conn.close()
        raise typer.Exit(1)

    if date_str is not None:
        try:
            date.fromisoformat(date_str)
        except ValueError:
            typer.echo(
                f"Error: '{date_str}' is not a valid date (YYYY-MM-DD).", err=True
            )
            conn.close()
            raise typer.Exit(1)

        try:
            client = get_client(conn)
        except RuntimeError as exc:
            typer.echo(f"Error: {exc}", err=True)
            conn.close()
            raise typer.Exit(1)

        rates = queries.load_financing_snapshot(conn, client.account_id, date_str)
        conn.close()

        if not rates:
            typer.echo(f"No financing snapshot recorded for {date_str}.")
            return

        _display_financing_rates(rates, rate_date=date_str)
        return

    try:
        client = get_client(conn)
    except RuntimeError as exc:
        typer.echo(f"Error: {exc}", err=True)
        conn.close()
        raise typer.Exit(1)

    # Unattended (--quiet) runs retry transient failures; interactive runs
    # make a single attempt (an empty delay tuple).
    delays = _QUIET_RETRY_DELAYS if quiet else ()
    try:
        rates = _fetch_with_retry(client, delays)
    except Exception as exc:
        typer.echo(f"Error fetching financing rates: {exc}", err=True)
        conn.close()
        raise typer.Exit(1)

    if rates:
        queries.record_financing_snapshot(
            conn, client.account_id, rates, date.today().isoformat()
        )
    conn.close()

    if quiet:
        return

    if not rates:
        typer.echo("No financing rates returned.")
        return

    _display_financing_rates(rates)
