"""F12/F13: the replay player, its single data bundle, and the dashboard.

Everything that knows how to turn the Phase 1–2 artefacts into player data lives
here, in :func:`build_replay_data`.  The three consumers -- ``replay-data.js``,
``player.html`` and ``GET /api/replay/timeline`` -- all call that one function,
so there is exactly one set of numbers and no second derivation to drift.

The first frame is rendered into the static DOM in Python.  That is what lets
the acceptance tests read the page with the standard library alone: no headless
browser, no ``fetch`` at ``file://``, no ES modules.
"""

from __future__ import annotations

import html
import json
import shutil
from typing import Any

import pandas as pd

from bike_agent.config import Layout, source_config
from bike_agent.replay.site import (
    CELL_GAP,
    CELL_PX,
    STRATEGIES,
    STRATEGY_LABELS,
    _center,
    _fmt_float,
    _fmt_int,
    _geometry,
    _sparse_note,
)
from bike_agent.utils import read_json

TREND_STEP = 900  # milliseconds per frame at 1x; speed only changes this pause
TREND_CANVAS_W = 900.0  # viewBox width basis of the trend chart; unrelated to TREND_STEP
DELTA_METRICS = ("bikes_moved", "unmet_requests", "inventory_end", "distance_bike_km")

# NF-15 hard caps, enforced at build time so a future growth fails loudly here
# instead of shipping a page that quietly breaks the budget.
MAX_DATA_BYTES = 1024 * 1024
MAX_PLAYER_BYTES = 256 * 1024
MAX_DASHBOARD_BYTES = 512 * 1024

# One caption per figure family, shared with the report renderer so the two
# descriptions cannot drift apart.
FIGURE_CAPTIONS = {
    "demand_heatmap": "需求热力图：网格 × 时间单元的借出量",
    "peak_hours": "高峰时段分布",
    "forecast_vs_actual": "预测值与实际值对比（测试段）",
    "priority_lists": "优先补车 / 优先清运清单",
    "strategy_comparison": "同一回放条件下的两策略对比",
    "significance": "六条结论的变化幅度与 95% 置信区间",
}

_CSV_METRICS = (
    "time_bucket",
    "borrow_requests",
    "borrow_met",
    "unmet_requests",
    "returns",
    "returns_applied",
    "returns_overflow",
    "bikes_moved",
    "distance_bike_km",
    "inventory_start",
    "inventory_end",
)


# -- one bundle, three consumers -------------------------------------------
def build_replay_data(config: dict[str, Any], layout: Layout, source: str) -> dict[str, Any]:
    """Pack every persisted frame of one source into the F12 data contract.

    The bundle is built from ``outputs/`` only: per-cell state from
    ``replay.<strategy>.<source>.parquet``, totals from
    ``sim_<strategy>.<source>.csv``, dispatch records from
    ``sim_moves.<strategy>.<source>.json``.  Nothing is interpolated, sampled or
    synthesised; a bucket without dispatch records maps to an empty list.
    """
    cell_size = int(source_config(config, source)["cell_size_m"])
    cells_path = layout.cells_path(source, cell_size)
    if not cells_path.exists():
        raise FileNotFoundError(f"missing {cells_path}; run `bike-agent repro` first")
    coords, _, _ = _geometry(pd.read_parquet(cells_path))
    cell_ids = sorted(coords)

    steps: dict[str, pd.DataFrame] = {}
    state_by_bucket: dict[str, dict[pd.Timestamp, pd.DataFrame]] = {}
    moves_by_bucket: dict[str, dict[pd.Timestamp, list[dict[str, Any]]]] = {}
    for strategy in STRATEGIES:
        csv_path = layout.sim_path(source, strategy, "csv")
        state_path = layout.replay_path(source, strategy)
        moves_path = layout.outputs / f"sim_moves.{strategy}.{source}.json"
        for path in (csv_path, state_path, moves_path):
            if not path.exists():
                raise FileNotFoundError(f"missing {path}; run `bike-agent repro` first")
        frame = pd.read_csv(csv_path)
        frame["time_bucket"] = pd.to_datetime(frame["time_bucket"])
        steps[strategy] = frame.sort_values("time_bucket").reset_index(drop=True)
        state = pd.read_parquet(state_path)
        state["time_bucket"] = pd.to_datetime(state["time_bucket"])
        state_by_bucket[strategy] = {
            bucket: rows.set_index("cell_id") for bucket, rows in state.groupby("time_bucket")
        }
        grouped: dict[pd.Timestamp, list[dict[str, Any]]] = {}
        for record in read_json(moves_path):
            grouped.setdefault(pd.Timestamp(record["time_bucket"]), []).append(
                {
                    "from_cell": str(record["from_cell"]),
                    "to_cell": str(record["to_cell"]),
                    "bikes": int(record["bikes"]),
                    "distance_m": float(record["distance_m"]),
                }
            )
        moves_by_bucket[strategy] = grouped

    buckets = list(steps["rule"]["time_bucket"])
    if list(steps["optimized"]["time_bucket"]) != buckets:
        raise ValueError(f"{source}: the two strategies replay different buckets; refusing to pack")

    # F13-1: one fixed domain per source, never recomputed per frame.
    color_max = 0
    for strategy in STRATEGIES:
        for rows in state_by_bucket[strategy].values():
            if len(rows):
                color_max = max(color_max, int(rows["inventory_end"].max()))

    frames: list[dict[str, Any]] = []
    for index, bucket in enumerate(buckets):
        entry: dict[str, Any] = {
            "index": index,
            "time_bucket": bucket.strftime("%Y-%m-%d %H:%M:%S"),
            "strategies": {},
            "moves": {},
        }
        for strategy in STRATEGIES:
            rows = state_by_bucket[strategy][bucket]
            # cell_ids order is the index of every parallel array.
            entry["strategies"][strategy] = {
                "inventory_end": [int(rows.loc[cell_id, "inventory_end"]) for cell_id in cell_ids],
                "unmet": [int(rows.loc[cell_id, "unmet_requests"]) for cell_id in cell_ids],
                "moved": [int(rows.loc[cell_id, "bikes_moved"]) for cell_id in cell_ids],
                "kpi": _kpi_row(steps[strategy].iloc[index]),
            }
            entry["moves"][strategy] = list(moves_by_bucket[strategy].get(bucket, []))

        frames.append(entry)

    return {
        "source": source,
        "n_frames": len(frames),
        "cell_px": CELL_PX,
        "cell_gap": CELL_GAP,
        "cell_ids": cell_ids,
        "geom": [list(coords[cell_id]) for cell_id in cell_ids],
        "color_domain": {"min": 0, "max": color_max},
        "frames": frames,
    }


