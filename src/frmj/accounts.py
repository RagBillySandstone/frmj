"""
Named account management for FRoMaJ.

Stores Oanda account profiles in the ``accounts`` SQLite table. Each profile
has a user-chosen name, the raw Oanda account ID, and a flag that determines
which Oanda API environment (practice or live) the profile connects to.

Two scalar settings live in the existing ``config`` table:
* ``active_account``  — name of the currently selected profile.
* ``live_mode``       — "true" when live order execution is permitted;
                        "false" (the default) keeps the system in safe
                        read-only mode even for live accounts.

Token storage (OS keychain) is intentionally kept in ``app.py``, the one
module allowed to perform external I/O. This module is pure SQLite CRUD.
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass

# ---------------------------------------------------------------------------
# Data model
# ---------------------------------------------------------------------------

_ACTIVE_ACCOUNT_KEY: str = "active_account"
_LIVE_MODE_KEY: str = "live_mode"


@dataclass(slots=True)
class AccountRecord:
    """An Oanda account profile stored in the local database."""

    # User-chosen name, e.g. "funded", "practice", "live".
    name: str
    # Raw Oanda account ID, e.g. "101-001-12345678-001".
    oanda_id: str
    # True → connects to practice.oanda.com; False → fxtrade.oanda.com.
    is_practice: bool
    # ISO-8601 wall-clock time recorded at insertion.
    created_at: str


# ---------------------------------------------------------------------------
# Account CRUD
# ---------------------------------------------------------------------------


def add_account(
    conn: sqlite3.Connection,
    name: str,
    oanda_id: str,
    is_practice: bool,
) -> None:
    """
    Insert a new account profile.

    Raises ``sqlite3.IntegrityError`` if *name* already exists (PRIMARY KEY
    constraint). The caller is responsible for showing a user-friendly error.
    """
    conn.execute(
        "INSERT INTO accounts (name, oanda_id, is_practice) VALUES (?, ?, ?)",
        (name, oanda_id, 1 if is_practice else 0),
    )
    conn.commit()


def list_accounts(conn: sqlite3.Connection) -> list[AccountRecord]:
    """Return all account profiles ordered alphabetically by name."""
    rows = conn.execute(
        "SELECT name, oanda_id, is_practice, created_at FROM accounts ORDER BY name"
    ).fetchall()
    return [
        AccountRecord(
            name=row[0],
            oanda_id=row[1],
            is_practice=bool(row[2]),
            created_at=row[3],
        )
        for row in rows
    ]


def get_account(conn: sqlite3.Connection, name: str) -> AccountRecord | None:
    """Return the profile for *name*, or ``None`` if it does not exist."""
    row = conn.execute(
        "SELECT name, oanda_id, is_practice, created_at FROM accounts WHERE name = ?",
        (name,),
    ).fetchone()
    if row is None:
        return None
    return AccountRecord(
        name=row[0],
        oanda_id=row[1],
        is_practice=bool(row[2]),
        created_at=row[3],
    )


def remove_account(conn: sqlite3.Connection, name: str) -> bool:
    """
    Delete the profile for *name*.

    Returns ``True`` when a row was removed, ``False`` when *name* was not
    found.  Does not remove the OS keychain entry — the caller should call
    ``delete_account_token`` from ``app`` if a clean removal is wanted.
    """
    cursor = conn.execute("DELETE FROM accounts WHERE name = ?", (name,))
    conn.commit()
    return cursor.rowcount > 0


def get_account_count(conn: sqlite3.Connection) -> int:
    """Return the total number of account profiles stored."""
    row = conn.execute("SELECT COUNT(*) FROM accounts").fetchone()
    return row[0]


# ---------------------------------------------------------------------------
# Active account helpers
# ---------------------------------------------------------------------------


def get_active_account_name(conn: sqlite3.Connection) -> str | None:
    """Return the name stored in config under ``active_account``, or ``None``."""
    row = conn.execute(
        "SELECT value FROM config WHERE key = ?", (_ACTIVE_ACCOUNT_KEY,)
    ).fetchone()
    return row[0] if row else None


def get_active_account(conn: sqlite3.Connection) -> AccountRecord | None:
    """
    Return the currently active ``AccountRecord``, or ``None``.

    Returns ``None`` both when no active account has been selected and when
    the recorded name no longer exists in the ``accounts`` table.
    """
    name = get_active_account_name(conn)
    if name is None:
        return None
    return get_account(conn, name)


def set_active_account(conn: sqlite3.Connection, name: str) -> None:
    """
    Write *name* as the active account in the config table.

    Does not validate that *name* actually exists in the ``accounts`` table.
    Callers should verify before calling.
    """
    conn.execute(
        "REPLACE INTO config (key, value) VALUES (?, ?)",
        (_ACTIVE_ACCOUNT_KEY, name),
    )
    conn.commit()


# ---------------------------------------------------------------------------
# Live mode helpers
# ---------------------------------------------------------------------------


def is_live_mode(conn: sqlite3.Connection) -> bool:
    """
    Return ``True`` when live trading mode is enabled.

    Defaults to ``False`` (safe / practice mode) when the key is absent —
    new installations start in the safest state without requiring explicit
    configuration.
    """
    row = conn.execute(
        "SELECT value FROM config WHERE key = ?", (_LIVE_MODE_KEY,)
    ).fetchone()
    if row is None:
        return False
    return row[0].lower() in ("true", "1", "yes")


def set_live_mode(conn: sqlite3.Connection, *, enabled: bool) -> None:
    """Write the live mode flag to config. ``enabled=False`` restores practice mode."""
    conn.execute(
        "REPLACE INTO config (key, value) VALUES (?, ?)",
        (_LIVE_MODE_KEY, "true" if enabled else "false"),
    )
    conn.commit()
