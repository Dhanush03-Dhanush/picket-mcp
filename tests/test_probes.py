import pytest

from picket.conditions import probes
from picket.core.models import InvalidSpec

FIRE = "import json\nprint(json.dumps({'fire': True, 'value': 5, 'payload': {'k': 'v'}}))\n"
NO_FIRE = "import json\nprint(json.dumps({'fire': False, 'value': 5}))\n"
ECHO = (
    "import json, os\n"
    "print(json.dumps({'fire': True,"
    " 'value': json.loads(os.environ['PICKET_LAST_VALUE']),"
    " 'payload': json.loads(os.environ['PICKET_PARAMS'])}))\n"
)


def _make_probe(home, body, probe_id="p", language="python", entry=None):
    entry = entry or ("probe.py" if language == "python" else "probe.sh")
    d = home / "probes" / probe_id
    d.mkdir(parents=True)
    (d / entry).write_text(body)
    return probes.register_probe(probe_id, language=language, entry=entry)


# --- registration (NEW-16) --------------------------------------------------


def test_register_writes_toml_with_hash(home):
    p = _make_probe(home, FIRE, "spx")
    assert p.content_hash
    assert (home / "probes" / "spx" / "probe.toml").is_file()
    assert probes.read_probe("spx") == p


def test_register_rejects_entry_outside_probe_dir(home):
    (home / "probes" / "spx").mkdir(parents=True)
    (home / "probes" / "evil.py").write_text("bad")
    with pytest.raises(InvalidSpec):
        probes.register_probe("spx", language="python", entry="../evil.py")


def test_register_rejects_missing_entry(home):
    (home / "probes" / "spx").mkdir(parents=True)
    with pytest.raises(InvalidSpec):
        probes.register_probe("spx", language="python", entry="nope.py")


def test_list_probes(home):
    _make_probe(home, FIRE, "spx")
    listed = probes.list_probes()
    assert [r["probe_id"] for r in listed] == ["spx"]
    assert listed[0]["language"] == "python"


def test_read_missing_probe_is_none(home):
    assert probes.read_probe("ghost") is None


def test_has_drifted(home):
    p = _make_probe(home, FIRE, "spx")
    assert not probes.has_drifted(p)
    (home / "probes" / "spx" / "probe.py").write_text(NO_FIRE)
    assert probes.has_drifted(p)


# --- execution contract (NEW-17) --------------------------------------------


def test_run_probe_fires_and_carries_payload(home):
    p = _make_probe(home, FIRE)
    result = probes.run_probe(p, {})
    assert result.fire is True
    assert result.value == 5
    assert result.payload == {"k": "v"}


def test_run_probe_no_fire(home):
    p = _make_probe(home, NO_FIRE)
    assert probes.run_probe(p, {}).fire is False


def test_run_probe_receives_params_and_last_value(home):
    p = _make_probe(home, ECHO)
    result = probes.run_probe(p, {"floor": 100}, last_value=42)
    assert result.value == 42  # PICKET_LAST_VALUE round-tripped
    assert result.payload == {"floor": 100}  # PICKET_PARAMS round-tripped


def test_run_probe_nonzero_exit_is_probe_error(home):
    p = _make_probe(home, "import sys\nsys.exit(3)\n")
    with pytest.raises(probes.ProbeError):
        probes.run_probe(p, {})


def test_run_probe_unparseable_output_is_probe_error(home):
    p = _make_probe(home, "print('not json')\n")
    with pytest.raises(probes.ProbeError):
        probes.run_probe(p, {})


def test_run_probe_timeout_is_probe_error(home):
    p = _make_probe(home, "import time\ntime.sleep(5)\n")
    with pytest.raises(probes.ProbeError):
        probes.run_probe(p, {}, timeout=0.2)


def test_run_probe_sh_language(home):
    p = _make_probe(home, 'echo \'{"fire": true, "value": 1}\'\n', language="sh")
    assert probes.run_probe(p, {}).value == 1


def test_run_test_probe_reports_verdict_without_state(home):
    p = _make_probe(home, FIRE)
    out = probes.run_test_probe(p, {})
    assert out == {"ok": True, "would_fire": True, "value": 5, "payload": {"k": "v"}, "error": None}
    assert not (home / "watches").exists()  # dry run wrote no state


def test_run_test_probe_surfaces_probe_error(home):
    p = _make_probe(home, "import sys\nsys.exit(1)\n")
    out = probes.run_test_probe(p, {})
    assert out["would_fire"] is False and out["error"]
