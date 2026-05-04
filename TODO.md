# FRoMaJ — TODO

Items are grouped by priority. Within each group, order reflects logical build sequence.

---

## P1 — Monitoring

### 1. Oanda Streaming API for transactions

`GET /v3/accounts/{id}/transactions/stream` opens a persistent HTTP connection; Oanda pushes transaction events as they happen. This eliminates polling lag and API quota waste from `sync --watch`. Should be an optional upgrade to `--watch` mode, falling back to polling if the stream drops. Requires keeping the `httpx` client alive as a streaming context.

---

## P2 — UX and extensibility

### 2. `frmj note --edit` — amend an existing note

Currently `frmj note` only appends. For cases where a note has a typo or needs updating, add an `--edit` flag that opens the most recent note on the given transaction in `$EDITOR` (or prompts inline if the env var is unset).

### 3. Service layer extraction

The CLI commands currently call `app.py` factories directly. Before building a GUI or REST API wrapper, extract a `services.py` layer that encapsulates multi-step operations (the full trade flow, the sync flow) as callable functions with no Typer dependency. The CLI then becomes a thin argument-parsing shell over the service layer. This is the prerequisite for all non-CLI interfaces.

### 4. CSV import from Oanda Hub download

Allow bootstrapping the DB from the CSV file Oanda provides in the account hub (`History → Download`). Gives a way to back-fill history for accounts that have years of transactions before the first `frmj sync --cold` run, and provides a cross-check against the API sync. Parser should map CSV column names to the `transactions` schema and skip rows already present.
