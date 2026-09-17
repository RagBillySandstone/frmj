"""Oanda v20 REST API integration.

This package is split into:

  models.py   Dataclasses shared between the client and its parsing helpers
              (``TransactionRow``, ``OpenTrade``, ``AccountSummary``, ...).
  parsing.py  Pure functions that turn raw Oanda API dicts into the above
              dataclasses — tested directly, without HTTP.
  client.py   ``OandaClient``, the httpx-based client, plus ``ClientProtocol``,
              the structural interface ``sync.py`` accepts.

Everything below is re-exported here so existing call sites can keep writing
``from frmj.execution.oanda import OandaClient`` etc. without caring which
submodule actually defines it.

Parent / child transaction IDs
-------------------------------
Oanda models DAILY_FINANCING as a single parent transaction that lists its
per-instrument children via ``relatedTransactionIDs``.  Children have no
back-reference to their parent.

``get_transactions_since`` applies ``_resolve_financing_parents`` to the
collected batch before returning it, stamping ``parent_oanda_id`` on every
child row.  The sync layer then resolves those IDs to the synthetic SQLite
FK.  Rows from a prior sync run are handled by the sync layer's
``_resolve_parent_id`` DB lookup — cross-batch links work correctly because
the parent is already in the database when the children arrive.
"""

from __future__ import annotations

from .client import (
    LIVE_BASE_URL,
    PRACTICE_BASE_URL,
    ClientProtocol,
    OandaClient,
)
from .models import (
    AccountSummary,
    CloseFill,
    FinancingRate,
    OpenTrade,
    OrderFill,
    TransactionRow,
)
from .parsing import (
    _compute_conversion_rate,
    _extract_bid_ask,
    _parse_account_summary,
    _parse_close_fill,
    _parse_financing_rate,
    _parse_instrument_spec,
    _parse_open_trade,
    _parse_order_create_txn_id,
    _resolve_financing_parents,
)

__all__ = [
    "PRACTICE_BASE_URL",
    "LIVE_BASE_URL",
    "ClientProtocol",
    "OandaClient",
    "TransactionRow",
    "OpenTrade",
    "AccountSummary",
    "FinancingRate",
    "OrderFill",
    "CloseFill",
    "_compute_conversion_rate",
    "_extract_bid_ask",
    "_parse_account_summary",
    "_parse_close_fill",
    "_parse_financing_rate",
    "_parse_instrument_spec",
    "_parse_open_trade",
    "_parse_order_create_txn_id",
    "_resolve_financing_parents",
]
