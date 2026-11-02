"""F26: the evaluation contract, real-engine equivalence, cache and boundaries."""

from __future__ import annotations

import ast
from pathlib import Path

import pandas as pd
import pytest
from multi_agent_helpers import (
    ASSUMPTIONS,
    FakeEvaluator,
    FakeOldTool,
    default_table,
    manifest,
    output,
    spec,
    task,
)

from bike_agent.config import Layout, load_config
from bike_agent.multi_agent.contracts import EVALUATE_OUTPUT_SCHEMA
from bike_agent.multi_agent.gateway import (
    MultiAgentGateway,
    evaluate_dispatch_candidate,
)
from bike_agent.multi_agent.roles import EvaluationContext, EvaluationRole
from bike_agent.multi_agent.task import (
    InputChangedError,
    freeze_inputs,
    verify_unchanged,
)
from bike_agent.tools.handlers import run_simulation as old_run_simulation
from bike_agent.tools.schemas import TOOL_SCHEMA

ROOT = Path(__file__).resolve().parents[1]

# -- old contract stays untouched ------------------------------------------
def test_old_schema_still_has_exactly_three_tools():
    tools = TOOL_SCHEMA["properties"]["tools"]
    assert set(tools["required"]) == {"get_forecast", "get_risk_ranking", "run_simulation"}
    assert tools["additionalProperties"] is False
    assert set(tools["properties"]) == {"get_forecast", "get_risk_ranking", "run_simulation"}
    # The new contract is a separate document with its own $id.
    assert "evaluate_dispatch_candidate" not in tools["properties"]
    assert EVALUATE_OUTPUT_SCHEMA["$id"].endswith("evaluate_dispatch_candidate.v1.output.json")

def test_new_tool_is_a_thin_wrapper(monkeypatch):
    """The wrapper must call run_simulation(..., write=False), not re-simulate."""
    calls = []

    def fake_run(config, layout, source, strategy, budget=None, write=True):
        calls.append({"source": source, "strategy": strategy, "budget": budget, "write": write})
        return _fake_engine_result()

    monkeypatch.setattr("bike_agent.multi_agent.gateway._run_simulation", fake_run)
    task_value = task()
    result = evaluate_dispatch_candidate(
        load_config(), Layout(), task_value, manifest(), strategy="optimized", budget=200
    )
    assert calls == [{"source": "xm", "strategy": "optimized", "budget": 200, "write": False}]
    assert result["tool"] == "evaluate_dispatch_candidate"
    assert result["simulated"] is True

def _fake_engine_result(violations: dict[str, int] | None = None) -> dict:
    steps = pd.DataFrame(
        [
            {
                "time_bucket": pd.Timestamp("2020-12-25 08:00:00"),
                "borrow_requests": 10,
                "unmet_requests": 2,
                "inventory_end": 5,
            }
        ]
    )
    summary = {
        "unmet_requests": 2,
        "distance_bike_km": 1.5,
        "composite_cost": 2.3,
        "service_rate": 0.8,
        "assumptions": dict(ASSUMPTIONS),
    }
    return {
        "summary": summary,
        "steps": steps,
        "moves": pd.DataFrame(),
        "violations": dict(
            violations
            or {
                "conservation": 0,
                "capacity": 0,
                "budget": 0,
                "negative_inventory": 0,
                "clamped_moves": 0,
            }
        ),
    }

def test_violations_pass_through_the_wrapper(monkeypatch):
    """A fault fixture: nonzero counters must survive the wrapper unchanged."""
    violations = {
        "conservation": 2,
        "capacity": 3,
        "budget": 1,
        "negative_inventory": 4,
        "clamped_moves": 5,
    }
    monkeypatch.setattr(
        "bike_agent.multi_agent.gateway._run_simulation",
        lambda *args, **kwargs: _fake_engine_result(violations),
    )
    result = evaluate_dispatch_candidate(
        load_config(), Layout(), task(), manifest(), strategy="optimized", budget=200
    )
    assert result["violations"] == violations

def test_nonzero_violations_make_the_evaluation_reject():
    """The evaluation role consumes the counters; it does not trust the name."""
    baseline = output(task(), manifest(), "rule", 200, spec(cost=100.0, unmet=10, rate=0.9, distance=50.0))
    broken = spec(
        cost=90.0,
        unmet=8,
        rate=0.92,
        distance=60.0,
        violations={"conservation": 1, "capacity": 0, "budget": 0, "negative_inventory": 0, "clamped_moves": 0},
    )

    class BrokenEvaluator(FakeEvaluator):
        def __call__(self, config, layout, task_spec, manifest_value, *, strategy, budget):
            from bike_agent.multi_agent.gateway import ToolCallResult

            return ToolCallResult(
                tool_call_id="tc-0002",
                tool="evaluate_dispatch_candidate",
                output=output(task_spec, manifest_value, strategy, budget, broken),
            )

    class FakeGateway:
        def evaluate(self, *, strategy, budget):
            return BrokenEvaluator(default_table()).__call__(
                {}, None, task(), manifest(), strategy=strategy, budget=budget
            )

    payload = EvaluationRole().handle(
        EvaluationContext(
            task=task(),
            round=1,
            candidates=(
                {
                    "candidate_id": "cand-r1-1",
                    "strategy": "optimized",
                    "budget": 200,
                },
            ),
            baseline=baseline,
            baseline_tool_call_id="tc-0001",
        ),
        FakeGateway(),
    )
    item = payload["evaluations"][0]
    assert item["verdict"] == "reject"
    assert item["violations_ok"] is False
    assert any("conservation" in defect for defect in item["defects"])

