"""F10: the replay site must be a faithful, offline, traceable view of the artefacts.

Every assertion here reads the shipped page (its DOM and the frame data it
renders from) and compares it against ``outputs/``.  No expectation is copied by
hand, and no check samples a few frames -- each one walks every page.
"""

from __future__ import annotations

import json
import re
from html.parser import HTMLParser
from pathlib import Path

import pandas as pd
import pytest

from bike_agent.config import Layout, load_config

ROOT = Path(__file__).resolve().parents[1]
SITE = ROOT / "replay"
EXPECTED_FRAMES = {"xm": 4, "sh": 149}


class Page(HTMLParser):
    """Collect the parts of a rendered page the acceptance criteria talk about."""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.elements: list[tuple[str, dict[str, str]]] = []
        self.frame_data: dict | None = None
        self.notes: list[tuple[dict[str, str], str]] = []
        self._json: list[str] = []
        self._in_json = False
        self._note_attrs: dict[str, str] | None = None
        self._note_text: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        values = {key: (value or "") for key, value in attrs}
        self.elements.append((tag, values))
        if tag == "script" and values.get("id") == "frame-data":
            self._in_json = True
        if tag == "p" and "note" in values.get("class", "").split():
            self._note_attrs, self._note_text = values, []

    def handle_data(self, data: str) -> None:
        if self._in_json:
            self._json.append(data)
        if self._note_attrs is not None:
            self._note_text.append(data)

    def handle_endtag(self, tag: str) -> None:
        if tag == "script" and self._in_json:
            self._in_json = False
            self.frame_data = json.loads("".join(self._json))
        if tag == "p" and self._note_attrs is not None:
            self.notes.append((self._note_attrs, "".join(self._note_text)))
            self._note_attrs, self._note_text = None, []

    # -- views -------------------------------------------------------------
    def by_class(self, name: str) -> list[dict[str, str]]:
        return [attrs for _, attrs in self.elements if name in attrs.get("class", "").split()]

    def metrics(self) -> list[dict[str, str]]:
        return self.by_class("metric")

    def arrows(self) -> list[dict[str, str]]:
        return self.by_class("arrow")


def _parse(path: Path) -> Page:
    page = Page()
    page.feed(path.read_text(encoding="utf-8"))
    page.close()
    return page


def _pages() -> list[Path]:
    if not SITE.exists():
        return []
    return sorted(SITE.glob("*/frame-*.html"))


PAGES = _pages()


@pytest.fixture(scope="module")
def parsed() -> dict[Path, Page]:
    return {path: _parse(path) for path in PAGES}


@pytest.fixture(scope="module")
def artifacts() -> dict:
    layout, config = Layout(), load_config()
    data = {"steps": {}, "states": {}, "moves": {}, "summaries": {}, "n_cells": {}}
    for source in config.get("sources", {}):
        for strategy in ("rule", "optimized"):
            key = (source, strategy)
            steps = pd.read_csv(layout.sim_path(source, strategy, "csv"))
            steps["time_bucket"] = pd.to_datetime(steps["time_bucket"])
            data["steps"][key] = steps.sort_values("time_bucket").reset_index(drop=True)
            states = pd.read_parquet(layout.replay_path(source, strategy))
            states["time_bucket"] = pd.to_datetime(states["time_bucket"])
            data["states"][key] = states
            data["moves"][key] = json.loads(
                (layout.outputs / f"sim_moves.{strategy}.{source}.json").read_text(encoding="utf-8")
            )
            data["summaries"][key] = json.loads(layout.sim_path(source, strategy, "json").read_text(encoding="utf-8"))
            data["n_cells"][source] = int(states["cell_id"].nunique())
    return data


# Every test here reads the shipped pages, so the whole module skips together.
pytestmark = pytest.mark.skipif(not PAGES, reason="replay/ not built; run `bike-agent repro` first")


