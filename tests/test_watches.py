import os
import subprocess
import sys
import time

import psutil

from picket import condition, daemon, runbooks, store, watches
from picket.models import CadenceSpec, EndpointSpec, PredicateSpec, WatchState

EP = {"url": "https://x/spx"}
PR = {"path": "$.last", "op": "lt", "value": 4800}
CAD = {"interval_seconds": 30}


def _register_runbook(home, rb_id="rb"):
    d = home / "runbooks" / rb_id
    d.mkdir(parents=True)
    (d / "run.sh").write_text("#!/bin/sh\nexit 0\n")
    runbooks.register_runbook(rb_id, runbook_type="exec", entry="run.sh")


def _fake_spawn(monkeypatch):
    """Simulate the daemon recording its identity into the state file."""

    def fake(watch_id):
        st = store.read_watch(watch_id)
        st.pid = os.getpid()
        st.pgid = os.getpgid(os.getpid())
        st.proc_create_time = psutil.Process().create_time()
        store.write_watch(st)

    monkeypatch.setattr(daemon, "spawn", fake)


def _state(watch_id, **kw):
    return WatchState(
        watch_id=watch_id,
        runbook_id="rb",
        endpoint=EndpointSpec(**EP),
        predicate=PredicateSpec(**PR),
        cadence=CadenceSpec(**CAD),
        **kw,
    )


# --- arm_watch -------------------------------------------------------------


def test_arm_watch_success(home, monkeypatch):
    _register_runbook(home)
    monkeypatch.setattr(condition, "fetch", lambda ep, **k: {"last": 4850})
    _fake_spawn(monkeypatch)

    res = watches.arm_watch(runbook_id="rb", endpoint=EP, predicate=PR, cadence=CAD, label="spx")
    assert res["ok"] and res["watch_id"].startswith("wch_")
    assert res["pid"] == os.getpid()
    assert res["trial_value"] == 4850 and res["baseline"] is None

    state = store.read_watch(res["watch_id"])
    assert state.status == "active" and state.label == "spx"


def test_arm_watch_on_change_persists_baseline(home, monkeypatch):
    _register_runbook(home)
    monkeypatch.setattr(condition, "fetch", lambda ep, **k: {"last": 100})
    _fake_spawn(monkeypatch)

    res = watches.arm_watch(
        runbook_id="rb", endpoint=EP, predicate={"path": "$.last", "op": "on_change"}, cadence=CAD
    )
    assert res["baseline"] == 100


def test_arm_pct_change_prior_close_persists_baseline(home, monkeypatch):
    _register_runbook(home)
    monkeypatch.setattr(condition, "fetch", lambda ep, **k: {"last": 4850, "prev_close": 4900})
    _fake_spawn(monkeypatch)

    res = watches.arm_watch(
        runbook_id="rb",
        endpoint=EP,
        predicate={
            "path": "$.last",
            "op": "pct_change",
            "value": -2,
            "baseline_mode": "prior_close",
            "baseline_path": "$.prev_close",
        },
        cadence=CAD,
    )
    assert res["baseline"] == 4900
    # persisted at arm time -> a restart (re-read) restores it without recompute
    assert store.read_watch(res["watch_id"]).baseline == 4900


def test_arm_watch_invalid_spec(home):
    res = watches.arm_watch(
        runbook_id="rb", endpoint={"url": "ftp://no"}, predicate=PR, cadence=CAD
    )
    assert res["error_code"] == "INVALID_SPEC"


def test_arm_watch_runbook_not_found(home):
    res = watches.arm_watch(runbook_id="ghost", endpoint=EP, predicate=PR, cadence=CAD)
    assert res["error_code"] == "RUNBOOK_NOT_FOUND"


def test_arm_watch_endpoint_unreachable(home, monkeypatch):
    _register_runbook(home)

    def boom(ep, **k):
        raise condition.ObserveError("connection refused")

    monkeypatch.setattr(condition, "fetch", boom)
    res = watches.arm_watch(runbook_id="rb", endpoint=EP, predicate=PR, cadence=CAD)
    assert res["error_code"] == "ENDPOINT_UNREACHABLE"


