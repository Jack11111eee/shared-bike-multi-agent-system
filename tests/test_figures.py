"""The significance figure must be derived from artefacts, not hand-placed."""

from __future__ import annotations

import matplotlib

matplotlib.use("Agg")

from bike_agent.report.figures import significance_forest


def _entry(source: str, model_mae: float, base_mae: float, sig: bool, cost_sig: bool) -> dict:
    return {
        "source": source,
        "forecast_metrics": {
            "baseline": {"out_count": {"mae": base_mae}, "in_count": {"mae": base_mae}},
            "model": {"out_count": {"mae": model_mae}, "in_count": {"mae": model_mae}},
        },
        "forecast_meta": {
            "significance": {
                target: {
                    "n": 100,
                    "ci_low": -0.5 if sig else -2.0,
                    "ci_high": -0.1 if sig else 1.0,
                    "significant": sig,
                }
                for target in ("out_count", "in_count")
            }
        },
        "comparison": {
            "rule": {"composite_cost": 100.0},
            "optimized": {"composite_cost": 95.0},
            "significance": {
                "n": 50,
                "ci_low": -8.0 if cost_sig else -2.0,
                "ci_high": -1.0 if cost_sig else 3.0,
                "significant": cost_sig,
            },
        },
    }


def test_significance_figure_writes_a_png(tmp_path):
    path = tmp_path / "significance.png"
    significance_forest(
        {
            "xm": _entry("xm", 1.2, 1.0, sig=False, cost_sig=False),
            "sh": _entry("sh", 0.9, 1.0, sig=True, cost_sig=True),
        },
        path,
    )
    assert path.exists()
    assert path.stat().st_size > 1000


def test_significance_figure_survives_missing_significance(tmp_path):
    """An older artefact set without the new field must not crash the report."""
    entry = _entry("xm", 1.0, 1.0, sig=True, cost_sig=True)
    entry["forecast_meta"] = {}
    entry["comparison"] = {"rule": {"composite_cost": 100.0}, "optimized": {"composite_cost": 95.0}}
    path = tmp_path / "significance.png"
    significance_forest({"xm": entry}, path)
    assert path.exists()
