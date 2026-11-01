"""M21 / F19: the per-cell inventory ledger.

F19-1 and F19-2 are conservation checks over **every** source, frame, cell and
strategy -- not the default cell.  A single cell with a wrong arrival count
would show up here, which is exactly what a spot check would miss.
"""

from __future__ import annotations

import json
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

from bike_agent.config import Layout

ROOT = Path(__file__).resolve().parents[1]
SITE = ROOT / "replay"
SOURCES = ("xm", "sh")
STRATEGIES = ("rule", "optimized")
LEDGER_FIELDS = ("start", "req", "met", "ret", "rapp", "out")


def _state(layout: Layout, source: str, strategy: str, n_frames: int, n_cells: int) -> dict[str, np.ndarray]:
    frame = pd.read_parquet(layout.replay_path(source, strategy))
    frame["time_bucket"] = pd.to_datetime(frame["time_bucket"])
    frame = frame.sort_values(["time_bucket", "cell_id"], kind="mergesort")
    return {
        "inventory_start": frame["inventory_start"].to_numpy(dtype=np.int64).reshape(n_frames, n_cells),
        "borrow_requests": frame["borrow_requests"].to_numpy(dtype=np.int64).reshape(n_frames, n_cells),
        "borrow_met": frame["borrow_met"].to_numpy(dtype=np.int64).reshape(n_frames, n_cells),
        "returns": frame["returns"].to_numpy(dtype=np.int64).reshape(n_frames, n_cells),
        "returns_applied": frame["returns_applied"].to_numpy(dtype=np.int64).reshape(n_frames, n_cells),
        "returns_overflow": frame["returns_overflow"].to_numpy(dtype=np.int64).reshape(n_frames, n_cells),
        "bikes_moved": frame["bikes_moved"].to_numpy(dtype=np.int64).reshape(n_frames, n_cells),
        "unmet_requests": frame["unmet_requests"].to_numpy(dtype=np.int64).reshape(n_frames, n_cells),
        "inventory_end": frame["inventory_end"].to_numpy(dtype=np.int64).reshape(n_frames, n_cells),
    }


def _moved_in(presentation: dict, strategy: str, n_frames: int, n_cells: int) -> np.ndarray:
    grid = np.zeros((n_frames, n_cells), dtype=np.int64)
    for index, frame in enumerate(presentation["frames"]):
        for cell_index, bikes in frame["strategies"][strategy]["moved_in"].items():
            grid[index, int(cell_index)] = int(bikes)
    return grid


def _bundle(presentation: dict, strategy: str) -> dict[str, np.ndarray]:
    return {
        field: np.array(
            [frame["strategies"][strategy]["ledger"][field] for frame in presentation["frames"]],
            dtype=np.int64,
        )
        for field in LEDGER_FIELDS
    }


# -- F19-1 ------------------------------------------------------------------
@pytest.mark.parametrize("source", SOURCES)
@pytest.mark.parametrize("strategy", STRATEGIES)
def test_ledger_fields_equal_the_parquet_for_every_cell(presentations, source, strategy):
    presentation = presentations[source]
    layout = Layout()
    n_frames, n_cells = presentation["n_frames"], presentation["n_cells"]
    truth = _state(layout, source, strategy, n_frames, n_cells)
    bundle = _bundle(presentation, strategy)

    mapping = {
        "start": "inventory_start",
        "req": "borrow_requests",
        "met": "borrow_met",
        "ret": "returns",
        "rapp": "returns_applied",
        "out": "bikes_moved",
    }
    for short, column in mapping.items():
        assert np.array_equal(bundle[short], truth[column]), f"{source}/{strategy}: {short} != {column}"


@pytest.mark.parametrize("source", SOURCES)
@pytest.mark.parametrize("strategy", STRATEGIES)
def test_all_four_equations_hold_on_every_cell(presentations, source, strategy):
    presentation = presentations[source]
    layout = Layout()
    n_frames, n_cells = presentation["n_frames"], presentation["n_cells"]
    truth = _state(layout, source, strategy, n_frames, n_cells)
    incoming = _moved_in(presentation, strategy, n_frames, n_cells)
    bundle = _bundle(presentation, strategy)

    scheduled = bundle["start"] + incoming - bundle["out"]
    assert scheduled.shape == truth["inventory_start"].shape

    # 期末 = 期初 + 调入 - 调出 + 接纳归还 - 满足借车
    end = scheduled + bundle["rapp"] - bundle["met"]
    assert np.array_equal(end, truth["inventory_end"]), f"{source}/{strategy}: inventory_end equation"
    # The bundle's own derived reading must agree with the parquet too.
    assert np.array_equal(bundle["req"] - bundle["met"], truth["unmet_requests"])
    assert np.array_equal(bundle["ret"] - bundle["rapp"], truth["returns_overflow"])
    # Scheduled inventory is never negative: the engine cannot move more than it has.
    assert (scheduled >= 0).all()


