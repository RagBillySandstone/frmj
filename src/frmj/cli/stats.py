"""``frmj stats`` — trade performance statistics from the local journal."""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from decimal import Decimal

import typer

from frmj.accounts import resolve_account
from frmj.app import get_client, get_db
from frmj.cli import app
from frmj.cli._completion import _complete_account_name
from frmj.cli._display import _color_pl_padded, _pl_visible_width
from frmj.domain.analytics import (
    ClosedTrade,
    DirectionStats,
    compute_summary,
    pl_by_direction,
    pl_by_hour,
    pl_by_instrument,
    pl_by_instrument_direction,
    pl_by_weekday,
)
from frmj.execution.sync import sync_incremental

# ---------------------------------------------------------------------------
# stats command
# ---------------------------------------------------------------------------


@app.command()
def stats(
    all_accounts: bool = typer.Option(
        False,
        "--all-accounts",
        "-A",
        help="Combine statistics from every account, not just the active one.",
    ),
    account_name: str | None = typer.Option(
        None,
        "--account",
        "-a",
        help="Use this account instead of the active one (see 'frmj account list').",
        autocompletion=_complete_account_name,
        show_default=False,
    ),
) -> None:
    """Show trade performance statistics from the local journal.

    By default only the active account's trades are counted; pass
    ``--account NAME`` to report on another account instead, or
    ``--all-accounts`` to combine every account in the local database.  When
    no active account is configured there is nothing to scope to, so all
    accounts are combined.
    """
    # The two scoping options contradict each other; refuse rather than
    # silently letting one win.
    if account_name is not None and all_accounts:
        typer.echo(
            "Error: --account and --all-accounts cannot be used together.", err=True
        )
        raise typer.Exit(1)

    conn = get_db()

    # Resolve the scope locally (no token needed) so stats still work when
    # the auto-sync below fails.  A typo in --account must fail here rather
    # than fall through to combining every account.
    account = None if all_accounts else resolve_account(conn, account_name)
    if account is None and account_name is not None:
        typer.echo(
            f"Error: No account named '{account_name}'. List accounts with:\n"
            "  frmj account list",
            err=True,
        )
        conn.close()
        raise typer.Exit(1)

    try:
        client = get_client(conn, account_name)
        sync_result = sync_incremental(conn, client)
        if sync_result.rows_ingested:
            typer.echo(f"[sync] +{sync_result.rows_ingested} transactions")
    except RuntimeError as exc:
        typer.echo(f"[sync] Warning: {exc}", err=True)
    except Exception as exc:
        typer.echo(f"[sync] Warning: sync failed — {exc}", err=True)

    # Every query below is limited to the resolved account, or unfiltered
    # when combining all accounts.  Each query aliases the transactions
    # table differently, so the column is qualified per query.
    scope_params: tuple[str, ...] = (account.oanda_id,) if account else ()
    fills_sql = " AND t.account_id = ?" if account else ""
    tags_sql = " AND tx.account_id = ?" if account else ""
    fin_sql = " AND account_id = ?" if account else ""

    try:
        rows = conn.execute(
            """
            SELECT t.id, t.oanda_id, t.time, t.raw_json,
                   open_t.time AS open_time
            FROM transactions t
            -- Resolve the opening fill so we can also bucket by open time.
            -- COALESCE covers both full closes (tradesClosed array) and
            -- partial reduces (tradeReduced object), each carrying tradeID.
            LEFT JOIN transactions open_t
                ON  open_t.account_id = t.account_id
                AND open_t.type       = 'ORDER_FILL'
                AND open_t.oanda_id   = COALESCE(
                        json_extract(t.raw_json, '$.tradesClosed[0].tradeID'),
                        json_extract(t.raw_json, '$.tradeReduced.tradeID')
                    )
            WHERE t.type = 'ORDER_FILL'
            """
            + fills_sql,
            scope_params,
        ).fetchall()
        # Tag breakdown: for each tag, collect P/L values of tagged closing fills.
        tag_rows = conn.execute(
            """
            SELECT tg.tag, tx.raw_json
            FROM tags tg
            JOIN transactions tx ON tg.transaction_id = tx.id
            WHERE tx.type = 'ORDER_FILL'
            """
            + tags_sql,
            scope_params,
        ).fetchall()
        # Financing breakdown: each DAILY_FINANCING row's "positionFinancings"
        # array is unpacked per-instrument below.
        financing_rows = conn.execute(
            "SELECT raw_json FROM transactions WHERE type = 'DAILY_FINANCING'"
            + fin_sql,
            scope_params,
        ).fetchall()
    finally:
        conn.close()

    trades: list[ClosedTrade] = []
    for row in rows:
        try:
            data = json.loads(row["raw_json"])
            pl_val = Decimal(data.get("pl", "0") or "0")
            if pl_val == 0:
                continue  # opening fill — no realised P/L
            units_raw = int(Decimal(data.get("units", "0")))
            trades.append(
                ClosedTrade(
                    oanda_id=row["oanda_id"],
                    instrument=data.get("instrument", ""),
                    time=row["time"],
                    pl=pl_val,
                    units=abs(units_raw),
                    # Closing a long = negative units in close txn; short = positive.
                    direction="LONG" if units_raw < 0 else "SHORT",
                    # None when the opening fill was not found via the JOIN.
                    open_time=row["open_time"],
                )
            )
        except Exception:
            continue

    # Name the scope up front: combined figures must never be mistaken for
    # one account's.
    typer.echo(f"Account: {account.name}" if account else "Accounts: all")

    if not trades:
        typer.echo("No closed trades in local database.")
        return

    # Build tag → list[Decimal] map from tag_rows (skip opening fills).
    tag_pl: dict[str, list[Decimal]] = {}
    for tr in tag_rows:
        try:
            data = json.loads(tr["raw_json"])
            pl_val = Decimal(data.get("pl", "0") or "0")
            if pl_val != 0:
                tag_pl.setdefault(tr["tag"], []).append(pl_val)
        except Exception:
            continue

    # Build instrument → list[Decimal] map of financing amounts. Each
    # DAILY_FINANCING row is a single self-contained transaction: the
    # top-level "financing" field is the day's account-wide total, and the
    # "positionFinancings" array carries the per-instrument breakdown.
    financing_by_instrument: dict[str, list[Decimal]] = {}
    for fr in financing_rows:
        try:
            data = json.loads(fr["raw_json"])
            for pf in data.get("positionFinancings", []):
                instrument = pf.get("instrument")
                if not instrument:
                    continue
                amount = Decimal(pf.get("financing") or "0")
                if amount != 0:
                    financing_by_instrument.setdefault(instrument, []).append(amount)
        except Exception:
            continue

    _display_stats(trades, tag_pl, financing_by_instrument)


