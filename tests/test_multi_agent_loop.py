"""F25 / F29: the bounded loop, feedback revision, ablation and failure matrix."""

from __future__ import annotations

import json
import time
from pathlib import Path

import pytest
from multi_agent_helpers import (
    FakeEvaluator,
    FakeOldTool,
    default_table,
    manifest,
    spec,
    task,
)

from bike_agent.config import Layout, load_config
from bike_agent.multi_agent.gateway import ToolTimeout
from bike_agent.multi_agent.orchestrator import Orchestrator
from bike_agent.multi_agent.roles import (
    DiagnosisRole,
    DispatchRole,
    EvaluationRole,
)


def _run(
    tmp_path: Path,
    monkeypatch,
    task_value,
    *,
    table=None,
    roles=None,
    old_tool=None,
    clock=None,
    run_id=None,
    output_root=None,
):
    manifest_value = manifest()
    monkeypatch.setattr(
        "bike_agent.multi_agent.orchestrator.freeze_inputs",
        lambda *args, **kwargs: manifest_value,
    )
    monkeypatch.setattr(
        "bike_agent.multi_agent.gateway.verify_unchanged",
        lambda *args, **kwargs: None,
    )
    evaluator = FakeEvaluator(table or default_table())
    old = old_tool or FakeOldTool()
    orchestrator = Orchestrator(
        load_config(),
        Layout(tmp_path),
        task_value,
        run_id=run_id,
        output_root=output_root or (tmp_path / "runs"),
        roles=roles,
        clock=clock,
        gateway_options={"evaluator": evaluator, "old_tool": old},
    )
    return orchestrator.run(), evaluator, old

def _read(path: Path):
    return json.loads(path.read_text(encoding="utf-8"))

def _messages(outcome) -> list[dict]:
    return [
        json.loads(line)
        for line in (outcome.run_dir / "messages.jsonl").read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]

# -- normal path ------------------------------------------------------------
def test_normal_path_accepts_a_strictly_better_candidate(tmp_path, monkeypatch):
    outcome, evaluator, old = _run(tmp_path, monkeypatch, task())
    assert outcome.exit_code == 0
    decision = outcome.decision
    assert decision["terminal_state"] == "accepted"
    assert decision["selected"]["candidate_id"] == "cand-r1-1"
    assert decision["selected"]["strategy"] == "optimized"
    assert decision["selected"]["deltas"]["cost_delta"] == pytest.approx(-10.0)
    assert decision["selected"]["deltas"]["unmet_gain"] == 2
    assert decision["baseline"]["feasible"] is True
    assert evaluator.calls == [("rule", 200), ("optimized", 200)]
    assert old.calls and [call[0] for call in old.calls] == ["get_forecast", "get_risk_ranking"]
    assert [message["type"] for message in _messages(outcome)] == [
        "task_assignment",
        "evaluation",
        "task_assignment",
        "diagnosis",
        "task_assignment",
        "proposal",
        "task_assignment",
        "evaluation",
        "final",
    ]
    for name in (
        "task.json",
        "input_manifest.json",
        "messages.jsonl",
        "tool_calls.jsonl",
        "candidates.json",
        "evaluations.json",
        "decision.json",
        "report.md",
        "run.json",
    ):
        assert (outcome.run_dir / name).exists(), name
    index = _read(outcome.run_dir / "run.json")
    assert index["run_id"] == outcome.run_id
    assert index["terminal_state"] == "accepted"
    assert index["rule_based"] is True
    assert index["llm_connected"] is False
    assert decision["limits"]["tool_calls_used"] == 4

def test_timing_separates_backend_simulation_from_scheduling(tmp_path, monkeypatch):
    """NF-39: the run's cost is not one number standing in for "agent speed"."""
    outcome, _, _ = _run(tmp_path, monkeypatch, task())
    timing = outcome.decision["timing"]
    assert timing["task_elapsed_ms"] >= 0
    # The fake backend is instant, so these are >= 0 rather than > 0: what is
    # being tested is that the split exists and is arithmetically closed.
    assert timing["backend_simulation_ms"] >= 0
    assert timing["diagnosis_tool_ms"] >= 0
    assert timing["scheduling_overhead_ms"] >= 0
    parts = (
        timing["backend_simulation_ms"]
        + timing["diagnosis_tool_ms"]
        + timing["scheduling_overhead_ms"]
    )
    assert parts == pytest.approx(timing["task_elapsed_ms"], abs=0.01)
    index = _read(outcome.run_dir / "run.json")
    assert index["timing"] == timing
    assert "耗时拆分" in (outcome.run_dir / "report.md").read_text(encoding="utf-8")

