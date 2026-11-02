"""F22: the prediction series the workbench plots, built once per source.

The authority is ``outputs/forecast.<source>.parquet``.  This module does not
recompute a forecast, a baseline or a metric; it *reshapes* the file into the
three series the page draws and records how each number can be re-derived:

* ``actual``    -- the observed counts (integers, exact)
* ``baseline``  -- the seasonal-mean baseline (float64)
* ``pred``      -- the model prediction (float64)

``net`` is never stored: it is ``out - in``, and the parquet's own
``*_net_flow`` columns are exactly that subtraction (verified in the phase-5
check), so deriving it keeps one derivation instead of two.

Encoding.  The float series are published as base64 little-endian float64 so
they round-trip bit-exactly; 38k values per series is 300 KB raw, and rounding
them to ten significant digits would have been 1.3 MB of JSON that no longer
equals the parquet.  ``actual`` stays a plain JSON integer array so a reader can
see the observations without decoding anything.

Missing points are NaN in the float arrays and ``null`` in the integer arrays --
never zero.  The page shows an empty state for them, and :func:`local_metrics`
counts only the points that actually exist.
"""

from __future__ import annotations

import base64
import math
from typing import Any, Iterable

import numpy as np
import pandas as pd

#: Metric keys the page exposes, in the order the switches show them.
METRICS: tuple[str, ...] = ("out", "in", "net")
#: Series keys, in drawing order.
SERIES: tuple[str, ...] = ("actual", "baseline", "pred")
#: How each metric maps onto the parquet's columns.
COLUMNS: dict[str, dict[str, str]] = {
    "out": {"actual": "actual_out", "baseline": "baseline_out", "pred": "pred_out", "net": "actual_net_flow"},
    "in": {"actual": "actual_in", "baseline": "baseline_in", "pred": "pred_in", "net": "actual_net_flow"},
}
#: Human labels, kept here so the page and the report cannot drift apart.
METRIC_LABELS = {"out": "借出（次/时段）", "in": "归还（次/时段）", "net": "净流量（借出 − 归还）"}
SERIES_LABELS = {"actual": "实际", "baseline": "季节均值基线", "pred": "模型预测"}
#: Sign convention for net flow, stated on the page and in the check.  It matches
#: the pipeline's own definition (`prep/aggregate.py`: `net_flow = out - in`,
#: REQUIREMENTS F4: 净流出为正), so a positive net is a cell that drained bikes.
NET_CONVENTION = "net = out − in（正 = 净流出，负 = 净流入）"


def _b64(values: np.ndarray) -> str:
    return base64.b64encode(np.ascontiguousarray(values, dtype="<f8").tobytes()).decode("ascii")


def _matrix(df: pd.DataFrame, cell_ids: list[str], buckets: list[pd.Timestamp], column: str) -> np.ndarray:
    """``[cell][bucket]`` with NaN where the parquet has no row.

    A missing row is *not* a zero: the page must be able to say "no prediction
    here" rather than draw a point on the axis.
    """
    out = np.full((len(cell_ids), len(buckets)), np.nan, dtype=np.float64)
    if column not in df.columns:
        return out
    position = {cell: index for index, cell in enumerate(cell_ids)}
    bucket_position = {bucket: index for index, bucket in enumerate(buckets)}
    for cell, bucket, value in zip(df["cell_id"], df["time_bucket"], df[column]):
        row = position.get(cell)
        column_index = bucket_position.get(pd.Timestamp(bucket))
        if row is None or column_index is None:
            continue
        out[row, column_index] = float(value)
    return out


def _metric_matrices(
    df: pd.DataFrame, cell_ids: list[str], buckets: list[pd.Timestamp], metric: str
) -> dict[str, np.ndarray]:
    if metric == "net":
        return {
            "actual": _matrix(df, cell_ids, buckets, "actual_out") - _matrix(df, cell_ids, buckets, "actual_in"),
            "baseline": _matrix(df, cell_ids, buckets, "baseline_out") - _matrix(df, cell_ids, buckets, "baseline_in"),
            "pred": _matrix(df, cell_ids, buckets, "pred_out") - _matrix(df, cell_ids, buckets, "pred_in"),
        }
    columns = COLUMNS[metric]
    return {series: _matrix(df, cell_ids, buckets, columns[series]) for series in SERIES}


def local_metrics(actual: np.ndarray, pred: np.ndarray) -> dict[str, Any]:
    """MAE over the points that actually exist, plus how many there were.

    Returned per cell and for the whole source.  ``n`` is carried because a MAE
    without its sample count invites the reader to compare it to a source-level
    number computed over a different set of points.
    """
    paired = ~(np.isnan(actual) | np.isnan(pred))
    n = int(paired.sum())
    if n == 0:
        return {"mae": None, "n": 0}
    return {"mae": float(np.abs(pred[paired] - actual[paired]).mean()), "n": n}


