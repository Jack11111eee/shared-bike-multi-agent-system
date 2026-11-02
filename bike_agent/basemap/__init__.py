"""F21: the offline basemap that sits under the grid.

Three pieces, in the order they run:

* :mod:`bike_agent.basemap.prepare` -- a development-time tool that clips the
  downloaded global datasets to each source's display window.  It writes the
  small extracts under ``data/basemap/raw/`` so the *build* never needs a network
  connection.
* :mod:`bike_agent.basemap.build` -- the offline build: project, simplify, and
  publish ``replay/<source>/basemap.js`` plus ``replay/basemap/manifest.json``.
* :mod:`bike_agent.basemap.sources` -- the licence and provenance registry the
  manifest is written from.  Nothing here is inferred at build time: a dataset
  without a recorded licence cannot be published.

The map is context, never evidence.  Every business number keeps its own
``data-source`` trail on the page; the basemap only says *where* the grid is.
"""

from __future__ import annotations

from bike_agent.basemap.build import (
    BASEMAP_DIR,
    BasemapError,
    Projection,
    build_basemap,
    grid_edges,
    load_raw,
)
from bike_agent.basemap.sources import DATASETS, dataset

__all__ = [
    "BASEMAP_DIR",
    "BasemapError",
    "DATASETS",
    "Projection",
    "build_basemap",
    "dataset",
    "grid_edges",
    "load_raw",
]
