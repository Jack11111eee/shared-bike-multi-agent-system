"""LightGBM demand model (F3).

Counts are modelled with a Poisson objective.  The model is deterministic for a
fixed seed: ``deterministic=True``, ``force_row_wise=True`` and
``n_jobs=1`` (NF-1 / F3-4).
"""

from __future__ import annotations

from typing import Any

import numpy as np
import pandas as pd

try:  # lightgbm is a hard dependency, but the import error is clearer here
    import lightgbm as lgb
except ImportError as exc:  # pragma: no cover
    raise ImportError("lightgbm is required for the forecast model") from exc


def train_model(
    features: pd.DataFrame,
    target: pd.Series,
    params: dict[str, Any],
    seed: int,
) -> lgb.LGBMRegressor:
    model = lgb.LGBMRegressor(
        objective=params.get("objective", "poisson"),
        n_estimators=int(params.get("n_estimators", 300)),
        learning_rate=float(params.get("learning_rate", 0.05)),
        num_leaves=int(params.get("num_leaves", 31)),
        min_child_samples=int(params.get("min_child_samples", 20)),
        colsample_bytree=float(params.get("feature_fraction", 0.9)),
        reg_lambda=float(params.get("l2_regularization", 1.0)),
        random_state=seed,
        n_jobs=int(params.get("n_jobs", 1)),
        deterministic=True,
        force_row_wise=True,
        verbose=-1,
    )
    model.fit(features, target, categorical_feature=["cell_id"])
    return model


def predict_model(model: lgb.LGBMRegressor, features: pd.DataFrame) -> np.ndarray:
    return np.clip(np.asarray(model.predict(features), dtype=float), 0.0, None)
