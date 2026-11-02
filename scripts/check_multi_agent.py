#!/usr/bin/env python3
"""REQUIREMENTS-PHASE6 §6.1: Phase 6 acceptance checks (F24–F29).

Runs the machine-checkable parts of the multi-agent contract against the
*shipped* audit artefacts and the *actual* simulation engine, then writes
``outputs/multi_agent_validation.phase6.json`` and exits non-zero on any
failure.

Two rules shape this file:

* **Nothing is verified by the code that produced it.**  Message validity is
  re-derived from the raw JSONL; numeric deltas are recomputed from the tool
  outputs; the page's node ids are matched against the audit files; and the new
  evaluation tool is compared with the old engine for every source, strategy
  and legal budget tier.
* **A missing artefact is a failure, not a skip.**  No real run, no page, no
  data: the corresponding checks fail and say which command to run.

Usage::

    python3 scripts/check_multi_agent.py [--json] [--verbose]
"""

from __future__ import annotations

import argparse
import ast
import hashlib
import json
import math
import re
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from jsonschema import Draft202012Validator

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from bike_agent.config import Layout, load_config
from bike_agent.multi_agent.contracts import (
    ALL_SCHEMAS,
    MESSAGE_SCHEMA,
    PAYLOAD_SCHEMAS,
    SUCCESS_STATES,
)
from bike_agent.multi_agent.gateway import (
    TOOL_PERMISSIONS,
    evaluate_dispatch_candidate,
)
from bike_agent.multi_agent.task import build_task, freeze_inputs
from bike_agent.presentation.collab import discover_runs, select_runs
from bike_agent.sim.pipeline import (
    run_simulation as engine_run_simulation,
)
from bike_agent.tools.schemas import TOOL_SCHEMA

REPORT = ROOT / "outputs" / "multi_agent_validation.phase6.json"
SITE = ROOT / "replay"
RUNS_ROOT = ROOT / "outputs" / "multi_agent"
SITE_CAP = 14 * 1024 * 1024
COLLAB_CAP = 1024 * 1024
AUDIT_CAP = 16 * 1024 * 1024
REL_TOL = 1e-9
ABS_TOL = 1e-9

def _close(left: float, right: float) -> bool:
    return math.isclose(float(left), float(right), rel_tol=REL_TOL, abs_tol=ABS_TOL)