def _kpi_row(row: pd.Series) -> dict[str, Any]:
    """The whole ``sim_<strategy>.<source>.csv`` row, values unchanged."""
    payload: dict[str, Any] = {}
    for field in _CSV_METRICS:
        value = row[field]
        if field == "time_bucket":
            payload[field] = pd.Timestamp(value).strftime("%Y-%m-%d %H:%M:%S")
        elif field == "distance_bike_km":
            payload[field] = float(value)
        else:
            payload[field] = int(value)
    return payload


def replay_data_js(data: dict[str, Any]) -> str:
    """The exact published file: ``window.REPLAY_DATA = <JSON>;`` and nothing else."""
    body = json.dumps(data, ensure_ascii=False, separators=(",", ":")).replace("<", "\\u003c")
    return f"window.REPLAY_DATA = {body};"


# -- player page ------------------------------------------------------------
def write_player(config: dict[str, Any], layout: Layout, source: str) -> dict[str, Any]:
    """Write ``replay/<source>/replay-data.js`` and ``player.html``."""
    data = build_replay_data(config, layout, source)
    source_dir = layout.root / "replay" / source
    source_dir.mkdir(parents=True, exist_ok=True)
    payload = replay_data_js(data)
    service_rates = {
        strategy: float(read_json(layout.sim_path(source, strategy, "json"))["service_rate"])
        for strategy in STRATEGIES
    }
    page = render_player(data, service_rates)
    # Fail fast on the NF-15 caps: shrinking the frame set to fit is forbidden,
    # so an oversized page must be an error here rather than a silent regression.
    if len(payload.encode("utf-8")) > MAX_DATA_BYTES:
        raise ValueError(
            f"{source}: replay-data.js is {len(payload.encode('utf-8'))} bytes, over the NF-15 cap "
            f"of {MAX_DATA_BYTES}; compress the encoding rather than dropping frames"
        )
    if len(page.encode("utf-8")) > MAX_PLAYER_BYTES:
        raise ValueError(
            f"{source}: player.html is {len(page.encode('utf-8'))} bytes, over the NF-15 cap "
            f"of {MAX_PLAYER_BYTES}; compress the encoding rather than dropping frames"
        )
    (source_dir / "replay-data.js").write_text(payload, encoding="utf-8")
    (source_dir / "player.html").write_text(page, encoding="utf-8")
    return {
        "source": source,
        "n_frames": data["n_frames"],
        "n_cells": len(data["cell_ids"]),
        "color_domain": data["color_domain"],
        "player_bytes": len(page.encode("utf-8")),
        "data_bytes": len(payload.encode("utf-8")),
    }


