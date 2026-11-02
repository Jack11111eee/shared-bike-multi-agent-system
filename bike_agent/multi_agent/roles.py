"""The four roles, each with its own context and decision interface (F24 / F25).

The roles are deliberately plain Python objects with a single ``handle`` (or,
for the coordinator, ``assignment``/``review``) method.  They run in one
process and in sequence; "independent" here means what the requirement says it
means -- separate responsibilities, separate input contexts and separate
decision functions -- not separate operating-system processes.

* Coordinator never calls a business tool and never computes a metric.  It
  splits the task, opens rounds, re-checks the hard gates reported by the
  evaluation and selects a terminal state.
* Diagnosis reads the baseline trajectory and queries the two read-only tools.
  It distinguishes "supported by actual unmet requests" from "possibly related
  forecast signal" and records what evidence is missing.
* Dispatch proposes legal strategy/budget pairs and, on a revision request,
  changes the field the feedback named -- or says why it cannot.
* Evaluation runs every candidate through the versioned evaluation tool and
  returns a structured verdict.  It cannot be bypassed: the coordinator only
  accepts candidates that carry a successful evaluation with zero violations.
"""

from __future__ import annotations

import math
from collections.abc import Mapping
from dataclasses import dataclass
from functools import cmp_to_key
from typing import Any

from bike_agent.multi_agent.gateway import MultiAgentGateway, ToolCallResult
from bike_agent.multi_agent.task import TaskSpec

REL_TOL = 1e-9
ABS_TOL = 1e-9

def _close(left: float, right: float) -> bool:
    return math.isclose(float(left), float(right), rel_tol=REL_TOL, abs_tol=ABS_TOL)

# -- contexts ---------------------------------------------------------------
@dataclass(frozen=True)
class DiagnosisContext:
    task: TaskSpec
    round: int
    baseline: Mapping[str, Any]
    baseline_tool_call_id: str
    inbox: tuple[Mapping[str, Any], ...] = ()

@dataclass(frozen=True)
class DispatchContext:
    task: TaskSpec
    round: int
    diagnosis: Mapping[str, Any]
    diagnosis_tool_call_ids: tuple[str, ...]
    baseline: Mapping[str, Any]
    feedback: Mapping[str, Any] | None
    candidates_history: tuple[Mapping[str, Any], ...]
    evaluated_keys: frozenset[tuple[str, int]]
    inbox: tuple[Mapping[str, Any], ...] = ()

@dataclass(frozen=True)
class EvaluationContext:
    task: TaskSpec
    round: int
    candidates: tuple[Mapping[str, Any], ...]
    baseline: Mapping[str, Any]
    baseline_tool_call_id: str
    inbox: tuple[Mapping[str, Any], ...] = ()

@dataclass(frozen=True)
class CoordinatorContext:
    task: TaskSpec
    round: int
    baseline: Mapping[str, Any] | None
    candidates: tuple[Mapping[str, Any], ...]
    evaluations: tuple[Mapping[str, Any], ...]
    evidence: Mapping[str, Any]
    limits: Mapping[str, Any]
    revised_candidate_ids: frozenset[str] = frozenset()
    inbox: tuple[Mapping[str, Any], ...] = ()

# -- base -------------------------------------------------------------------
class Role:
    name = "role"

    def handle(self, context: Any, gateway: MultiAgentGateway | None = None) -> dict[str, Any]:
        raise NotImplementedError

