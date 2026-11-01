"""Shared fixtures: tiny synthetic datasets in the real raw formats."""

from __future__ import annotations

from datetime import datetime, timedelta
from pathlib import Path

import pandas as pd
import pytest

from bike_agent.config import Layout, load_config


@pytest.fixture
def tiny_config() -> dict:
    config = load_config()
    config["sources"]["xm"]["cell_size_m"] = 200
    config["sources"]["xm"]["sensitivity_cell_sizes_m"] = [200, 400]
    config["sources"]["sh"]["cell_size_m"] = 500
    config["sources"]["sh"]["capacity"]["mode"] = "fixed"
    config["forecast"]["model"].update({"n_estimators": 5, "num_leaves": 4, "min_child_samples": 2})
    config["simulation"].update({"max_moves_per_bucket": 5, "max_candidate_surplus_per_deficit": 5})
    return config


@pytest.fixture
def tiny_layout(tmp_path: Path) -> Layout:
    layout = Layout(tmp_path)
    layout.ensure()
    return layout


def write_xm_fixture(layout: Layout, n_bikes: int = 3, trips_per_bike: int = 2) -> None:
    """Write a minimal DS-XM raw tree in the real (Excel-in-.csv) format."""
    extract = layout.raw / "xm" / "extracted"
    (extract / "Order data").mkdir(parents=True, exist_ok=True)
    (extract / "Electronic fence data").mkdir(parents=True, exist_ok=True)
    (extract / "Weather data").mkdir(parents=True, exist_ok=True)

    rows = []
    for bike in range(n_bikes):
        for trip in range(trips_per_bike):
            start = datetime(2020, 12, 21, 6, 0) + timedelta(minutes=40 * trip)
            end = start + timedelta(minutes=15)
            rows.append(
                {
                    "BICYCLE_ID": f"B{bike}" if trip == 0 else None,
                    "LATITUDE": 24.5000 + bike * 0.001,
                    "LONGITUDE": 118.1000,
                    "LOCK_STATUS": 0,
                    "UPDATE_TIME1": start.date(),
                    "UPDATE_TIME2": start.strftime("%H:%M:%S"),
                    "ORDER DURATION": None,
                }
            )
            rows.append(
                {
                    "BICYCLE_ID": None,
                    "LATITUDE": 24.5000 + bike * 0.001 + 0.001,
                    "LONGITUDE": 118.1010,
                    "LOCK_STATUS": 1,
                    "UPDATE_TIME1": end.date(),
                    "UPDATE_TIME2": end.strftime("%H:%M:%S"),
                    "ORDER DURATION": 900.0,
                }
            )
    pd.DataFrame(rows).to_excel(extract / "Order data" / "order data.csv", index=False)

    fences = pd.DataFrame(
        {
            "FENCE_ID": ["F1", "F2"],
            "FENCE_LOC": [
                "[118.1000,24.5000],[118.1010,24.5000],[118.1010,24.5010],[118.1000,24.5010],[118.1000,24.5000]",
                "[118.1015,24.5005],[118.1020,24.5005],[118.1020,24.5010],[118.1015,24.5010],[118.1015,24.5005]",
            ],
        }
    )
    fences.to_excel(extract / "Electronic fence data" / "STATION.csv", index=False)
    pd.DataFrame(
        {
            "TIME": ["2020-12-21 06:00:00", "2020-12-21 07:00:00"],
            "TEMP": [15.0, 16.0],
            "PRCP": [0.0, 0.0],
        }
    ).to_csv(extract / "Weather data" / "Weather data.csv", index=False)


def write_sh_fixture(layout: Layout, n_rows: int = 6) -> None:
    raw = layout.raw / "sh"
    raw.mkdir(parents=True, exist_ok=True)
    rows = []
    for index in range(n_rows):
        start = datetime(2016, 8, 1, 7, 0) + timedelta(minutes=30 * index)
        end = start + timedelta(minutes=12)
        rows.append(
            {
                "orderid": 1000 + index,
                "bikeid": 2000 + index % 3,
                "userid": 99999,
                "start_time": start.strftime("%Y-%m-%d %H:%M:%S"),
                "start_location_x": 121.40 + index * 0.001,
                "start_location_y": 31.20,
                "end_time": end.strftime("%Y-%m-%d %H:%M:%S"),
                "end_location_x": 121.401 + index * 0.001,
                "end_location_y": 31.201,
                "duration": 12,
            }
        )
    pd.DataFrame(rows).to_csv(raw / "mobike_shanghai_sample_clean.csv", index=False)


def make_panel(cells: list[str], buckets: list[pd.Timestamp], counts: list[tuple[int, int]]) -> pd.DataFrame:
    """Build a panel from an explicit list of (out, in) pairs."""
    rows = []
    for bucket, (out, inn) in zip(buckets, counts):
        for cell in cells:
            rows.append(
                {
                    "cell_id": cell,
                    "time_bucket": bucket,
                    "out_count": out,
                    "in_count": inn,
                    "net_flow": out - inn,
                }
            )
    return pd.DataFrame(rows)


@pytest.fixture
def xm_raw(tiny_layout: Layout) -> Layout:
    write_xm_fixture(tiny_layout)
    return tiny_layout


@pytest.fixture
def sh_raw(tiny_layout: Layout) -> Layout:
    write_sh_fixture(tiny_layout)
    return tiny_layout


@pytest.fixture
def panel_factory():
    return make_panel


# -- Phase 4: one analysis build for the whole session ----------------------
@pytest.fixture(scope="session")
def presentations() -> dict:
    """The single Phase 4 analysis object per source, built once.

    Every Phase 4 test module reads this instead of rebuilding, so a wrong
    number cannot pass in one module and fail in another for a different reason:
    they all see the same derivation.
    """
    from bike_agent.presentation.analysis import build_all_presentations

    layout = Layout()
    config = load_config()
    required = [
        layout.outputs / f"sim_sensitivity.{source}.json"
        for source in ("xm", "sh")
    ]
    if not all(path.exists() for path in required):
        pytest.skip("Phase 4 artefacts missing; run `bike-agent replay --sources xm,sh` first")
    built = build_all_presentations(config, layout, ("xm", "sh"))
    return built


@pytest.fixture(scope="session")
def site_root() -> Path:
    return Layout().root / "replay"
