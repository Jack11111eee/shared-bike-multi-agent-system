"""The normalised trip contract shared by every data source.

Every adapter converts its raw records into exactly these columns.  Downstream
stages (cleaning, aggregation, forecasting, simulation) never see
source-specific fields, so a new data source only requires a new adapter
(FR-DATA-2 / FR-DATA-3 / FR-DATA-6).
"""

from __future__ import annotations

from typing import Any

import numpy as np
import pandas as pd

CONTRACT_COLUMNS: tuple[str, ...] = (
    "trip_id",
    "bike_id",
    "start_time",
    "end_time",
    "start_lat",
    "start_lon",
    "end_lat",
    "end_lon",
    "duration_s",
)

NUMERIC_COLUMNS = (
    "start_lat",
    "start_lon",
    "end_lat",
    "end_lon",
    "duration_s",
)


class ContractError(ValueError):
    """Raised when a trip table violates the contract."""


def normalise_contract(frame: pd.DataFrame) -> pd.DataFrame:
    """Coerce an adapter frame to the canonical contract dtypes and order."""
    missing = [c for c in CONTRACT_COLUMNS if c not in frame.columns]
    if missing:
        raise ContractError(f"trip table missing contract columns: {missing}")

    out = frame.loc[:, list(CONTRACT_COLUMNS)].copy()
    out["trip_id"] = out["trip_id"].astype(str)
    out["bike_id"] = out["bike_id"].astype(str)
    for col in ("start_time", "end_time"):
        out[col] = pd.to_datetime(out[col], errors="coerce")
    for col in NUMERIC_COLUMNS:
        out[col] = pd.to_numeric(out[col], errors="coerce").astype("float64")
    return out.reset_index(drop=True)


def validate_contract(
    frame: pd.DataFrame,
    *,
    min_duration_s: float = 60.0,
    max_duration_s: float = 21600.0,
) -> dict[str, Any]:
    """Run the FR-DATA-4 integrity checks and return a machine-readable result."""
    if frame.empty:
        raise ContractError("trip table is empty")

    missing_columns = [c for c in CONTRACT_COLUMNS if c not in frame.columns]
    extra_columns = [c for c in frame.columns if c not in CONTRACT_COLUMNS]
    null_counts = {col: int(frame[col].isna().sum()) for col in CONTRACT_COLUMNS}

    start = pd.to_datetime(frame["start_time"], errors="coerce")
    end = pd.to_datetime(frame["end_time"], errors="coerce")
    invalid_time = int((start.isna() | end.isna()).sum())
    not_ordered = int((end <= start).sum()) + invalid_time

    lat_values = pd.concat([frame["start_lat"], frame["end_lat"]], ignore_index=True)
    lon_values = pd.concat([frame["start_lon"], frame["end_lon"]], ignore_index=True)
    lat_ok = bool(lat_values.dropna().between(-90, 90).all())
    lon_ok = bool(lon_values.dropna().between(-180, 180).all())

    duration = pd.to_numeric(frame["duration_s"], errors="coerce")
    abnormal = int(((duration < min_duration_s) | (duration > max_duration_s)).sum())
    duplicate_rows = int(frame.duplicated(subset=["trip_id"]).sum())

    total = int(len(frame))
    return {
        "rows": total,
        "columns_ok": not missing_columns,
        "missing_columns": missing_columns,
        "extra_columns": extra_columns,
        "null_counts": null_counts,
        "null_rate": {k: (v / total) for k, v in null_counts.items()},
        "time_parse_failures": invalid_time,
        "start_before_end_violations": not_ordered,
        "start_before_end_ok": not_ordered == 0,
        "time_min": start.min(),
        "time_max": end.max(),
        "bbox": {
            "lat_min": float(lat_values.min()),
            "lat_max": float(lat_values.max()),
            "lon_min": float(lon_values.min()),
            "lon_max": float(lon_values.max()),
        },
        "lat_range_ok": lat_ok,
        "lon_range_ok": lon_ok,
        "duplicate_trip_ids": duplicate_rows,
        "duplicate_rate": duplicate_rows / total,
        "abnormal_duration_rows": abnormal,
        "abnormal_duration_rate": abnormal / total,
        "duration_bounds_s": [min_duration_s, max_duration_s],
    }


def assert_contract(frame: pd.DataFrame, **kwargs: Any) -> dict[str, Any]:
    """Validate and raise :class:`ContractError` when a hard check fails."""
    report = validate_contract(frame, **kwargs)
    problems = []
    if not report["columns_ok"]:
        problems.append(f"missing columns {report['missing_columns']}")
    if not report["start_before_end_ok"]:
        problems.append(f"{report['start_before_end_violations']} rows with start_time >= end_time")
    if not report["lat_range_ok"] or not report["lon_range_ok"]:
        problems.append("coordinates outside plausible lat/lon ranges")
    if report["time_parse_failures"]:
        problems.append(f"{report['time_parse_failures']} unparseable timestamps")
    if report["null_counts"]["trip_id"] or report["null_counts"]["bike_id"]:
        problems.append("null trip_id or bike_id")
    if problems:
        raise ContractError("contract validation failed: " + "; ".join(problems))
    return report
