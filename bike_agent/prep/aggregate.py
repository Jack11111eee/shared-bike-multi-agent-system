"""Spatio-temporal aggregation to (cell, time bucket) counts (F2-2).

The panel is complete *for every observed time bucket*: each grid cell appears
once per bucket that actually contains a departure or a return, with explicit
zeros.  Buckets are taken from the observed data rather than from a
``dates x hours`` cross product, because a source whose data ends mid-night
would otherwise gain a full day of purely synthetic buckets (which downstream
forecasting and risk ranking would then treat as real time units).
"""

from __future__ import annotations

from typing import Any

import pandas as pd

from bike_agent.prep.grid import Grid


def build_panel(trips: pd.DataFrame, grid: Grid) -> tuple[pd.DataFrame, dict[str, Any]]:
    if trips.empty:
        raise ValueError("cannot aggregate an empty trip table")

    work = trips.copy()
    work["start_cell"] = grid.assign(work["start_lat"], work["start_lon"]).to_numpy()
    work["end_cell"] = grid.assign(work["end_lat"], work["end_lon"]).to_numpy()
    work["start_bucket"] = work["start_time"].dt.floor("h")
    work["end_bucket"] = work["end_time"].dt.floor("h")

    buckets = pd.DatetimeIndex(
        sorted(set(work["start_bucket"].unique()) | set(work["end_bucket"].unique()))
    )
    dates = pd.Index(sorted({ts.normalize() for ts in buckets}))
    hours = sorted({int(ts.hour) for ts in buckets})

    out_counts = (
        work.groupby(["start_cell", "start_bucket"], observed=True)
        .size()
        .rename("out_count")
        .reset_index()
        .rename(columns={"start_cell": "cell_id", "start_bucket": "time_bucket"})
    )
    in_counts = (
        work.groupby(["end_cell", "end_bucket"], observed=True)
        .size()
        .rename("in_count")
        .reset_index()
        .rename(columns={"end_cell": "cell_id", "end_bucket": "time_bucket"})
    )

    cell_ids = sorted(grid.cells["cell_id"].tolist())
    index = pd.MultiIndex.from_product(
        [buckets, cell_ids], names=["time_bucket", "cell_id"]
    )
    panel = pd.DataFrame(index=index).reset_index()
    panel = panel.merge(out_counts, on=["cell_id", "time_bucket"], how="left")
    panel = panel.merge(in_counts, on=["cell_id", "time_bucket"], how="left")
    panel["out_count"] = panel["out_count"].fillna(0).astype("int64")
    panel["in_count"] = panel["in_count"].fillna(0).astype("int64")
    panel["net_flow"] = panel["out_count"] - panel["in_count"]
    panel = panel.loc[:, ["cell_id", "time_bucket", "out_count", "in_count", "net_flow"]]
    panel = panel.sort_values(["time_bucket", "cell_id"], kind="mergesort").reset_index(drop=True)

    total_out = int(panel["out_count"].sum())
    total_in = int(panel["in_count"].sum())
    meta = {
        "n_cells": len(cell_ids),
        "n_buckets": int(len(buckets)),
        "n_dates": int(len(dates)),
        "hours": hours,
        "first_bucket": str(buckets.min()),
        "last_bucket": str(buckets.max()),
        "total_out": total_out,
        "total_in": total_in,
        "total_trips": int(len(work)),
        "out_conserved": total_out == len(work),
        "in_conserved": total_in == len(work),
        "zero_out_share": float((panel["out_count"] == 0).mean()),
        "zero_in_share": float((panel["in_count"] == 0).mean()),
        "zero_any_share": float(((panel["out_count"] == 0) & (panel["in_count"] == 0)).mean()),
        "mean_out_per_cell_bucket": float(panel["out_count"].mean()),
        "grid": grid.as_dict(),
    }
    return panel, meta
