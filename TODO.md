# FRoMaJ — TODO

Items are grouped by priority. Within each group, order reflects logical build sequence.

---

## P1 — Monitoring

### 1. Oanda Streaming API for transactions

`GET /v3/accounts/{id}/transactions/stream` opens a persistent HTTP connection; Oanda pushes transaction events as they happen. This eliminates polling lag and API quota waste from `sync --watch`. Should be an optional upgrade to `--watch` mode, falling back to polling if the stream drops. Requires keeping the `httpx` client alive as a streaming context.

---

## P2 — UX and extensibility

### 4. `frmj note --edit` — amend an existing note

Currently `frmj note` only appends. For cases where a note has a typo or needs updating, add an `--edit` flag that opens the most recent note on the given transaction in `$EDITOR` (or prompts inline if the env var is unset).

### 10. Extend mypy coverage to tests/

`mypy` currently only type-checks `src/frmj` (see pyproject.toml `[tool.mypy]`). Running it over `tests/` today surfaces ~200 errors, mostly from two sources: helper methods like `TestCloseCommand._invoke()` annotated `-> object` instead of `click.testing.Result`, and fake objects (`SimpleNamespace` standing in for `typer.Context`, hand-rolled fakes standing in for `sqlite3.Connection`) that satisfy call sites structurally but not nominally. Fixing the return-type annotations is mechanical; the fakes would need `Protocol` types to type-check cleanly without abandoning the structural-typing test style described in the Development section of README.md.

### 11. CSV-imported closing fills have no open-time link

Unlike the REST API's ORDER_FILL transactions, the Oanda Hub CSV export never states which trade a closing fill closed (no `tradesClosed`/`tradeReduced` equivalent — see `execution/csv_import.py`'s module docstring). `frmj stats`'s open-time lookup already degrades gracefully to a NULL open_time for these rows rather than crashing, but any CSV-imported closed trade will always show a blank open time and can't be bucketed by holding duration. No known fix without a second data source (e.g. cross-referencing the CSV's own MARKET_ORDER open rows by instrument/time proximity), so this is tracked rather than blocking.

### 12. `frmj orders cancel` — cancel a pending entry order

`frmj positions` lists pending entry orders, but cancelling one still means going to Oanda's UI. Add `frmj orders cancel ORDER_ID` (PUT `/accounts/{id}/orders/{orderID}/cancel`, confirm first, then sync). Notes/tags/trade plan on a cancelled order's LIMIT_ORDER transaction stay where they are; decide whether `journal` should show them or flag them as never filled.

### 13. `trade --limit` follow-ups: GTD expiry and `--multi`

`trade --limit` v1 is GTC-only and single-account. Two extensions were deferred: an `--expires` option for GTD orders (Oanda's `timeInForce: GTD` + `gtdTime`), and `--limit` with `--multi`, which needs the limit price chosen once and mirrored for `--opposite` accounts (a long's "15 pips below ask" becomes a short's "15 pips above bid").
