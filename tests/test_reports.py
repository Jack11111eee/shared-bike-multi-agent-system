"""F6-4: report numbers must come from the collected artefacts, not literals."""

from __future__ import annotations

from bike_agent.report.text import render_cross_source, render_forecast_report


def _sig(mean: float, low: float, high: float, n: int = 100) -> dict:
    return {
        "n": n,
        "n_bootstrap": 2000,
        "alpha": 0.05,
        "mean_diff": mean,
        "ci_low": low,
        "ci_high": high,
        "significant": low > 0 or high < 0,
    }


def _entry(
    source: str,
    cell_size: int,
    zero_share: float,
    model_wins: bool,
    cost_rule: float,
    cost_opt: float,
    model_sig: dict | None = None,
    cost_sig: dict | None = None,
) -> dict:
    model_mae = 0.9 if model_wins else 1.2
    significance = model_sig or _sig(-0.1, -0.2, -0.05)
    return {
        "source": source,
        "cell_size_m": cell_size,
        "quality": {"rows": 100},
        "prep": {"panel": {"n_cells": 4, "n_buckets": 6, "zero_out_share": zero_share, "total_out": 50}},
        "forecast_metrics": {
            "baseline": {"out_count": {"mae": 1.0}, "in_count": {"mae": 1.0}},
            "model": {"out_count": {"mae": model_mae}, "in_count": {"mae": model_mae}},
        },
        "forecast_meta": {
            "split": {"n_train_buckets": 4, "n_test_buckets": 2},
            "params": {},
            "features": [],
            "seed": 42,
            "train_fraction": 0.8,
            "significance": {"out_count": significance, "in_count": significance},
        },
        "risk": {"n_replenish": 1, "n_clear": 1},
        "comparison": {
            "rule": {"unmet_requests": 10, "distance_bike_km": 5.0, "composite_cost": cost_rule},
            "optimized": {"unmet_requests": 9, "distance_bike_km": 4.0, "composite_cost": cost_opt},
            "significance": cost_sig or _sig(cost_opt - cost_rule, -2.0, -0.5),
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
    data = {
        "xm": _entry(
            "xm", 1000, 0.2, model_wins=True, cost_rule=10.0, cost_opt=9.0,
            model_sig=_sig(-0.3, -0.5, -0.1), cost_sig=_sig(-1.0, -2.0, -0.5),
        ),
        "sh": _entry(
            "sh", 2000, 0.6, model_wins=False, cost_rule=10.0, cost_opt=11.0,
            model_sig=_sig(+0.2, +0.1, +0.4), cost_sig=_sig(+1.0, +0.2, +1.8),
        ),
    }
    markdown = render_cross_source(data)

    assert "模型**显著更优**" in markdown
    assert "模型**显著更差**" in markdown
    assert "S-OPT 的综合代价**显著更低**" in markdown
    assert "S-OPT 的综合代价**显著更高**" in markdown


def test_verdicts_do_not_claim_a_difference_the_interval_cannot_support():
    """A CI that crosses zero must never be reported as a win or a loss."""
    data = {
        "xm": _entry(
            "xm", 1000, 0.2, model_wins=True, cost_rule=10.0, cost_opt=9.5,
            model_sig=_sig(-0.1, -0.3, +0.2), cost_sig=_sig(-0.5, -1.5, +0.4),
        ),
        "sh": _entry(
            "sh", 2000, 0.6, model_wins=False, cost_rule=10.0, cost_opt=10.5,
            model_sig=_sig(+0.1, -0.2, +0.3), cost_sig=_sig(+0.5, -0.4, +1.4),
        ),
    }
    markdown = render_cross_source(data)

    assert "无显著差异" in markdown
    assert "差异不显著" in markdown
    # The misleading framings this change removed.
    assert "未能同时优于基线" not in markdown
    assert "S-RULE 优于 S-OPT" not in markdown
    assert "S-OPT 略优" not in markdown


def test_forecast_report_wording_follows_significance():
    significant = render_forecast_report(
        _entry("xm", 1000, 0.2, model_wins=True, cost_rule=10.0, cost_opt=9.0,
               model_sig=_sig(-0.3, -0.5, -0.1))
    )
    assert "显著优于" in significant

    inconclusive = render_forecast_report(
        _entry("xm", 1000, 0.2, model_wins=False, cost_rule=10.0, cost_opt=9.0,
               model_sig=_sig(+0.1, -0.2, +0.4))
    )
    assert "均无显著差异" in inconclusive
    assert "未能同时优于基线" not in inconclusive


def test_forecast_report_renders_without_metrics():
    entry = {"source": "xm", "forecast_metrics": None, "forecast_meta": None}
    markdown = render_forecast_report(entry)
    assert "# 短期需求预测评估 · XM" in markdown
    assert "预测指标尚未生成" in markdown
