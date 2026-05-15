"""
Application-level wiring: database factory, client factory, config helpers.

This module is the only place in the codebase that reads environment variables,
touches the filesystem, or accesses the OS keychain.  The domain layer (risk,
sizing, pricing) and the execution layer (oanda, sync) are all pure functions /
classes that accept their dependencies as arguments; this module assembles those
arguments from the environment and hands them to the CLI commands.

API token storage (per-account)
---------------------------------
Each named account stores its token in the OS keychain under the key
``oanda_token_{account_name}`` (service ``frmj``).  The resolution order for
``get_account_token(name, is_practice)`` is:

  1. ``FRMJ_TOKEN_{NAME_UPPER}`` environment variable (new format).
  2. ``oanda_token_{name}`` OS keychain entry (new format).
  3. Legacy practice fallbacks (when ``is_practice=True``):
       a. ``OANDA_API_TOKEN_PRACTICE`` env var.
       b. ``oanda_api_token_practice`` OS keychain.
       c. ``OANDA_API_TOKEN`` env var.
       d. ``oanda_api_token`` OS keychain.
  4. Legacy live fallbacks (when ``is_practice=False``):
       a. ``OANDA_API_TOKEN`` env var.
       b. ``oanda_api_token_live`` OS keychain.
       c. ``oanda_api_token`` OS keychain.

Legacy fallbacks exist solely to ease migration from v1 installations where
tokens were stored globally rather than per-account.

API token storage (legacy — kept for migration)
------------------------------------------------
``get_token``, ``store_token``, and ``delete_token`` continue to work on the
old global keyring keys so that the migration path can copy them to
per-account keys without requiring manual re-entry.

Other environment variables
---------------------------
``FRMJ_DB_PATH``      (optional) — path to the SQLite file; defaults to
                       ``~/.local/share/frmj/frmj.db`` on Linux,
                       ``~/Library/Application Support/frmj/frmj.db`` on
                       macOS, and ``%APPDATA%\\frmj\\frmj.db`` on Windows.
                       On macOS and Windows, if the legacy XDG-style path
                       already contains ``frmj.db`` it is used as-is to
                       preserve backward compatibility.

Config table keys (current)
----------------------------
``active_account``   Name of the active account profile.
``live_mode``        "true" when live order execution is permitted.
``max_open_trades``  Integer; required for the risk model.
``risk_strategy``    One of the ``RiskStrategy`` enum values.
``blocking_mode``    "hard_block" or "warning_only".
``scale_in``         "never", "warn", or "allow".
``safety_reserve_pct``  Decimal in [0, 1].
``percent_of_equity``   Required when risk_strategy = percent_of_equity.
``fixed_dollar``        Required when risk_strategy = fixed_dollar.
"""

from __future__ import annotations

import json
import os
import sqlite3
import sys
from decimal import Decimal
from pathlib import Path

import keyring
import keyring.errors

from frmj.accounts import (
    AccountRecord,
    get_account_count,
    get_active_account,
    add_account,
    set_active_account,
    set_live_mode,
)
from frmj.domain.risk import (
    BlockingMode,
    RiskConfig,
    RiskStrategy,
    ScaleInPolicy,
)
from frmj.execution.oanda import OandaClient
from frmj.persistence.schema import ensure_schema

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------


def _resolve_default_data_dir() -> Path:
    """
    Return the platform-appropriate data directory for FRoMaJ.

    Resolution order:
    - Linux: ``~/.local/share/frmj`` (XDG base-dir convention).
    - macOS (new install): ``~/Library/Application Support/frmj``.
    - macOS (existing install): if ``frmj.db`` already exists at the legacy
      XDG-style path (``~/.local/share/frmj/frmj.db``) created by an earlier
      version of frmj, that directory is returned unchanged.
    - Windows (new install): ``%APPDATA%\\frmj`` (i.e.
      ``C:\\Users\\<user>\\AppData\\Roaming\\frmj``).
    - Windows (existing install): same legacy-path fallback as macOS.
    """
    xdg_dir: Path = Path.home() / ".local" / "share" / "frmj"

    if sys.platform == "darwin":
        # Keep the legacy XDG path if a database is already there.
        if (xdg_dir / "frmj.db").exists():
            return xdg_dir
        return Path.home() / "Library" / "Application Support" / "frmj"

    if sys.platform == "win32":
        # Keep the legacy XDG path if a database is already there.
        if (xdg_dir / "frmj.db").exists():
            return xdg_dir
        return Path.home() / "AppData" / "Roaming" / "frmj"

    # Linux and any other POSIX platform.
    return xdg_dir


