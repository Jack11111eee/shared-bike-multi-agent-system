"""Collect every artefact the report renderers need."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pandas as pd

from bike_agent.config import Layout, source_config
from bike_agent.utils import read_json


def _optional_json(path: Path) -> Any:
    return read_json(path) if path.exists() else None


def _sim_steps(layout: Layout, source: str) -> dict[str, dict[str, int]]:
    """Phase 2 replay sizes, so F10's page numbers resolve through key_numbers."""
    sizes: dict[str, dict[str, int]] = {}
    for strategy in ("rule", "optimized"):
        steps = layout.sim_path(source, strategy, "csv")
        replay = layout.replay_path(source, strategy)
        moves = layout.outputs / f"sim_moves.{strategy}.{source}.json"
        if not (steps.exists() and replay.exists() and moves.exists()):
            continue
        sizes[strategy] = {
            "n_buckets": int(len(pd.read_csv(steps))),
            "n_cells": int(len(pd.read_parquet(replay)["cell_id"].unique())),
            "n_moves": int(len(read_json(moves))),
            "bytes": int(replay.stat().st_size),
        }
    return sizes


def collect_source(layout: Layout, config: dict[str, Any], source: str) -> dict[str, Any]:
    src_cfg = source_config(config, source)
    cell_size = int(src_cfg["cell_size_m"])
    return {
        "source": source,
        "cell_size_m": cell_size,
        "quality": _optional_json(layout.processed / f"quality.{source}.json"),
        "prep": _optional_json(layout.processed / f"prep_meta.{source}.{cell_size}m.json"),
        "forecast_metrics": _optional_json(layout.outputs / f"forecast_metrics.{source}.json"),
        "forecast_meta": _optional_json(layout.outputs / f"forecast_meta.{source}.json"),
        "sensitivity": _optional_json(layout.outputs / f"sensitivity_baseline.{source}.json"),
        "risk": _optional_json(layout.outputs / f"risk_summary.{source}.json"),
        "comparison": _optional_json(layout.outputs / f"strategy_comparison.{source}.json"),
        "sim_sensitivity": _optional_json(layout.outputs / f"sim_sensitivity.{source}.json"),
        "sim_steps": _sim_steps(layout, source),
        "sim_summary": {
            strategy: _optional_json(layout.sim_path(source, strategy, "json"))
            for strategy in ("rule", "optimized")
        },
    }


def risk_rank_stability(layout: Layout, source: str) -> dict[str, float] | None:
    """Top-10 overlap between the chosen weights and equal weights (D7)."""
    path = layout.outputs / f"priority_replenish.{source}.csv"
    if not path.exists():
        return None
    frame = pd.read_csv(path)
    if frame.empty:
        return None
    equal = frame["mag_norm"] + frame["demand_norm"] + frame["vol_norm"]
    chosen_top = set(frame.nlargest(min(10, len(frame)), "score")["cell_id"])
    equal_top = set(frame.loc[equal.nlargest(min(10, len(frame))).index, "cell_id"])
    union = chosen_top | equal_top
    return {
        "top10_overlap": len(chosen_top & equal_top) / len(union) if union else 0.0,
        "n_candidates": float(len(frame)),
    }


def build_key_numbers(data: dict[str, Any]) -> list[dict[str, Any]]:
    numbers: list[dict[str, Any]] = []
    for source, d in data.items():
        prefix = source.upper()
        quality = d.get("quality") or {}
        prep = (d.get("prep") or {}).get("panel") or {}
        metrics = d.get("forecast_metrics") or {}
        risk = d.get("risk") or {}
        comparison = d.get("comparison") or {}

        def add(metric: str, value: Any, path: str) -> None:
            numbers.append({"metric": f"{prefix} · {metric}", "value": value, "source_file": path})

        if quality:
            add("trip rows", quality["rows"], f"outputs/summary.{source}.json")
            add("duplicate rate", quality["duplicate_rate"], f"outputs/summary.{source}.json")
            add("abnormal duration share", quality["abnormal_duration_rate"], f"outputs/summary.{source}.json")
        if prep:
            add("grid cells", prep["n_cells"], f"outputs/summary.{source}.json")
            add("time buckets", prep["n_buckets"], f"outputs/summary.{source}.json")
            add("zero-departure share", prep["zero_out_share"], f"outputs/summary.{source}.json")
            add("total departures", prep["total_out"], f"outputs/summary.{source}.json")
        if metrics:
            for target in ("out_count", "in_count"):
                add(f"{target} baseline MAE", metrics["baseline"][target]["mae"], f"outputs/forecast_metrics.{source}.json")
                add(f"{target} model MAE", metrics["model"][target]["mae"], f"outputs/forecast_metrics.{source}.json")
        if risk:
            add("replenish candidates", risk["n_replenish"], f"outputs/risk_summary.{source}.json")
            add("clear candidates", risk["n_clear"], f"outputs/risk_summary.{source}.json")
        if comparison:
            add("rule unmet requests", comparison["rule"]["unmet_requests"], f"outputs/strategy_comparison.{source}.json")
            add("optimized unmet requests", comparison["optimized"]["unmet_requests"], f"outputs/strategy_comparison.{source}.json")
            add("rule distance bike-km", comparison["rule"]["distance_bike_km"], f"outputs/strategy_comparison.{source}.json")
            add("optimized distance bike-km", comparison["optimized"]["distance_bike_km"], f"outputs/strategy_comparison.{source}.json")
            add("rule composite cost", comparison["rule"]["composite_cost"], f"outputs/strategy_comparison.{source}.json")
            add("optimized composite cost", comparison["optimized"]["composite_cost"], f"outputs/strategy_comparison.{source}.json")
        # Phase 2 (F10-5): the replay page traces its numbers to these three
        # artefacts, so they must be resolvable through key_numbers.json too.
        for strategy in ("rule", "optimized"):
            steps_path = d.get("sim_steps", {}).get(strategy)
            if steps_path is None:
                continue
            add(f"{strategy} replay frames", d["sim_steps"][strategy]["n_buckets"], f"outputs/sim_{strategy}.{source}.csv")
            add(f"{strategy} replay cells", d["sim_steps"][strategy]["n_cells"], f"outputs/replay.{strategy}.{source}.parquet")
            add(
                f"{strategy} replay moves",
                d["sim_steps"][strategy]["n_moves"],
                f"outputs/sim_moves.{strategy}.{source}.json",
            )
            summary = (d.get("sim_summary") or {}).get(strategy)
            if summary:
                add(f"{strategy} service rate", summary["service_rate"], f"outputs/sim_{strategy}.{source}.json")
    return numbers
