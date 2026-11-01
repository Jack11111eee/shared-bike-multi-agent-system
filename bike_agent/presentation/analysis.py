"""Phase 4: the single analysis object behind the workbench.

Every number the Phase 4 pages show is derived here, once:

* per-frame, per-cell ledger fields and the ``delta_unmet`` map (F18/F19),
* the four key events per source (F17),
* the existing sensitivity scenarios with their baseline deltas (F20),
* the five guide chapters (F16),
* an evidence record for every derived value (§2.3).

The frames, per-frame KPIs and dispatch records come from
:func:`bike_agent.replay.player.build_replay_data` — the Phase 3 bundle — so the
workbench and the player cannot disagree about a frame.

Sign conventions are kept strictly apart (REQUIREMENTS-PHASE4 §3):

``gain = unmet_rule - unmet_optimized``        positive means S-OPT needed fewer
``delta_unmet = unmet_optimized - unmet_rule`` negative means S-OPT was better

They are exact negatives of each other, and every record says which one it is.
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any

import pandas as pd

from bike_agent.config import Layout, source_config
from bike_agent.replay.player import build_replay_data, replay_data_js
from bike_agent.replay.site import STRATEGIES
from bike_agent.utils import read_json

# The four event families F17 requires, in the order the page lists them.
EVENT_TYPES = (
    "rule_unmet_max",
    "rule_moved_max",
    "opt_gain_max",
    "opt_regression_max",
)

EVENT_LABELS = {
    "rule_unmet_max": "S-RULE 未满足请求最大",
    "rule_moved_max": "S-RULE 搬运车辆数最大",
    "opt_gain_max": "S-OPT 服务改善最大",
    "opt_regression_max": "S-OPT 服务退步最大",
}

# Ledger fields the bundle stores verbatim from the parquet.  ``unmet``,
# ``returns_overflow`` and ``inventory_end`` are *derived* from these plus the
# dispatch records, and the derivation is checked against the parquet by
# ``tests/test_presentation_ledger.py`` for every cell of every frame.
LEDGER_FIELDS = (
    "inventory_start",
    "borrow_requests",
    "borrow_met",
    "returns",
    "returns_applied",
    "bikes_moved",
)

# Short keys keep the published bundle small; the long names stay in the
# evidence records so a reader never has to guess what ``rq`` means.
SHORT = {
    "inventory_start": "start",
    "borrow_requests": "req",
    "borrow_met": "met",
    "returns": "ret",
    "returns_applied": "rapp",
    "bikes_moved": "out",
}

EQUATIONS = {
    "scheduled_inventory": "调度后库存 = 期初库存 + 调入 - 调出",
    "inventory_end": "期末库存 = 期初库存 + 调入 - 调出 + 接纳归还 - 满足借车",
    "unmet": "未满足请求 = 借车请求 - 满足借车",
    "overflow": "溢出归还 = 归还 - 接纳归还",
}

UNITS = {
    "unmet_requests": "次",
    "borrow_requests": "次",
    "borrow_met": "次",
    "returns": "次",
    "returns_applied": "次",
    "returns_overflow": "次",
    "bikes_moved": "辆",
    "inventory_start": "辆",
    "inventory_end": "辆",
    "distance_bike_km": "车·公里",
    "service_rate": "比例",
    "composite_cost": "分",
    "gain": "次",
    "delta_unmet": "次",
}


# -- helpers ---------------------------------------------------------------
def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _matrix(frame: pd.DataFrame, buckets: list[pd.Timestamp], cell_ids: list[str], field: str) -> list[list[int]]:
    """``field`` as ``[frame][cell]``, refusing to guess when the grid is ragged."""
    ordered = frame.sort_values(["time_bucket", "cell_id"], kind="mergesort")
    values = ordered[field].to_numpy(dtype="int64")
    expected = len(buckets) * len(cell_ids)
    if len(values) != expected:
        raise ValueError(
            f"{field}: expected {len(buckets)}x{len(cell_ids)}={expected} rows, got {len(values)}"
        )
    wide = values.reshape(len(buckets), len(cell_ids))
    # The reshape is only valid if the ordering really is (bucket, cell) with
    # the same cell set in every bucket.  A ragged grid whose totals happen to
    # match would otherwise shift every later row by one cell, silently.
    if list(ordered["time_bucket"].drop_duplicates()) != buckets:
        raise ValueError(f"{field}: buckets are not in the expected order")
    if list(ordered["cell_id"].iloc[: len(cell_ids)]) != cell_ids:
        raise ValueError(f"{field}: cell order does not match cell_ids")
    for bucket, block in ordered.groupby("time_bucket", sort=True):
        if list(block["cell_id"]) != cell_ids:
            raise ValueError(f"{field}: bucket {bucket} does not carry the expected cells")
    return [[int(value) for value in row] for row in wide]


def _moved_in(
    moves: dict[pd.Timestamp, list[dict[str, Any]]],
    buckets: list[pd.Timestamp],
    cell_index: dict[str, int],
) -> list[dict[str, int]]:
    """Incoming bikes per cell, from ``to_cell``; sparse because most cells get none.

    ``bikes_moved`` in the per-cell parquet counts *only* the bikes leaving a
    cell (one dispatch touches two cells, so counting both sides would double
    the total).  Arrivals therefore have to come from the dispatch records.
    """
    rows: list[dict[str, int]] = []
    for bucket in buckets:
        incoming: dict[int, int] = {}
        for record in moves.get(bucket, []):
            index = cell_index.get(str(record["to_cell"]))
            if index is None:
                raise ValueError(f"dispatch record targets unknown cell {record['to_cell']!r}")
            incoming[index] = incoming.get(index, 0) + int(record["bikes"])
        rows.append({str(index): bikes for index, bikes in sorted(incoming.items())})
    return rows


# -- events (F17) -----------------------------------------------------------
def _extreme(values: list[int], want: str) -> tuple[int | None, int, int]:
    """Return (frame index, extreme value, how many frames share it).

    Ties resolve to the earliest bucket, as F17 requires; ``count`` lets the
    page say so instead of pretending the extreme is unique.
    """
    if not values:
        return None, 0, 0
    extreme = max(values) if want == "max" else min(values)
    index = values.index(extreme)
    return index, extreme, sum(1 for value in values if value == extreme)


def build_events(
    source: str,
    buckets: list[str],
    unmet_rule: list[int],
    unmet_optimized: list[int],
    moved_rule: list[int],
) -> list[dict[str, Any]]:
    """The four F17 event families over *every* replay bucket."""
    gain = [rule - opt for rule, opt in zip(unmet_rule, unmet_optimized)]
    csv_rule = f"outputs/sim_rule.{source}.csv"
    events: list[dict[str, Any]] = []

    def record(
        kind: str,
        *,
        index: int | None,
        value: int,
        metric: str,
        strategy: str,
        source_file: str,
        formula: str,
        operands: dict[str, Any],
        status: str,
        tie_count: int = 0,
        extra: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        entry = {
            "id": f"{source}:{kind}",
            "source": source,
            "type": kind,
            "label": EVENT_LABELS[kind],
            "frame": index,
            "time_bucket": buckets[index] if index is not None else None,
            "metric": metric,
            "strategy": strategy,
            "value": int(value),
            "unit": UNITS.get(metric, ""),
            "source_file": source_file,
            "join_key": (
                f"time_bucket={buckets[index]}" if index is not None else "time_bucket=*"
            ),
            "formula": formula,
            "operands": operands,
            "status": status,
            "tie_break": "earliest_bucket",
            "tie_count": tie_count,
        }
        if extra:
            entry.update(extra)
        return entry

    # 1. S-RULE unmet requests, largest.
    index, value, count = _extreme(unmet_rule, "max")
    events.append(
        record(
            "rule_unmet_max",
            index=index if value > 0 else None,
            value=value,
            metric="unmet_requests",
            strategy="rule",
            source_file=csv_rule,
            formula="max over all replay buckets of sim_rule.unmet_requests",
            operands={"n_buckets": len(buckets)},
            status="ok" if value > 0 else "all_zero",
            tie_count=count,
            extra={"note": "" if value > 0 else "该指标全程为零"},
        )
    )

    # 2. S-RULE moved bikes, largest.
    index, value, count = _extreme(moved_rule, "max")
    events.append(
        record(
            "rule_moved_max",
            index=index if value > 0 else None,
            value=value,
            metric="bikes_moved",
            strategy="rule",
            source_file=csv_rule,
            formula="max over all replay buckets of sim_rule.bikes_moved",
            operands={"n_buckets": len(buckets)},
            status="ok" if value > 0 else "all_zero",
            tie_count=count,
            extra={"note": "" if value > 0 else "该指标全程为零"},
        )
    )

    # 3. S-OPT improvement, largest gain = unmet_rule - unmet_optimized > 0.
    index, value, count = _extreme(gain, "max")
    positive = [frame for frame, g in enumerate(gain) if g > 0]
    if value > 0 and index is not None:
        status, note, frame = "ok", "", index
    else:
        status, note, frame = "no_improvement", "无改善事件（没有任何时段 gain > 0）", None
    events.append(
        record(
            "opt_gain_max",
            index=frame,
            value=value if frame is not None else 0,
            metric="gain",
            strategy="optimized",
            source_file=csv_rule,
            formula="gain = unmet_rule - unmet_optimized；仅取 gain > 0",
            operands={
                "unmet_rule": unmet_rule[frame] if frame is not None else None,
                "unmet_optimized": unmet_optimized[frame] if frame is not None else None,
                "n_frames_with_gain": len(positive),
            },
            status=status,
            tie_count=count,
            extra={"note": note},
        )
    )

    # 4. S-OPT regression, smallest gain (most negative) < 0.
    index, value, count = _extreme(gain, "min")
    negative = [frame for frame, g in enumerate(gain) if g < 0]
    if value < 0 and index is not None:
        status, note, frame = "ok", "", index
    else:
        status, note, frame = "no_regression", "无退步事件（没有任何时段 gain < 0）", None
    events.append(
        record(
            "opt_regression_max",
            index=frame,
            value=value if frame is not None else 0,
            metric="gain",
            strategy="optimized",
            source_file=csv_rule,
            formula="gain = unmet_rule - unmet_optimized；仅取 gain < 0（数值最小者）",
            operands={
                "unmet_rule": unmet_rule[frame] if frame is not None else None,
                "unmet_optimized": unmet_optimized[frame] if frame is not None else None,
                "n_frames_with_regression": len(negative),
            },
            status=status,
            tie_count=count,
            extra={"note": note},
        )
    )
    return events


# -- scenarios (F20) --------------------------------------------------------
METRIC_LABELS = {
    "unmet_requests": "未满足请求（次）",
    "service_rate": "服务率",
    "distance_bike_km": "搬运距离（车·公里）",
    "composite_cost": "综合代价（分）",
}
SCENARIO_METRICS = ("unmet_requests", "service_rate", "distance_bike_km", "composite_cost")


def _changed_factor(name: str) -> dict[str, Any] | None:
    """``initial_fill=0.3`` → ``{factor, value}``; ``baseline`` → ``None``."""
    if name == "baseline" or "=" not in name:
        return None
    factor, _, value = name.partition("=")
    return {"factor": factor, "value": value}


def _delta(absolute: float, base: float) -> dict[str, Any]:
    """Absolute change plus a percentage that refuses to divide by zero."""
    change = absolute - base
    if base == 0:
        return {"absolute": change, "percent": None, "status": "zero_denominator"}
    return {"absolute": change, "percent": change / base * 100.0, "status": "ok"}


def build_scenarios(
    source: str,
    sensitivity: list[dict[str, Any]],
    comparison: dict[str, Any],
) -> dict[str, Any]:
    """The seven existing scenarios, with deltas against the same-source baseline."""
    baseline = next((row for row in sensitivity if row["scenario"] == "baseline"), None)
    if baseline is None:
        raise ValueError(f"{source}: sim_sensitivity has no baseline scenario")
    rows: list[dict[str, Any]] = []
    for row in sensitivity:
        entry: dict[str, Any] = {
            "id": row["scenario"],
            "label": row["scenario"],
            "source": source,
            "changed": _changed_factor(row["scenario"]),
            "status": "baseline" if row["scenario"] == "baseline" else "scenario",
            "source_file": f"outputs/sim_sensitivity.{source}.json",
            "join_key": f"scenario={row['scenario']}",
            "strategies": {},
        }
        for strategy in STRATEGIES:
            values = row[strategy]
            metrics: dict[str, Any] = {}
            for metric in SCENARIO_METRICS:
                metrics[metric] = {
                    "value": values[metric],
                    "unit": UNITS.get(metric, ""),
                    "delta": _delta(float(values[metric]), float(baseline[strategy][metric])),
                    "formula": (
                        f"{strategy}.{metric}(scenario) - {strategy}.{metric}(baseline)"
                    ),
                }
            entry["strategies"][strategy] = metrics
        rows.append(entry)

    # F20-1: the baseline row must be the same numbers as the strategy
    # comparison, which is the artefact the rest of the project quotes.
    baseline_check = {
        strategy: {
            metric: {
                "scenario": float(baseline[strategy][metric]),
                "comparison": float(comparison[strategy][metric]),
                "equal": float(baseline[strategy][metric]) == float(comparison[strategy][metric]),
            }
            for metric in SCENARIO_METRICS
        }
        for strategy in STRATEGIES
    }
    return {
        "source": source,
        "source_file": f"outputs/sim_sensitivity.{source}.json",
        "n_scenarios": len(rows),
        "baseline": "baseline",
        "rows": rows,
        "baseline_matches_comparison": baseline_check,
        "assumptions": comparison["rule"].get("assumptions", {}),
        "scope_note": (
            "场景只重算全回放汇总指标，不生成空间回放；上方地图始终是 baseline 回放，"
            "切换场景不会改写地图数据。"
        ),
    }


# -- guide (F16) ------------------------------------------------------------
def _signed(value: int) -> str:
    """``+3`` / ``-3`` / ``0`` — a zero delta is written as 0, not as "+0"."""
    if value == 0:
        return "0"
    return f"{value:+,}"


def _pct(numerator: float, denominator: float) -> str:
    if not denominator:
        return "分母为 0，无法计算"
    return f"{numerator / denominator * 100:.2f}%"


def build_guide(
    source: str,
    *,
    n_frames: int,
    n_cells: int,
    cell_size_m: int,
    buckets: list[str],
    totals: dict[str, dict[str, Any]],
    events: list[dict[str, Any]],
    unmet_rule: list[int],
    unmet_optimized: list[int],
    delta_domain: dict[str, Any],
    frames_delta: list[dict[str, Any]],
    ledger_top_cell: dict[str, Any],
    scenarios: dict[str, Any],
    other: dict[str, Any] | None,
) -> dict[str, Any]:
    """Five chapters, each computed from the artefacts rather than written by hand.

    ``other`` is the summary of the second source, used by chapter 1 so the
    guide covers both sources without a second page load.
    """
    by_type = {event["type"]: event for event in events}
    unmet_event = by_type["rule_unmet_max"]
    regression_event = by_type["opt_regression_max"]

    # Chapter 3 target: the frame with the largest |delta_unmet| in this source.
    widest = max(
        frames_delta,
        key=lambda frame: (frame["delta_summary"]["max_abs"], -frame["index"]),
    )

    # Chapter 5 target: the scenario that moves the S-RULE unmet count most,
    # excluding baseline; ties go to the earlier row so the choice is stable.
    movable = [row for row in scenarios["rows"] if row["status"] == "scenario"]
    changed = max(
        movable,
        key=lambda row: (
            abs(row["strategies"]["rule"]["unmet_requests"]["delta"]["absolute"]),
            -movable.index(row),
        ),
    )
    changed_delta = changed["strategies"]["rule"]["unmet_requests"]["delta"]

    rule_total = totals["rule"]
    opt_total = totals["optimized"]

    chapters: list[dict[str, Any]] = []

    chapters.append(
        {
            "index": 0,
            "id": "overview",
            "title": "数据与需求概貌",
            "target": {"source": source, "frame": 0, "mode": "side", "cell": None, "scenario": "baseline"},
            "body": (
                f"{source.upper()} 共 {n_frames} 个时间单元、{n_cells} 个格子（{cell_size_m} m 网格），"
                f"时间范围 {buckets[0]} → {buckets[-1]}。全回放借车请求 "
                f"{rule_total['borrow_requests']:,} 次；S-RULE 未满足 "
                f"{rule_total['unmet_requests']:,} 次（服务率 {_pct(rule_total['borrow_met'], rule_total['borrow_requests'])}），"
                f"S-OPT 未满足 {opt_total['unmet_requests']:,} 次"
                f"（服务率 {_pct(opt_total['borrow_met'], opt_total['borrow_requests'])}）。"
                + (
                    f"另一个数据源 {other['source'].upper()} 为 {other['n_frames']} 帧、"
                    f"{other['n_cells']} 格，S-RULE 未满足 {other['unmet_rule']:,} 次。"
                    f"两源的时间范围与网格不同，不合并成一条时间轴。"
                    if other
                    else ""
                )
            ),
            "evidence": [
                {
                    "metric": "borrow_requests",
                    "value": rule_total["borrow_requests"],
                    "source_file": f"outputs/sim_rule.{source}.csv",
                    "join_key": "sum over all time_bucket",
                    "formula": "sum(borrow_requests)",
                },
                {
                    "metric": "unmet_requests",
                    "value": rule_total["unmet_requests"],
                    "source_file": f"outputs/sim_rule.{source}.csv",
                    "join_key": "sum over all time_bucket",
                    "formula": "sum(unmet_requests)",
                },
                {
                    "metric": "unmet_requests",
                    "value": opt_total["unmet_requests"],
                    "source_file": f"outputs/sim_optimized.{source}.csv",
                    "join_key": "sum over all time_bucket",
                    "formula": "sum(unmet_requests)",
                },
            ],
        }
    )

    if unmet_event["status"] == "ok":
        frame_index = int(unmet_event["frame"])
        # The gain quoted here is for *this* frame, not for the best-gain frame:
        # the two events can land on different buckets, and mixing them would
        # put two different periods in one sentence.
        unmet_rule_here = unmet_rule[frame_index]
        unmet_opt_here = unmet_optimized[frame_index]
        gain = unmet_rule_here - unmet_opt_here
        chapters.append(
            {
                "index": 1,
                "id": "peak",
                "title": "关键时段",
                "target": {"source": source, "frame": frame_index, "mode": "side", "cell": None, "scenario": "baseline"},
                "body": (
                    f"{unmet_event['time_bucket']} 是 S-RULE 未满足请求最多的时段："
                    f"{unmet_event['value']:,} 次，占全回放 S-RULE 未满足的 "
                    f"{_pct(unmet_event['value'], rule_total['unmet_requests'])}。"
                    f"同一时段 S-OPT 为 {unmet_opt_here:,} 次，"
                    f"gain = unmet_rule − unmet_optimized = {_signed(gain)} 次"
                    f"（{'S-OPT 更少' if gain > 0 else ('两者相同' if gain == 0 else 'S-OPT 更多')}）。"
                    f"该极值在 {unmet_event['tie_count']} 个时段上同时成立，按最早时段选取。"
                ),
                "evidence": [
                    {
                        "metric": "unmet_requests",
                        "value": unmet_event["value"],
                        "source_file": unmet_event["source_file"],
                        "join_key": unmet_event["join_key"],
                        "formula": unmet_event["formula"],
                    },
                    {
                        "metric": "gain",
                        "value": gain,
                        "source_file": unmet_event["source_file"],
                        "join_key": unmet_event["join_key"],
                        "formula": "gain = unmet_rule - unmet_optimized",
                        "operands": {
                            "unmet_rule": unmet_rule_here,
                            "unmet_optimized": unmet_opt_here,
                        },
                    },
                ],
            }
        )
    else:
        chapters.append(
            {
                "index": 1,
                "id": "peak",
                "title": "关键时段",
                "target": {"source": source, "frame": 0, "mode": "side", "cell": None, "scenario": "baseline"},
                "body": "该源全回放 S-RULE 未满足请求全程为零，没有可指认的高峰时段。",
                "evidence": [],
            }
        )

    net = widest["delta_summary"]["net"]
    chapters.append(
        {
            "index": 2,
            "id": "difference",
            "title": "策略差异",
            "target": {
                "source": source,
                "frame": int(widest["index"]),
                "mode": "delta",
                "cell": None,
                "scenario": "baseline",
            },
            "body": (
                f"差分图逐格使用 delta_unmet = unmet_optimized − unmet_rule：负值表示 S-OPT 更少未满足，"
                f"正值表示更多。本源全回放 |delta_unmet| 的最大值为 {delta_domain['max_abs']} 次，"
                f"色域固定为 ±{delta_domain['max_abs']}。差异最大的时段是 {widest['time_bucket']}："
                f"净差 {net:+,} 次，改善格 {widest['delta_summary']['improved_cells']} 个、"
                f"退步格 {widest['delta_summary']['worsened_cells']} 个、"
                f"无差异格 {widest['delta_summary']['unchanged_cells']} 个。"
                f"注意 gain = unmet_rule − unmet_optimized 与本指标互为相反数，两者标签不混用。"
            ),
            "evidence": [
                {
                    "metric": "delta_unmet",
                    "value": net,
                    "source_file": f"outputs/replay.optimized.{source}.parquet",
                    "join_key": f"time_bucket={widest['time_bucket']}",
                    "formula": "delta_unmet = unmet_optimized - unmet_rule（逐格求和）",
                },
                {
                    "metric": "delta_domain",
                    "value": delta_domain["max_abs"],
                    "source_file": f"outputs/replay.rule.{source}.parquet",
                    "join_key": "all time_bucket × all cell_id",
                    "formula": "max(abs(unmet_optimized - unmet_rule))",
                },
            ],
        }
    )

    chapters.append(
        {
            "index": 3,
            "id": "ledger",
            "title": "单格账本",
            "target": {
                "source": source,
                "frame": int(ledger_top_cell["frame"]),
                "mode": "side",
                "cell": ledger_top_cell["cell_id"],
                "scenario": "baseline",
            },
            "body": (
                f"默认查看格子 {ledger_top_cell['cell_id']}：它的全回放累计 S-RULE 未满足请求最高"
                f"（{ledger_top_cell['cumulative_unmet_rule']:,} 次），平手按 cell_id 升序，"
                f"选择规则固定为“累计 S-RULE 未满足请求最高，其次 cell_id 升序”。"
                f"账本按引擎口径给出四条等式：{EQUATIONS['inventory_end']}；"
                f"{EQUATIONS['unmet']}；{EQUATIONS['overflow']}。"
                f"“观测/预测”来自预测与需求输入，“模拟库存”来自回放引擎，两者不混为一谈；"
                f"本期只以实际账本说明流量，不把时间不同的风险排名写成当前格子的成因。"
            ),
            "evidence": [
                {
                    "metric": "cumulative_unmet_rule",
                    "value": ledger_top_cell["cumulative_unmet_rule"],
                    "source_file": f"outputs/replay.rule.{source}.parquet",
                    "join_key": f"cell_id={ledger_top_cell['cell_id']}",
                    "formula": "sum(unmet_requests) over all time_bucket",
                }
            ],
        }
    )

    chapters.append(
        {
            "index": 4,
            "id": "scenario",
            "title": "资源参数实验",
            "target": {
                "source": source,
                "frame": int(ledger_top_cell["frame"]),
                "mode": "side",
                "cell": ledger_top_cell["cell_id"],
                "scenario": changed["id"],
            },
            "body": (
                f"把 {changed['changed']['factor']} 改为 {changed['changed']['value']}"
                f"（baseline 假设见下方场景表）后，本源全回放汇总为：S-RULE 未满足 "
                f"{changed['strategies']['rule']['unmet_requests']['value']:,} 次"
                f"（相对 baseline {changed_delta['absolute']:+,} 次），"
                f"S-OPT 未满足 {changed['strategies']['optimized']['unmet_requests']['value']:,} 次"
                f"（相对 baseline "
                f"{changed['strategies']['optimized']['unmet_requests']['delta']['absolute']:+,} 次）。"
                f"场景只重算全回放汇总，不生成空间回放：上方地图仍然是 baseline 回放。"
            ),
            "evidence": [
                {
                    "metric": "unmet_requests",
                    "value": changed["strategies"]["rule"]["unmet_requests"]["value"],
                    "source_file": scenarios["source_file"],
                    "join_key": f"scenario={changed['id']}",
                    "formula": "rule.unmet_requests",
                },
                {
                    "metric": "unmet_requests",
                    "value": changed["strategies"]["optimized"]["unmet_requests"]["value"],
                    "source_file": scenarios["source_file"],
                    "join_key": f"scenario={changed['id']}",
                    "formula": "optimized.unmet_requests",
                },
            ],
        }
    )

    return {
        "source": source,
        "n_chapters": len(chapters),
        "actions": [
            {"id": "start", "label": "开始导览"},
            {"id": "prev", "label": "上一章"},
            {"id": "next", "label": "下一章"},
            {"id": "restart", "label": "重新开始"},
            {"id": "exit", "label": "退出导览"},
        ],
        "chapters": chapters,
        "segment_note": (
            "导览是选段：它允许显式跳到关键事件与代表时段，并标明“关键事件/选段”；"
            "完整播放器仍覆盖全部帧。"
        ),
        "regression_note": (
            "本源的退步事件："
            + (
                f"{regression_event['time_bucket']} gain = {_signed(regression_event['value'])} 次。"
                if regression_event["status"] == "ok"
                else "无退步事件（没有任何时段 gain < 0）。"
            )
        ),
    }


# -- the single build -------------------------------------------------------
def _other_source_summary(
    config: dict[str, Any], layout: Layout, source: str
) -> dict[str, Any] | None:
    """Just enough of the *other* source for chapter 1's cross-source sentence.

    Chapter 1 compares the two sources, so a single-source build still has to
    look at the sibling's artefacts.  Reading three cheap files here keeps
    :func:`build_presentation` a one-call build -- the alternative was a second
    ``attach_guide`` step that callers had to remember, and that every consumer
    eventually forgot.
    """
    for name in config.get("sources", {}):
        if name == source:
            continue
        rule_csv = layout.sim_path(name, "rule", "csv")
        cells = layout.cells_path(name, int(source_config(config, name)["cell_size_m"]))
        if not rule_csv.exists() or not cells.exists():
            return None
        steps = pd.read_csv(rule_csv)
        return {
            "source": name,
            "n_frames": len(steps),
            "n_cells": len(pd.read_parquet(cells)),
            "unmet_rule": int(steps["unmet_requests"].sum()),
        }
    return None


def build_presentation(
    config: dict[str, Any],
    layout: Layout,
    source: str,
    *,
    replay: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """The single build: one source's whole analysis object, guide included."""
    replay = replay if replay is not None else build_replay_data(config, layout, source)
    if replay["source"] != source:
        raise ValueError(f"replay bundle is for {replay['source']}, not {source}")

    cell_ids: list[str] = list(replay["cell_ids"])
    cell_index = {cell_id: position for position, cell_id in enumerate(cell_ids)}
    frames = replay["frames"]
    buckets = [pd.Timestamp(frame["time_bucket"]) for frame in frames]
    bucket_labels = [frame["time_bucket"] for frame in frames]

    cell_size = int(source_config(config, source)["cell_size_m"])
    cells_path = layout.cells_path(source, cell_size)
    moves_by_bucket: dict[str, dict[pd.Timestamp, list[dict[str, Any]]]] = {}

    ledger: dict[str, list[list[int]]] = {}
    moved_in: dict[str, list[dict[str, int]]] = {}
    unmet: dict[str, list[list[int]]] = {}
    inventory_end: dict[str, list[list[int]]] = {}
    for strategy in STRATEGIES:
        state = pd.read_parquet(layout.replay_path(source, strategy))
        state["time_bucket"] = pd.to_datetime(state["time_bucket"])
        for field in LEDGER_FIELDS:
            ledger[f"{strategy}.{field}"] = _matrix(state, buckets, cell_ids, field)
        unmet[strategy] = _matrix(state, buckets, cell_ids, "unmet_requests")
        inventory_end[strategy] = _matrix(state, buckets, cell_ids, "inventory_end")
        grouped: dict[pd.Timestamp, list[dict[str, Any]]] = {}
        for record in read_json(layout.outputs / f"sim_moves.{strategy}.{source}.json"):
            grouped.setdefault(pd.Timestamp(record["time_bucket"]), []).append(record)
        moves_by_bucket[strategy] = grouped
        moved_in[strategy] = _moved_in(grouped, buckets, cell_index)

    # F18: delta per cell per frame; the domain is one fixed range per source.
    delta: list[list[int]] = []
    max_abs = 0
    for index in range(len(buckets)):
        row = [
            int(opt) - int(rule)
            for rule, opt in zip(unmet["rule"][index], unmet["optimized"][index])
        ]
        delta.append(row)
        if row:
            max_abs = max(max_abs, max(abs(value) for value in row))

    color_domain = {
        "inventory": {
            "min": int(replay["color_domain"]["min"]),
            "max": int(replay["color_domain"]["max"]),
        },
        "delta": {"min": -max_abs, "max": max_abs, "max_abs": max_abs},
    }

    frames_delta: list[dict[str, Any]] = []
    frame_payload: list[dict[str, Any]] = []
    parquet_rule = f"outputs/replay.rule.{source}.parquet"
    parquet_opt = f"outputs/replay.optimized.{source}.parquet"
    for index, label in enumerate(bucket_labels):
        row = delta[index]
        net = sum(row)
        improved = sum(1 for value in row if value < 0)
        worsened = sum(1 for value in row if value > 0)
        unchanged = len(row) - improved - worsened
        summary = {
            "net": net,
            "improved_cells": improved,
            "worsened_cells": worsened,
            "unchanged_cells": unchanged,
            "max_abs": max((abs(value) for value in row), default=0),
            "status": "no_difference" if max_abs == 0 else "ok",
        }
        frames_delta.append({"index": index, "time_bucket": label, "delta_summary": summary})

        strategies: dict[str, Any] = {}
        for strategy in STRATEGIES:
            strategies[strategy] = {
                "ledger": {
                    SHORT[field]: ledger[f"{strategy}.{field}"][index] for field in LEDGER_FIELDS
                },
                "moved_in": moved_in[strategy][index],
            }
        rule_total = sum(unmet["rule"][index])
        opt_total = sum(unmet["optimized"][index])
        frame_payload.append(
            {
                "index": index,
                "time_bucket": label,
                "strategies": strategies,
                "delta": row,
                "delta_summary": summary,
                "gain": -net,
                # §2.3: every derived value is a serialisable record naming its
                # source, metric, raw value, input file, join key, formula and
                # operands -- the numbers above are the raw values, this is how
                # they were obtained.
                "evidence": {
                    "delta_summary": {
                        "source_file": parquet_opt,
                        "metric": "delta_unmet",
                        "value": net,
                        "join_key": f"time_bucket={label}",
                        "formula": (
                            "delta_unmet = unmet_optimized − unmet_rule；net = sum(delta_unmet)；"
                            "improved = count(delta_unmet < 0)；worsened = count(delta_unmet > 0)；"
                            "unchanged = count(delta_unmet = 0)"
                        ),
                        "operands": {
                            "unmet_rule_total": rule_total,
                            "unmet_optimized_total": opt_total,
                            "n_cells": len(row),
                        },
                    },
                    "gain": {
                        "source_file": parquet_rule,
                        "metric": "gain",
                        "value": -net,
                        "join_key": f"time_bucket={label}",
                        "formula": "gain = unmet_rule − unmet_optimized",
                        "operands": {
                            "unmet_rule_total": rule_total,
                            "unmet_optimized_total": opt_total,
                        },
                    },
                    "moved_in": {
                        strategy: {
                            "source_file": f"outputs/sim_moves.{strategy}.{source}.json",
                            "metric": "moved_in",
                            "value": sum(moved_in[strategy][index].values()),
                            "join_key": f"time_bucket={label}",
                            "formula": "sum(bikes) where to_cell = cell_id",
                            # Not a repeated copy of the map: this names where
                            # the addends live and how to reduce them, so the
                            # value stays recomputable without storing it twice.
                            "operands": {
                                "source": "frames[i].strategies[s].moved_in",
                                "reduce": "sum(values)",
                            },
                        }
                        for strategy in STRATEGIES
                    },
                },
            }
        )

    unmet_rule = [int(frame["strategies"]["rule"]["kpi"]["unmet_requests"]) for frame in frames]
    unmet_opt = [int(frame["strategies"]["optimized"]["kpi"]["unmet_requests"]) for frame in frames]
    moved_rule = [int(frame["strategies"]["rule"]["kpi"]["bikes_moved"]) for frame in frames]

    events = build_events(source, bucket_labels, unmet_rule, unmet_opt, moved_rule)

    sensitivity = read_json(layout.outputs / f"sim_sensitivity.{source}.json")
    comparison = read_json(layout.outputs / f"strategy_comparison.{source}.json")
    scenarios = build_scenarios(source, sensitivity, comparison)

    # F19: the default cell is the one with the highest cumulative S-RULE unmet
    # count; ties break on cell_id ascending, which is the array order here.
    cumulative = [
        sum(unmet["rule"][index][position] for index in range(len(buckets)))
        for position in range(len(cell_ids))
    ]
    best = max(range(len(cell_ids)), key=lambda position: (cumulative[position], -position)) if cell_ids else 0
    top_cell = {
        "cell_id": cell_ids[best],
        "cumulative_unmet_rule": cumulative[best] if cell_ids else 0,
        "frame": unmet_rule.index(max(unmet_rule)) if unmet_rule and max(unmet_rule) > 0 else 0,
        "rule": "累计 S-RULE 未满足请求最高；平手按 cell_id 升序",
    }
    totals: dict[str, dict[str, Any]] = {}
    for strategy in STRATEGIES:
        totals[strategy] = {
            field: sum(int(frame["strategies"][strategy]["kpi"][field]) for frame in frames)
            for field in ("borrow_requests", "borrow_met", "unmet_requests", "bikes_moved")
        }
        totals[strategy]["distance_bike_km"] = sum(
            float(frame["strategies"][strategy]["kpi"]["distance_bike_km"]) for frame in frames
        )

    bundle_sha = hashlib.sha256(replay_data_js(replay).encode("utf-8")).hexdigest()
    inputs = {
        "cells": {
            "path": str(cells_path.relative_to(layout.root)),
            "sha256": _sha256(cells_path),
        },
        "replay": {
            strategy: {
                "path": f"outputs/replay.{strategy}.{source}.parquet",
                "sha256": _sha256(layout.replay_path(source, strategy)),
            }
            for strategy in STRATEGIES
        },
        "sim_csv": {
            strategy: {
                "path": f"outputs/sim_{strategy}.{source}.csv",
                "sha256": _sha256(layout.sim_path(source, strategy, "csv")),
            }
            for strategy in STRATEGIES
        },
        "moves": {
            strategy: {
                "path": f"outputs/sim_moves.{strategy}.{source}.json",
                "sha256": _sha256(layout.outputs / f"sim_moves.{strategy}.{source}.json"),
            }
            for strategy in STRATEGIES
        },
        "sensitivity": {
            "path": f"outputs/sim_sensitivity.{source}.json",
            "sha256": _sha256(layout.outputs / f"sim_sensitivity.{source}.json"),
        },
        "comparison": {
            "path": f"outputs/strategy_comparison.{source}.json",
            "sha256": _sha256(layout.outputs / f"strategy_comparison.{source}.json"),
        },
    }

    guide_inputs = {
        "n_frames": replay["n_frames"],
        "n_cells": len(cell_ids),
        "cell_size_m": cell_size,
        "buckets": bucket_labels,
        "totals": totals,
        "events": events,
        "unmet_rule": unmet_rule,
        "unmet_optimized": unmet_opt,
        "delta_domain": color_domain["delta"],
        "frames_delta": frames_delta,
        "ledger_top_cell": top_cell,
        "scenarios": scenarios,
    }

    return {
        "source": source,
        "n_frames": replay["n_frames"],
        "n_cells": len(cell_ids),
        "cell_size_m": cell_size,
        "cell_ids": cell_ids,
        "geom": replay["geom"],
        "buckets": bucket_labels,
        "color_domain": color_domain,
        "frames": frame_payload,
        "frames_delta": frames_delta,
        "events": events,
        "scenarios": scenarios,
        "ledger_top_cell": top_cell,
        "cell_cumulative": {
            cell_id: cumulative[position] for position, cell_id in enumerate(cell_ids)
        },
        "totals": totals,
        "equations": EQUATIONS,
        "units": UNITS,
        # §2.3: one serialisable record per derived value, keyed by what it
        # describes, so the bundle itself -- not just the DOM -- can be audited.
        "evidence": {
            "build": {
                "source": source,
                "join_key": f"source={source}",
                "inputs": inputs,
                "replay_bundle_sha256": bundle_sha,
            },
            "events": events,
            "scenarios": [
                {
                    "id": row["id"],
                    "source_file": row["source_file"],
                    "join_key": row["join_key"],
                    "strategies": {
                        strategy: {
                            metric: {
                                "source_file": row["source_file"],
                                "join_key": row["join_key"],
                                "metric": metric,
                                "value": record["value"],
                                # The value is the scenario's own number; the
                                # delta against baseline is derived, so it gets
                                # its own operands and formula.
                                "formula": f"sim_sensitivity[{row['id']}].{strategy}.{metric}",
                                "operands": {
                                    "value": record["value"],
                                    "baseline": record["value"] - record["delta"]["absolute"],
                                    "delta": record["delta"]["absolute"],
                                    "delta_formula": record["formula"],
                                    "delta_status": record["delta"]["status"],
                                },
                            }
                            for metric, record in row["strategies"][strategy].items()
                        }
                        for strategy in STRATEGIES
                    },
                }
                for row in scenarios["rows"]
            ],
            "ledger": {
                strategy: {
                    "source_file": f"outputs/replay.{strategy}.{source}.parquet",
                    "join_key": "cell_id={cell_id};time_bucket={time_bucket}",
                    "fields": {
                        "inventory_start": {
                            "source_file": f"outputs/replay.{strategy}.{source}.parquet",
                            "formula": "parquet.inventory_start",
                            "derived": False,
                        },
                        "moved_in": {
                            "source_file": f"outputs/sim_moves.{strategy}.{source}.json",
                            "formula": "sum(bikes) where to_cell = cell_id",
                            "derived": False,
                        },
                        "moved_out": {
                            "source_file": f"outputs/sim_moves.{strategy}.{source}.json",
                            "formula": "sum(bikes) where from_cell = cell_id；等于 parquet.bikes_moved",
                            "derived": False,
                        },
                        "scheduled_inventory": {
                            "source_file": f"outputs/replay.{strategy}.{source}.parquet",
                            "formula": EQUATIONS["scheduled_inventory"],
                            "derived": True,
                            "operands": ["inventory_start", "moved_in", "moved_out"],
                        },
                        "inventory_end": {
                            "source_file": f"outputs/replay.{strategy}.{source}.parquet",
                            "formula": EQUATIONS["inventory_end"],
                            "derived": True,
                            "operands": [
                                "inventory_start",
                                "moved_in",
                                "moved_out",
                                "returns_applied",
                                "borrow_met",
                            ],
                        },
                        "unmet": {
                            "source_file": f"outputs/replay.{strategy}.{source}.parquet",
                            "formula": EQUATIONS["unmet"],
                            "derived": True,
                            "operands": ["borrow_requests", "borrow_met"],
                        },
                        "overflow": {
                            "source_file": f"outputs/replay.{strategy}.{source}.parquet",
                            "formula": EQUATIONS["overflow"],
                            "derived": True,
                            "operands": ["returns", "returns_applied"],
                        },
                    },
                }
                for strategy in STRATEGIES
            },
        },
        # Kept out of the published bundle (``analysis_payload`` drops ``_`` keys):
        # the workbench prerenders its first grid from the same replay bundle the
        # player already loads, so no per-cell number is stored twice.
        "_replay_frames": frames,
        "replay_bundle_sha": bundle_sha,
        "inputs": inputs,
        "guide": build_guide(
            source,
            other=_other_source_summary(config, layout, source),
            **guide_inputs,
        ),
    }


