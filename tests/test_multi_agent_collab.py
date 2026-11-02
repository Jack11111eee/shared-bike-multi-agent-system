"""F28: the collaboration page, its node ids, deep links and empty state."""

from __future__ import annotations

import json
import re
from pathlib import Path

from bike_agent.config import Layout, load_config
from bike_agent.presentation.collab import build_collab, discover_runs, select_runs

ROOT = Path(__file__).resolve().parents[1]
SITE = ROOT / "replay"
COLLAB = SITE / "collab.html"

def _payload() -> dict:
    text = COLLAB.read_text(encoding="utf-8")
    match = re.search(r'<script type="application/json" id="collab-data">(.*?)</script>', text, re.DOTALL)
    assert match, "collab-data JSON block is missing"
    return json.loads(match.group(1))

def test_page_shows_a_real_run_for_both_sources():
    payload = _payload()
    assert {"xm", "sh"} <= set(payload["sources"])
    selected = select_runs(discover_runs(ROOT / "outputs"))
    by_run_id = {run["run_id"]: run for run in payload["runs"]}
    for source in ("xm", "sh"):
        shown = payload["selected"][source]
        assert by_run_id[shown]["source"] == source
        assert shown == selected[source]["run_id"]

def test_the_page_offers_a_run_selector_and_an_alternative_state():
    """F28: a baseline_retained run must be viewable, not only the accepted one."""
    payload = _payload()
    text = COLLAB.read_text(encoding="utf-8")
    assert 'class="run-picker"' in text
    assert "<select" in text
    for source in ("xm", "sh"):
        runs = [run for run in payload["runs"] if run["source"] == source]
        assert runs, source
        if len(runs) > 1:
            states = {run["terminal_state"] for run in runs}
            assert len(states) > 1
    xm_states = {run["terminal_state"] for run in payload["runs"] if run["source"] == "xm"}
    assert "baseline_retained" in xm_states
    # Exactly one section per source starts visible.
    for source in ("xm", "sh"):
        visible = re.findall(
            rf'<section class="run" data-source="{source}" data-run-id="([^"]+)">', text
        )
        assert visible == [payload["selected"][source]]

def test_every_message_and_tool_call_has_a_timeline_node():
    text = COLLAB.read_text(encoding="utf-8")
    payload = _payload()
    node_messages = set(re.findall(r'data-message-id="([^"]+)"', text))
    node_tools = set(re.findall(r'data-tool-call-id="([^"]+)"', text))
    for run in payload["runs"]:
        for message in run["messages"]:
            assert message["message_id"] in node_messages
        for call in run["tool_calls"]:
            assert call["tool_call_id"] in node_tools
    # No node may point at an id that is not in the audit payload.
    known_messages = {message["message_id"] for run in payload["runs"] for message in run["messages"]}
    known_tools = {call["tool_call_id"] for run in payload["runs"] for call in run["tool_calls"]}
    assert node_messages <= known_messages
    assert node_tools <= known_tools

def test_page_has_no_external_resources_or_network_calls():
    text = COLLAB.read_text(encoding="utf-8")
    assert not re.search(r'(?:src|href)="(?:https?:)?//', text)
    assert "fetch(" not in text
    assert "XMLHttpRequest" not in text
    assert "type=\"module\"" not in text

def test_deep_links_point_at_existing_pages_and_valid_cells():
    payload = _payload()
    checked = 0
    for run in payload["runs"]:
        for link in run["focus"]:
            if link.get("player_href"):
                source, _rest = link["player_href"].split("/", 1)
                assert (SITE / source / "player.html").exists()
                assert re.search(r"#frame=\d+", link["player_href"])
                checked += 1
            if link.get("workbench_href"):
                source = link["workbench_href"].split("/", 1)[0]
                assert (SITE / source / "workbench.html").exists()
            for cell in link.get("cells") or []:
                if cell.get("workbench_href"):
                    assert f"cell={cell['cell_id']}" in cell["workbench_href"]
                    checked += 1
    assert checked > 0