# -- F10-1 ------------------------------------------------------------------
@pytest.mark.parametrize("source", sorted(EXPECTED_FRAMES))
def test_frame_count_equals_product_frame_count(source, artifacts):
    """No interpolation: one page per persisted bucket, and the counts are 4 / 149."""
    steps = artifacts["steps"][(source, "rule")]
    pages = sorted(SITE.glob(f"{source}/frame-*.html"))
    assert len(pages) == len(steps) == EXPECTED_FRAMES[source]
    # Contiguous numbering, no gaps that could hide a dropped or added frame.
    assert [p.name for p in pages] == [f"frame-{i:04d}.html" for i in range(len(steps))]

    for page in pages:
        data = _parse(page).frame_data
        assert data["n_frames"] == EXPECTED_FRAMES[source]


# -- F10-2 ------------------------------------------------------------------
def test_every_frame_shows_the_artifact_values(parsed, artifacts):
    """Every page, both strategies: grid values and KPIs equal the artefacts."""
    csv_fields = (
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
    )
    for path, page in parsed.items():
        data = page.frame_data
        source = data["source"]
        bucket = pd.Timestamp(data["time_bucket"])

        # Page-level numbers: frame position, frame count and the bucket itself.
        steps_rule = artifacts["steps"][(source, "rule")]
        position = int(path.stem.split("-")[1])
        assert position == data["index"]
        for metric in page.metrics():
            name = metric.get("data-metric", "")
            if name == "frame_index":
                assert int(metric["data-value"]) == position + 1
            elif name == "frame_count":
                assert int(metric["data-value"]) == len(steps_rule) == EXPECTED_FRAMES[source]
            elif name == "time_bucket":
                assert pd.Timestamp(metric["data-value"]) == bucket
                assert bucket == steps_rule["time_bucket"].iloc[position]

        for strategy in ("rule", "optimized"):
            key = (source, strategy)

            # Grid: the numbers the page draws, cell by cell.
            states = artifacts["states"][key]
            rows = states.loc[states["time_bucket"] == bucket].set_index("cell_id")
            values = data["strategies"][strategy]["values"]
            assert len(values) == len(data["ids"]) == len(rows) == artifacts["n_cells"][source]
            for cell_id, triple in zip(data["ids"], values):
                record = rows.loc[cell_id]
                assert triple == [
                    int(record["inventory_end"]),
                    int(record["unmet_requests"]),
                    int(record["bikes_moved"]),
                ], f"{path.name} {strategy} {cell_id}"

            # KPIs: what the page prints, not what a helper recomputed.  The
            # header metrics belong to the page, not to one strategy, so the
            # strategy is read back from the declared source.
            for metric in page.metrics():
                name = metric.get("data-metric", "")
                src = metric.get("data-source", "")
                if name in csv_fields:
                    match = re.fullmatch(r"outputs/sim_(rule|optimized)\.([a-z]+)\.csv", src)
                    assert match, f"{path.name}: unexpected metric source {src!r}"
                    metric_strategy, metric_source = match.group(1), match.group(2)
                    assert metric_source == source
                    steps = artifacts["steps"][(source, metric_strategy)]
                    row = steps.loc[steps["time_bucket"] == bucket].iloc[0]
                    assert float(metric["data-value"]) == float(row[name]), f"{path.name} {metric_strategy} {name}"
                elif name == "n_moves":
                    match = re.fullmatch(r"outputs/sim_moves\.(rule|optimized)\.([a-z]+)\.json", src)
                    assert match, f"{path.name}: unexpected metric source {src!r}"
                    expected = [
                        m for m in artifacts["moves"][(source, match.group(1))]
                        if pd.Timestamp(m["time_bucket"]) == bucket
                    ]
                    assert int(metric["data-value"]) == len(expected)


# -- F10-3 ------------------------------------------------------------------
def test_every_arrow_matches_a_move_record(parsed, artifacts):
    """Arrows, counts and sums all have to line up with sim_moves.*.json."""
    for path, page in parsed.items():
        data = page.frame_data
        source = data["source"]
        bucket = pd.Timestamp(data["time_bucket"])
        arrows = page.arrows()
        for strategy in ("rule", "optimized"):
            section_arrows = [a for a in arrows if a["data-source"] == f"outputs/sim_moves.{strategy}.{source}.json"]
            expected = [m for m in artifacts["moves"][(source, strategy)] if pd.Timestamp(m["time_bucket"]) == bucket]

            rendered = sorted((a["data-from"], a["data-to"], int(a["data-bikes"])) for a in section_arrows)
            recorded = sorted((str(m["from_cell"]), str(m["to_cell"]), int(m["bikes"])) for m in expected)
            assert rendered == recorded, f"{path.name} {strategy} arrows differ from the move records"
            assert sum(item[2] for item in rendered) == sum(item[2] for item in recorded)


