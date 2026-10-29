"""Thin tool handlers over the F1-F5 modules (F7-2 / F7-4).

Handlers only load persisted artefacts, filter, and shape the response.  All
computation lives in the data/forecast/risk/sim modules and is tested there.
"""

from __future__ import annotations

from typing import Any

import jsonschema
import pandas as pd

from bike_agent.config import Layout
from bike_agent.sim.pipeline import run_simulation as _run_simulation
from bike_agent.tools.schemas import TOOL_SCHEMA
from bike_agent.utils import read_json


class ToolError(RuntimeError):
    """Raised when a tool input is invalid or its artefacts are missing."""


def _resolve_source(config: dict[str, Any], source: str | None) -> str:
    if source is None:
        return str(config.get("primary_source", "xm"))
    if source not in config.get("sources", {}):
        raise ToolError(f"unknown source {source!r}")
    return source


def _require(path) -> None:
    if not path.exists():
        raise ToolError(f"missing artefact {path}; run `bike-agent repro` first")


def get_forecast(
    config: dict[str, Any],
    layout: Layout,
    source: str | None = None,
    cell_ids: list[str] | None = None,
    time_bucket: str | None = None,
) -> dict[str, Any]:
    source = _resolve_source(config, source)
    path = layout.forecast_path(source)
    _require(path)
    frame = pd.read_parquet(path)
    if time_bucket is None:
        time_bucket = str(frame["time_bucket"].max())
    selected = frame.loc[frame["time_bucket"] == pd.Timestamp(time_bucket)]
    if cell_ids:
        selected = selected.loc[selected["cell_id"].isin(cell_ids)]
    if selected.empty:
        raise ToolError(f"no forecast rows for time_bucket={time_bucket!r}")

    meta_path = layout.forecast_meta_path(source)
    meta = read_json(meta_path) if meta_path.exists() else {}
    metrics = meta.get("metrics", {}).get("model", {})

    forecasts = [
        {
            "cell_id": str(row.cell_id),
            "time_bucket": str(row.time_bucket),
            "pred_out": float(row.pred_out),
            "pred_in": float(row.pred_in),
            "pred_net_flow": float(row.pred_net_flow),
        }
        for row in selected.sort_values("cell_id").itertuples(index=False)
    ]
    return {
        "source": source,
        "time_bucket": str(time_bucket),
        "forecasts": forecasts,
        "confidence": {
            "level": "point-estimate-with-test-error",
            "basis": "model MAE on the time-ordered test split (not a calibrated interval)",
            "mae_out": float(metrics.get("out_count", {}).get("mae", 0.0)),
            "mae_in": float(metrics.get("in_count", {}).get("mae", 0.0)),
        },
    }


def get_risk_ranking(
    config: dict[str, Any],
    layout: Layout,
    direction: str,
    top_k: int = 10,
    source: str | None = None,
) -> dict[str, Any]:
    source = _resolve_source(config, source)
    if direction not in ("replenish", "clear"):
        raise ToolError("direction must be 'replenish' or 'clear'")
    if top_k < 1:
        raise ToolError("top_k must be >= 1")
    path = layout.priority_replenish_path(source) if direction == "replenish" else layout.priority_clear_path(source)
    _require(path)
    frame = pd.read_csv(path)
    selected = frame.head(top_k)
    ranking = [
        {
            "rank": int(row.rank),
            "cell_id": str(row.cell_id),
            "score": float(row.score),
            "pred_net_flow": float(row.pred_net_flow),
            "pred_out": float(row.pred_out),
            "pred_in": float(row.pred_in),
            "explanation": str(row.explanation),
        }
        for row in selected.itertuples(index=False)
    ]
    summary_path = layout.outputs / f"risk_summary.{source}.json"
    summary = read_json(summary_path) if summary_path.exists() else {}
    return {
        "source": source,
        "direction": direction,
        "time_bucket": str(summary.get("time_bucket", "")),
        "ranking": ranking,
    }


def run_simulation(
    config: dict[str, Any],
    layout: Layout,
    strategy: str,
    budget: int | None = None,
    source: str | None = None,
) -> dict[str, Any]:
    source = _resolve_source(config, source)
    if strategy not in ("rule", "optimized"):
        raise ToolError("strategy must be 'rule' or 'optimized'")
    result = _run_simulation(config, layout, source, strategy, budget=budget, write=False)
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
    return {
        "source": source,
        "strategy": strategy,
        "simulated": True,
        "metrics": {
            "unmet_requests": int(summary["unmet_requests"]),
            "distance_bike_km": float(summary["distance_bike_km"]),
            "composite_cost": float(summary["composite_cost"]),
            "service_rate": float(summary["service_rate"]),
        },
        "trajectory": trajectory,
    }


TOOL_HANDLERS = {
    "get_forecast": get_forecast,
    "get_risk_ranking": get_risk_ranking,
    "run_simulation": run_simulation,
}


def _standalone(subschema: dict[str, Any]) -> dict[str, Any]:
    """Attach the shared ``$defs`` so ``#/$defs/...`` refs resolve in isolation."""
    return {
        "$schema": TOOL_SCHEMA["$schema"],
        "$defs": TOOL_SCHEMA["$defs"],
        **subschema,
    }


def validate_output(tool: str, payload: dict[str, Any]) -> None:
    schema = _standalone(TOOL_SCHEMA["$defs"][tool]["properties"]["output"])
    jsonschema.validate(payload, schema)


def validate_input(tool: str, payload: dict[str, Any]) -> None:
    schema = _standalone(TOOL_SCHEMA["$defs"][tool]["properties"]["input"])
    jsonschema.validate(payload, schema)


def call_tool(name: str, payload: dict[str, Any], config: dict[str, Any], layout: Layout) -> dict[str, Any]:
    if name not in TOOL_HANDLERS:
        raise ToolError(f"unknown tool {name!r}")
    validate_input(name, payload)
    handler = TOOL_HANDLERS[name]
    result = handler(config, layout, **payload)
    validate_output(name, result)
    return result
