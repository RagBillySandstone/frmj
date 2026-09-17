"""``frmj export`` — export transactions to CSV or JSON for external analysis."""

from __future__ import annotations

import csv as _csv
import io
import json
import sqlite3
from decimal import Decimal

import typer

from frmj.app import get_db
from frmj.cli import app
from frmj.cli._completion import _complete_instrument, _complete_txn_type

# ---------------------------------------------------------------------------
# export command
# ---------------------------------------------------------------------------

# Ordered columns for both CSV and JSON export.
_EXPORT_FIELDS = (
    "oanda_id",
    "account_id",
    "type",
    "time",
    "instrument",
    "units",
    "direction",
    "pl",
    "price",
)


def _complete_export_format(incomplete: str) -> list[str]:
    return [f for f in ("csv", "json") if f.startswith(incomplete.lower())]


@app.command()
def export(
    fmt: str = typer.Option(
        "csv",
        "--format",
        "-f",
        help="Output format: csv or json.",
        autocompletion=_complete_export_format,
    ),
    output: str | None = typer.Option(
        None,
        "--output",
        "-o",
        help="Write to this file path instead of stdout.",
        show_default=False,
    ),
    instrument: str | None = typer.Option(
        None,
        "--instrument",
        "-i",
        help="Filter to one instrument, e.g. EUR_USD.",
        autocompletion=_complete_instrument,
        show_default=False,
    ),
    txn_type: str | None = typer.Option(
        None,
        "--type",
        "-t",
        autocompletion=_complete_txn_type,
        help="Filter by transaction type, e.g. ORDER_FILL.",
        show_default=False,
    ),
    since: str | None = typer.Option(
        None,
        "--since",
        "-s",
        help="Export transactions on or after this date, e.g. 2026-04-01.",
        show_default=False,
    ),
    include_notes: bool = typer.Option(
        False,
        "--include-notes",
        "-n",
        help="Join notes as an extra column in the export.",
    ),
) -> None:
    """Export transactions to CSV or JSON for external analysis."""
    if fmt not in ("csv", "json"):
        typer.echo("Error: --format must be 'csv' or 'json'.", err=True)
        raise typer.Exit(1)

    conn = get_db()
    try:
        where: list[str] = []
        params: list[object] = []

        if txn_type:
            where.append("type = ?")
            params.append(txn_type)
        if since:
            where.append("time >= ?")
            params.append(since)
        if instrument:
            where.append("json_extract(raw_json, '$.instrument') = ?")
            params.append(instrument.upper())

        where_sql = ("WHERE " + " AND ".join(where)) if where else ""

        txns = conn.execute(
            f"""
            SELECT id, oanda_id, account_id, type, time, raw_json
            FROM transactions
            {where_sql}
            ORDER BY time ASC
            """,
            params,
        ).fetchall()

        notes_by_id: dict[int, list[str]] = {}
        if include_notes and txns:
            txn_ids = [t["id"] for t in txns]
            placeholders = ",".join("?" * len(txn_ids))
            for nr in conn.execute(
                f"SELECT transaction_id, body FROM notes "
                f"WHERE transaction_id IN ({placeholders}) ORDER BY id",
                txn_ids,
            ).fetchall():
                notes_by_id.setdefault(nr["transaction_id"], []).append(nr["body"])
    finally:
        conn.close()

    records = [_make_export_record(t, notes_by_id, include_notes) for t in txns]

    if fmt == "csv":
        content = _records_to_csv(records, include_notes)
    else:
        content = _records_to_json(records)

    if output:
        with open(output, "w", encoding="utf-8", newline="") as fh:
            fh.write(content)
        typer.echo(f"Exported {len(records)} rows to {output}")
    else:
        typer.echo(content, nl=False)


def _make_export_record(
    txn: sqlite3.Row,
    notes_by_id: dict[int, list[str]],
    include_notes: bool,
) -> dict:
    """Build one export record dict from a transactions row."""
    rec: dict = {
        "oanda_id": txn["oanda_id"],
        "account_id": txn["account_id"],
        "type": txn["type"],
        "time": txn["time"],
        "instrument": "",
        "units": None,
        "direction": "",
        "pl": None,
        "price": "",
    }
    try:
        data = json.loads(txn["raw_json"])
        if txn["type"] == "ORDER_FILL":
            units_raw = int(Decimal(data.get("units", "0")))
            rec["instrument"] = data.get("instrument", "")
            rec["units"] = abs(units_raw)
            rec["direction"] = "LONG" if units_raw >= 0 else "SHORT"
            rec["pl"] = data.get("pl", "") or ""
            rec["price"] = data.get("price", "") or ""
        elif txn["type"] == "DAILY_FINANCING":
            rec["instrument"] = data.get("instrument", "")
            rec["pl"] = data.get("amount") or data.get("financing") or ""
    except Exception:
        pass

    if include_notes:
        rec["notes"] = "; ".join(notes_by_id.get(txn["id"], []))

    return rec


def _records_to_csv(records: list[dict], include_notes: bool) -> str:
    """Serialise export records to CSV text."""
    fields = list(_EXPORT_FIELDS)
    if include_notes:
        fields.append("notes")

    buf = io.StringIO()
    writer = _csv.DictWriter(
        buf, fieldnames=fields, extrasaction="ignore", lineterminator="\n"
    )
    writer.writeheader()
    for rec in records:
        writer.writerow({k: ("" if rec.get(k) is None else rec[k]) for k in fields})
    return buf.getvalue()


def _records_to_json(records: list[dict]) -> str:
    """Serialise export records to JSON text (array of objects)."""
    return json.dumps(records, indent=2, default=str) + "\n"
