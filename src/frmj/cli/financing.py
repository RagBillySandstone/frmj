"""``frmj financing`` — show/record Oanda long/short financing rates."""

from __future__ import annotations

import sqlite3
from datetime import date
from decimal import Decimal

import typer

from frmj.app import get_client, get_db
from frmj.cli import app
from frmj.cli._completion import _FINANCING_PAIRS, _pair_tier
from frmj.cli._display import _color_financing_pct, _fmt_financing_pct
from frmj.execution.oanda import FinancingRate

# ---------------------------------------------------------------------------
# financing command
# ---------------------------------------------------------------------------


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


def _record_financing_snapshot(
    conn: sqlite3.Connection,
    account_id: str,
    rates: list[FinancingRate],
    rate_date: str,
) -> None:
    """Upsert today's fetched *rates* into ``financing_rate_snapshots``.

    Oanda's API exposes only the current rate, so this is the only way
    ``frmj financing --date`` has anything to look up later. Re-running on
    the same *rate_date* overwrites the existing rows (latest fetch wins)
    rather than accumulating duplicates.
    """
    conn.executemany(
        """
        INSERT OR REPLACE INTO financing_rate_snapshots
            (account_id, instrument, rate_date, long_rate, short_rate)
        VALUES (?, ?, ?, ?, ?)
        """,
        [
            (account_id, r.instrument, rate_date, str(r.long_rate), str(r.short_rate))
            for r in rates
        ],
    )
    conn.commit()


def _load_financing_snapshot(
    conn: sqlite3.Connection, account_id: str, rate_date: str
) -> list[FinancingRate]:
    """Return the recorded financing-rate snapshot for *account_id* on *rate_date*.

    Empty list if no snapshot was ever captured for that date (e.g. it
    predates the user's first ``frmj financing`` run, or falls on a date
    that command was never invoked on).
    """
    rows = conn.execute(
        """
        SELECT instrument, long_rate, short_rate
        FROM financing_rate_snapshots
        WHERE account_id = ? AND rate_date = ?
        """,
        (account_id, rate_date),
    ).fetchall()
    return [
        FinancingRate(
            row["instrument"], Decimal(row["long_rate"]), Decimal(row["short_rate"])
        )
        for row in rows
    ]


def _display_financing_rates(
    rates: list[FinancingRate], rate_date: str | None = None
) -> None:
    """Render the major/minor/exotic financing-rate table for *rates*.

    *rate_date* customizes the header for a stored snapshot (``--date``);
    ``None`` renders the header for a live fetch.
    """
    groups = _group_financing_rates(rates)
    long_w = max(len(_fmt_financing_pct(r.long_rate)) for r in rates)
    short_w = max(len(_fmt_financing_pct(r.short_rate)) for r in rates)
    instr_w = max(9, max(len(r.instrument) for r in rates))

    typer.echo("─" * 50)
    if rate_date is None:
        typer.echo("Annualized financing rates (updated daily)")
    else:
        typer.echo(f"Annualized financing rates — snapshot from {rate_date}")
    typer.echo("")

    first = True
    for label, tier_rates in (
        ("Majors", groups["major"]),
        ("Minors", groups["minor"]),
        ("Exotics", groups["exotic"]),
    ):
        if not tier_rates:
            continue
        if not first:
            typer.echo("")
        first = False
        typer.echo(label)
        typer.echo("─" * 50)
        typer.echo(f"  {'':<{instr_w}}  {'Long':>{long_w}}  {'Short':>{short_w}}")
        for rate in tier_rates:
            typer.echo(
                f"  {rate.instrument:<{instr_w}}  "
                f"{_color_financing_pct_padded(rate.long_rate, long_w)}  "
                f"{_color_financing_pct_padded(rate.short_rate, short_w)}"
            )


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
    grouped major/minor/exotic, alphabetical within each group.

    With no options, fetches live rates from Oanda and also records them as
    today's snapshot. With ``--date``, skips the API call and instead looks
    up whatever snapshot was recorded for that date — Oanda's API has no
    historical-rate endpoint, so only dates this command has previously run
    on will have data. ``--quiet`` still fetches and records live but prints
    nothing on success, for unattended use (e.g. a daily cron job that just
    wants the snapshot recorded); it cannot be combined with ``--date``.
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

        rates = _load_financing_snapshot(conn, client.account_id, date_str)
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

    try:
        rates = client.get_financing_rates(list(_FINANCING_PAIRS))
    except Exception as exc:
        typer.echo(f"Error fetching financing rates: {exc}", err=True)
        conn.close()
        raise typer.Exit(1)

    if rates:
        _record_financing_snapshot(
            conn, client.account_id, rates, date.today().isoformat()
        )
    conn.close()

    if quiet:
        return

    if not rates:
        typer.echo("No financing rates returned.")
        return

    _display_financing_rates(rates)
