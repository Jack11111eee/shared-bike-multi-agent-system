"""F7: schema validity, tool contracts and zero LLM dependency."""

from __future__ import annotations

import json
import re
from pathlib import Path

import pandas as pd
import pytest
from jsonschema import Draft202012Validator, ValidationError

from bike_agent.tools.handlers import (
    ToolError,
    call_tool,
    get_forecast,
    get_risk_ranking,
    run_simulation,
    validate_input,
    validate_output,
)
from bike_agent.tools.schemas import TOOL_SCHEMA


def _write_tool_artifacts(layout, config) -> None:
    source = "xm"
    cell_size = int(config["sources"][source]["cell_size_m"])
    bucket = pd.Timestamp("2020-12-21 06:00")

    forecast = pd.DataFrame(
        [
            {"cell_id": "A", "time_bucket": bucket, "pred_out": 8.0, "pred_in": 2.0, "pred_net_flow": 6.0},
            {"cell_id": "B", "time_bucket": bucket, "pred_out": 2.0, "pred_in": 8.0, "pred_net_flow": -6.0},
        ]
    )
    forecast.to_parquet(layout.forecast_path(source), index=False)
    (layout.outputs / f"forecast_meta.{source}.json").write_text(
        json.dumps({"metrics": {"model": {"out_count": {"mae": 1.5}, "in_count": {"mae": 1.2}}}}),
        encoding="utf-8",
    )

    priority = pd.DataFrame(
        [
            {
                "rank": 1,
                "cell_id": "A",
                "score": 0.9,
                "pred_net_flow": 6.0,
                "pred_out": 8.0,
                "pred_in": 2.0,
                "explanation": "net_outflow=+6.00 (pred_out=8.00, pred_in=2.00)",
                "time_bucket": str(bucket),
            }
        ]
    )
    priority.to_csv(layout.priority_replenish_path(source), index=False)
    priority.assign(cell_id="B", pred_net_flow=-6.0, pred_out=2.0, pred_in=8.0).to_csv(
        layout.priority_clear_path(source), index=False
    )
    (layout.outputs / f"risk_summary.{source}.json").write_text(
        json.dumps({"time_bucket": str(bucket)}), encoding="utf-8"
    )

    panel = pd.DataFrame(
        [
            {"cell_id": "A", "time_bucket": bucket, "out_count": 8, "in_count": 2, "net_flow": 6},
            {"cell_id": "B", "time_bucket": bucket, "out_count": 2, "in_count": 8, "net_flow": -6},
        ]
    )
    panel.to_parquet(layout.panel_path(source, cell_size), index=False)
    cells = pd.DataFrame(
        {
            "cell_id": ["A", "B"],
            "ix": [0, 1],
            "iy": [0, 0],
            "lon": [118.100, 118.102],
            "lat": [24.500, 24.500],
        }
    )
    cells.to_parquet(layout.cells_path(source, cell_size), index=False)
    clean = pd.DataFrame(
        [
            {
                "trip_id": "t1", "bike_id": "b1",
                "start_time": pd.Timestamp("2020-12-21 06:00"), "end_time": pd.Timestamp("2020-12-21 06:10"),
                "start_lat": 24.5, "start_lon": 118.1, "end_lat": 24.501, "end_lon": 118.101, "duration_s": 600.0,
            }
        ]
    )
    clean.to_parquet(layout.clean_path(source), index=False)


def test_schema_is_valid_draft_2020_12():
    Draft202012Validator.check_schema(TOOL_SCHEMA)
    assert TOOL_SCHEMA["$schema"].endswith("2020-12/schema")
    assert set(TOOL_SCHEMA["$defs"]) >= {"get_forecast", "get_risk_ranking", "run_simulation"}


def test_input_validation_rejects_bad_arguments():
    with pytest.raises(ValidationError):
        validate_input("get_risk_ranking", {"direction": "sideways"})
    with pytest.raises(ValidationError):
        validate_input("run_simulation", {"strategy": "magic"})
    with pytest.raises(ValidationError):
        validate_input("get_forecast", {"unexpected": 1})


def test_get_forecast_contract(tiny_config, tiny_layout):
    _write_tool_artifacts(tiny_layout, tiny_config)
    payload = get_forecast(tiny_config, tiny_layout, source="xm")
    validate_output("get_forecast", payload)
    assert len(payload["forecasts"]) == 2
    assert payload["confidence"]["mae_out"] == 1.5
    filtered = get_forecast(tiny_config, tiny_layout, source="xm", cell_ids=["A"])
    assert [row["cell_id"] for row in filtered["forecasts"]] == ["A"]


def test_get_risk_ranking_contract(tiny_config, tiny_layout):
    _write_tool_artifacts(tiny_layout, tiny_config)
    payload = get_risk_ranking(tiny_config, tiny_layout, direction="replenish", top_k=5, source="xm")
    validate_output("get_risk_ranking", payload)
    assert payload["direction"] == "replenish"
    assert payload["ranking"][0]["pred_net_flow"] > 0
    assert payload["ranking"][0]["explanation"]
    clear = get_risk_ranking(tiny_config, tiny_layout, direction="clear", top_k=5, source="xm")
    assert clear["ranking"][0]["pred_net_flow"] < 0


def test_run_simulation_contract(tiny_config, tiny_layout):
    _write_tool_artifacts(tiny_layout, tiny_config)
    config = dict(tiny_config)
    config["sources"] = {**tiny_config["sources"]}
    config["sources"]["xm"] = {**tiny_config["sources"]["xm"]}
    config["sources"]["xm"]["capacity"] = {"mode": "fixed", "fixed_capacity": 10}
    payload = run_simulation(config, tiny_layout, strategy="rule", budget=5, source="xm")
    validate_output("run_simulation", payload)
    assert payload["simulated"] is True
    assert payload["metrics"]["unmet_requests"] >= 0
    assert len(payload["trajectory"]) == 1


def test_call_tool_dispatches_and_validates(tiny_config, tiny_layout):
    _write_tool_artifacts(tiny_layout, tiny_config)
    payload = call_tool("get_risk_ranking", {"direction": "clear", "top_k": 1}, tiny_config, tiny_layout)
    assert payload["ranking"][0]["cell_id"] == "B"
    with pytest.raises(ToolError):
        call_tool("does_not_exist", {}, tiny_config, tiny_layout)


def test_zero_llm_dependency():
    forbidden = re.compile(
        r"^\s*(?:import|from)\s+(openai|anthropic|langchain|llama_index|litellm|transformers|ollama)\b",
        re.MULTILINE,
    )
    package_root = Path(__file__).resolve().parents[1] / "bike_agent"
    offenders = []
    for path in package_root.rglob("*.py"):
        if forbidden.search(path.read_text(encoding="utf-8")):
            offenders.append(str(path))
    assert offenders == [], f"LLM SDK imports found: {offenders}"
