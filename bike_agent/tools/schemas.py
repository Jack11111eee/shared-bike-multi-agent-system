"""JSON Schema contracts for the three agent tools (F7-1).

The schema is a single Draft 2020-12 document.  ``bike-agent tools --schema``
prints it verbatim; ``jsonschema`` validates both the document itself and every
tool response produced by :mod:`bike_agent.tools.handlers`.
"""

from __future__ import annotations

from typing import Any

SCHEMA_DRAFT = "https://json-schema.org/draft/2020-12/schema"

_TOOL_ENVELOPE = {
    "type": "object",
    "required": ["description", "input", "output"],
    "properties": {
        "description": {"type": "string"},
        "input": {"type": "object"},
        "output": {"type": "object"},
    },
    "additionalProperties": False,
}

TOOL_SCHEMA: dict[str, Any] = {
    "$schema": SCHEMA_DRAFT,
    "$id": "https://bike-agent.local/schemas/tools.schema.json",
    "title": "bike-agent tool contracts",
    "description": "Stable contracts for LLM-agent frameworks. The tool layer is a thin wrapper: all business logic lives in the F1-F5 modules.",
    "type": "object",
    "required": ["tools"],
    "properties": {
        "tools": {
            "type": "object",
            "required": ["get_forecast", "get_risk_ranking", "run_simulation"],
            "additionalProperties": False,
            "properties": {
                "get_forecast": {"$ref": "#/$defs/get_forecast"},
                "get_risk_ranking": {"$ref": "#/$defs/get_risk_ranking"},
                "run_simulation": {"$ref": "#/$defs/run_simulation"},
            },
        }
    },
    "additionalProperties": False,
    "$defs": {
        "source": {"type": "string", "enum": ["xm", "sh"]},
        "get_forecast": {
            **_TOOL_ENVELOPE,
            "properties": {
                "description": {"type": "string"},
                "input": {
                    "type": "object",
                    "properties": {
                        "source": {"$ref": "#/$defs/source"},
                        "cell_ids": {
                            "oneOf": [
                                {"type": "array", "items": {"type": "string"}},
                                {"type": "null"},
                            ]
                        },
                        "time_bucket": {"oneOf": [{"type": "string"}, {"type": "null"}]},
                    },
                    "additionalProperties": False,
                },
                "output": {
                    "type": "object",
                    "required": ["source", "time_bucket", "forecasts", "confidence"],
                    "properties": {
                        "source": {"$ref": "#/$defs/source"},
                        "time_bucket": {"type": "string"},
                        "forecasts": {
                            "type": "array",
                            "items": {
                                "type": "object",
                                "required": ["cell_id", "time_bucket", "pred_out", "pred_in", "pred_net_flow"],
                                "properties": {
                                    "cell_id": {"type": "string"},
                                    "time_bucket": {"type": "string"},
                                    "pred_out": {"type": "number", "minimum": 0},
                                    "pred_in": {"type": "number", "minimum": 0},
                                    "pred_net_flow": {"type": "number"},
                                },
                                "additionalProperties": False,
                            },
                        },
                        "confidence": {
                            "type": "object",
                            "required": ["level", "basis", "mae_out", "mae_in"],
                            "properties": {
                                "level": {"type": "string"},
                                "basis": {"type": "string"},
                                "mae_out": {"type": "number", "minimum": 0},
                                "mae_in": {"type": "number", "minimum": 0},
                            },
                            "additionalProperties": False,
                        },
                    },
                    "additionalProperties": False,
                },
            },
            "additionalProperties": False,
        },
        "get_risk_ranking": {
            **_TOOL_ENVELOPE,
            "properties": {
                "description": {"type": "string"},
                "input": {
                    "type": "object",
                    "required": ["direction"],
                    "properties": {
                        "source": {"$ref": "#/$defs/source"},
                        "top_k": {"type": "integer", "minimum": 1, "maximum": 1000},
                        "direction": {"type": "string", "enum": ["replenish", "clear"]},
                    },
                    "additionalProperties": False,
                },
                "output": {
                    "type": "object",
                    "required": ["source", "direction", "time_bucket", "ranking"],
                    "properties": {
                        "source": {"$ref": "#/$defs/source"},
                        "direction": {"type": "string", "enum": ["replenish", "clear"]},
                        "time_bucket": {"type": "string"},
                        "ranking": {
                            "type": "array",
                            "items": {
                                "type": "object",
                                "required": [
                                    "rank", "cell_id", "score", "pred_net_flow",
                                    "pred_out", "pred_in", "explanation",
                                ],
                                "properties": {
                                    "rank": {"type": "integer", "minimum": 1},
                                    "cell_id": {"type": "string"},
                                    "score": {"type": "number"},
                                    "pred_net_flow": {"type": "number"},
                                    "pred_out": {"type": "number"},
                                    "pred_in": {"type": "number"},
                                    "explanation": {"type": "string", "minLength": 1},
                                },
                                "additionalProperties": False,
                            },
                        },
                    },
                    "additionalProperties": False,
                },
            },
            "additionalProperties": False,
        },
        "run_simulation": {
            **_TOOL_ENVELOPE,
            "properties": {
                "description": {"type": "string"},
                "input": {
                    "type": "object",
                    "required": ["strategy"],
                    "properties": {
                        "source": {"$ref": "#/$defs/source"},
                        "strategy": {"type": "string", "enum": ["rule", "optimized"]},
                        "budget": {"oneOf": [{"type": "integer", "minimum": 0}, {"type": "null"}]},
                    },
                    "additionalProperties": False,
                },
                "output": {
                    "type": "object",
                    "required": ["source", "strategy", "metrics", "trajectory", "simulated"],
                    "properties": {
                        "source": {"$ref": "#/$defs/source"},
                        "strategy": {"type": "string", "enum": ["rule", "optimized"]},
                        "simulated": {"type": "boolean"},
                        "metrics": {
                            "type": "object",
                            "required": [
                                "unmet_requests", "distance_bike_km",
                                "composite_cost", "service_rate",
                            ],
                            "properties": {
                                "unmet_requests": {"type": "integer", "minimum": 0},
                                "distance_bike_km": {"type": "number", "minimum": 0},
                                "composite_cost": {"type": "number", "minimum": 0},
                                "service_rate": {"type": "number", "minimum": 0, "maximum": 1},
                            },
                            "additionalProperties": False,
                        },
                        "trajectory": {
                            "type": "array",
                            "items": {
                                "type": "object",
                                "required": ["time_bucket", "borrow_requests", "unmet_requests", "inventory_end"],
                                "properties": {
                                    "time_bucket": {"type": "string"},
                                    "borrow_requests": {"type": "integer", "minimum": 0},
                                    "unmet_requests": {"type": "integer", "minimum": 0},
                                    "inventory_end": {"type": "integer", "minimum": 0},
                                },
                                "additionalProperties": False,
                            },
                        },
                    },
                    "additionalProperties": False,
                },
            },
            "additionalProperties": False,
        },
    },
}
