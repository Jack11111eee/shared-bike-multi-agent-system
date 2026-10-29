"""Risk pipeline: score the next time unit and write priority lists (F4)."""

from __future__ import annotations

from typing import Any

import pandas as pd

from bike_agent.config import Layout, source_config
from bike_agent.forecast.split import time_split
from bike_agent.risk.score import (
    PRIORITY_COLUMNS,
    WEIGHTS,
    build_risk_table,
    priority_lists,
    select_risk_bucket,
)
from bike_agent.utils import markdown_table, write_json


def run_risk(
    config: dict[str, Any],
    layout: Layout,
    source: str,
    write: bool = True,
) -> dict[str, Any]:
    src_cfg = source_config(config, source)
    cell_size = int(src_cfg["cell_size_m"])
    forecast_path = layout.forecast_path(source)
    panel_path = layout.panel_path(source, cell_size)
    if not forecast_path.exists() or not panel_path.exists():
        raise FileNotFoundError(
            f"forecast/panel missing for {source}; run `bike-agent forecast --source {source}` first"
        )

    forecast = pd.read_parquet(forecast_path)
    panel = pd.read_parquet(panel_path)
    split = time_split(panel, float(config.get("forecast", {}).get("train_fraction", 0.8)))
    min_share = float(config.get("risk", {}).get("min_bucket_demand_share", 0.1))
    time_bucket = select_risk_bucket(forecast, panel, min_share)
    table = build_risk_table(forecast, panel, split.train_buckets, time_bucket=time_bucket)
    lists = priority_lists(table)

    result = {
        "source": source,
        "cell_size_m": cell_size,
        "time_bucket": str(time_bucket),
        "weights": WEIGHTS,
        "n_cells": int(len(table)),
        "n_replenish": int(len(lists["replenish"])),
        "n_clear": int(len(lists["clear"])),
        "observed_demand_in_bucket": int(
            panel.loc[panel["time_bucket"] == time_bucket, ["out_count", "in_count"]].to_numpy().sum()
        ),
        "table": table,
        "replenish": lists["replenish"],
        "clear": lists["clear"],
    }

    if write:
        layout.ensure()
        replenish = lists["replenish"].loc[:, PRIORITY_COLUMNS]
        clear = lists["clear"].loc[:, PRIORITY_COLUMNS]
        replenish.to_csv(layout.priority_replenish_path(source), index=False)
        clear.to_csv(layout.priority_clear_path(source), index=False)
        if source == config.get("primary_source") and cell_size == int(src_cfg["cell_size_m"]):
            replenish.to_csv(layout.outputs / "priority_replenish.csv", index=False)
            clear.to_csv(layout.outputs / "priority_clear.csv", index=False)
        write_json(
            layout.outputs / f"risk_summary.{source}.json",
            {
                "source": source,
                "time_bucket": result["time_bucket"],
                "weights": WEIGHTS,
                "n_cells": result["n_cells"],
                "n_replenish": result["n_replenish"],
                "n_clear": result["n_clear"],
                "top_replenish": replenish.head(5).loc[
                    :, ["rank", "cell_id", "score", "pred_net_flow", "pred_out", "pred_in"]
                ].to_dict(orient="records"),
                "top_clear": clear.head(5).loc[
                    :, ["rank", "cell_id", "score", "pred_net_flow", "pred_out", "pred_in"]
                ].to_dict(orient="records"),
            },
        )
    return result


def render_risk_report(result: dict[str, Any]) -> str:
    replenish = result["replenish"]
    clear = result["clear"]
    source = result["source"].upper()
    lines = [
        f"# 失衡风险与调度优先级报告 · {source}",
        "",
        f"预测时间单元：`{result['time_bucket']}`（该桶观测借还总量 "
        f"{result['observed_demand_in_bucket']}）；评分格数 {result['n_cells']}，"
        f"补车候选 {result['n_replenish']} 个，清运候选 {result['n_clear']} 个。",
        "",
        "时间单元选取规则：在测试时段内取**观测借还总量不低于峰值 10%** 的最后一个时间桶，"
        "避免把数据尾部（例如跨夜归还落在次日凌晨）的空桶当作调度目标。",
        "",
        "## 评分公式（可手工复算）",
        "",
        "```",
        "net        = pred_out - pred_in            # 正 = 净流出",
        "magnitude  = |net|",
        "demand     = pred_out + pred_in",
        "volatility = std(net_flow) over training buckets of the cell",
        "score      = 0.60 * magnitude/max(magnitude)",
        "           + 0.25 * demand/max(demand)",
        "           + 0.15 * volatility/max(volatility)",
        "```",
        "",
        "权重来源（D7）：可解释性优先。净流出幅度直接决定调度动作，权重最高；"
        "区域需求规模次之；历史波动受样本长度限制，权重最低。"
        "每个 CSV 条目都带有 `mag_norm / demand_norm / vol_norm`，可直接代入公式复核。",
        "",
        "## 符号约定",
        "",
        "- `pred_net_flow > 0`（净流出）→ 归入**优先补车清单**。",
        "- `pred_net_flow < 0`（净流入）→ 归入**优先清运清单**。",
        "",
        "## 优先补车 Top 10",
        "",
        markdown_table(
            ["rank", "cell_id", "score", "pred_net_flow", "pred_out", "pred_in"],
            [
                (r.rank, r.cell_id, f"{r.score:.4f}", f"{r.pred_net_flow:+.2f}", f"{r.pred_out:.2f}", f"{r.pred_in:.2f}")
                for r in replenish.head(10).itertuples(index=False)
            ],
        ),
        "",
        "## 优先清运 Top 10",
        "",
        markdown_table(
            ["rank", "cell_id", "score", "pred_net_flow", "pred_out", "pred_in"],
            [
                (r.rank, r.cell_id, f"{r.score:.4f}", f"{r.pred_net_flow:+.2f}", f"{r.pred_out:.2f}", f"{r.pred_in:.2f}")
                for r in clear.head(10).itertuples(index=False)
            ],
        ),
        "",
        "## 为什么排第一",
        "",
    ]
    if len(replenish):
        lines.append(f"- 补车第一：`{replenish.iloc[0]['explanation']}`")
    if len(clear):
        lines.append(f"- 清运第一：`{clear.iloc[0]['explanation']}`")
    lines += [
        "",
        f"完整清单见 `outputs/priority_replenish.{result['source']}.csv` 与 "
        f"`outputs/priority_clear.{result['source']}.csv`。",
        "",
    ]
    return "\n".join(lines)
