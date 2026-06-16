import os
from datetime import UTC, datetime, timedelta

import psutil
import pytest

from picket import condition, daemon, handler, store
from picket.models import CadenceSpec, EndpointSpec, PredicateSpec, WatchState


def _state(predicate=None, baseline=4900, **kw):
    return WatchState(
        watch_id="wch_1",
        runbook_id="rb",
        endpoint=EndpointSpec(url="https://x/spx"),
        predicate=predicate or PredicateSpec(path="$.last", op="lt", value=4800),
        cadence=CadenceSpec(interval_seconds=1),
        baseline=baseline,
        **kw,
    )


def _no_real_fire(monkeypatch):
    fired = []
    monkeypatch.setattr(handler, "fire", lambda st, v, **k: fired.append(v))
    return fired


def test_poll_once_fires_on_rising_edge(home, monkeypatch):
    fired = _no_real_fire(monkeypatch)
    monkeypatch.setattr(condition, "fetch", lambda ep, **k: {"last": 4700})
    st = daemon.poll_once(_state())
    assert fired == [4700]
    assert st.fire_count == 1 and st.satisfied is True
    assert st.last_value == 4700 and st.last_error is None
    assert st.heartbeat_at and st.last_fire_at


def test_poll_once_no_fire_when_unsatisfied(home, monkeypatch):
    fired = _no_real_fire(monkeypatch)
    monkeypatch.setattr(condition, "fetch", lambda ep, **k: {"last": 4850})
    st = daemon.poll_once(_state())
    assert fired == [] and st.fire_count == 0 and st.satisfied is False
    assert st.last_value == 4850


def test_poll_once_no_refire_while_satisfied(home, monkeypatch):
    fired = _no_real_fire(monkeypatch)
    monkeypatch.setattr(condition, "fetch", lambda ep, **k: {"last": 4700})
    st = _state()
    st.satisfied = True  # already fired earlier in this episode
    st.fired_this_episode = True
    daemon.poll_once(st)
    assert fired == [] and st.fire_count == 0


def test_poll_once_observe_error_never_fires(home, monkeypatch):
    fired = _no_real_fire(monkeypatch)

    def boom(ep, **k):
        raise condition.ObserveError("down")

    monkeypatch.setattr(condition, "fetch", boom)
    st = daemon.poll_once(_state())
    assert fired == [] and st.fire_count == 0
    assert st.last_error == "down"


def test_on_change_rearms_baseline_after_fire(home, monkeypatch):
    fired = _no_real_fire(monkeypatch)
    values = iter([{"last": 6}, {"last": 6}])
    monkeypatch.setattr(condition, "fetch", lambda ep, **k: next(values))
    st = _state(predicate=PredicateSpec(path="$.last", op="on_change"), baseline=5)

    daemon.poll_once(st)  # 6 != 5 -> fire, re-arm baseline to 6
    assert st.fire_count == 1 and st.baseline == 6 and st.satisfied is False
    daemon.poll_once(st)  # 6 == 6 -> no change
    assert st.fire_count == 1 and fired == [6]


def test_run_polls_persists_and_records_identity(home, monkeypatch):
    _no_real_fire(monkeypatch)
    values = iter([{"last": 4850}, {"last": 4700}])
    monkeypatch.setattr(condition, "fetch", lambda ep, **k: next(values))
    store.write_watch(_state())

    daemon.run("wch_1", iterations=2, sleeper=lambda s: None)

    final = store.read_watch("wch_1")
    assert final.last_value == 4700
    assert final.fire_count == 1  # fired on the 2nd poll's crossing
    assert final.pid == os.getpid()
    assert final.pgid == os.getpgid(os.getpid())
    assert final.heartbeat_at is not None


def test_run_on_missing_state_is_noop(home):
    daemon.run("wch_missing", iterations=1, sleeper=lambda s: None)  # must not raise


def test_record_identity(home):
    st = _state()
    daemon._record_identity(st)
    assert st.pid == os.getpid()
    assert abs(st.proc_create_time - psutil.Process().create_time()) < 1.0


