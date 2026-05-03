"""
Application-level wiring: database factory, client factory, config helpers.

This module is the only place in the codebase that reads environment variables,
touches the filesystem, or accesses the OS keychain.  The domain layer (risk,
sizing, pricing) and the execution layer (oanda, sync) are all pure functions /
classes that accept their dependencies as arguments; this module assembles those
arguments from the environment and hands them to the CLI commands.

API token storage
-----------------
The Oanda API token is resolved in this priority order:

1. ``OANDA_API_TOKEN`` environment variable — checked first so CI/container
   environments and existing shell-profile setups work with no changes.
2. OS keychain — set once with ``frmj config set-token``; read automatically
   on every subsequent invocation with no prompt.  Backed by GNOME Keyring /
   KWallet on Linux, Keychain on macOS, Credential Locker on Windows.

Store the token:    ``frmj config set-token``
Remove the token:   ``frmj config unset-token``

Other environment variables
---------------------------
``FRMJ_DB_PATH``      (optional) — path to the SQLite file; defaults to
                       ``~/.local/share/frmj/frmj.db``.

Config table keys
-----------------
``account_id``         Oanda account ID (required before any live call).
``practice_mode``      "true" or "false"; defaults to "true" if absent.
``max_open_trades``    Integer; required for the risk model.
``risk_strategy``      One of the ``RiskStrategy`` enum values; defaults to
                       "remaining_margin_fraction".
``blocking_mode``      "hard_block" or "warning_only"; defaults to "hard_block".
``scale_in``           "never", "warn", or "allow"; defaults to "never".
``safety_reserve_pct`` Decimal in [0, 1]; defaults to "0".
``percent_of_equity``  Required when risk_strategy = "percent_of_equity".
``fixed_dollar``       Required when risk_strategy = "fixed_dollar".
"""

from __future__ import annotations

import json
import os
import sqlite3
from decimal import Decimal
from pathlib import Path

import keyring
import keyring.errors

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

_DEFAULT_DB_PATH: Path = (
    Path.home() / ".local" / "share" / "frmj" / "frmj.db"
)

# Path for the draft plan saved when an order attempt fails mid-flow.
_DRAFT_PLAN_PATH: Path = (
    Path.home() / ".local" / "share" / "frmj" / "saved_plan.json"
)

# Keyring entry coordinates — single source of truth used by get/store/delete.
_KEYRING_SERVICE: str = "frmj"
_KEYRING_TOKEN_KEY: str = "oanda_api_token"


# ---------------------------------------------------------------------------
# Database factory
# ---------------------------------------------------------------------------


def get_db(path: Path | None = None) -> sqlite3.Connection:
    """
    Open (or create) the FRoMaJ SQLite database and apply the schema.

    If *path* is ``None`` the path is resolved in this priority order:
    1. ``FRMJ_DB_PATH`` environment variable.
    2. ``~/.local/share/frmj/frmj.db`` (XDG data home convention).

    The parent directory is created if it does not exist.  ``ensure_schema``
    is called on every open so startup is always idempotent and schema
    upgrades are automatic.
    """
    if path is None:
        env_path = os.environ.get("FRMJ_DB_PATH")
        path = Path(env_path) if env_path else _DEFAULT_DB_PATH
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(path))
    conn.row_factory = sqlite3.Row
    ensure_schema(conn)
    return conn


# ---------------------------------------------------------------------------
# Config helpers
# ---------------------------------------------------------------------------


def get_config(conn: sqlite3.Connection, key: str) -> str | None:
    """Return the value for *key* from the config table, or ``None``."""
    row = conn.execute(
        "SELECT value FROM config WHERE key = ?", (key,)
    ).fetchone()
    return row[0] if row else None


def set_config(conn: sqlite3.Connection, key: str, value: str) -> None:
    """Upsert *key* = *value* in the config table."""
    conn.execute(
        "REPLACE INTO config (key, value) VALUES (?, ?)", (key, value)
    )
    conn.commit()


def delete_config(conn: sqlite3.Connection, key: str) -> bool:
    """Delete *key* from the config table.  Returns True if a row was removed."""
    cursor = conn.execute("DELETE FROM config WHERE key = ?", (key,))
    conn.commit()
    return cursor.rowcount > 0


def get_all_config(conn: sqlite3.Connection) -> list[tuple[str, str]]:
    """Return all config rows as ``(key, value)`` pairs sorted by key."""
    rows = conn.execute(
        "SELECT key, value FROM config ORDER BY key"
    ).fetchall()
    return [(row[0], row[1]) for row in rows]


# ---------------------------------------------------------------------------
# Token helpers (OS keyring)
# ---------------------------------------------------------------------------


def get_token() -> str | None:
    """
    Resolve the Oanda API token using the priority order:

    1. ``OANDA_API_TOKEN`` environment variable — preserved for CI / containers
       and so a locally-exported variable always overrides a stale keyring entry.
    2. OS keychain — set once with ``frmj config set-token``.

    Returns ``None`` when neither source has the token.  Also returns ``None``
    (rather than raising) when the keyring backend is unavailable so that the
    caller (``get_client``) can surface the same unified missing-token message
    regardless of platform.
    """
    env_token = os.environ.get("OANDA_API_TOKEN")
    if env_token:
        return env_token

    # Treat any keyring error as "not available" — the missing-token error
    # from get_client is more actionable than a raw keyring exception.
    try:
        return keyring.get_password(_KEYRING_SERVICE, _KEYRING_TOKEN_KEY)
    except keyring.errors.KeyringError:
        return None


def store_token(token: str) -> None:
    """
    Save *token* to the OS keychain under the ``"frmj"`` service.

    Raises ``RuntimeError`` when no keyring backend is available (headless Linux
    without a Secret Service daemon running).  The CLI layer catches this and
    suggests the env-var alternative.
    """
    try:
        keyring.set_password(_KEYRING_SERVICE, _KEYRING_TOKEN_KEY, token)
    except keyring.errors.NoKeyringError as exc:
        raise RuntimeError(
            "No system keyring is available on this machine. "
            "Set the OANDA_API_TOKEN environment variable instead."
        ) from exc


def delete_token() -> None:
    """
    Remove the stored token from the OS keychain, if present.

    A no-op when the token was never stored — ``PasswordDeleteError`` is
    swallowed intentionally so the command is idempotent.
    Raises ``RuntimeError`` when no keyring backend is available.
    """
    try:
        keyring.delete_password(_KEYRING_SERVICE, _KEYRING_TOKEN_KEY)
    except keyring.errors.NoKeyringError as exc:
        raise RuntimeError(
            "No system keyring is available on this machine. "
            "Set the OANDA_API_TOKEN environment variable instead."
        ) from exc
    except keyring.errors.PasswordDeleteError:
        # Token was never stored — not an error from the user's perspective.
        pass


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
    Build an ``OandaClient`` from environment variables and config table.

    Raises ``RuntimeError`` with a clear message when a required value is
    missing so the CLI can surface it as a user-facing error rather than a
    traceback.
    """
    token = get_token()
    if not token:
        raise RuntimeError(
            "No API token found. Store it with:\n"
            "  frmj config set-token\n"
            "Or set the OANDA_API_TOKEN environment variable."
        )

    account_id = get_config(conn, "account_id")
    if not account_id:
        raise RuntimeError(
            "account_id is not configured. "
            "Run: frmj config set account_id <YOUR_OANDA_ACCOUNT_ID>"
        )

    practice_str = get_config(conn, "practice_mode") or "true"
    practice = practice_str.lower() in ("true", "1", "yes")

    return OandaClient(token=token, account_id=account_id, practice=practice)


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
