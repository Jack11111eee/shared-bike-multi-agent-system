"""Phase 6 F28: the offline collaboration page.

The page is built from the audit artefacts of *real* runs.  It never invents a
"thinking" turn, never synthesises a subtitle, and never runs an agent when the
page is opened: every node carries the ``message_id`` or ``tool_call_id`` of a
record that exists in ``outputs/multi_agent/<run_id>/``.

Two choices are deliberate:

* The timeline is prerendered in Python.  The browser script only toggles the
  detail panel and fills it from the embedded JSON, so the page still makes
  sense -- and can be checked -- without executing JavaScript.
* Tool-call nodes are placed before the message that cites them, using the
  message's ``evidence_refs``.  That is the real logical order: a role saw the
  tool result, then spoke.
"""

from __future__ import annotations

import html
import json
from pathlib import Path
from typing import Any

import pandas as pd

from bike_agent.config import Layout, source_config
from bike_agent.multi_agent.contracts import SUCCESS_STATES

ROLES = (
    ("coordinator", "协调智能体", "拆分任务、管理轮次、复核硬门槛并收敛；不直接调用业务工具。", "无"),
    ("diagnosis", "诊断智能体", "读取基准轨迹，定位损失时段，查询预测与风险；区分证据与待核查项。", "get_forecast, get_risk_ranking"),
    ("dispatch", "方案智能体", "提出合法的策略/预算候选；收到反馈后修改字段或说明为何不可改。", "无"),
    ("evaluation", "评估智能体", "实际运行候选，返回约束、指标、差异与接受/退回理由。", "evaluate_dispatch_candidate"),
)

def _read_json(path: Path, default: Any = None) -> Any:
    if not path.exists():
        return default
    return json.loads(path.read_text(encoding="utf-8"))

def _read_jsonl(path: Path) -> list[dict[str, Any]]:
    if not path.exists():
        return []
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]

def _frame_index(layout: Layout, source: str) -> dict[str, int]:
    path = layout.sim_path(source, "rule", "csv")
    if not path.exists():
        return {}
    frame = pd.read_csv(path)
    frame["time_bucket"] = pd.to_datetime(frame["time_bucket"])
    buckets = sorted(frame["time_bucket"].unique())
    return {str(pd.Timestamp(bucket)): index for index, bucket in enumerate(buckets)}

def _valid_cells(config: dict[str, Any], layout: Layout, source: str) -> set[str]:
    path = layout.cells_path(source, int(source_config(config, source)["cell_size_m"]))
    if not path.exists():
        return set()
    return {str(value) for value in pd.read_parquet(path)["cell_id"]}

def discover_runs(outputs: Path) -> list[dict[str, Any]]:
    """Every complete run directory, newest first; a run without a decision is skipped."""
    root = outputs / "multi_agent"
    runs: list[dict[str, Any]] = []
    if not root.exists():
        return runs
    for run_dir in sorted(path for path in root.iterdir() if path.is_dir()):
        decision = _read_json(run_dir / "decision.json")
        if not isinstance(decision, dict):
            continue
        index = _read_json(run_dir / "run.json", {}) or {}
        runs.append(
            {
                "run_dir": run_dir,
                "run_id": index.get("run_id") or run_dir.name,
                "source": decision.get("source") or index.get("source"),
                "started_at": index.get("started_at") or decision.get("started_at") or "",
                "decision": decision,
                "index": index,
                "messages": _read_jsonl(run_dir / "messages.jsonl"),
                "tool_calls": _read_jsonl(run_dir / "tool_calls.jsonl"),
                "candidates": _read_json(run_dir / "candidates.json", []) or [],
                "evaluations": _read_json(run_dir / "evaluations.json", []) or [],
            }
        )
    runs.sort(key=lambda run: (run["started_at"], run["run_id"]), reverse=True)
    return runs

#: When several completed runs exist for a source, show a successful decision
#: first; ``no_feasible_candidate`` is a real result but a poor default.
_PAGE_PRIORITY = {"accepted": 0, "baseline_retained": 1, "no_feasible_candidate": 2}