def test_no_improvement_retains_the_baseline(tmp_path, monkeypatch):
    table = default_table()
    table[("optimized", 200)] = spec(cost=110.0, unmet=12, rate=0.88, distance=70.0)
    outcome, _, _ = _run(tmp_path, monkeypatch, task(), table=table)
    assert outcome.exit_code == 0
    assert outcome.decision["terminal_state"] == "baseline_retained"
    assert outcome.decision["selected"]["kind"] == "baseline"
    assert "保留基准" in outcome.decision["reason"]

def test_a_tie_is_not_reported_as_an_optimization_win(tmp_path, monkeypatch):
    table = default_table()
    table[("optimized", 200)] = spec(cost=100.0, unmet=10, rate=0.90, distance=50.0)
    outcome, _, _ = _run(tmp_path, monkeypatch, task(), table=table)
    assert outcome.decision["terminal_state"] == "baseline_retained"
    assert outcome.decision["selected"]["kind"] == "baseline"

# -- revision ---------------------------------------------------------------
def test_rejection_changes_the_budget_and_reevaluates(tmp_path, monkeypatch):
    table = {
        ("rule", 200): spec(cost=100.0, unmet=10, rate=0.90, distance=50.0),
        ("optimized", 200): spec(cost=90.0, unmet=8, rate=0.90, distance=60.0),
        ("optimized", 400): spec(cost=80.0, unmet=6, rate=0.96, distance=70.0),
    }
    task_value = task(max_budget=400, min_service_rate=0.95, max_candidates_per_round=1)
    outcome, evaluator, _ = _run(tmp_path, monkeypatch, task_value, table=table)
    messages = _messages(outcome)
    revision_request = next(message for message in messages if message["type"] == "revision_request")
    revision_proposal = next(message for message in messages if message["type"] == "revision_proposal")
    assert revision_request["payload"]["candidate_id"] == "cand-r1-1"
    assert revision_request["payload"]["suggested_revision"]["budget"] == 400
    assert revision_proposal["payload"]["changed_fields"] == ["budget"]
    assert revision_proposal["payload"]["candidates"][0]["revision_of"] == "cand-r1-1"
    assert revision_proposal["payload"]["candidates"][0]["budget"] == 400
    assert outcome.decision["terminal_state"] == "accepted"
    assert outcome.decision["selected"]["budget"] == 400
    assert evaluator.calls == [("rule", 200), ("optimized", 200), ("optimized", 400)]

def test_rejection_changes_the_strategy_and_reevaluates(tmp_path, monkeypatch):
    table = {
        ("rule", 200): spec(cost=100.0, unmet=10, rate=0.90, distance=50.0),
        ("optimized", 200): spec(cost=90.0, unmet=8, rate=0.90, distance=60.0),
        ("optimized", 400): spec(cost=80.0, unmet=6, rate=0.90, distance=70.0),
        ("rule", 400): spec(cost=85.0, unmet=7, rate=0.96, distance=65.0),
    }
    task_value = task(max_budget=400, min_service_rate=0.95, max_candidates_per_round=2)
    outcome, _, _ = _run(tmp_path, monkeypatch, task_value, table=table)
    messages = _messages(outcome)
    revision_request = next(message for message in messages if message["type"] == "revision_request")
    revision_proposal = next(message for message in messages if message["type"] == "revision_proposal")
    # The coordinator picks the rejection whose suggestion is still actionable.
    assert revision_request["payload"]["candidate_id"] == "cand-r1-2"
    assert revision_request["payload"]["suggested_revision"]["strategy"] == "rule"
    assert revision_proposal["payload"]["changed_fields"] == ["strategy"]
    assert revision_proposal["payload"]["candidates"][0]["strategy"] == "rule"
    assert revision_proposal["payload"]["candidates"][0]["budget"] == 400
    assert outcome.decision["terminal_state"] == "accepted"
    assert outcome.decision["selected"]["strategy"] == "rule"

