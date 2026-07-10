import os
from datetime import UTC, datetime, timedelta

import psutil

from picket import condition, daemon, store
from picket.models import ActiveWindow, CadenceSpec, EndpointSpec, PredicateSpec, WatchState


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


class _FakeWorker:
    """Stand in for the fire-executing worker so run() tests stay deterministic:
    poll_once still enqueues durable pending fires; nothing executes them."""

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


def _fired_values(watch_id="wch_1"):
    """Trigger values of the durable fires this watch enqueued (oldest first)."""
    return [f["value"] for f in reversed(store.recent_fires(watch_id))]


def _drain(watch_id="wch_1"):
    """Simulate the worker completing in-flight fires (so the next episode isn't an overlap)."""
    for f in store.recent_fires(watch_id):
        if f["status"] in ("pending", "running"):
            store.finish_fire(f["fire_id"], "completed")


def test_poll_once_enqueues_durable_fire_on_rising_edge(home, monkeypatch):
    monkeypatch.setattr(condition, "fetch", lambda ep, **k: {"last": 4700})
    st = daemon.poll_once(_state())
    fires = store.recent_fires("wch_1")
    assert [f["value"] for f in fires] == [4700]
    assert fires[0]["status"] == "pending"  # durable intent, recorded before any side effect
    assert st.fire_count == 1 and st.satisfied is True
    assert st.last_value == 4700 and st.last_error is None
    assert st.heartbeat_at and st.last_fire_at


def test_poll_once_no_fire_when_unsatisfied(home, monkeypatch):
    monkeypatch.setattr(condition, "fetch", lambda ep, **k: {"last": 4850})
    st = daemon.poll_once(_state())
    assert store.recent_fires("wch_1") == [] and st.fire_count == 0 and st.satisfied is False
    assert st.last_value == 4850


def test_poll_once_no_refire_while_satisfied(home, monkeypatch):
    monkeypatch.setattr(condition, "fetch", lambda ep, **k: {"last": 4700})
    st = _state()
    st.satisfied = True  # already fired earlier in this episode
    st.fired_this_episode = True
    daemon.poll_once(st)
    assert store.recent_fires("wch_1") == [] and st.fire_count == 0


def test_poll_once_observe_error_never_fires(home, monkeypatch):
    def boom(ep, **k):
        raise condition.ObserveError("down")

    monkeypatch.setattr(condition, "fetch", boom)
    st = daemon.poll_once(_state())
    assert store.recent_fires("wch_1") == [] and st.fire_count == 0
    assert st.last_error == "down"


def test_on_change_rearms_baseline_after_fire(home, monkeypatch):
    values = iter([{"last": 6}, {"last": 6}])
    monkeypatch.setattr(condition, "fetch", lambda ep, **k: next(values))
    st = _state(predicate=PredicateSpec(path="$.last", op="on_change"), baseline=5)

    daemon.poll_once(st)  # 6 != 5 -> fire, re-arm baseline to 6
    assert st.fire_count == 1 and st.baseline == 6 and st.satisfied is False
    daemon.poll_once(st)  # 6 == 6 -> no change
    assert st.fire_count == 1 and _fired_values() == [6]


def test_run_polls_persists_and_records_identity(home, monkeypatch):
    values = iter([{"last": 4850}, {"last": 4700}])
    monkeypatch.setattr(condition, "fetch", lambda ep, **k: next(values))
    store.write_watch(_state())

    daemon.run("wch_1", iterations=2, sleeper=lambda s: None, worker=_FakeWorker())

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


# --- concurrency, gating, limits -------------------------------------------


def _past(seconds):
    return (datetime.now(UTC) - timedelta(seconds=seconds)).isoformat()


def test_debounce_delays_fire_until_condition_held(home, monkeypatch):
    monkeypatch.setattr(condition, "fetch", lambda ep, **k: {"last": 4700})
    st = _state()
    st.debounce_seconds = 10

    daemon.poll_once(st)  # rising edge, but not held long enough
    assert st.fire_count == 0 and st.satisfied_since is not None

    st.satisfied_since = _past(11)  # condition has now held > debounce
    daemon.poll_once(st)
    assert st.fire_count == 1 and _fired_values() == [4700]


def test_cooldown_blocks_rapid_refire(home, monkeypatch):
    values = iter([{"last": 4700}, {"last": 4850}, {"last": 4700}])
    monkeypatch.setattr(condition, "fetch", lambda ep, **k: next(values))
    st = _state()
    st.cooldown_seconds = 60

    daemon.poll_once(st)  # fire 1
    _drain()  # the worker completes it before the next episode
    daemon.poll_once(st)  # unsatisfied -> episode ends
    daemon.poll_once(st)  # satisfied again, but within cooldown
    assert st.fire_count == 1

    st.last_fire_at = _past(61)  # cooldown elapsed
    monkeypatch.setattr(condition, "fetch", lambda ep, **k: {"last": 4700})
    daemon.poll_once(st)
    assert st.fire_count == 2


