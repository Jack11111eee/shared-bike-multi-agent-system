#!/usr/bin/env python3
"""REQUIREMENTS-PHASE4 §6.1: deterministic acceptance for Phase 4 (F16–F20).

Runs the machine-checkable parts of F16-1..4, F17-1..3, F18-1..3, F19-1..3,
F20-1..3 and NF-22..25 against the *current* inputs and the *shipped* pages,
writes ``outputs/presentation_validation.phase4.json`` and exits non-zero when
any check fails.

This report is evidence, not a substitute for the test suite or the browser run.
Each entry records the check id, the inputs it read, the expected value, the
measured value and the result -- so a reader can re-derive the verdict instead of
trusting it.

Usage::

    python3 scripts/check_presentation.py --phase 4 [--json] [--verbose]
"""

from __future__ import annotations

import argparse
import hashlib
import html
import json
import math
import re
import subprocess
import sys
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd

from bike_agent.config import Layout

ROOT = Path(__file__).resolve().parents[1]
SITE = ROOT / "replay"
SOURCES = ("xm", "sh")
STRATEGIES = ("rule", "optimized")
REPORT = ROOT / "outputs" / "presentation_validation.phase4.json"

# Running this file directly puts ``scripts/`` on sys.path, not the repo root,
# so the sibling modules (bike_agent, scripts.browser_smoke) would not resolve.
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


# -- small helpers ----------------------------------------------------------
def _rel(path: Path) -> str:
    try:
        return str(path.relative_to(ROOT))
    except ValueError:
        return str(path)


def _sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()[:16]


def _close(a: float, b: float) -> bool:
    return abs(float(a) - float(b)) <= max(1e-9 * max(1.0, abs(float(b))), 1e-9)


class Checks:
    def __init__(self) -> None:
        self.items: list[dict[str, Any]] = []

    def add(
        self,
        check_id: str,
        requirement: str,
        *,
        ok: bool,
        expected: Any,
        actual: Any,
        inputs: list[str] | None = None,
        note: str = "",
    ) -> None:
        self.items.append(
            {
                "id": check_id,
                "requirement": requirement,
                "result": "pass" if ok else "fail",
                "expected": expected,
                "actual": actual,
                "inputs": inputs or [],
                "note": note,
            }
        )

    @property
    def failed(self) -> list[dict[str, Any]]:
        return [item for item in self.items if item["result"] != "pass"]


# -- the checks -------------------------------------------------------------
def run_checks() -> dict[str, Any]:
    from bike_agent.config import load_config
    from bike_agent.presentation.analysis import build_all_presentations

    config = load_config()
    layout = Layout()
    checks = Checks()

    missing = [
        path
        for source in SOURCES
        for path in (
            SITE / source / "workbench.html",
            SITE / source / "presentation.js",
            layout.outputs / f"sim_sensitivity.{source}.json",
        )
        if not path.exists()
    ]
    if missing:
        checks.add(
            "P4-PRE",
            "§6.1 输入齐备",
            ok=False,
            expected="workbench/presentation/敏感性与产物存在",
            actual=[_rel(path) for path in missing],
            note="先运行 `bike-agent replay --sources xm,sh`",
        )
        return _report(checks, {})

    presentations = build_all_presentations(config, layout, SOURCES)

    for source in SOURCES:
        presentation = presentations[source]
        _check_source(checks, layout, source, presentation)

    _check_guide_and_site(checks, presentations)
    _check_rendering(checks, presentations)
    _check_budget(checks, presentations)
    _check_no_regression(checks)
    _check_offline(checks)
    _check_browser(checks)

    return _report(checks, presentations)


