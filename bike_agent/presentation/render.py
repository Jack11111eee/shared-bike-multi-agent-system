"""Phase 4: the workbench page, its styles and its two scripts.

The page is a *view*: every number it prints comes from the single analysis
object built by :func:`bike_agent.presentation.analysis.build_presentation`, and
the first frame is rendered into the DOM in Python so the shipped HTML can be
checked without a browser (the same trick Phase 3 used for the player).

``workbench-core.js`` is a pure state machine with no DOM access, so the
navigation rules (chapter order, pause-on-jump, rejecting a cell that is not on
this grid) can be verified in a lightweight JS environment.  ``workbench.js``
is only DOM wiring around that machine.
"""

from __future__ import annotations

import html
import json
from typing import Any

from bike_agent.config import Layout
from bike_agent.presentation.analysis import (
    EVENT_LABELS,
    METRIC_LABELS,
    SCENARIO_METRICS,
    presentation_js,
)
from bike_agent.replay.site import CELL_GAP, CELL_PX, STRATEGIES, STRATEGY_LABELS

MAX_WORKBENCH_BYTES = 256 * 1024
MAX_PRESENTATION_BYTES = 2 * 1024 * 1024


def _esc(value: Any) -> str:
    return html.escape(str(value), quote=True)


def _fmt_int(value: Any) -> str:
    return f"{int(value):,}"


def _fmt_float(value: Any, digits: int = 2) -> str:
    return f"{float(value):,.{digits}f}"


def _signed(value: float, digits: int = 0) -> str:
    """Signed display; an exact zero is written as 0 rather than +0."""
    if digits:
        return f"{value:+,.{digits}f}" if round(value, digits) else f"{0:.{digits}f}"
    return f"{int(value):+,}" if int(value) else "0"


def _metric(
    *,
    label: str,
    metric: str,
    value: Any,
    source_file: str,
    display: str,
    join_key: str,
    formula: str,
    css: str = "",
    operands: dict[str, Any] | None = None,
) -> str:
    """One number with the full §2.3 evidence trail on the element itself."""
    extra = ""
    if operands is not None:
        extra = f' data-operands="{_esc(json.dumps(operands, ensure_ascii=False, separators=(",", ":")))}"'
    return (
        f'<div class="num{" " + css if css else ""}"><span class="num-label">{_esc(label)}</span>'
        f'<b class="metric" data-metric="{_esc(metric)}" data-value="{_esc(value)}" '
        f'data-source="{_esc(source_file)}" data-join-key="{_esc(join_key)}" '
        f'data-formula="{_esc(formula)}"{extra}>{_esc(display)}</b></div>'
    )


# -- grid rendering ---------------------------------------------------------
def _grid_rects(
    *,
    values: list[int],
    cell_ids: list[str],
    geom: list[list[int]],
    color_max: int,
) -> str:
    """Frame 0 as static SVG.  Deliberately terse: 255 cells per grid, twice.

    The strategy and source live on the enclosing ``<svg>``; repeating them on
    every rect would add ~100 KB to a page that is capped at 256 KB.
    """
    step = CELL_PX + CELL_GAP
    rects = []
    for cell_id, (ix, iy), inventory in zip(cell_ids, geom, values):
        opacity = 0.12 + 0.88 * (inventory / color_max) if color_max else 0.12
        rects.append(
            f'<rect class="cell" data-cell="{cell_id}" data-inventory-end="{inventory}" '
            f'x="{ix * step}" y="{iy * step}" width="{CELL_PX}" height="{CELL_PX}" '
            f'fill-opacity="{opacity:.3f}"><title>{cell_id} 期末库存 {inventory}</title></rect>'
        )
    return "".join(rects)


def _delta_rects(
    *,
    delta: list[int],
    cell_ids: list[str],
    geom: list[list[int]],
    max_abs: int,
) -> str:
    step = CELL_PX + CELL_GAP
    rects = []
    for cell_id, (ix, iy), value in zip(cell_ids, geom, delta):
        if value < 0:
            kind, word, glyph = "improved", "改善", "−"
        elif value > 0:
            kind, word, glyph = "worsened", "退步", "+"
        else:
            kind, word, glyph = "unchanged", "无差异", ""
        intensity = abs(value) / max_abs if max_abs else 0.0
        x, y = ix * step, iy * step
        # One glyph per cell, empty when the delta is zero: the script rewrites
        # the glyph text in place when the frame changes, so the sign is carried
        # by a non-colour channel without the script owning any geometry.
        text = (
            f'<text class="delta-glyph" x="{x + CELL_PX / 2:.1f}" '
            f'y="{y + CELL_PX * 0.72:.1f}">{glyph}</text>'
        )
        rects.append(
            f'<rect class="cell delta {kind}" data-cell="{cell_id}" data-delta="{value}" '
            f'data-delta-kind="{kind}" x="{x}" y="{y}" width="{CELL_PX}" height="{CELL_PX}" rx="1.5" '
            f'fill-opacity="{0.15 + 0.85 * intensity:.3f}">'
            f"<title>{cell_id} {word} {abs(value)} 次（delta_unmet = {value:+d}）</title></rect>"
            f"{text}"
        )
    return f'<g class="delta-cells">{"".join(rects)}</g>'


def _svg(
    *,
    css: str,
    width: int,
    height: int,
    label: str,
    content: str,
    extra: str = "",
) -> str:
    return (
        f'<svg class="grid {css}" viewBox="0 0 {width} {height}" role="img" '
        f'aria-label="{_esc(label)}"{extra}>{content}</svg>'
    )


# -- sections ---------------------------------------------------------------
def _guide_block(presentation: dict[str, Any]) -> str:
    guide = presentation["guide"]
    chapters = []
    for chapter in guide["chapters"]:
        target = chapter["target"]
        evidence = "".join(
            f'<li><span>{_esc(item["metric"])} = {_esc(item["value"])}</span>'
            f'<code>{_esc(item["source_file"])}</code>'
            f'<span class="join">关联键 {_esc(item["join_key"])}</span>'
            f'<span class="formula">{_esc(item["formula"])}</span></li>'
            for item in chapter["evidence"]
        )
        chapters.append(
            f'<li class="chapter" data-chapter="{chapter["index"]}" '
            f'data-chapter-id="{_esc(chapter["id"])}" data-target-frame="{target["frame"]}" '
            f'data-target-mode="{_esc(target["mode"])}" '
            f'data-target-cell="{_esc(target["cell"] or "")}" '
            f'data-target-scenario="{_esc(target["scenario"])}" '
            f'data-target-source="{_esc(target["source"])}">'
            f'<h3><span class="chapter-no">第 {chapter["index"] + 1} 章</span> '
            f'{_esc(chapter["title"])}</h3>'
            f"<p>{_esc(chapter['body'])}</p>"
            f'<ul class="evidence-list">{evidence}</ul></li>'
        )
    actions = "".join(
        f'<button data-guide-action="{action["id"]}">{_esc(action["label"])}</button>'
        for action in guide["actions"]
    )
    return (
        f'<section class="card wide guide" id="guide" data-guide '
        f'data-n-chapters="{guide["n_chapters"]}">'
        f"<h2>演示导览</h2>"
        f'<p class="hint">{_esc(guide["segment_note"])}</p>'
        f'<div class="guide-head">'
        f'<span>当前章 <b class="metric" id="guide-chapter" data-metric="guide_chapter" '
        f'data-value="-1" data-source="replay/{presentation["source"]}/workbench.html" '
        f'data-formula="导览状态机当前章节" data-join-key="guide">未开始</b></span>'
        f'<span id="guide-title" aria-live="polite">未开始</span>'
        f'<span class="guide-state" id="guide-state" data-state="idle">未开始</span></div>'
        f'<div class="guide-actions" role="group" aria-label="导览控制">{actions}</div>'
        f'<ol class="guide-chapters">{"".join(chapters)}</ol>'
        f'<p class="hint">{_esc(guide["regression_note"])}</p>'
        f"</section>"
    )


