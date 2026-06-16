"""Picket control plane — a FastMCP stdio server (§4A).

Fast request/response only: it arms/inspects/controls watchers but never polls
and never hosts the wait loop. Tools are thin adapters over the picket modules;
the logic they call is unit-tested directly.
"""

from __future__ import annotations

from fastmcp import FastMCP

from picket import __version__

mcp = FastMCP("picket")


@mcp.tool
def ping() -> dict:
    """Health check: confirm the Picket control plane is reachable."""
    return {"ok": True, "service": "picket", "version": __version__}


def main() -> None:
    """Console entry point — run the server over stdio."""
    mcp.run()


if __name__ == "__main__":
    main()
