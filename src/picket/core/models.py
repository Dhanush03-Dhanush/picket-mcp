from __future__ import annotations

import re
from typing import Any, Literal, TypeVar
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from pydantic import BaseModel, Field, ValidationError, field_validator, model_validator

PredicateOp = Literal[
    "on_change",
    "lt",
    "gt",
    "lte",
    "gte",
    "eq",
    "ne",
    "pct_change",
    "crosses_above",
    "crosses_below",
]
BaselineMode = Literal["last_value", "arm_time", "prior_close", "absolute"]
SAFE_METHODS = ("GET", "HEAD")
_HHMM = re.compile(r"^([01]\d|2[0-3]):[0-5]\d$")
DELIVERY_EVENTS = ("completed", "failed", "timed_out", "dead_lettered")


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

    @field_validator("method")
    @classmethod
    def _safe_method(cls, v: str) -> str:
        v = v.upper()
        if v not in SAFE_METHODS:
            raise ValueError(f"method must be one of {SAFE_METHODS} (polling is observation only)")
        return v


class PredicateSpec(BaseModel):
    """How to extract and test a value. ``op=on_change`` ignores ``value``."""

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

    @field_validator("tz")
    @classmethod
    def _known_tz(cls, v: str) -> str:
        try:
            ZoneInfo(v)
        except (ZoneInfoNotFoundError, ValueError) as err:
            raise ValueError(f"unknown timezone {v!r}") from err
        return v

    @field_validator("start", "end")
    @classmethod
    def _hhmm(cls, v: str) -> str:
        if not _HHMM.match(v):
            raise ValueError(f"time {v!r} must be HH:MM (00:00–23:59)")
        return v

    @field_validator("days")
    @classmethod
    def _weekdays(cls, v: list[int]) -> list[int]:
        if any(d < 0 or d > 6 for d in v):
            raise ValueError("days must be weekday indexes 0 (Mon) .. 6 (Sun)")
        return v


class CadenceSpec(BaseModel):
    """How often to poll."""

    interval_seconds: float = Field(gt=0)
    jitter_seconds: float = Field(default=0, ge=0)
    active_window: ActiveWindow | None = None


WatchStatus = Literal["active", "paused", "stopping", "stopped", "errored"]


class WatchState(BaseModel):
    """The durable watch record (persisted as a row in SQLite)."""

    watch_id: str
    runbook_id: str
    runbook_rev: str | None = None
    endpoint: EndpointSpec | None = None
    predicate: PredicateSpec | None = None
    probe_id: str | None = None
    probe_rev: str | None = None
    probe_params: dict = Field(default_factory=dict)
    cadence: CadenceSpec
    label: str | None = None
    status: WatchStatus = "active"
    desired_status: Literal["active", "paused", "stopped"] = "active"
    max_fires: int | None = Field(default=1)
    ttl_seconds: float | None = None
    debounce_seconds: float = Field(default=0, ge=0)
    cooldown_seconds: float = Field(default=0, ge=0)
    handler_timeout_seconds: float = Field(default=600, gt=0)
    overlap_policy: Literal["drop"] = "drop"
    max_retries: int = Field(default=0, ge=0)
    drift_policy: Literal["block", "run"] = "block"
    notify_runbook: str | None = None
    delivery_events: list[str] = Field(default_factory=lambda: list(DELIVERY_EVENTS))
    skip_permissions: bool = False
    created_at: str | None = None
    baseline: Any = None
    last_value: Any = None
    last_observed_at: str | None = None
    last_error: str | None = None
    satisfied: bool = False
    satisfied_since: str | None = None
    fired_this_episode: bool = False
    episode_seq: int = 0
    heartbeat_at: str | None = None
    fire_count: int = 0
    last_fire_at: str | None = None

    pid: int | None = None
    pgid: int | None = None
    proc_create_time: float | None = None

    @field_validator("max_fires", "ttl_seconds")
    @classmethod
    def _positive_optional(cls, v: float | None) -> float | None:
        if v is not None and v <= 0:
            raise ValueError("must be > 0 when set")
        return v

    @field_validator("delivery_events")
    @classmethod
    def _known_events(cls, v: list[str]) -> list[str]:
        bad = [e for e in v if e not in DELIVERY_EVENTS]
        if bad:
            raise ValueError(f"unknown delivery events {bad}; choose from {DELIVERY_EVENTS}")
        return v

    @model_validator(mode="after")
    def _one_condition_source(self) -> WatchState:
        has_endpoint = self.endpoint is not None and self.predicate is not None
        if has_endpoint == bool(self.probe_id):
            raise ValueError("a watch needs exactly one of (endpoint+predicate) or probe_id")
        return self


class InvalidSpec(ValueError):
    """A spec dict failed validation; tools map this to the INVALID_SPEC envelope."""


_M = TypeVar("_M", bound=BaseModel)


def parse(model: type[_M], data: dict) -> _M:
    """Validate a dict into a spec model, raising InvalidSpec on failure."""
    try:
        return model.model_validate(data)
    except ValidationError as err:
        raise InvalidSpec(str(err)) from err
