"""The agent's single door to data: the F7 tool contracts.

This module imports exactly one thing from the project -- ``bike_agent.tools.handlers``
-- and it never touches the filesystem.  ``config`` and ``layout`` are opaque
values the caller supplies; they are forwarded to the handlers untouched, so the
agent cannot learn a path, let alone read one (F11-3).
"""

from __future__ import annotations

from typing import Any, Callable

from bike_agent.tools.handlers import get_forecast, get_risk_ranking, run_simulation


class ToolGateway:
    """Records every call so the run can be audited afterwards (F11-4)."""

    def __init__(self, config: Any, layout: Any):
        self._config = config
        self._layout = layout
        self.calls: list[dict[str, Any]] = []

    # -- tools -------------------------------------------------------------
    def get_forecast(self, *, source: str, time_bucket: str | None = None) -> dict[str, Any]:
        return self._call(
            "get_forecast",
            get_forecast,
            {"source": source, "time_bucket": time_bucket},
        )

    def get_risk_ranking(self, *, source: str, direction: str, top_k: int) -> dict[str, Any]:
        return self._call(
            "get_risk_ranking",
            get_risk_ranking,
            {"source": source, "direction": direction, "top_k": top_k},
        )

    def run_simulation(self, *, source: str, strategy: str) -> dict[str, Any]:
        return self._call(
            "run_simulation",
            run_simulation,
            {"source": source, "strategy": strategy},
        )

    # -- audit -------------------------------------------------------------
    def mark(self) -> int:
        """A cursor into the call log, so a caller can take only what it added."""
        return len(self.calls)

    def since(self, mark: int) -> list[dict[str, Any]]:
        return self.calls[mark:]

    def _call(self, name: str, function: Callable[..., dict[str, Any]], arguments: dict[str, Any]) -> dict[str, Any]:
        payload = {key: value for key, value in arguments.items() if value is not None}
        result = function(self._config, self._layout, **payload)
        self.calls.append({"tool": name, "input": payload, "output": result})
        return result
