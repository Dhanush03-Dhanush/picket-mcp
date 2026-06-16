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
import subprocess
import tempfile
import uuid
from collections.abc import Callable
from pathlib import Path
from typing import Any

from picket import runbooks, store
from picket.models import WatchState
from picket.store import now_iso

Runner = Callable[..., subprocess.CompletedProcess]


def _claude_bin() -> str:
    return shutil.which("claude") or "claude"


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
    state: WatchState, value: Any, *, runner: Runner | None = None, max_turns: int = 30
) -> dict:
    """Launch the watch's runbook for one trigger and append the fire record."""
    runner = runner or subprocess.run
    started = now_iso()
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
            proc = runner(cmd, capture_output=True, text=True, env={**os.environ, **inv.env})
        except OSError as err:
            return _append_fire(state, fire_id, "failed", started, error=str(err))

    ok = proc.returncode == 0 and not (rb.type == "prompt" and _result_is_error(proc.stdout))
    status = "completed" if ok else "failed"
    error = None if ok else (proc.stderr or proc.stdout or "").strip()[:500]
    return _append_fire(state, fire_id, status, started, exit_code=proc.returncode, error=error)


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
    }
    store.append_jsonl(store.fires_path(state.watch_id), record)
    return record