def test_baseline_retained_is_labelled_as_such(tmp_path):
    runs = [
        run
        for run in discover_runs(ROOT / "outputs")
        if run["decision"].get("terminal_state") == "baseline_retained"
    ]
    assert runs, "the real run history should contain a baseline_retained run"
    result = build_collab(load_config(), Layout(tmp_path), runs=[runs[0]])
    page = Path(result["path"]).read_text(encoding="utf-8")
    source = runs[0]["source"].upper()
    assert f"{source} · baseline_retained" in page
    assert f"{source} · accepted" not in page
    assert "保留基准" in page

def test_empty_state_is_explicit_and_contains_no_fake_nodes(tmp_path):
    config = load_config()
    result = build_collab(config, Layout(tmp_path), runs=[])
    page = Path(result["path"]).read_text(encoding="utf-8")
    assert "尚无运行记录" in page
    assert 'data-message-id="' not in page
    assert 'data-tool-call-id="' not in page
    assert result["n_runs"] == 0

def test_collaboration_data_stays_within_one_mib():
    payload = _payload()
    text = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    assert len(text) <= 1024 * 1024

def test_site_stays_under_fourteen_mib():
    total = sum(path.stat().st_size for path in SITE.rglob("*") if path.is_file())
    assert total < 14 * 1024 * 1024

def test_focus_cells_carry_the_possible_related_caveat():
    """F26: a forecast cell link is not a confirmed loss location."""
    text = COLLAB.read_text(encoding="utf-8")
    payload = _payload()
    cells = [cell for run in payload["runs"] for link in run["focus"] for cell in link.get("cells") or []]
    assert cells, "the real diagnosis should offer at least one focus cell"
    assert {cell.get("evidence_kind") for cell in cells} == {"possible_related"}
    assert "可能相关" in text and "预测非实际损失" in text

def test_the_timeline_follows_the_logical_message_order():
    """NF-38: the timeline is the real order, not a re-sorted summary."""
    text = COLLAB.read_text(encoding="utf-8")
    payload = _payload()
    for run in payload["runs"]:
        expected = [message["message_id"] for message in run["messages"]]
        start = text.index(f'data-run-id="{run["run_id"]}"')
        end = text.find('<section class="run"', start + 1)
        chunk = text[start:end if end != -1 else len(text)]
        assert re.findall(r'data-message-id="([^"]+)"', chunk) == expected

def test_page_reports_effort_and_the_timing_split():
    """F29-5 / NF-39: candidates, calls and where the wall clock went."""
    text = COLLAB.read_text(encoding="utf-8")
    payload = _payload()
    assert "本次投入" in text
    assert "候选" in text and "工具调用" in text
    with_timing = [run for run in payload["runs"] if run["decision"].get("timing")]
    assert with_timing, "the selected runs should carry a timing breakdown"
    assert "耗时" in text and "后端仿真" in text and "调度" in text
    assert "不是页面加载时间" in text

def test_detail_lookups_are_scoped_to_the_owning_run():
    """Ids restart per run, so the page must resolve a section before lookup.

    ``msg-0001``/``tc-0001``/``cand-r1-1`` are only unique inside one run.  The
    shipped page must therefore key its indexes by run and decide which run a
    clicked node belongs to; a flat map would show another run's record.
    """
    text = COLLAB.read_text(encoding="utf-8")
    assert "closest('section.run')" in text
    assert "provenance(scope" in text
    assert "byMessage[" not in text and "byTool[" not in text
    payload = _payload()
    per_run = [{message["message_id"] for message in run["messages"]} for run in payload["runs"]]
    shared = [
        left & right
        for index, left in enumerate(per_run)
        for right in per_run[index + 1 :]
    ]
    assert any(shared), "per-run ids restart, so run scoping is load-bearing"

def test_shipped_scripts_parse_deep_links():
    player = (SITE / "player.js").read_text(encoding="utf-8")
    workbench = (SITE / "workbench.js").read_text(encoding="utf-8")
    assert "location.hash" in player and "deepApplied" in player
    assert "location.hash" in workbench
    assert "core.setFrame" in workbench
    assert "core.selectCell" in workbench
