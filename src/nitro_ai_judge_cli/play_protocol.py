"""Shared Play manager wire constants, validation, and typed errors."""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
import re
from typing import Any


API_VERSION = 1
MANAGER_IDENTITY = "naij-play-manager"
MANAGER_VERSION = "3.1.0"
MINIMUM_CLI_VERSION = "3.0.0"
DEFAULT_MANAGER_BIND = "127.0.0.1"
DEFAULT_MANAGER_PORT = 51123
DEFAULT_MANAGER_IMAGE = (
    "ghcr.io/mihneateodorstoica/naij-play-manager:3.1.0"
)
BASE_PATH = "/nitro"

SLUG_RE = re.compile(r"^[a-z0-9](?:[a-z0-9._-]{0,61}[a-z0-9])?$")
RESERVED_SLUGS = {
    "api",
    "assets",
    "competitions",
    "health",
    "info",
    "login",
    "logout",
    "operations",
    "proxy",
}


class ImageState(str, Enum):
    MISSING = "missing"
    PULLING = "pulling"
    READY = "ready"
    ERROR = "error"


class WorkspaceState(str, Enum):
    MISSING = "missing"
    READY = "ready"
    RUNNING = "running"
    STOPPED = "stopped"
    ERROR = "error"


class OperationStage(str, Enum):
    QUEUED = "queued"
    VALIDATING = "validating"
    PULLING = "pulling"
    PREPARING = "preparing"
    APPLYING = "applying"
    VERIFYING = "verifying"
    COMPLETE = "complete"
    FAILED = "failed"
    CANCELLED = "cancelled"
    INTERRUPTED = "interrupted"


class ServiceHealth(str, Enum):
    UNKNOWN = "unknown"
    STARTING = "starting"
    HEALTHY = "healthy"
    UNHEALTHY = "unhealthy"
    STOPPED = "stopped"


class ErrorType(str, Enum):
    AUTHENTICATION_REQUIRED = "authentication_required"
    COMPETITION_BUSY = "competition_busy"
    DOCKER_UNAVAILABLE = "docker_unavailable"
    INVALID_COMPETITION = "invalid_competition"
    INVALID_REQUEST = "invalid_request"
    MANAGER_INCOMPATIBLE = "manager_incompatible"
    NOT_FOUND = "not_found"
    OWNERSHIP_MISMATCH = "ownership_mismatch"
    OPERATION_FAILED = "operation_failed"
    SECURITY_ERROR = "security_error"


ACTION_NAMES = {
    "pull",
    "play",
    "start",
    "stop",
    "restart",
    "recreate",
    "delete-image",
    "delete-container",
    "delete-workspace",
}
LONG_ACTIONS = set(ACTION_NAMES)


@dataclass(frozen=True)
class WireError(RuntimeError):
    type: str
    message: str
    stage: str | None = None
    logs: tuple[str, ...] = ()
    status: int = 400

    def __str__(self) -> str:
        return self.message

    def as_dict(self) -> dict[str, Any]:
        return {
            "type": self.type,
            "message": self.message,
            "stage": self.stage,
            "logs": list(self.logs[-40:]),
        }


def validate_slug(value: str, *, field: str = "competition") -> str:
    if value != value.lower() or not SLUG_RE.fullmatch(value):
        raise WireError(
            ErrorType.INVALID_COMPETITION.value,
            f"{field} must be a canonical lowercase Nitro slug",
            status=400,
        )
    if value in RESERVED_SLUGS:
        raise WireError(
            ErrorType.INVALID_COMPETITION.value,
            f"{field} slug {value!r} is reserved",
            status=400,
        )
    return value


def validate_competition(org: str, competition: str) -> tuple[str, str]:
    return validate_slug(org, field="organization"), validate_slug(competition)


def competition_key(org: str, competition: str) -> str:
    org, competition = validate_competition(org, competition)
    return f"{org}/{competition}"


def parse_version(value: str) -> tuple[int, int, int]:
    match = re.match(r"^(\d+)\.(\d+)\.(\d+)", value)
    return tuple(map(int, match.groups())) if match else (0, 0, 0)
