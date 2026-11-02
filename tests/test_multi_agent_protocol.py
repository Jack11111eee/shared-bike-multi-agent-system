"""F24: task schema, message protocol, role interfaces and permissions."""

from __future__ import annotations

import dataclasses

import pytest
from jsonschema import Draft202012Validator
from multi_agent_helpers import (
    FakeEvaluator,
    FakeOldTool,
    default_table,
    manifest,
    task,
)

from bike_agent.multi_agent.contracts import (
    ALL_SCHEMAS,
    MESSAGE_SCHEMA,
    TASK_SCHEMA,
    TERMINAL_STATES,
)
from bike_agent.multi_agent.gateway import MultiAgentGateway, PermissionDenied
from bike_agent.multi_agent.messages import MessageRejected, MessageStore
from bike_agent.multi_agent.roles import DispatchContext, DispatchRole
from bike_agent.multi_agent.task import TaskError, budget_options


# -- task -------------------------------------------------------------------
def test_every_schema_is_valid_draft_2020_12():
    for schema in ALL_SCHEMAS.values():
        Draft202012Validator.check_schema(schema)
    Draft202012Validator.check_schema(MESSAGE_SCHEMA)
    assert "additionalProperties" in TASK_SCHEMA

def test_budget_options_are_derived_from_the_baseline():
    assert budget_options(200, 200) == (100, 200)
    assert budget_options(200, 400) == (100, 200, 400)
    assert budget_options(0, 0) == (0,)
    assert budget_options(3, 3) == (1, 3)
    with pytest.raises(TaskError):
        budget_options(200, 100)

def test_default_task_does_not_offer_the_doubled_tier():
    spec_value = task()
    assert spec_value.baseline_budget == 200
    assert spec_value.max_budget == 200
    assert spec_value.budget_options == (100, 200)
    assert 400 not in spec_value.budget_options

def test_explicit_ceiling_offers_the_doubled_tier():
    spec_value = task(max_budget=400)
    assert spec_value.max_budget == 400
    assert spec_value.budget_options == (100, 200, 400)

@pytest.mark.parametrize(
    "kwargs",
    [
        {"max_budget": 50},
        {"min_service_rate": 1.5},
        {"min_service_rate": -0.1},
        {"max_rounds": 0},
        {"max_candidates_per_round": 3},
        {"max_tool_calls": 0},
        {"tool_timeout_s": 0},
        {"task_timeout_s": 0},
    ],
)
def test_invalid_task_inputs_are_rejected(kwargs):
    with pytest.raises((TaskError, ValueError)):
        task(**kwargs)

def test_unknown_source_is_rejected():
    with pytest.raises(TaskError):
        task(source="nope")

def test_task_schema_rejects_a_foreign_strategy():
    spec_value = dataclasses.replace(task(), allowed_strategies=("rule", "magic"))
    with pytest.raises(TaskError):
        spec_value.validate()

def test_task_schema_rejects_baseline_outside_budget_options():
    spec_value = dataclasses.replace(task(), budget_options=(100,))
    with pytest.raises(TaskError):
        spec_value.validate()

# -- messages ---------------------------------------------------------------
def _store(source: str = "xm") -> MessageStore:
    store = MessageStore("run-1", "task-1")
    store.bind_source(source)
    store.register_tool_call("tc-0001", source=source, tool="get_forecast")
    return store

def _assignment(store: MessageStore) -> dict:
    return store.send(
        sender="coordinator",
        recipient="diagnosis",
        type="task_assignment",
        round=0,
        payload={"instruction": "diagnose", "assignee": "diagnosis", "round": 0},
        parent_id=None,
    )

