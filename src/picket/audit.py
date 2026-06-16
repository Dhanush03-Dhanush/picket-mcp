"""Audit queries (§12.10/§12.11/§14): "did it fire and what happened?" and
"is it even observing?".

get_fire_log answers the first from fires/<id>.jsonl; tail_watch_log answers the
second from the daemon's poll/debug log at logs/<id>.log.
"""

from __future__ import annotations

from picket import store
from picket.errors import ErrorCode, failure


def get_fire_log(watch_id: str | None = None, limit: int = 20) -> dict:
    """Most recent fire records, across all watchers if watch_id is omitted."""
    if watch_id:
        files = [store.fires_path(watch_id)]
    else:
        files = sorted((store.picket_home() / "fires").glob("*.jsonl"))
    records: list[dict] = []
    for path in files:
        records.extend(store.read_jsonl(path))
    records.sort(key=lambda r: r.get("started_at") or "", reverse=True)
    return {"ok": True, "fires": records[:limit]}


def tail_watch_log(watch_id: str, lines: int = 50) -> dict:
    """Recent poll/debug lines for one watch (why it has / hasn't fired)."""
    if store.read_watch(watch_id) is None:
        return failure(ErrorCode.NOT_FOUND, f"no watch {watch_id!r}")
    log = store.log_path(watch_id)
    tail = log.read_text().splitlines()[-lines:] if log.exists() else []
    return {"ok": True, "watch_id": watch_id, "lines": tail}
