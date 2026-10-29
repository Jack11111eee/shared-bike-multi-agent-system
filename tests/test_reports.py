"""F6-4: report numbers must come from the collected artefacts, not literals."""

from __future__ import annotations

from bike_agent.report.text import render_cross_source, render_forecast_report


def _entry(source: str, cell_size: int, zero_share: float, model_wins: bool, cost_rule: float, cost_opt: float) -> dict:
    model_mae = 0.9 if model_wins else 1.2
    return {
        "source": source,
        "cell_size_m": cell_size,
        "quality": {"rows": 100},
        "prep": {"panel": {"n_cells": 4, "n_buckets": 6, "zero_out_share": zero_share, "total_out": 50}},
        "forecast_metrics": {
            "baseline": {"out_count": {"mae": 1.0}, "in_count": {"mae": 1.0}},
            "model": {"out_count": {"mae": model_mae}, "in_count": {"mae": model_mae}},
        },
        "forecast_meta": {"split": {}, "params": {}, "features": [], "seed": 42, "train_fraction": 0.8},
        "risk": {"n_replenish": 1, "n_clear": 1},
        "comparison": {
            "rule": {"unmet_requests": 10, "distance_bike_km": 5.0, "composite_cost": cost_rule},
            "optimized": {"unmet_requests": 9, "distance_bike_km": 4.0, "composite_cost": cost_opt},
        },
    }


def test_cross_source_zero_share_prose_matches_the_data():
    data = {
        "xm": _entry("xm", 1000, 0.125, model_wins=True, cost_rule=10.0, cost_opt=9.0),
        "sh": _entry("sh", 2000, 0.5, model_wins=False, cost_rule=10.0, cost_opt=11.0),
    }
    markdown = render_cross_source(data)

    assert "12.50%" in markdown
    assert "50.00%" in markdown
    # The old hardcoded sentence must never come back.
    assert "约 21%" not in markdown
    assert "约 71%" not in markdown


def test_cross_source_verdicts_follow_the_numbers():
    win = {
        "xm": _entry("xm", 1000, 0.2, model_wins=True, cost_rule=10.0, cost_opt=9.0),
        "sh": _entry("sh", 2000, 0.6, model_wins=False, cost_rule=10.0, cost_opt=11.0),
    }
    markdown = render_cross_source(win)
    assert "XM 上 LightGBM 在借出与归还两个目标上均优于基线" in markdown
    assert "SH 上模型未能同时优于基线" in markdown
    assert "XM 上 S-OPT 略优" in markdown
    assert "SH 上 S-RULE 优于 S-OPT" in markdown


def test_forecast_report_renders_without_metrics():
    entry = {"source": "xm", "forecast_metrics": None, "forecast_meta": None}
    markdown = render_forecast_report(entry)
    assert "# 短期需求预测评估 · XM" in markdown