def _diagnosis_payload() -> dict:
    return {
        "focus_buckets": [
            {"time_bucket": "2020-12-25 08:00:00", "unmet_requests": 10, "rank": 1}
        ],
        "focus_cells": [
            {"cell_id": "C0001_0001", "reason": "synthetic", "evidence_kind": "possible_related"}
        ],
        "hypotheses": [],
        "evidence_gaps": [],
        "risk_ranking_scope": {
            "time_bucket": "2020-12-25 08:00:00",
            "covers_focus_bucket": True,
            "note": "synthetic",
        },
        "recommended": None,
    }

def test_valid_messages_are_accepted():
    store = _store()
    first = _assignment(store)
    second = store.send(
        sender="diagnosis",
        recipient="coordinator",
        type="diagnosis",
        round=0,
        payload=_diagnosis_payload(),
        parent_id=first["message_id"],
        evidence_refs=["tc-0001"],
    )
    assert [message["message_id"] for message in store.messages] == [
        first["message_id"],
        second["message_id"],
    ]
    assert second["evidence_refs"] == ["tc-0001"]
    assert store.rejections == []

def test_wrong_recipient_is_rejected():
    store = _store()
    first = _assignment(store)
    with pytest.raises(MessageRejected):
        store.send(
            sender="diagnosis",
            recipient="dispatch",
            type="diagnosis",
            round=0,
            payload=_diagnosis_payload(),
            parent_id=first["message_id"],
            evidence_refs=["tc-0001"],
        )
    assert len(store.rejections) == 1
    assert "wrong recipient" in store.rejections[0]["reason"]

def test_unknown_recipient_is_rejected():
    store = _store()
    with pytest.raises(MessageRejected):
        store.send(
            sender="coordinator",
            recipient="intruder",
            type="task_assignment",
            round=0,
            payload={"instruction": "diagnose", "assignee": "diagnosis", "round": 0},
            parent_id=None,
        )

def test_duplicate_message_id_is_rejected():
    store = _store()
    _assignment(store)
    with pytest.raises(MessageRejected):
        store.send(
            sender="coordinator",
            recipient="dispatch",
            type="task_assignment",
            round=0,
            payload={"instruction": "propose", "assignee": "dispatch", "round": 0},
            parent_id=None,
            message_id="msg-0001",
        )

def test_dangling_parent_is_rejected():
    store = _store()
    _assignment(store)
    with pytest.raises(MessageRejected):
        store.send(
            sender="diagnosis",
            recipient="coordinator",
            type="diagnosis",
            round=0,
            payload=_diagnosis_payload(),
            parent_id="msg-9999",
            evidence_refs=["tc-0001"],
        )

def test_dangling_evidence_reference_is_rejected():
    store = _store()
    first = _assignment(store)
    with pytest.raises(MessageRejected):
        store.send(
            sender="diagnosis",
            recipient="coordinator",
            type="diagnosis",
            round=0,
            payload=_diagnosis_payload(),
            parent_id=first["message_id"],
            evidence_refs=["tc-9999"],
        )

def test_cross_run_reference_is_rejected():
    store = _store()
    first = _assignment(store)
    with pytest.raises(MessageRejected):
        store.send(
            sender="diagnosis",
            recipient="coordinator",
            type="diagnosis",
            round=0,
            payload=_diagnosis_payload(),
            parent_id=first["message_id"],
            evidence_refs=["other-run:msg-0001"],
        )

def test_illegal_payload_is_rejected():
    store = _store()
    first = _assignment(store)
    with pytest.raises(MessageRejected):
        store.send(
            sender="diagnosis",
            recipient="coordinator",
            type="diagnosis",
            round=0,
            payload={"focus_buckets": "not-an-array"},
            parent_id=first["message_id"],
            evidence_refs=["tc-0001"],
        )

def test_expired_round_is_rejected():
    store = _store()
    _assignment(store)
    store.open_round(1)
    store.close_round(1)
    with pytest.raises(MessageRejected):
        store.send(
            sender="coordinator",
            recipient="dispatch",
            type="task_assignment",
            round=1,
            payload={"instruction": "propose", "assignee": "dispatch", "round": 1},
            parent_id=None,
        )