def _sha(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()

def _read_json(path: Path, default: Any = None) -> Any:
    if not path.exists():
        return default
    return json.loads(path.read_text(encoding="utf-8"))

class Checks:
    def __init__(self) -> None:
        self.items: list[dict[str, Any]] = []

    def add(
        self,
        check_id: str,
        requirement: str,
        *,
        ok: bool,
        expected: Any,
        actual: Any,
        command: str = "python3 scripts/check_multi_agent.py",
        inputs: list[str] | None = None,
        note: str = "",
        evidence: list[str] | None = None,
    ) -> None:
        self.items.append(
            {
                "id": check_id,
                "requirement": requirement,
                "command": command,
                "inputs": inputs or [],
                "expected": expected,
                "actual": actual,
                "result": "pass" if ok else "fail",
                "note": note,
                # F29-6 / §8: a verdict alone is not evidence -- name the files
                # the check actually read so a reader can re-derive the result.
                "evidence": evidence if evidence is not None else (inputs or []),
            }
        )

    @property
    def failed(self) -> list[dict[str, Any]]:
        return [item for item in self.items if item["result"] != "pass"]

def check_schemas(checks: Checks) -> None:
    errors = []
    for name, schema in ALL_SCHEMAS.items():
        try:
            Draft202012Validator.check_schema(schema)
        except Exception as exc:  # noqa: BLE001 - report, do not crash
            errors.append(f"{name}: {exc}")
    checks.add(
        "P6-SCHEMA",
        "F24-1 / F26: task, message, payload and evaluation schemas are valid Draft 2020-12",
        ok=not errors,
        expected="every schema validates",
        actual=errors or f"{len(ALL_SCHEMAS)} schemas valid",
        inputs=["bike_agent/multi_agent/contracts.py", "bike_agent/tools/schemas.py"],
    )

def check_old_tools(checks: Checks) -> None:
    tools = TOOL_SCHEMA["properties"]["tools"]
    old_ok = (
        set(tools["required"]) == {"get_forecast", "get_risk_ranking", "run_simulation"}
        and tools["additionalProperties"] is False
        and set(tools["properties"]) == {"get_forecast", "get_risk_ranking", "run_simulation"}
        and "evaluate_dispatch_candidate" not in tools["properties"]
    )
    checks.add(
        "P6-OLD-TOOLS",
        "F26-1: the old three-tool contract is unchanged and has no fourth tool",
        ok=old_ok,
        expected="exactly get_forecast/get_risk_ranking/run_simulation, additionalProperties=false",
        actual=sorted(tools["properties"]),
        inputs=["bike_agent/tools/schemas.py"],
    )
    agent_files = [
        ROOT / "outputs" / f"agent_run.{source}.{suffix}"
        for source in ("xm", "sh")
        for suffix in ("json", "md")
    ]
    missing = [str(path.relative_to(ROOT)) for path in agent_files if not path.exists()]
    checks.add(
        "P6-OLD-AGENT",
        "F26-1 / NF-34: the old single-agent outputs still exist",
        ok=not missing,
        expected="agent_run.<source>.{json,md} for both sources",
        actual=missing or "present",
        inputs=[str(path.relative_to(ROOT)) for path in agent_files],
    )

def _message_errors(messages: list[dict[str, Any]], tool_calls: list[dict[str, Any]], task_id: str, run_id: str) -> list[str]:
    errors: list[str] = []
    ids: set[str] = set()
    order: dict[str, int] = {}
    for index, message in enumerate(messages):
        if message.get("run_id") != run_id:
            errors.append(f"{message.get('message_id')}: run_id mismatch")
        if message.get("task_id") != task_id:
            errors.append(f"{message.get('message_id')}: task_id mismatch")
        if message.get("message_id") in ids:
            errors.append(f"{message.get('message_id')}: duplicate id")
        ids.add(message.get("message_id", ""))
        order[message.get("message_id", "")] = index
        try:
            Draft202012Validator(MESSAGE_SCHEMA).validate(message)
        except Exception as exc:  # noqa: BLE001
            errors.append(f"{message.get('message_id')}: envelope {exc}")
        payload_schema = PAYLOAD_SCHEMAS.get(message.get("type", ""))
        if payload_schema is None:
            errors.append(f"{message.get('message_id')}: unknown type {message.get('type')!r}")
        else:
            try:
                Draft202012Validator(payload_schema).validate(message.get("payload", {}))
            except Exception as exc:  # noqa: BLE001
                errors.append(f"{message.get('message_id')}: payload {exc}")
        if index == 0 and message.get("parent_id") is not None:
            errors.append("first message has a parent")
        if index > 0 and message.get("parent_id") is None:
            errors.append(f"{message.get('message_id')}: only the first message may have no parent")
    tool_ids = {call.get("tool_call_id") for call in tool_calls}
    for message in messages:
        parent = message.get("parent_id")
        if parent is not None:
            if parent not in order:
                errors.append(f"{message.get('message_id')}: dangling parent {parent!r}")
            elif order[parent] >= order[message.get("message_id")]:
                errors.append(f"{message.get('message_id')}: parent does not predate")
        for reference in message.get("evidence_refs") or []:
            if reference not in order and reference not in tool_ids:
                errors.append(f"{message.get('message_id')}: dangling evidence {reference!r}")
            elif reference in order and order[reference] >= order[message.get("message_id")]:
                errors.append(f"{message.get('message_id')}: evidence does not predate {reference!r}")
    return errors

def _tool_call_errors(
    tool_calls: list[dict[str, Any]], task: dict[str, Any], decision: dict[str, Any]
) -> list[str]:
    errors: list[str] = []
    ids: set[str] = set()
    for call in tool_calls:
        call_id = call.get("tool_call_id")
        if call_id in ids:
            errors.append(f"{call_id}: duplicate tool call id")
        ids.add(call_id)
        role = call.get("role")
        tool = call.get("tool")
        if tool not in TOOL_PERMISSIONS.get(role, frozenset()):
            errors.append(f"{call_id}: role {role!r} may not call {tool!r}")
        source = (call.get("input") or {}).get("source")
        if source != task.get("source"):
            errors.append(f"{call_id}: source {source!r} != task source {task.get('source')!r}")
        if call.get("input_digest") != decision.get("input_digest"):
            errors.append(f"{call_id}: input digest mismatch")
        if tool == "evaluate_dispatch_candidate":
            strategy = (call.get("input") or {}).get("strategy")
            budget = (call.get("input") or {}).get("budget")
            if strategy not in task.get("allowed_strategies", []):
                errors.append(f"{call_id}: illegal strategy {strategy!r}")
            if budget not in task.get("budget_options", []) or budget > task.get("max_budget", -1):
                errors.append(f"{call_id}: illegal budget {budget!r}")
    if len(tool_calls) > int(task.get("max_tool_calls", 0)):
        errors.append(f"tool calls {len(tool_calls)} exceed max_tool_calls {task.get('max_tool_calls')}")
    return errors

def _delta_errors(run: dict[str, Any]) -> list[str]:
    errors: list[str] = []
    tool_outputs = {
        call.get("tool_call_id"): call.get("output")
        for call in run["tool_calls"]
        if call.get("output")
    }
    baseline = next(
        (
            output
            for output in tool_outputs.values()
            if output.get("tool") == "evaluate_dispatch_candidate"
            and output.get("strategy") == "rule"
            and output.get("budget") == (run["task"] or {}).get("baseline_budget")
        ),
        None,
    )
    if baseline is None:
        return ["no baseline evaluation tool output"]
    baseline_metrics = baseline["metrics"]
    for record in run["evaluations"]:
        for item in record.get("evaluations") or []:
            if item.get("candidate_id") is None:
                continue
            output = tool_outputs.get(item.get("tool_call_id"))
            if output is None:
                errors.append(f"{item.get('candidate_id')}: no tool output for {item.get('tool_call_id')}")
                continue
            if output.get("result_id") != item.get("result_id"):
                errors.append(f"{item.get('candidate_id')}: result id mismatch")
            for key in ("unmet_requests", "distance_bike_km", "composite_cost", "service_rate"):
                if not _close(output["metrics"][key], item["metrics"][key]):
                    errors.append(f"{item.get('candidate_id')}: metric {key} differs from tool output")
            if output.get("violations") != item.get("violations"):
                errors.append(f"{item.get('candidate_id')}: violations differ from tool output")
            expected = {
                "cost_delta": float(output["metrics"]["composite_cost"]) - float(baseline_metrics["composite_cost"]),
                "unmet_gain": int(baseline_metrics["unmet_requests"]) - int(output["metrics"]["unmet_requests"]),
                "service_rate_delta_pp": (
                    float(output["metrics"]["service_rate"]) - float(baseline_metrics["service_rate"])
                )
                * 100.0,
            }
            for key, value in expected.items():
                actual = item["deltas"].get(key)
                if actual is None or not _close(actual, value):
                    errors.append(f"{item.get('candidate_id')}: delta {key} {actual} != {value}")
            if item.get("verdict") == "accept" and any(int(v) != 0 for v in (output.get("violations") or {}).values()):
                errors.append(f"{item.get('candidate_id')}: accepted with nonzero violations")
    return errors

def _run_errors(run: dict[str, Any]) -> list[str]:
    errors: list[str] = []
    decision = run["decision"]
    index = run["index"] or {}
    terminal = decision.get("terminal_state")
    if terminal not in SUCCESS_STATES + ("failed", "limit_reached"):
        errors.append(f"unknown terminal state {terminal!r}")
    expected_exit = 0 if terminal in SUCCESS_STATES else 1
    if int(decision.get("exit_code", -1)) != expected_exit:
        errors.append(f"exit code {decision.get('exit_code')} != {expected_exit}")
    if index.get("run_id") != decision.get("run_id"):
        errors.append("run.json and decision.json disagree on run_id")
    if decision.get("input_digest") != (run["manifest"] or {}).get("digest"):
        errors.append("decision input digest does not match the manifest")
    if decision.get("mode") != "deterministic":
        errors.append("run is not in deterministic mode")
    if decision.get("rule_based") is not True or decision.get("llm_connected") is not False:
        errors.append("run does not declare rule-driven / LLM-not-connected")
    limits = decision.get("limits") or {}
    if int(limits.get("rounds_used", 0)) > int(limits.get("max_rounds", 0)):
        errors.append("rounds_used exceeds max_rounds")
    if int(limits.get("distinct_candidates", 0)) > int(limits.get("max_distinct_candidates", 0)):
        errors.append("distinct candidates exceed the limit")
    if int(limits.get("tool_calls_used", 0)) > int(limits.get("max_tool_calls", 0)):
        errors.append("tool calls exceed the limit")
    selected = decision.get("selected")
    if terminal == "baseline_retained":
        if not selected or selected.get("kind") != "baseline":
            errors.append("baseline_retained does not select the baseline")
        if not (decision.get("baseline") or {}).get("feasible"):
            errors.append("baseline_retained but the baseline is not feasible")
    if terminal == "accepted":
        if not selected or selected.get("kind") != "candidate":
            errors.append("accepted does not select a candidate")
        elif any(int(value) != 0 for value in (selected.get("violations") or {}).values()):
            errors.append("accepted candidate has nonzero violations")
    if terminal == "no_feasible_candidate" and selected is not None:
        errors.append("no_feasible_candidate must not select anything")
    if terminal == "limit_reached" and selected is not None:
        errors.append("limit_reached must not claim a selected result")
    audit_bytes = sum(path.stat().st_size for path in run["run_dir"].rglob("*") if path.is_file())
    if audit_bytes > AUDIT_CAP:
        errors.append(f"audit directory {audit_bytes} exceeds {AUDIT_CAP}")
    # NF-39: one wall-clock number must not stand in for "agent speed"; the
    # split has to be recorded and has to add up.
    timing = decision.get("timing") or {}
    if not timing:
        errors.append("decision.json has no timing breakdown")
    else:
        parts = (
            float(timing.get("backend_simulation_ms") or 0.0)
            + float(timing.get("diagnosis_tool_ms") or 0.0)
            + float(timing.get("scheduling_overhead_ms") or 0.0)
        )
        total = float(timing.get("task_elapsed_ms") or 0.0)
        if abs(parts - total) > max(1.0, 0.001 * max(total, 1.0)):
            errors.append(f"timing parts {parts} do not add up to {total}")
    return errors

def check_real_runs(checks: Checks) -> dict[str, dict[str, Any]]:
    runs = discover_runs(ROOT / "outputs")
    selected = select_runs(runs)
    checks.add(
        "P6-RUN-PRESENT",
        "F27-1 / F29-1: real runs exist for both sources",
        ok={"xm", "sh"} <= set(selected),
        expected="xm and sh each have a run",
        actual=sorted(selected),
        inputs=[str(RUNS_ROOT.relative_to(ROOT))],
    )
    all_errors: list[str] = []
    for source, run in selected.items():
        run_dir = run["run_dir"]
        task = _read_json(run_dir / "task.json", {}).get("task") or {}
        manifest = _read_json(run_dir / "input_manifest.json", {}) or {}
        messages = run["messages"]
        tool_calls = run["tool_calls"]
        decision = run["decision"]
        run["task"] = task
        run["manifest"] = manifest
        errors = []
        errors += _message_errors(messages, tool_calls, task.get("task_id", ""), decision.get("run_id", ""))
        errors += _tool_call_errors(tool_calls, task, decision)
        errors += _delta_errors(run)
        errors += _run_errors(run)
        checks.add(
            f"P6-RUN-{source.upper()}",
            f"F24/F25/F26/F27: {source} run is internally consistent and numerically traceable",
            ok=not errors,
            expected="messages, tool calls, deltas, terminal state and limits agree",
            actual=errors[:12] or "consistent",
            inputs=[str(run_dir.relative_to(ROOT))],
        )
        all_errors += [f"{source}: {error}" for error in errors]

    default_budget_runs = [
        run for run in runs if (run["index"] or {}).get("task_id") and _is_default_budget(run)
    ]
    higher_budget_runs = [run for run in runs if _is_higher_budget(run)]
    checks.add(
        "P6-BUDGET-DEFAULT",
        "F29-1: at least one real run uses the default budget ceiling",
        ok=bool(default_budget_runs),
        expected="a run with max_budget == baseline_budget",
        actual=[run["run_id"] for run in default_budget_runs][:5],
        inputs=["outputs/multi_agent"],
    )
    disclosed = []
    for run in higher_budget_runs:
        task = _read_json(run["run_dir"] / "task.json", {}).get("task") or {}
        text = (run["run_dir"] / "report.md").read_text(encoding="utf-8") if (run["run_dir"] / "report.md").exists() else ""
        decision_text = json.dumps(run["decision"], ensure_ascii=False)
        if str(task.get("max_budget")) in text and ("预算" in text or "budget" in text) or str(task.get("max_budget")) in decision_text:
            disclosed.append(run["run_id"])
    checks.add(
        "P6-BUDGET-HIGHER",
        "F29-1: a higher budget ceiling was run and the resource change is disclosed",
        ok=bool(higher_budget_runs) and len(disclosed) == len(higher_budget_runs),
        expected="at least one run with max_budget > baseline, reported with the ceiling",
        actual={"runs": [run["run_id"] for run in higher_budget_runs][:5], "disclosed": disclosed[:5]},
        inputs=["outputs/multi_agent"],
    )
    return selected

def _is_default_budget(run: dict[str, Any]) -> bool:
    task = _read_json(run["run_dir"] / "task.json", {}).get("task") or {}
    return task.get("max_budget") == task.get("baseline_budget")

def _is_higher_budget(run: dict[str, Any]) -> bool:
    task = _read_json(run["run_dir"] / "task.json", {}).get("task") or {}
    return int(task.get("max_budget", 0)) > int(task.get("baseline_budget", 0))

def check_equivalence(checks: Checks) -> None:
    config = load_config()
    layout = Layout()
    business = [
        layout.sim_path("xm", "rule", "json"),
        layout.sim_path("xm", "optimized", "json"),
        layout.sim_path("sh", "rule", "json"),
        layout.sim_path("sh", "optimized", "json"),
        layout.key_numbers_path(),
    ]
    before = {path: _sha(path) for path in business if path.exists()}
    mismatches: list[str] = []
    for source in ("xm", "sh"):
        task = build_task(config, source, max_budget=400)
        manifest = freeze_inputs(config, layout, source)
        for strategy in ("rule", "optimized"):
            for budget in task.budget_options:
                new = evaluate_dispatch_candidate(
                    config, layout, task, manifest, strategy=strategy, budget=budget
                )
                # The old F7 handler is a thin reading of this same engine
                # result; re-derive its four metrics and trajectory here so the
                # check does not trust either wrapper's formatting.
                engine = engine_run_simulation(
                    config, layout, source, strategy, budget=budget, write=False
                )
                summary = engine["summary"]
                expected_metrics = {
                    "unmet_requests": int(summary["unmet_requests"]),
                    "distance_bike_km": float(summary["distance_bike_km"]),
                    "composite_cost": float(summary["composite_cost"]),
                    "service_rate": float(summary["service_rate"]),
                }
                expected_trajectory = [
                    {
                        "time_bucket": str(row.time_bucket),
                        "borrow_requests": int(row.borrow_requests),
                        "unmet_requests": int(row.unmet_requests),
                        "inventory_end": int(row.inventory_end),
                    }
                    for row in engine["steps"].itertuples(index=False)
                ]
                if new["metrics"] != expected_metrics:
                    mismatches.append(f"{source}/{strategy}/{budget}: metrics")
                if new["trajectory"] != expected_trajectory:
                    mismatches.append(f"{source}/{strategy}/{budget}: trajectory")
                if new["violations"] != engine["violations"]:
                    mismatches.append(f"{source}/{strategy}/{budget}: violations")
    checks.add(
        "P6-EQUIVALENCE",
        "F26-2: the new tool matches the old engine for both sources, strategies and budget tiers",
        ok=not mismatches,
        expected="identical metrics, trajectory and violations",
        actual=mismatches[:12] or "identical",
        inputs=["outputs/", "data/processed/"],
    )
    after = {path: _sha(path) for path in business if path.exists()}
    changed = [str(path.relative_to(ROOT)) for path in business if before.get(path) != after.get(path)]
    checks.add(
        "P6-NO-WRITE",
        "F26-4: evaluation does not rewrite existing business artefacts",
        ok=not changed,
        expected="business artefact hashes unchanged",
        actual=changed or "unchanged",
        inputs=[str(path.relative_to(ROOT)) for path in business],
    )

def check_boundaries(checks: Checks) -> None:
    roles_path = ROOT / "bike_agent" / "multi_agent" / "roles.py"
    tree = ast.parse(roles_path.read_text(encoding="utf-8"))
    forbidden = {"pandas", "pathlib", "bike_agent.config", "bike_agent.tools", "bike_agent.sim", "bike_agent.replay"}
    offenders = []
    for node in ast.walk(tree):
        modules = []
        if isinstance(node, ast.ImportFrom) and node.module:
            modules.append(node.module)
        elif isinstance(node, ast.Import):
            modules.extend(alias.name for alias in node.names)
        for module in modules:
            if any(module == item or module.startswith(item + ".") for item in forbidden):
                offenders.append(f"{node.lineno}: {module}")
    source = roles_path.read_text(encoding="utf-8")
    for token in ("read_parquet", "read_csv", "open(", "Path("):
        if token in source:
            offenders.append(f"forbidden token {token}")
    checks.add(
        "P6-BOUNDARY-ROLES",
        "F26-4: roles do not read business files; only the gateway and the run store do",
        ok=not offenders,
        expected="no file/data imports or open() in roles.py",
        actual=offenders or "clean",
        inputs=["bike_agent/multi_agent/roles.py"],
    )

    cli_path = ROOT / "bike_agent" / "cli.py"
    cli_tree = ast.parse(cli_path.read_text(encoding="utf-8"))
    module_level = []
    for node in cli_tree.body:
        if isinstance(node, ast.ImportFrom) and node.module:
            module_level.append(node.module)
        elif isinstance(node, ast.Import):
            module_level.extend(alias.name for alias in node.names)
    checks.add(
        "P6-REPRO-BOUNDARY",
        "NF-34: `bike-agent repro` does not import the multi-agent package at module scope",
        ok=not any("multi_agent" in module for module in module_level),
        expected="multi_agent imported lazily only",
        actual=module_level,
        inputs=["bike_agent/cli.py"],
    )

def check_collab(checks: Checks, selected: dict[str, dict[str, Any]]) -> None:
    path = SITE / "collab.html"
    exists = path.exists()
    checks.add(
        "P6-COLLAB-EXISTS",
        "F28-1: the offline collaboration page exists",
        ok=exists,
        expected="replay/collab.html",
        actual=str(path.relative_to(ROOT)) if exists else "missing",
        inputs=["replay/collab.html"],
    )
    if not exists:
        return
    text = path.read_text(encoding="utf-8")
    match = re.search(r'<script type="application/json" id="collab-data">(.*?)</script>', text, re.DOTALL)
    payload = json.loads(match.group(1)) if match else {}
    page_runs = {run["source"]: run for run in payload.get("runs") or []}
    checks.add(
        "P6-COLLAB-SOURCES",
        "F28-1: the page shows a real run for XM and SH",
        ok={"xm", "sh"} <= set(page_runs),
        expected="xm and sh runs",
        actual=sorted(page_runs),
        inputs=["replay/collab.html"],
    )
    # The page may ship more than one run per source (the selector).  Every
    # shipped run is checked against its own audit directory; the one shown
    # first must be the one ``select_runs`` chose.
    by_id = {run["run_id"]: run for run in discover_runs(ROOT / "outputs")}
    shown = payload.get("selected") or {}
    errors: list[str] = []
    for page_run in payload.get("runs") or []:
        source = page_run.get("source")
        audit = by_id.get(page_run.get("run_id"))
        if audit is None:
            errors.append(f"{source}: page run {page_run.get('run_id')} has no audit directory")
            continue
        if audit.get("source") != source:
            errors.append(f"{source}: page run {page_run.get('run_id')} belongs to {audit.get('source')}")
        message_ids = {message["message_id"] for message in audit["messages"]}
        tool_ids = {call["tool_call_id"] for call in audit["tool_calls"]}
        page_message_ids = {message["message_id"] for message in page_run.get("messages") or []}
        page_tool_ids = {call["tool_call_id"] for call in page_run.get("tool_calls") or []}
        if page_message_ids != message_ids:
            errors.append(f"{source}: message ids differ from {audit['run_id']}")
        if page_tool_ids != tool_ids:
            errors.append(f"{source}: tool call ids differ from {audit['run_id']}")
        if page_run.get("decision", {}).get("terminal_state") != audit["decision"].get("terminal_state"):
            errors.append(f"{source}: terminal state differs from {audit['run_id']}")
        node_message_ids = set(re.findall(r'data-message-id="([^"]+)"', text))
        node_tool_ids = set(re.findall(r'data-tool-call-id="([^"]+)"', text))
        if not page_message_ids <= node_message_ids:
            errors.append(f"{source}: some messages have no timeline node")
        if not page_tool_ids <= node_tool_ids:
            errors.append(f"{source}: some tool calls have no timeline node")
    for source, run in selected.items():
        if shown.get(source) != run["run_id"]:
            errors.append(f"{source}: the page starts on {shown.get(source)}, not {run['run_id']}")
    checks.add(
        "P6-COLLAB-NODES",
        "F28-1/F28-4: page nodes, run ids and terminal states match the audit artefacts",
        ok=not errors,
        expected="every node maps to a real message/tool id; terminal states agree",
        actual=errors[:12] or "consistent",
        inputs=["replay/collab.html", "outputs/multi_agent/"],
    )
    external = re.findall(r'(?:src|href)="(https?://[^"]+)"', text)
    checks.add(
        "P6-COLLAB-OFFLINE",
        "F28-3: the page has no external resources and needs no network",
        ok=not external and "fetch(" not in text and "XMLHttpRequest" not in text,
        expected="no external URLs, no fetch/XHR",
        actual=external[:5] or "offline",
        inputs=["replay/collab.html"],
    )
    data_bytes = len(match.group(1).encode("utf-8")) if match else 0
    checks.add(
        "P6-COLLAB-SIZE",
        "NF-37: collaboration data for both sources is at most 1 MiB",
        ok=0 < data_bytes <= COLLAB_CAP,
        expected=f"<= {COLLAB_CAP} bytes",
        actual=data_bytes,
        inputs=["replay/collab.html"],
    )
    deep_links = re.findall(r'href="(?:\.\./)?(xm|sh)/(player|workbench)\.html#([^"]+)"', text)
    valid_links = [
        (source, page, fragment)
        for source, page, fragment in deep_links
        if re.search(r"(?:^|&)frame=\d+", fragment)
    ]
    checks.add(
        "P6-COLLAB-DEEPLINK",
        "F28-2: the page links to a concrete player/workbench frame (and cell where valid)",
        ok=bool(valid_links),
        expected="at least one ../<source>/<page>.html#frame=N link",
        actual=len(valid_links),
        inputs=["replay/collab.html"],
    )

def check_site(checks: Checks) -> None:
    total = sum(path.stat().st_size for path in SITE.rglob("*") if path.is_file())
    checks.add(
        "P6-SITE-SIZE",
        "NF-37: the replay site stays under 14 MiB",
        ok=total < SITE_CAP,
        expected=f"< {SITE_CAP} bytes",
        actual=total,
        inputs=["replay/"],
    )

def check_deep_link_code(checks: Checks) -> None:
    player = (SITE / "player.js").read_text(encoding="utf-8") if (SITE / "player.js").exists() else ""
    workbench = (SITE / "workbench.js").read_text(encoding="utf-8") if (SITE / "workbench.js").exists() else ""
    player_ok = "location.hash" in player and "deepApplied" in player
    workbench_ok = "location.hash" in workbench and "core.setFrame" in workbench and "core.selectCell" in workbench
    checks.add(
        "P6-DEEPLINK-CODE",
        "F28-2: the shipped player and workbench parse frame/cell deep links",
        ok=player_ok and workbench_ok,
        expected="location.hash parsing in player.js and workbench.js",
        actual={"player": player_ok, "workbench": workbench_ok},
        inputs=["replay/player.js", "replay/workbench.js"],
    )

def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--json", action="store_true", help="print the report as JSON")
    parser.add_argument("--verbose", action="store_true")
    args = parser.parse_args(argv)

    checks = Checks()
    check_schemas(checks)
    check_old_tools(checks)
    selected = check_real_runs(checks)
    check_equivalence(checks)
    check_boundaries(checks)
    check_collab(checks, selected)
    check_site(checks)
    check_deep_link_code(checks)

    report = {
        "phase": "6",
        "requirement": "REQUIREMENTS-PHASE6.md F24–F29 / NF-34–NF-39",
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "n_checks": len(checks.items),
        "n_failed": len(checks.failed),
        "checks": checks.items,
    }
    REPORT.parent.mkdir(parents=True, exist_ok=True)
    REPORT.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    if args.json:
        print(json.dumps(report, ensure_ascii=False, indent=2))
    else:
        for item in checks.items:
            if item["result"] == "pass" and not args.verbose:
                continue
            print(f"[{item['result']}] {item['id']}: {item['actual']}")
        print(f"phase 6: {len(checks.items) - len(checks.failed)}/{len(checks.items)} checks passed -> {REPORT.relative_to(ROOT)}")
    return 0 if not checks.failed else 1

if __name__ == "__main__":
    raise SystemExit(main())
