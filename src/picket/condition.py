"""The deterministic core (§8/§10): fetch + extract + predicate evaluation.

Pure Python, no model in the loop — this is why waiting is free. A failure to
*observe* (network/timeout/decode/missing field) is never a fire: it raises
:class:`ObserveError`, which the daemon records as ``last_error`` and skips.
"""

from __future__ import annotations

import json
import os
from typing import Any

import httpx
from jsonpath_ng import parse as jsonpath_parse
from jsonpath_ng.exceptions import JSONPathError

from picket.models import EndpointSpec, PredicateSpec


class ObserveError(Exception):
    """Could not observe the value (fetch/extract/type failure). Never fires."""


def fetch(
    endpoint: EndpointSpec, *, client: httpx.Client | None = None, timeout: float = 10.0
) -> Any:
    """GET/POST the endpoint and parse JSON. ``auth_ref`` is read from env here."""
    headers = dict(endpoint.headers)
    if endpoint.auth_ref:
        token = os.environ.get(endpoint.auth_ref)
        if token is None:
            raise ObserveError(f"auth_ref {endpoint.auth_ref!r} is not set in the environment")
        headers["Authorization"] = f"Bearer {token}"

    owned = client is None
    client = client or httpx.Client(timeout=timeout)
    try:
        resp = client.request(endpoint.method, endpoint.url, headers=headers, json=endpoint.body)
        resp.raise_for_status()
        return resp.json()
    except (httpx.HTTPError, json.JSONDecodeError) as err:
        raise ObserveError(f"fetch failed: {err}") from err
    finally:
        if owned:
            client.close()


def extract(data: Any, path: str) -> Any:
    """Extract a value by JSONPath (jsonpath-ng), falling back to a dotted path."""
    try:
        matches = jsonpath_parse(path).find(data)
        if matches:
            return matches[0].value
    except JSONPathError:
        pass

    cur = data
    for part in path.lstrip("$.").split("."):
        if not part:
            continue
        if isinstance(cur, dict) and part in cur:
            cur = cur[part]
        elif isinstance(cur, list) and part.lstrip("-").isdigit():
            cur = cur[int(part)]
        else:
            raise ObserveError(f"path not found: {path!r}")
    return cur


def _as_target_type(value: Any, target: Any) -> Any:
    """Coerce the observed value to the threshold's type for comparison."""
    if isinstance(target, (int, float)) and not isinstance(target, bool):
        try:
            return float(value)
        except (TypeError, ValueError) as err:
            raise ObserveError(f"value {value!r} is not numeric") from err
    return value


def is_satisfied(predicate: PredicateSpec, value: Any, baseline: Any = None) -> bool:
    """Whether the predicate currently holds (stateless except for baseline-relative ops)."""
    if predicate.op == "on_change":
        return baseline is not None and value != baseline

    if predicate.op == "pct_change":
        # Coerce a numeric-string baseline (common in financial APIs) so a
        # "prior_close" of "4900" measures correctly instead of crashing the daemon.
        base = _as_target_type(baseline, 0.0) if baseline is not None else 0.0
        if not base:  # None, 0, or "0": nothing to measure against
            return False
        pct = (_as_target_type(value, 0.0) - base) / base * 100
        threshold = float(predicate.value)
        return pct <= threshold if threshold < 0 else pct >= threshold

    target = predicate.value
    v = _as_target_type(value, target)
    return {
        "lt": v < target,
        "lte": v <= target,
        "gt": v > target,
        "gte": v >= target,
        "eq": v == target,
        "ne": v != target,
        "crosses_below": v < target,  # the edge model makes this a crossing
        "crosses_above": v > target,
    }[predicate.op]


def initial_baseline(predicate: PredicateSpec, trial_value: Any, trial_data: Any) -> Any:
    """Capture the baseline to persist at arm time (so a restart never recomputes it)."""
    if predicate.op == "on_change":
        return trial_value
    if predicate.op == "pct_change":
        if predicate.baseline_mode == "arm_time":
            return trial_value
        if predicate.baseline_mode == "absolute":
            return predicate.baseline_value
        if predicate.baseline_mode == "prior_close":
            return extract(trial_data, predicate.baseline_path)
        return trial_value  # last_value: start from the arm-time value
    return None  # lt/gt/crosses_* compare to a fixed threshold, no baseline needed


def _excerpt(data: Any, limit: int = 500) -> str:
    return json.dumps(data)[:limit]


def run_test_predicate(endpoint: EndpointSpec, predicate: PredicateSpec) -> dict:
    """One fetch+evaluate, no daemon and no state. Never raises (dry-run tool, §8)."""
    try:
        data = fetch(endpoint)
    except ObserveError as err:
        return _result(would_fire=False, extract_error=str(err))

    excerpt = _excerpt(data)
    try:
        value = extract(data, predicate.path)
    except ObserveError as err:
        return _result(would_fire=False, response_excerpt=excerpt, extract_error=str(err))

    # Evaluate against the baseline arm would capture, so a dry run of a stateful
    # op (pct_change / on_change) reflects the real first-poll decision.
    try:
        baseline = initial_baseline(predicate, value, data)
        satisfied = is_satisfied(predicate, value, baseline=baseline)
    except ObserveError as err:
        return _result(
            would_fire=False,
            response_excerpt=excerpt,
            extracted_value=value,
            extract_error=str(err),
        )

    return _result(
        would_fire=satisfied, response_excerpt=excerpt, extracted_value=value, baseline=baseline
    )


def _result(
    *, would_fire, response_excerpt=None, extracted_value=None, extract_error=None, baseline=None
) -> dict:
    return {
        "ok": True,
        "would_fire": would_fire,
        "extracted_value": extracted_value,
        "baseline": baseline,
        "response_excerpt": response_excerpt,
        "extract_error": extract_error,
    }
