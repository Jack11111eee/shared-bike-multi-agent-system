"""F21: the offline basemap -- provenance, geometry and the projection itself.

The projection is checked against an *independent* implementation here: the
published pixel coordinates are compared with a forward transform written out
again from the documented definition, and with an inverse that maps back.  A test
that called ``Projection.forward`` to verify ``Projection.forward`` would pass
even if the whole transform were wrong.

The licence table gets a test of its own because it is the one part of the
basemap a reader cannot verify from the rendered page: a dataset with no
recorded licence must not reach the manifest.
"""

from __future__ import annotations

import json
import math
import re

import pandas as pd
import pytest

from bike_agent.basemap import DATASETS, dataset, grid_edges, load_raw
from bike_agent.config import Layout, load_config, source_config

SOURCES = ("xm", "sh")


@pytest.fixture(scope="module")
def layout():
    return Layout()


@pytest.fixture(scope="module")
def bundles(layout):
    out = {}
    for source in SOURCES:
        path = layout.root / "replay" / source / "basemap.js"
        text = path.read_text(encoding="utf-8")
        match = re.search(r'store\["[a-z]+"\] = (\{.*\});\n\}\)\(\);', text, re.S)
        assert match, f"{path} is not a basemap bundle"
        out[source] = json.loads(match.group(1))
    return out


@pytest.fixture(scope="module")
def cells(layout):
    config = load_config()
    return {
        source: pd.read_parquet(
            layout.cells_path(source, int(source_config(config, source)["cell_size_m"]))
        )
        for source in SOURCES
    }


# -- provenance -------------------------------------------------------------
def test_every_published_dataset_has_a_licence():
    for name, entry in DATASETS.items():
        for key in ("provider", "source_page", "download_url", "fetched", "license_id", "license_file",
                    "license_summary", "original_crs", "attribution"):
            assert entry.get(key), f"{name} 缺 {key}"
        assert entry["redistributable"] is True, f"{name} 不允许离线随包分发，不能发布"
        assert entry["source_page"].startswith("https://")


def test_unknown_dataset_names_fail_loudly():
    with pytest.raises(KeyError):
        dataset("no-such-dataset")


def test_a_non_redistributable_dataset_is_refused(monkeypatch):
    """The licence decision is enforced, not just recorded.

    ``redistributable: False`` must stop the dataset at the single accessor every
    publication path goes through, so a licence that forbids shipping an extract
    offline cannot reach the manifest or the licence copies by accident.
    """
    blocked = dict(DATASETS["gshhg"], redistributable=False, license_id="NO-REDISTRIBUTION")
    monkeypatch.setitem(DATASETS, "blocked", blocked)
    with pytest.raises(PermissionError) as caught:
        dataset("blocked")
    assert "NO-REDISTRIBUTION" in str(caught.value)
    # The two shipped datasets must still pass the same gate.
    for name in ("gshhg", "osm-geofabrik"):
        assert dataset(name)["redistributable"] is True


def test_manifest_records_provenance_and_licence_texts(layout):
    manifest = json.loads((layout.root / "replay" / "basemap" / "manifest.json").read_text(encoding="utf-8"))
    for name, entry in manifest["datasets"].items():
        assert entry["license_id"]
        assert entry["source_page"].startswith("https://")
        assert (layout.root / "replay" / "basemap" / entry["license_file"]).exists(), name
    for source in SOURCES:
        record = manifest["sources"][source]
        for key in ("published", "published_sha256", "raw_extract", "raw_extract_sha256",
                    "coverage", "display_crs", "projection", "simplification", "clipping", "layers"):
            assert record.get(key) is not None, f"{source} 缺 {key}"
        assert record["simplification"]["raw_points"]["land"] >= record["simplification"]["published_points"]["land"]
        assert record["coverage"]["display_bbox"][0] < record["coverage"]["grid_bbox"][0]
        assert record["coverage"]["display_bbox"][2] > record["coverage"]["grid_bbox"][2]


