"""JSON Schema contracts for the Phase 6 multi-agent system (F24 / F26).

Three contracts live here and nowhere else:

* ``TASK_SCHEMA`` -- the frozen task definition.  Every value the loop may use
  is decided before the first business call, so a run cannot quietly widen its
  own budget or strategy set.
* ``MESSAGE_SCHEMA`` plus ``PAYLOAD_SCHEMAS`` -- the message envelope and one
  payload schema per message type.  ``additionalProperties`` is ``false``
  throughout: a field that is not part of the protocol is a protocol error, not
  an extension point.
* ``EVALUATE_INPUT_SCHEMA`` / ``EVALUATE_OUTPUT_SCHEMA`` -- the versioned
  ``evaluate_dispatch_candidate`` contract.  It is intentionally *not* added to
  the F7 tool document: the old three-tool set stays byte-for-byte compatible.

The evaluation output mirrors the original engine, including all five
``violations`` counters.  The old F7 handler dropped them; this contract keeps
them so a candidate can be rejected on evidence rather than on a tool's name.
"""

from __future__ import annotations

from typing import Any

SCHEMA_DRAFT = "https://json-schema.org/draft/2020-12/schema"
PROTOCOL_VERSION = "1.0"
EVALUATION_VERSION = "1.0"

ROLES = ("coordinator", "diagnosis", "dispatch", "evaluation")
MESSAGE_TYPES = (
    "task_assignment",
    "diagnosis",
    "proposal",
    "evaluation",
    "revision_request",
    "revision_proposal",
    "final",
)
TERMINAL_STATES = (
    "accepted",
    "baseline_retained",
    "no_feasible_candidate",
    "failed",
    "limit_reached",
)
#: Terminal states that mean "the run completed normally" (exit code 0).
SUCCESS_STATES = ("accepted", "baseline_retained", "no_feasible_candidate")

STRATEGIES = ("rule", "optimized")
OBJECTIVE = "minimize_composite_cost"

_TASK = {
    "type": "object",
    "required": [
        "task_id",
        "source",
        "objective",
        "baseline_strategy",
        "allowed_strategies",
        "baseline_budget",
        "budget_options",
        "max_budget",
        "min_service_rate",
        "max_rounds",
        "max_candidates_per_round",
        "max_distinct_candidates",
        "max_tool_calls",
        "tool_timeout_s",
        "task_timeout_s",
        "mode",
        "seed",
    ],
    "properties": {
        "task_id": {"type": "string", "minLength": 1},
        "source": {"type": "string", "enum": ["xm", "sh"]},
        "objective": {"type": "string", "enum": [OBJECTIVE]},
        "baseline_strategy": {"type": "string", "enum": ["rule"]},
        "allowed_strategies": {
            "type": "array",
            "minItems": 1,
            "uniqueItems": True,
            "items": {"type": "string", "enum": list(STRATEGIES)},
        },
        "baseline_budget": {"type": "integer", "minimum": 0},
        "budget_options": {
            "type": "array",
            "minItems": 1,
            "uniqueItems": True,
            "items": {"type": "integer", "minimum": 0},
        },
        "max_budget": {"type": "integer", "minimum": 0},
        "min_service_rate": {
            "oneOf": [
                {"type": "number", "minimum": 0, "maximum": 1},
                {"type": "null"},
            ]
        },
        "max_rounds": {"type": "integer", "minimum": 1, "maximum": 10},
        "max_candidates_per_round": {"type": "integer", "minimum": 1, "maximum": 2},
        "max_distinct_candidates": {"type": "integer", "minimum": 1, "maximum": 20},
        "max_tool_calls": {"type": "integer", "minimum": 1, "maximum": 100},
        "tool_timeout_s": {"type": "number", "exclusiveMinimum": 0},
        "task_timeout_s": {"type": "number", "exclusiveMinimum": 0},
        "mode": {"type": "string", "enum": ["deterministic"]},
        "seed": {"type": "integer"},
    },
    "additionalProperties": False,
}

