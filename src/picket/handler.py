"""The handler (§4C/§9): launch one runbook at fire time, then record the fire.

prompt runbooks run as a scoped, non-interactive ``claude -p`` (deny-by-default:
``--permission-mode dontAsk`` + an explicit ``--allowedTools`` allowlist, so a
tool that isn't listed is refused, never awaited). exec runbooks run their script
directly — no LLM, no tokens. Either way a record is appended to fires/<id>.jsonl.
Per §16.4 there is no --max-budget-usd; runaways are bounded by --max-turns.
"""

from __future__ import annotations

import json
import os
import shutil
import signal
import subprocess
import tempfile
import time
import uuid
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from picket import runbooks, store
from picket.models import WatchState
from picket.store import now_iso

_TRANSCRIPT_TAIL = 2000


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


def _default_runner(cmd: list[str], *, timeout: float, env: dict) -> HandlerResult:
    """Run the handler in its own session, capturing output and killing the group on timeout."""
    proc = subprocess.Popen(
        cmd,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        env=env,
        start_new_session=True,
    )
    try:
        out, err = proc.communicate(timeout=timeout)
        return HandlerResult(proc.returncode, out, err, proc.pid, False)
    except subprocess.TimeoutExpired:
        try:
            os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
        except (ProcessLookupError, OSError):
            proc.kill()
        out, err = proc.communicate()
        return HandlerResult(None, out or "", err or "", proc.pid, True)


def build_payload(state: WatchState, value: Any, fired_at: str) -> dict:
    """The trigger payload handed to the runbook (§7)."""
    return {
        "watch_id": state.watch_id,
        "label": state.label,
        "runbook_id": state.runbook_id,
        "fired_at": fired_at,
        "value": value,
        "baseline": state.baseline,
        "predicate": state.predicate.model_dump(),
        "endpoint_url": state.endpoint.url,
    }


def handler_command(rb: runbooks.Runbook, inv: runbooks.Invocation, max_turns: int) -> list[str]:
    """Build the launch command: direct script for exec, scoped claude -p for prompt."""
    if rb.type == "exec":
        return [str(inv.entry_path)]
    cmd = [
        _claude_bin(),
        "-p",
        inv.prompt_text or "",
        "--permission-mode",
        "dontAsk",
        "--max-turns",
        str(max_turns),
        "--output-format",
        "json",
        "--add-dir",
        str(store.runbook_dir(rb.id)),
    ]
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
) -> dict:
    """Launch the watch's runbook for one trigger, supervise it, append the fire record."""
    runner = runner or _default_runner
    started = now_iso()
    start_t = time.monotonic()
    fire_id = f"fire_{uuid.uuid4().hex[:12]}"

    rb = runbooks.read_runbook(state.runbook_id)
    if rb is None:
        return _append_fire(
            state, fire_id, "failed", started, error=f"runbook {state.runbook_id!r} not found"
        )

    payload = build_payload(state, value, started)
    with tempfile.TemporaryDirectory() as tmp:
        inv = runbooks.prepare_invocation(rb, payload, Path(tmp))
        cmd = handler_command(rb, inv, max_turns)
        try:
            res = runner(cmd, timeout=timeout, env={**os.environ, **inv.env})
        except OSError as err:
            return _append_fire(state, fire_id, "failed", started, error=str(err))

    duration_ms = int((time.monotonic() - start_t) * 1000)
    if res.timed_out:
        status = "timed_out"
    elif res.returncode == 0 and not (rb.type == "prompt" and _result_is_error(res.stdout)):
        status = "completed"
    else:
        status = "failed"
    error = None
    if status == "timed_out":
        error = f"handler exceeded {timeout}s timeout"
    elif status == "failed":
        error = (res.stderr or res.stdout or "").strip()[:500]
    return _append_fire(
        state,
        fire_id,
        status,
        started,
        exit_code=res.returncode,
        error=error,
        handler_pid=res.pid,
        duration_ms=duration_ms,
        transcript_tail=(res.stdout or res.stderr or "").strip()[-_TRANSCRIPT_TAIL:],
    )


def _result_is_error(stdout: str) -> bool:
    """A claude -p --output-format json result can report is_error even on exit 0."""
    try:
        return bool(json.loads(stdout).get("is_error"))
    except (json.JSONDecodeError, AttributeError):
        return False


def record_skipped_overlap(state: WatchState) -> dict:
    """A crossing arrived while a handler held the in-flight lock (overlap_policy=drop)."""
    return _append_fire(state, f"fire_{uuid.uuid4().hex[:12]}", "skipped_overlap", now_iso())


def _append_fire(
    state: WatchState,
    fire_id: str,
    status: str,
    started: str,
    *,
    exit_code: int | None = None,
    error: str | None = None,
    handler_pid: int | None = None,
    duration_ms: int | None = None,
    transcript_tail: str | None = None,
) -> dict:
    record = {
        "fire_id": fire_id,
        "watch_id": state.watch_id,
        "runbook_id": state.runbook_id,
        "status": status,
        "started_at": started,
        "ended_at": now_iso(),
        "exit_code": exit_code,
        "error": error,
        "handler_pid": handler_pid,
        "duration_ms": duration_ms,
        "transcript_tail": transcript_tail,
    }
    store.append_jsonl(store.fires_path(state.watch_id), record)
    return record
