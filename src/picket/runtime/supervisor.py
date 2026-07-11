"""Supervisor: restore desired watches after crashes/reboots and recover fires.

Picket normally needs no long-running service — ``arm`` spawns a detached daemon
and you walk away. But a daemon can die (crash, OOM, reboot). The supervisor is
an *optional* loop you wire to launchd/systemd (see the README) that periodically
reconciles desired vs actual: it re-spawns a daemon for any watch whose
``desired_status`` is ``active`` but whose daemon is gone, fails fires abandoned
by a crashed worker (via their expired lease), and prunes old result artifacts.
The same sweep is exposed as the ``reconcile`` MCP tool for on-demand use.
"""

from __future__ import annotations

import sys
import time

from picket.persistence import store
from picket.runtime import watches
from picket.runtime.daemon import spawn


def reconcile(*, result_retention: int = 500) -> dict:
    """One sweep: restart dead desired-active watches, recover fires, prune results."""
    store.ensure_root()
    restarted = []
    for watch_id in store.watch_ids_by_desired("active"):
        state = store.read_watch(watch_id)
        if state is None or state.status not in ("active", "errored"):
            continue
        if not watches.is_alive(state):  # daemon gone: bring it back
            spawn(watch_id)
            restarted.append(watch_id)
    recovered = store.recover_abandoned()  # global lease recovery
    pruned = store.prune_results(keep=result_retention)
    return {"ok": True, "restarted": restarted, "recovered": recovered, "pruned": pruned}


def run_forever(interval: float = 30.0) -> None:
    while True:
        try:
            reconcile()
        except Exception:  # a supervisor must never die on a transient error
            pass
        time.sleep(interval)


def main() -> None:
    interval = float(sys.argv[1]) if len(sys.argv) > 1 else 30.0
    run_forever(interval)


if __name__ == "__main__":
    main()