TASK_SCHEMA: dict[str, Any] = {
    "$schema": SCHEMA_DRAFT,
    "$id": "https://bike-agent.local/schemas/multi_agent.task.v1.json",
    "title": "bike-agent multi-agent task",
    **_TASK,
}

_MESSAGE = {
    "type": "object",
    "required": [
        "schema_version",
        "run_id",
        "message_id",
        "parent_id",
        "sender",
        "recipient",
        "type",
        "round",
        "task_id",
        "payload",
        "evidence_refs",
        "status",
        "sequence",
        "timestamp",
    ],
    "properties": {
        "schema_version": {"type": "string", "const": PROTOCOL_VERSION},
        "run_id": {"type": "string", "minLength": 1},
        "message_id": {"type": "string", "pattern": "^msg-[0-9]{4,}$"},
        "parent_id": {"oneOf": [{"type": "string"}, {"type": "null"}]},
        "sender": {"type": "string", "enum": list(ROLES)},
        "recipient": {"type": "string", "enum": list(ROLES)},
        "type": {"type": "string", "enum": list(MESSAGE_TYPES)},
        "round": {"type": "integer", "minimum": 0},
        "task_id": {"type": "string", "minLength": 1},
        "payload": {"type": "object"},
        "evidence_refs": {"type": "array", "items": {"type": "string"}},
        "status": {"type": "string", "enum": ["ok", "rejected", "error"]},
        "sequence": {"type": "integer", "minimum": 0},
        "timestamp": {"type": "string", "minLength": 1},
    },
    "additionalProperties": False,
}

MESSAGE_SCHEMA: dict[str, Any] = {
    "$schema": SCHEMA_DRAFT,
    "$id": "https://bike-agent.local/schemas/multi_agent.message.v1.json",
    "title": "bike-agent multi-agent message",
    **_MESSAGE,
}

_FOCUS_BUCKET = {
    "type": "object",
    "required": ["time_bucket", "unmet_requests", "rank"],
    "properties": {
        "time_bucket": {"type": "string", "minLength": 1},
        "unmet_requests": {"type": "integer", "minimum": 0},
        "rank": {"type": "integer", "minimum": 1},
    },
    "additionalProperties": False,
}

_FOCUS_CELL = {
    "type": "object",
    "required": ["cell_id", "reason", "evidence_kind"],
    "properties": {
        "cell_id": {"type": "string", "minLength": 1},
        "reason": {"type": "string", "minLength": 1},
        # F26: a forecast net outflow is not proof of an actual loss.
        "evidence_kind": {"type": "string", "enum": ["possible_related", "supported"]},
    },
    "additionalProperties": False,
}

_HYPOTHESIS = {
    "type": "object",
    "required": ["claim", "evidence_refs", "status"],
    "properties": {
        "claim": {"type": "string", "minLength": 1},
        "evidence_refs": {"type": "array", "items": {"type": "string"}},
        "status": {"type": "string", "enum": ["possible_related", "supported"]},
    },
    "additionalProperties": False,
}

_CANDIDATE = {
    "type": "object",
    "required": [
        "candidate_id",
        "strategy",
        "budget",
        "rationale",
        "expected_tradeoff",
        "revision_of",
    ],
    "properties": {
        "candidate_id": {"type": "string", "pattern": "^cand-r[0-9]+-[0-9]+$"},
        "strategy": {"type": "string", "enum": list(STRATEGIES)},
        "budget": {"type": "integer", "minimum": 0},
        "rationale": {"type": "string", "minLength": 1},
        "expected_tradeoff": {"type": "string", "minLength": 1},
        "revision_of": {"oneOf": [{"type": "string"}, {"type": "null"}]},
    },
    "additionalProperties": False,
}

