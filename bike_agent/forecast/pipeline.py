"""Forecast pipeline: baseline + LightGBM on a time-ordered split."""

from __future__ import annotations

from typing import Any

import numpy as np
import pandas as pd

from bike_agent.config import Layout, source_config
from bike_agent.forecast.baseline import seasonal_mean_predict
from bike_agent.forecast.evaluate import error_metrics
from bike_agent.stats import paired_bootstrap
from bike_agent.forecast.features import TARGETS, build_features, design_matrix
from bike_agent.forecast.model import predict_model, train_model
from bike_agent.forecast.split import assert_time_ordered, time_split
from bike_agent.prep.pipeline import run_prep
from bike_agent.utils import write_json


def run_forecast(
    config: dict[str, Any],
    layout: Layout,
    source: str,
    cell_size: int | None = None,
    panel: pd.DataFrame | None = None,
    write: bool = True,
) -> dict[str, Any]:
    src_cfg = source_config(config, source)
    cell_size = int(cell_size or src_cfg["cell_size_m"])
    if panel is None:
        panel = run_prep(config, layout, source, cell_size=cell_size, write=False)["panel_frame"]

    forecast_cfg = config.get("forecast", {})
    train_fraction = float(forecast_cfg.get("train_fraction", 0.8))
    seed = int(config.get("seed", 42))
    params = forecast_cfg.get("model", {})

    split = time_split(panel, train_fraction)
    assert_time_ordered(panel, split)
    feature_set = build_features(panel, split)
    frame = feature_set.frame
    train = frame.loc[feature_set.train_mask]
    test = frame.loc[feature_set.test_mask]
    if train.empty or test.empty:
        raise ValueError("forecast split produced an empty train or test set")

    x_train = design_matrix(train, feature_set.feature_columns)
    x_test = design_matrix(test, feature_set.feature_columns)

    predictions = test.loc[:, ["cell_id", "time_bucket", "out_count", "in_count"]].copy()
    predictions = predictions.rename(columns={"out_count": "actual_out", "in_count": "actual_in"})
    metrics: dict[str, Any] = {"baseline": {}, "model": {}}
    significance: dict[str, Any] = {}

    for target in TARGETS:
        baseline = seasonal_mean_predict(train, test, target)
        model = train_model(x_train, train[target].astype(float), params, seed)
        predicted = predict_model(model, x_test)
        suffix = target.replace("_count", "")
        predictions[f"baseline_{suffix}"] = baseline
        predictions[f"pred_{suffix}"] = predicted
        actual = test[target].to_numpy()
        metrics["baseline"][target] = error_metrics(actual, baseline)
        metrics["model"][target] = error_metrics(actual, predicted)
        # Paired test on per-row absolute error: is the MAE gap real or noise?
        significance[target] = paired_bootstrap(
            np.abs(predicted - actual) - np.abs(baseline - actual)
        )

    predictions["pred_net_flow"] = predictions["pred_out"] - predictions["pred_in"]
    predictions["baseline_net_flow"] = predictions["baseline_out"] - predictions["baseline_in"]
    predictions["actual_net_flow"] = predictions["actual_out"] - predictions["actual_in"]
    predictions["source"] = source
    predictions["cell_size_m"] = cell_size
    predictions["model"] = "lightgbm_poisson"
    predictions = predictions.sort_values(["time_bucket", "cell_id"], kind="mergesort").reset_index(drop=True)

    result = {
        "source": source,
        "cell_size_m": cell_size,
        "split": split.as_dict(),
        "seed": seed,
        "train_fraction": train_fraction,
        "params": params,
        "features": feature_set.feature_columns,
        "metrics": metrics,
        "significance": significance,
        "predictions": predictions,
    }

    if write:
        layout.ensure()
        predictions.to_parquet(layout.forecast_path(source), index=False)
        if source == config.get("primary_source") and cell_size == int(src_cfg["cell_size_m"]):
            predictions.to_parquet(layout.outputs / "forecast.parquet", index=False)
        write_json(
            layout.forecast_meta_path(source),
            {
                "source": source,
                "cell_size_m": cell_size,
                "split": result["split"],
                "seed": seed,
                "train_fraction": train_fraction,
                "params": params,
                "features": feature_set.feature_columns,
                "metrics": metrics,
                "significance": significance,
                "n_train_rows": int(feature_set.train_stats["n_train_rows"]),
                "n_test_rows": int(feature_set.train_stats["n_test_rows"]),
            },
        )
        write_json(layout.outputs / f"forecast_metrics.{source}.json", metrics)
    return result


def run_baseline_sensitivity(
    config: dict[str, Any],
    layout: Layout,
    source: str,
    cell_sizes: list[int],
    clean_frame: pd.DataFrame | None = None,
) -> list[dict[str, Any]]:
    """Baseline-only error comparison across grid sizes (F2-3 evidence)."""
    from bike_agent.prep.aggregate import build_panel
    from bike_agent.prep.grid import build_grid

    if clean_frame is None:
        clean_frame = pd.read_parquet(layout.clean_path(source))
    train_fraction = float(config.get("forecast", {}).get("train_fraction", 0.8))
    results = []
    for size in cell_sizes:
        grid = build_grid(clean_frame, int(size))
        panel, meta = build_panel(clean_frame, grid)
        split = time_split(panel, train_fraction)
        feature_set = build_features(panel, split)
        frame = feature_set.frame
        train = frame.loc[feature_set.train_mask]
        test = frame.loc[feature_set.test_mask]
        entry: dict[str, Any] = {
            "cell_size_m": int(size),
            "n_cells": meta["n_cells"],
            "n_buckets": meta["n_buckets"],
            "zero_out_share": meta["zero_out_share"],
            "metrics": {},
        }
        for target in TARGETS:
            predicted = seasonal_mean_predict(train, test, target)
            entry["metrics"][target] = error_metrics(test[target].to_numpy(), predicted)
        results.append(entry)
    write_json(layout.outputs / f"sensitivity_baseline.{source}.json", results)
    return results
