"""
Global pytest fixtures.

The ``_no_real_keyring`` autouse fixture stubs out all keyring operations for
every test so the suite never touches the real OS keychain.  By default the
stub returns ``None`` from ``get_password`` (simulating "nothing stored").

Tests that want a keyring-sourced token can override with their own
``monkeypatch.setattr("frmj.app.keyring.get_password", ...)``.
"""

from __future__ import annotations

import pytest


@pytest.fixture(autouse=True)
def _no_real_keyring(monkeypatch: pytest.MonkeyPatch) -> None:
    """Replace keyring operations with no-op stubs for every test."""
    monkeypatch.setattr("frmj.app.keyring.get_password", lambda svc, usr: None)
    monkeypatch.setattr("frmj.app.keyring.set_password", lambda svc, usr, pwd: None)
    monkeypatch.setattr("frmj.app.keyring.delete_password", lambda svc, usr: None)
