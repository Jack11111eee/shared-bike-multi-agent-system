"""Explainable imbalance risk scoring (F4 / D7).

Formula (also documented in DECISIONS.md and REPORT.md):

    net          = pred_out - pred_in                 (positive = net outflow)
    magnitude    = |net|
    demand       = pred_out + pred_in
    volatility   = std(net_flow) over the training history of the cell
    score        = 0.60 * norm(magnitude)
                 + 0.25 * norm(demand)
                 + 0.15 * norm(volatility)

``norm`` is min-max normalisation across the ranked cells of one time bucket.
The sign convention is fixed: net outflow -> replenish list, net inflow ->
clear list.  Reversing it is the single most damaging mistake in this project,
so it is covered by an explicit unit test.
"""

from __future__ import annotations

from typing import Any

import numpy as np
import pandas as pd

WEIGHTS = {"magnitude": 0.60, "demand": 0.25, "volatility": 0.15}
DIRECTIONS = ("replenish", "clear")


def _normalise(values: pd.Series) -> pd.Series:
    maximum = float(values.max()) if len(values) else 0.0
    if maximum <= 0:
        return pd.Series(0.0, index=values.index)
    return values / maximum


def select_risk_bucket(
    forecast: pd.DataFrame,
    panel: pd.DataFrame,
    min_demand_share: float = 0.1,
) -> pd.Timestamp:
    """Pick the time bucket the priority lists are scored for.

    The lists answer "what should we dispatch next", so the bucket must be a
    real, representative time unit.  Taking the last forecast bucket blindly is
    wrong when the data ends mid-night: that bucket can be a near-empty tail.
    We therefore take the last bucket whose observed demand is at least
    ``min_demand_share`` of the peak bucket demand, falling back to the last
    forecast bucket when nothing qualifies.
    """
    totals = panel.groupby("time_bucket")[["out_count", "in_count"]].sum().sum(axis=1)
    forecast_buckets = pd.DatetimeIndex(sorted(forecast["time_bucket"].unique()))
    totals = totals.reindex(forecast_buckets).fillna(0.0)
    peak = float(totals.max())
    if peak <= 0:
        return pd.Timestamp(forecast_buckets.max())
    eligible = totals.loc[totals >= peak * float(min_demand_share)]
    if eligible.empty:
        return pd.Timestamp(forecast_buckets.max())
    return pd.Timestamp(eligible.index.max())


def build_risk_table(
    forecast: pd.DataFrame,
    panel: pd.DataFrame,
    train_buckets: pd.DatetimeIndex,
    time_bucket: pd.Timestamp | None = None,
    min_demand_share: float = 0.1,
) -> pd.DataFrame:
    """Score every cell for one time bucket."""
    if time_bucket is None:
        time_bucket = select_risk_bucket(forecast, panel, min_demand_share)
    time_bucket = pd.Timestamp(time_bucket)
    current = forecast.loc[forecast["time_bucket"] == time_bucket].copy()
    if current.empty:
        raise ValueError(f"no forecast rows for time bucket {time_bucket}")

    history = panel.loc[panel["time_bucket"].isin(train_buckets)]
    volatility = (
        history.groupby("cell_id", observed=True)["net_flow"]
        .std(ddof=0)
        .rename("history_volatility")
    )

    table = current.loc[:, ["cell_id", "time_bucket", "pred_out", "pred_in"]].copy()
    table["pred_net_flow"] = table["pred_out"] - table["pred_in"]
    table["pred_demand"] = table["pred_out"] + table["pred_in"]
    table["history_volatility"] = table["cell_id"].map(volatility).fillna(0.0)

    table["magnitude"] = table["pred_net_flow"].abs()
    table["mag_norm"] = _normalise(table["magnitude"])
    table["demand_norm"] = _normalise(table["pred_demand"])
    table["vol_norm"] = _normalise(table["history_volatility"])
    table["score"] = (
        WEIGHTS["magnitude"] * table["mag_norm"]
        + WEIGHTS["demand"] * table["demand_norm"]
        + WEIGHTS["volatility"] * table["vol_norm"]
    )
    table["direction"] = np.where(table["pred_net_flow"] > 0, "replenish", "clear")
    table["explanation"] = [
        _explain_row(row) for row in table.itertuples(index=False)
    ]
    return table.sort_values(["score", "magnitude", "cell_id"], ascending=[False, False, True]).reset_index(drop=True)


def _explain_row(row: Any) -> str:
    return (
        f"net_outflow={row.pred_net_flow:+.2f} "
        f"(pred_out={row.pred_out:.2f}, pred_in={row.pred_in:.2f}); "
        f"demand={row.pred_demand:.2f}; volatility={row.history_volatility:.2f}; "
        f"normalised[mag={row.mag_norm:.3f}, demand={row.demand_norm:.3f}, vol={row.vol_norm:.3f}]; "
        f"score=0.60*{row.mag_norm:.3f}+0.25*{row.demand_norm:.3f}+0.15*{row.vol_norm:.3f}={row.score:.4f}"
    )


def priority_lists(table: pd.DataFrame, top_k: int | None = None) -> dict[str, pd.DataFrame]:
    """Split the scored table into replenish (net outflow) and clear (net inflow)."""
    replenish = table.loc[table["pred_net_flow"] > 0].copy()
    clear = table.loc[table["pred_net_flow"] < 0].copy()
    replenish = replenish.sort_values(["score", "pred_net_flow"], ascending=[False, False])
    clear = clear.sort_values(["score", "pred_net_flow"], ascending=[False, True])
    if top_k is not None:
        replenish = replenish.head(top_k)
        clear = clear.head(top_k)
    for name, frame in (("replenish", replenish), ("clear", clear)):
        frame.insert(0, "rank", np.arange(1, len(frame) + 1))
    return {"replenish": replenish.reset_index(drop=True), "clear": clear.reset_index(drop=True)}


PRIORITY_COLUMNS = [
    "rank",
    "cell_id",
    "score",
    "pred_net_flow",
    "pred_out",
    "pred_in",
    "explanation",
    "time_bucket",
    "pred_demand",
    "history_volatility",
    "mag_norm",
    "demand_norm",
    "vol_norm",
]
