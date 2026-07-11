import json
import os
import signal
import threading
import time
from http.server import BaseHTTPRequestHandler, HTTPServer

import pytest

from picket.conditions import probes
from picket.execution import runbooks
from picket.persistence import store
from picket.runtime import watches


@pytest.fixture
def home(tmp_path, monkeypatch):
    """Point PICKET_HOME at a throwaway dir so tests never touch ~/.claude/picket."""
    monkeypatch.setenv("PICKET_HOME", str(tmp_path))
    return tmp_path


# --- smoke-test fixtures (real processes; shared by the smoke suites) -------


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
    """Temp PICKET_HOME plus a teardown that stops and reaps EVERY real daemon."""
    store.ensure_root()
    yield home
    watches.stop_all_watches(confirm=True, status_filter="all", mode="immediate")
    for watch_id in store.all_watch_ids():
        state = store.read_watch(watch_id)
        if state and state.pgid:
            try:
                os.killpg(state.pgid, signal.SIGKILL)
            except (ProcessLookupError, OSError):
                pass


@pytest.fixture
def poll_until():
    def _wait(predicate, timeout=10.0):
        deadline = time.time() + timeout
        while time.time() < deadline:
            if predicate():
                return True
            time.sleep(0.1)
        return False

    return _wait


@pytest.fixture
def exec_runbook(home):
    """Factory: register an exec runbook (no LLM, no tokens) from a shell body."""

    def _make(body, rb_id="rb"):
        d = home / "runbooks" / rb_id
        d.mkdir(parents=True)
        script = d / "run.sh"
        script.write_text(body)
        script.chmod(0o755)
        runbooks.register_runbook(rb_id, runbook_type="exec", entry="run.sh")

    return _make


@pytest.fixture
def probe(home):
    """Factory: register a python probe (condition script) from a body."""

    def _make(body, probe_id="pr"):
        d = home / "probes" / probe_id
        d.mkdir(parents=True)
        (d / "probe.py").write_text(body)
        probes.register_probe(probe_id, language="python", entry="probe.py")

    return _make


@pytest.fixture
def prompt_runbook(home):
    """Factory: register a prompt runbook (launches a real claude -p)."""

    def _make(text, tools=None, rb_id="rb"):
        d = home / "runbooks" / rb_id
        d.mkdir(parents=True)
        (d / "prompt.md").write_text(text)
        runbooks.register_runbook(
            rb_id, runbook_type="prompt", entry="prompt.md", allowed_tools=tools or []
        )

    return _make
