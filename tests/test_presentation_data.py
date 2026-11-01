"""M18: one analysis build, one derivation, full-data checks.

REQUIREMENTS-PHASE4 §2.3 requires the guide, events, delta map, ledger and
scenario table to share one build.  This module proves the build is *correct*
against the artefacts on every frame and every cell -- not a sample -- and that
the published ``presentation.js`` is that same build rather than a second one.
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
TOL = {"rel_tol": 1e-9, "abs_tol": 1e-9}


def _parquet(layout: Layout, source: str, strategy: str) -> pd.DataFrame:
    frame = pd.read_parquet(layout.replay_path(source, strategy))
    frame["time_bucket"] = pd.to_datetime(frame["time_bucket"])
    return frame.sort_values(["time_bucket", "cell_id"], kind="mergesort").reset_index(drop=True)


def _csv(layout: Layout, source: str, strategy: str) -> pd.DataFrame:
    frame = pd.read_csv(layout.sim_path(source, strategy, "csv"))
    frame["time_bucket"] = pd.to_datetime(frame["time_bucket"])
    return frame.sort_values("time_bucket").reset_index(drop=True)


# -- F18-1 / F19-1: the delta is the parquet difference, everywhere ---------
@pytest.mark.parametrize("source", SOURCES)
def test_delta_is_the_exact_parquet_difference_for_every_cell(presentations, source):
    layout = Layout()
    rule = _parquet(layout, source, "rule")["unmet_requests"].to_numpy(dtype=np.int64)
    optimized = _parquet(layout, source, "optimized")["unmet_requests"].to_numpy(dtype=np.int64)
    expected = (optimized - rule).reshape(len(_csv(layout, source, "rule")), -1)

    rendered = np.array([frame["delta"] for frame in presentations[source]["frames"]], dtype=np.int64)
    assert rendered.shape == expected.shape
    assert np.array_equal(rendered, expected), "delta_unmet differs from the parquet difference"


@pytest.mark.parametrize("source", SOURCES)
def test_gain_is_the_negative_of_delta_every_frame(presentations, source):
    presentation = presentations[source]
    for frame in presentation["frames"]:
        assert frame["gain"] == -frame["delta_summary"]["net"]
        assert frame["gain"] == sum(frame["delta"]) * -1


@pytest.mark.parametrize("source", SOURCES)
def test_delta_summary_is_recomputable_from_the_cells(presentations, source):
    for frame in presentations[source]["frames"]:
        row = np.array(frame["delta"], dtype=np.int64)
        summary = frame["delta_summary"]
        assert summary["net"] == int(row.sum())
        assert summary["improved_cells"] == int((row < 0).sum())
        assert summary["worsened_cells"] == int((row > 0).sum())
        assert summary["unchanged_cells"] == int((row == 0).sum())
        expected_max_abs = int(np.abs(row).max()) if row.size else 0
        assert summary["max_abs"] == expected_max_abs
        assert summary["improved_cells"] + summary["worsened_cells"] + summary["unchanged_cells"] == len(row)


@pytest.mark.parametrize("source", SOURCES)
def test_cell_deltas_sum_to_the_csv_difference_every_frame(presentations, source):
    layout = Layout()
    rule = _csv(layout, source, "rule")
    optimized = _csv(layout, source, "optimized")
    for index, frame in enumerate(presentations[source]["frames"]):
        expected = int(optimized["unmet_requests"].iloc[index]) - int(rule["unmet_requests"].iloc[index])
        assert sum(frame["delta"]) == expected, f"{source} frame {index}"


# -- F18-2: the colour domain is one fixed range per source -----------------
@pytest.mark.parametrize("source", SOURCES)
def test_delta_domain_is_the_global_max_abs(presentations, source):
    presentation = presentations[source]
    global_max = max(
        (abs(value) for frame in presentation["frames"] for value in frame["delta"]),
        default=0,
    )
    domain = presentation["color_domain"]["delta"]
    assert domain["max_abs"] == global_max
    assert domain["min"] == -global_max and domain["max"] == global_max
    # The domain is a property of the source, not of a single frame: it must
    # cover the widest frame, and at least one frame is narrower than it unless
    # every frame happens to hit the same extreme.
    assert max(frame["delta_summary"]["max_abs"] for frame in presentation["frames"]) == global_max
    assert all(frame["delta_summary"]["max_abs"] <= global_max for frame in presentation["frames"])


# -- §2.3: every derived record is traceable --------------------------------
@pytest.mark.parametrize("source", SOURCES)
def test_every_event_record_has_source_join_key_and_formula(presentations, source):
    for event in presentations[source]["events"]:
        assert event["source"] == source
        assert event["type"] in {"rule_unmet_max", "rule_moved_max", "opt_gain_max", "opt_regression_max"}
        assert (ROOT / event["source_file"]).exists(), event["source_file"]
        assert event["join_key"]
        assert event["formula"]
        assert "operands" in event
        assert event["status"] in {"ok", "all_zero", "no_improvement", "no_regression"}
        if event["status"] == "ok":
            assert event["frame"] is not None
            assert event["time_bucket"] == presentations[source]["buckets"][event["frame"]]
        else:
            assert event["frame"] is None
            assert event["note"], "an empty state must say why, not fall back to zero"


@pytest.mark.parametrize("source", SOURCES)
def test_every_scenario_value_cites_the_sensitivity_file(presentations, source):
    scenarios = presentations[source]["scenarios"]
    cited = ROOT / scenarios["source_file"]
    assert cited.exists()
    assert json.loads(cited.read_text(encoding="utf-8")), "the cited file must actually hold the scenarios"
    for row in scenarios["rows"]:
        for strategy in row["strategies"].values():
            for block in strategy.values():
                assert block["formula"]
                assert "delta" in block
                assert block["delta"]["status"] in {"ok", "zero_denominator"}


@pytest.mark.parametrize("source", SOURCES)
def test_every_guide_number_is_backed_by_an_evidence_record(presentations, source):
    guide = presentations[source]["guide"]
    assert guide["n_chapters"] == len(guide["chapters"]) == 5
    for chapter in guide["chapters"]:
        for item in chapter["evidence"]:
            assert (ROOT / item["source_file"]).exists(), item["source_file"]
            assert item["join_key"] and item["formula"]
            assert item["value"] is not None


# -- zero / missing are explicit, never a zero-shaped guess -----------------
@pytest.mark.parametrize("source", SOURCES)
def test_zero_and_missing_states_are_explicit(presentations, source):
    presentation = presentations[source]
    domain = presentation["color_domain"]["delta"]
    if domain["max_abs"] == 0:
        # A source where S-OPT never differs must say so rather than show an
        # empty ±0 legend as if it were a normal scale.
        assert all(frame["delta_summary"]["status"] == "no_difference" for frame in presentation["frames"])
    else:
        assert all(frame["delta_summary"]["status"] == "ok" for frame in presentation["frames"])

    for event in presentation["events"]:
        if event["status"] == "all_zero":
            assert event["value"] == 0
        if event["status"] == "no_improvement":
            assert all(g >= 0 for g in (frame["gain"] for frame in presentation["frames"]))
        if event["status"] == "no_regression":
            assert all(g <= 0 for g in (frame["gain"] for frame in presentation["frames"]))

    # A zero denominator is recorded as such, never as "0%".
    for row in presentation["scenarios"]["rows"]:
        for strategy in STRATEGIES:
            for block in row["strategies"][strategy].values():
                delta = block["delta"]
                if delta["status"] == "zero_denominator":
                    assert delta["percent"] is None


# -- one build, published once ---------------------------------------------
@pytest.mark.parametrize("source", SOURCES)
def test_one_build_returns_the_whole_contract(presentations, source):
    """M18: a single ``build_presentation`` call yields guide + evidence too.

    Building the guide used to be a second step callers had to remember, which
    meant "the single build" was really two and the return value did not carry
    everything the contract enumerates.
    """
    from bike_agent.config import Layout, load_config
    from bike_agent.presentation.analysis import build_presentation

    built = build_presentation(load_config(), Layout(), source)
    for key in (
        "source",
        "n_frames",
        "cell_ids",
        "geom",
        "color_domain",
        "frames",
        "events",
        "scenarios",
        "guide",
        "evidence",
    ):
        assert key in built, f"{source}: build_presentation does not return {key!r}"
    assert built["guide"]["n_chapters"] == 5
    assert built["guide"] == presentations[source]["guide"], "the one build must agree with the fixture"
    # Frame-level evidence lives on ``frames[i].evidence``, not on a second
    # top-level copy: the same records twice cost 171 KB of the SH bundle.
    assert set(built["evidence"]) == {"build", "events", "scenarios", "ledger"}
    assert all("evidence" in frame for frame in built["frames"])


@pytest.mark.parametrize("source", SOURCES)
def test_every_frame_derived_value_carries_provenance(presentations, source):
    """``delta_summary`` and ``gain`` are derived numbers, so §2.3 applies to them."""
    for frame in presentations[source]["frames"]:
        evidence = frame["evidence"]
        records = [("delta_summary", evidence["delta_summary"]), ("gain", evidence["gain"])]
        records += [(f"moved_in[{strategy}]", evidence["moved_in"][strategy]) for strategy in ("rule", "optimized")]
        for name, record in records:
            for field in ("source_file", "metric", "value", "join_key", "formula", "operands"):
                assert field in record, f"{source}/{frame['index']}/{name}: missing {field}"
            assert record["join_key"] == f"time_bucket={frame['time_bucket']}"
        summary = evidence["delta_summary"]
        assert summary["value"] == frame["delta_summary"]["net"]
        assert summary["operands"]["n_cells"] == presentations[source]["n_cells"]
        assert summary["operands"]["unmet_optimized_total"] - summary["operands"][
            "unmet_rule_total"
        ] == frame["delta_summary"]["net"]
        assert evidence["gain"]["value"] == frame["gain"] == -frame["delta_summary"]["net"]
        for strategy in ("rule", "optimized"):
            moved = evidence["moved_in"][strategy]
            assert moved["value"] == sum(frame["strategies"][strategy]["moved_in"].values())
            # The operand is a pointer plus a reduce rule, so following it must
            # reproduce the value without a second copy of the map.
            assert moved["operands"]["source"] == "frames[i].strategies[s].moved_in"
            assert moved["operands"]["reduce"] == "sum(values)"


@pytest.mark.parametrize("source", SOURCES)
def test_scenario_evidence_distinguishes_value_from_delta(presentations, source):
    """The formula on a record must describe that record's value, not a sibling's.

    A scenario record carries the scenario's own number; the change against
    baseline is derived from it, so it gets its own operands instead of a
    formula that describes the difference.
    """
    rows = presentations[source]["evidence"]["scenarios"]
    assert len(rows) == presentations[source]["scenarios"]["n_scenarios"]
    for row in rows:
        for strategy in ("rule", "optimized"):
            for metric, record in row["strategies"][strategy].items():
                assert record["metric"] == metric
                assert record["join_key"] == row["join_key"]
                ops = record["operands"]
                assert record["formula"].startswith(f"sim_sensitivity[{row['id']}]"), record["formula"]
                assert ops["value"] - ops["baseline"] == ops["delta"], (
                    f"{source}/{row['id']}/{strategy}/{metric}: value − baseline != delta"
                )
                live = next(
                    entry
                    for entry in presentations[source]["scenarios"]["rows"]
                    if entry["id"] == row["id"]
                )["strategies"][strategy][metric]
                assert record["value"] == live["value"]
                assert ops["delta"] == live["delta"]["absolute"]


@pytest.mark.parametrize("source", SOURCES)
def test_every_ledger_field_is_documented_with_its_formula(presentations, source):
    for strategy in ("rule", "optimized"):
        fields = presentations[source]["evidence"]["ledger"][strategy]["fields"]
        for name, record in fields.items():
            assert record["source_file"].startswith("outputs/"), name
            assert record["formula"], name
            if record["derived"]:
                assert record["operands"], f"{source}/{strategy}/{name}: a derived value needs operands"
            else:
                assert "operands" not in record, f"{source}/{strategy}/{name} is not derived"


@pytest.mark.parametrize("source", SOURCES)
def test_published_bundle_is_the_build_we_verified(presentations, source):
    """``presentation.js`` must carry this exact object, not a re-derivation."""
    from bike_agent.presentation.analysis import analysis_payload

    text = (SITE / source / "presentation.js").read_text(encoding="utf-8")
    assert text.startswith("(function(){")
    assert "window.PRESENTATION_DATA" in text
    assert text.rstrip().endswith("})();")

    start = text.index("P[R.source] = ") + len("P[R.source] = ")
    end = text.index(";\n", start)
    published = json.loads(text[start:end].replace("\\u003c", "<"))

    expected = analysis_payload(presentations[source])
    expected = json.loads(json.dumps(expected, ensure_ascii=False))
    assert published == expected


@pytest.mark.parametrize("source", SOURCES)
def test_presentation_bundle_does_not_duplicate_the_player_frames(presentations, source):
    """The workbench reads inventory/unmet/moves from ``replay-data.js``.

    Storing them again would double the site for no new information, so the
    bundle declares them shared and omits them.
    """
    payload = json.loads(
        (SITE / source / "presentation.js")
        .read_text(encoding="utf-8")
        .split("P[R.source] = ", 1)[1]
        .rsplit(";\n", 1)[0]
    )
    assert payload["shared_with_player"] == ["inventory_end", "unmet", "moved", "moves", "kpi"]
    for frame in payload["frames"]:
        for strategy in STRATEGIES:
            assert set(frame["strategies"][strategy]) == {"ledger", "moved_in"}


def test_site_stays_within_the_phase4_budget(presentations):
    total = sum(path.stat().st_size for path in SITE.rglob("*") if path.is_file())
    assert total < 8 * 1024 * 1024, f"replay/ is {total / 1048576:.3f} MiB"
    for source in SOURCES:
        presentation_bytes = (SITE / source / "presentation.js").stat().st_size
        assert presentation_bytes <= 2 * 1024 * 1024, f"{source} presentation.js is {presentation_bytes}"
        workbench_bytes = (SITE / source / "workbench.html").stat().st_size
        assert workbench_bytes <= 256 * 1024, f"{source} workbench.html is {workbench_bytes}"
