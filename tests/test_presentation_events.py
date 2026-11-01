"""M20 / F17: key events over every replay bucket.

F17-1 recomputes all four event families from the raw CSVs for every frame of
both sources.  F17-2 exercises the edge cases on small synthetic arrays that
never reach the shipped site.  F17-3 drives the published state machine for
*every* generated event and checks the frame, the time and the components the
jump is supposed to move.
"""

from __future__ import annotations

from html.parser import HTMLParser
from pathlib import Path

import pandas as pd
import pytest

from bike_agent.config import Layout
from bike_agent.presentation.analysis import EVENT_TYPES, build_events

ROOT = Path(__file__).resolve().parents[1]
SITE = ROOT / "replay"
SOURCES = ("xm", "sh")


class Page(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.elements: list[tuple[str, dict[str, str]]] = []

    def handle_starttag(self, tag, attrs):
        self.elements.append((tag, {key: (value or "") for key, value in attrs}))

    def handle_startendtag(self, tag, attrs):
        self.handle_starttag(tag, attrs)

    def by_attr(self, name: str) -> list[dict[str, str]]:
        return [attrs for _, attrs in self.elements if name in attrs]

    def by_class(self, name: str) -> list[dict[str, str]]:
        return [attrs for _, attrs in self.elements if name in attrs.get("class", "").split()]


# -- F17-1 ------------------------------------------------------------------
@pytest.mark.parametrize("source", SOURCES)
def test_events_are_recomputed_from_the_csv_for_every_frame(presentations, source):
    layout = Layout()
    rule = pd.read_csv(layout.sim_path(source, "rule", "csv"))
    optimized = pd.read_csv(layout.sim_path(source, "optimized", "csv"))
    rule["time_bucket"] = pd.to_datetime(rule["time_bucket"])
    optimized["time_bucket"] = pd.to_datetime(optimized["time_bucket"])
    rule = rule.sort_values("time_bucket").reset_index(drop=True)
    optimized = optimized.sort_values("time_bucket").reset_index(drop=True)
    assert list(rule["time_bucket"]) == list(optimized["time_bucket"])

    gain = (rule["unmet_requests"] - optimized["unmet_requests"]).to_numpy()

    def earliest_max(values):
        return int(values.argmax()), int(values.max())

    def earliest_min(values):
        return int(values.argmin()), int(values.min())

    expected = {}
    index, value = earliest_max(rule["unmet_requests"].to_numpy())
    expected["rule_unmet_max"] = (index, value) if value > 0 else (None, 0)
    index, value = earliest_max(rule["bikes_moved"].to_numpy())
    expected["rule_moved_max"] = (index, value) if value > 0 else (None, 0)
    index, value = earliest_max(gain)
    expected["opt_gain_max"] = (index, value) if value > 0 else (None, 0)
    index, value = earliest_min(gain)
    expected["opt_regression_max"] = (index, value) if value < 0 else (None, 0)

    events = {event["type"]: event for event in presentations[source]["events"]}
    assert set(events) == set(EVENT_TYPES)
    for kind, (index, value) in expected.items():
        event = events[kind]
        assert event["frame"] == index, f"{source}/{kind}: frame {event['frame']} != {index}"
        assert event["value"] == value, f"{source}/{kind}: value {event['value']} != {value}"
        if index is not None:
            assert event["time_bucket"] == rule["time_bucket"].iloc[index].strftime("%Y-%m-%d %H:%M:%S")
        else:
            assert event["status"] in {"all_zero", "no_improvement", "no_regression"}

    # Ties must resolve to the earliest bucket, and the count must be honest.
    for kind, values in (
        ("rule_unmet_max", rule["unmet_requests"].to_numpy()),
        ("rule_moved_max", rule["bikes_moved"].to_numpy()),
    ):
        event = events[kind]
        if event["status"] == "ok":
            assert event["tie_count"] == int((values == values.max()).sum())
            assert event["frame"] == int(values.argmax())


@pytest.mark.parametrize("source", SOURCES)
def test_gain_events_only_use_the_matching_sign(presentations, source):
    events = {event["type"]: event for event in presentations[source]["events"]}
    gain_event = events["opt_gain_max"]
    regression_event = events["opt_regression_max"]
    if gain_event["status"] == "ok":
        assert gain_event["value"] > 0, "an 'improvement' event must have gain > 0"
    if regression_event["status"] == "ok":
        assert regression_event["value"] < 0, "a 'regression' event must have gain < 0"
    # Never the same frame labelled both ways.
    if gain_event["status"] == "ok" and regression_event["status"] == "ok":
        assert gain_event["frame"] != regression_event["frame"]


@pytest.mark.parametrize("source", SOURCES)
def test_events_render_one_record_per_frame_not_one_per_label(presentations, source):
    events = presentations[source]["events"]
    frames = [event["frame"] for event in events if event["frame"] is not None]
    # Several labels may share a frame; that must not create extra data frames.
    assert len(presentations[source]["frames"]) == presentations[source]["n_frames"]
    assert len(frames) <= len(EVENT_TYPES), "four labels can point at four frames at most"
    assert set(frames) <= set(range(presentations[source]["n_frames"]))
    page = Page()
    page.feed((SITE / source / "workbench.html").read_text(encoding="utf-8"))
    items = page.by_attr("data-event-id")
    assert len(items) == len(EVENT_TYPES)
    assert {attrs["data-event-type"] for attrs in items} == set(EVENT_TYPES)


# -- F17-2 (synthetic) ------------------------------------------------------
def test_tie_goes_to_the_earliest_bucket():
    events = build_events(
        "toy",
        ["b0", "b1", "b2"],
        [5, 5, 1],
        [5, 5, 1],
        [3, 3, 3],
    )
    unmet = next(event for event in events if event["type"] == "rule_unmet_max")
    assert unmet["frame"] == 0
    assert unmet["tie_count"] == 2
    assert unmet["status"] == "ok"
    moved = next(event for event in events if event["type"] == "rule_moved_max")
    assert moved["frame"] == 0 and moved["tie_count"] == 3


def test_all_zero_is_reported_as_zero_not_as_an_event():
    events = build_events("toy", ["b0", "b1"], [0, 0], [0, 0], [0, 0])
    for kind in ("rule_unmet_max", "rule_moved_max"):
        event = next(item for item in events if item["type"] == kind)
        assert event["status"] == "all_zero"
        assert event["frame"] is None
        assert event["value"] == 0
        assert event["note"] == "该指标全程为零"
    assert next(e for e in events if e["type"] == "opt_gain_max")["status"] == "no_improvement"
    assert next(e for e in events if e["type"] == "opt_regression_max")["status"] == "no_regression"


def test_only_improvement_has_no_regression_event():
    events = build_events("toy", ["b0", "b1"], [5, 3], [3, 2], [1, 1])
    gain = next(event for event in events if event["type"] == "opt_gain_max")
    regression = next(event for event in events if event["type"] == "opt_regression_max")
    assert gain["status"] == "ok" and gain["frame"] == 0 and gain["value"] == 2
    assert regression["status"] == "no_regression"
    assert regression["frame"] is None


def test_only_regression_has_no_improvement_event():
    events = build_events("toy", ["b0", "b1"], [2, 2], [5, 4], [1, 1])
    gain = next(event for event in events if event["type"] == "opt_gain_max")
    regression = next(event for event in events if event["type"] == "opt_regression_max")
    assert gain["status"] == "no_improvement"
    assert regression["status"] == "ok" and regression["frame"] == 0 and regression["value"] == -3


def test_no_difference_is_its_own_state():
    events = build_events("toy", ["b0", "b1"], [4, 4], [4, 4], [2, 2])
    assert next(e for e in events if e["type"] == "opt_gain_max")["status"] == "no_improvement"
    assert next(e for e in events if e["type"] == "opt_regression_max")["status"] == "no_regression"


def test_several_labels_can_share_one_frame():
    # Frame 1 is the max for unmet, for moved and for gain at the same time;
    # frame 2 is the only regression.  Three labels, two data frames.
    events = build_events("toy", ["b0", "b1", "b2"], [1, 9, 2], [1, 4, 4], [0, 30, 0])
    assert [event["frame"] for event in events] == [1, 1, 1, 2]
    assert len({event["frame"] for event in events}) == 2, "one frame per bucket, not per label"


# -- F17-3 ------------------------------------------------------------------
@pytest.mark.parametrize("source", SOURCES)
def test_every_generated_event_jumps_to_its_own_frame_and_pauses(source):
    from js_harness import run_node

    result = run_node(
        f"""
const data = P['{source}'];
const out = [];
for (const event of data.events) {{
  core.setPlaying(true);
  core.setFrame(0);
  const ok = core.jumpToEvent(event.id);
  const state = core.get();
  const frame = data.frames[state.frame];
  out.push({{
    id: event.id, status: event.status, accepted: ok, frame: state.frame,
    playing: state.playing,
    frame_bucket: frame ? frame.time_bucket : null,
    event_bucket: event.time_bucket,
    delta_summary: frame ? frame.delta_summary : null,
    event_summary: event.status === 'ok' ? {{ net: frame ? frame.delta_summary.net : null }} : null
  }});
}}
console.log(JSON.stringify({{ events: out, unknown: core.jumpToEvent('nope') }}));
""",
        sources=(source,),
    )
    assert result["unknown"] is False
    assert len(result["events"]) == len(EVENT_TYPES)
    for entry in result["events"]:
        if entry["status"] != "ok":
            assert entry["accepted"] is False, f"{entry['id']}: an empty event must not jump"
            continue
        assert entry["accepted"] is True
        assert entry["playing"] is False, f"{entry['id']}: jumping must pause playback"
        assert entry["frame_bucket"] == entry["event_bucket"], entry["id"]
        assert entry["delta_summary"] is not None


@pytest.mark.parametrize("source", SOURCES)
def test_event_buttons_carry_the_frame_they_jump_to(presentations, source):
    page = Page()
    page.feed((SITE / source / "workbench.html").read_text(encoding="utf-8"))
    buttons = {attrs["data-event"]: attrs for attrs in page.by_attr("data-event")}
    for event in presentations[source]["events"]:
        assert event["id"] in buttons
        disabled = buttons[event["id"]].get("disabled") is not None
        assert disabled is (event["status"] != "ok"), (
            f"{event['id']}: an empty event must be disabled, a real one must be clickable"
        )
    # The metric element inside the button carries the value and its provenance.
    metrics = {
        attrs["data-metric"]: attrs
        for attrs in page.by_class("metric")
        if attrs.get("data-source", "").startswith("outputs/sim_")
    }
    assert metrics, "event values must be traceable metrics"
