"""Shell tab-completion callbacks shared by two or more CLI commands.

Completers used by only a single command live next to that command instead
(e.g. ``_complete_export_format`` in ``export.py``, ``_complete_oanda_id`` and
``_complete_tag`` in ``journal.py``, ``_complete_config_key``/``_complete_config_value``
in ``config.py``).
"""

from __future__ import annotations

import typer

from frmj.accounts import list_accounts, list_group_members, list_group_names
from frmj.app import get_client, get_db

# ---------------------------------------------------------------------------
# Shell completion helpers
# ---------------------------------------------------------------------------

# All Oanda FX instruments used to drive tab completion.  This list does not
# gate input — any instrument string is accepted regardless of whether it
# appears here.  Sorted alphabetically within each group.
_FX_PAIRS: tuple[str, ...] = (
    # Majors
    "aud_usd",
    "eur_usd",
    "gbp_usd",
    "nzd_usd",
    "usd_cad",
    "usd_chf",
    "usd_jpy",
    # Euro crosses
    "eur_aud",
    "eur_cad",
    "eur_chf",
    "eur_gbp",
    "eur_jpy",
    "eur_nzd",
    # Sterling crosses
    "gbp_aud",
    "gbp_cad",
    "gbp_chf",
    "gbp_jpy",
    "gbp_nzd",
    # Antipodean / commodity crosses
    "aud_cad",
    "aud_chf",
    "aud_jpy",
    "aud_nzd",
    "cad_chf",
    "cad_jpy",
    "chf_jpy",
    "nzd_cad",
    "nzd_chf",
    "nzd_jpy",
    # SGD crosses
    "sgd_chf",
    "sgd_jpy",
    # USD exotics
    "usd_cnh",
    "usd_czk",
    "usd_dkk",
    "usd_hkd",
    "usd_huf",
    "usd_mxn",
    "usd_nok",
    "usd_pln",
    "usd_sek",
    "usd_sgd",
    "usd_thb",
    "usd_try",
    "usd_zar",
    # EUR exotics
    "eur_czk",
    "eur_dkk",
    "eur_huf",
    "eur_nok",
    "eur_pln",
    "eur_sek",
    "eur_try",
    "eur_zar",
    # Metals / spot commodities
    "xag_usd",
    "xau_usd",
    "xcu_usd",
    "xpd_usd",
    "xpt_usd",
)

# The eight currencies conventionally treated as "majors" in FX. Used to
# classify a currency pair as major/minor/exotic for the ``financing``
# command — see _pair_tier.
_MAJOR_CURRENCIES: frozenset[str] = frozenset(
    {"USD", "EUR", "GBP", "JPY", "CHF", "CAD", "AUD", "NZD"}
)

# Metals trade against USD like a currency pair but aren't FX at all, so
# they're excluded from the major/minor/exotic financing-rate listing.
_METAL_INSTRUMENTS: frozenset[str] = frozenset(
    {"XAG_USD", "XAU_USD", "XCU_USD", "XPD_USD", "XPT_USD"}
)

#: Every FX currency pair from _FX_PAIRS, uppercased and with metals
#: excluded — the instrument list the ``financing`` command fetches rates for.
_FINANCING_PAIRS: tuple[str, ...] = tuple(
    p.upper() for p in _FX_PAIRS if p.upper() not in _METAL_INSTRUMENTS
)


def _pair_tier(instrument: str) -> str:
    """Classify an FX pair as ``"major"``, ``"minor"``, or ``"exotic"``.

    Major: USD paired with one of the other seven major currencies (the
    conventional USD majors). Minor (cross): both currencies are majors,
    neither is USD. Exotic: at least one currency isn't in the major set.
    """
    base, quote = instrument.split("_")
    if base not in _MAJOR_CURRENCIES or quote not in _MAJOR_CURRENCIES:
        return "exotic"
    return "major" if "USD" in (base, quote) else "minor"


def _complete_instrument(incomplete: str) -> list[str]:
    """Return FX pairs whose names start with *incomplete* (case-insensitive)."""
    return [p for p in _FX_PAIRS if p.startswith(incomplete.lower())]


def _complete_open_instrument(ctx: typer.Context, incomplete: str) -> list[str]:
    """Return instruments with an open position, for ``frmj close``.

    Unlike ``_complete_instrument`` (the static FX pair list used for
    ``trade``), this queries the broker for actual open trades so shell
    completion only ever offers something ``close`` can act on. Queries the
    account named by ``--account`` when it precedes the instrument on the
    command line, otherwise the active account. Any failure (no active
    account configured, network/auth error) is swallowed and yields no
    completions rather than breaking the user's shell.
    """
    conn = get_db()
    try:
        client = get_client(conn, ctx.params.get("account"))
        instruments = {t.instrument for t in client.get_open_trades()}
    except Exception:
        return []
    finally:
        conn.close()
    return sorted(i for i in instruments if i.upper().startswith(incomplete.upper()))


def _complete_direction(incomplete: str) -> list[str]:
    return [d for d in ("long", "short") if d.startswith(incomplete.lower())]


def _complete_account_name(incomplete: str) -> list[str]:
    """Return configured account names whose names start with *incomplete*."""
    conn = get_db()
    try:
        names = [a.name for a in list_accounts(conn)]
    finally:
        conn.close()
    return [n for n in names if n.startswith(incomplete)]


def _complete_env_type(incomplete: str) -> list[str]:
    return [e for e in ("practice", "live") if e.startswith(incomplete.lower())]


def _complete_account_group(incomplete: str) -> list[str]:
    """Return saved group names whose names start with *incomplete*.

    Unlike the other completion helpers (static lists), group names are
    per-database data, so this opens a connection to look them up.
    """
    conn = get_db()
    try:
        names = list_group_names(conn)
    finally:
        conn.close()
    return [n for n in names if n.startswith(incomplete)]


def _complete_group_member(ctx: typer.Context, incomplete: str) -> list[str]:
    """Return accounts already in the group named by the preceding argument.

    Falls back to an empty list if the group name hasn't resolved to any
    members yet (e.g. it doesn't exist), mirroring ``_complete_config_value``.
    """
    conn = get_db()
    try:
        members = list_group_members(conn, ctx.params.get("group_name", ""))
    finally:
        conn.close()
    return [m.name for m in members if m.name.startswith(incomplete)]


def _complete_txn_type(incomplete: str) -> list[str]:
    """Return distinct transaction types already seen in the local DB."""
    conn = get_db()
    try:
        rows = conn.execute(
            "SELECT DISTINCT type FROM transactions ORDER BY type"
        ).fetchall()
    finally:
        conn.close()
    return [r[0] for r in rows if r[0].startswith(incomplete.upper())]