def _check_source(checks: Checks, layout: Layout, source: str, presentation: dict[str, Any]) -> None:
    """Every check id is suffixed with the source, so the report shows both runs."""
    n_frames, n_cells = presentation["n_frames"], presentation["n_cells"]

    rule_state = pd.read_parquet(layout.replay_path(source, "rule"))
    opt_state = pd.read_parquet(layout.replay_path(source, "optimized"))
    rule_state = rule_state.sort_values(["time_bucket", "cell_id"], kind="mergesort")
    opt_state = opt_state.sort_values(["time_bucket", "cell_id"], kind="mergesort")
    rule_csv = pd.read_csv(layout.sim_path(source, "rule", "csv")).sort_values("time_bucket").reset_index(drop=True)
    opt_csv = pd.read_csv(layout.sim_path(source, "optimized", "csv")).sort_values("time_bucket").reset_index(drop=True)
    parquet = f"outputs/replay.{{strategy}}.{source}.parquet"
    csv_rule = f"outputs/sim_rule.{source}.csv"
    csv_opt = f"outputs/sim_optimized.{source}.csv"

    # F18-1: cell-level delta equals the parquet difference; sums equal the CSV difference.
    expected_delta = (
        opt_state["unmet_requests"].to_numpy(dtype=np.int64) - rule_state["unmet_requests"].to_numpy(dtype=np.int64)
    ).reshape(n_frames, n_cells)
    rendered_delta = np.array([frame["delta"] for frame in presentation["frames"]], dtype=np.int64)
    per_frame_ok = np.array_equal(rendered_delta, expected_delta)
    sums_ok = all(
        int(rendered_delta[index].sum())
        == int(opt_csv["unmet_requests"].iloc[index]) - int(rule_csv["unmet_requests"].iloc[index])
        for index in range(n_frames)
    )
    checks.add(
        f"F18-1[{source}]",
        "逐格逐帧 delta_unmet = unmet_optimized - unmet_rule，且格级之和等于 CSV 汇总差",
        ok=bool(per_frame_ok and sums_ok),
        expected=f"{n_frames}×{n_cells} 格全部相等，{n_frames} 帧求和全部相等",
        actual={"cells_equal": bool(per_frame_ok), "frame_sums_equal": bool(sums_ok)},
        inputs=[parquet.format(strategy="rule"), parquet.format(strategy="optimized"), csv_rule, csv_opt],
    )

    # F18-2: one fixed domain per source, independently recomputed.
    max_abs = int(np.abs(expected_delta).max()) if expected_delta.size else 0
    domain = presentation["color_domain"]["delta"]
    checks.add(
        f"F18-2[{source}]",
        "色域固定为 [-M, M]，M = 全回放全部格子 max(abs(delta_unmet))",
        ok=domain["max_abs"] == max_abs and domain["min"] == -max_abs and domain["max"] == max_abs,
        expected={"min": -max_abs, "max": max_abs},
        actual=domain,
        inputs=[parquet.format(strategy="optimized")],
    )
    page = (SITE / source / "workbench.html").read_text(encoding="utf-8")
    scale = re.search(r'data-scale="delta"[^>]*data-min="(-?\d+)" data-max="(-?\d+)"', page)
    checks.add(
        f"F18-2b[{source}]",
        "页面上的差分色标与数据域一致；M=0 时明确显示全程无差异",
        ok=bool(scale)
        and int(scale.group(1)) == -max_abs
        and int(scale.group(2)) == max_abs
        and (("全程无差异" in page) if max_abs == 0 else ("全程无差异" not in page)),
        expected={"min": -max_abs, "max": max_abs, "zero_message": max_abs == 0},
        actual={
            "min": int(scale.group(1)) if scale else None,
            "max": int(scale.group(2)) if scale else None,
            "zero_message": "全程无差异" in page,
        },
        inputs=[f"replay/{source}/workbench.html"],
    )

    # F17-1: the four events, recomputed from the CSVs.
    gain = (rule_csv["unmet_requests"] - opt_csv["unmet_requests"]).to_numpy()
    expected_events = {
        "rule_unmet_max": (
            int(rule_csv["unmet_requests"].to_numpy().argmax()),
            int(rule_csv["unmet_requests"].max()),
        ),
        "rule_moved_max": (
            int(rule_csv["bikes_moved"].to_numpy().argmax()),
            int(rule_csv["bikes_moved"].max()),
        ),
        "opt_gain_max": (int(gain.argmax()), int(gain.max())),
        "opt_regression_max": (int(gain.argmin()), int(gain.min())),
    }
    actual_events = {event["type"]: event for event in presentation["events"]}
    event_ok = True
    event_detail = {}
    for kind, (index, value) in expected_events.items():
        event = actual_events[kind]
        if kind in {"opt_gain_max", "opt_regression_max"}:
            qualifies = value > 0 if kind == "opt_gain_max" else value < 0
            want_frame = index if qualifies else None
            want_value = value if qualifies else 0
        else:
            qualifies = value > 0
            want_frame = index if qualifies else None
            want_value = value if qualifies else 0
        good = event["frame"] == want_frame and event["value"] == want_value
        event_ok = event_ok and good
        event_detail[kind] = {"frame": event["frame"], "value": event["value"], "ok": good}
    checks.add(
        f"F17-1[{source}]",
        "四类事件从原 CSV 对全帧复算，与打包记录相等（平手取最早时段）",
        ok=event_ok,
        expected={kind: {"frame": frame, "value": value} for kind, (frame, value) in expected_events.items()},
        actual=event_detail,
        inputs=[csv_rule, csv_opt],
    )

    # F17 sign discipline.
    gain_event = actual_events["opt_gain_max"]
    regression_event = actual_events["opt_regression_max"]
    sign_ok = (gain_event["status"] != "ok" or gain_event["value"] > 0) and (
        regression_event["status"] != "ok" or regression_event["value"] < 0
    )
    checks.add(
        f"F17-sign[{source}]",
        "改善事件只取 gain>0，退步事件只取 gain<0；无符合项显示空态",
        ok=sign_ok,
        expected="改善>0 / 退步<0 / 否则空态",
        actual={
            "gain": {"status": gain_event["status"], "value": gain_event["value"]},
            "regression": {"status": regression_event["status"], "value": regression_event["value"]},
        },
        inputs=[csv_rule, csv_opt],
    )

    # F19-1 / F19-2: the ledger, on every cell.
    ledger_ok = True
    conservation_ok = True
    ledger_detail: dict[str, Any] = {}
    for strategy in STRATEGIES:
        state = rule_state if strategy == "rule" else opt_state
        bundle = {
            field: np.array(
                [frame["strategies"][strategy]["ledger"][field] for frame in presentation["frames"]],
                dtype=np.int64,
            )
            for field in ("start", "req", "met", "ret", "rapp", "out")
        }
        incoming = np.zeros((n_frames, n_cells), dtype=np.int64)
        for index, frame in enumerate(presentation["frames"]):
            for cell_index, bikes in frame["strategies"][strategy]["moved_in"].items():
                incoming[index, int(cell_index)] = int(bikes)

        start = state["inventory_start"].to_numpy(dtype=np.int64).reshape(n_frames, n_cells)
        end = state["inventory_end"].to_numpy(dtype=np.int64).reshape(n_frames, n_cells)
        met = state["borrow_met"].to_numpy(dtype=np.int64).reshape(n_frames, n_cells)
        req = state["borrow_requests"].to_numpy(dtype=np.int64).reshape(n_frames, n_cells)
        ret = state["returns"].to_numpy(dtype=np.int64).reshape(n_frames, n_cells)
        rapp = state["returns_applied"].to_numpy(dtype=np.int64).reshape(n_frames, n_cells)
        out = state["bikes_moved"].to_numpy(dtype=np.int64).reshape(n_frames, n_cells)
        unmet = state["unmet_requests"].to_numpy(dtype=np.int64).reshape(n_frames, n_cells)
        overflow = state["returns_overflow"].to_numpy(dtype=np.int64).reshape(n_frames, n_cells)

        strategy_ok = (
            np.array_equal(bundle["start"], start)
            and np.array_equal(bundle["req"], req)
            and np.array_equal(bundle["met"], met)
            and np.array_equal(bundle["ret"], ret)
            and np.array_equal(bundle["rapp"], rapp)
            and np.array_equal(bundle["out"], out)
            and np.array_equal(start + incoming - out + rapp - met, end)
            and np.array_equal(req - met, unmet)
            and np.array_equal(ret - rapp, overflow)
        )
        ledger_ok = ledger_ok and strategy_ok

        records = json.loads(
            (layout.outputs / f"sim_moves.{strategy}.{source}.json").read_text(encoding="utf-8")
        )
        per_bucket = np.zeros(n_frames, dtype=np.int64)
        position = {bucket: index for index, bucket in enumerate(presentation["buckets"])}
        for record in records:
            bucket = pd.Timestamp(record["time_bucket"]).strftime("%Y-%m-%d %H:%M:%S")
            per_bucket[position[bucket]] += int(record["bikes"])
        strategy_conservation = (
            np.array_equal(incoming.sum(axis=1), per_bucket)
            and np.array_equal(out.sum(axis=1), per_bucket)
            and np.array_equal(incoming.sum(axis=1), out.sum(axis=1))
        )
        conservation_ok = conservation_ok and strategy_conservation
        ledger_detail[strategy] = {
            "fields_equal": bool(strategy_ok),
            "in_equals_out_equals_records": bool(strategy_conservation),
        }

    checks.add(
        f"F19-1[{source}]",
        "全源全帧全格双策略：账本原始字段与四条等式",
        ok=ledger_ok,
        expected="字段逐格相等；期末=期初+调入-调出+接纳归还-满足；未满足=请求-满足；溢出=归还-接纳",
        actual=ledger_detail,
        inputs=[parquet.format(strategy="rule"), parquet.format(strategy="optimized")],
    )
    checks.add(
        f"F19-2[{source}]",
        "每帧全格调入总量 == 调出总量 == 原始搬运车辆数；逐格调出 == bikes_moved",
        ok=conservation_ok,
        expected=f"{n_frames} 帧全部守恒",
        actual=ledger_detail,
        inputs=[f"outputs/sim_moves.rule.{source}.json", f"outputs/sim_moves.optimized.{source}.json"],
    )
    # F19-3: the default cell and the page's empty states.
    cumulative = state_unmet_sum(layout, source, n_frames, n_cells)
    best = int(cumulative.argmax())
    expected_cell = presentation["cell_ids"][best]
    checks.add(
        f"F19-3[{source}]",
        "首次选格 = 累计 S-RULE 未满足最高（平手按 cell_id 升序），且页面暴露选择规则",
        ok=presentation["ledger_top_cell"]["cell_id"] == expected_cell
        and presentation["ledger_top_cell"]["rule"] == "累计 S-RULE 未满足请求最高；平手按 cell_id 升序",
        expected=expected_cell,
        actual=presentation["ledger_top_cell"],
        inputs=[parquet.format(strategy="rule")],
    )

    # F20-1 / F20-2: scenarios.
    sensitivity = json.loads(
        (layout.outputs / f"sim_sensitivity.{source}.json").read_text(encoding="utf-8")
    )
    comparison = json.loads(
        (layout.outputs / f"strategy_comparison.{source}.json").read_text(encoding="utf-8")
    )
    truth = {row["scenario"]: row for row in sensitivity}
    rendered = {row["id"]: row for row in presentation["scenarios"]["rows"]}
    values_ok = set(truth) == set(rendered)
    deltas_ok = True
    for scenario, expected in truth.items():
        for strategy in STRATEGIES:
            for metric in ("unmet_requests", "service_rate", "distance_bike_km", "composite_cost"):
                block = rendered[scenario]["strategies"][strategy][metric]
                values_ok = values_ok and _close(block["value"], expected[strategy][metric])
                absolute = expected[strategy][metric] - truth["baseline"][strategy][metric]
                deltas_ok = deltas_ok and _close(block["delta"]["absolute"], absolute)
                if truth["baseline"][strategy][metric] == 0:
                    deltas_ok = deltas_ok and block["delta"]["status"] == "zero_denominator"
                else:
                    deltas_ok = deltas_ok and _close(
                        block["delta"]["percent"],
                        absolute / truth["baseline"][strategy][metric] * 100.0,
                    )
    baseline_ok = all(
        _close(truth["baseline"][strategy][metric], comparison[strategy][metric])
        for strategy in STRATEGIES
        for metric in ("unmet_requests", "service_rate", "distance_bike_km", "composite_cost")
    )
    checks.add(
        f"F20-1[{source}]",
        "场景集合与每项双策略四指标等于输入；baseline 与 strategy_comparison 一致",
        ok=values_ok and baseline_ok,
        expected={"scenarios": sorted(truth), "baseline_equals_comparison": True},
        actual={"scenarios": sorted(rendered), "values_equal": values_ok, "baseline_equal": baseline_ok},
        inputs=[f"outputs/sim_sensitivity.{source}.json", f"outputs/strategy_comparison.{source}.json"],
    )
    checks.add(
        f"F20-2[{source}]",
        "相对基准变化可复算（含无变化、负向、零分母）",
        ok=deltas_ok,
        expected="absolute 与 percent 均可从输入复算；零分母标记为 zero_denominator",
        actual={"deltas_equal": deltas_ok},
        inputs=[f"outputs/sim_sensitivity.{source}.json"],
    )

    # F20-3: every scenario is a discrete button and baseline is reachable.
    buttons = set(re.findall(r'<button data-scenario="([^"]+)"', page))
    checks.add(
        f"F20-3[{source}]",
        "每源每个已计算场景都有离散按钮，且可回到 baseline；场景不改写基准地图",
        ok=set(truth) <= buttons and "回到 baseline" in page and "不改写地图数据" in page,
        expected=sorted(truth),
        actual=sorted(buttons),
        inputs=[f"replay/{source}/workbench.html"],
    )


