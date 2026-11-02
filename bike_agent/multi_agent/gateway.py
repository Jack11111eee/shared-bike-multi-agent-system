"""The only door to business data (F24-4 / F26).

Every role goes through this gateway.  It enforces, in order:

1. the role is allowed to call that tool at all;
2. the call parameters are legal for *this* task (source, strategy, budget);
3. the frozen inputs are unchanged;
4. the call/task limits still hold.

Only then does it invoke a backend -- the original F7 handlers for the three
old tools, or the thin ``evaluate_dispatch_candidate`` wrapper around
``run_simulation(..., write=False)``.  A permission denial therefore provably
happens before the backend is touched, which is what the audit records and the
tests assert.

The gateway is also the run's cache: an evaluation keyed by
``(frozen_context_id, source, strategy, budget)`` is reused, and a cache hit is
recorded as an audit event rather than disguised as a fresh execution.
"""

from __future__ import annotations

import copy
import hashlib
import time
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from concurrent.futures import TimeoutError as FutureTimeout
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

import jsonschema

from bike_agent.config import Layout
from bike_agent.multi_agent.contracts import (
    EVALUATE_INPUT_SCHEMA,
    EVALUATE_OUTPUT_SCHEMA,
    EVALUATION_VERSION,
)
from bike_agent.multi_agent.messages import MessageStore
from bike_agent.multi_agent.task import (
    InputChangedError,
    InputManifest,
    TaskSpec,
    verify_unchanged,
)
from bike_agent.sim.pipeline import run_simulation as _run_simulation
from bike_agent.tools.handlers import ToolError, call_tool


class PermissionDenied(RuntimeError):
    """The role may not call this tool, or may not call it with these params."""

class ToolLimitReached(RuntimeError):
    """A run limit (tool calls or task deadline) was reached."""

class ToolTimeout(RuntimeError):
    """One tool invocation exceeded the per-tool timeout."""

class ToolExecutionError(RuntimeError):
    """The backend raised; the attempt is recorded before this propagates."""

#: F24 role table.  A role with an empty set has no business tool at all.
TOOL_PERMISSIONS: dict[str, frozenset[str]] = {
    "coordinator": frozenset(),
    "diagnosis": frozenset({"get_forecast", "get_risk_ranking"}),
    "dispatch": frozenset(),
    "evaluation": frozenset({"evaluate_dispatch_candidate"}),
}

#: Tools whose result can be cached; only the expensive simulation qualifies.
CACHEABLE = frozenset({"evaluate_dispatch_candidate"})

def _now() -> str:
    return datetime.now(timezone.utc).isoformat()