def build_all_presentations(
    config: dict[str, Any],
    layout: Layout,
    sources: list[str] | tuple[str, ...],
) -> dict[str, dict[str, Any]]:
    """Every source in one call; each is already complete, ``guide`` included."""
    presentations = {source: build_presentation(config, layout, source) for source in sources}
    attach_guide(presentations)
    return presentations


def attach_guide(presentations: dict[str, dict[str, Any]]) -> None:
    """Deprecated: :func:`build_presentation` now attaches the guide itself.

    Kept so an external caller that still builds source-by-source keeps working;
    it is a no-op for presentations that already carry a guide.
    """
    for source, presentation in presentations.items():
        if "guide" in presentation:
            presentation.pop("_guide_inputs", None)
            continue
        others = [value for key, value in presentations.items() if key != source]
        other = None
        if others:
            other_presentation = others[0]
            other = {
                "source": other_presentation["source"],
                "n_frames": other_presentation["n_frames"],
                "n_cells": other_presentation["n_cells"],
                "unmet_rule": other_presentation["totals"]["rule"]["unmet_requests"],
            }
        inputs = presentation.pop("_guide_inputs")
        presentation["guide"] = build_guide(source, other=other, **inputs)


# -- published bundle -------------------------------------------------------
#: Keys the workbench reads from the paired ``replay-data.js`` instead of
#: duplicating them here (one build, one copy of each number).
SHARED_WITH_PLAYER = ("inventory_end", "unmet", "moved", "moves", "kpi")


