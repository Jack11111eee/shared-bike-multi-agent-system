"""Build the offline basemap: project, simplify, publish, record provenance.

The published artefacts are

* ``replay/<source>/basemap.js`` -- one classic script that registers
  ``window.BASEMAP_DATA[<source>]``.  It carries *geometry only*: land and water
  paths, the grid's cell rectangles in map pixels, the scale bar and the
  attribution text.  No business value is stored here, so the page cannot read a
  number from the basemap that it should have read from an output file.
* ``replay/basemap/manifest.json`` -- the audit trail: source page, provider,
  fetch date, licence id and local licence file, original and display CRS,
  coverage, simplification, and sha256 of both the raw extract and the published
  file.
* ``replay/basemap/licenses/*.txt`` -- the licence texts the manifest points at.

Everything below runs offline.  The raw extracts under ``data/basemap/raw/`` are
committed, so ``bike-agent replay`` reproduces the same geometry on a machine
that has never seen the upstream archives.  Neither this module nor
``prepare.py`` opens a network connection: a human downloads the archives, and
``prepare.py`` only unzips and clips the local copies it is given.

Projection.  A local equirectangular (plate carrée) projection with the standard
parallel at the grid's mean latitude::

    x = (lon - lon_min) / lon_span * width
    y = (lat_max - lat) / lat_span * height,   height = width * lat_span / (lon_span * cos(phi0))

At these extents (13–34 km across) the distortion is far below a pixel, and both
directions are closed-form, so the geometry check can invert the published
pixels without reusing this code's forward pass.
"""

from __future__ import annotations

import hashlib
import json
import math
from pathlib import Path
from typing import Any, Iterable

import pandas as pd

from bike_agent.basemap.sources import DATASETS, LAYER_SOURCES, dataset

#: Where the published basemap lives, relative to the site root.
BASEMAP_DIR = "basemap"
#: Published map width in CSS pixels.  Height follows from the aspect ratio.
MAP_WIDTH = 760
#: Context padding around the grid, as a fraction of the grid span per side.
PADDING = 0.10
#: Douglas-Peucker tolerance in *pixels*.  Below half a pixel, so a simplification
#: cannot move a coastline further than the geometry check's own threshold.
SIMPLIFY_PX = 0.35
#: Rounding for published pixel coordinates.  0.1 px is an order of magnitude
#: finer than the check tolerance and halves the file size against full floats.
PIXEL_DECIMALS = 1


class BasemapError(RuntimeError):
    """Raised when the basemap cannot be built honestly (missing or unlicensed data)."""


# -- projection -------------------------------------------------------------
class Projection:
    """Equirectangular projection onto a fixed pixel box.

    ``forward`` maps (lon, lat) to map pixels; ``inverse`` maps back.  Both are
    plain arithmetic so that an independent check can re-derive one from the
    other without importing this class.
    """

    def __init__(self, bbox: tuple[float, float, float, float], width: int = MAP_WIDTH):
        lon_min, lat_min, lon_max, lat_max = bbox
        if not (lon_min < lon_max and lat_min < lat_max):
            raise BasemapError(f"degenerate basemap bbox {bbox}")
        self.bbox = (float(lon_min), float(lat_min), float(lon_max), float(lat_max))
        self.width = int(width)
        self.std_parallel = (lat_min + lat_max) / 2.0
        self.lon_span = lon_max - lon_min
        self.lat_span = lat_max - lat_min
        # Aspect: one degree of longitude is cos(phi0) as long as one of latitude.
        self.height = int(round(width * self.lat_span / (self.lon_span * math.cos(math.radians(self.std_parallel)))))
        self._sx = self.width / self.lon_span
        self._sy = self.height / self.lat_span

    def forward(self, lon: float, lat: float) -> tuple[float, float]:
        return (
            (lon - self.bbox[0]) * self._sx,
            (self.bbox[3] - lat) * self._sy,
        )

    def inverse(self, x: float, y: float) -> tuple[float, float]:
        return (
            self.bbox[0] + x / self._sx,
            self.bbox[3] - y / self._sy,
        )

    def metres_per_pixel(self) -> float:
        """Ground distance of one pixel, using the standard parallel."""
        return 111320.0 * math.cos(math.radians(self.std_parallel)) / self._sx

    def as_dict(self) -> dict[str, Any]:
        return {
            "kind": "equirectangular",
            "standard_parallel": round(self.std_parallel, 6),
            "bbox": [round(value, 6) for value in self.bbox],
            "width": self.width,
            "height": self.height,
            "px_per_degree_lon": self._sx,
            "px_per_degree_lat": self._sy,
            "metres_per_pixel": round(self.metres_per_pixel(), 4),
            "north_up": True,
        }


