"""``frmj config`` — read/write each account's configuration values, manage
the API token."""

from __future__ import annotations

import os
import sqlite3
from decimal import Decimal

import typer

from frmj.accounts import (
    ACCOUNT_CONFIG_KEYS,
    AccountRecord,
    delete_account_config,
    get_account_config,
    get_active_account,
    get_all_account_config,
    is_live_mode,
    resolve_account,
    set_account_config,
)
from frmj.app import (
    DEFAULT_ATR_PERIOD,
    DEFAULT_SL_ATR_MULTIPLE,
    delete_token,
    get_client,
    get_db,
    get_token,
    store_token,
)
from frmj.cli import config_app
from frmj.cli._completion import _complete_account_name
from frmj.cli._display import _display_account_header
from frmj.domain.risk import BlockingMode, RiskStrategy, ScaleInPolicy

# ---------------------------------------------------------------------------
# config sub-commands
# ---------------------------------------------------------------------------


def _complete_config_key(incomplete: str) -> list[str]:
    """Return valid config keys whose names start with *incomplete*."""
    return sorted(k for k in ACCOUNT_CONFIG_KEYS if k.startswith(incomplete.lower()))


#: For config keys backed by an enum, the settings a user is allowed to set.
#: Keys not listed here (e.g. max_open_trades, percent_of_equity) take a
#: free-form numeric value and get no value completion.
_CONFIG_KEY_VALUE_CHOICES: dict[str, list[str]] = {
    "risk_strategy": [s.value for s in RiskStrategy],
    "blocking_mode": [m.value for m in BlockingMode],
    "correlation_blocking_mode": [m.value for m in BlockingMode],
    "scale_in": [p.value for p in ScaleInPolicy],
}


def _complete_config_value(ctx: typer.Context, incomplete: str) -> list[str]:
    """Return valid settings for the config key already typed, if any."""
    choices = _CONFIG_KEY_VALUE_CHOICES.get(ctx.params.get("key") or "", [])
    return [v for v in choices if v.startswith(incomplete.lower())]


def _account_option() -> typer.models.OptionInfo:
    """The ``--account NAME`` option shared by set/get/unset/check."""
    return typer.Option(
        None,
        "--account",
        "-a",
        help="Use this account instead of the active one (see 'frmj account list').",
        autocompletion=_complete_account_name,
    )


def _resolve_target_account(
    conn: sqlite3.Connection, account_name: str | None
) -> AccountRecord:
    """
    Return the account a config command acts on: *account_name* (the
    ``--account`` override) or the active account.

    Prints an error and exits 1 when there is no such account, with the
    same hints as ``get_client``. Callers close *conn* in their own
    ``finally``.
    """
    record = resolve_account(conn, account_name)
    if record is not None:
        return record
    if account_name is not None:
        typer.echo(
            f"Error: No account named '{account_name}'. "
            "List accounts with: frmj account list",
            err=True,
        )
    else:
        typer.echo(
            "Error: No active account. Add one with: frmj account add NAME",
            err=True,
        )
    raise typer.Exit(1)


@config_app.command("set")
def config_set(
    key: str = typer.Argument(
        ...,
        help="Config key, e.g. max_open_trades",
        autocompletion=_complete_config_key,
    ),
    value: str = typer.Argument(
        ..., help="Config value", autocompletion=_complete_config_value
    ),
    account: str | None = _account_option(),
) -> None:
    """Set a configuration value for the active account (or --account)."""
    if key not in ACCOUNT_CONFIG_KEYS:
        valid = ", ".join(sorted(ACCOUNT_CONFIG_KEYS))
        typer.echo(f"Error: '{key}' is not a valid config key.", err=True)
        typer.echo(f"Valid keys: {valid}", err=True)
        raise typer.Exit(1)
    conn = get_db()
    try:
        record = _resolve_target_account(conn, account)
        set_account_config(conn, record.name, key, value)
    finally:
        conn.close()
    typer.echo(f"Set {key} = {value} for account '{record.name}'")


@config_app.command("get")
def config_get(
    key: str | None = typer.Argument(
        None,
        help="Config key to retrieve. Omit to show all configured values.",
        autocompletion=_complete_config_key,
    ),
    account: str | None = _account_option(),
) -> None:
    """Read a configuration value for the active account (or --account), or
    show all of its values if no key is given."""
    conn = get_db()
    try:
        record = _resolve_target_account(conn, account)
        if key is None:
            # Name the account first so the values can't be mistaken for
            # another account's.
            _display_account_header(conn, record.name)
            pairs = get_all_account_config(conn, record.name)
        else:
            value = get_account_config(conn, record.name, key)
    finally:
        conn.close()

    if key is None:
        if not pairs:
            typer.echo("No configuration values set.")
        else:
            width = max(len(k) for k, _ in pairs)
            for k, v in pairs:
                typer.echo(f"{k:<{width}}  =  {v}")
        _print_token_status(record)
        return

    if value is None:
        typer.echo(f"{key} is not set for account '{record.name}'.")
        raise typer.Exit(1)
    typer.echo(value)


