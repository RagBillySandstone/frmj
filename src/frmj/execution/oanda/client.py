"""Thin httpx wrapper for the Oanda v20 REST API.

Endpoints implemented
---------------------
  GET  /accounts/{id}/transactions              (cold sync — pages index)
  GET  /accounts/{id}/transactions/sinceid      (incremental sync)
  GET  /accounts/{id}/summary                  (NAV, margin, open trade count)
  GET  /accounts/{id}/instruments              (InstrumentSpec / financing rates)
  GET  /accounts/{id}/pricing                  (live bid/ask + conversions)
  GET  /accounts/{id}/trades                   (open ticket count per instrument)
  POST /accounts/{id}/orders                   (place market order)

We deliberately avoid the official v20 Python SDK so that:

  * We stay in full control of timeout and retry policy (no hidden waits).
  * The dependency surface stays tiny (httpx only).
  * We can add new fields to the parsed output without waiting on SDK updates.

Timeout configuration
---------------------
    connect = 5 s   fail fast if the network / DNS is broken
    read    = 15 s  give Oanda time to assemble a 1000-row cold page
    write   = 5 s   request upload; 5 s is generous for our tiny payloads
    pool    = 5 s   wait for a connection slot in the pool

Authentication
--------------
``Bearer {token}`` in the ``Authorization`` header. The token is supplied
at construction time — we never read the environment here. The CLI layer
reads ``OANDA_API_TOKEN`` and passes it in.

Base URLs
---------
    Practice : https://api-fxpractice.oanda.com/v3
    Live     : https://api-oanda.com/v3

Retry policy
------------
None — deliberately. If a request fails we let the exception propagate to
the CLI, which can offer "retry / save plan / abort" options. Silent
automatic retries inside the client would obscure network problems and make
the "abort" path unreachable.
"""

from __future__ import annotations

import json
from decimal import Decimal
from typing import Any, Protocol

import httpx

from frmj.domain.sizing import InstrumentSpec, PriceQuote

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

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

PRACTICE_BASE_URL: str = "https://api-fxpractice.oanda.com/v3"
LIVE_BASE_URL: str = "https://api-oanda.com/v3"

# Oanda's documented per-call limit for the /sinceid endpoint.  When a
# response contains exactly this many rows there may be more; we loop.
_SINCEID_PAGE_LIMIT: int = 500

_CONNECT_TIMEOUT: float = 5.0  # seconds — fail fast on broken network
_READ_TIMEOUT: float = 15.0  # seconds — cold pages can be large
_WRITE_TIMEOUT: float = 5.0  # seconds — tiny uploads
_POOL_TIMEOUT: float = 5.0  # seconds — connection pool wait


# ---------------------------------------------------------------------------
# Protocol — the abstraction boundary
# ---------------------------------------------------------------------------


class ClientProtocol(Protocol):
    """
    Structural interface for any object that can supply transaction rows.

    The sync functions in ``sync.py`` accept a ``ClientProtocol``, not a
    concrete ``OandaClient``.  Any class that has an ``account_id`` attribute
    and a ``get_transactions_since`` method with this exact signature satisfies
    the Protocol — no inheritance required.

    This lets test doubles be plain dataclasses or simple classes with no
    shared ancestor. It also means adding a new implementation (e.g. a CSV
    importer) requires zero changes to the sync layer.
    """

    account_id: str

    def get_transactions_since(
        self,
        from_id: str | None = None,
    ) -> list[TransactionRow]:
        """
        Return all transactions after *from_id* (exclusive), or all
        transactions ever if *from_id* is ``None``.

        Callers must not assume anything about the order within the returned
        list beyond: if two transactions have a parent/child relationship, the
        parent appears before its children (Oanda guarantees this in its own
        delivery order, and implementations must preserve it).

        On any network or API error, raises rather than returning a partial
        list.  Retry decisions belong to the caller.
        """
        ...


# ---------------------------------------------------------------------------
# Real HTTP client
# ---------------------------------------------------------------------------


