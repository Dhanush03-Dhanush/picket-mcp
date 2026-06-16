import pytest

from picket import runbooks, store
from picket.models import InvalidSpec


def _make_runbook(home, runbook_id="notify", entry="run.sh", body="#!/bin/sh\necho hi\n"):
    d = home / "runbooks" / runbook_id
    d.mkdir(parents=True)
    (d / entry).write_text(body)
    return d


def test_register_writes_toml_with_hash(home):
    _make_runbook(home)
    rb = runbooks.register_runbook(
        "notify", runbook_type="exec", entry="run.sh", description="notify me"
    )
    assert rb.content_hash
    assert (home / "runbooks" / "notify" / "runbook.toml").is_file()
    assert runbooks.read_runbook("notify") == rb


def test_register_rejects_entry_outside_runbook_dir(home):
    _make_runbook(home)
    (home / "runbooks" / "evil.sh").write_text("bad")
    with pytest.raises(InvalidSpec):
        runbooks.register_runbook("notify", runbook_type="exec", entry="../evil.sh")


def test_register_rejects_missing_entry(home):
    (home / "runbooks" / "notify").mkdir(parents=True)
    with pytest.raises(InvalidSpec):
        runbooks.register_runbook("notify", runbook_type="exec", entry="nope.sh")


def test_content_hash_changes_with_entry(home):
    d = _make_runbook(home)
    h1 = runbooks.register_runbook("notify", runbook_type="exec", entry="run.sh").content_hash
    (d / "run.sh").write_text("#!/bin/sh\necho changed\n")
    h2 = runbooks.register_runbook("notify", runbook_type="exec", entry="run.sh").content_hash
    assert h1 != h2


def test_list_runbooks(home):
    _make_runbook(home, "notify")
    runbooks.register_runbook("notify", runbook_type="exec", entry="run.sh")
    listed = runbooks.list_runbooks()
    assert [r["runbook_id"] for r in listed] == ["notify"]
    assert listed[0]["type"] == "exec"


def test_read_missing_runbook_is_none(home):
    assert runbooks.read_runbook("ghost") is None


def test_prepare_invocation_prompt_delivers_all_channels(home, tmp_path):
    d = home / "runbooks" / "analyze"
    d.mkdir(parents=True)
    (d / "prompt.md").write_text("Analyze the move.")
    rb = runbooks.register_runbook("analyze", runbook_type="prompt", entry="prompt.md")

    inv = runbooks.prepare_invocation(rb, {"watch_id": "wch_1", "value": 4700}, tmp_path)
    assert inv.kind == "prompt"
    assert "Analyze the move." in inv.prompt_text
    assert "wch_1" in inv.prompt_text  # payload rendered inline
    assert inv.env["PICKET_PAYLOAD_FILE"] == str(inv.payload_file)
    assert "wch_1" in inv.env["PICKET_PAYLOAD"]
    assert store.read_json(inv.payload_file)["value"] == 4700


def test_prepare_invocation_exec_has_no_prompt(home, tmp_path):
    _make_runbook(home, "notify")
    rb = runbooks.register_runbook("notify", runbook_type="exec", entry="run.sh")
    inv = runbooks.prepare_invocation(rb, {"watch_id": "wch_1"}, tmp_path)
    assert inv.kind == "exec"
    assert inv.prompt_text is None
    assert inv.entry_path == home / "runbooks" / "notify" / "run.sh"
