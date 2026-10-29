"""Figures for the report (F6-2).

Five figure families are produced per source: demand heatmap, peak-hour
distribution, forecast vs actual, priority lists and strategy comparison.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt  # noqa: E402
import pandas as pd  # noqa: E402

from bike_agent.config import Layout  # noqa: E402


def _save(fig: plt.Figure, path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fig.tight_layout()
    fig.savefig(path, dpi=150)
    plt.close(fig)


def demand_heatmap(panel: pd.DataFrame, cells: pd.DataFrame, source: str, path: Path) -> None:
    totals = panel.groupby("cell_id", observed=True)["out_count"].sum().rename("total_out").reset_index()
    grid = cells.merge(totals, on="cell_id", how="left").fillna({"total_out": 0})
    matrix = grid.pivot(index="iy", columns="ix", values="total_out").fillna(0).to_numpy()
    fig, ax = plt.subplots(figsize=(6.4, 5.2))
    image = ax.imshow(matrix, origin="lower", cmap="YlOrRd", aspect="auto")
    ax.set_title(f"{source.upper()} · total departures per grid cell")
    ax.set_xlabel("grid x")
    ax.set_ylabel("grid y")
    fig.colorbar(image, ax=ax, label="departures (all buckets)")
    _save(fig, path)


def peak_hours(panel: pd.DataFrame, source: str, path: Path) -> None:
    by_hour = panel.groupby(panel["time_bucket"].dt.hour)["out_count"].sum()
    fig, ax = plt.subplots(figsize=(6.4, 3.6))
    ax.bar(by_hour.index.astype(int), by_hour.to_numpy(), color="#2b6cb0")
    ax.set_title(f"{source.upper()} · departures by hour of day")
    ax.set_xlabel("hour")
    ax.set_ylabel("departures")
    ax.set_xticks(sorted(by_hour.index.astype(int)))
    _save(fig, path)


def forecast_vs_actual(forecast: pd.DataFrame, source: str, path: Path) -> None:
    sample = forecast
    if len(sample) > 8000:
        sample = sample.sample(n=8000, random_state=0)
    fig, ax = plt.subplots(figsize=(5.6, 5.2))
    ax.scatter(sample["actual_out"], sample["pred_out"], s=6, alpha=0.35, label="departures")
    ax.scatter(sample["actual_in"], sample["pred_in"], s=6, alpha=0.35, label="returns")
    limit = float(max(sample["actual_out"].max(), sample["pred_out"].max(), sample["actual_in"].max(), sample["pred_in"].max(), 1))
    ax.plot([0, limit], [0, limit], color="black", linewidth=1, linestyle="--", label="y = x")
    ax.set_xlabel("actual")
    ax.set_ylabel("predicted")
    ax.set_title(f"{source.upper()} · forecast vs actual (test buckets)")
    ax.legend()
    _save(fig, path)


def priority_lists(replenish: pd.DataFrame, clear: pd.DataFrame, source: str, path: Path) -> None:
    fig, axes = plt.subplots(1, 2, figsize=(9.2, 4.2))
    top_rep = replenish.head(10).iloc[::-1]
    axes[0].barh(top_rep["cell_id"], top_rep["pred_net_flow"], color="#c53030")
    axes[0].set_title("top replenish (net outflow)")
    axes[0].set_xlabel("predicted net outflow (bikes)")
    top_clear = clear.head(10).iloc[::-1]
    axes[1].barh(top_clear["cell_id"], top_clear["pred_net_flow"], color="#2f855a")
    axes[1].set_title("top clear (net inflow)")
    axes[1].set_xlabel("predicted net flow (bikes)")
    fig.suptitle(f"{source.upper()} · dispatch priority lists")
    _save(fig, path)


def strategy_comparison(rule: dict[str, Any], optimized: dict[str, Any], source: str, path: Path) -> None:
    fig, axes = plt.subplots(1, 2, figsize=(9.2, 3.8))
    labels = ["S-RULE", "S-OPT"]
    unmet = [rule["unmet_requests"], optimized["unmet_requests"]]
    distance = [rule["distance_bike_km"], optimized["distance_bike_km"]]
    axes[0].bar(labels, unmet, color=["#4a5568", "#2b6cb0"])
    axes[0].set_title("unmet borrow requests (simulated)")
    axes[1].bar(labels, distance, color=["#4a5568", "#2b6cb0"])
    axes[1].set_title("dispatch distance (bike-km, simulated)")
    fig.suptitle(f"{source.upper()} · strategy comparison")
    _save(fig, path)


def significance_forest(data: dict[str, Any], path: Path) -> None:
    """Percent change with its 95% interval, one row per tested claim.

    Raw units are not comparable across sources (XM MAE moves by 0.3, SH by
    0.02), so every row is expressed as a percentage change against its own
    reference.  Rows whose interval crosses zero are drawn hollow: the point
    estimate exists, but the data cannot separate it from no change.
    """
    rows: list[tuple[str, float, float, float, bool]] = []
    for source, entry in data.items():
        label = source.upper()
        metrics = entry.get("forecast_metrics") or {}
        significance = (entry.get("forecast_meta") or {}).get("significance") or {}
        for target, name in (("out_count", "departures"), ("in_count", "returns")):
            stats = significance.get(target)
            try:
                reference = float(metrics["baseline"][target]["mae"])
                candidate = float(metrics["model"][target]["mae"])
            except (KeyError, TypeError):
                continue
            if not stats or stats.get("ci_low") is None or reference == 0:
                continue
            scale = 100.0 / reference
            rows.append(
                (
                    f"{label} · forecast {name}",
                    (candidate - reference) * scale,
                    stats["ci_low"] * scale,
                    stats["ci_high"] * scale,
                    bool(stats["significant"]),
                )
            )

        comparison = entry.get("comparison") or {}
        stats = comparison.get("significance") or {}
        try:
            reference = float(comparison["rule"]["composite_cost"])
            candidate = float(comparison["optimized"]["composite_cost"])
        except (KeyError, TypeError):
            continue
        # Prefer the total-scaled interval: it shares units with the total cost
        # the reports quote.  The raw interval is per-bucket and would draw a
        # needle-thin bar that sits nowhere near the point estimate.
        if stats.get("ci_low_total") is not None and reference != 0:
            scale = 100.0 / reference
            rows.append(
                (
                    f"{label} · dispatch cost",
                    (candidate - reference) * scale,
                    stats["ci_low_total"] * scale,
                    stats["ci_high_total"] * scale,
                    bool(stats["significant"]),
                )
            )

    fig, ax = plt.subplots(figsize=(9.0, 0.55 * len(rows) + 1.8))
    positions = list(range(len(rows)))[::-1]
    for y, (name, mean, low, high, significant) in zip(positions, rows):
        color = "#c53030" if significant else "#718096"
        ax.plot([low, high], [y, y], color=color, linewidth=2, solid_capstyle="butt")
        ax.plot([low, low], [y - 0.12, y + 0.12], color=color, linewidth=1.5)
        ax.plot([high, high], [y - 0.12, y + 0.12], color=color, linewidth=1.5)
        ax.plot(
            [mean],
            [y],
            marker="o",
            markersize=7,
            color=color if significant else "white",
            markeredgecolor=color,
            markeredgewidth=1.6,
            zorder=3,
        )
    ax.axvline(0, color="black", linewidth=1, linestyle="--")
    ax.set_yticks(positions)
    ax.set_yticklabels([row[0] for row in rows])
    ax.set_xlabel("change vs reference (%), 95% bootstrap interval")
    ax.set_title("Is the difference real?  filled = interval excludes zero")
    ax.grid(axis="x", alpha=0.25)
    ax.set_axisbelow(True)
    _save(fig, path)


def generate_figures(
    layout: Layout,
    source: str,
    panel: pd.DataFrame,
    cells: pd.DataFrame,
    forecast: pd.DataFrame,
    replenish: pd.DataFrame,
    clear: pd.DataFrame,
    rule: dict[str, Any],
    optimized: dict[str, Any],
) -> list[Path]:
    paths = [
        layout.figures / f"demand_heatmap.{source}.png",
        layout.figures / f"peak_hours.{source}.png",
        layout.figures / f"forecast_vs_actual.{source}.png",
        layout.figures / f"priority_lists.{source}.png",
        layout.figures / f"strategy_comparison.{source}.png",
    ]
    demand_heatmap(panel, cells, source, paths[0])
    peak_hours(panel, source, paths[1])
    forecast_vs_actual(forecast, source, paths[2])
    priority_lists(replenish, clear, source, paths[3])
    strategy_comparison(rule, optimized, source, paths[4])
    return paths