def state_unmet_sum(layout: Any, source: str, n_frames: int, n_cells: int) -> np.ndarray:
    state = pd.read_parquet(layout.replay_path(source, "rule"))
    state = state.sort_values(["time_bucket", "cell_id"], kind="mergesort")
    return state["unmet_requests"].to_numpy(dtype=np.int64).reshape(n_frames, n_cells).sum(axis=0)


def _check_guide_and_site(checks: Checks, presentations: dict[str, dict[str, Any]]) -> None:
    index = (SITE / "index.html").read_text(encoding="utf-8")
    guide_links = re.findall(r'href="([a-z]+/workbench\.html)"', index)
    checks.add(
        "F16-1a",
        "首页有演示导览入口，链接目标存在",
        ok="演示导览" in index and bool(guide_links) and all((SITE / link).exists() for link in guide_links),
        expected="首页入口 + 目标页面存在",
        actual={"links": guide_links, "exist": [str((SITE / link).exists()) for link in guide_links]},
        inputs=["replay/index.html"],
    )

    chapter_ok = True
    action_ok = True
    detail = {}
    for source, presentation in presentations.items():
        guide = presentation["guide"]
        chapter_ok = chapter_ok and guide["n_chapters"] == 5
        action_ok = action_ok and {action["id"] for action in guide["actions"]} == {
            "start",
            "prev",
            "next",
            "restart",
            "exit",
        }
        page = (SITE / source / "workbench.html").read_text(encoding="utf-8")
        chapter_ok = chapter_ok and len(re.findall(r'class="chapter"', page)) == 5
        detail[source] = {
            "chapters": guide["n_chapters"],
            "titles": [chapter["title"] for chapter in guide["chapters"]],
            "actions": [action["id"] for action in guide["actions"]],
        }
    checks.add(
        "F16-1b",
        "导览记录有五个有序章节与全部导航动作",
        ok=chapter_ok and action_ok,
        expected={"chapters": 5, "actions": ["start", "prev", "next", "restart", "exit"]},
        actual=detail,
        inputs=["replay/xm/workbench.html", "replay/sh/workbench.html"],
    )

    targets_ok = True
    for source, presentation in presentations.items():
        for chapter in presentation["guide"]["chapters"]:
            target = chapter["target"]
            targets_ok = targets_ok and 0 <= target["frame"] < presentation["n_frames"]
            targets_ok = targets_ok and (
                target["cell"] is None or target["cell"] in presentation["cell_ids"]
            )
            targets_ok = targets_ok and (
                target["scenario"] == "baseline"
                or target["scenario"] in {row["id"] for row in presentation["scenarios"]["rows"]}
            )
    checks.add(
        "F16-2",
        "每章目标源/时段/格子/场景均能在权威输入中找到",
        ok=targets_ok,
        expected="五个章节的目标全部存在",
        actual={"targets_valid": targets_ok},
        inputs=["outputs/replay.rule.xm.parquet", "outputs/sim_sensitivity.xm.json"],
    )

    old_entries_ok = True
    for source, presentation in presentations.items():
        page = (SITE / source / "workbench.html").read_text(encoding="utf-8")
        frames = sorted(SITE.glob(f"{source}/frame-*.html"))
        old_entries_ok = old_entries_ok and len(frames) == presentation["n_frames"]
        old_entries_ok = old_entries_ok and all(
            href in page for href in ("player.html", "../dashboard.html", "frame-0000.html", "../index.html")
        )
        fallback = re.search(r"<noscript>(.*?)</noscript>", page, re.DOTALL)
        old_entries_ok = old_entries_ok and bool(fallback) and "frame-0000.html" in fallback.group(1)
    checks.add(
        "F16-4",
        "保留旧入口与完整帧集；禁用 JS 时有可读介绍与既有回放链接",
        ok=old_entries_ok,
        expected="player/dashboard/全部逐帧页入口存在，noscript 有介绍与链接",
        actual={"ok": old_entries_ok},
        inputs=["replay/xm", "replay/sh"],
    )