@pytest.mark.parametrize("source", SOURCES)
@pytest.mark.parametrize("strategy", STRATEGIES)
def test_moved_out_is_only_the_outgoing_side(presentations, source, strategy):
    """``bikes_moved`` is the outflow; treating it as arrivals would double-count."""
    presentation = presentations[source]
    n_frames, n_cells = presentation["n_frames"], presentation["n_cells"]
    incoming = _moved_in(presentation, strategy, n_frames, n_cells)
    bundle = _bundle(presentation, strategy)
    assert np.array_equal(bundle["out"], _state(Layout(), source, strategy, n_frames, n_cells)["bikes_moved"])
    assert not np.array_equal(incoming, bundle["out"]), (
        f"{source}/{strategy}: arrivals and departures happen to be equal, so this test cannot "
        "tell them apart -- check the dispatch records"
    )


# -- F19-2 ------------------------------------------------------------------
@pytest.mark.parametrize("source", SOURCES)
@pytest.mark.parametrize("strategy", STRATEGIES)
def test_inflow_equals_outflow_equals_the_dispatch_records(presentations, source, strategy):
    layout = Layout()
    presentation = presentations[source]
    n_frames, n_cells = presentation["n_frames"], presentation["n_cells"]
    incoming = _moved_in(presentation, strategy, n_frames, n_cells)
    bundle = _bundle(presentation, strategy)

    records = json.loads(
        (layout.outputs / f"sim_moves.{strategy}.{source}.json").read_text(encoding="utf-8")
    )
    per_bucket = np.zeros(n_frames, dtype=np.int64)
    buckets = presentation["buckets"]
    position = {bucket: index for index, bucket in enumerate(buckets)}
    for record in records:
        bucket = pd.Timestamp(record["time_bucket"]).strftime("%Y-%m-%d %H:%M:%S")
        per_bucket[position[bucket]] += int(record["bikes"])

    assert np.array_equal(incoming.sum(axis=1), per_bucket), f"{source}/{strategy}: arrivals != dispatch total"
    assert np.array_equal(bundle["out"].sum(axis=1), per_bucket), f"{source}/{strategy}: departures != dispatch total"
    assert np.array_equal(incoming.sum(axis=1), bundle["out"].sum(axis=1))

    steps = pd.read_csv(layout.sim_path(source, strategy, "csv"))
    steps["time_bucket"] = pd.to_datetime(steps["time_bucket"])
    steps = steps.sort_values("time_bucket").reset_index(drop=True)
    assert np.array_equal(steps["bikes_moved"].to_numpy(dtype=np.int64), per_bucket)


@pytest.mark.parametrize("source", SOURCES)
@pytest.mark.parametrize("strategy", STRATEGIES)
def test_per_cell_departures_equal_bikes_moved(presentations, source, strategy):
    presentation = presentations[source]
    n_frames, n_cells = presentation["n_frames"], presentation["n_cells"]
    truth = _state(Layout(), source, strategy, n_frames, n_cells)
    bundle = _bundle(presentation, strategy)
    assert np.array_equal(bundle["out"], truth["bikes_moved"])
    # And the incoming side is *not* the same array (one move touches two cells).
    incoming = _moved_in(presentations[source], strategy, n_frames, n_cells)
    assert incoming.sum() == bundle["out"].sum()


# -- F19-3 ------------------------------------------------------------------
@pytest.mark.parametrize("source", SOURCES)
def test_default_cell_is_the_highest_cumulative_rule_unmet(presentations, source):
    presentation = presentations[source]
    n_frames, n_cells = presentation["n_frames"], presentation["n_cells"]
    truth = _state(Layout(), source, "rule", n_frames, n_cells)
    cumulative = truth["unmet_requests"].sum(axis=0)
    best = int(cumulative.argmax())
    expected = presentation["cell_ids"][best]
    # Ties break on cell_id ascending, which is the sorted array order.
    tied = [presentation["cell_ids"][i] for i in np.flatnonzero(cumulative == cumulative[best])]
    assert expected == min(tied)
    assert presentation["ledger_top_cell"]["cell_id"] == expected
    assert presentation["ledger_top_cell"]["cumulative_unmet_rule"] == int(cumulative[best])
    assert presentation["ledger_top_cell"]["rule"] == "累计 S-RULE 未满足请求最高；平手按 cell_id 升序"


