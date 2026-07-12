from __future__ import annotations

from picket.core.errors import ErrorCode, failure
from picket.persistence import store


def get_fire_log(watch_id: str | None = None, limit: int = 20) -> dict:
    """Most recent fire records (indexed query), across all watchers if omitted."""
    return {"ok": True, "fires": store.recent_fires(watch_id, limit)}


def tail_watch_log(watch_id: str, lines: int = 50) -> dict:
    """Recent poll/debug lines for one watch (why it has / hasn't fired)."""
    if store.read_watch(watch_id) is None:
        return failure(ErrorCode.NOT_FOUND, f"no watch {watch_id!r}")
    log = store.log_path(watch_id)
    tail = log.read_text().splitlines()[-lines:] if log.exists() else []
    return {"ok": True, "watch_id": watch_id, "lines": tail}
