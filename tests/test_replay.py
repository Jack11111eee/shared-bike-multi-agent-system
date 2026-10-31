"""F8: per-cell replay state must reproduce the persisted step totals exactly.

The replay file is what the F10 front-end draws, so it is only trustworthy if
summing it over cells gives back every number ``sim_<strategy>.<source>.csv``
already reports.  These tests check *every* bucket, not a sample.
"""

from __future__ import annotations

import pandas as pd
import pytest

from bike_agent.config import Layout, load_config
from bike_agent.sim.engine import simulate

# The nine fields F8-2 requires to reconcile, in the order they are compared.
RECONCILED_FIELDS = (
    "borrow_requests",
    "borrow_met",
    "unmet_requests",
    "returns",
    "returns_applied",
    "returns_overflow",
    "bikes_moved",
    "inventory_start",
    "inventory_end",
)


def _scenario() -> tuple[pd.DataFrame, pd.DataFrame, pd.DataFrame, pd.Series]:
    """Three cells, three buckets, with real moves in the first bucket."""
    buckets = [pd.Timestamp("2020-12-21 06:00"), pd.Timestamp("2020-12-21 07:00"), pd.Timestamp("2020-12-21 08:00")]
    cells = pd.DataFrame(
        {
            "cell_id": ["A", "B", "C"],
            "ix": [0, 1, 2],
            "iy": [0, 0, 0],
            "lon": [118.100, 118.102, 118.104],
            "lat": [24.500, 24.500, 24.500],
        }
    )
    # (out, in) per cell per bucket
    counts = {
        "A": [(6, 0), (4, 1), (7, 0)],
        "B": [(0, 3), (2, 2), (1, 4)],
        "C": [(1, 1), (3, 0), (0, 2)],
    }
    panel_rows, forecast_rows = [], []
    for bucket in buckets:
        for cell, series in counts.items():
            index = buckets.index(bucket)
            out, inn = series[index]
            panel_rows.append(
                {"cell_id": cell, "time_bucket": bucket, "out_count": out, "in_count": inn, "net_flow": out - inn}
            )
            forecast_rows.append({"cell_id": cell, "time_bucket": bucket, "pred_out": float(out), "pred_in": float(inn)})
    capacities = pd.Series({"A": 10, "B": 10, "C": 10})
    return pd.DataFrame(panel_rows), pd.DataFrame(forecast_rows), cells, capacities


def _config(budget: int = 5) -> dict:
    config = load_config()
    config["simulation"] = dict(config["simulation"])
    config["simulation"]["max_moves_per_bucket"] = budget
    config["simulation"]["max_candidate_surplus_per_deficit"] = 5
    return config


@pytest.mark.parametrize("strategy", ["rule", "optimized"])
def test_replay_has_exactly_one_row_per_cell_and_bucket(strategy):
    panel, forecast, cells, capacities = _scenario()
    result = simulate(panel, forecast, cells, capacities, _config(), strategy)

    states = result.cell_states
    n_buckets = panel["time_bucket"].nunique()
    n_cells = cells["cell_id"].nunique()

    assert len(states) == n_buckets * n_cells
    assert not states.duplicated(subset=["cell_id", "time_bucket"]).any()
    assert set(states["cell_id"]) == set(cells["cell_id"])
    assert set(states["time_bucket"]) == set(panel["time_bucket"])
    for field in RECONCILED_FIELDS:
        assert field in states.columns, f"missing replay field {field}"


@pytest.mark.parametrize("strategy", ["rule", "optimized"])
def test_per_cell_sums_equal_step_totals_for_every_bucket(strategy):
    """F8-2: no sampling -- every bucket, every reconciled field."""
    panel, forecast, cells, capacities = _scenario()
    result = simulate(panel, forecast, cells, capacities, _config(), strategy)

    sums = result.cell_states.groupby("time_bucket", sort=True)[list(RECONCILED_FIELDS)].sum()
    steps = result.steps.set_index("time_bucket").sort_index()

    assert list(sums.index) == list(steps.index)
    for bucket in steps.index:
        for field in RECONCILED_FIELDS:
            assert int(sums.loc[bucket, field]) == int(steps.loc[bucket, field]), (
                f"{strategy} {bucket} {field}: per-cell sum {int(sums.loc[bucket, field])} "
                f"!= step total {int(steps.loc[bucket, field])}"
            )


def test_moves_are_counted_once_at_the_source_cell():
    """A dispatch touches two cells; counting both would double ``bikes_moved``."""
    panel, forecast, cells, capacities = _scenario()
    result = simulate(panel, forecast, cells, capacities, _config(), "rule")

    total = int(result.cell_states["bikes_moved"].sum())
    assert total == int(result.steps["bikes_moved"].sum())
    if result.moves.empty:
        assert total == 0
    else:
        assert total == int(result.moves["bikes"].sum())