def test_revision_fields_are_caused_by_the_previous_feedback(tmp_path, monkeypatch):
    table = {
        ("rule", 200): spec(cost=100.0, unmet=10, rate=0.90, distance=50.0),
        ("optimized", 200): spec(cost=90.0, unmet=8, rate=0.90, distance=60.0),
        ("optimized", 400): spec(cost=80.0, unmet=6, rate=0.90, distance=70.0),
        ("rule", 400): spec(cost=85.0, unmet=7, rate=0.96, distance=65.0),
    }
    task_value = task(max_budget=400, min_service_rate=0.95, max_candidates_per_round=2)
    outcome, _, _ = _run(tmp_path, monkeypatch, task_value, table=table)
    messages = _messages(outcome)
    evaluation = next(
        message
        for message in messages
        if message["type"] == "evaluation" and message["round"] == 1
    )
    rejected = next(
        item for item in evaluation["payload"]["evaluations"] if item["candidate_id"] == "cand-r1-2"
    )
    revision_request = next(message for message in messages if message["type"] == "revision_request")
    revision_proposal = next(message for message in messages if message["type"] == "revision_proposal")
    assert revision_request["payload"]["defects"] == rejected["defects"]
    assert revision_request["payload"]["suggested_revision"] == rejected["suggested_revision"]
    assert revision_proposal["payload"]["feedback_response"]["defects"] == rejected["defects"]
    assert revision_proposal["payload"]["changed_fields"] == ["strategy"]

# -- ablation and sign-off --------------------------------------------------
def test_removing_evaluation_never_produces_accepted(tmp_path, monkeypatch):
    outcome, _, _ = _run(
        tmp_path, monkeypatch, task(), roles={"evaluation": None}
    )
    assert outcome.exit_code == 1
    assert outcome.decision["terminal_state"] == "failed"
    assert outcome.decision["selected"] is None
    assert all(message["type"] != "final" or message["payload"]["terminal_state"] != "accepted" for message in _messages(outcome))

class LyingEvaluation(EvaluationRole):
    """Reports success while the tool output says the constraints failed."""

    def handle(self, context, gateway=None):
        payload = super().handle(context, gateway)
        item = payload["evaluations"][0]
        item["feasible"] = True
        item["violations_ok"] = True
        item["service_rate_ok"] = True
        item["violations"] = {
            "conservation": 0,
            "capacity": 0,
            "budget": 0,
            "negative_inventory": 0,
            "clamped_moves": 0,
        }
        item["verdict"] = "accept"
        item["defects"] = []
        return payload

def test_coordinator_refuses_a_self_reported_pass_without_evidence(tmp_path, monkeypatch):
    table = default_table()
    table[("optimized", 200)] = spec(
        cost=90.0,
        unmet=8,
        rate=0.92,
        distance=60.0,
        violations={"conservation": 3, "capacity": 0, "budget": 0, "negative_inventory": 0, "clamped_moves": 0},
    )
    outcome, _, _ = _run(
        tmp_path, monkeypatch, task(), table=table, roles={"evaluation": LyingEvaluation()}
    )
    assert outcome.decision["terminal_state"] == "baseline_retained"
    assert outcome.decision["selected"]["kind"] == "baseline"

class ForgedResult(EvaluationRole):
    """Cites a result id that no tool call in this run produced."""

    def handle(self, context, gateway=None):
        payload = super().handle(context, gateway)
        item = payload["evaluations"][0]
        item["result_id"] = "res-forged"
        item["tool_call_id"] = "tc-9999"
        return payload

def test_coordinator_refuses_a_forged_result_id(tmp_path, monkeypatch):
    outcome, _, _ = _run(
        tmp_path, monkeypatch, task(), roles={"evaluation": ForgedResult()}
    )
    assert outcome.decision["terminal_state"] != "accepted"

# -- limits -----------------------------------------------------------------
def test_round_limit_stops_with_limit_reached(tmp_path, monkeypatch):
    table = {
        ("rule", 200): spec(cost=100.0, unmet=10, rate=0.90, distance=50.0),
        ("optimized", 200): spec(cost=90.0, unmet=8, rate=0.90, distance=60.0),
    }
    task_value = task(max_rounds=1, max_candidates_per_round=1, min_service_rate=0.95)
    outcome, _, _ = _run(tmp_path, monkeypatch, task_value, table=table)
    assert outcome.exit_code == 1
    assert outcome.decision["terminal_state"] == "limit_reached"
    assert outcome.decision["selected"] is None

