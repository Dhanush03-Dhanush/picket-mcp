import os

import psutil
import pytest
from pydantic import ValidationError

from picket.conditions import probes
from picket.core.models import CadenceSpec, EndpointSpec, PredicateSpec, WatchState
from picket.execution import handler, runbooks
from picket.persistence import store
from picket.runtime import daemon, watches

EP = {"url": "https://x/spx"}
PR = {"path": "$.last", "op": "lt", "value": 4800}
CAD = {"interval_seconds": 30}
FIRE_BODY = (
    "import json\nprint(json.dumps({'fire': True, 'value': 7, 'payload': {'sym': 'SPX'}}))\n"
)


def _register_runbook(home, rb_id="rb"):
    d = home / "runbooks" / rb_id
    d.mkdir(parents=True)
    (d / "run.sh").write_text("#!/bin/sh\nexit 0\n")
    runbooks.register_runbook(rb_id, runbook_type="exec", entry="run.sh")


def _register_probe(home, body, probe_id="pr"):
    d = home / "probes" / probe_id
    d.mkdir(parents=True)
    (d / "probe.py").write_text(body)
    return probes.register_probe(probe_id, language="python", entry="probe.py")


def _fake_spawn(monkeypatch):
    def fake(watch_id):
        st = store.read_watch(watch_id)
        st.pid, st.pgid = os.getpid(), os.getpgid(os.getpid())
        st.proc_create_time = psutil.Process().create_time()
        store.write_watch(st)

    monkeypatch.setattr(daemon, "spawn", fake)


def _probe_state(**kw):
    return WatchState(
        watch_id="wch_p",
        runbook_id="rb",
        probe_id="pr",
        probe_params={"floor": 100},
        cadence=CadenceSpec(interval_seconds=1),
        **kw,
    )


# --- WatchState invariant ---------------------------------------------------


def test_watchstate_rejects_two_sources():
    with pytest.raises(ValidationError):
        WatchState(
            watch_id="w",
            runbook_id="rb",
            probe_id="pr",
            endpoint=EndpointSpec(url="https://x"),
            predicate=PredicateSpec(path="$.a", op="on_change"),
            cadence=CadenceSpec(interval_seconds=1),
        )


def test_watchstate_rejects_zero_sources():
    with pytest.raises(ValidationError):
        WatchState(watch_id="w", runbook_id="rb", cadence=CadenceSpec(interval_seconds=1))


# --- poll_once in probe mode (reuses the edge/gating pipeline) ---------------


def test_poll_once_probe_enqueues_fire_with_payload(home, monkeypatch):
    monkeypatch.setattr(probes, "observe", lambda st: probes.ProbeResult(True, 7, {"sym": "SPX"}))
    st = daemon.poll_once(_probe_state())
    assert st.fire_count == 1 and st.last_value == 7 and st.satisfied is True
    fire = store.recent_fires("wch_p")[0]  # probe value + payload persisted on the fire
    assert fire["value"] == 7 and fire["payload"] == {"sym": "SPX"}


def test_poll_once_probe_no_fire(home, monkeypatch):
    monkeypatch.setattr(handler, "fire", lambda *a, **k: pytest.fail("must not fire"))
    monkeypatch.setattr(probes, "observe", lambda st: probes.ProbeResult(False, 3))
    st = daemon.poll_once(_probe_state())
    assert st.fire_count == 0 and st.satisfied is False and st.last_value == 3


def test_poll_once_probe_error_never_fires(home, monkeypatch):
    monkeypatch.setattr(handler, "fire", lambda *a, **k: pytest.fail("must not fire"))

    def boom(st):
        raise probes.ProbeError("bad exit")

    monkeypatch.setattr(probes, "observe", boom)
    st = daemon.poll_once(_probe_state())
    assert st.fire_count == 0 and st.last_error == "bad exit"


def test_poll_once_probe_fires_once_per_episode(home, monkeypatch):
    results = iter(
        [
            probes.ProbeResult(True, 1),
            probes.ProbeResult(True, 1),  # still satisfied -> no re-fire
            probes.ProbeResult(False, 0),  # episode ends
            probes.ProbeResult(True, 2),  # new episode -> fire
        ]
    )
    monkeypatch.setattr(probes, "observe", lambda st: next(results))
    st = _probe_state()
    for _ in range(4):
        daemon.poll_once(st)
        for f in store.recent_fires("wch_p"):  # a worker completes each fire between episodes
            if f["status"] == "pending":
                store.finish_fire(f["fire_id"], "completed")
    assert [f["value"] for f in reversed(store.recent_fires("wch_p"))] == [1, 2]
    assert st.fire_count == 2


def test_build_payload_probe_mode(home):
    payload = handler.build_payload(_probe_state(), 7, "2026-01-01T00:00:00Z", extra={"sym": "SPX"})
    assert payload["probe_id"] == "pr" and payload["sym"] == "SPX" and payload["value"] == 7
    assert "predicate" not in payload and "endpoint_url" not in payload


# --- arm_watch in probe mode ------------------------------------------------


def test_arm_watch_probe_success(home, monkeypatch):
    _register_runbook(home)
    _register_probe(home, FIRE_BODY)
    _fake_spawn(monkeypatch)
    res = watches.arm_watch(
        runbook_id="rb", probe_id="pr", probe_params={"floor": 100}, cadence=CAD, label="p"
    )
    assert res["ok"] and res["trial_value"] == 7 and res["baseline"] is None
    st = store.read_watch(res["watch_id"])
    assert st.probe_id == "pr" and st.probe_params == {"floor": 100}
    assert st.endpoint is None and st.predicate is None


def test_arm_watch_rejects_two_sources(home):
    _register_runbook(home)
    res = watches.arm_watch(runbook_id="rb", endpoint=EP, predicate=PR, probe_id="pr", cadence=CAD)
    assert res["error_code"] == "INVALID_SPEC"


def test_arm_watch_rejects_zero_sources(home):
    _register_runbook(home)
    assert watches.arm_watch(runbook_id="rb", cadence=CAD)["error_code"] == "INVALID_SPEC"


def test_arm_watch_probe_not_found(home):
    _register_runbook(home)
    res = watches.arm_watch(runbook_id="rb", probe_id="ghost", cadence=CAD)
    assert res["error_code"] == "PROBE_NOT_FOUND"


def test_arm_watch_probe_failed(home):
    _register_runbook(home)
    _register_probe(home, "import sys\nsys.exit(2)\n")
    res = watches.arm_watch(runbook_id="rb", probe_id="pr", cadence=CAD)
    assert res["error_code"] == "PROBE_FAILED"
