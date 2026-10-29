"""F5: conservation, capacity and budget invariants for both strategies."""

from __future__ import annotations

import numpy as np
import pandas as pd
import pytest

from bike_agent.sim.capacity import cell_capacities, initial_inventory
from bike_agent.sim.engine import simulate
from bike_agent.sim.strategies import plan_optimized, plan_rule


def _scenario(in_count_b: int = 8) -> tuple[pd.DataFrame, pd.DataFrame, pd.DataFrame, pd.Series]:
    bucket = pd.Timestamp("2020-12-21 06:00")
    cells = pd.DataFrame(
        {
            "cell_id": ["A", "B"],
            "ix": [0, 1],
            "iy": [0, 0],
            "lon": [118.100, 118.102],
            "lat": [24.500, 24.500],
        }
    )
    panel = pd.DataFrame(
        [
            {"cell_id": "A", "time_bucket": bucket, "out_count": 8, "in_count": 0, "net_flow": 8},
            {"cell_id": "B", "time_bucket": bucket, "out_count": 0, "in_count": in_count_b, "net_flow": -in_count_b},
        ]
    )
    forecast = pd.DataFrame(
        [
            {"cell_id": "A", "time_bucket": bucket, "pred_out": 8.0, "pred_in": 0.0},
            {"cell_id": "B", "time_bucket": bucket, "pred_out": 0.0, "pred_in": 8.0},
        ]
    )
    capacities = pd.Series({"A": 10, "B": 10})
    return panel, forecast, cells, capacities


def _config(tiny_config: dict, budget: int = 5) -> dict:
    config = dict(tiny_config)
    config["simulation"] = dict(tiny_config["simulation"])
    config["simulation"]["max_moves_per_bucket"] = budget
    return config


def test_simulation_conserves_bikes_and_respects_capacity_and_budget(tiny_config):
    panel, forecast, cells, capacities = _scenario()
    config = _config(tiny_config)
    result = simulate(panel, forecast, cells, capacities, config, "rule")

    assert result.violations == {
        "conservation": 0,
        "capacity": 0,
        "budget": 0,
        "negative_inventory": 0,
        "clamped_moves": 0,
    }
    step = result.steps.iloc[0]
    assert step["inventory_end"] == step["inventory_start"] + step["returns_applied"] - step["borrow_met"]
    assert step["bikes_moved"] <= config["simulation"]["max_moves_per_bucket"]
    assert step["inventory_end"] <= int(capacities.sum())
    assert step["unmet_requests"] == 0


def test_returns_overflow_is_tracked_not_lost(tiny_config):
    panel, forecast, cells, capacities = _scenario(in_count_b=20)
    config = _config(tiny_config)
    result = simulate(panel, forecast, cells, capacities, config, "rule")
    step = result.steps.iloc[0]

    assert step["returns_overflow"] == 12  # 20 returned, only 8 free slots
    assert step["inventory_end"] == 10  # A emptied, B exactly full
    assert result.violations["capacity"] == 0


def test_budget_binds_and_unmet_requests_are_reported(tiny_config):
    panel, forecast, cells, capacities = _scenario()
    config = _config(tiny_config, budget=2)
    result = simulate(panel, forecast, cells, capacities, config, "rule")
    step = result.steps.iloc[0]
    assert step["bikes_moved"] == 2
    assert step["unmet_requests"] == 1  # A had 5 + 2 = 7 bikes for 8 requests
    assert result.violations["budget"] == 0


def test_both_strategies_share_inputs_and_are_feasible(tiny_config):
    panel, forecast, cells, capacities = _scenario()
    config = _config(tiny_config)
    rule = simulate(panel, forecast, cells, capacities, config, "rule")
    optimized = simulate(panel, forecast, cells, capacities, config, "optimized")

    assert rule.summary["initial_inventory"] == optimized.summary["initial_inventory"]
    assert rule.summary["borrow_requests"] == optimized.summary["borrow_requests"]
    assert optimized.summary["bikes_moved"] >= 0
    assert optimized.violations["conservation"] == 0


def test_optimized_planner_never_moves_more_than_budget():
    deficits = np.array([10, 5, 0, 0])
    surplus = np.array([0, 0, 8, 20])
    room = np.array([100, 100, 100, 100])
    distance = np.array([[0, 1, 2, 3], [1, 0, 2, 3], [2, 2, 0, 1], [3, 3, 1, 0]], dtype=float)
    rule = plan_rule(deficits, surplus, room, distance, 7)
    optimized = plan_optimized(deficits, surplus, room, distance, 7)
    assert sum(move[2] for move in rule) == 7
    assert sum(move[2] for move in optimized) == 7
    assert all(move[2] > 0 for move in optimized)


def test_capacity_modes():
    cells = pd.DataFrame({"cell_id": ["A", "B"], "ix": [0, 1], "iy": [0, 0], "lon": [118.1, 118.2], "lat": [24.5, 24.5]})
    fixed = cell_capacities(cells, {"mode": "fixed", "fixed_capacity": 40})
    assert fixed.tolist() == [40, 40]

    from bike_agent.prep.grid import build_grid

    trips = pd.DataFrame(
        [
            {
                "trip_id": "t1", "bike_id": "b1",
                "start_time": pd.Timestamp("2020-12-21 06:00"), "end_time": pd.Timestamp("2020-12-21 06:10"),
                "start_lat": 24.5, "start_lon": 118.1, "end_lat": 24.501, "end_lon": 118.101, "duration_s": 600.0,
            }
        ]
    )
    grid = build_grid(trips, 500)
    grid_cells = grid.cells.loc[:, ["cell_id", "ix", "iy", "lon", "lat"]]
    fences = pd.DataFrame({"centroid_lat": [24.5], "centroid_lon": [118.1]})
    fenced = cell_capacities(
        grid_cells,
        {"mode": "fences", "slots_per_fence": 2, "min_capacity": 1, "max_capacity": 100},
        fences=fences,
        grid=grid,
    )
    assert fenced.max() >= 2
    assert (fenced >= 1).all()
    assert initial_inventory(pd.Series([10, 20]), 0.5).tolist() == [5, 10]