_EVALUATION_ITEM = {
    "type": "object",
    "required": [
        "candidate_id",
        "result_id",
        "feasible",
        "violations_ok",
        "service_rate_ok",
        "metrics",
        "violations",
        "deltas",
        "formula",
        "operands",
        "verdict",
        "defects",
        "suggested_revision",
    ],
    "properties": {
        "candidate_id": {"oneOf": [{"type": "string"}, {"type": "null"}]},
        "result_id": {"type": "string", "minLength": 1},
        "tool_call_id": {"type": "string", "minLength": 1},
        "feasible": {"type": "boolean"},
        "violations_ok": {"type": "boolean"},
        "service_rate_ok": {"type": "boolean"},
        "metrics": {
            "type": "object",
            "required": [
                "unmet_requests",
                "distance_bike_km",
                "composite_cost",
                "service_rate",
            ],
            "properties": {
                "unmet_requests": {"type": "integer", "minimum": 0},
                "distance_bike_km": {"type": "number", "minimum": 0},
                "composite_cost": {"type": "number"},
                "service_rate": {"type": "number", "minimum": 0, "maximum": 1},
            },
            "additionalProperties": False,
        },
        "violations": {
            "type": "object",
            "required": [
                "conservation",
                "capacity",
                "budget",
                "negative_inventory",
                "clamped_moves",
            ],
            "properties": {
                "conservation": {"type": "integer", "minimum": 0},
                "capacity": {"type": "integer", "minimum": 0},
                "budget": {"type": "integer", "minimum": 0},
                "negative_inventory": {"type": "integer", "minimum": 0},
                "clamped_moves": {"type": "integer", "minimum": 0},
            },
            "additionalProperties": False,
        },
        "deltas": {
            "type": "object",
            "required": ["cost_delta", "unmet_gain", "service_rate_delta_pp"],
            "properties": {
                "cost_delta": {"type": "number"},
                "unmet_gain": {"type": "integer"},
                "service_rate_delta_pp": {"type": "number"},
            },
            "additionalProperties": False,
        },
        # F26: the formula, its operands and the result ids travel with the
        # numbers, so a reader can recompute the deltas without guessing.
        "formula": {
            "type": "object",
            "required": ["cost_delta", "unmet_gain", "service_rate_delta_pp"],
            "properties": {
                "cost_delta": {"type": "string", "minLength": 1},
                "unmet_gain": {"type": "string", "minLength": 1},
                "service_rate_delta_pp": {"type": "string", "minLength": 1},
            },
            "additionalProperties": False,
        },
        "operands": {
            "type": "object",
            "required": ["baseline", "candidate"],
            "properties": {
                side: {
                    "type": "object",
                    "required": ["result_id", "metrics"],
                    "properties": {
                        "result_id": {"type": "string", "minLength": 1},
                        "metrics": {
                            "type": "object",
                            "required": [
                                "unmet_requests",
                                "distance_bike_km",
                                "composite_cost",
                                "service_rate",
                            ],
                            "properties": {
                                "unmet_requests": {"type": "integer", "minimum": 0},
                                "distance_bike_km": {"type": "number", "minimum": 0},
                                "composite_cost": {"type": "number"},
                                "service_rate": {"type": "number", "minimum": 0, "maximum": 1},
                            },
                            "additionalProperties": False,
                        },
                    },
                    "additionalProperties": False,
                }
                for side in ("baseline", "candidate")
            },
            "additionalProperties": False,
        },
        "verdict": {"type": "string", "enum": ["accept", "reject"]},
        "defects": {"type": "array", "items": {"type": "string"}},
        "suggested_revision": {"oneOf": [{"type": "object"}, {"type": "null"}]},
    },
    "additionalProperties": False,
}