class OandaClient:
    """
    HTTP client for the Oanda v3 REST API.

    Instantiate once and either use as a context manager (recommended for CLI
    flows) or call ``.close()`` explicitly when done.

    Usage::

        with OandaClient(token=token, account_id=account_id) as client:
            rows = client.get_transactions_since()
    """

    def __init__(
        self,
        *,
        token: str,
        account_id: str,
        practice: bool = True,
    ) -> None:
        self.account_id = account_id
        self._base_url = PRACTICE_BASE_URL if practice else LIVE_BASE_URL
        self._http = httpx.Client(
            headers={"Authorization": f"Bearer {token}"},
            timeout=httpx.Timeout(
                connect=_CONNECT_TIMEOUT,
                read=_READ_TIMEOUT,
                write=_WRITE_TIMEOUT,
                pool=_POOL_TIMEOUT,
            ),
        )

    # ------------------------------------------------------------------
    # Public interface (satisfies ClientProtocol)
    # ------------------------------------------------------------------

    def get_transactions_since(
        self,
        from_id: str | None = None,
    ) -> list[TransactionRow]:
        """
        Fetch all transactions after *from_id*, handling Oanda pagination.

        When *from_id* is ``None`` (cold sync), fetches the full account
        history via the paginated ``/transactions`` endpoint.

        When *from_id* is provided (incremental sync), uses the faster
        ``/transactions/sinceid`` endpoint, looping until a sub-limit
        response signals no more data.

        Raises ``httpx.HTTPStatusError`` on 4xx / 5xx responses.
        Raises ``httpx.TimeoutException`` if a request exceeds the timeout.
        """
        rows = self._fetch_all_cold() if from_id is None else self._fetch_since(from_id)
        return _resolve_financing_parents(rows)

    def get_account_summary(self) -> AccountSummary:
        """Fetch NAV, available margin, and open trade count.

        Called at the start of the trade flow so the risk model has fresh
        account state. Raises ``httpx.HTTPStatusError`` on Oanda errors.
        """
        resp = self._http.get(f"{self._base_url}/accounts/{self.account_id}/summary")
        resp.raise_for_status()
        return _parse_account_summary(resp.json())

    def get_instrument(self, name: str) -> InstrumentSpec:
        """Fetch metadata for one instrument (pip location, margin rate, etc.).

        Uses GET /accounts/{id}/instruments filtered to a single name so we
        get the account-specific margin rate (which differs between practice
        and live accounts and can vary per-account based on account type).
        """
        resp = self._http.get(
            f"{self._base_url}/accounts/{self.account_id}/instruments",
            params={"instruments": name},
        )
        resp.raise_for_status()
        instruments = resp.json().get("instruments", [])
        if not instruments:
            raise ValueError(f"Instrument {name!r} not found for this account")
        return _parse_instrument_spec(instruments[0])

    def get_financing_rates(self, instruments: list[str]) -> list[FinancingRate]:
        """Fetch long/short financing rates for *instruments* in one request.

        Uses GET /accounts/{id}/instruments with a comma-separated
        ``instruments`` filter, so displaying rates for the whole tradable
        pairs list costs one HTTP call rather than one per pair. If *any*
        instrument in the list isn't tradeable for this account, Oanda
        returns 404 for the whole request rather than filtering it out —
        every name passed in must be valid.
        """
        resp = self._http.get(
            f"{self._base_url}/accounts/{self.account_id}/instruments",
            params={"instruments": ",".join(instruments)},
        )
        resp.raise_for_status()
        return [_parse_financing_rate(i) for i in resp.json().get("instruments", [])]

    def get_price(
        self,
        instrument: str,
        home_currency: str = "USD",
    ) -> PriceQuote:
        """Fetch live bid/ask and compute currency conversion rates.

        Conversion rates (``quote_to_home`` and ``base_to_home``) are what
        the sizing and pricing models need to translate pip moves and margin
        requirements into home-currency (account-currency) amounts.

        Handles three cases:
        1. ``quote == home`` (e.g., EUR_USD on USD account):
           ``quote_to_home = 1``, ``base_to_home = mid``.
        2. ``base == home`` (e.g., USD_JPY on USD account):
           ``base_to_home = 1``, ``quote_to_home = 1 / mid``.
        3. Cross-pair (e.g., EUR_GBP on USD account): fetches one additional
           price per side via ``_currency_to_home`` to resolve both rates.

        The cross-pair path makes extra HTTP calls. For the instruments
        Stephen typically trades (major USD pairs), path 1 or 2 is always
        taken.
        """
        resp = self._http.get(
            f"{self._base_url}/accounts/{self.account_id}/pricing",
            params={"instruments": instrument},
        )
        resp.raise_for_status()
        bid, ask = _extract_bid_ask(resp.json())
        mid = (bid + ask) / 2

        base, quote = instrument.split("_")

        if quote == home_currency:
            # e.g. EUR_USD on USD account: quote IS home, no conversion needed.
            quote_to_home = Decimal("1")
            base_to_home = mid
        elif base == home_currency:
            # e.g. USD_JPY on USD account: base IS home.
            base_to_home = Decimal("1")
            quote_to_home = Decimal("1") / mid
        else:
            # Cross-pair: resolve each currency's home value separately.
            base_to_home = self._currency_to_home(base, home_currency)
            quote_to_home = self._currency_to_home(quote, home_currency)

        return PriceQuote(
            bid=bid,
            ask=ask,
            quote_to_home=quote_to_home,
            base_to_home=base_to_home,
        )

    def get_open_tickets_on_instrument(self, instrument: str) -> int:
        """Count open trade tickets for *instrument* (for the scale-in check).

        Uses GET /accounts/{id}/trades?instrument={name}&state=OPEN.  Each
        element in ``trades`` is one open ticket regardless of units size,
        which is what the risk model's ``open_tickets_on_instrument`` expects.
        """
        resp = self._http.get(
            f"{self._base_url}/accounts/{self.account_id}/trades",
            params={"instrument": instrument, "state": "OPEN"},
        )
        resp.raise_for_status()
        return len(resp.json().get("trades", []))

    def get_open_trades(self) -> list[OpenTrade]:
        """Fetch all currently open trades for the account.

        Uses GET /accounts/{id}/openTrades which returns the full trade detail
        including unrealised P/L, margin used, and any attached TP/SL orders.
        Returns an empty list when there are no open positions.
        Raises ``httpx.HTTPStatusError`` on Oanda errors.
        """
        resp = self._http.get(f"{self._base_url}/accounts/{self.account_id}/openTrades")
        resp.raise_for_status()
        return [_parse_open_trade(t) for t in resp.json().get("trades", [])]

    def close_trade(self, trade_id: str) -> CloseFill:
        """Close an open trade in full.

        Uses PUT /accounts/{id}/trades/{tradeID}/close with no body, which
        always closes the complete position.  Returns the closing fill details.
        Raises ``httpx.HTTPStatusError`` on Oanda errors (e.g. 404 if the
        trade is already closed or the ID is wrong).
        """
        resp = self._http.put(
            f"{self._base_url}/accounts/{self.account_id}/trades/{trade_id}/close"
        )
        resp.raise_for_status()
        return _parse_close_fill(resp.json())

    def place_market_order(
        self,
        instrument: str,
        units_signed: int,
    ) -> OrderFill:
        """Place a market order and return the fill details.

        ``units_signed`` follows Oanda's sign convention: positive = long,
        negative = short.  We use FOK (Fill-Or-Kill) so the order either fills
        immediately at the visible price or is rejected — no partial fills, no
        resting orders left behind.

        Raises:
            RuntimeError: if Oanda does not return an ``orderFillTransaction``
                (i.e., the order was killed, usually due to insufficient
                liquidity — extremely rare for major FX pairs).
            httpx.HTTPStatusError: on 4xx/5xx from the orders endpoint.
        """
        resp = self._http.post(
            f"{self._base_url}/accounts/{self.account_id}/orders",
            json={
                "order": {
                    "type": "MARKET",
                    "instrument": instrument,
                    "units": str(units_signed),
                    "timeInForce": "FOK",
                }
            },
        )
        resp.raise_for_status()
        payload = resp.json()

        if "orderFillTransaction" not in payload:
            # FOK was killed — the order did not execute.
            raise RuntimeError(
                f"Order not filled (instrument={instrument}, "
                f"units={units_signed}). "
                f"Oanda response: {json.dumps(payload)}"
            )

        fill = payload["orderFillTransaction"]
        trade_opened = fill.get("tradeOpened")
        return OrderFill(
            transaction_id=str(fill["id"]),
            fill_price=Decimal(fill["price"]),
            units_filled=int(Decimal(fill["units"])),
            trade_id=str(trade_opened["tradeID"]) if trade_opened else None,
        )

    def attach_take_profit(self, trade_id: str, price: Decimal) -> str:
        """Attach a GTC take-profit order to an existing open trade.

        Returns the ``orderCreateTransaction.id`` from Oanda's response.
        Raises ``httpx.HTTPStatusError`` on 4xx / 5xx responses.
        """
        return self._attach_exit_order("TAKE_PROFIT", trade_id, price)

    def attach_stop_loss(self, trade_id: str, price: Decimal) -> str:
        """Attach a GTC stop-loss order to an existing open trade.

        Returns the ``orderCreateTransaction.id`` from Oanda's response.
        Raises ``httpx.HTTPStatusError`` on 4xx / 5xx responses.
        """
        return self._attach_exit_order("STOP_LOSS", trade_id, price)

    def close(self) -> None:
        """Release the underlying httpx connection pool."""
        self._http.close()

    def __enter__(self) -> OandaClient:
        return self

    def __exit__(self, *_: object) -> None:
        self.close()

    # ------------------------------------------------------------------
    # Private helpers
    # ------------------------------------------------------------------

    def _attach_exit_order(
        self,
        order_type: str,
        trade_id: str,
        price: Decimal,
    ) -> str:
        """POST a TAKE_PROFIT or STOP_LOSS order linked to *trade_id*.

        Both order types share identical request/response shapes; the only
        difference is the ``type`` field.  GTC (good-till-cancelled) is the
        only time-in-force that makes sense for exit orders on an open trade —
        DAY orders would expire at session end, leaving the position unprotected.
        """
        resp = self._http.post(
            f"{self._base_url}/accounts/{self.account_id}/orders",
            json={
                "order": {
                    "type": order_type,
                    "tradeID": trade_id,
                    "price": str(price),
                    "timeInForce": "GTC",
                }
            },
        )
        resp.raise_for_status()
        return _parse_order_create_txn_id(resp.json())

    def _fetch_all_cold(self) -> list[TransactionRow]:
        """
        Cold path: GET /transactions to discover pages, then fetch each page.

        Oanda responds with a ``pages`` array of full URLs.  Each URL returns
        a ``transactions`` array.  We fetch pages in sequence and concatenate
        the results.  Empty accounts return an empty pages list.
        """
        # Step 1: get the pages index.
        resp = self._http.get(
            f"{self._base_url}/accounts/{self.account_id}/transactions"
        )
        resp.raise_for_status()
        pages: list[str] = resp.json().get("pages", [])

        # Step 2: fetch each page in order and collect rows.
        all_rows: list[TransactionRow] = []
        for page_url in pages:
            page_resp = self._http.get(page_url)
            page_resp.raise_for_status()
            for txn in page_resp.json().get("transactions", []):
                all_rows.append(self._parse_transaction(txn))

        return all_rows

    def _fetch_since(self, from_id: str) -> list[TransactionRow]:
        """
        Incremental path: GET /transactions/sinceid, looping until done.

        Oanda returns up to ``_SINCEID_PAGE_LIMIT`` rows per call.  When a
        response contains exactly that many rows, we advance ``from_id`` to
        the last received ID and repeat.  A sub-limit response means no more
        data.
        """
        all_rows: list[TransactionRow] = []
        current_from = from_id

        while True:
            resp = self._http.get(
                f"{self._base_url}/accounts/{self.account_id}/transactions/sinceid",
                params={"id": current_from},
            )
            resp.raise_for_status()
            txns: list[dict[str, Any]] = resp.json().get("transactions", [])

            for txn in txns:
                all_rows.append(self._parse_transaction(txn))

            # Fewer than the per-call limit → no more pages to fetch.
            if len(txns) < _SINCEID_PAGE_LIMIT:
                break

            # Advance the cursor to the last received ID and loop.
            current_from = str(txns[-1]["id"])

        return all_rows

    def _fetch_mid(self, instrument: str) -> Decimal:
        """Fetch the mid price for *instrument* (helper for cross-pair conversions)."""
        resp = self._http.get(
            f"{self._base_url}/accounts/{self.account_id}/pricing",
            params={"instruments": instrument},
        )
        resp.raise_for_status()
        bid, ask = _extract_bid_ask(resp.json())
        return (bid + ask) / 2

    def _currency_to_home(self, currency: str, home: str) -> Decimal:
        """HTTP: fetch mid price(s) and delegate to ``_compute_conversion_rate``.

        Fetches whichever of ``{currency}_{home}`` or ``{home}_{currency}``
        Oanda knows about, then calls the pure conversion helper.  Raises
        ``ValueError`` when neither pair is available on this account.
        """
        if currency == home:
            return Decimal("1")
        direct = f"{currency}_{home}"
        inverted = f"{home}_{currency}"
        mids: dict[str, Decimal] = {}
        try:
            mids[direct] = self._fetch_mid(direct)
        except httpx.HTTPStatusError:
            pass
        if direct not in mids:
            try:
                mids[inverted] = self._fetch_mid(inverted)
            except httpx.HTTPStatusError:
                raise ValueError(
                    f"Cannot determine {currency}/{home} conversion rate: "
                    f"neither {direct} nor {inverted} "
                    f"is a recognised Oanda pair for this account"
                ) from None
        return _compute_conversion_rate(currency, home, mids)

    def _parse_transaction(self, txn: dict[str, Any]) -> TransactionRow:
        """Extract the fields we index from one Oanda transaction dict.

        ``raw_json`` is the compact-serialised full dict so we never lose
        fields we don't yet parse.

        ``parent_oanda_id`` is always ``None`` here; the caller
        (``get_transactions_since``) applies ``_resolve_financing_parents``
        to the full batch afterward to stamp parent links on DAILY_FINANCING
        children.
        """
        return TransactionRow(
            oanda_id=str(txn["id"]),
            account_id=self.account_id,
            type=txn["type"],
            time=txn["time"],
            parent_oanda_id=None,
            raw_json=json.dumps(txn, separators=(",", ":")),
        )
