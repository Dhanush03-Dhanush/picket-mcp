"""Typed input specs for a watch (§8).

These three models describe *what* to watch; they are validated at the tool
boundary, where a ``ValidationError`` becomes an ``INVALID_SPEC`` failure.
Later phases extend the predicate op set (NEW-11) and the cadence window
(NEW-11); the v0 surface lives here.
"""

from __future__ import annotations

from typing import Any, Literal, TypeVar

from pydantic import BaseModel, Field, ValidationError, field_validator, model_validator

# v0 predicate operators (NEW-4). NEW-11 extends this set.
PredicateOp = Literal["on_change", "lt", "gt", "lte", "gte", "eq", "ne"]


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
    """How to extract and test a value. ``op=on_change`` ignores ``value``."""

    path: str
    op: PredicateOp
    value: float | str | None = None

    @model_validator(mode="after")
    def _threshold_needs_value(self) -> PredicateSpec:
        if self.op != "on_change" and self.value is None:
            raise ValueError(f"op {self.op!r} requires 'value'")
        return self


class CadenceSpec(BaseModel):
    """How often to poll."""

    interval_seconds: float = Field(gt=0)


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
    overlap_policy: Literal["drop"] = "drop"
    skip_permissions: bool = False
    created_at: str | None = None

    # observation — written each loop by the daemon
    baseline: Any = None
    last_value: Any = None
    last_observed_at: str | None = None
    last_error: str | None = None
    satisfied: bool = False
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
