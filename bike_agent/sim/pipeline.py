"""Simulation pipeline: run both strategies and render the comparison (F5)."""

from __future__ import annotations

from typing import Any

import pandas as pd

from bike_agent.config import Layout, source_config
from bike_agent.prep.grid import build_grid
from bike_agent.sim.capacity import cell_capacities
from bike_agent.sim.engine import simulate
from bike_agent.stats import describe, is_significant, paired_bootstrap
from bike_agent.utils import delta_pct, markdown_table, pct, write_json


def comparison_verdict(comparison: dict[str, Any]) -> str:
    """One-sentence verdict on S-OPT vs S-RULE, gated on the paired bootstrap.

    A lower point estimate is not the same claim as a real improvement, so the
    wording follows ``significance`` rather than the raw cost difference.
    """
    rule = comparison.get("rule") or {}
    optimized = comparison.get("optimized") or {}
    try:
        rule_cost = float(rule["composite_cost"])
        opt_cost = float(optimized["composite_cost"])
    except (KeyError, TypeError, ValueError):
        return ""
    lower = opt_cost < rule_cost
    change = abs(opt_cost - rule_cost) / rule_cost * 100 if rule_cost else 0.0
    detail = (
        f"综合代价 {rule_cost:.2f} → {opt_cost:.2f}"
        f"（点估计{'下降' if lower else '上升'} {change:.2f}%）"
    )
    stats = comparison.get("significance") or {}
    significant = is_significant(stats)
    total = stats.get("total_diff")
    if total is not None and stats.get("ci_low_total") is not None:
        interval = (
            f"总差异 {total:+.2f}，95% CI "
            f"[{stats['ci_low_total']:+.2f}, {stats['ci_high_total']:+.2f}]，n={stats['n']}"
        )
    else:
        interval = describe(stats)
    if significant is None:
        return f"{detail}；显著性检验尚未生成。"
    if significant:
        return f"S-OPT 的综合代价**显著{'更低' if lower else '更高'}**：{detail}；{interval}。"
    if lower:
        return f"S-OPT 的综合代价点估计更低，但**差异不显著**：{detail}；{interval}。"
    return f"S-OPT 的综合代价点估计更高，但**差异不显著**：{detail}；{interval}。"


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


def _paired_significance(
    rule_steps: pd.DataFrame,
    optimized_steps: pd.DataFrame,
    assumptions: dict[str, Any],
) -> dict[str, Any]:
    """Paired bootstrap on the per-bucket composite-cost difference.

    The composite cost is the headline metric (unmet penalty + travel cost), so
    it is what gets tested.  Both strategies replay the same buckets, so only
    ``optimized - rule`` per bucket matters.  Also counts how often each side
    actually wins, because a small mean difference spread over many tied
    buckets is a different claim from a consistent win.
    """
    penalty = float(assumptions["unmet_penalty"])
    distance_cost = float(assumptions["distance_cost_per_bike_km"])
    merged = rule_steps[["time_bucket", "unmet_requests", "distance_bike_km"]].merge(
        optimized_steps[["time_bucket", "unmet_requests", "distance_bike_km"]],
        on="time_bucket",
        suffixes=("_rule", "_optimized"),
    )
    rule_cost = merged["unmet_requests_rule"] * penalty + merged["distance_bike_km_rule"] * distance_cost
    opt_cost = (
        merged["unmet_requests_optimized"] * penalty
        + merged["distance_bike_km_optimized"] * distance_cost
    )
    diff = (opt_cost - rule_cost).to_numpy(dtype=float)
    stats = paired_bootstrap(diff)
    stats["metric"] = "composite_cost"
    stats["buckets_optimized_better"] = int((diff < 0).sum())
    stats["buckets_rule_better"] = int((diff > 0).sum())
    stats["buckets_tied"] = int((diff == 0).sum())
    # The bootstrap describes the *mean per-bucket* difference, but every report
    # quotes the *total*. Percentile intervals are equivariant under scaling, so
    # multiplying the endpoints by n gives the interval of the total exactly.
    # Without this, a total-sized point estimate sits outside a mean-sized CI.
    stats["total_diff"] = float(diff.sum())
    stats["ci_low_total"] = float(stats["ci_low"]) * stats["n"] if stats["ci_low"] is not None else None
    stats["ci_high_total"] = float(stats["ci_high"]) * stats["n"] if stats["ci_high"] is not None else None
    return stats


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
        "significance": _paired_significance(
            rule["steps"], optimized["steps"], rule["summary"]["assumptions"]
        ),
    }
    if write:
        layout.ensure()
        layout.report_path(f"strategy_comparison.{source}.md").write_text(
            render_comparison(report), encoding="utf-8"
        )
        write_json(layout.outputs / f"strategy_comparison.{source}.json", report)
    return {"report": report, "rule": rule, "optimized": optimized}