def _display_stats(
    trades: list[ClosedTrade],
    tag_pl: dict[str, list[Decimal]] | None = None,
    financing_by_instrument: dict[str, list[Decimal]] | None = None,
) -> None:
    """Render the full stats report for the given closed trades."""
    summary = compute_summary(trades)
    assert summary is not None  # trades is guaranteed non-empty by caller

    # Total financing paid/earned across all instruments, folded into the
    # summary block so it reads alongside Total P/L. Decimal(0) when the
    # account has no DAILY_FINANCING transactions synced yet.
    total_financing = sum(
        (
            amt
            for amounts in (financing_by_instrument or {}).values()
            for amt in amounts
        ),
        Decimal(0),
    )

    typer.echo(f"Trade summary  ({summary.total} closed trades)")
    typer.echo("─" * 50)
    typer.echo(
        f"  Win rate:   {summary.win_rate * 100:.1f}%"
        f"  ({summary.wins}W / {summary.losses}L)"
    )
    # Right-align all five dollar values to the widest one in the block.
    summary_pl_w = max(
        _pl_visible_width(summary.avg_pl),
        _pl_visible_width(summary.total_pl),
        _pl_visible_width(summary.best_pl),
        _pl_visible_width(summary.worst_pl),
        _pl_visible_width(total_financing),
    )
    typer.echo(f"  Avg P/L:    {_color_pl_padded(summary.avg_pl, summary_pl_w)}")
    typer.echo(f"  Total P/L:  {_color_pl_padded(summary.total_pl, summary_pl_w)}")
    typer.echo(f"  Financing:  {_color_pl_padded(total_financing, summary_pl_w)}")
    typer.echo(f"  Best:       {_color_pl_padded(summary.best_pl, summary_pl_w)}")
    typer.echo(f"  Worst:      {_color_pl_padded(summary.worst_pl, summary_pl_w)}")

    # "By direction" — overall LONG vs SHORT side-by-side.  Helps spot a
    # systemic bias (e.g. only the long side is profitable).
    by_dir = pl_by_direction(trades)
    if by_dir:
        typer.echo("")
        typer.echo("By direction")
        typer.echo("─" * 50)
        # Pre-compute max P/L widths so total and avg columns align across rows.
        dir_total_w = max(_pl_visible_width(s.total_pl) for s in by_dir)
        dir_avg_w = max(_pl_visible_width(s.avg_pl) for s in by_dir)
        for stats in by_dir:
            typer.echo(
                _format_direction_row(stats, total_w=dir_total_w, avg_w=dir_avg_w)
            )

    by_instr = pl_by_instrument(trades)
    if by_instr:
        typer.echo("")
        typer.echo("By instrument")
        typer.echo("─" * 50)
        iw = max(len(r[0]) for r in by_instr)
        # Pre-compute max P/L widths so total and avg columns align across rows.
        instr_total_w = max(_pl_visible_width(r[2]) for r in by_instr)
        instr_avg_w = max(_pl_visible_width(r[3]) for r in by_instr)
        for instr, count, total, avg in by_instr:
            typer.echo(
                f"  {instr:<{iw}}  {count:>4}  {_color_pl_padded(total, instr_total_w)}"
                f"  avg {_color_pl_padded(avg, instr_avg_w)}"
            )

    # "By instrument & direction" — surfaces (pair, side) edges that the
    # plain instrument view averages away.  Emitted only when at least one
    # instrument has trades on both sides, otherwise it duplicates the
    # plain instrument view above.
    by_instr_dir = pl_by_instrument_direction(trades)
    if by_instr_dir and _has_both_sides(by_instr_dir):
        typer.echo("")
        typer.echo("By instrument & direction")
        typer.echo("─" * 50)
        # Pad instrument column to the widest instrument name for alignment.
        iw = max(len(instr) for instr, _ in by_instr_dir)
        # Pre-compute max P/L widths so total and avg columns align across rows.
        id_total_w = max(_pl_visible_width(s.total_pl) for _, s in by_instr_dir)
        id_avg_w = max(_pl_visible_width(s.avg_pl) for _, s in by_instr_dir)
        for instr_name, stats in by_instr_dir:
            typer.echo(
                f"  {instr_name:<{iw}}  "
                f"{_format_direction_row(stats, total_w=id_total_w, avg_w=id_avg_w, indent=False)}"
            )

    # Fixed UTC+10 (AEST) aligns day boundaries with the Sydney market open,
    # which marks the start of each Forex trading day.  A fixed offset rather
    # than zoneinfo.ZoneInfo("Australia/Sydney") ensures DST never shifts the
    # bucket boundaries.
    _AEST = timezone(timedelta(hours=10))
    # Local timezone for the hour table — matches the trader's wall-clock.
    _local_tz = datetime.now().astimezone().tzinfo

    by_day = pl_by_weekday(trades, tz=_AEST)
    by_day_opened = pl_by_weekday(trades, tz=_AEST, use_open_time=True)
    if by_day or by_day_opened:
        typer.echo("")
        typer.echo("By weekday (AEST)")
        typer.echo("─" * 50)
        # Pre-compute max P/L widths for each column so cells are fixed-width;
        # this keeps the "opened" column aligned even when "closed" has no data.
        day_close_pl_w = max((_pl_visible_width(t) for _, _, t in by_day), default=10)
        day_open_pl_w = max(
            (_pl_visible_width(t) for _, _, t in by_day_opened), default=10
        )
        day_closed_cell_w = 6 + day_close_pl_w  # 4-char count + 2-char gap + P/L
        day_opened_cell_w = 6 + day_open_pl_w
        # Sub-header right-aligned over each column group.
        typer.echo(
            f"  {'':3}  {'closed':>{day_closed_cell_w}}    {'opened':>{day_opened_cell_w}}"
        )
        # Build lookup maps keyed by day name for O(1) access in the loop.
        close_day: dict[str, tuple[int, Decimal]] = {n: (c, t) for n, c, t in by_day}
        open_day: dict[str, tuple[int, Decimal]] = {
            n: (c, t) for n, c, t in by_day_opened
        }
        # Forex trades Mon-Fri only.  Late-Friday UTC fills can map to
        # Saturday AEST (NY close ≈ 07:00 AEST Sat), so we cap at Friday
        # to prevent ghost weekend rows appearing in the opened column.
        _WEEKDAY_NAMES = ("Mon", "Tue", "Wed", "Thu", "Fri")
        for day in _WEEKDAY_NAMES:
            c = close_day.get(day)
            o = open_day.get(day)
            if c is None and o is None:
                continue
            typer.echo(
                f"  {day}  {_fmt_time_bucket(c, day_close_pl_w)}"
                f"    {_fmt_time_bucket(o, day_open_pl_w)}"
            )

    by_hour = pl_by_hour(trades, tz=_local_tz)
    by_hour_opened = pl_by_hour(trades, tz=_local_tz, use_open_time=True)
    if by_hour or by_hour_opened:
        typer.echo("")
        typer.echo("By hour (local)")
        typer.echo("─" * 50)
        # Pre-compute max P/L widths for each column so cells are fixed-width;
        # this keeps the "opened" column aligned even when "closed" has no data.
        hr_close_pl_w = max((_pl_visible_width(t) for _, _, t in by_hour), default=10)
        hr_open_pl_w = max(
            (_pl_visible_width(t) for _, _, t in by_hour_opened), default=10
        )
        hr_closed_cell_w = 6 + hr_close_pl_w  # 4-char count + 2-char gap + P/L
        hr_opened_cell_w = 6 + hr_open_pl_w
        # Sub-header right-aligned over each column group.
        typer.echo(
            f"  {'':5}  {'closed':>{hr_closed_cell_w}}    {'opened':>{hr_opened_cell_w}}"
        )
        # Build lookup maps keyed by hour (0-23).
        close_hr: dict[int, tuple[int, Decimal]] = {h: (c, t) for h, c, t in by_hour}
        open_hr: dict[int, tuple[int, Decimal]] = {
            h: (c, t) for h, c, t in by_hour_opened
        }
        # Iterate hours in order; emit rows only for hours with data in either column.
        for hour in range(24):
            c = close_hr.get(hour)
            o = open_hr.get(hour)
            if c is None and o is None:
                continue
            typer.echo(
                f"  {hour:02d}:00  {_fmt_time_bucket(c, hr_close_pl_w)}"
                f"    {_fmt_time_bucket(o, hr_open_pl_w)}"
            )

    if tag_pl:
        by_tag: list[tuple[str, int, Decimal]] = []
        for t, pls in tag_pl.items():
            by_tag.append((t, len(pls), sum(pls, Decimal(0))))
        by_tag.sort(key=lambda r: r[2], reverse=True)
        typer.echo("")
        typer.echo("By tag")
        typer.echo("─" * 50)
        tw = max(len(r[0]) for r in by_tag)
        tag_pl_w = max(_pl_visible_width(total) for _, _, total in by_tag)
        for t, count, total in by_tag:
            typer.echo(f"  {t:<{tw}}  {count:>4}  {_color_pl_padded(total, tag_pl_w)}")

    if financing_by_instrument:
        by_financing: list[tuple[str, int, Decimal]] = []
        for instr, amounts in financing_by_instrument.items():
            by_financing.append((instr, len(amounts), sum(amounts, Decimal(0))))
        by_financing.sort(key=lambda r: r[2], reverse=True)
        typer.echo("")
        typer.echo("Financing by instrument")
        typer.echo("─" * 50)
        fw = max(len(r[0]) for r in by_financing)
        financing_w = max(_pl_visible_width(total) for _, _, total in by_financing)
        for instr, count, total in by_financing:
            typer.echo(
                f"  {instr:<{fw}}  {count:>4}  {_color_pl_padded(total, financing_w)}"
            )