# -- real-engine equivalence ------------------------------------------------
def test_new_tool_matches_old_run_simulation_on_real_data():
    config = load_config()
    layout = Layout()
    mismatches = []
    for source in ("xm", "sh"):
        task_value = task(source=source, max_budget=400)
        manifest_value = freeze_inputs(config, layout, source)
        for strategy in ("rule", "optimized"):
            for budget in task_value.budget_options:
                new = evaluate_dispatch_candidate(
                    config, layout, task_value, manifest_value,
                    strategy=strategy, budget=budget,
                )
                old = old_run_simulation(
                    config, layout, source=source, strategy=strategy, budget=budget
                )
                if new["metrics"] != old["metrics"]:
                    mismatches.append(f"{source}/{strategy}/{budget}: metrics {new['metrics']} != {old['metrics']}")
                if new["trajectory"] != old["trajectory"]:
                    mismatches.append(f"{source}/{strategy}/{budget}: trajectory differs")
    assert not mismatches, "\n".join(mismatches)

# -- numeric traceability ---------------------------------------------------
def _baseline() -> dict:
    return output(task(), manifest(), "rule", 200, spec(cost=100.0, unmet=10, rate=0.9, distance=50.0))

def _evaluation_for(candidate_result: dict, *, min_service_rate=None) -> dict:
    class Gateway:
        def evaluate(self, *, strategy, budget):
            from bike_agent.multi_agent.gateway import ToolCallResult

            return ToolCallResult(
                tool_call_id="tc-0002",
                tool="evaluate_dispatch_candidate",
                output=output(task(min_service_rate=min_service_rate), manifest(), strategy, budget, candidate_result),
            )

    return EvaluationRole().handle(
        EvaluationContext(
            task=task(min_service_rate=min_service_rate),
            round=1,
            candidates=(
                {"candidate_id": "cand-r1-1", "strategy": "optimized", "budget": 200},
            ),
            baseline=_baseline(),
            baseline_tool_call_id="tc-0001",
        ),
        Gateway(),
    )["evaluations"][0]

def test_deltas_use_the_documented_sign_convention():
    better = _evaluation_for(spec(cost=90.0, unmet=8, rate=0.92, distance=60.0))
    assert better["deltas"]["cost_delta"] == pytest.approx(90.0 - 100.0)
    assert better["deltas"]["unmet_gain"] == 10 - 8
    assert better["deltas"]["service_rate_delta_pp"] == pytest.approx((0.92 - 0.9) * 100.0)

    worse = _evaluation_for(spec(cost=110.0, unmet=12, rate=0.88, distance=70.0))
    assert worse["deltas"]["cost_delta"] == pytest.approx(10.0)
    assert worse["deltas"]["unmet_gain"] == -2
    assert worse["deltas"]["service_rate_delta_pp"] == pytest.approx(-2.0)

def test_service_rate_constraint_rejects_and_suggests_a_revision():
    evaluation = _evaluation_for(
        spec(cost=90.0, unmet=8, rate=0.92, distance=60.0), min_service_rate=0.95
    )
    assert evaluation["verdict"] == "reject"
    assert evaluation["service_rate_ok"] is False
    assert evaluation["suggested_revision"]["action"] in {"raise_budget", "change_strategy"}

def test_evaluation_carries_the_result_id_and_operands():
    evaluation = _evaluation_for(spec(cost=90.0, unmet=8, rate=0.92, distance=60.0))
    assert evaluation["result_id"] == "res-optimized-200"
    assert evaluation["tool_call_id"] == "tc-0002"
    assert evaluation["metrics"]["composite_cost"] == pytest.approx(90.0)
    assert evaluation["metrics"]["unmet_requests"] == 8

def test_the_formula_and_its_operands_are_persisted_with_the_result():
    """F26: a reader can recompute every delta from the audit alone."""
    evaluation = _evaluation_for(spec(cost=90.0, unmet=8, rate=0.92, distance=60.0))
    formula = evaluation["formula"]
    assert "candidate" in formula["cost_delta"] and "baseline" in formula["cost_delta"]
    assert "baseline" in formula["unmet_gain"] and "candidate" in formula["unmet_gain"]
    assert "* 100" in formula["service_rate_delta_pp"]
    operands = evaluation["operands"]
    assert operands["baseline"]["result_id"] == "res-rule-200"
    assert operands["candidate"]["result_id"] == evaluation["result_id"]
    base, cand = operands["baseline"]["metrics"], operands["candidate"]["metrics"]
    assert evaluation["deltas"]["cost_delta"] == pytest.approx(
        cand["composite_cost"] - base["composite_cost"]
    )
    assert evaluation["deltas"]["unmet_gain"] == base["unmet_requests"] - cand["unmet_requests"]
    assert evaluation["deltas"]["service_rate_delta_pp"] == pytest.approx(
        (cand["service_rate"] - base["service_rate"]) * 100.0
    )