def select_runs(runs: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    """One run per source: the most informative normally-completed run.

    ``runs`` is newest-first, so a stable sort by terminal state keeps the
    newest run inside each state.  A failed attempt is used only when no run
    completed normally -- the page's job is to show a real decision.
    """
    selected: dict[str, dict[str, Any]] = {}
    for source in ("xm", "sh"):
        candidates = [run for run in runs if run.get("source") == source]
        if not candidates:
            continue
        completed = [
            run
            for run in candidates
            if run["decision"].get("terminal_state") in SUCCESS_STATES
        ]
        pool = completed or candidates
        selected[source] = min(
            pool, key=lambda run: _PAGE_PRIORITY.get(run["decision"].get("terminal_state"), 3)
        )
    return selected

def page_runs(runs: list[dict[str, Any]]) -> tuple[list[dict[str, Any]], dict[str, str]]:
    """The runs the page ships, plus which one is shown first per source.

    One run per source would hide a real outcome: the newest completed XM run
    is ``accepted`` only because a higher budget was requested, while the
    default-budget run is ``baseline_retained`` and another legal request has no
    feasible candidate at all.  Shipping one run per terminal state (and letting
    the reader switch) makes "no improvement is a normal result" visible without
    inventing anything -- every section is a complete audit directory.
    """
    selected = select_runs(runs)
    ordered: list[dict[str, Any]] = []
    shown: dict[str, str] = {}
    for source in ("xm", "sh"):
        run = selected.get(source)
        if run is None:
            continue
        ordered.append(run)
        shown[source] = run["run_id"]
        extras: list[dict[str, Any]] = []
        for state in ("accepted", "baseline_retained", "no_feasible_candidate"):
            if state == run["decision"].get("terminal_state"):
                continue
            alternative = next(
                (
                    item
                    for item in runs
                    if item.get("source") == source
                    and item["run_id"] != run["run_id"]
                    and item["decision"].get("terminal_state") == state
                ),
                None,
            )
            if alternative is not None:
                extras.append(alternative)
        ordered.extend(extras[:2])
    return ordered, shown

def _tool_summary(call: dict[str, Any]) -> dict[str, Any]:
    output = call.get("output") or {}
    summary: dict[str, Any] = {
        "tool_call_id": call.get("tool_call_id"),
        "role": call.get("role"),
        "tool": call.get("tool"),
        "input": call.get("input"),
        "status": call.get("status"),
        "cache_hit": bool(call.get("cache_hit")),
        "attempts": call.get("attempts"),
        "elapsed_ms": call.get("elapsed_ms"),
        "result_id": output.get("result_id"),
        "metrics": output.get("metrics"),
        "violations": output.get("violations"),
        "time_bucket": output.get("time_bucket"),
        "trajectory_points": len(output.get("trajectory") or []),
        "error": call.get("error"),
    }
    if call.get("tool") == "get_risk_ranking":
        summary["ranking_top"] = [
            {"rank": row.get("rank"), "cell_id": row.get("cell_id"), "score": row.get("score")}
            for row in (output.get("ranking") or [])[:5]
        ]
    if call.get("tool") == "get_forecast":
        summary["n_forecasts"] = len(output.get("forecasts") or [])
        summary["confidence"] = output.get("confidence")
    return summary

def _focus_links(
    run: dict[str, Any], config: dict[str, Any], layout: Layout, source: str
) -> list[dict[str, Any]]:
    diagnosis = next(
        (message for message in run["messages"] if message.get("type") == "diagnosis"), None
    )
    if diagnosis is None:
        return []
    payload = diagnosis.get("payload") or {}
    frames = _frame_index(layout, source)
    valid_cells = _valid_cells(config, layout, source)
    links: list[dict[str, Any]] = []
    focus_cells = [
        {
            "cell_id": cell.get("cell_id"),
            # F26: a forecast is not a ledger.  Carry the caveat to the page so
            # a deep link cannot be read as a confirmed loss location.
            "evidence_kind": cell.get("evidence_kind") or "possible_related",
            "reason": cell.get("reason"),
        }
        for cell in payload.get("focus_cells") or []
    ]
    for bucket in payload.get("focus_buckets") or []:
        time_bucket = str(bucket.get("time_bucket"))
        frame = frames.get(time_bucket)
        link: dict[str, Any] = {
            "time_bucket": time_bucket,
            "unmet_requests": bucket.get("unmet_requests"),
            "frame": frame,
            "cells": [],
            "player_href": None,
            "workbench_href": None,
        }
        if frame is not None:
            link["player_href"] = f"{source}/player.html#frame={frame}"
            link["workbench_href"] = f"{source}/workbench.html#frame={frame}"
        for cell in focus_cells:
            cell_id = cell["cell_id"]
            link["cells"].append(
                {
                    "cell_id": cell_id,
                    "evidence_kind": cell["evidence_kind"],
                    "reason": cell["reason"],
                    "valid": cell_id in valid_cells,
                    "workbench_href": (
                        f"{source}/workbench.html#frame={frame}&cell={cell_id}"
                        if frame is not None and cell_id in valid_cells
                        else None
                    ),
                }
            )
        links.append(link)
    return links

def _run_payload(run: dict[str, Any], config: dict[str, Any], layout: Layout) -> dict[str, Any]:
    source = run["source"]
    decision = run["decision"]
    return {
        "run_id": run["run_id"],
        "source": source,
        "mode": decision.get("mode"),
        "rule_based": bool(decision.get("rule_based", True)),
        "llm_connected": bool(decision.get("llm_connected", False)),
        "terminal_state": decision.get("terminal_state"),
        "reason": decision.get("reason"),
        "started_at": decision.get("started_at"),
        "finished_at": decision.get("finished_at"),
        "input_digest": decision.get("input_digest"),
        "task": _read_json(run["run_dir"] / "task.json", {}).get("task"),
        "roles": [
            {"name": name, "label": label, "responsibility": responsibility, "tools": tools}
            for name, label, responsibility, tools in ROLES
        ],
        "messages": run["messages"],
        "tool_calls": [_tool_summary(call) for call in run["tool_calls"]],
        "candidates": run["candidates"],
        "evaluations": run["evaluations"],
        "decision": {
            "terminal_state": decision.get("terminal_state"),
            "reason": decision.get("reason"),
            "selected": decision.get("selected"),
            "baseline": decision.get("baseline"),
            "ranking": decision.get("ranking"),
            "limits": decision.get("limits"),
            "timing": decision.get("timing"),
        },
        "focus": _focus_links(run, config, layout, source),
    }

def _esc(value: Any) -> str:
    return html.escape("" if value is None else str(value))

def _fmt(value: Any, digits: int = 4) -> str:
    if value is None:
        return "—"
    if isinstance(value, float):
        return f"{value:,.{digits}f}"
    return f"{value:,}" if isinstance(value, int) else str(value)

def _timeline_nodes(run: dict[str, Any]) -> list[dict[str, Any]]:
    tool_by_id = {call["tool_call_id"]: call for call in run["tool_calls"]}
    nodes: list[dict[str, Any]] = []
    shown: set[str] = set()
    for message in run["messages"]:
        for reference in message.get("evidence_refs") or []:
            if reference in tool_by_id and reference not in shown:
                shown.add(reference)
                nodes.append({"kind": "tool", "id": reference})
        nodes.append({"kind": "message", "id": message["message_id"]})
    for call in run["tool_calls"]:
        if call["tool_call_id"] not in shown:
            shown.add(call["tool_call_id"])
            nodes.append({"kind": "tool", "id": call["tool_call_id"]})
    return nodes

def _candidate_table(candidates: list[dict[str, Any]]) -> str:
    if not candidates:
        return '<p class="empty">本次运行没有候选。</p>'
    rows = []
    for item in candidates:
        metrics = item.get("metrics") or {}
        deltas = item.get("deltas") or {}
        rows.append(
            "<tr>"
            f'<td><button type="button" class="candidate-link" data-candidate-id="{_esc(item.get("candidate_id"))}">'
            f'<code>{_esc(item.get("candidate_id"))}</code></button></td>'
            f'<td>{_esc(item.get("round"))}</td>'
            f'<td>{_esc(item.get("strategy"))}</td>'
            f'<td>{_esc(item.get("budget"))}</td>'
            f'<td>{_esc(item.get("revision_of") or "—")}</td>'
            f'<td class="state {_esc(item.get("status"))}">{_esc(item.get("status"))}</td>'
            f'<td>{_fmt(metrics.get("service_rate"), 6)}</td>'
            f'<td>{_fmt(metrics.get("unmet_requests"))}</td>'
            f'<td>{_fmt(metrics.get("distance_bike_km"))}</td>'
            f'<td>{_fmt(metrics.get("composite_cost"))}</td>'
            f'<td>{_fmt(deltas.get("cost_delta"), 6)}</td>'
            f'<td>{_fmt(deltas.get("unmet_gain"))}</td>'
            "</tr>"
        )
    return (
        '<table class="compare"><thead><tr>'
        "<th>候选</th><th>轮次</th><th>策略</th><th>每时段预算</th><th>修订自</th><th>状态</th>"
        "<th>服务率</th><th>未满足</th><th>搬运距离</th><th>综合代价</th><th>cost_delta</th><th>unmet_gain</th>"
        "</tr></thead><tbody>" + "".join(rows) + "</tbody></table>"
    )

def _focus_markup(run: dict[str, Any]) -> str:
    items = []
    for link in run["focus"]:
        if link.get("player_href") is None:
            items.append(
                f'<li><span class="empty">时段 {_esc(link["time_bucket"])} 没有可用的回放帧；不伪造定位。</span></li>'
            )
            continue
        cells = []
        for cell in link.get("cells") or []:
            caveat = (
                ' <span class="caveat">（可能相关，预测非实际损失，待核查）</span>'
                if cell.get("evidence_kind") == "possible_related"
                else ""
            )
            if cell.get("workbench_href"):
                cells.append(
                    f'<a href="{_esc(cell["workbench_href"])}" data-cell="{_esc(cell["cell_id"])}">'
                    f'工作台 · {_esc(cell["cell_id"])}</a>{caveat}'
                )
            else:
                cells.append(f'<span class="empty">{_esc(cell["cell_id"])}（无有效格级定位）</span>')
        items.append(
            "<li>"
            f'<a href="{_esc(link["player_href"])}" data-bucket="{_esc(link["time_bucket"])}">'
            f'播放器 · 时段 {_esc(link["time_bucket"])}（帧 {_esc(link["frame"])}）</a> '
            + " ".join(cells)
            + "</li>"
        )
    if not items:
        return '<p class="empty">诊断没有给出可定位的时段。</p>'
    return "<ul class=\"focus\">" + "".join(items) + "</ul>"

def _node_markup(run: dict[str, Any], node: dict[str, Any]) -> str:
    if node["kind"] == "message":
        message = next(item for item in run["messages"] if item["message_id"] == node["id"])
        refs = " ".join(
            f'<span class="chip" data-ref="{_esc(reference)}">{_esc(reference)}</span>'
            for reference in message.get("evidence_refs") or []
        )
        return (
            f'<li class="node message" data-message-id="{_esc(message["message_id"])}">'
            f'<button type="button" class="node-head">'
            f'<span class="seq">{_esc(message.get("sequence"))}</span>'
            f'<span class="who">{_esc(message.get("sender"))} → {_esc(message.get("recipient"))}</span>'
            f'<span class="type">{_esc(message.get("type"))}</span>'
            f'<span class="round">r{_esc(message.get("round"))}</span>'
            f'<code>{_esc(message["message_id"])}</code>'
            "</button>"
            f'<div class="node-body" hidden><p class="refs">证据：{refs or "—"}</p>'
            '<pre class="node-detail"></pre></div></li>'
        )
    call = next(item for item in run["tool_calls"] if item["tool_call_id"] == node["id"])
    return (
        f'<li class="node tool" data-tool-call-id="{_esc(call["tool_call_id"])}">'
        f'<button type="button" class="node-head">'
        f'<span class="seq">工具</span>'
        f'<span class="who">{_esc(call.get("role"))}</span>'
        f'<span class="type">{_esc(call.get("tool"))}</span>'
        f'<span class="round">{"缓存" if call.get("cache_hit") else _esc(call.get("attempts")) + " 次"}</span>'
        f'<code>{_esc(call["tool_call_id"])}</code>'
        "</button>"
        '<div class="node-body" hidden><pre class="node-detail"></pre></div></li>'
    )

def _run_markup(run: dict[str, Any], payload_run: dict[str, Any]) -> str:
    decision = run["decision"]
    task = _read_json(run["run_dir"] / "task.json", {}).get("task") or {}
    selected = decision.get("selected") or {}
    state = decision.get("terminal_state")
    selected_text = (
        f"{selected.get('strategy')} / {selected.get('budget')}"
        if selected.get("strategy")
        else "无（未选择候选）"
    )
    roles = "".join(
        f'<li><strong>{_esc(label)}</strong>（{_esc(name)}）：{_esc(responsibility)}'
        f'<br><span class="tools">工具：{_esc(tools)}</span></li>'
        for name, label, responsibility, tools in ROLES
    )
    limits = decision.get("limits") or {}
    timing = decision.get("timing") or {}
    effort = (
        f"候选 {_esc(decision.get('candidate_count', len(run['candidates'])))} 个；"
        f"工具调用 {_esc(limits.get('tool_calls_used', len(run['tool_calls'])))} 次；"
        f"轮次 {_esc(limits.get('rounds_used'))} / {_esc(limits.get('max_rounds'))}"
    )
    if timing:
        effort += (
            f"；耗时 {_esc(_fmt(timing.get('task_elapsed_ms'), 1))} ms ＝ 后端仿真 "
            f"{_esc(_fmt(timing.get('backend_simulation_ms'), 1))} ms ＋ 诊断 "
            f"{_esc(_fmt(timing.get('diagnosis_tool_ms'), 1))} ms ＋ 调度 "
            f"{_esc(_fmt(timing.get('scheduling_overhead_ms'), 1))} ms"
        )
    return f"""
<section class="run" data-source="{_esc(run['source'])}" data-run-id="{_esc(run['run_id'])}">
  <h2>{_esc(run['source'].upper())} · {_esc(state)}</h2>
  <p class="notice"><strong>{_esc(decision.get('reason'))}</strong></p>
  <dl class="summary">
    <div><dt>运行</dt><dd><code>{_esc(run['run_id'])}</code></dd></div>
    <div><dt>模式</dt><dd>规则驱动多智能体，LLM 未接入（{_esc(decision.get('mode'))}）</dd></div>
    <div><dt>目标</dt><dd>{_esc(task.get('objective'))}</dd></div>
    <div><dt>基准 / 预算档</dt><dd>{_esc(task.get('baseline_strategy'))} / {_esc(task.get('baseline_budget'))}；
      可用 {_esc(task.get('budget_options'))}；上限 {_esc(task.get('max_budget'))}</dd></div>
    <div><dt>服务率约束</dt><dd>{_esc(task.get('min_service_rate'))}</dd></div>
    <div><dt>最终选择</dt><dd>{_esc(selected_text)}</dd></div>
    <div><dt>本次投入</dt><dd>{effort}</dd></div>
    <div><dt>离线复盘</dt><dd>所有指标为<strong>模拟值</strong>，只代表本次已评估的有限候选</dd></div>
  </dl>
  <h3>角色职责</h3>
  <ul class="roles">{roles}</ul>
  <h3>候选对比</h3>
  {_candidate_table(run['candidates'])}
  <h3>分析深链</h3>
  {_focus_markup(payload_run)}
  <h3>消息与工具时间线</h3>
  <ol class="timeline">
    {''.join(_node_markup(run, node) for node in _timeline_nodes(run))}
  </ol>
</section>
"""

CSS = """
:root{--bg:#f7f8fa;--fg:#1d2430;--muted:#5b6573;--line:#d9dee6;--card:#fff;--accent:#1f6feb;--ok:#1a7f37;--bad:#b3341f;--warn:#9a6700}
@media (prefers-color-scheme:dark){:root{--bg:#14181e;--fg:#e6e9ef;--muted:#a2acba;--line:#303844;--card:#1b2028;--accent:#6aa8ff;--ok:#56d364;--bad:#ff8a6b;--warn:#e3b341}}
*{box-sizing:border-box}
body{margin:0;background:var(--bg);color:var(--fg);font:14px/1.55 -apple-system,BlinkMacSystemFont,"Segoe UI","Noto Sans SC",sans-serif}
header{padding:24px;border-bottom:1px solid var(--line)}
header h1{margin:0 0 8px;font-size:22px}
header p{margin:4px 0;color:var(--muted)}
main{max-width:1180px;margin:0 auto;padding:20px}
.run{background:var(--card);border:1px solid var(--line);border-radius:10px;padding:18px;margin:18px 0}
.run h2{margin:0 0 6px;font-size:18px}
.run h3{margin:18px 0 8px;font-size:15px}
.notice{color:var(--muted)}
.summary{display:grid;grid-template-columns:repeat(auto-fit,minmax(260px,1fr));gap:8px 18px;margin:10px 0}
.summary dt{color:var(--muted);font-size:12px}
.summary dd{margin:0}
.roles{margin:0;padding-left:18px}
.roles li{margin:4px 0}
.tools{color:var(--muted);font-size:12px}
table.compare{width:100%;border-collapse:collapse;font-size:12px;overflow-wrap:anywhere}
table.compare th,table.compare td{border-bottom:1px solid var(--line);padding:5px 6px;text-align:right}
table.compare th:first-child,table.compare td:first-child,table.compare th:nth-child(3),table.compare td:nth-child(3){text-align:left}
.state.accepted{color:var(--ok);font-weight:600}.state.rejected{color:var(--bad)}.state.proposed{color:var(--warn)}
.candidate-link{background:none;border:0;color:var(--accent);cursor:pointer;padding:0;font:inherit}
#candidate-detail{background:var(--card);border:1px solid var(--line);border-radius:10px;padding:12px;margin:12px 0}
.focus{margin:0;padding-left:18px}.focus a{color:var(--accent)}
.empty{color:var(--muted);font-style:italic}
.caveat{color:var(--warn);font-size:12px}
.timeline{list-style:none;margin:0;padding:0}
.node{border-left:3px solid var(--line);margin:0 0 6px;padding:2px 0 2px 10px}
.node.tool{border-left-color:var(--warn)}
.node-head{display:flex;gap:10px;align-items:baseline;width:100%;background:none;border:0;color:inherit;text-align:left;padding:4px 0;cursor:pointer;font:inherit}
.node-head:hover{color:var(--accent)}
.seq{color:var(--muted);min-width:36px}
.who{min-width:170px}.type{min-width:120px;font-weight:600}
.round{color:var(--muted);min-width:52px}
.node-body{border:1px dashed var(--line);border-radius:6px;padding:8px;margin:4px 0 8px}
.node-detail{white-space:pre-wrap;word-break:break-word;font-size:12px;margin:6px 0 0;max-height:340px;overflow:auto}
.chip{display:inline-block;border:1px solid var(--line);border-radius:10px;padding:0 7px;margin:2px;font-size:11px;color:var(--muted)}
code{font-family:ui-monospace,SFMono-Regular,Menlo,monospace;font-size:12px}
.run-picker{display:block;margin:8px 0 4px;color:var(--muted);font-size:13px}
.run-picker{max-width:100%}
.run-picker select{font:inherit;color:inherit;background:var(--card);border:1px solid var(--line);border-radius:6px;padding:3px 6px;max-width:100%}
@media (max-width:520px){main{padding:10px}.run{padding:12px}.who{min-width:0}.type{min-width:0}.node-head{flex-wrap:wrap;gap:6px}}
@media (prefers-reduced-motion:reduce){*{transition:none!important;animation:none!important;scroll-behavior:auto!important}}
"""

JS = """
(function(){
  'use strict';
  var node = document.getElementById('collab-data');
  if(!node) return;
  var data = JSON.parse(node.textContent);
  // Message, tool-call and candidate ids restart in every run (msg-0001,
  // tc-0001, cand-r1-1), so a flat map would let one run's detail panel show
  // another run's record.  Keep one index per run and resolve the owning
  // <section class="run"> before any lookup.
  var byRun = {};
  data.runs.forEach(function(run){
    var messages = {}, tools = {}, candidates = {};
    run.messages.forEach(function(message){ messages[message.message_id] = message; });
    run.tool_calls.forEach(function(call){ tools[call.tool_call_id] = call; });
    run.candidates.forEach(function(candidate){ candidates[candidate.candidate_id] = candidate; });
    byRun[run.run_id] = {run: run, messages: messages, tools: tools, candidates: candidates};
  });
  function scopeOf(element){
    var section = element && element.closest ? element.closest('section.run') : null;
    var runId = section && section.getAttribute('data-run-id');
    return runId && Object.prototype.hasOwnProperty.call(byRun, runId) ? byRun[runId] : null;
  }
  function provenance(scope, kind, id){
    return 'run_id: ' + scope.run.run_id + '\\nsource: ' + scope.run.source +
      '\\n' + kind + ': ' + id + '\\n\\n';
  }
  function pretty(value){ return JSON.stringify(value, null, 2); }
  document.addEventListener('change', function(event){
    var picker = event.target.closest ? event.target.closest('.run-picker select') : null;
    if(!picker) return;
    var source = picker.getAttribute('data-source');
    var picked = picker.value;
    document.querySelectorAll('section.run[data-source="' + source + '"]').forEach(function(section){
      section.hidden = section.getAttribute('data-run-id') !== picked;
    });
  });
  document.addEventListener('click', function(event){
    var link = event.target.closest ? event.target.closest('.candidate-link') : null;
    if(link){
      var scope = scopeOf(link);
      var candidateId = link.getAttribute('data-candidate-id');
      var candidate = scope ? scope.candidates[candidateId] : null;
      var box = document.getElementById('candidate-detail');
      var body = document.getElementById('candidate-detail-body');
      if(candidate && box && body){
        body.textContent = provenance(scope, 'candidate_id', candidateId) + pretty(candidate);
        box.hidden = false;
      }
      return;
    }
    var head = event.target.closest ? event.target.closest('.node-head') : null;
    if(head){
      var item = head.parentNode;
      var body = item.querySelector('.node-body');
      var pre = item.querySelector('.node-detail');
      if(!body || !pre) return;
      if(body.hidden){
        var scope = scopeOf(head);
        if(!scope) return;
        var id = item.getAttribute('data-message-id');
        if(id && scope.messages[id]) pre.textContent = provenance(scope, 'message_id', id) + pretty(scope.messages[id]);
        id = item.getAttribute('data-tool-call-id');
        if(id && scope.tools[id]) pre.textContent = provenance(scope, 'tool_call_id', id) + pretty(scope.tools[id]);
        body.hidden = false;
      } else {
        body.hidden = true;
      }
      return;
    }
    var chip = event.target.closest ? event.target.closest('.chip') : null;
    if(chip){
      var ref = chip.getAttribute('data-ref');
      var scope = scopeOf(chip);
      if(!scope) return;
      var kind = scope.tools[ref] ? 'tool_call_id' : 'message_id';
      var target = scope.tools[ref] || scope.messages[ref];
      if(target){
        var holder = document.createElement('div');
        holder.className = 'node-body';
        var pre = document.createElement('pre');
        pre.className = 'node-detail';
        pre.textContent = provenance(scope, kind, ref) + pretty(target);
        holder.appendChild(pre);
        chip.parentNode.appendChild(holder);
      }
    }
  });
})();
"""

def render_page(payload: dict[str, Any], runs: list[dict[str, Any]]) -> str:
    payload_by_run = {run["run_id"]: run for run in payload.get("runs") or []}
    shown = payload.get("selected") or {}
    sections: list[str] = []
    for source in ("xm", "sh"):
        source_runs = [run for run in runs if run["source"] == source]
        if not source_runs:
            continue
        if len(source_runs) > 1:
            options = "".join(
                f'<option value="{_esc(run["run_id"])}"'
                f'{" selected" if shown.get(source) == run["run_id"] else ""}>'
                f'{_esc(run["run_id"])} · {_esc(run["decision"].get("terminal_state"))}</option>'
                for run in source_runs
            )
            sections.append(
                f'<label class="run-picker">{_esc(source.upper())} 运行 '
                f'<select data-source="{_esc(source)}">{options}</select></label>'
            )
        for run in source_runs:
            markup = _run_markup(run, payload_by_run.get(run["run_id"], {}))
            if shown.get(source) != run["run_id"]:
                markup = markup.replace('<section class="run"', '<section class="run" hidden', 1)
            sections.append(markup)
    body = "".join(sections)
    empty = (
        '<section class="run" id="empty"><h2>尚无运行记录</h2>'
        "<p>还没有可展示的多智能体运行。先执行 "
        "<code>bike-agent multi-agent --source xm --mode deterministic</code> 与 "
        "<code>--source sh</code>；本页不会用假记录填充。</p></section>"
        if not runs
        else ""
    )
    data_json = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).replace("</", "<\\/")
    return f"""<!doctype html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>多智能体协作过程 · bike-agent</title>
<style>{CSS}</style>
</head>
<body>
<header>
<h1>多智能体协作过程</h1>
<p><strong>规则驱动多智能体，LLM 未接入。</strong>页面来自 <code>outputs/multi_agent/&lt;run_id&gt;/</code> 的真实审计记录；
节点对应真实的 <code>message_id</code> 或 <code>tool_call_id</code>。所有业务数值为<strong>模拟值</strong>，
只代表本次已评估的有限候选，不声称全局最优或必然运营收益。</p>
<p><a href="index.html">全部数据源</a> · <a href="dashboard.html">总览页</a></p>
</header>
<main>
{empty}
{body}
<div id="candidate-detail" class="node-body" hidden><pre id="candidate-detail-body" class="node-detail"></pre></div>
</main>
<script type="application/json" id="collab-data">{data_json}</script>
<script>{JS}</script>
</body>
</html>
"""

def build_collab(
    config: dict[str, Any], layout: Layout, runs: list[dict[str, Any]] | None = None
) -> dict[str, Any]:
    """Write ``replay/collab.html`` from the real runs; return a size summary."""
    site_dir = layout.root / "replay"
    site_dir.mkdir(parents=True, exist_ok=True)
    if runs is None:
        runs = discover_runs(layout.outputs)
    ordered, shown = page_runs(runs)
    payload = {
        "schema_version": "1.0",
        "mode": "rule_driven_offline",
        "llm_connected": False,
        "sources": sorted({run["source"] for run in ordered}),
        "selected": shown,
        "runs": [_run_payload(run, config, layout) for run in ordered],
        "available_runs": len(runs),
    }
    page = render_page(payload, ordered)
    path = site_dir / "collab.html"
    path.write_text(page, encoding="utf-8")
    data_bytes = len(json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8"))
    return {
        "path": str(path),
        "bytes": len(page.encode("utf-8")),
        "data_bytes": data_bytes,
        "sources": payload["sources"],
        "n_runs": len(payload["runs"]),
        "run_ids": {run["source"]: run["run_id"] for run in ordered},
    }
