import subprocess

from picket import handler, runbooks, store
from picket.models import CadenceSpec, EndpointSpec, PredicateSpec, WatchState


def _state(runbook_id="rb"):
    return WatchState(
        watch_id="wch_1",
        runbook_id=runbook_id,
        endpoint=EndpointSpec(url="https://x/spx"),
        predicate=PredicateSpec(path="$.last", op="lt", value=4800),
        cadence=CadenceSpec(interval_seconds=30),
        baseline=4900,
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
        return subprocess.CompletedProcess(cmd, 0, stdout='{"result":"ok"}', stderr="")

    return run


def test_exec_runbook_runs_directly_and_records_completed(home):
    _exec_runbook(home, "#!/bin/sh\nexit 0\n")
    rec = handler.fire(_state(), 4700)
    assert rec["status"] == "completed"
    assert rec["exit_code"] == 0
    assert len(store.read_jsonl(store.fires_path("wch_1"))) == 1


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


def test_prompt_result_is_error_records_failed(home):
    _prompt_runbook(home, tools=["Read"])

    def run(cmd, **kw):
        return subprocess.CompletedProcess(
            cmd, 0, stdout='{"is_error": true, "result": "denied"}', stderr=""
        )

    rec = handler.fire(_state(), 4700, runner=run)
    assert rec["status"] == "failed"  # exit 0 but the handler reported an error


def test_missing_runbook_records_failed_fire(home):
    store.ensure_root()
    rec = handler.fire(_state(runbook_id="ghost"), 4700)
    assert rec["status"] == "failed"
    assert "not found" in rec["error"]
    assert store.read_jsonl(store.fires_path("wch_1"))[0]["status"] == "failed"