_DATA_DIR: Path = _resolve_default_data_dir()
_DEFAULT_DB_PATH: Path = _DATA_DIR / "frmj.db"

# Path for the draft plan saved when an order attempt fails mid-flow.
_DRAFT_PLAN_PATH: Path = _DATA_DIR / "saved_plan.json"

# Keyring entry coordinates — single source of truth used by get/store/delete.
_KEYRING_SERVICE: str = "frmj"
_KEYRING_TOKEN_KEY_LIVE: str = "oanda_api_token_live"
_KEYRING_TOKEN_KEY_PRACTICE: str = "oanda_api_token_practice"
# Legacy key written by older versions of frmj; read as a fallback, never written.
_KEYRING_TOKEN_KEY_LEGACY: str = "oanda_api_token"


# ---------------------------------------------------------------------------
# Database factory
# ---------------------------------------------------------------------------


def get_db(path: Path | None = None) -> sqlite3.Connection:
    """
    Open (or create) the FRoMaJ SQLite database and apply the schema.

    If *path* is ``None`` the path is resolved in this priority order:
    1. ``FRMJ_DB_PATH`` environment variable.
    2. Platform-appropriate default via ``_resolve_default_data_dir()``
       (see that function's docstring for the full resolution logic).

    The parent directory is created if it does not exist.  ``ensure_schema``
    is called on every open so startup is always idempotent and schema
    upgrades are automatic.  ``migrate_v1_accounts`` runs immediately after
    to silently convert any old-style flat config into named account profiles
    — a no-op when the accounts table is already populated.
    """
    if path is None:
        env_path = os.environ.get("FRMJ_DB_PATH")
        path = Path(env_path) if env_path else _DEFAULT_DB_PATH
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(path))
    conn.row_factory = sqlite3.Row
    ensure_schema(conn)
    migrate_v1_accounts(conn)
    return conn


# ---------------------------------------------------------------------------
# Config helpers
# ---------------------------------------------------------------------------


def get_config(conn: sqlite3.Connection, key: str) -> str | None:
    """Return the value for *key* from the config table, or ``None``."""
    row = conn.execute("SELECT value FROM config WHERE key = ?", (key,)).fetchone()
    return row[0] if row else None


def set_config(conn: sqlite3.Connection, key: str, value: str) -> None:
    """Upsert *key* = *value* in the config table."""
    conn.execute("REPLACE INTO config (key, value) VALUES (?, ?)", (key, value))
    conn.commit()


def delete_config(conn: sqlite3.Connection, key: str) -> bool:
    """Delete *key* from the config table.  Returns True if a row was removed."""
    cursor = conn.execute("DELETE FROM config WHERE key = ?", (key,))
    conn.commit()
    return cursor.rowcount > 0


def get_all_config(conn: sqlite3.Connection) -> list[tuple[str, str]]:
    """Return all config rows as ``(key, value)`` pairs sorted by key."""
    rows = conn.execute("SELECT key, value FROM config ORDER BY key").fetchall()
    return [(row[0], row[1]) for row in rows]


# ---------------------------------------------------------------------------
# Token helpers (OS keyring)
# ---------------------------------------------------------------------------


def get_token(practice: bool = False) -> str | None:
    """
    Resolve the Oanda API token for *practice* or live mode.

    Practice mode priority:
      1. ``OANDA_API_TOKEN_PRACTICE`` env var.
      2. ``oanda_api_token_practice`` OS keychain.
      3. ``OANDA_API_TOKEN`` env var (legacy fallback — works if only one token
         was ever configured).
      4. ``oanda_api_token`` OS keychain (legacy fallback).

    Live mode priority:
      1. ``OANDA_API_TOKEN`` env var.
      2. ``oanda_api_token_live`` OS keychain.
      3. ``oanda_api_token`` OS keychain (legacy fallback).

    Returns ``None`` when no source has the token.  Keyring errors are treated
    as "not available" so the caller can surface a unified missing-token message.
    """

    def _keyring_get(key: str) -> str | None:
        try:
            return keyring.get_password(_KEYRING_SERVICE, key)
        except keyring.errors.KeyringError:
            return None

    if practice:
        return (
            os.environ.get("OANDA_API_TOKEN_PRACTICE")
            or _keyring_get(_KEYRING_TOKEN_KEY_PRACTICE)
            or os.environ.get("OANDA_API_TOKEN")  # legacy
            or _keyring_get(_KEYRING_TOKEN_KEY_LEGACY)  # legacy
        ) or None
    else:
        return (
            os.environ.get("OANDA_API_TOKEN")
            or _keyring_get(_KEYRING_TOKEN_KEY_LIVE)
            or _keyring_get(_KEYRING_TOKEN_KEY_LEGACY)  # legacy
        ) or None


