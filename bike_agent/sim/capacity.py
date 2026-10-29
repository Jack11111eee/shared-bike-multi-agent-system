"""Station capacity assumptions (D6).

DS-XM ships real electronic fences, so capacity is derived from fence density
(``mode: fences``).  DS-SH has no fences, so it falls back to a configured
fixed capacity (``mode: fixed``); the degradation is reported, never silent
(FR-DATA-6).
"""

from __future__ import annotations

from typing import Any

import numpy as np
import pandas as pd

from bike_agent.prep.grid import Grid


def cell_capacities(
    cells: pd.DataFrame,
    capacity_cfg: dict[str, Any],
    fences: pd.DataFrame | None = None,
    grid: Grid | None = None,
) -> pd.Series:
    mode = capacity_cfg.get("mode", "fixed")
    cell_ids = cells["cell_id"].tolist()
    if mode == "fences":
        if fences is None or grid is None or fences.empty:
            raise ValueError("fence-derived capacity requires fence data and a grid")
        assigned = grid.assign(fences["centroid_lat"], fences["centroid_lon"])
        counts = assigned.value_counts()
        slots = float(capacity_cfg.get("slots_per_fence", 2))
        minimum = float(capacity_cfg.get("min_capacity", 10))
        maximum = float(capacity_cfg.get("max_capacity", 200))
        raw = pd.Series({cid: counts.get(cid, 0) * slots for cid in cell_ids}, dtype=float)
        capacity = raw.clip(lower=minimum, upper=maximum).round().astype(int)
        return capacity
    if mode == "fixed":
        fixed = int(capacity_cfg.get("fixed_capacity", 40))
        return pd.Series(fixed, index=cell_ids, dtype=int)
    raise ValueError(f"unknown capacity mode: {mode!r}")


def initial_inventory(capacity: pd.Series, fill_ratio: float) -> np.ndarray:
    return np.round(capacity.to_numpy(dtype=float) * float(fill_ratio)).astype(int)