def render_player(data: dict[str, Any], service_rates: dict[str, float]) -> str:
    source = data["source"]
    n_frames = data["n_frames"]
    frames = data["frames"]
    first = frames[0]
    coords = {cell_id: tuple(geom) for cell_id, geom in zip(data["cell_ids"], data["geom"])}
    max_ix = max((pos[0] for pos in coords.values()), default=0)
    max_iy = max((pos[1] for pos in coords.values()), default=0)
    step = CELL_PX + CELL_GAP
    width = (max_ix + 1) * step
    height = (max_iy + 1) * step
    color_max = int(data["color_domain"]["max"])

    csv_sources = {strategy: f"outputs/sim_{strategy}.{source}.csv" for strategy in STRATEGIES}
    summary_sources = {strategy: f"outputs/sim_{strategy}.{source}.json" for strategy in STRATEGIES}
    moves_sources = {strategy: f"outputs/sim_moves.{strategy}.{source}.json" for strategy in STRATEGIES}
    state_sources = {strategy: f"outputs/replay.{strategy}.{source}.parquet" for strategy in STRATEGIES}

    sections: list[str] = []
    cell_ids = data["cell_ids"]
    color_min = int(data["color_domain"]["min"])
    move_counts = {strategy: [len(f["moves"][strategy]) for f in frames] for strategy in STRATEGIES}
    for strategy in STRATEGIES:
        frame = first["strategies"][strategy]
        moves = first["moves"][strategy]
        rects = []
        for cell_id, inventory, unmet, moved in zip(
            cell_ids, frame["inventory_end"], frame["unmet"], frame["moved"]
        ):
            ix, iy = coords[cell_id]
            opacity = _opacity(inventory, color_max)
            rects.append(
                f'<rect class="cell" data-cell="{html.escape(cell_id)}" '
                f'data-inventory-end="{inventory}" data-unmet="{unmet}" data-moved="{moved}" '
                f'x="{ix * step}" y="{iy * step}" width="{CELL_PX}" height="{CELL_PX}" rx="1.5" '
                f'fill-opacity="{opacity:.3f}" data-source="{state_sources[strategy]}">'
                f"<title>{html.escape(cell_id)} 期末 {inventory} 未满足 {unmet} 搬出 {moved}</title></rect>"
            )

        arrows = "".join(_arrow_svg(move, coords, moves_sources[strategy], strategy) for move in moves)
        kpis = _kpi_block(
            strategy,
            source,
            frame["kpi"],
            moves,
            state_sources[strategy],
            moves_sources[strategy],
            len(cell_ids),
        )
        detail = _detail_block(
            cell_ids, frame, moves, state_sources[strategy], moves_sources[strategy]
        )
        summary_service_rate = float(service_rates[strategy])

        sections.append(
            f'<section class="strategy" data-strategy="{strategy}">'
            f"<h2>{html.escape(STRATEGY_LABELS[strategy])}</h2>"
            f'<p class="rate">服务率 <b class="metric" data-source="{summary_sources[strategy]}" '
            f'data-metric="service_rate" data-value="{summary_service_rate!r}">'
            f"{summary_service_rate * 100:.2f}%</b>（全时段模拟值）</p>"
            f'<dl class="kpis">{kpis}</dl>'
            f'<div class="canvas">'
            f'<svg class="grid" data-strategy="{strategy}" data-source="{state_sources[strategy]}" '
            f'viewBox="0 0 {width} {height}" role="img" '
            f'aria-label="{html.escape(STRATEGY_LABELS[strategy])} 逐格库存">'
            f'<defs><marker id="arrow-{strategy}" viewBox="0 0 10 10" refX="9" refY="5" '
            f'markerUnits="userSpaceOnUse" markerWidth="6" markerHeight="6" orient="auto-start-reverse">'
            f'<path d="M0,0 L10,5 L0,10 z" fill="var(--arrow)"/></marker></defs>'
            f'<g class="cells">{"".join(rects)}</g>'
            f'<g class="arrows">{arrows}</g></svg>'
            f'<p class="legend">格子深浅 = 期末库存，色标固定为 0–{color_max}（见上方刻度）；'
            f"红框 = 该格有未满足请求；箭头 = <code>{moves_sources[strategy]}</code> 中的一条搬运记录。"
            f"箭头只表示方向，不代表更细的时间分辨率。</p>"
            f"</div>"
            f"{detail}"
            f"{_sparse_note(summary_service_rate, source, strategy, move_counts[strategy])}"
            f"</section>"
        )

    trend = _trend_svg(data, csv_sources)
    start_bucket = frames[0]["time_bucket"]
    end_bucket = frames[-1]["time_bucket"]
    speeds = "".join(
        f'<button class="speed{" active" if value == 1 else ""}" data-control="speed" '
        f'data-speed="{value}">{value}×</button>'
        for value in (0.5, 1, 2, 4)
    )

    return f"""<!doctype html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>回放播放器 · {html.escape(source.upper())} · bike-agent</title>
<link rel="stylesheet" href="../style.css">
<link rel="stylesheet" href="../player.css">
</head>
<body class="player">
<header>
<h1>调度回放播放器 · {html.escape(source.upper())}</h1>
<p class="meta">时间范围
<b class="metric" data-source="{csv_sources["rule"]}" data-derived="first-bucket" data-metric="time_range_start" data-value="{html.escape(start_bucket)}">{html.escape(start_bucket)}</b>
→ <b class="metric" data-source="{csv_sources["rule"]}" data-derived="last-bucket" data-metric="time_range_end" data-value="{html.escape(end_bucket)}">{html.escape(end_bucket)}</b>
· 帧数 <b class="metric" data-source="{csv_sources["rule"]}" data-derived="row-count" data-metric="frame_count" data-value="{n_frames}">{n_frames}</b>
· 当前帧 <b class="metric" id="frame-label" data-source="{csv_sources["rule"]}" data-derived="row-position" data-metric="frame_index" data-value="1" aria-live="polite">1</b>
· 时间单元 <b class="metric" id="bucket-label" data-source="{csv_sources["rule"]}" data-metric="time_bucket" data-value="{html.escape(start_bucket)}">{html.escape(start_bucket)}</b></p>
<nav class="pager"><a href="../index.html">全部数据源</a><a href="../dashboard.html">总览页</a><a href="frame-0000.html">逐帧页</a></nav>
<p class="notice">所有数字均为<strong>模拟值</strong>，来自 <code>outputs/</code> 下的静态产物；
播放只改变帧与帧之间的停顿，不改变数据。没有搬运记录的时间单元不会出现箭头。</p>
</header>
<section class="controls" aria-label="播放控制" data-step-ms="{TREND_STEP}">
<button data-control="play" data-state="paused">播放</button>
<button data-control="prev">上一帧</button>
<button data-control="next">下一帧</button>
<label class="scrub">进度
<input type="range" data-control="scrub" min="0" max="{max(n_frames - 1, 0)}" step="1" value="0" aria-label="进度">
</label>
<div class="speeds" role="group" aria-label="速度">{speeds}</div>
<label class="loop"><input type="checkbox" data-control="loop"> 循环</label>
</section>
<p class="scale" data-scale="inventory" data-min="{color_min}" data-max="{color_max}" data-source="{state_sources["rule"]}">
期末库存固定色标：<span class="scale-end" data-bound="min" data-value="{color_min}">{color_min}</span>
<span class="scale-bar" aria-hidden="true"></span>
<span class="scale-end" data-bound="max" data-value="{color_max}">{color_max}</span>
（跨帧不变，两策略共用）</p>
<main>
<section class="card wide trend-card">
<h2>跨帧趋势</h2>
{trend}
<p class="series-legend">
<span data-series="bikes_moved" data-unit="辆" data-source="{csv_sources["rule"]}">搬运车辆数（辆）：S-RULE 柱 / S-OPT 柱</span>
<span data-series="unmet_requests" data-unit="个" data-source="{csv_sources["rule"]}">未满足请求（个）：折线点</span>
</p>
<p class="hint">每个柱/点对应一个时间单元，点击可跳到该帧；高亮为当前帧。柱高为零表示该帧没有搬运。</p>
</section>
{''.join(sections)}
</main>
<noscript>
<p class="notice">JavaScript 已禁用：下方静态内容为第 1 帧。完整内容以逐帧页为准：
<a href="frame-0000.html">frame-0000.html</a>。本页时间范围 {html.escape(start_bucket)} → {html.escape(end_bucket)}，
共 {n_frames} 帧。</p>
</noscript>
<script src="replay-data.js"></script>
<script src="../player.js"></script>
</body>
</html>
"""


def _opacity(value: int, color_max: int) -> float:
    return 0.12 + 0.88 * (float(value) / color_max) if color_max else 0.12


def _arrow_svg(
    move: dict[str, Any],
    coords: dict[str, tuple[int, int]],
    source_file: str,
    strategy: str,
) -> str:
    x1, y1 = _center(coords, str(move["from_cell"]))
    x2, y2 = _center(coords, str(move["to_cell"]))
    span = ((x2 - x1) ** 2 + (y2 - y1) ** 2) ** 0.5
    if span > 0:
        shrink = min(CELL_PX * 0.45, span / 3.0)
        x2 -= (x2 - x1) / span * shrink
        y2 -= (y2 - y1) / span * shrink
    bikes = int(move["bikes"])
    return (
        f'<line class="arrow" x1="{x1:.2f}" y1="{y1:.2f}" x2="{x2:.2f}" y2="{y2:.2f}" '
        f'marker-end="url(#arrow-{strategy})" data-source="{source_file}" '
        f'data-from="{html.escape(str(move["from_cell"]))}" data-to="{html.escape(str(move["to_cell"]))}" '
        f'data-bikes="{bikes}" stroke-width="{0.6 + min(2.4, bikes / 30.0):.2f}">'
        f'<title>{html.escape(str(move["from_cell"]))} → {html.escape(str(move["to_cell"]))}: '
        f'{bikes} 辆（{float(move["distance_m"]):.0f} m）</title></line>'
    )


