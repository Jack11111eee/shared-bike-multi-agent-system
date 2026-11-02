"""F22: the prediction payload, checked against the parquet and against the page.

Two layers, deliberately separate:

* the *build* is checked exhaustively here -- every cell, every test bucket, all
  three series, all three metrics, plus the summary, the current error and the
  local MAE recomputed from the parquet with independent code;
* the *page* is checked for the contract it must expose (state machine hooks,
  honesty statements, range labels), while the actual clicking is done in
  ``tests/test_presentation_guide.py``'s node harness and in
  ``scripts/browser_smoke.py`` against the shipped page.

Missing points get their own tests because "absent" is the one value a chart
loves to turn into a zero.
"""

from __future__ import annotations

import base64
import re

import numpy as np
import pandas as pd
import pytest

from bike_agent.config import Layout, load_config
from bike_agent.presentation.analysis import build_all_presentations
from bike_agent.presentation.forecast import (
    SERIES,
    build_forecast,
    forecast_js,
    local_metrics,
)

SOURCES = ("xm", "sh")


@pytest.fixture(scope="module")
def built():
    config = load_config()
    layout = Layout()
    presentations = build_all_presentations(config, layout, SOURCES)
    return {
        source: build_forecast(config, layout, source, presentations[source]["cell_ids"])
        for source in SOURCES
    }


@pytest.fixture(scope="module")
def truth():
    return {source: pd.read_parquet(Layout().forecast_path(source)) for source in SOURCES}


def _decode(block):
    raw = base64.b64decode(block["values"])
    return np.frombuffer(raw, dtype="<f8").astype(np.float64)


def _actual_matrix(block, n, m):
    return np.array(
        [[np.nan if value is None else float(value) for value in row] for row in block["series"]["actual"]["values"]],
        dtype=np.float64,
    ).reshape(n, m)


# -- F22-1 ------------------------------------------------------------------
@pytest.mark.parametrize("source", SOURCES)
def test_every_series_equals_the_parquet(built, truth, source):
    payload = built[source]
    df = truth[source].copy()
    df["time_bucket"] = pd.to_datetime(df["time_bucket"])
    cells = payload["cell_ids"]
    buckets = [pd.Timestamp(value) for value in payload["buckets"]]
    n, m = len(cells), len(buckets)
    cell_index = {cell: i for i, cell in enumerate(cells)}
    bucket_index = {bucket: i for i, bucket in enumerate(buckets)}

    columns = {
        "out": ("actual_out", "baseline_out", "pred_out"),
        "in": ("actual_in", "baseline_in", "pred_in"),
    }
    for metric, (col_actual, col_baseline, col_pred) in columns.items():
        block = payload["metrics"][metric]
        actual = _actual_matrix(block, n, m)
        baseline = _decode(block["series"]["baseline"]).reshape(n, m)
        pred = _decode(block["series"]["pred"]).reshape(n, m)
        for row in df.itertuples():
            i = cell_index[row.cell_id]
            j = bucket_index[pd.Timestamp(row.time_bucket)]
            assert actual[i, j] == getattr(row, col_actual), (metric, row.cell_id, row.time_bucket)
            assert baseline[i, j] == pytest.approx(getattr(row, col_baseline), rel=0, abs=0)
            assert pred[i, j] == pytest.approx(getattr(row, col_pred), rel=0, abs=0)


@pytest.mark.parametrize("source", SOURCES)
def test_net_is_out_minus_in_in_both_the_file_and_the_payload(built, truth, source):
    df = truth[source]
    for prefix in ("actual", "baseline", "pred"):
        assert ((df[f"{prefix}_out"] - df[f"{prefix}_in"]) - df[f"{prefix}_net_flow"]).abs().max() == 0
    payload = built[source]
    m = payload["n_buckets"]
    out_block = payload["metrics"]["out"]
    in_block = payload["metrics"]["in"]
    net_block = payload["metrics"]["net"]
    # The page derives net from out and in, so the published summary must be the
    # same subtraction -- not a third, independently rounded number.
    for series in SERIES:
        out_values = out_block["summary"][series]["values"]
        in_values = in_block["summary"][series]["values"]
        net_values = net_block["summary"][series]["values"]
        for index in range(m):
            if out_values[index] is None or in_values[index] is None:
                assert net_values[index] is None
                continue
            assert net_values[index] == pytest.approx(out_values[index] - in_values[index], abs=1e-9)
    assert net_block["derived_from"] == ["out", "in"]
    assert "net" not in net_block["series"]


