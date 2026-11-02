"""Publish a self-contained, local decision workbench from real run records."""
from __future__ import annotations

import csv
import hashlib
import html
import json
import shutil
from pathlib import Path

from bike_agent.multi_agent.journal import encoded, write_json
from bike_agent.presentation.run_view import build_run_view, list_runs, resolve_run

ASSETS = Path(__file__).parent / "assets"
UI_FILES = ("index.html", "console.css", "console.js", "maps.js")
LABELS = {"accepted": "找到可行候选", "baseline_retained": "保留基准",
          "no_feasible_candidate": "本次未找到可行方案", "failed": "执行失败",
          "limit_reached": "达到运行上限"}


def report_html(markdown: str) -> str:
    """Render the deliberately small report vocabulary to readable print HTML."""
    parts, table, listing, code = [], False, False, False
    for line in markdown.splitlines():
        if line.startswith("~~~"):
            if listing:
                parts.append("</ul>"); listing = False
            parts.append("</code></pre>" if code else "<pre><code>")
            code = not code
            continue
        if code:
            parts.append(html.escape(line) + "\n")
            continue
        if table and not line.startswith("|"):
            parts.append("</tbody></table></div>"); table = False
        if listing and not line.startswith("- "):
            parts.append("</ul>"); listing = False
        if line.startswith("|"):
            cells = [html.escape(v.strip()) for v in line.strip("|").split("|")]
            if all(v and set(v) <= set("-: ") for v in cells):
                continue
            if not table:
                parts.append('<div class="table-wrap"><table><thead><tr>' +
                             "".join("<th>"+v+"</th>" for v in cells) + "</tr></thead><tbody>")
                table = True
            else:
                parts.append("<tr>"+"".join("<td>"+v+"</td>" for v in cells)+"</tr>")
        elif line.startswith("# "):
            parts.append("<h1>"+html.escape(line[2:])+"</h1>")
        elif line.startswith("## "):
            parts.append("<h2>"+html.escape(line[3:])+"</h2>")
        elif line.startswith("- "):
            if not listing:
                parts.append("<ul>"); listing = True
            parts.append("<li>"+html.escape(line[2:])+"</li>")
        elif line.strip():
            parts.append("<p>"+html.escape(line)+"</p>")
    if table: parts.append("</tbody></table></div>")
    if listing: parts.append("</ul>")
    return ('<!doctype html><html lang="zh-CN"><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">'
            '<title>VELO · 调度决策报告</title><style>'
            'body{max-width:1100px;margin:40px auto;padding:35px;font:15px/1.9 system-ui;color:#263c4c;background:#fff}'
            'header{color:#278273;font-size:12px;letter-spacing:3px;border-bottom:2px solid #23816f;padding-bottom:18px}'
            'h1{font-size:30px;margin:30px 0}h2{font-size:19px;margin-top:35px;border-bottom:1px solid #dfe8e6;padding-bottom:10px}'
            'p,li{overflow-wrap:anywhere}li{margin:8px 0}.table-wrap{overflow:auto}table{width:100%;border-collapse:collapse;font-size:12px}'
            'th,td{text-align:left;padding:12px;border-bottom:1px solid #e2e8e6}th{background:#edf5f1}'
            'pre{background:#f4f7f6;padding:20px;font:12px/1.7 monospace;white-space:pre-wrap}'
            'button{float:right;padding:9px 15px;background:#1c7e70;color:white;border:0;border-radius:5px;cursor:pointer}'
            '@media print{body{margin:0;padding:15px}button{display:none}h2{break-after:avoid}tr{break-inside:avoid}}</style>'
            '<header>VELO / DECISION REPORT <button onclick="print()">打印 / 保存 PDF</button></header>' +
            "".join(parts) + "</html>")


def render_report(view: dict) -> str:
    d, task = view["decision"], view["task"]
    selected = d.get("selected")
    lines = [
        "# 共享单车调度决策报告", "", f"- 运行：{view['identity']['run_id']}",
        f"- 模式：{view['identity']['mode']} / {view['identity']['authenticity_status']}",
        f"- 数据源：{task.get('source', '—')}；历史复盘，所有业务指标均为模拟值",
        f"- 结果：{LABELS.get(d['terminal_state'], d['terminal_state'])}", "",
        ("已选择 " + selected["candidate_id"] + "，通过仿真工具约束检查；见下表的已核验结果。"
         if selected else "本次没有可签收的最终方案；已发生的评估与错误保留在原始记录。"), "",
        f"- 每小时搬运上限：{task.get('max_budget', '—')} 辆",
        f"- 满足率下限：{task.get('min_service_rate', '未设置')}",
        "", "| 候选 | 策略 | 每小时上限（辆） | 模拟满足率 | 未满足（次） | 工作量（车·公里） | 综合代价 | 可行 |",
        "|---|---|---:|---:|---:|---:|---:|---|",
    ]
    for c in view["candidates"]:
        r = c.get("result")
        if r:
            m = r["metrics"]
            lines.append(f"| {c['candidate_id']} | {c['strategy']} | {c['budget']} | {m['service_rate']:.2%} | "
                         f"{m['unmet_requests']} | {m['distance_bike_km']:.2f} | {m['composite_cost']:.2f} | {'是' if r['feasible'] else '否'} |")
    if selected and selected.get("result"):
        r = selected["result"]
        lines += ["", f"最终选择：{selected['candidate_id']}；结果身份：{r['result_id']}"]
        delta = r.get("deltas", {})
        if delta:
            lines += [f"- 未满足请求减少：{delta['unmet_gain']:+d} 次",
                      f"- 满足率变化：{delta['service_rate_delta_pp']:+.2f} 个百分点",
                      f"- 工作量变化：{delta['distance_delta']:+.2f} 车·公里",
                      f"- 综合代价变化：{delta['cost_delta']:+.2f}"]
        lines += ["", "## 仿真假设", "", "初始库存、容量和成本权重见以下引擎输出：", "",
                  "~~~json", json.dumps(r["summary"]["assumptions"], ensure_ascii=False, indent=2), "~~~"]
    lines += ["", "## 决策证据与模型原始解释", "",
              "以下文字为模型交付原文；工具指标以上表为准，因果解释和推测需另行核查。", ""]
    for msg in view["messages"]:
        p = msg["payload"]
        lines += [f"- {msg['sender']} / {msg['message_id']} / {msg['model_call_id']}：{p.get('summary', p.get('reason', ''))}",
                  "  证据：" + "、".join(p.get("evidence_refs", []))]
    lines += ["", "## 适用边界", "",
              "- 历史订单驱动的仿真，不是实际运营收益；未满足请求不等于实测缺车量。",
              "- 模型在有限策略与预算组合中决策，逐格搬运由既有策略引擎计算。",
              "- 提高搬运上限带来的变化包含资源效应，不能全部归因于算法或多智能体。",
              "- 诊断读取基准历史回放，是复盘辅助决策，不是无未来信息的在线预测实验。",
              "- 搬运工作量为车辆数量与距离的累计，不是运输车辆实际行驶里程。", ""]
    return "\n".join(lines)


