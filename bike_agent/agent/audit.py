"""The run record: every decision, its inputs and the numbers it quoted.

A record is a plain dict so it can be serialised, diffed and asserted on.  The
markdown view is generated from the same dict, so the readable log and the
machine-readable log can never drift apart (F11-4).
"""

from __future__ import annotations

import json
from typing import Any

SIMULATION_LABEL = "模拟"


def as_number(value: Any) -> str:
    """Render a number exactly the way ``json.dumps`` renders it.

    F11-5 requires every quoted number to be findable, as a string, inside a
    tool response.  Using the same serialisation here means the check is about
    provenance rather than about float formatting.
    """
    if isinstance(value, bool):
        return json.dumps(value)
    if isinstance(value, int):
        return str(value)
    if isinstance(value, float):
        return json.dumps(value)
    return str(value)


class RunRecord:
    """Accumulates one agent run."""

    def __init__(self, task: str, source: str, strategy: str):
        self.task = task
        self.source = source
        self.strategy = strategy
        self.steps: list[dict[str, Any]] = []
        self.conclusion = ""
        self.notes: list[str] = []

    # -- building ----------------------------------------------------------
    def add_calls(self, calls: list[dict[str, Any]]) -> None:
        for call in calls:
            self.steps.append(
                {
                    "index": len(self.steps),
                    "tool": call["tool"],
                    "input": call["input"],
                    "return_summary": _summarise(call["output"]),
                    "return": call["output"],
                }
            )

    def conclude(self, text: str) -> None:
        self.conclusion = text

    def note(self, text: str) -> None:
        self.notes.append(text)

    # -- views -------------------------------------------------------------
    def to_dict(self) -> dict[str, Any]:
        return {
            "task": self.task,
            "source": self.source,
            "strategy": self.strategy,
            "label": SIMULATION_LABEL,
            "steps": self.steps,
            "conclusion": self.conclusion,
            "notes": self.notes,
        }


def _summarise(payload: dict[str, Any]) -> str:
    """A one-line, tool-specific digest -- readable without opening the payload."""
    if "trajectory" in payload:
        trajectory = payload["trajectory"]
        metrics = payload.get("metrics", {})
        return (
            f"{payload.get('strategy')} 回放 {len(trajectory)} 个时间单元，"
            f"未满足 {metrics.get('unmet_requests')}，服务率 {metrics.get('service_rate')}，"
            f"综合代价 {metrics.get('composite_cost')}"
        )
    if "ranking" in payload:
        return (
            f"{payload.get('direction')} 清单 {len(payload['ranking'])} 条，"
            f"首条格子 {payload['ranking'][0]['cell_id'] if payload['ranking'] else 'n/a'}，"
            f"时间单元 {payload.get('time_bucket')}"
        )
    if "forecasts" in payload:
        return f"{payload.get('time_bucket')} 预测 {len(payload['forecasts'])} 格"
    return "已返回"


def render_markdown(record: dict[str, Any]) -> str:
    """The readable execution log (F11-4)."""
    lines = [
        f"# 智能体执行记录 · {record['task']}",
        "",
        f"> 本记录中的全部数字均为**{SIMULATION_LABEL}值**，来自同一次运行内工具返回体，"
        "不构成任何运营结论。",
        "",
        f"- 数据源：`{record['source']}`",
        f"- 策略：`{record['strategy']}`",
        f"- 调用次数：{len(record['steps'])}",
        "",
        "## 调用轨迹",
        "",
    ]
    for step in record["steps"]:
        arguments = json.dumps(step["input"], ensure_ascii=False, sort_keys=True)
        lines += [
            f"### {step['index'] + 1}. `{step['tool']}`",
            "",
            f"- 入参：`{arguments}`",
            f"- 返回摘要：{step['return_summary']}",
            "",
        ]
    lines += [
        "## 结论",
        "",
        record["conclusion"],
        "",
    ]
    if record["notes"]:
        lines += ["## 说明", ""]
        lines += [f"- {note}" for note in record["notes"]]
        lines.append("")
    return "\n".join(lines)
