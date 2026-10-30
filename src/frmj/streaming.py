"""
Background transaction streaming while ``frmj shell`` is open.

One shared background process, the *streamer*, follows Oanda's transaction
stream for every configured account (``execution.sync.follow_transactions``,
one thread per account). It syncs each new transaction into the local
database and records a stream notice for it, which the open shells print.
It runs only while at least one interactive shell is open.

Lifecycle
---------
* A shell calls ``register_shell`` when it opens. That writes a file named
  after its PID into the registry, and starts the streamer if none is
  running.
* The streamer checks the registry every couple of seconds. It starts or
  stops account threads as accounts are added or removed. It exits once no
  registered shell is still alive.
* A shell calls ``unregister_shell`` when it closes. A shell that crashes
  never does; its PID file is noticed as stale (no such process) and
  removed.

Two ``flock`` locks in ``app.get_stream_dir()`` keep this race-free across
any number of shells:

* ``streamer.lock`` is held by the running streamer for its whole life, so
  at most one streamer runs, and a shell can tell whether one is running.
* ``registry.lock`` is held briefly whenever a shell registers or
  unregisters, and while the streamer decides to exit. Before a deciding
  streamer releases the registry lock, it has already released
  ``streamer.lock``. So a shell registering at that moment sees no streamer
  and starts a new one, and is never left without one.

The streamer logs to ``streamer.log`` in the same directory. Streaming needs
``fcntl``, so it is POSIX-only. On other platforms ``streaming_supported()``
is ``False`` and shells run without it.
"""

from __future__ import annotations

import os
import subprocess
import sys
import threading
import time
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import IO

from frmj import queries
from frmj.accounts import AccountRecord, list_accounts
from frmj.app import get_client_for_account, get_db, get_stream_dir
from frmj.execution.sync import follow_transactions

try:
    import fcntl
except ImportError:  # Windows: no flock, so no streaming.
    fcntl = None  # type: ignore[assignment]

# How often the streamer re-checks the registry and the account list.
_REGISTRY_POLL_SECONDS: float = 2.0
# How long an account thread waits before retrying a failed stream (the
# polling fallback) or a missing API token.
_RETRY_SECONDS: float = 15.0
# Stream notices older than this are deleted when the streamer starts.
_NOTICE_RETENTION: timedelta = timedelta(days=1)

# A function that follows one account until its stop event is set.
FollowFn = Callable[[AccountRecord, threading.Event], None]


def streaming_supported() -> bool:
    """Return ``True`` where the streamer can run (needs POSIX ``fcntl``)."""
    return fcntl is not None


# ---------------------------------------------------------------------------
# Files and locks
# ---------------------------------------------------------------------------


def _shells_dir() -> Path:
    """Directory of registered shells: one empty file per shell, named by PID."""
    path = get_stream_dir() / "shells"
    path.mkdir(exist_ok=True)
    return path


def _open_lock(name: str) -> IO[bytes]:
    """Open (creating if needed) the lock file *name* in the stream dir."""
    return open(get_stream_dir() / name, "ab")


