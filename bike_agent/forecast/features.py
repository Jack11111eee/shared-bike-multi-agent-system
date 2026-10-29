"""Feature construction for the demand model (F3).

All statistics that could leak future information are computed on the training
buckets only.  Lag and rolling features use observed values from earlier
buckets (one-step-ahead evaluation); the first bucket of each cell has no
history and is filled with zeros.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

import pandas as pd

from bike_agent.forecast.split import TimeSplit

TARGETS = ("out_count", "in_count")


@dataclass
class FeatureSet:
    frame: pd.DataFrame
    feature_columns: list[str]
    train_mask: pd.Series
    test_mask: pd.Series
    train_stats: dict[str, Any]


def build_features(panel: pd.DataFrame, split: TimeSplit) -> FeatureSet:
    df = panel.sort_values(["cell_id", "time_bucket"], kind="mergesort").reset_index(drop=True)
    train_mask = df["time_bucket"].isin(split.train_buckets)
    test_mask = df["time_bucket"].isin(split.test_buckets)

    df["hour"] = df["time_bucket"].dt.hour.astype("int16")
    df["dow"] = df["time_bucket"].dt.dayofweek.astype("int8")
    df["is_weekend"] = (df["dow"] >= 5).astype("int8")

    grouped = df.groupby("cell_id", observed=True)
    for target in TARGETS:
        df[f"lag1_{target}"] = grouped[target].shift(1)
        df[f"roll3_{target}"] = grouped[target].transform(
            lambda s: s.shift(1).rolling(3, min_periods=1).mean()
        )
    df["gap_hours"] = grouped["time_bucket"].diff().dt.total_seconds().div(3600.0).fillna(0.0)

    train = df.loc[train_mask]
    cell_mean = train.groupby("cell_id", observed=True)[list(TARGETS)].mean()
    cell_hour_mean = train.groupby(["cell_id", "hour"], observed=True)[list(TARGETS)].mean()

    for target in TARGETS:
        df[f"cell_mean_{target}"] = df["cell_id"].map(cell_mean[target]).astype("float64")
        keys = pd.MultiIndex.from_arrays([df["cell_id"], df["hour"]])
        df[f"cell_hour_mean_{target}"] = cell_hour_mean[target].reindex(keys).to_numpy(dtype=float)

    lag_columns = [f"lag1_{t}" for t in TARGETS] + [f"roll3_{t}" for t in TARGETS]
    df[lag_columns] = df[lag_columns].fillna(0.0)
    mean_columns = [f"cell_mean_{t}" for t in TARGETS] + [f"cell_hour_mean_{t}" for t in TARGETS]
    df[mean_columns] = df[mean_columns].fillna(0.0)

    feature_columns = [
        "cell_id",
        "hour",
        "dow",
        "is_weekend",
        "gap_hours",
        *[f"lag1_{t}" for t in TARGETS],
        *[f"roll3_{t}" for t in TARGETS],
        *[f"cell_mean_{t}" for t in TARGETS],
        *[f"cell_hour_mean_{t}" for t in TARGETS],
    ]
    stats = {
        "n_train_rows": int(train_mask.sum()),
        "n_test_rows": int(test_mask.sum()),
        "global_train_mean": {t: float(train[t].mean()) for t in TARGETS},
    }
    return FeatureSet(
        frame=df,
        feature_columns=feature_columns,
        train_mask=train_mask,
        test_mask=test_mask,
        train_stats=stats,
    )


def design_matrix(frame: pd.DataFrame, feature_columns: list[str]) -> pd.DataFrame:
    matrix = frame.loc[:, feature_columns].copy()
    matrix["cell_id"] = matrix["cell_id"].astype("category")
    for column in feature_columns:
        if column != "cell_id":
            matrix[column] = matrix[column].astype("float64")
    return matrix