def grid_edges(cells: pd.DataFrame) -> tuple[float, float, float, float]:
    """The grid's outer edges in the source CRS.

    ``cells`` carries cell *centres*; the grid extends half a step beyond the
    outermost centres.  Deriving the edges from the centres (rather than reading a
    stored bbox) keeps the map honest when the grid changes size.
    """
    lon = cells["lon"].to_numpy(dtype=float)
    lat = cells["lat"].to_numpy(dtype=float)
    ix = cells["ix"].to_numpy(dtype=int)
    iy = cells["iy"].to_numpy(dtype=int)
    if len(set(ix.tolist())) > 1:
        lon_step = (lon.max() - lon.min()) / (ix.max() - ix.min())
    else:  # a single column: no step to measure
        lon_step = 0.0
    if len(set(iy.tolist())) > 1:
        lat_step = (lat.max() - lat.min()) / (iy.max() - iy.min())
    else:
        lat_step = 0.0
    return (
        float(lon.min() - lon_step / 2.0),
        float(lat.min() - lat_step / 2.0),
        float(lon.max() + lon_step / 2.0),
        float(lat.max() + lat_step / 2.0),
    )


def display_bbox(edges: tuple[float, float, float, float], padding: float = PADDING) -> tuple[float, float, float, float]:
    """The grid edges plus context on every side."""
    lon_min, lat_min, lon_max, lat_max = edges
    pad_lon = (lon_max - lon_min) * padding
    pad_lat = (lat_max - lat_min) * padding
    return (lon_min - pad_lon, lat_min - pad_lat, lon_max + pad_lon, lat_max + pad_lat)


# -- geometry helpers -------------------------------------------------------
def _simplify(points: list[tuple[float, float]], tolerance: float) -> list[tuple[float, float]]:
    """Douglas-Peucker on already-projected points.

    Iterative rather than recursive: a clipped coastline can carry tens of
    thousands of vertices and Python's recursion limit is not a geometry
    parameter.
    """
    if len(points) <= 2:
        return list(points)
    keep = [False] * len(points)
    keep[0] = keep[-1] = True
    stack = [(0, len(points) - 1)]
    tolerance_sq = tolerance * tolerance
    while stack:
        start, end = stack.pop()
        if end <= start + 1:
            continue
        x0, y0 = points[start]
        x1, y1 = points[end]
        dx, dy = x1 - x0, y1 - y0
        length_sq = dx * dx + dy * dy
        worst, worst_index = -1.0, None
        for index in range(start + 1, end):
            px, py = points[index]
            if length_sq == 0:
                dist_sq = (px - x0) ** 2 + (py - y0) ** 2
            else:
                t = ((px - x0) * dx + (py - y0) * dy) / length_sq
                t = 0.0 if t < 0 else (1.0 if t > 1 else t)
                dist_sq = (px - (x0 + t * dx)) ** 2 + (py - (y0 + t * dy)) ** 2
            if dist_sq > worst:
                worst, worst_index = dist_sq, index
        if worst_index is not None and worst > tolerance_sq:
            keep[worst_index] = True
            stack.append((start, worst_index))
            stack.append((worst_index, end))
    return [point for point, kept in zip(points, keep) if kept]


