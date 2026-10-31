"""F10: render the dispatch replay as a static, offline, per-frame site.

Design constraints that shaped this module:

* **No synthesis.** One page per persisted time bucket, one arrow per record in
  ``sim_moves.*.json``.  Nothing is interpolated, duplicated or filtered.
* **Offline.** Styles and scripts ship with the site as local files and each
  page carries its own frame data, so ``file://`` works with zero network
  requests (F10-9).
* **On demand.** Each page carries only its own frame, so the site is a set of
  small documents rather than one large bundle (NF-13).
* **Traceable.** Every element that shows a number carries ``data-source`` with
  the repo-relative path of the artefact it came from (F10-5).

The grid is drawn by a few lines of local JavaScript from the frame's embedded
JSON; the arrows and every metric are rendered here in Python, so the delivered
DOM can be checked without a browser.
"""

from __future__ import annotations

import html
import json
from typing import Any

import pandas as pd

from bike_agent.config import Layout, source_config
from bike_agent.utils import read_json

STRATEGIES = ("rule", "optimized")
STRATEGY_LABELS = {"rule": "S-RULE（规则调度）", "optimized": "S-OPT（约束优化）"}
CELL_PX = 12
CELL_GAP = 1
SPARSE_SERVICE_RATE = 0.99
DENSE_SERVICE_RATE = 0.95
SINGLE_DIGIT_MOVES = 9


# -- helpers ---------------------------------------------------------------
def _fmt_int(value: Any) -> str:
    return f"{int(value):,}"


def _fmt_float(value: Any, digits: int = 2) -> str:
    return f"{float(value):,.{digits}f}"


def _bucket_str(value: Any) -> str:
    return pd.Timestamp(value).strftime("%Y-%m-%d %H:%M:%S")


def _geometry(cells: pd.DataFrame) -> tuple[dict[str, tuple[int, int]], int, int]:
    coords = {str(row.cell_id): (int(row.ix), int(row.iy)) for row in cells.itertuples(index=False)}
    max_ix = max(ix for ix, _ in coords.values()) if coords else 0
    max_iy = max(iy for _, iy in coords.values()) if coords else 0
    return coords, max_ix, max_iy


def _center(coords: dict[str, tuple[int, int]], cell_id: str) -> tuple[float, float]:
    ix, iy = coords[cell_id]
    step = CELL_PX + CELL_GAP
    return ix * step + CELL_PX / 2.0, iy * step + CELL_PX / 2.0


def _sparse_note(
    service_rate: float,
    source: str,
    strategy: str,
    move_counts: list[int],
) -> str:
    """F10-6: the wording is derived from the artefact, never hand-written.

    Above 99% the page must say the replay is sparse and quote the real rate;
    below 95% it must not.  In between, no claim is made either way.  The
    "how sparse" clause is computed from the per-bucket move counts rather than
    asserted, so it cannot drift from the data.
    """
    path = f"outputs/sim_{strategy}.{source}.json"
    rate = f"{service_rate * 100:.2f}%"
    if service_rate > SPARSE_SERVICE_RATE:
        sparse_share = (
            sum(1 for count in move_counts if count <= SINGLE_DIGIT_MOVES) / len(move_counts)
            if move_counts
            else 0.0
        )
        return (
            f'<p class="note sparse" data-source="{path}" data-service-rate="{service_rate!r}" '
            f'data-sparse-share="{sparse_share!r}">'
            f"<strong>搬运稀疏：</strong>本次回放服务率 {rate}；"
            f"{len(move_counts)} 个时间单元中有 {sparse_share * 100:.1f}% 的搬运量不超过 "
            f"{SINGLE_DIGIT_MOVES} 辆，画面接近静止。"
            f"这是数据本身的样子，不是渲染故障——服务率高意味着几乎没有未被满足的"
            f"借车请求，调度器自然无车可搬。未做任何插值、补帧或放大处理。</p>"
        )
    if service_rate < DENSE_SERVICE_RATE:
        return (
            f'<p class="note dense" data-source="{path}" data-service-rate="{service_rate!r}">'
            f"本次回放服务率 {rate}，低于 95%：画面中会出现明显的未满足请求。"
            f"请结合帧数一并理解——该源的仿真窗口本身就很短。</p>"
        )
    return ""