@pytest.mark.parametrize("source", SOURCES)
def test_displayed_values_recompute_from_their_own_operands(presentations, source):
    """The page's own evidence trail has to reproduce the page's own numbers.

    ``data-operands`` was baked for one frame while the values were rewritten for
    another, so a reader who followed the page's instructions got a different
    number than the one on screen.  This recomputes each derived value from the
    operands attached to that very element.
    """
    from html.parser import HTMLParser

    class Metrics(HTMLParser):
        def __init__(self) -> None:
            super().__init__(convert_charrefs=True)
            self.nodes: list[dict[str, str]] = []
            self._pane: str | None = None

        def handle_starttag(self, tag, attrs):
            values = {k: (v or "") for k, v in attrs}
            if "data-ledger-strategy" in values:
                self._pane = values["data-ledger-strategy"]
            if "data-metric" in values and self._pane is not None:
                values["_strategy"] = self._pane
                self.nodes.append(values)

        handle_startendtag = handle_starttag

    raw = (SITE / source / "workbench.html").read_text(encoding="utf-8")
    # Scope the parse to the ledger section: the scenario table and the event
    # list reuse the same metric names, so a page-wide index would collide.
    raw = raw.split('<section class="card wide ledger-card" id="ledger"', 1)[1].split("</section>", 1)[0]
    page = Metrics()
    page.feed(raw)
    page.close()
    index = {(node["_strategy"], node["data-metric"]): node for node in page.nodes}
    bucket = presentations[source]["frames"][0]["time_bucket"]

    def recompute(name: str, ops: dict[str, int]) -> int:
        if name == "scheduled_inventory":
            return ops["inventory_start"] + ops["moved_in"] - ops["moved_out"]
        if name == "returns_overflow":
            return ops["returns"] - ops["returns_applied"]
        if name == "unmet_requests":
            return ops["borrow_requests"] - ops["borrow_met"]
        if name == "inventory_end":
            return (
                ops["inventory_start"]
                + ops["moved_in"]
                - ops["moved_out"]
                + ops["returns_applied"]
                - ops["borrow_met"]
            )
        raise AssertionError(name)

    for strategy in STRATEGIES:
        cell = index[(strategy, "inventory_start")]["data-join-key"].split("cell_id=")[1].split(";")[0]
        expected_join = f"cell_id={cell};time_bucket={bucket}"
        for name in ("scheduled_inventory", "returns_overflow", "unmet_requests", "inventory_end"):
            node = index[(strategy, name)]
            assert "data-operands" in node, f"{source}/{strategy}/{name} lost its operands"
            ops = json.loads(node["data-operands"])
            assert int(node["data-value"]) == recompute(name, ops), (
                f"{source}/{strategy}/{name}: {node['data-value']} != its own operands {ops}"
            )
            assert node["data-join-key"] == expected_join, (
                f"{source}/{strategy}/{name}: join key does not name the frame on screen"
            )
    # The static page opens on frame 1, so the ledger must show frame 1 too.
    assert presentations[source]["frames"][0]["time_bucket"] in (
        SITE / source / "workbench.html"
    ).read_text(encoding="utf-8").split('id="bucket-label"', 1)[1][:200]


@pytest.mark.parametrize("source", SOURCES)
def test_ledger_tracks_cell_frame_strategy_and_source(presentations, source):
    from js_harness import run_node

    result = run_node(
        f"""
const data = P['{source}'];
const out = {{}};
out.defaultCell = data.ledger_top_cell.cell_id;
core.setFrame(3);
out.frameAfterSet = core.get().frame;
out.selectOk = core.selectCell(data.cell_ids[5]);
out.cellAfterSelect = core.get().cell;
out.emptyOk = core.selectCell(null);
out.cellAfterClear = core.get().cell;
out.missingRejected = core.selectCell('C9999_9999');
out.strategies = Object.keys(data.frames[0].strategies);
out.ledgerKeys = Object.keys(data.frames[0].strategies.rule.ledger);
console.log(JSON.stringify(out));
""",
        sources=(source,),
    )
    assert result["defaultCell"] == presentations[source]["ledger_top_cell"]["cell_id"]
    assert result["selectOk"] is True
    assert result["cellAfterSelect"] == presentations[source]["cell_ids"][5]
    assert result["emptyOk"] is True and result["cellAfterClear"] is None
    assert result["missingRejected"] is False
    assert result["strategies"] == list(STRATEGIES)
    assert set(result["ledgerKeys"]) == set(LEDGER_FIELDS)