def store_token(token: str, practice: bool = False) -> None:
    """
    Save *token* to the OS keychain for the given mode.

    ``practice=False`` writes to ``oanda_api_token_live``;
    ``practice=True`` writes to ``oanda_api_token_practice``.

    Raises ``RuntimeError`` when no keyring backend is available (headless Linux
    without a Secret Service daemon running).  The CLI layer catches this and
    suggests the env-var alternative.
    """
    key = _KEYRING_TOKEN_KEY_PRACTICE if practice else _KEYRING_TOKEN_KEY_LIVE
    try:
        keyring.set_password(_KEYRING_SERVICE, key, token)
    except keyring.errors.NoKeyringError as exc:
        env_var = "OANDA_API_TOKEN_PRACTICE" if practice else "OANDA_API_TOKEN"
        raise RuntimeError(
            f"No system keyring is available on this machine. "
            f"Set the {env_var} environment variable instead."
        ) from exc


def delete_token(practice: bool = False) -> None:
    """
    Remove the stored token from the OS keychain for the given mode.

    ``practice=False`` deletes ``oanda_api_token_live``;
    ``practice=True`` deletes ``oanda_api_token_practice``.

    A no-op when the token was never stored — ``PasswordDeleteError`` is
    swallowed intentionally so the command is idempotent.
    Raises ``RuntimeError`` when no keyring backend is available.
    """
    key = _KEYRING_TOKEN_KEY_PRACTICE if practice else _KEYRING_TOKEN_KEY_LIVE
    try:
        keyring.delete_password(_KEYRING_SERVICE, key)
    except keyring.errors.NoKeyringError as exc:
        env_var = "OANDA_API_TOKEN_PRACTICE" if practice else "OANDA_API_TOKEN"
        raise RuntimeError(
            f"No system keyring is available on this machine. "
            f"Set the {env_var} environment variable instead."
        ) from exc
    except keyring.errors.PasswordDeleteError:
        # Token was never stored — not an error from the user's perspective.
        pass


# ---------------------------------------------------------------------------
# Per-account token helpers (OS keyring)
# ---------------------------------------------------------------------------


def _account_keyring_key(account_name: str) -> str:
    """Return the OS keychain key for *account_name*'s token."""
    return f"oanda_token_{account_name}"


def _account_env_var(account_name: str) -> str:
    """Return the env var name for *account_name*'s token override."""
    safe_name = account_name.upper().replace("-", "_")
    return f"FRMJ_TOKEN_{safe_name}"


def get_account_token(account_name: str, is_practice: bool = False) -> str | None:
    """
    Resolve the Oanda API token for the named account.

    Resolution order (first non-empty value wins):
    1. ``FRMJ_TOKEN_{NAME_UPPER}`` env var.
    2. ``oanda_token_{name}`` OS keychain (new per-account format).
    3. Legacy env vars / keychain keys based on *is_practice*, preserved so
       that v1 installations can be migrated without re-entering tokens.

    Returns ``None`` when no source has the token.
    """

    def _kr(key: str) -> str | None:
        try:
            return keyring.get_password(_KEYRING_SERVICE, key)
        except keyring.errors.KeyringError:
            return None

    # New-format lookup: account-namespaced env var or keyring key.
    token = os.environ.get(_account_env_var(account_name)) or _kr(
        _account_keyring_key(account_name)
    )
    if token:
        return token

    # Legacy fallbacks — match the v1 resolution order so migrating users
    # don't need to re-enter their token immediately after upgrading.
    if is_practice:
        token = (
            os.environ.get("OANDA_API_TOKEN_PRACTICE")
            or _kr(_KEYRING_TOKEN_KEY_PRACTICE)
            or os.environ.get("OANDA_API_TOKEN")
            or _kr(_KEYRING_TOKEN_KEY_LEGACY)
        )
    else:
        token = (
            os.environ.get("OANDA_API_TOKEN")
            or _kr(_KEYRING_TOKEN_KEY_LIVE)
            or _kr(_KEYRING_TOKEN_KEY_LEGACY)
        )

    return token or None


