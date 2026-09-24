# Configuration

[← Back to README](../README.md)

For a first-time walkthrough, see [Quick start](../README.md#quick-start) in the README.

## API tokens

Oanda issues one API token per environment (practice vs live), not per account. Tokens are stored by environment in the OS keychain:

```sh
frmj account set-token practice    # store practice token (prompted, never echoed)
frmj account set-token live        # store live token (prompted, never echoed)
frmj account set-token             # defaults to the active account's environment type
```

Backed by GNOME Keyring / KWallet on Linux, Keychain on macOS, Credential Locker on Windows.

## Switching between accounts

```sh
frmj account use practice    # activate the 'practice' profile
frmj account use funded      # activate the 'funded' profile
frmj account current         # show which account is active
frmj account list            # show all configured accounts
```

## Execution mode (practice vs. live)

Account selection and execution mode are kept separate as an additional safety gate. Switching to a live account does not automatically enable live order placement — you must also enable live mode explicitly:

```sh
frmj mode practice           # disable live order placement (safe default)
frmj mode live               # enable live order placement (requires confirmation)
```

`frmj mode live` displays the active account name and requires typing `ENABLE LIVE` exactly before proceeding. This prevents accidental live trades when testing new workflows.

## Status at a glance

```sh
frmj status
```

Shows the active account name, type (practice / live), Oanda account ID, and current execution mode.

## Environment Variables

| Variable | Required | Description |
|---|---|---|
| `OANDA_API_TOKEN_PRACTICE` | No | API token for practice accounts. Takes priority over the OS keychain. |
| `OANDA_API_TOKEN` | No | API token for live accounts; also used as a fallback for practice accounts. |
| `FRMJ_DB_PATH` | No | Path to the SQLite file. Defaults to `~/.local/share/frmj/frmj.db`. |

## Config table keys (set with `frmj config set`)

Account IDs and active account selection are managed via `frmj account`, not `frmj config set`. The following keys are valid:

| Key | Required | Default | Description |
|---|---|---|---|
| `max_open_trades` | Yes | — | Maximum concurrent open tickets (e.g. `6`) |
| `risk_strategy` | No | `remaining_margin_fraction` | Sizing strategy (see [Risk model](#risk-model-domainriskpy)) |
| `blocking_mode` | No | `hard_block` | `hard_block` or `warning_only` at the trade cap |
| `scale_in` | No | `never` | `never`, `warn`, or `allow` for same-instrument adds (an open ticket or pending order on the instrument) |
| `correlation_blocking_mode` | No | `warning_only` | `hard_block` or `warning_only` for correlated open positions or pending orders |
| `safety_reserve_pct` | No | `0` | Fraction of equity to never deploy, e.g. `0.10` for 10% |
| `percent_of_equity` | Conditional | — | Required when `risk_strategy = percent_of_equity` |
| `fixed_dollar` | Conditional | — | Required when `risk_strategy = fixed_dollar` |

## Risk model (`domain/risk.py`)

Three sizing strategies are supported:

**`remaining_margin_fraction`** (default) — the primary strategy. With `M` max trades and `N` currently open, the next trade deploys `1 / (M + 1 - N)` of available margin. This produces an invariant: over `M` filled trades, each consumes exactly `1/(M+1)` of the original margin, leaving a permanent `1/(M+1)` buffer as breathing room for margin calls. No parameter needed beyond `max_open_trades`.

**`percent_of_equity`** — a fixed fraction of total account equity, regardless of open trades. Set `percent_of_equity` config key.

**`fixed_dollar`** — a fixed dollar amount per trade. Set `fixed_dollar` config key.

All strategies respect `safety_reserve_pct`: that fraction of equity is subtracted from available margin before any formula is applied.

**Pending entry orders** (limit, stop, market-if-touched) are treated as if they had already filled, for market and limit trades alike: each one counts toward `N` and the `max_open_trades` cap, its estimated margin at current prices is subtracted from available margin before sizing, and it counts for the `scale_in` and correlation checks. Oanda sets aside no margin for a pending order, so without this a new trade could leave too little margin for it to fill. The trade plan shows them next to open trades, e.g. `Open trades: 2 / 6 (+1 pending)`.
