# FRoMaJ — TODO

Items are grouped by priority. Within each group, order reflects logical build sequence.

---

## P1 — Data integrity

### 1. Financing parent/child linking

`_parse_transaction` in `oanda.py` documents this as explicitly deferred: the `parent_oanda_id` field is always `None` because the right field names needed to be validated against live data. The parent `DAILY_FINANCING` row has `relatedTransactionIDs`; each child's synthetic `parent_id` FK should be set at ingest time. Required before any stats that include financing cost, and before `journal` can roll up financing rows cleanly.

### 2. Three-leg cross-pair currency conversion

`OandaClient._currency_to_home` raises `ValueError` when neither `{ccy}_{home}` nor `{home}_{ccy}` is a known pair. For crosses like EUR/GBP on a USD account, a third intermediate pair is needed (e.g. EUR/USD and GBP/USD). The fix is to resolve each side independently via `_currency_to_home` and combine. Low frequency in practice (most traded pairs are USD majors) but should be handled cleanly rather than failing loudly.

---

## P2 — Monitoring

### 3. Oanda Streaming API for transactions

`GET /v3/accounts/{id}/transactions/stream` opens a persistent HTTP connection; Oanda pushes transaction events as they happen. This eliminates polling lag and API quota waste from `sync --watch`. Should be an optional upgrade to `--watch` mode, falling back to polling if the stream drops. Requires keeping the `httpx` client alive as a streaming context.

---

## P3 — UX and extensibility

### 4. `frmj note --edit` — amend an existing note

Currently `frmj note` only appends. For cases where a note has a typo or needs updating, add an `--edit` flag that opens the most recent note on the given transaction in `$EDITOR` (or prompts inline if the env var is unset).

### 5. Service layer extraction

The CLI commands currently call `app.py` factories directly. Before building a GUI or REST API wrapper, extract a `services.py` layer that encapsulates multi-step operations (the full trade flow, the sync flow) as callable functions with no Typer dependency. The CLI then becomes a thin argument-parsing shell over the service layer. This is the prerequisite for all non-CLI interfaces.

### 6. CSV import from Oanda Hub download

Allow bootstrapping the DB from the CSV file Oanda provides in the account hub (`History → Download`). Gives a way to back-fill history for accounts that have years of transactions before the first `frmj sync --cold` run, and provides a cross-check against the API sync. Parser should map CSV column names to the `transactions` schema and skip rows already present.