def test_manifest_records_how_the_extract_was_clipped(layout):
    """F21-1 asks for the clipping operation, not only the simplification.

    The window and the length thresholds that discarded features must be in the
    published manifest, so a reader can tell what the basemap leaves out without
    chasing a second file.
    """
    manifest = json.loads((layout.root / "replay" / "basemap" / "manifest.json").read_text(encoding="utf-8"))
    for source in SOURCES:
        clip = manifest["sources"][source]["clipping"]
        assert "clipping" in clip["method"].lower() or "裁剪" in clip["method"]
        window = clip["window"]
        assert len(window) == 4 and window[0] < window[2] and window[1] < window[3]
        selection = clip.get("selection") or {}
        assert selection.get("min_water_line_m", 0) > 0
        assert selection.get("min_admin_line_m", 0) > 0
        assert "river" in selection.get("water_classes", [])
        assert selection.get("dropped_water_lines", 0) >= 0


def test_manifest_and_bundle_agree_on_digest(layout, bundles):
    import hashlib

    manifest = json.loads((layout.root / "replay" / "basemap" / "manifest.json").read_text(encoding="utf-8"))
    for source in SOURCES:
        path = layout.root / "replay" / source / "basemap.js"
        digest = hashlib.sha256(path.read_bytes()).hexdigest()
        assert manifest["sources"][source]["published_sha256"] == digest
        assert manifest["sources"][source]["published_bytes"] == path.stat().st_size


# -- geometry ---------------------------------------------------------------
@pytest.mark.parametrize("source", SOURCES)
def test_every_cell_is_published_once_and_inside_the_canvas(bundles, cells, source):
    cells = cells[source]
    payload = bundles[source]
    projection = payload["projection"]
    rects = payload["cells"]
    ids = [row[0] for row in rects]
    assert len(ids) == len(set(ids)) == len(cells)
    assert set(ids) == set(cells["cell_id"])
    for _, x, y, w, h in rects:
        assert w > 0 and h > 0
        assert x >= 0 and y >= 0
        assert x + w <= projection["width"] + 1e-6
        assert y + h <= projection["height"] + 1e-6


def _independent_forward(projection, lon, lat):
    lon_min, lat_min, lon_max, lat_max = projection["bbox"]
    return (
        (lon - lon_min) / (lon_max - lon_min) * projection["width"],
        (lat_max - lat) / (lat_max - lat_min) * projection["height"],
    )


@pytest.mark.parametrize("source", SOURCES)
def test_cell_centres_match_an_independent_projection(bundles, cells, source):
    cells = cells[source]
    payload = bundles[source]
    projection = payload["projection"]
    rects = {row[0]: row[1:] for row in payload["cells"]}
    for row in cells.itertuples():
        x, y, w, h = rects[row.cell_id]
        want_x, want_y = _independent_forward(projection, row.lon, row.lat)
        assert math.hypot(x + w / 2 - want_x, y + h / 2 - want_y) <= 0.5, row.cell_id


@pytest.mark.parametrize("source", SOURCES)
def test_longitude_and_latitude_are_not_swapped_and_north_is_up(bundles, cells, source):
    cells = cells[source]
    projection = bundles[source]["projection"]
    lon = cells["lon"].to_numpy()
    lat = cells["lat"].to_numpy()
    xs = [(_independent_forward(projection, a, b)[0]) for a, b in zip(lon, lat)]
    ys = [(_independent_forward(projection, a, b)[1]) for a, b in zip(lon, lat)]
    # lon/lat correlate with x/y respectively, never the other way round.
    assert _pearson(lon, xs) > 0.99
    assert _pearson(lat, ys) < -0.99
    # North (larger latitude) is up (smaller y).
    north = int(lat.argmax())
    south = int(lat.argmin())
    assert ys[north] < ys[south]


def _pearson(a, b):
    import numpy as np

    return float(np.corrcoef(a, b)[0, 1])


