"""F2-2 / F2-5: complete panel, long-table schema and conservation."""

from __future__ import annotations

import pandas as pd

from bike_agent.prep.aggregate import build_panel
from bike_agent.prep.grid import build_grid


def _trips() -> pd.DataFrame:
    rows = []
    specs = [
        ("a", "2020-12-21 06:05", "2020-12-21 06:20", 24.5000, 118.1000, 24.5010, 118.1010),
        ("b", "2020-12-21 06:15", "2020-12-21 06:25", 24.5000, 118.1000, 24.5020, 118.1020),
        ("c", "2020-12-21 07:05", "2020-12-21 07:20", 24.5010, 118.1010, 24.5000, 118.1000),
        ("d", "2020-12-21 07:30", "2020-12-21 07:45", 24.5020, 118.1020, 24.5005, 118.1005),
    ]
    for trip_id, start, end, lat, lon, end_lat, end_lon in specs:
        rows.append(
            {
                "trip_id": trip_id,
                "bike_id": "b1",
                "start_time": pd.Timestamp(start),
                "end_time": pd.Timestamp(end),
                "start_lat": lat,
                "start_lon": lon,
                "end_lat": end_lat,
                "end_lon": end_lon,
                "duration_s": 900.0,
            }
        )
    return pd.DataFrame(rows)


def test_panel_has_long_table_schema_and_complete_grid():
    trips = _trips()
    grid = build_grid(trips, 200)
    panel, meta = build_panel(trips, grid)

    assert list(panel.columns) == ["cell_id", "time_bucket", "out_count", "in_count", "net_flow"]
    assert len(panel) == meta["n_cells"] * meta["n_buckets"]
    assert panel["time_bucket"].nunique() == meta["n_buckets"] == 2
    assert (panel["net_flow"] == panel["out_count"] - panel["in_count"]).all()
    assert panel["out_count"].dtype == "int64"
    assert panel["in_count"].dtype == "int64"


def test_panel_conserves_departures_and_returns():
    trips = _trips()
    grid = build_grid(trips, 200)
    panel, meta = build_panel(trips, grid)

    assert panel["out_count"].sum() == len(trips)
    assert panel["in_count"].sum() == len(trips)
    assert meta["out_conserved"] is True
    assert meta["in_conserved"] is True


def test_panel_reports_zero_share():
    trips = _trips()
    grid = build_grid(trips, 200)
    panel, meta = build_panel(trips, grid)

    assert 0.0 <= meta["zero_out_share"] <= 1.0
    assert meta["zero_out_share"] == float((panel["out_count"] == 0).mean())
    assert meta["mean_out_per_cell_bucket"] == float(panel["out_count"].mean())


def test_grid_assignment_is_deterministic():
    trips = _trips()
    grid = build_grid(trips, 200)
    first = grid.assign(trips["start_lat"], trips["start_lon"])
    second = grid.assign(trips["start_lat"], trips["start_lon"])
    assert first.tolist() == second.tolist()
    assert first.isin(grid.cells["cell_id"]).all()