def _round(value: float) -> float:
    return round(float(value), PIXEL_DECIMALS)


def _path(points: list[tuple[float, float]], *, close: bool) -> str:
    """SVG path data for one polyline; a closed ring is completed with ``Z``."""
    if len(points) < 2:
        return ""
    head = f"M{_round(points[0][0])} {_round(points[0][1])}"
    body = "".join(f"L{_round(x)} {_round(y)}" for x, y in points[1:])
    return head + body + ("Z" if close else "")


# -- raw extracts -----------------------------------------------------------
def raw_path(layout: Any, source: str) -> Path:
    return Path(layout.root) / "data" / "basemap" / "raw" / f"{source}.json"


def load_raw(layout: Any, source: str) -> dict[str, Any]:
    """Read one source's clipped extract, with a message that says how to make it."""
    path = raw_path(layout, source)
    if not path.exists():
        raise BasemapError(
            f"missing basemap extract {path}. Rebuild it with "
            f"`python3 -m bike_agent.basemap.prepare` (needs the upstream archives)."
        )
    payload = json.loads(path.read_text(encoding="utf-8"))
    for layer in LAYER_SOURCES:
        if layer not in payload.get("layers", {}):
            raise BasemapError(f"{path} has no {layer!r} layer")
    return payload


def _clipping(layout: Any, source: str, payload: dict[str, Any]) -> dict[str, Any]:
    """Describe how the raw extract was clipped and filtered, for the manifest.

    The authoritative record is ``data/basemap/raw/manifest.json`` (written by the
    prepare step); this copies its per-source entry so the published manifest
    alone answers "what was thrown away and why".
    """
    block: dict[str, Any] = {
        "method": (
            "Sutherland-Hodgman polygon clipping and Liang-Barsky line clipping "
            "to the window below; lines shorter than the thresholds are dropped"
        ),
        "window": [round(value, 6) for value in payload["bbox"]],
        "province": payload.get("province"),
        "detail": "data/basemap/raw/manifest.json",
    }
    record = Path(layout.root) / "data" / "basemap" / "raw" / "manifest.json"
    if record.exists():
        try:
            entry = json.loads(record.read_text(encoding="utf-8"))["sources"][source]
        except (ValueError, KeyError):
            entry = None
        if isinstance(entry, dict):
            if entry.get("selection"):
                block["selection"] = entry["selection"]
            if entry.get("bbox"):
                block["window"] = entry["bbox"]
    return block


