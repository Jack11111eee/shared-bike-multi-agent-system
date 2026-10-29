"""F2-1: every cleaning rule reports what it removed and why."""

from __future__ import annotations

import pandas as pd

from bike_agent.prep.clean import clean_trips


def _trip(trip_id: str, start: str, end: str, lat: float = 24.5, lon: float = 118.1,
          end_lat: float = 24.501, end_lon: float = 118.101, duration: float = 600.0) -> dict:
    return {
        "trip_id": trip_id,
        "bike_id": "b1",
        "start_time": pd.Timestamp(start),
        "end_time": pd.Timestamp(end),
        "start_lat": lat,
        "start_lon": lon,
        "end_lat": end_lat,
        "end_lon": end_lon,
        "duration_s": duration,
    }


def _frame() -> pd.DataFrame:
    rows = [
        _trip("t1", "2020-12-21 06:00", "2020-12-21 06:10"),
        _trip("t2", "2020-12-21 06:20", "2020-12-21 06:30", lat=24.502),
        _trip("t3", "2020-12-21 06:40", "2020-12-21 06:50", lat=24.503),
        _trip("t1", "2020-12-21 07:00", "2020-12-21 07:10", lat=24.504),  # duplicate id
        _trip("t5", "2020-12-21 07:20", "2020-12-21 07:30", lat=float("nan")),  # missing coord
        _trip("t6", "2020-12-21 07:40", "2020-12-21 07:41", duration=10.0),  # too short
        _trip("t7", "2020-12-21 07:50", "2020-12-22 07:50", duration=30000.0),  # too long
        _trip("t8", "2020-12-21 08:00", "2020-12-21 08:10", lat=50.0, end_lat=50.1),  # out of bounds
        _trip("t9", "2020-12-21 08:20", "2020-12-21 08:30", end_lat=24.5, end_lon=118.1),  # pseudo
        _trip("t10", "2020-12-21 08:40", "2020-12-21 08:50", lat=24.505),
    ]
    return pd.DataFrame(rows)


def test_clean_trips_applies_each_rule_and_counts_rows():
    result = clean_trips(_frame(), bbox_quantiles=(0.05, 0.90))

    assert result.rows_in == 10
    assert result.rows_out == 4
    assert [rule["rule"] for rule in result.rules] == [
        "duplicate_trip_id",
        "missing_coordinates",
        "abnormal_duration",
        "out_of_bounds_coordinates",
        "pseudo_trip_same_origin_destination",
    ]
    assert [rule["rows_removed"] for rule in result.rules] == [1, 1, 2, 1, 1]
    assert all(rule["reason"] for rule in result.rules)
    assert all("rows_remaining" in rule for rule in result.rules)
    assert set(result.frame["trip_id"]) == {"t1", "t2", "t3", "t10"}


def test_clean_trips_keeps_bbox_for_reporting():
    result = clean_trips(_frame(), bbox_quantiles=(0.05, 0.90))
    assert result.bbox["lat_min"] <= result.bbox["lat_max"]
    assert result.bbox["lon_min"] <= result.bbox["lon_max"]
    assert result.as_dict()["rows_removed"] == 6


def test_clean_trips_is_idempotent_on_clean_data():
    frame = pd.DataFrame(
        [_trip("a", "2020-12-21 06:00", "2020-12-21 06:10"), _trip("b", "2020-12-21 07:00", "2020-12-21 07:10")]
    )
    first = clean_trips(frame)
    second = clean_trips(first.frame)
    assert first.rows_out == 2
    assert second.rows_out == 2
    assert all(rule["rows_removed"] == 0 for rule in second.rules)
