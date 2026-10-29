"""Forecast error metrics."""

from __future__ import annotations

import numpy as np


def mae(actual: np.ndarray, predicted: np.ndarray) -> float:
    actual = np.asarray(actual, dtype=float)
    predicted = np.asarray(predicted, dtype=float)
    return float(np.mean(np.abs(actual - predicted)))


def rmse(actual: np.ndarray, predicted: np.ndarray) -> float:
    actual = np.asarray(actual, dtype=float)
    predicted = np.asarray(predicted, dtype=float)
    return float(np.sqrt(np.mean((actual - predicted) ** 2)))


def wape(actual: np.ndarray, predicted: np.ndarray) -> float:
    """Weighted absolute percentage error: sum|err| / sum|actual|.

    Unlike MAE, WAPE is comparable across grid sizes because it normalises by
    the total volume.
    """
    actual = np.asarray(actual, dtype=float)
    predicted = np.asarray(predicted, dtype=float)
    denominator = float(np.abs(actual).sum())
    if denominator == 0:
        return 0.0
    return float(np.abs(actual - predicted).sum() / denominator)


def error_metrics(actual: np.ndarray, predicted: np.ndarray) -> dict[str, float]:
    return {"mae": mae(actual, predicted), "rmse": rmse(actual, predicted), "wape": wape(actual, predicted)}