def test_tool_call_limit_stops_before_the_next_backend(tmp_path, monkeypatch):
    task_value = task(max_tool_calls=1)
    outcome, evaluator, old = _run(tmp_path, monkeypatch, task_value)
    assert outcome.exit_code == 1
    assert outcome.decision["terminal_state"] == "limit_reached"
    assert evaluator.calls == [("rule", 200)]
    assert old.calls == []
    assert outcome.decision["limits"]["tool_calls_used"] == 1

def test_task_deadline_stops_the_run(tmp_path, monkeypatch):
    ticks = iter([0.0, 1.0, 2.0, 3.0, 4.0, 5.0])
    task_value = task(task_timeout_s=0.001)
    outcome, evaluator, _ = _run(
        tmp_path, monkeypatch, task_value, clock=lambda: next(ticks, 100.0)
    )
    assert outcome.exit_code == 1
    assert outcome.decision["terminal_state"] == "limit_reached"
    assert evaluator.calls == []

# -- determinism ------------------------------------------------------------
def test_deterministic_mode_repeats_the_decision(tmp_path, monkeypatch):
    first, _, _ = _run(tmp_path / "a", monkeypatch, task())
    second, _, _ = _run(tmp_path / "b", monkeypatch, task())
    keys = ("terminal_state", "reason", "selected", "baseline", "ranking")
    assert {key: first.decision[key] for key in keys} == {key: second.decision[key] for key in keys}
    assert _read(first.run_dir / "candidates.json") == _read(second.run_dir / "candidates.json")
    def comparable(message):
        payload = dict(message["payload"])
        if message["type"] == "final":
            payload.pop("limits", None)  # timing metadata is allowed to differ
        return (message["sender"], message["recipient"], message["type"], message["round"], payload)

    first_messages = [comparable(m) for m in _messages(first)]
    second_messages = [comparable(m) for m in _messages(second)]
    assert first_messages == second_messages

def test_run_directories_are_never_overwritten(tmp_path, monkeypatch):
    first, _, _ = _run(tmp_path, monkeypatch, task(), run_id="fixed")
    second, _, _ = _run(tmp_path, monkeypatch, task(), run_id="fixed")
    assert first.run_id == "fixed"
    assert second.run_id == "fixed-2"
    assert first.run_dir != second.run_dir
    assert (first.run_dir / "decision.json").exists()
    assert (second.run_dir / "decision.json").exists()

# -- failure matrix ---------------------------------------------------------
class InvalidDispatch(DispatchRole):
    def handle(self, context, gateway=None):
        payload = super().handle(context, gateway)
        payload["candidates"] = [
            {
                "candidate_id": "cand-r1-1",
                "strategy": "magic",
                "budget": 200,
                "rationale": "invalid",
                "expected_tradeoff": "invalid",
                "revision_of": None,
            }
        ]
        return payload

def test_invalid_strategy_fails_the_run(tmp_path, monkeypatch):
    outcome, _, _ = _run(
        tmp_path, monkeypatch, task(), roles={"dispatch": InvalidDispatch()}
    )
    assert outcome.exit_code == 1
    assert outcome.decision["terminal_state"] == "failed"
    assert "任务契约" in outcome.decision["reason"]

class IllegalBudgetDispatch(DispatchRole):
    def handle(self, context, gateway=None):
        payload = super().handle(context, gateway)
        payload["candidates"] = [
            {
                "candidate_id": "cand-r1-1",
                "strategy": "optimized",
                "budget": 999,
                "rationale": "budget outside the legal tiers",
                "expected_tradeoff": "invalid",
                "revision_of": None,
            }
        ]
        return payload

def test_proposal_level_illegal_budget_fails_before_any_evaluation(tmp_path, monkeypatch):
    """F29-4: the proposal layer rejects an illegal tier, not only the gateway."""
    outcome, evaluator, _ = _run(
        tmp_path, monkeypatch, task(), roles={"dispatch": IllegalBudgetDispatch()}
    )
    assert outcome.exit_code == 1
    assert outcome.decision["terminal_state"] == "failed"
    assert "budget" in outcome.decision["reason"]
    # Only the baseline was evaluated; the illegal candidate never reached the engine.
    assert evaluator.calls == [("rule", 200)]
    assert outcome.decision["candidate_count"] == 0