# -- F22-2 ------------------------------------------------------------------
@pytest.mark.parametrize("source", SOURCES)
def test_source_summary_is_a_column_sum(built, source):
    payload = built[source]
    n, m = payload["n_cells"], payload["n_buckets"]
    for metric in ("out", "in"):
        block = payload["metrics"][metric]
        for series in SERIES:
            matrix = (
                _actual_matrix(block, n, m)
                if series == "actual"
                else _decode(block["series"][series]).reshape(n, m)
            )
            for j in range(m):
                column = matrix[:, j]
                present = column[~np.isnan(column)]
                want = None if present.size == 0 else float(present.sum())
                got = block["summary"][series]["values"][j]
                if want is None:
                    assert got is None
                else:
                    assert got == pytest.approx(want, abs=1e-9)
                assert block["summary"][series]["n_cells"][j] == int(present.size)


@pytest.mark.parametrize("source", SOURCES)
def test_local_mae_uses_paired_points_and_reports_n(built, source):
    payload = built[source]
    n, m = payload["n_cells"], payload["n_buckets"]
    for metric in ("out", "in"):
        block = payload["metrics"][metric]
        actual = _actual_matrix(block, n, m)
        pred = _decode(block["series"]["pred"]).reshape(n, m)
        for i in range(n):
            paired = ~(np.isnan(actual[i]) | np.isnan(pred[i]))
            want_n = int(paired.sum())
            want = float(np.abs(pred[i][paired] - actual[i][paired]).mean()) if want_n else None
            got = block["local"]["per_cell"][i]
            assert got["n"] == want_n
            if want is None:
                assert got["mae"] is None
            else:
                assert got["mae"] == pytest.approx(want, rel=0, abs=0)


def test_missing_points_stay_missing():
    """A NaN must never be published as 0 -- that is the whole point of the nulls."""
    actual = np.array([1.0, np.nan, 3.0, np.nan])
    pred = np.array([2.0, 5.0, np.nan, np.nan])
    metrics = local_metrics(actual, pred)
    assert metrics["n"] == 1
    assert metrics["mae"] == pytest.approx(1.0)
    empty = local_metrics(np.array([np.nan]), np.array([np.nan]))
    assert empty == {"mae": None, "n": 0}


def test_zero_and_single_point_cases():
    zero = local_metrics(np.array([0.0, 0.0]), np.array([0.0, 0.0]))
    assert zero["n"] == 2 and zero["mae"] == 0.0
    single = local_metrics(np.array([4.0]), np.array([1.0]))
    assert single["n"] == 1 and single["mae"] == pytest.approx(3.0)
    # Positive and negative errors must not cancel in the MAE.
    signed = local_metrics(np.array([0.0, 10.0]), np.array([-5.0, 15.0]))
    assert signed["mae"] == pytest.approx(5.0)


# -- F22-4 ------------------------------------------------------------------
@pytest.mark.parametrize("source", SOURCES)
def test_page_states_the_limits_of_the_prediction_view(built, source):
    page = (Layout().root / "replay" / source / "workbench.html").read_text(encoding="utf-8")
    assert "不是预测区间" in page
    assert "不随调度策略变化" in page
    assert "不补训练期" in page
    assert "95%" not in page
    assert "全源汇总" in page and "当前格" in page
    payload = built[source]
    assert payload["scope_note"].startswith("只包含")
    assert payload["labels"]["net"].startswith("net = out − in")


