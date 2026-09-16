# FRoMaJ — TODO

Items are grouped by priority. Within each group, order reflects logical build sequence.

---

## P1 — Monitoring

### 1. Oanda Streaming API for transactions

`GET /v3/accounts/{id}/transactions/stream` opens a persistent HTTP connection; Oanda pushes transaction events as they happen. This eliminates polling lag and API quota waste from `sync --watch`. Should be an optional upgrade to `--watch` mode, falling back to polling if the stream drops. Requires keeping the `httpx` client alive as a streaming context.

---

## P2 — UX and extensibility

### 2. Per-command account override

The `frmj trade`, `sync`, `positions`, and `close` commands always use the
active account. A `--account NAME` flag would allow targeting a specific
profile without switching the global active account. Tracked as a future
enhancement to the `get_client()` callsite.

### 4. `frmj note --edit` — amend an existing note

Currently `frmj note` only appends. For cases where a note has a typo or needs updating, add an `--edit` flag that opens the most recent note on the given transaction in `$EDITOR` (or prompts inline if the env var is unset).

### 5. `frmj trade --limit` — place a limit (pending) order

Add a `--limit` flag to `frmj trade`. When set, the dialog inserts a
limit-entry prompt after displaying the current market price. The intended
input model is a pip offset from the current ask/bid (e.g. `15` → 15 pips
below ask for a long), mirroring the existing TP/SL pip format so the user
never has to type an absolute price. The limit price and TP/SL targets are
then displayed before confirmation. Oanda requires limit orders to embed
`takeProfitOnFill`/`stopLossOnFill` in the order body rather than attaching
them after fill; the API layer needs a `place_limit_order()` method alongside
the existing `place_market_order()`. Exact UX and validation rules TBD pending
architectural discussion.

### 10. Extend mypy coverage to tests/

`mypy` currently only type-checks `src/frmj` (see pyproject.toml `[tool.mypy]`). Running it over `tests/` today surfaces ~200 errors, mostly from two sources: helper methods like `TestCloseCommand._invoke()` annotated `-> object` instead of `click.testing.Result`, and fake objects (`SimpleNamespace` standing in for `typer.Context`, hand-rolled fakes standing in for `sqlite3.Connection`) that satisfy call sites structurally but not nominally. Fixing the return-type annotations is mechanical; the fakes would need `Protocol` types to type-check cleanly without abandoning the structural-typing test style described in the Development section of README.md.

### 11. CSV-imported closing fills have no open-time link

Unlike the REST API's ORDER_FILL transactions, the Oanda Hub CSV export never states which trade a closing fill closed (no `tradesClosed`/`tradeReduced` equivalent — see `execution/csv_import.py`'s module docstring). `frmj stats`'s open-time lookup already degrades gracefully to a NULL open_time for these rows rather than crashing, but any CSV-imported closed trade will always show a blank open time and can't be bucketed by holding duration. No known fix without a second data source (e.g. cross-referencing the CSV's own MARKET_ORDER open rows by instrument/time proximity), so this is tracked rather than blocking.