class DuplicateDispatch(DispatchRole):
    def handle(self, context, gateway=None):
        payload = super().handle(context, gateway)
        payload["candidates"] = [payload["candidates"][0], dict(payload["candidates"][0])]
        return payload

def test_duplicate_candidates_are_dropped_and_audited(tmp_path, monkeypatch):
    outcome, evaluator, _ = _run(
        tmp_path, monkeypatch, task(), roles={"dispatch": DuplicateDispatch()}
    )
    assert evaluator.calls == [("rule", 200), ("optimized", 200)]
    events = [
        json.loads(line)
        for line in (outcome.run_dir / "events.jsonl").read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]
    assert any(event["kind"] == "duplicate_candidate" for event in events)

class NoRevisionDispatch(DispatchRole):
    def handle(self, context, gateway=None):
        if context.feedback is None:
            return super().handle(context, gateway)
        return {
            "candidates": [],
            "changed_fields": [],
            "feedback_response": {
                "candidate_id": context.feedback["candidate_id"],
                "cannot_change": True,
                "explanation": "no legal alternative in this fixture",
            },
        }

def test_no_feasible_candidate_is_a_normal_terminal_state(tmp_path, monkeypatch):
    table = {
        ("rule", 200): spec(cost=100.0, unmet=10, rate=0.90, distance=50.0),
        ("optimized", 200): spec(cost=90.0, unmet=8, rate=0.90, distance=60.0),
    }
    task_value = task(min_service_rate=0.95)
    outcome, _, _ = _run(
        tmp_path,
        monkeypatch,
        task_value,
        table=table,
        roles={"dispatch": NoRevisionDispatch()},
    )
    assert outcome.exit_code == 0
    assert outcome.decision["terminal_state"] == "no_feasible_candidate"
    assert outcome.decision["selected"] is None

def test_baseline_retained_always_selects_the_baseline(tmp_path, monkeypatch):
    """A feasible baseline with nothing better must be the selection.

    ``_no_candidate_decision`` used to return ``selected=None``; the shipped
    checker rejects that, and the page then read "无（未选择候选）" for a run
    whose correct answer was "keep the baseline".
    """
    table = {
        ("rule", 200): spec(cost=100.0, unmet=10, rate=0.90, distance=50.0),
        ("optimized", 200): spec(cost=90.0, unmet=8, rate=0.85, distance=60.0),
    }
    outcome, _, _ = _run(
        tmp_path,
        monkeypatch,
        task(min_service_rate=0.89),
        table=table,
        roles={"dispatch": NoRevisionDispatch()},
    )
    assert outcome.exit_code == 0
    assert outcome.decision["terminal_state"] == "baseline_retained"
    selected = outcome.decision["selected"]
    assert selected is not None
    assert selected["kind"] == "baseline"
    assert selected["strategy"] == "rule"
    assert selected["budget"] == 200
    assert selected["result_id"] == outcome.decision["baseline"]["result_id"]

def test_backend_failure_is_audited_and_fails_the_run(tmp_path, monkeypatch):
    table = {("rule", 200): spec(cost=100.0, unmet=10, rate=0.90, distance=50.0)}
    outcome, evaluator, _ = _run(tmp_path, monkeypatch, task(), table=table)
    assert outcome.exit_code == 1
    assert outcome.decision["terminal_state"] == "failed"
    events = [
        json.loads(line)
        for line in (outcome.run_dir / "events.jsonl").read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]
    assert any(event["kind"] == "tool_failure" for event in events)
    assert any(event["kind"] == "retry" for event in events)
    assert evaluator.calls == [("rule", 200), ("optimized", 200), ("optimized", 200)]

class CancellingEvaluator(FakeEvaluator):
    """Raises the same exception Ctrl-C produces, once."""

    def __init__(self, table, *, cancel_after: int = 0):
        super().__init__(table)
        self.cancel_after = cancel_after

    def __call__(self, *args, **kwargs):
        if len(self.calls) >= self.cancel_after:
            raise KeyboardInterrupt
        return super().__call__(*args, **kwargs)

