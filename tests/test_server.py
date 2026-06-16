import asyncio

from fastmcp import Client

from picket.server import mcp


def _call(tool: str, **args):
    async def go():
        async with Client(mcp) as client:
            return await client.call_tool(tool, args)

    return asyncio.run(go())


def test_ping_returns_well_formed_dict():
    result = _call("ping")
    assert result.data == {"ok": True, "service": "picket", "version": "0.1.0"}