PAYLOAD_SCHEMAS: dict[str, dict[str, Any]] = {
    "task_assignment": {
        "type": "object",
        "required": ["instruction", "assignee", "round"],
        "properties": {
            "instruction": {
                "type": "string",
                "enum": [
                    "establish_baseline",
                    "diagnose",
                    "propose",
                    "evaluate",
                    "revise",
                    "finalize",
                ],
            },
            "assignee": {"type": "string", "enum": list(ROLES)},
            "round": {"type": "integer", "minimum": 0},
            "details": {"type": "object"},
        },
        "additionalProperties": False,
    },
    "diagnosis": {
        "type": "object",
        "required": [
            "focus_buckets",
            "focus_cells",
            "hypotheses",
            "evidence_gaps",
            "risk_ranking_scope",
            "recommended",
        ],
        "properties": {
            "focus_buckets": {"type": "array", "items": _FOCUS_BUCKET},
            "focus_cells": {"type": "array", "items": _FOCUS_CELL},
            "hypotheses": {"type": "array", "items": _HYPOTHESIS},
            "evidence_gaps": {"type": "array", "items": {"type": "string"}},
            "risk_ranking_scope": {
                "type": "object",
                "required": ["time_bucket", "covers_focus_bucket", "note"],
                "properties": {
                    "time_bucket": {"type": "string"},
                    "covers_focus_bucket": {"type": "boolean"},
                    "note": {"type": "string", "minLength": 1},
                },
                "additionalProperties": False,
            },
            "recommended": {"oneOf": [{"type": "object"}, {"type": "null"}]},
        },
        "additionalProperties": False,
    },
    "proposal": {
        "type": "object",
        "required": ["candidates", "changed_fields", "feedback_response"],
        "properties": {
            "candidates": {"type": "array", "maxItems": 2, "items": _CANDIDATE},
            "changed_fields": {"type": "array", "items": {"type": "string"}},
            "feedback_response": {"oneOf": [{"type": "object"}, {"type": "null"}]},
        },
        "additionalProperties": False,
    },
    "evaluation": {
        "type": "object",
        "required": ["baseline_result_id", "evaluations"],
        "properties": {
            "baseline_result_id": {"type": "string", "minLength": 1},
            "evaluations": {"type": "array", "minItems": 1, "items": _EVALUATION_ITEM},
        },
        "additionalProperties": False,
    },
    "revision_request": {
        "type": "object",
        "required": ["candidate_id", "defects", "suggested_revision"],
        "properties": {
            "candidate_id": {"type": "string", "minLength": 1},
            "defects": {"type": "array", "minItems": 1, "items": {"type": "string"}},
            "suggested_revision": {"type": "object"},
        },
        "additionalProperties": False,
    },
    "revision_proposal": {
        "type": "object",
        "required": ["candidates", "changed_fields", "feedback_response"],
        "properties": {
            "candidates": {"type": "array", "maxItems": 2, "items": _CANDIDATE},
            "changed_fields": {"type": "array", "items": {"type": "string"}},
            "feedback_response": {"type": "object"},
        },
        "additionalProperties": False,
    },
    "final": {
        "type": "object",
        "required": ["terminal_state", "reason", "selected", "baseline", "ranking", "limits"],
        "properties": {
            "terminal_state": {"type": "string", "enum": list(TERMINAL_STATES)},
            "reason": {"type": "string", "minLength": 1},
            "selected": {"oneOf": [{"type": "object"}, {"type": "null"}]},
            "baseline": {"oneOf": [{"type": "object"}, {"type": "null"}]},
            "ranking": {"type": "array", "items": {"type": "object"}},
            "limits": {"type": "object"},
        },
        "additionalProperties": False,
    },
}

_VIOLATIONS = {
    "type": "object",
    "required": [
        "conservation",
        "capacity",
        "budget",
        "negative_inventory",
        "clamped_moves",
    ],
    "properties": {
        "conservation": {"type": "integer", "minimum": 0},
        "capacity": {"type": "integer", "minimum": 0},
        "budget": {"type": "integer", "minimum": 0},
        "negative_inventory": {"type": "integer", "minimum": 0},
        "clamped_moves": {"type": "integer", "minimum": 0},
    },
    "additionalProperties": False,
}

EVALUATE_INPUT_SCHEMA: dict[str, Any] = {
    "$schema": SCHEMA_DRAFT,
    "$id": "https://bike-agent.local/schemas/multi_agent.evaluate_dispatch_candidate.v1.input.json",
    "title": "evaluate_dispatch_candidate input (v1)",
    "type": "object",
    "required": ["task_id", "source", "strategy", "budget", "frozen_context_id"],
    "properties": {
        "task_id": {"type": "string", "minLength": 1},
        "source": {"type": "string", "enum": ["xm", "sh"]},
        "strategy": {"type": "string", "enum": list(STRATEGIES)},
        "budget": {"type": "integer", "minimum": 0},
        "frozen_context_id": {"type": "string", "minLength": 1},
    },
    "additionalProperties": False,
}

