"""Picket control plane — a FastMCP stdio server (§4A).

Fast request/response only: it arms/inspects/controls watchers but never polls
and never hosts the wait loop. Tools are thin adapters over the picket modules;
the logic they call is unit-tested directly.
"""

from __future__ import annotations

from typing import Literal

from fastmcp import FastMCP

from picket import __version__, condition, runbooks
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


def main() -> None:
    """Console entry point — run the server over stdio."""
    mcp.run()


if __name__ == "__main__":
    main()
