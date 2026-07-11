"""Watch lifecycle: arm, list, inspect, control, stop.

arm_watch validates, does one trial fetch/probe (capturing the baseline), pins
the runbook/probe **content revision** so a later re-registration can't silently
retarget the watch, persists the durable state, spawns the detached daemon, and
reads back its identity. One-shot is the default; recurrence is an explicit
``recurring=true`` opt-in. Liveness and stop use verify-before-kill (pid present
AND psutil create_time matches) to guard against PID reuse; an immediate stop
also cancels the in-flight handler so it can't outlive the reported terminal.
"""

from __future__ import annotations

import os
import signal
from datetime import UTC, datetime

import psutil
from pydantic import ValidationError

from picket.conditions import condition, probes
from picket.conditions.condition import ObserveError
from picket.core.errors import ErrorCode, failure
from picket.core.models import (
    DELIVERY_EVENTS,
    CadenceSpec,
    EndpointSpec,
    InvalidSpec,
    PredicateSpec,
    WatchState,
    parse,
)
from picket.execution import runbooks
from picket.persistence import store
from picket.persistence.store import now_iso
from picket.runtime import daemon


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


def _heartbeat_stale(state: WatchState) -> bool:
    if state.heartbeat_at is None:
        return False  # just armed; the daemon hasn't ticked yet
    age = (datetime.now(UTC) - datetime.fromisoformat(state.heartbeat_at)).total_seconds()
    return age > max(60, 3 * state.cadence.interval_seconds)


def effective_status(state: WatchState) -> str:
    """Report 'errored' for an active watch whose daemon died or went stale."""
    if state.status == "active" and (not is_alive(state) or _heartbeat_stale(state)):
        return "errored"
    return state.status


def _await_identity(
    watch_id: str, timeout: float = 5.0, interval: float = 0.05
) -> WatchState | None:
    """Wait for the daemon to record its pid in the state row (the spawn handshake)."""
    import time

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
    endpoint: dict | None = None,
    predicate: dict | None = None,
    cadence: dict,
    probe_id: str | None = None,
    probe_params: dict | None = None,
    label: str | None = None,
    max_fires: int | None = None,
    recurring: bool = False,
    ttl_seconds: float | None = None,
    debounce_seconds: float = 0,
    cooldown_seconds: float = 0,
    max_retries: int = 0,
    drift_policy: str = "block",
    notify_runbook: str | None = None,
    delivery_events: list[str] | None = None,
    skip_permissions: bool = False,
    confirm_skip: bool = False,
) -> dict:
    """Validate, trial-observe, pin the revision, persist, and spawn a daemon."""
    store.ensure_root()
    try:
        cad = parse(CadenceSpec, cadence)
        ep = parse(EndpointSpec, endpoint) if endpoint else None
        pr = parse(PredicateSpec, predicate) if predicate else None
    except InvalidSpec as err:
        return failure(ErrorCode.INVALID_SPEC, str(err))

    use_probe = bool(probe_id)
    if use_probe == bool(ep and pr):
        return failure(
            ErrorCode.INVALID_SPEC, "provide exactly one of (endpoint+predicate) or probe_id"
        )

    if skip_permissions and not confirm_skip:
        return failure(
            ErrorCode.PERMISSION_REQUIRED, "skip_permissions=true requires confirm_skip=true"
        )

    rb = runbooks.read_runbook(runbook_id)
    if rb is None:
        return failure(ErrorCode.RUNBOOK_NOT_FOUND, f"runbook {runbook_id!r} is not registered")

    probe_rev = None
    if use_probe:
        probe = probes.read_probe(probe_id)
        if probe is None:
            return failure(ErrorCode.PROBE_NOT_FOUND, f"probe {probe_id!r} is not registered")
        probe_rev = probe.content_hash
        try:
            trial_value = probes.run_probe(probe, probe_params or {}).value
        except probes.ProbeError as err:
            return failure(ErrorCode.PROBE_FAILED, str(err))
        baseline = None
    else:
        try:
            trial_data = condition.fetch(ep)
            trial_value = condition.extract(trial_data, pr.path)
            baseline = condition.initial_baseline(pr, trial_value, trial_data)
        except ObserveError as err:
            return failure(ErrorCode.ENDPOINT_UNREACHABLE, str(err))

    if max_fires is None:  # safe default: one-shot; recurrence is an explicit opt-in
        max_fires = None if recurring else 1

    watch_id = store.new_watch_id()
    try:
        state = WatchState(
            watch_id=watch_id,
            runbook_id=runbook_id,
            runbook_rev=rb.content_hash,
            endpoint=ep,
            predicate=pr,
            probe_id=probe_id,
            probe_rev=probe_rev,
            probe_params=probe_params or {},
            cadence=cad,
            label=label,
            status="active",
            desired_status="active",
            baseline=baseline,
            created_at=now_iso(),
            max_fires=max_fires,
            ttl_seconds=ttl_seconds,
            debounce_seconds=debounce_seconds,
            cooldown_seconds=cooldown_seconds,
            max_retries=max_retries,
            drift_policy=drift_policy,
            notify_runbook=notify_runbook,
            delivery_events=delivery_events or list(DELIVERY_EVENTS),
            skip_permissions=skip_permissions,
        )
    except ValidationError as err:
        return failure(ErrorCode.INVALID_SPEC, str(err))
    store.write_watch(state)

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
        "max_fires": max_fires,
    }