# -- F10-4 ------------------------------------------------------------------
def test_buckets_without_moves_render_no_arrows(parsed, artifacts):
    """The anti-synthesis line: no move record means no arrow on screen."""
    empty_frames = 0
    for path, page in parsed.items():
        data = page.frame_data
        source = data["source"]
        bucket = pd.Timestamp(data["time_bucket"])
        for strategy in ("rule", "optimized"):
            expected = [m for m in artifacts["moves"][(source, strategy)] if pd.Timestamp(m["time_bucket"]) == bucket]
            rendered = [a for a in page.arrows() if a["data-source"] == f"outputs/sim_moves.{strategy}.{source}.json"]
            if not expected:
                empty_frames += 1
                assert rendered == [], f"{path.name} {strategy} drew {len(rendered)} arrows with no move record"
            else:
                assert rendered, f"{path.name} {strategy} dropped its {len(expected)} arrows"
    assert empty_frames > 0, "fixture is not exercising the no-move case"


# -- F10-5 ------------------------------------------------------------------
def test_every_metric_is_traceable_to_a_key_number_source(parsed):
    """Each metric carries data-source; the file exists and key_numbers knows it."""
    known = {
        entry["source_file"]
        for entry in json.loads((ROOT / "outputs" / "key_numbers.json").read_text(encoding="utf-8"))
    }
    checked = 0
    for path, page in parsed.items():
        for metric in page.metrics():
            source_file = metric.get("data-source")
            assert source_file, f"{path.name}: metric {metric.get('data-metric')!r} has no data-source"
            assert (ROOT / source_file).exists(), f"{path.name}: {source_file} does not exist"
            assert source_file in known, f"{path.name}: {source_file} is absent from key_numbers.json source_file set"
            checked += 1
        for tag, attrs in page.elements:
            if "data-source" in attrs:
                assert (ROOT / attrs["data-source"]).exists(), f"{path.name}: {attrs['data-source']} does not exist"
    assert checked > 0

    index = _parse(SITE / "index.html")
    for metric in index.metrics():
        assert metric["data-source"] in known


# -- F10-6 ------------------------------------------------------------------
def test_sparse_note_is_computed_from_the_service_rate(parsed, artifacts):
    """>99% must explain the sparseness with the real rate; <95% must not."""
    seen_sparse, seen_silent = set(), set()
    for path, page in parsed.items():
        data = page.frame_data
        source = data["source"]
        notes = page.notes
        for strategy in ("rule", "optimized"):
            rate = float(artifacts["summaries"][(source, strategy)]["service_rate"])
            sparse_notes = [text for _, text in notes if "搬运稀疏" in text]
            rendered_rate = [text for _, text in notes if f"{rate * 100:.2f}%" in text]

            if rate > 0.99:
                assert sparse_notes, f"{path.name} {strategy}: service rate {rate:.4f} needs a sparseness note"
                assert rendered_rate, f"{path.name} {strategy}: the note must quote the real rate"
                seen_sparse.add((source, strategy))
            elif rate < 0.95:
                assert not sparse_notes, f"{path.name} {strategy}: rate {rate:.4f} must not claim sparseness"
                seen_silent.add((source, strategy))

    assert seen_sparse, "no >99% source exercised the sparseness note"
    assert seen_silent, "no <95% source exercised the absence of the note"


# -- F10-9 ------------------------------------------------------------------
_EXTERNAL = re.compile(
    r"""(?:src|href)\s*=\s*["'](?:(?:https?:)?//)[^"']+["']|@import\s+(?:url\()?["']?(?:https?:)?//|url\(\s*["']?(?:https?:)?//""",
    re.IGNORECASE,
)