@contextmanager
def _registry_lock() -> Iterator[None]:
    """Hold ``registry.lock`` (blocking) for the duration of the block."""
    with _open_lock("registry.lock") as lock_file:
        fcntl.flock(lock_file, fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(lock_file, fcntl.LOCK_UN)


def _streamer_running() -> bool:
    """Return ``True`` if a streamer currently holds ``streamer.lock``."""
    with _open_lock("streamer.lock") as lock_file:
        try:
            fcntl.flock(lock_file, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            return True
        # We got it, so nobody holds it; let go straight away.
        fcntl.flock(lock_file, fcntl.LOCK_UN)
        return False


def _pid_alive(pid: int) -> bool:
    """Return ``True`` if a process with *pid* exists."""
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        # Exists, but belongs to someone else.
        return True
    return True


def live_shell_pids() -> list[int]:
    """Return the PIDs of registered shells still running, dropping stale ones.

    A PID file whose process is gone (a shell that crashed or was killed)
    is deleted here. Call with the registry lock held.
    """
    alive: list[int] = []
    for entry in _shells_dir().iterdir():
        try:
            pid = int(entry.name)
        except ValueError:
            continue  # Not ours; leave it alone.
        if _pid_alive(pid):
            alive.append(pid)
        else:
            entry.unlink(missing_ok=True)
    return sorted(alive)


# ---------------------------------------------------------------------------
# Shell side
# ---------------------------------------------------------------------------


def _spawn_streamer() -> None:
    """Start ``frmj streamer`` as a detached background process.

    It gets its own session (so closing the terminal or pressing Ctrl+C in
    the shell doesn't reach it), no stdin/stdout, and appends stderr to
    ``streamer.log``. It inherits the environment, including
    ``FRMJ_DB_PATH`` and any token variables.
    """
    with open(get_stream_dir() / "streamer.log", "ab") as log_file:
        subprocess.Popen(
            [sys.executable, "-m", "frmj", "streamer"],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=log_file,
            start_new_session=True,
            close_fds=True,
        )


def register_shell(pid: int) -> None:
    """Register the shell *pid* and make sure the streamer is running."""
    with _registry_lock():
        (_shells_dir() / str(pid)).touch()
        if not _streamer_running():
            _spawn_streamer()


def unregister_shell(pid: int) -> None:
    """Remove the shell *pid* from the registry; the streamer exits if it was the last."""
    with _registry_lock():
        (_shells_dir() / str(pid)).unlink(missing_ok=True)


# ---------------------------------------------------------------------------
# Streamer side
# ---------------------------------------------------------------------------


def _log(message: str) -> None:
    """Write a timestamped line to stderr (``streamer.log`` when spawned)."""
    stamp = datetime.now(tz=timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
    print(f"{stamp} {message}", file=sys.stderr, flush=True)


def _follow_account(account: AccountRecord, stop: threading.Event) -> None:
    """Follow *account*'s stream into the database until *stop* is set.

    Runs in its own thread with its own database connection. Every
    transaction the stream brings in gets a stream notice. A missing API
    token or a failing stream is logged (once per distinct error) and
    retried every ``_RETRY_SECONDS``.
    """
    last_error: list[str] = []

    def log_error(exc: Exception) -> None:
        # Log only changes, so a long outage doesn't flood the log.
        message = f"{account.name}: {type(exc).__name__}: {exc}"
        if last_error != [message]:
            _log(message)
            last_error[:] = [message]

    conn = get_db()
    try:
        # A token may be stored later, while shells stay open.
        while not stop.is_set():
            try:
                client = get_client_for_account(account)
                break
            except RuntimeError as exc:
                log_error(exc)
                stop.wait(_RETRY_SECONDS)
        else:
            return

        def add_notices(after_oanda_id: str) -> None:
            # The rows the last sync added: announce each one.
            rows = queries.list_transactions_after(
                conn, client.account_id, after_oanda_id
            )
            queries.add_stream_notices(conn, [row["id"] for row in rows])

        try:
            follow_transactions(
                conn,
                client,
                on_ingested=add_notices,
                on_error=log_error,
                stop=stop,
                retry_interval=_RETRY_SECONDS,
            )
        finally:
            client.close()
    finally:
        conn.close()


def _reconcile_followers(
    followers: dict[str, threading.Event], follow: FollowFn
) -> None:
    """Start a thread for each new account and stop those for removed ones.

    *followers* maps an account's Oanda ID to its thread's stop event.
    Threads are daemons, so a streamer that exits doesn't wait for a stream
    to notice its stop event.
    """
    conn = get_db()
    try:
        accounts = {account.oanda_id: account for account in list_accounts(conn)}
    finally:
        conn.close()

    for oanda_id, account in accounts.items():
        if oanda_id not in followers:
            stop = threading.Event()
            followers[oanda_id] = stop
            threading.Thread(
                target=follow,
                args=(account, stop),
                name=f"follow-{account.name}",
                daemon=True,
            ).start()
            _log(f"following {account.name} ({oanda_id})")

    for oanda_id in [oid for oid in followers if oid not in accounts]:
        followers.pop(oanda_id).set()
        _log(f"stopped following {oanda_id} (account removed)")


def _prune_notices() -> None:
    """Delete stream notices older than ``_NOTICE_RETENTION``."""
    cutoff = datetime.now(tz=timezone.utc) - _NOTICE_RETENTION
    conn = get_db()
    try:
        queries.prune_stream_notices(conn, cutoff.strftime("%Y-%m-%dT%H:%M:%S.000Z"))
    finally:
        conn.close()


def run_streamer(
    follow: FollowFn = _follow_account,
    poll_interval: float = _REGISTRY_POLL_SECONDS,
) -> None:
    """Run the streamer until no registered shell is alive.

    Returns at once if another streamer already holds ``streamer.lock``.
    *follow* and *poll_interval* exist for tests.
    """
    lock_file = _open_lock("streamer.lock")
    try:
        fcntl.flock(lock_file, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        lock_file.close()
        return

    _log(f"streamer started (pid {os.getpid()})")
    _prune_notices()
    followers: dict[str, threading.Event] = {}
    try:
        while True:
            with _registry_lock():
                if not live_shell_pids():
                    # Release streamer.lock while still holding the registry
                    # lock: a shell registering right after this sees no
                    # streamer and starts a new one.
                    fcntl.flock(lock_file, fcntl.LOCK_UN)
                    lock_file.close()
                    break
            _reconcile_followers(followers, follow)
            time.sleep(poll_interval)
    finally:
        for stop in followers.values():
            stop.set()
        if not lock_file.closed:
            lock_file.close()
        _log("streamer stopped (no open shells)")
