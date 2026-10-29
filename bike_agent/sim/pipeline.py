"""Simulation pipeline: run both strategies and render the comparison (F5)."""

from __future__ import annotations

from typing import Any

import pandas as pd

from bike_agent.config import Layout, source_config
from bike_agent.prep.grid import build_grid
from bike_agent.sim.capacity import cell_capacities
from bike_agent.sim.engine import simulate
from bike_agent.utils import markdown_table, pct, write_json


def _load_inputs(config: dict[str, Any], layout: Layout, source: str) -> dict[str, Any]:
    src_cfg = source_config(config, source)
    cell_size = int(src_cfg["cell_size_m"])
    panel_path = layout.panel_path(source, cell_size)
    forecast_path = layout.forecast_path(source)
    clean_path = layout.clean_path(source)
    for path in (panel_path, forecast_path, clean_path):
        if not path.exists():
            raise FileNotFoundError(f"missing simulation input {path}; run the earlier pipeline stages first")

    panel = pd.read_parquet(panel_path)
    forecast = pd.read_parquet(forecast_path)
    clean = pd.read_parquet(clean_path)
    cells = pd.read_parquet(layout.cells_path(source, cell_size))
    grid = build_grid(clean, cell_size)
    fences_path = layout.fences_path(source)
    fences = pd.read_parquet(fences_path) if fences_path.exists() else None
    capacity_cfg = src_cfg.get("capacity", {})
    capacities = cell_capacities(cells, capacity_cfg, fences=fences, grid=grid)
    return {
        "panel": panel,
        "forecast": forecast,
        "cells": cells,
        "capacities": capacities,
        "capacity_mode": capacity_cfg.get("mode", "fixed"),
    }


def run_simulation(
    config: dict[str, Any],
    layout: Layout,
    source: str,
    strategy: str,
    budget: int | None = None,
    write: bool = True,
) -> dict[str, Any]:
    inputs = _load_inputs(config, layout, source)
    result = simulate(
        panel=inputs["panel"],
        forecast=inputs["forecast"],
        cells=inputs["cells"],
        capacities=inputs["capacities"],
        config=config,
        strategy=strategy,
        budget=budget,
    )
    summary = dict(result.summary)
    summary["source"] = source
    summary["capacity_mode"] = inputs["capacity_mode"]

    if write:
        layout.ensure()
        result.steps.to_csv(layout.sim_path(source, strategy, "csv"), index=False)
        write_json(layout.sim_path(source, strategy, "json"), summary)
        if source == config.get("primary_source"):
            result.steps.to_csv(layout.outputs / f"sim_{strategy}.csv", index=False)
            write_json(layout.outputs / f"sim_{strategy}.json", summary)
        write_json(layout.outputs / f"sim_moves.{strategy}.{source}.json", result.moves.to_dict(orient="records"))
    return {
        "summary": summary,
        "steps": result.steps,
        "moves": result.moves,
        "violations": result.violations,
    }


def run_comparison(
    config: dict[str, Any],
    layout: Layout,
    source: str,
    budget: int | None = None,
    write: bool = True,
) -> dict[str, Any]:
    rule = run_simulation(config, layout, source, "rule", budget=budget, write=write)
    optimized = run_simulation(config, layout, source, "optimized", budget=budget, write=write)
    report = {
        "source": source,
        "rule": rule["summary"],
        "optimized": optimized["summary"],
    }
    if write:
        layout.ensure()
        layout.report_path(f"strategy_comparison.{source}.md").write_text(
            render_comparison(report), encoding="utf-8"
        )
        write_json(layout.outputs / f"strategy_comparison.{source}.json", report)
    return {"report": report, "rule": rule, "optimized": optimized}


def run_sim_sensitivity(
    config: dict[str, Any],
    layout: Layout,
    source: str,
) -> list[dict[str, Any]]:
    """D6 sensitivity: how the comparison moves with the key assumptions."""
    import copy

    import numpy as np

    inputs = _load_inputs(config, layout, source)
    scenarios: list[dict[str, Any]] = [
        {"name": "baseline", "changes": {}},
        {"name": "initial_fill=0.3", "changes": {"initial_fill_ratio": 0.3}},
        {"name": "initial_fill=0.7", "changes": {"initial_fill_ratio": 0.7}},
        {"name": "budget=100", "changes": {"max_moves_per_bucket": 100}},
        {"name": "budget=400", "changes": {"max_moves_per_bucket": 400}},
        {"name": "capacity=0.5x", "changes": {}, "capacity_scale": 0.5},
        {"name": "capacity=2x", "changes": {}, "capacity_scale": 2.0},
    ]
    results: list[dict[str, Any]] = []
    for scenario in scenarios:
        cfg = copy.deepcopy(config)
        cfg.setdefault("simulation", {}).update(scenario["changes"])
        capacities = inputs["capacities"]
        if scenario.get("capacity_scale"):
            capacities = (capacities.astype(float) * float(scenario["capacity_scale"])).round().clip(lower=5).astype(int)
        entry: dict[str, Any] = {"scenario": scenario["name"]}
        for strategy in ("rule", "optimized"):
            summary = simulate(
                panel=inputs["panel"],
                forecast=inputs["forecast"],
                cells=inputs["cells"],
                capacities=capacities,
                config=cfg,
                strategy=strategy,
            ).summary
            entry[strategy] = {
                "unmet_requests": summary["unmet_requests"],
                "distance_bike_km": summary["distance_bike_km"],
                "composite_cost": summary["composite_cost"],
                "service_rate": summary["service_rate"],
            }
        results.append(entry)
    write_json(layout.outputs / f"sim_sensitivity.{source}.json", results)
    return results