def list_watches(status_filter: str = "all") -> dict:
    """List watches with per-row liveness verification."""
    rows = []
    for watch_id in store.all_watch_ids():
        state = store.read_watch(watch_id)
        if state is None:
            continue
        status = effective_status(state)
        if status_filter != "all" and status != status_filter:
            continue
        rows.append(
            {
                "watch_id": state.watch_id,
                "label": state.label,
                "status": status,
                "runbook_id": state.runbook_id,
                "source": f"probe:{state.probe_id}" if state.probe_id else "endpoint",
                "cadence_summary": f"every {state.cadence.interval_seconds:g}s",
                "mode": "recurring" if state.max_fires is None else f"max_fires={state.max_fires}",
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
    log = store.log_path(watch_id)
    tail = log.read_text().splitlines()[-log_lines:] if log.exists() else []
    return {
        "ok": True,
        "watch": state.model_dump(),
        "alive": is_alive(state),
        "effective_status": effective_status(state),
        "most_recent_fire": store.most_recent_fire(watch_id),
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
    store.enqueue_command(watch_id, command)
    return {"ok": True, "watch_id": watch_id, "requested": command}


def _killpg(pgid: int, sig: int) -> None:
    try:
        os.killpg(pgid, sig)
    except (ProcessLookupError, OSError):
        pass


def stop_watch(watch_id: str, mode: str = "graceful") -> dict:
    """Verify-before-kill stop. Idempotent: a second call returns ALREADY_STOPPED.

    ``graceful`` asks the daemon to drain the in-flight handler, then exit.
    ``immediate`` cancels the in-flight handler's process group and the daemon's,
    so no side effect outlives the reported terminal status. The server records
    the terminal status (the daemon may be killed before it can write its own).
    """
    state = store.read_watch(watch_id)
    if state is None:
        return failure(ErrorCode.NOT_FOUND, f"no watch {watch_id!r}")
    if state.status == "stopped":
        return failure(ErrorCode.ALREADY_STOPPED, f"{watch_id!r} already stopped")

    in_flight = store.has_active_fire(watch_id)
    if is_alive(state):
        if mode == "immediate":
            hpid = store.active_handler_pid(watch_id)
            if hpid:
                _killpg(hpid, signal.SIGKILL)  # cancel the running handler first
            _killpg(state.pgid, signal.SIGTERM)
        else:  # graceful: the daemon drains the current handler, then exits
            store.enqueue_command(watch_id, "stop")

    state.status = "stopped"
    state.desired_status = "stopped"
    store.write_watch(state)
    return {"ok": True, "final_status": "stopped", "handler_was_in_flight": in_flight}


def stop_all_watches(
    confirm: bool = False, status_filter: str = "active", mode: str = "graceful"
) -> dict:
    """Bulk stop. Requires confirm=true (PERMISSION_REQUIRED otherwise)."""
    if not confirm:
        return failure(ErrorCode.PERMISSION_REQUIRED, "stop_all_watches requires confirm=true")
    stopped, failures = [], []
    for watch_id in store.all_watch_ids():
        state = store.read_watch(watch_id)
        if state is None or (status_filter != "all" and state.status != status_filter):
            continue
        result = stop_watch(state.watch_id, mode)
        (stopped if result.get("ok") else failures).append(state.watch_id)
    return {"ok": True, "stopped_count": len(stopped), "watch_ids": stopped, "failures": failures}