def _kpi(label: str, metric: str, value: Any, source_file: str, display: str) -> str:
    return (
        f'<div class="kpi"><dt>{html.escape(label)}</dt>'
        f'<dd class="metric" data-source="{html.escape(source_file)}" '
        f'data-metric="{html.escape(metric)}" data-value="{html.escape(str(value))}">'
        f"{html.escape(display)}</dd></div>"
    )


# -- per-frame rendering ---------------------------------------------------
def _render_frame(
    *,
    source: str,
    index: int,
    n_frames: int,
    bucket: str,
    steps: dict[str, pd.Series],
    summaries: dict[str, dict[str, Any]],
    cell_states: dict[str, pd.DataFrame],
    moves: dict[str, list[dict[str, Any]]],
    move_counts: dict[str, list[int]],
    coords: dict[str, tuple[int, int]],
    max_ix: int,
    max_iy: int,
    cell_count: int,
) -> str:
    step = CELL_PX + CELL_GAP
    width = (max_ix + 1) * step
    height = (max_iy + 1) * step
    src_attr = html.escape(source)

    # Geometry is shared by both strategies and stored once per page; the two
    # strategies then carry only the numbers that actually differ.
    cell_ids = sorted(coords)
    payload: dict[str, Any] = {
        "source": source,
        "time_bucket": bucket,
        "index": index,
        "n_frames": n_frames,
        "cell_px": CELL_PX,
        "cell_gap": CELL_GAP,
        "ids": cell_ids,
        "geom": [list(coords[cell_id]) for cell_id in cell_ids],
        "strategies": {},
    }

    sections: list[str] = []
    for strategy in STRATEGIES:
        row = steps[strategy]
        summary = summaries[strategy]
        state = cell_states[strategy].set_index("cell_id")
        frame_moves = moves[strategy]

        values: list[list[int]] = []
        max_inventory = 0
        for cell_id in cell_ids:
            record = state.loc[cell_id]
            inventory_end = int(record["inventory_end"])
            values.append([inventory_end, int(record["unmet_requests"]), int(record["bikes_moved"])])
            max_inventory = max(max_inventory, inventory_end)

        payload["strategies"][strategy] = {
            "values": values,
            "max_inventory": max_inventory,
            "n_moves": len(frame_moves),
        }

        csv_path = f"outputs/sim_{strategy}.{source}.csv"
        moves_path = f"outputs/sim_moves.{strategy}.{source}.json"
        replay_path = f"outputs/replay.{strategy}.{source}.parquet"
        summary_path = f"outputs/sim_{strategy}.{source}.json"
        moved_total = sum(int(m["bikes"]) for m in frame_moves)

        kpis = "".join(
            [
                _kpi("借车请求", "borrow_requests", int(row["borrow_requests"]), csv_path, _fmt_int(row["borrow_requests"])),
                _kpi("实际满足", "borrow_met", int(row["borrow_met"]), csv_path, _fmt_int(row["borrow_met"])),
                _kpi("未满足请求", "unmet_requests", int(row["unmet_requests"]), csv_path, _fmt_int(row["unmet_requests"])),
                _kpi("归还", "returns", int(row["returns"]), csv_path, _fmt_int(row["returns"])),
                _kpi("入库归还", "returns_applied", int(row["returns_applied"]), csv_path, _fmt_int(row["returns_applied"])),
                _kpi("容量溢出", "returns_overflow", int(row["returns_overflow"]), csv_path, _fmt_int(row["returns_overflow"])),
                _kpi("搬运车辆数", "bikes_moved", int(row["bikes_moved"]), csv_path, _fmt_int(row["bikes_moved"])),
                _kpi(
                    "搬运距离（bike-km）",
                    "distance_bike_km",
                    float(row["distance_bike_km"]),
                    csv_path,
                    _fmt_float(row["distance_bike_km"]),
                ),
                _kpi("期初库存", "inventory_start", int(row["inventory_start"]), csv_path, _fmt_int(row["inventory_start"])),
                _kpi("期末库存", "inventory_end", int(row["inventory_end"]), csv_path, _fmt_int(row["inventory_end"])),
                _kpi("本帧搬运记录数", "n_moves", len(frame_moves), moves_path, _fmt_int(len(frame_moves))),
                _kpi("本帧搬运车辆合计", "moves_bikes", moved_total, moves_path, _fmt_int(moved_total)),
                _kpi("本帧格子数", "n_cells", cell_count, replay_path, _fmt_int(cell_count)),
            ]
        )

        arrows = []
        for move in frame_moves:
            x1, y1 = _center(coords, str(move["from_cell"]))
            x2, y2 = _center(coords, str(move["to_cell"]))
            # Shorten the line so the arrowhead lands inside the target cell
            # instead of over its centre.
            span = ((x2 - x1) ** 2 + (y2 - y1) ** 2) ** 0.5
            if span > 0:
                shrink = min(CELL_PX * 0.45, span / 3.0)
                x2 -= (x2 - x1) / span * shrink
                y2 -= (y2 - y1) / span * shrink
            arrows.append(
                f'<line class="arrow" x1="{x1:.2f}" y1="{y1:.2f}" x2="{x2:.2f}" y2="{y2:.2f}" '
                f'marker-end="url(#arrow-{strategy})" '
                f'data-source="{moves_path}" data-from="{html.escape(str(move["from_cell"]))}" '
                f'data-to="{html.escape(str(move["to_cell"]))}" data-bikes="{int(move["bikes"])}" '
                f'stroke-width="{0.6 + min(2.4, int(move["bikes"]) / 30.0):.2f}">'
                f'<title>{html.escape(str(move["from_cell"]))} → {html.escape(str(move["to_cell"]))}: '
                f'{int(move["bikes"])} 辆（{float(move["distance_m"]):.0f} m）</title></line>'
            )

        marker = (
            f'<defs><marker id="arrow-{strategy}" viewBox="0 0 10 10" refX="9" refY="5" '
            f'markerUnits="userSpaceOnUse" markerWidth="6" markerHeight="6" orient="auto-start-reverse">'
            f'<path d="M0,0 L10,5 L0,10 z" fill="var(--arrow)"/></marker></defs>'
        )

        sections.append(
            f'<section class="strategy" data-strategy="{strategy}">'
            f"<h2>{html.escape(STRATEGY_LABELS[strategy])}</h2>"
            f'<p class="rate">服务率 <b class="metric" data-source="{summary_path}" data-metric="service_rate" '
            f'data-value="{float(summary["service_rate"])!r}">{float(summary["service_rate"]) * 100:.2f}%</b>'
            f"（全时段模拟值）</p>"
            f'<dl class="kpis">{kpis}</dl>'
            f'<div class="canvas">'
            f'<svg class="grid" data-strategy="{strategy}" data-source="{replay_path}" '
            f'viewBox="0 0 {width} {height}" role="img" '
            f'aria-label="{html.escape(STRATEGY_LABELS[strategy])} 逐格库存">'
            f'<g class="cells"></g><g class="arrows">{marker}{"".join(arrows)}</g></svg>'
            f'<p class="legend">格子深浅 = 期末库存（同帧内相对最大值）；红框 = 该格有未满足请求；'
            f"箭头 = <code>{moves_path}</code> 中的一条搬运记录。本帧搬运记录 {len(frame_moves)} 条。</p>"
            f"</div>"
            f"{_sparse_note(float(summary['service_rate']), source, strategy, move_counts[strategy])}"
            f"</section>"
        )

    prev_link = (
        f'<a rel="prev" href="frame-{index - 1:04d}.html">← 上一帧</a>'
        if index > 0
        else '<span class="disabled">← 上一帧</span>'
    )
    next_link = (
        f'<a rel="next" href="frame-{index + 1:04d}.html">下一帧 →</a>'
        if index < n_frames - 1
        else '<span class="disabled">下一帧 →</span>'
    )
    payload_json = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).replace("<", "\\u003c")

    return f"""<!doctype html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>回放 · {html.escape(source.upper())} · {html.escape(bucket)} · bike-agent</title>
<link rel="stylesheet" href="../style.css">
</head>
<body>
<header>
<h1>调度回放 · {html.escape(source.upper())}</h1>
<p class="meta">第 <b class="metric" data-source="outputs/sim_rule.{src_attr}.csv" data-derived="row-position" data-metric="frame_index" data-value="{index + 1}">{index + 1}</b>
/ <b class="metric" data-source="outputs/sim_rule.{src_attr}.csv" data-derived="row-count" data-metric="frame_count" data-value="{n_frames}">{n_frames}</b> 帧
· 时间单元 <b class="metric" data-source="outputs/sim_rule.{src_attr}.csv" data-metric="time_bucket" data-value="{html.escape(bucket)}">{html.escape(bucket)}</b>
· <a href="../index.html">全部数据源</a></p>
<nav class="pager">{prev_link}<a href="../index.html#timeline-{src_attr}">时间轴</a>{next_link}</nav>
<p class="notice">所有数字均为<strong>模拟值</strong>，来自 <code>outputs/</code> 下的静态产物；
回放是只读重演，不驱动任何调度决策。没有搬运记录的时间单元不会出现箭头。</p>
</header>
<main>
{''.join(sections)}
</main>
<script type="application/json" id="frame-data">{payload_json}</script>
<script src="../app.js"></script>
</body>
</html>
"""


