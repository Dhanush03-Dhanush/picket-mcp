"""Opt-in real-process smoke tests (NEW-14/NEW-15).

These spawn REAL detached daemons that poll a REAL HTTP server and fire REAL exec
handlers — the seams the hermetic suite mocks. They stay cheap (exec only: no
claude, no tokens; sub-second intervals; max_fires=1 so each watcher fires once
and self-stops) and self-clean (temp PICKET_HOME + a teardown that reaps every
daemon — see conftest `smoke_home`).

Deselected from the default run; execute with:  uv run pytest -m smoke
"""

import httpx
import psutil
import pytest

from picket import store, watches

pytestmark = pytest.mark.smoke

# A real, no-auth public API for the monitoring example (mirrors the SPX use case).
PUBLIC_API = "https://api.coinbase.com/v2/prices/BTC-USD/spot"


def test_smoke_arm_fires_real_exec_handler_then_self_stops(
    smoke_home, server, exec_runbook, poll_until
):
    url, value = server
    marker = smoke_home / "fired.txt"
    exec_runbook(f'#!/bin/sh\nprintf "%s" "$PICKET_PAYLOAD" > "{marker}"\n')

    res = watches.arm_watch(
        runbook_id="rb",
        endpoint={"url": url},
        predicate={"path": "$.last", "op": "lt", "value": 4800},
        cadence={"interval_seconds": 0.2},
        max_fires=1,
    )
    assert res["ok"] and psutil.pid_exists(res["pid"])

    value["last"] = 4700  # the real daemon observes this on its next poll and fires
    assert poll_until(lambda: store.read_jsonl(store.fires_path(res["watch_id"])))
    assert store.read_jsonl(store.fires_path(res["watch_id"]))[-1]["status"] == "completed"
    assert marker.exists() and res["watch_id"] in marker.read_text()
    assert poll_until(lambda: store.read_watch(res["watch_id"]).status == "stopped")  # max_fires=1


def test_smoke_stop_verify_before_kill_and_pause_resume(
    smoke_home, server, exec_runbook, poll_until
):
    url, _ = server
    exec_runbook("#!/bin/sh\nexit 0\n")

    res = watches.arm_watch(
        runbook_id="rb",
        endpoint={"url": url},
        predicate={"path": "$.last", "op": "lt", "value": 1},  # 5000 < 1 is never true
        cadence={"interval_seconds": 0.2},
    )
    pid = res["pid"]
    assert psutil.pid_exists(pid)

    watches.pause_watch(res["watch_id"])
    assert poll_until(lambda: store.read_watch(res["watch_id"]).status == "paused")
    watches.resume_watch(res["watch_id"])
    assert poll_until(lambda: store.read_watch(res["watch_id"]).status == "active")

    watches.stop_watch(res["watch_id"], mode="immediate")
    assert poll_until(lambda: not psutil.pid_exists(pid), timeout=5)  # verify-before-kill


@pytest.mark.parametrize(
    "initial, predicate, trigger",
    [
        (100, {"path": "$.last", "op": "pct_change", "value": -2, "baseline_mode": "arm_time"}, 97),
        (5, {"path": "$.last", "op": "crosses_above", "value": 10}, 12),
        (1, {"path": "$.last", "op": "on_change"}, 2),
    ],
)
def test_smoke_conditional_daemon_fires_once(
    smoke_home, server, exec_runbook, poll_until, initial, predicate, trigger
):
    """A real daemon firing once on the crossing, across the richer predicate types."""
    url, value = server
    value["last"] = initial  # set before arm so the baseline is captured correctly
    exec_runbook("#!/bin/sh\nexit 0\n")

    res = watches.arm_watch(
        runbook_id="rb",
        endpoint={"url": url},
        predicate=predicate,
        cadence={"interval_seconds": 0.2},
        max_fires=1,
    )
    assert res["ok"]
    value["last"] = trigger  # the real daemon observes the crossing and fires

    assert poll_until(lambda: store.read_jsonl(store.fires_path(res["watch_id"])))
    assert store.read_jsonl(store.fires_path(res["watch_id"]))[-1]["status"] == "completed"
    assert poll_until(lambda: store.read_watch(res["watch_id"]).status == "stopped")


def test_smoke_probe_daemon_fires_real_exec_handler_then_self_stops(
    smoke_home, probe, exec_runbook, poll_until
):
    """A real detached daemon runs a REAL probe script each tick; when the probe returns
    fire=true the exec runbook fires once (max_fires=1) and the watcher self-stops. The
    probe's payload reaches the runbook via PICKET_PAYLOAD (probe_id in the trigger)."""
    trigger = smoke_home / "trigger.flag"
    marker = smoke_home / "probe_fired.txt"
    probe(
        "import json, os\n"
        "p = json.loads(os.environ['PICKET_PARAMS'])\n"
        "fire = os.path.exists(p['trigger_file'])\n"
        "print(json.dumps({'fire': fire, 'value': fire, 'payload': {'src': 'probe'}}))\n"
    )
    exec_runbook(f'#!/bin/sh\nprintf "%s" "$PICKET_PAYLOAD" > "{marker}"\n')

    res = watches.arm_watch(
        runbook_id="rb",
        probe_id="pr",
        probe_params={"trigger_file": str(trigger)},
        cadence={"interval_seconds": 0.2},
        max_fires=1,
    )
    assert res["ok"] and psutil.pid_exists(res["pid"])

    trigger.touch()  # the real daemon's next probe run now returns fire=true
    assert poll_until(lambda: store.read_jsonl(store.fires_path(res["watch_id"])))
    assert store.read_jsonl(store.fires_path(res["watch_id"]))[-1]["status"] == "completed"
    assert marker.exists() and "probe_id" in marker.read_text()  # probe payload reached runbook
    assert poll_until(lambda: store.read_watch(res["watch_id"]).status == "stopped")  # max_fires=1


def test_smoke_public_api_monitor_fires_once_and_cleans_up(smoke_home, exec_runbook, poll_until):
    """Mimic "watch the SPX endpoint, run my runbook on the condition" against a real,
    no-auth public API. The predicate (price > 0) is guaranteed true so this one-shot
    fires deterministically (real use would be pct_change / lt a real threshold);
    max_fires=1 self-stops and the daemon is reaped on teardown."""
    try:
        httpx.get(PUBLIC_API, timeout=5).raise_for_status()
    except (httpx.HTTPError, OSError):
        pytest.skip("public API unreachable")

    marker = smoke_home / "public_fired.txt"
    exec_runbook(f'#!/bin/sh\nprintf "%s" "$PICKET_PAYLOAD" > "{marker}"\n')

    res = watches.arm_watch(
        runbook_id="rb",
        endpoint={"url": PUBLIC_API},
        predicate={"path": "$.data.amount", "op": "gt", "value": 0},
        cadence={"interval_seconds": 1},
        max_fires=1,
    )
    assert res["ok"]

    assert poll_until(lambda: store.read_watch(res["watch_id"]).status == "stopped", timeout=20)
    assert marker.exists()
    assert store.read_jsonl(store.fires_path(res["watch_id"]))[-1]["status"] == "completed"