def test_future_round_is_rejected():
    store = _store()
    _assignment(store)
    with pytest.raises(MessageRejected):
        store.send(
            sender="coordinator",
            recipient="dispatch",
            type="task_assignment",
            round=2,
            payload={"instruction": "propose", "assignee": "dispatch", "round": 2},
            parent_id=None,
        )

def test_first_message_must_have_no_parent():
    store = _store()
    with pytest.raises(MessageRejected):
        store.send(
            sender="coordinator",
            recipient="diagnosis",
            type="task_assignment",
            round=0,
            payload={"instruction": "diagnose", "assignee": "diagnosis", "round": 0},
            parent_id="msg-0000",
        )

def test_cross_source_evidence_is_rejected():
    store = MessageStore("run-1", "task-1")
    store.bind_source("xm")
    store.register_tool_call("tc-0001", source="sh", tool="get_forecast")
    first = _assignment(store)
    with pytest.raises(MessageRejected):
        store.send(
            sender="diagnosis",
            recipient="coordinator",
            type="diagnosis",
            round=0,
            payload=_diagnosis_payload(),
            parent_id=first["message_id"],
            evidence_refs=["tc-0001"],
        )

# -- permissions ------------------------------------------------------------
def _gateway(monkeypatch, task_value=None):
    monkeypatch.setattr("bike_agent.multi_agent.gateway.verify_unchanged", lambda *args, **kwargs: None)
    evaluator = FakeEvaluator(default_table())
    old_tool = FakeOldTool()
    gateway = MultiAgentGateway(
        {}, None, task_value or task(), manifest(), None,
        evaluator=evaluator, old_tool=old_tool,
    )
    return gateway, evaluator, old_tool

def test_diagnosis_marks_a_risk_ranking_from_another_bucket_as_not_covering(monkeypatch):
    """F26-3: the ranking covers its own window; it is not a per-bucket list."""
    from bike_agent.multi_agent.roles import DiagnosisContext, DiagnosisRole

    monkeypatch.setattr("bike_agent.multi_agent.gateway.verify_unchanged", lambda *args, **kwargs: None)
    old_tool = FakeOldTool(ranking_bucket="2019-01-01 00:00:00")
    gateway = MultiAgentGateway(
        {}, None, task(), manifest(), None,
        evaluator=FakeEvaluator(default_table()), old_tool=old_tool,
    )
    baseline = {
        "result_id": "res-rule-200",
        "metrics": {"composite_cost": 100.0, "unmet_requests": 10, "distance_bike_km": 50.0, "service_rate": 0.9},
        "violations": {},
        "trajectory": [
            {"time_bucket": "2020-12-25 08:00:00", "unmet_requests": 10, "borrow_requests": 40},
            {"time_bucket": "2020-12-25 09:00:00", "unmet_requests": 4, "borrow_requests": 30},
        ],
    }
    output = DiagnosisRole().handle(
        DiagnosisContext(
            task=task(),
            round=0,
            baseline=baseline,
            baseline_tool_call_id="tc-0001",
        ),
        gateway,
    )
    scope = output["risk_ranking_scope"]
    assert scope["time_bucket"] == "2019-01-01 00:00:00"
    assert scope["covers_focus_bucket"] is False
    assert "不是关注时段" in scope["note"]
    # A forecast is not a ledger: no cell is claimed as a confirmed loss.
    assert output["focus_cells"]
    assert {cell["evidence_kind"] for cell in output["focus_cells"]} == {"possible_related"}
    assert any("预测" in gap or "非实际" in gap for gap in output["evidence_gaps"] + [cell["reason"] for cell in output["focus_cells"]])

