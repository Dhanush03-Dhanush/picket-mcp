"""Watch lifecycle (§10/§11/§12.1-12.4): arm, list, inspect, stop.

The v0 control surface that ties the pieces together. arm_watch validates, does
one trial observation (reusing test_predicate), persists the baseline + state,
spawns the detached daemon, and reads back the identity the daemon records.
Liveness and stop use verify-before-kill (pid present AND psutil create_time
matches) to guard against PID reuse.
"""

from __future__ import annotations

import os
import signal
import time

import psutil

from picket import condition, daemon, runbooks, store
from picket.condition import ObserveError
from picket.errors import ErrorCode, failure
from picket.models import (
    CadenceSpec,
    EndpointSpec,
    InvalidSpec,
    PredicateSpec,
    WatchState,
    parse,
)
from picket.store import now_iso


def is_alive(state: WatchState) -> bool:
    """True only if the recorded pid is running AND its create_time still matches."""
    if not state.pid:
        return False
    try:
        proc = psutil.Process(state.pid)
        if not proc.is_running():
            return False
    except psutil.NoSuchProcess:
        return False
    if state.proc_create_time is not None:
        if abs(proc.create_time() - state.proc_create_time) > 1.0:
            return False  # pid was reused by a different process
    return True


def _await_identity(
    watch_id: str, timeout: float = 5.0, interval: float = 0.05
) -> WatchState | None:
    """Wait for the daemon to record its pid in the state file (the spawn handshake)."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        state = store.read_watch(watch_id)
        if state and state.pid is not None:
            return state
        time.sleep(interval)
    return store.read_watch(watch_id)


def arm_watch(
    *,
    runbook_id: str,
    endpoint: dict,
    predicate: dict,
    cadence: dict,
    label: str | None = None,
    max_fires: int | None = None,
    ttl_seconds: float | None = None,
    debounce_seconds: float = 0,
    cooldown_seconds: float = 0,
) -> dict:
    """Validate, trial-observe, persist, and spawn a detached daemon for one watch."""
    try:
        ep = parse(EndpointSpec, endpoint)
        pr = parse(PredicateSpec, predicate)
        cad = parse(CadenceSpec, cadence)
    except InvalidSpec as err:
        return failure(ErrorCode.INVALID_SPEC, str(err))

    if runbooks.read_runbook(runbook_id) is None:
        return failure(ErrorCode.RUNBOOK_NOT_FOUND, f"runbook {runbook_id!r} is not registered")

    try:
        trial_data = condition.fetch(ep)
        trial_value = condition.extract(trial_data, pr.path)
        baseline = condition.initial_baseline(pr, trial_value, trial_data)
    except ObserveError as err:
        return failure(ErrorCode.ENDPOINT_UNREACHABLE, str(err))

    watch_id = store.new_watch_id()
    store.ensure_root()
    store.write_watch(
        WatchState(
            watch_id=watch_id,
            runbook_id=runbook_id,
            endpoint=ep,
            predicate=pr,
            cadence=cad,
            label=label,
            status="active",
            baseline=baseline,
            created_at=now_iso(),
            max_fires=max_fires,
            ttl_seconds=ttl_seconds,
            debounce_seconds=debounce_seconds,
            cooldown_seconds=cooldown_seconds,
        )
    )

    try:
        daemon.spawn(watch_id)
    except OSError as err:
        return failure(ErrorCode.DAEMON_SPAWN_FAILED, str(err))

    live = _await_identity(watch_id)
    if live is None or live.pid is None:
        return failure(ErrorCode.DAEMON_SPAWN_FAILED, "daemon did not report its identity")
    return {
        "ok": True,
        "watch_id": watch_id,
        "status": live.status,
        "pid": live.pid,
        "pgid": live.pgid,
        "baseline": baseline,
        "trial_value": trial_value,
    }


def list_watches(status_filter: str = "all") -> dict:
    """List watches with per-row liveness verification."""
    rows = []
    for path in sorted((store.picket_home() / "watches").glob("*.json")):
        state = store.read_watch(path.stem)
        if state is None or (status_filter != "all" and state.status != status_filter):
            continue
        rows.append(
            {
                "watch_id": state.watch_id,
                "label": state.label,
                "status": state.status,
                "runbook_id": state.runbook_id,
                "cadence_summary": f"every {state.cadence.interval_seconds:g}s",
                "fire_count": state.fire_count,
                "last_observed_at": state.last_observed_at,
                "last_error": state.last_error,
                "alive": is_alive(state),
            }
        )
    return {"ok": True, "watches": rows}


def get_watch(watch_id: str, log_lines: int = 20) -> dict:
    """Full state + most recent fire + last K poll-log lines."""
    state = store.read_watch(watch_id)
    if state is None:
        return failure(ErrorCode.NOT_FOUND, f"no watch {watch_id!r}")
    fires = store.read_jsonl(store.fires_path(watch_id))
    log = store.log_path(watch_id)
    tail = log.read_text().splitlines()[-log_lines:] if log.exists() else []
    return {
        "ok": True,
        "watch": state.model_dump(),
        "alive": is_alive(state),
        "most_recent_fire": fires[-1] if fires else None,
        "log_tail": tail,
    }


def pause_watch(watch_id: str) -> dict:
    """Halt polling without killing the daemon (baseline + history preserved)."""
    return _send_control(watch_id, "pause")


def resume_watch(watch_id: str) -> dict:
    """Resume polling; the baseline is restored, never recomputed."""
    return _send_control(watch_id, "resume")


def _send_control(watch_id: str, command: str) -> dict:
    state = store.read_watch(watch_id)
    if state is None:
        return failure(ErrorCode.NOT_FOUND, f"no watch {watch_id!r}")
    if state.status == "stopped":
        return failure(ErrorCode.ALREADY_STOPPED, f"{watch_id!r} is stopped")
    store.write_control(watch_id, command)
    return {"ok": True, "watch_id": watch_id, "requested": command}


def stop_watch(watch_id: str, mode: str = "graceful") -> dict:
    """Verify-before-kill stop. Idempotent: a second call returns ALREADY_STOPPED.

    The server records the terminal status (a documented exception to daemon
    ownership — a stopped/killed daemon cannot write its own final status).
    """
    state = store.read_watch(watch_id)
    if state is None:
        return failure(ErrorCode.NOT_FOUND, f"no watch {watch_id!r}")
    if state.status == "stopped":
        return failure(ErrorCode.ALREADY_STOPPED, f"{watch_id!r} already stopped")

    in_flight = store.lock_path(watch_id).exists()
    if is_alive(state):
        if mode == "immediate":
            try:
                os.killpg(state.pgid, signal.SIGTERM)
            except (ProcessLookupError, OSError):
                pass
        else:  # graceful: let the daemon stop after its current loop/handler
            store.write_control(watch_id, "stop")

    state.status = "stopped"
    store.write_watch(state)
    return {"ok": True, "final_status": "stopped", "handler_was_in_flight": in_flight}
