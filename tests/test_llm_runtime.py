"""Critical v2 behavior, using explicitly labelled test providers and real tools."""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from bike_agent.config import Layout, load_config
from bike_agent.llm.provider import ModelReply
from bike_agent.multi_agent.runtime import LLMRuntime
from bike_agent.multi_agent.task import build_task
from bike_agent.presentation.console import publish_run
from bike_agent.presentation.run_view import build_run_view, list_runs


class FixtureProvider:
    name, model, is_real = "test_fixture", "scripted-test-only", False

    def __init__(self, *, lie=False, cancel=False, bad_refs=False):
        self.count = 0
        self.lie, self.cancel, self.bad_refs = lie, cancel, bad_refs

    def complete(self, *, messages, tools, timeout):
        self.count += 1
        if self.cancel and self.count == 4:
            raise KeyboardInterrupt()
        context = json.loads(messages[1]["content"])
        names = {t["function"]["name"] for t in tools}
        results = [json.loads(m["content"]) for m in messages if m["role"] == "tool"]
        tool_results = [r for r in results if "tool_call_id" in r]
        refs = context["evidence_ids"]
        if "get_forecast" in names:
            if not tool_results:
                return self.reply("get_forecast", {"time_bucket": context["available_buckets"][0]})
            output = {"summary": "预测信号只能支持可能相关的判断。",
                      "focus_buckets": context["available_buckets"][:1],
                      "focus_cells": context["available_cells"][:1], "claims": [],
                      "evidence_gaps": ["没有格级账本工具"],
                      "evidence_refs": [tool_results[0]["tool_call_id"]]}
        elif "evaluate_dispatch_candidate" in names:
            pending = context["pending_candidate_ids"]
            done = {r["candidate_id"] for r in tool_results}
            missing = [cid for cid in pending if cid not in done]
            if missing:
                return self.reply("evaluate_dispatch_candidate", {"candidate_id": missing[0]})
            output = {
                "summary": "根据实际仿真结果评审可行性。",
                "assessments": [{
                    "candidate_id": r["candidate_id"], "result_id": r["result_id"],
                    "verdict": "feasible" if (r["feasible"] or self.lie) else "infeasible",
                    "explanation": "以工具约束检查为准。",
                    "recommended_action": "accept" if (r["feasible"] or self.lie) else "revise",
                } for r in tool_results],
                "evidence_refs": [r["tool_call_id"] for r in tool_results],
            }
        elif "allowed_actions" in context and context["allowed_actions"]:
            options = context["allowed_actions"]
            final_ids = context["finalizable_candidate_ids"]
            finalize = next((a for a in options if a["action"] == "finalize"), None)
            revision = next((a for a in options if a["action"] == "request_revision"), None)
            if finalize and final_ids != [None]:
                action, cid = finalize, final_ids[0]
            elif revision:
                action, cid = revision, [c["candidate_id"] for c in context["candidates"] if c["result"]][-1]
                refs = [next(c["result"]["tool_call_id"] for c in context["candidates"] if c["candidate_id"] == cid)]
            else:
                action, cid = options[0], None
            output = {**action, "candidate_id": cid, "reason": "按事实选择合法下一步。",
                      "evidence_refs": refs[:1]}
            if action["action"] == "request_revision":
                output["revision_fields"] = ["strategy"]
        else:
            combinations = context["available_combinations"]
            original = next((c for c in context["candidates"] if c["candidate_id"] == context["revision_of"]), None)
            if original:
                chosen = [next(c for c in combinations if c["strategy"] == "rule" and c["budget"] == 400)]
                changed = [k for k in ("strategy", "budget") if chosen[0][k] != original[k]]
            else:
                chosen = [c for c in combinations if c["strategy"] == "optimized" and c["budget"] in {200,400}][:2]
                changed = []
            output = {
                "summary": "提出合法候选，实际效果由工具验证。",
                "candidates": [{**c, "rationale": "测试候选", "expected_tradeoff": "资源和服务存在取舍。"} for c in chosen],
                "revision_of": context["revision_of"], "changed_fields": changed,
                "evidence_refs": refs[:1],
            }
        if self.bad_refs:
            output["evidence_refs"] = ["unknown-other-run"]
        return self.reply("submit_role_output", output)

    def reply(self, name, args):
        return ModelReply(
            message={"role": "assistant", "content": None, "tool_calls": [{
                "id": "fixture-call-" + str(self.count), "type": "function",
                "function": {"name": name, "arguments": json.dumps(args, ensure_ascii=False)},
            }]}, model=self.model, request_id="test-only", usage=None, finish_reason="tool_calls")


