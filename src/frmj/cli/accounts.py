"""``frmj account`` and ``frmj account group`` — manage named Oanda account profiles."""

from __future__ import annotations

import typer

from frmj.accounts import (
    add_account,
    add_group_member,
    delete_group,
    get_account,
    get_active_account,
    get_active_account_name,
    list_accounts,
    list_group_members,
    list_group_names,
    remove_account,
    remove_group_member,
    rename_account,
    set_active_account,
)
from frmj.app import get_db, get_token, store_token
from frmj.cli import account_app, group_app
from frmj.cli._completion import (
    _complete_account_group,
    _complete_account_name,
    _complete_env_type,
    _complete_group_member,
)

# ---------------------------------------------------------------------------
# account sub-commands
# ---------------------------------------------------------------------------


@account_app.command("add")
def account_add(
    name: str = typer.Argument(..., help="Short name for the account, e.g. funded"),
) -> None:
    """Add a new Oanda account profile."""
    name = name.strip()
    if not name:
        typer.echo("Error: account name cannot be empty.", err=True)
        raise typer.Exit(1)

    conn = get_db()
    try:
        # Reject duplicate names.
        if get_account(conn, name) is not None:
            typer.echo(
                f"Error: account '{name}' already exists. "
                "Use 'frmj account list' to see existing accounts.",
                err=True,
            )
            conn.close()
            raise typer.Exit(1)

        # Gather account details interactively.
        oanda_id: str = typer.prompt("Oanda account ID").strip()
        if not oanda_id:
            typer.echo("Error: Oanda account ID cannot be empty.", err=True)
            conn.close()
            raise typer.Exit(1)

        acct_type: str = (
            typer.prompt(
                "Account type [practice/live]",
                default="practice",
            )
            .strip()
            .lower()
        )
        if acct_type not in ("practice", "live"):
            typer.echo("Error: type must be 'practice' or 'live'.", err=True)
            conn.close()
            raise typer.Exit(1)
        is_practice = acct_type == "practice"

        # Persist the account record.
        import sqlite3 as _sqlite3

        try:
            add_account(conn, name, oanda_id, is_practice=is_practice)
        except _sqlite3.IntegrityError:
            typer.echo(f"Error: account '{name}' already exists.", err=True)
            conn.close()
            raise typer.Exit(1)

        # Auto-activate when this is the first account.
        from frmj.accounts import get_account_count as _count

        if _count(conn) == 1:
            set_active_account(conn, name)
            typer.echo(f"Account '{name}' added and set as active.")
        else:
            typer.echo(
                f"Account '{name}' added. Run 'frmj account use {name}' to activate it."
            )

        # Remind the user to store a token if none is set for this environment.
        if not get_token(is_practice):
            typer.echo(
                f"  No {acct_type} token stored. Run: frmj account set-token {acct_type}"
            )

    finally:
        conn.close()


@account_app.command("list")
def account_list() -> None:
    """List all configured account profiles."""
    conn = get_db()
    try:
        accounts = list_accounts(conn)
        active_name = get_active_account_name(conn)
    finally:
        conn.close()

    if not accounts:
        typer.echo("No accounts configured. Add one with: frmj account add NAME")
        return

    for acct in accounts:
        marker = "*" if acct.name == active_name else " "
        acct_type = "practice" if acct.is_practice else "live"
        typer.echo(f"  {marker} {acct.name}  [{acct_type}, {acct.oanda_id}]")


@account_app.command("use")
def account_use(
    name: str = typer.Argument(
        ..., help="Account name to activate", autocompletion=_complete_account_name
    ),
) -> None:
    """Set the active account."""
    conn = get_db()
    try:
        if get_account(conn, name) is None:
            typer.echo(
                f"Error: account '{name}' not found. "
                "Run 'frmj account list' to see available accounts.",
                err=True,
            )
            conn.close()
            raise typer.Exit(1)
        set_active_account(conn, name)
    finally:
        conn.close()
    typer.echo(f"Active account set to '{name}'.")


@account_app.command("current")
def account_current() -> None:
    """Show the currently active account."""
    conn = get_db()
    try:
        account = get_active_account(conn)
    finally:
        conn.close()

    if account is None:
        typer.echo("No active account. Run: frmj account use NAME")
        raise typer.Exit(1)

    acct_type = "practice" if account.is_practice else "live"
    typer.echo(f"{account.name}  [{acct_type}, {account.oanda_id}]")


@account_app.command("remove")
def account_remove(
    name: str = typer.Argument(
        ..., help="Account name to remove", autocompletion=_complete_account_name
    ),
) -> None:
    """Remove an account profile (does not delete the associated token)."""
    conn = get_db()
    try:
        active_name = get_active_account_name(conn)
        if name == active_name:
            typer.echo(
                f"Error: '{name}' is the active account. "
                "Switch to another account first with: frmj account use NAME",
                err=True,
            )
            conn.close()
            raise typer.Exit(1)

        removed = remove_account(conn, name)
    finally:
        conn.close()

    if removed:
        typer.echo(f"Account '{name}' removed.")
    else:
        typer.echo(f"Error: account '{name}' not found.", err=True)
        raise typer.Exit(1)


