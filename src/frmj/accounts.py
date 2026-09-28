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

Named, reusable sets of accounts (for fanning a single trade out across
several profiles) live in the ``account_groups`` table — see the account
group helpers below.

Each account's own trading settings (``max_open_trades``, ``risk_strategy``,
...) live in the ``account_config`` table — see the account config helpers
below. Accounts don't share settings: a key an account hasn't set uses its
built-in default.

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

#: Settings each account keeps for itself in ``account_config``, set with
#: ``frmj config set``. Account identity and mode are managed via
#: ``frmj account`` and ``frmj mode`` and are intentionally not listed.
ACCOUNT_CONFIG_KEYS: frozenset[str] = frozenset(
    {
        "atr_period",
        "blocking_mode",
        "correlation_blocking_mode",
        "fixed_dollar",
        "max_open_trades",
        "percent_of_equity",
        "risk_strategy",
        "safety_reserve_pct",
        "scale_in",
        "sl_atr_multiple",
    }
)


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

    Also removes *name* from every account group and deletes its
    ``account_config`` rows, in the same transaction: both tables reference
    ``accounts(name)`` with no ``ON DELETE`` action, so the profile row can't
    be deleted while they remain. Callers wanting to report the affected
    groups should read them first with ``list_groups_for_account``.
    """
    # Dependent rows first, so no foreign key points at a missing row.
    conn.execute("DELETE FROM account_groups WHERE account_name = ?", (name,))
    conn.execute("DELETE FROM account_config WHERE account_name = ?", (name,))
    cursor = conn.execute("DELETE FROM accounts WHERE name = ?", (name,))
    if cursor.rowcount == 0:
        # Nothing to remove — undo the (necessarily empty) dependent deletes.
        conn.rollback()
        return False
    conn.commit()
    return True


def get_account_count(conn: sqlite3.Connection) -> int:
    """Return the total number of account profiles stored."""
    row = conn.execute("SELECT COUNT(*) FROM accounts").fetchone()
    return row[0]


def rename_account(conn: sqlite3.Connection, old_name: str, new_name: str) -> bool:
    """
    Rename the profile *old_name* to *new_name*.

    Updates the ``accounts`` table and, in the same transaction, the
    account's group memberships, its ``account_config`` rows, and — when
    *old_name* is the active account —
    the ``active_account`` config key, so nothing is left pointing at the old
    name.

    Returns ``True`` when the rename succeeded, ``False`` when *old_name* was
    not found.  Raises ``sqlite3.IntegrityError`` when *new_name* already
    exists (PRIMARY KEY constraint).  The caller is responsible for converting
    that to a user-visible error.

    Does **not** touch the OS keychain — callers that need to migrate the
    stored token should call ``rename_account_token`` from ``app.py``.
    """
    # Read current active name before modifying the accounts table so all
    # writes can be committed atomically.
    active_name = get_active_account_name(conn)

    # account_groups and account_config reference accounts(name) with no ON
    # UPDATE action, so whichever table is updated first briefly violates the
    # foreign key. Defer the check to COMMIT, by which point both agree.
    # The pragma resets itself when the transaction ends.
    conn.execute("PRAGMA defer_foreign_keys = ON")
    cursor = conn.execute(
        "UPDATE accounts SET name = ? WHERE name = ?",
        (new_name, old_name),
    )
    if cursor.rowcount == 0:
        # old_name did not exist — roll back any implicit transaction state.
        conn.rollback()
        return False
    conn.execute(
        "UPDATE account_groups SET account_name = ? WHERE account_name = ?",
        (new_name, old_name),
    )
    conn.execute(
        "UPDATE account_config SET account_name = ? WHERE account_name = ?",
        (new_name, old_name),
    )

    # Keep active_account config in sync when renaming the active profile.
    if active_name == old_name:
        conn.execute(
            "REPLACE INTO config (key, value) VALUES (?, ?)",
            (_ACTIVE_ACCOUNT_KEY, new_name),
        )

    conn.commit()
    return True


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


def resolve_account(conn: sqlite3.Connection, name: str | None) -> AccountRecord | None:
    """
    Return the account named *name*, or the active account when *name* is ``None``.

    Backs the per-command ``--account NAME`` override: commands pass the
    option's value straight through, so omitting the flag keeps the existing
    active-account behavior. Returns ``None`` when the named account does not
    exist, or when *name* is ``None`` and no active account is set.
    """
    if name is None:
        return get_active_account(conn)
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


# ---------------------------------------------------------------------------
# Account config — each account's own trading settings
# ---------------------------------------------------------------------------


def get_account_config(
    conn: sqlite3.Connection, account_name: str, key: str
) -> str | None:
    """Return *account_name*'s value for *key*, or ``None`` when unset."""
    row = conn.execute(
        "SELECT value FROM account_config WHERE account_name = ? AND key = ?",
        (account_name, key),
    ).fetchone()
    return row[0] if row else None


def set_account_config(
    conn: sqlite3.Connection, account_name: str, key: str, value: str
) -> None:
    """
    Upsert *key* = *value* in *account_name*'s config.

    Raises ``sqlite3.IntegrityError`` when *account_name* does not exist
    (foreign key). Does not validate *key* — callers check it against
    ``ACCOUNT_CONFIG_KEYS``.
    """
    conn.execute(
        "REPLACE INTO account_config (account_name, key, value) VALUES (?, ?, ?)",
        (account_name, key, value),
    )
    conn.commit()


