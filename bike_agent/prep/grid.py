"""Regular grid spatial unit (decision D2).

The grid is defined in metres and anchored on a robust bounding box of the
cleaned trips.  Points are assigned to cells by floor division; because the
grid is derived from the same quantiles used to drop out-of-bounds rows, every
surviving trip maps to a cell (no clipping, so aggregate totals stay exact).
"""

from __future__ import annotations

from dataclasses import dataclass

import numpy as np
import pandas as pd

LAT_M_PER_DEG = 110_540.0
LON_M_PER_DEG_AT_EQUATOR = 111_320.0


@dataclass
class Grid:
    cell_size_m: int
    lat_min: float
    lat_max: float
    lon_min: float
    lon_max: float
    lat_step: float
    lon_step: float
    nx: int
    ny: int
    cells: pd.DataFrame

    def assign(self, lat: pd.Series, lon: pd.Series) -> pd.Series:
        ix = np.floor((lon.to_numpy() - self.lon_min) / self.lon_step).astype(int)
        iy = np.floor((lat.to_numpy() - self.lat_min) / self.lat_step).astype(int)
        ix = np.clip(ix, 0, self.nx - 1)
        iy = np.clip(iy, 0, self.ny - 1)
        return pd.Series(
            [f"C{x:04d}_{y:04d}" for x, y in zip(ix, iy)],
            index=lat.index,
            dtype="object",
        )

    def as_dict(self) -> dict[str, float | int]:
        return {
            "cell_size_m": self.cell_size_m,
            "lat_min": self.lat_min,
            "lat_max": self.lat_max,
            "lon_min": self.lon_min,
            "lon_max": self.lon_max,
            "lat_step": self.lat_step,
            "lon_step": self.lon_step,
            "nx": self.nx,
            "ny": self.ny,
            "n_cells": int(len(self.cells)),
        }


def build_grid(
    frame: pd.DataFrame,
    cell_size_m: int,
    quantiles: tuple[float, float] | None = None,
) -> Grid:
    """Build a grid over the frame's coordinate envelope.

    Cleaning has already removed the out-of-bounds tails, so the default
    extent is the exact min/max of the surviving trips.  That guarantees every
    trip maps to a cell without clipping (the aggregate totals stay exact).
    """
    if frame.empty:
        raise ValueError("cannot build a grid from an empty trip table")
    lat = pd.concat([frame["start_lat"], frame["end_lat"]], ignore_index=True)
    lon = pd.concat([frame["start_lon"], frame["end_lon"]], ignore_index=True)
    if quantiles is None:
        lat_min, lat_max = float(lat.min()), float(lat.max())
        lon_min, lon_max = float(lon.min()), float(lon.max())
    else:
        lat_min, lat_max = float(lat.quantile(quantiles[0])), float(lat.quantile(quantiles[1]))
        lon_min, lon_max = float(lon.quantile(quantiles[0])), float(lon.quantile(quantiles[1]))

    mean_lat = float((lat_min + lat_max) / 2.0)
    lat_step = cell_size_m / LAT_M_PER_DEG
    lon_step = cell_size_m / (LON_M_PER_DEG_AT_EQUATOR * np.cos(np.radians(mean_lat)))

    nx = int(np.floor((lon_max - lon_min) / lon_step)) + 1
    ny = int(np.floor((lat_max - lat_min) / lat_step)) + 1
    nx = max(nx, 1)
    ny = max(ny, 1)

    records = []
    for ix in range(nx):
        for iy in range(ny):
            records.append(
                {
                    "cell_id": f"C{ix:04d}_{iy:04d}",
                    "ix": ix,
                    "iy": iy,
                    "lon": lon_min + (ix + 0.5) * lon_step,
                    "lat": lat_min + (iy + 0.5) * lat_step,
                }
            )
    cells = pd.DataFrame.from_records(records)
    return Grid(
        cell_size_m=int(cell_size_m),
        lat_min=lat_min,
        lat_max=lat_max,
        lon_min=lon_min,
        lon_max=lon_max,
        lat_step=lat_step,
        lon_step=lon_step,
        nx=nx,
        ny=ny,
        cells=cells,
    )