def test_the_empty_forecast_branch_clears_the_readouts():
    """A cell with no forecast must not keep the previous cell's numbers.

    Every shipped cell has points, so this branch is latent -- but the failure it
    guards against is the one this phase already fixed once: a label or a number
    that disagrees with what is on screen.  Checked on the published script
    because that is the file the browser executes.
    """
    script = (Layout().root / "replay" / "workbench.js").read_text(encoding="utf-8")
    branch = script.split("if (!values) {", 1)[1].split("return;", 1)[0]
    assert "forecast-mae-label" in branch, "空预测时 MAE 标签没有复位"
    assert "forecast-range-label" in branch, "空预测时范围标签没有复位"
    assert "scope=none" in branch, "空预测时读数没有清空"
    for name in ("forecast_mae", "forecast_n", "forecast_error", "forecast_pred"):
        assert name in branch, f"空预测时 {name} 仍显示上一次的值"


@pytest.mark.parametrize("source", SOURCES)
def test_net_labels_state_the_same_direction_as_the_numbers(built, truth, source):
    """The label, the sign and the pipeline must agree -- not just each other's strings.

    A positive ``net`` here is ``out − in``: more bikes left the cell than
    returned, i.e. 净流出.  The page's own labels have to say that, because a
    label saying "归还 − 借出 / 正 = 净流入" next to a +91 value is a wrong
    number to a reader even though the underlying data is right.
    """
    from bike_agent.presentation.forecast import METRIC_LABELS, NET_CONVENTION

    assert METRIC_LABELS["net"] == "净流量（借出 − 归还）"
    assert "net = out − in" in NET_CONVENTION
    assert "正 = 净流出" in NET_CONVENTION and "负 = 净流入" in NET_CONVENTION

    payload = built[source]
    assert payload["labels"]["metrics"]["net"] == METRIC_LABELS["net"]
    assert payload["labels"]["net"] == NET_CONVENTION

    # And the numbers obey the stated direction: where out > in, net is positive.
    df = truth[source]
    drained = df[df["actual_out"] > df["actual_in"]]
    supplied = df[df["actual_out"] < df["actual_in"]]
    assert len(drained) and len(supplied), "test source must contain both directions"
    assert (drained["actual_net_flow"] > 0).all()
    assert (supplied["actual_net_flow"] < 0).all()


@pytest.mark.parametrize("source", SOURCES)
def test_forecast_buckets_are_the_replay_buckets_in_order(built, source):
    """F22-1: the page aligns the chart to the map by position, not by timestamp.

    So the two bucket lists must be the same list.  A shifted or renamed bucket
    would put the wrong prediction under the map with no error anywhere else.
    """
    layout = Layout()
    replay = pd.read_parquet(layout.replay_path(source, "rule"), columns=["time_bucket"])
    replay_buckets = [pd.Timestamp(v) for v in sorted(replay["time_bucket"].unique())]
    assert [pd.Timestamp(v) for v in built[source]["buckets"]] == replay_buckets


def test_a_misaligned_forecast_refuses_to_publish(tmp_path, monkeypatch):
    """The "fail loudly" half of F22-1: mismatched buckets must raise, not publish."""
    from bike_agent.presentation import forecast as forecast_module

    config = load_config()
    layout = Layout()
    cell_ids = sorted(pd.read_parquet(layout.forecast_path("xm"))["cell_id"].unique())
    built = build_forecast(config, layout, "xm", cell_ids)
    assert built["n_buckets"] > 0

    real = forecast_module.pd.read_parquet

    def shifted(path, *args, **kwargs):
        frame = real(path, *args, **kwargs)
        if "replay." in str(path) and "time_bucket" in frame.columns and len(frame):
            # Move every bucket one hour later: same count, different values.
            frame = frame.copy()
            frame["time_bucket"] = pd.to_datetime(frame["time_bucket"]) + pd.Timedelta(hours=1)
        return frame

    monkeypatch.setattr(forecast_module.pd, "read_parquet", shifted)
    with pytest.raises(ValueError) as caught:
        build_forecast(config, layout, "xm", cell_ids)
    assert "do not line up" in str(caught.value)