def test_overlap_records_skipped_overlap(home, monkeypatch):
    # a fire is already pending/running; a fresh crossing must be dropped, not queued
    monkeypatch.setattr(condition, "fetch", lambda ep, **k: {"last": 4700})
    store.create_fire("fire_inflight", "wch_1", "running", runbook_id="rb")

    st = daemon.poll_once(_state())

    fires = store.recent_fires("wch_1")
    assert any(f["status"] == "skipped_overlap" for f in fires)
    assert st.fire_count == 1  # only the in-flight fire counts; the crossing was dropped


def test_pause_then_resume_via_control(home, monkeypatch):
    monkeypatch.setattr(condition, "fetch", lambda ep, **k: {"last": 4850})
    store.write_watch(_state())

    store.enqueue_command("wch_1", "pause")
    daemon.run("wch_1", iterations=1, sleeper=lambda s: None, worker=_FakeWorker())
    paused = store.read_watch("wch_1")
    assert paused.status == "paused" and paused.last_value is None  # polling skipped

    store.enqueue_command("wch_1", "resume")
    daemon.run("wch_1", iterations=1, sleeper=lambda s: None, worker=_FakeWorker())
    resumed = store.read_watch("wch_1")
    assert resumed.status == "active" and resumed.baseline == 4900  # baseline preserved


def test_max_fires_self_stops(home, monkeypatch):
    monkeypatch.setattr(condition, "fetch", lambda ep, **k: {"last": 4700})
    st = _state()
    st.max_fires = 1
    store.write_watch(st)

    daemon.run("wch_1", iterations=5, sleeper=lambda s: None, worker=_FakeWorker())
    final = store.read_watch("wch_1")
    assert final.fire_count == 1 and final.status == "stopped"


def test_ttl_self_stops(home, monkeypatch):
    monkeypatch.setattr(condition, "fetch", lambda ep, **k: {"last": 4850})
    st = _state()
    st.ttl_seconds = 10
    st.created_at = _past(100)
    store.write_watch(st)

    daemon.run("wch_1", iterations=5, sleeper=lambda s: None, worker=_FakeWorker())
    assert store.read_watch("wch_1").status == "stopped"


def test_poll_once_writes_poll_log(home, monkeypatch):
    monkeypatch.setattr(condition, "fetch", lambda ep, **k: {"last": 4850})
    daemon.poll_once(_state())
    assert any("observed" in line for line in store.log_path("wch_1").read_text().splitlines())


# --- advanced predicates and cadence ---------------------------------------


def _utc(h):
    return datetime(2026, 6, 15, h, 0, tzinfo=UTC)


def test_in_active_window_time_days_and_wrap():
    every_day = list(range(7))
    hours = CadenceSpec(
        interval_seconds=30,
        active_window=ActiveWindow(start="09:00", end="17:00", days=every_day),
    )
    assert daemon.in_active_window(hours, _utc(10)) is True
    assert daemon.in_active_window(hours, _utc(20)) is False
    assert daemon.in_active_window(CadenceSpec(interval_seconds=30)) is True  # no window

    no_days = CadenceSpec(interval_seconds=30, active_window=ActiveWindow(days=[]))
    assert daemon.in_active_window(no_days, _utc(10)) is False

    wrap = CadenceSpec(
        interval_seconds=30,
        active_window=ActiveWindow(start="22:00", end="02:00", days=every_day),
    )
    assert daemon.in_active_window(wrap, _utc(23)) is True
    assert daemon.in_active_window(wrap, _utc(12)) is False


def test_active_window_suppresses_polling(home, monkeypatch):
    fetched = []
    monkeypatch.setattr(condition, "fetch", lambda ep, **k: fetched.append(1) or {"last": 4700})
    st = _state()
    st.cadence.active_window = ActiveWindow(days=[])  # never active
    store.write_watch(st)

    daemon.run("wch_1", iterations=2, sleeper=lambda s: None, worker=_FakeWorker())
    assert fetched == []  # polling suppressed outside the window
    assert store.read_watch("wch_1").heartbeat_at is not None  # but daemon stays alive


def test_pct_change_last_value_rearms_each_poll(home, monkeypatch):
    values = iter([{"last": 100}, {"last": 103}, {"last": 103}])
    monkeypatch.setattr(condition, "fetch", lambda ep, **k: next(values))
    st = _state(predicate=PredicateSpec(path="$.last", op="pct_change", value=2), baseline=100)

    daemon.poll_once(st)  # 0% vs 100 -> no fire; baseline tracks to 100
    assert st.fire_count == 0 and st.baseline == 100
    daemon.poll_once(st)  # +3% vs 100 -> fire; baseline tracks to 103
    assert st.fire_count == 1 and st.baseline == 103
    daemon.poll_once(st)  # 0% vs 103 -> no fire
    assert st.fire_count == 1 and _fired_values() == [103]


def test_crosses_above_fires_only_on_crossing(home, monkeypatch):
    values = iter([{"last": 9}, {"last": 11}, {"last": 12}])
    monkeypatch.setattr(condition, "fetch", lambda ep, **k: next(values))
    st = _state(predicate=PredicateSpec(path="$.last", op="crosses_above", value=10))

    daemon.poll_once(st)  # below
    daemon.poll_once(st)  # crosses above -> fire
    daemon.poll_once(st)  # already above -> no re-fire
    assert _fired_values() == [11] and st.fire_count == 1
