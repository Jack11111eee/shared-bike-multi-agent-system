"""Development-time preparation of the basemap extracts (needs the archives).

``bike-agent replay`` never calls this.  The build reads the small clipped
extracts this tool writes, so a machine that has never downloaded GSHHG or the
Geofabrik extracts still produces byte-identical basemap geometry.

Run it once per dataset refresh::

    python3 -m bike_agent.basemap.prepare \\
        --gshhg /path/to/gshhg-shp-2.3.7.zip \\
        --osm shanghai=/path/to/shanghai-261031-free.shp.zip \\
        --osm fujian=/path/to/fujian-261031-free.shp.zip

It writes

* ``data/basemap/raw/<source>.json``  -- clipped, unsimplified lon/lat rings
* ``data/basemap/raw/manifest.json``  -- which archive each extract came from,
  its sha256, the clip window and the point counts
* ``replay/basemap/licenses/``        -- the licence texts the manifest cites

Clipping is geometric, not a coordinate filter: a polygon is clipped with
Sutherland-Hodgman and a polyline with Liang-Barsky, so a ring that leaves the
window comes back closed along the window edge instead of being stitched across
the gap with a chord that never existed on the coast.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import shutil
import sys
import tempfile
import zipfile
from pathlib import Path
from typing import Any, Iterable

ROOT = Path(__file__).resolve().parents[2]

#: Display windows, a little wider than the grid plus its padding, so the map
#: still has context after the padding is applied at build time.
WINDOWS: dict[str, tuple[float, float, float, float]] = {
    "xm": (118.02, 24.39, 118.24, 24.60),
    "sh": (121.24, 31.04, 121.66, 31.46),
}
#: Which province extract supplies each source's OSM layers.
OSM_PROVINCE = {"xm": "fujian", "sh": "shanghai"}
#: OSM classes kept.  Roads are deliberately absent: the arrow lines are grid
#: moves, not road routes, and drawing streets would invite the opposite reading.
WATER_CLASSES = {"river", "canal"}
#: Administrative granularity follows the extent of the grid, not the province:
#: Xiamen's window is 13 km across, so a township boundary is the useful context;
#: Shanghai's is 40 km, where a district boundary is.  The same layer would be
#: either noise (townships in Shanghai) or too coarse (districts in Xiamen).
ADMIN_CLASSES = {"xm": {"admin_level8"}, "sh": {"admin_level6"}}
#: Minimum visible length, in metres, for a water line or a boundary to be kept.
#: Shanghai has thousands of sub-kilometre canals; drawing them turns the map into
#: hatching and buries the one line a reader needs (the Huangpu).  The threshold is
#: a *selection* rule, recorded in the extract manifest, not a simplification.
MIN_WATER_LINE_M = 1500.0
MIN_ADMIN_LINE_M = 2000.0


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


# -- clipping ---------------------------------------------------------------
def clip_polygon(ring: list[tuple[float, float]], bbox: tuple[float, float, float, float]) -> list[tuple[float, float]]:
    """Sutherland-Hodgman against an axis-aligned window (exact for convex windows)."""
    lon_min, lat_min, lon_max, lat_max = bbox

    def clip_edge(points: list[tuple[float, float]], inside, intersect) -> list[tuple[float, float]]:
        if not points:
            return []
        out: list[tuple[float, float]] = []
        previous = points[-1]
        previous_inside = inside(previous)
        for current in points:
            current_inside = inside(current)
            if current_inside:
                if not previous_inside:
                    out.append(intersect(previous, current))
                out.append(current)
            elif previous_inside:
                out.append(intersect(previous, current))
            previous, previous_inside = current, current_inside
        return out

    def at_x(p, q, x):
        t = (x - p[0]) / (q[0] - p[0])
        return (x, p[1] + t * (q[1] - p[1]))

    def at_y(p, q, y):
        t = (y - p[1]) / (q[1] - p[1])
        return (p[0] + t * (q[0] - p[0]), y)

    points = list(ring)
    points = clip_edge(points, lambda p: p[0] >= lon_min, lambda p, q: at_x(p, q, lon_min))
    points = clip_edge(points, lambda p: p[0] <= lon_max, lambda p, q: at_x(p, q, lon_max))
    points = clip_edge(points, lambda p: p[1] >= lat_min, lambda p, q: at_y(p, q, lat_min))
    points = clip_edge(points, lambda p: p[1] <= lat_max, lambda p, q: at_y(p, q, lat_max))
    return points


def clip_polyline(
    points: list[tuple[float, float]], bbox: tuple[float, float, float, float]
) -> list[list[tuple[float, float]]]:
    """Liang-Barsky per segment; returns the visible sub-polylines."""
    lon_min, lat_min, lon_max, lat_max = bbox
    out: list[list[tuple[float, float]]] = []
    current: list[tuple[float, float]] = []

    def clip_segment(p, q):
        x0, y0 = p
        x1, y1 = q
        dx, dy = x1 - x0, y1 - y0
        t0, t1 = 0.0, 1.0
        for denom, numer in ((-dx, x0 - lon_min), (dx, lon_max - x0), (-dy, y0 - lat_min), (dy, lat_max - y0)):
            if denom == 0:
                if numer < 0:
                    return None
            else:
                r = numer / denom
                if denom < 0:
                    if r > t1:
                        return None
                    if r > t0:
                        t0 = r
                else:
                    if r < t0:
                        return None
                    if r < t1:
                        t1 = r
        return ((x0 + t0 * dx, y0 + t0 * dy), (x0 + t1 * dx, y0 + t1 * dy))

    for index in range(len(points) - 1):
        segment = clip_segment(points[index], points[index + 1])
        if segment is None:
            if len(current) > 1:
                out.append(current)
            current = []
            continue
        start, end = segment
        if not current or current[-1] != start:
            if len(current) > 1:
                out.append(current)
            current = [start]
        current.append(end)
    if len(current) > 1:
        out.append(current)
    return out


# -- readers ----------------------------------------------------------------
def _iter_shapefile_parts(reader):
    for index in range(len(reader)):
        try:
            shape = reader.shape(index)
        except Exception:  # a corrupt record must not abort the whole extract
            continue
        if not shape.points:
            continue
        points = list(shape.points)
        parts = list(shape.parts) + [len(points)]
        for part in range(len(parts) - 1):
            yield index, points[parts[part] : parts[part + 1]]


def polygons_from(path: Path, bbox) -> list[list[tuple[float, float]]]:
    import shapefile

    reader = shapefile.Reader(str(path))
    rings: list[list[tuple[float, float]]] = []
    for index, part in _iter_shapefile_parts(reader):
        if len(part) < 3:
            continue
        x0, y0, x1, y1 = reader.shape(index).bbox
        if x1 < bbox[0] or x0 > bbox[2] or y1 < bbox[1] or y0 > bbox[3]:
            continue
        clipped = clip_polygon([tuple(p) for p in part], bbox)
        if len(clipped) >= 3:
            rings.append(clipped)
    return rings


def lines_from(path: Path, bbox, classes: Iterable[str] | None = None) -> list[list[tuple[float, float]]]:
    import shapefile

    reader = shapefile.Reader(str(path))
    wanted = set(classes) if classes is not None else None
    lines: list[list[tuple[float, float]]] = []
    for index, part in _iter_shapefile_parts(reader):
        if len(part) < 2:
            continue
        x0, y0, x1, y1 = reader.shape(index).bbox
        if x1 < bbox[0] or x0 > bbox[2] or y1 < bbox[1] or y0 > bbox[3]:
            continue
        if wanted is not None:
            try:
                if reader.record(index).as_dict().get("fclass") not in wanted:
                    continue
            except Exception:
                continue
        lines.extend(clip_polyline([tuple(p) for p in part], bbox))
    return lines


def _round(rings, digits: int = 5):
    return [[[round(p[0], digits), round(p[1], digits)] for p in ring] for ring in rings]


def _length_m(line: list[tuple[float, float]]) -> float:
    """Ground length of a lon/lat polyline, good to well under a percent here."""
    total = 0.0
    for (x0, y0), (x1, y1) in zip(line, line[1:]):
        mean_lat = math.radians((y0 + y1) / 2.0)
        dx = (x1 - x0) * 111320.0 * math.cos(mean_lat)
        dy = (y1 - y0) * 111320.0
        total += math.hypot(dx, dy)
    return total


def _keep_long(lines: list[list[tuple[float, float]]], minimum_m: float) -> tuple[list, int]:
    kept = [line for line in lines if _length_m(line) >= minimum_m]
    return kept, len(lines) - len(kept)


# -- extraction -------------------------------------------------------------
def _unzip_to(archive: Path, target: Path) -> Path:
    target.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(archive) as bundle:
        bundle.extractall(target)
    return target


def _find(root: Path, name: str) -> Path:
    for path in root.rglob(f"{name}.shp"):
        return path
    raise FileNotFoundError(f"{name}.shp not found under {root}")


#: Licence texts the build refuses to publish without, and where each one comes
#: from.  The GSHHG archive carries two of them verbatim; the GPL and ODbL texts
#: are not in either archive, so they are named here instead of being invented.
LICENSE_SOURCES: dict[str, tuple[str | None, str]] = {
    "GSHHG-LICENSE.txt": ("LICENSE.TXT", "GSHHG archive (LICENSE.TXT)"),
    "LGPL-3.0.txt": ("COPYING.LESSERv3", "GSHHG archive (COPYING.LESSERv3)"),
    "GPL-3.0.txt": (None, "https://www.gnu.org/licenses/gpl-3.0.txt（LGPL v3 引用它）"),
    "ODbL-1.0.txt": (None, "https://opendatacommons.org/licenses/odbl/1-0/"),
}


def _copy_licenses(gshhg_root: Path, licenses_dir: Path) -> None:
    """Copy every licence text the archive actually carries, then check the set.

    Copying is not enough: a rebuild that leaves the build unable to publish
    would fail later with a message pointing at this tool.  So the missing texts
    are reported here, with where to get them, before the extracts are written.
    """
    licenses_dir.mkdir(parents=True, exist_ok=True)
    for name, (archive_name, _) in LICENSE_SOURCES.items():
        if archive_name is None:
            continue
        source = gshhg_root / archive_name
        if source.exists():
            shutil.copyfile(source, licenses_dir / name)
    missing = [name for name in LICENSE_SOURCES if not (licenses_dir / name).exists()]
    if missing:
        details = "\n".join(f"  - {name}：{LICENSE_SOURCES[name][1]}" for name in missing)
        raise FileNotFoundError(
            f"{licenses_dir} 缺 {len(missing)} 份许可文本，构建会拒绝发布底图：\n{details}\n"
            "把文件放进该目录后重跑；仓库里的同名文件可直接沿用。"
        )


def prepare(
    *,
    gshhg_archive: Path,
    osm_archives: dict[str, Path],
    output: Path,
    licenses_dir: Path,
    workdir: Path | None = None,
) -> dict[str, Any]:
    """Clip both sources; write the extracts, their manifest and the licences."""
    manifest: dict[str, Any] = {"generated_by": "bike_agent.basemap.prepare", "sources": {}, "archives": {}}

    with tempfile.TemporaryDirectory(dir=workdir) as tmp:
        tmp_path = Path(tmp)
        gshhg_root = _unzip_to(gshhg_archive, tmp_path / "gshhg")
        land_shp = _find(gshhg_root, "GSHHS_h_L1")
        manifest["archives"]["gshhg"] = {
            "file": gshhg_archive.name,
            "sha256": _sha256(gshhg_archive),
            "bytes": gshhg_archive.stat().st_size,
            "layer": str(land_shp.relative_to(gshhg_root)),
        }
        _copy_licenses(gshhg_root, licenses_dir)
        osm_roots: dict[str, Path] = {}
        for province, archive in osm_archives.items():
            root = _unzip_to(archive, tmp_path / f"osm-{province}")
            osm_roots[province] = root
            manifest["archives"][f"osm-{province}"] = {
                "file": archive.name,
                "sha256": _sha256(archive),
                "bytes": archive.stat().st_size,
            }

        for source, bbox in WINDOWS.items():
            province = OSM_PROVINCE[source]
            root = osm_roots[province]
            land = polygons_from(land_shp, bbox)
            water_all = lines_from(_find(root, "gis_osm_waterways_free_1"), bbox, WATER_CLASSES)
            admin_all = lines_from(_find(root, "gis_osm_adminareas_a_free_1"), bbox, ADMIN_CLASSES[source])
            water, water_dropped = _keep_long(water_all, MIN_WATER_LINE_M)
            admin, admin_dropped = _keep_long(admin_all, MIN_ADMIN_LINE_M)
            payload = {
                "source": source,
                "bbox": list(bbox),
                "province": province,
                "layers": {"land": _round(land), "water": _round(water), "admin": _round(admin)},
            }
            path = output / f"{source}.json"
            output.mkdir(parents=True, exist_ok=True)
            path.write_text(json.dumps(payload, separators=(",", ":")), encoding="utf-8")
            manifest["sources"][source] = {
                "extract": str(path.relative_to(ROOT)) if path.is_relative_to(ROOT) else str(path),
                "extract_sha256": _sha256(path),
                "extract_bytes": path.stat().st_size,
                "bbox": list(bbox),
                "province": province,
                "points": {layer: sum(len(ring) for ring in rings) for layer, rings in payload["layers"].items()},
                "parts": {layer: len(rings) for layer, rings in payload["layers"].items()},
                "selection": {
                    "water_classes": sorted(WATER_CLASSES),
                    "admin_classes": sorted(ADMIN_CLASSES[source]),
                    "min_water_line_m": MIN_WATER_LINE_M,
                    "min_admin_line_m": MIN_ADMIN_LINE_M,
                    "dropped_water_lines": water_dropped,
                    "dropped_admin_lines": admin_dropped,
                    "note": "按可见长度筛选线要素；多边形只做裁剪，不做简化（简化在构建期完成）",
                },
            }
            print(
                f"[prepare] {source}: "
                + ", ".join(f"{layer} {len(rings)}×{sum(len(r) for r in rings)}pts" for layer, rings in payload["layers"].items())
                + f" -> {path.stat().st_size:,} B"
            )

    (output / "manifest.json").write_text(json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return manifest


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--gshhg", type=Path, required=True, help="gshhg-shp-2.3.7.zip")
    parser.add_argument(
        "--osm",
        action="append",
        default=[],
        metavar="PROVINCE=PATH",
        help="Geofabrik free shapefile archive, e.g. shanghai=/tmp/shanghai-261031-free.shp.zip",
    )
    parser.add_argument("--output", type=Path, default=ROOT / "data" / "basemap" / "raw")
    parser.add_argument("--licenses", type=Path, default=ROOT / "replay" / "basemap" / "licenses")
    args = parser.parse_args(argv)

    archives: dict[str, Path] = {}
    for item in args.osm:
        if "=" not in item:
            parser.error(f"--osm expects PROVINCE=PATH, got {item!r}")
        province, path = item.split("=", 1)
        archives[province] = Path(path)
    missing = [name for name in set(OSM_PROVINCE.values()) if name not in archives]
    if missing:
        parser.error(f"missing --osm archives for {sorted(missing)}")

    try:
        prepare(
            gshhg_archive=args.gshhg,
            osm_archives=archives,
            output=args.output,
            licenses_dir=args.licenses,
        )
    except FileNotFoundError as error:
        # A missing archive or licence is an operator error, not a crash: report
        # it the same way argparse reports a bad argument.
        parser.error(str(error))
    return 0


if __name__ == "__main__":  # pragma: no cover - developer entry point
    sys.exit(main())