@config_app.command("unset")
def config_unset(
    key: str = typer.Argument(
        ...,
        help="Config key to remove, e.g. risk_strategy",
        autocompletion=_complete_config_key,
    ),
    account: str | None = _account_option(),
) -> None:
    """Remove a configuration value from the active account (or --account),
    restoring its default."""
    conn = get_db()
    try:
        record = _resolve_target_account(conn, account)
        removed = delete_account_config(conn, record.name, key)
    finally:
        conn.close()
    if removed:
        typer.echo(f"Unset {key} for account '{record.name}'.")
    else:
        typer.echo(f"{key} was not set for account '{record.name}'.")
        raise typer.Exit(1)


@config_app.command("set-token")
def config_set_token() -> None:
    """Store the Oanda API token for the active account's environment in the OS keychain.

    Use ``frmj account set-token [practice|live]`` to specify the environment directly.
    """
    conn = get_db()
    try:
        account = get_active_account(conn)
    finally:
        conn.close()
    if account is None:
        typer.echo(
            "Error: No active account. Add one with: frmj account add NAME",
            err=True,
        )
        raise typer.Exit(1)
    env_label = "practice" if account.is_practice else "live"
    token = typer.prompt(f"Oanda API token for {env_label} accounts", hide_input=True)
    try:
        store_token(token, practice=account.is_practice)
    except RuntimeError as exc:
        typer.echo(f"Error: {exc}", err=True)
        raise typer.Exit(1)
    typer.echo(f"Token for {env_label} accounts stored in OS keychain.")


@config_app.command("unset-token")
def config_unset_token() -> None:
    """Remove the Oanda API token for the active account's environment from the OS keychain.

    Use ``frmj account set-token [practice|live]`` to manage tokens directly.
    """
    conn = get_db()
    try:
        account = get_active_account(conn)
    finally:
        conn.close()
    if account is None:
        typer.echo(
            "Error: No active account. Add one with: frmj account add NAME",
            err=True,
        )
        raise typer.Exit(1)
    env_label = "practice" if account.is_practice else "live"
    try:
        delete_token(practice=account.is_practice)
    except RuntimeError as exc:
        typer.echo(f"Error: {exc}", err=True)
        raise typer.Exit(1)
    typer.echo(f"Token for {env_label} accounts removed from OS keychain.")


