"""Opt-in smoke test that fires a REAL `claude -p` handler (NEW-15).

This SPENDS a small amount of tokens (one trivial, tool-less, single-turn prompt),
so it has its own marker and is excluded from both the default run and `-m smoke`.
Run explicitly with:  uv run pytest -m claude_smoke
It skips cleanly when the claude CLI is not on PATH. Self-cleans via `smoke_home`.
"""

import shutil

import pytest

from picket.persistence import store
from picket.runtime import watches

pytestmark = [
    pytest.mark.claude_smoke,
    pytest.mark.skipif(shutil.which("claude") is None, reason="claude CLI not on PATH"),
]


def test_smoke_trigger_invokes_real_claude(smoke_home, server, prompt_runbook, poll_until):
    url, value = server
    prompt_runbook("Reply with exactly: PICKET-OK. Do not use any tools.")  # minimal, no tools

    res = watches.arm_watch(
        runbook_id="rb",
        endpoint={"url": url},
        predicate={"path": "$.last", "op": "lt", "value": 4800},
        cadence={"interval_seconds": 0.5},
        max_fires=1,
    )
    assert res["ok"]

    value["last"] = 4700  # trigger -> the daemon launches a real claude -p handler

    def _completed():
        return any(f["status"] == "completed" for f in store.recent_fires(res["watch_id"]))

    assert poll_until(_completed, timeout=120)
    fire = store.recent_fires(res["watch_id"])[0]
    assert fire["status"] == "completed"
    assert fire["handler_pid"]  # a real claude process actually ran
    assert fire["result_path"]  # the full output was persisted as a durable artifact
