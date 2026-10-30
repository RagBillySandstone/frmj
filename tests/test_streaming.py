"""Tests for ``frmj.streaming``: the shell registry and the background streamer."""

from __future__ import annotations

import fcntl
import os
import subprocess
import threading
import time
from collections.abc import Callable, Iterator
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import pytest

from frmj import queries, streaming
from frmj.accounts import AccountRecord, add_account, remove_account
from frmj.app import get_db, get_stream_dir
from frmj.execution.oanda import TransactionRow


@pytest.fixture()
def stream_db(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """A temp database with two practice accounts; spawning is recorded, not run."""
    path = tmp_path / "frmj.db"
    monkeypatch.setenv("FRMJ_DB_PATH", str(path))
    conn = get_db()
    add_account(conn, "alpha", "acct-a", is_practice=True)
    add_account(conn, "beta", "acct-b", is_practice=True)
    conn.close()
    return path


@pytest.fixture()
def spawned(monkeypatch: pytest.MonkeyPatch) -> list[bool]:
    """Record streamer spawns instead of starting a real process."""
    calls: list[bool] = []
    monkeypatch.setattr(streaming, "_spawn_streamer", lambda: calls.append(True))
    return calls


def _dead_pid() -> int:
    """Return the PID of a process that has already exited."""
    proc = subprocess.Popen(["true"])
    proc.wait()
    return proc.pid


def _wait_for(condition: Callable[[], bool], timeout: float = 5.0) -> None:
    """Poll *condition* until it holds, failing the test after *timeout* seconds."""
    deadline = time.monotonic() + timeout
    while not condition():
        if time.monotonic() > deadline:
            raise AssertionError("condition not met in time")
        time.sleep(0.01)


# ---------------------------------------------------------------------------
# Registry
# ---------------------------------------------------------------------------


class TestRegistry:
    def test_register_records_pid_and_spawns_streamer(
        self, stream_db: Path, spawned: list[bool]
    ) -> None:
        streaming.register_shell(os.getpid())
        assert (get_stream_dir() / "shells" / str(os.getpid())).exists()
        assert spawned == [True]

    def test_register_does_not_spawn_when_streamer_running(
        self, stream_db: Path, spawned: list[bool]
    ) -> None:
        # Hold streamer.lock as a running streamer would.
        with open(get_stream_dir() / "streamer.lock", "ab") as held:
            fcntl.flock(held, fcntl.LOCK_EX)
            assert streaming._streamer_running()
            streaming.register_shell(os.getpid())
        assert spawned == []
        assert not streaming._streamer_running()

    def test_unregister_removes_pid(self, stream_db: Path, spawned: list[bool]) -> None:
        streaming.register_shell(os.getpid())
        streaming.unregister_shell(os.getpid())
        assert streaming.live_shell_pids() == []
        # Unregistering twice is harmless.
        streaming.unregister_shell(os.getpid())

    def test_stale_pids_are_dropped(self, stream_db: Path) -> None:
        """A shell that crashed without unregistering doesn't keep the streamer alive."""
        shells = get_stream_dir() / "shells"
        shells.mkdir(exist_ok=True)
        dead = _dead_pid()
        (shells / str(dead)).touch()
        (shells / str(os.getpid())).touch()
        (shells / "not-a-pid").touch()
        assert streaming.live_shell_pids() == [os.getpid()]
        assert not (shells / str(dead)).exists()
        assert (shells / "not-a-pid").exists()


# ---------------------------------------------------------------------------
# Streamer lifecycle
# ---------------------------------------------------------------------------


@dataclass
class _FakeFollow:
    """Records follower threads instead of streaming: account -> stop event."""

    started: dict[str, threading.Event] = field(default_factory=dict)

    def __call__(self, account: AccountRecord, stop: threading.Event) -> None:
        self.started[account.name] = stop
        stop.wait()


def _start_streamer(follow: _FakeFollow) -> threading.Thread:
    thread = threading.Thread(
        target=streaming.run_streamer,
        kwargs={"follow": follow, "poll_interval": 0.01},
        daemon=True,
    )
    thread.start()
    return thread


class TestRunStreamer:
    def test_returns_at_once_if_another_streamer_runs(self, stream_db: Path) -> None:
        follow = _FakeFollow()
        with open(get_stream_dir() / "streamer.lock", "ab") as held:
            fcntl.flock(held, fcntl.LOCK_EX)
            streaming.run_streamer(follow=follow, poll_interval=0.01)
        assert follow.started == {}

    def test_exits_immediately_without_shells(self, stream_db: Path) -> None:
        follow = _FakeFollow()
        streaming.run_streamer(follow=follow, poll_interval=0.01)
        assert follow.started == {}
        assert not streaming._streamer_running()

    def test_follows_every_account_until_last_shell_closes(
        self, stream_db: Path, spawned: list[bool]
    ) -> None:
        streaming.register_shell(os.getpid())
        follow = _FakeFollow()
        thread = _start_streamer(follow)

        # Every configured account is followed, and the lock is held.
        _wait_for(lambda: set(follow.started) == {"alpha", "beta"})
        assert streaming._streamer_running()

        # An account added later is picked up; a removed one is stopped.
        conn = get_db()
        add_account(conn, "gamma", "acct-c", is_practice=True)
        remove_account(conn, "beta")
        conn.close()
        _wait_for(lambda: "gamma" in follow.started)
        _wait_for(lambda: follow.started["beta"].is_set())
        assert not follow.started["alpha"].is_set()

        # The last shell closing stops everything and frees the lock.
        streaming.unregister_shell(os.getpid())
        thread.join(timeout=5)
        assert not thread.is_alive()
        assert all(stop.is_set() for stop in follow.started.values())
        assert not streaming._streamer_running()

    def test_prunes_old_notices_on_start(self, stream_db: Path) -> None:
        conn = get_db()
        cur = conn.execute(
            "INSERT INTO transactions (oanda_id, account_id, type, time, raw_json) "
            "VALUES ('1', 'acct-a', 'ORDER_FILL', '2026-01-01T00:00:00Z', '{}')"
        )
        assert cur.lastrowid is not None
        queries.add_stream_notices(conn, [cur.lastrowid])
        conn.execute("UPDATE stream_notices SET created_at = '2020-01-01T00:00:00Z'")
        conn.commit()
        streaming.run_streamer(follow=_FakeFollow(), poll_interval=0.01)
        assert queries.list_stream_notices_after(conn, 0) == []
        conn.close()


# ---------------------------------------------------------------------------
# Following one account
# ---------------------------------------------------------------------------


def _row(oanda_id: str, account_id: str = "acct-a") -> TransactionRow:
    return TransactionRow(
        oanda_id=oanda_id,
        account_id=account_id,
        type="ORDER_FILL",
        time=f"2026-04-25T12:00:{int(oanda_id):02d}.000000Z",
        parent_oanda_id=None,
        raw_json="{}",
    )


@dataclass
class _StreamClient:
    """Fake OandaClient: queued sync responses and one scripted stream.

    The stream yields its messages, then sets *stop*, so the follower
    returns after one round.
    """

    stop: threading.Event
    account_id: str = "acct-a"
    responses: list[list[TransactionRow]] = field(default_factory=list)
    messages: list[dict[str, Any]] = field(default_factory=list)
    closed: bool = False

    def get_transactions_since(
        self, from_id: str | None = None
    ) -> list[TransactionRow]:
        return self.responses.pop(0) if self.responses else []

    def stream_transactions(self) -> Iterator[dict[str, Any]]:
        yield from self.messages
        self.stop.set()

    def close(self) -> None:
        self.closed = True


def _account(name: str = "alpha", oanda_id: str = "acct-a") -> AccountRecord:
    return AccountRecord(
        name=name, oanda_id=oanda_id, is_practice=True, created_at="2026-01-01"
    )


class TestFollowAccount:
    def test_streamed_transactions_get_notices(
        self, stream_db: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        stop = threading.Event()
        client = _StreamClient(
            stop,
            # Cold sync (history: no notice), then the streamed transaction.
            responses=[[_row("1")], [_row("2")]],
            messages=[{"id": "2", "type": "ORDER_FILL"}],
        )
        monkeypatch.setattr(streaming, "get_client_for_account", lambda a: client)
        streaming._follow_account(_account(), stop)

        conn = get_db()
        notices = queries.list_stream_notices_after(conn, 0)
        conn.close()
        assert [(n["account_id"], n["oanda_id"]) for n in notices] == [("acct-a", "2")]
        assert client.closed

    def test_missing_token_is_logged_once_and_retried_until_stopped(
        self,
        stream_db: Path,
        monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        stop = threading.Event()
        attempts: list[int] = []

        def no_token(account: AccountRecord) -> Any:
            attempts.append(1)
            if len(attempts) == 3:
                stop.set()
            raise RuntimeError("No API token found for the live environment.")

        monkeypatch.setattr(streaming, "get_client_for_account", no_token)
        monkeypatch.setattr(streaming, "_RETRY_SECONDS", 0)
        streaming._follow_account(_account(), stop)
        assert len(attempts) == 3
        assert capsys.readouterr().err.count("No API token") == 1