# -- diagnosis --------------------------------------------------------------
class DiagnosisRole(Role):
    """Locate the loss buckets and the cells that *might* explain them."""

    name = "diagnosis"
    TOP_BUCKETS = 3
    TOP_CELLS = 3

    def handle(self, context: DiagnosisContext, gateway: MultiAgentGateway | None = None) -> dict[str, Any]:
        if gateway is None:
            raise ValueError("diagnosis requires the gateway")
        trajectory = list(context.baseline.get("trajectory") or [])
        if not trajectory:
            return {
                "focus_buckets": [],
                "focus_cells": [],
                "hypotheses": [],
                "evidence_gaps": ["基准轨迹为空，无法定位损失时段。"],
                "risk_ranking_scope": {
                    "time_bucket": "",
                    "covers_focus_bucket": False,
                    "note": "没有基准轨迹，未查询风险排名。",
                },
                "recommended": None,
            }

        ordered = sorted(
            trajectory,
            key=lambda row: (-int(row["unmet_requests"]), int(row["borrow_requests"]) * -1, row["time_bucket"]),
        )
        focus = [row for row in ordered if int(row["unmet_requests"]) > 0][: self.TOP_BUCKETS]
        if not focus:
            focus = ordered[:1]
        focus_buckets = [
            {
                "time_bucket": str(row["time_bucket"]),
                "unmet_requests": int(row["unmet_requests"]),
                "rank": index + 1,
            }
            for index, row in enumerate(focus)
        ]
        worst = focus_buckets[0]["time_bucket"]

        forecast_call = gateway.call(
            self.name, "get_forecast", {"source": context.task.source, "time_bucket": worst}
        )
        ranking_call = gateway.call(
            self.name,
            "get_risk_ranking",
            {"source": context.task.source, "direction": "replenish", "top_k": 5},
        )
        forecast = forecast_call.output
        ranking = ranking_call.output

        net_out = sorted(
            (row for row in forecast.get("forecasts", []) if float(row["pred_net_flow"]) > 0),
            key=lambda row: (-float(row["pred_net_flow"]), row["cell_id"]),
        )[: self.TOP_CELLS]
        if not net_out:
            net_out = sorted(
                forecast.get("forecasts", []),
                key=lambda row: (-float(row["pred_out"]), row["cell_id"]),
            )[: self.TOP_CELLS]
        focus_cells = [
            {
                "cell_id": str(row["cell_id"]),
                # F26: a forecast is not a ledger.  Without cell-level
                # inventory evidence this can only be "possibly related".
                "reason": f"预测净流出 {float(row['pred_net_flow']):.2f}（预测值，非实际损失）",
                "evidence_kind": "possible_related",
            }
            for row in net_out
        ]

        ranking_bucket = str(ranking.get("time_bucket") or "")
        covers = ranking_bucket == worst
        scope_note = (
            f"风险排名覆盖时段 {ranking_bucket or '未知'}，与关注时段 {worst} 一致。"
            if covers
            else f"风险排名只覆盖时段 {ranking_bucket or '未知'}，不是关注时段 {worst} 的风险清单；"
            "仅作参考，不能据此断言该时段的损失原因。"
        )

        hypotheses = [
            {
                "claim": (
                    f"时段 {worst} 的未满足请求最高（{focus_buckets[0]['unmet_requests']} 次），"
                    "是本次回放中损失最集中的时段。"
                ),
                "evidence_refs": [context.baseline_tool_call_id],
                "status": "supported",
            }
        ]
        if focus_cells:
            leaders = "、".join(f"`{cell['cell_id']}`" for cell in focus_cells)
            hypotheses.append(
                {
                    "claim": (
                        f"预测净流出最高的格子（{leaders}）可能与未满足请求相关；"
                        "没有格级库存账本，不能把它写成已证实的损失原因。"
                    ),
                    "evidence_refs": [forecast_call.tool_call_id],
                    "status": "possible_related",
                }
            )
        if not covers:
            hypotheses.append(
                {
                    "claim": scope_note,
                    "evidence_refs": [ranking_call.tool_call_id],
                    "status": "possible_related",
                }
            )

        evidence_gaps = ["没有格级库存账本工具；预测净流出不等于实际损失，相关结论只能标为待核查。"]
        if not covers:
            evidence_gaps.append(scope_note)

        return {
            "focus_buckets": focus_buckets,
            "focus_cells": focus_cells,
            "hypotheses": hypotheses,
            "evidence_gaps": evidence_gaps,
            "risk_ranking_scope": {
                "time_bucket": ranking_bucket,
                "covers_focus_bucket": covers,
                "note": scope_note,
            },
            "recommended": {
                "strategy": "optimized",
                "budget": context.task.baseline_budget,
                "reason": "在相同预算下检验约束优化能否减少未满足请求；不预设一定改善。",
            },
            "_tool_call_ids": [forecast_call.tool_call_id, ranking_call.tool_call_id],
        }

