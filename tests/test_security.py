from picket import condition, daemon, handler, runbooks, store, watches
from picket.models import CadenceSpec, EndpointSpec, PredicateSpec, WatchState

EP = {"url": "https://x/spx", "auth_ref": "SECRET_TOKEN"}
PR = {"path": "$.last", "op": "lt", "value": 4800}
CAD = {"interval_seconds": 30}


def _register_runbook(home, rb_id="rb"):
    d = home / "runbooks" / rb_id
    d.mkdir(parents=True)
    (d / "prompt.md").write_text("Do the thing.")
    runbooks.register_runbook(
        rb_id, runbook_type="prompt", entry="prompt.md", allowed_tools=["Read"]
    )


def _fake_spawn(monkeypatch):
    monkeypatch.setattr(daemon, "spawn", lambda wid: None)
    monkeypatch.setattr(watches, "_await_identity", lambda wid, **k: _patch_identity(wid))


def _patch_identity(watch_id):
    st = store.read_watch(watch_id)
    st.pid = 4242
    st.pgid = 4242
    st.proc_create_time = 1.0
    store.write_watch(st)
    return st


# --- skip-permissions gating + invocation (NEW-13.1 / .2) ------------------


def test_skip_permissions_requires_confirm(home):
    res = watches.arm_watch(
        runbook_id="rb", endpoint=EP, predicate=PR, cadence=CAD, skip_permissions=True
    )
    assert res["error_code"] == "PERMISSION_REQUIRED"


def test_skip_permissions_with_confirm_is_recorded(home, monkeypatch):
    _register_runbook(home)
    monkeypatch.setenv("SECRET_TOKEN", "tok")
    monkeypatch.setattr(condition, "fetch", lambda ep, **k: {"last": 4850})
    _fake_spawn(monkeypatch)
    res = watches.arm_watch(
        runbook_id="rb",
        endpoint=EP,
        predicate=PR,
        cadence=CAD,
        skip_permissions=True,
        confirm_skip=True,
    )
    assert res["ok"] and store.read_watch(res["watch_id"]).skip_permissions is True


def test_skip_permissions_command_uses_disallowed_guardrails(home):
    d = home / "runbooks" / "rb"
    d.mkdir(parents=True)
    (d / "prompt.md").write_text("go")
    rb = runbooks.register_runbook(
        "rb", runbook_type="prompt", entry="prompt.md", allowed_tools=["Read"]
    )
    inv = runbooks.prepare_invocation(rb, {"watch_id": "w"}, home)

    cmd = handler.handler_command(rb, inv, 30, skip_permissions=True)
    assert "--dangerously-skip-permissions" in cmd
    assert "--disallowedTools" in cmd
    assert "Bash(rm:*)" in cmd and "Bash(curl:*)" in cmd
    assert "--allowedTools" not in cmd  # ignored under bypass; would be unsafe
    assert "--permission-mode" not in cmd


# --- secret-ref: no literal credential on disk (NEW-13.4) ------------------


def test_auth_ref_leaves_no_literal_credential_on_disk(home, monkeypatch):
    secret = "supersecretvalue123"
    monkeypatch.setenv("SECRET_TOKEN", secret)
    _register_runbook(home)
    monkeypatch.setattr(condition, "fetch", lambda ep, **k: {"last": 4850})
    _fake_spawn(monkeypatch)

    res = watches.arm_watch(runbook_id="rb", endpoint=EP, predicate=PR, cadence=CAD)
    assert res["ok"]

    for path in home.rglob("*"):
        if path.is_file():
            assert secret not in path.read_text(errors="ignore")
    # only the env-var NAME is persisted, never the value
    assert "SECRET_TOKEN" in store.watch_path(res["watch_id"]).read_text()


def test_build_payload_carries_no_credential():
    state = WatchState(
        watch_id="w",
        runbook_id="rb",
        endpoint=EndpointSpec(url="https://x", auth_ref="SECRET_TOKEN"),
        predicate=PredicateSpec(path="$.last", op="lt", value=1),
        cadence=CadenceSpec(interval_seconds=1),
    )
    payload = handler.build_payload(state, 1, "2026-01-01")
    assert "SECRET_TOKEN" not in str(payload)  # the env-var name isn't leaked into payloads either
