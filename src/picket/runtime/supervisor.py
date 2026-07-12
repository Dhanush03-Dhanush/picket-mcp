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
        if not watches.is_alive(state):
            spawn(watch_id)
            restarted.append(watch_id)
    recovered = store.recover_abandoned()
    pruned = store.prune_results(keep=result_retention)
    return {"ok": True, "restarted": restarted, "recovered": recovered, "pruned": pruned}


def run_forever(interval: float = 30.0) -> None:
    while True:
        try:
            reconcile()
        except Exception:
            pass
        time.sleep(interval)


def main() -> None:
    interval = float(sys.argv[1]) if len(sys.argv) > 1 else 30.0
    run_forever(interval)


if __name__ == "__main__":
    main()
