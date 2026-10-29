"""Dispatch strategies: rule-based (control) vs constrained optimisation.

Both planners receive exactly the same inputs (inventory, capacity, predicted
net flow, budget, distance matrix).  Only the allocation algorithm differs,
which is the fairness requirement of F5-3.
"""

from __future__ import annotations

from typing import Any

import numpy as np

try:
    import networkx as nx
except ImportError as exc:  # pragma: no cover
    raise ImportError("networkx is required for the optimised dispatch strategy") from exc


def plan_rule(
    deficits: np.ndarray,
    surplus: np.ndarray,
    room: np.ndarray,
    distances: np.ndarray,
    budget: int,
) -> list[tuple[int, int, int, float]]:
    """Greedy nearest-surplus allocation, ordered by deficit size.

    Returns a list of ``(source_index, target_index, amount, distance_m)``.
    """
    moves: list[tuple[int, int, int, float]] = []
    remaining_surplus = surplus.astype(int).copy()
    remaining_budget = int(budget)
    deficit_order = np.argsort(-deficits, kind="mergesort")
    for target in deficit_order:
        need = int(min(deficits[target], room[target]))
        if need <= 0:
            continue
        for source in np.argsort(distances[target], kind="mergesort"):
            if need <= 0 or remaining_budget <= 0:
                break
            if source == target:
                continue
            available = int(remaining_surplus[source])
            if available <= 0:
                continue
            amount = int(min(need, available, remaining_budget))
            if amount <= 0:
                continue
            moves.append((int(source), int(target), amount, float(distances[target, source])))
            remaining_surplus[source] -= amount
            remaining_budget -= amount
            need -= amount
    return moves


def plan_optimized(
    deficits: np.ndarray,
    surplus: np.ndarray,
    room: np.ndarray,
    distances: np.ndarray,
    budget: int,
    max_candidates: int = 20,
) -> list[tuple[int, int, int, float]]:
    """Min-cost flow: maximise bikes delivered, then minimise travel distance.

    Each delivered bike earns a reward ``M`` larger than any single trip
    distance, so the solver first serves as much demand as the budget allows
    and uses distance only to break ties.
    """
    deficits = np.asarray(deficits, dtype=int)
    surplus = np.asarray(surplus, dtype=int)
    room = np.asarray(room, dtype=int)
    demand = np.minimum(deficits, room)
    demand = np.where(demand > 0, demand, 0)
    if demand.sum() <= 0 or surplus.sum() <= 0 or budget <= 0:
        return []

    sources = np.flatnonzero(surplus > 0)
    targets = np.flatnonzero(demand > 0)
    if len(sources) == 0 or len(targets) == 0:
        return []

    # Candidate arcs: the nearest `max_candidates` surplus cells per deficit.
    candidate_pairs: list[tuple[int, int, int]] = []
    max_distance = 0.0
    for target in targets:
        order = np.argsort(distances[target, sources], kind="mergesort")[:max_candidates]
        for position in order:
            source = int(sources[position])
            distance = float(distances[target, source])
            if distance <= 0:
                continue
            candidate_pairs.append((source, int(target), int(round(distance))))
            max_distance = max(max_distance, distance)
    if not candidate_pairs:
        return []

    reward = int(max_distance) + 1
    max_demand = float(demand[targets].max()) if len(targets) else 0.0
    graph = nx.DiGraph()
    graph.add_node("S", demand=-int(budget))
    graph.add_node("T", demand=int(budget))
    for source in sources:
        graph.add_edge("S", int(source), capacity=int(surplus[source]), weight=0)
    for target in targets:
        # A unit delivered to a larger predicted deficit is worth more; the
        # distance term only decides which source serves it.
        extra = int(round((float(demand[target]) / max_demand) * max_distance)) if max_demand > 0 else 0
        graph.add_edge(int(target), "T", capacity=int(demand[target]), weight=-(reward + extra))
    for source, target, distance in candidate_pairs:
        graph.add_edge(source, target, capacity=int(min(surplus[source], demand[target])), weight=distance)
    graph.add_edge("S", "T", capacity=int(budget), weight=0)

    try:
        flow = nx.min_cost_flow(graph)
    except nx.NetworkXUnfeasible:  # pragma: no cover - defensive
        return []

    moves: list[tuple[int, int, int, float]] = []
    for source, target, _distance in candidate_pairs:
        amount = int(flow.get(source, {}).get(target, 0))
        if amount > 0:
            moves.append((source, target, amount, float(distances[target, source])))
    return moves


PLANNERS: dict[str, Any] = {"rule": plan_rule, "optimized": plan_optimized}