# --- list / get ------------------------------------------------------------


def test_list_watches_with_liveness_and_filter(home, monkeypatch):
    _register_runbook(home)
    monkeypatch.setattr(condition, "fetch", lambda ep, **k: {"last": 4850})
    _fake_spawn(monkeypatch)
    watches.arm_watch(runbook_id="rb", endpoint=EP, predicate=PR, cadence=CAD)
    watches.arm_watch(runbook_id="rb", endpoint=EP, predicate=PR, cadence=CAD)

    listed = watches.list_watches()["watches"]
    assert len(listed) == 2
    assert all(row["alive"] for row in listed)
    assert listed[0]["cadence_summary"] == "every 30s"
    assert watches.list_watches(status_filter="stopped")["watches"] == []


def test_get_watch_returns_state_and_recent_fire(home, monkeypatch):
    _register_runbook(home)
    monkeypatch.setattr(condition, "fetch", lambda ep, **k: {"last": 4850})
    _fake_spawn(monkeypatch)
    watch_id = watches.arm_watch(runbook_id="rb", endpoint=EP, predicate=PR, cadence=CAD)[
        "watch_id"
    ]
    store.append_jsonl(store.fires_path(watch_id), {"fire_id": "fire_1", "status": "completed"})

    res = watches.get_watch(watch_id)
    assert res["watch"]["watch_id"] == watch_id
    assert res["most_recent_fire"]["fire_id"] == "fire_1"
    assert res["alive"] is True and res["log_tail"] == []


def test_get_watch_not_found(home):
    assert watches.get_watch("wch_nope")["error_code"] == "NOT_FOUND"


# --- stop ------------------------------------------------------------------


def test_stop_watch_graceful_writes_control_and_is_idempotent(home):
    store.write_watch(_state("wch_g", status="active"))  # no live pid -> treated as dead
    res = watches.stop_watch("wch_g", mode="graceful")
    assert res["final_status"] == "stopped"
    assert store.read_watch("wch_g").status == "stopped"
    # second call is idempotent
    assert watches.stop_watch("wch_g")["error_code"] == "ALREADY_STOPPED"


def test_stop_watch_not_found(home):
    assert watches.stop_watch("wch_nope")["error_code"] == "NOT_FOUND"


def test_pause_and_resume_write_control(home):
    store.write_watch(_state("wch_p", status="active"))
    assert watches.pause_watch("wch_p")["requested"] == "pause"
    assert store.control_path("wch_p").read_text() == "pause"
    assert watches.resume_watch("wch_p")["requested"] == "resume"
    assert store.control_path("wch_p").read_text() == "resume"


def test_pause_rejects_stopped_watch(home):
    store.write_watch(_state("wch_s", status="stopped"))
    assert watches.pause_watch("wch_s")["error_code"] == "ALREADY_STOPPED"


def test_pause_not_found(home):
    assert watches.pause_watch("wch_nope")["error_code"] == "NOT_FOUND"


def test_verify_before_kill_rejects_reused_pid(home):
    state = _state("wch_r", pid=os.getpid(), pgid=os.getpgid(0), proc_create_time=1.0)
    assert watches.is_alive(state) is False  # create_time mismatch => not our process


def test_stop_watch_immediate_kills_real_process_group(home):
    proc = subprocess.Popen(
        [sys.executable, "-c", "import time; time.sleep(30)"], start_new_session=True
    )
    try:
        store.write_watch(
            _state(
                "wch_kill",
                status="active",
                pid=proc.pid,
                pgid=os.getpgid(proc.pid),
                proc_create_time=psutil.Process(proc.pid).create_time(),
            )
        )
        res = watches.stop_watch("wch_kill", mode="immediate")
        assert res["final_status"] == "stopped"
        for _ in range(60):
            if proc.poll() is not None:
                break
            time.sleep(0.05)
        assert proc.poll() is not None  # the daemon process group was terminated
    finally:
        if proc.poll() is None:
            proc.kill()
