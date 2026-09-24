# Architecture

[← Back to README](../README.md)

```
src/frmj/
├── cli.py              # Typer CLI — prompts/output, thin shell over services + app layer
├── services.py         # Multi-step flows (trade planning, post-fill, positions, close) — no Typer dependency
├── app.py              # Wiring: DB factory, client factory, config helpers, keychain
├── accounts.py         # Pure SQLite CRUD for named account profiles and live-mode flag
├── domain/
│   ├── risk.py         # Pure risk model: trade cap, scale-in policy, sizing decision
│   ├── sizing.py       # Pure unit sizing: capital → units respecting margin formula
│   └── pricing.py      # Pure exit pricing: TP/SL pips or %RoM → prices, P/L, R:R
├── execution/
│   ├── oanda/
│   │   ├── client.py   # OandaClient — httpx wrapper for Oanda v3 REST API
│   │   ├── parsing.py  # Pure functions: Oanda API dicts → dataclasses
│   │   └── models.py   # Dataclasses shared by client.py and parsing.py
│   └── sync.py         # Ingestion: Oanda rows → SQLite, cursor management
└── persistence/
    └── schema.py       # SQLite DDL and ensure_schema()
```

## Layer separation

The three domain modules (`risk`, `sizing`, `pricing`) are **pure functions with no I/O**. They accept data objects and return data objects. No database, no HTTP, no environment variables, no clocks. This makes them trivially testable and reusable from any future interface (GUI, REST API, back-testing harness).

The execution layer (`oanda`, `sync`) handles all network and database I/O. It feeds structured data into the domain layer and writes results to SQLite.

`accounts.py` is pure SQLite CRUD — no I/O beyond the database connection. All keychain access and environment-variable resolution happens in `app.py`.

`app.py` is the only place that reads environment variables, touches the filesystem, or accesses the OS keychain. The CLI commands call `app.py` to obtain wired-up dependencies, then pass them into `services.py` and the domain layer.

`services.py` holds multi-step operations that combine several Oanda API calls and/or domain calls into one unit — fetching the market data needed to plan a trade, evaluating risk and correlation, attaching TP/SL and syncing after a fill, fetching the data behind `positions`, and closing tickets for `close`. It takes an already-open connection and client as arguments and has no Typer dependency, so it's reusable from any future non-CLI interface. Prompting, confirmation, and terminal output stay in `cli.py`.

`plan_account_sizing()` is the one per-account planning step — risk check, correlation check, and unit sizing — shared by both `trade()` (called once) and the `--multi` group flow (called once per account, against a shared `InstrumentContext` but each account's own `AccountContext`), so the two commands can't drift out of sync on that logic.

## Database schema

SQLite at `~/.local/share/frmj/frmj.db` (or `$FRMJ_DB_PATH`). WAL mode. Foreign keys enforced.

| Table | Purpose |
|---|---|
| `accounts` | Named Oanda account profiles (name, account ID, practice flag). Active account and live-mode flag are stored in `config`. |
| `transactions` | Append-only Oanda event ledger. Stores full raw JSON alongside parsed index columns. |
| `notes` | Free-text notes attached to transactions. |
| `tags` | Short labels attached to transactions; used in journal filters and stats breakdowns. |
| `trade_plans` | Intended TP/SL prices recorded at order time; shown in `journal` alongside fills. For a limit order the plan (and any notes/tags) sits on the pending order's transaction until sync moves it to the fill. |
| `sync_cursors` | One row per account; tracks the last ingested Oanda transaction ID for incremental sync. |
| `config` | Flat key/value store for all runtime configuration, including `active_account` and `live_mode`. |

Transactions are never updated or deleted — Oanda is the system of record. Corrective events arrive as new rows. The full raw JSON payload is preserved in every row so new columns can be added via migration without re-fetching from the API.

## Migration from earlier versions

If you have an existing database using the old flat-config account system (`practice_account_id`, `account_id`, `practice_mode` keys), FRoMaJ will auto-migrate on first run: it reads those keys, creates corresponding named account profiles (`"practice"` and/or `"live"`), and removes the old keys. No manual action required.
