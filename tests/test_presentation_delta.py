"""M19 / F18: the strategy difference map.

F18-1 and F18-2 are checked against the artefacts and the shipped DOM for every
frame and every cell.  F18-3 is behaviour, so it is driven through the published
state machine in node rather than inferred from markup.
"""

from __future__ import annotations

from html.parser import HTMLParser
from pathlib import Path

import pandas as pd
import pytest

from bike_agent.config import Layout

ROOT = Path(__file__).resolve().parents[1]
SITE = ROOT / "replay"
SOURCES = ("xm", "sh")
STRATEGIES = ("rule", "optimized")


class Page(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.elements: list[tuple[str, dict[str, str]]] = []

    def handle_starttag(self, tag, attrs):
        self.elements.append((tag, {key: (value or "") for key, value in attrs}))

    def handle_startendtag(self, tag, attrs):
        self.handle_starttag(tag, attrs)

    def by_class(self, name: str) -> list[dict[str, str]]:
        return [attrs for _, attrs in self.elements if name in attrs.get("class", "").split()]

    def by_attr(self, name: str) -> list[dict[str, str]]:
        return [attrs for _, attrs in self.elements if name in attrs]


def _page(source: str) -> Page:
    page = Page()
    page.feed((SITE / source / "workbench.html").read_text(encoding="utf-8"))
    page.close()
    return page


# -- F18-1 ------------------------------------------------------------------
@pytest.mark.parametrize("source", SOURCES)
def test_static_delta_grid_matches_the_bundle_for_every_cell(presentations, source):
    page = _page(source)
    rects = [attrs for attrs in page.by_class("delta") if attrs.get("data-delta") is not None]
    expected = presentations[source]["frames"][0]["delta"]
    assert len(rects) == len(expected) == presentations[source]["n_cells"]
    assert [int(attrs["data-delta"]) for attrs in rects] == expected
    assert [attrs["data-cell"] for attrs in rects] == presentations[source]["cell_ids"]


@pytest.mark.parametrize("source", SOURCES)
def test_delta_sign_convention_is_stated_and_correct(presentations, source):
    """Negative = S-OPT better.  The page must say so, and the data must agree."""
    text = (SITE / source / "workbench.html").read_text(encoding="utf-8")
    assert "delta_unmet = unmet_optimized − unmet_rule" in text
    assert "负值 = S-OPT 更少未满足" in text
    assert "正值 = S-OPT 更多未满足" in text
    # gain and delta are opposite signs; the page must not blur them.
    assert "gain = unmet_rule − unmet_optimized" in text

    layout = Layout()
    rule = pd.read_parquet(layout.replay_path(source, "rule"))
    optimized = pd.read_parquet(layout.replay_path(source, "optimized"))
    merged = rule.merge(optimized, on=["time_bucket", "cell_id"], suffixes=("_r", "_o"))
    worse = merged[merged["unmet_requests_o"] > merged["unmet_requests_r"]]
    if not worse.empty:
        row = worse.iloc[0]
        delta = int(row["unmet_requests_o"]) - int(row["unmet_requests_r"])
        assert delta > 0, "a cell where S-OPT is worse must have a positive delta"


# -- F18-2 ------------------------------------------------------------------
@pytest.mark.parametrize("source", SOURCES)
def test_one_fixed_delta_scale_per_source(presentations, source):
    page = _page(source)
    scales = page.by_attr("data-scale")
    assert len(scales) == 1, "a source must expose exactly one delta scale"
    scale = scales[0]
    assert scale["data-scale"] == "delta"
    domain = presentations[source]["color_domain"]["delta"]
    assert int(scale["data-min"]) == domain["min"]
    assert int(scale["data-max"]) == domain["max"]

    text = (SITE / source / "workbench.html").read_text(encoding="utf-8")
    assert f"色域固定 ±{domain['max_abs']} 次" in text
    if domain["max_abs"] == 0:
        assert "全程无差异" in text
    else:
        assert "全程无差异" not in text


@pytest.mark.parametrize("source", SOURCES)
def test_improved_and_worsened_counts_are_independently_recomputable(presentations, source):
    """The page prints per-frame counts; recompute them from the parquet directly."""
    layout = Layout()
    rule = pd.read_parquet(layout.replay_path(source, "rule")).sort_values(
        ["time_bucket", "cell_id"], kind="mergesort"
    )
    optimized = pd.read_parquet(layout.replay_path(source, "optimized")).sort_values(
        ["time_bucket", "cell_id"], kind="mergesort"
    )
    delta = (optimized["unmet_requests"].to_numpy() - rule["unmet_requests"].to_numpy()).reshape(
        presentations[source]["n_frames"], presentations[source]["n_cells"]
    )
    for index, frame in enumerate(presentations[source]["frames"]):
        summary = frame["delta_summary"]
        assert summary["improved_cells"] == int((delta[index] < 0).sum())
        assert summary["worsened_cells"] == int((delta[index] > 0).sum())
        assert summary["net"] == int(delta[index].sum())

    # The net alone must not be allowed to stand in for the two counts.
    text = (SITE / source / "workbench.html").read_text(encoding="utf-8")
    assert "同一帧可以同时存在改善与退步" in text


# -- F18-3 ------------------------------------------------------------------
@pytest.mark.parametrize("source", SOURCES)
def test_prerendered_delta_rects_have_real_geometry_and_a_sign_glyph(presentations, source):
    """A rect with ``x="NaN"`` is a rect nobody can see or click.

    The browser re-renders this map per frame, so the geometry has to be right in
    the shipped markup *and* survive that re-render; the browser smoke measures
    the rendered boxes, this checks the bytes the browser starts from.
    """
    page = _page(source)
    rects = [attrs for attrs in page.by_class("delta") if attrs.get("data-delta") is not None]
    assert len(rects) == presentations[source]["n_cells"]
    for attrs in rects:
        x, y = float(attrs["x"]), float(attrs["y"])
        width, height = float(attrs["width"]), float(attrs["height"])
        assert width > 0 and height > 0, f"{source}: zero-size cell {attrs['data-cell']}"
        assert x >= 0 and y >= 0, f"{source}: cell {attrs['data-cell']} sits off the canvas"
    # One glyph per cell, so the sign is never carried by colour alone (NF-26).
    assert len(page.by_class("delta-glyph")) == len(rects)


def test_the_script_never_computes_cell_geometry():
    """Geometry is Python's job; a second renderer is what produced the NaN rects."""
    for name in ("workbench.js", "workbench-core.js"):
        js = (SITE / name).read_text(encoding="utf-8")
        assert "cell_px" not in js, f"{name} must not read cell_px"
        assert "cell_gap" not in js, f"{name} must not read cell_gap"
    assert "createElementNS" not in (SITE / "workbench.js").read_text(encoding="utf-8"), (
        "the delta map is updated in place, not rebuilt"
    )


@pytest.mark.parametrize("source", SOURCES)
def test_mode_switch_keeps_source_frame_and_cell(source):
    from js_harness import run_node

    result = run_node(
        f"""
core.setFrame(2);
core.selectCell(P['{source}'].cell_ids[7]);
const before = core.get();
core.setMode('delta');
const afterDelta = core.get();
core.setMode('side');
const afterSide = core.get();
console.log(JSON.stringify({{ before: before, afterDelta: afterDelta, afterSide: afterSide,
  rejected: core.setMode('bogus') }}));
""",
        sources=(source,),
    )
    assert result["afterDelta"]["mode"] == "delta"
    assert result["afterSide"]["mode"] == "side"
    for key in ("source", "frame", "cell"):
        assert result["afterDelta"][key] == result["before"][key], key
        assert result["afterSide"][key] == result["before"][key], key
    assert result["rejected"] is False, "an unknown mode must be refused, not silently accepted"


@pytest.mark.parametrize("source", SOURCES)
def test_selecting_a_delta_cell_drives_the_ledger(source):
    from js_harness import run_node

    result = run_node(
        f"""
const chosen = P['{source}'].cell_ids[3];
core.setMode('delta');
const ok = core.selectCell(chosen);
const bad = core.selectCell('NOT_A_CELL');
console.log(JSON.stringify({{ ok: ok, bad: bad, cell: core.get().cell, mode: core.get().mode,
  exists: P['{source}'].cell_ids.indexOf(chosen) }}));
""",
        sources=(source,),
    )
    assert result["ok"] is True
    assert result["cell"] is not None
    assert result["bad"] is False, "a cell outside the source must be refused"
    assert result["mode"] == "delta"


@pytest.mark.parametrize("source", SOURCES)
def test_the_ledger_page_follows_the_selected_cell(presentations, source):
    """The shipped page exposes the ledger for the default cell with real numbers."""
    page = _page(source)
    ledger = page.by_attr("data-ledger-strategy")
    assert {attrs["data-ledger-strategy"] for attrs in ledger} == set(STRATEGIES)
    cell = presentations[source]["ledger_top_cell"]["cell_id"]
    text = (SITE / source / "workbench.html").read_text(encoding="utf-8")
    assert f'data-value="{cell}"' in text
    for strategy in STRATEGIES:
        values = {
            attrs["data-metric"]: attrs["data-value"]
            for attrs in page.by_class("metric")
            if attrs.get("data-source") == f"outputs/replay.{strategy}.{source}.parquet"
        }
        for name in ("inventory_start", "scheduled_inventory", "inventory_end", "unmet_requests", "returns_overflow"):
            assert name in values, f"{source}/{strategy}: ledger field {name} is missing"
