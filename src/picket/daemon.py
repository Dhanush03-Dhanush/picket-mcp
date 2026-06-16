"""The runtime (§4B/§10): one detached daemon per active watcher.

Run as ``python -m picket.daemon <watch_id>``. It double-forks + setsid so that
closing the arming session (or the MCP server) does not SIGHUP it, reloads its
state file, then polls on a fixed cadence: fetch, extract, evaluate the predicate
against the persisted baseline, persist, and on the unsatisfied->satisfied edge
launch the handler. Pure Python — no model runs while it waits.
"""

from __future__ import annotations

import os
import sys
import time
from collections.abc import Callable

import psutil

from picket import condition, handler, store
from picket.condition import ObserveError
from picket.models import WatchState
from picket.store import now_iso


def poll_once(state: WatchState) -> WatchState:
    """One poll: observe, persist, and fire on the rising edge. Mutates + writes state."""
    state.heartbeat_at = now_iso()
    state.last_observed_at = now_iso()
    try:
        data = condition.fetch(state.endpoint)
        value = condition.extract(data, state.predicate.path)
        now_satisfied, should_fire = condition.evaluate(
            state.predicate, value, state.satisfied, state.baseline
        )
    except ObserveError as err:
        state.last_error = str(err)  # could-not-observe != change: never fires
        store.write_watch(state)
        return state

    state.last_value = value
    state.last_error = None
    if should_fire:
        handler.fire(state, value)
        state.fire_count += 1
        state.last_fire_at = now_iso()
        if state.predicate.op == "on_change":
            state.baseline = value  # re-arm against the new value
            now_satisfied = False
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
        poll_once(state)
        n += 1
        if iterations is None or n < iterations:
            sleeper(state.cadence.interval_seconds)


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
