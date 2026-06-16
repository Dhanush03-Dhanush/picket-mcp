from picket import audit, store
from picket.models import CadenceSpec, EndpointSpec, PredicateSpec, WatchState


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
    store.append_jsonl(
        store.fires_path("wch_a"), {"fire_id": "a1", "started_at": "2026-01-01T00:00:00"}
    )
    store.append_jsonl(
        store.fires_path("wch_b"), {"fire_id": "b1", "started_at": "2026-03-01T00:00:00"}
    )
    store.append_jsonl(
        store.fires_path("wch_a"), {"fire_id": "a2", "started_at": "2026-02-01T00:00:00"}
    )

    fires = audit.get_fire_log(limit=2)["fires"]
    assert [f["fire_id"] for f in fires] == ["b1", "a2"]  # newest first, limited


def test_get_fire_log_single_watch(home):
    store.append_jsonl(store.fires_path("wch_a"), {"fire_id": "a1", "started_at": "2026-01-01"})
    store.append_jsonl(store.fires_path("wch_b"), {"fire_id": "b1", "started_at": "2026-01-01"})
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
