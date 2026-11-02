"""The bounded collaboration loop (F25 / F27).

One run is a deterministic sequence:

    baseline -> diagnosis -> proposal -> evaluation -> (revision -> evaluation)*
    -> final decision

The loop is bounded by the task itself: rounds, candidates per round, distinct
candidates, tool calls, per-tool timeout and the task deadline.  Every terminal
state is explicit, and a failed or bounded run is never rewritten as success.
"""

from __future__ import annotations

import copy
import os
import time
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from bike_agent.config import Layout
from bike_agent.multi_agent import audit
from bike_agent.multi_agent.contracts import SUCCESS_STATES
from bike_agent.multi_agent.gateway import (
    InputChangedError,
    MultiAgentGateway,
    PermissionDenied,
    ToolExecutionError,
    ToolLimitReached,
    ToolTimeout,
)
from bike_agent.multi_agent.messages import MessageRejected, MessageStore
from bike_agent.multi_agent.roles import (
    CoordinatorContext,
    CoordinatorDecision,
    CoordinatorRole,
    DiagnosisContext,
    DiagnosisRole,
    DispatchContext,
    DispatchRole,
    EvaluationContext,
    EvaluationRole,
)
from bike_agent.multi_agent.task import InputManifest, TaskSpec, freeze_inputs


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()

class ProposalRejected(RuntimeError):
    """The dispatch role produced something outside the task contract."""

@dataclass
class RunOutcome:
    run_id: str
    run_dir: Path
    terminal_state: str
    reason: str
    decision: dict[str, Any]
    index: dict[str, Any]
    exit_code: int

    def summary(self) -> dict[str, Any]:
        return {
            "run_id": self.run_id,
            "run_dir": str(self.run_dir),
            "terminal_state": self.terminal_state,
            "reason": self.reason,
            "exit_code": self.exit_code,
        }

