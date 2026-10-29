"""Report orchestration: metrics -> markdown -> figures."""

from __future__ import annotations

from typing import Any

import pandas as pd

from bike_agent.config import Layout, source_config
from bike_agent.report.figures import generate_figures, significance_forest
from bike_agent.report.metrics import build_key_numbers, collect_source, risk_rank_stability
from bike_agent.report.text import render_cross_source, render_decisions, render_forecast_report, render_report
from bike_agent.utils import write_json


def run_reports(
    config: dict[str, Any],
    layout: Layout,
    sources: list[str] | None = None,
    with_figures: bool = True,
) -> dict[str, Any]:
    sources = sources or list(config.get("sources", {}).keys())
    data = {source: collect_source(layout, config, source) for source in sources}
    stability = {source: risk_rank_stability(layout, source) for source in sources}

    # F6-4: every key number must resolve to a file under outputs/.
    for source, entry in data.items():
        write_json(layout.outputs / f"summary.{source}.json", entry)

    key_numbers = build_key_numbers(data)

    layout.ensure()
    write_json(layout.key_numbers_path(), key_numbers)

    for source, entry in data.items():
        layout.report_path(f"forecast_eval.{source}.md").write_text(
            render_forecast_report(entry), encoding="utf-8"
        )
        if with_figures:
            cell_size = int(source_config(config, source)["cell_size_m"])
            panel = pd.read_parquet(layout.panel_path(source, cell_size))
            cells = pd.read_parquet(layout.cells_path(source, cell_size))
            forecast = pd.read_parquet(layout.forecast_path(source))
            replenish = pd.read_csv(layout.priority_replenish_path(source))
            clear = pd.read_csv(layout.priority_clear_path(source))
            comparison = entry.get("comparison") or {}
            if comparison:
                generate_figures(
                    layout,
                    source,
                    panel,
                    cells,
                    forecast,
                    replenish,
                    clear,
                    comparison["rule"],
                    comparison["optimized"],
                )

    layout.report_path("cross_source.md").write_text(render_cross_source(data), encoding="utf-8")
    if with_figures:
        significance_forest(data, layout.figures / "significance.png")
    # DECISIONS.md and REPORT.md live at the project root (F6-2).
    (layout.root / "DECISIONS.md").write_text(render_decisions(data, config, stability), encoding="utf-8")
    (layout.root / "REPORT.md").write_text(
        render_report(data, config, key_numbers, stability), encoding="utf-8"
    )
    return {"data": data, "key_numbers": key_numbers, "stability": stability}