# -- site -------------------------------------------------------------------
def build_site(config: dict[str, Any], layout: Layout, sources: list[str] | None = None) -> dict[str, Any]:
    """Write ``<root>/replay/`` from the persisted artefacts; returns a summary."""
    # Imported here, not at module scope: player.py reuses this module's
    # formatting helpers, and a top-level mutual import would be circular.
    from bike_agent.replay.player import PLAYER_CSS, PLAYER_JS, build_dashboard, write_player

    sources = sources or list(config.get("sources", {}).keys())
    site_dir = layout.root / "replay"
    site_dir.mkdir(parents=True, exist_ok=True)
    (site_dir / "style.css").write_text(_CSS, encoding="utf-8")
    (site_dir / "app.js").write_text(_JS, encoding="utf-8")
    (site_dir / "player.css").write_text(PLAYER_CSS, encoding="utf-8")
    (site_dir / "player.js").write_text(PLAYER_JS, encoding="utf-8")

    built: dict[str, Any] = {}
    for source in sources:
        cell_size = int(source_config(config, source)["cell_size_m"])
        cells = pd.read_parquet(layout.cells_path(source, cell_size))
        coords, max_ix, max_iy = _geometry(cells)

        steps: dict[str, pd.DataFrame] = {}
        summaries: dict[str, dict[str, Any]] = {}
        states: dict[str, pd.DataFrame] = {}
        moves: dict[str, dict[pd.Timestamp, list[dict[str, Any]]]] = {}
        for strategy in STRATEGIES:
            path = layout.sim_path(source, strategy, "csv")
            if not path.exists():
                raise FileNotFoundError(f"missing {path}; run `bike-agent repro` first")
            frame = pd.read_csv(path)
            frame["time_bucket"] = pd.to_datetime(frame["time_bucket"])
            steps[strategy] = frame.sort_values("time_bucket").reset_index(drop=True)
            summaries[strategy] = read_json(layout.sim_path(source, strategy, "json"))
            state = pd.read_parquet(layout.replay_path(source, strategy))
            state["time_bucket"] = pd.to_datetime(state["time_bucket"])
            states[strategy] = state
            grouped: dict[pd.Timestamp, list[dict[str, Any]]] = {}
            for record in read_json(layout.outputs / f"sim_moves.{strategy}.{source}.json"):
                grouped.setdefault(pd.Timestamp(record["time_bucket"]), []).append(record)
            moves[strategy] = grouped

        buckets = list(steps["rule"]["time_bucket"])
        if list(steps["optimized"]["time_bucket"]) != buckets:
            raise ValueError(f"{source}: the two strategies replay different buckets; refusing to render")

        source_dir = site_dir / source
        source_dir.mkdir(parents=True, exist_ok=True)
        for stale in source_dir.glob("frame-*.html"):
            stale.unlink()

        # Per-bucket move counts for the whole replay, so each page can state how
        # sparse its source is without recomputing it.
        move_counts = {
            strategy: [len(moves[strategy].get(b, [])) for b in buckets] for strategy in STRATEGIES
        }

        for index, bucket in enumerate(buckets):
            page = _render_frame(
                source=source,
                index=index,
                n_frames=len(buckets),
                bucket=_bucket_str(bucket),
                steps={strategy: steps[strategy].iloc[index] for strategy in STRATEGIES},
                summaries=summaries,
                cell_states={
                    strategy: states[strategy].loc[states[strategy]["time_bucket"] == bucket]
                    for strategy in STRATEGIES
                },
                moves={strategy: moves[strategy].get(bucket, []) for strategy in STRATEGIES},
                move_counts=move_counts,
                coords=coords,
                max_ix=max_ix,
                max_iy=max_iy,
                cell_count=len(coords),
            )
            (source_dir / f"frame-{index:04d}.html").write_text(page, encoding="utf-8")

        built[source] = {
            "n_frames": len(buckets),
            "n_cells": len(coords),
            "first_bucket": _bucket_str(buckets[0]),
            "last_bucket": _bucket_str(buckets[-1]),
            "n_moves": {strategy: sum(len(v) for v in moves[strategy].values()) for strategy in STRATEGIES},
            "service_rate": {strategy: float(summaries[strategy]["service_rate"]) for strategy in STRATEGIES},
            "player": write_player(config, layout, source),
        }

    build_dashboard(config, layout)
    (site_dir / "index.html").write_text(_render_index(built), encoding="utf-8")
    return {"site": str(site_dir), "sources": built}


