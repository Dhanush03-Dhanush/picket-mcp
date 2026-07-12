from __future__ import annotations

import json
import os
import shutil
import signal
import subprocess
import tempfile
import time
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from picket.core.models import WatchState
from picket.execution import runbooks
from picket.persistence import store
from picket.persistence.store import now_iso

_TRANSCRIPT_TAIL = 2000
_RESULT_MAX = 256_000


@dataclass
class HandlerResult:
    returncode: int | None
    stdout: str
    stderr: str
    pid: int | None
    timed_out: bool


Runner = Callable[..., HandlerResult]


def _claude_bin() -> str:
    return shutil.which("claude") or "claude"


def _kill_group(pid: int) -> None:
    try:
        os.killpg(os.getpgid(pid), signal.SIGKILL)
    except (ProcessLookupError, OSError):
        pass


def _default_runner(cmd: list[str], *, timeout: float, env: dict, on_start=None) -> HandlerResult:
    """Run the handler in its own session; report its pid so a stop can cancel the group."""
    proc = subprocess.Popen(
        cmd,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        env=env,
        start_new_session=True,
    )
    if on_start:
        on_start(proc.pid)
    try:
        out, err = proc.communicate(timeout=timeout)
        return HandlerResult(proc.returncode, out, err, proc.pid, False)
    except subprocess.TimeoutExpired:
        _kill_group(proc.pid)
        out, err = proc.communicate()
        return HandlerResult(None, out or "", err or "", proc.pid, True)


def build_payload(
    state: WatchState,
    value: Any,
    fired_at: str,
    *,
    fire_id: str | None = None,
    extra: dict | None = None,
) -> dict:
    """The trigger payload handed to the runbook, shaped by the condition source.

    Carries ``fire_id`` and a stable ``idempotency_key`` so a runbook can refuse
    to act twice on the same fire. Probe-supplied fields are untrusted: they may
    add keys but can never overwrite the core fields.
    """
    payload = {
        "watch_id": state.watch_id,
        "fire_id": fire_id,
        "idempotency_key": f"{state.watch_id}:{state.episode_seq}",
        "label": state.label,
        "runbook_id": state.runbook_id,
        "fired_at": fired_at,
        "value": value,
        "baseline": state.baseline,
    }
    if state.probe_id:
        payload["probe_id"] = state.probe_id
        for k, v in (extra or {}).items():
            payload.setdefault(k, v)
    else:
        payload["predicate"] = state.predicate.model_dump()
        payload["endpoint_url"] = state.endpoint.url
    return payload


DEFAULT_DISALLOWED = ["Bash(rm:*)", "Bash(curl:*)", "Bash(sudo:*)"]


def handler_command(
    rb: runbooks.Runbook,
    inv: runbooks.Invocation,
    max_turns: int,
    *,
    skip_permissions: bool = False,
) -> list[str]:
    """Build the launch command: direct script for exec, scoped claude -p for prompt."""
    if rb.type == "exec":
        return [str(inv.entry_path)]
    cmd = [
        _claude_bin(),
        "-p",
        inv.prompt_text or "",
        "--max-turns",
        str(max_turns),
        "--output-format",
        "json",
        "--add-dir",
        str(store.runbook_dir(rb.id)),
    ]
    if skip_permissions:
        return cmd + ["--dangerously-skip-permissions", "--disallowedTools", *DEFAULT_DISALLOWED]
    cmd += ["--permission-mode", "dontAsk"]
    if rb.allowed_tools:
        cmd += ["--allowedTools", *rb.allowed_tools]
    return cmd


def fire(
    state: WatchState,
    value: Any,
    *,
    runner: Runner | None = None,
    max_turns: int = 30,
    timeout: float = 600,
    sleeper: Callable[[float], None] = time.sleep,
    payload_extra: dict | None = None,
) -> dict:
    """Create a durable fire row, then execute it. Standalone convenience for callers
    that are not the daemon worker (tests, one-off runs)."""
    fire_id = store.new_fire_id()
    store.create_fire(fire_id, state.watch_id, "running", runbook_id=state.runbook_id, value=value)
    return run_fire(
        state,
        value,
        fire_id,
        runner=runner,
        max_turns=max_turns,
        timeout=timeout,
        sleeper=sleeper,
        payload_extra=payload_extra,
    )