def test_pages_are_offline():
    """No external hosts anywhere in the shipped site (F10-9)."""
    offenders = []
    for path in SITE.rglob("*"):
        if path.suffix not in {".html", ".css", ".js"}:
            continue
        text = path.read_text(encoding="utf-8")
        for match in _EXTERNAL.finditer(text):
            offenders.append(f"{path.relative_to(ROOT)}: {match.group(0)[:80]}")
    assert not offenders, "external references found:\n" + "\n".join(offenders)


def test_local_assets_the_pages_reference_exist():
    for path in SITE.glob("*/frame-*.html"):
        text = path.read_text(encoding="utf-8")
        for asset in re.findall(r'(?:src|href)="([^"]+)"', text):
            if asset.startswith(("http://", "https://", "//")):
                continue
            target = asset.split("#", 1)[0]
            if not target:
                continue
            assert (path.parent / target).resolve().exists(), f"{path.name} references missing {asset}"


# -- NF-13 ------------------------------------------------------------------
def test_site_stays_small_and_frames_are_per_page():
    """On-demand frames, not one bundle: bounded total size, bounded page size."""
    pages = _pages()
    total = sum(p.stat().st_size for p in SITE.rglob("*") if p.is_file())
    largest = max(pages, key=lambda p: p.stat().st_size)
    assert total < 6 * 1024 * 1024, f"replay/ is {total / 1024 / 1024:.2f} MB"
    assert largest.stat().st_size < 96 * 1024, f"{largest.name} is {largest.stat().st_size / 1024:.0f} KB"
    # Each page carries exactly one frame's data.
    for path in pages:
        data = _parse(path).frame_data
        assert data["index"] == int(path.stem.split("-")[1])


# -- F10-6 honesty ----------------------------------------------------------
def test_sparseness_is_computed_not_asserted(parsed, artifacts):
    """The 'how sparse' clause must come from the per-bucket move counts."""
    from bike_agent.replay.site import SINGLE_DIGIT_MOVES

    # Compute the expected share once per (source, strategy), not once per page.
    expected: dict[tuple[str, str], float] = {}
    for (source, strategy), steps in artifacts["steps"].items():
        counts = [
            len([m for m in artifacts["moves"][(source, strategy)] if pd.Timestamp(m["time_bucket"]) == b])
            for b in steps["time_bucket"]
        ]
        expected[(source, strategy)] = sum(1 for c in counts if c <= SINGLE_DIGIT_MOVES) / len(counts)

    checked = 0
    for path, page in parsed.items():
        source = page.frame_data["source"]
        for attrs, text in page.notes:
            if "搬运稀疏" not in text:
                continue
            strategy = re.search(r"outputs/sim_(rule|optimized)\.", attrs["data-source"]).group(1)
            share = float(attrs["data-sparse-share"])
            assert abs(share - expected[(source, strategy)]) < 1e-12, f"{path.name}: {share} != {expected[(source, strategy)]}"
            assert f"{share * 100:.1f}%" in text
            checked += 1
    assert checked, "no sparse note found to check"


# -- F10-5 precision --------------------------------------------------------
def test_derived_metrics_really_derive_from_their_cited_file(parsed):
    """A `data-derived` claim must be reproducible from the file it cites.

    ``frame_index`` and ``frame_count`` are not literal cells in the step CSV,
    so the page declares *how* they derive from it.  This checks the derivation
    rather than accepting the attribute at face value.
    """
    checked = 0
    for path, page in parsed.items():
        source = page.frame_data["source"]
        for metric in page.metrics():
            derived = metric.get("data-derived")
            if not derived:
                continue
            cited = ROOT / metric["data-source"]
            rows = pd.read_csv(cited)
            if derived == "row-count":
                assert int(metric["data-value"]) == len(rows)
            elif derived == "row-position":
                position = int(metric["data-value"])
                assert 1 <= position <= len(rows)
                assert str(rows["time_bucket"].iloc[position - 1]) == page.frame_data["time_bucket"]
            else:
                pytest.fail(f"unknown data-derived {derived!r}")
            checked += 1
    assert checked, "no derived metrics found"
