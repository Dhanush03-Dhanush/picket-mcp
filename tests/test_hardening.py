"""Regression guards for the correctness, validation and security fixes.

Hermetic (no network, no real claude); runs in the default suite. Each test pins
a behaviour the rearchitecture was meant to guarantee.
"""

import psutil
import pytest
from pydantic import ValidationError

from picket.conditions import condition, probes
from picket.core.models import (
    ActiveWindow,
    CadenceSpec,
    EndpointSpec,
    InvalidSpec,
    PredicateSpec,
    WatchState,
)
from picket.execution import handler, runbooks
from picket.persistence import store
from picket.runtime import daemon, watches

EP = {"url": "https://x/spx"}
PR = {"path": "$.last", "op": "lt", "value": 4800}
CAD = {"interval_seconds": 30}


def _register_runbook(home, rb_id="rb", body="#!/bin/sh\nexit 0\n"):
    d = home / "runbooks" / rb_id
    d.mkdir(parents=True)
    (d / "run.sh").write_text(body)
    (d / "run.sh").chmod(0o755)
    return runbooks.register_runbook(rb_id, runbook_type="exec", entry="run.sh")


def _fake_spawn(monkeypatch):
    def fake(watch_id):
        st = store.read_watch(watch_id)
        st.pid, st.pgid = 4242, 4242
        st.proc_create_time = 1.0
        store.write_watch(st)

    monkeypatch.setattr(daemon, "spawn", fake)
    monkeypatch.setattr(watches, "_await_identity", lambda wid, **k: store.read_watch(wid))


def _watchstate(**kw):
    base = dict(
        watch_id="w",
        runbook_id="rb",
        endpoint=EndpointSpec(url="https://x"),
        predicate=PredicateSpec(path="$.last", op="lt", value=1),
        cadence=CadenceSpec(interval_seconds=1),
    )
    base.update(kw)
    return WatchState(**base)


# --- HTTP method restriction (GET/HEAD only) -------------------------------


def test_endpoint_rejects_side_effecting_methods():
    for method in ("POST", "DELETE", "PUT", "PATCH"):
        with pytest.raises(ValidationError):
            EndpointSpec(url="https://x", method=method)
    assert EndpointSpec(url="https://x", method="get").method == "GET"  # normalized
    assert EndpointSpec(url="https://x", method="HEAD").method == "HEAD"


# --- input validation that used to be able to crash a daemon ---------------


def test_active_window_validation():
    with pytest.raises(ValidationError):
        ActiveWindow(tz="Mars/Nowhere")
    with pytest.raises(ValidationError):
        ActiveWindow(start="25:00")
    with pytest.raises(ValidationError):
        ActiveWindow(days=[9])
    ActiveWindow(tz="America/New_York", start="09:30", end="16:00", days=[0, 4])  # ok


@pytest.mark.parametrize(
    "kw",
    [
        {"max_fires": 0},
        {"ttl_seconds": -1},
        {"cooldown_seconds": -1},
        {"debounce_seconds": -1},
        {"max_retries": -1},
        {"handler_timeout_seconds": 0},
        {"delivery_events": ["bogus"]},
    ],
)
def test_bad_limits_rejected(kw):
    with pytest.raises(ValidationError):
        _watchstate(**kw)


def test_negative_jitter_rejected():
    with pytest.raises(ValidationError):
        CadenceSpec(interval_seconds=30, jitter_seconds=-1)


# --- id path-traversal protection ------------------------------------------


def test_safe_id_rejects_traversal(home):
    for bad in ["../evil", "a/b", "..", ".hidden", "", "x" * 65]:
        with pytest.raises(InvalidSpec):
            store.safe_id("runbook", bad)
    assert store.safe_id("runbook", "picket-notify") == "picket-notify"


def test_register_rejects_traversal_id(home):
    with pytest.raises(InvalidSpec):
        runbooks.register_runbook("../evil", runbook_type="exec", entry="x.sh")


# --- pct_change numeric-string baseline (financial APIs) -------------------


def test_pct_change_coerces_string_baseline():
    drop = PredicateSpec(path="$.x", op="pct_change", value=-2)
    assert condition.is_satisfied(drop, 97, baseline="100") is True  # -3% <= -2
    assert condition.is_satisfied(drop, "98.5", baseline="100") is False  # -1.5% not <= -2
    assert condition.is_satisfied(drop, 97, baseline="0") is False  # no baseline to measure