def _frame_block(presentation: dict[str, Any]) -> str:
    source = presentation["source"]
    csv_rule = f"outputs/sim_rule.{source}.csv"
    n_frames = presentation["n_frames"]
    first = presentation["frames"][0]
    return (
        f'<section class="card wide frame-bar">'
        f"<h2>分析状态</h2>"
        f'<div class="state-row">'
        f'<span>数据源 <b id="state-source" data-metric="source" data-value="{_esc(source)}" '
        f'data-source="replay/{source}/workbench.html" data-formula="页面数据源" '
        f'data-join-key="source">{_esc(source.upper())}</b></span>'
        f'<span>当前帧 <b class="metric" id="frame-label" data-metric="frame_index" data-value="1" '
        f'data-source="{csv_rule}" data-formula="row-position" '
        f'data-join-key="time_bucket=第 1 行">1</b> / {n_frames}</span>'
        f'<span>时间单元 <b class="metric" id="bucket-label" data-metric="time_bucket" '
        f'data-value="{_esc(first["time_bucket"])}" data-source="{csv_rule}" '
        f'data-formula="row-position → time_bucket" data-join-key="row 1">'
        f'{_esc(first["time_bucket"])}</b></span>'
        f'<span>模式 <b id="state-mode" data-metric="mode" data-value="side" '
        f'data-source="replay/{source}/workbench.html" data-formula="界面模式" '
        f'data-join-key="mode">并排对照</b></span>'
        f'<span>选中格子 <b id="state-cell" data-metric="selected_cell" data-value="" '
        f'data-source="replay/{source}/workbench.html" data-formula="用户选择" '
        f'data-join-key="cell_id">未选择</b></span>'
        f'<span>场景 <b id="state-scenario" data-metric="scenario" data-value="baseline" '
        f'data-source="outputs/sim_sensitivity.{source}.json" data-formula="场景选择" '
        f'data-join-key="scenario">baseline</b></span></div>'
        f'<div class="controls" data-step-ms="900">'
        f'<button data-control="play" data-state="paused">播放</button>'
        f'<button data-control="prev">上一帧</button>'
        f'<button data-control="next">下一帧</button>'
        f'<label class="scrub">进度'
        f'<input type="range" data-control="scrub" min="0" max="{max(n_frames - 1, 0)}" '
        f'step="1" value="0" aria-label="进度"></label>'
        f'<span class="play-state" id="play-state" data-playing="false">已暂停</span>'
        f"</div></section>"
    )


def _map_block(presentation: dict[str, Any]) -> str:
    source = presentation["source"]
    cell_ids = presentation["cell_ids"]
    geom = presentation["geom"]
    first = presentation["frames"][0]
    # 期末库存 is the player's reading of the same frame; it comes from the shared
    # replay bundle so the two pages cannot show different numbers for one cell.
    first_replay = presentation["_replay_frames"][0]
    max_ix = max((pos[0] for pos in geom), default=0)
    max_iy = max((pos[1] for pos in geom), default=0)
    step = CELL_PX + CELL_GAP
    width = (max_ix + 1) * step
    height = (max_iy + 1) * step
    color_max = int(presentation["color_domain"]["inventory"]["max"])
    max_abs = int(presentation["color_domain"]["delta"]["max_abs"])

    side = "".join(
        f'<div class="pane" data-pane="{strategy}">'
        f"<h3>{_esc(STRATEGY_LABELS[strategy])}</h3>"
        f'<p class="legend">期末库存固定色标 0–{color_max}（与播放器一致，跨帧不变）</p>'
        + _svg(
            css="inventory",
            width=width,
            height=height,
            label=f"{STRATEGY_LABELS[strategy]} 逐格期末库存",
            content=_grid_rects(
                values=first_replay["strategies"][strategy]["inventory_end"],
                cell_ids=cell_ids,
                geom=geom,
                color_max=color_max,
            ),
            extra=(
                f' data-strategy="{strategy}" '
                f'data-source="outputs/replay.{strategy}.{source}.parquet"'
            ),
        )
        + "</div>"
        for strategy in STRATEGIES
    )

    delta_pane = (
        f'<div class="pane" data-pane="delta">'
        f"<h3>未满足请求差分（delta_unmet = unmet_optimized − unmet_rule）</h3>"
        f'<p class="legend delta-legend" data-scale="delta" data-min="{-max_abs}" data-max="{max_abs}">'
        f"色域固定 ±{max_abs} 次，中点为零；<span class=\"key improved\">− 负值 = S-OPT 更少未满足（改善）</span>、"
        f'<span class="key worsened">+ 正值 = S-OPT 更多未满足（退步）</span>、'
        f'<span class="key unchanged">0 = 无差异</span>。'
        + (
            "本源全回放所有格子 delta_unmet 恒为 0：<strong>全程无差异</strong>。"
            if max_abs == 0
            else "单元格内的 − / + 号与颜色同时表示方向，不只依赖颜色。"
        )
        + "</p>"
        + _svg(
            css="delta-map",
            width=width,
            height=height,
            label="逐格未满足请求差分",
            content=_delta_rects(
                delta=first["delta"],
                cell_ids=cell_ids,
                geom=geom,
                max_abs=max_abs,
            ),
            extra=(
                ' data-strategy="delta" '
                f'data-source="outputs/replay.optimized.{source}.parquet"'
            ),
        )
        + "</div>"
    )

    summary = first["delta_summary"]
    return (
        f'<section class="card wide maps" data-maps>'
        f"<h2>地图</h2>"
        f'<div class="mode-switch" role="group" aria-label="地图模式">'
        f'<button data-mode="side" class="active">并排对照</button>'
        f'<button data-mode="delta">未满足请求差分</button></div>'
        f'<div class="map-summary" id="delta-summary" data-status="{_esc(summary["status"])}">'
        f'<span>本帧净差 <b class="metric" data-metric="delta_net" data-value="{summary["net"]}" '
        f'data-source="outputs/replay.optimized.{source}.parquet" '
        f'data-formula="sum(unmet_optimized - unmet_rule) over cells" '
        f'data-join-key="time_bucket={_esc(first["time_bucket"])}">'
        f"{_signed(summary['net'])}</b> 次</span>"
        f'<span>改善格 <b class="metric" data-metric="improved_cells" data-value="{summary["improved_cells"]}" '
        f'data-source="outputs/replay.optimized.{source}.parquet" '
        f'data-formula="count(delta_unmet < 0)" '
        f'data-join-key="time_bucket={_esc(first["time_bucket"])}">{summary["improved_cells"]}</b></span>'
        f'<span>退步格 <b class="metric" data-metric="worsened_cells" data-value="{summary["worsened_cells"]}" '
        f'data-source="outputs/replay.optimized.{source}.parquet" '
        f'data-formula="count(delta_unmet > 0)" '
        f'data-join-key="time_bucket={_esc(first["time_bucket"])}">{summary["worsened_cells"]}</b></span>'
        f'<span>无差异格 <b class="metric" data-metric="unchanged_cells" data-value="{summary["unchanged_cells"]}" '
        f'data-source="outputs/replay.optimized.{source}.parquet" '
        f'data-formula="count(delta_unmet = 0)" '
        f'data-join-key="time_bucket={_esc(first["time_bucket"])}">{summary["unchanged_cells"]}</b></span>'
        f'<span class="hint">净差为正不代表本帧没有改善格：同一帧可以同时存在改善与退步。</span>'
        f"</div>"
        f'<div class="panes" id="panes">'
        f'<div class="side" data-view="side">{side}</div>'
        f'<div class="delta-view" data-view="delta" hidden>{delta_pane}</div>'
        f"</div>"
        f'<p class="hint">点击格子即可选择；键盘用户请用账本上方的「选择格子」下拉框'
        f"（{presentation['n_cells']} 个格子不适合逐个做成 tab 停靠点）。"
        f"差分图与并排图共用同一份时段数据；切换模式不会改变数据源、时段或已选格子。</p>"
        f"</section>"
    )


def _events_block(presentation: dict[str, Any]) -> str:
    rows = []
    for event in presentation["events"]:
        ok = event["status"] == "ok"
        value_display = (
            f"{event['value']:+,} 次" if event["metric"] == "gain" else f"{_fmt_int(event['value'])} {event['unit']}"
        )
        rows.append(
            f'<li class="event" data-event-id="{_esc(event["id"])}" data-event-type="{_esc(event["type"])}" '
            f'data-status="{_esc(event["status"])}">'
            f'<button data-event="{_esc(event["id"])}"{" disabled" if not ok else ""}>'
            f'<span class="event-label">{_esc(EVENT_LABELS[event["type"]])}</span>'
            f'<span class="event-value metric" data-metric="{_esc(event["metric"])}" '
            f'data-value="{event["value"]}" data-source="{_esc(event["source_file"])}" '
            f'data-formula="{_esc(event["formula"])}" data-join-key="{_esc(event["join_key"])}">'
            f"{_esc(value_display)}</span>"
            f'<span class="event-time">{_esc(event["time_bucket"] or "—")}</span>'
            f'<span class="event-state">{"关键事件" if ok else _esc(event["note"] or event["status"])}</span>'
            f"</button>"
            f'<p class="hint">{_esc(event["formula"])}'
            f'{"；平手取最早时段（" + str(event["tie_count"]) + " 个时段同为极值）" if ok and event["tie_count"] > 1 else ""}'
            f"</p></li>"
        )
    return (
        f'<section class="card wide events" id="events" data-events>'
        f"<h2>关键事件</h2>"
        f'<p class="hint">四类事件在全部 {presentation["n_frames"]} 个时间单元上计算；'
        f"点击后跳到该时段并暂停播放。gain = unmet_rule − unmet_optimized，正数表示 S-OPT 少了未满足请求。</p>"
        f'<ul class="event-list">{"".join(rows)}</ul>'
        f"</section>"
    )