# -- dispatch ---------------------------------------------------------------
class DispatchRole(Role):
    """Propose legal strategy/budget pairs, and respond to concrete feedback."""

    name = "dispatch"

    def handle(self, context: DispatchContext, gateway: MultiAgentGateway | None = None) -> dict[str, Any]:
        if context.feedback is None:
            return self._initial(context)
        return self._revise(context)

    def _candidate(
        self,
        *,
        round_number: int,
        index: int,
        strategy: str,
        budget: int,
        rationale: str,
        tradeoff: str,
        revision_of: str | None,
    ) -> dict[str, Any]:
        return {
            "candidate_id": f"cand-r{round_number}-{index}",
            "strategy": strategy,
            "budget": int(budget),
            "rationale": rationale,
            "expected_tradeoff": tradeoff,
            "revision_of": revision_of,
        }

    def _initial(self, context: DispatchContext) -> dict[str, Any]:
        task = context.task
        focus = context.diagnosis.get("focus_buckets") or []
        where = (
            f"诊断显示时段 {focus[0]['time_bucket']} 的未满足请求最高"
            f"（{focus[0]['unmet_requests']} 次）"
            if focus
            else "基准轨迹没有明显的未满足请求"
        )
        candidates = [
            self._candidate(
                round_number=context.round,
                index=1,
                strategy="optimized",
                budget=task.baseline_budget,
                rationale=f"{where}；在相同每时段预算 {task.baseline_budget} 下改用约束优化，检验算法本身能否减少未满足请求。",
                tradeoff="可能降低未满足请求，但可能增加搬运距离；预算不变，收益不能归因于资源增加。",
                revision_of=None,
            )
        ]
        higher = sorted(budget for budget in task.budget_options if budget > task.baseline_budget)
        if higher and len(candidates) < task.max_candidates_per_round:
            candidates.append(
                self._candidate(
                    round_number=context.round,
                    index=2,
                    strategy="optimized",
                    budget=higher[-1],
                    rationale=(
                        f"{where}；把每时段预算上限从 {task.baseline_budget} 提高到 {higher[-1]}，"
                        "检验资源增加带来的变化。"
                    ),
                    tradeoff=(
                        f"预算上限提高到 {higher[-1]}，资源变化必须单独披露；"
                        "不能把改善全部归因于算法。"
                    ),
                    revision_of=None,
                )
            )
        return {
            "candidates": candidates,
            "changed_fields": [],
            "feedback_response": None,
            "_evidence_refs": list(context.diagnosis_tool_call_ids),
        }

    def _revise(self, context: DispatchContext) -> dict[str, Any]:
        task = context.task
        feedback = context.feedback or {}
        rejected_id = str(feedback.get("candidate_id") or "")
        rejected = next(
            (cand for cand in context.candidates_history if cand.get("candidate_id") == rejected_id),
            None,
        )
        if rejected is None:
            return {
                "candidates": [],
                "changed_fields": [],
                "feedback_response": {
                    "candidate_id": rejected_id,
                    "cannot_change": True,
                    "explanation": "反馈指向的候选不在本次运行的历史中，无法修订。",
                },
                "_evidence_refs": [],
            }

        suggested = feedback.get("suggested_revision") or {}
        current_strategy = str(rejected["strategy"])
        current_budget = int(rejected["budget"])
        evaluated = context.evaluated_keys
        options: list[tuple[str, int, str]] = []

        def add(strategy: str, budget: int, why: str) -> None:
            if strategy not in task.allowed_strategies:
                return
            if budget not in task.budget_options or budget > task.max_budget:
                return
            if (strategy, budget) == (current_strategy, current_budget):
                return
            if (strategy, budget) in evaluated:
                return
            if any((strategy, budget) == (item[0], item[1]) for item in options):
                return
            options.append((strategy, budget, why))

        # 1. Follow the evaluation's concrete suggestion when it is legal.
        action = suggested.get("action")
        higher = sorted(budget for budget in task.budget_options if budget > current_budget)
        other_strategies = [
            strategy for strategy in task.allowed_strategies if strategy != current_strategy
        ]
        if action == "raise_budget":
            # A service-rate defect is not fixed by spending less.  Try the
            # suggested budget, then any remaining higher tier, then a
            # different strategy at the same or a higher budget.
            if isinstance(suggested.get("budget"), int):
                add(current_strategy, int(suggested["budget"]), "按评估建议提高预算")
            for budget in higher:
                add(current_strategy, budget, "提高每时段预算")
            for strategy in other_strategies:
                add(strategy, current_budget, "改换策略")
                for budget in higher:
                    add(strategy, budget, "改换策略并提高预算")
        elif action == "change_strategy":
            chosen = suggested.get("strategy")
            if chosen in task.allowed_strategies:
                add(str(chosen), current_budget, "按评估建议调整策略")
                for budget in higher:
                    add(str(chosen), budget, "按评估建议调整策略并提高预算")
            for strategy in other_strategies:
                add(strategy, current_budget, "改换策略")
                for budget in higher:
                    add(strategy, budget, "改换策略并提高预算")
        else:
            if isinstance(suggested.get("budget"), int):
                add(current_strategy, int(suggested["budget"]), "按评估建议调整预算")
            if suggested.get("strategy") in task.allowed_strategies:
                add(str(suggested["strategy"]), current_budget, "按评估建议调整策略")
            for budget in higher:
                add(current_strategy, budget, "提高每时段预算")
            for strategy in task.allowed_strategies:
                add(strategy, current_budget, "改换策略")
            lower = sorted(
                (budget for budget in task.budget_options if budget < current_budget), reverse=True
            )
            for budget in lower:
                add(current_strategy, budget, "降低每时段预算")

        if not options:
            return {
                "candidates": [],
                "changed_fields": [],
                "feedback_response": {
                    "candidate_id": rejected_id,
                    "cannot_change": True,
                    "defects": list(feedback.get("defects") or []),
                    "explanation": (
                        "在允许的策略与预算档内没有尚未评估的替代组合；"
                        "继续修改只会重复已评估的候选，因此不再提出新候选。"
                    ),
                },
                "_evidence_refs": [],
            }

        strategy, budget, why = options[0]
        changed = []
        if strategy != current_strategy:
            changed.append("strategy")
        if budget != current_budget:
            changed.append("budget")
        candidate = self._candidate(
            round_number=context.round,
            index=1,
            strategy=strategy,
            budget=budget,
            rationale=(
                f"回应评估对 {rejected_id} 的反馈（{why}）："
                f"从 {current_strategy}/{current_budget} 改为 {strategy}/{budget}。"
            ),
            tradeoff="这是一次定向修订；是否更好由重新评估决定，不预设结论。",
            revision_of=rejected_id,
        )
        return {
            "candidates": [candidate],
            "changed_fields": changed,
            "feedback_response": {
                "candidate_id": rejected_id,
                "defects": list(feedback.get("defects") or []),
                "addressed": changed,
                "explanation": f"按反馈调整了 {', '.join(changed)}；旧候选保留，新版本标记 revision_of。",
            },
            "_evidence_refs": [],
        }