def _render_index(built: dict[str, Any]) -> str:
    cards, timeline = [], []
    for source, info in built.items():
        src_attr = html.escape(source)
        rate_lines = "".join(
            f"<li>{html.escape(STRATEGY_LABELS[strategy])}：服务率 "
            f'<b class="metric" data-source="outputs/sim_{strategy}.{source}.json" data-metric="service_rate" '
            f'data-value="{info["service_rate"][strategy]!r}">{info["service_rate"][strategy] * 100:.2f}%</b>，'
            f"搬运记录 {info['n_moves'][strategy]} 条</li>"
            for strategy in STRATEGIES
        )
        cards.append(
            f'<section class="card"><h2>{html.escape(source.upper())}</h2>'
            f"<p>网格 <b>{info['n_cells']}</b> 格 · "
            f"帧数 <b class=\"metric\" data-source=\"outputs/sim_rule.{source}.csv\" data-metric=\"n_frames\" "
            f'data-value="{info["n_frames"]}">{info["n_frames"]}</b> · '
            f'<span>{html.escape(info["first_bucket"])} → {html.escape(info["last_bucket"])}</span></p>'
            f"<ul>{rate_lines}</ul>"
            f'<p><a class="start" href="{src_attr}/player.html">播放 →</a> '
            f'<a href="{src_attr}/frame-0000.html">逐帧查看 →</a></p></section>'
        )
        links = "".join(
            f'<a href="{src_attr}/frame-{index:04d}.html">{index + 1}</a>' for index in range(info["n_frames"])
        )
        timeline.append(
            f'<div class="timeline" id="timeline-{src_attr}"><h3>{html.escape(source.upper())}</h3>'
            f'<p class="hint">共 {info["n_frames"]} 帧，与产物时间单元数一致（无补帧）。</p>'
            f'<div class="frames">{links}</div></div>'
        )
    return f"""<!doctype html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>调度回放 · bike-agent</title>
<link rel="stylesheet" href="style.css">
</head>
<body>
<header>
<h1>调度回放</h1>
<p class="notice">逐帧重演 Phase 1 的仿真产物：S-RULE 与 S-OPT 并排，同一时间单元下各自展示。
所有数字均为<strong>模拟值</strong>；帧数等于产物时间单元数，没有插值、补帧或合成数据。</p>
<p class="notice">两个数据源的短板不同，如实说明：{_tension_text(built)}</p>
<p class="notice">播放器会自动逐帧播放，可暂停、拖动与跳帧；全部图表与关键数字见
<a href="dashboard.html">总览页</a>。逐帧页仍然保留，供深链与无脚本回退使用。</p>
</header>
<main>
{''.join(cards)}
<section class="card wide"><h2>时间轴</h2>{''.join(timeline)}</section>
</main>
</body>
</html>
"""


