import os

import psutil

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
    st.satisfied = True  # already satisfied last poll
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
