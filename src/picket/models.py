"""Typed input specs for a watch (§8).

These three models describe *what* to watch; they are validated at the tool
boundary, where a ``ValidationError`` becomes an ``INVALID_SPEC`` failure.
Later phases extend the predicate op set (NEW-11) and the cadence window
(NEW-11); the v0 surface lives here.
"""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, Field, field_validator

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


class CadenceSpec(BaseModel):
    """How often to poll."""

    interval_seconds: float = Field(gt=0)
