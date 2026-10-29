"""Time-ordered train/test split (F3 / F3-4).

Random splits are forbidden: training always uses earlier buckets and testing
always uses later buckets.  The split is by distinct time bucket, so every cell
in a test bucket is predicted from strictly earlier data.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

import pandas as pd


@dataclass
class TimeSplit:
    train_buckets: pd.DatetimeIndex
    test_buckets: pd.DatetimeIndex
    train_mask: pd.Series
    test_mask: pd.Series

    def as_dict(self) -> dict[str, Any]:
        return {
            "n_train_buckets": int(len(self.train_buckets)),
            "n_test_buckets": int(len(self.test_buckets)),
            "train_start": str(self.train_buckets.min()),
            "train_end": str(self.train_buckets.max()),
            "test_start": str(self.test_buckets.min()),
            "test_end": str(self.test_buckets.max()),
        }


def time_split(panel: pd.DataFrame, train_fraction: float = 0.8) -> TimeSplit:
    if not 0.0 < train_fraction < 1.0:
        raise ValueError("train_fraction must be strictly between 0 and 1")
    buckets = pd.DatetimeIndex(sorted(panel["time_bucket"].unique()))
    if len(buckets) < 2:
        raise ValueError("need at least two time buckets to split train/test")
    n_train = int(len(buckets) * train_fraction)
    n_train = min(max(n_train, 1), len(buckets) - 1)
    train_buckets = buckets[:n_train]
    test_buckets = buckets[n_train:]
    train_mask = panel["time_bucket"].isin(train_buckets)
    test_mask = panel["time_bucket"].isin(test_buckets)
    return TimeSplit(
        train_buckets=train_buckets,
        test_buckets=test_buckets,
        train_mask=train_mask,
        test_mask=test_mask,
    )


def assert_time_ordered(panel: pd.DataFrame, split: TimeSplit) -> None:
    """Raise when the split violates train-before-test (used by tests)."""
    if len(split.train_buckets) == 0 or len(split.test_buckets) == 0:
        raise AssertionError("empty split side")
    if split.train_buckets.max() >= split.test_buckets.min():
        raise AssertionError("train buckets overlap or follow test buckets")
    train_rows = panel.loc[split.train_mask, "time_bucket"]
    test_rows = panel.loc[split.test_mask, "time_bucket"]
    if train_rows.max() >= test_rows.min():
        raise AssertionError("train rows are not strictly before test rows")
