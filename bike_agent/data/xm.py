"""DS-XM adapter: Xiamen shared-bicycle dataset (Zenodo 17415891).

Raw files are distributed as a single ZIP whose CSV members are actually
Excel workbooks (the ``order data.csv`` and ``STATION.csv`` files start with
the ``PK`` ZIP signature).  The adapter reads them with ``pandas.read_excel``.

Two known defects from REQUIREMENTS.md section 2.2 are handled explicitly:

* ``UPDATE_TIME1`` contains only the date and ``UPDATE_TIME2`` only the time;
  they are combined into a single timestamp (otherwise everything collapses
  to midnight).
* ``BICYCLE_ID`` is only filled on the first row of each bicycle block, so it
  is forward-filled before the unlock/lock rows are paired into trips.
"""

from __future__ import annotations

import re
import urllib.request
import zipfile
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd

from bike_agent.data.base import DataSourceAdapter, DataSourceError

_ORDER_MEMBER = "Order data/order data.csv"
_FENCE_MEMBER = "Electronic fence data/STATION.csv"
_WEATHER_MEMBER = "Weather data/Weather data.csv"
_README_MEMBER = "README.md"

_POINT_RE = re.compile(r"\[\s*(-?\d+(?:\.\d+)?)\s*,\s*(-?\d+(?:\.\d+)?)\s*\]")