def _ledger_block(presentation: dict[str, Any]) -> str:
    source = presentation["source"]
    top = presentation["ledger_top_cell"]
    cell_id = top["cell_id"]
    # The static page opens on frame 1 (``_frame_block`` says so), so the ledger
    # has to show frame 1 too: a page whose label and numbers disagree before the
    # script runs is not checkable without a browser.  The guide's chapter 4 is
    # what jumps to ``top["frame"]``.
    frame_index = 0
    frame = presentation["frames"][frame_index]
    equations = presentation["equations"]
    cumulative = presentation["cell_cumulative"]
    position = presentation["cell_ids"].index(cell_id)
    panes = []
    for strategy in STRATEGIES:
        ledger = frame["strategies"][strategy]["ledger"]
        incoming = frame["strategies"][strategy]["moved_in"].get(str(position), 0)
        moved_out = ledger["out"][position]
        scheduled = ledger["start"][position] + incoming - moved_out
        unmet_value = ledger["req"][position] - ledger["met"][position]
        overflow = ledger["ret"][position] - ledger["rapp"][position]
        end_value = scheduled + ledger["rapp"][position] - ledger["met"][position]
        parquet = f"outputs/replay.{strategy}.{source}.parquet"
        moves = f"outputs/sim_moves.{strategy}.{source}.json"
        join = f"cell_id={cell_id};time_bucket={frame['time_bucket']}"
        # The dispatch records for this cell and frame, both directions, so the
        # arrivals above can be opened rather than taken on trust.
        records = [
            record
            for record in presentation["_replay_frames"][frame_index]["moves"][strategy]
            if str(record["from_cell"]) == cell_id or str(record["to_cell"]) == cell_id
        ]
        move_items = "".join(
            f'<li class="move-row" data-direction="{"in" if str(r["to_cell"]) == cell_id else "out"}" '
            f'data-from="{_esc(r["from_cell"])}" data-to="{_esc(r["to_cell"])}" '
            f'data-bikes="{int(r["bikes"])}" data-source="{moves}">'
            f'{_esc(r["from_cell"])} → {_esc(r["to_cell"])}：{int(r["bikes"])} 辆'
            f'（{float(r["distance_m"]):.0f} m）</li>'
            for r in records
        ) or '<li class="empty">本帧没有涉及该格的搬运记录。</li>'
        panes.append(
            f'<div class="pane" data-ledger-strategy="{strategy}">'
            f"<h3>{_esc(STRATEGY_LABELS[strategy])}</h3>"
            f'<dl class="ledger">'
            + _metric(label="期初库存（辆）", metric="inventory_start", value=ledger["start"][position],
                      source_file=parquet, display=_fmt_int(ledger["start"][position]),
                      join_key=join, formula="parquet.inventory_start")
            + _metric(label="调入（辆）", metric="moved_in", value=incoming,
                      source_file=moves, display=_fmt_int(incoming), join_key=join,
                      formula="sum(bikes) where to_cell = 选中格")
            + _metric(label="调出（辆）", metric="moved_out", value=moved_out,
                      source_file=moves, display=_fmt_int(moved_out), join_key=join,
                      formula="sum(bikes) where from_cell = 选中格；等于 parquet.bikes_moved")
            + _metric(label="调度后库存（辆）", metric="scheduled_inventory", value=scheduled,
                      source_file=parquet, display=_fmt_int(scheduled), join_key=join,
                      formula=equations["scheduled_inventory"],
                      operands={"inventory_start": ledger["start"][position],
                                "moved_in": incoming, "moved_out": moved_out})
            + _metric(label="归还（辆）", metric="returns", value=ledger["ret"][position],
                      source_file=parquet, display=_fmt_int(ledger["ret"][position]),
                      join_key=join, formula="parquet.returns")
            + _metric(label="接纳归还（辆）", metric="returns_applied", value=ledger["rapp"][position],
                      source_file=parquet, display=_fmt_int(ledger["rapp"][position]),
                      join_key=join, formula="parquet.returns_applied")
            + _metric(label="溢出归还（辆）", metric="returns_overflow", value=overflow,
                      source_file=parquet, display=_fmt_int(overflow), join_key=join,
                      formula=equations["overflow"],
                      operands={"returns": ledger["ret"][position],
                                "returns_applied": ledger["rapp"][position]})
            + _metric(label="借车请求（次）", metric="borrow_requests", value=ledger["req"][position],
                      source_file=parquet, display=_fmt_int(ledger["req"][position]),
                      join_key=join, formula="parquet.borrow_requests")
            + _metric(label="满足（次）", metric="borrow_met", value=ledger["met"][position],
                      source_file=parquet, display=_fmt_int(ledger["met"][position]),
                      join_key=join, formula="parquet.borrow_met")
            + _metric(label="未满足（次）", metric="unmet_requests", value=unmet_value,
                      source_file=parquet, display=_fmt_int(unmet_value), join_key=join,
                      formula=equations["unmet"],
                      operands={"borrow_requests": ledger["req"][position],
                                "borrow_met": ledger["met"][position]})
            + _metric(label="期末库存（辆）", metric="inventory_end", value=end_value,
                      source_file=parquet, display=_fmt_int(end_value), join_key=join,
                      formula=equations["inventory_end"],
                      operands={"inventory_start": ledger["start"][position],
                                "moved_in": incoming, "moved_out": moved_out,
                                "returns_applied": ledger["rapp"][position],
                                "borrow_met": ledger["met"][position]})
            + "</dl>"
            f'<p class="hint">搬运记录入口：<code>{moves}</code> 中 <code>to_cell</code> = 选中格为调入、'
            f"<code>from_cell</code> = 选中格为调出；<code>bikes_moved</code> 只表示搬出，不能当作调入。</p>"
            f'<h4>本帧涉及该格的搬运记录</h4>'
            f'<ul class="cell-moves" data-moves-strategy="{strategy}">{move_items}</ul>'
            f'<h4>全回放历史</h4>'
            f'<div class="history" data-history-strategy="{strategy}">'
            f'<table class="history-table"><thead><tr><th>时间单元</th><th>期初</th><th>调入</th><th>调出</th>'
            f"<th>期末</th><th>未满足</th></tr></thead><tbody></tbody></table>"
            f'<p class="hint history-empty">历史行由脚本按同一份分析数据生成（{presentation["n_frames"]} 行）。</p>'
            f"</div></div>"
        )
    return (
        f'<section class="card wide ledger-card" id="ledger" data-ledger>'
        f"<h2>单格库存账本</h2>"
        f'<p class="hint">默认格子 <b id="ledger-cell" data-metric="selected_cell" '
        f'data-value="{_esc(cell_id)}" data-source="outputs/replay.rule.{source}.parquet" '
        f'data-formula="{_esc(top["rule"])}" data-join-key="cell_id">'
        f"{_esc(cell_id)}</b>（{_esc(top['rule'])}）。"
        f"账本把<strong>观测/预测</strong>（输入需求）、<strong>模拟库存</strong>（回放引擎结果）"
        f"与<strong>派生算式</strong>（本页按引擎口径计算）分开标注，不把时间不同的风险排名当作当前格子的成因。</p>"
        f'<label class="cell-picker">选择格子'
        f'<select data-cell-select aria-label="选择格子">'
        + "".join(
            f'<option value="{name}"{" selected" if name == cell_id else ""}>'
            f"{name} ({cumulative[name]})</option>"
            for name in presentation["cell_ids"]
        )
        + f"</select></label>"
        f'<div class="ledger-panes">{"".join(panes)}</div>'
        f"</section>"
    )


