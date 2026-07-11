"""Opt-in real-process stress / durability tests (run with -m stress).

These spawn REAL detached daemons and exercise the failure paths the happy-path
smoke suite doesn't: a hard daemon crash + supervisor restart, cancelling an
in-flight handler on immediate stop, and many daemons contending on one SQLite
DB. Cheap (exec only: no claude, no tokens) and self-cleaning via `smoke_home`.
"""

import os
import signal

import psutil
import pytest

from picket.persistence import store
from picket.runtime import supervisor, watches

pytestmark = pytest.mark.stress


def test_stress_supervisor_restarts_crashed_daemon(smoke_home, server, exec_runbook, poll_until):
    """Hard-kill a daemon's process group; the supervisor reconcile brings it back."""
    url, _ = server
    exec_runbook("#!/bin/sh\nexit 0\n")
    res = watches.arm_watch(
        runbook_id="rb",
        endpoint={"url": url},
        predicate={"path": "$.last", "op": "lt", "value": 1},  # never true: just polls
        cadence={"interval_seconds": 0.2},
        recurring=True,
    )
    old_pid = res["pid"]
    assert psutil.pid_exists(old_pid)

    os.killpg(store.read_watch(res["watch_id"]).pgid, signal.SIGKILL)  # simulate a crash
    assert poll_until(lambda: not psutil.pid_exists(old_pid), timeout=5)
    assert watches.effective_status(store.read_watch(res["watch_id"])) == "errored"

    out = supervisor.reconcile()
    assert res["watch_id"] in out["restarted"]
    assert poll_until(lambda: watches.is_alive(store.read_watch(res["watch_id"])), timeout=5)
    assert store.read_watch(res["watch_id"]).pid != old_pid  # a fresh daemon took over


def test_stress_immediate_stop_cancels_inflight_handler(
    smoke_home, server, exec_runbook, poll_until
):
    """An immediate stop must cancel the in-flight handler — no side effect outlives it."""
    url, value = server
    value["last"] = 4700  # already satisfied: the daemon fires on its first poll
    exec_runbook("#!/bin/sh\nsleep 30\n")  # a long-running handler
    res = watches.arm_watch(
        runbook_id="rb",
        endpoint={"url": url},
        predicate={"path": "$.last", "op": "lt", "value": 4800},
        cadence={"interval_seconds": 0.2},
        recurring=True,
    )
    assert poll_until(lambda: store.active_handler_pid(res["watch_id"]) is not None, timeout=5)
    hpid = store.active_handler_pid(res["watch_id"])
    assert psutil.pid_exists(hpid)

    watches.stop_watch(res["watch_id"], mode="immediate")
    assert poll_until(lambda: not psutil.pid_exists(hpid), timeout=5)  # handler was cancelled
    assert store.read_watch(res["watch_id"]).status == "stopped"


def test_stress_many_daemons_all_fire_and_ledger_consistent(
    smoke_home, server, exec_runbook, poll_until
):
    """Eight concurrent daemons write one SQLite ledger under WAL; each fires exactly once."""
    url, value = server
    value["last"] = 4700
    exec_runbook("#!/bin/sh\nexit 0\n")
    ids = []
    for _ in range(8):
        res = watches.arm_watch(
            runbook_id="rb",
            endpoint={"url": url},
            predicate={"path": "$.last", "op": "lt", "value": 4800},
            cadence={"interval_seconds": 0.1},
            max_fires=1,
        )
        assert res["ok"]
        ids.append(res["watch_id"])

    for wid in ids:
        assert poll_until(lambda wid=wid: store.read_watch(wid).status == "stopped", timeout=20)

    completed = [f for f in store.recent_fires(limit=200) if f["status"] == "completed"]
    assert len(completed) == 8  # every watcher fired exactly once; no lost/duplicated rows
