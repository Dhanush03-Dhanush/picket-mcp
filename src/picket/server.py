"""Picket control plane — a FastMCP stdio server (§4A).

Fast request/response only: it arms/inspects/controls watchers but never polls
and never hosts the wait loop. Tools are thin adapters over the picket modules;
the logic they call is unit-tested directly.
"""

from __future__ import annotations

from fastmcp import FastMCP

from picket import __version__, condition
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


def main() -> None:
    """Console entry point — run the server over stdio."""
    mcp.run()


if __name__ == "__main__":
    main()