@pytest.mark.parametrize("source", SOURCES)
def test_payload_fits_the_budget(built, source):
    js = forecast_js(built[source])
    assert len(js.encode("utf-8")) <= 2 * 1024 * 1024


@pytest.mark.parametrize("source", SOURCES)
def test_a_page_only_carries_its_own_source(built, source):
    """F22-3 换源：switching sources is navigation, so nothing can leak across.

    Each workbench loads its own ``replay-data.js`` / ``forecast.js`` /
    ``basemap.js``; the failure mode worth testing is a page referencing the
    *other* source's bundle, and a bundle whose cell list or bucket count
    disagrees with that source's replay parquet.
    """
    other = "sh" if source == "xm" else "xm"
    page = (Layout().root / "replay" / source / "workbench.html").read_text(encoding="utf-8")
    for asset in ("replay-data.js", "forecast.js", "basemap.js"):
        assert asset in page, f"{source} 页面没有加载 {asset}"
    # A cross-source *link* is navigation and is expected; a cross-source
    # *script* would put the other source's numbers on this page.
    scripts = re.findall(r'<script[^>]*\bsrc="([^"]+)"', page)
    assert scripts, f"{source} 页面没有脚本"
    for asset in scripts:
        assert not asset.startswith(("http://", "https://", "//")), asset
        assert other not in asset.split("/"), f"{source} 页面加载了另一源的脚本 {asset}"
        assert (Layout().root / "replay" / source / asset).exists(), f"{source} 缺 {asset}"

    payload = built[source]
    assert payload["source"] == source
    assert payload["n_cells"] == len(payload["cell_ids"])
    replay = pd.read_parquet(Layout().replay_path(source, "rule"))
    assert set(payload["cell_ids"]) == set(replay["cell_id"].unique())
    assert payload["n_buckets"] == replay["time_bucket"].nunique()


def test_payload_is_a_classic_script_not_a_fetch():
    js = forecast_js({"source": "xm", "n_cells": 0, "n_buckets": 0, "metrics": {}})
    assert "window.FORECAST_DATA" in js
    assert "fetch(" not in js
    assert js.strip().endswith("})();")


# -- F22-3 ------------------------------------------------------------------
def test_state_machine_carries_the_forecast_selection():
    """The pure state machine is where the linkage rules live, so check it there.

    ``run_node`` loads the *published* ``workbench-core.js`` -- the same bytes the
    browser executes -- so this is behaviour verification, not a re-implementation.
    """
    from tests.js_harness import node_path, run_node

    if node_path() is None:  # pragma: no cover - environment dependent
        pytest.fail(
            "node is not installed, so the published state machine cannot be exercised. "
            "Install Node.js; skipping would leave F22-3's linkage unverified."
        )
    result = run_node(
        """
const start = core.get();
const okMetric = core.setForecastMetric('net');
const okRange = core.setForecastRange('source');
const badMetric = core.setForecastMetric('bogus');
core.selectCell(core.get().cell || P[core.get().source].cell_ids[1]);
const chosen = core.get().cell;
core.setFrame(2);
const after = core.get();
console.log(JSON.stringify({
  initial: [start.forecastMetric, start.forecastRange],
  okMetric, okRange, badMetric,
  metric: after.forecastMetric, range: after.forecastRange,
  cell: after.cell, chosen, frame: after.frame
}));
""",
        sources=("xm",),
    )
    assert result["initial"] == ["out", "cell"]
    assert result["okMetric"] is True and result["okRange"] is True
    assert result["badMetric"] is False
    assert result["metric"] == "net" and result["range"] == "source"
    # Selecting a cell and changing the frame must not disturb the chart's mode.
    assert result["cell"] == result["chosen"] and result["frame"] == 2
