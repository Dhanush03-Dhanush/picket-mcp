"""Opt-in real-process smoke tests (NEW-14).

These spawn REAL detached daemons that poll a REAL (local, stdlib) HTTP server and
fire REAL exec handlers — the seams the hermetic suite mocks. They stay cheap
(exec only: no claude, no tokens; sub-second intervals; self-limiting watchers)
and self-clean (temp PICKET_HOME + a teardown that stops and reaps every daemon).

Deselected from the default run; execute with:  uv run pytest -m smoke
"""

import json
import os
import signal
import threading
import time
from http.server import BaseHTTPRequestHandler, HTTPServer

import psutil
import pytest

from picket import runbooks, store, watches

pytestmark = pytest.mark.smoke


@pytest.fixture
def server():
    """A localhost JSON endpoint whose value the test can mutate to trigger a fire."""
    value = {"last": 5000}

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            body = json.dumps(value).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *args):
            pass  # keep test output clean

    httpd = HTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    try:
        yield f"http://127.0.0.1:{httpd.server_address[1]}/", value
    finally:
        httpd.shutdown()


@pytest.fixture
def smoke_home(home):
    """Temp PICKET_HOME (from `home`) plus a teardown that reaps every real daemon."""
    store.ensure_root()
    yield home
    watches.stop_all_watches(confirm=True, status_filter="all", mode="immediate")
    for path in (home / "watches").glob("*.json"):
        state = store.read_watch(path.stem)
        if state and state.pgid:
            try:
                os.killpg(state.pgid, signal.SIGKILL)
            except (ProcessLookupError, OSError):
                pass


def _exec_runbook(home, body, rb_id="rb"):
    d = home / "runbooks" / rb_id
    d.mkdir(parents=True)
    script = d / "run.sh"
    script.write_text(body)
    script.chmod(0o755)
    runbooks.register_runbook(rb_id, runbook_type="exec", entry="run.sh")


def _wait(predicate, timeout=10.0):
    deadline = time.time() + timeout
    while time.time() < deadline:
        if predicate():
            return True
        time.sleep(0.1)
    return False


def test_smoke_arm_fires_real_exec_handler_then_self_stops(smoke_home, server):
    url, value = server
    marker = smoke_home / "fired.txt"
    _exec_runbook(smoke_home, f'#!/bin/sh\nprintf "%s" "$PICKET_PAYLOAD" > "{marker}"\n')

    res = watches.arm_watch(
        runbook_id="rb",
        endpoint={"url": url},
        predicate={"path": "$.last", "op": "lt", "value": 4800},
        cadence={"interval_seconds": 0.2},
        max_fires=1,
    )
    assert res["ok"] and psutil.pid_exists(res["pid"])

    value["last"] = 4700  # the real daemon will observe this on its next poll and fire
    assert _wait(lambda: store.read_jsonl(store.fires_path(res["watch_id"])))
    fires = store.read_jsonl(store.fires_path(res["watch_id"]))
    assert fires[-1]["status"] == "completed"
    assert marker.exists() and res["watch_id"] in marker.read_text()

    # max_fires=1 -> the daemon self-stops
    assert _wait(lambda: store.read_watch(res["watch_id"]).status == "stopped")


def test_smoke_stop_verify_before_kill_and_pause_resume(smoke_home, server):
    url, _ = server
    _exec_runbook(smoke_home, "#!/bin/sh\nexit 0\n")

    res = watches.arm_watch(
        runbook_id="rb",
        endpoint={"url": url},
        predicate={"path": "$.last", "op": "lt", "value": 1},  # 5000 < 1 is never true
        cadence={"interval_seconds": 0.2},
    )
    pid = res["pid"]
    assert psutil.pid_exists(pid)

    watches.pause_watch(res["watch_id"])
    assert _wait(lambda: store.read_watch(res["watch_id"]).status == "paused")
    watches.resume_watch(res["watch_id"])
    assert _wait(lambda: store.read_watch(res["watch_id"]).status == "active")

    watches.stop_watch(res["watch_id"], mode="immediate")
    assert _wait(lambda: not psutil.pid_exists(pid), timeout=5)  # verify-before-kill worked