def _check_rendering(checks: Checks, presentations: dict[str, dict[str, Any]]) -> None:
    """What the shipped page renders, not just what the bundle holds.

    The first three checks here exist because a defect shipped once that every
    other gate passed: the delta map's rects were prerendered correctly and then
    rebuilt by the page script with geometry the payload never carried, so every
    cell was ``x="NaN"`` and zero pixels wide.  Counting elements did not notice.
    """
    js = (SITE / "workbench.js").read_text(encoding="utf-8")
    for source in SOURCES:
        page = (SITE / source / "workbench.html").read_text(encoding="utf-8")
        rects = re.findall(
            r'<rect class="cell delta [a-z]+" data-cell="[^"]*" data-delta="(-?\d+)"[^>]*?'
            r'x="([^"]+)" y="([^"]+)" width="([^"]+)" height="([^"]+)"',
            page,
        )
        bad = [
            values
            for values in rects
            if not all(_finite(value) for value in values[1:])
            or float(values[3]) <= 0
            or float(values[4]) <= 0
        ]
        checks.add(
            f"F18-1[{source}] 差分格几何",
            "F18-1 差分地图每个格子可见且可点击",
            ok=len(rects) == presentations[source]["n_cells"] and not bad,
            expected=f"{presentations[source]['n_cells']} 个 rect 带数值坐标与正宽高",
            actual={"rects": len(rects), "bad": len(bad), "sample": bad[:3]},
            inputs=[f"replay/{source}/workbench.html"],
        )
        checks.add(
            f"F18-1[{source}] 单一渲染器",
            "§2.3 同一数字只有一份推导：几何由 Python 预渲染，脚本不再计算",
            ok="cell_px" not in js and "cell_gap" not in js,
            expected="workbench.js 不引用 cell_px / cell_gap",
            actual={
                "cell_px": "cell_px" in js,
                "cell_gap": "cell_gap" in js,
            },
            inputs=["replay/workbench.js"],
        )

        marker = '<section class="card wide ledger-card" id="ledger"'
        ledger = page.split(marker, 1)[1].split("</section>", 1)[0] if marker in page else ""
        bucket = presentations[source]["frames"][0]["time_bucket"]
        metrics: dict[str, dict[str, str]] = {}
        for match in re.finditer(r'<b class="metric"([^>]*)>', ledger):
            attrs = dict(re.findall(r'(data-[\w-]+)="([^"]*)"', match.group(1)))
            if "data-metric" in attrs:
                metrics[attrs["data-metric"]] = attrs
        mismatched = []
        for name, operands, compute in (
            ("scheduled_inventory", ("inventory_start", "moved_in", "moved_out"),
             lambda o: o["inventory_start"] + o["moved_in"] - o["moved_out"]),
            ("returns_overflow", ("returns", "returns_applied"),
             lambda o: o["returns"] - o["returns_applied"]),
            ("unmet_requests", ("borrow_requests", "borrow_met"),
             lambda o: o["borrow_requests"] - o["borrow_met"]),
            ("inventory_end", ("inventory_start", "moved_in", "moved_out", "returns_applied", "borrow_met"),
             lambda o: o["inventory_start"] + o["moved_in"] - o["moved_out"]
             + o["returns_applied"] - o["borrow_met"]),
        ):
            node = metrics.get(name)
            if node is None or "data-operands" not in node:
                mismatched.append({"metric": name, "reason": "缺少 data-operands"})
                continue
            ops = json.loads(html.unescape(node["data-operands"]))
            missing = [key for key in operands if key not in ops]
            if missing:
                mismatched.append({"metric": name, "reason": f"操作数缺 {missing}"})
            elif compute(ops) != int(node["data-value"]):
                mismatched.append(
                    {"metric": name, "value": node["data-value"], "operands": ops, "recomputed": compute(ops)}
                )
            elif bucket not in node.get("data-join-key", ""):
                mismatched.append(
                    {"metric": name, "join_key": node.get("data-join-key"), "frame": bucket}
                )
        checks.add(
            f"F19-1[{source}] 证据可复算",
            "§2.3 派生值自带公式与操作数，且能复算出屏幕上的数",
            ok=not mismatched,
            expected=f"4 个派生值均可由自身 data-operands 复算，关联键指向 {bucket}",
            actual=mismatched,
            inputs=[f"replay/{source}/workbench.html"],
        )

        other = "sh" if source == "xm" else "xm"
        switch = re.search(
            rf'<a data-source-switch="{other}" href="([^"]+)"', page
        )
        checks.add(
            f"P4-M18[{source}] 数据源切换",
            "F16 工作台切换数据源可用",
            ok=bool(switch) and (SITE / switch.group(1).replace("../", "", 1)).exists(),
            expected=f"指向 ../{other}/workbench.html 的链接存在",
            actual=switch.group(1) if switch else None,
            inputs=[f"replay/{source}/workbench.html"],
            note="每源一页、各自加载自己的数据包，切换即导航（同时加载两包会超出站点体积上限）",
        )


