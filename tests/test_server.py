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


def test_test_predicate_tool_runs(monkeypatch):
    from picket import condition

    monkeypatch.setattr(condition, "fetch", lambda ep, **k: {"last": 4700})
    result = _call(
        "test_predicate",
        endpoint={"url": "https://x"},
        predicate={"path": "$.last", "op": "lt", "value": 4800},
    )
    assert result.data["would_fire"] is True


def test_test_predicate_tool_rejects_bad_spec():
    result = _call(
        "test_predicate",
        endpoint={"url": "ftp://nope"},
        predicate={"path": "$.last", "op": "lt", "value": 1},
    )
    assert result.data == {
        "ok": False,
        "error_code": "INVALID_SPEC",
        "message": result.data["message"],
    }
