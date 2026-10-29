"""Preparation pipeline: clean -> grid -> panel, with persisted artefacts."""

from __future__ import annotations

from typing import Any

import pandas as pd

from bike_agent.config import Layout, source_config
from bike_agent.prep.aggregate import build_panel
from bike_agent.prep.clean import clean_trips
from bike_agent.prep.grid import build_grid
from bike_agent.utils import markdown_table, pct, write_json


def run_prep(
    config: dict[str, Any],
    layout: Layout,
    source: str,
    cell_size: int | None = None,
    write: bool = True,
) -> dict[str, Any]:
    src_cfg = source_config(config, source)
    cell_size = int(cell_size or src_cfg["cell_size_m"])
    contract_path = layout.contract_path(source)
    if not contract_path.exists():
        raise FileNotFoundError(
            f"contract table missing for {source}; run `bike-agent data fetch --source {source}` first"
        )

    trips = pd.read_parquet(contract_path)
    clean_cfg = config.get("clean", {})
    result = clean_trips(
        trips,
        min_duration_s=float(clean_cfg.get("min_duration_s", 60)),
        max_duration_s=float(clean_cfg.get("max_duration_s", 21600)),
        pseudo_trip_max_distance_m=float(clean_cfg.get("pseudo_trip_max_distance_m", 50)),
        bbox_quantiles=tuple(src_cfg.get("bbox_quantiles", (0.005, 0.995))),
    )
    grid = build_grid(result.frame, cell_size)
    panel, meta = build_panel(result.frame, grid)

    payload = {
        "source": source,
        "cell_size_m": cell_size,
        "clean": result.as_dict(),
        "panel": meta,
        "panel_frame": panel,
        "grid": grid,
        "clean_frame": result.frame,
    }

    if write:
        layout.ensure()
        result.frame.to_parquet(layout.clean_path(source), index=False)
        panel.to_parquet(layout.panel_path(source, cell_size), index=False)
        grid.cells.to_parquet(layout.cells_path(source, cell_size), index=False)
        write_json(
            layout.processed / f"prep_meta.{source}.{cell_size}m.json",
            {"source": source, "cell_size_m": cell_size, "clean": result.as_dict(), "panel": meta},
        )
    return payload


def render_prep_report(source: str, payload: dict[str, Any]) -> str:
    clean = payload["clean"]
    meta = payload["panel"]
    grid = meta["grid"]
    lines = [
        f"# 预处理与时空聚合报告 · {source.upper()}",
        "",
        "本报告由 `bike-agent prep` 生成，对应 F2。空间单元为规则网格（D2），时间单元为 1 小时（D3）。",
        "",
        "## 清洗规则（逐条）",
        "",
        markdown_table(
            ["规则", "移除行数", "剩余行数", "原因"],
            [
                (r["rule"], r["rows_removed"], r["rows_remaining"], r["reason"])
                for r in clean["rules"]
            ],
        ),
        "",
        f"输入 {clean['rows_in']:,} 行，保留 {clean['rows_out']:,} 行，"
        f"共移除 {clean['rows_removed']:,} 行（{pct(clean['rows_removed'] / max(clean['rows_in'], 1))}）。",
        "",
        "## 空间单元（D2）",
        "",
        markdown_table(
            ["指标", "数值"],
            [
                ("网格边长", f"{grid['cell_size_m']} m"),
                ("网格尺寸", f"{grid['nx']} × {grid['ny']}"),
                ("有效格子数", meta["n_cells"]),
                ("经度范围", f"{grid['lon_min']:.6f} → {grid['lon_max']:.6f}"),
                ("纬度范围", f"{grid['lat_min']:.6f} → {grid['lat_max']:.6f}"),
            ],
        ),
        "",
        "## 时间单元（D3）",
        "",
        markdown_table(
            ["指标", "数值"],
            [
                ("时间桶数", meta["n_buckets"]),
                ("覆盖日期数", meta["n_dates"]),
                ("出现的小时", ", ".join(str(h) for h in meta["hours"])),
                ("首桶", meta["first_bucket"]),
                ("末桶", meta["last_bucket"]),
            ],
        ),
        "",
        "## 聚合与守恒（F2-2 / F2-5）",
        "",
        markdown_table(
            ["指标", "数值"],
            [
                ("借出总量 out_count", f"{meta['total_out']:,}"),
                ("归还总量 in_count", f"{meta['total_in']:,}"),
                ("清洗后行程数", f"{meta['total_trips']:,}"),
                ("借出守恒", "是" if meta["out_conserved"] else "否"),
                ("归还守恒", "是" if meta["in_conserved"] else "否"),
                ("每格每桶平均借出", f"{meta['mean_out_per_cell_bucket']:.3f}"),
            ],
        ),
        "",
        "## 零值报告（F2-4）",
        "",
        markdown_table(
            ["指标", "占比"],
            [
                ("零借出格子-时间片", pct(meta["zero_out_share"])),
                ("零归还格子-时间片", pct(meta["zero_in_share"])),
                ("借出与归还同时为零", pct(meta["zero_any_share"])),
            ],
        ),
        "",
    ]
    if meta["zero_out_share"] > 0.80:
        lines += [
            "> 零借出占比超过 80%。该粒度下大多数格子-时间片没有观测，"
            "预测与风险排序的不确定性会显著上升；DECISIONS.md 中记录了是否调整粒度及依据。",
            "",
        ]
    else:
        lines += [
            "> 零借出占比低于 80%，该粒度可直接用于预测与仿真。",
            "",
        ]
    return "\n".join(lines)
