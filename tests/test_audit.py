from picket.core.models import CadenceSpec, EndpointSpec, PredicateSpec, WatchState
from picket.persistence import audit, store


def _watch(watch_id):
    store.write_watch(
        WatchState(
            watch_id=watch_id,
            runbook_id="rb",
            endpoint=EndpointSpec(url="https://x"),
            predicate=PredicateSpec(path="$.last", op="lt", value=1),
            cadence=CadenceSpec(interval_seconds=1),
        )
    )


def test_get_fire_log_across_watchers_sorted_and_limited(home):
    store.create_fire("a1", "wch_a", "completed")
    store.create_fire("b1", "wch_b", "completed")
    store.create_fire("a2", "wch_a", "completed")  # newest

    fires = audit.get_fire_log(limit=2)["fires"]
    assert [f["fire_id"] for f in fires] == ["a2", "b1"]  # newest first, limited


def test_get_fire_log_single_watch(home):
    store.create_fire("a1", "wch_a", "completed")
    store.create_fire("b1", "wch_b", "completed")
    fires = audit.get_fire_log(watch_id="wch_a")["fires"]
    assert [f["fire_id"] for f in fires] == ["a1"]


def test_tail_watch_log(home):
    _watch("wch_a")
    for i in range(5):
        store.append_log(store.log_path("wch_a"), f"line {i}")
    res = audit.tail_watch_log("wch_a", lines=2)
    assert res["lines"] == ["line 3", "line 4"]


def test_tail_watch_log_not_found(home):
    assert audit.tail_watch_log("wch_nope")["error_code"] == "NOT_FOUND"
