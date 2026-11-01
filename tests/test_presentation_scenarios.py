"""M22 / F20: the existing sensitivity scenarios.

The scenarios are read, never recomputed: this module checks that every value on
the page equals ``sim_sensitivity.<source>.json``, that the baseline row equals
``strategy_comparison.<source>.json``, and that the relative-change arithmetic
(including the zero-denominator case) is reproducible.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from bike_agent.config import Layout
from bike_agent.presentation.analysis import _delta, build_scenarios

ROOT = Path(__file__).resolve().parents[1]
SITE = ROOT / "replay"
SOURCES = ("xm", "sh")
STRATEGIES = ("rule", "optimized")
METRICS = ("unmet_requests", "service_rate", "distance_bike_km", "composite_cost")
# rel_tol=1e-9 / abs_tol=1e-9 as REQUIREMENTS-PHASE4 §2.3 requires.
TOL = {"rel": 1e-9, "abs": 1e-9}


def _sensitivity(source: str) -> list[dict]:
    return json.loads(
        (Layout().outputs / f"sim_sensitivity.{source}.json").read_text(encoding="utf-8")
    )


# -- F20-1 ------------------------------------------------------------------
@pytest.mark.parametrize("source", SOURCES)
def test_every_scenario_and_metric_equals_the_input(presentations, source):
    truth = {row["scenario"]: row for row in _sensitivity(source)}
    rendered = {row["id"]: row for row in presentations[source]["scenarios"]["rows"]}
    assert set(rendered) == set(truth), "a scenario must never be silently dropped"
    for scenario, expected in truth.items():
        for strategy in STRATEGIES:
            for metric in METRICS:
                actual = rendered[scenario]["strategies"][strategy][metric]["value"]
                assert actual == pytest.approx(expected[strategy][metric], **TOL), (
                    f"{source}/{scenario}/{strategy}/{metric}"
                )


@pytest.mark.parametrize("source", SOURCES)
def test_baseline_matches_the_strategy_comparison(presentations, source):
    comparison = json.loads(
        (Layout().outputs / f"strategy_comparison.{source}.json").read_text(encoding="utf-8")
    )
    baseline = next(row for row in presentations[source]["scenarios"]["rows"] if row["id"] == "baseline")
    for strategy in STRATEGIES:
        for metric in METRICS:
            assert baseline["strategies"][strategy][metric]["value"] == pytest.approx(
                comparison[strategy][metric], **TOL
            )
    checks = presentations[source]["scenarios"]["baseline_matches_comparison"]
    assert all(entry["equal"] for strategy in checks.values() for entry in strategy.values())


@pytest.mark.parametrize("source", SOURCES)
def test_all_seven_existing_scenarios_are_present(presentations, source):
    names = {row["id"] for row in presentations[source]["scenarios"]["rows"]}
    assert names == {
        "baseline",
        "initial_fill=0.3",
        "initial_fill=0.7",
        "budget=100",
        "budget=400",
        "capacity=0.5x",
        "capacity=2x",
    }
    for row in presentations[source]["scenarios"]["rows"]:
        if row["id"] == "baseline":
            assert row["changed"] is None and row["status"] == "baseline"
        else:
            assert row["changed"] is not None
            assert row["changed"]["factor"] in {"initial_fill", "budget", "capacity"}


# -- F20-2 ------------------------------------------------------------------
@pytest.mark.parametrize("source", SOURCES)
def test_relative_change_is_recomputable(presentations, source):
    truth = {row["scenario"]: row for row in _sensitivity(source)}
    baseline = truth["baseline"]
    for row in presentations[source]["scenarios"]["rows"]:
        for strategy in STRATEGIES:
            for metric in METRICS:
                block = row["strategies"][strategy][metric]
                absolute = truth[row["id"]][strategy][metric] - baseline[strategy][metric]
                assert block["delta"]["absolute"] == pytest.approx(absolute, **TOL)
                if baseline[strategy][metric] == 0:
                    assert block["delta"]["status"] == "zero_denominator"
                    assert block["delta"]["percent"] is None
                else:
                    expected = absolute / baseline[strategy][metric] * 100.0
                    assert block["delta"]["percent"] == pytest.approx(expected, **TOL)


def test_delta_helper_covers_no_change_negative_and_zero_denominator():
    unchanged = _delta(10.0, 10.0)
    assert unchanged == {"absolute": 0.0, "percent": 0.0, "status": "ok"}

    worse = _delta(12.0, 10.0)
    assert worse["absolute"] == pytest.approx(2.0)
    assert worse["percent"] == pytest.approx(20.0)

    better = _delta(8.0, 10.0)
    assert better["absolute"] == pytest.approx(-2.0)
    assert better["percent"] == pytest.approx(-20.0)

    zero = _delta(3.0, 0.0)
    assert zero["status"] == "zero_denominator"
    assert zero["percent"] is None, "dividing by zero must be reported, never rendered as 0%"
    assert zero["absolute"] == 3.0


def test_scenario_builder_reports_a_missing_baseline_instead_of_guessing():
    with pytest.raises(ValueError):
        build_scenarios("toy", [{"scenario": "initial_fill=0.3", "rule": {}, "optimized": {}}], {})


# -- F20-3 ------------------------------------------------------------------
@pytest.mark.parametrize("source", SOURCES)
def test_page_lists_every_scenario_as_a_discrete_button(presentations, source):
    from html.parser import HTMLParser

    class Page(HTMLParser):
        def __init__(self):
            super().__init__(convert_charrefs=True)
            self.elements: list[tuple[str, dict[str, str]]] = []

        def handle_starttag(self, tag, attrs):
            self.elements.append((tag, {k: (v or "") for k, v in attrs}))

        handle_startendtag = handle_starttag

    page = Page()
    page.feed((SITE / source / "workbench.html").read_text(encoding="utf-8"))
    page.close()
    buttons = [attrs for _, attrs in page.elements if "data-scenario" in attrs and attrs.get("data-scenario")]
    scenario_buttons = {attrs["data-scenario"] for attrs in buttons if attrs.get("class") != "baseline-return"}
    expected = {row["id"] for row in presentations[source]["scenarios"]["rows"]}
    assert expected <= scenario_buttons, "every computed scenario needs a button"
    # No slider, no free-form parameter: only discrete, already-computed values.
    text = (SITE / source / "workbench.html").read_text(encoding="utf-8")
    assert "type=\"range\"" not in text.split('data-scenarios', 1)[1]
    assert "不插值、不组合参数、不在浏览器重跑仿真" in text


@pytest.mark.parametrize("source", SOURCES)
def test_scenario_selection_does_not_touch_the_baseline_map(presentations, source):
    from js_harness import run_node

    result = run_node(
        f"""