EVALUATE_OUTPUT_SCHEMA: dict[str, Any] = {
    "$schema": SCHEMA_DRAFT,
    "$id": "https://bike-agent.local/schemas/multi_agent.evaluate_dispatch_candidate.v1.output.json",
    "title": "evaluate_dispatch_candidate output (v1)",
    "type": "object",
    "required": [
        "tool",
        "schema_version",
        "result_id",
        "task_id",
        "source",
        "strategy",
        "budget",
        "frozen_context_id",
        "simulated",
        "metrics",
        "trajectory",
        "violations",
        "assumptions",
        "input_manifest",
    ],
    "properties": {
        "tool": {"type": "string", "const": "evaluate_dispatch_candidate"},
        "schema_version": {"type": "string", "const": EVALUATION_VERSION},
        "result_id": {"type": "string", "minLength": 1},
        "task_id": {"type": "string", "minLength": 1},
        "source": {"type": "string", "enum": ["xm", "sh"]},
        "strategy": {"type": "string", "enum": list(STRATEGIES)},
        "budget": {"type": "integer", "minimum": 0},
        "frozen_context_id": {"type": "string", "minLength": 1},
        "simulated": {"type": "boolean", "const": True},
        "metrics": {
            "type": "object",
            "required": [
                "unmet_requests",
                "distance_bike_km",
                "composite_cost",
                "service_rate",
            ],
            "properties": {
                "unmet_requests": {"type": "integer", "minimum": 0},
                "distance_bike_km": {"type": "number", "minimum": 0},
                "composite_cost": {"type": "number"},
                "service_rate": {"type": "number", "minimum": 0, "maximum": 1},
            },
            "additionalProperties": False,
        },
        "trajectory": {
            "type": "array",
            "items": {
                "type": "object",
                "required": [
                    "time_bucket",
                    "borrow_requests",
                    "unmet_requests",
                    "inventory_end",
                ],
                "properties": {
                    "time_bucket": {"type": "string"},
                    "borrow_requests": {"type": "integer", "minimum": 0},
                    "unmet_requests": {"type": "integer", "minimum": 0},
                    "inventory_end": {"type": "integer"},
                },
                "additionalProperties": False,
            },
        },
        "violations": _VIOLATIONS,
        "assumptions": {
            "type": "object",
            "required": [
                "initial_fill_ratio",
                "safety_stock_ratio",
                "max_moves_per_bucket",
                "unmet_penalty",
                "distance_cost_per_bike_km",
                "max_candidate_surplus_per_deficit",
            ],
            "properties": {
                "initial_fill_ratio": {"type": "number"},
                "safety_stock_ratio": {"type": "number"},
                "max_moves_per_bucket": {"type": "integer", "minimum": 0},
                "unmet_penalty": {"type": "number"},
                "distance_cost_per_bike_km": {"type": "number"},
                "max_candidate_surplus_per_deficit": {"type": "integer", "minimum": 1},
            },
            "additionalProperties": False,
        },
        "input_manifest": {
            "type": "object",
            "required": ["digest", "files"],
            "properties": {
                "digest": {"type": "string", "minLength": 1},
                "files": {
                    "type": "array",
                    "items": {
                        "type": "object",
                        "required": ["path", "sha256", "bytes"],
                        "properties": {
                            "path": {"type": "string"},
                            "sha256": {"type": "string"},
                            "bytes": {"type": "integer", "minimum": 0},
                        },
                        "additionalProperties": False,
                    },
                },
            },
            "additionalProperties": False,
        },
    },
    "additionalProperties": False,
}

#: Every schema that must itself validate as Draft 2020-12 at import/check time.
ALL_SCHEMAS = {
    "task": TASK_SCHEMA,
    "message": MESSAGE_SCHEMA,
    "evaluate_input": EVALUATE_INPUT_SCHEMA,
    "evaluate_output": EVALUATE_OUTPUT_SCHEMA,
    **{f"payload.{name}": schema for name, schema in PAYLOAD_SCHEMAS.items()},
}
