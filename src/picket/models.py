"""Typed input specs for a watch (§8).

These three models describe *what* to watch; they are validated at the tool
boundary, where a ``ValidationError`` becomes an ``INVALID_SPEC`` failure.
Later phases extend the predicate op set (NEW-11) and the cadence window
(NEW-11); the v0 surface lives here.
"""

from __future__ import annotations

from typing import Any, Literal, TypeVar

from pydantic import BaseModel, Field, ValidationError, field_validator, model_validator

PredicateOp = Literal[
    "on_change",
    "lt",
    "gt",
    "lte",
    "gte",
    "eq",
    "ne",  # NEW-4
    "pct_change",
    "crosses_above",
    "crosses_below",  # NEW-11
]
BaselineMode = Literal["last_value", "arm_time", "prior_close", "absolute"]


class EndpointSpec(BaseModel):
    """The HTTP endpoint to poll. ``auth_ref`` names an env var, never a literal."""

    url: str
    method: str = "GET"
    headers: dict[str, str] = Field(default_factory=dict)
    body: Any = None
    auth_ref: str | None = None

    @field_validator("url")
    @classmethod
    def _http_url(cls, v: str) -> str:
        if not v.startswith(("http://", "https://")):
            raise ValueError("url must be http(s)")
        return v


class PredicateSpec(BaseModel):
    """How to extract and test a value. ``op=on_change`` ignores ``value``.

    For ``pct_change``, ``value`` is the signed % threshold (e.g. -2 = dropped 2%)
    and ``baseline_mode`` says what to measure against: ``last_value`` (prior poll),
    ``arm_time`` (value when armed), ``prior_close`` (``baseline_path`` extracted at
    arm time), or ``absolute`` (``baseline_value``). Non-last_value baselines are
    captured + persisted at arm time so a restart restores rather than recomputes.
    """

    path: str
    op: PredicateOp
    value: float | str | None = None
    baseline_mode: BaselineMode = "last_value"
    baseline_value: float | None = None
    baseline_path: str | None = None

    @model_validator(mode="after")
    def _check(self) -> PredicateSpec:
        if self.op != "on_change" and self.value is None:
            raise ValueError(f"op {self.op!r} requires 'value'")
        if self.op == "pct_change":
            if self.baseline_mode == "absolute" and self.baseline_value is None:
                raise ValueError("pct_change absolute baseline requires baseline_value")
            if self.baseline_mode == "prior_close" and not self.baseline_path:
                raise ValueError("pct_change prior_close baseline requires baseline_path")
        return self


class ActiveWindow(BaseModel):
    """When polling is allowed (else the daemon idles). Weekdays: Mon=0 .. Sun=6."""

    tz: str = "UTC"
    start: str = "00:00"
    end: str = "23:59"
    days: list[int] = Field(default_factory=lambda: [0, 1, 2, 3, 4, 5, 6])


class CadenceSpec(BaseModel):
    """How often to poll."""

    interval_seconds: float = Field(gt=0)
    jitter_seconds: float = 0
    active_window: ActiveWindow | None = None


WatchStatus = Literal["active", "paused", "stopped", "errored"]


class WatchState(BaseModel):
    """The watches/<id>.json record (§6) — the single source of truth for a watch.

    Fields are grouped by writer. The server writes the spec/limits block once at
    arm time; after the daemon spawns it is the sole writer of the observation and
    process-identity blocks (single-writer ownership rule, NEW-2).
    """

    # spec + limits — written once by the server at arm time
    watch_id: str
    runbook_id: str
    endpoint: EndpointSpec
    predicate: PredicateSpec
    cadence: CadenceSpec
    label: str | None = None
    status: WatchStatus = "active"
    max_fires: int | None = None
    ttl_seconds: float | None = None
    debounce_seconds: float = 0
    cooldown_seconds: float = 0
    handler_timeout_seconds: float = 600
    overlap_policy: Literal["drop"] = "drop"
    skip_permissions: bool = False
    created_at: str | None = None

    # observation — written each loop by the daemon
    baseline: Any = None
    last_value: Any = None
    last_observed_at: str | None = None
    last_error: str | None = None
    satisfied: bool = False
    satisfied_since: str | None = None  # episode start, for debounce
    fired_this_episode: bool = False  # one fire per satisfied episode
    heartbeat_at: str | None = None
    fire_count: int = 0
    last_fire_at: str | None = None

    # process identity — captured at spawn for verify-before-kill
    pid: int | None = None
    pgid: int | None = None
    proc_create_time: float | None = None


class InvalidSpec(ValueError):
    """A spec dict failed validation; tools map this to the INVALID_SPEC envelope."""


_M = TypeVar("_M", bound=BaseModel)


def parse(model: type[_M], data: dict) -> _M:
    """Validate a dict into a spec model, raising InvalidSpec on failure."""
    try:
        return model.model_validate(data)
    except ValidationError as err:
        raise InvalidSpec(str(err)) from err
