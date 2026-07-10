"""The runtime: one detached daemon per active watcher, split into two roles.

Run as ``python -m picket.daemon <watch_id>``. It double-forks + setsid so
closing the arming session doesn't SIGHUP it, then runs two cooperating parts:

* the **scheduler** (main thread) polls on the cadence, evaluates the condition,
  and — on the unsatisfied→satisfied edge — records a *durable pending fire* in
  the SQLite ledger. It is the sole writer of the watch row and never blocks on a
  handler.
* the **worker** (:class:`Worker`) leases pending fires from the ledger and
  executes them, so a ten-minute runbook can never pause polling. A crash leaves
  the fire leased; a later start reclaims the abandoned lease (at-most-once).

Stop is acknowledged: a ``stop`` command drains (graceful) or cancels (immediate)
the worker, then records the terminal status. Pure Python in the wait loop — no
model runs while it waits.
"""

from __future__ import annotations

import json
import os
import random
import sys
import threading
import time
from collections.abc import Callable
from datetime import datetime
from zoneinfo import ZoneInfo

import psutil

from picket import condition, handler, probes, store
from picket.condition import ObserveError
from picket.models import CadenceSpec, WatchState
from picket.store import now_iso


def _parse(ts: str) -> datetime:
    return datetime.fromisoformat(ts)


def in_active_window(cadence: CadenceSpec, now: datetime | None = None) -> bool:
    """Whether polling is allowed right now (tz-aware window; supports midnight wrap)."""
    window = cadence.active_window
    if window is None:
        return True
    now = now or datetime.now(ZoneInfo(window.tz))
    if now.weekday() not in window.days:
        return False
    t = now.strftime("%H:%M")
    if window.start <= window.end:
        return window.start <= t <= window.end
    return t >= window.start or t <= window.end  # window wraps past midnight


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


def poll_once(state: WatchState) -> WatchState:
    """One poll: observe, persist, and enqueue a durable fire once per satisfied episode.

    The scheduler decides *whether* to fire and records the intent in the ledger;
    the worker executes it. Mutates + writes the watch row (sole writer).
    """
    now = now_iso()
    state.heartbeat_at = now
    state.last_observed_at = now
    probe_payload = None
    try:
        if state.probe_id:  # condition source B: the probe owns the decision
            result = probes.observe(state)
            value, now_satisfied, probe_payload = result.value, result.fire, result.payload
        else:  # condition source A: fetch + extract + predicate
            data = condition.fetch(state.endpoint)
            value = condition.extract(data, state.predicate.path)
            now_satisfied = condition.is_satisfied(state.predicate, value, state.baseline)
    except (ObserveError, probes.ProbeError) as err:
        state.last_error = str(err)  # could-not-observe != change: never fires
        _log(state, f"observe-error: {err}")
        store.write_watch(state)
        return state

    state.last_value = value
    state.last_error = None
    _log(state, f"observed {value!r} satisfied={now_satisfied}")
    if now_satisfied and not state.satisfied:  # rising edge starts an episode
        state.satisfied_since = now
        state.fired_this_episode = False
        state.episode_seq += 1
    if not now_satisfied:  # episode ended; re-arm
        state.satisfied_since = None
        state.fired_this_episode = False

    if now_satisfied and not state.fired_this_episode and _gates_open(state, now):
        if store.has_active_fire(state.watch_id):  # overlap_policy=drop
            _log(state, "skipped_overlap: a fire is already pending/running")
            handler.record_skipped_overlap(state)
        else:
            fire_id = store.new_fire_id()
            idem = f"{state.watch_id}:{state.episode_seq}"  # stable per episode
            if store.create_fire(
                fire_id, state.watch_id, "pending", runbook_id=state.runbook_id,
                idem_key=idem, value=value, payload=probe_payload,
            ):  # fmt: skip
                _log(state, f"FIRE queued {fire_id} -> runbook {state.runbook_id}")
                state.last_fire_at = now
        state.fired_this_episode = True
        if state.predicate and state.predicate.op == "on_change":
            state.baseline = value  # re-arm against the new value
            now_satisfied = False
            state.satisfied_since = None

    if state.predicate and state.predicate.op == "pct_change":
        if state.predicate.baseline_mode == "last_value":
            state.baseline = value  # track the prior poll for per-interval % change
    state.satisfied = now_satisfied
    state.fire_count = store.count_fires(state.watch_id)
    store.write_watch(state)
    return state


