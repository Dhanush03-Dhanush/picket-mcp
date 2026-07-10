"""Probe model, registration, and execution (NEW-16/NEW-17).

A *probe* is the generalized condition: a registered script the daemon runs on
the watch cadence to decide whether to fire. Like a runbook it lives under
``probes/<id>/`` and is referenced by id — its code is NEVER passed as a
parameter. The script prints one JSON object on its last stdout line::

    {"fire": true, "value": <any>, "payload": {...}}

Exit 0 means evaluated; a non-zero exit, a timeout, or unparseable stdout is a
:class:`ProbeError` — logged and never a fire (the ``ObserveError`` analog). The
script receives ``probe_params`` via ``PICKET_PARAMS`` (+ a ``params.json`` file),
the prior tick's value via ``PICKET_LAST_VALUE``, and ``PICKET_WATCH_ID``;
secrets come from inherited env vars (the same auth_ref model as endpoints).
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import tomllib
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Literal

import tomli_w
from pydantic import BaseModel

from picket import store
from picket.models import InvalidSpec
from picket.runbooks import content_hash

# Run by declared language rather than relying on a shebang + chmod (more robust,
# and gives Python probes the picket venv's deps such as httpx).
_INTERPRETER = {"python": [sys.executable], "sh": ["/bin/sh"]}
_PROBE_TIMEOUT = 30.0  # a probe runs every tick; it must be quick


class Probe(BaseModel):
    id: str
    language: Literal["python", "sh"]
    entry: str
    description: str = ""
    content_hash: str = ""
    version: int = 1


class ProbeError(Exception):
    """The probe could not be evaluated (bad exit/timeout/output). Never fires."""


@dataclass
class ProbeResult:
    fire: bool
    value: Any = None
    payload: dict = field(default_factory=dict)


def _toml_path(probe_id: str) -> Path:
    return store.probe_dir(probe_id) / "probe.toml"


def register_probe(
    probe_id: str,
    *,
    language: Literal["python", "sh"],
    entry: str,
    description: str = "",
    version: int = 1,
) -> Probe:
    """Register a script a human already placed under probes/<id>/ (never a body)."""
    pd = store.probe_dir(probe_id)
    resolved = (pd / entry).resolve()
    if not resolved.is_relative_to(pd.resolve()):
        raise InvalidSpec(f"entry {entry!r} resolves outside the probe directory")
    if not resolved.is_file():
        raise InvalidSpec(f"entry file not found: {entry!r}")

    probe = Probe(
        id=probe_id,
        language=language,
        entry=entry,
        description=description,
        version=version,
        content_hash=content_hash(pd, entry),
    )
    _toml_path(probe_id).write_bytes(tomli_w.dumps(probe.model_dump()).encode())
    return probe


def read_probe(probe_id: str) -> Probe | None:
    path = _toml_path(probe_id)
    if not path.is_file():
        return None
    return Probe(**tomllib.loads(path.read_text()))


def list_probes() -> list[dict]:
    root = store.picket_home() / "probes"
    out = []
    if not root.is_dir():
        return out
    for d in sorted(root.iterdir()):
        if (d / "probe.toml").is_file():
            p = read_probe(d.name)
            out.append(
                {
                    "probe_id": p.id,
                    "language": p.language,
                    "entry": p.entry,
                    "description": p.description,
                    "content_hash": p.content_hash,
                    "version": p.version,
                }
            )
    return out


def has_drifted(probe: Probe, expected_hash: str | None = None) -> bool:
    """Re-hash the entry (+ scripts/) and compare to the hash pinned at arm time.

    ``expected_hash`` is the watch's pinned ``probe_rev``; falling back to the
    registration hash keeps back-compat. Comparing against the *pinned* value is
    what stops a re-registered probe from silently retargeting existing watches.
    """
    expected = expected_hash or probe.content_hash
    if not expected:
        return False
    return content_hash(store.probe_dir(probe.id), probe.entry) != expected


def run_probe(
    probe: Probe,
    params: dict | None,
    *,
    last_value: Any = None,
    watch_id: str | None = None,
    timeout: float = _PROBE_TIMEOUT,
) -> ProbeResult:
    """Execute the probe and parse its {fire,value,payload} verdict from stdout."""
    entry_path = store.probe_dir(probe.id) / probe.entry
    params_json = json.dumps(params or {})
    with tempfile.TemporaryDirectory() as tmp:
        params_file = Path(tmp) / "params.json"
        params_file.write_text(params_json)
        env = {
            **os.environ,
            "PICKET_PARAMS": params_json,
            "PICKET_PARAMS_FILE": str(params_file),
            "PICKET_LAST_VALUE": json.dumps(last_value),
            "PICKET_WATCH_ID": watch_id or "",
        }
        try:
            proc = subprocess.run(
                [*_INTERPRETER[probe.language], str(entry_path)],
                capture_output=True,
                text=True,
                timeout=timeout,
                env=env,
            )
        except subprocess.TimeoutExpired as err:
            raise ProbeError(f"probe exceeded {timeout:g}s timeout") from err
        except OSError as err:
            raise ProbeError(f"probe failed to launch: {err}") from err

    if proc.returncode != 0:
        raise ProbeError(f"probe exited {proc.returncode}: {(proc.stderr or '').strip()[:300]}")
    return _parse_verdict(proc.stdout)


def _parse_verdict(stdout: str) -> ProbeResult:
    """The last non-empty stdout line must be a JSON object (debug lines above are ignored)."""
    lines = [ln for ln in stdout.splitlines() if ln.strip()]
    if not lines:
        raise ProbeError("probe produced no output")
    try:
        verdict = json.loads(lines[-1])
    except json.JSONDecodeError as err:
        raise ProbeError(f"probe stdout is not JSON: {err}") from err
    if not isinstance(verdict, dict):
        raise ProbeError("probe stdout must be a JSON object")
    fire = verdict.get("fire")
    if isinstance(fire, str):  # a JSON string like "false" is NOT truthy here
        fire = fire.strip().lower() not in ("", "false", "0", "no")
    else:
        fire = bool(fire)
    payload = verdict.get("payload") or {}
    if not isinstance(payload, dict):
        raise ProbeError("probe 'payload' must be a JSON object")
    return ProbeResult(fire=fire, value=verdict.get("value"), payload=payload)


def observe(state: Any) -> ProbeResult:
    """Daemon-side: load the probe, drift-guard, and run it for one watch's state."""
    probe = read_probe(state.probe_id)
    if probe is None:
        raise ProbeError(f"probe {state.probe_id!r} is not registered")
    if state.drift_policy == "block" and has_drifted(probe, state.probe_rev):
        raise ProbeError("PROBE_DRIFT: probe entry changed since arm")
    return run_probe(
        probe, state.probe_params, last_value=state.last_value, watch_id=state.watch_id
    )


def run_test_probe(probe: Probe, params: dict) -> dict:
    """One execution, no daemon and no state. Never raises (dry-run, mirrors test_predicate)."""
    try:
        result = run_probe(probe, params)
    except ProbeError as err:
        return {"ok": True, "would_fire": False, "value": None, "payload": None, "error": str(err)}
    return {
        "ok": True,
        "would_fire": result.fire,
        "value": result.value,
        "payload": result.payload,
        "error": None,
    }