@account_app.command("set-token")
def account_set_token(
    env_type: str | None = typer.Argument(
        None,
        help="Token environment: 'practice' or 'live'. Defaults to the active account's type.",
        autocompletion=_complete_env_type,
    ),
) -> None:
    """Store the Oanda API token for the practice or live environment in the OS keychain."""
    # Resolve env_type from the argument or fall back to the active account's type.
    if env_type is not None:
        env_type = env_type.strip().lower()
        if env_type not in ("practice", "live"):
            typer.echo("Error: argument must be 'practice' or 'live'.", err=True)
            raise typer.Exit(1)
        is_practice = env_type == "practice"
    else:
        conn = get_db()
        try:
            account = get_active_account(conn)
        finally:
            conn.close()
        if account is None:
            typer.echo(
                "Error: No active account. Specify 'practice' or 'live', or run "
                "'frmj account use NAME' first.",
                err=True,
            )
            raise typer.Exit(1)
        is_practice = account.is_practice
        env_type = "practice" if is_practice else "live"

    token = typer.prompt(f"Oanda API token for {env_type} accounts", hide_input=True)
    try:
        store_token(token, practice=is_practice)
    except RuntimeError as exc:
        typer.echo(f"Error: {exc}", err=True)
        raise typer.Exit(1)
    typer.echo(f"Token for {env_type} accounts stored in OS keychain.")


@account_app.command("rename")
def account_rename(
    old_name: str = typer.Argument(
        ..., help="Current account name", autocompletion=_complete_account_name
    ),
    new_name: str = typer.Argument(..., help="New account name"),
) -> None:
    """Rename an account profile without changing its Oanda ID or settings."""
    # Normalise and validate the new name before touching the database.
    new_name = new_name.strip()
    if not new_name:
        typer.echo("Error: new name cannot be empty.", err=True)
        raise typer.Exit(1)
    if old_name == new_name:
        typer.echo("Error: old and new names are the same.", err=True)
        raise typer.Exit(1)

    conn = get_db()
    try:
        # Guard: old account must exist.
        if get_account(conn, old_name) is None:
            typer.echo(
                f"Error: account '{old_name}' not found. "
                "Run 'frmj account list' to see available accounts.",
                err=True,
            )
            conn.close()
            raise typer.Exit(1)

        # Guard: new name must not collide with an existing profile.
        if get_account(conn, new_name) is not None:
            typer.echo(
                f"Error: account '{new_name}' already exists.",
                err=True,
            )
            conn.close()
            raise typer.Exit(1)

        # Rename in DB (accounts row + active_account config if applicable).
        rename_account(conn, old_name, new_name)
    finally:
        conn.close()

    typer.echo(f"Account '{old_name}' renamed to '{new_name}'.")


# ---------------------------------------------------------------------------
# account group sub-commands
# ---------------------------------------------------------------------------


@group_app.command("add")
def account_group_add(
    group_name: str = typer.Argument(
        ..., help="Group name, e.g. prop-firms", autocompletion=_complete_account_group
    ),
    account_name: str = typer.Argument(
        ...,
        help="Account name to add to the group",
        autocompletion=_complete_account_name,
    ),
) -> None:
    """Add an account to a group, creating the group if it doesn't exist yet."""
    conn = get_db()
    try:
        if get_account(conn, account_name) is None:
            typer.echo(
                f"Error: account '{account_name}' not found. "
                "Run 'frmj account list' to see available accounts.",
                err=True,
            )
            conn.close()
            raise typer.Exit(1)

        import sqlite3 as _sqlite3

        try:
            add_group_member(conn, group_name, account_name)
        except _sqlite3.IntegrityError:
            typer.echo(
                f"Error: '{account_name}' is already in group '{group_name}'.", err=True
            )
            conn.close()
            raise typer.Exit(1)
    finally:
        conn.close()
    typer.echo(f"Added '{account_name}' to group '{group_name}'.")


@group_app.command("remove")
def account_group_remove(
    group_name: str = typer.Argument(
        ..., help="Group name", autocompletion=_complete_account_group
    ),
    account_name: str = typer.Argument(
        ...,
        help="Account name to remove from the group",
        autocompletion=_complete_group_member,
    ),
) -> None:
    """Remove an account from a group."""
    conn = get_db()
    try:
        removed = remove_group_member(conn, group_name, account_name)
    finally:
        conn.close()
    if removed:
        typer.echo(f"Removed '{account_name}' from group '{group_name}'.")
    else:
        typer.echo(f"Error: '{account_name}' is not in group '{group_name}'.", err=True)
        raise typer.Exit(1)


@group_app.command("delete")
def account_group_delete(
    group_name: str = typer.Argument(
        ..., help="Group name to delete", autocompletion=_complete_account_group
    ),
) -> None:
    """Delete a group entirely (removes all its memberships)."""
    conn = get_db()
    try:
        removed = delete_group(conn, group_name)
    finally:
        conn.close()
    if removed:
        typer.echo(f"Deleted group '{group_name}' ({removed} member(s) removed).")
    else:
        typer.echo(f"Error: group '{group_name}' not found.", err=True)
        raise typer.Exit(1)


@group_app.command("list")
def account_group_list() -> None:
    """List all saved account groups and their members."""
    conn = get_db()
    try:
        names = list_group_names(conn)
        members_by_group = {name: list_group_members(conn, name) for name in names}
    finally:
        conn.close()

    if not names:
        typer.echo(
            "No account groups configured. "
            "Add one with: frmj account group add GROUP ACCOUNT"
        )
        return

    for name in names:
        members = ", ".join(m.name for m in members_by_group[name])
        typer.echo(f"  {name}: {members}")


@group_app.command("show")
def account_group_show(
    group_name: str = typer.Argument(
        ..., help="Group name", autocompletion=_complete_account_group
    ),
) -> None:
    """Show the members of a single group."""
    conn = get_db()
    try:
        members = list_group_members(conn, group_name)
    finally:
        conn.close()

    if not members:
        typer.echo(
            f"Error: group '{group_name}' not found or has no members.", err=True
        )
        raise typer.Exit(1)

    for acct in members:
        acct_type = "practice" if acct.is_practice else "live"
        typer.echo(f"  {acct.name}  [{acct_type}, {acct.oanda_id}]")