# -- build ------------------------------------------------------------------
def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def build_basemap(
    config: dict[str, Any],
    layout: Any,
    sources: Iterable[str],
    *,
    width: int = MAP_WIDTH,
) -> dict[str, Any]:
    """Write the per-source basemap scripts and the shared manifest.

    Returns ``{source: {"bytes": n, "sha256": ..., "cells": n, "north_up": True}}``
    so the caller can report sizes without re-reading the files.
    """
    site_dir = Path(layout.root) / "replay"
    shared_dir = site_dir / BASEMAP_DIR
    shared_dir.mkdir(parents=True, exist_ok=True)

    published: dict[str, dict[str, Any]] = {}
    manifest_sources: dict[str, Any] = {}

    for source in sources:
        cells = pd.read_parquet(
            Path(layout.cells_path(source, int(config["sources"][source]["cell_size_m"])))
        )
        edges = grid_edges(cells)
        raw = load_raw(layout, source)
        raw_file = raw_path(layout, source)
        projection = Projection(display_bbox(edges), width=width)

        layers: dict[str, list[str]] = {"land": [], "water": [], "admin": []}
        kept_points: dict[str, int] = {}
        raw_points: dict[str, int] = {}
        for layer, rings in raw["layers"].items():
            if layer not in layers:
                continue
            paths: list[str] = []
            total_raw = total_kept = 0
            for ring in rings:
                projected = [projection.forward(lon, lat) for lon, lat in ring]
                total_raw += len(projected)
                simplified = _simplify(projected, SIMPLIFY_PX)
                total_kept += len(simplified)
                data = _path(simplified, close=(layer == "land"))
                if data:
                    paths.append(data)
            layers[layer] = paths
            kept_points[layer] = total_kept
            raw_points[layer] = total_raw

        # Cell rectangles in map pixels.  A cell is a lon/lat box, and this
        # projection is separable, so its image is exactly a rectangle -- no
        # rotation to approximate.
        lon_step = (edges[2] - edges[0]) / max(int(cells["ix"].max()) + 1, 1)
        lat_step = (edges[3] - edges[1]) / max(int(cells["iy"].max()) + 1, 1)
        cell_rects: list[list[Any]] = []
        for row in cells.sort_values(["iy", "ix"], kind="mergesort").itertuples():
            lon0 = edges[0] + int(row.ix) * lon_step
            lat0 = edges[1] + int(row.iy) * lat_step
            x0, y1 = projection.forward(lon0, lat0)
            x1, y0 = projection.forward(lon0 + lon_step, lat0 + lat_step)
            cell_rects.append([row.cell_id, _round(x0), _round(y0), _round(x1 - x0), _round(y1 - y0)])

        scale_bar = _scale_bar(projection)
        payload = {
            "source": source,
            "projection": projection.as_dict(),
            "grid_bbox": [round(value, 6) for value in edges],
            "cells": cell_rects,
            "layers": layers,
            "scale_bar": scale_bar,
            "attribution": _attribution(),
            "crs": {
                "business": "EPSG:4326 (WGS84 geographic) — as stored in data/processed",
                "basemap": "EPSG:4326 (WGS84 geographic) — per the shipped .prj files",
                "transform_applied": False,
                "evidence": "docs/crs-evidence.md",
            },
            "simplification": {
                "tolerance_px": SIMPLIFY_PX,
                "raw_points": raw_points,
                "published_points": kept_points,
            },
            "generated_by": "bike_agent.basemap.build",
        }
        text = (
            "(function(){\n"
            "  var store = window.BASEMAP_DATA = window.BASEMAP_DATA || {};\n"
            f"  store[{json.dumps(source)}] = "
            + json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
            + ";\n})();"
        )
        out = site_dir / source / "basemap.js"
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(text, encoding="utf-8")

        published[source] = {
            "path": f"replay/{source}/basemap.js",
            "bytes": out.stat().st_size,
            "sha256": _sha256(out),
            "cells": len(cell_rects),
            "width": projection.width,
            "height": projection.height,
        }
        manifest_sources[source] = {
            "published": f"replay/{source}/basemap.js",
            "published_sha256": published[source]["sha256"],
            "published_bytes": published[source]["bytes"],
            "raw_extract": str(raw_file.relative_to(Path(layout.root))),
            "raw_extract_sha256": _sha256(raw_file),
            "raw_extract_bytes": raw_file.stat().st_size,
            "coverage": {
                "grid_bbox": [round(value, 6) for value in edges],
                "display_bbox": [round(value, 6) for value in projection.bbox],
            },
            "display_crs": "EPSG:4326 (WGS84 geographic), equirectangular projection",
            "clipping": _clipping(layout, source, raw),
            "projection": projection.as_dict(),
            "simplification": {
                "method": "Douglas-Peucker in projected pixels",
                "tolerance_px": SIMPLIFY_PX,
                "raw_points": raw_points,
                "published_points": kept_points,
            },
            "layers": {
                layer: {
                    "dataset": LAYER_SOURCES[layer],
                    "paths": len(layers[layer]),
                    "points": kept_points.get(layer, 0),
                }
                for layer in layers
            },
        }

    manifest = {
        "phase": 5,
        "requirement": "REQUIREMENTS-PHASE5.md F21",
        "generated_by": "bike_agent.basemap.build",
        "note": (
            "底图只提供位置语境，不携带任何业务数值；所有数字仍来自 outputs/ 的静态产物。"
            "来源 URL 以普通文本记录，页面不引用外部资源。"
        ),
        "datasets": {
            name: {key: value for key, value in dataset(name).items() if key != "redistributable"}
            for name in DATASETS
        },
        "sources": manifest_sources,
        "budget": {
            "published_total_bytes": sum(item["bytes"] for item in published.values()),
            "cap_bytes": 2 * 1024 * 1024,
        },
    }
    (shared_dir / "manifest.json").write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    _write_licenses(shared_dir)
    return published


