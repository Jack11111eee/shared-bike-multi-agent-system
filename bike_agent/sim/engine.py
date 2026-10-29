"""Replay simulation engine (F5).

Order of events inside one time bucket:

1. dispatch (using the forecast for that bucket) -- only the allocation
   algorithm differs between strategies;
2. returns (capped by free capacity; overflow is tracked, never silently lost);
3. borrow requests (met from inventory; the rest is an *unmet request*).

The engine records a violation counter for every invariant so the tests can
assert conservation, capacity and budget compliance.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

import numpy as np
import pandas as pd

from bike_agent.sim.strategies import PLANNERS
from bike_agent.utils import haversine_m


@dataclass
class SimResult:
    summary: dict[str, Any]
    steps: pd.DataFrame
    moves: pd.DataFrame
    violations: dict[str, int] = field(default_factory=dict)


def distance_matrix(cells: pd.DataFrame) -> np.ndarray:
    lat = cells["lat"].to_numpy(dtype=float)
    lon = cells["lon"].to_numpy(dtype=float)
    return haversine_m(lat[:, None], lon[:, None], lat[None, :], lon[None, :])


def _pivot(panel: pd.DataFrame, value: str, buckets: pd.DatetimeIndex, cell_ids: list[str]) -> np.ndarray:
    wide = (
        panel.pivot(index="time_bucket", columns="cell_id", values=value)
        .reindex(index=buckets, columns=cell_ids)
        .fillna(0.0)
    )
    return wide.to_numpy(dtype=float)


def simulate(
    panel: pd.DataFrame,
    forecast: pd.DataFrame,
    cells: pd.DataFrame,
    capacities: pd.Series,
    config: dict[str, Any],
    strategy: str,
    budget: int | None = None,
) -> SimResult:
    if strategy not in PLANNERS:
        raise ValueError(f"unknown strategy {strategy!r}; expected one of {sorted(PLANNERS)}")
    sim_cfg = config.get("simulation", {})
    planner = PLANNERS[strategy]

    cells = cells.sort_values("cell_id", kind="mergesort").reset_index(drop=True)
    cell_ids = cells["cell_id"].tolist()
    capacity = capacities.reindex(cell_ids).to_numpy(dtype=int)
    safety_ratio = float(sim_cfg.get("safety_stock_ratio", 0.2))
    safety = np.round(capacity * safety_ratio).astype(int)
    inventory = np.round(capacity * float(sim_cfg.get("initial_fill_ratio", 0.5))).astype(int)
    initial_inventory = inventory.copy()

    bucket_values = pd.DatetimeIndex(sorted(set(panel["time_bucket"]) & set(forecast["time_bucket"])))
    if len(bucket_values) == 0:
        raise ValueError("simulation needs at least one shared time bucket between panel and forecast")

    requested = _pivot(panel, "out_count", bucket_values, cell_ids)
    returned = _pivot(panel, "in_count", bucket_values, cell_ids)
    pred_out = _pivot(forecast, "pred_out", bucket_values, cell_ids)
    pred_in = _pivot(forecast, "pred_in", bucket_values, cell_ids)

    distance = distance_matrix(cells)
    per_bucket_budget = int(budget if budget is not None else sim_cfg.get("max_moves_per_bucket", 25))
    max_candidates = int(sim_cfg.get("max_candidate_surplus_per_deficit", 20))

    steps: list[dict[str, Any]] = []
    move_rows: list[dict[str, Any]] = []
    violations = {
        "conservation": 0,
        "capacity": 0,
        "budget": 0,
        "negative_inventory": 0,
        "clamped_moves": 0,
    }

    for index, bucket in enumerate(bucket_values):
        inventory_before = inventory.copy()
        predicted_net = np.round(pred_out[index] - pred_in[index]).astype(int)
        deficits = np.clip(predicted_net, 0, None)
        # A cell expecting net outflow is a demander, never a supplier in the
        # same bucket; otherwise the optimiser could "serve" a deficit with
        # bikes from the same cell (a zero-distance self-move).
        surplus = np.where(
            predicted_net <= 0,
            np.clip(inventory - safety, 0, None),
            0,
        )
        room = np.clip(capacity - inventory, 0, None)

        if strategy == "optimized":
            moves = planner(deficits, surplus, room, distance, per_bucket_budget, max_candidates=max_candidates)
        else:
            moves = planner(deficits, surplus, room, distance, per_bucket_budget)

        moved = 0
        distance_m = 0.0
        for source, target, amount, dist in moves:
            allowed = int(min(amount, inventory[source], capacity[target] - inventory[target]))
            if allowed != amount:
                violations["clamped_moves"] += 1
            if allowed <= 0:
                continue
            inventory[source] -= allowed
            inventory[target] += allowed
            moved += allowed
            distance_m += allowed * dist
            move_rows.append(
                {
                    "time_bucket": bucket,
                    "from_cell": cell_ids[source],
                    "to_cell": cell_ids[target],
                    "bikes": allowed,
                    "distance_m": float(dist),
                    "distance_bike_km": allowed * dist / 1000.0,
                }
            )
        if moved > per_bucket_budget:
            violations["budget"] += 1

        room_after_moves = capacity - inventory
        applied_returns = np.minimum(returned[index].astype(int), np.clip(room_after_moves, 0, None))
        overflow = returned[index].astype(int) - applied_returns
        inventory += applied_returns

        requests = requested[index].astype(int)
        met = np.minimum(requests, inventory)
        unmet = requests - met
        inventory -= met

        if (inventory < 0).any():
            violations["negative_inventory"] += 1
        if (inventory > capacity).any():
            violations["capacity"] += 1

        expected_total = int(inventory_before.sum() + applied_returns.sum() - met.sum())
        if int(inventory.sum()) != expected_total:
            violations["conservation"] += 1

        steps.append(
            {
                "time_bucket": bucket,
                "borrow_requests": int(requests.sum()),
                "borrow_met": int(met.sum()),
                "unmet_requests": int(unmet.sum()),
                "returns": int(returned[index].sum()),
                "returns_applied": int(applied_returns.sum()),
                "returns_overflow": int(overflow.sum()),
                "bikes_moved": int(moved),
                "distance_bike_km": distance_m / 1000.0,
                "inventory_start": int(inventory_before.sum()),
                "inventory_end": int(inventory.sum()),
            }
        )

    steps_frame = pd.DataFrame(steps)
    moves_frame = pd.DataFrame(move_rows)
    total_requests = int(steps_frame["borrow_requests"].sum())
    total_met = int(steps_frame["borrow_met"].sum())
    total_unmet = int(steps_frame["unmet_requests"].sum())
    total_distance = float(steps_frame["distance_bike_km"].sum())
    unmet_penalty = float(sim_cfg.get("unmet_penalty", 1.0))
    distance_cost = float(sim_cfg.get("distance_cost_per_bike_km", 0.2))

    summary = {
        "strategy": strategy,
        "n_buckets": int(len(bucket_values)),
        "first_bucket": str(bucket_values.min()),
        "last_bucket": str(bucket_values.max()),
        "borrow_requests": total_requests,
        "borrow_met": total_met,
        "unmet_requests": total_unmet,
        "service_rate": (total_met / total_requests) if total_requests else 1.0,
        "bikes_moved": int(steps_frame["bikes_moved"].sum()),
        "distance_bike_km": total_distance,
        "composite_cost": total_unmet * unmet_penalty + total_distance * distance_cost,
        "returns": int(steps_frame["returns"].sum()),
        "returns_overflow": int(steps_frame["returns_overflow"].sum()),
        "initial_inventory": int(initial_inventory.sum()),
        "final_inventory": int(steps_frame["inventory_end"].iloc[-1]),
        "capacity_total": int(capacity.sum()),
        "budget_per_bucket": per_bucket_budget,
        "assumptions": {
            "initial_fill_ratio": float(sim_cfg.get("initial_fill_ratio", 0.5)),
            "safety_stock_ratio": safety_ratio,
            "max_moves_per_bucket": per_bucket_budget,
            "unmet_penalty": unmet_penalty,
            "distance_cost_per_bike_km": distance_cost,
            "max_candidate_surplus_per_deficit": max_candidates,
        },
        "violations": violations,
        "simulated": True,
    }
    return SimResult(summary=summary, steps=steps_frame, moves=moves_frame, violations=violations)