def test_replay_state_is_conserved_within_each_cell():
    """Per cell: end == start - moved_out + moved_in + returns_applied - borrow_met.

    ``bikes_moved`` is deliberately recorded at the source cell only, so the
    "moved in" side is rebuilt from the moves table here.  Together they must
    close the per-cell inventory identity.
    """
    panel, forecast, cells, capacities = _scenario()
    result = simulate(panel, forecast, cells, capacities, _config(), "rule")

    moves = result.moves
    moved_out = moves.groupby(["time_bucket", "from_cell"])["bikes"].sum() if not moves.empty else pd.Series(dtype=int)
    moved_in = moves.groupby(["time_bucket", "to_cell"])["bikes"].sum() if not moves.empty else pd.Series(dtype=int)

    def lookup(series, bucket, cell) -> int:
        try:
            return int(series.loc[(bucket, cell)])
        except KeyError:
            return 0

    states = result.cell_states.sort_values(["cell_id", "time_bucket"])
    for row in states.itertuples(index=False):
        expected = (
            int(row.inventory_start)
            - lookup(moved_out, row.time_bucket, row.cell_id)
            + lookup(moved_in, row.time_bucket, row.cell_id)
            + int(row.returns_applied)
            - int(row.borrow_met)
        )
        assert int(row.inventory_end) == expected, f"cell {row.cell_id} at {row.time_bucket} does not conserve bikes"

    # The next bucket must start from where the previous one ended.
    for cell, group in states.groupby("cell_id"):
        ordered = group.sort_values("time_bucket")
        if len(ordered) > 1:
            assert (ordered["inventory_start"].iloc[1:].to_numpy() == ordered["inventory_end"].iloc[:-1].to_numpy()).all(), (
                f"cell {cell} inventory does not carry over between buckets"
            )


# -- real artefacts ---------------------------------------------------------


def _artifact_pairs() -> list[tuple[str, str]]:
    layout = Layout()
    config = load_config()
    pairs = []
    for source in config.get("sources", {}):
        for strategy in ("rule", "optimized"):
            if layout.replay_path(source, strategy).exists() and layout.sim_path(source, strategy, "csv").exists():
                pairs.append((source, strategy))
    return pairs


@pytest.mark.parametrize(("source", "strategy"), _artifact_pairs() or [pytest.param("none", "none")])
def test_persisted_replay_reconciles_with_persisted_steps(source, strategy):
    """The real ``bike-agent repro`` artefacts, every bucket, all nine fields."""
    if source == "none":
        pytest.skip("no replay artefacts on disk; run `bike-agent repro` first")

    layout = Layout()
    states = pd.read_parquet(layout.replay_path(source, strategy))
    steps = pd.read_csv(layout.sim_path(source, strategy, "csv"))
    steps["time_bucket"] = pd.to_datetime(steps["time_bucket"])
    states["time_bucket"] = pd.to_datetime(states["time_bucket"])

    n_cells = len(states["cell_id"].unique())
    assert len(states) == len(states["time_bucket"].unique()) * n_cells
    assert not states.duplicated(subset=["cell_id", "time_bucket"]).any()
    assert list(steps["time_bucket"]) == sorted(states["time_bucket"].unique())

    sums = states.groupby("time_bucket", sort=True)[list(RECONCILED_FIELDS)].sum()
    ordered = steps.sort_values("time_bucket").set_index("time_bucket")
    assert list(sums.index) == list(ordered.index)
    for bucket in ordered.index:
        for field in RECONCILED_FIELDS:
            assert int(sums.loc[bucket, field]) == int(ordered.loc[bucket, field]), (
                f"{source}/{strategy} {bucket} {field} does not reconcile"
            )


@pytest.mark.parametrize(("source", "strategy"), _artifact_pairs() or [pytest.param("none", "none")])
def test_persisted_simulation_reports_no_violations(source, strategy):
    """F8-4: the conservation counters are still all zero."""
    if source == "none":
        pytest.skip("no replay artefacts on disk; run `bike-agent repro` first")

    import json

    layout = Layout()
    summary = json.loads(layout.sim_path(source, strategy, "json").read_text(encoding="utf-8"))
    assert summary["violations"] == {
        "conservation": 0,
        "capacity": 0,
        "budget": 0,
        "negative_inventory": 0,
        "clamped_moves": 0,
    }


def test_returns_truncation_cannot_desync_the_two_layers():
    """Per-cell and step totals must truncate identically (latent-bug guard).

    ``returns`` is recorded per cell as an int and summed at the step level.
    If the two layers truncated differently, F8-2 would fail for a reason with
    no visible cause, so the engine truncates once and reuses the integers.
    """
    panel, forecast, cells, capacities = _scenario()
    result = simulate(panel, forecast, cells, capacities, _config(), "rule")

    per_cell = result.cell_states.groupby("time_bucket")["returns"].sum()
    step_total = result.steps.set_index("time_bucket")["returns"]
    assert (per_cell == step_total).all()
    assert (result.cell_states["returns"] == result.cell_states["returns"].astype(int)).all()