@config_app.command("check")
def config_check(
    connectivity: bool = typer.Option(
        False,
        "--connectivity",
        "-c",
        help="Also verify the account's ID and token are accepted by Oanda.",
    ),
    account_name: str | None = _account_option(),
) -> None:
    """Validate the active account's (or --account's) configuration and
    report any issues."""
    conn = get_db()
    try:
        # Each item is (label, status, detail).
        # status: "OK" | "WARN" | "MISSING" | "INVALID" | "INFO"
        # "INFO" entries are displayed but do not affect the exit code.
        checks: list[tuple[str, str, str]] = []

        # --- Target account --------------------------------------------------
        # An unknown --account is an outright error; a missing active account
        # is reported as a check so the rest of the report still shows.
        if account_name is not None:
            account: AccountRecord | None = _resolve_target_account(conn, account_name)
        else:
            account = get_active_account(conn)
        live = is_live_mode(conn)
        mode_label = "LIVE" if live else "PRACTICE"
        account_label = "active account" if account_name is None else "account"
        # Hints for fixing a key name --account when it isn't the active one.
        hint_suffix = f" --account {account_name}" if account_name is not None else ""

        if account is None:
            all_cfg: dict[str, str] = {}
            checks.append(
                (
                    account_label,
                    "MISSING",
                    "run: frmj account add NAME  then  frmj account use NAME",
                )
            )
        else:
            all_cfg = dict(get_all_account_config(conn, account.name))
            acct_type = "practice" if account.is_practice else "live"
            checks.append(
                (
                    account_label,
                    "OK",
                    f"{account.name}  (Oanda {acct_type} account {account.oanda_id})",
                )
            )

        # --- Token for the account ------------------------------------------
        if account is not None:
            env_type = "practice" if account.is_practice else "live"
            # Detect token source in same priority order as get_token.
            if account.is_practice and os.environ.get("OANDA_API_TOKEN_PRACTICE"):
                checks.append(("token", "OK", "OANDA_API_TOKEN_PRACTICE env var"))
            elif os.environ.get("OANDA_API_TOKEN"):
                label = "OANDA_API_TOKEN env var"
                if account.is_practice:
                    label += " (legacy practice fallback)"
                checks.append(("token", "OK", label))
            elif get_token(account.is_practice):
                checks.append(("token", "OK", "OS keychain"))
            else:
                checks.append(
                    (
                        "token",
                        "MISSING",
                        f"run: frmj account set-token {env_type}",
                    )
                )

        # --- Execution mode --------------------------------------------------
        checks.append(("mode", "INFO", mode_label))

        # --- max_open_trades (required for trading) --------------------------
        mot = all_cfg.get("max_open_trades")
        if mot is None:
            checks.append(
                (
                    "max_open_trades",
                    "WARN",
                    "not set — trading disabled; run: "
                    f"frmj config set max_open_trades <N>{hint_suffix}",
                )
            )
        else:
            try:
                if int(mot) <= 0:
                    raise ValueError
                checks.append(("max_open_trades", "OK", mot))
            except ValueError:
                checks.append(
                    (
                        "max_open_trades",
                        "INVALID",
                        f"{mot!r} — must be a positive integer",
                    )
                )

        # --- risk_strategy ---------------------------------------------------
        rs_val = all_cfg.get("risk_strategy")
        valid_strategies = [s.value for s in RiskStrategy]
        if rs_val is None:
            checks.append(
                ("risk_strategy", "OK", "remaining_margin_fraction (default)")
            )
        elif rs_val in valid_strategies:
            checks.append(("risk_strategy", "OK", rs_val))
        else:
            checks.append(
                (
                    "risk_strategy",
                    "INVALID",
                    f"{rs_val!r} — must be one of: {', '.join(valid_strategies)}",
                )
            )

        # --- percent_of_equity (required when strategy=percent_of_equity) ----
        effective_strategy = rs_val or "remaining_margin_fraction"
        if effective_strategy == RiskStrategy.PERCENT_OF_EQUITY.value:
            poe = all_cfg.get("percent_of_equity")
            if poe is None:
                checks.append(
                    (
                        "percent_of_equity",
                        "MISSING",
                        "required when risk_strategy = percent_of_equity",
                    )
                )
            else:
                checks.append(("percent_of_equity", "OK", poe))

        # --- fixed_dollar (required when strategy=fixed_dollar) --------------
        if effective_strategy == RiskStrategy.FIXED_DOLLAR.value:
            fd = all_cfg.get("fixed_dollar")
            if fd is None:
                checks.append(
                    (
                        "fixed_dollar",
                        "MISSING",
                        "required when risk_strategy = fixed_dollar",
                    )
                )
            else:
                checks.append(("fixed_dollar", "OK", fd))

        # --- blocking_mode ---------------------------------------------------
        bm_val = all_cfg.get("blocking_mode")
        valid_modes = [m.value for m in BlockingMode]
        if bm_val is None:
            checks.append(("blocking_mode", "OK", "hard_block (default)"))
        elif bm_val in valid_modes:
            checks.append(("blocking_mode", "OK", bm_val))
        else:
            checks.append(
                (
                    "blocking_mode",
                    "INVALID",
                    f"{bm_val!r} — must be one of: {', '.join(valid_modes)}",
                )
            )

        # --- correlation_blocking_mode ----------------------------------------
        cbm_val = all_cfg.get("correlation_blocking_mode")
        if cbm_val is None:
            checks.append(("correlation_blocking_mode", "OK", "warning_only (default)"))
        elif cbm_val in valid_modes:
            checks.append(("correlation_blocking_mode", "OK", cbm_val))
        else:
            checks.append(
                (
                    "correlation_blocking_mode",
                    "INVALID",
                    f"{cbm_val!r} — must be one of: {', '.join(valid_modes)}",
                )
            )

        # --- scale_in --------------------------------------------------------
        si_val = all_cfg.get("scale_in")
        valid_si = [p.value for p in ScaleInPolicy]
        if si_val is None:
            checks.append(("scale_in", "OK", "never (default)"))
        elif si_val in valid_si:
            checks.append(("scale_in", "OK", si_val))
        else:
            checks.append(
                (
                    "scale_in",
                    "INVALID",
                    f"{si_val!r} — must be one of: {', '.join(valid_si)}",
                )
            )

        # --- safety_reserve_pct ----------------------------------------------
        sr_val = all_cfg.get("safety_reserve_pct")
        if sr_val is None:
            checks.append(("safety_reserve_pct", "OK", "0 (default)"))
        else:
            try:
                sr = Decimal(sr_val)
                if not (0 <= sr < 1):
                    raise ValueError
                checks.append(("safety_reserve_pct", "OK", sr_val))
            except Exception:
                checks.append(
                    (
                        "safety_reserve_pct",
                        "INVALID",
                        f"{sr_val!r} — must be a decimal in [0, 1)",
                    )
                )

        # --- atr_period --------------------------------------------------------
        ap_val = all_cfg.get("atr_period")
        if ap_val is None:
            checks.append(("atr_period", "OK", f"{DEFAULT_ATR_PERIOD} (default)"))
        else:
            try:
                if int(ap_val) < 1:
                    raise ValueError
                checks.append(("atr_period", "OK", ap_val))
            except ValueError:
                checks.append(
                    (
                        "atr_period",
                        "INVALID",
                        f"{ap_val!r} — must be a positive integer",
                    )
                )

        # --- sl_atr_multiple ---------------------------------------------------
        sm_val = all_cfg.get("sl_atr_multiple")
        if sm_val is None:
            checks.append(
                ("sl_atr_multiple", "OK", f"{DEFAULT_SL_ATR_MULTIPLE} (default)")
            )
        else:
            try:
                sm = Decimal(sm_val)
                if not (sm.is_finite() and sm > 0):
                    raise ValueError
                checks.append(("sl_atr_multiple", "OK", sm_val))
            except Exception:
                checks.append(
                    (
                        "sl_atr_multiple",
                        "INVALID",
                        f"{sm_val!r} — must be a positive number",
                    )
                )

        # --- Connectivity (opt-in) -------------------------------------------
        if connectivity:
            if account is not None and get_token(account.is_practice):
                try:
                    client = get_client(conn, account.name)
                    summary = client.get_account_summary()
                    checks.append(
                        (
                            "connectivity",
                            "OK",
                            f"Oanda responded — NAV ${summary.nav:,.2f}",
                        )
                    )
                except Exception as exc:
                    checks.append(
                        ("connectivity", "INVALID", f"API call failed: {exc}")
                    )
            else:
                checks.append(
                    (
                        "connectivity",
                        "WARN",
                        f"skipped — {account_label} or token not configured",
                    )
                )

    finally:
        conn.close()

    # --- Render --------------------------------------------------------------
    typer.echo("Configuration check")
    typer.echo("─" * 56)

    label_w = max(len(c[0]) for c in checks)
    status_w = max(len(c[1]) for c in checks)

    for label, status, detail in checks:
        if status == "OK":
            badge = typer.style(f"{status:<{status_w}}", fg=typer.colors.GREEN)
        elif status == "WARN":
            badge = typer.style(f"{status:<{status_w}}", fg=typer.colors.YELLOW)
        elif status == "INFO":
            badge = f"{status:<{status_w}}"  # neutral — mode display
        else:  # MISSING / INVALID
            badge = typer.style(f"{status:<{status_w}}", fg=typer.colors.RED)

        typer.echo(f"  {label:<{label_w}}  {badge}  {detail}")

    errors = [c for c in checks if c[1] in ("MISSING", "INVALID")]
    warnings = [c for c in checks if c[1] == "WARN"]
    # INFO entries are informational only and do not affect exit code.

    typer.echo("")
    if not errors and not warnings:
        typer.echo(typer.style("All checks passed.", fg=typer.colors.GREEN))
    elif not errors:
        typer.echo(
            f"{len(warnings)} warning(s). Configuration is usable but incomplete."
        )
    else:
        count = len(errors) + len(warnings)
        typer.echo(typer.style(f"{count} issue(s) found.", fg=typer.colors.RED))
        raise typer.Exit(1)


def _print_token_status(account: AccountRecord) -> None:
    """Print the token status for *account*'s environment.

    Called by ``config_get`` when showing all values.  Token values are never
    printed — only the source.
    """
    env_label = "practice" if account.is_practice else "live"

    # Detect source in the same priority order as get_token.
    if account.is_practice and os.environ.get("OANDA_API_TOKEN_PRACTICE"):
        source = "set via OANDA_API_TOKEN_PRACTICE env var"
    elif os.environ.get("OANDA_API_TOKEN"):
        suffix = " (legacy practice fallback)" if account.is_practice else ""
        source = f"set via OANDA_API_TOKEN env var{suffix}"
    elif get_token(account.is_practice) is not None:
        source = "stored in OS keychain"
    else:
        source = f"not set — run: frmj account set-token {env_label}"

    typer.echo(f"API token ({env_label:<8}) =  ({source})")
