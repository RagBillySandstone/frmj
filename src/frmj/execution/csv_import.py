"""
Parser for Oanda Hub's transaction-history CSV export.

Why this exists
----------------
Oanda's REST ``/transactions`` endpoints only return history the API still
retains; old accounts can have that window truncated server-side. The Hub
UI's CSV export (Reports -> Transaction History -> Export to csv) covers
whatever date range you ask for regardless, so it gives a way to back-fill
history the API can no longer return, and a way to cross-check what an API
sync already ingested.

CSV shape (observed 2026-09, with the export dialog's Timezone set to UTC)
----------------------------------------------------------------------------
Columns: TICKET, TRANSACTION DATE, TRANSACTION TYPE, DETAILS, INSTRUMENT,
PRICE, UNITS, DIRECTION, ESTIMATED SPREAD COST, STOP LOSS, TAKE PROFIT,
TRAILING STOP, FINANCING, FUNDING RATE, COMMISSION, CONVERSION RATE,
CONVERSION FEE, PL, AMOUNT, BALANCE.

TICKET is ``"{oanda_transaction_id}-0"`` for every real row; we strip the
``-0`` suffix to recover Oanda's own transaction ID — the same ID space the
REST API uses. That is what makes deduplication against a later API sync
work via the existing ``(account_id, oanda_id)`` unique index.

DAILY_FINANCING is special: one ticketed "parent" row (INSTRUMENT blank,
FINANCING = the day's account-wide total) is immediately followed by one
ticket-less "child" row per instrument that had an open position that day
(DETAILS = "Trade ID: <id>", INSTRUMENT set, FINANCING = that instrument's
amount). The child rows have no ID of their own, so we fold them into the
parent's ``positionFinancings`` array instead of inserting separate DB rows
— the same field the API sync's DAILY_FINANCING transaction carries, just
assembled from adjacent CSV rows instead of arriving pre-assembled in one
JSON payload.

INSTRUMENT uses slash notation ("EUR/USD"); we normalise to Oanda's
REST-style underscore notation ("EUR_USD") so it matches every other
instrument string in the codebase (filters, tags, display).

UNITS is unsigned; DIRECTION ("Buy"/"Sell") carries the sign, matching the
REST API's convention where a transaction's ``units`` is positive when it
bought and negative when it sold — the same convention applies whether the
transaction opened or closed a position.

Known gap: unlike the REST API's ORDER_FILL transactions, the CSV never
states which trade a closing fill closed (no ``tradesClosed`` /
``tradeReduced`` equivalent). CSV-imported closing fills therefore carry no
such link; ``frmj stats``'s open-time lookup already tolerates a missing
link (its LEFT JOIN degrades to a NULL open_time), so this is a display
gap for CSV-imported rows, not a crash.
"""

from __future__ import annotations

import csv
import io
import json
from decimal import Decimal
from pathlib import Path
from typing import Any

from frmj.execution.oanda import TransactionRow

# Columns the parser reads directly. Oanda changing its export format would
# show up as a missing key here — we'd rather fail loudly at the top than
# silently mis-map columns.
_REQUIRED_COLUMNS: frozenset[str] = frozenset(
    {
        "TICKET",
        "TRANSACTION DATE",
        "TRANSACTION TYPE",
        "DETAILS",
        "INSTRUMENT",
        "PRICE",
        "UNITS",
        "DIRECTION",
        "FINANCING",
        "PL",
        "BALANCE",
    }
)


def _parse_time(raw: str) -> str:
    """ "2026-02-03 05:13:59 UTC" -> "2026-02-03T05:13:59.000000Z".

    Raises ``ValueError`` if the export wasn't generated with the Timezone
    dialog set to UTC — any other offset can't be losslessly converted here
    without pulling in a full timezone database for a one-time import.
    """
    suffix = " UTC"
    if not raw.endswith(suffix):
        raise ValueError(
            "Expected a UTC timestamp — re-export from the Hub with "
            f"Timezone = UTC: {raw!r}"
        )
    date_part, time_part = raw[: -len(suffix)].split(" ")
    return f"{date_part}T{time_part}.000000Z"


def _normalize_instrument(raw: str) -> str:
    """ "EUR/USD" -> "EUR_USD", matching Oanda's REST instrument naming."""
    return raw.replace("/", "_")


def _ticket_to_oanda_id(ticket: str) -> str:
    """Strip the constant "-0" suffix the CSV export appends to every ticket."""
    head, sep, tail = ticket.rpartition("-")
    if not sep or tail != "0" or not head:
        raise ValueError(f"Unexpected ticket format: {ticket!r}")
    return head


