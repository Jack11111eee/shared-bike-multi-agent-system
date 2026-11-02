"""Provenance for every dataset that reaches the published basemap.

The manifest is only as trustworthy as this table, so each entry answers the
questions a licence reviewer asks before data ships inside a package: who
published it, where it came from, when it was fetched, under what licence, and
whether that licence permits redistribution of a derived extract.

``redistributable`` is a decision, not a guess.  A dataset whose licence forbids
offline redistribution must not be published; :func:`dataset` refuses to hand one
out, so every path that reaches the manifest or the licence copies fails loudly
instead of silently shipping it.
"""

from __future__ import annotations

from typing import Any

#: Datasets used by the published basemap.  Keys are the ``dataset`` values that
#: appear in ``data/basemap/raw/<source>.json`` layers and in the manifest.
DATASETS: dict[str, dict[str, Any]] = {
    "gshhg": {
        "name": "GSHHG — Global Self-consistent Hierarchical High-resolution Geography",
        "provider": "NOAA National Centers for Environmental Information / University of Hawaii SOEST",
        "version": "2.3.7 (2017-06-15)",
        "source_page": "https://www.ngdc.noaa.gov/mgg/shorelines/gshhg.html",
        "download_url": "https://github.com/GenericMappingTools/gshhg-gmt/releases/download/2.3.7/gshhg-shp-2.3.7.zip",
        "archive_sha256": "8dbbe7e071e77e9e75f2d639239099ebca8d5c16d6a07df8169729d49f15cf41",
        "fetched": "2026-11-01",
        "license_id": "LGPL-3.0-or-later",
        "license_file": "licenses/GSHHG-LICENSE.txt",
        "license_texts": [
            "licenses/GSHHG-LICENSE.txt",
            "licenses/LGPL-3.0.txt",
            "licenses/GPL-3.0.txt",
        ],
        "license_summary": (
            "GSHHG is distributed under the GNU Lesser General Public License v3 or later; "
            "the bundled permission notice additionally allows use, copying and distribution "
            "provided the copyright and permission notices travel with the data."
        ),
        "redistributable": True,
        "attribution": "GSHHG 2.3.7 © NOAA / University of Hawaii",
        "original_crs": "EPSG:4326 (WGS84 geographic, per the shipped .prj)",
        "role": "land polygons (coastline, islands, estuary shoreline)",
    },
    "osm-geofabrik": {
        "name": "OpenStreetMap extracts (Geofabrik free shapefiles)",
        "provider": "OpenStreetMap contributors, extracted by Geofabrik GmbH",
        "version": "2026-10-31 extract (shanghai / fujian free .shp.zip)",
        "source_page": "https://download.geofabrik.de/asia/china.html",
        "download_url": (
            "https://download.geofabrik.de/asia/china/shanghai-261031-free.shp.zip ; "
            "https://download.geofabrik.de/asia/china/fujian-261031-free.shp.zip"
        ),
        "archive_sha256": (
            "shanghai 3027496ec551fd45e39f9040e2d21624d67c235b008f708ec4e6bcc38163d008 ; "
            "fujian 4d8774eaff1fa7319814b3276a0ca76b7a10047fab5415b8cc2b1f5d6071c1f6"
        ),
        "fetched": "2026-11-01",
        "license_id": "ODbL-1.0",
        "license_file": "licenses/ODbL-1.0.txt",
        "license_texts": ["licenses/ODbL-1.0.txt"],
        "license_summary": (
            "OpenStreetMap data is made available under the Open Database License; "
            "any public use must credit OpenStreetMap contributors. A produced map "
            "carries attribution; the underlying database keeps the ODbL terms."
        ),
        "redistributable": True,
        "attribution": "© OpenStreetMap contributors (ODbL 1.0)",
        "original_crs": "EPSG:4326 (WGS84 geographic, per the shipped .prj)",
        "role": "inland water lines (rivers, canals) and administrative outlines",
    },
}

#: Layers each source extract carries, and which dataset they come from.  The
#: build writes exactly these into the page, so a layer that appears in the raw
#: file but not here is a signal that the extract and the registry disagree.
LAYER_SOURCES = {
    "land": "gshhg",
    "water": "osm-geofabrik",
    "admin": "osm-geofabrik",
}


def dataset(name: str) -> dict[str, Any]:
    """One registry entry, with the failure mode spelled out when it is absent.

    Also the single gate for redistribution: a dataset whose licence does not
    permit shipping a derived extract offline raises here rather than reaching
    the published manifest.
    """
    if name not in DATASETS:
        raise KeyError(
            f"basemap dataset {name!r} is not in the provenance registry; "
            f"add it to bike_agent/basemap/sources.py before publishing it"
        )
    entry = DATASETS[name]
    if not entry.get("redistributable"):
        raise PermissionError(
            f"basemap dataset {name!r} is not redistributable under "
            f"{entry.get('license_id')!r}; refusing to publish it. Replace the dataset "
            f"or get a licence that permits offline redistribution."
        )
    return entry
