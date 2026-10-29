"""F1: adapters, the trip contract and the integrity report."""

from __future__ import annotations

import pandas as pd
import pytest

from bike_agent.data.contract import (
    CONTRACT_COLUMNS,
    ContractError,
    assert_contract,
    normalise_contract,
    validate_contract,
)
from bike_agent.data.quality import build_quality_report, render_quality_markdown
from bike_agent.data.sh import ShanghaiAdapter
from bike_agent.data.xm import XiamenAdapter
from bike_agent.config import Layout


def test_xm_adapter_pairs_unlock_and_lock_rows(tiny_config, xm_raw):
    adapter = XiamenAdapter(tiny_config, xm_raw)
    frame = adapter.contract_frame()

    assert len(frame) == 6  # 3 bikes x 2 trips
    assert list(frame.columns) == list(CONTRACT_COLUMNS)
    assert (frame["end_time"] > frame["start_time"]).all()
    assert frame["duration_s"].between(800, 1000).all()
    assert frame["bike_id"].nunique() == 3
    assert adapter.stats["raw_rows"] == 12
    assert adapter.stats["paired_trips"] == 6


def test_xm_adapter_reads_fences_and_weather(tiny_config, xm_raw):
    adapter = XiamenAdapter(tiny_config, xm_raw)
    extras = adapter.load_extras()
    assert len(extras["fences"]) == 2
    assert {"fence_id", "centroid_lat", "centroid_lon", "area_m2"} <= set(extras["fences"].columns)
    assert len(extras["weather"]) == 2


def test_sh_adapter_normalises_contract_and_drops_userid(tiny_config, sh_raw):
    adapter = ShanghaiAdapter(tiny_config, sh_raw)
    frame = adapter.contract_frame()

    assert len(frame) == 6
    assert list(frame.columns) == list(CONTRACT_COLUMNS)
    assert "userid" not in frame.columns
    assert (frame["duration_s"] == 720).all()
    assert (frame["end_time"] > frame["start_time"]).all()
    # Shanghai x = longitude, y = latitude
    assert frame["start_lon"].between(121.3, 121.5).all()
    assert frame["start_lat"].between(31.1, 31.3).all()


def test_contract_validation_detects_time_order_violation():
    frame = pd.DataFrame(
        {
            "trip_id": ["t1", "t2"],
            "bike_id": ["b1", "b1"],
            "start_time": pd.to_datetime(["2020-01-01 08:00", "2020-01-01 09:00"]),
            "end_time": pd.to_datetime(["2020-01-01 08:10", "2020-01-01 08:50"]),
            "start_lat": [24.5, 24.5],
            "start_lon": [118.1, 118.1],
            "end_lat": [24.51, 24.51],
            "end_lon": [118.11, 118.11],
            "duration_s": [600.0, -600.0],
        }
    )
    report = validate_contract(frame)
    assert report["start_before_end_ok"] is False
    assert report["start_before_end_violations"] == 1
    with pytest.raises(ContractError):
        assert_contract(frame)


def test_contract_validation_reports_required_metrics():
    frame = pd.DataFrame(
        {
            "trip_id": ["t1", "t1"],
            "bike_id": ["b1", "b1"],
            "start_time": pd.to_datetime(["2020-01-01 08:00", "2020-01-01 09:00"]),
            "end_time": pd.to_datetime(["2020-01-01 08:10", "2020-01-01 09:10"]),
            "start_lat": [24.5, None],
            "start_lon": [118.1, 118.1],
            "end_lat": [24.51, 24.51],
            "end_lon": [118.11, 118.11],
            "duration_s": [600.0, 600.0],
        }
    )
    report = validate_contract(frame)
    assert report["rows"] == 2
    assert report["duplicate_trip_ids"] == 1
    assert report["duplicate_rate"] == 0.5
    assert report["null_rate"]["start_lat"] == 0.5
    assert report["abnormal_duration_rows"] == 0
    assert set(report["bbox"]) == {"lat_min", "lat_max", "lon_min", "lon_max"}


def test_quality_markdown_contains_required_sections():
    frame = pd.DataFrame(
        {
            "trip_id": ["t1"],
            "bike_id": ["b1"],
            "start_time": pd.to_datetime(["2020-01-01 08:00"]),
            "end_time": pd.to_datetime(["2020-01-01 08:10"]),
            "start_lat": [24.5],
            "start_lon": [118.1],
            "end_lat": [24.51],
            "end_lon": [118.11],
            "duration_s": [600.0],
        }
    )
    validation = validate_contract(frame)
    report = build_quality_report("xm", frame, validation)
    markdown = render_quality_markdown(report)
    for token in ("总行数", "时间跨度", "坐标包围盒", "重复率", "缺失率", "异常时长占比"):
        assert token in markdown


def test_normalise_contract_coerces_dtypes():
    frame = pd.DataFrame(
        {
            "trip_id": [1],
            "bike_id": [2],
            "start_time": ["2020-01-01 08:00:00"],
            "end_time": ["2020-01-01 08:10:00"],
            "start_lat": ["24.5"],
            "start_lon": ["118.1"],
            "end_lat": ["24.51"],
            "end_lon": ["118.11"],
            "duration_s": ["600"],
        }
    )
    normalised = normalise_contract(frame)
    assert normalised["trip_id"].dtype == object
    assert pd.api.types.is_datetime64_any_dtype(normalised["start_time"])
    assert normalised["duration_s"].dtype == "float64"


def test_real_contract_if_present():
    root = Layout().root
    path = root / "data" / "processed" / "contract.xm.parquet"
    if not path.exists():
        pytest.skip("real DS-XM contract not generated in this environment")
    frame = pd.read_parquet(path)
    report = validate_contract(frame)
    assert report["rows"] > 200_000
    assert report["start_before_end_ok"]
    assert list(frame.columns) == list(CONTRACT_COLUMNS)