def _delta(rule_value: float, opt_value: float) -> str:
    if rule_value == 0:
        return "n/a" if opt_value != 0 else "0.0%"
    return f"{(opt_value - rule_value) / rule_value * 100:+.2f}%"


def render_comparison(report: dict[str, Any]) -> str:
    rule = report["rule"]
    optimized = report["optimized"]
    source = report["source"].upper()
    assumptions = rule["assumptions"]
    lines = [
        f"# 调度策略对比 · {source}",
        "",
        "> 所有数字均为**模拟值**。`未满足借车请求` ≠ 实际缺车数量："
        "真实缺车需要库存数据，本项目没有。",
        "",
        f"回放时段：`{rule['first_bucket']}` → `{rule['last_bucket']}`，"
        f"共 {rule['n_buckets']} 个时间单元；容量来源：`{rule['capacity_mode']}`。",
        "",
        "## 三项指标对比（F5-2）",
        "",
        markdown_table(
            ["指标", "S-RULE（规则）", "S-OPT（优化）", "变化"],
            [
                ("未满足借车请求数", rule["unmet_requests"], optimized["unmet_requests"],
                 _delta(rule["unmet_requests"], optimized["unmet_requests"])),
                ("车辆搬运距离（bike-km）", f"{rule['distance_bike_km']:.2f}", f"{optimized['distance_bike_km']:.2f}",
                 _delta(rule["distance_bike_km"], optimized["distance_bike_km"])),
                ("综合代价（模拟单位）", f"{rule['composite_cost']:.2f}", f"{optimized['composite_cost']:.2f}",
                 _delta(rule["composite_cost"], optimized["composite_cost"])),
                ("服务率", pct(rule["service_rate"]), pct(optimized["service_rate"]),
                 _delta(rule["service_rate"], optimized["service_rate"])),
                ("搬运车辆数（bikes）", rule["bikes_moved"], optimized["bikes_moved"],
                 _delta(rule["bikes_moved"], optimized["bikes_moved"])),
            ],
        ),
        "",
        "## 共享假设（F5-3 公平性）",
        "",
        "两策略**共享**以下全部输入与假设，唯一不同的变量是**调度决策算法**：",
        "",
        markdown_table(
            ["假设", "取值", "来源"],
            [
                ("初始库存比例", assumptions["initial_fill_ratio"], "估计（D6，可配置）"),
                ("安全库存比例", assumptions["safety_stock_ratio"], "估计（D6，可配置）"),
                ("每时间单元搬运预算（bikes）", assumptions["max_moves_per_bucket"], "约定（D6，可配置）"),
                ("未满足请求惩罚", assumptions["unmet_penalty"], "约定（D6）"),
                ("距离成本（每 bike-km）", assumptions["distance_cost_per_bike_km"], "约定（D6）"),
                ("停放容量", rule["capacity_mode"], "XM=电子围栏密度；SH=固定假设（无围栏数据）"),
                ("需求预测输入", "同一份 forecast 文件", "F3 产物"),
                ("回放需求", "同一份历史借还计数", "F2 产物"),
                ("调度时点", "每个时间单元开始时，用该单元的预测做一次性决策", "两策略相同"),
            ],
        ),
        "",
        "调度在每个时间单元开始时，用**该单元的预测**（一步预测）决定搬运，然后依次执行"
        "归还与借车请求；两策略使用完全相同的预测、库存与容量，因此差异只来自分配算法。",
        "",
        "## 结果解读",
        "",
    ]
    if optimized["composite_cost"] <= rule["composite_cost"]:
        lines.append(
            "在本回放条件下，S-OPT 的综合代价不高于 S-RULE。"
            f"未满足请求 {rule['unmet_requests']} → {optimized['unmet_requests']}，"
            f"搬运距离 {rule['distance_bike_km']:.2f} → {optimized['distance_bike_km']:.2f} bike-km。"
        )
    else:
        lines.append(
            "在本回放条件下，S-OPT **未**优于 S-RULE（F5-5 允许该结果）。"
            "可能原因：回放窗口短、预测误差主导、或预算约束并未真正绑定。"
            "该结果如实报告，未通过调整假设来制造优势。"
        )
    lines += [
        "",
        "## 守恒与容量溢出",
        "",
        markdown_table(
            ["指标", "S-RULE", "S-OPT"],
            [
                ("回放归还总数", rule["returns"], optimized["returns"]),
                ("因容量不足未能入库的归还（returns_overflow）", rule["returns_overflow"], optimized["returns_overflow"]),
                ("初始库存", rule["initial_inventory"], optimized["initial_inventory"]),
                ("末期库存", rule["final_inventory"], optimized["final_inventory"]),
                ("总容量", rule["capacity_total"], optimized["capacity_total"]),
            ],
        ),
        "",
        "`returns_overflow` 是回放中**超过该格子停放容量**的归还数量。这些车辆被计为溢出并"
        "从被跟踪的库存中剔除，而不是凭空消失后又被当成可用供给；两策略使用相同的溢出规则，"
        "因此不影响对比的公平性。守恒计数器（`violations`）覆盖调度搬运与借还，不含溢出，"
        "溢出单独统计，见上表与 `outputs/sim_<strategy>.<source>.json`。",
        "",
        "## 局限",
        "",
        "- 仿真只回放 F3 测试时段，窗口长度受数据时间覆盖限制（XM 仅 4 个小时片，SH 约 6 天）。",
        "- 容量、初始库存、运力预算均为假设值，REPORT.md 的敏感性分析给出了变化后的结果。",
        "- 未建模车辆路径（VRP），搬运成本按 bike-km 计，不区分车辆调度次数。",
        "",
    ]
    return "\n".join(lines)