def _finite(value: str) -> bool:
    try:
        return math.isfinite(float(value))
    except (TypeError, ValueError):
        return False


#: Files ``build_site`` writes at the site root, before per-source pages.
ROOT_ASSETS = (
    "index.html",
    "dashboard.html",
    "style.css",
    "app.js",
    "player.css",
    "player.js",
    "workbench.css",
    "workbench.js",
    "workbench-core.js",
)
#: Files ``build_site`` writes into each ``replay/<source>/``.
SOURCE_ASSETS = ("player.html", "replay-data.js", "presentation.js", "workbench.html")


def _expected_site_files(presentations: dict[str, dict[str, Any]]) -> set[str]:
    """Exactly the paths the build produces -- derived, not enumerated by hand.

    The site is generated, so its file set is deterministic.  Comparing against
    a derived set closes the whole class of "something else got left in the
    shipped tree" (a crashed smoke page, a verification probe, a stray copy),
    instead of listing the two names known to have gone wrong so far.
    """
    from bike_agent.config import Layout

    files = set(ROOT_ASSETS)
    for source, presentation in presentations.items():
        for name in SOURCE_ASSETS:
            files.add(f"{source}/{name}")
        for index in range(presentation["n_frames"]):
            files.add(f"{source}/frame-{index:04d}.html")
    for figure in Layout().figures.glob("*.png"):
        files.add(f"figures/{figure.name}")
    return files