def _kpi_block(
    strategy: str,
    source: str,
    kpi: dict[str, Any],
    moves: list[dict[str, Any]],
    state_file: str,
    moves_file: str,
    cell_count: int,
) -> str:
    csv_file = f"outputs/sim_{strategy}.{source}.csv"

    def item(label: str, metric: str, value: Any, source_file: str, display: str, fmt: str) -> str:
        delta = ""
        if metric in DELTA_METRICS:
            delta = (
                f'<span class="delta" data-delta-metric="{metric}" data-prev="">—</span>'
            )
        return (
            f'<div class="kpi"><dt>{html.escape(label)}</dt>'
            f'<dd class="metric" data-source="{source_file}" data-metric="{metric}" '
            f'data-value="{html.escape(str(value))}" data-format="{fmt}">'
            f"<span class=\"value\">{html.escape(display)}</span>{delta}</dd></div>"
        )

    moved_total = sum(int(move["bikes"]) for move in moves)
    return "".join(
        [
            item("借车请求", "borrow_requests", kpi["borrow_requests"], csv_file, _fmt_int(kpi["borrow_requests"]), "int"),
            item("实际满足", "borrow_met", kpi["borrow_met"], csv_file, _fmt_int(kpi["borrow_met"]), "int"),
            item("未满足请求", "unmet_requests", kpi["unmet_requests"], csv_file, _fmt_int(kpi["unmet_requests"]), "int"),
            item("归还", "returns", kpi["returns"], csv_file, _fmt_int(kpi["returns"]), "int"),
            item("入库归还", "returns_applied", kpi["returns_applied"], csv_file, _fmt_int(kpi["returns_applied"]), "int"),
            item("容量溢出", "returns_overflow", kpi["returns_overflow"], csv_file, _fmt_int(kpi["returns_overflow"]), "int"),
            item("搬运车辆数", "bikes_moved", kpi["bikes_moved"], csv_file, _fmt_int(kpi["bikes_moved"]), "int"),
            item(
                "搬运距离（bike-km）",
                "distance_bike_km",
                kpi["distance_bike_km"],
                csv_file,
                _fmt_float(kpi["distance_bike_km"]),
                "float",
            ),
            item("期初库存", "inventory_start", kpi["inventory_start"], csv_file, _fmt_int(kpi["inventory_start"]), "int"),
            item("期末库存", "inventory_end", kpi["inventory_end"], csv_file, _fmt_int(kpi["inventory_end"]), "int"),
            item("本帧搬运记录数", "n_moves", len(moves), moves_file, _fmt_int(len(moves)), "int"),
            item("本帧搬运车辆合计", "moves_bikes", moved_total, moves_file, _fmt_int(moved_total), "int"),
            item("本帧格子数", "n_cells", cell_count, state_file, _fmt_int(cell_count), "int"),
        ]
    )


def _detail_block(
    cell_ids: list[str],
    frame: dict[str, Any],
    moves: list[dict[str, Any]],
    state_file: str,
    moves_file: str,
) -> str:
    move_items = "".join(
        f'<li class="move-row" data-from="{html.escape(move["from_cell"])}" '
        f'data-to="{html.escape(move["to_cell"])}" data-bikes="{int(move["bikes"])}" '
        f'data-source="{moves_file}">{html.escape(move["from_cell"])} → '
        f'{html.escape(move["to_cell"])}：{int(move["bikes"])} 辆</li>'
        for move in moves
    )
    if not move_items:
        move_items = '<li class="empty">本帧没有搬运记录。</li>'
    unmet_items = []
    for cell_id, unmet in zip(cell_ids, frame["unmet"]):
        if unmet > 0:
            unmet_items.append(
                f'<li class="unmet-row" data-cell="{html.escape(cell_id)}" data-unmet="{int(unmet)}" '
                f'data-source="{state_file}">{html.escape(cell_id)}：{int(unmet)}</li>'
            )
    unmet_markup = "".join(unmet_items) or '<li class="empty">本帧没有未满足请求。</li>'
    return (
        f'<div class="details">'
        f'<div><h3>本帧搬运明细（{len(moves)} 条）</h3><ol class="move-list">{move_items}</ol></div>'
        f'<div><h3>本帧缺车清单</h3><ul class="unmet-list">{unmet_markup}</ul></div>'
        f"</div>"
    )


def _trend_svg(data: dict[str, Any], csv_sources: dict[str, str]) -> str:
    frames = data["frames"]
    n_frames = len(frames)
    # Horizontal basis for the chart, unrelated to TREND_STEP (which is a pause).
    step_x = max(6.0, TREND_CANVAS_W / max(n_frames, 1))
    width = max(420.0, step_x * n_frames)
    height = 130.0
    top, bottom = 10.0, 108.0
    plot_height = bottom - top

    bikes = [
        int(frame["strategies"][strategy]["kpi"]["bikes_moved"])
        for frame in frames
        for strategy in STRATEGIES
    ]
    unmet = [int(frame["strategies"]["rule"]["kpi"]["unmet_requests"]) for frame in frames]
    max_bikes = max(bikes) if bikes else 0
    max_unmet = max(unmet) if unmet else 0

    bar_width = max(1.5, step_x * 0.32)
    bars = []
    points = []
    centres = []
    for index, frame in enumerate(frames):
        base_x = index * step_x
        for offset, strategy in enumerate(STRATEGIES):
            value = int(frame["strategies"][strategy]["kpi"]["bikes_moved"])
            bar_h = plot_height * (value / max_bikes) if max_bikes else 0.0
            x = base_x + step_x * (0.14 + 0.38 * offset)
            bars.append(
                f'<rect class="trend-bar" data-strategy="{strategy}" data-frame-index="{index}" '
                f'data-value="{value}" data-seek="{index}" data-source="{csv_sources[strategy]}" '
                f'x="{x:.2f}" y="{bottom - bar_h:.2f}" width="{bar_width:.2f}" height="{bar_h:.2f}"/>'
            )
        value = int(frame["strategies"]["rule"]["kpi"]["unmet_requests"])
        point_y = bottom - (plot_height * (value / max_unmet) if max_unmet else 0.0)
        centre_x = base_x + step_x * 0.5
        centres.append((centre_x, point_y))
        points.append(
            f'<circle class="trend-point" data-frame-index="{index}" data-value="{value}" '
            f'data-seek="{index}" data-source="{csv_sources["rule"]}" '
            f'cx="{centre_x:.2f}" cy="{point_y:.2f}" r="2.2"/>'
        )
    line_points = " ".join(f"{x:.2f},{y:.2f}" for x, y in centres)
    cursor_x = centres[0][0]
    return (
        f'<svg class="trend" viewBox="0 0 {width:.2f} {height:.2f}" role="img" '
        f'aria-label="跨帧趋势：搬运车辆数与未满足请求">'
        f'<line class="trend-axis" x1="0" y1="{bottom}" x2="{width:.2f}" y2="{bottom}"/>'
        f'<line class="trend-cursor" x1="{cursor_x:.2f}" y1="{top}" x2="{cursor_x:.2f}" y2="{bottom}" '
        f'data-frame-index="0"/>'
        f'<g class="trend-bars">{"".join(bars)}</g>'
        f'<polyline class="trend-line" points="{line_points}"/>'
        f'<g class="trend-points">{"".join(points)}</g>'
        f"</svg>"
    )


