import httpx
import pytest

from picket import condition
from picket.models import EndpointSpec, PredicateSpec


def _client(handler):
    return httpx.Client(transport=httpx.MockTransport(handler))


# --- extract ---------------------------------------------------------------


def test_extract_jsonpath_and_dotted_and_list():
    data = {"a": {"b": 3}, "items": [{"x": 9}]}
    assert condition.extract(data, "$.a.b") == 3
    assert condition.extract(data, "a.b") == 3  # dotted fallback (no $)
    assert condition.extract(data, "$.items[0].x") == 9
    assert condition.extract(data, "items.0.x") == 9  # dotted list index


def test_extract_missing_raises_observe_error():
    with pytest.raises(condition.ObserveError):
        condition.extract({"a": 1}, "a.z")


# --- predicate + edge ------------------------------------------------------


def test_threshold_and_coercion():
    pr = PredicateSpec(path="$.x", op="gt", value=4800)
    assert condition.is_satisfied(pr, 4850) is True
    assert condition.is_satisfied(pr, "4850") is True  # string coerced to number
    assert condition.is_satisfied(pr, 4700) is False


def test_non_numeric_value_is_observe_error():
    pr = PredicateSpec(path="$.x", op="lt", value=10)
    with pytest.raises(condition.ObserveError):
        condition.is_satisfied(pr, "not-a-number")


def test_on_change_uses_baseline():
    pr = PredicateSpec(path="$.x", op="on_change")
    assert condition.is_satisfied(pr, 6, baseline=5) is True
    assert condition.is_satisfied(pr, 5, baseline=5) is False
    assert condition.is_satisfied(pr, 6, baseline=None) is False  # nothing to compare yet


# --- fetch -----------------------------------------------------------------


def test_fetch_parses_json():
    data = condition.fetch(
        EndpointSpec(url="https://x/spx"),
        client=_client(lambda r: httpx.Response(200, json={"last": 4850})),
    )
    assert data == {"last": 4850}


def test_fetch_injects_auth_from_env_without_persisting_literal(monkeypatch):
    monkeypatch.setenv("SPX_TOKEN", "secret123")
    seen = {}

    def handler(request):
        seen["auth"] = request.headers.get("Authorization")
        return httpx.Response(200, json={"ok": 1})

    ep = EndpointSpec(url="https://x/spx", auth_ref="SPX_TOKEN")
    condition.fetch(ep, client=_client(handler))
    assert seen["auth"] == "Bearer secret123"
    assert "secret123" not in ep.model_dump_json()  # only the env-var NAME is on the spec


def test_fetch_missing_auth_env_raises(monkeypatch):
    monkeypatch.delenv("SPX_TOKEN", raising=False)
    with pytest.raises(condition.ObserveError):
        condition.fetch(
            EndpointSpec(url="https://x", auth_ref="SPX_TOKEN"), client=_client(lambda r: None)
        )


def test_fetch_http_error_and_bad_json_are_observe_errors():
    with pytest.raises(condition.ObserveError):
        condition.fetch(
            EndpointSpec(url="https://x"), client=_client(lambda r: httpx.Response(404))
        )
    with pytest.raises(condition.ObserveError):
        condition.fetch(
            EndpointSpec(url="https://x"),
            client=_client(lambda r: httpx.Response(200, text="nope")),
        )


# --- test_predicate dry run -----------------------------------------------


def test_run_test_predicate_reports_would_fire(monkeypatch):
    monkeypatch.setattr(condition, "fetch", lambda ep, **k: {"last": 4700})
    res = condition.run_test_predicate(
        EndpointSpec(url="https://x"), PredicateSpec(path="$.last", op="lt", value=4800)
    )
    assert res["would_fire"] is True
    assert res["extracted_value"] == 4700
    assert res["extract_error"] is None


def test_run_test_predicate_never_fires_on_observe_failure(monkeypatch):
    def boom(ep, **k):
        raise condition.ObserveError("endpoint down")

    monkeypatch.setattr(condition, "fetch", boom)
    res = condition.run_test_predicate(
        EndpointSpec(url="https://x"), PredicateSpec(path="$.last", op="lt", value=1)
    )
    assert res["would_fire"] is False
    assert res["extract_error"] == "endpoint down"


def test_run_test_predicate_reports_extract_error(monkeypatch):
    monkeypatch.setattr(condition, "fetch", lambda ep, **k: {"other": 1})
    res = condition.run_test_predicate(
        EndpointSpec(url="https://x"), PredicateSpec(path="$.last", op="lt", value=1)
    )
    assert res["would_fire"] is False
    assert "not found" in res["extract_error"]


# --- NEW-11: pct_change, crosses, baseline capture -------------------------


def test_pct_change_signed_threshold():
    drop = PredicateSpec(path="$.last", op="pct_change", value=-2)  # dropped >= 2%
    assert condition.is_satisfied(drop, 98, baseline=100) is True
    assert condition.is_satisfied(drop, 99, baseline=100) is False
    assert condition.is_satisfied(drop, 102, baseline=100) is False
    assert condition.is_satisfied(drop, 98, baseline=None) is False  # no baseline yet

    rise = PredicateSpec(path="$.last", op="pct_change", value=2)  # up >= 2%
    assert condition.is_satisfied(rise, 102, baseline=100) is True
    assert condition.is_satisfied(rise, 101, baseline=100) is False


def test_crosses_above_below_are_threshold_checks():
    below = PredicateSpec(path="$.x", op="crosses_below", value=10)
    above = PredicateSpec(path="$.x", op="crosses_above", value=10)
    assert condition.is_satisfied(below, 9) is True
    assert condition.is_satisfied(below, 11) is False
    assert condition.is_satisfied(above, 11) is True
    assert condition.is_satisfied(above, 9) is False


def test_initial_baseline_modes():
    data = {"last": 4850, "prev_close": 4900}
    lt = PredicateSpec(path="$.last", op="lt", value=1)
    assert condition.initial_baseline(lt, 4850, data) is None
    on_change = PredicateSpec(path="$.last", op="on_change")
    assert condition.initial_baseline(on_change, 4850, data) == 4850

    arm = PredicateSpec(path="$.last", op="pct_change", value=-2, baseline_mode="arm_time")
    assert condition.initial_baseline(arm, 4850, data) == 4850
    absolute = PredicateSpec(
        path="$.last", op="pct_change", value=-2, baseline_mode="absolute", baseline_value=5000
    )
    assert condition.initial_baseline(absolute, 4850, data) == 5000
    prior = PredicateSpec(
        path="$.last",
        op="pct_change",
        value=-2,
        baseline_mode="prior_close",
        baseline_path="$.prev_close",
    )
    assert condition.initial_baseline(prior, 4850, data) == 4900
