# FRoMaJ

[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](LICENSE)

**Forex Risk Operations, Management & Journal** — a CLI trading assistant for Oanda.

FRoMaJ handles the mechanical parts of a discretionary FX trading workflow: position sizing, TP/SL planning, order execution, and a local transaction journal. The risk model is pure and decoupled so the CLI is a thin shell over it; a GUI or API layer can be wired in later without touching the domain.

---

> **Disclaimer:** This software implements the author's personal risk management rules and is shared for personal and educational use only. It is **not** financial or investment advice, and nothing in this repository should be construed as a recommendation to buy, sell, or hold any financial instrument. Forex trading involves substantial risk of loss and is not suitable for all participants. Past performance is not indicative of future results. You are solely responsible for any trading decisions you make. Use at your own risk.

---

## Requirements

- Python 3.11+
- [uv](https://docs.astral.sh/uv/) (build + venv management)
- An Oanda v20 account (practice or live)

---

## Installation

```sh
git clone https://github.com/RagBillySandstone/frmj.git
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

### First-time setup

FRoMaJ uses named account profiles. Add accounts once, then switch between them freely without re-entering credentials.

```sh
# Add a practice account (prompts for Oanda account ID and type)
frmj account add practice
frmj account set-token practice    # store practice token in OS keychain (prompted, never echoed)

# Add a live account (prompts for Oanda account ID; choose type: live)
frmj account add funded
frmj account set-token live        # store live token in OS keychain (prompted, never echoed)

# Activate whichever account you want to work with
frmj account use practice

# Shared risk config
frmj config set max_open_trades 6

# Check everything is wired up
frmj config check
frmj config check --connectivity   # also calls the Oanda API to verify credentials
```

### API tokens

Oanda issues one API token per environment (practice vs live), not per account. Tokens are stored by environment in the OS keychain:

```sh
frmj account set-token practice    # store practice token (prompted, never echoed)
frmj account set-token live        # store live token (prompted, never echoed)
frmj account set-token             # defaults to the active account's environment type
```

Backed by GNOME Keyring / KWallet on Linux, Keychain on macOS, Credential Locker on Windows.

### Switching between accounts

```sh
frmj account use practice    # activate the 'practice' profile
frmj account use funded      # activate the 'funded' profile
frmj account current         # show which account is active
frmj account list            # show all configured accounts
```

### Execution mode (practice vs. live)

Account selection and execution mode are kept separate as an additional safety gate. Switching to a live account does not automatically enable live order placement — you must also enable live mode explicitly:

```sh
frmj mode practice           # disable live order placement (safe default)
frmj mode live               # enable live order placement (requires confirmation)
```

`frmj mode live` displays the active account name and requires typing `ENABLE LIVE` exactly before proceeding. This prevents accidental live trades when testing new workflows.

### Status at a glance

```sh
frmj status
```

Shows the active account name, type (practice / live), Oanda account ID, and current execution mode.

---

## Usage

### `frmj sync`

Pull transactions from Oanda into the local database.

```sh
frmj sync               # incremental (only new transactions since last sync)
frmj sync --cold        # full history re-fetch (safe to re-run; duplicates are skipped)
frmj sync --watch       # poll for new transactions continuously (Ctrl+C to stop)
frmj sync --watch --interval 30   # poll every 30 seconds (default: 60)
```

### `frmj positions`

Show all open trades with live P/L, margin, and TP/SL levels, plus an account summary footer.

```sh
frmj positions
```

### `frmj trade`

Interactive trade planning and execution flow.

```sh
frmj trade EUR_USD long
frmj trade USD_JPY short
frmj trade AUD_USD long --dry-run    # show plan only; no order placed
frmj trade --resume                  # execute a previously saved draft plan
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
9. Attaches TP/SL to the open trade on Oanda.
10. Prompts for an optional note and tags after fill.

**TP/SL input formats:**

| Input | Meaning |
|---|---|
| `50` or `50p` | 50 pips |
| `5%` | 5% return on margin used |

If the active account is a live account and live mode is not enabled, the `trade` command exits with a clear error before placing any order.

If the order placement request times out or fails, the plan can be saved (`s`) and resumed later with `frmj trade --resume`.

### `frmj close`

Close all open tickets for an instrument.

```sh
frmj close EUR_USD
```

Shows each ticket's current P/L, prompts for confirmation, then runs an incremental sync after closing.

### `frmj stats`

Show trade performance statistics from the local journal. Auto-syncs before displaying.

```sh
frmj stats
```

Output includes: win rate, average P/L, total P/L, best/worst trade, and breakdowns by instrument, weekday, hour (UTC), and tag.

### `frmj journal`

Display recent transactions with any attached notes and tags. Auto-syncs before displaying.

```sh
frmj journal                          # last 20 transactions
frmj journal --n 50                   # last 50 transactions
frmj journal --instrument EUR_USD     # filter by instrument
frmj journal --type ORDER_FILL        # filter by transaction type
frmj journal --since 2026-04-01       # on or after a date
frmj journal --with-notes             # only transactions with notes
frmj journal --tag breakout           # only transactions tagged 'breakout'
```

### `frmj export`

Export transactions to CSV or JSON for external analysis.

```sh
frmj export                                  # CSV to stdout
frmj export --format json                    # JSON to stdout
frmj export --output trades.csv              # write to file
frmj export --instrument EUR_USD --since 2026-01-01 --include-notes
```

Supports the same `--instrument`, `--type`, and `--since` filters as `journal`.

### `frmj note`

Attach a free-text note to any transaction by its Oanda transaction ID.

```sh
frmj note 12345 "Entered on 4H breakout, tight spread"
```

Run `frmj sync` first if the transaction is not yet in the local database.

### `frmj tag`

Attach one or more short labels to a transaction.

```sh
frmj tag 12345 breakout london-open
```

Tags are normalised to lowercase. Only letters, digits, hyphens, and underscores are allowed.

### `frmj account`

Manage named Oanda account profiles.

```sh
frmj account add NAME              # add a new account profile (prompts for Oanda ID and type)
frmj account list                  # list all configured accounts
frmj account use NAME              # set NAME as the active account
frmj account current               # show the currently active account
frmj account remove NAME           # remove an account profile
frmj account set-token practice    # store or update the practice API token
frmj account set-token live        # store or update the live API token
```

### `frmj mode`

Control whether live order placement is enabled. This is independent of account selection and acts as an additional confirmation gate.

```sh
frmj mode practice    # disable live order placement (safe default)
frmj mode live        # enable live order placement (requires typing "ENABLE LIVE")
```

### `frmj config`

```sh
frmj config set max_open_trades 6  # set a config key
frmj config get max_open_trades    # read one key
frmj config get                    # show all keys + token status
frmj config unset risk_strategy    # remove a key (resets to default)
frmj config check                  # validate all config, report issues
frmj config check --connectivity   # also verify credentials against the API
```

### Risk model (`domain/risk.py`)

Three sizing strategies are supported:

**`remaining_margin_fraction`** (default) — the primary strategy. With `M` max trades and `N` currently open, the next trade deploys `1 / (M + 1 - N)` of available margin. This produces an invariant: over `M` filled trades, each consumes exactly `1/(M+1)` of the original margin, leaving a permanent `1/(M+1)` buffer as breathing room for margin calls. No parameter needed beyond `max_open_trades`.

**`percent_of_equity`** — a fixed fraction of total account equity, regardless of open trades. Set `percent_of_equity` config key.

**`fixed_dollar`** — a fixed dollar amount per trade. Set `fixed_dollar` config key.

All strategies respect `safety_reserve_pct`: that fraction of equity is subtracted from available margin before any formula is applied.


---

## Environment Variables

| Variable | Required | Description |
|---|---|---|
| `OANDA_API_TOKEN_PRACTICE` | No | API token for practice accounts. Takes priority over the OS keychain. |
| `OANDA_API_TOKEN` | No | API token for live accounts; also used as a fallback for practice accounts. |
| `FRMJ_DB_PATH` | No | Path to the SQLite file. Defaults to `~/.local/share/frmj/frmj.db`. |

### Config table keys (set with `frmj config set`)

Account IDs and active account selection are managed via `frmj account`, not `frmj config set`. The following keys are valid:

| Key | Required | Default | Description |
|---|---|---|---|
| `max_open_trades` | Yes | — | Maximum concurrent open tickets (e.g. `6`) |
| `risk_strategy` | No | `remaining_margin_fraction` | Sizing strategy (see Risk Model) |
| `blocking_mode` | No | `hard_block` | `hard_block` or `warning_only` at the trade cap |
| `scale_in` | No | `never` | `never`, `warn`, or `allow` for same-instrument adds |
| `safety_reserve_pct` | No | `0` | Fraction of equity to never deploy, e.g. `0.10` for 10% |
| `percent_of_equity` | Conditional | — | Required when `risk_strategy = percent_of_equity` |
| `fixed_dollar` | Conditional | — | Required when `risk_strategy = fixed_dollar` |


---

## Architecture

```
src/frmj/
├── cli.py              # Typer CLI — thin shell over domain + app layer
├── app.py              # Wiring: DB factory, client factory, config helpers, keychain
├── accounts.py         # Pure SQLite CRUD for named account profiles and live-mode flag
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

`accounts.py` is pure SQLite CRUD — no I/O beyond the database connection. All keychain access and environment-variable resolution happens in `app.py`.

`app.py` is the only place that reads environment variables, touches the filesystem, or accesses the OS keychain. The CLI commands call `app.py` to obtain wired-up dependencies, then pass them into the execution and domain layers.

### Database schema

SQLite at `~/.local/share/frmj/frmj.db` (or `$FRMJ_DB_PATH`). WAL mode. Foreign keys enforced.

| Table | Purpose |
|---|---|
| `accounts` | Named Oanda account profiles (name, account ID, practice flag). Active account and live-mode flag are stored in `config`. |
| `transactions` | Append-only Oanda event ledger. Stores full raw JSON alongside parsed index columns. |
| `notes` | Free-text notes attached to transactions. |
| `tags` | Short labels attached to transactions; used in journal filters and stats breakdowns. |
| `trade_plans` | Intended TP/SL prices recorded at order time; shown in `journal` alongside fills. |
| `sync_cursors` | One row per account; tracks the last ingested Oanda transaction ID for incremental sync. |
| `config` | Flat key/value store for all runtime configuration, including `active_account` and `live_mode`. |

Transactions are never updated or deleted — Oanda is the system of record. Corrective events arrive as new rows. The full raw JSON payload is preserved in every row so new columns can be added via migration without re-fetching from the API.

### Migration from earlier versions

If you have an existing database using the old flat-config account system (`practice_account_id`, `account_id`, `practice_mode` keys), FRoMaJ will auto-migrate on first run: it reads those keys, creates corresponding named account profiles (`"practice"` and/or `"live"`), and removes the old keys. No manual action required.

---

## Development

```sh
uv sync --group dev
uv run pytest
```

Tests live in `tests/` and mirror the `src/` layout. The domain tests (`tests/domain/`) use no fixtures or mocks — pure data in, pure data out. The execution tests use lightweight test doubles that satisfy `ClientProtocol` via structural typing (no inheritance required).