# -- dashboard (F14) --------------------------------------------------------
def build_dashboard(config: dict[str, Any], layout: Layout) -> dict[str, Any]:
    """Write ``replay/dashboard.html`` and mirror ``figures/`` into the site.

    Every card is a ``key_numbers.json`` entry; the cross-source table is built
    from the same entries, so no number on the page has a second source of truth
    (F14-1 / F14-3 / NF-18).
    """
    key_numbers_path = layout.key_numbers_path()
    if not key_numbers_path.exists():
        raise FileNotFoundError(f"missing {key_numbers_path}; run `bike-agent repro` first")
    entries = read_json(key_numbers_path)
    site_dir = layout.root / "replay"
    site_dir.mkdir(parents=True, exist_ok=True)

    figures = sorted(layout.figures.glob("*.png"))
    if not figures:
        raise FileNotFoundError(f"no figures under {layout.figures}; run `bike-agent repro` first")
    figure_dir = site_dir / "figures"
    figure_dir.mkdir(parents=True, exist_ok=True)
    for figure in figures:
        shutil.copyfile(figure, figure_dir / figure.name)

    page = render_dashboard(entries, figures)
    if len(page.encode("utf-8")) > MAX_DASHBOARD_BYTES:
        raise ValueError(
            f"dashboard.html is {len(page.encode('utf-8'))} bytes, over the NF-15 cap of {MAX_DASHBOARD_BYTES}"
        )
    (site_dir / "dashboard.html").write_text(page, encoding="utf-8")
    return {"path": str(site_dir / "dashboard.html"), "n_cards": len(entries), "n_figures": len(figures)}


def render_dashboard(entries: list[dict[str, Any]], figures: list[Any]) -> str:
    cards = "".join(
        f'<article class="num-card"><span class="num-label">{html.escape(str(entry["metric"]))}</span>'
        f'<b class="metric" data-metric="{html.escape(str(entry["metric"]))}" '
        f'data-value="{html.escape(str(entry["value"]))}" '
        f'data-source="{html.escape(str(entry["source_file"]))}">'
        f"{html.escape(_display_value(entry['value']))}</b>"
        f'<span class="num-source">{html.escape(str(entry["source_file"]))}</span></article>'
        for entry in entries
    )

    by_metric = {str(entry["metric"]): entry for entry in entries}
    rows = []
    for metric, xm_entry in by_metric.items():
        if not metric.startswith("XM · "):
            continue
        suffix = metric[len("XM · "):]
        sh_entry = by_metric.get(f"SH · {suffix}")
        if sh_entry is None:
            continue
        rows.append(
            "<tr>"
            f'<th scope="row">{html.escape(suffix)}</th>'
            + "".join(
                f'<td class="metric" data-metric="{html.escape(str(entry["metric"]))}" '
                f'data-value="{html.escape(str(entry["value"]))}" '
                f'data-source="{html.escape(str(entry["source_file"]))}">'
                f"{html.escape(_display_value(entry['value']))}</td>"
                for entry in (xm_entry, sh_entry)
            )
            + "</tr>"
        )

    strategy_suffixes = [
        "rule unmet requests",
        "optimized unmet requests",
        "rule distance bike-km",
        "optimized distance bike-km",
        "rule composite cost",
        "optimized composite cost",
        "rule service rate",
        "optimized service rate",
    ]
    strategy_rows = []
    for suffix in strategy_suffixes:
        cells = [by_metric.get(f"{label} · {suffix}") for label in ("XM", "SH")]
        if any(cell is None for cell in cells):
            continue
        strategy_rows.append(
            "<tr>"
            f'<th scope="row">{html.escape(suffix)}</th>'
            + "".join(
                f'<td class="metric" data-metric="{html.escape(str(entry["metric"]))}" '
                f'data-value="{html.escape(str(entry["value"]))}" '
                f'data-source="{html.escape(str(entry["source_file"]))}">'
                f"{html.escape(_display_value(entry['value']))}</td>"
                for entry in cells
                if entry is not None
            )
            + "</tr>"
        )

    figure_markup = []
    for figure in figures:
        stem = figure.stem.split(".")[0]
        label = FIGURE_CAPTIONS.get(stem, stem.replace("_", " "))
        figure_markup.append(
            f'<figure class="fig" data-source="figures/{html.escape(figure.name)}">'
            f'<img src="figures/{html.escape(figure.name)}" alt="{html.escape(label)}" loading="lazy">'
            f"<figcaption>{html.escape(label)}"
            f'<span class="fig-source"> · 来源 <code>figures/{html.escape(figure.name)}</code></span>'
            f"</figcaption></figure>"
        )

    return f"""<!doctype html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>总览 · bike-agent</title>
<link rel="stylesheet" href="style.css">
<link rel="stylesheet" href="player.css">
</head>
<body class="dashboard">
<header>
<h1>回放总览</h1>
<p class="notice">一页汇总 Phase 1–2 的关键数字与全部图表。所有数字均为<strong>模拟值</strong>，
来自 <code>outputs/</code> 与 <code>figures/</code> 下的静态产物；每张卡片都标注了来源文件。</p>
<nav class="pager"><a href="index.html">全部数据源</a><a href="xm/player.html">XM 播放器</a><a href="sh/player.html">SH 播放器</a></nav>
</header>
<main>
<section class="card wide"><h2>关键数字</h2>
<div class="num-grid">{cards}</div>
</section>
<section class="card wide"><h2>两源对照</h2>
<p class="hint">只列出两个源都存在的指标；数值直接取自 <code>outputs/key_numbers.json</code>。</p>
<table class="compare"><thead><tr><th>指标</th><th>DS-XM</th><th>DS-SH</th></tr></thead>
<tbody>{''.join(rows)}</tbody></table>
</section>
<section class="card wide"><h2>策略对比（同一回放条件）</h2>
<p class="hint">S-RULE 与 S-OPT 在同一份需求预测、同一初始库存与容量假设下回放；
差异只在调度规则。数值取自 <code>outputs/key_numbers.json</code>，逐帧明细见
<a href="xm/player.html">XM 播放器</a> 与 <a href="sh/player.html">SH 播放器</a>。</p>
<table class="compare"><thead><tr><th>指标</th><th>DS-XM</th><th>DS-SH</th></tr></thead>
<tbody>{''.join(strategy_rows)}</tbody></table>
</section>
<section class="card wide"><h2>图表</h2>
<div class="fig-grid">{''.join(figure_markup)}</div>
</section>
</main>
</body>
</html>
"""


