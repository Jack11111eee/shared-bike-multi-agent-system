"""Seasonal-mean baseline (F3).

The baseline predicts each cell's historical mean for the same hour of day,
falling back to the cell mean and then the global mean when a cell has no
history.  It is deliberately simple and is always evaluated on the same
time-ordered test split as the model.
"""

from __future__ import annotations

import numpy as np
import pandas as pd


def seasonal_mean_predict(
    train: pd.DataFrame,
    test: pd.DataFrame,
    target: str,
) -> np.ndarray:
    """Predict ``target`` for ``test`` rows using train-only statistics."""
    work_train = train.loc[:, ["cell_id", "time_bucket", target]].copy()
    work_train["hour"] = work_train["time_bucket"].dt.hour

    by_cell_hour = work_train.groupby(["cell_id", "hour"], observed=True)[target].mean()
    by_cell = work_train.groupby("cell_id", observed=True)[target].mean()
    global_mean = float(work_train[target].mean())

    test_hour = test["time_bucket"].dt.hour
    keys = pd.MultiIndex.from_arrays([test["cell_id"], test_hour])
    predicted = by_cell_hour.reindex(keys).to_numpy(dtype=float)
    cell_fallback = by_cell.reindex(test["cell_id"]).to_numpy(dtype=float)
    predicted = np.where(np.isnan(predicted), cell_fallback, predicted)
    predicted = np.where(np.isnan(predicted), global_mean, predicted)
    return np.clip(predicted, 0.0, None)