def test_cancellation_writes_a_decision_and_stops_calling(tmp_path, monkeypatch):
    """F25-3: cancellation is a bounded stop with a recorded terminal state."""
    # ``_run`` builds its own evaluator; construct the run directly instead.
    manifest_value = manifest()
    monkeypatch.setattr(
        "bike_agent.multi_agent.orchestrator.freeze_inputs",
        lambda *args, **kwargs: manifest_value,
    )
    monkeypatch.setattr(
        "bike_agent.multi_agent.gateway.verify_unchanged", lambda *args, **kwargs: None
    )
    evaluator = CancellingEvaluator(default_table())
    orchestrator = Orchestrator(
        load_config(),
        Layout(tmp_path),
        task(),
        run_id="cancelled",
        output_root=tmp_path / "runs",
        gateway_options={"evaluator": evaluator, "old_tool": FakeOldTool()},
    )
    result = orchestrator.run()
    assert result.exit_code == 1
    assert result.decision["terminal_state"] == "failed"
    assert "取消" in result.decision["reason"]
    assert evaluator.calls == []
    assert result.decision["limits"]["rounds_used"] == 0
    for name in ("decision.json", "messages.jsonl", "run.json", "report.md"):
        assert (result.run_dir / name).exists(), name
    messages = _messages(result)
    assert messages[-1]["type"] == "final"
    assert messages[-1]["payload"]["terminal_state"] == "failed"

def test_replacing_the_diagnosis_role_changes_the_dispatch_rationale(tmp_path, monkeypatch):
    """F29-3: a replacement DiagnosisRole must change the proposal, not the code.

    The protocol test above shows that a *different diagnosis payload* moves the
    dispatch rationale.  This one injects an actual replacement role into the
    orchestrator, so "replaceable" is proven end to end rather than by analogy.
    """
    class OtherDiagnosis(DiagnosisRole):
        name = "diagnosis"

        def handle(self, context, gateway=None):
            return {
                "focus_buckets": [
                    {"time_bucket": "2099-01-01 00:00:00", "unmet_requests": 4242, "rank": 1}
                ],
                "focus_cells": [],
                "hypotheses": [],
                "evidence_gaps": ["替换角色的合成诊断。"],
                "risk_ranking_scope": {
                    "time_bucket": "",
                    "covers_focus_bucket": False,
                    "note": "替换角色不查询风险排名。",
                },
                "recommended": None,
            }

    default_outcome, _, _ = _run(tmp_path / "default", monkeypatch, task())
    replaced_outcome, _, _ = _run(
        tmp_path / "replaced", monkeypatch, task(), roles={"diagnosis": OtherDiagnosis()}
    )

    def first_rationale(outcome):
        proposals = [
            message
            for message in _messages(outcome)
            if message["type"] in {"proposal", "revision_proposal"}
        ]
        return proposals[0]["payload"]["candidates"][0]["rationale"]

    default_text = first_rationale(default_outcome)
    replaced_text = first_rationale(replaced_outcome)
    assert default_text != replaced_text
    assert "2099-01-01 00:00:00" in replaced_text
    assert "2099-01-01 00:00:00" not in default_text

    # The other roles were not touched: the same classes still ran.
    assert type(OtherDiagnosis()).__mro__[1] is DiagnosisRole
    diagnosis_messages = [
        message for message in _messages(replaced_outcome) if message["type"] == "diagnosis"
    ]
    assert diagnosis_messages[0]["payload"]["focus_buckets"][0]["time_bucket"] == "2099-01-01 00:00:00"

def test_run_report_discloses_the_distance_change_against_the_baseline(tmp_path, monkeypatch):
    """F29-1 / §2.3: a budget change buys distance too; the run must say so.

    ``default_table`` keeps the baseline at 50 bike-km and gives the accepted
    candidate 60; the report has to print that +10 rather than cost alone.
    """
    outcome, _, _ = _run(tmp_path, monkeypatch, task())
    report = (outcome.run_dir / "report.md").read_text(encoding="utf-8")
    assert "搬运距离：50.000000 bike-km" in report
    assert "基准可行：是" in report
    assert "距离 (bike-km)" in report
    assert "60.000000 (+10.00)" in report
    decision = outcome.decision
    assert decision["selected"]["metrics"]["distance_bike_km"] == 60.0
    assert decision["baseline"]["metrics"]["distance_bike_km"] == 50.0

