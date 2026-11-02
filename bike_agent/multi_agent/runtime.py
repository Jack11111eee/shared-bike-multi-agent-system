"""Four model roles choose actions; the runtime validates and executes them."""
from __future__ import annotations

import copy
import hashlib
import json
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path

import pandas as pd

from bike_agent.llm.provider import ModelLimitReached
from bike_agent.multi_agent.artifacts import ArtifactStore
from bike_agent.multi_agent.journal import Journal, encoded, now, write_json
from bike_agent.multi_agent.llm_roles import ModelRoles
from bike_agent.multi_agent.task import freeze_inputs, verify_unchanged
from bike_agent.tools.handlers import call_tool


class LLMRuntime:
    """Independent v2 path; legacy deterministic orchestration stays intact."""
    def __init__(self, config, layout, task, provider, *, output_root=None, run_id=None):
        task.validate()
        if task.mode != "llm":
            raise ValueError("LLMRuntime 只接受 mode=llm")
        self.config, self.layout, self.task = copy.deepcopy(config), layout, task
        self.provider = provider
        self.run_id = run_id or (
            datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ") + f"-{task.source}-llm-" + uuid.uuid4().hex[:6])
        root = Path(output_root or layout.outputs / "multi_agent")
        self.run_dir = root / self.run_id
        self.run_dir.mkdir(parents=True, exist_ok=False)
        self.journal = Journal(self.run_dir, self.run_id)
        self.started_at = now()
        self.deadline = time.monotonic() + task.task_timeout_s
        settings = config.get("llm", {})
        self.roles = ModelRoles(provider, self.journal, deadline=self.deadline,
                               max_calls=int(settings.get("max_model_calls", 24)),
                               request_timeout=float(settings.get("request_timeout_s", 60)))
        self.candidates = [{
            "candidate_id": "baseline", "strategy": task.baseline_strategy,
            "budget": task.baseline_budget, "round": 0, "is_baseline": True,
            "rationale": "冻结任务指定的基准", "revision_of": None, "status": "pending",
        }]
        self.diagnoses, self.evaluations = [], []
        self.pending = ["baseline"]
        self.phase, self.round = "baseline_pending", 0
        self.revision_of = None
        self.revision_reason = None
        self.revision_fields = []
        self.extra_diagnoses = 0
        self.current_tool_ids: list[str] = []
        self.store = None
        self.tool_count = 0

    def _alive(self):
        if time.monotonic() >= self.deadline:
            raise ModelLimitReached("任务截止时间已到")

    def _checkpoint(self):
        write_json(self.run_dir / "candidates.json", self.candidates)
        write_json(self.run_dir / "evaluations.json", self.evaluations)

    def _candidate(self, cid):
        found = next((c for c in self.candidates if c["candidate_id"] == cid), None)
        if found is None:
            raise ValueError("候选不属于当前运行")
        return found

    def _available(self):
        used = {(c["strategy"], c["budget"]) for c in self.candidates}
        return [{"strategy": s, "budget": b} for s in self.task.allowed_strategies
                for b in self.task.budget_options if (s, b) not in used]

    def _finalizable(self):
        feasible = [c for c in self.candidates if c.get("result", {}).get("feasible")]
        if not feasible:
            return [None]
        cost = min(c["result"]["metrics"]["composite_cost"] for c in feasible)
        best = [c for c in feasible if abs(c["result"]["metrics"]["composite_cost"] - cost) <= 1e-9]
        if any(c["is_baseline"] for c in best):
            return ["baseline"]
        return [sorted(best, key=lambda c: c["candidate_id"])[0]["candidate_id"]]

    def _options(self):
        if self.phase in {"baseline_pending", "evaluation_pending"}:
            return [{"action": "assign", "assignee": "evaluation"}]
        if self.phase == "diagnosis_pending":
            return [{"action": "assign", "assignee": "diagnosis"}]
        if self.phase in {"proposal_pending", "revision_pending"}:
            return [{"action": "assign", "assignee": "dispatch"}]
        options = [{"action": "finalize", "assignee": None}]
        capacity = len(self.candidates) - 1 < self.task.max_distinct_candidates
        if self._available() and self.round < self.task.max_rounds and capacity:
            options += [{"action": "assign", "assignee": "dispatch"},
                        {"action": "request_revision", "assignee": "dispatch"}]
        if self.extra_diagnoses < 1 and self._available() and capacity and self.round < self.task.max_rounds:
            options += [{"action": "request_diagnosis", "assignee": "diagnosis"}]
        return options

    def _context(self, role):
        facts = [{"candidate_id": c["candidate_id"], "strategy": c["strategy"],
                  "budget": c["budget"], "revision_of": c["revision_of"],
                  "result": c.get("result"), "assessment": c.get("assessment")}
                 for c in self.candidates]
        return {
            "instruction": {
                "coordinator": "根据当前状态选择合法下一步，review 阶段判断是否继续、修订或结束。",
                "diagnosis": "查询证据，诊断损失集中的时段及可能相关区域。",
                "dispatch": "提出合法新组合；若为修订，响应给定反馈。",
                "evaluation": "主动调用仿真工具，评审所有待评估候选。",
            }[role],
            "task": self.task.to_dict(), "state": self.phase, "round": self.round,
            "candidates": facts, "diagnoses": self.diagnoses[-2:],
            "recent_messages": self.journal.messages[-6:],
            "allowed_actions": self._options() if role == "coordinator" else [],
            "finalizable_candidate_ids": self._finalizable(),
            "available_combinations": self._available(),
            "pending_candidate_ids": self.pending,
            "revision_of": self.revision_of if role == "dispatch" else None,
            "revision_reason": self.revision_reason,
            "requested_revision_fields": self.revision_fields,
            "available_buckets": self.available_buckets,
            "available_cells": self.available_cells,
            "evidence_ids": sorted(self.journal.refs),
            "remaining": {
                "role_business_tools": 3,
                "model_calls": self.roles.max_calls - self.roles.call_count,
                "tool_calls": self.task.max_tool_calls - self.tool_count,
                "seconds": max(0, round(self.deadline - time.monotonic(), 1)),
            },
        }

    def _validate(self, role, payload):
        self._alive()
        refs = payload["evidence_refs"]
        if any(ref not in self.journal.refs for ref in refs):
            raise ValueError("证据 ID不存在或不属于本次运行")
        if role == "coordinator":
            if not any(payload["action"] == a["action"] and payload["assignee"] == a["assignee"]
                       for a in self._options()):
                raise ValueError("当前状态不允许该动作或接收角色")
            action, cid = payload["action"], payload["candidate_id"]
            if action == "finalize" and cid not in self._finalizable():
                raise ValueError("最终选择必须是本次已评估可行最优候选；无可行项时为 null")
            if action == "request_revision":
                c = self._candidate(cid)
                if not c.get("result"):
                    raise ValueError("只能修订已评估候选")
                if c["result"]["tool_call_id"] not in refs:
                    raise ValueError("修订要求必须引用该候选评估工具证据")
                fields = set(payload.get("revision_fields", []))
                if not fields or not any(
                    {k for k in ("strategy", "budget") if option[k] != c[k]} == fields
                    for option in self._available()
                ):
                    raise ValueError("revision_fields 必须明确指定可实际修改的 strategy/budget 字段，并有合法未评估组合；不能要求修改逐格搬运或冻结容量")
            elif action != "finalize" and cid is not None:
                    raise ValueError("一般分派的 candidate_id 必须为 null；待评估名单已在 pending_candidate_ids 中给出")
        if role == "diagnosis":
            if not self.current_tool_ids or not set(refs) & set(self.current_tool_ids):
                raise ValueError("诊断必须主动查询工具并引用本次工具证据")
            if not set(payload["focus_buckets"]) <= set(self.available_buckets):
                raise ValueError("关注时段超出历史回放窗口")
            if not set(payload["focus_cells"]) <= set(self.available_cells):
                raise ValueError("关注区域不属于当前数据源")
            for claim in payload["claims"]:
                if not set(claim["evidence_refs"]) <= self.journal.refs:
                    raise ValueError("诊断 claim 使用未知证据")
                if claim["status"] == "supported" and not claim["evidence_refs"]:
                    raise ValueError("supported 结论必须引用证据")
        if role == "dispatch":
            if not payload["candidates"]:
                raise ValueError("当前仍有合法组合，需提出候选后再交给协调终结")
            if len(payload["candidates"]) > self.task.max_candidates_per_round:
                raise ValueError("候选超过每轮上限")
            if len(self.candidates) - 1 + len(payload["candidates"]) > self.task.max_distinct_candidates:
                raise ValueError("不同候选数超过上限")
            keys = [(c["strategy"], c["budget"]) for c in payload["candidates"]]
            allowed = {(c["strategy"], c["budget"]) for c in self._available()}
            if len(set(keys)) != len(keys) or not set(keys) <= allowed:
                raise ValueError("候选重复、超预算或不在合法组合中")
            if self.revision_of is None and (payload["revision_of"] is not None or payload["changed_fields"]):
                raise ValueError("这是新提案：必须同时设置 revision_of=null 且 changed_fields=[]；轮次增加本身不代表修订")
            if payload["revision_of"] != self.revision_of:
                raise ValueError("修订引用与协调请求不一致")
            if self.revision_of:
                if len(keys) != 1:
                    raise ValueError("修订一次仅提出一个候选")
                original = self._candidate(self.revision_of)
                changed = {k for k in ("strategy", "budget")
                           if original[k] != payload["candidates"][0][k]}
                if changed != set(payload["changed_fields"]):
                    raise ValueError("changed_fields 与实际参数变化不一致")
                if changed != set(self.revision_fields):
                    raise ValueError("修订字段必须与协调反馈 requested_revision_fields 一致")
            elif payload["changed_fields"]:
                raise ValueError("初始提案不能声明修订字段")
        if role == "evaluation":
            assessments = payload["assessments"]
            if len(assessments) != len(self.pending) or {a["candidate_id"] for a in assessments} != set(self.pending):
                raise ValueError("必须逐一评审全部待评估候选")
            for a in assessments:
                result = self._candidate(a["candidate_id"]).get("result")
                if not result or result["tool_call_id"] not in self.current_tool_ids:
                    raise ValueError("评估角色必须先对候选主动调用工具")
                if a["result_id"] != result["result_id"]:
                    raise ValueError("评审 result_id 与工具事实不一致")
                verdict = "feasible" if result["feasible"] else "infeasible"
                if a["verdict"] != verdict or (not result["feasible"] and a["recommended_action"] in {"accept", "retain_baseline"}):
                    raise ValueError("评审达标结论与工具事实冲突")
                if result["tool_call_id"] not in refs:
                    raise ValueError("评审缺少候选工具证据")

    def _tool(self, role, name, args):
        self._alive()
        if self.tool_count >= self.task.max_tool_calls:
            raise ModelLimitReached("业务工具调用达到上限")
        self.tool_count += 1
        tool_id = f"tool-{self.tool_count:04d}"
        self.journal.event("tool_started", role=role, tool=name, tool_call_id=tool_id, input=args)
        started = time.monotonic()
        evaluated_candidate = None
        try:
            verify_unchanged(self.config, self.layout, self.task.source, self.manifest)
            if role == "evaluation" and name == "evaluate_dispatch_candidate":
                cid = args["candidate_id"]
                if cid not in self.pending:
                    raise ValueError("只能评估分派给当前角色的候选")
                candidate = self._candidate(cid)
                result, cache_hit = self.store.evaluate(candidate)
                result = copy.deepcopy(result)
                result["tool_call_id"] = tool_id
                baseline = self.candidates[0].get("result")
                if baseline:
                    b, c = baseline["metrics"], result["metrics"]
                    result["deltas"] = {
                        "cost_delta": c["composite_cost"] - b["composite_cost"],
                        "unmet_gain": b["unmet_requests"] - c["unmet_requests"],
                        "service_rate_delta_pp": (c["service_rate"] - b["service_rate"]) * 100,
                        "distance_delta": c["distance_bike_km"] - b["distance_bike_km"],
                    }
                evaluated_candidate = candidate
                output = {**result, "candidate_id": cid}
            elif role == "diagnosis" and name in {"get_forecast", "get_risk_ranking"}:
                if name == "get_forecast" and args["time_bucket"] not in self.available_buckets:
                    raise ValueError("预测查询超出本次窗口")
                output = call_tool(name, {**args, "source": self.task.source}, self.config, self.layout)
                if name == "get_forecast":
                    predictions = output["forecasts"]
                    output["verified_flow_summary"] = {
                        "definition": "net_flow = pred_out - pred_in；正为净流出，负为净流入",
                        "top_net_outflow": sorted(
                            [x for x in predictions if x["pred_net_flow"] > 0],
                            key=lambda x: -x["pred_net_flow"])[:5],
                        "top_net_inflow": sorted(
                            [x for x in predictions if x["pred_net_flow"] < 0],
                            key=lambda x: x["pred_net_flow"])[:5],
                    }
                cache_hit = False
            else:
                raise ValueError("角色无权调用该业务工具")
            verify_unchanged(self.config, self.layout, self.task.source, self.manifest)
            self._alive()
            if time.monotonic() - started > self.task.tool_timeout_s:
                raise TimeoutError("业务工具超过单次时间额度；结果不作为成功评估")
            if evaluated_candidate is not None:
                evaluated_candidate.update(result=result, status="evaluated")
                self.journal.event("candidate_evaluated", role=role,
                                   candidate_id=evaluated_candidate["candidate_id"],
                                   result=result, tool_call_id=tool_id)
            record = {"tool_call_id": tool_id, "role": role, "tool": name, "input": args,
                      "output": output, "cache_hit": cache_hit, "status": "ok",
                      "elapsed_ms": (time.monotonic()-started)*1000}
        except Exception as exc:
            self.journal.append("tool_calls", {"tool_call_id": tool_id, "role": role,
                                              "tool": name, "status": "error", "error": str(exc)})
            self.journal.event("tool_failed", role=role, tool_call_id=tool_id, error=str(exc))
            raise
        self.journal.tool_calls.append(record)
        self.journal.refs.add(tool_id)
        if name == "evaluate_dispatch_candidate":
            self.journal.refs.update({output["result_id"], output["artifact_set_id"]})
        self.current_tool_ids.append(tool_id)
        self.journal.append("tool_calls", record)
        self.journal.event("tool_completed", role=role, tool=name, tool_call_id=tool_id,
                           candidate_id=args.get("candidate_id"), cache_hit=cache_hit)
        self._checkpoint()
        return {"tool_call_id": tool_id, **output}

    def _perform(self, role):
        self.current_tool_ids = []
        output, message = self.roles.perform(role, self._context(role), self._tool,
                                            lambda p: self._validate(role, p), self.round)
        self.journal.append("checks", {"ok": True, "role": role,
                                      "message_id": message["message_id"]})
        return output, message

    def run(self):
        terminal, reason, selected, execution = "failed", "运行未完成", None, "failed"
        write_json(self.run_dir / "task.json", {"schema_version": "2.0", "task": self.task.to_dict()})
        write_json(self.run_dir / "execution_config.json", {
            "provider": self.provider.name, "model": self.provider.model,
            "real_provider": bool(self.provider.is_real), "max_model_calls": self.roles.max_calls,
            "request_timeout_s": self.roles.request_timeout,
            "prompt_digests": {r: hashlib.sha256(p.encode()).hexdigest() for r,p in self.roles.prompts.items()},
        })
        self.journal.event("run_started", mode="llm", source=self.task.source)
        try:
            self.manifest = freeze_inputs(self.config, self.layout, self.task.source)
            write_json(self.run_dir / "input_manifest.json", self.manifest.to_dict())
            self.store = ArtifactStore(self.config, self.layout, self.task, self.manifest, self.run_dir)
            cell_size = self.config["sources"][self.task.source]["cell_size_m"]
            self.available_cells = pd.read_parquet(self.layout.cells_path(self.task.source, cell_size)).cell_id.tolist()
            forecast = pd.read_parquet(self.layout.forecast_path(self.task.source))
            self.available_buckets = sorted(forecast.time_bucket.astype(str).unique().tolist())
            for _ in range(20):
                self._alive()
                action, action_msg = self._perform("coordinator")
                name = action["action"]
                if name == "finalize":
                    selected = self._candidate(action["candidate_id"]) if action["candidate_id"] else None
                    baseline = self.candidates[0]
                    terminal = ("baseline_retained" if selected and selected["is_baseline"] else
                                "accepted" if selected else "no_feasible_candidate")
                    reason, execution = action["reason"], "completed"
                    self.journal.event("decision_finalized", role="coordinator",
                                       candidate_id=action["candidate_id"], terminal_state=terminal,
                                       reason=reason, message_id=action_msg["message_id"])
                    break
                role = action["assignee"]
                if name == "request_revision":
                    self.revision_of, self.revision_reason = action["candidate_id"], action["reason"]
                    self.revision_fields = action["revision_fields"]
                    self.phase = "revision_pending"
                    self.journal.event("revision_requested", role="coordinator",
                                       candidate_id=self.revision_of, reason=self.revision_reason)
                elif name == "request_diagnosis":
                    self.extra_diagnoses += 1
                    self.revision_reason = action["reason"]
                if role == "dispatch":
                    self.round += 1
                    if self.round > self.task.max_rounds:
                        raise ModelLimitReached("方案轮次达到上限")
                output, message = self._perform(role)
                if role == "evaluation":
                    for a in output["assessments"]:
                        self._candidate(a["candidate_id"])["assessment"] = a
                    self.evaluations.append({**output, "message_id": message["message_id"]})
                    self.phase = "diagnosis_pending" if self.pending == ["baseline"] else "review_pending"
                    self.pending = []
                elif role == "diagnosis":
                    self.diagnoses.append({**output, "message_id": message["message_id"]})
                    self.phase = "proposal_pending"
                elif role == "dispatch":
                    self.pending = []
                    for index, proposed in enumerate(output["candidates"], 1):
                        cid = f"cand-r{self.round}-{index}"
                        candidate = {**proposed, "candidate_id": cid, "round": self.round,
                                     "is_baseline": False, "revision_of": output["revision_of"],
                                     "changed_fields": output["changed_fields"], "status": "pending"}
                        self.candidates.append(candidate)
                        self.pending.append(cid)
                        self.journal.event("candidate_proposed", role=role, candidate_id=cid,
                                           candidate=candidate, message_id=message["message_id"])
                    self.phase = "evaluation_pending"
                    self.revision_of = self.revision_reason = None
                    self.revision_fields = []
                self._checkpoint()
            else:
                raise ModelLimitReached("协调动作达到上限")
        except KeyboardInterrupt:
            reason, execution = "运行被取消", "cancelled"
            self.journal.event("run_cancelled", error=reason)
        except ModelLimitReached as exc:
            terminal, reason, execution = "limit_reached", str(exc), "limit_reached"
            self.journal.event("run_failed", error=reason, terminal_state=terminal)
        except Exception as exc:
            reason = f"{type(exc).__name__}: {exc}"
            self.journal.event("run_failed", error=reason, terminal_state=terminal)
        self._checkpoint()
        complete = set(self.roles.completed_roles) == {"coordinator", "diagnosis", "dispatch", "evaluation"}
        real_successes = [c for c in self.journal.model_calls if c.get("status") == "ok" and c["real_provider"]]
        authenticity = ("real_model_complete" if complete and execution == "completed" and self.provider.is_real
                        else "real_model_partial" if real_successes else "test_fixture" if not self.provider.is_real
                        else "not_connected")
        decision = {
            "schema_version": "2.0", "run_id": self.run_id, "source": self.task.source,
            "mode": "llm", "rule_based": False, "llm_connected": bool(real_successes),
            "authenticity_status": authenticity, "execution_status": execution,
            "terminal_state": terminal, "reason": reason,
            "baseline": self.candidates[0], "selected": selected,
            "model_roles": self.roles.completed_roles,
            "timing": {"task_elapsed_ms": (time.monotonic()-self.journal.started)*1000},
        }
        write_json(self.run_dir / "decision.json", decision)
        index = {**{k:v for k,v in decision.items() if k not in {"baseline", "selected"}},
                 "started_at": self.started_at, "finished_at": now(),
                 "provider": self.provider.name, "model": self.provider.model,
                 "model_calls": self.roles.call_count, "tool_calls": self.tool_count}
        write_json(self.run_dir / "run.json", index)
        return {"run_id": self.run_id, "run_dir": str(self.run_dir),
                "terminal_state": terminal, "reason": reason,
                "authenticity_status": authenticity,
                "exit_code": 0 if execution == "completed" else 1}
