"""Durable store: stdlib SQLite (WAL) for operational state, files for content.

The on-disk root (default ``~/.claude/picket``, override with ``PICKET_HOME``)::

    $PICKET_HOME/
      picket.db            SQLite (WAL): watches, fires ledger, commands, meta
      runbooks/<id>/       human-placed runbook files + runbook.toml
      probes/<id>/         human-placed probe script + probe.toml
      logs/<id>.log        rotating poll/debug log (daemon-owned)
      results/<fire>.json  durable structured result artifact, one per fire

SQLite holds the mutable operational state that actually needs transactions,
worker leases, acknowledged control commands, indexed audit queries and schema
migrations — with no server to stand up (it is a single file). Runbooks, probes,
logs and results stay ordinary inspectable files. Every id used as a path
component is validated (:func:`safe_id`) so it can never escape the root.
"""

from __future__ import annotations

import json
import os
import re
import sqlite3
import uuid
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from picket.models import InvalidSpec, WatchState

SUBDIRS = ("runbooks", "probes", "logs", "results")
SCHEMA_VERSION = 1
_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")

_SCHEMA = """
CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT);

CREATE TABLE IF NOT EXISTS watches (
  watch_id   TEXT PRIMARY KEY,
  data       TEXT NOT NULL,          -- full WatchState as JSON
  status     TEXT NOT NULL,          -- denormalized for filtering
  desired    TEXT NOT NULL,          -- desired_status, for the reconciler
  updated_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS ix_watches_status ON watches(status);

CREATE TABLE IF NOT EXISTS fires (
  fire_id         TEXT PRIMARY KEY,
  watch_id        TEXT NOT NULL,
  idem_key        TEXT,             -- dedupe token (watch+episode+fired_at)
  status          TEXT NOT NULL,    -- pending|running|<terminal outcome>
  runbook_id      TEXT,
  value           TEXT,             -- JSON trigger value
  payload         TEXT,             -- JSON trigger payload (for inspect/replay)
  started_at      TEXT,
  ended_at        TEXT,
  exit_code       INTEGER,
  error           TEXT,
  handler_pid     INTEGER,
  duration_ms     INTEGER,
  transcript_tail TEXT,
  result_path     TEXT,
  worker_pid      INTEGER,
  lease_expires_at TEXT,            -- set while running; NULL otherwise
  delivery_status TEXT,             -- NULL|delivered|failed|skipped
  delivered_at    TEXT,
  created_at      TEXT NOT NULL
);
CREATE UNIQUE INDEX IF NOT EXISTS ix_fires_idem ON fires(idem_key) WHERE idem_key IS NOT NULL;
CREATE INDEX IF NOT EXISTS ix_fires_watch ON fires(watch_id, created_at);
CREATE INDEX IF NOT EXISTS ix_fires_status ON fires(status);

CREATE TABLE IF NOT EXISTS commands (
  id         INTEGER PRIMARY KEY AUTOINCREMENT,
  watch_id   TEXT NOT NULL,
  command    TEXT NOT NULL,         -- pause|resume|stop
  generation INTEGER NOT NULL,
  created_at TEXT NOT NULL,
  acked_at   TEXT
);
CREATE INDEX IF NOT EXISTS ix_commands_watch ON commands(watch_id, id);
"""

_FIRE_PUBLIC = (
    "fire_id", "watch_id", "runbook_id", "status", "started_at", "ended_at",
    "exit_code", "error", "handler_pid", "duration_ms", "transcript_tail",
    "result_path", "delivery_status", "delivered_at", "idem_key", "created_at",
)  # fmt: skip


def now_iso() -> str:
    """Current UTC time as an ISO-8601 string (used for all stored timestamps)."""
    return datetime.now(UTC).isoformat()


def _now() -> datetime:
    return datetime.now(UTC)


def picket_home() -> Path:
    """Resolve the root: $PICKET_HOME, else ~/.claude/picket. Does not create it."""
    env = os.environ.get("PICKET_HOME")
    return Path(env).expanduser() if env else Path.home() / ".claude" / "picket"


def db_path() -> Path:
    return picket_home() / "picket.db"


def safe_id(kind: str, value: str) -> str:
    """Reject an id that could escape PICKET_HOME as a path component."""
    if not value or not _ID_RE.match(value):
        raise InvalidSpec(
            f"invalid {kind} id {value!r}: use letters/digits/'.'/'_'/'-' (no traversal)"
        )
    return value