def publish_run(config, layout, run_id: str, *, include_tests=False) -> dict:
    root = resolve_run(layout, run_id)
    view = build_run_view(config, layout, run_id)
    if view["identity"]["authenticity_status"] == "test_fixture" and not include_tests:
        raise ValueError("测试夹具不能作为真实演示发布")
    target = root / "presentation"
    target.mkdir(exist_ok=True)
    write_json(target / "run-view.json", view)
    script = "window.BIKE_RUNS = " + encoded([view]).replace("<", "\\u003c") + ";\n"
    (target / "replay-data.js").write_text(script, encoding="utf-8")
    report = render_report(view)
    (target / "decision-report.md").write_text(report, encoding="utf-8")
    (root / "report.md").write_text(report, encoding="utf-8")
    (target / "decision-report.html").write_text(report_html(report), encoding="utf-8")
    chosen = view["decision"].get("selected")
    export_name = None
    if chosen and chosen["candidate_id"] in view["spatial"]:
        export_name = "dispatch-moves.csv"
        fields = ["run_id", "candidate_id", "result_id", "time_bucket", "from_cell",
                  "to_cell", "bikes", "distance_m", "distance_bike_km"]
        with (target / export_name).open("w", encoding="utf-8-sig", newline="") as stream:
            writer = csv.DictWriter(stream, fieldnames=fields)
            writer.writeheader()
            for move in view["spatial"][chosen["candidate_id"]]["moves"]:
                writer.writerow({"run_id": run_id, "candidate_id": chosen["candidate_id"],
                                 "result_id": chosen["result"]["result_id"], **move})
    # One immutable standalone entry per run.
    for name in UI_FILES:
        shutil.copy2(ASSETS / name, target / name)
    shutil.copytree(ASSETS / "vendor", target / "vendor", dirs_exist_ok=True)
    licenses = layout.root / "replay" / "basemap" / "licenses"
    if licenses.is_dir():
        shutil.copytree(licenses, target / "licenses", dirs_exist_ok=True)
    (target / "config.js").write_text("window.BIKE_CONSOLE_CONFIG={standalone:true};\n")
    (target / "map-config.js").write_text("window.BIKE_MAP_CONFIG={};\n")
    # Aggregate console at a distinct root, never overwrite the old replay site.
    console = layout.root / "console"
    console.mkdir(exist_ok=True)
    for name in UI_FILES:
        shutil.copy2(ASSETS / name, console / name)
    shutil.copytree(ASSETS / "vendor", console / "vendor", dirs_exist_ok=True)
    published = []
    registry = list_runs(layout, include_tests=include_tests)
    for row in registry:
        path = layout.outputs / "multi_agent" / row["run_id"] / "presentation" / "run-view.json"
        if path.is_file():
            published.append(json.loads(path.read_text()))
    if not any(v["identity"]["run_id"] == run_id for v in published):
        published.insert(0, view)
    (console / "replay-data.js").write_text(
        "window.BIKE_RUNS=" + encoded(published).replace("<", "\\u003c") + ";\n", encoding="utf-8")
    (console / "config.js").write_text("window.BIKE_CONSOLE_CONFIG={standalone:false};\n")
    (console / "map-config.js").write_text("window.BIKE_MAP_CONFIG={};\n")
    export_dir = console / "exports" / run_id
    export_dir.mkdir(parents=True, exist_ok=True)
    for name in ("decision-report.md", "decision-report.html", export_name):
        if name:
            shutil.copy2(target / name, export_dir / name)
    files = {str(p.relative_to(target)): hashlib.sha256(p.read_bytes()).hexdigest()
             for p in target.rglob("*") if p.is_file() and p != target / "manifest.json"}
    write_json(target / "manifest.json", {
        "schema_version": "2.0", "run_id": run_id,
        "authenticity_status": view["identity"]["authenticity_status"],
        "files": files, "has_dispatch_export": bool(export_name),
    })
    return {"console": str(console / "index.html"), "standalone": str(target / "index.html"),
            "report": str(target / "decision-report.html")}
