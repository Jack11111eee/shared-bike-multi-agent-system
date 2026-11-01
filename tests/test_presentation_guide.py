"""M23 / F16: the five-chapter guide, wired to the analysis state.

F16-1..4 split into two kinds of evidence:

* the shipped DOM and data (entry point, chapter order, targets that exist in
  the authoritative inputs, numbers that recompute);
* behaviour (forward, back, exit, restart, pause-on-jump), driven through the
  published state machine in node, plus one real-browser smoke run.
"""

from __future__ import annotations

import json
import re
import shutil
import subprocess
from html.parser import HTMLParser
from pathlib import Path

import pandas as pd
import pytest

from bike_agent.config import Layout

ROOT = Path(__file__).resolve().parents[1]
SITE = ROOT / "replay"
SOURCES = ("xm", "sh")
ACTIONS = {"start", "prev", "next", "restart", "exit"}


class Page(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.elements: list[tuple[str, dict[str, str]]] = []
        self.text: list[str] = []
        self.noscript: list[str] = []
        self._in_noscript = False

    def handle_starttag(self, tag, attrs):
        values = {k: (v or "") for k, v in attrs}
        self.elements.append((tag, values))
        if tag == "noscript":
            self._in_noscript = True

    def handle_startendtag(self, tag, attrs):
        self.handle_starttag(tag, attrs)

    def handle_data(self, data):
        self.text.append(data)
        if self._in_noscript:
            self.noscript.append(data)

    def handle_endtag(self, tag):
        if tag == "noscript":
            self._in_noscript = False

    def by_attr(self, name: str) -> list[dict[str, str]]:
        return [attrs for _, attrs in self.elements if name in attrs]

    def by_class(self, name: str) -> list[dict[str, str]]:
        return [attrs for _, attrs in self.elements if name in attrs.get("class", "").split()]

    def body_text(self) -> str:
        return "".join(self.text)


def _parse(path: Path) -> Page:
    page = Page()
    page.feed(path.read_text(encoding="utf-8"))
    page.close()
    return page


# -- F16-1 ------------------------------------------------------------------
def test_home_page_links_the_guide_and_the_target_exists():
    index = _parse(SITE / "index.html")
    links = [attrs.get("href", "") for attrs in index.by_attr("href")]
    guide_links = [href for href in links if href.endswith("workbench.html")]
    assert guide_links, "the home page must link the guide"
    for href in guide_links:
        assert (SITE / href).exists(), f"{href} does not exist"
    text = (SITE / "index.html").read_text(encoding="utf-8")
    assert "演示导览" in text


def test_home_page_only_promises_pages_that_exist():
    """Every guide claim on the index must be true of the site that was built.

    The page used to advertise an in-page source switch that did not work; the
    same class of defect is a sentence describing pages a single-source build
    never writes.
    """
    from bike_agent.replay.site import _guide_scope_text

    index = _parse(SITE / "index.html")
    guide_links = [
        attrs.get("href", "")
        for attrs in index.by_attr("href")
        if attrs.get("href", "").endswith("workbench.html")
    ]
    assert guide_links
    for href in guide_links:
        assert (SITE / href).exists()

    text = (SITE / "index.html").read_text(encoding="utf-8")
    # Both sources really are built here, so the cross-link claim must appear...
    assert "每个数据源各有独立的工作台页" in text
    for source in SOURCES:
        assert (SITE / source / "workbench.html").exists()
        assert f"{source}/workbench.html" in {href.lstrip("./") for href in guide_links}
    # ...and a single-source build must not repeat the promise.
    single = _guide_scope_text({SOURCES[0]: {}})
    assert "跳到另一源" not in single, single
    assert "只生成了一个数据源" in single
    both = _guide_scope_text({source: {} for source in SOURCES})
    assert "跳到另一源" in both and "独立的工作台页" in both


@pytest.mark.parametrize("source", SOURCES)
def test_guide_has_five_ordered_chapters_and_all_navigation_actions(presentations, source):
    guide = presentations[source]["guide"]
    assert guide["n_chapters"] == 5
    assert [chapter["index"] for chapter in guide["chapters"]] == [0, 1, 2, 3, 4]
    titles = [chapter["title"] for chapter in guide["chapters"]]
    assert titles == ["数据与需求概貌", "关键时段", "策略差异", "单格账本", "资源参数实验"]

    assert {action["id"] for action in guide["actions"]} == ACTIONS

    page = _parse(SITE / source / "workbench.html")
    buttons = {attrs["data-guide-action"] for attrs in page.by_attr("data-guide-action")}
    assert buttons == ACTIONS
    chapters = page.by_class("chapter")
    assert [int(attrs["data-chapter"]) for attrs in chapters] == [0, 1, 2, 3, 4]
    assert int(page.by_attr("data-guide")[0]["data-n-chapters"]) == 5


# -- F16-2 ------------------------------------------------------------------
@pytest.mark.parametrize("source", SOURCES)
def test_every_chapter_target_exists_in_the_authoritative_inputs(presentations, source):
    layout = Layout()
    presentation = presentations[source]
    for chapter in presentation["guide"]["chapters"]:
        target = chapter["target"]
        assert target["source"] in SOURCES
        assert 0 <= target["frame"] < presentation["n_frames"]
        assert target["mode"] in {"side", "delta"}
        if target["cell"] is not None:
            assert target["cell"] in presentation["cell_ids"]
        assert target["scenario"] == "baseline" or target["scenario"] in {
            row["id"] for row in presentation["scenarios"]["rows"]
        }
        # The frame the chapter points at must really be that source's bucket.
        steps = pd.read_csv(layout.sim_path(source, "rule", "csv"))
        steps["time_bucket"] = pd.to_datetime(steps["time_bucket"])
        steps = steps.sort_values("time_bucket").reset_index(drop=True)
        assert presentation["buckets"][target["frame"]] == steps["time_bucket"].iloc[target["frame"]].strftime(
            "%Y-%m-%d %H:%M:%S"
        )


@pytest.mark.parametrize("source", SOURCES)
def test_guide_numbers_recompute_from_their_join_keys(presentations, source):
    """Not "the digits appear somewhere" -- recompute the claim and compare."""
    layout = Layout()
    presentation = presentations[source]
    rule = pd.read_csv(layout.sim_path(source, "rule", "csv"))
    optimized = pd.read_csv(layout.sim_path(source, "optimized", "csv"))
    rule["time_bucket"] = pd.to_datetime(rule["time_bucket"])
    optimized["time_bucket"] = pd.to_datetime(optimized["time_bucket"])
    rule = rule.sort_values("time_bucket").reset_index(drop=True)
    optimized = optimized.sort_values("time_bucket").reset_index(drop=True)

    chapters = presentation["guide"]["chapters"]

    # Chapter 1: totals.
    borrow_total = int(rule["borrow_requests"].sum())
    unmet_rule = int(rule["unmet_requests"].sum())
    unmet_opt = int(optimized["unmet_requests"].sum())
    body = chapters[0]["body"]
    assert f"{borrow_total:,}" in body
    assert f"{unmet_rule:,}" in body
    assert f"{unmet_opt:,}" in body
    rate = rule["borrow_met"].sum() / borrow_total * 100
    assert f"{rate:.2f}%" in body

    # Chapter 2: the peak bucket and its gain.
    peak = int(rule["unmet_requests"].idxmax())
    peak_value = int(rule["unmet_requests"].max())
    gain = peak_value - int(optimized["unmet_requests"].iloc[peak])
    body = chapters[1]["body"]
    assert presentation["buckets"][peak] in body
    assert f"{peak_value:,}" in body
    # A zero gain is written "0", not "+0" -- the page must not imply a direction.
    assert (f"{gain:+,}" if gain else "0") in body
    if gain == 0:
        assert "+0" not in body

    # Chapter 3: the largest |delta| frame and its cell counts.
    widest = max(presentation["frames"], key=lambda f: f["delta_summary"]["max_abs"])
    body = chapters[2]["body"]
    assert f"±{presentation['color_domain']['delta']['max_abs']}" in body
    assert f"{widest['delta_summary']['net']:+d}" in body
    assert f"改善格 {widest['delta_summary']['improved_cells']} 个" in body
    assert f"退步格 {widest['delta_summary']['worsened_cells']} 个" in body

    # Chapter 4: the default cell and its cumulative count.
    top = presentation["ledger_top_cell"]
    body = chapters[3]["body"]
    assert top["cell_id"] in body
    assert f"{top['cumulative_unmet_rule']:,}" in body

    # Chapter 5: the scenario and its delta against baseline.
    row = next(
        row for row in presentation["scenarios"]["rows"] if row["id"] == chapters[4]["target"]["scenario"]
    )
    body = chapters[4]["body"]
    assert row["id"] != "baseline"
    assert f"{row['strategies']['rule']['unmet_requests']['value']:,}" in body
    assert f"{row['strategies']['rule']['unmet_requests']['delta']['absolute']:+,}" in body


@pytest.mark.parametrize("source", SOURCES)
def test_guide_covers_both_sources_and_a_scenario(presentations, source):
    text = " ".join(chapter["body"] for chapter in presentations[source]["guide"]["chapters"])
    other = "sh" if source == "xm" else "xm"
    assert other.upper() in text, "chapter 1 must compare the two sources"
    assert any(
        chapter["target"]["scenario"] != "baseline" for chapter in presentations[source]["guide"]["chapters"]
    ), "at least one chapter must land on a sensitivity scenario"


# -- F16-3 (behaviour) ------------------------------------------------------
@pytest.mark.parametrize("source", SOURCES)
def test_forward_back_exit_and_restart_keep_state_in_sync(source):
    from js_harness import run_node

    result = run_node(
        f"""
const data = P['{source}'];
const out = [];
function snap(label) {{
  const s = core.get();
  const chapter = data.guide.chapters[s.chapter];
  return {{
    label: label, chapter: s.chapter, started: s.started, frame: s.frame, mode: s.mode,
    cell: s.cell, scenario: s.scenario, playing: s.playing,
    title: chapter ? chapter.title : null,
    target_frame: chapter ? chapter.target.frame : null,
    target_mode: chapter ? chapter.target.mode : null,
    target_cell: chapter ? chapter.target.cell : null,
    target_scenario: chapter ? chapter.target.scenario : null
  }};
}}
core.start(); out.push(snap('start'));
for (let i = 0; i < 4; i++) {{ core.next(); out.push(snap('next' + i)); }}
out.push({{ label: 'next-at-end', accepted: core.next() }});
core.prev(); out.push(snap('prev'));
core.exit(); out.push(snap('exit'));
const kept = core.get();
core.restart(); out.push(snap('restart'));
console.log(JSON.stringify({{ steps: out, kept: kept }}));
""",
        sources=(source,),
    )
    steps = {step["label"]: step for step in result["steps"] if "label" in step}
    assert steps["start"]["chapter"] == 0 and steps["start"]["started"] is True
    for index in range(4):
        step = steps[f"next{index}"]
        assert step["chapter"] == index + 1
        # The analysis state follows the chapter target exactly.
        assert step["frame"] == step["target_frame"]
        assert step["mode"] == step["target_mode"]
        assert step["scenario"] == step["target_scenario"]
        if step["target_cell"]:
            assert step["cell"] == step["target_cell"]

    assert any(step.get("accepted") is False for step in result["steps"] if step.get("label") == "next-at-end")
    assert steps["prev"]["chapter"] == 3
    assert steps["exit"]["chapter"] == -1 and steps["exit"]["started"] is False
    # Exiting keeps the analysis position (the frame/cell the reader was on).
    assert steps["exit"]["frame"] == steps["prev"]["frame"]
    assert steps["exit"]["cell"] == steps["prev"]["cell"]
    assert steps["restart"]["chapter"] == 0


@pytest.mark.parametrize("source", SOURCES)
def test_chapter_change_and_event_jump_pause_playback(source):
    from js_harness import run_node

    result = run_node(
        f"""
const data = P['{source}'];
const out = {{}};
core.setPlaying(true);
out.beforeChapter = core.get().playing;
core.start();
out.afterStart = core.get().playing;
core.setPlaying(true);
core.next();
out.afterNext = core.get().playing;
core.setPlaying(true);
const event = data.events.filter(function (e) {{ return e.status === 'ok'; }})[0];
out.jumpAccepted = core.jumpToEvent(event.id);
out.afterJump = core.get().playing;
out.frame = core.get().frame;
out.eventFrame = event.frame;
console.log(JSON.stringify(out));
""",
        sources=(source,),
    )
    assert result["beforeChapter"] is True
    assert result["afterStart"] is False, "starting a chapter must pause the clock"
    assert result["afterNext"] is False, "changing chapter must pause the clock"
    assert result["jumpAccepted"] is True
    assert result["afterJump"] is False, "jumping to an event must pause the clock"
    assert result["frame"] == result["eventFrame"]


# -- F16-4 ------------------------------------------------------------------
@pytest.mark.parametrize("source", SOURCES)
def test_old_entries_and_every_frame_page_survive(presentations, source):
    page = _parse(SITE / source / "workbench.html")
    hrefs = {attrs.get("href", "") for attrs in page.by_attr("href")}
    for required in ("../index.html", "player.html", "../dashboard.html", "frame-0000.html"):
        assert required in hrefs, f"{source}: {required} entry disappeared"
    frames = sorted(SITE.glob(f"{source}/frame-*.html"))
    assert len(frames) == presentations[source]["n_frames"]
    assert (SITE / source / "player.html").exists()
    assert (SITE / "dashboard.html").exists()


@pytest.mark.parametrize("source", SOURCES)
def test_no_javascript_still_gets_an_introduction_and_links(presentations, source):
    raw = (SITE / source / "workbench.html").read_text(encoding="utf-8")
    block = re.search(r"<noscript>(.*?)</noscript>", raw, re.DOTALL)
    assert block, "the page must carry a <noscript> fallback"
    fallback = block.group(1)
    assert 'href="frame-0000.html"' in fallback
    assert 'href="player.html"' in fallback
    assert 'href="../dashboard.html"' in fallback
    assert str(presentations[source]["n_frames"]) in fallback
    assert str(presentations[source]["n_cells"]) in fallback
    assert "模拟值" in fallback


# -- NF-27: a real browser, once -------------------------------------------
def _chrome() -> str | None:
    from scripts.browser_smoke import find_chrome  # type: ignore[import-not-found]

    return find_chrome()


@pytest.mark.parametrize("source", SOURCES)
def test_real_browser_smoke(source):
    """Load the shipped page in headless Chrome, click every control, read the DOM."""
    chrome = _chrome()
    if chrome is None:
        pytest.skip("no Chrome/Chromium binary available; the browser smoke cannot run here")
    completed = subprocess.run(
        [shutil.which("python3") or "python3", str(ROOT / "scripts" / "browser_smoke.py"), "--source", source, "--json"],
        capture_output=True,
        text=True,
        timeout=900,
        cwd=str(ROOT),
        check=False,
    )
    assert completed.returncode == 0, completed.stdout[-2000:] + completed.stderr[-2000:]
    result = json.loads(completed.stdout)
    assert result["ready"] is True, "the page script never reached its ready flag"
    assert not result["errors"], result["errors"]
    # Cells that exist but have no geometry are invisible; a value whose own
    # operands do not recompute to it is not traceable.
    assert result["geometryOk"] is True, {
        key: result.get(key)
        for key in ("deltaGeometryDelta", "deltaGeometryAfterJump")
    }
    assert result["provenanceOk"] is True, {
        key: result.get(key)
        for key in ("provenanceInitial", "provenanceDelta", "provenanceAfterJump")
    }

    steps = {step["label"]: step for step in result["steps"]}
    # Guide: five chapters, one active at a time.
    assert steps["start"]["chapter"] == "0"
    assert steps["start"]["activeChapters"] == 1
    for index in range(4):
        assert steps[f"next-{index}"]["chapter"] == str(index + 1)
    assert steps["prev"]["chapter"] == "3"
    assert steps["exit"]["chapter"] == "-1"
    assert steps["restart"]["chapter"] == "0"
    # Mode and cell.
    assert steps["delta-mode"]["mode"] == "delta"
    assert steps["delta-cell-click"]["cell"] != ""
    assert steps["delta-cell-click"]["ledgerCell"] == steps["delta-cell-click"]["cell"]
    # Jumping to an event pauses the clock.
    assert result["playingBeforeJump"] == "true"
    assert steps["event-jump"]["playing"] == "false"
    # Every scenario is selectable and baseline comes back.
    assert len(result["scenarioIds"]) == 7
    for snap in result["scenarios"]:
        assert snap["scenario"] == snap["label"].split(":", 1)[1]
    assert steps["baseline-return"]["scenario"] == "baseline"
    # The keyboard-select path moves the ledger too.
    assert steps["cell-select"]["ledgerCell"] == steps["cell-select"]["cell"]

    # And the ledger the browser actually rendered equals the parquet for that
    # cell and frame -- the DOM wiring, not just the bundle it read from.
    context = result["ledgerContext"]
    state = pd.read_parquet(Layout().replay_path(source, "rule"))
    state["time_bucket"] = pd.to_datetime(state["time_bucket"])
    row = state[
        (state["cell_id"] == context["cell"])
        & (state["time_bucket"].dt.strftime("%Y-%m-%d %H:%M:%S") == context["time_bucket"])
    ]
    assert len(row) == 1, f"{source}: no parquet row for {context['cell']} @ {context['time_bucket']}"
    row = row.iloc[0]
    rendered = result["ledger"]["rule"]
    assert int(rendered["inventory_start"]) == int(row["inventory_start"])
    assert int(rendered["borrow_requests"]) == int(row["borrow_requests"])
    assert int(rendered["borrow_met"]) == int(row["borrow_met"])
    assert int(rendered["returns"]) == int(row["returns"])
    assert int(rendered["returns_applied"]) == int(row["returns_applied"])
    assert int(rendered["unmet_requests"]) == int(row["unmet_requests"])
    assert int(rendered["returns_overflow"]) == int(row["returns_overflow"])
    assert int(rendered["moved_out"]) == int(row["bikes_moved"])
    assert int(rendered["inventory_end"]) == int(row["inventory_end"])
    # The full-replay history table has one row per frame, and exactly one row
    # per strategy is marked as the current frame.
    assert context["historyRows"] > 0
    assert context["historyCurrent"] == 2, "one current history row per strategy"
