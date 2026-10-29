"""DS-SH adapter: Shanghai Mobike sample (ArtemisLiu GitHub repository).

The sample has no fences, no weather and no station capacity, so downstream
stages degrade gracefully: fence-derived capacity falls back to the configured
fixed capacity (FR-DATA-6).  The ``userid`` column is dropped at the adapter
boundary and never written anywhere (NF-6).
"""

from __future__ import annotations

import urllib.request
from pathlib import Path
from typing import Any

import pandas as pd

from bike_agent.data.base import DataSourceAdapter, DataSourceError

_COLUMN_MAP = {
    "orderid": "trip_id",
    "bikeid": "bike_id",
    "start_time": "start_time",
    "end_time": "end_time",
    "start_location_y": "start_lat",
    "start_location_x": "start_lon",
    "end_location_y": "end_lat",
    "end_location_x": "end_lon",
}


class ShanghaiAdapter(DataSourceAdapter):
    name = "sh"

    @property
    def raw_dir(self) -> Path:
        return self.layout.raw_dir(self.name)

    @property
    def csv_path(self) -> Path:
        return self.raw_dir / self.source_cfg.get("archive_name", "mobike_shanghai_sample_clean.csv")

    def fetch(self) -> dict[str, Any]:
        url = self.source_cfg["url"]
        if self.force or not self.csv_path.exists():
            self.raw_dir.mkdir(parents=True, exist_ok=True)
            try:
                urllib.request.urlretrieve(url, self.csv_path)
            except Exception as exc:  # pragma: no cover - network failure path
                raise DataSourceError(f"failed to download DS-SH from {url}: {exc}") from exc
        return self._record_manifest([self.csv_path])

    def load_trips(self) -> pd.DataFrame:
        if not self.csv_path.exists():
            raise DataSourceError(f"DS-SH data not staged; run `bike-agent data fetch --source sh` ({self.csv_path})")
        raw = pd.read_csv(self.csv_path)
        missing = set(_COLUMN_MAP) - set(raw.columns)
        if missing:
            raise DataSourceError(f"DS-SH data missing columns: {sorted(missing)}")

        frame = raw.loc[:, list(_COLUMN_MAP)].rename(columns=_COLUMN_MAP).copy()
        frame["trip_id"] = frame["trip_id"].astype(str)
        frame["bike_id"] = frame["bike_id"].astype(str)
        frame["start_time"] = pd.to_datetime(frame["start_time"], errors="coerce")
        frame["end_time"] = pd.to_datetime(frame["end_time"], errors="coerce")
        for col in ("start_lat", "start_lon", "end_lat", "end_lon"):
            frame[col] = pd.to_numeric(frame[col], errors="coerce")
        frame["duration_s"] = (frame["end_time"] - frame["start_time"]).dt.total_seconds()
        frame = frame.dropna(subset=["start_time", "end_time"]).reset_index(drop=True)
        raw_rows = int(len(raw))
        dropped_unparseable = raw_rows - len(frame)
        nonpositive = int((frame["duration_s"] <= 0).sum())
        frame = frame[frame["duration_s"] > 0].reset_index(drop=True)
        self.stats = {
            "raw_rows": raw_rows,
            "dropped_unparseable_rows": int(dropped_unparseable),
            "dropped_nonpositive_duration": nonpositive,
            "paired_trips": int(len(frame)),
        }
        return frame.loc[:, [
            "trip_id", "bike_id", "start_time", "end_time",
            "start_lat", "start_lon", "end_lat", "end_lon", "duration_s",
        ]]