def delete_account_config(
    conn: sqlite3.Connection, account_name: str, key: str
) -> bool:
    """Remove *key* from *account_name*'s config. Returns ``True`` if it was set."""
    cursor = conn.execute(
        "DELETE FROM account_config WHERE account_name = ? AND key = ?",
        (account_name, key),
    )
    conn.commit()
    return cursor.rowcount > 0


def get_all_account_config(
    conn: sqlite3.Connection, account_name: str
) -> list[tuple[str, str]]:
    """Return *account_name*'s config as ``(key, value)`` pairs sorted by key."""
    rows = conn.execute(
        "SELECT key, value FROM account_config WHERE account_name = ? ORDER BY key",
        (account_name,),
    ).fetchall()
    return [(row[0], row[1]) for row in rows]


def migrate_shared_config_to_accounts(conn: sqlite3.Connection) -> None:
    """
    Copy legacy shared settings from ``config`` into every account's config.

    Before per-account config, the ``ACCOUNT_CONFIG_KEYS`` settings were
    stored once in the ``config`` table and applied to every account. To keep
    each existing account's behavior unchanged, every such row is copied into
    each account's ``account_config`` (without overwriting a value the
    account already has) and then removed from ``config``, in one
    transaction.

    Idempotent: a no-op once no legacy keys remain. When no accounts exist
    yet the legacy rows are left in place, so the first account added picks
    them up on the next run instead of them being lost.
    """
    # Legacy settings still sitting in the shared table.
    placeholders = ", ".join("?" for _ in ACCOUNT_CONFIG_KEYS)
    legacy = conn.execute(
        f"SELECT key, value FROM config WHERE key IN ({placeholders})",
        tuple(ACCOUNT_CONFIG_KEYS),
    ).fetchall()
    if not legacy or get_account_count(conn) == 0:
        return

    # Copy each legacy row to every account; OR IGNORE keeps any value an
    # account already set for itself.
    for key, value in legacy:
        conn.execute(
            "INSERT OR IGNORE INTO account_config (account_name, key, value) "
            "SELECT name, ?, ? FROM accounts",
            (key, value),
        )

    # Drop the copied rows so the migration never runs again.
    conn.execute(
        f"DELETE FROM config WHERE key IN ({placeholders})",
        tuple(ACCOUNT_CONFIG_KEYS),
    )
    conn.commit()


# ---------------------------------------------------------------------------
# Account groups — named sets of accounts for multi-account trades
# ---------------------------------------------------------------------------


def add_group_member(
    conn: sqlite3.Connection, group_name: str, account_name: str
) -> None:
    """
    Add *account_name* to *group_name*, creating the group if it doesn't exist.

    Raises ``sqlite3.IntegrityError`` if *account_name* is already a member of
    *group_name* (unique index), or if *account_name* does not exist in the
    ``accounts`` table (foreign key). The caller is responsible for converting
    either into a user-friendly error.
    """
    conn.execute(
        "INSERT INTO account_groups (group_name, account_name) VALUES (?, ?)",
        (group_name, account_name),
    )
    conn.commit()


def remove_group_member(
    conn: sqlite3.Connection, group_name: str, account_name: str
) -> bool:
    """
    Remove *account_name* from *group_name*.

    Returns ``True`` when a membership row was removed, ``False`` when the
    pair was not found. Removing the last member makes the group disappear
    from ``list_group_names`` — there is no separate row for the group itself.
    """
    cursor = conn.execute(
        "DELETE FROM account_groups WHERE group_name = ? AND account_name = ?",
        (group_name, account_name),
    )
    conn.commit()
    return cursor.rowcount > 0


def delete_group(conn: sqlite3.Connection, group_name: str) -> int:
    """Remove every membership row for *group_name*. Returns the number removed."""
    cursor = conn.execute(
        "DELETE FROM account_groups WHERE group_name = ?",
        (group_name,),
    )
    conn.commit()
    return cursor.rowcount


def list_group_names(conn: sqlite3.Connection) -> list[str]:
    """Return every distinct group name, alphabetically sorted."""
    rows = conn.execute(
        "SELECT DISTINCT group_name FROM account_groups ORDER BY group_name"
    ).fetchall()
    return [row[0] for row in rows]


def list_groups_for_account(conn: sqlite3.Connection, account_name: str) -> list[str]:
    """Return the names of every group *account_name* belongs to, sorted."""
    rows = conn.execute(
        "SELECT group_name FROM account_groups WHERE account_name = ? "
        "ORDER BY group_name",
        (account_name,),
    ).fetchall()
    return [row[0] for row in rows]


def list_group_members(
    conn: sqlite3.Connection, group_name: str
) -> list[AccountRecord]:
    """Return the ``AccountRecord`` for every member of *group_name*, sorted by name."""
    rows = conn.execute(
        """
        SELECT a.name, a.oanda_id, a.is_practice, a.created_at
        FROM account_groups g
        JOIN accounts a ON a.name = g.account_name
        WHERE g.group_name = ?
        ORDER BY a.name
        """,
        (group_name,),
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