# --- NEW-9: concurrency, gating, limits -----------------------------------


def _past(seconds):
    return (datetime.now(UTC) - timedelta(seconds=seconds)).isoformat()


def test_debounce_delays_fire_until_condition_held(home, monkeypatch):
    fired = _no_real_fire(monkeypatch)
    monkeypatch.setattr(condition, "fetch", lambda ep, **k: {"last": 4700})
    st = _state()
    st.debounce_seconds = 10

    daemon.poll_once(st)  # rising edge, but not held long enough
    assert st.fire_count == 0 and st.satisfied_since is not None

    st.satisfied_since = _past(11)  # condition has now held > debounce
    daemon.poll_once(st)
    assert fired == [4700] and st.fire_count == 1


def test_cooldown_blocks_rapid_refire(home, monkeypatch):
    _no_real_fire(monkeypatch)
    values = iter([{"last": 4700}, {"last": 4850}, {"last": 4700}])
    monkeypatch.setattr(condition, "fetch", lambda ep, **k: next(values))
    st = _state()
    st.cooldown_seconds = 60

    daemon.poll_once(st)  # fire 1
    daemon.poll_once(st)  # unsatisfied -> episode ends
    daemon.poll_once(st)  # satisfied again, but within cooldown
    assert st.fire_count == 1

    st.last_fire_at = _past(61)  # cooldown elapsed
    monkeypatch.setattr(condition, "fetch", lambda ep, **k: {"last": 4700})
    daemon.poll_once(st)
    assert st.fire_count == 2


def test_overlap_lock_records_skipped_overlap(home, monkeypatch):
    # hold the in-flight lock externally; a crossing must not run concurrently
    monkeypatch.setattr(handler, "fire", lambda *a, **k: pytest.fail("must not fire under lock"))
    monkeypatch.setattr(condition, "fetch", lambda ep, **k: {"last": 4700})
    held = daemon._acquire_lock("wch_1")
    try:
        st = daemon.poll_once(_state())
    finally:
        held.close()
    assert st.fire_count == 0
    assert store.read_jsonl(store.fires_path("wch_1"))[-1]["status"] == "skipped_overlap"


def test_pause_then_resume_via_control(home, monkeypatch):
    _no_real_fire(monkeypatch)
    monkeypatch.setattr(condition, "fetch", lambda ep, **k: {"last": 4850})
    store.write_watch(_state())

    store.write_control("wch_1", "pause")
    daemon.run("wch_1", iterations=1, sleeper=lambda s: None)
    paused = store.read_watch("wch_1")
    assert paused.status == "paused" and paused.last_value is None  # polling skipped

    store.write_control("wch_1", "resume")
    daemon.run("wch_1", iterations=1, sleeper=lambda s: None)
    resumed = store.read_watch("wch_1")
    assert resumed.status == "active" and resumed.baseline == 4900  # baseline preserved


def test_max_fires_self_stops(home, monkeypatch):
    _no_real_fire(monkeypatch)
    monkeypatch.setattr(condition, "fetch", lambda ep, **k: {"last": 4700})
    st = _state()
    st.max_fires = 1
    store.write_watch(st)

    daemon.run("wch_1", iterations=5, sleeper=lambda s: None)
    final = store.read_watch("wch_1")
    assert final.fire_count == 1 and final.status == "stopped"


def test_ttl_self_stops(home, monkeypatch):
    monkeypatch.setattr(condition, "fetch", lambda ep, **k: {"last": 4850})
    st = _state()
    st.ttl_seconds = 10
    st.created_at = _past(100)
    store.write_watch(st)

    daemon.run("wch_1", iterations=5, sleeper=lambda s: None)
    assert store.read_watch("wch_1").status == "stopped"


def test_poll_once_writes_poll_log(home, monkeypatch):
    _no_real_fire(monkeypatch)
    monkeypatch.setattr(condition, "fetch", lambda ep, **k: {"last": 4850})
    daemon.poll_once(_state())
    assert any("observed" in line for line in store.log_path("wch_1").read_text().splitlines())
