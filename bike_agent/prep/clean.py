"""Cleaning rules for the trip contract (F2-1).

Each rule is applied in a fixed order and reports how many rows it removed and
why, so the pipeline can answer "what was filtered, and how much".
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

import pandas as pd

from bike_agent.utils import haversine_m

COORD_COLUMNS = ("start_lat", "start_lon", "end_lat", "end_lon")


@dataclass
class CleanResult:
    frame: pd.DataFrame
    rules: list[dict[str, Any]] = field(default_factory=list)
    rows_in: int = 0
    rows_out: int = 0
    bbox: dict[str, float] = field(default_factory=dict)

    def as_dict(self) -> dict[str, Any]:
        return {
            "rows_in": self.rows_in,
            "rows_out": self.rows_out,
            "rows_removed": self.rows_in - self.rows_out,
            "bbox": self.bbox,
            "rules": self.rules,
        }


def _bbox(frame: pd.DataFrame, quantiles: tuple[float, float]) -> dict[str, float]:
    lat = pd.concat([frame["start_lat"], frame["end_lat"]], ignore_index=True)
    lon = pd.concat([frame["start_lon"], frame["end_lon"]], ignore_index=True)
    low, high = quantiles
    return {
        "lat_min": float(lat.quantile(low)),
        "lat_max": float(lat.quantile(high)),
        "lon_min": float(lon.quantile(low)),
        "lon_max": float(lon.quantile(high)),
    }


def clean_trips(
    frame: pd.DataFrame,
    *,
    min_duration_s: float = 60.0,
    max_duration_s: float = 21600.0,
    pseudo_trip_max_distance_m: float = 50.0,
    bbox_quantiles: tuple[float, float] = (0.005, 0.995),
) -> CleanResult:
    """Apply the five cleaning rules and return the surviving trips."""
    work = frame.copy()
    rows_in = len(work)
    rules: list[dict[str, Any]] = []

    def apply_rule(name: str, mask_remove: pd.Series, reason: str) -> None:
        nonlocal work
        removed = int(mask_remove.sum())
        rules.append(
            {
                "rule": name,
                "rows_removed": removed,
                "rows_remaining": int(len(work) - removed),
                "reason": reason,
            }
        )
        work = work.loc[~mask_remove].reset_index(drop=True)

    # 1. duplicate trip_id (keep first occurrence)
    apply_rule(
        "duplicate_trip_id",
        work.duplicated(subset=["trip_id"], keep="first"),
        "same trip_id appears more than once; first occurrence kept",
    )

    # 2. missing coordinates
    missing_coords = work.loc[:, list(COORD_COLUMNS)].isna().any(axis=1)
    apply_rule(
        "missing_coordinates",
        missing_coords,
        "at least one of start/end latitude or longitude is missing",
    )

    # 3. abnormal duration
    duration = pd.to_numeric(work["duration_s"], errors="coerce")
    abnormal = duration.isna() | (duration < min_duration_s) | (duration > max_duration_s)
    apply_rule(
        "abnormal_duration",
        abnormal,
        f"duration outside [{min_duration_s:.0f}s, {max_duration_s:.0f}s]",
    )

    # 4. out-of-bounds coordinates (robust bounding box)
    bbox = _bbox(work, bbox_quantiles)
    inside = (
        work["start_lat"].between(bbox["lat_min"], bbox["lat_max"])
        & work["start_lon"].between(bbox["lon_min"], bbox["lon_max"])
        & work["end_lat"].between(bbox["lat_min"], bbox["lat_max"])
        & work["end_lon"].between(bbox["lon_min"], bbox["lon_max"])
    )
    apply_rule(
        "out_of_bounds_coordinates",
        ~inside,
        f"start or end outside the {bbox_quantiles[0]:.1%}-{bbox_quantiles[1]:.1%} coordinate envelope",
    )

    # 5. pseudo trips: start and end are the same place
    distance = haversine_m(
        work["start_lat"].to_numpy(),
        work["start_lon"].to_numpy(),
        work["end_lat"].to_numpy(),
        work["end_lon"].to_numpy(),
    )
    same_rounded = (
        (work["start_lat"].round(6) == work["end_lat"].round(6))
        & (work["start_lon"].round(6) == work["end_lon"].round(6))
    )
    pseudo = pd.Series(distance < pseudo_trip_max_distance_m, index=work.index) | same_rounded
    apply_rule(
        "pseudo_trip_same_origin_destination",
        pseudo,
        f"start and end within {pseudo_trip_max_distance_m:.0f} m (or identical coordinates)",
    )

    return CleanResult(
        frame=work.reset_index(drop=True),
        rules=rules,
        rows_in=rows_in,
        rows_out=len(work),
        bbox=bbox,
    )