# -- evaluation -------------------------------------------------------------
class EvaluationRole(Role):
    """Run every candidate and return a verdict grounded in the tool output."""

    name = "evaluation"

    def establish_baseline(
        self, task: TaskSpec, gateway: MultiAgentGateway
    ) -> tuple[ToolCallResult, dict[str, Any]]:
        """The coordinator's first request: evaluate the rule baseline.

        The baseline is evaluated through the same versioned tool as every
        candidate, so the comparison is between two real runs of the engine and
        not between a run and a remembered number.
        """
        result = gateway.evaluate(
            strategy=task.baseline_strategy, budget=task.baseline_budget
        )
        output = result.output
        metrics = output["metrics"]
        violations = output["violations"]
        violations_ok = all(int(value) == 0 for value in violations.values())
        service_rate_ok = (
            task.min_service_rate is None
            or float(metrics["service_rate"]) + ABS_TOL >= float(task.min_service_rate)
        )
        defects = [f"{key} violations = {int(value)}" for key, value in violations.items() if int(value) != 0]
        if not service_rate_ok:
            defects.append(
                f"service_rate {float(metrics['service_rate']):.6f} < min_service_rate "
                f"{float(task.min_service_rate):.6f}"
            )
        item = {
            "candidate_id": None,
            "result_id": output["result_id"],
            "tool_call_id": result.tool_call_id,
            "feasible": violations_ok and service_rate_ok,
            "violations_ok": violations_ok,
            "service_rate_ok": service_rate_ok,
            "metrics": {
                "unmet_requests": int(metrics["unmet_requests"]),
                "distance_bike_km": float(metrics["distance_bike_km"]),
                "composite_cost": float(metrics["composite_cost"]),
                "service_rate": float(metrics["service_rate"]),
            },
            "violations": {key: int(value) for key, value in violations.items()},
            "deltas": {"cost_delta": 0.0, "unmet_gain": 0, "service_rate_delta_pp": 0.0},
            "formula": {
                "cost_delta": "candidate.composite_cost - baseline.composite_cost",
                "unmet_gain": "baseline.unmet_requests - candidate.unmet_requests",
                "service_rate_delta_pp": "(candidate.service_rate - baseline.service_rate) * 100",
            },
            # The baseline is its own reference: both operands are this run.
            "operands": {
                "baseline": {
                    "result_id": output["result_id"],
                    "metrics": {
                        "unmet_requests": int(metrics["unmet_requests"]),
                        "distance_bike_km": float(metrics["distance_bike_km"]),
                        "composite_cost": float(metrics["composite_cost"]),
                        "service_rate": float(metrics["service_rate"]),
                    },
                },
                "candidate": {
                    "result_id": output["result_id"],
                    "metrics": {
                        "unmet_requests": int(metrics["unmet_requests"]),
                        "distance_bike_km": float(metrics["distance_bike_km"]),
                        "composite_cost": float(metrics["composite_cost"]),
                        "service_rate": float(metrics["service_rate"]),
                    },
                },
            },
            "verdict": "accept" if violations_ok and service_rate_ok else "reject",
            "defects": defects,
            "suggested_revision": None,
        }
        return result, item

    def handle(self, context: EvaluationContext, gateway: MultiAgentGateway | None = None) -> dict[str, Any]:
        if gateway is None:
            raise ValueError("evaluation requires the gateway")
        baseline = context.baseline
        baseline_metrics = baseline["metrics"]
        evaluations: list[dict[str, Any]] = []
        evidence: list[str] = [context.baseline_tool_call_id]
        for candidate in context.candidates:
            result: ToolCallResult = gateway.evaluate(
                strategy=str(candidate["strategy"]), budget=int(candidate["budget"])
            )
            output = result.output
            metrics = output["metrics"]
            violations = output["violations"]
            violations_ok = all(int(value) == 0 for value in violations.values())
            service_rate_ok = (
                context.task.min_service_rate is None
                or float(metrics["service_rate"]) + ABS_TOL >= float(context.task.min_service_rate)
            )
            feasible = violations_ok and service_rate_ok
            deltas = {
                "cost_delta": float(metrics["composite_cost"]) - float(baseline_metrics["composite_cost"]),
                "unmet_gain": int(baseline_metrics["unmet_requests"]) - int(metrics["unmet_requests"]),
                "service_rate_delta_pp": (
                    float(metrics["service_rate"]) - float(baseline_metrics["service_rate"])
                )
                * 100.0,
            }
            defects: list[str] = []
            for key, value in violations.items():
                if int(value) != 0:
                    defects.append(f"{key} violations = {int(value)}")
            if not service_rate_ok:
                defects.append(
                    f"service_rate {float(metrics['service_rate']):.6f} < min_service_rate "
                    f"{float(context.task.min_service_rate):.6f}"
                )
            if float(metrics["composite_cost"]) > float(baseline_metrics["composite_cost"]) and not _close(
                metrics["composite_cost"], baseline_metrics["composite_cost"]
            ):
                defects.append(
                    f"composite_cost 高于基准：{float(metrics['composite_cost']):.6f} > "
                    f"{float(baseline_metrics['composite_cost']):.6f}（cost_delta = candidate - baseline）"
                )
            suggested = self._suggest(context.task, candidate, feasible, service_rate_ok, violations_ok, deltas)
            evaluations.append(
                {
                    "candidate_id": candidate["candidate_id"],
                    "result_id": output["result_id"],
                    "tool_call_id": result.tool_call_id,
                    "feasible": feasible,
                    "violations_ok": violations_ok,
                    "service_rate_ok": service_rate_ok,
                    "metrics": {
                        "unmet_requests": int(metrics["unmet_requests"]),
                        "distance_bike_km": float(metrics["distance_bike_km"]),
                        "composite_cost": float(metrics["composite_cost"]),
                        "service_rate": float(metrics["service_rate"]),
                    },
                    "violations": {key: int(value) for key, value in violations.items()},
                    "deltas": deltas,
                    "formula": {
                        "cost_delta": "candidate.composite_cost - baseline.composite_cost",
                        "unmet_gain": "baseline.unmet_requests - candidate.unmet_requests",
                        "service_rate_delta_pp": "(candidate.service_rate - baseline.service_rate) * 100",
                    },
                    "operands": {
                        "baseline": {
                            "result_id": baseline["result_id"],
                            "metrics": {
                                "unmet_requests": int(baseline_metrics["unmet_requests"]),
                                "distance_bike_km": float(baseline_metrics["distance_bike_km"]),
                                "composite_cost": float(baseline_metrics["composite_cost"]),
                                "service_rate": float(baseline_metrics["service_rate"]),
                            },
                        },
                        "candidate": {
                            "result_id": output["result_id"],
                            "metrics": {
                                "unmet_requests": int(metrics["unmet_requests"]),
                                "distance_bike_km": float(metrics["distance_bike_km"]),
                                "composite_cost": float(metrics["composite_cost"]),
                                "service_rate": float(metrics["service_rate"]),
                            },
                        },
                    },
                    "verdict": "accept" if feasible else "reject",
                    "defects": defects,
                    "suggested_revision": suggested,
                }
            )
            evidence.append(result.tool_call_id)
        return {
            "baseline_result_id": baseline["result_id"],
            "evaluations": evaluations,
            "_evidence_refs": evidence,
        }

    def _suggest(
        self,
        task: TaskSpec,
        candidate: Mapping[str, Any],
        feasible: bool,
        service_rate_ok: bool,
        violations_ok: bool,
        deltas: Mapping[str, Any],
    ) -> dict[str, Any] | None:
        if feasible:
            if deltas["cost_delta"] > 0 and not _close(deltas["cost_delta"], 0.0):
                return {"action": "retain_baseline", "reason": "候选可行但综合代价高于基准。"}
            return None
        if not service_rate_ok:
            higher = sorted(budget for budget in task.budget_options if budget > int(candidate["budget"]))
            if higher:
                return {
                    "action": "raise_budget",
                    "budget": higher[0],
                    "reason": f"服务率未达到 min_service_rate；先尝试提高每时段预算到 {higher[0]}。",
                }
            other = next(
                (
                    strategy
                    for strategy in task.allowed_strategies
                    if strategy != candidate["strategy"]
                ),
                None,
            )
            if other:
                return {
                    "action": "change_strategy",
                    "strategy": other,
                    "reason": "预算档已无更高可用档；尝试改换策略。",
                }
        if not violations_ok:
            other = next(
                (
                    strategy
                    for strategy in task.allowed_strategies
                    if strategy != candidate["strategy"]
                ),
                None,
            )
            if other:
                return {
                    "action": "change_strategy",
                    "strategy": other,
                    "reason": "仿真报告硬约束 violations；尝试改换策略。",
                }
            higher = sorted(budget for budget in task.budget_options if budget > int(candidate["budget"]))
            if higher:
                return {
                    "action": "raise_budget",
                    "budget": higher[0],
                    "reason": "仿真报告硬约束 violations；尝试提高每时段预算。",
                }
        return None