def ensure_root() -> Path:
    """Create the root (0700), its content subdirs, and the SQLite schema."""
    home = picket_home()
    home.mkdir(parents=True, exist_ok=True, mode=0o700)
    try:
        home.chmod(0o700)
    except OSError:
        pass
    for sub in SUBDIRS:
        (home / sub).mkdir(parents=True, exist_ok=True, mode=0o700)
    with _connect():  # triggers the lazy migration below
        pass
    return home


# --- content-file paths (validated ids) ------------------------------------


def runbook_dir(runbook_id: str) -> Path:
    return picket_home() / "runbooks" / safe_id("runbook", runbook_id)


def probe_dir(probe_id: str) -> Path:
    return picket_home() / "probes" / safe_id("probe", probe_id)


def log_path(watch_id: str) -> Path:
    return picket_home() / "logs" / f"{safe_id('watch', watch_id)}.log"


def result_path(fire_id: str) -> Path:
    return picket_home() / "results" / f"{safe_id('fire', fire_id)}.json"


def new_watch_id() -> str:
    return f"wch_{uuid.uuid4().hex[:12]}"


def new_fire_id() -> str:
    return f"fire_{uuid.uuid4().hex[:12]}"


# --- SQLite connection + migrations ----------------------------------------


_MIGRATED: set[str] = set()


@contextmanager
def _connect() -> Iterator[sqlite3.Connection]:
    """A short-lived autocommit connection (one per call → thread/process safe).

    The schema is migrated once per DB path per process, so any entry point that
    touches the store works without a separate init step.
    """
    path = db_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path, timeout=30.0, isolation_level=None)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA busy_timeout=30000")
    conn.execute("PRAGMA synchronous=NORMAL")
    key = str(path)
    if key not in _MIGRATED:
        _migrate(conn)
        _MIGRATED.add(key)
    try:
        yield conn
    finally:
        conn.close()


def _migrate(conn: sqlite3.Connection) -> None:
    conn.execute("PRAGMA journal_mode=WAL")
    conn.executescript(_SCHEMA)
    row = conn.execute("SELECT value FROM meta WHERE key='schema_version'").fetchone()
    version = int(row["value"]) if row else 0
    if version < SCHEMA_VERSION:  # future versions branch on `version` here
        conn.execute(
            "INSERT INTO meta(key,value) VALUES('schema_version',?) "
            "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
            (str(SCHEMA_VERSION),),
        )


# --- watches ---------------------------------------------------------------


def write_watch(state: WatchState) -> None:
    """Upsert the full watch state (single-row source of truth per watch)."""
    with _connect() as conn:
        conn.execute(
            "INSERT INTO watches(watch_id,data,status,desired,updated_at) VALUES(?,?,?,?,?) "
            "ON CONFLICT(watch_id) DO UPDATE SET data=excluded.data,status=excluded.status,"
            "desired=excluded.desired,updated_at=excluded.updated_at",
            (
                state.watch_id,
                state.model_dump_json(),
                state.status,
                state.desired_status,
                now_iso(),
            ),
        )


def read_watch(watch_id: str) -> WatchState | None:
    with _connect() as conn:
        row = conn.execute("SELECT data FROM watches WHERE watch_id=?", (watch_id,)).fetchone()
    return WatchState.model_validate_json(row["data"]) if row else None


def all_watch_ids() -> list[str]:
    with _connect() as conn:
        rows = conn.execute("SELECT watch_id FROM watches ORDER BY watch_id").fetchall()
    return [r["watch_id"] for r in rows]


def watch_ids_by_desired(desired: str) -> list[str]:
    with _connect() as conn:
        rows = conn.execute(
            "SELECT watch_id FROM watches WHERE desired=? ORDER BY watch_id", (desired,)
        ).fetchall()
    return [r["watch_id"] for r in rows]


# --- fires: the durable ledger ---------------------------------------------