def test_role_without_the_tool_is_denied_before_backend(monkeypatch):
    gateway, evaluator, old_tool = _gateway(monkeypatch)
    with pytest.raises(PermissionDenied):
        gateway.call("dispatch", "run_simulation", {"source": "xm", "strategy": "rule"})
    with pytest.raises(PermissionDenied):
        gateway.call("coordinator", "get_forecast", {"source": "xm"})
    with pytest.raises(PermissionDenied):
        gateway.call("diagnosis", "evaluate_dispatch_candidate", {"source": "xm", "strategy": "optimized", "budget": 200})
    assert old_tool.calls == []
    assert evaluator.calls == []
    assert gateway.attempt_count == 0
    assert len(gateway.denials) == 3

def test_illegal_parameters_are_denied_before_backend(monkeypatch):
    gateway, evaluator, old_tool = _gateway(monkeypatch)
    with pytest.raises(PermissionDenied):
        gateway.call("evaluation", "evaluate_dispatch_candidate", {"source": "xm", "strategy": "optimized", "budget": 999})
    with pytest.raises(PermissionDenied):
        gateway.call("evaluation", "evaluate_dispatch_candidate", {"source": "sh", "strategy": "optimized", "budget": 200})
    with pytest.raises(PermissionDenied):
        gateway.call("diagnosis", "get_forecast", {"source": "sh"})
    assert evaluator.calls == []
    assert old_tool.calls == []

def test_allowed_call_reaches_the_backend(monkeypatch):
    gateway, evaluator, _old_tool = _gateway(monkeypatch)
    result = gateway.call(
        "evaluation", "evaluate_dispatch_candidate",
        {"source": "xm", "strategy": "optimized", "budget": 200},
    )
    assert result.output["result_id"] == "res-optimized-200"
    assert evaluator.calls == [("optimized", 200)]
    assert gateway.backend_executions == 1

# -- role consumption -------------------------------------------------------
def _diagnosis_payload_for(bucket: str) -> dict:
    payload = _diagnosis_payload()
    payload["focus_buckets"][0]["time_bucket"] = bucket
    return payload

def test_replacing_diagnosis_changes_the_proposal():
    role = DispatchRole()
    task_value = task()
    baseline = {
        "result_id": "res-rule-200",
        "metrics": {"composite_cost": 100.0, "unmet_requests": 10, "distance_bike_km": 50.0, "service_rate": 0.9},
        "violations": {},
        "trajectory": [],
    }
    first = role.handle(
        DispatchContext(
            task=task_value,
            round=1,
            diagnosis=_diagnosis_payload_for("2020-12-25 08:00:00"),
            diagnosis_tool_call_ids=("tc-0001",),
            baseline=baseline,
            feedback=None,
            candidates_history=(),
            evaluated_keys=frozenset(),
        )
    )
    second = role.handle(
        DispatchContext(
            task=task_value,
            round=1,
            diagnosis=_diagnosis_payload_for("2020-12-26 09:00:00"),
            diagnosis_tool_call_ids=("tc-0001",),
            baseline=baseline,
            feedback=None,
            candidates_history=(),
            evaluated_keys=frozenset(),
        )
    )
    assert first["candidates"][0]["rationale"] != second["candidates"][0]["rationale"]
    assert "2020-12-25 08:00:00" in first["candidates"][0]["rationale"]
    assert "2020-12-26 09:00:00" in second["candidates"][0]["rationale"]

def test_every_role_overrides_handle():
    """F24-2: one interface for all four roles, not four ad-hoc entry points."""
    from bike_agent.multi_agent.roles import (
        CoordinatorRole,
        DiagnosisRole,
        DispatchRole,
        EvaluationRole,
    )

    for role_class in (CoordinatorRole, DiagnosisRole, DispatchRole, EvaluationRole):
        assert "handle" in role_class.__dict__, f"{role_class.__name__} does not implement handle"

def test_terminal_states_are_the_required_five():
    assert set(TERMINAL_STATES) == {
        "accepted",
        "baseline_retained",
        "no_feasible_candidate",
        "failed",
        "limit_reached",
    }
