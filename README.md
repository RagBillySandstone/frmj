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

Optionally enable tab completion (account names, instruments, tags, config keys, ...) for your shell, then restart it:

```sh
frmj --install-completion
```

---

## Quick start

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

# Risk settings (shared by all accounts)
frmj config set max_open_trades 6

# Check everything is wired up
frmj config check
frmj config check --connectivity   # also calls the Oanda API to verify credentials
```

---

## Commands

| Command | What it does |
|---|---|
| [`frmj status`](docs/configuration.md#status-at-a-glance) | Active account, its type, and the execution mode |
| [`frmj sync`](docs/commands.md#frmj-sync) | Pull transactions from Oanda (or an Oanda Hub CSV) into the local database |
| [`frmj positions`](docs/commands.md#frmj-positions) | Open trades and pending orders with live P/L, TP/SL, financing, and an account summary |
| [`frmj financing`](docs/commands.md#frmj-financing) | Current long/short financing rates, recorded daily for later lookup |
| [`frmj trade`](docs/commands.md#frmj-trade) | Interactive risk-checked sizing, TP/SL planning, and order placement |
| [`frmj close`](docs/commands.md#frmj-close) | Close all open tickets for an instrument |
| [`frmj stats`](docs/commands.md#frmj-stats) | Performance statistics from the local journal |
| [`frmj journal`](docs/commands.md#frmj-journal) | Recent transactions with their notes and tags |
| [`frmj export`](docs/commands.md#frmj-export) | Export transactions to CSV or JSON |
| [`frmj note`](docs/commands.md#frmj-note) / [`tag`](docs/commands.md#frmj-tag) | Annotate a transaction |
| [`frmj account`](docs/commands.md#frmj-account) | Add, rename, and remove account profiles; API tokens; [account groups](docs/commands.md#frmj-account-group) |
| [`frmj mode`](docs/commands.md#frmj-mode) | Enable or disable live order placement |
| [`frmj config`](docs/commands.md#frmj-config) | Get, set, and validate configuration and risk settings |

`frmj sync`, `positions`, `trade`, `close`, `journal`, and `stats` act on the active account by default. Pass `--account NAME` (`-a NAME`) to target another configured account for that one command; `journal` and `stats` also take `--all-accounts` (`-A`).

![Example frmj stats output](docs/frmj_stats.png)

## Documentation

- [Command reference](docs/commands.md) — every command and option in detail
- [Configuration](docs/configuration.md) — API tokens, execution mode, environment variables, config keys, the risk model, and trade limits
- [Architecture](docs/architecture.md) — module layout, layer separation, and database schema

---

## Development

```sh
uv sync --group dev
uv run pytest
uv run mypy
```

Tests live in `tests/` and mirror the `src/` layout. The domain tests (`tests/domain/`) use no fixtures or mocks — pure data in, pure data out. The execution tests use lightweight test doubles that satisfy `ClientProtocol` via structural typing (no inheritance required).

`mypy` currently type-checks `src/frmj` only; `tests/` isn't included yet (see TODO.md).