class Worker(threading.Thread):
    """Drains this watch's pending-fire ledger so polling never blocks on a handler."""

    def __init__(self, watch_id: str, *, poll: float = 0.2):
        super().__init__(daemon=True)
        self.watch_id = watch_id
        self._poll = poll
        self._cancel = threading.Event()
        self._drain = threading.Event()
        self._current_pid: int | None = None
        self._lock = threading.Lock()

    def run(self) -> None:
        while not self._cancel.is_set():
            state = store.read_watch(self.watch_id)
            if state is None:
                return
            claimed = store.claim_next_fire(
                self.watch_id, os.getpid(), state.handler_timeout_seconds + 60
            )
            if claimed is None:
                if self._drain.is_set():
                    return  # graceful drain: nothing left to run
                self._cancel.wait(self._poll)
                continue
            self._execute(state, claimed)

    def _execute(self, state: WatchState, claimed: dict) -> None:
        fire_id = claimed["fire_id"]
        if self._cancel.is_set():
            store.finish_fire(fire_id, "failed", error="cancelled before start")
            return
        value = json.loads(claimed["value"]) if claimed["value"] else None
        extra = json.loads(claimed["payload"]) if claimed["payload"] else None

        def on_start(pid: int) -> None:
            with self._lock:
                self._current_pid = pid
            store.set_running_pid(fire_id, pid)

        handler.run_fire(
            state, value, fire_id,
            timeout=state.handler_timeout_seconds, payload_extra=extra, on_start=on_start,
        )  # fmt: skip
        with self._lock:
            self._current_pid = None

    def drain(self) -> None:
        """Finish any pending fires, then exit (graceful stop)."""
        self._drain.set()

    def cancel(self) -> None:
        """Kill the in-flight handler and exit promptly (immediate stop)."""
        self._cancel.set()
        with self._lock:
            if self._current_pid:
                handler._kill_group(self._current_pid)


def _log(state: WatchState, message: str) -> None:
    store.append_log(store.log_path(state.watch_id), f"{now_iso()} {message}")


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
    worker: Worker | None = None,
) -> None:
    """Reload state, recover abandoned fires, then run the scheduler + worker."""
    state = store.read_watch(watch_id)
    if state is None:
        return
    _record_identity(state)
    store.recover_abandoned(watch_id)  # crash recovery: fail leases abandoned by a crash
    if state.status in ("stopping", "errored"):
        state.status = "active"
    store.write_watch(state)

    worker = worker if worker is not None else Worker(watch_id)
    worker.start()
    stopped = False
    n = 0
    try:
        while iterations is None or n < iterations:
            cmd = store.poll_command(watch_id)
            if cmd:
                cmd_id, name = cmd
                if name == "stop":
                    state.status = "stopping"
                    store.write_watch(state)
                    store.ack_command(watch_id, cmd_id)
                    stopped = True
                    break
                if name in ("pause", "resume"):
                    state.status = "paused" if name == "pause" else "active"
                    state.desired_status = state.status
                    store.write_watch(state)
                    store.ack_command(watch_id, cmd_id)

            if _ttl_expired(state):
                stopped = True
                break

            if state.status == "paused" or not in_active_window(state.cadence):
                state.heartbeat_at = now_iso()  # stay alive, do not poll
                store.write_watch(state)
            else:
                poll_once(state)
                if state.max_fires is not None and store.count_fires(watch_id) >= state.max_fires:
                    stopped = True
                    break

            n += 1
            if iterations is None or n < iterations:
                jitter = random.uniform(0, state.cadence.jitter_seconds)
                sleeper(state.cadence.interval_seconds + jitter)
    finally:
        _shutdown(worker, watch_id, drain=stopped, mark_stopped=stopped)


def _shutdown(worker: Worker, watch_id: str, *, drain: bool, mark_stopped: bool) -> None:
    """Stop the worker (drain lets in-flight finish), then record the terminal status."""
    if drain:
        st = store.read_watch(watch_id)
        timeout = (st.handler_timeout_seconds + 30) if st else 60
        worker.drain()
        worker.join(timeout=timeout)
    worker.cancel()
    worker.join(timeout=5)
    if mark_stopped:
        st = store.read_watch(watch_id)
        if st is not None:
            st.status = "stopped"
            st.desired_status = "stopped"
            store.write_watch(st)


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