def create_fire(
    fire_id: str,
    watch_id: str,
    status: str,
    *,
    runbook_id: str | None = None,
    idem_key: str | None = None,
    value: Any = None,
    payload: dict | None = None,
) -> bool:
    """Insert a fire row (durable trigger intent). False if idem_key already exists."""
    started = None if status == "pending" else now_iso()
    ended = now_iso() if status in ("skipped_overlap",) else None
    with _connect() as conn:
        try:
            conn.execute(
                "INSERT INTO fires(fire_id,watch_id,idem_key,status,runbook_id,value,payload,"
                "started_at,ended_at,created_at) VALUES(?,?,?,?,?,?,?,?,?,?)",
                (
                    fire_id,
                    watch_id,
                    idem_key,
                    status,
                    runbook_id,
                    json.dumps(value),
                    json.dumps(payload) if payload is not None else None,
                    started,
                    ended,
                    now_iso(),
                ),
            )
            return True
        except sqlite3.IntegrityError:
            return False


def claim_next_fire(watch_id: str, worker_pid: int, lease_seconds: float) -> dict | None:
    """Atomically lease the oldest pending fire for this watch (pending → running)."""
    lease = (_now() + timedelta(seconds=lease_seconds)).isoformat()
    with _connect() as conn:
        conn.execute("BEGIN IMMEDIATE")
        row = conn.execute(
            "SELECT * FROM fires WHERE watch_id=? AND status='pending' ORDER BY created_at LIMIT 1",
            (watch_id,),
        ).fetchone()
        if row is None:
            conn.execute("COMMIT")
            return None
        conn.execute(
            "UPDATE fires SET status='running', worker_pid=?, lease_expires_at=?, started_at=? "
            "WHERE fire_id=?",
            (worker_pid, lease, now_iso(), row["fire_id"]),
        )
        conn.execute("COMMIT")
    return dict(row)


def finish_fire(
    fire_id: str,
    status: str,
    *,
    exit_code: int | None = None,
    error: str | None = None,
    handler_pid: int | None = None,
    duration_ms: int | None = None,
    transcript_tail: str | None = None,
    result_path: str | None = None,
) -> None:
    with _connect() as conn:
        conn.execute(
            "UPDATE fires SET status=?, exit_code=?, error=?, handler_pid=?, duration_ms=?, "
            "transcript_tail=?, result_path=?, ended_at=?, lease_expires_at=NULL WHERE fire_id=?",
            (
                status,
                exit_code,
                error,
                handler_pid,
                duration_ms,
                transcript_tail,
                result_path,
                now_iso(),
                fire_id,
            ),
        )


def set_delivery(fire_id: str, delivery_status: str) -> None:
    with _connect() as conn:
        conn.execute(
            "UPDATE fires SET delivery_status=?, delivered_at=? WHERE fire_id=?",
            (delivery_status, now_iso(), fire_id),
        )


def set_running_pid(fire_id: str, pid: int) -> None:
    """Record the live handler pid so an immediate stop can cancel its process group."""
    with _connect() as conn:
        conn.execute("UPDATE fires SET handler_pid=? WHERE fire_id=?", (pid, fire_id))


def active_handler_pid(watch_id: str) -> int | None:
    """The handler pid of the currently-running fire, if any (for cancellation)."""
    with _connect() as conn:
        row = conn.execute(
            "SELECT handler_pid FROM fires WHERE watch_id=? AND status='running' "
            "AND handler_pid IS NOT NULL ORDER BY started_at DESC LIMIT 1",
            (watch_id,),
        ).fetchone()
    return row["handler_pid"] if row else None


def count_fires(watch_id: str) -> int:
    """Fires that count toward max_fires: everything decided except dropped overlaps."""
    with _connect() as conn:
        row = conn.execute(
            "SELECT COUNT(*) AS n FROM fires WHERE watch_id=? AND status!='skipped_overlap'",
            (watch_id,),
        ).fetchone()
    return row["n"]


def has_active_fire(watch_id: str) -> bool:
    """True if a fire is pending or running for this watch (overlap_policy=drop)."""
    with _connect() as conn:
        row = conn.execute(
            "SELECT 1 FROM fires WHERE watch_id=? AND status IN ('pending','running') LIMIT 1",
            (watch_id,),
        ).fetchone()
    return row is not None


def recover_abandoned(watch_id: str | None = None) -> int:
    """Fail fires whose worker lease expired (a crash). At-most-once: never re-run."""
    now = now_iso()
    sql = (
        "UPDATE fires SET status='failed', error='recovered: worker lease expired (crash)', "
        "ended_at=?, lease_expires_at=NULL WHERE status='running' AND lease_expires_at IS NOT NULL "
        "AND lease_expires_at < ?"
    )
    args: tuple = (now, now)
    if watch_id:
        sql += " AND watch_id=?"
        args += (watch_id,)
    with _connect() as conn:
        return conn.execute(sql, args).rowcount


