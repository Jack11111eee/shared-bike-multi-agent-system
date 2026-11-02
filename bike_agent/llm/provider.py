"""Small injectable provider boundary. No SDK imports on the offline path."""
from __future__ import annotations

import os
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Protocol

from bike_agent.config import ConfigError


class ModelError(ConfigError):
    pass


class ModelLimitReached(ModelError):
    pass


def load_local_environment(root: Path) -> None:
    """Read only known configuration keys; never evaluate shell expressions."""
    allowed = {"DEEPSEEK_API_KEY", "DEEPSEEK_BASE_URL", "DEEPSEEK_MODEL",
               "AMAP_KEY", "AMAP_SECURITY_CODE"}
    path = root / ".env.local"
    if not path.is_file():
        return
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("export "):
            line = line[7:]
        key, sep, value = line.partition("=")
        key, value = key.strip(), value.strip()
        if sep and key in allowed:
            if len(value) > 1 and value[0] == value[-1] and value[0] in {"'", '"'}:
                value = value[1:-1]
            if value:
                os.environ.setdefault(key, value)


@dataclass
class ModelReply:
    message: dict[str, Any]
    model: str
    request_id: str | None = None
    usage: dict[str, Any] | None = None
    finish_reason: str | None = None
    metadata: dict[str, Any] = field(default_factory=dict)


class Provider(Protocol):
    name: str
    model: str
    is_real: bool

    def complete(self, *, messages: list[dict], tools: list[dict],
                 timeout: float) -> ModelReply: ...


def remaining_timeout(deadline: float, requested: float) -> float:
    value = min(requested, deadline - time.monotonic())
    if value <= 0:
        raise ModelLimitReached("任务截止时间已到")
    return value
