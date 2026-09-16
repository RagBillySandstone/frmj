"""
Tests for the Oanda Hub CSV importer.

These test the pure ``parse_csv`` function directly with small, synthetic
CSV text — no files, no database — mirroring how ``oanda.py``'s parsing
helpers are tested. ``load_csv_file`` (the thin file-reading wrapper) is not
tested here since it has no logic beyond ``Path.read_text``.
"""

from __future__ import annotations

import json

import pytest

from frmj.execution.csv_import import parse_csv

_HEADER = (
    "TICKET,TRANSACTION DATE,TRANSACTION TYPE,DETAILS,INSTRUMENT,PRICE,"
    "UNITS,DIRECTION,ESTIMATED SPREAD COST,STOP LOSS,TAKE PROFIT,"
    "TRAILING STOP,FINANCING,FUNDING RATE,COMMISSION,CONVERSION RATE,"
    "CONVERSION FEE,PL,AMOUNT,BALANCE"
)


def _csv(*rows: str) -> str:
    """Join the standard header with the given raw data lines."""
    return "\n".join((_HEADER, *rows))


# ---------------------------------------------------------------------------
# Header validation
# ---------------------------------------------------------------------------


class TestHeaderValidation:
    def test_missing_required_column_raises(self) -> None:
        text = "TICKET,TRANSACTION DATE,TRANSACTION TYPE\n1-0,2026-01-01 00:00:00 UTC,MARGIN_CALL_ENTER"
        with pytest.raises(ValueError, match="missing expected columns"):
            parse_csv(text, "acct-1")


# ---------------------------------------------------------------------------
# Plain (non-financing) rows
# ---------------------------------------------------------------------------


class TestPlainRows:
    def test_opening_fill_parses_positive_units_and_zero_pl(self) -> None:
        row = (
            '"2525-0","2026-02-03 05:13:59 UTC","ORDER_FILL","MARKET_ORDER",'
            '"USD/CHF","0.77825","1000000.00","Buy","109.2320","","","",'
            '"0.00000","","0.0000","","0.0000","0.00000","","126763.26"'
        )
        [txn] = parse_csv(_csv(row), "acct-1")
        assert txn.oanda_id == "2525"
        assert txn.account_id == "acct-1"
        assert txn.type == "ORDER_FILL"
        assert txn.time == "2026-02-03T05:13:59.000000Z"
        assert txn.parent_oanda_id is None

        data = json.loads(txn.raw_json)
        assert data["instrument"] == "USD_CHF"
        assert data["units"] == "1000000.00"
        assert data["pl"] == "0.00000"
        assert data["reason"] == "MARKET_ORDER"

    def test_closing_fill_parses_negative_units_and_nonzero_pl(self) -> None:
        row = (
            '"2527-0","2026-02-03 05:15:34 UTC","ORDER_FILL","TAKE_PROFIT_ORDER",'
            '"USD/CHF","0.77836","1000000.00","Sell","96.3478","","","",'
            '"0.00000","","0.0000","1.2782","-0.6938","140.60350","","126903.87"'
        )
        [txn] = parse_csv(_csv(row), "acct-1")
        data = json.loads(txn.raw_json)
        assert data["units"] == "-1000000.00"
        assert data["pl"] == "140.60350"

    def test_row_with_no_units_omits_units_field(self) -> None:
        row = (
            '"2526-0","2026-02-03 05:13:59 UTC","TAKE_PROFIT_ORDER","ON_FILL",'
            '"","0.77835","","","","","","","","","","","","","",""'
        )
        [txn] = parse_csv(_csv(row), "acct-1")
        data = json.loads(txn.raw_json)
        assert "units" not in data
        assert "instrument" not in data

    def test_unknown_transaction_type_passes_through_verbatim(self) -> None:
        """Forward-compatibility: a type we don't special-case still imports."""
        row = (
            '"3074-0","2026-02-17 08:45:00 UTC","MARGIN_CALL_ENTER","",'
            '"","","","","","","","","","","","","","","",""'
        )
        [txn] = parse_csv(_csv(row), "acct-1")
        assert txn.type == "MARGIN_CALL_ENTER"
        assert txn.oanda_id == "3074"

    def test_malformed_ticket_raises(self) -> None:
        row = (
            '"not-a-real-ticket","2026-02-03 05:13:59 UTC","MARKET_ORDER","CLIENT_ORDER",'
            '"","","","","","","","","","","","","","","",""'
        )
        with pytest.raises(ValueError, match="Unexpected ticket format"):
            parse_csv(_csv(row), "acct-1")

    def test_non_utc_timestamp_raises(self) -> None:
        row = (
            '"2524-0","2026-02-02 17:13:59 -12","MARKET_ORDER","CLIENT_ORDER",'
            '"","","","","","","","","","","","","","","",""'
        )
        with pytest.raises(ValueError, match="Expected a UTC timestamp"):
            parse_csv(_csv(row), "acct-1")


