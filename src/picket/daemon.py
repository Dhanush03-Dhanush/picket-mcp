"""The runtime (§4B/§10): one detached daemon per active watcher.

Run as ``python -m picket.daemon <watch_id>``. It double-forks + setsid so that
closing the arming session (or the MCP server) does not SIGHUP it, reloads its
state file, then polls on a fixed cadence: fetch, extract, evaluate the predicate
against the persisted baseline, persist, and on the unsatisfied->satisfied edge
launch the handler. Pure Python — no model runs while it waits.
"""

from __future__ import annotations

import fcntl
import os
import sys
import time
from collections.abc import Callable
from datetime import datetime
from typing import IO

import psutil

from picket import condition, handler, store
from picket.condition import ObserveError
from picket.models import WatchState
from picket.store import now_iso


def _parse(ts: str) -> datetime:
    return datetime.fromisoformat(ts)


def _gates_open(state: WatchState, now: str) -> bool:
    """Debounce (condition must hold long enough) and cooldown (min gap between fires)."""
    t = _parse(now)
    if state.debounce_seconds and state.satisfied_since:
        if (t - _parse(state.satisfied_since)).total_seconds() < state.debounce_seconds:
            return False
    if state.cooldown_seconds and state.last_fire_at:
        if (t - _parse(state.last_fire_at)).total_seconds() < state.cooldown_seconds:
            return False
    return True


def _acquire_lock(watch_id: str) -> IO | None:
    """Non-blocking in-flight lock: at most one handler per watcher (None if held)."""
    path = store.lock_path(watch_id)
    path.parent.mkdir(parents=True, exist_ok=True)
    handle = path.open("w")
    try:
        fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
        return handle
    except OSError:
        handle.close()
        return None


def poll_once(state: WatchState) -> WatchState:
    """One poll: observe, persist, and fire once per satisfied episode. Mutates + writes."""
    now = now_iso()
    state.heartbeat_at = now
    state.last_observed_at = now
    try:
        data = condition.fetch(state.endpoint)
        value = condition.extract(data, state.predicate.path)
        now_satisfied = condition.is_satisfied(state.predicate, value, state.baseline)
    except ObserveError as err:
        state.last_error = str(err)  # could-not-observe != change: never fires
        store.write_watch(state)
        return state

    state.last_value = value
    state.last_error = None
    if now_satisfied and not state.satisfied:  # rising edge starts an episode
        state.satisfied_since = now
        state.fired_this_episode = False
    if not now_satisfied:  # episode ended; re-arm
        state.satisfied_since = None
        state.fired_this_episode = False

    if now_satisfied and not state.fired_this_episode and _gates_open(state, now):
        lock = _acquire_lock(state.watch_id)
        if lock is None:
            handler.record_skipped_overlap(state)  # a handler is already in flight
        else:
            try:
                handler.fire(state, value)
            finally:
                fcntl.flock(lock, fcntl.LOCK_UN)
                lock.close()
            state.fire_count += 1
            state.last_fire_at = now
            state.fired_this_episode = True
            if state.predicate.op == "on_change":
                state.baseline = value  # re-arm against the new value
                now_satisfied = False
                state.satisfied_since = None
    state.satisfied = now_satisfied
    store.write_watch(state)
    return state


def _record_identity(state: WatchState) -> None:
    """Capture pid/pgid/create_time so stop_watch can verify identity before killing."""
    proc = psutil.Process()
    state.pid = proc.pid
    state.pgid = os.getpgid(proc.pid)
    state.proc_create_time = proc.create_time()


def run(
    watch_id: str,
    *,
    iterations: int | None = None,
    sleeper: Callable[[float], None] = time.sleep,
) -> None:
    """Reload state, record identity, then poll forever (or `iterations` times in tests)."""
    state = store.read_watch(watch_id)
    if state is None:
        return
    _record_identity(state)
    store.write_watch(state)

    n = 0
    while iterations is None or n < iterations:
        command = store.read_control(watch_id)
        if command == "stop":
            return _self_stop(state)
        if command in ("pause", "resume"):
            state.status = "paused" if command == "pause" else "active"
            store.write_watch(state)

        if _ttl_expired(state):
            return _self_stop(state)

        if state.status == "paused":
            state.heartbeat_at = now_iso()  # stay alive, do not poll
            store.write_watch(state)
        else:
            poll_once(state)
            if state.max_fires is not None and state.fire_count >= state.max_fires:
                return _self_stop(state)

        n += 1
        if iterations is None or n < iterations:
            sleeper(state.cadence.interval_seconds)


def _self_stop(state: WatchState) -> None:
    state.status = "stopped"
    store.write_watch(state)


def _ttl_expired(state: WatchState) -> bool:
    if state.ttl_seconds is None or state.created_at is None:
        return False
    return (_parse(now_iso()) - _parse(state.created_at)).total_seconds() >= state.ttl_seconds


def detach() -> None:
    """Classic double-fork + setsid daemonization; detach std streams from the tty."""
    if os.fork() > 0:
        os._exit(0)
    os.setsid()
    if os.fork() > 0:
        os._exit(0)
    devnull = os.open(os.devnull, os.O_RDWR)
    for fd in (0, 1, 2):
        os.dup2(devnull, fd)


def spawn(watch_id: str) -> None:
    """Launch a detached daemon for a watch (the daemon double-forks itself)."""
    import subprocess

    subprocess.Popen(
        [sys.executable, "-m", "picket.daemon", watch_id],
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        close_fds=True,
    )


def main() -> None:
    detach()
    run(sys.argv[1])


if __name__ == "__main__":
    main()