def read_fire(fire_id: str) -> dict | None:
    with _connect() as conn:
        row = conn.execute("SELECT * FROM fires WHERE fire_id=?", (fire_id,)).fetchone()
    return _fire_dict(row) if row else None


def recent_fires(watch_id: str | None = None, limit: int = 20) -> list[dict]:
    where, args = ("WHERE watch_id=?", (watch_id, limit)) if watch_id else ("", (limit,))
    with _connect() as conn:
        rows = conn.execute(
            f"SELECT * FROM fires {where} ORDER BY created_at DESC, rowid DESC LIMIT ?", args
        ).fetchall()
    return [_fire_dict(r) for r in rows]


def most_recent_fire(watch_id: str) -> dict | None:
    fires = recent_fires(watch_id, limit=1)
    return fires[0] if fires else None


def prune_results(keep: int = 500) -> int:
    """Retention: drop terminal fire rows + result artifacts beyond the newest `keep`."""
    removed = 0
    with _connect() as conn:
        rows = conn.execute(
            "SELECT fire_id, result_path FROM fires WHERE status NOT IN ('pending','running') "
            "ORDER BY created_at DESC, rowid DESC LIMIT -1 OFFSET ?",
            (keep,),
        ).fetchall()
        for r in rows:
            if r["result_path"]:
                try:
                    Path(r["result_path"]).unlink(missing_ok=True)
                except OSError:
                    pass
            conn.execute("DELETE FROM fires WHERE fire_id=?", (r["fire_id"],))
            removed += 1
    return removed


def _fire_dict(row: sqlite3.Row) -> dict:
    d = {k: row[k] for k in _FIRE_PUBLIC}
    d["value"] = json.loads(row["value"]) if row["value"] is not None else None
    d["payload"] = json.loads(row["payload"]) if row["payload"] is not None else None
    return d


# --- commands: control channel with generations + acknowledgement ----------


def enqueue_command(watch_id: str, command: str) -> int:
    """Append a control command; returns its monotonically increasing generation."""
    with _connect() as conn:
        conn.execute("BEGIN IMMEDIATE")
        row = conn.execute(
            "SELECT COALESCE(MAX(generation),0) AS g FROM commands WHERE watch_id=?", (watch_id,)
        ).fetchone()
        gen = row["g"] + 1
        conn.execute(
            "INSERT INTO commands(watch_id,command,generation,created_at) VALUES(?,?,?,?)",
            (watch_id, command, gen, now_iso()),
        )
        conn.execute("COMMIT")
    return gen


def poll_command(watch_id: str) -> tuple[int, str] | None:
    """The newest unacked command for the daemon to apply (id, command)."""
    with _connect() as conn:
        row = conn.execute(
            "SELECT id, command FROM commands WHERE watch_id=? AND acked_at IS NULL "
            "ORDER BY id DESC LIMIT 1",
            (watch_id,),
        ).fetchone()
    return (row["id"], row["command"]) if row else None


def ack_command(watch_id: str, up_to_id: int) -> None:
    """Acknowledge every command up to and including ``up_to_id`` (newest wins)."""
    with _connect() as conn:
        conn.execute(
            "UPDATE commands SET acked_at=? WHERE watch_id=? AND id<=? AND acked_at IS NULL",
            (now_iso(), watch_id, up_to_id),
        )


def is_command_acked(watch_id: str, generation: int) -> bool:
    with _connect() as conn:
        row = conn.execute(
            "SELECT acked_at FROM commands WHERE watch_id=? AND generation=?",
            (watch_id, generation),
        ).fetchone()
    return bool(row and row["acked_at"])


# --- generic file helpers (results artifacts, params) ----------------------


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


def append_log(path: Path, line: str, max_bytes: int = 1_000_000) -> None:
    """Append a line to a size-capped log; roll over to <name>.1 when it exceeds the cap."""
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists() and path.stat().st_size > max_bytes:
        path.replace(path.with_name(path.name + ".1"))
    with path.open("a") as f:
        f.write(line.rstrip("\n") + "\n")