def _display_value(value: Any) -> str:
    if isinstance(value, bool):
        return "是" if value else "否"
    if isinstance(value, int):
        return f"{value:,}"
    if isinstance(value, float):
        return f"{value:,.4f}"
    return str(value)


# -- shared browser asset ---------------------------------------------------
PLAYER_CSS = """/* F12/F13 player + F14 dashboard additions; follows the system theme. */
.player header,.dashboard header{margin-bottom:12px}
.controls{display:flex;flex-wrap:wrap;gap:8px;align-items:center;margin:12px 0;
padding:10px;border:1px solid var(--line);border-radius:10px;background:var(--panel)}
.controls button,.controls input[type=range]{font:inherit}
.controls button{padding:5px 12px;border:1px solid var(--line);border-radius:6px;
background:var(--bg);color:var(--fg);cursor:pointer}
.controls button[data-control="play"]{border-color:var(--accent);color:var(--accent);min-width:64px}
.controls .scrub{display:flex;align-items:center;gap:6px;color:var(--muted);font-size:13px}
.controls input[type=range]{accent-color:var(--accent);min-width:160px}
.controls .speeds{display:flex;gap:4px;align-items:center;color:var(--muted);font-size:13px}
.controls .speed.active{border-color:var(--accent);color:var(--accent);font-weight:600}
.controls .loop{color:var(--muted);font-size:13px}
.scale{display:flex;flex-wrap:wrap;align-items:center;gap:8px;color:var(--muted);font-size:12px;margin:8px 0}
.scale-bar{display:inline-block;width:120px;height:8px;border:1px solid var(--line);border-radius:4px;
background:linear-gradient(90deg,rgba(31,111,235,.12),rgba(31,111,235,.95))}
.scale-end{font-variant-numeric:tabular-nums;color:var(--fg)}
.player main{display:grid;grid-template-columns:repeat(2,minmax(0,1fr));gap:16px}
.player .trend-card{grid-column:1/-1}
.player .kpi dd .delta{margin-left:6px;font-size:11px;font-weight:500;color:var(--muted)}
.player .kpi dd .delta.up{color:var(--warn)}
.player .kpi dd .value{font-variant-numeric:tabular-nums}
.player .details{display:grid;grid-template-columns:1fr 1fr;gap:12px;margin-top:8px}
.player .details h3{font-size:12px;margin:6px 0 4px;color:var(--muted)}
.player .move-list,.player .unmet-list{max-height:150px;overflow:auto;margin:0;padding-left:18px;
font-size:11px;font-variant-numeric:tabular-nums;color:var(--muted)}
.player .move-list li,.player .unmet-list li{margin:1px 0}
svg.cell.unmet,svg .cell.unmet{stroke:var(--warn);stroke-width:1.5}
svg.trend{display:block;width:100%;height:auto;max-height:150px;overflow:visible}
svg.trend .trend-axis{stroke:var(--line);stroke-width:1}
svg.trend .trend-bar{fill:var(--accent);opacity:.55}
svg.trend .trend-bar[data-strategy="optimized"]{fill:var(--arrow);opacity:.5}
svg.trend .trend-bar.is-current{opacity:1}
svg.trend .trend-point{fill:var(--warn);stroke:var(--bg);stroke-width:1}
svg.trend .trend-point.is-current{r:4}
svg.trend .trend-line{fill:none;stroke:var(--warn);stroke-width:1;opacity:.7}
svg.trend .trend-cursor{stroke:var(--fg);stroke-width:1;opacity:.35;stroke-dasharray:3 3}
.series-legend{display:flex;flex-wrap:wrap;gap:16px;color:var(--muted);font-size:12px;margin:6px 0}
.dashboard .num-grid{display:grid;grid-template-columns:repeat(auto-fill,minmax(190px,1fr));gap:8px}
.dashboard .num-card{border:1px solid var(--line);border-radius:8px;padding:8px 10px;
background:var(--bg);display:flex;flex-direction:column;gap:2px;min-width:0}
.dashboard .num-label{font-size:11px;color:var(--muted)}
.dashboard .num-card .metric{font-size:16px;font-variant-numeric:tabular-nums}
.dashboard .num-source{font-size:10px;color:var(--muted);overflow-wrap:anywhere}
.dashboard table.compare{border-collapse:collapse;width:100%;font-size:13px;font-variant-numeric:tabular-nums}
.dashboard table.compare th,.dashboard table.compare td{border:1px solid var(--line);padding:4px 8px;text-align:right}
.dashboard table.compare thead th:first-child,.dashboard table.compare tbody th{text-align:left}
.dashboard .fig-grid{display:grid;grid-template-columns:repeat(auto-fill,minmax(260px,1fr));gap:12px}
.dashboard figure.fig{margin:0;border:1px solid var(--line);border-radius:8px;padding:8px;background:var(--bg)}
.dashboard figure.fig img{display:block;width:100%;height:auto;border-radius:4px}
.dashboard figcaption{font-size:11px;color:var(--muted);margin-top:6px}
.dashboard .fig-source{color:var(--muted)}
@media (max-width:760px){
.player main{grid-template-columns:1fr}
.player .details{grid-template-columns:1fr}
.controls input[type=range]{min-width:120px}
}
@media (prefers-reduced-motion: reduce){
.player *, .dashboard *{transition:none !important; animation:none !important; scroll-behavior:auto !important}
}
"""

