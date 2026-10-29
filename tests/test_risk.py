"""F4: sign convention, explainability and priority-list schema."""

from __future__ import annotations

import pandas as pd
import pytest

from bike_agent.risk.score import (
    PRIORITY_COLUMNS,
    WEIGHTS,
    build_risk_table,
    priority_lists,
)


def _forecast() -> pd.DataFrame:
    bucket = pd.Timestamp("2020-12-21 09:00")
    return pd.DataFrame(
        [
            {"cell_id": "A", "time_bucket": bucket, "pred_out": 12.0, "pred_in": 2.0},
            {"cell_id": "B", "time_bucket": bucket, "pred_out": 7.0, "pred_in": 2.0},
            {"cell_id": "C", "time_bucket": bucket, "pred_out": 2.0, "pred_in": 7.0},
            {"cell_id": "D", "time_bucket": bucket, "pred_out": 2.0, "pred_in": 12.0},
        ]
    )


def _panel() -> pd.DataFrame:
    rows = []
    for bucket in pd.date_range("2020-12-21 06:00", periods=3, freq="h"):
        for cell, net in [("A", 5), ("B", 2), ("C", -3), ("D", -6)]:
            rows.append(
                {
                    "cell_id": cell,
                    "time_bucket": bucket,
                    "out_count": max(net, 0),
                    "in_count": max(-net, 0),
                    "net_flow": net,
                }
            )
    return pd.DataFrame(rows)


def test_sign_convention_splits_replenish_and_clear():
    panel = _panel()
    train_buckets = pd.DatetimeIndex(panel["time_bucket"].unique())
    table = build_risk_table(_forecast(), panel, train_buckets)
    lists = priority_lists(table)

    assert set(lists["replenish"]["cell_id"]) == {"A", "B"}
    assert set(lists["clear"]["cell_id"]) == {"C", "D"}
    assert (lists["replenish"]["pred_net_flow"] > 0).all()
    assert (lists["clear"]["pred_net_flow"] < 0).all()
    # Reversing the sign convention must never happen.
    assert "A" not in set(lists["clear"]["cell_id"])


def test_score_is_manually_recomputable():
    panel = _panel()
    train_buckets = pd.DatetimeIndex(panel["time_bucket"].unique())
    table = build_risk_table(_forecast(), panel, train_buckets)
    for row in table.itertuples(index=False):
        expected = (
            WEIGHTS["magnitude"] * row.mag_norm
            + WEIGHTS["demand"] * row.demand_norm
            + WEIGHTS["volatility"] * row.vol_norm
        )
        assert row.score == pytest.approx(expected, abs=1e-12)
        assert "net_outflow" in row.explanation
        assert "score=" in row.explanation


def test_ranking_is_sorted_by_score():
    panel = _panel()
    train_buckets = pd.DatetimeIndex(panel["time_bucket"].unique())
    lists = priority_lists(build_risk_table(_forecast(), panel, train_buckets))
    for frame in lists.values():
        assert frame["score"].is_monotonic_decreasing
        assert frame["rank"].tolist() == list(range(1, len(frame) + 1))


def test_priority_columns_include_required_contract():
    required = {"rank", "cell_id", "score", "pred_net_flow", "pred_out", "pred_in", "explanation"}
    assert required <= set(PRIORITY_COLUMNS)


def test_priority_lists_write_expected_csv_columns(tiny_config, tiny_layout):
    from bike_agent.risk.pipeline import run_risk

    panel = _panel()
    forecast = _forecast()
    cell_size = tiny_config["sources"]["xm"]["cell_size_m"]
    forecast.to_parquet(tiny_layout.forecast_path("xm"), index=False)
    panel.to_parquet(tiny_layout.panel_path("xm", cell_size), index=False)

    run_risk(tiny_config, tiny_layout, "xm")
    replenish = pd.read_csv(tiny_layout.priority_replenish_path("xm"))
    clear = pd.read_csv(tiny_layout.priority_clear_path("xm"))
    required = ["rank", "cell_id", "score", "pred_net_flow", "pred_out", "pred_in", "explanation"]
    for frame in (replenish, clear):
        assert frame.columns.tolist()[: len(required)] == required
        assert len(frame) > 0


def test_risk_bucket_skips_near_empty_tail():
    from bike_agent.risk.score import select_risk_bucket

    buckets = pd.date_range("2020-12-21 20:00", periods=5, freq="h")
    counts = [120, 110, 90, 4, 0]  # the last two are a near-empty tail
    panel = pd.DataFrame(
        [
            {"cell_id": c, "time_bucket": b, "out_count": n, "in_count": n, "net_flow": 0}
            for b, n in zip(buckets, counts)
            for c in ("A", "B")
        ]
    )
    forecast = pd.DataFrame(
        [{"cell_id": c, "time_bucket": b, "pred_out": 1.0, "pred_in": 1.0} for b in buckets for c in ("A", "B")]
    )
    assert select_risk_bucket(forecast, panel, 0.1) == buckets[2]
    # An explicit threshold of 0 keeps the last bucket.
    assert select_risk_bucket(forecast, panel, 0.0) == buckets[-1]


def test_selected_risk_bucket_has_observed_demand(tiny_config, tiny_layout):
    from bike_agent.risk.pipeline import run_risk

    buckets = pd.date_range("2020-12-21 06:00", periods=5, freq="h")
    counts = [(6, 2), (5, 3), (4, 4), (1, 1), (0, 0)]
    rows = []
    forecast_rows = []
    for bucket, (out, inn) in zip(buckets, counts):
        for cell in ("A", "B"):
            rows.append(
                {"cell_id": cell, "time_bucket": bucket, "out_count": out, "in_count": inn, "net_flow": out - inn}
            )
            forecast_rows.append(
                {"cell_id": cell, "time_bucket": bucket, "pred_out": float(out), "pred_in": float(inn)}
            )
    panel = pd.DataFrame(rows)
    forecast = pd.DataFrame(forecast_rows)
    cell_size = tiny_config["sources"]["xm"]["cell_size_m"]
    panel.to_parquet(tiny_layout.panel_path("xm", cell_size), index=False)
    forecast.to_parquet(tiny_layout.forecast_path("xm"), index=False)

    result = run_risk(tiny_config, tiny_layout, "xm")
    assert result["observed_demand_in_bucket"] > 0
    assert result["time_bucket"] != str(buckets[-1])