const data = P['{source}'];
const frameBefore = JSON.stringify(data.frames[0].delta);
const ledgerBefore = JSON.stringify(data.frames[0].strategies.rule.ledger);
const targets = data.scenarios.rows.map(function (row) {{ return row.id; }});
const out = [];
for (const id of targets) {{
  const ok = core.setScenario(id);
  const state = core.get();
  out.push({{ id: id, accepted: ok, scenario: state.scenario }});
}}
core.setScenario('baseline');
console.log(JSON.stringify({{
  steps: out,
  frameUnchanged: JSON.stringify(data.frames[0].delta) === frameBefore,
  ledgerUnchanged: JSON.stringify(data.frames[0].strategies.rule.ledger) === ledgerBefore,
  back: core.get().scenario,
  unknown: core.setScenario('budget=999')
}}));
""",
        sources=(source,),
    )
    for step in result["steps"]:
        assert step["accepted"] is True, f"{step['id']} must be selectable"
        assert step["scenario"] == step["id"]
    assert result["frameUnchanged"], "switching a scenario must not rewrite the baseline map data"
    assert result["ledgerUnchanged"]
    assert result["back"] == "baseline"
    assert result["unknown"] is False, "a scenario that was never computed must be refused"


@pytest.mark.parametrize("source", SOURCES)
def test_page_separates_scenario_scope_from_the_map(presentations, source):
    text = (SITE / source / "workbench.html").read_text(encoding="utf-8")
    assert "场景只重算全回放汇总指标，不生成空间回放" in text
    assert "baseline 回放地图" in text
    assert "回到 baseline" in text