def _tension_text(built: dict[str, Any]) -> str:
    """The §2.3 tension, computed from the artefacts rather than asserted."""
    parts = []
    for source, info in built.items():
        rate = info["service_rate"]["rule"]
        label = html.escape(source.upper())
        if info["n_frames"] <= 10:
            parts.append(
                f"<strong>{label} 有戏没帧</strong>——服务率 {rate * 100:.2f}%，"
                f"画面有事发生，但仿真只覆盖 {info['n_frames']} 个时间单元，时间轴几乎是空的"
            )
        elif rate > SPARSE_SERVICE_RATE:
            parts.append(
                f"<strong>{label} 有帧没戏</strong>——{info['n_frames']} 个时间单元拖得动，"
                f"但服务率 {rate * 100:.2f}%，绝大多数帧里搬运量只有个位数，画面接近静止"
            )
        else:
            parts.append(f"<strong>{label}</strong>——{info['n_frames']} 帧，服务率 {rate * 100:.2f}%")
    return "；".join(parts) + "。"


_CSS = """:root{--bg:#ffffff;--panel:#f5f6f8;--fg:#14181d;--muted:#5b6672;--line:#d7dce2;
--accent:#1f6feb;--warn:#c0392b;--cell:#1f6feb;--arrow:#b3341f}
@media (prefers-color-scheme: dark){
:root{--bg:#0f1318;--panel:#171d24;--fg:#e8edf3;--muted:#9aa7b4;--line:#2b343d;
--accent:#6aa8ff;--warn:#ff7b6b;--cell:#6aa8ff;--arrow:#ffb08a}
}
*{box-sizing:border-box}
a{color:var(--accent)}
body{margin:0;padding:16px;background:var(--bg);color:var(--fg);
font:14px/1.6 -apple-system,BlinkMacSystemFont,"Segoe UI","Noto Sans SC",sans-serif}
header{margin-bottom:16px}
h1{font-size:20px;margin:0 0 6px}
h2{font-size:15px;margin:0 0 8px}
h3{font-size:14px;margin:0 0 4px}
.meta,.notice,.hint,.rate{color:var(--muted);font-size:13px;margin:4px 0}
.pager{display:flex;gap:12px;flex-wrap:wrap;margin:8px 0}
.pager a,.pager span{padding:4px 10px;border:1px solid var(--line);border-radius:6px;text-decoration:none}
.pager a{color:var(--accent)}
.pager .disabled{color:var(--muted);opacity:.6}
main{display:grid;grid-template-columns:repeat(2,minmax(0,1fr));gap:16px}
.strategy,.card{background:var(--panel);border:1px solid var(--line);border-radius:10px;padding:12px;min-width:0}
.card.wide{grid-column:1/-1}
.kpis{display:grid;grid-template-columns:repeat(auto-fill,minmax(120px,1fr));gap:6px 12px;margin:8px 0}
.kpi{margin:0}
.kpi dt{font-size:11px;color:var(--muted)}
.kpi dd{margin:0;font-variant-numeric:tabular-nums;font-weight:600}
.canvas{overflow:auto;max-width:100%;margin-top:8px}
svg.grid{display:block;width:100%;height:auto;max-width:640px}
svg.grid rect{fill:var(--cell);stroke:none}
svg.grid rect.unmet{stroke:var(--warn);stroke-width:1.5}
svg.grid line.arrow{stroke:var(--arrow);fill:none;opacity:.9;stroke-linecap:round}
.legend{font-size:11px;color:var(--muted);margin:6px 0 0}
.note{font-size:12px;border-left:3px solid var(--line);padding:6px 10px;margin:8px 0 0;color:var(--muted)}
.note.sparse{border-color:var(--accent)}
.note.dense{border-color:var(--warn)}
.frames{display:flex;flex-wrap:wrap;gap:4px;margin-top:6px}
.frames a{font-size:11px;padding:2px 6px;border:1px solid var(--line);border-radius:4px;
text-decoration:none;color:var(--accent)}
a.start{display:inline-block;margin-top:8px;padding:6px 12px;border:1px solid var(--accent);
border-radius:6px;text-decoration:none;color:var(--accent)}
code{font-size:11px}
@media (max-width:760px){
main{grid-template-columns:1fr}
body{padding:12px}
svg.grid{max-width:100%}
}
"""

