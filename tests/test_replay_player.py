"""F12/F13: the player bundle and page must be a faithful, offline, static view.

Like the Phase 2 replay tests, every assertion reads the shipped files and
compares them against ``outputs/`` -- every frame, every cell, every move.  No
headless browser is involved: the first frame is rendered into the DOM in
Python precisely so this module can check it with the standard library.
"""

from __future__ import annotations

import json
import re
from html.parser import HTMLParser
from pathlib import Path

import pandas as pd
import pytest

from bike_agent.config import Layout

ROOT = Path(__file__).resolve().parents[1]
SITE = ROOT / "replay"
STRATEGIES = ("rule", "optimized")
EXPECTED_FRAMES = {"xm": 4, "sh": 149}
EXPECTED_EMPTY_MOVES = {"xm": 0, "sh": 40}
EXPECTED_COLOR_MAX = {"xm": 1228, "sh": 40}
PREFIX = "window.REPLAY_DATA = "


class Nodes(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.elements: list[tuple[str, dict[str, str]]] = []
        self.title = ""
        self.noscript = ""
        self._in_title = False
        self._in_noscript = False
        self._title: list[str] = []
        self._noscript: list[str] = []

    def handle_starttag(self, tag, attrs):
        values = {key: (value or "") for key, value in attrs}
        self.elements.append((tag, values))
        if tag == "title":
            self._in_title = True
        if tag == "noscript":
            self._in_noscript = True

    def handle_startendtag(self, tag, attrs):
        self.handle_starttag(tag, attrs)

    def handle_data(self, data):
        if self._in_title:
            self._title.append(data)
        if self._in_noscript:
            self._noscript.append(data)

    def handle_endtag(self, tag):
        if tag == "title":
            self._in_title = False
            self.title = "".join(self._title)
        if tag == "noscript":
            self._in_noscript = False
            self.noscript = "".join(self._noscript)

    def by_class(self, name: str) -> list[dict[str, str]]:
        return [attrs for _, attrs in self.elements if name in attrs.get("class", "").split()]

    def by_attr(self, name: str) -> list[dict[str, str]]:
        return [attrs for _, attrs in self.elements if name in attrs]


def _parse(path: Path) -> Nodes:
    page = Nodes()
    page.feed(path.read_text(encoding="utf-8"))
    page.close()
    return page


def _bundle(source: str) -> dict:
    path = SITE / source / "replay-data.js"
    text = path.read_text(encoding="utf-8")
    assert text.startswith(PREFIX), f"{path.name} must be a classic script assigning window.REPLAY_DATA"
    assert text.endswith(";"), f"{path.name} must end with a semicolon"
    assert text.count("window.REPLAY_DATA") == 1, f"{path.name} must contain no other logic"
    return json.loads(text[len(PREFIX):-1])


def _steps(source: str, strategy: str) -> pd.DataFrame:
    frame = pd.read_csv(Layout().sim_path(source, strategy, "csv"))
    frame["time_bucket"] = pd.to_datetime(frame["time_bucket"])
    return frame.sort_values("time_bucket").reset_index(drop=True)


def _states(source: str, strategy: str) -> pd.DataFrame:
    state = pd.read_parquet(Layout().replay_path(source, strategy))
    state["time_bucket"] = pd.to_datetime(state["time_bucket"])
    return state


def _moves(source: str, strategy: str) -> list[dict]:
    return json.loads(
        (Layout().outputs / f"sim_moves.{strategy}.{source}.json").read_text(encoding="utf-8")
    )


def _buckets(source: str, strategy: str) -> list[pd.Timestamp]:
    return list(_steps(source, strategy)["time_bucket"])


def _available() -> bool:
    return all((SITE / source / "player.html").exists() for source in EXPECTED_FRAMES)


pytestmark = pytest.mark.skipif(not _available(), reason="player site not built; run `bike-agent repro` first")


# -- F12-1 ------------------------------------------------------------------
@pytest.mark.parametrize("source", sorted(EXPECTED_FRAMES))
def test_frame_count_equals_product_and_frame_pages(source):
    bundle = _bundle(source)
    rule = _steps(source, "rule")
    pages = sorted(SITE.glob(f"{source}/frame-*.html"))
    assert bundle["n_frames"] == len(bundle["frames"]) == len(rule) == len(pages) == EXPECTED_FRAMES[source]
    assert [frame["index"] for frame in bundle["frames"]] == list(range(bundle["n_frames"]))
    assert [frame["time_bucket"] for frame in bundle["frames"]] == [
        ts.strftime("%Y-%m-%d %H:%M:%S") for ts in rule["time_bucket"]
    ]
    assert _buckets(source, "optimized") == _buckets(source, "rule")


# -- F12-2 ------------------------------------------------------------------
@pytest.mark.parametrize("source", sorted(EXPECTED_FRAMES))
def test_every_frame_every_cell_equals_the_parquet(source):
    bundle = _bundle(source)
    cell_ids = bundle["cell_ids"]
    assert len(cell_ids) == len(bundle["geom"]) == len(set(cell_ids))
    for strategy in STRATEGIES:
        state = _states(source, strategy)
        for frame in bundle["frames"]:
            bucket = pd.Timestamp(frame["time_bucket"])
            rows = state.loc[state["time_bucket"] == bucket].set_index("cell_id")
            assert set(rows.index) == set(cell_ids)
            block = frame["strategies"][strategy]
            assert block["inventory_end"] == [int(rows.loc[cell, "inventory_end"]) for cell in cell_ids]
            assert block["unmet"] == [int(rows.loc[cell, "unmet_requests"]) for cell in cell_ids]
            assert block["moved"] == [int(rows.loc[cell, "bikes_moved"]) for cell in cell_ids]


# -- F12-3 ------------------------------------------------------------------
@pytest.mark.parametrize("source", sorted(EXPECTED_FRAMES))
def test_every_frame_kpi_equals_the_csv(source):
    bundle = _bundle(source)
    for strategy in STRATEGIES:
        steps = _steps(source, strategy)
        for frame in bundle["frames"]:
            row = steps.iloc[frame["index"]]
            kpi = frame["strategies"][strategy]["kpi"]
            assert kpi["time_bucket"] == pd.Timestamp(row["time_bucket"]).strftime("%Y-%m-%d %H:%M:%S")
            for field in (
                "borrow_requests",
                "borrow_met",
                "unmet_requests",
                "returns",
                "returns_applied",
                "returns_overflow",
                "bikes_moved",
                "distance_bike_km",
                "inventory_start",
                "inventory_end",
            ):
                expected = float(row[field])
                assert abs(float(kpi[field]) - expected) <= 1e-9 * max(1.0, abs(expected)), (field, frame["index"])


# -- F12-4 / F12-5 ----------------------------------------------------------
@pytest.mark.parametrize("source", sorted(EXPECTED_FRAMES))
def test_every_frame_moves_are_exactly_the_artifact(source):
    bundle = _bundle(source)
    for strategy in STRATEGIES:
        records = _moves(source, strategy)
        empty = 0
        for frame in bundle["frames"]:
            bucket = pd.Timestamp(frame["time_bucket"])
            expected = sorted(
                (str(item["from_cell"]), str(item["to_cell"]), int(item["bikes"]))
                for item in records
                if pd.Timestamp(item["time_bucket"]) == bucket
            )
            rendered = sorted(
                (str(item["from_cell"]), str(item["to_cell"]), int(item["bikes"]))
                for item in frame["moves"][strategy]
            )
            assert rendered == expected, f"{source}/{strategy} frame {frame['index']}"
            if not expected:
                empty += 1
                assert frame["moves"][strategy] == []
        assert empty == EXPECTED_EMPTY_MOVES[source], f"{source}/{strategy}: {empty} empty frames"


# -- F12-6 ------------------------------------------------------------------
@pytest.mark.parametrize("source", sorted(EXPECTED_FRAMES))
def test_controls_are_present_and_identified(source):
    page = _parse(SITE / source / "player.html")
    n_frames = _bundle(source)["n_frames"]
    controls = page.by_attr("data-control")
    values = {attrs["data-control"] for attrs in controls}
    assert {"play", "prev", "next", "scrub", "speed", "loop"} <= values
    speeds = [float(attrs["data-speed"]) for attrs in controls if attrs["data-control"] == "speed"]
    assert len(speeds) >= 3 and 1.0 in speeds
    scrub = next(attrs for attrs in controls if attrs["data-control"] == "scrub")
    assert int(scrub["max"]) == n_frames - 1


# -- F12-7 ------------------------------------------------------------------
@pytest.mark.parametrize("source", sorted(EXPECTED_FRAMES))
def test_first_frame_is_prerendered_in_the_static_dom(source):
    page = _parse(SITE / source / "player.html")
    bundle = _bundle(source)
    first = bundle["frames"][0]
    bucket = pd.Timestamp(first["time_bucket"])

    # KPIs, straight from the first CSV row.
    csv_fields = {
        "borrow_requests",
        "borrow_met",
        "unmet_requests",
        "returns",
        "returns_applied",
        "returns_overflow",
        "bikes_moved",
        "distance_bike_km",
        "inventory_start",
        "inventory_end",
    }
    checked = 0
    for attrs in page.by_class("metric"):
        metric = attrs.get("data-metric", "")
        if metric not in csv_fields:
            continue
        match = re.fullmatch(r"outputs/sim_(rule|optimized)\.([a-z]+)\.csv", attrs["data-source"])
        assert match, attrs["data-source"]
        strategy, metric_source = match.group(1), match.group(2)
        assert metric_source == source
        row = _steps(source, strategy).loc[_steps(source, strategy)["time_bucket"] == bucket].iloc[0]
        assert float(attrs["data-value"]) == float(row[metric]), (source, strategy, metric)
        checked += 1
    assert checked > 0

    # Grid cells: one rect per cell per strategy, values from the parquet.
    cell_ids = bundle["cell_ids"]
    for strategy in STRATEGIES:
        rects = [
            attrs
            for attrs in page.by_class("cell")
            if attrs.get("data-source") == f"outputs/replay.{strategy}.{source}.parquet"
        ]
        assert len(rects) == len(cell_ids)
        rows = _states(source, strategy)
        rows = rows.loc[rows["time_bucket"] == bucket].set_index("cell_id")
        rendered = {attrs["data-cell"]: attrs for attrs in rects}
        for cell in cell_ids:
            assert int(rendered[cell]["data-inventory-end"]) == int(rows.loc[cell, "inventory_end"])
            assert int(rendered[cell]["data-unmet"]) == int(rows.loc[cell, "unmet_requests"])
            assert int(rendered[cell]["data-moved"]) == int(rows.loc[cell, "bikes_moved"])

    # Arrows: every first-frame move record, and nothing else.
    for strategy in STRATEGIES:
        expected = sorted(
            (str(item["from_cell"]), str(item["to_cell"]), int(item["bikes"]))
            for item in _moves(source, strategy)
            if pd.Timestamp(item["time_bucket"]) == bucket
        )
        rendered = sorted(
            (attrs["data-from"], attrs["data-to"], int(attrs["data-bikes"]))
            for attrs in page.by_class("arrow")
            if attrs.get("data-source") == f"outputs/sim_moves.{strategy}.{source}.json"
        )
        assert rendered == expected


# -- F12-8 / NF-16 ----------------------------------------------------------
_EXTERNAL = re.compile(
    r"""(?:src|href)\s*=\s*["'](?:(?:https?:)?//)[^"']+["']|@import\s+(?:url\()?["']?(?:https?:)?//|url\(\s*["']?(?:https?:)?//""",
    re.IGNORECASE,
)


def test_the_whole_site_is_offline():
    offenders = []
    for path in SITE.rglob("*"):
        if path.suffix not in {".html", ".css", ".js"}:
            continue
        for match in _EXTERNAL.finditer(path.read_text(encoding="utf-8")):
            offenders.append(f"{path.relative_to(ROOT)}: {match.group(0)[:80]}")
    assert not offenders, "external references found:\n" + "\n".join(offenders)


# -- F12-9 ------------------------------------------------------------------
@pytest.mark.parametrize("source", sorted(EXPECTED_FRAMES))
def test_noscript_fallback_and_readable_header(source):
    page = _parse(SITE / source / "player.html")
    assert "frame-0000.html" in page.noscript
    assert source.upper() in page.title or source in page.title
    metrics = {attrs.get("data-metric"): attrs for attrs in page.by_class("metric")}
    rule = _steps(source, "rule")
    assert int(metrics["frame_count"]["data-value"]) == len(rule)
    assert pd.Timestamp(metrics["time_range_start"]["data-value"]) == rule["time_bucket"].iloc[0]
    assert pd.Timestamp(metrics["time_range_end"]["data-value"]) == rule["time_bucket"].iloc[-1]


# -- F13-1 ------------------------------------------------------------------
@pytest.mark.parametrize("source", sorted(EXPECTED_FRAMES))
def test_color_domain_is_fixed_and_shown(source):
    bundle = _bundle(source)
    global_max = 0
    for strategy in STRATEGIES:
        state = _states(source, strategy)
        global_max = max(global_max, int(state["inventory_end"].max()))
    assert bundle["color_domain"] == {"min": 0, "max": global_max}
    assert bundle["color_domain"]["max"] == EXPECTED_COLOR_MAX[source]

    text = (SITE / source / "player.html").read_text(encoding="utf-8")
    scale = re.search(r'data-scale="inventory"[^>]*>', text)
    assert scale, "no data-scale='inventory' element"
    assert f'data-min="{bundle["color_domain"]["min"]}"' in scale.group(0)
    assert f'data-max="{bundle["color_domain"]["max"]}"' in scale.group(0)
    endpoints = {
        bound: value.strip()
        for bound, value in re.findall(r'data-bound="(min|max)"[^>]*>([^<]*)<', text)
    }
    assert endpoints == {
        "min": str(bundle["color_domain"]["min"]),
        "max": str(bundle["color_domain"]["max"]),
    }


# -- F13-2 / F13-3 ----------------------------------------------------------
@pytest.mark.parametrize("source", sorted(EXPECTED_FRAMES))
def test_trend_chart_is_prerendered_frame_by_frame(source):
    page = _parse(SITE / source / "player.html")
    n_frames = EXPECTED_FRAMES[source]
    bars = page.by_class("trend-bar")
    points = page.by_class("trend-point")
    assert len(points) == n_frames
    assert len(bars) == n_frames * len(STRATEGIES)

    rule = _steps(source, "rule")
    for strategy in STRATEGIES:
        steps = _steps(source, strategy)
        strategy_bars = [attrs for attrs in bars if attrs.get("data-strategy") == strategy]
        assert len(strategy_bars) == n_frames
        assert sorted(int(attrs["data-frame-index"]) for attrs in strategy_bars) == list(range(n_frames))
        for attrs in strategy_bars:
            index = int(attrs["data-frame-index"])
            assert int(attrs["data-value"]) == int(steps.iloc[index]["bikes_moved"])
            assert int(attrs["data-seek"]) == index
            assert attrs["data-source"] == f"outputs/sim_{strategy}.{source}.csv"

    assert sorted(int(attrs["data-frame-index"]) for attrs in points) == list(range(n_frames))
    for attrs in points:
        index = int(attrs["data-frame-index"])
        assert int(attrs["data-value"]) == int(rule.iloc[index]["unmet_requests"])
        assert int(attrs["data-seek"]) == index
        assert attrs["data-source"] == f"outputs/sim_rule.{source}.csv"

    seeks = {int(attrs["data-seek"]) for attrs in bars + points}
    assert seeks == set(range(n_frames))


# -- F13-4 ------------------------------------------------------------------
@pytest.mark.parametrize("source", sorted(EXPECTED_FRAMES))
def test_first_frame_detail_is_traceable(source):
    page = _parse(SITE / source / "player.html")
    first_bucket = pd.Timestamp(_bundle(source)["frames"][0]["time_bucket"])
    for strategy in STRATEGIES:
        source_file = f"outputs/sim_moves.{strategy}.{source}.json"
        expected = sorted(
            (str(item["from_cell"]), str(item["to_cell"]), int(item["bikes"]))
            for item in _moves(source, strategy)
            if pd.Timestamp(item["time_bucket"]) == first_bucket
        )
        rows = [
            attrs
            for attrs in page.by_class("move-row")
            if attrs.get("data-source") == source_file
        ]
        assert len(rows) == len(expected)
        assert sorted((attrs["data-from"], attrs["data-to"], int(attrs["data-bikes"])) for attrs in rows) == expected


# -- F13-5 ------------------------------------------------------------------
@pytest.mark.parametrize("source", sorted(EXPECTED_FRAMES))
def test_sparse_note_matches_the_frame_pages(source):
    text = (SITE / source / "player.html").read_text(encoding="utf-8")
    bundle = _bundle(source)
    for strategy in STRATEGIES:
        rate = float(
            json.loads((Layout().sim_path(source, strategy, "json")).read_text(encoding="utf-8"))["service_rate"]
        )
        sparse = "搬运稀疏" in text
        if rate > 0.99:
            assert sparse, f"{source}/{strategy} service rate {rate:.4f} requires the sparse note"
            assert f"{rate * 100:.2f}%" in text
        elif rate < 0.95:
            # XM is dense: the page must not claim sparseness at all.
            assert not sparse
        assert len(bundle["frames"]) == EXPECTED_FRAMES[source]


# -- F13-6 ------------------------------------------------------------------
@pytest.mark.parametrize("source", sorted(EXPECTED_FRAMES))
def test_trend_series_have_units_and_known_sources(source):
    page = _parse(SITE / source / "player.html")
    known = {
        entry["source_file"]
        for entry in json.loads((ROOT / "outputs" / "key_numbers.json").read_text(encoding="utf-8"))
    }
    series = page.by_attr("data-series")
    names = {attrs["data-series"] for attrs in series}
    assert {"bikes_moved", "unmet_requests"} <= names
    for attrs in series:
        assert attrs.get("data-unit")
        assert attrs["data-source"] in known


# -- NF-15 / NF-18 / NF-19 --------------------------------------------------
def test_new_pages_stay_within_the_size_budget():
    total = sum(path.stat().st_size for path in SITE.rglob("*") if path.is_file())
    assert total < 6 * 1024 * 1024, f"replay/ is {total / 1024 / 1024:.2f} MB"
    for source in EXPECTED_FRAMES:
        assert (SITE / source / "replay-data.js").stat().st_size <= 1024 * 1024
        assert (SITE / source / "player.html").stat().st_size <= 256 * 1024
    assert (SITE / "dashboard.html").stat().st_size <= 512 * 1024
    for page in SITE.glob("*/frame-*.html"):
        assert page.stat().st_size < 96 * 1024


def test_every_metric_on_the_player_page_is_traceable():
    known = {
        entry["source_file"]
        for entry in json.loads((ROOT / "outputs" / "key_numbers.json").read_text(encoding="utf-8"))
    }
    checked = 0
    for source in EXPECTED_FRAMES:
        page = _parse(SITE / source / "player.html")
        for attrs in page.by_class("metric"):
            assert attrs.get("data-source"), (source, attrs.get("data-metric"))
            assert (ROOT / attrs["data-source"]).exists(), attrs["data-source"]
            assert attrs["data-source"] in known, attrs["data-source"]
            checked += 1
        for _, attrs in page.elements:
            if "data-source" in attrs:
                assert (ROOT / attrs["data-source"]).exists(), attrs["data-source"]
    assert checked > 0


def test_no_build_step_and_no_es_modules():
    for path in SITE.rglob("*"):
        if path.suffix not in {".html", ".css", ".js"}:
            continue
        text = path.read_text(encoding="utf-8")
        assert 'type="module"' not in text, path
        if path.suffix == ".js":
            assert not re.search(r"^\s*(?:import|export)\s", text, re.MULTILINE), path
    assert not (SITE / "package.json").exists()
    assert not (SITE / "node_modules").exists()
