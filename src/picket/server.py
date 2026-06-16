"""Picket control plane — a FastMCP stdio server (§4A).

Fast request/response only: it arms/inspects/controls watchers but never polls
and never hosts the wait loop. Tools are thin adapters over the picket modules;
the logic they call is unit-tested directly.
"""

from __future__ import annotations

from typing import Literal

from fastmcp import FastMCP

from picket import __version__, audit, condition, runbooks, watches
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
    predicate: {path, op (on_change|lt|gt|lte|gte|eq|ne), value?}
    Returns response_excerpt, extracted_value, would_fire, extract_error.
    """
    try:
        ep = parse(EndpointSpec, endpoint)
        pr = parse(PredicateSpec, predicate)
    except InvalidSpec as err:
        return failure(ErrorCode.INVALID_SPEC, str(err))
    return condition.run_test_predicate(ep, pr)


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
def arm_watch(
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
    """Arm a watcher and spawn its detached daemon, then return immediately.

    endpoint/predicate/cadence are the §8 spec dicts; runbook_id must already be
    registered. Returns watch_id, status, pid, pgid, baseline, trial_value.
    """
    return watches.arm_watch(
        runbook_id=runbook_id,
        endpoint=endpoint,
        predicate=predicate,
        cadence=cadence,
        label=label,
        max_fires=max_fires,
        ttl_seconds=ttl_seconds,
        debounce_seconds=debounce_seconds,
        cooldown_seconds=cooldown_seconds,
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
