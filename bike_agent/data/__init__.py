"""Data-source registry and the fetch/validate pipeline (F1)."""

from __future__ import annotations

from typing import Any

import pandas as pd

from bike_agent.config import Layout
from bike_agent.data.base import DataSourceAdapter, DataSourceError
from bike_agent.data.contract import assert_contract, validate_contract
from bike_agent.data.quality import build_quality_report, render_quality_markdown
from bike_agent.data.sh import ShanghaiAdapter
from bike_agent.data.xm import XiamenAdapter
from bike_agent.utils import read_json, write_json

_ADAPTERS: dict[str, type[DataSourceAdapter]] = {
    "xm": XiamenAdapter,
    "sh": ShanghaiAdapter,
}


def get_adapter(
    config: dict[str, Any],
    layout: Layout,
    source: str,
    force: bool = False,
) -> DataSourceAdapter:
    if source not in _ADAPTERS:
        raise DataSourceError(f"no adapter for source {source!r}; available: {sorted(_ADAPTERS)}")
    return _ADAPTERS[source](config, layout, force=force)


def _duration_bounds(config: dict[str, Any]) -> dict[str, float]:
    clean = config.get("clean", {})
    return {
        "min_duration_s": float(clean.get("min_duration_s", 60)),
        "max_duration_s": float(clean.get("max_duration_s", 21600)),
    }


def fetch_source(
    config: dict[str, Any],
    layout: Layout,
    source: str,
    force: bool = False,
) -> dict[str, Any]:
    """Download, validate and persist one source (FR-DATA-1 / FR-DATA-5)."""
    layout.ensure()
    adapter = get_adapter(config, layout, source, force=force)
    manifest_entry = adapter.fetch()
    trips = adapter.contract_frame()
    validation = assert_contract(trips, **_duration_bounds(config))

    contract_path = layout.contract_path(source)
    trips.to_parquet(contract_path, index=False)

    extras = adapter.load_extras()
    for name, table in extras.items():
        path = layout.processed / f"{name}.{source}.parquet"
        table.to_parquet(path, index=False)

    write_json(layout.processed / f"adapter_stats.{source}.json", adapter.stats)
    return {
        "source": source,
        "rows": validation["rows"],
        "contract_path": str(contract_path),
        "manifest": manifest_entry,
        "adapter_stats": adapter.stats,
        "extras": {name: len(table) for name, table in extras.items()},
    }


def _load_extras(layout: Layout, source: str) -> dict[str, pd.DataFrame]:
    extras: dict[str, pd.DataFrame] = {}
    for name in ("fences", "weather"):
        path = layout.processed / f"{name}.{source}.parquet"
        if path.exists():
            extras[name] = pd.read_parquet(path)
    return extras


def _source_notes(source: str, adapter_stats: dict[str, Any], extras: dict[str, pd.DataFrame]) -> list[str]:
    notes: list[str] = []
    if source == "xm":
        notes.append(
            "时间覆盖仅 2020-12-21～12-25 五个工作日、每天 06:00–10:00，"
            "因此可用于训练/测试的时间片非常少；这是 REQUIREMENTS.md 2.2 已披露的缺陷。"
        )
        notes.append(
            "原始订单表 UPDATE_TIME1 只含日期、UPDATE_TIME2 只含时刻，适配器已显式拼接；"
            "否则全部时间会落到 00:00（2.2 记录的已知坑）。"
        )
        if "fences" in extras:
            notes.append(
                f"电子围栏文件包含 {len(extras['fences']):,} 个围栏（含四角坐标），"
                "是仿真中停放容量约束的唯一数据来源。"
            )
        notes.append("轨迹数据（2,849,243 个 GPS 点）未参与本流水线，仅使用订单、围栏与天气。")
    if source == "sh":
        notes.append(
            "空间稀疏：约 68 km × 68 km 内日均约 3,302 条订单；本流水线使用稳健包围盒内的规则网格。"
        )
        notes.append("数据源没有电子围栏、天气与停放容量，仿真中的容量退化为配置的固定假设（FR-DATA-6）。")
        notes.append("原始 userid 在适配器边界被丢弃，未写入任何产物（NF-6）。")
    if adapter_stats:
        def _num(key: str) -> str:
            value = adapter_stats.get(key)
            return f"{value:,}" if isinstance(value, int) else "n/a"

        notes.append(
            "适配器解析统计："
            f"原始记录 {_num('raw_rows')} 行，"
            f"配对成功 {_num('paired_trips')} 条行程，"
            f"丢弃无法解析时间/坐标 {_num('dropped_unparseable_rows')} 行，"
            f"丢弃非正时长 {_num('dropped_nonpositive_duration')} 条。"
        )
    return notes


def validate_source(config: dict[str, Any], layout: Layout, source: str) -> dict[str, Any]:
    """Run FR-DATA-4 checks on the persisted contract and write the report."""
    contract_path = layout.contract_path(source)
    if not contract_path.exists():
        raise DataSourceError(
            f"no contract table for {source!r} at {contract_path}; run `bike-agent data fetch --source {source}` first"
        )
    frame = pd.read_parquet(contract_path)
    validation = validate_contract(frame, **_duration_bounds(config))
    extras = _load_extras(layout, source)

    stats_path = layout.processed / f"adapter_stats.{source}.json"
    adapter_stats = read_json(stats_path) if stats_path.exists() else {}

    report = build_quality_report(source, frame, validation, extras)
    report["adapter_stats"] = adapter_stats
    report["notes"] = _source_notes(source, adapter_stats, extras)
    report["hard_checks_ok"] = bool(
        validation["columns_ok"]
        and validation["start_before_end_ok"]
        and validation["lat_range_ok"]
        and validation["lon_range_ok"]
        and validation["time_parse_failures"] == 0
    )

    layout.reports.mkdir(parents=True, exist_ok=True)
    markdown = render_quality_markdown(report, report["notes"])
    layout.report_path(f"data_quality.{source}.md").write_text(markdown, encoding="utf-8")
    write_json(layout.processed / f"quality.{source}.json", report)

    if not report["hard_checks_ok"]:
        raise DataSourceError(f"integrity checks failed for {source}; see reports/data_quality.{source}.md")
    return report

