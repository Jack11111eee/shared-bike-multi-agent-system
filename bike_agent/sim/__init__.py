"""Dispatch simulation: replay, rule strategy, optimised strategy, metrics."""

from bike_agent.sim.capacity import cell_capacities, initial_inventory
from bike_agent.sim.engine import SimResult, distance_matrix, simulate
from bike_agent.sim.pipeline import run_comparison, run_simulation
from bike_agent.sim.strategies import plan_optimized, plan_rule

__all__ = [
    "SimResult",
    "cell_capacities",
    "distance_matrix",
    "initial_inventory",
    "plan_optimized",
    "plan_rule",
    "run_comparison",
    "run_simulation",
    "simulate",
]