# --- probe verdict robustness ----------------------------------------------


def _probe(home, body, pid="p"):
    d = home / "probes" / pid
    d.mkdir(parents=True)
    (d / "probe.py").write_text(body)
    return probes.register_probe(pid, language="python", entry="probe.py")


def test_probe_fire_false_string_does_not_fire(home):
    p = _probe(home, 'print(\'{"fire": "false"}\')\n')
    assert probes.run_probe(p, {}).fire is False  # a JSON string "false" is NOT truthy


def test_probe_nondict_payload_is_error(home):
    p = _probe(home, 'print(\'{"fire": true, "payload": [1, 2]}\')\n')
    with pytest.raises(probes.ProbeError):
        probes.run_probe(p, {})


# --- one-shot default; recurrence is an explicit opt-in --------------------


def test_one_shot_default_and_recurring_optin(home, monkeypatch):
    _register_runbook(home)
    monkeypatch.setattr(condition, "fetch", lambda ep, **k: {"last": 4850})
    _fake_spawn(monkeypatch)

    default = watches.arm_watch(runbook_id="rb", endpoint=EP, predicate=PR, cadence=CAD)
    assert store.read_watch(default["watch_id"]).max_fires == 1  # safe default

    rec = watches.arm_watch(runbook_id="rb", endpoint=EP, predicate=PR, cadence=CAD, recurring=True)
    assert store.read_watch(rec["watch_id"]).max_fires is None  # explicit opt-in

    n = watches.arm_watch(runbook_id="rb", endpoint=EP, predicate=PR, cadence=CAD, max_fires=5)
    assert store.read_watch(n["watch_id"]).max_fires == 5


# --- pinned revision: re-registration cannot silently retarget a watch -----


def test_pinned_revision_detects_reregistration(home):
    rb1 = _register_runbook(home)
    pinned = rb1.content_hash
    (home / "runbooks" / "rb" / "run.sh").write_text("#!/bin/sh\nexit 0\n# changed\n")
    rb2 = runbooks.register_runbook("rb", runbook_type="exec", entry="run.sh")  # toml re-hashed
    assert rb2.content_hash != pinned
    assert handler._has_drifted(rb2, pinned) is True  # vs the pinned rev -> drift caught
    assert handler._has_drifted(rb2, rb2.content_hash) is False  # vs current -> no drift


# --- untrusted trigger data can't overwrite core payload fields ------------


def test_payload_protects_core_fields_and_carries_idempotency():
    st = WatchState(
        watch_id="wch_1",
        runbook_id="rb",
        probe_id="pr",
        cadence=CadenceSpec(interval_seconds=1),
        episode_seq=3,
    )
    payload = handler.build_payload(
        st, 7, "t", fire_id="fire_9", extra={"watch_id": "HACK", "value": 999, "sym": "SPX"}
    )
    assert payload["watch_id"] == "wch_1" and payload["value"] == 7  # not overwritten
    assert payload["sym"] == "SPX"  # a non-core probe field is allowed through
    assert payload["fire_id"] == "fire_9"
    assert payload["idempotency_key"] == "wch_1:3"  # stable per episode


def test_render_prompt_marks_trigger_untrusted():
    text = runbooks.render_prompt("do it", {"value": 1})
    assert "UNTRUSTED" in text and "do not follow" in text.lower()


# --- delivery sink runs on success (closes the loop) -----------------------


def test_delivery_runs_on_success(home):
    _register_runbook(home)  # exec exit 0 -> completed
    marker = home / "delivered.txt"
    nd = home / "runbooks" / "notifier"
    nd.mkdir(parents=True)
    (nd / "n.sh").write_text(f'#!/bin/sh\necho "$PICKET_PAYLOAD" > "{marker}"\n')
    (nd / "n.sh").chmod(0o755)
    runbooks.register_runbook("notifier", runbook_type="exec", entry="n.sh")

    st = _watchstate(runbook_id="rb", notify_runbook="notifier", baseline=4900)
    rec = handler.fire(st, 4700)
    assert rec["status"] == "completed"
    assert rec["delivery_status"] == "delivered"  # a delivery receipt was recorded
    assert marker.exists() and "completed" in marker.read_text()  # success delivery ran