def _scenarios_block(presentation: dict[str, Any]) -> str:
    scenarios = presentation["scenarios"]
    buttons = "".join(
        f'<button data-scenario="{_esc(row["id"])}"'
        + (' class="active"' if row["status"] == "baseline" else "")
        + f'>{_esc(row["label"])}</button>'
        for row in scenarios["rows"]
    )
    header = "".join(f"<th>{_esc(METRIC_LABELS[metric])}</th>" for metric in SCENARIO_METRICS)
    body_rows = []
    for row in scenarios["rows"]:
        for strategy in STRATEGIES:
            cells = []
            for metric in SCENARIO_METRICS:
                block = row["strategies"][strategy][metric]
                delta = block["delta"]
                if delta["status"] == "zero_denominator":
                    delta_text = f"绝对 {_signed(delta['absolute'], 2)}；基准为 0，百分比不适用"
                else:
                    delta_text = f"{_signed(delta['absolute'], 2)}（{_signed(delta['percent'], 2)}%）"
                cells.append(
                    f'<td><b class="metric" data-metric="{metric}" data-value="{block["value"]}" '
                    f'data-source="{_esc(scenarios["source_file"])}" '
                    f'data-formula="{_esc(block["formula"])}" '
                    f'data-join-key="scenario={_esc(row["id"])};strategy={strategy}">'
                    f'{_esc(_fmt_scenario(metric, block["value"]))}</b>'
                    f'<span class="delta" data-delta-status="{_esc(delta["status"])}">{_esc(delta_text)}</span></td>'
                )
            body_rows.append(
                f'<tr data-scenario="{_esc(row["id"])}" data-strategy="{strategy}" '
                f'data-status="{_esc(row["status"])}">'
                f'<th scope="row">{_esc(row["label"])}</th>'
                f"<td>{_esc(STRATEGY_LABELS[strategy])}</td>"
                f"{''.join(cells)}</tr>"
            )
    changed = "、".join(
        f"{row['changed']['factor']}={row['changed']['value']}"
        for row in scenarios["rows"]
        if row["changed"]
    )
    return (
        f'<section class="card wide scenarios" id="scenarios" data-scenarios>'
        f"<h2>敏感性场景</h2>"
        f'<p class="hint" id="scenario-note">{_esc(scenarios["scope_note"])}</p>'
        f'<div class="scenario-switch" role="group" aria-label="场景">'
        f'{buttons}<button data-scenario="baseline" class="baseline-return">回到 baseline</button></div>'
        f'<p class="hint">每个场景只使用 <code>{_esc(scenarios["source_file"])}</code> 中已计算好的汇总，'
        f"不插值、不组合参数、不在浏览器重跑仿真。改变的因素：{_esc(changed) or '无（baseline）'}。</p>"
        f'<div class="table-wrap"><table class="scenario-table"><thead><tr><th>场景</th><th>策略</th>{header}</tr></thead>'
        f'<tbody>{"".join(body_rows)}</tbody></table></div>'
        f'<p class="hint" id="scenario-scope" data-scope="scenario">当前显示：<b>baseline 回放地图</b>；'
        f"场景切换只更新上表数字，不改写地图数据。</p>"
        f"</section>"
    )


def _fmt_scenario(metric: str, value: Any) -> str:
    if metric == "service_rate":
        return f"{float(value) * 100:.2f}%"
    if metric == "distance_bike_km":
        return _fmt_float(value, 3)
    if metric == "composite_cost":
        return _fmt_float(value, 2)
    return _fmt_int(value)


def _evidence_block(presentation: dict[str, Any]) -> str:
    source = presentation["source"]
    inputs = presentation["inputs"]
    rows = "".join(
        f'<tr><td>{_esc(name)}</td><td><code>{_esc(meta["path"])}</code></td>'
        f'<td class="sha">{_esc(meta["sha256"][:16])}…</td></tr>'
        for name, meta in inputs.items()
        if isinstance(meta, dict) and "path" in meta
    )
    return (
        f'<section class="card wide evidence" id="evidence" data-evidence>'
        f"<h2>证据与口径</h2>"
        f'<p class="hint">本页每个数字都带 <code>data-source</code>、<code>data-join-key</code> 与 '
        f"<code>data-formula</code>；派生值另带 <code>data-operands</code>。"
        f"口径：{_esc(presentation['equations']['inventory_end'])}；"
        f"{_esc(presentation['equations']['unmet'])}；{_esc(presentation['equations']['overflow'])}。</p>"
        f'<div class="table-wrap"><table class="inputs"><thead><tr><th>输入</th><th>文件</th><th>sha256</th></tr></thead>'
        f"<tbody>{rows}</tbody></table></div>"
        f'<p class="hint">播放器数据包 sha256：<code>{_esc(presentation["replay_bundle_sha"][:16])}…</code>'
        f"（与 <code>replay/{source}/replay-data.js</code> 相同，工作台不另算一份帧数据）。</p>"
        f"</section>"
    )


# -- page -------------------------------------------------------------------
def render_workbench(presentation: dict[str, Any], other_sources: list[str]) -> str:
    source = presentation["source"]
    # Each source ships its own page with its own bundle, so switching source is
    # a navigation, not a re-render: a plain link works without the other
    # bundle being loaded (which would double the site past its size budget).
    switches = "".join(
        f'<a data-source-switch="{_esc(name)}"'
        + (' class="active" aria-current="page"' if name == source else f' href="../{_esc(name)}/workbench.html"')
        + f">{_esc(name.upper())}</a>"
        for name in [source, *other_sources]
    )
    return f"""<!doctype html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>复盘工作台 · {_esc(source.upper())} · bike-agent</title>
<link rel="stylesheet" href="../style.css">
<link rel="stylesheet" href="../player.css">
<link rel="stylesheet" href="../workbench.css">
</head>
<body class="workbench" data-source="{_esc(source)}">
<header>
<h1>复盘工作台 · {_esc(source.upper())}</h1>
<nav class="pager">
<a href="../index.html">全部数据源</a>
<a href="player.html">完整播放器</a>
<a href="../dashboard.html">总览页</a>
<a href="frame-0000.html">逐帧页</a>
<a href="#guide">演示导览</a>
</nav>
<div class="source-switch" role="group" aria-label="数据源">{switches}</div>
<p class="notice">进入演示 → 定位关键时刻 → 比较策略差异 → 查看单格账本 → 切换参数场景。
所有数字均为<strong>模拟值</strong>，来自 <code>outputs/</code> 的静态产物；导览与事件允许显式跳转并标注
“关键事件/选段”，完整播放器仍覆盖全部帧。</p>
</header>
<main>
{_guide_block(presentation)}
{_frame_block(presentation)}
{_map_block(presentation)}
{_events_block(presentation)}
{_ledger_block(presentation)}
{_scenarios_block(presentation)}
{_evidence_block(presentation)}
</main>
<noscript>
<section class="card wide">
<h2>需要脚本</h2>
<p class="notice">本页的联动需要 JavaScript。没有脚本时，请使用既有的静态回放：
<a href="frame-0000.html">逐帧页</a>（每帧一页，无需脚本）、
<a href="player.html">播放器</a>、<a href="../dashboard.html">总览页</a>。
本页覆盖 {presentation["n_frames"]} 个时间单元、{presentation["n_cells"]} 个格子；
数据源 {_esc(source.upper())}，时间范围 {_esc(presentation["buckets"][0])} → {_esc(presentation["buckets"][-1])}。
所有数值均为模拟值。</p>
</section>
</noscript>
<script src="replay-data.js"></script>
<script src="presentation.js"></script>
<script src="../workbench-core.js"></script>
<script src="../workbench.js"></script>
</body>
</html>
"""


def render_workbench_page(presentation: dict[str, Any], other_sources: list[str]) -> str:
    """The page as shipped.

    Switching source navigates to that source's own page instead of re-rendering
    this one: every grid, event list and scenario table is prerendered per
    source in Python, and a client-side second renderer would be exactly the
    duplicate derivation §2.3 forbids.  Navigation also clears a stale cell or
    scenario selection for free, because the next page starts from its own
    defaults.
    """
    return render_workbench(presentation, other_sources)


