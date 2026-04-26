"""
Application-level wiring: database factory, client factory, config helpers.

This module is the only place in the codebase that reads environment variables
or touches the filesystem.  The domain layer (risk, sizing, pricing) and the
execution layer (oanda, sync) are all pure functions / classes that accept
their dependencies as arguments; this module assembles those arguments from
the environment and hands them to the CLI commands.

Environment variables
---------------------
``OANDA_API_TOKEN``   (required) — Oanda personal access token.
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

import os
import sqlite3
from decimal import Decimal
from pathlib import Path

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
    token = os.environ.get("OANDA_API_TOKEN")
    if not token:
        raise RuntimeError(
            "OANDA_API_TOKEN environment variable is not set. "
            "Set it to your Oanda personal access token."
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