def store_account_token(account_name: str, token: str) -> None:
    """
    Save *token* for *account_name* in the OS keychain.

    Raises ``RuntimeError`` when no keyring backend is available.
    """
    try:
        keyring.set_password(
            _KEYRING_SERVICE, _account_keyring_key(account_name), token
        )
    except keyring.errors.NoKeyringError as exc:
        env_var = _account_env_var(account_name)
        raise RuntimeError(
            f"No system keyring is available on this machine. "
            f"Set the {env_var} environment variable instead."
        ) from exc


def delete_account_token(account_name: str) -> None:
    """
    Remove *account_name*'s token from the OS keychain.

    A no-op when the token was never stored.
    Raises ``RuntimeError`` when no keyring backend is available.
    """
    try:
        keyring.delete_password(_KEYRING_SERVICE, _account_keyring_key(account_name))
    except keyring.errors.NoKeyringError as exc:
        raise RuntimeError("No system keyring is available on this machine.") from exc
    except keyring.errors.PasswordDeleteError:
        # Token was never stored — not an error from the user's perspective.
        pass


def rename_account_token(old_name: str, new_name: str) -> None:
    """
    Move the OS keychain entry for *old_name* to *new_name*.

    Reads only the account-namespaced keychain key (``oanda_token_{name}``),
    not env-var or legacy fallback sources — those do not need migration
    because they are not keyed by account name.  When no account-namespaced
    entry exists the function is a no-op.

    Raises ``RuntimeError`` when no keyring backend is available.
    """
    try:
        token = keyring.get_password(_KEYRING_SERVICE, _account_keyring_key(old_name))
    except keyring.errors.NoKeyringError as exc:
        raise RuntimeError("No system keyring is available on this machine.") from exc
    except keyring.errors.KeyringError:
        # Any other keyring error — treat as "no entry to migrate".
        return

    if token is None:
        # No account-namespaced entry stored; nothing to migrate.
        return

    # Write under the new name first, then remove the old entry so the token
    # is never lost if the delete step fails.
    store_account_token(new_name, token)
    delete_account_token(old_name)


# ---------------------------------------------------------------------------
# V1 → V2 migration
# ---------------------------------------------------------------------------


def migrate_v1_accounts(conn: sqlite3.Connection) -> None:
    """
    Convert old flat config keys into the named-account system.

    Reads the legacy keys ``practice_account_id``, ``account_id``, and
    ``practice_mode`` from the config table.  If any exist *and* the accounts
    table is still empty, it creates one or two named profiles ("practice"
    and/or "live"), sets the active account, copies the live-mode flag, and
    removes the old keys.

    This function is idempotent: it is a no-op when the accounts table
    already has at least one row (migration already ran) or when no legacy
    keys are present (fresh install).
    """
    # Skip if accounts have already been configured.
    if get_account_count(conn) > 0:
        return

    practice_id = get_config(conn, "practice_account_id")
    live_id = get_config(conn, "account_id")

    # Nothing to migrate on a fresh install.
    if not practice_id and not live_id:
        return

    practice_mode_str = get_config(conn, "practice_mode") or "true"
    is_practice_active = practice_mode_str.lower() in ("true", "1", "yes")

    active_name: str | None = None

    if practice_id:
        add_account(conn, "practice", practice_id, is_practice=True)
        # Attempt to copy the v1 keyring token to the new per-account key.
        old_token = get_token(practice=True)
        if old_token:
            try:
                keyring.set_password(
                    _KEYRING_SERVICE,
                    _account_keyring_key("practice"),
                    old_token,
                )
            except keyring.errors.NoKeyringError:
                pass  # Token is in an env var; nothing to copy.
        if is_practice_active:
            active_name = "practice"

    if live_id:
        add_account(conn, "live", live_id, is_practice=False)
        old_token = get_token(practice=False)
        if old_token:
            try:
                keyring.set_password(
                    _KEYRING_SERVICE,
                    _account_keyring_key("live"),
                    old_token,
                )
            except keyring.errors.NoKeyringError:
                pass
        if not is_practice_active:
            active_name = "live"

    if active_name:
        set_active_account(conn, active_name)

    # Live mode = inverse of the old practice_mode flag.
    set_live_mode(conn, enabled=not is_practice_active)

    # Remove the now-superseded config keys.
    for old_key in ("account_id", "practice_account_id", "practice_mode"):
        delete_config(conn, old_key)