class XiamenAdapter(DataSourceAdapter):
    name = "xm"

    @property
    def raw_dir(self) -> Path:
        return self.layout.raw_dir(self.name)

    @property
    def archive_path(self) -> Path:
        return self.raw_dir / self.source_cfg.get("archive_name", "shared_bicycle_data.zip")

    @property
    def extract_dir(self) -> Path:
        return self.raw_dir / "extracted"

    def fetch(self) -> dict[str, Any]:
        url = self.source_cfg["url"]
        if self.force or not self.archive_path.exists():
            self.raw_dir.mkdir(parents=True, exist_ok=True)
            try:
                urllib.request.urlretrieve(url, self.archive_path)
            except Exception as exc:  # pragma: no cover - network failure path
                raise DataSourceError(f"failed to download DS-XM from {url}: {exc}") from exc

        members = [_ORDER_MEMBER, _FENCE_MEMBER, _WEATHER_MEMBER, _README_MEMBER]
        self.extract_dir.mkdir(parents=True, exist_ok=True)
        try:
            with zipfile.ZipFile(self.archive_path) as archive:
                names = set(archive.namelist())
                missing = [m for m in members if m not in names]
                if missing:
                    raise DataSourceError(f"DS-XM archive missing members: {missing}")
                for member in members:
                    target = self.extract_dir / member
                    if self.force or not target.exists():
                        target.parent.mkdir(parents=True, exist_ok=True)
                        archive.extract(member, self.extract_dir)
        except zipfile.BadZipFile as exc:
            raise DataSourceError(f"DS-XM archive is not a valid zip: {exc}") from exc

        extracted = [self.extract_dir / m for m in members]
        return self._record_manifest(
            [self.archive_path, *extracted],
            extra={"note": "Trajectory CSVs are not extracted: the pipeline uses orders, fences and weather only."},
        )

    # -- trips --------------------------------------------------------------
    def load_trips(self) -> pd.DataFrame:
        path = self.extract_dir / _ORDER_MEMBER
        if not path.exists():
            raise DataSourceError(f"DS-XM order data not staged; run `bike-agent data fetch --source xm` ({path})")

        raw = pd.read_excel(path, sheet_name=0)
        raw.columns = [str(c).strip().upper() for c in raw.columns]
        raw = raw.rename(columns={"ORDER DURATION": "ORDER_DURATION"})
        required = {"BICYCLE_ID", "LATITUDE", "LONGITUDE", "LOCK_STATUS", "UPDATE_TIME1", "UPDATE_TIME2"}
        missing = required - set(raw.columns)
        if missing:
            raise DataSourceError(f"DS-XM order data missing columns: {sorted(missing)}")

        raw["BICYCLE_ID"] = raw["BICYCLE_ID"].ffill()
        date_part = pd.to_datetime(raw["UPDATE_TIME1"], errors="coerce").dt.strftime("%Y-%m-%d")
        time_part = raw["UPDATE_TIME2"].astype(str)
        timestamps = pd.to_datetime(date_part + " " + time_part, errors="coerce")

        frame = pd.DataFrame(
            {
                "bike_id": raw["BICYCLE_ID"].astype(str),
                "lat": pd.to_numeric(raw["LATITUDE"], errors="coerce"),
                "lon": pd.to_numeric(raw["LONGITUDE"], errors="coerce"),
                "lock_status": pd.to_numeric(raw["LOCK_STATUS"], errors="coerce"),
                "ts": timestamps,
            }
        )
        raw_rows = int(len(frame))
        frame = frame.dropna(subset=["ts", "lat", "lon", "lock_status"])
        dropped_unparseable = raw_rows - len(frame)
        frame = frame.sort_values(["bike_id", "ts"], kind="mergesort").reset_index(drop=True)

        previous = frame.shift(1)
        paired = (
            (frame["lock_status"] == 1)
            & (previous["lock_status"] == 0)
            & (previous["bike_id"] == frame["bike_id"])
        )
        trips = pd.DataFrame(
            {
                "bike_id": frame.loc[paired, "bike_id"].to_numpy(),
                "start_time": previous.loc[paired, "ts"].to_numpy(),
                "end_time": frame.loc[paired, "ts"].to_numpy(),
                "start_lat": previous.loc[paired, "lat"].to_numpy(),
                "start_lon": previous.loc[paired, "lon"].to_numpy(),
                "end_lat": frame.loc[paired, "lat"].to_numpy(),
                "end_lon": frame.loc[paired, "lon"].to_numpy(),
            }
        )
        trips["duration_s"] = (
            pd.to_datetime(trips["end_time"]) - pd.to_datetime(trips["start_time"])
        ).dt.total_seconds()
        nonpositive = int((trips["duration_s"] <= 0).sum())
        trips = trips[trips["duration_s"] > 0].reset_index(drop=True)
        trips["trip_id"] = [f"xm-{i:07d}" for i in range(len(trips))]
        self.stats = {
            "raw_rows": raw_rows,
            "dropped_unparseable_rows": int(dropped_unparseable),
            "unpaired_lock_events": int(raw_rows - dropped_unparseable - 2 * (len(trips) + nonpositive)),
            "dropped_nonpositive_duration": nonpositive,
            "paired_trips": int(len(trips)),
        }
        return trips

    # -- extras -------------------------------------------------------------
    def load_fences(self) -> pd.DataFrame:
        path = self.extract_dir / _FENCE_MEMBER
        if not path.exists():
            raise DataSourceError(f"DS-XM fence data not staged: {path}")
        raw = pd.read_excel(path, sheet_name=0)
        raw.columns = [str(c).strip().upper() for c in raw.columns]
        records = []
        for _, row in raw.iterrows():
            points = _parse_polygon(str(row.get("FENCE_LOC", "")))
            if not points:
                continue
            lons = [p[0] for p in points]
            lats = [p[1] for p in points]
            records.append(
                {
                    "fence_id": str(row.get("FENCE_ID")),
                    "centroid_lon": float(np.mean(lons)),
                    "centroid_lat": float(np.mean(lats)),
                    "area_m2": _polygon_area_m2(points),
                    "n_vertices": len(points),
                }
            )
        return pd.DataFrame.from_records(records)

    def load_weather(self) -> pd.DataFrame:
        path = self.extract_dir / _WEATHER_MEMBER
        if not path.exists():
            raise DataSourceError(f"DS-XM weather data not staged: {path}")
        weather = pd.read_csv(path)
        weather["TIME"] = pd.to_datetime(weather["TIME"], errors="coerce")
        return weather.dropna(subset=["TIME"]).reset_index(drop=True)

    def load_extras(self) -> dict[str, pd.DataFrame]:
        return {"fences": self.load_fences(), "weather": self.load_weather()}


def _parse_polygon(text: str) -> list[tuple[float, float]]:
    points = [(float(lon), float(lat)) for lon, lat in _POINT_RE.findall(text)]
    # The ring is closed (last point repeats the first); drop the duplicate.
    if len(points) > 1 and points[0] == points[-1]:
        points = points[:-1]
    return points


def _polygon_area_m2(points: list[tuple[float, float]]) -> float:
    if len(points) < 3:
        return 0.0
    lats = np.array([p[1] for p in points])
    lons = np.array([p[0] for p in points])
    mean_lat = float(lats.mean())
    x = lons * 111_320.0 * np.cos(np.radians(mean_lat))
    y = lats * 110_540.0
    return float(abs(np.dot(x, np.roll(y, -1)) - np.dot(y, np.roll(x, -1))) / 2.0)
