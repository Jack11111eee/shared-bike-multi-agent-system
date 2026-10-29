"""F3: time-ordered split, baseline correctness, model output and reproducibility."""

from __future__ import annotations

import numpy as np
import pandas as pd

from bike_agent.forecast.baseline import seasonal_mean_predict
from bike_agent.forecast.pipeline import run_forecast
from bike_agent.forecast.split import assert_time_ordered, time_split


def _panel(n_buckets: int = 8, cells: list[str] | None = None) -> pd.DataFrame:
    cells = cells or ["A", "B", "C", "D"]
    buckets = pd.date_range("2020-12-21 06:00", periods=n_buckets, freq="h")
    rows = []
    for index, bucket in enumerate(buckets):
        for cell_index, cell in enumerate(cells):
            out = (index + cell_index) % 5
            inn = (index * 2 + cell_index) % 4
            rows.append(
                {
                    "cell_id": cell,
                    "time_bucket": bucket,
                    "out_count": out,
                    "in_count": inn,
                    "net_flow": out - inn,
                }
            )
    return pd.DataFrame(rows)


def test_time_split_is_strictly_ordered_and_partitions_rows():
    panel = _panel(10)
    split = time_split(panel, 0.8)
    assert len(split.train_buckets) == 8
    assert len(split.test_buckets) == 2
    assert split.train_buckets.max() < split.test_buckets.min()
    assert int(split.train_mask.sum()) + int(split.test_mask.sum()) == len(panel)
    assert not (split.train_mask & split.test_mask).any()
    assert_time_ordered(panel, split)


def test_baseline_uses_only_training_statistics():
    buckets = pd.date_range("2020-12-21 06:00", periods=4, freq="h")
    panel = pd.DataFrame(
        [
            {"cell_id": "A", "time_bucket": b, "out_count": (2 if i < 3 else 100), "in_count": 0, "net_flow": 0}
            for i, b in enumerate(buckets)
        ]
    )
    split = time_split(panel, 0.75)
    train = panel.loc[split.train_mask]
    test = panel.loc[split.test_mask]
    predicted = seasonal_mean_predict(train, test, "out_count")
    # The test bucket's hour (09:00) never appears in training, so the cell
    # mean (2) is used - the 100 in the test row must not leak.
    assert np.allclose(predicted, 2.0)


def test_run_forecast_produces_stable_output_and_metrics(tiny_config):
    panel = _panel(8)
    result = run_forecast(tiny_config, layout=None, source="xm", panel=panel, write=False)

    predictions = result["predictions"]
    for column in ("cell_id", "time_bucket", "pred_out", "pred_in", "pred_net_flow"):
        assert column in predictions.columns
    assert len(predictions) == 4 * 2  # 4 cells x 2 test buckets
    assert predictions["pred_out"].min() >= 0
    assert predictions["pred_in"].min() >= 0
    assert np.allclose(predictions["pred_net_flow"], predictions["pred_out"] - predictions["pred_in"])

    for kind in ("baseline", "model"):
        for target in ("out_count", "in_count"):
            assert {"mae", "rmse", "wape"} <= set(result["metrics"][kind][target])
    assert result["split"]["train_end"] < result["split"]["test_start"]


def test_forecast_is_reproducible(tiny_config):
    panel = _panel(10)
    first = run_forecast(tiny_config, layout=None, source="xm", panel=panel, write=False)
    second = run_forecast(tiny_config, layout=None, source="xm", panel=panel, write=False)
    assert first["metrics"] == second["metrics"]
    pd.testing.assert_frame_equal(first["predictions"], second["predictions"])
