from pathlib import Path

from picket import store
from picket.models import CadenceSpec, EndpointSpec, PredicateSpec, WatchState


def _sample_state() -> WatchState:
    return WatchState(
        watch_id="wch_test",
        runbook_id="notify",
        endpoint=EndpointSpec(url="https://example.com/spx", auth_ref="SPX_TOKEN"),
        predicate=PredicateSpec(path="$.last", op="lt", value=4800),
        cadence=CadenceSpec(interval_seconds=30),
        baseline=4900.0,
        last_value=4850.0,
        satisfied=True,
        fire_count=2,
        pid=1234,
        pgid=1234,
        proc_create_time=111.5,
    )


def test_default_home_when_unset(monkeypatch):
    monkeypatch.delenv("PICKET_HOME", raising=False)
    assert store.picket_home() == Path.home() / ".claude" / "picket"


def test_env_override(home):
    assert store.picket_home() == home


def test_ensure_root_scaffolds_subdirs(home):
    store.ensure_root()
    for sub in store.SUBDIRS:
        assert (home / sub).is_dir()


def test_new_watch_id_unique_and_prefixed():
    a, b = store.new_watch_id(), store.new_watch_id()
    assert a.startswith("wch_") and b.startswith("wch_")
    assert a != b


def test_watch_round_trips_every_field(home):
    state = _sample_state()
    store.write_watch(state)
    assert store.read_watch(state.watch_id) == state


def test_read_missing_watch_returns_none(home):
    assert store.read_watch("wch_nope") is None


def test_atomic_write_leaves_no_temp_and_overwrites(home):
    path = home / "watches" / "wch_test.json"
    store.write_watch(_sample_state())
    store.write_watch(_sample_state())  # overwrite
    assert store.read_json(path)["watch_id"] == "wch_test"
    assert list((home / "watches").glob("*.tmp")) == []
