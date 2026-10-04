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

### 11. CSV-imported closing fills have no open-time link

Unlike the REST API's ORDER_FILL transactions, the Oanda Hub CSV export never states which trade a closing fill closed (no `tradesClosed`/`tradeReduced` equivalent — see `execution/csv_import.py`'s module docstring). `frmj stats`'s open-time lookup already degrades gracefully to a NULL open_time for these rows rather than crashing, but any CSV-imported closed trade will always show a blank open time and can't be bucketed by holding duration. No known fix without a second data source (e.g. cross-referencing the CSV's own MARKET_ORDER open rows by instrument/time proximity), so this is tracked rather than blocking.

### 12. `frmj orders cancel` — cancel a pending entry order

`frmj positions` lists pending entry orders, but cancelling one still means going to Oanda's UI. Add `frmj orders cancel ORDER_ID` (PUT `/accounts/{id}/orders/{orderID}/cancel`, confirm first, then sync). Notes/tags/trade plan on a cancelled order's LIMIT_ORDER transaction stay where they are; decide whether `journal` should show them or flag them as never filled.

### 13. `trade --limit` follow-ups: GTD expiry and `--multi`

`trade --limit` v1 is GTC-only and single-account. Two extensions were deferred: an `--expires` option for GTD orders (Oanda's `timeInForce: GTD` + `gtdTime`), and `--limit` with `--multi`, which needs the limit price chosen once and mirrored for `--opposite` accounts (a long's "15 pips below ask" becomes a short's "15 pips above bid").

### 15. ATR-multiple take-profit (`3x` at the TP prompt)

`trade` accepts `2x` / `2atr` only at the stop-loss prompt (see the ATR stop-loss in `docs/commands.md`). Allowing it at the take-profit prompt too would let a plan be set up fully in ATR terms (e.g. 1.5× ATR stop, 3× ATR target). This needs `_prompt_tpsl` to take the ATR, and a `tp_atr_multiple` in `trade_plans` and the draft, next to `sl_atr_multiple`. There should be no default: Enter still skips the TP.

### 16. Move direct SQL out of `cli/` into a query module

`services.py` is meant to hold everything a non-CLI front end would need, but 27 `conn.execute` calls still live in `cli/` (see "Components and dependencies" in `docs/architecture.md`): the `journal` listing and filters, `stats`' closed-trade/tag/financing queries, `export`, `note`/`tag` lookup and inserts (`_resolve_transaction`, `_attach_tags`), the post-trade note prompt in `trade.py` and `_trade_multi.py`, `[note]` counts in `_display.py`, `sync --watch` output, `financing` snapshot read/write, and transaction-type completion. Move them into plain functions that take a connection and return data (in `services.py`, or a `queries.py` beside it if that grows too large), leaving `cli/` to prompt and format. No behavior change; the existing CLI tests should cover it.