def test_result_artifact_persists_full_output(home):
    _register_runbook(home, body='#!/bin/sh\necho "the full analysis output"\n')
    rec = handler.fire(_watchstate(runbook_id="rb", baseline=4900), 4700)
    assert rec["result_path"]
    art = store.read_json(store.result_path(rec["fire_id"]))
    assert "the full analysis output" in art["stdout"]  # not just a 2000-char tail


# --- durable ledger: idempotency, leases/recovery, retention, commands -----


def test_idem_key_dedupes_fire(home):
    assert store.create_fire("f1", "w", "pending", idem_key="w:1") is True
    assert store.create_fire("f2", "w", "pending", idem_key="w:1") is False  # duplicate episode
    assert len(store.recent_fires("w")) == 1


def test_recover_abandoned_fails_expired_lease(home):
    store.create_fire("f1", "w", "pending")
    claimed = store.claim_next_fire("w", worker_pid=999, lease_seconds=-1)  # already-expired lease
    assert claimed["fire_id"] == "f1"
    assert store.recover_abandoned("w") == 1
    rec = store.read_fire("f1")
    assert rec["status"] == "failed" and "recovered" in rec["error"]


def test_prune_results_retention(home):
    for i in range(5):
        store.create_fire(f"f{i}", "w", "completed")
    assert store.prune_results(keep=2) == 3
    assert len(store.recent_fires("w")) == 2


def test_command_channel_ack_and_newest_wins(home):
    g1 = store.enqueue_command("w", "pause")
    g2 = store.enqueue_command("w", "stop")
    cid, cmd = store.poll_command("w")
    assert cmd == "stop"  # newest unacked wins
    store.ack_command("w", cid)
    assert store.poll_command("w") is None
    assert store.is_command_acked("w", g1) and store.is_command_acked("w", g2)


def test_recent_fires_survives_many_rows(home):
    for i in range(200):
        store.create_fire(f"f{i}", "w", "completed")
    fires = store.recent_fires("w", limit=20)
    assert len(fires) == 20 and fires[0]["fire_id"] == "f199"  # indexed, newest-first


# --- immediate-stop can find the in-flight handler to cancel ---------------


def test_active_handler_pid_tracked_for_cancellation(home):
    store.create_fire("f1", "w", "running")
    store.set_running_pid("f1", psutil.Process().pid)
    assert store.active_handler_pid("w") == psutil.Process().pid


# --- runtime resilience: a bad fire or transient DB error must not strand a watch ---


class _FakeWorker:
    def __init__(self, *a, **k):
        pass

    def start(self):
        pass

    def drain(self):
        pass

    def cancel(self):
        pass

    def join(self, timeout=None):
        pass


def test_worker_finalizes_fire_when_handler_raises(home, monkeypatch):
    """A crash inside run_fire must finalize the fire as failed, not strand it 'running'."""
    st = _watchstate(watch_id="wch_1", runbook_id="rb")
    store.write_watch(st)
    store.create_fire("f1", "wch_1", "pending")
    claimed = store.claim_next_fire("wch_1", worker_pid=999, lease_seconds=60)  # -> running

    def boom(*a, **k):
        raise RuntimeError("handler blew up")

    monkeypatch.setattr(handler, "run_fire", boom)
    daemon.Worker("wch_1")._execute(st, claimed)  # must NOT propagate

    fire = store.read_fire("f1")
    assert fire["status"] == "failed" and "worker error" in fire["error"]


def test_daemon_loop_survives_transient_poll_error(home, monkeypatch):
    """A transient store/DB error in a poll must skip the tick, not kill the daemon."""
    st = _watchstate(watch_id="wch_1", runbook_id="rb", baseline=4900, max_fires=None)
    store.write_watch(st)
    calls = {"n": 0}

    def flaky(_state):
        calls["n"] += 1
        raise RuntimeError("database is locked")

    monkeypatch.setattr(daemon, "poll_once", flaky)
    daemon.run("wch_1", iterations=3, sleeper=lambda s: None, worker=_FakeWorker())  # no raise
    assert calls["n"] == 3  # kept polling across every error


def test_fetch_caps_oversize_response():
    """A response past the byte cap is an observe error, never an OOM."""
    import httpx

    big = b'{"x":"' + b"a" * 6_000_000 + b'"}'  # > 5 MB cap
    client = httpx.Client(transport=httpx.MockTransport(lambda r: httpx.Response(200, content=big)))
    with pytest.raises(condition.ObserveError):
        condition.fetch(EndpointSpec(url="https://x"), client=client)