# -- coordinator ------------------------------------------------------------
@dataclass
class CoordinatorDecision:
    action: str
    reason: str
    round: int | None = None
    candidate_id: str | None = None
    defects: tuple[str, ...] = ()
    suggested_revision: Mapping[str, Any] | None = None
    terminal_state: str | None = None
    selected: Mapping[str, Any] | None = None
    ranking: tuple[Mapping[str, Any], ...] = ()

class CoordinatorRole(Role):
    """Split the work, open rounds and select a terminal state.

    It never runs a business tool and never recomputes a metric.  It *does*
    re-read the hard gates in the evaluation payload (feasible, violations,
    service rate) and refuses to accept a candidate that lacks a successful
    evaluation -- a message that merely says "approved" is not enough.
    """

    name = "coordinator"

    def assignment(
        self,
        instruction: str,
        assignee: str,
        round_number: int,
        details: Mapping[str, Any] | None = None,
    ) -> dict[str, Any]:
        payload = {"instruction": instruction, "assignee": assignee, "round": int(round_number)}
        if details:
            payload["details"] = dict(details)
        return payload

    def handle(
        self, context: CoordinatorContext, gateway: MultiAgentGateway | None = None
    ) -> CoordinatorDecision:
        """The role interface: review a round and decide what happens next.

        ``assignment`` builds the payload it sends to other roles; ``handle`` is
        the decision function every role exposes, so the orchestrator can treat
        the coordinator the same way as the other three.
        """
        return self.review(context)

    def review(self, context: CoordinatorContext) -> CoordinatorDecision:
        baseline = context.baseline
        if baseline is None:
            return CoordinatorDecision(
                action="finalize",
                reason="没有基准评估，协调角色不能签收任何候选。",
                terminal_state="failed",
            )
        if baseline.get("result_id") not in context.evidence:
            return CoordinatorDecision(
                action="finalize",
                reason="基准结果 ID 不在本次运行的工具证据中，协调角色拒绝签收。",
                terminal_state="failed",
            )
        baseline_evidence = context.evidence.get(baseline.get("result_id"))
        if not isinstance(baseline_evidence, dict):
            return CoordinatorDecision(
                action="finalize",
                reason="基准结果没有对应的工具返回，协调角色拒绝签收。",
                terminal_state="failed",
            )
        if baseline_evidence.get("result_id") != baseline.get("result_id"):
            return CoordinatorDecision(
                action="finalize",
                reason="基准结果 ID 与工具返回不一致，协调角色拒绝签收。",
                terminal_state="failed",
            )
        if not self._metrics_match(baseline_evidence.get("metrics"), baseline.get("metrics")):
            return CoordinatorDecision(
                action="finalize",
                reason="基准指标与工具返回不一致，协调角色拒绝签收。",
                terminal_state="failed",
            )
        if baseline_evidence.get("violations") != baseline.get("violations"):
            return CoordinatorDecision(
                action="finalize",
                reason="基准 violations 与工具返回不一致，协调角色拒绝签收。",
                terminal_state="failed",
            )
        baseline_feasible = self._baseline_feasible(context.task, baseline)

        views: list[dict[str, Any]] = []
        if baseline_feasible:
            views.append(
                self._view(
                    "baseline",
                    None,
                    context.task.baseline_strategy,
                    context.task.baseline_budget,
                    {
                        "result_id": baseline["result_id"],
                        "metrics": baseline["metrics"],
                        "violations": baseline.get("violations") or {},
                        "deltas": {"cost_delta": 0.0, "unmet_gain": 0, "service_rate_delta_pp": 0.0},
                    },
                )
            )
        for evaluation in context.evaluations:
            if not evaluation.get("feasible"):
                continue
            if not evaluation.get("violations_ok") or not evaluation.get("service_rate_ok"):
                continue
            if evaluation.get("result_id") not in context.evidence:
                continue
            tool_call_id = evaluation.get("tool_call_id")
            if tool_call_id and tool_call_id not in context.evidence:
                continue
            # Do not trust the evaluation's self-reported flags: re-read the
            # tool output and require its violations and metrics to match.
            evidence_output = (
                context.evidence.get(tool_call_id)
                if tool_call_id
                else context.evidence.get(evaluation.get("result_id"))
            )
            if not isinstance(evidence_output, dict):
                continue
            if evidence_output.get("result_id") != evaluation.get("result_id"):
                continue
            actual_violations = evidence_output.get("violations") or {}
            if any(int(value) != 0 for value in actual_violations.values()):
                continue
            actual_metrics = evidence_output.get("metrics") or {}
            if not self._metrics_match(actual_metrics, evaluation.get("metrics")):
                continue
            if (
                baseline.get("frozen_context_id")
                and evidence_output.get("frozen_context_id") != baseline.get("frozen_context_id")
            ):
                continue
            candidate = next(
                (
                    item
                    for item in context.candidates
                    if item.get("candidate_id") == evaluation.get("candidate_id")
                ),
                None,
            )
            if candidate is None:
                continue
            views.append(
                self._view(
                    "candidate",
                    candidate,
                    str(candidate["strategy"]),
                    int(candidate["budget"]),
                    {
                        "result_id": evaluation["result_id"],
                        "metrics": actual_metrics,
                        "violations": actual_violations,
                        "deltas": evaluation["deltas"],
                    },
                )
            )

        views.sort(key=cmp_to_key(self._compare))
        ranking = tuple(views)
        rejected = [
            item
            for item in context.evaluations
            if not item.get("feasible")
            and str(item.get("candidate_id")) not in context.revised_candidate_ids
        ]

        # Prefer a rejection whose suggested revision is still actionable.
        # Otherwise the loop would spend its last round on a suggestion that
        # points at a candidate it already evaluated, while a different
        # rejected candidate had a legal alternative all along.
        if rejected:
            evaluated_keys = {
                (str(item["strategy"]), int(item["budget"])) for item in context.candidates
            }
            evaluated_keys.add(
                (context.task.baseline_strategy, context.task.baseline_budget)
            )

            def actionable(item: Mapping[str, Any]) -> bool:
                suggestion = item.get("suggested_revision") or {}
                candidate = next(
                    (
                        entry
                        for entry in context.candidates
                        if entry.get("candidate_id") == item.get("candidate_id")
                    ),
                    None,
                )
                if candidate is None:
                    return False
                strategy = suggestion.get("strategy", candidate["strategy"])
                budget = int(suggestion.get("budget", candidate["budget"]))
                if strategy not in context.task.allowed_strategies:
                    return False
                if budget not in context.task.budget_options:
                    return False
                return (str(strategy), budget) not in evaluated_keys

            rejected.sort(key=lambda item: (not actionable(item), str(item.get("candidate_id"))))
        baseline_view = next((view for view in views if view["kind"] == "baseline"), None)

        # A strict improvement is a finished decision; a rejected side
        # candidate does not justify another round once a better feasible
        # candidate already exists.
        if views:
            best = views[0]
            if (
                best["kind"] == "candidate"
                and baseline_view is not None
                and self._strictly_better(best, baseline_view)
            ):
                return CoordinatorDecision(
                    action="finalize",
                    reason=(
                        f"候选 {best['candidate_id']} 通过硬约束且优于可行基准："
                        f"cost_delta = {best['deltas']['cost_delta']:+.6f}，"
                        f"unmet_gain = {best['deltas']['unmet_gain']:+d}。"
                    ),
                    terminal_state="accepted",
                    selected=best,
                    ranking=ranking,
                )

        # Baseline infeasible: any feasible candidate is already the best
        # available answer.  Keeping the loop alive because some *other*
        # candidate was rejected would waste rounds and could end in a false
        # "no feasible candidate" even though one exists.
        if views and baseline_view is None:
            best = views[0]
            return CoordinatorDecision(
                action="finalize",
                reason=(
                    f"基准违反硬约束，不能作为可行回退；候选 {best['candidate_id']} "
                    f"（{best['strategy']}/{best['budget']}）是本次评估中可用的可行方案。"
                ),
                terminal_state="accepted",
                selected=best,
                ranking=ranking,
            )

        # A pending, still-actionable rejection gets one more round.
        if rejected and context.round < context.task.max_rounds:
            target = rejected[0]
            return CoordinatorDecision(
                action="revise",
                round=context.round + 1,
                candidate_id=str(target["candidate_id"]),
                defects=tuple(target.get("defects") or ()),
                suggested_revision=target.get("suggested_revision"),
                reason=(
                    f"候选 {target['candidate_id']} 未通过评估（{'; '.join(target.get('defects') or ['未说明'])}）；"
                    f"进入第 {context.round + 1} 轮定向修订。"
                ),
            )

        if views:
            best = views[0]
            if baseline_view is not None:
                reason = (
                    "基准可行，有限候选内没有严格更优方案，保留基准。"
                    if rejected
                    else "基准可行，候选未优于基准，保留基准。"
                )
                if rejected and context.round >= context.task.max_rounds:
                    reason += " 已达到轮次上限；候选仍被评估拒绝，按基准保留收束。"
                return CoordinatorDecision(
                    action="finalize",
                    reason=reason,
                    terminal_state="baseline_retained",
                    selected=baseline_view,
                    ranking=ranking,
                )
            # Baseline infeasible and a candidate is feasible: the best
            # candidate is the only defensible choice.
            return CoordinatorDecision(
                action="finalize",
                reason=(
                    f"基准违反硬约束，不能作为可行回退；候选 {best['candidate_id']} 是本次评估中"
                    "唯一可用的可行方案。"
                ),
                terminal_state="accepted",
                selected=best,
                ranking=ranking,
            )

        # No feasible view at all.
        if rejected and context.round >= context.task.max_rounds:
            return CoordinatorDecision(
                action="finalize",
                reason=(
                    "已达到轮次上限，仍有候选被评估拒绝且没有可行方案；"
                    "这是有界停止，不是成功。"
                ),
                terminal_state="limit_reached",
                selected=None,
                ranking=ranking,
            )
        if rejected:
            return CoordinatorDecision(
                action="finalize",
                reason="所有候选均未通过硬约束，且评估建议的方向已没有可执行的合法组合。",
                terminal_state="no_feasible_candidate",
                selected=None,
                ranking=ranking,
            )
        return CoordinatorDecision(
            action="finalize",
            reason="没有任何候选通过评估，也没有可行基准。",
            terminal_state="no_feasible_candidate",
            selected=None,
            ranking=ranking,
        )

    def _baseline_feasible(self, task: TaskSpec, baseline: Mapping[str, Any]) -> bool:
        violations = baseline.get("violations") or {}
        if any(int(value) != 0 for value in violations.values()):
            return False
        if task.min_service_rate is not None:
            return float(baseline["metrics"]["service_rate"]) + ABS_TOL >= float(task.min_service_rate)
        return True

    def _view(
        self,
        kind: str,
        candidate: Mapping[str, Any] | None,
        strategy: str,
        budget: int,
        result: Mapping[str, Any],
    ) -> dict[str, Any]:
        metrics = result["metrics"]
        return {
            "kind": kind,
            "candidate_id": candidate.get("candidate_id") if candidate else None,
            "strategy": strategy,
            "budget": int(budget),
            "result_id": result["result_id"],
            "metrics": {
                "unmet_requests": int(metrics["unmet_requests"]),
                "distance_bike_km": float(metrics["distance_bike_km"]),
                "composite_cost": float(metrics["composite_cost"]),
                "service_rate": float(metrics["service_rate"]),
            },
            "violations": {key: int(value) for key, value in (result.get("violations") or {}).items()},
            "deltas": dict(result.get("deltas") or {}),
        }

    def _metrics_match(
        self, actual: Mapping[str, Any] | None, reported: Mapping[str, Any] | None
    ) -> bool:
        if not isinstance(actual, Mapping) or not isinstance(reported, Mapping):
            return False
        for key in ("unmet_requests", "distance_bike_km", "composite_cost", "service_rate"):
            if key not in actual or key not in reported:
                return False
            left, right = actual[key], reported[key]
            if isinstance(left, int) and isinstance(right, int):
                if left != right:
                    return False
            elif not _close(left, right):
                return False
        return True

    def _compare(self, left: Mapping[str, Any], right: Mapping[str, Any]) -> int:
        lc, rc = left["metrics"], right["metrics"]
        if not _close(lc["composite_cost"], rc["composite_cost"]):
            return -1 if lc["composite_cost"] < rc["composite_cost"] else 1
        if not _close(lc["service_rate"], rc["service_rate"]):
            return -1 if lc["service_rate"] > rc["service_rate"] else 1
        if left["budget"] != right["budget"]:
            return -1 if left["budget"] < right["budget"] else 1
        if not _close(lc["distance_bike_km"], rc["distance_bike_km"]):
            return -1 if lc["distance_bike_km"] < rc["distance_bike_km"] else 1
        if left["strategy"] != right["strategy"]:
            return -1 if left["strategy"] < right["strategy"] else 1
        return 0

    def _strictly_better(
        self, candidate: Mapping[str, Any], baseline: Mapping[str, Any] | None
    ) -> bool:
        """Accepted requires a strict improvement, not merely a legal choice.

        The strategy-name tie-break is a deterministic *sort* rule; it is not
        evidence of improvement.  A candidate that ties the baseline on cost,
        service rate, budget and distance is therefore reported as
        ``baseline_retained``, not as an optimization win.
        """
        if baseline is None:
            return True
        lc, rc = candidate["metrics"], baseline["metrics"]
        if not _close(lc["composite_cost"], rc["composite_cost"]):
            return lc["composite_cost"] < rc["composite_cost"]
        if not _close(lc["service_rate"], rc["service_rate"]):
            return lc["service_rate"] > rc["service_rate"]
        if candidate["budget"] != baseline["budget"]:
            return candidate["budget"] < baseline["budget"]
        if not _close(lc["distance_bike_km"], rc["distance_bike_km"]):
            return lc["distance_bike_km"] < rc["distance_bike_km"]
        return False