def _shipped_files() -> list[Path]:
    """The files that ship.  ``.DS_Store`` is excluded because it is Finder
    metadata -- gitignored, absent on other machines, so counting it would make
    the quoted total unreproducible.  Everything else is counted, so an
    unexpected file keeps inflating the total until NF-24 names it."""
    return [path for path in SITE.rglob("*") if path.is_file() and path.name != ".DS_Store"]


def _check_budget(checks: Checks, presentations: dict[str, dict[str, Any]]) -> None:
    files = _shipped_files()
    total = sum(path.stat().st_size for path in files)
    shipped = {str(path.relative_to(SITE)) for path in files}
    expected = _expected_site_files(presentations)
    detail = {
        "total_bytes": total,
        "cap_bytes": 8 * 1024 * 1024,
        "presentation": {
            source: (SITE / source / "presentation.js").stat().st_size for source in SOURCES
        },
        "workbench": {
            source: (SITE / source / "workbench.html").stat().st_size for source in SOURCES
        },
        "replay_data": {source: (SITE / source / "replay-data.js").stat().st_size for source in SOURCES},
        "player": {source: (SITE / source / "player.html").stat().st_size for source in SOURCES},
        "dashboard": (SITE / "dashboard.html").stat().st_size,
        "largest_frame": max(
            (path.stat().st_size for path in SITE.glob("*/frame-*.html")), default=0
        ),
        # Anything the build did not write must never ship -- a crashed smoke
        # page, a verification probe, a half-written copy.
        "stray_files": sorted(shipped - expected),
        "missing_files": sorted(expected - shipped),
    }
    ok = (
        total < 8 * 1024 * 1024
        and all(value <= 2 * 1024 * 1024 for value in detail["presentation"].values())
        and all(value <= 256 * 1024 for value in detail["workbench"].values())
        and all(value <= 1024 * 1024 for value in detail["replay_data"].values())
        and all(value <= 256 * 1024 for value in detail["player"].values())
        and detail["dashboard"] <= 512 * 1024
        and detail["largest_frame"] < 96 * 1024
        and not detail["stray_files"]
        and not detail["missing_files"]
    )
    checks.add(
        "NF-24",
        "replay/ < 8 MiB；presentation ≤ 2 MiB/源；workbench ≤ 256 KiB；旧单文件上限不变",
        ok=ok,
        expected={"total_lt": 8 * 1024 * 1024, "presentation_le": 2 * 1024 * 1024},
        actual=detail,
        inputs=["replay/"],
    )


