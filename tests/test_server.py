import asyncio
import os

import psutil
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


def test_v0_walking_skeleton_end_to_end(home, monkeypatch):
    """register -> arm -> list -> get -> stop, end to end across the MCP tools."""
    from picket import condition, daemon, store

    d = home / "runbooks" / "notify"
    d.mkdir(parents=True)
    (d / "run.sh").write_text("#!/bin/sh\nexit 0\n")

    monkeypatch.setattr(condition, "fetch", lambda ep, **k: {"last": 4850})

    def fake_spawn(watch_id):
        st = store.read_watch(watch_id)
        st.pid = os.getpid()
        st.pgid = os.getpgid(os.getpid())
        st.proc_create_time = psutil.Process().create_time()
        store.write_watch(st)

    monkeypatch.setattr(daemon, "spawn", fake_spawn)

    assert _call(
        "register_runbook", runbook_id="notify", runbook_type="exec", entry="run.sh"
    ).data["ok"]

    armed = _call(
        "arm_watch",
        runbook_id="notify",
        endpoint={"url": "https://x/spx"},
        predicate={"path": "$.last", "op": "lt", "value": 4800},
        cadence={"interval_seconds": 30},
    ).data
    assert armed["ok"]
    watch_id = armed["watch_id"]

    listed = _call("list_watches").data["watches"]
    assert len(listed) == 1 and listed[0]["alive"]

    assert _call("get_watch", watch_id=watch_id).data["watch"]["watch_id"] == watch_id
    assert _call("stop_watch", watch_id=watch_id).data["final_status"] == "stopped"