def build_forecast(config: dict[str, Any], layout: Any, source: str, cell_ids: Iterable[str]) -> dict[str, Any]:
    """The published payload for one source (``replay/<source>/forecast.js``)."""
    path = layout.forecast_path(source)
    if not path.exists():
        raise FileNotFoundError(
            f"missing {path}; run `bike-agent forecast --source {source}` (or `bike-agent repro`) first"
        )
    df = pd.read_parquet(path)
    df["time_bucket"] = pd.to_datetime(df["time_bucket"])
    df = df.sort_values(["time_bucket", "cell_id"], kind="mergesort")

    cells = list(cell_ids)
    present = set(df["cell_id"].unique())
    unknown = sorted(present - set(cells))
    if unknown:
        raise ValueError(
            f"{source}: forecast carries cells that are not on this grid: {unknown[:5]}"
        )

    buckets = [pd.Timestamp(value) for value in sorted(df["time_bucket"].unique())]
    bucket_labels = [bucket.strftime("%Y-%m-%d %H:%M:%S") for bucket in buckets]

    # The page positions the forecast cursor by frame index, so the two bucket
    # lists must be the same list -- not merely the same length.  A shifted or
    # renamed bucket would otherwise put the wrong prediction under the map
    # without any error.  F22-1: "无法关联时明确失败".
    replay_path = layout.replay_path(source, "rule")
    if not replay_path.exists():
        raise FileNotFoundError(
            f"missing {replay_path}; the forecast cannot be aligned to a replay. "
            f"Run `bike-agent simulate --strategy rule --source {source}` first."
        )
    replay_buckets = [
        pd.Timestamp(value)
        for value in sorted(pd.read_parquet(replay_path, columns=["time_bucket"])["time_bucket"].unique())
    ]
    if replay_buckets != buckets:
        first_diff = next(
            (i for i, (a, b) in enumerate(zip(replay_buckets, buckets)) if a != b),
            min(len(replay_buckets), len(buckets)),
        )
        raise ValueError(
            f"{source}: forecast buckets do not line up with {replay_path.name} "
            f"({len(buckets)} forecast vs {len(replay_buckets)} replay; first difference at "
            f"index {first_diff}). The page aligns them by position, so publishing would "
            f"show the wrong prediction under the map."
        )

    payload: dict[str, Any] = {
        "source": source,
        "source_file": f"outputs/forecast.{source}.parquet",
        "model": str(df["model"].iloc[0]) if "model" in df.columns and len(df) else "unknown",
        "n_cells": len(cells),
        "n_buckets": len(buckets),
        "buckets": bucket_labels,
        "cell_ids": cells,
        "encoding": {
            "floats": "base64 little-endian float64, row-major [cell][bucket]",
            "missing": "NaN in the float arrays, null in the integer arrays",
            "derived": "net = out - in (never stored)",
        },
        "labels": {"metrics": METRIC_LABELS, "series": SERIES_LABELS, "net": NET_CONVENTION},
        "scope_note": (
            "只包含 forecast 文件覆盖的测试段；不补训练期、不插值、不前向填充。"
            "两条策略消费同一份需求预测，预测不随调度策略变化。"
        ),
        "metrics": {},
    }

    for metric in METRICS:
        matrices = _metric_matrices(df, cells, buckets, metric)
        block: dict[str, Any] = {
            "series": {},
            "summary": {},
            "local": {},
        }
        if metric == "net":
            # net is published as its summary and per-cell error only: the page
            # derives the series from out and in, so the 2 MB budget is spent on
            # two series rather than three copies of the same information.
            block["derived_from"] = ["out", "in"]
            block["derived_rule"] = "net = out − in, applied to each series independently"
        for series in SERIES:
            matrix = matrices[series]
            if metric != "net":
                if series == "actual":
                    # Integers stay readable: a reader can check the observations
                    # without decoding base64, and exactness is free in JSON.
                    block["series"][series] = {
                        "kind": "int",
                        "values": [
                            [None if math.isnan(value) else int(value) for value in row] for row in matrix
                        ],
                    }
                else:
                    block["series"][series] = {"kind": "float64-b64", "values": _b64(matrix.reshape(-1))}
            # Source-wide series: sum over the cells present at that bucket.  The
            # count travels with it so "全源汇总" can say how many cells it saw.
            counts = (~np.isnan(matrix)).sum(axis=0)
            totals = np.where(np.isnan(matrix), 0.0, matrix).sum(axis=0)
            block["summary"][series] = {
                "values": [
                    (None if counts[index] == 0 else (int(totals[index]) if series == "actual" else float(totals[index])))
                    for index in range(len(buckets))
                ],
                "n_cells": [int(value) for value in counts],
            }
        block["local"] = {
            "per_cell": [
                local_metrics(matrices["actual"][index], matrices["pred"][index]) for index in range(len(cells))
            ],
            "source": local_metrics(matrices["actual"], matrices["pred"]),
            "definition": "MAE = mean(|pred − actual|) over the paired test points of that cell; n = 配对数",
        }
        payload["metrics"][metric] = block

    meta_path = layout.forecast_meta_path(source)
    metrics_path = layout.outputs / f"forecast_metrics.{source}.json"
    for required in (metrics_path,):
        if not required.exists():
            raise FileNotFoundError(
                f"missing {required}; the page cites the source-wide evaluation, so it cannot "
                f"be published without it. Run `bike-agent forecast --source {source}`."
            )
    payload["evaluation"] = {
        "meta": _read_json(meta_path),
        "metrics": _read_json(metrics_path),
        "note": "MAE 是点估计误差，不是预测区间；全源评估数字直接引用既有评估产物。",
    }
    return payload


def _read_json(path: Any) -> Any:
    import json

    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


def forecast_js(payload: dict[str, Any]) -> str:
    """A classic script registering one source; ``file://`` cannot fetch."""
    import json

    body = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).replace("<", "\\u003c")
    return (
        "(function(){\n"
        "  var store = window.FORECAST_DATA = window.FORECAST_DATA || {};\n"
        f"  store[{json.dumps(payload['source'])}] = {body};\n"
        "})();"
    )