# -- writing ----------------------------------------------------------------
def build_workbench(
    config: dict[str, Any],
    layout: Layout,
    presentations: dict[str, dict[str, Any]],
) -> dict[str, Any]:
    """Write ``replay/<source>/{presentation.js,workbench.html}`` and the shared assets."""
    site_dir = layout.root / "replay"
    site_dir.mkdir(parents=True, exist_ok=True)
    (site_dir / "workbench.css").write_text(WORKBENCH_CSS, encoding="utf-8")
    (site_dir / "workbench-core.js").write_text(WORKBENCH_CORE_JS, encoding="utf-8")
    (site_dir / "workbench.js").write_text(WORKBENCH_JS, encoding="utf-8")

    written: dict[str, Any] = {}
    for source, presentation in presentations.items():
        source_dir = site_dir / source
        source_dir.mkdir(parents=True, exist_ok=True)
        payload = presentation_js(presentation)
        if len(payload.encode("utf-8")) > MAX_PRESENTATION_BYTES:
            raise ValueError(
                f"{source}: presentation.js is {len(payload.encode('utf-8'))} bytes, over the "
                f"NF-24 cap of {MAX_PRESENTATION_BYTES}; compact the encoding, never drop frames"
            )
        (source_dir / "presentation.js").write_text(payload, encoding="utf-8")
        others = [value for key, value in presentations.items() if key != source]
        page = render_workbench_page(presentation, [others[0]["source"]] if others else [])
        if len(page.encode("utf-8")) > MAX_WORKBENCH_BYTES:
            raise ValueError(
                f"{source}: workbench.html is {len(page.encode('utf-8'))} bytes, over the "
                f"NF-24 cap of {MAX_WORKBENCH_BYTES}"
            )
        (source_dir / "workbench.html").write_text(page, encoding="utf-8")
        written[source] = {
            "path": str(source_dir / "workbench.html"),
            "presentation_bytes": len(payload.encode("utf-8")),
            "workbench_bytes": len(page.encode("utf-8")),
            "n_frames": presentation["n_frames"],
            "n_cells": presentation["n_cells"],
            "n_events": len(presentation["events"]),
            "n_scenarios": presentation["scenarios"]["n_scenarios"],
            "n_chapters": presentation["guide"]["n_chapters"],
        }
    return written


# -- assets -----------------------------------------------------------------
WORKBENCH_CSS = """.workbench{--improved:#1f6feb;--worsened:#b3341f;--neutral:#8b96a3}
@media (prefers-color-scheme: dark){
.workbench{--improved:#6aa8ff;--worsened:#ff8a6b;--neutral:#7c8794}
}
.workbench main{display:grid;grid-template-columns:repeat(2,minmax(0,1fr));gap:16px}
.workbench .card.wide{grid-column:1/-1}
/* A wide table scrolls inside its own box instead of pushing the page sideways. */
.workbench .card{min-width:0;overflow-wrap:anywhere}
.table-wrap{max-width:100%;overflow-x:auto}
.table-wrap table{min-width:520px}
.workbench h2{font-size:16px}
.workbench h3{font-size:14px;margin:8px 0 4px}
.workbench h4{font-size:13px;margin:8px 0 4px;color:var(--muted)}
.source-switch,.mode-switch,.scenario-switch,.guide-actions{display:flex;flex-wrap:wrap;gap:6px;margin:8px 0}
.source-switch a,.source-switch button,.mode-switch button,.scenario-switch button,.guide-actions button{padding:5px 12px;
border:1px solid var(--line);border-radius:6px;background:var(--bg);color:var(--fg);font:inherit;cursor:pointer;text-decoration:none}
.source-switch a.active,.source-switch button.active,.mode-switch button.active,.scenario-switch button.active,
.guide-actions button[data-guide-action="next"]{border-color:var(--accent);color:var(--accent);font-weight:600}
.guide-actions button:disabled,.scenario-switch button:disabled{opacity:.5;cursor:not-allowed}
.guide-head{display:flex;flex-wrap:wrap;gap:14px;align-items:baseline;margin:6px 0;font-size:13px;color:var(--muted)}
.guide-state[data-state="active"]{color:var(--accent);font-weight:600}
.guide-chapters{list-style:none;margin:8px 0 0;padding:0;display:grid;gap:8px}
.guide-chapters .chapter{border:1px solid var(--line);border-radius:8px;padding:8px 10px;background:var(--bg)}
.guide-chapters .chapter[data-active="true"]{border-color:var(--accent);box-shadow:inset 0 0 0 1px var(--accent)}
.guide-chapters .chapter[hidden]{display:none}
.chapter-no{font-size:11px;color:var(--muted);margin-right:6px}
.evidence-list{list-style:none;margin:6px 0 0;padding:0;font-size:11px;color:var(--muted);
display:grid;gap:2px;font-variant-numeric:tabular-nums}
.evidence-list li{display:flex;flex-wrap:wrap;gap:8px}
.evidence-list .formula{opacity:.8}
.state-row{display:flex;flex-wrap:wrap;gap:14px;font-size:13px;color:var(--muted);margin:6px 0}
.state-row b{color:var(--fg);font-variant-numeric:tabular-nums}
.play-state{font-size:12px;color:var(--muted)}
.play-state[data-playing="true"]{color:var(--accent);font-weight:600}
.map-summary{display:flex;flex-wrap:wrap;gap:16px;align-items:baseline;font-size:13px;color:var(--muted);margin:6px 0}
.map-summary b{color:var(--fg);font-variant-numeric:tabular-nums}
.panes .side{display:grid;grid-template-columns:repeat(2,minmax(0,1fr));gap:12px}
.panes .pane{min-width:0}
svg.grid{display:block;width:100%;height:auto;max-width:560px}
svg.grid rect.cell{fill:var(--cell);stroke:none;cursor:pointer}
svg.grid rect.cell:focus{outline:none;stroke:var(--accent);stroke-width:2}
svg.grid rect.cell.selected{stroke:var(--fg);stroke-width:2}
svg.grid rect.delta.improved{fill:var(--improved)}
svg.grid rect.delta.worsened{fill:var(--worsened)}
svg.grid rect.delta.unchanged{fill:var(--muted)}
svg.grid text.delta-glyph{fill:var(--bg);font-weight:700;pointer-events:none;text-anchor:middle;font-size:7px}
.delta-legend .key{margin-right:10px}
.delta-legend .key.improved{color:var(--improved)}
.delta-legend .key.worsened{color:var(--worsened)}
.delta-legend .key.unchanged{color:var(--muted)}
.event-list{list-style:none;margin:0;padding:0;display:grid;gap:6px}
.event button{display:grid;grid-template-columns:minmax(150px,1.4fr) minmax(90px,.8fr) minmax(140px,1fr) minmax(120px,1fr);
gap:10px;width:100%;text-align:left;padding:8px 10px;border:1px solid var(--line);border-radius:8px;
background:var(--bg);color:var(--fg);font:inherit;cursor:pointer}
.event button:hover:not(:disabled){border-color:var(--accent)}
.event button:disabled{cursor:not-allowed;opacity:.7}
.event[data-status="ok"] .event-state{color:var(--accent);font-weight:600}
.event[data-status^="no_"] .event-state{color:var(--warn)}
.event-value{font-variant-numeric:tabular-nums;font-weight:600}
.event-time{color:var(--muted);font-variant-numeric:tabular-nums}
.event-state{color:var(--muted);font-size:12px}
.ledger-panes{display:grid;grid-template-columns:repeat(2,minmax(0,1fr));gap:12px}
dl.ledger{display:grid;grid-template-columns:repeat(auto-fill,minmax(150px,1fr));gap:6px 12px;margin:6px 0}
dl.ledger .num{display:flex;flex-direction:column;border:1px solid var(--line);border-radius:6px;padding:4px 8px}
.num-label{font-size:11px;color:var(--muted)}
.num .metric{font-variant-numeric:tabular-nums;font-size:14px}
.history{max-height:220px;overflow:auto;border:1px solid var(--line);border-radius:6px}
.cell-picker{display:flex;gap:8px;align-items:center;font-size:13px;color:var(--muted);margin:6px 0}
.cell-picker select{font:inherit;padding:3px 6px;border:1px solid var(--line);border-radius:6px;
background:var(--bg);color:var(--fg);max-width:260px}
.cell-moves{list-style:none;margin:4px 0;padding:0;font-size:11px;color:var(--muted);
max-height:120px;overflow:auto;font-variant-numeric:tabular-nums}
.cell-moves li{margin:1px 0}
.cell-moves .empty{font-style:normal}
.history-table{width:100%;border-collapse:collapse;font-size:11px;font-variant-numeric:tabular-nums}
.history-table th,.history-table td{border-bottom:1px solid var(--line);padding:2px 6px;text-align:right}
.history-table th:first-child,.history-table td:first-child{text-align:left}
.history-table tr[data-current="true"]{background:var(--panel);font-weight:600}
.scenario-table{width:100%;border-collapse:collapse;font-size:12px;font-variant-numeric:tabular-nums}
.scenario-table th,.scenario-table td{border:1px solid var(--line);padding:4px 8px;text-align:right}
.scenario-table thead th:first-child,.scenario-table thead th:nth-child(2),
.scenario-table tbody th{text-align:left}
.scenario-table tr[data-status="baseline"]{background:var(--panel)}
.scenario-table tr.is-current{outline:2px solid var(--accent);outline-offset:-2px}
.scenario-table .delta{display:block;font-size:10px;color:var(--muted)}
.scenario-table .delta[data-delta-status="zero_denominator"]{color:var(--warn)}
table.inputs{width:100%;border-collapse:collapse;font-size:11px}
table.inputs th,table.inputs td{border:1px solid var(--line);padding:3px 8px;text-align:left}
table.inputs .sha{font-variant-numeric:tabular-nums;color:var(--muted)}
.workbench .notice{margin:6px 0}
@media (max-width:900px){
.workbench main{grid-template-columns:1fr}
.panes .side,.ledger-panes{grid-template-columns:1fr}
.event button{grid-template-columns:1fr}
}
@media (prefers-reduced-motion: reduce){
.workbench *{transition:none !important;animation:none !important;scroll-behavior:auto !important}
}
"""