PLAYER_JS = """(function(){
  var D = window.REPLAY_DATA;
  if(!D || !D.frames || !D.frames.length) return;
  var STRATS = ['rule','optimized'];
  var n = D.n_frames;
  var NS = 'http://www.w3.org/2000/svg';
  var index = 0, playing = false, speed = 1, loop = false, timer = null;
  var reduced = false;
  try {
    reduced = !!(window.matchMedia && window.matchMedia('(prefers-reduced-motion: reduce)').matches);
  } catch(e) { reduced = false; }

  var playBtn = document.querySelector('[data-control="play"]');
  var scrub = document.querySelector('[data-control="scrub"]');
  var loopBox = document.querySelector('[data-control="loop"]');
  var frameLabel = document.getElementById('frame-label');
  var bucketLabel = document.getElementById('bucket-label');
  var trend = document.querySelector('svg.trend');
  var step = (D.cell_px || 12) + (D.cell_gap || 1);
  var colorMax = (D.color_domain && D.color_domain.max) || 1;
  // The pause comes from the page Python wrote, so the documented default and
  // the running animation cannot disagree.
  var controls = document.querySelector('.controls');
  var baseMs = parseInt(controls ? controls.getAttribute('data-step-ms') : '', 10) || 900;

  function fmtInt(v){ return Number(v).toLocaleString('en-US'); }
  function fmtFloat(v){ return Number(v).toLocaleString('en-US',{minimumFractionDigits:2,maximumFractionDigits:2}); }
  function sectionFor(strategy){
    return document.querySelector('section.strategy[data-strategy="' + strategy + '"]');
  }
  function opacityFor(value){ return (0.12 + 0.88 * (Number(value) / (colorMax || 1))).toFixed(3); }
  function centreOf(cellId){
    var i = D.cell_ids.indexOf(cellId);
    if(i < 0) return null;
    var ix = D.geom[i][0], iy = D.geom[i][1];
    return [ix * step + (D.cell_px || 12) / 2, iy * step + (D.cell_px || 12) / 2];
  }

  function updateGrid(section, strategy, strat){
    var rects = section.querySelectorAll('rect.cell');
    for(var k = 0; k < rects.length && k < D.cell_ids.length; k++){
      var inv = strat.inventory_end[k], unmet = strat.unmet[k], moved = strat.moved[k];
      var rect = rects[k];
      rect.setAttribute('data-inventory-end', inv);
      rect.setAttribute('data-unmet', unmet);
      rect.setAttribute('data-moved', moved);
      rect.setAttribute('fill-opacity', opacityFor(inv));
      rect.setAttribute('class', unmet > 0 ? 'cell unmet' : 'cell');
      var title = rect.querySelector('title');
      if(title) title.textContent = D.cell_ids[k] + ' 期末 ' + inv + ' 未满足 ' + unmet + ' 搬出 ' + moved;
    }
  }

  function updateArrows(section, strategy, moves){
    var group = section.querySelector('g.arrows');
    while(group.firstChild) group.removeChild(group.firstChild);
    for(var i = 0; i < moves.length; i++){
      var move = moves[i];
      var a = centreOf(move.from_cell), b = centreOf(move.to_cell);
      if(!a || !b) continue;
      var x1 = a[0], y1 = a[1], x2 = b[0], y2 = b[1];
      var span = Math.sqrt((x2-x1)*(x2-x1) + (y2-y1)*(y2-y1));
      if(span > 0){
        var shrink = Math.min((D.cell_px || 12) * 0.45, span / 3);
        x2 -= (x2-x1)/span * shrink;
        y2 -= (y2-y1)/span * shrink;
      }
      var line = document.createElementNS(NS, 'line');
      line.setAttribute('class', 'arrow');
      line.setAttribute('x1', x1.toFixed(2));
      line.setAttribute('y1', y1.toFixed(2));
      line.setAttribute('x2', x2.toFixed(2));
      line.setAttribute('y2', y2.toFixed(2));
      line.setAttribute('marker-end', 'url(#arrow-' + strategy + ')');
      line.setAttribute('data-source', 'outputs/sim_moves.' + strategy + '.' + D.source + '.json');
      line.setAttribute('data-from', move.from_cell);
      line.setAttribute('data-to', move.to_cell);
      line.setAttribute('data-bikes', move.bikes);
      line.setAttribute('stroke-width', (0.6 + Math.min(2.4, move.bikes / 30)).toFixed(2));
      var title = document.createElementNS(NS, 'title');
      title.textContent = move.from_cell + ' → ' + move.to_cell + ': ' + move.bikes + ' 辆（' +
        Math.round(move.distance_m) + ' m）';
      line.appendChild(title);
      group.appendChild(line);
    }
  }

  function updateKpis(section, strategy, frame){
    var kpi = frame.strategies[strategy].kpi;
    var moves = frame.moves[strategy] || [];
    var movedBikes = 0;
    for(var i = 0; i < moves.length; i++) movedBikes += moves[i].bikes;
    var values = {
      borrow_requests: kpi.borrow_requests, borrow_met: kpi.borrow_met,
      unmet_requests: kpi.unmet_requests, returns: kpi.returns,
      returns_applied: kpi.returns_applied, returns_overflow: kpi.returns_overflow,
      bikes_moved: kpi.bikes_moved, distance_bike_km: kpi.distance_bike_km,
      inventory_start: kpi.inventory_start, inventory_end: kpi.inventory_end,
      n_moves: moves.length, moves_bikes: movedBikes, n_cells: D.cell_ids.length
    };
    var previous = index > 0 ? D.frames[index-1].strategies[strategy].kpi : null;
    for(var name in values){
      if(!values.hasOwnProperty(name)) continue;
      var dd = section.querySelector('dd[data-metric="' + name + '"]');
      if(!dd) continue;
      var value = values[name];
      dd.setAttribute('data-value', String(value));
      // The element carries its own format, so the JS does not need to know
      // which metric is measured in bike-km.
      var isFloat = dd.getAttribute('data-format') === 'float';
      var span = dd.querySelector('.value');
      if(span) span.textContent = isFloat ? fmtFloat(value) : fmtInt(value);
      var delta = dd.querySelector('.delta');
      if(delta){
        if(!previous || previous[name] === undefined){
          delta.textContent = '—';
          delta.setAttribute('data-prev', '');
          delta.setAttribute('class', 'delta');
        } else {
          var diff = value - previous[name];
          var text = (diff > 0 ? '+' : '') + (isFloat ? diff.toFixed(2) : fmtInt(diff));
          delta.textContent = text;
          delta.setAttribute('data-prev', String(previous[name]));
          delta.setAttribute('class', 'delta' + (diff > 0 ? ' up' : ''));
        }
      }
    }
  }

  function updateDetail(section, strategy, frame){
    var moves = frame.moves[strategy] || [];
    var moveList = section.querySelector('.move-list');
    var heading = section.querySelector('.details h3');
    if(heading) heading.textContent = '本帧搬运明细（' + moves.length + ' 条）';
    while(moveList.firstChild) moveList.removeChild(moveList.firstChild);
    if(!moves.length){
      var empty = document.createElement('li');
      empty.className = 'empty';
      empty.textContent = '本帧没有搬运记录。';
      moveList.appendChild(empty);
    } else {
      for(var i = 0; i < moves.length; i++){
        var move = moves[i];
        var li = document.createElement('li');
        li.className = 'move-row';
        li.setAttribute('data-from', move.from_cell);
        li.setAttribute('data-to', move.to_cell);
        li.setAttribute('data-bikes', move.bikes);
        li.setAttribute('data-source', 'outputs/sim_moves.' + strategy + '.' + D.source + '.json');
        li.textContent = move.from_cell + ' → ' + move.to_cell + '：' + move.bikes + ' 辆';
        moveList.appendChild(li);
      }
    }
    var unmetList = section.querySelector('.unmet-list');
    while(unmetList.firstChild) unmetList.removeChild(unmetList.firstChild);
    var any = false;
    for(var k = 0; k < frame.strategies[strategy].unmet.length; k++){
      var unmet = frame.strategies[strategy].unmet[k];
      if(unmet <= 0) continue;
      any = true;
      var item = document.createElement('li');
      item.className = 'unmet-row';
      item.setAttribute('data-cell', D.cell_ids[k]);
      item.setAttribute('data-unmet', unmet);
      item.setAttribute('data-source', 'outputs/replay.' + strategy + '.' + D.source + '.parquet');
      item.textContent = D.cell_ids[k] + '：' + unmet;
      unmetList.appendChild(item);
    }
    if(!any){
      var none = document.createElement('li');
      none.className = 'empty';
      none.textContent = '本帧没有未满足请求。';
      unmetList.appendChild(none);
    }
  }

  function updateTrend(){
    var bars = document.querySelectorAll('svg.trend [data-seek]');
    for(var i = 0; i < bars.length; i++){
      var current = parseInt(bars[i].getAttribute('data-frame-index'), 10) === index;
      bars[i].classList.toggle('is-current', current);
    }
    var cursor = document.querySelector('svg.trend .trend-cursor');
    if(cursor){
      cursor.setAttribute('data-frame-index', String(index));
      var points = document.querySelectorAll('svg.trend .trend-point');
      var x = points[index] ? points[index].getAttribute('cx') : '0';
      cursor.setAttribute('x1', x);
      cursor.setAttribute('x2', x);
    }
  }

  function render(){
    var frame = D.frames[index];
    if(frameLabel){
      frameLabel.textContent = String(index + 1);
      frameLabel.setAttribute('data-value', String(index + 1));
    }
    if(bucketLabel){
      bucketLabel.textContent = frame.time_bucket;
      bucketLabel.setAttribute('data-value', frame.time_bucket);
    }
    if(scrub) scrub.value = String(index);
    for(var s = 0; s < STRATS.length; s++){
      var strategy = STRATS[s];
      var section = sectionFor(strategy);
      if(!section) continue;
      updateGrid(section, strategy, frame.strategies[strategy]);
      updateArrows(section, strategy, frame.moves[strategy] || []);
      updateKpis(section, strategy, frame);
      updateDetail(section, strategy, frame);
    }
    updateTrend();
  }

  function goto(i){
    if(n <= 0) return;
    if(i < 0) i = 0;
    if(i > n - 1) i = n - 1;
    index = i;
    render();
  }

  function schedule(){
    if(timer) clearTimeout(timer);
    if(!playing) return;
    var pause = baseMs / speed;
    timer = setTimeout(function(){
      if(index >= n - 1){
        if(loop){ goto(0); schedule(); }
        else { playing = false; reflectPlayState(); }
      } else {
        goto(index + 1);
        schedule();
      }
    }, pause);
  }

  function reflectPlayState(){
    if(!playBtn) return;
    playBtn.textContent = playing ? '暂停' : '播放';
    playBtn.setAttribute('data-state', playing ? 'playing' : 'paused');
  }

  function setPlaying(value){
    playing = !!value;
    reflectPlayState();
    if(playing) schedule(); else if(timer) clearTimeout(timer);
  }

  if(playBtn) playBtn.addEventListener('click', function(){ setPlaying(!playing); });
  var prevBtn = document.querySelector('[data-control="prev"]');
  if(prevBtn) prevBtn.addEventListener('click', function(){ setPlaying(false); goto(index - 1); });
  var nextBtn = document.querySelector('[data-control="next"]');
  if(nextBtn) nextBtn.addEventListener('click', function(){ setPlaying(false); goto(index + 1); });
  if(scrub) scrub.addEventListener('input', function(){ setPlaying(false); goto(parseInt(scrub.value, 10)); });
  if(loopBox) loopBox.addEventListener('change', function(){ loop = !!loopBox.checked; schedule(); });

  var speedButtons = document.querySelectorAll('[data-control="speed"]');
  for(var b = 0; b < speedButtons.length; b++){
    speedButtons[b].addEventListener('click', function(){
      speed = parseFloat(this.getAttribute('data-speed')) || 1;
      for(var k = 0; k < speedButtons.length; k++){
        speedButtons[k].classList.toggle('active', speedButtons[k] === this);
      }
      if(playing) schedule();
    });
  }

  if(trend){
    trend.addEventListener('click', function(event){
      var target = event.target;
      while(target && target !== trend && !target.getAttribute('data-seek')) target = target.parentNode;
      if(target && target !== trend && target.getAttribute('data-seek') !== null){
        setPlaying(false);
        goto(parseInt(target.getAttribute('data-seek'), 10));
      }
    });
  }

  document.addEventListener('keydown', function(event){
    var tag = (event.target && event.target.tagName || '').toLowerCase();
    if(tag === 'input' || tag === 'select' || tag === 'textarea') return;
    if(event.key === ' ' || event.key === 'Spacebar'){
      // A focused button already activates on Space (native click); acting on
      // it here as well would toggle twice and look like the key did nothing.
      if(tag === 'button') return;
      event.preventDefault();
      setPlaying(!playing);
    } else if(event.key === 'ArrowLeft'){
      event.preventDefault(); setPlaying(false); goto(index - 1);
    } else if(event.key === 'ArrowRight'){
      event.preventDefault(); setPlaying(false); goto(index + 1);
    } else if(event.key === 'Home'){
      event.preventDefault(); setPlaying(false); goto(0);
    } else if(event.key === 'End'){
      event.preventDefault(); setPlaying(false); goto(n - 1);
    }
  });

  render();
  // NF-20: honour reduced motion by never starting on its own.
  if(!reduced) setPlaying(true);
})();
"""
