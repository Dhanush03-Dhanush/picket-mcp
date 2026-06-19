"""Flat-file store and global root layout (§5/§6/§15.1).

The on-disk root is the single source of truth — no DB. Layout::

    $PICKET_HOME/                 (default ~/.claude/picket)
      watches/<id>.json           daemon-owned state (server writes once at arm)
      watches/<id>.control        server-owned control channel (NEW-9)
      runbooks/<id>/              human-placed, registered by id (NEW-6)
      fires/<id>.jsonl            daemon-owned fire records (NEW-7)
      logs/<id>.log               daemon-owned poll/debug log (NEW-10)
      locks/<id>.lock             in-flight handler lock (NEW-9)

Single-writer ownership rule: after the daemon spawns it is the sole writer of
``watches/<id>.json``, ``fires/<id>.jsonl`` and ``logs/<id>.log``; the server
writes only ``watches/<id>.control`` (and the initial state file at arm time).
Atomic writes (temp + rename) mean a reader never sees a torn file regardless.
"""

from __future__ import annotations

import json
import os
import uuid
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from picket.models import WatchState

SUBDIRS = ("watches", "runbooks", "probes", "fires", "logs", "locks")


def now_iso() -> str:
    """Current UTC time as an ISO-8601 string (used for all stored timestamps)."""
    return datetime.now(UTC).isoformat()


def picket_home() -> Path:
    """Resolve the root: $PICKET_HOME, else ~/.claude/picket. Does not create it."""
    env = os.environ.get("PICKET_HOME")
    return Path(env).expanduser() if env else Path.home() / ".claude" / "picket"


def ensure_root() -> Path:
    """Create the root and all subdirs on first use; return the root path."""
    home = picket_home()
    for sub in SUBDIRS:
        (home / sub).mkdir(parents=True, exist_ok=True)
    return home


def watch_path(watch_id: str) -> Path:
    return picket_home() / "watches" / f"{watch_id}.json"


def control_path(watch_id: str) -> Path:
    return picket_home() / "watches" / f"{watch_id}.control"


def fires_path(watch_id: str) -> Path:
    return picket_home() / "fires" / f"{watch_id}.jsonl"


def log_path(watch_id: str) -> Path:
    return picket_home() / "logs" / f"{watch_id}.log"


def lock_path(watch_id: str) -> Path:
    return picket_home() / "locks" / f"{watch_id}.lock"


def runbook_dir(runbook_id: str) -> Path:
    return picket_home() / "runbooks" / runbook_id


def probe_dir(probe_id: str) -> Path:
    return picket_home() / "probes" / probe_id


def new_watch_id() -> str:
    """Generate a unique watch id with the ``wch_`` prefix."""
    return f"wch_{uuid.uuid4().hex[:12]}"


def write_json_atomic(path: Path, obj: Any) -> None:
    """Write JSON via temp file + os.replace so readers never see a partial file."""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + f".{os.getpid()}.tmp")
    with tmp.open("w") as f:
        json.dump(obj, f, indent=2)
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp, path)


def read_json(path: Path) -> Any:
    return json.loads(path.read_text())


def append_jsonl(path: Path, obj: Any) -> None:
    """Append one JSON record + newline (single-writer append-only log)."""
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a") as f:
        f.write(json.dumps(obj) + "\n")


def read_jsonl(path: Path) -> list[dict]:
    if not path.exists():
        return []
    return [json.loads(line) for line in path.read_text().splitlines() if line.strip()]


def append_log(path: Path, line: str, max_bytes: int = 1_000_000) -> None:
    """Append a line to a size-capped log; roll over to <name>.1 when it exceeds the cap."""
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists() and path.stat().st_size > max_bytes:
        path.replace(path.with_name(path.name + ".1"))
    with path.open("a") as f:
        f.write(line.rstrip("\n") + "\n")


def write_watch(state: WatchState) -> None:
    write_json_atomic(watch_path(state.watch_id), state.model_dump())


def read_watch(watch_id: str) -> WatchState | None:
    path = watch_path(watch_id)
    if not path.exists():
        return None
    return WatchState.model_validate(read_json(path))


def write_control(watch_id: str, command: str) -> None:
    """Server-owned channel: drop a one-word command for the daemon to read+clear."""
    path = control_path(watch_id)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(command)


def read_control(watch_id: str) -> str | None:
    """Daemon side: read and clear the control command (None if absent)."""
    path = control_path(watch_id)
    if not path.exists():
        return None
    command = path.read_text().strip()
    path.unlink()
    return command or None