def _signed_units(units: str, direction: str) -> str | None:
    """Apply DIRECTION's sign to CSV's unsigned UNITS.

    Matches the REST API's convention (positive = bought, negative = sold
    on this transaction). Returns ``None`` when the row has no unit count
    (order-management rows like TAKE_PROFIT_ORDER/REPLACEMENT).
    """
    if not units:
        return None
    magnitude = Decimal(units)
    if direction == "Sell":
        magnitude = -magnitude
    elif direction != "Buy":
        raise ValueError(f"Unexpected direction for units {units!r}: {direction!r}")
    return str(magnitude)


def _build_fields(row: dict[str, str], oanda_id: str, iso_time: str) -> dict[str, Any]:
    """Assemble the subset of Oanda's REST transaction shape the rest of the
    app reads from ``raw_json`` (instrument, units, price, pl, financing,
    reason). Empty CSV cells are omitted rather than written as "", matching
    how the REST API omits fields that don't apply to a given transaction
    type.
    """
    fields: dict[str, Any] = {
        "id": oanda_id,
        "time": iso_time,
        "type": row["TRANSACTION TYPE"],
    }
    if row["DETAILS"]:
        fields["reason"] = row["DETAILS"]
    if row["INSTRUMENT"]:
        fields["instrument"] = _normalize_instrument(row["INSTRUMENT"])
    signed_units = _signed_units(row["UNITS"], row["DIRECTION"])
    if signed_units is not None:
        fields["units"] = signed_units
    if row["PRICE"]:
        fields["price"] = row["PRICE"]
    if row["PL"]:
        fields["pl"] = row["PL"]
    if row["FINANCING"]:
        fields["financing"] = row["FINANCING"]
    if row["BALANCE"]:
        fields["accountBalance"] = row["BALANCE"]
    return fields


def parse_csv(text: str, account_id: str) -> list[TransactionRow]:
    """
    Parse the full text of an Oanda Hub transaction-history CSV export.

    Returns rows in file order (chronological), ready to hand to
    ``sync._ingest_rows`` — every DAILY_FINANCING breakdown line has already
    been folded into its parent's ``positionFinancings`` array, so every
    returned row has a real ``oanda_id`` and ``parent_oanda_id`` is always
    ``None`` (CSV-imported rows never need the FK linking the REST sync
    uses for financing children).
    """
    reader = csv.DictReader(io.StringIO(text))
    fieldnames = set(reader.fieldnames or [])
    missing = _REQUIRED_COLUMNS - fieldnames
    if missing:
        raise ValueError(
            f"CSV is missing expected columns {sorted(missing)} — "
            "Oanda's export format may have changed."
        )

    out: list[TransactionRow] = []
    financing_parent: dict[str, Any] | None = None
    financing_children: list[dict[str, Any]] = []

    def flush_financing() -> None:
        nonlocal financing_parent, financing_children
        if financing_parent is not None:
            financing_parent["positionFinancings"] = financing_children
            out.append(
                TransactionRow(
                    oanda_id=financing_parent["id"],
                    account_id=account_id,
                    type="DAILY_FINANCING",
                    time=financing_parent["time"],
                    parent_oanda_id=None,
                    raw_json=json.dumps(financing_parent),
                )
            )
        financing_parent = None
        financing_children = []

    for row in reader:
        txn_type = row["TRANSACTION TYPE"]
        ticket = row["TICKET"]

        if txn_type == "DAILY_FINANCING" and not ticket:
            # Ticket-less per-instrument breakdown row for the most
            # recently seen ticketed DAILY_FINANCING row.
            if financing_parent is None:
                raise ValueError(
                    "DAILY_FINANCING breakdown row with no preceding parent row"
                )
            financing_children.append(
                {
                    "instrument": _normalize_instrument(row["INSTRUMENT"]),
                    "financing": row["FINANCING"],
                    "tradeID": row["DETAILS"].removeprefix("Trade ID: "),
                }
            )
            continue

        flush_financing()

        oanda_id = _ticket_to_oanda_id(ticket)
        iso_time = _parse_time(row["TRANSACTION DATE"])
        fields = _build_fields(row, oanda_id, iso_time)

        if txn_type == "DAILY_FINANCING":
            financing_parent = fields
            continue

        out.append(
            TransactionRow(
                oanda_id=oanda_id,
                account_id=account_id,
                type=txn_type,
                time=iso_time,
                parent_oanda_id=None,
                raw_json=json.dumps(fields),
            )
        )

    flush_financing()
    return out


def load_csv_file(path: Path, account_id: str) -> list[TransactionRow]:
    """Read and parse an Oanda Hub transaction-history CSV export from disk.

    ``utf-8-sig`` strips the BOM the Hub export prepends to the file.
    """
    return parse_csv(path.read_text(encoding="utf-8-sig"), account_id)