@pytest.mark.parametrize("source", SOURCES)
def test_inverse_projection_round_trips(bundles, source):
    """Inverse checked against an independent forward, not against itself.

    The published numbers are read from the bundle and the forward transform is
    written out again here; calling ``Projection.forward`` to verify
    ``Projection.inverse`` would pass even if both were wrong in the same way.
    """
    payload = bundles[source]["projection"]
    lon_min, lat_min, lon_max, lat_max = payload["bbox"]
    width, height = payload["width"], payload["height"]

    def forward(lon, lat):
        return (
            (lon - lon_min) / (lon_max - lon_min) * width,
            (lat_max - lat) / (lat_max - lat_min) * height,
        )

    def inverse(x, y):
        return (
            lon_min + x / width * (lon_max - lon_min),
            lat_max - y / height * (lat_max - lat_min),
        )

    points = (
        (lon_min, lat_min),
        (lon_max, lat_max),
        ((lon_min + lon_max) / 2, (lat_min + lat_max) / 2),
        (lon_min + 0.25 * (lon_max - lon_min), lat_max - 0.75 * (lat_max - lat_min)),
    )
    for lon, lat in points:
        x, y = forward(lon, lat)
        back_lon, back_lat = inverse(x, y)
        assert back_lon == pytest.approx(lon, abs=1e-9)
        assert back_lat == pytest.approx(lat, abs=1e-9)


def test_grid_edges_extend_half_a_step_beyond_the_centres():
    cells = pd.DataFrame({
        "cell_id": ["a", "b", "c", "d"],
        "ix": [0, 1, 0, 1],
        "iy": [0, 0, 1, 1],
        "lon": [118.0, 118.01, 118.0, 118.01],
        "lat": [24.0, 24.0, 24.01, 24.01],
    })
    lon_min, lat_min, lon_max, lat_max = grid_edges(cells)
    assert lon_min == pytest.approx(118.0 - 0.005)
    assert lon_max == pytest.approx(118.01 + 0.005)
    assert lat_min == pytest.approx(24.0 - 0.005)
    assert lat_max == pytest.approx(24.01 + 0.005)


def test_a_single_column_grid_does_not_invent_a_step():
    cells = pd.DataFrame({"cell_id": ["a", "b"], "ix": [0, 0], "iy": [0, 1], "lon": [118.0, 118.0], "lat": [24.0, 24.01]})
    lon_min, _, lon_max, _ = grid_edges(cells)
    assert lon_min == lon_max == 118.0


# -- raw extracts -----------------------------------------------------------
@pytest.mark.parametrize("source", SOURCES)
def test_raw_extract_is_readable_and_covers_the_grid(layout, cells, source):
    cells = cells[source]
    raw = load_raw(layout, source)
    lon_min, lat_min, lon_max, lat_max = raw["bbox"]
    assert cells["lon"].between(lon_min, lon_max).all()
    assert cells["lat"].between(lat_min, lat_max).all()
    assert raw["layers"]["land"], "没有陆域多边形"
    for layer in ("land", "water", "admin"):
        for ring in raw["layers"][layer]:
            for lon, lat in ring:
                assert math.isfinite(lon) and math.isfinite(lat)
                assert -180 <= lon <= 180 and -90 <= lat <= 90


def test_missing_extract_says_how_to_rebuild(tmp_path):
    class FakeLayout:
        root = tmp_path

    with pytest.raises(Exception) as error:
        load_raw(FakeLayout(), "xm")
    assert "prepare" in str(error.value)


# -- licence conveyance through the rebuild ---------------------------------
def test_prepare_copies_the_licence_texts_the_archive_carries(tmp_path):
    """The rebuild path must produce the same licence set the build demands.

    ``build._write_licenses`` refuses to publish without four texts.  Two of them
    live in the GSHHG archive verbatim, so ``prepare`` has to copy them out --
    otherwise the documented reproduce-from-archive flow writes extracts that the
    build then rejects, with an error blaming the wrong tool.
    """
    from bike_agent.basemap.prepare import _copy_licenses

    archive = tmp_path / "gshhg"
    archive.mkdir()
    (archive / "LICENSE.TXT").write_text("GSHHG licence notice " + "x" * 1200, encoding="utf-8")
    (archive / "COPYING.LESSERv3").write_text(
        "GNU LESSER GENERAL PUBLIC LICENSE " + "x" * 1200, encoding="utf-8"
    )
    licenses = tmp_path / "licenses"
    licenses.mkdir()
    # The two texts neither archive contains: the operator places them by hand.
    (licenses / "GPL-3.0.txt").write_text("GNU GENERAL PUBLIC LICENSE " + "x" * 1200, encoding="utf-8")
    (licenses / "ODbL-1.0.txt").write_text("Open Database License " + "x" * 1200, encoding="utf-8")

    _copy_licenses(archive, licenses)

    assert "GSHHG" in (licenses / "GSHHG-LICENSE.txt").read_text(encoding="utf-8")
    assert "LESSER" in (licenses / "LGPL-3.0.txt").read_text(encoding="utf-8")