def analysis_payload(presentation: dict[str, Any]) -> dict[str, Any]:
    """The part ``presentation.js`` carries: everything the player bundle lacks."""
    payload = {key: value for key, value in presentation.items() if not key.startswith("_")}
    payload["shared_with_player"] = list(SHARED_WITH_PLAYER)
    payload["frames"] = [
        {
            "index": frame["index"],
            "time_bucket": frame["time_bucket"],
            "strategies": frame["strategies"],
            "delta": frame["delta"],
            "delta_summary": frame["delta_summary"],
            "gain": frame["gain"],
            "evidence": frame["evidence"],
        }
        for frame in presentation["frames"]
    ]
    return payload


def presentation_js(presentation: dict[str, Any]) -> str:
    """A classic script that registers one source and keeps the paired bundle.

    ``replay/<source>/replay-data.js`` assigns ``window.REPLAY_DATA`` (a Phase 3
    contract that must not change), so a page that wants two sources loads each
    pair in order and this file stashes the bundle it just saw.  No copy of the
    bundle is written, so the site does not grow by a second 900 KB file.
    """
    body = json.dumps(
        analysis_payload(presentation), ensure_ascii=False, separators=(",", ":")
    ).replace("<", "\\u003c")
    return (
        "(function(){\n"
        "  var R = window.REPLAY_DATA;\n"
        "  if(!R || !R.source) return;\n"
        "  var store = window.REPLAY_DATA_SOURCES = window.REPLAY_DATA_SOURCES || {};\n"
        "  store[R.source] = R;\n"
        "  var P = window.PRESENTATION_DATA = window.PRESENTATION_DATA || {};\n"
        f"  P[R.source] = {body};\n"
        "})();"
    )
