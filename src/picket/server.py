"""Picket control plane — a FastMCP stdio server (§4A).

Fast request/response only: it arms/inspects/controls watchers but never polls
and never hosts the wait loop. Tools are thin adapters over the picket modules;
the logic they call is unit-tested directly.
"""

from __future__ import annotations

from typing import Literal

from fastmcp import FastMCP

from picket import __version__, audit, condition, probes, runbooks, watches
from picket.errors import ErrorCode, failure
from picket.models import EndpointSpec, InvalidSpec, PredicateSpec, parse

mcp = FastMCP("picket")


@mcp.tool
def ping() -> dict:
    """Health check: confirm the Picket control plane is reachable."""
    return {"ok": True, "service": "picket", "version": __version__}


@mcp.tool
def test_predicate(endpoint: dict, predicate: dict) -> dict:
    """Dry-run a spec: one fetch+extract+evaluate, no daemon and no state written.

    endpoint: {url, method?, headers?, body?, auth_ref?}
    predicate: {path, op (on_change|lt|gt|lte|gte|eq|ne|pct_change|crosses_above|
    crosses_below), value?, baseline_mode?}
    Returns response_excerpt, extracted_value, would_fire, extract_error.
    """
    try:
        ep = parse(EndpointSpec, endpoint)
        pr = parse(PredicateSpec, predicate)
    except InvalidSpec as err:
        return failure(ErrorCode.INVALID_SPEC, str(err))
    return condition.run_test_predicate(ep, pr)


@mcp.tool
def test_probe(probe_id: str, probe_params: dict | None = None) -> dict:
    """Dry-run a probe: one execution with probe_params, no daemon and no state written.

    Returns would_fire, value, payload, and error (a non-zero exit / timeout /
    unparseable stdout becomes a never-fire probe-error reported in `error`).
    """
    probe = probes.read_probe(probe_id)
    if probe is None:
        return failure(ErrorCode.PROBE_NOT_FOUND, f"probe {probe_id!r} is not registered")
    return probes.run_test_probe(probe, probe_params or {})


@mcp.tool
def register_runbook(
    runbook_id: str,
    runbook_type: Literal["prompt", "exec"],
    entry: str,
    description: str = "",
    allowed_tools: list[str] | None = None,
    version: int = 1,
) -> dict:
    """Register a runbook from files a human already placed under runbooks/<id>/.

    Never accepts a script body — only metadata + the entry path (validated to be
    inside the runbook dir). Computes content_hash and writes runbook.toml.
    """
    try:
        rb = runbooks.register_runbook(
            runbook_id,
            runbook_type=runbook_type,
            entry=entry,
            description=description,
            allowed_tools=allowed_tools,
            version=version,
        )
    except InvalidSpec as err:
        return failure(ErrorCode.INVALID_SPEC, str(err))
    return {"ok": True, **rb.model_dump()}


@mcp.tool
def list_runbooks() -> dict:
    """List registered runbooks (id, type, entry, declared_tools, content_hash, version)."""
    return {"ok": True, "runbooks": runbooks.list_runbooks()}


@mcp.tool
def install_default_runbooks() -> dict:
    """Install + register the shipped default macOS-notification runbook (picket-notify)."""
    rb = runbooks.install_default_notify_runbook()
    return {"ok": True, **rb.model_dump()}


@mcp.tool
def register_probe(
    probe_id: str,
    language: Literal["python", "sh"],
    entry: str,
    description: str = "",
    version: int = 1,
) -> dict:
    """Register a probe (condition script) from files a human placed under probes/<id>/.

    Never accepts a script body — only metadata + the entry path (validated to be
    inside the probe dir). Computes content_hash and writes probe.toml. The script
    prints {fire, value?, payload?} JSON on its last stdout line; a non-zero exit
    or unparseable output is a probe-error that never fires.
    """
    try:
        p = probes.register_probe(
            probe_id, language=language, entry=entry, description=description, version=version
        )
    except InvalidSpec as err:
        return failure(ErrorCode.INVALID_SPEC, str(err))
    return {"ok": True, **p.model_dump()}