def _check_no_regression(checks: Checks) -> None:
    key_numbers = ROOT / "outputs" / "key_numbers.json"
    script = ROOT / "scripts" / "check_regression.py"
    if not script.exists() or not key_numbers.exists():
        checks.add(
            "NF-22",
            "业务不回归：冻结既有数值与 schema，保留旧 key_numbers.json",
            ok=False,
            expected="scripts/check_regression.py 可运行",
            actual="缺失",
        )
        return
    completed = subprocess.run(
        [sys.executable, str(script)], capture_output=True, text=True, cwd=str(ROOT), check=False
    )
    checks.add(
        "NF-22",
        "业务不回归：冻结既有 forecast/risk/simulation/sensitivity 数值与 schema",
        ok=completed.returncode == 0,
        expected="check_regression.py 退出码 0",
        actual=completed.returncode,
        inputs=["scripts/phase1-snapshot.json", "outputs/key_numbers.json"],
        note=completed.stdout.strip()[-400:],
    )


_EXTERNAL = re.compile(
    r"""(?:src|href)\s*=\s*["'](?:(?:https?:)?//)[^"']+["']|@import\s+(?:url\()?["']?(?:https?:)?//|url\(\s*["']?(?:https?:)?//""",
    re.IGNORECASE,
)


def _check_offline(checks: Checks) -> None:
    offenders = []
    for path in SITE.rglob("*"):
        if path.suffix not in {".html", ".css", ".js"}:
            continue
        for match in _EXTERNAL.finditer(path.read_text(encoding="utf-8")):
            offenders.append(f"{_rel(path)}: {match.group(0)[:60]}")
    checks.add(
        "NF-23",
        "可离线：站点无外部运行时请求，file:// 与 serve 均可用",
        ok=not offenders,
        expected="没有任何外部 URL 引用",
        actual=offenders[:10],
        inputs=["replay/"],
    )


