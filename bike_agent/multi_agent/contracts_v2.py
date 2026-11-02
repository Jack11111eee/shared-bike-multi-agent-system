"""Version 2 model actions, separate from the legacy strict contracts."""
from __future__ import annotations

from copy import deepcopy
from typing import Any

from bike_agent.multi_agent.contracts import TASK_SCHEMA

TASK_SCHEMA_V2 = deepcopy(TASK_SCHEMA)
TASK_SCHEMA_V2["$id"] = "https://bike-agent.local/schemas/multi_agent.task.v2.json"
TASK_SCHEMA_V2["properties"]["mode"] = {"const": "llm"}


def obj(properties: dict, required: list[str] | None = None) -> dict:
    return {"type": "object", "properties": properties,
            "required": list(properties) if required is None else required,
            "additionalProperties": False}


TEXT = {"type": "string", "minLength": 1, "maxLength": 2000}
REFS = {"type": "array", "items": TEXT, "maxItems": 20, "uniqueItems": True}
COORDINATOR_OUTPUT = obj({
    "action": {"enum": ["assign", "request_revision", "request_diagnosis", "finalize"]},
    "assignee": {"enum": ["diagnosis", "dispatch", "evaluation", None]},
    "candidate_id": {"type": ["string", "null"]},
    "reason": TEXT,
    "evidence_refs": REFS,
})
COORDINATOR_OUTPUT["properties"]["revision_fields"] = {
    "type": "array", "items": {"enum": ["strategy", "budget"]},
    "uniqueItems": True, "maxItems": 2,
}
CLAIM = obj({
    "claim": TEXT, "status": {"enum": ["supported", "possible_related", "evidence_missing"]},
    "evidence_refs": REFS,
})
DIAGNOSIS_OUTPUT = obj({
    "summary": TEXT,
    "focus_buckets": {"type": "array", "items": TEXT, "maxItems": 3},
    "focus_cells": {"type": "array", "items": TEXT, "maxItems": 5},
    "claims": {"type": "array", "items": CLAIM, "maxItems": 6},
    "evidence_gaps": {"type": "array", "items": TEXT, "maxItems": 6},
    "evidence_refs": REFS,
})
CANDIDATE = obj({
    "strategy": {"enum": ["rule", "optimized"]},
    "budget": {"type": "integer", "minimum": 0},
    "rationale": TEXT,
    "expected_tradeoff": TEXT,
})
DISPATCH_OUTPUT = obj({
    "summary": TEXT,
    "candidates": {"type": "array", "items": CANDIDATE, "maxItems": 2},
    "revision_of": {"type": ["string", "null"]},
    "changed_fields": {"type": "array", "items": {"enum": ["strategy", "budget"]}, "uniqueItems": True},
    "evidence_refs": REFS,
})
ASSESSMENT = obj({
    "candidate_id": TEXT, "result_id": TEXT,
    "verdict": {"enum": ["feasible", "infeasible"]},
    "explanation": TEXT,
    "recommended_action": {"enum": ["accept", "revise", "retain_baseline", "stop"]},
})
EVALUATION_OUTPUT = obj({
    "summary": TEXT,
    "assessments": {"type": "array", "items": ASSESSMENT, "minItems": 1, "maxItems": 2},
    "evidence_refs": REFS,
})
ROLE_OUTPUTS: dict[str, dict[str, Any]] = {
    "coordinator": COORDINATOR_OUTPUT, "diagnosis": DIAGNOSIS_OUTPUT,
    "dispatch": DISPATCH_OUTPUT, "evaluation": EVALUATION_OUTPUT,
}


def function(name: str, description: str, parameters: dict) -> dict:
    return {"type": "function", "function": {
        "name": name, "description": description, "parameters": parameters,
    }}


def role_tools(role: str) -> list[dict]:
    tools = [function("submit_role_output", "提交当前角色的结构化交付，完成本次角色工作。",
                      ROLE_OUTPUTS[role])]
    if role == "diagnosis":
        tools += [
            function("get_forecast", "读取已生成的需求预测，不是实际损失。时间必须在给出的可用时段中。",
                     obj({"time_bucket": TEXT}, ["time_bucket"])),
            function("get_risk_ranking", "读取风险排名；返回的时段可能不同于关注时段，必须如实说明。",
                     obj({"direction": {"enum": ["replenish", "clear"]},
                          "top_k": {"type": "integer", "minimum": 1, "maximum": 10}})),
        ]
    if role == "evaluation":
        tools.append(function(
            "evaluate_dispatch_candidate", "实际运行指定待评估候选的仿真，获取机器事实和完整空间产物。",
            obj({"candidate_id": TEXT}),
        ))
    return tools
