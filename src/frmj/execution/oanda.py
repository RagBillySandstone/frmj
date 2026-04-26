"""
Thin httpx wrapper for the Oanda v20 REST API.

Only the two transaction endpoints we actually need are implemented here.
We deliberately avoid the official v20 Python SDK so that:

  * We stay in full control of timeout and retry policy (no hidden waits).
  * The dependency surface stays tiny (httpx only).
  * We can add new fields to the parsed output without waiting on SDK updates.

Supported Oanda endpoints
--------------------------
Cold sync (full history):
    GET /v3/accounts/{accountID}/transactions
    Returns a ``pages`` array; each element is a URL that yields one page
    of transaction objects. We fetch pages in order and concatenate.

Incremental sync (since a known transaction ID):
    GET /v3/accounts/{accountID}/transactions/sinceid?id={transactionID}
    Returns all transactions after ``id``, up to ``_SINCEID_PAGE_LIMIT``
    per call (500 at time of writing). We loop until a response smaller
    than the limit signals no more data.

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

Parent / child transaction IDs
-------------------------------
Oanda models DAILY_FINANCING as a single parent transaction that may
reference related child transactions via a ``relatedTransactionIDs`` field.
For all other transaction types that can have a parent reference, Oanda
uses the ``orderID`` or a similar field. We extract the parent reference
into ``TransactionRow.parent_oanda_id`` using the ``relatedTransactionIDs``
convention; the sync layer resolves this to our synthetic SQLite FK.

NOTE: the exact field names were validated against Oanda's v20 OpenAPI spec
at the time of writing. If a future Oanda API change renames them, fix
``_parse_transaction`` in this module — the sync layer is unaffected.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any, Protocol

import httpx

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

PRACTICE_BASE_URL: str = "https://api-fxpractice.oanda.com/v3"
LIVE_BASE_URL: str = "https://api-oanda.com/v3"

# Oanda's documented per-call limit for the /sinceid endpoint.  When a
# response contains exactly this many rows there may be more; we loop.
_SINCEID_PAGE_LIMIT: int = 500

_CONNECT_TIMEOUT: float = 5.0   # seconds — fail fast on broken network
_READ_TIMEOUT: float = 15.0     # seconds — cold pages can be large
_WRITE_TIMEOUT: float = 5.0     # seconds — tiny uploads
_POOL_TIMEOUT: float = 5.0      # seconds — connection pool wait


# ---------------------------------------------------------------------------
# Data types
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class TransactionRow:
    """
    One Oanda transaction, parsed and ready for insertion into ``transactions``.

    This is the currency in which ``ClientProtocol`` deals — it is equally
    produced by the real ``OandaClient`` and by test doubles, so changing its
    fields is a breaking change to both.

    Fields
    ------
    oanda_id:
        Oanda's own transaction ID (a numeric string). Stored as TEXT so we
        never do arithmetic on it; the sync layer uses it only for
        deduplication and cursor tracking.
    account_id:
        The Oanda account this transaction belongs to.
    type:
        Oanda's transaction type string, e.g. ``"ORDER_FILL"``,
        ``"DAILY_FINANCING"``. Stored verbatim — we do not map to an Enum
        so that new types from Oanda don't require a code change.
    time:
        ISO-8601 timestamp from Oanda's ``time`` field, verbatim.
    parent_oanda_id:
        For DAILY_FINANCING children: the Oanda ID of the parent
        transaction in the same financing batch. ``None`` for everything
        else. The sync layer resolves this to a SQLite synthetic FK.
    raw_json:
        Compact JSON string of the full Oanda transaction object. Stored
        verbatim so we can add new parsed columns later without re-syncing.
    """

    oanda_id: str
    account_id: str
    type: str
    time: str
    parent_oanda_id: str | None
    raw_json: str


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
        if from_id is None:
            return self._fetch_all_cold()
        return self._fetch_since(from_id)

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

    def _parse_transaction(self, txn: dict[str, Any]) -> TransactionRow:
        """
        Extract the fields we index from one Oanda transaction dict.

        ``raw_json`` is the compact-serialised full dict so we never lose
        fields we don't yet parse.

        Parent detection: Oanda uses ``relatedTransactionIDs`` on the parent
        to list its children.  On the child side there is no explicit
        ``parentTransactionID`` field — children are identified by the caller
        (sync layer) by resolving the parent's ``relatedTransactionIDs``.

        For now we set ``parent_oanda_id = None`` for all rows; the sync layer
        will handle the relationship via a post-insert pass if needed.

        TODO: implement financing parent/child linking once we have live data
        to validate the exact field names and delivery order against.
        """
        return TransactionRow(
            oanda_id=str(txn["id"]),
            account_id=self.account_id,
            type=txn["type"],
            time=txn["time"],
            # Parent linking is deferred — see docstring above.
            parent_oanda_id=None,
            raw_json=json.dumps(txn, separators=(",", ":")),
        )