def write_comparison_from_disk(layout: Layout, source: str) -> bool:
    """Render the comparison report from the two persisted summaries.

    ``bike-agent simulate --strategy <x>`` runs one strategy at a time (F5-1),
    so the comparison table is refreshed once both summaries exist on disk.
    Returns True when a report was written.
    """
    from bike_agent.utils import read_json

    paths = {strategy: layout.sim_path(source, strategy, "json") for strategy in ("rule", "optimized")}
    if not all(path.exists() for path in paths.values()):
        return False
    report = {
        "source": source,
        "rule": read_json(paths["rule"]),
        "optimized": read_json(paths["optimized"]),
    }
    steps = {strategy: layout.sim_path(source, strategy, "csv") for strategy in ("rule", "optimized")}
    if all(path.exists() for path in steps.values()):
        report["significance"] = _paired_significance(
            pd.read_csv(steps["rule"]),
            pd.read_csv(steps["optimized"]),
            report["rule"]["assumptions"],
        )
    layout.ensure()
    layout.report_path(f"strategy_comparison.{source}.md").write_text(
        render_comparison(report), encoding="utf-8"
    )
    write_json(layout.outputs / f"strategy_comparison.{source}.json", report)
    return True


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
                 delta_pct(rule["unmet_requests"], optimized["unmet_requests"])),
                ("车辆搬运距离（bike-km）", f"{rule['distance_bike_km']:.2f}", f"{optimized['distance_bike_km']:.2f}",
                 delta_pct(rule["distance_bike_km"], optimized["distance_bike_km"])),
                ("综合代价（模拟单位）", f"{rule['composite_cost']:.2f}", f"{optimized['composite_cost']:.2f}",
                 delta_pct(rule["composite_cost"], optimized["composite_cost"])),
                ("服务率", pct(rule["service_rate"]), pct(optimized["service_rate"]),
                 delta_pct(rule["service_rate"], optimized["service_rate"])),
                ("搬运车辆数（bikes）", rule["bikes_moved"], optimized["bikes_moved"],
                 delta_pct(rule["bikes_moved"], optimized["bikes_moved"])),
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
    verdict = comparison_verdict(report)
    lines.append("在本回放条件下，" + verdict if verdict else "在本回放条件下，策略对比尚未生成。")
    lines.append("")
    lines.append(
        f"未满足请求 {rule['unmet_requests']} → {optimized['unmet_requests']}，"
        f"搬运距离 {rule['distance_bike_km']:.2f} → {optimized['distance_bike_km']:.2f} bike-km。"
    )
    stats = report.get("significance") or {}
    if stats.get("buckets_tied") is not None:
        better = stats["buckets_optimized_better"]
        worse = stats["buckets_rule_better"]
        lines.append("")
        lines.append(
            f"逐时间单元看：共 {stats['n']} 个单元，S-OPT 综合代价更低 {better} 个、"
            f"更高 {worse} 个、持平 {stats['buckets_tied']} 个。"
        )
        total_lower = float(optimized["composite_cost"]) < float(rule["composite_cost"])
        if total_lower and better >= worse:
            lines.append("改善在多数或并列多数的时间单元上出现，与总代价的方向一致。")
        elif total_lower:
            lines.append(
                "改善并非在每个单元上一致：S-OPT 的收益集中在少数单元（单次幅度较大），"
                "S-RULE 则在更多单元上略占优（单次幅度很小）。因此“总代价更低”成立，"
                "但不能表述为“每个时段都更好”。"
            )
        elif better >= worse:
            lines.append(
                "S-OPT 在多数单元上略占优，但总代价仍更高——少数单元上 S-RULE 的优势幅度更大。"
            )
        else:
            lines.append(
                "S-OPT 在更少的单元上占优，总代价也更高，两个层面方向一致。"
            )
    lines.append("")
    lines.append("按 F5-5，无论 S-OPT 胜出与否都如实报告，未通过调整假设制造优势。")
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
        "溢出单独统计，见上表与 "
        f"`outputs/sim_rule.{report['source']}.json`、`outputs/sim_optimized.{report['source']}.json`。",
        "",
        "## 局限",
        "",
        "- 仿真只回放 F3 测试时段，窗口长度受数据时间覆盖限制（XM 仅 4 个小时片，SH 约 6 天）。",
        "- 容量、初始库存、运力预算均为假设值，REPORT.md 的敏感性分析给出了变化后的结果。",
        "- 未建模车辆路径（VRP），搬运成本按 bike-km 计，不区分车辆调度次数。",
        "",
    ]
    return "\n".join(lines)