# -- input freezing ---------------------------------------------------------
def _freeze_layout(tmp_path: Path) -> tuple[Layout, dict]:
    layout = Layout(tmp_path)
    layout.ensure()
    config = load_config()
    config["sources"]["xm"]["cell_size_m"] = 1000
    files = {
        layout.panel_path("xm", 1000): b"panel",
        layout.forecast_path("xm"): b"forecast",
        layout.clean_path("xm"): b"clean",
        layout.cells_path("xm", 1000): b"cells",
        layout.priority_replenish_path("xm"): b"replenish",
        layout.priority_clear_path("xm"): b"clear",
    }
    for path, payload in files.items():
        path.write_bytes(payload)
    return layout, config

def test_freeze_and_detect_a_changed_input(tmp_path):
    layout, config = _freeze_layout(tmp_path)
    manifest_value = freeze_inputs(config, layout, "xm")
    assert len(manifest_value.files) == 6
    verify_unchanged(config, layout, "xm", manifest_value)
    layout.forecast_path("xm").write_bytes(b"changed")
    with pytest.raises(InputChangedError) as error:
        verify_unchanged(config, layout, "xm", manifest_value)
    assert "forecast" in str(error.value)

def test_missing_input_is_reported(tmp_path):
    layout, config = _freeze_layout(tmp_path)
    layout.forecast_path("xm").unlink()
    with pytest.raises(FileNotFoundError):
        freeze_inputs(config, layout, "xm")

# -- cache ------------------------------------------------------------------
def test_cache_hit_is_audited_and_does_not_reexecute(monkeypatch):
    monkeypatch.setattr("bike_agent.multi_agent.gateway.verify_unchanged", lambda *args, **kwargs: None)
    evaluator = FakeEvaluator(default_table())
    gateway = MultiAgentGateway(
        {}, None, task(), manifest(), None, evaluator=evaluator, old_tool=FakeOldTool()
    )
    first = gateway.call(
        "evaluation", "evaluate_dispatch_candidate",
        {"source": "xm", "strategy": "optimized", "budget": 200},
    )
    second = gateway.call(
        "evaluation", "evaluate_dispatch_candidate",
        {"source": "xm", "strategy": "optimized", "budget": 200},
    )
    assert first.cache_hit is False
    assert second.cache_hit is True
    assert evaluator.calls == [("optimized", 200)]
    assert gateway.backend_executions == 1
    assert gateway.cache_hits == 1
    assert any(event["kind"] == "cache_hit" for event in gateway.events)

def test_backend_failure_is_retried_once(monkeypatch):
    monkeypatch.setattr("bike_agent.multi_agent.gateway.verify_unchanged", lambda *args, **kwargs: None)
    evaluator = FakeEvaluator(default_table())
    evaluator.fail_once.add(("optimized", 200))
    gateway = MultiAgentGateway(
        {}, None, task(), manifest(), None, evaluator=evaluator, old_tool=FakeOldTool()
    )
    result = gateway.call(
        "evaluation", "evaluate_dispatch_candidate",
        {"source": "xm", "strategy": "optimized", "budget": 200},
    )
    assert result.attempts == 2
    assert evaluator.calls == [("optimized", 200), ("optimized", 200)]
    assert any(event["kind"] == "retry" for event in gateway.events)

# -- boundaries -------------------------------------------------------------
FORBIDDEN_IN_ROLES = {"pandas", "pathlib", "bike_agent.config", "bike_agent.tools", "bike_agent.sim", "bike_agent.replay"}

def test_roles_do_not_import_business_data_modules():
    path = ROOT / "bike_agent" / "multi_agent" / "roles.py"
    tree = ast.parse(path.read_text(encoding="utf-8"))
    offenders = []
    for node in ast.walk(tree):
        modules = []
        if isinstance(node, ast.ImportFrom) and node.module:
            modules.append(node.module)
        elif isinstance(node, ast.Import):
            modules.extend(alias.name for alias in node.names)
        for module in modules:
            if any(module == item or module.startswith(item + ".") for item in FORBIDDEN_IN_ROLES):
                offenders.append(f"{node.lineno}: {module}")
    assert not offenders, offenders

def test_roles_do_not_open_files():
    source = (ROOT / "bike_agent" / "multi_agent" / "roles.py").read_text(encoding="utf-8")
    for forbidden in ("read_parquet", "read_csv", "open(", "Path("):
        assert forbidden not in source, forbidden