WORKBENCH_CORE_JS = """/* Phase 4 navigation state machine.  No DOM access: tests/test_presentation_guide.py
   drives this file in node, and workbench.js is the only thing that touches the page. */
(function (root) {
  'use strict';

  function clone(state) {
    return {
      source: state.source,
      chapter: state.chapter,
      started: state.started,
      frame: state.frame,
      mode: state.mode,
      cell: state.cell,
      scenario: state.scenario,
      playing: state.playing,
      visited: state.visited.slice()
    };
  }

  function create(options) {
    var sources = options.sources || {};
    var state = {
      source: options.source,
      chapter: -1,
      started: false,
      frame: 0,
      mode: 'side',
      cell: null,
      scenario: 'baseline',
      playing: false,
      visited: []
    };

    function bundle(name) {
      var value = sources[name];
      if (!value) throw new Error('unknown source ' + name);
      return value;
    }

    function clampFrame(value) {
      var n = bundle(state.source).n_frames;
      if (value < 0) return 0;
      if (value > n - 1) return n - 1;
      return value;
    }

    function hasCell(name, cellId) {
      return bundle(name).cell_ids.indexOf(cellId) >= 0;
    }

    function chapterTarget(index) {
      var chapters = bundle(state.source).guide.chapters;
      if (index < 0 || index >= chapters.length) return null;
      return chapters[index];
    }

    /* Every chapter jump applies its target and stops the clock, so the frame
       cannot drift away from the chapter the reader just opened. */
    function applyChapter(index) {
      var chapter = chapterTarget(index);
      if (!chapter) return false;
      var target = chapter.target;
      if (target.source && target.source !== state.source) {
        if (!sources[target.source]) return false;
        state.source = target.source;
        state.cell = target.cell && hasCell(target.source, target.cell) ? target.cell : null;
      }
      state.frame = clampFrame(target.frame || 0);
      state.mode = target.mode || 'side';
      if (target.cell) {
        state.cell = hasCell(state.source, target.cell) ? target.cell : null;
      }
      state.scenario = target.scenario || 'baseline';
      state.chapter = index;
      state.playing = false;
      if (state.visited.indexOf(index) < 0) state.visited.push(index);
      return true;
    }

    var api = {
      get: function () { return clone(state); },
      start: function () {
        state.started = true;
        return applyChapter(0);
      },
      next: function () {
        if (!state.started) return false;
        return applyChapter(state.chapter + 1);
      },
      prev: function () {
        if (!state.started) return false;
        return applyChapter(state.chapter - 1);
      },
      restart: function () {
        state.started = true;
        return applyChapter(0);
      },
      /* Leaving the guide keeps the analysis position; only the guide state resets. */
      exit: function () {
        state.started = false;
        state.chapter = -1;
        state.playing = false;
        return true;
      },
      gotoChapter: function (index) {
        state.started = true;
        return applyChapter(index);
      },
      /* Event navigation always pauses: the target must not be taken away. */
      jumpToEvent: function (eventId) {
        var events = bundle(state.source).events;
        for (var i = 0; i < events.length; i++) {
          if (events[i].id === eventId) {
            if (events[i].status !== 'ok' || events[i].frame === null) return false;
            state.frame = clampFrame(events[i].frame);
            state.playing = false;
            return true;
          }
        }
        return false;
      },
      setFrame: function (value) {
        state.frame = clampFrame(value);
        return state.frame;
      },
      setMode: function (mode) {
        if (mode !== 'side' && mode !== 'delta') return false;
        state.mode = mode;
        return true;
      },
      selectCell: function (cellId) {
        if (cellId === null || cellId === undefined) {
          state.cell = null;
          return true;
        }
        if (!hasCell(state.source, cellId)) return false;
        state.cell = cellId;
        return true;
      },
      setScenario: function (id) {
        var rows = bundle(state.source).scenarios.rows;
        for (var i = 0; i < rows.length; i++) {
          if (rows[i].id === id) {
            state.scenario = id;
            return true;
          }
        }
        return false;
      },
      /* Changing source is a navigation to that source's own page, not a
         re-render: a cell or scenario that does not exist there can never be
         carried across, because the next page starts from its own defaults. */
      sourceHref: function (name) {
        if (!sources[name]) return null;
        return '../' + name + '/workbench.html';
      },
      setPlaying: function (value) {
        state.playing = !!value;
        return state.playing;
      },
      togglePlaying: function () {
        state.playing = !state.playing;
        return state.playing;
      }
    };
    return api;
  }

  root.WorkbenchCore = { create: create };
})(typeof window !== 'undefined' ? window : this);
"""