def _format_direction_row(
    stats: DirectionStats,
    total_w: int = 0,
    avg_w: int = 0,
    indent: bool = True,
) -> str:
    """Render one DirectionStats as a single fixed-width line.

    Used both by the "By direction" and "By instrument & direction" tables.
    *total_w* and *avg_w* are the max visible widths of P/L values across all
    rows in the table; non-zero values pad each cell so columns align.
    *indent* controls the leading two-space gutter (the instrument-prefixed
    variant supplies its own indent so passes False).
    """
    # Direction label padded to "SHORT" width; count right-aligned in 4 chars.
    prefix = "  " if indent else ""
    return (
        f"{prefix}{stats.direction:<5}  {stats.count:>4}"
        f"  win {stats.win_rate * 100:5.1f}%"
        f"  total {_color_pl_padded(stats.total_pl, total_w)}"
        f"  avg {_color_pl_padded(stats.avg_pl, avg_w)}"
    )


def _has_both_sides(rows: list[tuple[str, DirectionStats]]) -> bool:
    """Return True if any instrument in *rows* has both LONG and SHORT trades.

    Suppresses the "By instrument & direction" section when every pair is
    one-sided, since in that case it would just restate "By instrument".
    """
    seen: dict[str, set[str]] = {}
    for instrument, stats in rows:
        seen.setdefault(instrument, set()).add(stats.direction)
    return any(len(sides) > 1 for sides in seen.values())


def _fmt_time_bucket(data: tuple[int, Decimal] | None, pl_width: int = 0) -> str:
    """Return a formatted (count, total_pl) cell for the combined weekday/hour tables.

    When *pl_width* > 0, the P/L value is padded to that visible width so that
    the column following this cell starts at a consistent horizontal position.
    When *data* is None, a dash placeholder padded to the same total width is
    returned so both column groups stay aligned even when one side has no trades.
    """
    if data is None:
        # "   —" fills the 4-char count field; trailing spaces fill the
        # 2-char gap plus P/L field so the next column lands correctly.
        return "   —" + " " * (2 + pl_width)
    count, total = data
    return f"{count:>4}  {_color_pl_padded(total, pl_width)}"
