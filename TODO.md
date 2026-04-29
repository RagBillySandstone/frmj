# FRoMaJ — TODO

Items are grouped by priority. Within each group, order reflects logical build sequence (things that unblock other things come first).

---

## P0 — Core workflow gaps (trading is incomplete without these)

### 1. Place TP/SL orders on Oanda after a fill
The `trade` command computes and displays exit levels but never sends them to Oanda. After the market order fills, two follow-up requests are needed: a `TAKE_PROFIT_ORDER` and a `STOP_LOSS_ORDER` attached to the fill's trade ID. Oanda's v3 API accepts these via `POST /accounts/{id}/orders` with `type: TAKE_PROFIT` / `STOP_LOSS` and a `tradeID` field. The `OandaClient` needs two new methods (`attach_take_profit`, `attach_stop_loss`), and the `trade` command should call them immediately after the fill if the user supplied levels.

### 2. `frmj positions` — show open trades
There is no way to see what is currently open without going to Oanda's web UI. This command should call `GET /accounts/{id}/openTrades`, display each open ticket (instrument, direction, units, entry price, unrealised P/L, current TP/SL levels), and indicate which ones have journal notes. Requires a new `get_open_trades()` method on `OandaClient`.

### 3. `frmj close <instrument>` — close a position
Complements `positions`. Should fetch the open ticket(s) for the instrument, show the current P/L, confirm with the user, then call `PUT /accounts/{id}/trades/{tradeID}/close` for each ticket. Requires a new `close_trade(trade_id)` method on `OandaClient`.

---

## P1 — Data integrity

### 4. Attach TP/SL levels to the journal on entry
When a trade is placed, the TP/SL prices should be stored alongside the fill transaction in the database so the journal has a complete picture of intent. The schema already has `raw_json` (raw Oanda payload is preserved), but a lightweight parsed column for each — or a note auto-created at fill time — would make `frmj journal` and future stats queries much simpler.

### 5. Financing parent/child linking
`_parse_transaction` in `oanda.py` documents this as explicitly deferred: the `parent_oanda_id` field is always `None` because the right field names needed to be validated against live data. Now that there is live data available, implement the parent resolution. The parent `DAILY_FINANCING` row has `relatedTransactionIDs`; each child's synthetic `parent_id` FK should be set at ingest time. This makes the `journal` command able to roll up financing rows correctly and is required before any stats that include financing cost.

### 6. Graceful API failure with actionable options
The current error path just exits with `exit code 1`. The original design spec calls for surfacing three options when a network call fails mid-trade: **Retry / Save plan / Abort**. "Save plan" should serialize the computed trade plan (instrument, direction, units, TP/SL) to a local file or DB row so the user can resume without re-entering everything. "Retry" should re-attempt the failed API call immediately. This is especially important during the order placement step.

### 7. Three-leg cross-pair currency conversion
`OandaClient._currency_to_home` raises `ValueError` when neither `{ccy}_{home}` nor `{home}_{ccy}` is a known pair. For crosses like EUR/GBP on a USD account, a third intermediate pair is needed (e.g. EUR/USD and GBP/USD). The fix is to resolve each side independently via `_currency_to_home` and combine them. Low frequency in practice (most traded pairs are USD majors) but should be handled cleanly rather than failing loudly.

---

## P2 — Analytics (the core long-term value of the project)

### 8. `frmj stats` — trade performance summary
The primary analytics command. Reads from the local DB only. Minimum viable output:
- Total trades, win rate, average P/L per trade
- Best and worst single trade
- P/L by hour of day (UTC and local)
- P/L by day of week
- P/L by instrument

All calculations should be implemented as pure SQL or pure Python functions in a new `domain/analytics.py` module so they are testable without the CLI and reusable for future export.

### 9. `frmj journal` filtering
The current `journal` command only supports `--n`. Useful additions:
- `--instrument EUR_USD` — filter to one pair
- `--type ORDER_FILL` — filter by transaction type
- `--since 2026-04-01` — date range
- `--with-notes` — only transactions that have notes

### 10. Data export for external analysis
A `frmj export` command (or `--format csv/json` flag on `journal`) that writes the transaction ledger to a flat file. The original design brief explicitly calls out feeding this into the DuckDB/Polars backtesting stack. The export should include parsed fields (not just raw JSON) and optionally join the `notes` table so notes travel with the transactions.

---

## P3 — Sync and monitoring

### 11. `frmj sync --watch` — continuous incremental polling
A long-running mode that polls `sync_incremental` on a configurable interval (e.g. every 60 s) and prints new transactions as they arrive. This is the lightweight alternative to the streaming API and fits better with the app's stateless connection model. Useful when actively trading and wanting the local journal to stay current. Should handle `KeyboardInterrupt` cleanly.

### 12. Oanda Streaming API for transactions
The full solution to real-time sync: `GET /v3/accounts/{id}/transactions/stream` opens a persistent HTTP connection; Oanda pushes transaction events as they happen. This eliminates polling lag and API quota waste. Should be an optional upgrade to `--watch` mode, falling back to polling if the stream drops. Requires keeping the `httpx` client alive as a streaming context, which is architecturally straightforward but needs careful connection lifecycle management.

---

## P4 — UX and config

### 13. `frmj config check` — validate the current configuration
Reads all config keys and checks that required keys are present, values are within valid ranges, and strategy-specific required fields are set. Also verifies that `OANDA_API_TOKEN` is in the environment and optionally does a lightweight connectivity check (e.g. fetch account summary). Useful for diagnosing setup problems without having to run a real command and hit an opaque error.

### 14. Trade tags
Allow attaching one or more short labels to a transaction (e.g. `"breakout"`, `"news-play"`, `"faded-move"`). Tags are structurally similar to notes — a separate `tags` table with an FK to `transactions` — but are single words/tokens rather than free text so they can be grouped in stats queries. The `trade` command should prompt for optional tags after the fill note prompt.

### 15. `frmj note --edit` — amend an existing note
Currently `frmj note` only appends. For cases where a note has a typo or needs updating, add an `--edit` flag that opens the most recent note on the given transaction in `$EDITOR` (or prompts inline if the env var is unset).

---

## P5 — Future / extensibility

### 16. Service layer extraction
The CLI commands currently call `app.py` factories directly. Before building a GUI or REST API wrapper, extract a `services.py` layer that encapsulates multi-step operations (the full trade flow, the sync flow) as callable functions with no Typer dependency. The CLI then becomes a thin argument-parsing shell over the service layer. This was called out as a non-negotiable architectural requirement in the original design brief and is the prerequisite for all non-CLI interfaces.

### 17. Structured trade plan serialisation
The computed trade plan (instrument, direction, units, entry, TP/SL, margin) should be serialisable to a dict/dataclass so it can be: saved on API failure (see P1 #6), logged to the DB as a "planned trade" row before execution, and eventually fed to an automated trading bot without going through the CLI. This is the first step toward the ATB reuse path.

### 18. CSV import from Oanda Hub download
Allow bootstrapping the DB from the CSV file Oanda provides in the account hub (`History → Download`). This gives a way to back-fill history for accounts that have years of transactions before the first `frmj sync --cold` run, and provides a cross-check against the API sync. Parser should map CSV column names to the `transactions` schema and skip rows already present.