WORKBENCH_JS = """/* Phase 4: DOM wiring only.  All navigation rules live in workbench-core.js. */
(function () {
  'use strict';
  var Core = window.WorkbenchCore;
  var P = window.PRESENTATION_DATA;
  var R = window.REPLAY_DATA_SOURCES;
  if (!Core || !P || !R) return;

  var sourceNames = Object.keys(P);
  var core = Core.create({ source: document.body.getAttribute('data-source'), sources: P });
  var timer = null;

  function q(selector) { return document.querySelector(selector); }
  function qa(selector) { return Array.prototype.slice.call(document.querySelectorAll(selector)); }
  function fmtInt(v) { return Number(v).toLocaleString('en-US'); }
  function signed(v) { return (v > 0 ? '+' : '') + fmtInt(v); }
  function pack(name) { return P[name]; }
  function replay(name) { return R[name]; }

  function opacity(value, max) { return (0.12 + 0.88 * (Number(value) / (max || 1))).toFixed(3); }

  function renderGrids(state) {
    var data = pack(state.source);
    var shared = replay(state.source);
    var maxInv = data.color_domain.inventory.max;
    var maxAbs = data.color_domain.delta.max_abs;
    var frame = data.frames[state.frame];
    var ids = data.cell_ids;

    ['rule', 'optimized'].forEach(function (strategy) {
      var svg = q('svg.grid[data-strategy="' + strategy + '"]');
      if (!svg) return;
      // 期末库存 comes from the shared player bundle, not a second copy.
      var values = shared.frames[state.frame].strategies[strategy].inventory_end;
      var rects = svg.querySelectorAll('rect.cell');
      for (var i = 0; i < rects.length && i < ids.length; i++) {
        var rect = rects[i];
        rect.setAttribute('data-inventory-end', values[i]);
        rect.setAttribute('fill-opacity', opacity(values[i], maxInv));
        rect.setAttribute('class', 'cell' + (ids[i] === state.cell ? ' selected' : ''));
        var title = rect.querySelector('title');
        if (title) title.textContent = ids[i] + ' 期末库存 ' + values[i];
        rect.setAttribute('aria-label', ids[i] + ' 期末库存 ' + values[i]);
      }
    });

    var deltaSvg = q('svg.delta-map');
    if (deltaSvg) {
      // The rects are prerendered by Python with their geometry; the script only
      // rewrites what changes per frame.  Rebuilding them here is what let a
      // missing geometry field turn every cell into a zero-size NaN rect.
      var deltas = frame.delta;
      var cells = deltaSvg.querySelectorAll('rect.cell');
      var glyphs = deltaSvg.querySelectorAll('text.delta-glyph');
      for (var k = 0; k < cells.length && k < ids.length; k++) {
        var value = deltas[k];
        var kind = value < 0 ? 'improved' : (value > 0 ? 'worsened' : 'unchanged');
        var word = value < 0 ? '改善' : (value > 0 ? '退步' : '无差异');
        var rectEl = cells[k];
        rectEl.setAttribute('class', 'cell delta ' + kind + (ids[k] === state.cell ? ' selected' : ''));
        rectEl.setAttribute('data-delta', value);
        rectEl.setAttribute('data-delta-kind', kind);
        rectEl.setAttribute('fill-opacity', (0.15 + 0.85 * (maxAbs ? Math.abs(value) / maxAbs : 0)).toFixed(3));
        rectEl.setAttribute('aria-label', ids[k] + ' ' + word + ' ' + Math.abs(value) + ' 次');
        var t = rectEl.querySelector('title');
        if (t) t.textContent = ids[k] + ' ' + word + ' ' + Math.abs(value) + ' 次（delta_unmet = ' + signed(value) + '）';
        if (glyphs[k]) glyphs[k].textContent = value < 0 ? '−' : (value > 0 ? '+' : '');
      }
    }
  }

  function renderSummary(state) {
    var frame = pack(state.source).frames[state.frame];
    var s = frame.delta_summary;
    var box = q('#delta-summary');
    if (box) box.setAttribute('data-status', s.status);
    var join = 'time_bucket=' + frame.time_bucket;
    setMetric('delta_net', s.net, signed(s.net), join);
    setMetric('improved_cells', s.improved_cells, String(s.improved_cells), join);
    setMetric('worsened_cells', s.worsened_cells, String(s.worsened_cells), join);
    setMetric('unchanged_cells', s.unchanged_cells, String(s.unchanged_cells), join);
  }

  function setMetric(name, value, display, joinKey) {
    var nodes = qa('[data-metric="' + name + '"]');
    for (var i = 0; i < nodes.length; i++) {
      nodes[i].setAttribute('data-value', String(value));
      if (joinKey) nodes[i].setAttribute('data-join-key', joinKey);
      if (display !== undefined && nodes[i].classList.contains('metric')) nodes[i].textContent = display;
    }
  }

  function renderFrameLabels(state) {
    var frame = pack(state.source).frames[state.frame];
    var label = q('#frame-label');
    if (label) {
      label.textContent = String(state.frame + 1);
      label.setAttribute('data-value', String(state.frame + 1));
      label.setAttribute('data-join-key', 'row ' + (state.frame + 1));
    }
    var bucket = q('#bucket-label');
    if (bucket) {
      bucket.textContent = frame.time_bucket;
      bucket.setAttribute('data-value', frame.time_bucket);
      bucket.setAttribute('data-join-key', 'time_bucket=' + frame.time_bucket);
    }
    var scrub = q('[data-control="scrub"]');
    if (scrub) scrub.value = String(state.frame);
    var playState = q('#play-state');
    if (playState) {
      playState.textContent = state.playing ? '播放中' : '已暂停';
      playState.setAttribute('data-playing', state.playing ? 'true' : 'false');
    }
    var playBtn = q('[data-control="play"]');
    if (playBtn) {
      playBtn.textContent = state.playing ? '暂停' : '播放';
      playBtn.setAttribute('data-state', state.playing ? 'playing' : 'paused');
    }
  }

  function renderState(state) {
    var chapter = state.chapter;
    var src = q('#state-source');
    if (src) { src.textContent = state.source.toUpperCase(); src.setAttribute('data-value', state.source); }
    var mode = q('#state-mode');
    if (mode) {
      mode.textContent = state.mode === 'delta' ? '未满足请求差分' : '并排对照';
      mode.setAttribute('data-value', state.mode);
    }
    var cell = q('#state-cell');
    if (cell) {
      cell.textContent = state.cell || '未选择';
      cell.setAttribute('data-value', state.cell || '');
    }
    var cellSelect = q('[data-cell-select]');
    if (cellSelect && state.cell && cellSelect.value !== state.cell) cellSelect.value = state.cell;
    var scenario = q('#state-scenario');
    if (scenario) { scenario.textContent = state.scenario; scenario.setAttribute('data-value', state.scenario); }
    qa('[data-mode]').forEach(function (button) {
      button.classList.toggle('active', button.getAttribute('data-mode') === state.mode);
    });
    qa('[data-source-switch]').forEach(function (button) {
      button.classList.toggle('active', button.getAttribute('data-source-switch') === state.source);
    });
    var side = q('[data-view="side"]'), deltaView = q('[data-view="delta"]');
    if (side) side.hidden = state.mode !== 'side';
    if (deltaView) deltaView.hidden = state.mode !== 'delta';
    var guideChapter = q('#guide-chapter');
    if (guideChapter) {
      guideChapter.textContent = chapter < 0 ? '未开始' : String(chapter + 1);
      guideChapter.setAttribute('data-value', String(chapter));
    }
    var guideState = q('#guide-state');
    if (guideState) {
      guideState.textContent = state.started ? ('导览进行中 · 第 ' + (state.chapter + 1) + ' 章') : '未开始';
      guideState.setAttribute('data-state', state.started ? 'active' : 'idle');
    }
    var guideTitle = q('#guide-title');
    if (guideTitle) {
      var chapters = pack(state.source).guide.chapters;
      guideTitle.textContent = chapter >= 0 && chapters[chapter] ? chapters[chapter].title : '未开始';
    }
    qa('.chapter').forEach(function (node) {
      var index = parseInt(node.getAttribute('data-chapter'), 10);
      node.setAttribute('data-active', index === state.chapter ? 'true' : 'false');
      node.hidden = state.started ? index !== state.chapter : false;
    });
    qa('[data-guide-action]').forEach(function (button) {
      var action = button.getAttribute('data-guide-action');
      var n = pack(state.source).guide.n_chapters;
      button.disabled = (action === 'prev' && state.chapter <= 0) ||
        (action === 'next' && state.chapter >= n - 1) ||
        ((action === 'prev' || action === 'next') && !state.started);
    });
    var ledgerCell = q('#ledger-cell');
    if (ledgerCell) {
      var chosen = state.cell || pack(state.source).ledger_top_cell.cell_id;
      ledgerCell.textContent = chosen;
      ledgerCell.setAttribute('data-value', chosen);
    }
    qa('[data-scenario]').forEach(function (button) {
      button.classList.toggle('active', button.getAttribute('data-scenario') === state.scenario);
    });
    qa('.scenario-table tr').forEach(function (row) {
      row.classList.toggle('is-current', row.getAttribute('data-scenario') === state.scenario);
    });
    var scope = q('#scenario-scope');
    if (scope) {
      scope.textContent = '当前显示：baseline 回放地图；场景 ' + state.scenario +
        ' 只更新上表数字，不改写地图数据。';
    }
  }

  function renderLedger(state) {
    var data = pack(state.source);
    var cell = state.cell || data.ledger_top_cell.cell_id;
    var index = data.cell_ids.indexOf(cell);
    if (index < 0) return;
    var frame = data.frames[state.frame];
    ['rule', 'optimized'].forEach(function (strategy) {
      var block = frame.strategies[strategy];
      var incoming = Number(block.moved_in[String(index)] || 0);
      var out = block.ledger.out[index];
      var start = block.ledger.start[index];
      var rapp = block.ledger.rapp[index];
      var met = block.ledger.met[index];
      var values = {
        inventory_start: start,
        moved_in: incoming,
        moved_out: out,
        scheduled_inventory: start + incoming - out,
        returns: block.ledger.ret[index],
        returns_applied: rapp,
        returns_overflow: block.ledger.ret[index] - rapp,
        borrow_requests: block.ledger.req[index],
        borrow_met: met,
        unmet_requests: block.ledger.req[index] - met,
        inventory_end: start + incoming - out + rapp - met
      };
      // The evidence trail has to describe the numbers on screen, so it is
      // rewritten from the same state that rewrites the numbers.
      var join = 'cell_id=' + cell + ';time_bucket=' + frame.time_bucket;
      var operands = {
        scheduled_inventory: { inventory_start: start, moved_in: incoming, moved_out: out },
        returns_overflow: { returns: block.ledger.ret[index], returns_applied: rapp },
        unmet_requests: { borrow_requests: block.ledger.req[index], borrow_met: met },
        inventory_end: {
          inventory_start: start, moved_in: incoming, moved_out: out,
          returns_applied: rapp, borrow_met: met
        }
      };
      var pane = q('[data-ledger-strategy="' + strategy + '"]');
      if (!pane) return;
      for (var name in values) {
        if (!values.hasOwnProperty(name)) continue;
        var node = pane.querySelector('[data-metric="' + name + '"]');
        if (!node) continue;
        node.setAttribute('data-value', String(values[name]));
        node.setAttribute('data-join-key', join);
        if (operands[name]) node.setAttribute('data-operands', JSON.stringify(operands[name]));
        else node.removeAttribute('data-operands');
        node.textContent = fmtInt(values[name]);
      }
      var moveList = pane.querySelector('.cell-moves');
      if (moveList) {
        while (moveList.firstChild) moveList.removeChild(moveList.firstChild);
        var records = replay(state.source).frames[state.frame].moves[strategy] || [];
        var listed = 0;
        for (var m = 0; m < records.length; m++) {
          var move = records[m];
          if (move.from_cell !== cell && move.to_cell !== cell) continue;
          var li = document.createElement('li');
          li.className = 'move-row';
          li.setAttribute('data-direction', move.to_cell === cell ? 'in' : 'out');
          li.setAttribute('data-from', move.from_cell);
          li.setAttribute('data-to', move.to_cell);
          li.setAttribute('data-bikes', move.bikes);
          li.setAttribute('data-source', 'outputs/sim_moves.' + strategy + '.' + state.source + '.json');
          li.textContent = move.from_cell + ' → ' + move.to_cell + '：' + move.bikes + ' 辆（' +
            Math.round(move.distance_m) + ' m）';
          moveList.appendChild(li);
          listed++;
        }
        if (!listed) {
          var emptyMove = document.createElement('li');
          emptyMove.className = 'empty';
          emptyMove.textContent = '本帧没有涉及该格的搬运记录。';
          moveList.appendChild(emptyMove);
        }
      }
      var body = pane.querySelector('.history-table tbody');
      if (body) {
        while (body.firstChild) body.removeChild(body.firstChild);
        for (var f = 0; f < data.frames.length; f++) {
          var row = data.frames[f];
          var cellBlock = row.strategies[strategy];
          var rowIn = Number(cellBlock.moved_in[String(index)] || 0);
          var tr = document.createElement('tr');
          tr.setAttribute('data-frame-index', f);
          tr.setAttribute('data-current', f === state.frame ? 'true' : 'false');
          var cells = [
            row.time_bucket,
            cellBlock.ledger.start[index],
            rowIn,
            cellBlock.ledger.out[index],
            cellBlock.ledger.start[index] + rowIn - cellBlock.ledger.out[index] +
              cellBlock.ledger.rapp[index] - cellBlock.ledger.met[index],
            cellBlock.ledger.req[index] - cellBlock.ledger.met[index]
          ];
          for (var c = 0; c < cells.length; c++) {
            var td = document.createElement('td');
            td.textContent = typeof cells[c] === 'number' ? fmtInt(cells[c]) : cells[c];
            tr.appendChild(td);
          }
          body.appendChild(tr);
        }
        var empty = pane.querySelector('.history-empty');
        if (empty) empty.hidden = true;
      }
    });
  }

  function renderAll() {
    var state = core.get();
    renderGrids(state);
    renderSummary(state);
    renderFrameLabels(state);
    renderState(state);
    renderLedger(state);
    if (window.__WORKBENCH_RENDER_COUNT__ !== undefined) window.__WORKBENCH_RENDER_COUNT__++;
  }

  function stop() { if (timer) { clearTimeout(timer); timer = null; } }

  function schedule() {
    stop();
    var state = core.get();
    if (!state.playing) return;
    var ms = parseInt((q('.controls') || {}).getAttribute ? q('.controls').getAttribute('data-step-ms') : '', 10) || 900;
    timer = setTimeout(function () {
      var now = core.get();
      if (!now.playing) return;
      if (now.frame >= pack(now.source).n_frames - 1) {
        core.setPlaying(false);
        renderAll();
        return;
      }
      core.setFrame(now.frame + 1);
      renderAll();
      schedule();
    }, ms);
  }

  function act(fn) { var ok = fn(); renderAll(); if (!core.get().playing) stop(); else schedule(); return ok; }

  qa('[data-guide-action]').forEach(function (button) {
    button.addEventListener('click', function () {
      var action = button.getAttribute('data-guide-action');
      if (action === 'start') act(function () { return core.start(); });
      else if (action === 'next') act(function () { return core.next(); });
      else if (action === 'prev') act(function () { return core.prev(); });
      else if (action === 'restart') act(function () { return core.restart(); });
      else if (action === 'exit') act(function () { return core.exit(); });
      var chapter = q('.chapter[data-active="true"]');
      if (chapter) chapter.scrollIntoView({ block: 'nearest' });
    });
  });

  qa('[data-event]').forEach(function (button) {
    button.addEventListener('click', function () {
      act(function () { return core.jumpToEvent(button.getAttribute('data-event')); });
      var maps = q('[data-maps]');
      if (maps) maps.scrollIntoView({ block: 'start' });
    });
  });

  qa('[data-mode]').forEach(function (button) {
    button.addEventListener('click', function () {
      act(function () { return core.setMode(button.getAttribute('data-mode')); });
    });
  });

  qa('[data-scenario]').forEach(function (button) {
    button.addEventListener('click', function () {
      act(function () { return core.setScenario(button.getAttribute('data-scenario')); });
    });
  });

  function selectCell(cellId) {
    act(function () { return core.selectCell(cellId); });
    var ledger = q('#ledger');
    if (ledger) ledger.scrollIntoView({ block: 'nearest' });
  }

  document.addEventListener('click', function (event) {
    var target = event.target;
    while (target && target !== document.body && !target.getAttribute('data-cell')) target = target.parentNode;
    if (target && target.getAttribute && target.getAttribute('data-cell')) selectCell(target.getAttribute('data-cell'));
  });

  document.addEventListener('keydown', function (event) {
    var tag = (event.target && event.target.tagName || '').toLowerCase();
    var onCell = event.target && event.target.getAttribute && event.target.getAttribute('data-cell');
    if (onCell && (event.key === 'Enter' || event.key === ' ')) {
      event.preventDefault();
      selectCell(onCell);
      return;
    }
    if (tag === 'input' || tag === 'select' || tag === 'textarea' || tag === 'button') return;
    if (event.key === 'ArrowLeft') { event.preventDefault(); act(function () { return core.setFrame(core.get().frame - 1); }); }
    else if (event.key === 'ArrowRight') { event.preventDefault(); act(function () { return core.setFrame(core.get().frame + 1); }); }
    else if (event.key === 'Escape' && core.get().started) { act(function () { return core.exit(); }); }
  });

  var playBtn = q('[data-control="play"]');
  if (playBtn) playBtn.addEventListener('click', function () { act(function () { return core.setPlaying(!core.get().playing); }); });
  var prevBtn = q('[data-control="prev"]');
  if (prevBtn) prevBtn.addEventListener('click', function () {
    act(function () { core.setPlaying(false); return core.setFrame(core.get().frame - 1); });
  });
  var nextBtn = q('[data-control="next"]');
  if (nextBtn) nextBtn.addEventListener('click', function () {
    act(function () { core.setPlaying(false); return core.setFrame(core.get().frame + 1); });
  });
  var scrub = q('[data-control="scrub"]');
  if (scrub) scrub.addEventListener('input', function () {
    act(function () { core.setPlaying(false); return core.setFrame(parseInt(scrub.value, 10)); });
  });

  // A select is the keyboard path to cell selection: a grid with 255 focusable
  // rects would be 255 tab stops, which is not usable navigation.
  var cellSelect = q('[data-cell-select]');
  if (cellSelect) cellSelect.addEventListener('change', function () {
    act(function () { return core.selectCell(cellSelect.value); });
  });

  var reduced = false;
  try { reduced = !!(window.matchMedia && window.matchMedia('(prefers-reduced-motion: reduce)').matches); } catch (e) { reduced = false; }
  if (reduced) core.setPlaying(false);

  renderAll();
  window.__WORKBENCH_READY__ = true;
})();
"""