@pytest.mark.parametrize("source", SOURCES)
def test_source_switch_is_a_navigation_and_cannot_carry_a_stale_cell(presentations, source):
    """Switching source is a link to that source's page, not an in-page re-render.

    The page loads only its own bundle (loading both would push ``replay/`` past
    its size cap), so a script-driven switch could never work here: the control
    has to be an anchor, and this test loads the page's real bundle set.
    """
    from js_harness import run_node

    other = "sh" if source == "xm" else "xm"
    result = run_node(
        f"""
const out = {{}};
out.hrefSelf = core.sourceHref('{source}');
out.hrefOther = core.sourceHref('{other}');
out.unknownHref = core.sourceHref('nope');
out.loaded = Object.keys(P);
console.log(JSON.stringify(out));
""",
        sources=(source,),
    )
    assert result["loaded"] == [source], "a workbench page ships one source bundle"
    assert result["hrefSelf"] == f"../{source}/workbench.html"
    # The state machine can only resolve a source whose bundle it was given --
    # which is precisely why a script-driven switch was a dead control here.
    assert result["hrefOther"] is None, "the other bundle is not loaded on this page"
    assert result["unknownHref"] is None
    assert (SITE / other / "workbench.html").exists(), "the navigation target must exist"

    # The shipped control must be the anchor that href describes -- not a button
    # whose click handler depends on a bundle the page never loads.
    text = (SITE / source / "workbench.html").read_text(encoding="utf-8")
    assert f'<a data-source-switch="{other}" href="../{other}/workbench.html">' in text
    assert f'<a data-source-switch="{source}" class="active" aria-current="page">' in text
    assert "<button data-source-switch" not in text, "no dead button may stand in for the link"
    # The target page starts from its own defaults, not the previous page's state.
    target = presentations[other]
    target_text = (SITE / other / "workbench.html").read_text(encoding="utf-8")
    assert f'id="ledger-cell" data-metric="selected_cell" data-value="{target["ledger_top_cell"]["cell_id"]}"' in target_text
    assert 'data-value="" ' in target_text or 'data-value=""' in target_text, "no cell selected on a fresh page"


@pytest.mark.parametrize("source", SOURCES)
def test_empty_states_are_rendered_as_text_not_as_zero(presentations, source):
    text = (SITE / source / "workbench.html").read_text(encoding="utf-8")
    assert "未选择" in text
    # The ledger must name the equations it uses, not just show numbers.
    for equation in ("调度后库存 = 期初库存 + 调入 - 调出",
                     "期末库存 = 期初库存 + 调入 - 调出 + 接纳归还 - 满足借车",
                     "未满足请求 = 借车请求 - 满足借车",
                     "溢出归还 = 归还 - 接纳归还"):
        assert equation in text, equation
    assert "bikes_moved" in text and "只表示搬出" in text

    # A cell/frame with no dispatch record gets a sentence, not a silent zero.
    script = (SITE / "workbench.js").read_text(encoding="utf-8")
    assert "本帧没有涉及该格的搬运记录。" in script
    # An event with no qualifying frame is labelled, never shown as a zero.
    for event in presentations[source]["events"]:
        if event["status"] in {"no_improvement", "no_regression", "all_zero"}:
            assert event["note"] in text, f"{source}: {event['id']} has no visible explanation"


@pytest.mark.parametrize("source", SOURCES)
def test_static_ledger_lists_exactly_the_records_for_the_default_cell(presentations, source):
    """The prerendered move list must be the dispatch records, both directions."""
    import json as _json
    from html.parser import HTMLParser as _Parser

    class _Page(_Parser):
        def __init__(self):
            super().__init__(convert_charrefs=True)
            self.rows: list[dict[str, str]] = []

        def handle_starttag(self, tag, attrs):
            values = {k: (v or "") for k, v in attrs}
            if "move-row" in values.get("class", "").split():
                self.rows.append(values)

        handle_startendtag = handle_starttag

    page = _Page()
    page.feed((SITE / source / "workbench.html").read_text(encoding="utf-8"))
    page.close()

    presentation = presentations[source]
    cell = presentation["ledger_top_cell"]["cell_id"]
    # The page opens on frame 1, and the ledger block now renders that frame.
    frame = presentation["frames"][0]
    for strategy in STRATEGIES:
        records = _json.loads(
            (Layout().outputs / f"sim_moves.{strategy}.{source}.json").read_text(encoding="utf-8")
        )
        expected = sorted(
            (str(r["from_cell"]), str(r["to_cell"]), int(r["bikes"]))
            for r in records
            if pd.Timestamp(r["time_bucket"]).strftime("%Y-%m-%d %H:%M:%S") == frame["time_bucket"]
            and (str(r["from_cell"]) == cell or str(r["to_cell"]) == cell)
        )
        rendered = sorted(
            (row["data-from"], row["data-to"], int(row["data-bikes"]))
            for row in page.rows
            if row.get("data-source") == f"outputs/sim_moves.{strategy}.{source}.json"
        )
        assert rendered == expected, f"{source}/{strategy}: prerendered move list differs from the records"