def run_fire(
    state: WatchState,
    value: Any,
    fire_id: str,
    *,
    runner: Runner | None = None,
    max_turns: int = 30,
    timeout: float = 600,
    sleeper: Callable[[float], None] = time.sleep,
    payload_extra: dict | None = None,
    on_start: Callable[[int], None] | None = None,
) -> dict:
    """Execute an already-created fire with drift protection, retry, result + delivery."""
    runner = runner or _default_runner
    started = now_iso()
    start_t = time.monotonic()

    rb = runbooks.read_runbook(state.runbook_id)
    if rb is None:
        return _finalize(
            state, fire_id, "failed", start_t, error=f"runbook {state.runbook_id!r} not found"
        )

    if _has_drifted(rb, state.runbook_rev) and state.drift_policy == "block":
        return _finalize(
            state, fire_id, "failed", start_t, error="RUNBOOK_DRIFT: entry changed since arm"
        )

    payload = build_payload(state, value, started, fire_id=fire_id, extra=payload_extra)
    attempts = 1 + max(0, state.max_retries)
    status, res = "failed", None
    with tempfile.TemporaryDirectory() as tmp:
        inv = runbooks.prepare_invocation(rb, payload, Path(tmp))
        cmd = handler_command(rb, inv, max_turns, skip_permissions=state.skip_permissions)
        env = {**os.environ, **inv.env}
        for attempt in range(attempts):
            try:
                res = runner(cmd, timeout=timeout, env=env, on_start=on_start)
            except OSError as err:
                return _finalize(state, fire_id, "failed", start_t, error=str(err))
            status = _attempt_status(rb, res)  # safety-termination shows as nonzero -> failed
            if status == "completed":
                break
            if attempt < attempts - 1:
                sleeper(2.0**attempt)  # exponential backoff

    if status != "completed" and state.max_retries > 0:
        status = "dead_lettered"
    error = None if status == "completed" else _error_text(status, res, timeout)
    result_path = _write_result(fire_id, state, value, res, status, started)
    return _finalize(
        state,
        fire_id,
        status,
        start_t,
        exit_code=res.returncode if res else None,
        error=error,
        handler_pid=res.pid if res else None,
        transcript_tail=(res.stdout or res.stderr or "").strip()[-_TRANSCRIPT_TAIL:]
        if res
        else None,
        result_path=result_path,
    )


def _finalize(
    state: WatchState,
    fire_id: str,
    status: str,
    start_t: float,
    *,
    exit_code: int | None = None,
    error: str | None = None,
    handler_pid: int | None = None,
    transcript_tail: str | None = None,
    result_path: str | None = None,
) -> dict:
    store.finish_fire(
        fire_id,
        status,
        exit_code=exit_code,
        error=error,
        handler_pid=handler_pid,
        duration_ms=int((time.monotonic() - start_t) * 1000),
        transcript_tail=transcript_tail,
        result_path=result_path,
    )
    _deliver(state, fire_id, status, error or "ok")
    return store.read_fire(fire_id)


def _write_result(fire_id, state, value, res, status, started) -> str:
    """Persist the full runbook output as a durable, inspectable result artifact."""
    path = store.result_path(fire_id)
    store.write_json_atomic(
        path,
        {
            "fire_id": fire_id,
            "watch_id": state.watch_id,
            "runbook_id": state.runbook_id,
            "status": status,
            "value": value,
            "started_at": started,
            "ended_at": now_iso(),
            "returncode": res.returncode if res else None,
            "stdout": (res.stdout or "")[:_RESULT_MAX] if res else "",
            "stderr": (res.stderr or "")[:_RESULT_MAX] if res else "",
        },
    )
    return str(path)


def _attempt_status(rb: runbooks.Runbook, res: HandlerResult) -> str:
    if res.timed_out:
        return "timed_out"
    if res.returncode == 0 and not (rb.type == "prompt" and _result_is_error(res.stdout)):
        return "completed"
    return "failed"


def _error_text(status: str, res: HandlerResult | None, timeout: float) -> str:
    if status == "timed_out":
        return f"handler exceeded {timeout}s timeout"
    if res is None:
        return "handler error"
    return (res.stderr or res.stdout or "").strip()[:500]


def _has_drifted(rb: runbooks.Runbook, expected: str | None) -> bool:
    """Re-hash the entry (+ scripts/) and compare to the hash pinned at arm time."""
    expected = expected or rb.content_hash
    if not expected:
        return False
    current = runbooks.content_hash(store.runbook_dir(rb.id), rb.entry)
    return current != expected


def _deliver(state: WatchState, fire_id: str, status: str, summary: str) -> None:
    """Run the delivery sink for a subscribed outcome and record a delivery receipt."""
    if not state.notify_runbook or status not in state.delivery_events:
        return
    rb = runbooks.read_runbook(state.notify_runbook)
    if rb is None:
        store.set_delivery(fire_id, "failed")
        return
    payload = json.dumps(
        {
            "watch_id": state.watch_id,
            "fire_id": fire_id,
            "status": status,
            "label": state.label,
            "summary": summary,
            "result_path": str(store.result_path(fire_id)),
        }
    )
    try:
        subprocess.run(
            [str(store.runbook_dir(rb.id) / rb.entry)],
            env={**os.environ, "PICKET_PAYLOAD": payload},
            capture_output=True,
            timeout=30,
        )
        store.set_delivery(fire_id, "delivered")
    except (OSError, subprocess.SubprocessError):
        store.set_delivery(fire_id, "failed")


def _result_is_error(stdout: str) -> bool:
    """A claude -p --output-format json result can report is_error even on exit 0."""
    try:
        return bool(json.loads(stdout).get("is_error"))
    except (json.JSONDecodeError, AttributeError):
        return False


def record_skipped_overlap(state: WatchState) -> dict:
    """A crossing arrived while a fire was still pending/running (overlap_policy=drop)."""
    fire_id = store.new_fire_id()
    store.create_fire(fire_id, state.watch_id, "skipped_overlap", runbook_id=state.runbook_id)
    return store.read_fire(fire_id)