class Orchestrator:
    """Run one task; all state lives on this object for the duration of the run."""

    def __init__(
        self,
        config: dict[str, Any],
        layout: Layout,
        task: TaskSpec,
        *,
        run_id: str | None = None,
        output_root: Path | None = None,
        roles: dict[str, Any] | None = None,
        clock: Callable[[], float] = time.monotonic,
        gateway_options: dict[str, Any] | None = None,
    ):
        self.config = config
        self.layout = layout
        self.task = task
        self.clock = clock or time.monotonic
        self.output_root = output_root or (layout.outputs / "multi_agent")
        self.run_id = run_id or self._new_run_id()
        self.run_dir = self._unique_dir(self.output_root / self.run_id)
        roles = roles or {}
        self.coordinator = roles.get("coordinator", CoordinatorRole())
        self.diagnosis = roles.get("diagnosis", DiagnosisRole())
        self.dispatch = roles.get("dispatch", DispatchRole())
        # ``evaluation=None`` is the ablation: without it no baseline and no
        # candidate verdict exists, so the coordinator cannot accept anything.
        self.evaluation = roles.get("evaluation", EvaluationRole())
        self.gateway_options = dict(gateway_options or {})
        self.store: MessageStore | None = None
        self.gateway: MultiAgentGateway | None = None
        self.manifest: InputManifest | None = None
        self.started_at: str | None = None
        self.candidates: list[dict[str, Any]] = []
        self.evaluations: list[dict[str, Any]] = []
        self.events: list[dict[str, Any]] = []

    # -- setup -------------------------------------------------------------
    def _new_run_id(self) -> str:
        stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
        return f"{stamp}-{self.task.source}-{os.urandom(3).hex()}"

    def _unique_dir(self, path: Path) -> Path:
        candidate = path
        suffix = 2
        while candidate.exists():
            candidate = path.with_name(f"{path.name}-{suffix}")
            suffix += 1
        candidate.mkdir(parents=True, exist_ok=False)
        self.run_id = candidate.name
        return candidate

    # -- run ---------------------------------------------------------------
    def run(self) -> RunOutcome:
        started_at = _now()
        self.started_at = started_at
        started_clock = self.clock()
        try:
            manifest = freeze_inputs(self.config, self.layout, self.task.source)
        except Exception as exc:  # noqa: BLE001 - missing inputs are a failed run, not a crash
            return self._fail_before_start(started_at, f"{type(exc).__name__}: {exc}")
        self.manifest = manifest

        self.store = MessageStore(self.run_id, self.task.task_id)
        self.store.bind_source(self.task.source)
        self.gateway = MultiAgentGateway(
            self.config,
            self.layout,
            self.task,
            manifest,
            self.store,
            deadline=started_clock + self.task.task_timeout_s,
            clock=self.clock,
            **self.gateway_options,
        )
        decision: dict[str, Any] | None = None
        baseline_output: dict[str, Any] | None = None
        baseline_item: dict[str, Any] | None = None
        last_message_id: str | None = None
        round_number = 0
        try:
            decision, baseline_output, baseline_item, last_message_id, round_number = self._loop(
                manifest, started_clock
            )
        except ToolLimitReached as exc:
            decision = self._decision_payload(
                terminal_state="limit_reached",
                reason=f"达到运行上限：{exc}",
                baseline_output=baseline_output,
                baseline_item=baseline_item,
                selected=None,
                ranking=(),
                round_number=round_number,
                started_clock=started_clock,
            )
        except (InputChangedError, PermissionDenied, ToolExecutionError, ToolTimeout) as exc:
            decision = self._decision_payload(
                terminal_state="failed",
                reason=f"{type(exc).__name__}: {exc}",
                baseline_output=baseline_output,
                baseline_item=baseline_item,
                selected=None,
                ranking=(),
                round_number=round_number,
                started_clock=started_clock,
            )
        except MessageRejected as exc:
            decision = self._decision_payload(
                terminal_state="failed",
                reason=f"协议拒绝消息：{exc}",
                baseline_output=baseline_output,
                baseline_item=baseline_item,
                selected=None,
                ranking=(),
                round_number=round_number,
                started_clock=started_clock,
            )
        except ProposalRejected as exc:
            decision = self._decision_payload(
                terminal_state="failed",
                reason=f"方案违反任务契约：{exc}",
                baseline_output=baseline_output,
                baseline_item=baseline_item,
                selected=None,
                ranking=(),
                round_number=round_number,
                started_clock=started_clock,
            )
        except KeyboardInterrupt:
            # F25-3: cancellation is a bounded stop, not a vanished run.  The
            # audit below still runs, so Ctrl-C leaves a decision and events.
            decision = self._decision_payload(
                terminal_state="failed",
                reason="运行被取消（KeyboardInterrupt）；已保留取消前发生的事件。",
                baseline_output=baseline_output,
                baseline_item=baseline_item,
                selected=None,
                ranking=(),
                round_number=round_number,
                started_clock=started_clock,
            )
        except Exception as exc:  # noqa: BLE001 - a failed run still leaves evidence
            decision = self._decision_payload(
                terminal_state="failed",
                reason=f"未处理错误：{type(exc).__name__}: {exc}",
                baseline_output=baseline_output,
                baseline_item=baseline_item,
                selected=None,
                ranking=(),
                round_number=round_number,
                started_clock=started_clock,
            )
        finally:
            # A run interrupted inside ``_loop`` never returns its round
            # counter, but the store already knows which round was opened.
            # Without this, Ctrl-C would report ``rounds_used: 0`` despite
            # having executed rounds.
            rounds_used = max(round_number, self.store.current_round if self.store else 0)
            if decision is None:
                decision = self._decision_payload(
                    terminal_state="failed",
                    reason="运行在产生决策前结束。",
                    baseline_output=baseline_output,
                    baseline_item=baseline_item,
                    selected=None,
                    ranking=(),
                    round_number=rounds_used,
                    started_clock=started_clock,
                )
            else:
                decision["limits"] = self._limits(rounds_used, started_clock)
            self._send_final(decision, last_message_id, rounds_used)
        finished_at = _now()
        decision["finished_at"] = finished_at
        decision["duration_s"] = round(self.clock() - started_clock, 6)
        decision["exit_code"] = 0 if decision["terminal_state"] in SUCCESS_STATES else 1
        selected_id = (decision.get("selected") or {}).get("candidate_id")
        for candidate in self.candidates:
            if selected_id and candidate.get("candidate_id") == selected_id:
                candidate["status"] = "selected"
        index = audit.write_run(
            self.run_dir,
            run_id=self.run_id,
            task=self.task,
            manifest=manifest,
            config=self.config,
            store=self.store,
            gateway=self.gateway,
            candidates=self.candidates,
            evaluations=self.evaluations,
            decision=decision,
            started_at=started_at,
            finished_at=finished_at,
            extra={"events": len(self.events) + len(getattr(self.gateway, "events", []))},
            extra_events=self.events,
        )
        return RunOutcome(
            run_id=self.run_id,
            run_dir=self.run_dir,
            terminal_state=decision["terminal_state"],
            reason=decision["reason"],
            decision=decision,
            index=index,
            exit_code=decision["exit_code"],
        )

    def _fail_before_start(self, started_at: str, reason: str) -> RunOutcome:
        decision = {
            "run_id": self.run_id,
            "task_id": self.task.task_id,
            "source": self.task.source,
            "mode": self.task.mode,
            "rule_based": True,
            "llm_connected": False,
            "terminal_state": "failed",
            "reason": reason,
            "selected": None,
            "baseline": None,
            "ranking": [],
            "limits": {},
            "started_at": started_at,
            "finished_at": _now(),
            "duration_s": 0.0,
            "exit_code": 1,
        }
        manifest = InputManifest(digest="", config_digest="", files=())
        index = audit.write_run(
            self.run_dir,
            run_id=self.run_id,
            task=self.task,
            manifest=manifest,
            config=self.config,
            store=None,
            gateway=None,
            candidates=[],
            evaluations=[],
            decision=decision,
            started_at=started_at,
            finished_at=decision["finished_at"],
            extra={"events": 1},
        )
        return RunOutcome(
            run_id=self.run_id,
            run_dir=self.run_dir,
            terminal_state="failed",
            reason=reason,
            decision=decision,
            index=index,
            exit_code=1,
        )

    # -- the loop ----------------------------------------------------------
    def _loop(
        self, manifest: InputManifest, started_clock: float
    ) -> tuple[dict[str, Any], dict[str, Any] | None, dict[str, Any] | None, str | None, int]:
        assert self.store is not None and self.gateway is not None
        store = self.store
        gateway = self.gateway
        task = self.task
        last_message_id: str | None = None
        baseline_output: dict[str, Any] | None = None
        baseline_item: dict[str, Any] | None = None
        round_number = 0

        # -- baseline ------------------------------------------------------
        assignment = self._send(
            "coordinator",
            "evaluation",
            "task_assignment",
            0,
            self.coordinator.assignment(
                "establish_baseline",
                "evaluation",
                0,
                {"strategy": task.baseline_strategy, "budget": task.baseline_budget},
            ),
            parent_id=last_message_id,
        )
        last_message_id = assignment["message_id"]
        if self.evaluation is None:
            return (
                self._decision_payload(
                    terminal_state="failed",
                    reason="评估角色被禁用：没有基准评估，协调角色不能签收任何候选。",
                    baseline_output=None,
                    baseline_item=None,
                    selected=None,
                    ranking=(),
                    round_number=0,
                    started_clock=started_clock,
                ),
                None,
                None,
                last_message_id,
                0,
            )
        baseline_result, baseline_item = self.evaluation.establish_baseline(task, gateway)
        baseline_output = baseline_result.output
        evaluation_message = self._send(
            "evaluation",
            "coordinator",
            "evaluation",
            0,
            {
                "baseline_result_id": baseline_output["result_id"],
                "evaluations": [baseline_item],
            },
            parent_id=last_message_id,
            evidence_refs=[baseline_result.tool_call_id],
        )
        last_message_id = evaluation_message["message_id"]

        # -- diagnosis -----------------------------------------------------
        assignment = self._send(
            "coordinator",
            "diagnosis",
            "task_assignment",
            0,
            self.coordinator.assignment("diagnose", "diagnosis", 0),
            parent_id=last_message_id,
        )
        last_message_id = assignment["message_id"]
        diagnosis_context = DiagnosisContext(
            task=task,
            round=0,
            baseline=baseline_output,
            baseline_tool_call_id=baseline_result.tool_call_id,
            inbox=tuple(store.for_role("diagnosis")),
        )
        diagnosis = self.diagnosis.handle(diagnosis_context, gateway)
        diagnosis_tool_ids = tuple(diagnosis.pop("_tool_call_ids", []))
        diagnosis_message = self._send(
            "diagnosis",
            "coordinator",
            "diagnosis",
            0,
            diagnosis,
            parent_id=last_message_id,
            evidence_refs=[baseline_result.tool_call_id, *diagnosis_tool_ids],
        )
        last_message_id = diagnosis_message["message_id"]

        # -- proposal/evaluation rounds -----------------------------------
        evaluated_keys: set[tuple[str, int]] = {(task.baseline_strategy, task.baseline_budget)}
        revised_candidate_ids: set[str] = set()
        feedback: dict[str, Any] | None = None
        round_number = 1
        previous_round: int | None = None
        decision: CoordinatorDecision | None = None
        while round_number <= task.max_rounds:
            if previous_round is not None and previous_round != round_number:
                store.close_round(previous_round)
            store.open_round(round_number)
            previous_round = round_number

            instruction = "revise" if feedback is not None else "propose"
            assignment = self._send(
                "coordinator",
                "dispatch",
                "task_assignment",
                round_number,
                self.coordinator.assignment(
                    instruction,
                    "dispatch",
                    round_number,
                    {"feedback": feedback} if feedback else None,
                ),
                parent_id=last_message_id,
            )
            last_message_id = assignment["message_id"]

            dispatch_context = DispatchContext(
                task=task,
                round=round_number,
                diagnosis=diagnosis,
                diagnosis_tool_call_ids=diagnosis_tool_ids,
                baseline=baseline_output,
                feedback=feedback,
                candidates_history=tuple(self.candidates),
                evaluated_keys=frozenset(evaluated_keys),
                inbox=tuple(store.for_role("dispatch")),
            )
            proposal = self.dispatch.handle(dispatch_context)
            proposal_refs = list(proposal.pop("_evidence_refs", []))
            proposal = self._validate_proposal(
                proposal, round_number, feedback, evaluated_keys
            )
            proposal_type = "revision_proposal" if feedback is not None else "proposal"
            if not proposal["candidates"]:
                proposal_message = self._send(
                    "dispatch",
                    "coordinator",
                    proposal_type,
                    round_number,
                    proposal,
                    parent_id=last_message_id,
                    evidence_refs=proposal_refs,
                )
                last_message_id = proposal_message["message_id"]
                decision = self._no_candidate_decision(
                    baseline_output, baseline_item, round_number, started_clock
                )
                break

            for candidate in proposal["candidates"]:
                self.candidates.append(
                    {
                        **copy.deepcopy(candidate),
                        "round": round_number,
                        "status": "proposed",
                        "result_id": None,
                        "metrics": None,
                        "violations": None,
                        "deltas": None,
                    }
                )
                evaluated_keys.add((candidate["strategy"], int(candidate["budget"])))
            proposal_message = self._send(
                "dispatch",
                "coordinator",
                proposal_type,
                round_number,
                proposal,
                parent_id=last_message_id,
                evidence_refs=proposal_refs,
            )
            last_message_id = proposal_message["message_id"]

            distinct = len({(item["strategy"], int(item["budget"])) for item in self.candidates})
            if distinct > task.max_distinct_candidates:
                decision = self._decision_payload(
                    terminal_state="limit_reached",
                    reason=(
                        f"基准之外的候选数 {distinct} 超过上限 {task.max_distinct_candidates}；"
                        "停止继续提案。"
                    ),
                    baseline_output=baseline_output,
                    baseline_item=baseline_item,
                    selected=None,
                    ranking=(),
                    round_number=round_number,
                    started_clock=started_clock,
                )
                break

            assignment = self._send(
                "coordinator",
                "evaluation",
                "task_assignment",
                round_number,
                self.coordinator.assignment(
                    "evaluate",
                    "evaluation",
                    round_number,
                    {"candidate_ids": [item["candidate_id"] for item in proposal["candidates"]]},
                ),
                parent_id=last_message_id,
            )
            last_message_id = assignment["message_id"]
            evaluation_context = EvaluationContext(
                task=task,
                round=round_number,
                candidates=tuple(proposal["candidates"]),
                baseline=baseline_output,
                baseline_tool_call_id=baseline_result.tool_call_id,
                inbox=tuple(store.for_role("evaluation")),
            )
            evaluation = self.evaluation.handle(evaluation_context, gateway)
            evaluation_refs = list(evaluation.pop("_evidence_refs", []))
            evaluation_message = self._send(
                "evaluation",
                "coordinator",
                "evaluation",
                round_number,
                evaluation,
                parent_id=last_message_id,
                evidence_refs=evaluation_refs,
            )
            last_message_id = evaluation_message["message_id"]
            self.evaluations.append(
                {
                    "round": round_number,
                    "message_id": evaluation_message["message_id"],
                    "baseline_result_id": evaluation["baseline_result_id"],
                    "evaluations": copy.deepcopy(evaluation["evaluations"]),
                }
            )
            self._apply_evaluations(evaluation["evaluations"])

            coordinator_context = CoordinatorContext(
                task=task,
                round=round_number,
                baseline=baseline_output,
                candidates=tuple(self.candidates),
                evaluations=tuple(
                    item
                    for record in self.evaluations
                    for item in record["evaluations"]
                    if item.get("candidate_id") is not None
                ),
                evidence=self._evidence(),
                limits=self._limits(round_number, started_clock),
                revised_candidate_ids=frozenset(revised_candidate_ids),
                inbox=tuple(store.for_role("coordinator")),
            )
            decision = self.coordinator.handle(coordinator_context)
            if decision.action == "finalize":
                break
            if decision.round is None or decision.round > task.max_rounds:
                decision = CoordinatorDecision(
                    action="finalize",
                    reason="轮次上限已到，仍有待修订的候选；按有界停止收束。",
                    terminal_state="limit_reached",
                )
                break

            rejected = next(
                (
                    item
                    for record in self.evaluations
                    for item in record["evaluations"]
                    if item.get("candidate_id") == decision.candidate_id
                ),
                None,
            )
            revised_candidate_ids.add(str(decision.candidate_id))
            revision_payload = {
                "candidate_id": str(decision.candidate_id),
                "defects": list(decision.defects),
                "suggested_revision": dict(decision.suggested_revision or {}),
            }
            revision_refs = [evaluation_message["message_id"]]
            if rejected and rejected.get("tool_call_id"):
                revision_refs.append(rejected["tool_call_id"])
            revision_message = self._send(
                "coordinator",
                "dispatch",
                "revision_request",
                round_number,
                revision_payload,
                parent_id=last_message_id,
                evidence_refs=revision_refs,
            )
            last_message_id = revision_message["message_id"]
            feedback = {**revision_payload, "round": decision.round}
            round_number = decision.round

        if decision is None:
            decision = CoordinatorDecision(
                action="finalize",
                reason="循环结束但没有产生决策。",
                terminal_state="failed",
            )
        payload = self._decision_payload(
            terminal_state=str(decision.terminal_state or "failed"),
            reason=decision.reason,
            baseline_output=baseline_output,
            baseline_item=baseline_item,
            selected=decision.selected,
            ranking=decision.ranking,
            round_number=round_number,
            started_clock=started_clock,
        )
        return payload, baseline_output, baseline_item, last_message_id, round_number

    # -- helpers -----------------------------------------------------------
    def _send(
        self,
        sender: str,
        recipient: str,
        type: str,
        round_number: int,
        payload: dict[str, Any],
        *,
        parent_id: str | None,
        evidence_refs: list[str] | tuple[str, ...] = (),
    ) -> dict[str, Any]:
        assert self.store is not None
        return self.store.send(
            sender=sender,
            recipient=recipient,
            type=type,
            round=round_number,
            payload=payload,
            parent_id=parent_id,
            evidence_refs=list(evidence_refs),
        )

    def _validate_proposal(
        self,
        proposal: dict[str, Any],
        round_number: int,
        feedback: dict[str, Any] | None,
        evaluated_keys: set[tuple[str, int]],
    ) -> dict[str, Any]:
        task = self.task
        candidates = list(proposal.get("candidates") or [])
        if len(candidates) > task.max_candidates_per_round:
            raise ProposalRejected(
                f"{len(candidates)} candidates in one round exceeds {task.max_candidates_per_round}"
            )
        seen: set[tuple[str, int]] = set()
        filtered: list[dict[str, Any]] = []
        for candidate in candidates:
            strategy = candidate.get("strategy")
            budget = candidate.get("budget")
            if strategy not in task.allowed_strategies:
                raise ProposalRejected(f"strategy {strategy!r} is not allowed")
            if not isinstance(budget, int) or isinstance(budget, bool):
                raise ProposalRejected("budget must be an integer")
            if budget not in task.budget_options or budget > task.max_budget:
                raise ProposalRejected(f"budget {budget} is not a legal option")
            key = (str(strategy), int(budget))
            if key in evaluated_keys or key in seen:
                self.events.append(
                    {
                        "kind": "duplicate_candidate",
                        "round": round_number,
                        "candidate": candidate.get("candidate_id"),
                        "strategy": strategy,
                        "budget": budget,
                        "at": _now(),
                    }
                )
                continue
            if feedback is not None and candidate.get("revision_of") != feedback.get("candidate_id"):
                raise ProposalRejected(
                    "a revision candidate must point at the rejected candidate via revision_of"
                )
            seen.add(key)
            filtered.append(candidate)
        if feedback is None and not filtered:
            raise ProposalRejected("the initial proposal must contain at least one legal candidate")
        if feedback is not None and filtered:
            if not proposal.get("changed_fields"):
                raise ProposalRejected("a revision must report the changed fields")
            rejected = next(
                (item for item in self.candidates if item.get("candidate_id") == feedback.get("candidate_id")),
                None,
            )
            if rejected is None:
                raise ProposalRejected("the revision points at an unknown candidate")
            changed = set(proposal["changed_fields"])
            actual = set()
            if filtered[0]["strategy"] != rejected["strategy"]:
                actual.add("strategy")
            if int(filtered[0]["budget"]) != int(rejected["budget"]):
                actual.add("budget")
            if not actual or not actual <= changed:
                raise ProposalRejected(
                    f"revision fields {sorted(changed)} do not match the actual change {sorted(actual)}"
                )
        proposal["candidates"] = filtered
        return proposal

    def _apply_evaluations(self, evaluations: list[dict[str, Any]]) -> None:
        for evaluation in evaluations:
            for candidate in self.candidates:
                if candidate.get("candidate_id") == evaluation.get("candidate_id"):
                    # "feasible" is a property of the evaluation; whether it was
                    # *selected* is decided later and recorded as "selected".
                    candidate["status"] = "feasible" if evaluation.get("feasible") else "rejected"
                    candidate["result_id"] = evaluation.get("result_id")
                    candidate["metrics"] = copy.deepcopy(evaluation.get("metrics"))
                    candidate["violations"] = copy.deepcopy(evaluation.get("violations"))
                    candidate["deltas"] = copy.deepcopy(evaluation.get("deltas"))
                    candidate["defects"] = list(evaluation.get("defects") or [])
                    break

    def _evidence(self) -> dict[str, Any]:
        evidence: dict[str, Any] = {}
        if self.gateway is not None:
            for call in self.gateway.tool_calls:
                if call.get("output") is None:
                    continue
                evidence[call["tool_call_id"]] = call["output"]
                result_id = call["output"].get("result_id")
                if result_id:
                    evidence[result_id] = call["output"]
        if self.store is not None:
            for message in self.store.messages:
                evidence[message["message_id"]] = message["payload"]
        return evidence

    def _limits(self, round_number: int, started_clock: float) -> dict[str, Any]:
        task = self.task
        gateway = self.gateway
        return {
            "max_rounds": task.max_rounds,
            "rounds_used": round_number,
            "max_candidates_per_round": task.max_candidates_per_round,
            "max_distinct_candidates": task.max_distinct_candidates,
            "distinct_candidates": len(
                {(item["strategy"], int(item["budget"])) for item in self.candidates}
            ),
            "max_tool_calls": task.max_tool_calls,
            "tool_calls_used": int(gateway.attempt_count) if gateway else 0,
            "tool_calls_recorded": len(gateway.tool_calls) if gateway else 0,
            "cache_hits": int(gateway.cache_hits) if gateway else 0,
            "backend_executions": int(gateway.backend_executions) if gateway else 0,
            "tool_timeout_s": task.tool_timeout_s,
            "task_timeout_s": task.task_timeout_s,
            "task_elapsed_s": round(self.clock() - started_clock, 6),
        }

    def _no_candidate_decision(
        self,
        baseline_output: dict[str, Any] | None,
        baseline_item: dict[str, Any] | None,
        round_number: int,
        started_clock: float,
    ) -> CoordinatorDecision:
        baseline_feasible = bool(baseline_item and baseline_item.get("feasible"))
        state = "baseline_retained" if baseline_feasible else "no_feasible_candidate"
        # Deliberately narrow: the revision follows the evaluation's stated
        # direction, so this says "no actionable revision left", not "no legal
        # combination exists".
        reason = (
            "评估给出的修订建议已没有可执行的合法组合；基准可行，保留基准。"
            if baseline_feasible
            else "评估给出的修订建议已没有可执行的合法组合，且基准本身违反硬约束，不能作为可行回退。"
        )
        return CoordinatorDecision(
            action="finalize",
            reason=reason,
            terminal_state=state,
            selected=None,
            ranking=(),
        )

    def _timing(self, started_clock: float) -> dict[str, Any]:
        """Split wall clock into backend simulation and message scheduling.

        NF-39: the run's cost must not be reported as one number that invites
        "the agent was fast" when nearly all of it was the engine.  Every tool
        call already carries ``elapsed_ms``; this only groups them by purpose
        and states the remainder explicitly as overhead.
        """
        calls = self.gateway.tool_calls if self.gateway else []
        simulation_ms = sum(
            float(call.get("elapsed_ms") or 0.0)
            for call in calls
            if call.get("tool") == "evaluate_dispatch_candidate"
        )
        diagnosis_ms = sum(
            float(call.get("elapsed_ms") or 0.0)
            for call in calls
            if call.get("tool") != "evaluate_dispatch_candidate"
        )
        total_ms = round((self.clock() - started_clock) * 1000.0, 3)
        return {
            "task_elapsed_ms": total_ms,
            "backend_simulation_ms": round(simulation_ms, 3),
            "diagnosis_tool_ms": round(diagnosis_ms, 3),
            # Message validation, sorting and audit writes; measured, not
            # inferred from a model of how long they "should" take.
            "scheduling_overhead_ms": round(
                max(0.0, total_ms - simulation_ms - diagnosis_ms), 3
            ),
            "note": "调度开销 = 任务总时长 − 工具耗时；不是页面加载时间",
        }

    def _decision_payload(
        self,
        *,
        terminal_state: str,
        reason: str,
        baseline_output: dict[str, Any] | None,
        baseline_item: dict[str, Any] | None,
        selected: Any,
        ranking: Any,
        round_number: int,
        started_clock: float,
    ) -> dict[str, Any]:
        task = self.task
        baseline_view = None
        if baseline_output is not None:
            baseline_view = {
                "kind": "baseline",
                "candidate_id": None,
                "strategy": task.baseline_strategy,
                "budget": task.baseline_budget,
                "result_id": baseline_output.get("result_id"),
                "metrics": copy.deepcopy(baseline_output.get("metrics")),
                "violations": copy.deepcopy(baseline_output.get("violations")),
                "feasible": bool(baseline_item and baseline_item.get("feasible")),
                "deltas": {"cost_delta": 0.0, "unmet_gain": 0, "service_rate_delta_pp": 0.0},
            }
        # ``baseline_retained`` means the baseline *is* the selection.  The
        # no-candidate shortcut cannot build a view of its own, so substitute
        # one here rather than emitting a decision that selects nothing.
        if (
            selected is None
            and terminal_state == "baseline_retained"
            and baseline_view is not None
            and baseline_view["feasible"]
        ):
            selected = baseline_view
        return {
            "run_id": self.run_id,
            "task_id": task.task_id,
            "source": task.source,
            "mode": task.mode,
            "rule_based": True,
            "llm_connected": False,
            "terminal_state": terminal_state,
            "reason": reason,
            "selected": copy.deepcopy(dict(selected)) if selected is not None else None,
            "baseline": baseline_view,
            "ranking": [copy.deepcopy(dict(item)) for item in (ranking or ())],
            "limits": self._limits(round_number, started_clock),
            "timing": self._timing(started_clock),
            "input_digest": self.manifest.digest if self.manifest is not None else "",
            "candidate_count": len(self.candidates),
            "tool_calls": len(self.gateway.tool_calls) if self.gateway else 0,
            "cache_hits": int(self.gateway.cache_hits) if self.gateway else 0,
            "backend_executions": int(self.gateway.backend_executions) if self.gateway else 0,
            "started_at": self.started_at or _now(),
        }

    def _send_final(
        self, decision: dict[str, Any], last_message_id: str | None, round_number: int
    ) -> None:
        if self.store is None:
            return
        # ``_loop`` returns the id only when it completes; every failure path
        # reaches here with ``None`` while the log already has messages.  A
        # final message must answer the last one, not claim to be the first.
        if last_message_id is None and self.store.messages:
            last_message_id = self.store.messages[-1]["message_id"]
        payload = {
            "terminal_state": decision["terminal_state"],
            "reason": decision["reason"],
            "selected": decision["selected"],
            "baseline": decision["baseline"],
            "ranking": decision["ranking"],
            "limits": decision["limits"],
        }
        evidence: list[str] = []
        if self.gateway is not None:
            for call in self.gateway.tool_calls:
                if call.get("output") and call["output"].get("result_id"):
                    evidence.append(call["tool_call_id"])
        try:
            self.store.send(
                sender="coordinator",
                recipient="coordinator",
                type="final",
                round=self.store.current_round,
                payload=payload,
                parent_id=last_message_id,
                evidence_refs=evidence[-1:] if evidence else [],
            )
        except MessageRejected as exc:
            self.store.reject(f"final message rejected: {exc}", payload)