def _sha(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()

@dataclass
class ToolCallResult:
    tool_call_id: str
    tool: str
    output: dict[str, Any]
    cache_hit: bool = False
    attempts: int = 1
    elapsed_ms: float = 0.0

    def as_dict(self) -> dict[str, Any]:
        return {
            "tool_call_id": self.tool_call_id,
            "tool": self.tool,
            "output": copy.deepcopy(self.output),
            "cache_hit": self.cache_hit,
            "attempts": self.attempts,
            "elapsed_ms": self.elapsed_ms,
        }

def evaluate_dispatch_candidate(
    config: dict[str, Any],
    layout: Layout,
    task: TaskSpec,
    manifest: InputManifest,
    *,
    strategy: str,
    budget: int,
) -> dict[str, Any]:
    """The versioned evaluation contract: a thin wrapper over the real engine.

    It deliberately reuses ``run_simulation(..., write=False)`` rather than
    reimplementing dispatch.  The four metrics and the trajectory are copied
    from the same return the F7 ``run_simulation`` handler exposes, and the five
    violation counters come from ``result["violations"]`` unchanged -- never
    defaulted to zero here.
    """
    payload = {
        "task_id": task.task_id,
        "source": task.source,
        "strategy": strategy,
        "budget": int(budget),
        "frozen_context_id": manifest.digest,
    }
    jsonschema.validate(payload, EVALUATE_INPUT_SCHEMA)
    result = _run_simulation(
        config, layout, task.source, strategy, budget=int(budget), write=False
    )
    summary = result["summary"]
    steps = result["steps"]
    trajectory = [
        {
            "time_bucket": str(row.time_bucket),
            "borrow_requests": int(row.borrow_requests),
            "unmet_requests": int(row.unmet_requests),
            "inventory_end": int(row.inventory_end),
        }
        for row in steps.itertuples(index=False)
    ]
    violations = {
        key: int(result["violations"].get(key, 0))
        for key in (
            "conservation",
            "capacity",
            "budget",
            "negative_inventory",
            "clamped_moves",
        )
    }
    result_id = "res-" + _sha(
        f"{manifest.digest}|{task.source}|{strategy}|{int(budget)}"
    )[:16]
    output = {
        "tool": "evaluate_dispatch_candidate",
        "schema_version": EVALUATION_VERSION,
        "result_id": result_id,
        "task_id": task.task_id,
        "source": task.source,
        "strategy": strategy,
        "budget": int(budget),
        "frozen_context_id": manifest.digest,
        "simulated": True,
        "metrics": {
            "unmet_requests": int(summary["unmet_requests"]),
            "distance_bike_km": float(summary["distance_bike_km"]),
            "composite_cost": float(summary["composite_cost"]),
            "service_rate": float(summary["service_rate"]),
        },
        "trajectory": trajectory,
        "violations": violations,
        "assumptions": {
            "initial_fill_ratio": float(summary["assumptions"]["initial_fill_ratio"]),
            "safety_stock_ratio": float(summary["assumptions"]["safety_stock_ratio"]),
            "max_moves_per_bucket": int(summary["assumptions"]["max_moves_per_bucket"]),
            "unmet_penalty": float(summary["assumptions"]["unmet_penalty"]),
            "distance_cost_per_bike_km": float(
                summary["assumptions"]["distance_cost_per_bike_km"]
            ),
            "max_candidate_surplus_per_deficit": int(
                summary["assumptions"]["max_candidate_surplus_per_deficit"]
            ),
        },
        "input_manifest": {
            "digest": manifest.digest,
            "files": [dict(entry) for entry in manifest.files],
        },
    }
    jsonschema.validate(output, EVALUATE_OUTPUT_SCHEMA)
    return output

class MultiAgentGateway:
    """Role-checked, cached, audited access to the business tools."""

    def __init__(
        self,
        config: dict[str, Any],
        layout: Layout,
        task: TaskSpec,
        manifest: InputManifest,
        store: MessageStore | None = None,
        *,
        tool_timeout_s: float | None = None,
        max_tool_calls: int | None = None,
        deadline: float | None = None,
        clock: Callable[[], float] = time.monotonic,
        evaluator: Callable[..., dict[str, Any]] = evaluate_dispatch_candidate,
        old_tool: Callable[..., dict[str, Any]] = call_tool,
    ):
        self.config = config
        self.layout = layout
        self.task = task
        self.manifest = manifest
        self.store = store
        self.tool_timeout_s = float(tool_timeout_s if tool_timeout_s is not None else task.tool_timeout_s)
        self.max_tool_calls = int(max_tool_calls if max_tool_calls is not None else task.max_tool_calls)
        self.deadline = deadline
        self._clock = clock
        self._evaluator = evaluator
        self._old_tool = old_tool
        self._cache: dict[tuple[str, str, str, int], dict[str, Any]] = {}
        self._call_counter = 0
        self.tool_calls: list[dict[str, Any]] = []
        self.events: list[dict[str, Any]] = []
        self.denials: list[dict[str, Any]] = []
        self.attempt_count = 0
        self.backend_executions = 0
        self.cache_hits = 0

    # -- limits ------------------------------------------------------------
    def _check_deadline(self, role: str, tool: str) -> None:
        if self.deadline is not None and self._clock() > self.deadline:
            self.events.append(
                {"kind": "task_timeout", "role": role, "tool": tool, "at": _now()}
            )
            raise ToolLimitReached("task deadline reached")

    def _check_limits(self, role: str, tool: str) -> None:
        self._check_deadline(role, tool)
        if self.attempt_count >= self.max_tool_calls:
            self.events.append(
                {"kind": "tool_call_limit", "role": role, "tool": tool, "at": _now()}
            )
            raise ToolLimitReached(
                f"tool call limit reached ({self.max_tool_calls})"
            )

    # -- public API --------------------------------------------------------
    def call(self, role: str, tool: str, params: dict[str, Any] | None = None) -> ToolCallResult:
        params = dict(params or {})
        allowed = TOOL_PERMISSIONS.get(role, frozenset())
        if tool not in allowed:
            denial = {
                "role": role,
                "tool": tool,
                "reason": "role is not permitted to call this tool",
                "at": _now(),
            }
            self.denials.append(denial)
            self.events.append({"kind": "permission_denied", **denial})
            raise PermissionDenied(
                f"role {role!r} may not call {tool!r}; allowed: {sorted(allowed)}"
            )
        self._validate_params(role, tool, params)
        self._check_limits(role, tool)
        # Even a cache hit must be refused when the frozen inputs moved: the
        # cached value belongs to the old context, and reusing it would mix two
        # datasets without a new execution to reveal it.
        try:
            verify_unchanged(self.config, self.layout, self.task.source, self.manifest)
        except InputChangedError:
            self.events.append(
                {"kind": "input_changed", "role": role, "tool": tool, "at": _now()}
            )
            raise

        cache_key = self._cache_key(tool, params)
        if cache_key is not None and cache_key in self._cache:
            self._call_counter += 1
            call_id = f"tc-{self._call_counter:04d}"
            self._register(call_id, tool)
            cached = copy.deepcopy(self._cache[cache_key])
            self.cache_hits += 1
            self.attempt_count += 1
            record = {
                "tool_call_id": call_id,
                "role": role,
                "tool": tool,
                "input": copy.deepcopy(params),
                "output": cached,
                "status": "ok",
                "cache_hit": True,
                "attempts": 0,
                "elapsed_ms": 0.0,
                "input_digest": self.manifest.digest,
                "at": _now(),
            }
            self.tool_calls.append(record)
            self.events.append(
                {"kind": "cache_hit", "tool_call_id": call_id, "tool": tool, "at": _now()}
            )
            return ToolCallResult(
                tool_call_id=call_id, tool=tool, output=cached, cache_hit=True, attempts=0
            )

        self._call_counter += 1
        call_id = f"tc-{self._call_counter:04d}"
        self._register(call_id, tool)
        started = self._clock()
        last_error: Exception | None = None
        attempts = 0
        while attempts < 2:
            attempts += 1
            self.attempt_count += 1
            self._check_deadline(role, tool)
            if self.attempt_count > self.max_tool_calls:
                self.events.append(
                    {"kind": "tool_call_limit", "role": role, "tool": tool, "at": _now()}
                )
                raise ToolLimitReached(
                    f"tool call limit reached ({self.max_tool_calls})"
                )
            try:
                verify_unchanged(self.config, self.layout, self.task.source, self.manifest)
                output = self._execute(role, tool, params)
            except InputChangedError:
                self._record(call_id, role, tool, params, None, "input_changed", attempts, started, None)
                raise
            except (PermissionDenied, ToolLimitReached):
                raise
            except Exception as exc:
                last_error = exc
                self.events.append(
                    {
                        "kind": "tool_failure",
                        "tool_call_id": call_id,
                        "tool": tool,
                        "attempt": attempts,
                        "error": f"{type(exc).__name__}: {exc}",
                        "at": _now(),
                    }
                )
                if attempts < 2 and self._retryable(exc):
                    self.events.append(
                        {"kind": "retry", "tool_call_id": call_id, "attempt": attempts + 1, "at": _now()}
                    )
                    continue
                self._record(
                    call_id, role, tool, params, None, "error", attempts, started,
                    f"{type(exc).__name__}: {exc}",
                )
                if isinstance(exc, ToolTimeout):
                    raise
                raise ToolExecutionError(f"{tool} failed: {exc}") from exc
            try:
                verify_unchanged(self.config, self.layout, self.task.source, self.manifest)
            except InputChangedError:
                self._record(
                    call_id, role, tool, params, None, "input_changed", attempts, started, None
                )
                raise
            self.backend_executions += 1
            elapsed_ms = (self._clock() - started) * 1000.0
            self._record(call_id, role, tool, params, output, "ok", attempts, started, None)
            if cache_key is not None:
                self._cache[cache_key] = copy.deepcopy(output)
            return ToolCallResult(
                tool_call_id=call_id,
                tool=tool,
                output=output,
                cache_hit=False,
                attempts=attempts,
                elapsed_ms=elapsed_ms,
            )
        raise ToolExecutionError(f"{tool} failed: {last_error}")

    def evaluate(self, *, strategy: str, budget: int) -> ToolCallResult:
        """Convenience for the Evaluation role; still permission-checked."""
        return self.call(
            "evaluation",
            "evaluate_dispatch_candidate",
            {"source": self.task.source, "strategy": strategy, "budget": int(budget)},
        )

    # -- internals ---------------------------------------------------------
    def _register(self, call_id: str, tool: str) -> None:
        if self.store is not None:
            self.store.register_tool_call(call_id, source=self.task.source, tool=tool)

    def _cache_key(self, tool: str, params: dict[str, Any]) -> tuple[str, str, str, int] | None:
        if tool not in CACHEABLE:
            return None
        return (
            self.manifest.digest,
            str(params.get("source")),
            str(params.get("strategy")),
            int(params.get("budget", 0)),
        )

    def _retryable(self, exc: Exception) -> bool:
        # A timeout is retried at most once like any other backend failure;
        # permission/validation errors never reach here.
        return not isinstance(exc, (jsonschema.ValidationError, ToolError, ValueError))

    def _validate_params(self, role: str, tool: str, params: dict[str, Any]) -> None:
        def deny(reason: str) -> None:
            denial = {"role": role, "tool": tool, "params": copy.deepcopy(params), "reason": reason, "at": _now()}
            self.denials.append(denial)
            self.events.append({"kind": "permission_denied", **denial})
            raise PermissionDenied(reason)

        if tool == "evaluate_dispatch_candidate":
            source = params.get("source")
            if source != self.task.source:
                deny(f"source {source!r} does not match the task source {self.task.source!r}")
            strategy = params.get("strategy")
            if strategy not in self.task.allowed_strategies:
                deny(f"strategy {strategy!r} is not allowed by the task")
            budget = params.get("budget")
            if not isinstance(budget, int) or isinstance(budget, bool):
                deny("budget must be an integer")
            if budget not in self.task.budget_options:
                deny(f"budget {budget!r} is not one of {list(self.task.budget_options)}")
            if budget > self.task.max_budget:
                deny(f"budget {budget} exceeds max_budget {self.task.max_budget}")
            return
        # Old tools: force the task source rather than letting the default
        # (primary_source) silently select another one.
        source = params.get("source")
        if source is None:
            params["source"] = self.task.source
        elif source != self.task.source:
            deny(f"source {source!r} does not match the task source {self.task.source!r}")

    def _execute(self, role: str, tool: str, params: dict[str, Any]) -> dict[str, Any]:
        if tool == "evaluate_dispatch_candidate":
            function = lambda: self._evaluator(
                self.config,
                self.layout,
                self.task,
                self.manifest,
                strategy=str(params["strategy"]),
                budget=int(params["budget"]),
            )
        else:
            payload = {key: value for key, value in params.items() if value is not None}
            function = lambda: self._old_tool(tool, payload, self.config, self.layout)
        # Include submission/scheduling time: the caller may be descheduled
        # until after a slow future has already completed.
        execution_started = time.monotonic()
        executor = ThreadPoolExecutor(max_workers=1)
        future = executor.submit(function)
        try:
            remaining = max(0.0, self.tool_timeout_s - (time.monotonic() - execution_started))
            try:
                result = future.result(timeout=remaining)
            except Exception as exc:
                if isinstance(exc, FutureTimeout) or time.monotonic() - execution_started >= self.tool_timeout_s:
                    raise ToolTimeout(
                        f"{tool} exceeded the {self.tool_timeout_s:g}s per-tool timeout"
                    ) from exc
                raise
            if time.monotonic() - execution_started >= self.tool_timeout_s:
                raise ToolTimeout(f"{tool} exceeded the {self.tool_timeout_s:g}s per-tool timeout")
            return result
        finally:
            executor.shutdown(wait=False, cancel_futures=True)

    def _record(
        self,
        call_id: str,
        role: str,
        tool: str,
        params: dict[str, Any],
        output: dict[str, Any] | None,
        status: str,
        attempts: int,
        started: float,
        error: str | None,
    ) -> None:
        record = {
            "tool_call_id": call_id,
            "role": role,
            "tool": tool,
            "input": copy.deepcopy(params),
            "output": copy.deepcopy(output) if output is not None else None,
            "status": status,
            "cache_hit": False,
            "attempts": attempts,
            "elapsed_ms": (self._clock() - started) * 1000.0,
            "input_digest": self.manifest.digest,
            "error": error,
            "at": _now(),
        }
        self.tool_calls.append(record)
