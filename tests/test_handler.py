from picket.core.models import CadenceSpec, EndpointSpec, PredicateSpec, WatchState
from picket.execution import handler, runbooks
from picket.persistence import store


def _state(runbook_id="rb", **kw):
    return WatchState(
        watch_id="wch_1",
        runbook_id=runbook_id,
        endpoint=EndpointSpec(url="https://x/spx"),
        predicate=PredicateSpec(path="$.last", op="lt", value=4800),
        cadence=CadenceSpec(interval_seconds=30),
        baseline=4900,
        **kw,
    )


def _exec_runbook(home, body, rb_id="rb"):
    d = home / "runbooks" / rb_id
    d.mkdir(parents=True)
    script = d / "run.sh"
    script.write_text(body)
    script.chmod(0o755)
    return runbooks.register_runbook(rb_id, runbook_type="exec", entry="run.sh")


def _prompt_runbook(home, tools=None, rb_id="rb"):
    d = home / "runbooks" / rb_id
    d.mkdir(parents=True)
    (d / "prompt.md").write_text("Analyze the move.")
    return runbooks.register_runbook(
        rb_id, runbook_type="prompt", entry="prompt.md", allowed_tools=tools or []
    )


def _fake_runner(captured):
    def run(cmd, **kw):
        captured["cmd"] = cmd
        captured["env"] = kw["env"]
        return handler.HandlerResult(0, '{"result":"ok"}', "", 4242, False)

    return run


def test_exec_runbook_runs_directly_and_records_completed(home):
    _exec_runbook(home, "#!/bin/sh\nexit 0\n")
    rec = handler.fire(_state(), 4700)
    assert rec["status"] == "completed"
    assert rec["exit_code"] == 0
    assert len(store.recent_fires("wch_1")) == 1


def test_exec_runbook_nonzero_exit_records_failed(home):
    _exec_runbook(home, "#!/bin/sh\nexit 3\n")
    rec = handler.fire(_state(), 4700)
    assert rec["status"] == "failed"
    assert rec["exit_code"] == 3


def test_exec_runbook_receives_payload_env_and_file(home):
    # script exits 0 only if both payload channels are present at run time
    _exec_runbook(home, '#!/bin/sh\n[ -n "$PICKET_PAYLOAD" ] && [ -f "$PICKET_PAYLOAD_FILE" ]\n')
    rec = handler.fire(_state(), 4700)
    assert rec["status"] == "completed"


def test_prompt_runbook_builds_scoped_deny_by_default_command(home):
    _prompt_runbook(home, tools=["Read"])
    captured = {}
    rec = handler.fire(_state(), 4700, runner=_fake_runner(captured))
    cmd = captured["cmd"]

    assert "claude" in cmd[0]
    prompt = cmd[cmd.index("-p") + 1]
    assert "Analyze the move." in prompt and "wch_1" in prompt  # template + payload inline
    assert cmd[cmd.index("--permission-mode") + 1] == "dontAsk"  # deny-by-default
    assert cmd[cmd.index("--max-turns") + 1] == "30"
    assert cmd[cmd.index("--output-format") + 1] == "json"
    assert cmd[cmd.index("--allowedTools") + 1] == "Read"  # only the allowlisted tool
    assert "--add-dir" in cmd
    assert "--max-budget-usd" not in cmd  # §16.4: no budget flag
    assert captured["env"]["PICKET_PAYLOAD"]
    assert rec["status"] == "completed"
    assert rec["handler_pid"] == 4242 and rec["duration_ms"] is not None


def test_prompt_result_is_error_records_failed(home):
    _prompt_runbook(home, tools=["Read"])

    def run(cmd, **kw):
        return handler.HandlerResult(0, '{"is_error": true, "result": "denied"}', "", 1, False)

    rec = handler.fire(_state(), 4700, runner=run)
    assert rec["status"] == "failed"  # exit 0 but the handler reported an error


def test_handler_timeout_records_timed_out_with_transcript(home):
    _prompt_runbook(home, tools=["Read"])

    def run(cmd, **kw):
        return handler.HandlerResult(None, "partial output before kill", "", 99, True)

    rec = handler.fire(_state(), 4700, runner=run, timeout=5)
    assert rec["status"] == "timed_out"
    assert "timeout" in rec["error"]
    assert rec["transcript_tail"] == "partial output before kill"


def test_missing_runbook_records_failed_fire(home):
    store.ensure_root()
    rec = handler.fire(_state(runbook_id="ghost"), 4700)
    assert rec["status"] == "failed"
    assert "not found" in rec["error"]
    assert store.recent_fires("wch_1")[0]["status"] == "failed"


# --- NEW-12: retry / dead-letter / drift / notify --------------------------


def test_retry_then_dead_letter(home):
    _exec_runbook(home, "#!/bin/sh\nexit 1\n")  # always fails
    rec = handler.fire(_state(max_retries=2), 4700, sleeper=lambda s: None)
    assert rec["status"] == "dead_lettered"  # 1 + 2 retries all failed


def test_retry_then_success(home):
    _prompt_runbook(home, tools=["Read"])
    calls = []

    def run(cmd, **kw):
        calls.append(1)
        ok = len(calls) == 2
        return handler.HandlerResult(0 if ok else 1, "{}", "boom", 7, False)

    rec = handler.fire(_state(max_retries=1), 4700, runner=run, sleeper=lambda s: None)
    assert rec["status"] == "completed" and len(calls) == 2


def test_drift_block_refuses_and_does_not_run(home):
    _exec_runbook(home, "#!/bin/sh\nexit 0\n")
    (home / "runbooks" / "rb" / "run.sh").write_text("#!/bin/sh\nexit 0\n# changed\n")  # drift

    def run(cmd, **kw):
        raise AssertionError("must not launch a drifted runbook")

    rec = handler.fire(_state(), 4700, runner=run)
    assert rec["status"] == "failed" and "RUNBOOK_DRIFT" in rec["error"]


def test_drift_run_policy_executes_anyway(home):
    _exec_runbook(home, "#!/bin/sh\nexit 0\n")
    (home / "runbooks" / "rb" / "run.sh").write_text("#!/bin/sh\nexit 0\n# changed\n")

    def run(cmd, **kw):
        return handler.HandlerResult(0, "{}", "", 5, False)

    rec = handler.fire(_state(drift_policy="run"), 4700, runner=run)
    assert rec["status"] == "completed"


def test_dead_letter_triggers_notify_runbook(home):
    _exec_runbook(home, "#!/bin/sh\nexit 1\n", rb_id="rb")  # the watch's runbook always fails
    marker = home / "notified.txt"
    nd = home / "runbooks" / "notifier"
    nd.mkdir(parents=True)
    (nd / "run.sh").write_text(f'#!/bin/sh\necho "$PICKET_PAYLOAD" > "{marker}"\n')
    (nd / "run.sh").chmod(0o755)
    runbooks.register_runbook("notifier", runbook_type="exec", entry="run.sh")

    handler.fire(_state(max_retries=1, notify_runbook="notifier"), 4700, sleeper=lambda s: None)
    assert marker.exists() and "wch_1" in marker.read_text()
