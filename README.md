# FRoMaJ

**Forex Risk Operations, Management & Journal** — a CLI trading assistant for Oanda.

FRoMaJ handles the mechanical parts of a discretionary FX trading workflow: position sizing, TP/SL planning, order execution, and a local transaction journal. The risk model is pure and decoupled so the CLI is a thin shell over it; a GUI or API layer can be wired in later without touching the domain.

---

## Requirements

- Python 3.11+
- [uv](https://docs.astral.sh/uv/) (build + venv management)
- An Oanda v20 account (practice or live)

---

## Installation

```sh
git clone <repo>
cd frmj
uv sync
```

The `frmj` entry point is installed into the project's virtual environment:

```sh
uv run frmj --help
```

Or activate the venv first:

```sh
source .venv/bin/activate
frmj --help
```

---

## Configuration

### API token

The Oanda API token is resolved in this order:

1. **OS keychain** (preferred) — stored once, read silently on every run:
   ```sh
   frmj config set-token     # prompted, never echoed to the terminal
   frmj config unset-token   # remove it
   ```
   Backed by GNOME Keyring / KWallet on Linux, Keychain on macOS, Credential Locker on Windows. The token is encrypted by the OS using your login credentials — no master passphrase needed.

2. **Environment variable** (fallback for CI / containers):
   ```sh
   export OANDA_API_TOKEN=your-token-here
   ```
   When `OANDA_API_TOKEN` is set it always takes precedence over the keychain, so existing setups require no changes.

### Environment variables

| Variable | Required | Description |
|---|---|---|
| `OANDA_API_TOKEN` | No | Oanda personal access token. Falls back to the OS keychain if unset. |
| `FRMJ_DB_PATH` | No | Path to the SQLite file. Defaults to `~/.local/share/frmj/frmj.db` |

### Config table keys (set with `frmj config set`)

| Key | Required | Default | Description |
|---|---|---|---|
| `account_id` | Yes | — | Oanda account ID |
| `practice_mode` | No | `true` | `true` for practice, `false` for live |
| `max_open_trades` | Yes | — | Maximum concurrent open tickets (e.g. `6`) |
| `risk_strategy` | No | `remaining_margin_fraction` | Sizing strategy (see Risk Model) |
| `blocking_mode` | No | `hard_block` | `hard_block` or `warning_only` at the trade cap |
| `scale_in` | No | `never` | `never`, `warn`, or `allow` for same-instrument adds |
| `safety_reserve_pct` | No | `0` | Fraction of equity to never deploy, e.g. `0.10` for 10% |
| `percent_of_equity` | Conditional | — | Required when `risk_strategy = percent_of_equity` |
| `fixed_dollar` | Conditional | — | Required when `risk_strategy = fixed_dollar` |

### First-time setup

```sh
# Store the API token in the OS keychain (prompted, not echoed)
frmj config set-token

# Or for CI / headless environments:
# export OANDA_API_TOKEN=your-token-here

frmj config set account_id 001-001-12345678-001
frmj config set practice_mode true
frmj config set max_open_trades 6
```

---

## Usage

### `frmj sync`

Pull transactions from Oanda into the local database.

```sh
frmj sync               # incremental (only new transactions since last sync)
frmj sync --cold        # full history re-fetch (safe to re-run; duplicates are skipped)
```

### `frmj trade`

Interactive trade planning and execution flow.

```sh
frmj trade EUR_USD long
frmj trade USD_JPY short
frmj trade AUD_USD long --dry-run    # show plan only; no order placed
```

The flow:

1. Auto-syncs new transactions (silent unless new rows arrive).
2. Fetches live account state (NAV, available margin, open trade count).
3. Runs the risk model to determine capital to deploy.
4. Fetches live bid/ask and instrument metadata.
5. Computes position size (units and actual margin used).
6. Prompts for take-profit and stop-loss (pips or `%` return-on-margin).
7. Displays the full trade plan including exit prices, projected P/L, and R:R ratio.
8. Confirms before placing a market order (`y` / `n` / `e` to edit TP/SL).
9. Prompts for an optional trade note after fill.

**TP/SL input formats:**

| Input | Meaning |
|---|---|
| `50` or `50p` | 50 pips |
| `5%` | 5% return on margin used |

### `frmj note`

Attach a free-text note to any transaction by its Oanda transaction ID.

```sh
frmj note 12345 "Entered on 4H breakout, tight spread"
```

Run `frmj sync` first if the transaction is not yet in the local database.

### `frmj journal`

Display recent transactions with any attached notes (reads local DB only — no network call).

```sh
frmj journal            # last 20 transactions
frmj journal --n 50     # last 50 transactions
```

### `frmj config set` / `frmj config get` / token commands

```sh
frmj config set max_open_trades 8
frmj config get practice_mode
frmj config get              # show all values + token status
frmj config set-token        # store API token in OS keychain (prompted)
frmj config unset-token      # remove token from OS keychain
```

---

## Architecture

```
src/frmj/
├── cli.py              # Typer CLI — thin shell over domain + app layer
├── app.py              # Wiring: DB factory, client factory, config helpers
├── domain/
│   ├── risk.py         # Pure risk model: trade cap, scale-in policy, sizing decision
│   ├── sizing.py       # Pure unit sizing: capital → units respecting margin formula
│   └── pricing.py      # Pure exit pricing: TP/SL pips or %RoM → prices, P/L, R:R
├── execution/
│   ├── oanda.py        # httpx wrapper for Oanda v3 REST API
│   └── sync.py         # Ingestion: Oanda rows → SQLite, cursor management
└── persistence/
    └── schema.py       # SQLite DDL and ensure_schema()
```

### Layer separation

The three domain modules (`risk`, `sizing`, `pricing`) are **pure functions with no I/O**. They accept data objects and return data objects. No database, no HTTP, no environment variables, no clocks. This makes them trivially testable and reusable from any future interface (GUI, REST API, back-testing harness).

The execution layer (`oanda`, `sync`) handles all network and database I/O. It feeds structured data into the domain layer and writes results to SQLite.

`app.py` is the only place that reads environment variables or resolves the database path. The CLI commands call `app.py` to obtain wired-up dependencies, then pass them into the execution and domain layers.

### Risk model (`domain/risk.py`)

Three sizing strategies are supported:

**`remaining_margin_fraction`** (default) — the primary strategy. With `M` max trades and `N` currently open, the next trade deploys `1 / (M + 1 - N)` of available margin. This produces an invariant: over `M` filled trades, each consumes exactly `1/(M+1)` of the original margin, leaving a permanent `1/(M+1)` buffer as breathing room for margin calls. No parameter needed beyond `max_open_trades`.

**`percent_of_equity`** — a fixed fraction of total account equity, regardless of open trades. Set `percent_of_equity` config key.

**`fixed_dollar`** — a fixed dollar amount per trade. Set `fixed_dollar` config key.

All strategies respect `safety_reserve_pct`: that fraction of equity is subtracted from available margin before any formula is applied.

### Database schema

SQLite at `~/.local/share/frmj/frmj.db` (or `$FRMJ_DB_PATH`). WAL mode. Foreign keys enforced.

| Table | Purpose |
|---|---|
| `transactions` | Append-only Oanda event ledger. Stores full raw JSON alongside parsed index columns. |
| `notes` | Free-text notes attached to transactions. |
| `sync_cursors` | One row per account; tracks the last ingested Oanda transaction ID for incremental sync. |
| `config` | Flat key/value store for all runtime configuration. |

Transactions are never updated or deleted — Oanda is the system of record. Corrective events arrive as new rows. The full raw JSON payload is preserved in every row so new columns can be added via migration without re-fetching from the API.

---

## Development

```sh
uv sync --group dev
uv run pytest
```

Tests live in `tests/` and mirror the `src/` layout. The domain tests (`tests/domain/`) use no fixtures or mocks — pure data in, pure data out. The execution tests use lightweight test doubles that satisfy `ClientProtocol` via structural typing (no inheritance required).

---

## Roadmap

- Attach TP/SL orders to fills via Oanda's order API
- `frmj stats` — P/L aggregates, win rate, best/worst hours and days from the journal
- Multi-account support (the schema already has `account_id` discriminator columns)