def _scale_bar(projection: Projection) -> dict[str, Any]:
    """A round ground distance that fits in roughly a fifth of the map width."""
    target_px = projection.width / 5.0
    metres_per_pixel = projection.metres_per_pixel()
    target_metres = target_px * metres_per_pixel
    # 1-2-5 sequence, so the label is a number a reader can divide in their head.
    exponent = 10 ** math.floor(math.log10(max(target_metres, 1e-6)))
    nice = None
    for factor in (1, 2, 5, 10):
        if factor * exponent >= target_metres:
            nice = factor * exponent
            break
    nice = float(nice or target_metres)
    pixels = nice / metres_per_pixel
    label = f"{nice / 1000:g} km" if nice >= 1000 else f"{nice:g} m"
    return {
        "metres": nice,
        "pixels": round(pixels, 1),
        "label": label,
        "recompute": (
            f"{nice:g} m ÷ {metres_per_pixel:.4f} m/px = {pixels:.1f} px "
            f"(标准纬线 {projection.std_parallel:.4f}°, 111320 m/°)"
        ),
    }


def _attribution() -> dict[str, str]:
    """Plain text only: the offline scan forbids external href/src anywhere."""
    return {
        "land": "GSHHG 2.3.7（NOAA / University of Hawaii，LGPL-3.0）",
        "water_admin": "水系与行政轮廓 © OpenStreetMap contributors（ODbL 1.0）",
        "land_url": "https://www.ngdc.noaa.gov/mgg/shorelines/gshhg.html",
        "osm_url": "https://www.openstreetmap.org/copyright",
        "note": "底图为位置语境，不参与任何指标计算；比例尺按标准纬线换算，可复算。",
    }


def _write_licenses(shared_dir: Path) -> None:
    """Verify the licence texts the manifest points at, verbatim from the archives.

    Existence alone is not enough: a truncated or placeholder file would satisfy
    "the file is there" while failing the licence's own conveyance requirement.
    Each text must be non-trivial and contain the licence's identifying line.
    """
    licenses = shared_dir / "licenses"
    licenses.mkdir(parents=True, exist_ok=True)
    required = {
        "GSHHG-LICENSE.txt": "GSHHG",
        "LGPL-3.0.txt": "GNU LESSER GENERAL PUBLIC LICENSE",
        "GPL-3.0.txt": "GNU GENERAL PUBLIC LICENSE",
        "ODbL-1.0.txt": "Open Database License",
    }
    for name in DATASETS:
        entry = dataset(name)
        for relative in entry.get("license_texts") or [entry["license_file"]]:
            path = licenses / Path(relative).name
            if not path.exists():
                raise BasemapError(
                    f"missing {path}; the licence text must ship next to the extract. "
                    "prepare.py copies the GSHHG texts out of the archive, but the GPL and "
                    "ODbL texts are not inside either archive -- place them there manually "
                    "(see docs/basemap-sources.md §5) before building"
                )
            text = path.read_text(encoding="utf-8", errors="replace")
            phrase = required.get(path.name)
            if len(text) < 1000:
                raise BasemapError(
                    f"{path} is only {len(text)} bytes -- that is a notice, not the licence text. "
                    f"Bundle the full text."
                )
            if phrase and phrase not in text:
                raise BasemapError(
                    f"{path} does not contain {phrase!r}; it is not the licence it is named after."
                )