_JS = """(function(){
  var node = document.getElementById('frame-data');
  if(!node) return;
  var data = JSON.parse(node.textContent);
  var NS = 'http://www.w3.org/2000/svg';
  // Pitch comes from the payload so the rects stay aligned with the arrows,
  // which Python positions using the same CELL_PX + CELL_GAP.
  var step = data.cell_px + data.cell_gap;
  document.querySelectorAll('svg.grid').forEach(function(svg){
    var payload = data.strategies[svg.getAttribute('data-strategy')];
    if(!payload) return;
    var layer = svg.querySelector('g.cells');
    var maxInventory = Math.max(1, payload.max_inventory);
    for(var i = 0; i < data.ids.length; i++){
      var cellId = data.ids[i], ix = data.geom[i][0], iy = data.geom[i][1];
      var invEnd = payload.values[i][0], unmet = payload.values[i][1], moved = payload.values[i][2];
      var rect = document.createElementNS(NS, 'rect');
      rect.setAttribute('x', (ix * step).toString());
      rect.setAttribute('y', (iy * step).toString());
      rect.setAttribute('width', data.cell_px);
      rect.setAttribute('height', data.cell_px);
      rect.setAttribute('rx', 1.5);
      rect.setAttribute('opacity', (0.15 + 0.85 * (invEnd / maxInventory)).toFixed(3));
      rect.setAttribute('data-source', svg.getAttribute('data-source'));
      rect.setAttribute('data-cell', cellId);
      rect.setAttribute('data-inventory-end', invEnd);
      rect.setAttribute('data-unmet', unmet);
      rect.setAttribute('data-moved', moved);
      if (unmet > 0) rect.setAttribute('class', 'unmet');
      var title = document.createElementNS(NS, 'title');
      title.textContent = cellId + ' 期末 ' + invEnd + ' 未满足 ' + unmet + ' 搬出 ' + moved;
      rect.appendChild(title);
      layer.appendChild(rect);
    }
  });
})();
"""