def _check_browser(checks: Checks) -> None:
    from scripts.browser_smoke import find_chrome

    if find_chrome() is None:
        checks.add(
            "NF-27",
            "真实浏览器联动冒烟",
            ok=False,
            expected="headless Chrome 可运行",
            actual="未找到 Chrome/Chromium；浏览器冒烟未执行",
            note="这是未验证，不是通过",
        )
        return
    results = {}
    ok = True
    for source in SOURCES:
        completed = subprocess.run(
            [
                sys.executable,
                str(ROOT / "scripts" / "browser_smoke.py"),
                "--source",
                source,
                "--json",
                "--timeout",
                "300",
            ],
            capture_output=True,
            text=True,
            cwd=str(ROOT),
            timeout=420,
            check=False,
        )
        try:
            payload = json.loads(completed.stdout)
        except json.JSONDecodeError:
            payload = {"ok": False, "reason": completed.stdout[-300:] + completed.stderr[-300:]}
        results[source] = {
            "ok": payload.get("ok", False),
            "steps": len(payload.get("steps", [])),
            "errors": payload.get("errors", []),
            "reason": payload.get("reason"),
        }
        ok = ok and results[source]["ok"]
    checks.add(
        "NF-27",
        "真实浏览器联动冒烟（导览/事件/模式/选格/场景）",
        ok=ok,
        expected="两源全部步骤通过",
        actual=results,
        inputs=["replay/xm/workbench.html", "replay/sh/workbench.html"],
    )


def _report(checks: Checks, presentations: dict[str, dict[str, Any]]) -> dict[str, Any]:
    inputs = {}
    for source in SOURCES:
        for name in (
            f"outputs/replay.rule.{source}.parquet",
            f"outputs/replay.optimized.{source}.parquet",
            f"outputs/sim_rule.{source}.csv",
            f"outputs/sim_optimized.{source}.csv",
            f"outputs/sim_moves.rule.{source}.json",
            f"outputs/sim_moves.optimized.{source}.json",
            f"outputs/sim_sensitivity.{source}.json",
            f"outputs/strategy_comparison.{source}.json",
        ):
            path = ROOT / name
            if path.exists():
                inputs[name] = {"bytes": path.stat().st_size, "sha256_16": _sha(path)}
    return {
        "phase": 4,
        "requirement": "REQUIREMENTS-PHASE4.md",
        "generated_by": "scripts/check_presentation.py --phase 4",
        "note": (
            "本报告是确定性验收证据，不替代 `python3 -m pytest` 与浏览器实测；"
            "人工视觉检查（§6.2）仍需人确认。"
        ),
        "ok": not checks.failed,
        "n_checks": len(checks.items),
        "n_failed": len(checks.failed),
        "sources": {
            source: {
                "n_frames": presentation["n_frames"],
                "n_cells": presentation["n_cells"],
                "delta_max_abs": presentation["color_domain"]["delta"]["max_abs"],
                "n_events": len(presentation["events"]),
                "n_scenarios": presentation["scenarios"]["n_scenarios"],
                "n_chapters": presentation["guide"]["n_chapters"],
            }
            for source, presentation in presentations.items()
        },
        "inputs": inputs,
        "checks": checks.items,
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--phase", type=int, default=4, choices=[4])
    parser.add_argument("--json", action="store_true", help="print the report to stdout")
    parser.add_argument(
        "--verbose",
        action="store_true",
        help="print every check, pass or fail (default: only failures plus a fixed summary)",
    )
    args = parser.parse_args(argv)

    report = run_checks()
    REPORT.parent.mkdir(parents=True, exist_ok=True)
    REPORT.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")

    if args.json:
        print(json.dumps(report, ensure_ascii=False, indent=2))
    else:
        for item in report["checks"]:
            if args.verbose or item["result"] != "pass":
                mark = "PASS" if item["result"] == "pass" else "FAIL"
                print(f"[{mark}] {item['id']:<12} {item['requirement']}")
            if item["result"] != "pass":
                print(f"        expected: {item['expected']}")
                print(f"        actual:   {item['actual']}")
        # Deliberately free of counts: the report file carries them, and a
        # summary that changes whenever a check is added would make every
        # feedback-diff run look like a regression.
        if report["ok"]:
            print(f"phase 4: all checks passed -> {_rel(REPORT)}")
        else:
            print(f"phase 4: {report['n_failed']} of {report['n_checks']} checks FAILED -> {_rel(REPORT)}")
    return 0 if report["ok"] else 1


if __name__ == "__main__":
    sys.exit(main())