@pytest.fixture
def llm_env(tmp_path):
    source = Layout()
    if not source.forecast_path("xm").is_file():
        pytest.skip("requires prepared XM data")
    layout = Layout(tmp_path)
    layout.ensure()
    # Read-only links to the existing prepared public dataset, never change it.
    for file in source.processed.iterdir():
        if file.is_file() and ".xm" in file.name:
            (layout.processed / file.name).symlink_to(file)
    for file in source.outputs.iterdir():
        if file.is_file() and ".xm" in file.name:
            (layout.outputs / file.name).symlink_to(file)
    (tmp_path / "replay" / "xm").mkdir(parents=True)
    (tmp_path / "replay" / "xm" / "basemap.js").symlink_to(source.root / "replay/xm/basemap.js")
    return load_config(), layout


def execute(llm_env, **kwargs):
    config, layout = llm_env
    task = build_task(config, "xm", mode="llm", max_budget=400,
                      min_service_rate=.92, task_timeout_s=90)
    runtime = LLMRuntime(config, layout, task, FixtureProvider(**kwargs))
    return runtime, runtime.run()


def test_four_roles_revision_and_candidate_spatial_are_consistent(llm_env):
    runtime, out = execute(llm_env)
    assert out["terminal_state"] == "accepted", out
    assert out["authenticity_status"] == "test_fixture"
    view = build_run_view(*llm_env, out["run_id"])
    selected = view["decision"]["selected"]
    assert (selected["strategy"], selected["budget"]) == ("rule", 400)
    assert selected["revision_of"] == "cand-r1-2"
    assert set(view["decision"]["model_roles"]) == {"coordinator", "diagnosis", "dispatch", "evaluation"}
    assert view["decision"]["llm_connected"] is False
    for candidate in view["candidates"]:
        assert view["spatial"][candidate["candidate_id"]]["result_id"] == candidate["result"]["result_id"]
    assert selected["result"]["metrics"]["unmet_requests"] == 3306
    assert view["spatial"]["baseline"]["result_id"] != view["spatial"][selected["candidate_id"]]["result_id"]
    assert list_runs(llm_env[1]) == []
    with pytest.raises(ValueError, match="测试夹具"):
        publish_run(*llm_env, out["run_id"])
    published = publish_run(*llm_env, out["run_id"], include_tests=True)
    assert Path(published["standalone"]).is_file()
    rows = (runtime.run_dir / "presentation/dispatch-moves.csv").read_text(encoding="utf-8-sig")
    assert selected["result"]["result_id"] in rows
    assert selected["result"]["artifact_set_id"] not in rows


def test_wrong_feasibility_is_rejected_without_rule_fallback(llm_env):
    runtime, out = execute(llm_env, lie=True)
    assert out["terminal_state"] == "failed"
    assert "冲突" in out["reason"]
    assert runtime.candidates[0]["result"]["feasible"] is False
    assert runtime.roles.completed_roles.keys() == {"coordinator"}
    assert not json.loads((runtime.run_dir/"decision.json").read_text())["selected"]


def test_cancellation_keeps_realized_tool_result(llm_env):
    runtime, out = execute(llm_env, cancel=True)
    assert out["terminal_state"] == "failed"
    d = json.loads((runtime.run_dir/"decision.json").read_text())
    assert d["execution_status"] == "cancelled"
    assert runtime.candidates[0].get("result")
    assert runtime.journal.events[-1]["kind"] == "run_cancelled"


def test_unknown_evidence_fails_after_one_correction(llm_env):
    runtime, out = execute(llm_env, bad_refs=True)
    assert out["terminal_state"] == "failed"
    assert runtime.roles.call_count == 2
    assert runtime.tool_count == 0
    assert len([e for e in runtime.journal.events if e["kind"] == "action_rejected"]) == 2


def test_artifact_tampering_is_not_silently_replaced(llm_env):
    runtime, out = execute(llm_env)
    art = runtime.candidates[-1]["result"]["artifact_set_id"]
    (runtime.run_dir / "artifacts" / art / "summary.json").write_text("{}")
    with pytest.raises(ValueError, match="摘要"):
        build_run_view(*llm_env, out["run_id"])