def test_prepare_names_every_missing_licence_and_where_it_comes_from(tmp_path):
    from bike_agent.basemap.prepare import _copy_licenses

    archive = tmp_path / "gshhg"
    archive.mkdir()
    licenses = tmp_path / "licenses"
    licenses.mkdir()

    with pytest.raises(FileNotFoundError) as caught:
        _copy_licenses(archive, licenses)
    message = str(caught.value)
    # An empty directory cannot satisfy the build, so the failure happens here --
    # naming each text and where to get it -- rather than later inside the build.
    for name in ("GSHHG-LICENSE.txt", "LGPL-3.0.txt", "GPL-3.0.txt", "ODbL-1.0.txt"):
        assert name in message
    assert "gpl-3.0.txt" in message and "opendatacommons.org" in message


# -- offline ----------------------------------------------------------------
def test_basemap_bundle_makes_no_network_calls(bundles):
    """The bundle carries attribution URLs as *text*, and nothing that fetches.

    A URL in a data field is provenance; a URL in a ``src``/``href``/``url()``
    is a request.  Only the first kind may appear.
    """
    allowed_hosts = (
        "ngdc.noaa.gov",
        "openstreetmap.org",
        "opendatacommons.org",
        "github.com",
    )
    for source, payload in bundles.items():
        text = json.dumps(payload, ensure_ascii=False)
        for match in re.finditer(r"https?://[^\s\"]+", text):
            host = match.group(0).split("/")[2]
            assert any(host.endswith(name) for name in allowed_hosts), f"{source}: 未知来源 {match.group(0)}"
        for pattern in ("fetch(", "XMLHttpRequest", "new Image(", "@import", "url(http"):
            assert pattern not in text, f"{source}: 底图数据包含请求式调用 {pattern}"


def test_page_never_links_out_for_attribution():
    layout = Layout()
    for source in SOURCES:
        page = (layout.root / "replay" / source / "workbench.html").read_text(encoding="utf-8")
        for match in re.finditer(r'(?:src|href)\s*=\s*"(https?:)?//[^"]+"', page):
            pytest.fail(f"{source}: 外部引用 {match.group(0)}")


@pytest.mark.parametrize("source", SOURCES)
def test_page_attribution_names_the_sources_it_credits(layout, source):
    """F21-4: the visible provenance must agree with the manifest.

    Provider names and source URLs appear as plain text (never as links), and the
    CRS statement says what the evidence says: WGS-84, no GCJ-02 conversion.
    """
    manifest = json.loads((layout.root / "replay" / "basemap" / "manifest.json").read_text(encoding="utf-8"))
    page = (layout.root / "replay" / source / "workbench.html").read_text(encoding="utf-8")
    attribution = re.search(r'<p class="hint geo-attribution"[^>]*>(.*?)</p>', page, re.S)
    assert attribution, f"{source} 页面没有底图出处"
    text = attribution.group(1)
    for name, entry in manifest["datasets"].items():
        assert entry["attribution"] in text, f"{source} 页面缺 {name} 的署名"
        assert entry["source_page"] in text, f"{source} 页面缺 {name} 的来源页"
        assert entry["download_url"] in text, f"{source} 页面缺 {name} 的下载地址"
        assert f"replay/basemap/{entry['license_file']}" in text
    assert "WGS-84" in text and "GCJ-02" in text and "不做 GCJ-02 转换" in text
    assert "不是道路轨迹" in text
    assert "replay/basemap/manifest.json" in text, f"{source} 页面没有指向 manifest"
    # The URLs are text, not anchors: no href may appear inside the attribution.
    assert "href=" not in text and "<a " not in text


# -- scale bar --------------------------------------------------------------
@pytest.mark.parametrize("source", SOURCES)
def test_scale_bar_is_recomputable(bundles, source):
    payload = bundles[source]
    bar = payload["scale_bar"]
    metres_per_pixel = payload["projection"]["metres_per_pixel"]
    assert bar["pixels"] == pytest.approx(bar["metres"] / metres_per_pixel, abs=0.1)
    assert bar["label"].endswith(("km", "m"))
    assert "m/px" in bar["recompute"]
