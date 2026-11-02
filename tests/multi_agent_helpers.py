"""Shared fakes for the Phase 6 unit tests.

The loop tests must exercise every branch without spending a real simulation on
each one.  The fakes below keep the *contracts* (the gateway still validates
permissions, the message store still validates messages, the coordinator still
re-reads the hard gates) and replace only the expensive backend and the frozen
files.  Real-engine equivalence is covered separately in
``tests/test_multi_agent_tools.py``.
"""

from __future__ import annotations

from typing import Any

from bike_agent.config import Layout, load_config
from bike_agent.multi_agent.task import InputManifest, TaskSpec, build_task

ZERO_VIOLATIONS = {
    "conservation": 0,
    "capacity": 0,
    "budget": 0,
    "negative_inventory": 0,
    "clamped_moves": 0,
}

ASSUMPTIONS = {
    "initial_fill_ratio": 0.5,
    "safety_stock_ratio": 0.2,
    "max_moves_per_bucket": 200,
    "unmet_penalty": 1.0,
    "distance_cost_per_bike_km": 0.2,
    "max_candidate_surplus_per_deficit": 20,
}

def manifest() -> InputManifest:
    return InputManifest(digest="fake-digest", config_digest="fake-config", files=())

def task(**overrides: Any) -> TaskSpec:
    config = load_config()
    return build_task(config, overrides.pop("source", "xm"), **overrides)

def spec(
    *,
    cost: float,
    unmet: int,
    rate: float,
    distance: float,
    violations: dict[str, int] | None = None,
    bucket: str = "2020-12-25 08:00:00",
    borrow: int = 100,
    inventory_end: int = 50,
) -> dict[str, Any]:
    return {
        "cost": float(cost),
        "unmet": int(unmet),
        "rate": float(rate),
        "distance": float(distance),
        "violations": dict(violations or ZERO_VIOLATIONS),
        "bucket": bucket,
        "borrow": int(borrow),
        "inventory_end": int(inventory_end),
    }

def output(
    task_spec: TaskSpec,
    manifest_value: InputManifest,
    strategy: str,
    budget: int,
    result: dict[str, Any],
) -> dict[str, Any]:
    return {
        "tool": "evaluate_dispatch_candidate",
        "schema_version": "1.0",
        "result_id": f"res-{strategy}-{budget}",
        "task_id": task_spec.task_id,
        "source": task_spec.source,
        "strategy": strategy,
        "budget": int(budget),
        "frozen_context_id": manifest_value.digest,
        "simulated": True,
        "metrics": {
            "unmet_requests": int(result["unmet"]),
            "distance_bike_km": float(result["distance"]),
            "composite_cost": float(result["cost"]),
            "service_rate": float(result["rate"]),
        },
        "trajectory": [
            {
                "time_bucket": str(result["bucket"]),
                "borrow_requests": int(result["borrow"]),
                "unmet_requests": int(result["unmet"]),
                "inventory_end": int(result["inventory_end"]),
            }
        ],
        "violations": {key: int(value) for key, value in result["violations"].items()},
        "assumptions": dict(ASSUMPTIONS),
        "input_manifest": {"digest": manifest_value.digest, "files": []},
    }

class FakeEvaluator:
    """A table-driven backend: (strategy, budget) -> result spec."""

    def __init__(self, table: dict[tuple[str, int], dict[str, Any]]):
        self.table = dict(table)
        self.calls: list[tuple[str, int]] = []
        self.fail_once: set[tuple[str, int]] = set()

    def __call__(
        self,
        config: dict[str, Any],
        layout: Layout,
        task_spec: TaskSpec,
        manifest_value: InputManifest,
        *,
        strategy: str,
        budget: int,
    ) -> dict[str, Any]:
        key = (strategy, int(budget))
        self.calls.append(key)
        if key in self.fail_once:
            self.fail_once.remove(key)
            raise RuntimeError("synthetic backend failure")
        if key not in self.table:
            raise KeyError(f"no fake result for {key}")
        return output(task_spec, manifest_value, strategy, int(budget), self.table[key])

class FakeOldTool:
    """The two read-only diagnosis tools, with a configurable ranking bucket."""

    def __init__(self, *, ranking_bucket: str | None = None, cells: tuple[str, ...] = ("C0001_0001", "C0002_0002")):
        self.calls: list[tuple[str, dict[str, Any]]] = []
        self.ranking_bucket = ranking_bucket
        self.cells = cells

    def __call__(
        self, tool: str, payload: dict[str, Any], config: dict[str, Any], layout: Layout
    ) -> dict[str, Any]:
        self.calls.append((tool, dict(payload)))
        bucket = payload.get("time_bucket") or "2020-12-25 08:00:00"
        if tool == "get_forecast":
            return {
                "source": payload.get("source", "xm"),
                "time_bucket": str(bucket),
                "forecasts": [
                    {
                        "cell_id": cell,
                        "time_bucket": str(bucket),
                        "pred_out": float(10 - index),
                        "pred_in": float(index),
                        "pred_net_flow": float(10 - 2 * index),
                    }
                    for index, cell in enumerate(self.cells)
                ],
                "confidence": {
                    "level": "point-estimate-with-test-error",
                    "basis": "synthetic fixture",
                    "mae_out": 0.5,
                    "mae_in": 0.4,
                },
            }
        if tool == "get_risk_ranking":
            return {
                "source": payload.get("source", "xm"),
                "direction": payload.get("direction", "replenish"),
                "time_bucket": str(self.ranking_bucket if self.ranking_bucket is not None else bucket),
                "ranking": [
                    {
                        "rank": index + 1,
                        "cell_id": cell,
                        "score": float(10 - index),
                        "pred_net_flow": float(10 - index),
                        "pred_out": 10.0,
                        "pred_in": 1.0,
                        "explanation": "synthetic fixture",
                    }
                    for index, cell in enumerate(self.cells)
                ],
            }
        raise AssertionError(f"unexpected old tool {tool!r}")

def default_table() -> dict[tuple[str, int], dict[str, Any]]:
    """A baseline and a strictly better candidate at the same budget."""
    return {
        ("rule", 200): spec(cost=100.0, unmet=10, rate=0.90, distance=50.0),
        ("optimized", 200): spec(cost=90.0, unmet=8, rate=0.92, distance=60.0),
        ("optimized", 400): spec(cost=80.0, unmet=6, rate=0.96, distance=70.0),
        ("rule", 400): spec(cost=85.0, unmet=7, rate=0.95, distance=65.0),
        ("rule", 100): spec(cost=110.0, unmet=12, rate=0.85, distance=40.0),
        ("optimized", 100): spec(cost=105.0, unmet=11, rate=0.86, distance=45.0),
    }