# ---------------------------------------------------------------------------
# DAILY_FINANCING parent/child folding
# ---------------------------------------------------------------------------


class TestDailyFinancing:
    def test_parent_and_children_fold_into_one_row(self) -> None:
        parent = (
            '"2609-0","2026-02-03 22:00:00 UTC","DAILY_FINANCING","",'
            '"","","","","","","","","47.24940","","","","-0.2318","","","123375.59"'
        )
        child_1 = (
            '"","2026-02-03 22:00:00 UTC","DAILY_FINANCING","Trade ID: 2589",'
            '"EUR/USD","","","","","","","","45.74700","0.0071","","","","","",""'
        )
        child_2 = (
            '"","2026-02-03 22:00:00 UTC","DAILY_FINANCING","Trade ID: 2607",'
            '"SGD/CHF","","","","","","","","1.50240","0.0007","","","","","",""'
        )
        [txn] = parse_csv(_csv(parent, child_1, child_2), "acct-1")
        assert txn.oanda_id == "2609"
        assert txn.type == "DAILY_FINANCING"

        data = json.loads(txn.raw_json)
        assert data["financing"] == "47.24940"
        assert data["positionFinancings"] == [
            {"instrument": "EUR_USD", "financing": "45.74700", "tradeID": "2589"},
            {"instrument": "SGD_CHF", "financing": "1.50240", "tradeID": "2607"},
        ]

    def test_parent_with_no_children_still_emits_row(self) -> None:
        parent = (
            '"2651-0","2026-02-04 22:00:00 UTC","DAILY_FINANCING","",'
            '"","","","","","","","","0.00000","","","","0","","","124003.67"'
        )
        [txn] = parse_csv(_csv(parent), "acct-1")
        data = json.loads(txn.raw_json)
        assert data["positionFinancings"] == []

    def test_child_before_any_parent_raises(self) -> None:
        child = (
            '"","2026-02-03 22:00:00 UTC","DAILY_FINANCING","Trade ID: 2589",'
            '"EUR/USD","","","","","","","","45.74700","0.0071","","","","","",""'
        )
        with pytest.raises(ValueError, match="no preceding parent"):
            parse_csv(_csv(child), "acct-1")

    def test_non_financing_row_flushes_pending_parent(self) -> None:
        """A parent immediately followed by an unrelated row (no children
        that day) must still be emitted, and the unrelated row parsed too.
        """
        parent = (
            '"2651-0","2026-02-04 22:00:00 UTC","DAILY_FINANCING","",'
            '"","","","","","","","","51.52940","","","","-0.6240","","","124003.67"'
        )
        other = (
            '"2652-0","2026-02-04 22:05:00 UTC","MARGIN_CALL_ENTER","",'
            '"","","","","","","","","","","","","","","",""'
        )
        rows = parse_csv(_csv(parent, other), "acct-1")
        assert [r.oanda_id for r in rows] == ["2651", "2652"]
        assert json.loads(rows[0].raw_json)["positionFinancings"] == []
