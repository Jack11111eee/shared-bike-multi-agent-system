"""Paired significance test for "is this difference real?" claims.

The reports compare two predictors (or two dispatch strategies) on the *same*
rows, so the comparison is paired: only the difference between the two errors
matters.  A percentile bootstrap on the mean paired difference answers "could
this difference be zero?" without assuming normality and without adding a
SciPy dependency.

Deterministic by construction: a fixed seed and a fixed resample count mean the
same inputs always produce the same interval (NF-1).
"""

from __future__ import annotations

from typing import Any

import numpy as np

N_BOOTSTRAP = 2000
BOOTSTRAP_SEED = 42
ALPHA = 0.05


def paired_bootstrap(
    differences: Any,
    n_boot: int = N_BOOTSTRAP,
    seed: int = BOOTSTRAP_SEED,
    alpha: float = ALPHA,
) -> dict[str, Any]:
    """Mean paired difference with a percentile bootstrap confidence interval.

    ``differences`` is elementwise ``candidate - reference``.  ``significant``
    is True when the whole interval sits on one side of zero.
    """
    diff = np.asarray(differences, dtype=float)
    diff = diff[np.isfinite(diff)]
    n = int(diff.size)
    if n < 2:
        return {
            "n": n,
            "n_bootstrap": 0,
            "alpha": alpha,
            "mean_diff": float(diff.mean()) if n else 0.0,
            "ci_low": None,
            "ci_high": None,
            "significant": False,
            "reason": "fewer than 2 paired observations",
        }

    rng = np.random.default_rng(seed)
    means = np.empty(n_boot, dtype=float)
    for index in range(n_boot):
        means[index] = diff[rng.integers(0, n, n)].mean()

    low = float(np.percentile(means, 100 * alpha / 2))
    high = float(np.percentile(means, 100 * (1 - alpha / 2)))
    return {
        "n": n,
        "n_bootstrap": int(n_boot),
        "seed": int(seed),
        "alpha": alpha,
        "mean_diff": float(diff.mean()),
        "ci_low": low,
        "ci_high": high,
        "significant": bool(low > 0 or high < 0),
    }


def describe(stats: dict[str, Any] | None, percent: bool = False, digits: int = 4) -> str:
    """Human-readable interval, e.g. ``95% CI [-0.1510, +0.0030]，n=149``."""
    if not stats:
        return ""
    if stats.get("ci_low") is None:
        return f"n={stats.get('n', 0)}，样本量不足，无法估计区间"
    low, high = stats["ci_low"], stats["ci_high"]
    unit = ""
    if percent:
        low, high = low * 100, high * 100
        unit = "%"
    return f"95% CI [{low:+.{digits}f}{unit}, {high:+.{digits}f}{unit}]，n={stats['n']}"


def is_significant(stats: dict[str, Any] | None) -> bool | None:
    """True/False when the test ran, None when there is no result at all.

    A result that exists but could not be computed (fewer than two pairs) is
    reported as False: the data does not support a difference claim.
    """
    if not stats:
        return None
    return bool(stats.get("significant"))
