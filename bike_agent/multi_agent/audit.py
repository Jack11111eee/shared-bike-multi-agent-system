"""Per-run audit artefacts (F27).

One directory per run, never overwritten.  The files are written even when the
run fails, so a failed attempt still explains what actually happened instead of
vanishing.  Nothing here re-runs a tool or recomputes a metric: every value is
copied from the message log, the gateway's call log and the final decision.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from bike_agent.multi_agent.task import InputManifest, TaskSpec


def _json_default(value: Any) -> Any:
    if hasattr(value, "item"):
        return value.item()
    if hasattr(value, "isoformat"):
        return value.isoformat()
    return str(value)

def _dump(path: Path, payload: Any) -> int:
    text = json.dumps(payload, ensure_ascii=False, indent=2, default=_json_default)
    path.write_text(text + "\n", encoding="utf-8")
    return len(text.encode("utf-8"))

def _dump_lines(path: Path, rows: list[dict[str, Any]]) -> int:
    lines = [
        json.dumps(row, ensure_ascii=False, default=_json_default) for row in rows
    ]
    text = "\n".join(lines) + ("\n" if lines else "")
    path.write_text(text, encoding="utf-8")
    return len(text.encode("utf-8"))

def _fmt(value: Any, digits: int = 6) -> str:
    if isinstance(value, float):
        return f"{value:.{digits}f}"
    return str(value)

def _candidate_table(
    candidates: list[dict[str, Any]], baseline_metrics: dict[str, Any] | None = None
) -> str:
    if not candidates:
        return "_本次运行没有候选。_\n"
    lines = [
        "| 候选 | 轮次 | 策略 | 每时段预算 | 修订自 | 状态 | 综合代价 | 未满足 | 服务率 | 距离 (bike-km) | cost_delta | unmet_gain |",
        "|---|---|---|---|---|---|---|---|---|---|---|---|",
    ]
    for item in candidates:
        metrics = item.get("metrics") or {}
        deltas = item.get("deltas") or {}
        # F29-1 / §2.3: a higher budget buys distance as well as service, so the
        # resource change travels with the cost delta in every run report.
        distance = metrics.get("distance_bike_km")
        stock = (baseline_metrics or {}).get("distance_bike_km")
        if isinstance(distance, (int, float)) and isinstance(stock, (int, float)):
            distance_delta = f"{distance - stock:+,.2f}"
        else:
            distance_delta = "—"
        lines.append(
            "| {cid} | {round} | {strategy} | {budget} | {revision_of} | {status} | {cost} | {unmet} | {rate} | {distance} ({delta}) | {cost_delta} | {unmet_gain} |".format(
                cid=item.get("candidate_id", "—"),
                round=item.get("round", "—"),
                strategy=item.get("strategy", "—"),
                budget=item.get("budget", "—"),
                revision_of=item.get("revision_of") or "—",
                status=item.get("status", "—"),
                cost=_fmt(metrics.get("composite_cost")),
                unmet=_fmt(metrics.get("unmet_requests")),
                rate=_fmt(metrics.get("service_rate")),
                distance=_fmt(distance),
                delta=distance_delta,
                cost_delta=_fmt(deltas.get("cost_delta")),
                unmet_gain=_fmt(deltas.get("unmet_gain")),
            )
        )
    return "\n".join(lines) + "\n"

def render_report(
    *,
    run_id: str,
    task: TaskSpec,
    decision: dict[str, Any],
    candidates: list[dict[str, Any]],
    messages: list[dict[str, Any]],
    tool_calls: list[dict[str, Any]],
    started_at: str,
    finished_at: str,
) -> str:
    selected = decision.get("selected") or {}
    baseline = decision.get("baseline") or {}
    lines = [
        f"# 多智能体调度决策运行 {run_id}",
        "",
        (
            "> 规则驱动多智能体，LLM 未接入。所有数值为**模拟值**，来自原仿真引擎的实际运行；"
            "结论只代表本次已评估的有限候选，不声称全局最优或必然运营收益。"
        ),
        "",
        "## 任务",
        "",
        f"- 源：`{task.source}`",
        "- 目标：最小化综合代价（`composite_cost`）",
        f"- 基准策略：`{task.baseline_strategy}`，基准每时段预算：`{task.baseline_budget}`",
        f"- 允许策略：{', '.join(task.allowed_strategies)}",
        f"- 预算档位：{', '.join(str(value) for value in task.budget_options)}；硬上限：`{task.max_budget}`",
        f"- 服务率约束：{task.min_service_rate if task.min_service_rate is not None else '未设置'}",
        (
            f"- 轮次上限：{task.max_rounds}；每轮候选上限：{task.max_candidates_per_round}；"
            f"工具调用上限：{task.max_tool_calls}"
        ),
        f"- 模式：`{task.mode}`（离线、确定性）",
        "",
        "## 终态",
        "",
        f"- **{decision.get('terminal_state')}**：{decision.get('reason')}",
        "",
    ]
    if baseline:
        metrics = baseline.get("metrics") or {}
        lines += [
            "## 基准",
            "",
            f"- 结果 ID：`{baseline.get('result_id')}`",
            f"- 策略/预算：`{baseline.get('strategy')}` / `{baseline.get('budget')}`",
            (
                f"- 综合代价：{_fmt(metrics.get('composite_cost'))}；未满足：{_fmt(metrics.get('unmet_requests'))}；"
                f"服务率：{_fmt(metrics.get('service_rate'))}；"
                f"搬运距离：{_fmt(metrics.get('distance_bike_km'))} bike-km"
            ),
            f"- 基准可行：{'是' if baseline.get('feasible') else '否（不能作为可行回退）'}",
            f"- violations：`{json.dumps(baseline.get('violations') or {}, ensure_ascii=False)}`",
            "",
        ]
    if selected:
        lines += [
            "## 最终选择",
            "",
            f"- 类型：`{selected.get('kind')}`"
            + (f"；候选：`{selected.get('candidate_id')}`" if selected.get("candidate_id") else ""),
            f"- 策略/预算：`{selected.get('strategy')}` / `{selected.get('budget')}`",
            f"- 结果 ID：`{selected.get('result_id')}`",
            "",
        ]
    lines += [
        "## 候选比较",
        "",
        _candidate_table(candidates, (decision.get("baseline") or {}).get("metrics")),
        "",
    ]
    lines += [
        "## 运行统计",
        "",
        f"- 消息数：{len(messages)}；工具调用：{len(tool_calls)}",
        f"- 开始：{started_at}；结束：{finished_at}",
    ]
    timing = decision.get("timing") or {}
    if timing:
        lines += [
            (
                f"- 耗时拆分：任务 {_fmt(timing.get('task_elapsed_ms'))} ms ＝ 后端仿真 "
                f"{_fmt(timing.get('backend_simulation_ms'))} ms ＋ 诊断工具 "
                f"{_fmt(timing.get('diagnosis_tool_ms'))} ms ＋ 消息调度 "
                f"{_fmt(timing.get('scheduling_overhead_ms'))} ms"
            ),
        ]
    lines += [
        "- 输入冻结与数值追溯见 `input_manifest.json`、`tool_calls.jsonl`、`evaluations.json`。",
        "",
    ]
    return "\n".join(lines)

def write_run(
    run_dir: Path,
    *,
    run_id: str,
    task: TaskSpec,
    manifest: InputManifest,
    config: dict[str, Any],
    store,
    gateway,
    candidates: list[dict[str, Any]],
    evaluations: list[dict[str, Any]],
    decision: dict[str, Any],
    started_at: str,
    finished_at: str,
    extra: dict[str, Any] | None = None,
    extra_events: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """Write every artefact and return a size/index summary."""
    run_dir.mkdir(parents=True, exist_ok=True)
    sizes: dict[str, int] = {}
    messages = store.to_records() if store is not None else []
    tool_calls = gateway.tool_calls if gateway is not None else []
    rejections = store.rejections if store is not None else []
    events = list(gateway.events if gateway is not None else []) + list(extra_events or [])
    sizes["task.json"] = _dump(
        run_dir / "task.json",
        {
            "task": task.to_dict(),
            "config_snapshot": {
                "seed": config.get("seed"),
                "primary_source": config.get("primary_source"),
                "simulation": config.get("simulation", {}),
                "forecast": config.get("forecast", {}),
                "risk": config.get("risk", {}),
                "source": config.get("sources", {}).get(task.source, {}),
            },
            "run_id": run_id,
        },
    )
    sizes["input_manifest.json"] = _dump(run_dir / "input_manifest.json", manifest.to_dict())
    sizes["messages.jsonl"] = _dump_lines(run_dir / "messages.jsonl", messages)
    sizes["rejections.jsonl"] = _dump_lines(run_dir / "rejections.jsonl", rejections)
    sizes["tool_calls.jsonl"] = _dump_lines(run_dir / "tool_calls.jsonl", tool_calls)
    sizes["events.jsonl"] = _dump_lines(run_dir / "events.jsonl", events)
    sizes["candidates.json"] = _dump(run_dir / "candidates.json", candidates)
    sizes["evaluations.json"] = _dump(run_dir / "evaluations.json", evaluations)
    sizes["decision.json"] = _dump(run_dir / "decision.json", decision)
    report = render_report(
        run_id=run_id,
        task=task,
        decision=decision,
        candidates=candidates,
        messages=messages,
        tool_calls=tool_calls,
        started_at=started_at,
        finished_at=finished_at,
    )
    (run_dir / "report.md").write_text(report, encoding="utf-8")
    sizes["report.md"] = len(report.encode("utf-8"))

    total = sum(sizes.values())
    index = {
        "run_id": run_id,
        "task_id": task.task_id,
        "source": task.source,
        "mode": task.mode,
        "rule_based": True,
        "llm_connected": False,
        "terminal_state": decision.get("terminal_state"),
        "started_at": started_at,
        "finished_at": finished_at,
        "input_digest": manifest.digest,
        "tool_calls": len(tool_calls),
        "cache_hits": int(getattr(gateway, "cache_hits", 0)) if gateway is not None else 0,
        "backend_executions": int(getattr(gateway, "backend_executions", 0)) if gateway is not None else 0,
        "messages": len(messages),
        "rejections": len(rejections),
        "candidates": len(candidates),
        # NF-39: never present one wall-clock number as "the agent's speed".
        "timing": decision.get("timing") or {},
        "bytes": sizes,
        "total_bytes": total,
        **(extra or {}),
    }
    sizes["run.json"] = _dump(run_dir / "run.json", index)
    index["total_bytes"] = sum(sizes.values())
    _dump(run_dir / "run.json", index)
    return index
