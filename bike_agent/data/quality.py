"""FR-DATA-4 integrity report for a normalised trip contract."""

from __future__ import annotations

from typing import Any

import pandas as pd

from bike_agent.utils import markdown_table, pct


def build_quality_report(
    source: str,
    frame: pd.DataFrame,
    validation: dict[str, Any],
    extras: dict[str, pd.DataFrame] | None = None,
) -> dict[str, Any]:
    extras = extras or {}
    report: dict[str, Any] = {
        "source": source,
        "rows": validation["rows"],
        "columns": list(frame.columns),
        "time_min": str(validation["time_min"]),
        "time_max": str(validation["time_max"]),
        "bbox": validation["bbox"],
        "duplicate_trip_ids": validation["duplicate_trip_ids"],
        "duplicate_rate": validation["duplicate_rate"],
        "missing_rate": validation["null_rate"],
        "abnormal_duration_rows": validation["abnormal_duration_rows"],
        "abnormal_duration_rate": validation["abnormal_duration_rate"],
        "start_before_end_ok": validation["start_before_end_ok"],
        "start_before_end_violations": validation["start_before_end_violations"],
        "lat_range_ok": validation["lat_range_ok"],
        "lon_range_ok": validation["lon_range_ok"],
        "unique_bikes": int(frame["bike_id"].nunique()),
        "extras": {name: int(len(table)) for name, table in extras.items()},
    }
    return report


def render_quality_markdown(report: dict[str, Any], extra_notes: list[str] | None = None) -> str:
    source = report["source"].upper()
    missing_rows = [(col, rate) for col, rate in report["missing_rate"].items() if rate > 0]
    bbox = report["bbox"]
    lines = [
        f"# 数据完整性报告 · {source}",
        "",
        "本报告由 `bike-agent data validate` 自动生成，对应 REQUIREMENTS.md 的 F1 / FR-DATA-4。",
        "原始数据只读，清洗产物写入 `data/processed/`。",
        "",
        "## 核心指标",
        "",
        markdown_table(
            ["指标", "数值"],
            [
                ("总行数（订单契约表）", report["rows"]),
                ("唯一车辆数", report["unique_bikes"]),
                ("时间跨度", f"{report['time_min']} → {report['time_max']}"),
                ("坐标包围盒（纬度）", f"{bbox['lat_min']:.6f} → {bbox['lat_max']:.6f}"),
                ("坐标包围盒（经度）", f"{bbox['lon_min']:.6f} → {bbox['lon_max']:.6f}"),
                ("重复 trip_id 行数", report["duplicate_trip_ids"]),
                ("重复率", pct(report["duplicate_rate"], 4)),
                ("异常时长行数（<60s 或 >6h）", report["abnormal_duration_rows"]),
                ("异常时长占比", pct(report["abnormal_duration_rate"], 4)),
                ("start_time < end_time 违反行数", report["start_before_end_violations"]),
                ("坐标范围合法", "是" if (report["lat_range_ok"] and report["lon_range_ok"]) else "否"),
            ],
        ),
        "",
        "## 缺失率（按契约字段）",
        "",
        markdown_table(["字段", "缺失率"], [(col, pct(rate, 4)) for col, rate in report["missing_rate"].items()]),
        "",
    ]
    if missing_rows:
        lines += ["缺失字段：" + "、".join(col for col, _ in missing_rows), ""]
    else:
        lines += ["所有契约字段无缺失。", ""]

    if report["extras"]:
        lines += [
            "## 附加数据",
            "",
            markdown_table(["表", "行数"], [(name, count) for name, count in report["extras"].items()]),
            "",
        ]

    if extra_notes:
        lines += ["## 数据源已知缺陷与说明", ""]
        lines += [f"- {note}" for note in extra_notes]
        lines.append("")
    return "\n".join(lines)