def test_cancelling_mid_round_reports_the_round_that_was_open(tmp_path, monkeypatch):
    """F25-3 honesty: a round that ran before Ctrl-C must not report 0 rounds."""
    manifest_value = manifest()
    monkeypatch.setattr(
        "bike_agent.multi_agent.orchestrator.freeze_inputs",
        lambda *args, **kwargs: manifest_value,
    )
    monkeypatch.setattr(
        "bike_agent.multi_agent.gateway.verify_unchanged", lambda *args, **kwargs: None
    )
    # Baseline succeeds; the first candidate evaluation of round 1 is cancelled.
    evaluator = CancellingEvaluator(default_table(), cancel_after=1)
    orchestrator = Orchestrator(
        load_config(),
        Layout(tmp_path),
        task(),
        run_id="cancelled-mid-round",
        output_root=tmp_path / "runs",
        gateway_options={"evaluator": evaluator, "old_tool": FakeOldTool()},
    )
    result = orchestrator.run()
    assert result.exit_code == 1
    assert result.decision["terminal_state"] == "failed"
    assert evaluator.calls == [("rule", 200)]
    assert result.decision["limits"]["rounds_used"] == 1
    messages = _messages(result)
    assert messages[-1]["type"] == "final"
    # The round's own messages survive; the final message is not the only one.
    assert any(message["round"] >= 1 for message in messages[:-1])

def test_a_failed_run_still_ends_with_a_parented_final_message(tmp_path, monkeypatch):
    """F27-2: failure preserves what happened, including the terminal message."""
    table = {("rule", 200): spec(cost=100.0, unmet=10, rate=0.90, distance=50.0)}
    outcome, _, _ = _run(tmp_path, monkeypatch, task(), table=table)
    messages = _messages(outcome)
    assert messages[-1]["type"] == "final"
    assert messages[-1]["parent_id"] == messages[-2]["message_id"]
    assert messages[-1]["payload"]["terminal_state"] == "failed"
    rejections = [
        json.loads(line)
        for line in (outcome.run_dir / "rejections.jsonl").read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]
    assert not [item for item in rejections if "parent_id = null" in item.get("reason", "")]

def test_per_tool_timeout_is_enforced(monkeypatch):
    from bike_agent.multi_agent.gateway import MultiAgentGateway

    monkeypatch.setattr("bike_agent.multi_agent.gateway.verify_unchanged", lambda *args, **kwargs: None)

    def slow(config, layout, task_spec, manifest_value, *, strategy, budget):
        time.sleep(0.2)
        raise AssertionError("should have timed out")

    gateway = MultiAgentGateway(
        {}, None, task(), manifest(), None,
        tool_timeout_s=0.01, evaluator=slow, old_tool=FakeOldTool(),
    )
    with pytest.raises(ToolTimeout):
        gateway.call(
            "evaluation", "evaluate_dispatch_candidate",
            {"source": "xm", "strategy": "optimized", "budget": 200},
        )
    assert gateway.attempt_count == 2

def test_cache_is_not_reused_after_an_input_change(monkeypatch):
    from bike_agent.multi_agent.gateway import MultiAgentGateway
    from bike_agent.multi_agent.task import InputChangedError

    calls = {"count": 0}

    def verify(*args, **kwargs):
        calls["count"] += 1
        # First call: pre-cache, pre-execute, post-execute (1, 2, 3).
        # Second call: the pre-cache check is 4 and must stop before a hit.
        if calls["count"] >= 4:
            raise InputChangedError("frozen input changed")

    monkeypatch.setattr("bike_agent.multi_agent.gateway.verify_unchanged", verify)
    evaluator = FakeEvaluator(default_table())
    gateway = MultiAgentGateway(
        {}, None, task(), manifest(), None, evaluator=evaluator, old_tool=FakeOldTool()
    )
    gateway.call(
        "evaluation", "evaluate_dispatch_candidate",
        {"source": "xm", "strategy": "optimized", "budget": 200},
    )
    with pytest.raises(InputChangedError):
        gateway.call(
            "evaluation", "evaluate_dispatch_candidate",
            {"source": "xm", "strategy": "optimized", "budget": 200},
        )
    assert evaluator.calls == [("optimized", 200)]