@mcp.tool
def list_probes() -> dict:
    """List registered probes (id, language, entry, description, content_hash, version)."""
    return {"ok": True, "probes": probes.list_probes()}


@mcp.tool
def arm_watch(
    runbook_id: str,
    cadence: dict,
    endpoint: dict | None = None,
    predicate: dict | None = None,
    probe_id: str | None = None,
    probe_params: dict | None = None,
    label: str | None = None,
    max_fires: int | None = None,
    ttl_seconds: float | None = None,
    debounce_seconds: float = 0,
    cooldown_seconds: float = 0,
    max_retries: int = 0,
    drift_policy: str = "block",
    notify_runbook: str | None = None,
    skip_permissions: bool = False,
    confirm_skip: bool = False,
) -> dict:
    """Arm a watcher and spawn its detached daemon, then return immediately.

    Provide exactly one condition source: an endpoint+predicate (§8 spec dicts) OR
    a probe_id (a registered probe script, with optional probe_params). runbook_id
    must already be registered. skip_permissions=true requires confirm_skip=true
    (high-stakes bypass; see SECURITY in the README). Returns watch_id, status,
    pid, pgid, baseline, trial_value.
    """
    return watches.arm_watch(
        runbook_id=runbook_id,
        endpoint=endpoint,
        predicate=predicate,
        cadence=cadence,
        probe_id=probe_id,
        probe_params=probe_params,
        label=label,
        max_fires=max_fires,
        ttl_seconds=ttl_seconds,
        debounce_seconds=debounce_seconds,
        cooldown_seconds=cooldown_seconds,
        max_retries=max_retries,
        drift_policy=drift_policy,
        notify_runbook=notify_runbook,
        skip_permissions=skip_permissions,
        confirm_skip=confirm_skip,
    )


@mcp.tool
def list_watches(status_filter: str = "all") -> dict:
    """List watches (active|paused|stopped|errored|all) with per-row liveness."""
    return watches.list_watches(status_filter)


@mcp.tool
def get_watch(watch_id: str, log_lines: int = 20) -> dict:
    """Inspect one watch: full state, liveness, most recent fire, last poll-log lines."""
    return watches.get_watch(watch_id, log_lines)


@mcp.tool
def stop_watch(watch_id: str, mode: str = "graceful") -> dict:
    """Stop a watch (graceful via control file, or immediate SIGTERM). Idempotent."""
    return watches.stop_watch(watch_id, mode)


@mcp.tool
def stop_all_watches(
    confirm: bool = False, status_filter: str = "active", mode: str = "graceful"
) -> dict:
    """Bulk-stop watches. Requires confirm=true (else PERMISSION_REQUIRED)."""
    return watches.stop_all_watches(confirm, status_filter, mode)


@mcp.tool
def pause_watch(watch_id: str) -> dict:
    """Pause polling (daemon stays alive; baseline and history preserved)."""
    return watches.pause_watch(watch_id)


@mcp.tool
def resume_watch(watch_id: str) -> dict:
    """Resume polling on a paused watch without recomputing the baseline."""
    return watches.resume_watch(watch_id)


@mcp.tool
def get_fire_log(watch_id: str | None = None, limit: int = 20) -> dict:
    """Recent fire records (across all watches if watch_id is omitted)."""
    return audit.get_fire_log(watch_id, limit)


@mcp.tool
def tail_watch_log(watch_id: str, lines: int = 50) -> dict:
    """Recent poll/debug log lines for one watch — 'is it even observing?'."""
    return audit.tail_watch_log(watch_id, lines)


def main() -> None:
    """Console entry point — run the server over stdio."""
    mcp.run()


if __name__ == "__main__":
    main()