# ---------------------------------------------------------------------------
# Draft plan helpers (order-failure recovery)
# ---------------------------------------------------------------------------


def save_draft_plan(data: dict) -> Path:
    """Write *data* as JSON to the draft plan file and return the path.

    The file is created (or overwritten) at ``_DRAFT_PLAN_PATH``.  The parent
    directory is created if absent.
    """
    _DRAFT_PLAN_PATH.parent.mkdir(parents=True, exist_ok=True)
    _DRAFT_PLAN_PATH.write_text(json.dumps(data, indent=2))
    return _DRAFT_PLAN_PATH


def load_draft_plan() -> dict | None:
    """Return the saved draft plan as a dict, or ``None`` if no file exists.

    Returns ``None`` on any read/parse error so callers see a clean "no plan"
    state instead of a traceback.
    """
    if not _DRAFT_PLAN_PATH.exists():
        return None
    try:
        return json.loads(_DRAFT_PLAN_PATH.read_text())
    except Exception:
        return None


def clear_draft_plan() -> None:
    """Delete the draft plan file if it exists; no-op otherwise."""
    try:
        _DRAFT_PLAN_PATH.unlink()
    except FileNotFoundError:
        pass


# ---------------------------------------------------------------------------
# Client factory
# ---------------------------------------------------------------------------


def get_client(conn: sqlite3.Connection) -> OandaClient:
    """
    Build an ``OandaClient`` from the active account profile.

    Reads the active account name from config, looks up its profile in the
    ``accounts`` table, resolves the API token, and constructs an
    ``OandaClient`` pointed at the correct Oanda environment
    (practice.oanda.com vs fxtrade.oanda.com based on ``is_practice``).

    Raises ``RuntimeError`` with a clear message when a required value is
    missing so the CLI can surface it as a user-facing error rather than a
    traceback.
    """
    account: AccountRecord | None = get_active_account(conn)
    if account is None:
        raise RuntimeError(
            "No active account selected. Add an account with:\n"
            "  frmj account add NAME\n"
            "Then activate it with:\n"
            "  frmj account use NAME"
        )

    token = get_account_token(account.name, is_practice=account.is_practice)
    if not token:
        env_var = _account_env_var(account.name)
        raise RuntimeError(
            f"No API token found for account '{account.name}'. Store it with:\n"
            f"  frmj account set-token {account.name}\n"
            f"Or set the {env_var} environment variable."
        )

    return OandaClient(
        token=token,
        account_id=account.oanda_id,
        practice=account.is_practice,
    )


# ---------------------------------------------------------------------------
# Risk config factory
# ---------------------------------------------------------------------------


def get_risk_config(conn: sqlite3.Connection) -> RiskConfig:
    """
    Build a ``RiskConfig`` from the config table.

    ``max_open_trades`` is the only required key; all others have sensible
    defaults that match Stephen's stated preferences (REMAINING_MARGIN_FRACTION,
    HARD_BLOCK, NEVER scale-in, 0 reserve).

    Raises ``RuntimeError`` if ``max_open_trades`` is not configured.
    """
    max_trades_str = get_config(conn, "max_open_trades")
    if not max_trades_str:
        raise RuntimeError(
            "max_open_trades is not configured. "
            "Run: frmj config set max_open_trades <N>"
        )

    strategy_str = get_config(conn, "risk_strategy") or "remaining_margin_fraction"
    blocking_str = get_config(conn, "blocking_mode") or "hard_block"
    scale_in_str = get_config(conn, "scale_in") or "never"
    reserve_str = get_config(conn, "safety_reserve_pct") or "0"

    strategy = RiskStrategy(strategy_str)
    blocking_mode = BlockingMode(blocking_str)
    scale_in = ScaleInPolicy(scale_in_str)

    # Strategy-specific optional fields.
    pct_str = get_config(conn, "percent_of_equity")
    fixed_str = get_config(conn, "fixed_dollar")

    return RiskConfig(
        max_open_trades=int(max_trades_str),
        strategy=strategy,
        blocking_mode=blocking_mode,
        scale_in=scale_in,
        safety_reserve_pct=Decimal(reserve_str),
        percent_of_equity=Decimal(pct_str) if pct_str else None,
        fixed_dollar=Decimal(fixed_str) if fixed_str else None,
    )
