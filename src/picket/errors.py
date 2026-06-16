"""Standard failure envelope and error codes (§12).

Every tool returns either a success dict (``{"ok": True, ...}``) or the failure
envelope produced by :func:`failure`.
"""

from __future__ import annotations

from enum import StrEnum
from typing import Any


class ErrorCode(StrEnum):
    NOT_FOUND = "NOT_FOUND"
    INVALID_SPEC = "INVALID_SPEC"
    RUNBOOK_NOT_FOUND = "RUNBOOK_NOT_FOUND"
    RUNBOOK_DRIFT = "RUNBOOK_DRIFT"
    DAEMON_SPAWN_FAILED = "DAEMON_SPAWN_FAILED"
    PERMISSION_REQUIRED = "PERMISSION_REQUIRED"
    ENDPOINT_UNREACHABLE = "ENDPOINT_UNREACHABLE"
    ALREADY_STOPPED = "ALREADY_STOPPED"


def failure(code: ErrorCode, message: str) -> dict[str, Any]:
    """Build the standard failure envelope."""
    return {"ok": False, "error_code": code.value, "message": message}
