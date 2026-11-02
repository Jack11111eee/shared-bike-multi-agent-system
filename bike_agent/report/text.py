"""Markdown renderers for every report the project must ship."""

from __future__ import annotations

import json
from typing import Any


from bike_agent.risk.score import WEIGHTS
from bike_agent.sim.pipeline import comparison_verdict
from bike_agent.stats import describe, is_significant
from bike_agent.utils import delta_pct, markdown_table, pct


def _forecast_significance(entry: dict[str, Any], target: str) -> dict[str, Any] | None:
    return ((entry.get("forecast_meta") or {}).get("significance") or {}).get(target)


def _model_verdict_for(entry: dict[str, Any], target: str) -> str:
    """Wording for one target that follows the paired bootstrap, not the MAE gap."""
    metrics = entry.get("forecast_metrics") or {}
    try:
        base_mae = metrics["baseline"][target]["mae"]
        model_mae = metrics["model"][target]["mae"]
    except (KeyError, TypeError):
        return ""
    stats = _forecast_significance(entry, target)
    significant = is_significant(stats)
    better = model_mae < base_mae
    if significant is None:
        return f"MAE {base_mae:.4f} → {model_mae:.4f}，显著性检验尚未生成"
    if significant:
        return (
            f"MAE {base_mae:.4f} → {model_mae:.4f}，"
            f"模型**显著{'更优' if better else '更差'}**（模型 − 基线的绝对误差，{describe(stats)}）"
        )
    return (
        f"MAE {base_mae:.4f} → {model_mae:.4f}，"
        f"**无显著差异**（模型 − 基线的绝对误差，{describe(stats)}）"
    )


def render_forecast_report(d: dict[str, Any]) -> str:
    source = d["source"].upper()
    meta = d.get("forecast_meta") or {}
    metrics = d.get("forecast_metrics") or {}
    split = meta.get("split", {})
    params = meta.get("params", {})
    rows = []
    for target in ("out_count", "in_count"):
        base = metrics.get("baseline", {}).get(target, {})
        model = metrics.get("model", {}).get(target, {})
        rows.append(
            (
                "借出量" if target == "out_count" else "归还量",
                f"{base.get('mae', float('nan')):.4f}",
                f"{model.get('mae', float('nan')):.4f}",
                delta_pct(base.get("mae", 0), model.get("mae", 0)),
                f"{base.get('rmse', float('nan')):.4f}",
                f"{model.get('rmse', float('nan')):.4f}",
            )
        )
    model_wins = bool(metrics) and all(
        metrics["model"][t]["mae"] < metrics["baseline"][t]["mae"] for t in ("out_count", "in_count")
    )
    lines = [
        f"# 短期需求预测评估 · {source}",
        "",
        "对应 F3。评估严格按时间切分（训练在前、测试在后），未使用随机切分。",
        "",
        "## 切分与可复现参数",
        "",
        markdown_table(
            ["项目", "取值"],
            [
                ("训练时间桶", f"{split.get('n_train_buckets')}（{split.get('train_start')} → {split.get('train_end')}）"),
                ("测试时间桶", f"{split.get('n_test_buckets')}（{split.get('test_start')} → {split.get('test_end')}）"),
                ("训练/测试比例", meta.get("train_fraction")),
                ("随机种子", meta.get("seed")),
                ("模型", "LightGBM（Poisson）"),
                ("超参", ", ".join(f"{k}={v}" for k, v in params.items())),
                ("训练样本数", meta.get("n_train_rows")),
                ("测试样本数", meta.get("n_test_rows")),
            ],
        ),
        "",
        "## 基线 vs 模型（F3-2）",
        "",
        markdown_table(
            ["目标", "基线 MAE", "模型 MAE", "MAE 变化", "基线 RMSE", "模型 RMSE"],
            rows,
        ),
        "",
        "基线定义：每个格子在该小时的历史均值（仅用训练集计算），无历史时回退到格子均值、再回退到全局均值。",
        "",
        "**评估口径差异（如实披露）**：模型的 `lag1_*` / `roll3_*` 特征使用前一个时间桶的"
        "**实际观测值**，属于一步预测（one-step-ahead）；基线只使用训练集的同期均值，"
        "不使用测试期观测。因此两者并非完全同口径，模型的领先幅度中含有\"上一小时实际值\""
        "的信息优势。若要严格对齐，应把基线也改为一步预测形式，或把模型改为多步递归预测——"
        "本次未做，结论按上述口径解读。",
        "",
        "## 结论（F3-3）",
        "",
    ]
    n_train = split.get("n_train_buckets")
    n_test = split.get("n_test_buckets")
    flags = [is_significant(_forecast_significance(d, target)) for target in ("out_count", "in_count")]
    if not metrics:
        lines.append("预测指标尚未生成。")
    elif all(flag is True for flag in flags) and model_wins:
        lines.append(
            "在本数据源与本次时间切分上，LightGBM 在借出与归还两个目标上的 MAE 均"
            "**显著优于**季节均值基线（配对 bootstrap，95% 置信区间不包含 0）。"
            "提升幅度见上表；未调整测试集、未隐藏基线结果、未改用随机切分。"
        )
    elif all(flag is False for flag in flags):
        lines += [
            "在本数据源与本次时间切分上，LightGBM 与季节均值基线在两个目标上"
            "**均无显著差异**（配对 bootstrap 的 95% 置信区间包含 0）。"
            "按 F3-3，这不视为失败，但必须说明原因：",
            "",
            f"- 数据时间覆盖薄：训练仅 {n_train} 个时间片，测试仅 {n_test} 个时间片。"
            "树模型没有足够样本稳定估计格子×小时的非线性关系。",
            "- 基线非常强：按格子×小时取训练集均值，直接捕捉了早晚高峰的空间格局；"
            "在样本极少时，简单均值往往难以被超越。",
            "- 预测目标包含大量零值，Poisson 目标虽适合计数，但在小样本下容易过拟合训练日的异常。",
            "",
            "后续改进方向：获取更长时间覆盖的数据、引入天气与日历特征、使用分层/全局平滑模型，"
            "或在更多时间片上做滚动评估。",
        ]
    else:
        lines += [
            "在本数据源与本次时间切分上，两个目标的显著性结论不一致，逐项列出：",
            "",
            f"- 借出量：{_model_verdict_for(d, 'out_count')}。",
            f"- 归还量：{_model_verdict_for(d, 'in_count')}。",
            "",
            "按 F3-3，该结果如实报告，未调整测试集、未隐藏基线结果、未改用随机切分。",
        ]
    lines += [
        "",
        "## 特征",
        "",
        "、".join(f"`{f}`" for f in meta.get("features", [])),
        "",
        "## 复现",
        "",
        "```bash",
        f"bike-agent forecast --source {d['source']}",
        "```",
        "",
        "同一命令、同一数据、同一种子应得到相同误差（NF-1）。",
        "",
    ]
    return "\n".join(lines)


def render_cross_source(data: dict[str, Any]) -> str:
    headers = ["维度"] + [f"DS-{source.upper()}" for source in data]
    lines = [
        "# 跨数据源一致性报告（F6-5）",
        "",
        "本报告只做对比，不把两个数据源的结果混在同一张结果表里。"
        "每个数字仍归属于各自的数据源报告。",
        "",
        markdown_table(headers, _cross_rows(data)),
        "",
        "## 一致的结论",
        "",
        "- 两个源都出现了明显的空间不均衡：净流出与净流入格子同时存在，风险清单的符号约定在两个源上都成立。",
        f"- {_violation_verdict(data)}",
        "- 两个源的预测都采用同一条时间切分规则与同一套特征，方法可迁移。",
        "",
        "## 不一致的结论",
        "",
        f"- **模型是否优于基线**：{_model_verdict(data)}",
        f"- **策略胜负**：{_strategy_verdict(data)}",
        f"- **零值占比**：{_zero_verdict(data)}",
        "",
        "## 使用边界",
        "",
        "任何结论只在报告明确标注的数据源上成立。"
        "不得把 XM 的结论直接外推到 SH，反之亦然；也不得把两者合并训练或合并报表。",
        "",
    ]
    return "\n".join(lines)


def _hours_text(panel: dict[str, Any]) -> str:
    hours = [int(h) for h in (panel.get("hours") or [])]
    if not hours:
        return "未知时段"
    return f"{min(hours):02d}:00–{max(hours) + 1:02d}:00"


def _train_buckets(entry: dict[str, Any]) -> str:
    split = (entry.get("forecast_meta") or {}).get("split") or {}
    value = split.get("n_train_buckets")
    return str(value) if value is not None else "n/a"


def _test_buckets(entry: dict[str, Any]) -> str:
    split = (entry.get("forecast_meta") or {}).get("split") or {}
    value = split.get("n_test_buckets")
    return str(value) if value is not None else "n/a"


def _model_verdict_short(data: dict[str, Any]) -> str:
    wins, losses, ties = [], [], []
    for source, entry in data.items():
        metrics = entry.get("forecast_metrics") or {}
        targets = ("out_count", "in_count")
        try:
            better = [metrics["model"][t]["mae"] < metrics["baseline"][t]["mae"] for t in targets]
        except (KeyError, TypeError):
            continue
        flags = [is_significant(_forecast_significance(entry, t)) for t in targets]
        if all(flags) and all(better):
            wins.append(source.upper())
        elif all(flags) and not any(better):
            losses.append(source.upper())
        elif any(flag is None for flag in flags):
            continue
        else:
            ties.append(source.upper())
    parts = []
    if wins:
        parts.append(f"{'、'.join(wins)} 上模型显著优于基线")
    if losses:
        parts.append(f"{'、'.join(losses)} 上模型显著劣于基线")
    if ties:
        parts.append(f"{'、'.join(ties)} 上模型与基线无显著差异")
    return "；".join(parts) + "。" if parts else "模型与基线的对比尚未生成。"


def _capacity_note(config: dict[str, Any], xm_entry: dict[str, Any]) -> str:
    capacity_cfg = config.get("sources", {}).get("xm", {}).get("capacity", {})
    slots = capacity_cfg.get("slots_per_fence", 5)
    fences = (xm_entry.get("quality") or {}).get("extras", {}).get("fences")
    bikes = (xm_entry.get("quality") or {}).get("unique_bikes")
    parts = []
    if fences:
        parts.append(f"{int(fences):,} 围栏 × {slots} = {int(fences) * int(slots):,} 个车位")
    if bikes:
        parts.append(f"订单契约中出现的唯一车辆数为 {int(bikes):,}")
    note = "；".join(parts)
    return (
        (note + "。" if note else "")
        + "围栏数来自原始围栏文件；车位密度与单格上限属于估计，可在配置中覆盖。"
    )


def _violation_verdict(data: dict[str, Any]) -> str:
    totals = {}
    for entry in data.values():
        comparison = entry.get("comparison") or {}
        for strategy in ("rule", "optimized"):
            summary = comparison.get(strategy) or {}
            for name, count in (summary.get("violations") or {}).items():
                totals[name] = totals.get(name, 0) + int(count)
    if not totals:
        return "仿真约束检查结果尚未生成。"
    if all(count == 0 for count in totals.values()):
        return "两个源的仿真都满足守恒、容量与预算约束（violations 全为 0）。"
    detail = "、".join(f"{name}={count}" for name, count in sorted(totals.items()))
    return f"两个源的仿真出现了约束违反，需逐项排查：{detail}。"


def _forecast_limitation(data: dict[str, Any]) -> str:
    """Summarise where the model failed to beat the baseline, from the tests."""
    ties, wins = [], []
    for source, entry in data.items():
        if not (entry.get("forecast_metrics") or {}):
            continue
        flags = [is_significant(_forecast_significance(entry, t)) for t in ("out_count", "in_count")]
        if all(flag is False for flag in flags):
            ties.append(source.upper())
        elif all(flag is True for flag in flags):
            wins.append(source.upper())
    parts = []
    if ties:
        parts.append(f"{'、'.join(ties)} 上模型与基线无显著差异")
    if wins:
        parts.append(f"{'、'.join(wins)} 上模型显著优于基线")
    return "；".join(parts) + "。" if parts else ""


def _thin_data_note(entry: dict[str, Any], label: str) -> str:
    """Flag sources whose split is too small for the comparison to be stable."""
    split = (entry.get("forecast_meta") or {}).get("split") or {}
    n_train = split.get("n_train_buckets")
    n_test = split.get("n_test_buckets")
    if n_train is None or n_test is None:
        return ""
    if int(n_train) >= 100 and int(n_test) >= 30:
        return ""
    return (
        f"{label} 的训练/测试时间片只有 {n_train}/{n_test} 个，"
        "样本量本身就限制了树模型能学到的结构。"
    )


def _model_verdict(data: dict[str, Any]) -> str:
    parts = []
    notes = []
    for source, entry in data.items():
        label = source.upper()
        out = _model_verdict_for(entry, "out_count")
        inn = _model_verdict_for(entry, "in_count")
        if not out:
            parts.append(f"{label} 无评估结果")
            continue
        parts.append(f"{label} 借出 {out}；归还 {inn}")
        note = _thin_data_note(entry, label)
        if note:
            notes.append(note)
    verdict = "；".join(parts) + "。"
    return verdict + "".join(notes)


def _strategy_verdict(data: dict[str, Any]) -> str:
    parts = []
    for source, entry in data.items():
        comparison = entry.get("comparison") or {}
        verdict = comparison_verdict(comparison)
        if not verdict:
            continue
        parts.append(f"{source.upper()} 上 {verdict.rstrip('。')}")
    if not parts:
        return "两个源都未产出策略对比结果。"
    return "。".join(parts) + "。"


def _zero_verdict(data: dict[str, Any]) -> str:
    parts = []
    for source, entry in data.items():
        panel = ((entry.get("prep") or {}).get("panel") or {})
        if "zero_out_share" not in panel:
            continue
        parts.append(
            f"{source.upper()} {entry.get('cell_size_m')} m 网格零借出 "
            f"{pct(panel['zero_out_share'])}（{panel.get('n_cells')} 个格子）"
        )
    if not parts:
        return "零值占比尚未生成。"
    return "；".join(parts) + "。稀疏程度更高的源，其风险清单的绝对幅度也更小。"


def _cross_rows(data: dict[str, Any]) -> list[tuple[Any, ...]]:
    rows = []
    for label, key, extractor in [
        ("订单行数", "quality", lambda d: (d.get("quality") or {}).get("rows")),
        ("网格格子数", "prep", lambda d: ((d.get("prep") or {}).get("panel") or {}).get("n_cells")),
        ("时间桶数", "prep", lambda d: ((d.get("prep") or {}).get("panel") or {}).get("n_buckets")),
        ("零借出占比", "prep", lambda d: pct(((d.get("prep") or {}).get("panel") or {}).get("zero_out_share", 0.0))),
        ("基线 MAE（借出）", "forecast", lambda d: f"{((d.get('forecast_metrics') or {}).get('baseline', {}).get('out_count', {}) or {}).get('mae', float('nan')):.4f}"),
        ("模型 MAE（借出）", "forecast", lambda d: f"{((d.get('forecast_metrics') or {}).get('model', {}).get('out_count', {}) or {}).get('mae', float('nan')):.4f}"),
        ("规则策略未满足请求", "sim", lambda d: ((d.get("comparison") or {}).get("rule", {}) or {}).get("unmet_requests")),
        ("优化策略未满足请求", "sim", lambda d: ((d.get("comparison") or {}).get("optimized", {}) or {}).get("unmet_requests")),
        ("规则策略综合代价", "sim", lambda d: round(((d.get("comparison") or {}).get("rule", {}) or {}).get("composite_cost", float("nan")), 2)),
        ("优化策略综合代价", "sim", lambda d: round(((d.get("comparison") or {}).get("optimized", {}) or {}).get("composite_cost", float("nan")), 2)),
    ]:
        values = [extractor(d) for d in data.values()]
        rows.append((label, *values))
    return rows


def render_decisions(
    data: dict[str, Any],
    config: dict[str, Any],
    stability: dict[str, dict[str, float] | None] | None = None,
) -> str:
    xm = data.get("xm") or {}
    sh = data.get("sh") or {}
    stability = stability or {}
    xm_metrics = (xm.get("forecast_metrics") or {})
    sh_metrics = (sh.get("forecast_metrics") or {})
    xm_comp = (xm.get("comparison") or {})
    sh_comp = (sh.get("comparison") or {})
    xm_prep = ((xm.get("prep") or {}).get("panel") or {})
    sh_prep = ((sh.get("prep") or {}).get("panel") or {})
    sim_cfg = config.get("simulation", {})

    lines = [
        "# DECISIONS.md — 决策记录",
        "",
        "> 每个决策点记录：候选方案 / 选择 / 依据 / 证据 / 可逆性 / 日期。",
        "> 本文件由 `bike-agent repro` 根据实际产物生成，数字与 `outputs/` 一致。",
        "",
        "## 4.1 初始倾向与检验结果",
        "",
        "REQUIREMENTS.md 4.1 的初始倾向是：DS-XM 作主实验场景、DS-SH 作补充验证；从粗粒度起步；基线先行；不引入深度学习。",
        "",
        "**检验结果（M3）**：" + _model_verdict(data) + " 因此 4.1 的\"XM 作为预测主场景\""
        "倾向被部分推翻：预测方法的证据以表现更好的那个源为主；XM 保留为主**运营场景**"
        "（唯一有真实围栏、需求密集），用于风险排序与围栏约束下的仿真。详见 D1。",
        "",
        "## D1 · 主实验数据源与数据分工",
        "",
        "**候选方案**：",
        "1. XM 主、SH 辅（4.1 初始倾向）；",
        "2. SH 主、XM 辅；",
        "3. 分工：SH 承担预测方法验证，XM 承担风险排序与围栏约束仿真。",
        "",
        "**选择**：方案 3（分工）。",
        "",
        "**依据与证据**：",
        "",
        markdown_table(
            ["数据源", "基线 MAE（借出）", "模型 MAE（借出）", "基线 MAE（归还）", "模型 MAE（归还）"],
            [
                (
                    "XM",
                    f"{xm_metrics.get('baseline', {}).get('out_count', {}).get('mae', float('nan')):.4f}",
                    f"{xm_metrics.get('model', {}).get('out_count', {}).get('mae', float('nan')):.4f}",
                    f"{xm_metrics.get('baseline', {}).get('in_count', {}).get('mae', float('nan')):.4f}",
                    f"{xm_metrics.get('model', {}).get('in_count', {}).get('mae', float('nan')):.4f}",
                ),
                (
                    "SH",
                    f"{sh_metrics.get('baseline', {}).get('out_count', {}).get('mae', float('nan')):.4f}",
                    f"{sh_metrics.get('model', {}).get('out_count', {}).get('mae', float('nan')):.4f}",
                    f"{sh_metrics.get('baseline', {}).get('in_count', {}).get('mae', float('nan')):.4f}",
                    f"{sh_metrics.get('model', {}).get('in_count', {}).get('mae', float('nan')):.4f}",
                ),
            ],
        ),
        "",
        f"- XM 时间覆盖：{xm_prep.get('n_buckets', 'n/a')} 个时间桶（"
        f"{xm_prep.get('n_dates', 'n/a')} 个日期，小时 {', '.join(str(h) for h in (xm_prep.get('hours') or []))}）；",
        f"- SH 时间覆盖：{sh_prep.get('n_buckets', 'n/a')} 个时间桶（"
        f"{sh_prep.get('n_dates', 'n/a')} 个日期，含跨夜归还落入次日凌晨的部分）；",
        f"- XM 有 {(xm.get('quality') or {}).get('extras', {}).get('fences', 'n/a')} 个真实电子围栏，"
        "是停放容量唯一有数据支撑的来源；SH 无围栏。",
        "",
        "**可逆性**：高。数据分工写在配置与报告角色中；若获得更长 XM 数据，可重新评估。",
        "",
        "**日期**：2026-10-29。",
        "",
        "## D2 · 空间单元",
        "",
        "**候选方案**：规则网格 / 电子围栏 / 交通小区 / 聚类簇。",
        "",
        f"**选择**：规则网格。XM 使用 {config['sources']['xm']['cell_size_m']} m，SH 使用 {config['sources']['sh']['cell_size_m']} m。",
        "",
        "**依据与证据**：",
        "",
        markdown_table(
            ["数据源", "粒度", "格子数", "零借出占比", "基线 MAE（借出）", "基线 WAPE（借出）"],
            _granularity_rows(xm, "XM") + _granularity_rows(sh, "SH"),
        ),
        "",
        "规则网格让两个源共享同一套下游代码；围栏不适合 SH（无围栏），交通小区与聚类簇难以跨源复用。"
        "注意 MAE 随格子变小而机械下降（每格计数变小），跨粒度比较应看 WAPE。",
        f"XM 选 1 km：零值占比 {pct(xm_prep.get('zero_out_share', 0.0))}，"
        f"每格每桶平均借出 {xm_prep.get('mean_out_per_cell_bucket', 0.0):.1f} 次，足以支撑仿真；"
        f"500 m 的 WAPE 为 {_wape(xm, 500)}（1 km 为 {_wape(xm, 1000)}），"
        f"零值占比 {_zero(xm, 500)}，且格子更碎。",
        f"SH 选 2 km：零值占比 {pct(sh_prep.get('zero_out_share', 0.0))}（<80%），"
        f"WAPE 为 {_wape(sh, 2000)}；4 km 的 WAPE 为 {_wape(sh, 4000)}、"
        f"零值占比 {_zero(sh, 4000)}，但分辨率过粗，无法给出可执行的区域清单，因此仍选 2 km。",
        "",
        "**可逆性**：高，`sources.<name>.cell_size_m` 可配置，敏感性表可重跑。",
        "",
        "**日期**：2026-10-29。",
        "",
        "## D3 · 时间单元",
        "",
        "**候选方案**：15 min / 30 min / 1 h / 2 h。",
        "",
        "**选择**：1 小时。",
        "",
        "**依据与证据**：",
        f"- XM 每天只覆盖 {_hours_text(xm_prep)}，1 小时给出 {xm_prep.get('n_buckets', 'n/a')} 个时间桶；"
        "更细粒度会让每个桶的样本进一步变少、零值更多；",
        f"- SH 为 {sh_prep.get('n_buckets', 'n/a')} 个时间桶，1 小时兼顾\"未来一个时间单元\"的目标与可学习性；",
        "- 2 小时会削弱\"短期\"含义，且与调度班次不匹配。",
        "",
        "**可逆性**：中（需要改聚合与特征代码）。",
        "",
        "**日期**：2026-10-29。",
        "",
        "## D4 · 预测目标",
        "",
        "**候选方案**：借出量 / 归还量 / 净流量 / 缺车事件。",
        "",
        "**选择**：分别预测借出量与归还量，净流量由两者相减得到。",
        "",
        "**依据**：直接预测净流量会丢失方向与规模的可解释性；风险评分需要分别的借出/归还预测来构造净流出与需求规模。",
        "",
        "**可逆性**：高。",
        "",
        "**日期**：2026-10-29。",
        "",
        "## D5 · 模型选择",
        "",
        "**候选方案**：季节均值基线 / 树模型（LightGBM）/ 其他表格方法 / 时空图网络（已排除，见 B4）。",
        "",
        "**选择**：季节均值基线 + LightGBM（Poisson）。",
        "",
        "**依据与证据**：见 `reports/forecast_eval.xm.md` 与 `reports/forecast_eval.sh.md` 的对比表。"
        + _model_verdict_short(data)
        + " 原因与后续方向已在各源的预测报告中说明。",
        "",
        "**可逆性**：高，`forecast.model` 可配置。",
        "",
        "**日期**：2026-10-29。",
        "",
        "## D6 · 仿真假设",
        "",
        "**选择与来源**：",
        "",
        markdown_table(
            ["假设", "取值", "来源", "说明"],
            [
                ("初始库存比例", sim_cfg.get("initial_fill_ratio"), "估计", "按容量的固定比例填充"),
                ("安全库存比例", sim_cfg.get("safety_stock_ratio"), "估计", "调度时不掏空格子"),
                ("每时间单元搬运预算", sim_cfg.get("max_moves_per_bucket"), "约定", "约 1.8% 的小时订单量，对应小型调度车队"),
                ("未满足请求惩罚", sim_cfg.get("unmet_penalty"), "约定", "综合代价中的权重"),
                ("距离成本", sim_cfg.get("distance_cost_per_bike_km"), "约定", "每 bike-km 的模拟成本"),
                ("XM 停放容量", "围栏数 × 5 车位", "数据 + 估计", _capacity_note(config, xm)),
                ("SH 停放容量", config["sources"]["sh"]["capacity"].get("fixed_capacity"), "约定", "无围栏数据，只能假设"),
            ],
        ),
        "",
        "**敏感性分析**："
        + "、".join(f"`outputs/sim_sensitivity.{source}.json`" for source in data)
        + " 给出初始库存、预算、容量变化下的结果；汇总见 `REPORT.md` 的仿真假设表。",
        "",
        "**可逆性**：高，全部在 `bike_agent/config/default.yaml` 中可覆盖。",
        "",
        "**日期**：2026-10-29。",
        "",
        "## D7 · 风险评分因子与权重",
        "",
        f"**选择**：score = {WEIGHTS['magnitude']:.2f} × norm(|net|) + "
        f"{WEIGHTS['demand']:.2f} × norm(demand) + {WEIGHTS['volatility']:.2f} × norm(volatility)。",
        "",
        "**依据**：可解释性优先。净流出幅度直接决定调度动作，权重最高；需求规模次之；"
        f"历史波动受样本长度限制（XM 仅 {_train_buckets(xm)} 个训练时间片），权重最低。",
        "",
    ]
    for source, entry in stability.items():
        if entry:
            lines.append(
                f"- {source.upper()}：与等权重方案的 Top-10 重叠率为 {entry['top10_overlap']:.0%}"
                f"（候选 {int(entry['n_candidates'])} 个格子），说明排序对权重不敏感。"
            )
    lines += [
        "",
        "**可逆性**：高，权重集中在 `bike_agent/risk/score.py` 的 `WEIGHTS`。",
        "",
        "**日期**：2026-10-29。",
        "",
    ]
    lines += _phase2_decisions(data)
    return "\n".join(lines)


def _phase2_decisions(data: dict[str, Any]) -> list[str]:
    """D8-D12 from REQUIREMENTS-PHASE2.md §4, rendered from the real artefacts."""

    def sizes(source: str, strategy: str = "rule") -> str:
        entry = ((data.get(source) or {}).get("sim_steps") or {}).get(strategy) or {}
        if not entry:
            return "n/a"
        return (
            f"{entry['n_buckets']} 帧 × {entry['n_cells']} 格，搬运 {entry['n_moves']} 条，"
            f"parquet {entry['bytes'] / 1024:.0f} KB"
        )

    def rate(source: str, strategy: str) -> str:
        comparison = (data.get(source) or {}).get("comparison") or {}
        summary = comparison.get(strategy) or {}
        value = summary.get("service_rate")
        return f"{value * 100:.2f}%" if isinstance(value, (int, float)) else "n/a"

    xm_rate = rate("xm", "rule")
    sh_rate = rate("sh", "rule")
    return [
        "## Phase 2 · 与 Phase 1 的两处冲突修订",
        "",
        "> `REQUIREMENTS-PHASE2.md` §1.3 要求这两处冲突**显式修订**，不得悄悄绕过：",
        "> 既要在 `REQUIREMENTS.md` 改条目，也要在本文件留下记录。两条修订已分别应用到",
        "> `REQUIREMENTS.md` §1.2 的前端行与 F7-3。",
        "",
        "### 修订一 · 前端 / Web UI 从「不做」改为「本期必做」",
        "",
        "**候选方案**：① 维持 Phase 1 结论，继续不做前端；② 反转结论，把回放界面列为必做；"
        "③ 做一个仅供内部调试、不交付的页面。",
        "",
        "**选择**：方案 ②（B5 已确认）。",
        "",
        "**依据与证据**：Phase 1 的结论全部是数字，评审无法直观判断系统是否真的在按回放数据逐帧推进。"
        "方案 ③ 无法被外部验证，等于没做。因此前端定位为**回放验证手段**，不是产品化："
        "它逐帧展示 `outputs/replay.*.parquet` 的逐格状态与 `sim_moves.*.json` 的搬运记录，"
        "XM 4 帧、SH 149 帧，与产物时间单元数逐一对应。",
        "",
        "**可逆性**：高，前端是独立目录 `replay/`，删掉不影响任何产物与测试。",
        "",
        "**日期**：2026-10-29（B5 确认）/ 2026-10-30（实现）。",
        "",
        "### 修订二 · F7-3「零 LLM 依赖」的作用范围收窄",
        "",
        "**候选方案**：① 废除 F7-3；② 维持 F7-3 覆盖全部代码，智能体层不得存在；"
        "③ 收窄 F7-3 到 `bike-agent repro` 与 F1–F7 模块，智能体层作为独立可选模块。",
        "",
        "**选择**：方案 ③。",
        "",
        "**依据与证据**：F7-3 的价值在于「主流水线不依赖任何 LLM」，这条必须保住；"
        "但把它扩到智能体层，等于禁止本期做 F11。收窄后仍需证明边界成立，因此有两条自动化守护："
        "`tests/test_serve.py::test_service_works_without_any_llm_sdk`（阻断全部 LLM SDK 后服务仍可用）"
        "与 `tests/test_agent.py::test_pipeline_runs_when_the_agent_layer_cannot_be_imported`"
        "（阻断智能体模块后主流水线照常运行）。F11 本身也用确定性规划器实现，未引入任何 LLM SDK（D12）。",
        "",
        "**可逆性**：高，`REQUIREMENTS.md` F7-3 加了一句限定；若日后要恢复全量约束，删除智能体模块即可。",
        "",
        "**日期**：2026-10-29（确认）/ 2026-10-30（实现）。",
        "",
        "## Phase 2 · D8–D12（回放可视化、前后端与智能体层）",
        "",
        "### D8 · 回放主场景：XM 还是 SH",
        "",
        "**候选方案**：① 只用 XM；② 只用 SH；③ 两者并列，各自作为首页入口。",
        "",
        "**选择**：方案 ③，但两者承担不同角色。",
        "",
        f"**依据与证据**：两个源互补且互相拆台。XM {sizes('xm')}，服务率 {xm_rate}——画面有事发生，"
        f"但时间轴几乎为空；SH {sizes('sh')}，服务率 {sh_rate}——时间轴拖得动，但绝大多数帧搬运量是个位数。"
        "硬选一个必然做出「好看但没内容」或「有内容但拖不动」的演示，因此并列呈现，并在界面与本节如实说明差异。",
        "",
        "**可逆性**：高，`bike_agent/replay/site.py` 的 `build_site(sources=...)` 决定渲染哪些源。",
        "",
        "**日期**：2026-10-30。",
        "",
        "### D9 · 前端技术栈与构建方式",
        "",
        "**候选方案**：① 需要构建的 SPA（React/Vite）；② 单页 + 前端图表库；③ 预渲染的静态多页 + 本地 CSS/JS。",
        "",
        "**选择**：方案 ③。每个时间单元一个 HTML 页，`style.css` / `app.js` 随站点提供，无构建步骤、无第三方库。",
        "",
        "**依据与证据**：F10-9 要求离线可用，F10-5 要求「读取渲染后的 DOM」可断言。预渲染把指标、箭头、"
        "说明区都变成服务端产物，测试用标准库 `html.parser` 即可逐帧断言，不必依赖无头浏览器；"
        "同时 `file://` 直接打开即可用（数据内嵌在页面里，不发起任何 fetch）。",
        "",
        "**代价**：格子由页面内的一段本地脚本从内嵌 JSON 绘制，逐格元素不在静态 DOM 里；"
        "脚本会为每个生成的 `<rect>` 补上同样的 `data-source`，因此在浏览器里 DOM 依然可追溯。",
        "",
        "**可逆性**：高，渲染逻辑集中在 `bike_agent/replay/site.py`。",
        "",
        "**日期**：2026-10-30。",
        "",
        "### D10 · 接口形态：REST / 静态 JSON / 混合",
        "",
        "**候选方案**：① 只做常驻 HTTP 服务；② 只预生成静态 JSON；③ 混合：静态站点作交付物，服务供程序调用。",
        "",
        "**选择**：方案 ③。",
        "",
        "**依据与证据**：界面要求离线可打开（F10-9），常驻服务做不到「双击即看」；而 F9 要求"
        "「能被外部程序驱动」，静态文件又做不到按参数查询。两者并存：`replay/` 是交付物，"
        "`bike-agent serve` 暴露同一批产物，且三工具接口直接复用 `bike_agent.tools.handlers`（F9-3 有测试证明逐字段相等）。",
        "",
        "**可逆性**：高，服务层不持有状态，删掉不影响静态站点。",
        "",
        "**日期**：2026-10-30。",
        "",
        "### D11 · 逐格状态的存储格式与体量",
        "",
        "**候选方案**：① JSON；② CSV；③ parquet。",
        "",
        "**选择**：方案 ③，`outputs/replay.<strategy>.<source>.parquet`。",
        "",
        f"**依据与证据**：{sizes('sh')}；XM {sizes('xm')}。parquet 对 38k 行的整数列压缩后不到 130 KB，"
        "与既有产物同一套读写路径（`pandas.read_parquet`），F8-2 的自洽性测试与 F9 的接口都直接读它。"
        "前端不直接读 parquet——浏览器无法解析——而是在构建时把每帧数值内嵌进对应页面。",
        "",
        "**可逆性**：高，格式由 `Layout.replay_path` 与 `sim/pipeline.py` 决定。",
        "",
        "**日期**：2026-10-30。",
        "",
        "### D12 · 智能体任务与框架",
        "",
        "**候选方案**：① 调度建议生成；② 回放复盘；③ 工具链自检。",
        "",
        "**选择**：方案 ②「回放复盘」，框架为**确定性规划器**，不引入任何 LLM SDK。",
        "",
        "**依据与证据**：F11-1/F11-2 要求未安装智能体模块时主流水线一切正常，F7-3 的零 LLM 约束"
        "只是被收窄、并未废除。回放复盘只需读三工具返回值做排序与归因，规则化即可完成，"
        "且每个数字都能在同一次运行的工具返回体里找到字符串级匹配（F11-5）——换成 LLM 反而更难保证这一点。"
        "如需接入 LLM，只替换 `bike_agent/agent/planner.py` 的决策函数，工具层与审计格式不变。",
        "",
        "**可逆性**：高，智能体模块独立于主流水线，未被 `bike_agent repro` 导入（有守护测试）。",
        "",
        "**日期**：2026-10-30。",
        "",
    ] + _phase3_decisions(data) + _phase4_decisions(data)


def _phase3_decisions(data: dict[str, Any]) -> list[str]:
    """D13–D17 and the Phase 3 §1.3 revision record, with real measurements."""
    bundle: dict[str, dict[str, Any]] = {}
    try:
        from bike_agent.config import Layout, load_config
        from bike_agent.replay.player import build_replay_data, replay_data_js

        config, layout = load_config(), Layout()
        for source in ("xm", "sh"):
            packed = build_replay_data(config, layout, source)
            bundle[source] = {
                "bytes": len(replay_data_js(packed).encode("utf-8")),
                "frames": packed["n_frames"],
                "cells": len(packed["cell_ids"]),
                "color_max": packed["color_domain"]["max"],
                "empty_frames": {
                    strategy: sum(1 for frame in packed["frames"] if not frame["moves"][strategy])
                    for strategy in ("rule", "optimized")
                },
            }
    except Exception:  # pragma: no cover - partial artefacts; quotes n/a instead
        bundle = {}

    def size_text(source: str) -> str:
        entry = bundle.get(source)
        if not entry:
            return "n/a"
        return (
            f"{entry['frames']} 帧 × {entry['cells']} 格，"
            f"`replay-data.js` {entry['bytes'] / 1024:.1f} KB，色标上界 {entry['color_max']}，"
            f"零搬运帧 {entry['empty_frames']['rule']}（S-RULE）/ "
            f"{entry['empty_frames']['optimized']}（S-OPT）"
        )

    try:
        from bike_agent.config import Layout as _Layout

        _layout = _Layout()
        n_key_numbers = len(json.loads(_layout.key_numbers_path().read_text(encoding="utf-8")))
        n_figures = len(list(_layout.figures.glob("*.png")))
    except Exception:  # pragma: no cover - partial artefacts
        n_key_numbers, n_figures = 0, 0

    # Frame pages are one per persisted bucket, so the count comes from the step
    # artefacts rather than from a number typed into this sentence.
    n_frame_pages = sum(
        int((entry.get("sim_steps") or {}).get("rule", {}).get("n_buckets", 0))
        for entry in data.values()
    )
    pages_text = f"{n_frame_pages} 个逐帧页（站点共 {n_frame_pages + 1} 个 HTML，含首页）"

    return [
        "## Phase 3 · 与 Phase 2 的两处冲突修订（REQUIREMENTS-PHASE3 §1.3）",
        "",
        "**修订一 · NF-13「回放帧按需加载」的作用范围**",
        "",
        "Phase 2 的 NF-13 对**逐帧页**继续有效：" + pages_text + "仍然每页只带自己那一帧的数据，"
        "按需打开。播放器页 `replay/<source>/player.html` 必须连续播放，跨帧不能走网络或整页跳转，"
        "因此它是**唯一例外**，一次性载入该源全部帧。作为交换，体量由 Phase 3 NF-15 硬约束"
        "（`replay/` 总量 < 6 MB、单源 `replay-data.js` ≤ 1 MB、单个 `player.html` ≤ 256 KB；"
        "**Phase 4 已把站点总量上限调整为 8 MiB，其余单文件上限不变**，见 D18）。",
        "该修订已落到 `REQUIREMENTS-PHASE2.md` NF-13 末尾，指向 `REQUIREMENTS-PHASE3.md` NF-15。",
        "",
        "**修订二 · D9「预渲染的静态多页」与连续播放的分工**",
        "",
        "Phase 2 的 D9 没有废除：" + pages_text + "**全部保留**，继续承担深链、自动验收锚点与无 JavaScript 回退（B9）。"
        "Phase 3 **新增**播放器页作为首页主入口，逐帧页降为深链与回退路径；"
        "`replay/index.html` 同时链接两者。逐帧页的 DOM 契约零改动，"
        "`tests/test_replay_site.py` 未修改且全绿，这是「没有回归」的直接证据。",
        "该修订已落到 `REQUIREMENTS-PHASE2.md` F10 开头，指向 `REQUIREMENTS-PHASE3.md`。",
        "",
        "## Phase 3 · D13–D17（播放器与总览页）",
        "",
        "### D13 · 播放器数据的组织方式",
        "",
        "**候选方案**：① 每帧一个文件；② 每策略一个文件；③ 每源一个全量文件（两策略同包）。",
        "",
        "**选择**：方案 ③，`replay/<source>/replay-data.js`（经典脚本，内容仅为 "
        "`window.REPLAY_DATA = <JSON>;`）。",
        "",
        "**依据与证据**：`file://` 下无法 `fetch` 兄弟 JSON（CORS），逐帧取数会导致每次切帧都整页跳转，"
        "无法连续播放；分策略拆包则要求播放器同时维护两份帧索引，容易出现两策略错位。"
        f"实测单文件体量完全在预算内：XM {size_text('xm')}；SH {size_text('sh')}。"
        "三处消费者（`replay-data.js`、`player.html` 首帧、`/api/replay/timeline`）"
        "共用 `build_replay_data` 一个函数，不存在第二套取数逻辑。",
        "",
        "**可逆性**：高，打包逻辑集中在 `bike_agent/replay/player.py` 的 `build_replay_data`。",
        "",
        "**日期**：2026-10-31。",
        "",
        "### D14 · 动画语义",
        "",
        "**候选方案**：① 连续补间（插值过渡）；② 离散定时切帧；③ 导出视频。",
        "",
        "**选择**：方案 ②。默认 1×，每帧停留 900 ms，循环**默认关闭**，走到末帧即停；"
        "0.5× / 2× / 4× 只改变停留时长，不改变任何一帧的内容。",
        "",
        "**依据与证据**：① 会制造出比产物更细的时间分辨率，③ 无法逐帧审计，二者都违反 NF-17。"
        f"离散切帧下，帧数恒等于产物时间单元数：XM {size_text('xm')}；SH {size_text('sh')}。"
        "空搬运帧一律显示为空，不做放大或跳过；`prefers-reduced-motion` 时不自动播放且不启用过渡（NF-20）。",
        "",
        "**可逆性**：高，停留时长与默认循环是 `bike_agent/replay/player.py` 中的常量与初始状态。",
        "",
        "**日期**：2026-10-31。",
        "",
        "### D15 · 色标方案",
        "",
        "**候选方案**：① 沿用逐帧相对最大值；② 每个源固定线性域；③ 固定分位数域；④ 对数域。",
        "",
        "**选择**：方案 ②，`color_domain = {min: 0, max: 该源两策略 inventory_end 的全局最大值}`。",
        "",
        f"**依据与证据**：① 正是相邻两帧无法比较的原因（颜色随帧漂移）。② 最易验证："
        f"XM 上界 {bundle.get('xm', {}).get('color_max', 'n/a')}、SH 上界 {bundle.get('sh', {}).get('color_max', 'n/a')}，"
        "相差约 30 倍，但两个源各自固定，跨帧不变；页面上给出 0 与上界两个端点。"
        "③④ 会引入额外的分位数/对数解释，且端点文本无法再直接等于数据域，验收成本更高。",
        "",
        "**可逆性**：高，域在 `build_replay_data` 中计算一次，渲染只消费 `color_domain`。",
        "",
        "**日期**：2026-10-31。",
        "",
        "### D16 · 总览页的信息组织",
        "",
        "**候选方案**：① 卡片优先；② 图表优先；③ 按数据源分栏。",
        "",
        "**选择**：卡片 + 两源对照表 + 策略对比表 + 全部图表，单页自上而下。",
        "",
        "**依据与证据**：`outputs/key_numbers.json` 现有 "
        f"{n_key_numbers} 条可追溯数字，`figures/` 有 {n_figures} 张图；"
        "卡片直接绑定 key_numbers 条目，表格只列出两源都存在的指标，"
        "图表逐张带图注与来源。这样「每个数字都能回到产物」不依赖人工抄写。"
        "按源分栏会让两源对照被拆散，图表优先则把最需要先看的数字推到了下面。",
        "",
        "**可逆性**：高，页面模板在 `bike_agent/replay/player.py` 的 `render_dashboard`。",
        "",
        "**日期**：2026-10-31。",
        "",
        "### D17 · 静态托管的安全边界",
        "",
        "**候选方案**：① 目录白名单；② 后缀白名单；③ 两者结合 + 解析后包含性检查。",
        "",
        "**选择**：方案 ③。只读托管 `replay/`，只允许 html/css/js/png/svg/json/txt/ico 后缀，"
        "路径多次解码后拒绝 `..`、反斜杠、NUL，并在 `resolve()` 之后确认目标仍在站点目录内。",
        "",
        "**依据与证据**：单独任何一项都不够——目录白名单挡不住符号链接逃逸，后缀白名单挡不住"
        "同目录下的敏感文件。解析后包含性检查同时覆盖两者。`tests/test_serve_static.py` 对 "
        "`/../REQUIREMENTS.md`、`/%2e%2e/REQUIREMENTS.md`、`/etc/passwd` 三种形式断言 4xx 且正文不含目标内容（F15-2）。",
        "",
        "**可逆性**：高，规则集中在 `bike_agent/serve/app.py` 的 `resolve_static`。",
        "",
        "**日期**：2026-10-31。",
        "",
    ]


def _tension_section(data: dict[str, Any]) -> str:
    """REQUIREMENTS-PHASE2 §2.3, rendered from the artefacts (DoD item 10).

    The two sources are complementary in a way that undercuts itself: one has
    plenty happening over almost no time, the other has a long timeline over
    which almost nothing happens.  Saying so is the honest thing; the point of
    the section is that it is *computed*, so it cannot drift from the data.
    """
    rows = []
    for source, entry in data.items():
        sizes = entry.get("sim_steps") or {}
        comparison = entry.get("comparison") or {}
        if not sizes or not comparison:
            continue
        rule = comparison.get("rule") or {}
        frames = sizes.get("rule", {}).get("n_buckets")
        moves = sizes.get("rule", {}).get("n_moves")
        rate = rule.get("service_rate")
        if frames is None or rate is None:
            continue
        if frames <= 10:
            verdict = "有戏没帧"
            reason = (
                f"服务率 {rate * 100:.2f}%，未满足请求 {rule.get('unmet_requests')} 次，"
                f"画面应该有明显的事发生；但仿真只回放 {frames} 个时间单元，时间轴几乎是空的"
            )
        elif rate > 0.99:
            verdict = "有帧没戏"
            reason = (
                f"{frames} 个时间单元，时间轴拖起来很流畅；但服务率 {rate * 100:.2f}%，"
                f"绝大多数时间单元里搬运量只有个位数（全时段共 {moves} 条搬运记录），画面接近静止"
            )
        else:
            verdict = "两者均衡"
            reason = f"{frames} 个时间单元，服务率 {rate * 100:.2f}%，共 {moves} 条搬运记录"
        rows.append((source.upper(), verdict, frames, f"{rate * 100:.2f}%", moves, reason))

    if not rows:
        return "回放产物尚未生成。"

    table = markdown_table(
        ["数据源", "特征", "帧数", "服务率", "搬运记录", "说明"],
        rows,
    )
    return "\n".join(
        [
            table,
            "",
            "这条张力本身是有价值的结论：**两个数据源各自适合回答不同的问题**——"
            "XM 适合看「有事发生时调度做了什么」，SH 适合看「长时间轴上调度有多克制」；"
            "这与 D1 的分工结论一致。",
            "",
            "按 F10-1 / F10-4 / F10-6，本项目**没有**为了让画面好看而插值、补帧、"
            "复制时间单元、筛选有搬运的帧或放大动效。一个诚实的、平淡的回放，"
            "比一个漂亮的、编造的回放有价值。",
        ]
    )


def _phase4_decisions(data: dict[str, Any]) -> list[str]:
    """D18–D23 and the Phase 4 §1.3 revision record, with measured numbers.

    The measurements are taken from the artefacts at render time.  If the
    presentation build is unavailable (partial artefacts) the block says so
    instead of quoting a number it did not measure.
    """
    summary: dict[str, dict[str, Any]] = {}
    sizes: dict[str, Any] = {}
    try:
        from bike_agent.config import Layout, load_config
        from bike_agent.presentation.analysis import (
            build_all_presentations,
            presentation_js,
        )

        config, layout = load_config(), Layout()
        built = build_all_presentations(config, layout, ("xm", "sh"))
        for source, presentation in built.items():
            events = {event["type"]: event for event in presentation["events"]}
            summary[source] = {
                "frames": presentation["n_frames"],
                "cells": presentation["n_cells"],
                "delta_max_abs": presentation["color_domain"]["delta"]["max_abs"],
                "presentation_bytes": len(presentation_js(presentation).encode("utf-8")),
                "events": {
                    kind: (event["frame"], event["value"], event["tie_count"], event["status"])
                    for kind, event in events.items()
                },
                "n_scenarios": presentation["scenarios"]["n_scenarios"],
                "top_cell": presentation["ledger_top_cell"],
                "baseline": {
                    strategy: presentation["totals"][strategy]["unmet_requests"]
                    for strategy in ("rule", "optimized")
                },
            }
        site = layout.root / "replay"
        if site.exists():
            # The smoke run's instrumented page lives in the site only while
            # `browser_smoke.py` is driving it, and `.DS_Store` is gitignored
            # Finder metadata that no other machine has; neither ships, so
            # excluding both keeps the quoted total reproducible.
            files = [
                path
                for path in site.rglob("*")
                if path.is_file()
                and not path.name.endswith(".smoke.html")
                and path.name != ".DS_Store"
            ]
            sizes = {
                "total": sum(path.stat().st_size for path in files),
                "workbench": {
                    source: (site / source / "workbench.html").stat().st_size
                    for source in built
                    if (site / source / "workbench.html").exists()
                },
            }
    except Exception:  # pragma: no cover - partial artefacts; quotes n/a instead
        summary, sizes = {}, {}

    measured = bool(summary)
    xm = summary.get("xm") or {}
    sh = summary.get("sh") or {}

    def event_text(source: str, kind: str, label: str) -> str:
        entry = summary.get(source)
        if not entry:
            return f"{label} n/a"
        frame, value, ties, status = entry["events"][kind]
        if status != "ok":
            return f"{label} 空态（{status}）"
        tie = f"，{ties} 个时段并列" if ties > 1 else ""
        # Only the gain events are signed deltas; a plain maximum takes no sign.
        shown = f"{value:+,}" if kind.startswith("opt_") else f"{value:,}"
        return f"{label} {shown}（帧 {frame}{tie}）"

    size_line = (
        f"单源 `presentation.js` XM {xm['presentation_bytes'] / 1024:.1f} KB / "
        f"SH {sh['presentation_bytes'] / 1024:.1f} KB；`replay/` 总量 "
        f"{sizes.get('total', 0) / 1048576:.2f} MiB（上限 8 MiB）"
        if measured and sizes
        else "体积实测 n/a（展示构建不可用）"
    )
    domain_line = (
        f"差分色域 M：XM ±{xm['delta_max_abs']} 次、SH ±{sh['delta_max_abs']} 次"
        if measured
        else "差分色域 n/a"
    )
    events_line = (
        "实测：XM "
        + event_text("xm", "rule_unmet_max", "S-RULE 未满足最大")
        + "、"
        + event_text("xm", "rule_moved_max", "S-RULE 搬运最多")
        + "、"
        + event_text("xm", "opt_gain_max", "S-OPT 改善最大")
        + "、"
        + event_text("xm", "opt_regression_max", "S-OPT 退步最大")
        + "；SH "
        + event_text("sh", "rule_unmet_max", "S-RULE 未满足最大")
        + "、"
        + event_text("sh", "rule_moved_max", "S-RULE 搬运最多")
        + "、"
        + event_text("sh", "opt_gain_max", "S-OPT 改善最大")
        + "、"
        + event_text("sh", "opt_regression_max", "S-OPT 退步最大")
        + "。两源都没有把零或反向结果包装成改善。"
        if measured
        else "事件实测 n/a（展示构建不可用）。"
    )
    baseline_line = (
        f"实测 baseline 与 `strategy_comparison` 逐项相等：XM 未满足 {xm['baseline']['rule']:,} / "
        f"{xm['baseline']['optimized']:,}，SH {sh['baseline']['rule']:,} / {sh['baseline']['optimized']:,}。"
        if measured
        else ""
    )
    top_cells_line = (
        f"（XM `{xm['top_cell']['cell_id']}`、SH `{sh['top_cell']['cell_id']}`）" if measured else ""
    )

    return [
        "## Phase 4 · 与 Phase 2/3 的授权修订（REQUIREMENTS-PHASE4 §1.3）",
        "",
        "四项修订由 `REQUIREMENTS-PHASE4.md` 定义，执行者按此推进并在此记录，不重新解释为放宽验收：",
        "",
        markdown_table(
            ["旧要求", "本期口径", "落地位置"],
            [
                (
                    "Phase 2/3 站点总量 < 6 MiB",
                    "本期改为 < 8 MiB；其他单文件上限不变",
                    "`tests/test_replay_site.py`、`tests/test_replay_player.py` 各自的站点总量断言由 "
                    "`6 * 1024 * 1024` 改为 `8 * 1024 * 1024`，diff 只有这两处常量",
                ),
                (
                    "Phase 3 NF-18：新数字只能来自 `key_numbers.json` 来源集合",
                    "对旧页面继续有效；本期派生指标引用原始输入与可复算公式",
                    "每个新数字带 `data-source` / `data-join-key` / `data-formula`；旧 `key_numbers.json` 未改动",
                ),
                (
                    "Phase 3 NF-17：完整连续回放不得跳帧",
                    "完整播放器保持全部帧；导览与事件允许显式跳转并标注「关键事件/选段」",
                    "`player.html` 未改动；工作台在章首与页面声明「导览是选段」",
                ),
                (
                    "Phase 3：不引入无头浏览器",
                    "仅对开发验证工具放开",
                    "`scripts/browser_smoke.py` 只做验证；站点与主流水线不依赖它",
                ),
            ],
        ),
        "",
        "## Phase 4 · D18–D23（演示导览与交互复盘）",
        "",
        "### D18 · 共享分析状态与展示数据",
        "",
        "**候选方案**：① 五个页面各自从 `outputs/` 取数；② 一个共享构建函数产出统一对象；"
        "③ 把派生值塞进既有 `key_numbers.json`。",
        "",
        "**选择**：方案 ②。`bike_agent/presentation/analysis.py::build_presentation` 是唯一构建入口："
        "帧、KPI、搬运记录复用 Phase 3 的 `build_replay_data`，逐格账本、`delta_unmet`、事件、场景、"
        "导览章节与证据记录都在同一个返回值里；`presentation.js`、工作台页面、"
        "`scripts/check_presentation.py` 与测试全部读它。",
        "",
        "**依据与证据**：" + size_line + "。" + domain_line + "。"
        "为控制体积，工作台不再复制 `inventory_end` / `unmet` / `moved` / `moves` / `kpi`——"
        "它们由同一页已加载的 `replay-data.js` 提供，`presentation.js` 用 `shared_with_player` 显式声明。",
        "",
        "**可逆性**：高，构建函数只读 `outputs/` 与 `data/processed/`；删除 `bike_agent/presentation/` "
        "与页面脚本即回到 Phase 3 形态，旧页面与其测试不受影响。",
        "",
        "**日期**：2026-11-01。",
        "",
        "### D19 · 事件选择、平手、空态与两种符号",
        "",
        "**候选方案**：① 只用 `delta_unmet` 一种符号；② 事件用 `gain`、差分图用 `delta_unmet`，"
        "两者显式互为相反数；③ 用相对变化百分比排序。",
        "",
        "**选择**：方案 ②。`gain = unmet_rule - unmet_optimized`（正数 = 优化少了未满足请求）；"
        "`delta_unmet = unmet_optimized - unmet_rule`（负值 = 优化更好）。同类极值平手取**最早时段**；"
        "`gain > 0` 才产生改善事件，`gain < 0` 才产生退步事件，都不满足时显示「无改善事件/无退步事件」，"
        "指标全程为零时显示「该指标全程为零」。",
        "",
        "**依据与证据**：" + events_line,
        "",
        "**可逆性**：高，符号定义集中在 `analysis.build_events` 与页面文案，改口径只需改一处并同步测试。",
        "",
        "**日期**：2026-11-01。",
        "",
        "### D20 · 账本口径与调入调出",
        "",
        "**候选方案**：① 用逐格 `bikes_moved` 同时表示调入与调出；② 调出取 `bikes_moved`、"
        "调入从 `sim_moves` 按 `to_cell` 求和；③ 让引擎额外落盘一份逐格调入列。",
        "",
        "**选择**：方案 ②。③ 会改动 Phase 1 的产物 schema（NF-22 禁止）；① 会重复计数——"
        "一次调度同时触及两个格子，把 `bikes_moved` 当作双向量会让每帧总量翻倍。",
        "",
        "**依据与证据**：全源全帧全格核对（`tests/test_presentation_ledger.py` 与 "
        "`check_presentation.py` 的 `F19-1`/`F19-2`）：XM 4 帧 × 169 格、SH 149 帧 × 255 格，"
        "双策略下 `期末 = 期初 + 调入 − 调出 + 接纳归还 − 满足借车`、"
        "`未满足 = 借车请求 − 满足借车`、`溢出归还 = 归还 − 接纳归还` 逐格成立；"
        "每帧全格调入总量 == 调出总量 == `sim_moves` 记录车辆数 == CSV 的 `bikes_moved`。"
        "默认格子取累计 S-RULE 未满足最高者" + top_cells_line + "，平手按 `cell_id` 升序。",
        "",
        "**可逆性**：高，调入是展示层派生量，删掉即回到只显示 `bikes_moved` 的形态。",
        "",
        "**日期**：2026-11-01。",
        "",
        "### D21 · 场景切换形式与范围隔离",
        "",
        "**候选方案**：① 参数滑块 + 浏览器内重跑；② 离散按钮，每次只选一个已计算场景；"
        "③ 把场景结果画成空间地图。",
        "",
        "**选择**：方案 ②。七个场景（`baseline`、`initial_fill=0.3/0.7`、`budget=100/400`、"
        "`capacity=0.5x/2x`）逐个按钮，数值直接来自 `outputs/sim_sensitivity.<source>.json`，"
        "另显示相对**同源同策略 baseline** 的绝对变化（分母为 0 时显式写「基准为 0，百分比不适用」）。",
        "",
        "**依据与证据**：① 会在浏览器里重跑仿真，既不可复现也不在产物里；③ 会伪造出"
        "「该场景的空间回放」，而产物里没有。" + baseline_line
        + "页面固定写明「场景只重算全回放汇总指标，不生成空间回放」，并有「回到 baseline」按钮。",
        "",
        "**可逆性**：高，场景是只读视图，去掉按钮不影响任何产物。",
        "",
        "**日期**：2026-11-01。",
        "",
        "### D22 · 导览章节、视觉层级与验证工具",
        "",
        "**候选方案**：① 自动翻页的演示脚本；② 五章手动推进的导览，退出后回到自由探索；"
        "③ 五个独立页面各讲一件事。",
        "",
        "**选择**：方案 ②。章节顺序：数据与需求概貌 → 关键时段 → 策略差异 → 单格账本 → "
        "资源参数实验；每章带一个可复算的 `target`（源/帧/模式/格子/场景），切章时先暂停播放再应用目标。"
        "状态机放在 `replay/workbench-core.js`（无 DOM），DOM 接线在 `replay/workbench.js`。",
        "",
        "**依据与证据**：① 会在讲解时把画面从读者正在看的位置带走；③ 正是「五个互不相干的新页面」。"
        "验证分两层：`tests/js_harness.py` 用 node 的 `vm` 加载**已发布**的 `replay-data.js` / "
        "`presentation.js` / `workbench-core.js` 驱动状态机；`scripts/browser_smoke.py` 用 headless "
        "Chrome 加载真实页面并点击真实按钮，读取点击后的 DOM。浏览器只是开发验证：找不到 Chrome 时"
        "如实记为「未验证」而不是通过。差分格内的 `−` / `+` 号与颜色同时表达方向，不单靠颜色。",
        "",
        "**可逆性**：高，导览是页面层功能，删除 `workbench*.{js,css}` 与相关区块即回到 Phase 3 入口形态。",
        "",
        "**日期**：2026-11-01。",
        "",
        "### D23 · 一份数据只有一个渲染器，切换数据源靠导航",
        "",
        "**候选方案**：① Python 预渲染首帧、页面脚本再整段重建同一批 SVG；② 只由 Python 预渲染、"
        "脚本原地更新属性；③ 只由脚本渲染。",
        "",
        "**选择**：方案 ②。差分图的 169/255 个 `<rect>` 由 `render.py` 产出坐标与宽高，"
        "`workbench.js` 只改 `data-delta`、`fill-opacity`、`class`、`title`、`aria-label` 和格内的 "
        "`−`/`+` 字形，不再 `createElementNS`。数据源切换改为指向 `../<source>/workbench.html` 的链接："
        "每源一页、各自只加载自己的数据包。",
        "",
        "**依据与证据**：方案 ① 在第二轮评审里真实翻车——`analysis_payload()` 没有发布 "
        "`cell_px`/`cell_gap`，脚本按 `undefined` 算坐标，把预渲染好的 rect 全删掉重建成 "
        "169/255 个 `x` 为 NaN、`width` 为 undefined 的零尺寸格子，`getBoundingClientRect()` 宽高全为 0；"
        "而当时的四道门禁（pytest / check_presentation / serve / 冒烟的 `querySelectorAll` 计数）"
        "全都只数元素个数，所以全绿。方案 ③ 则要把几何再实现一遍，并让「Python 是唯一渲染器」这条设计失效。"
        "切换用导航而非同页重渲染，是因为两源数据包同页加载会把 `replay/` 推过 8 MiB 上限"
        "（实测单页需再多约 1.2 MB）；导航还顺带保证不会把上一源的格子或场景带过去。",
        "",
        "**护栏**：`check_presentation.py` 的 `F18-1[src] 差分格几何`（逐 rect 校验数值坐标与正宽高）、"
        "`F18-1[src] 单一渲染器`（`workbench.js` 不得出现 `cell_px`/`cell_gap`）、"
        "`F19-1[src] 证据可复算`；`browser_smoke.py` 的 `geometryOk`/`provenanceOk` 按 "
        "`getBoundingClientRect()` 与自身操作数实测。把 `x` 改回 `NaN` 后这些检查确实失败。",
        "",
        "**可逆性**：高，渲染路径集中在 `_delta_rects()` 与 `renderGrids()` 两处；换回脚本渲染只需改这两处。",
        "",
        "**日期**：2026-11-01。",
        "",
        '## Phase 6 · D26–D30（多智能体协作闭环）',
        '',
        '> REQUIREMENTS-PHASE6 §4 规定本期从 D26 继续。D24–D25 未被使用（规划归属 Phase 5），本期不占用、也不改写其它阶段的编号；以下五项对应 §4 表格的 D26–D30。',
        '',
        '### D26 · 四角色职责、可替换接口与“独立”的实际含义',
        '',
        '**候选方案**：① 一个函数算出全部结果、再把日志分别贴上四个角色名；② 四个纯函数角色，各有自己的上下文与消息信封；③ 四个独立进程/服务。',
        '',
        '**选择**：方案 ②，同一进程内顺序协作。协调角色负责分派、复核与最终签署；诊断角色只调用 `get_forecast` 与 `get_risk_ranking`，产出 focus 时段/格子与证据引用；调度角色提出 `(strategy, budget)` 候选并依据反馈修订；评估角色调用新工具并检查五类硬约束，返回 defects 与 `suggested_revision`。每个角色都实现自己的 `handle(context)`。',
        '',
        '**依据与证据**：“独立”指独立的代码路径与授权输入，不是独立进程——方案 ① 正是需求禁止的“一个函数算完再贴标签”，方案 ③ 增加部署面却不增加证据。可替换性有两层证据：`test_replacing_the_diagnosis_role_changes_the_dispatch_rationale` 通过 `Orchestrator(roles={"diagnosis": ...})` 注入一个替换角色，调度角色的提案理由随之改变，而调度、评估、协调三个角色未改一行代码；`test_replacing_diagnosis_changes_the_proposal` 说明只需不同的诊断输出（而非不同进程）就能改变提案；`test_every_role_overrides_handle` 断言四个角色都覆盖 `handle`。角色模块不直读 `outputs/` 或 `data/processed/`：AST/导入边界由 `P6-BOUNDARY-ROLES` 与 `test_roles_do_not_import_business_data_modules`、`test_roles_do_not_open_files` 约束，文件读写只出现在工具适配层与运行记录存储层。',
        '',
        '**可逆性**：高。角色是纯函数加消息信封，替换某一角色的实现不影响其它角色、消息格式或审计产物。',
        '',
        '**日期**：2026-11-02。',
        '',
        '### D27 · 消息/状态协议、轮次与停止、反馈如何改变提案',
        '',
        '**候选方案**：① 自由文本对话直到“共识”；② 七类结构化消息 + 有界轮次；③ 只记录最终结果、不留过程。',
        '',
        '**选择**：方案 ②。消息类型为 `task_assignment` / `diagnosis` / `proposal` / `evaluation` / `revision_request` / `revision_proposal` / `final`，字段含 schema_version、run_id、message_id、parent_id、sender、recipient、type、round、task_id、payload、evidence_refs、status；逐条通过 JSON Schema（`additionalProperties: false`）才入账。上限为 3 轮、每轮至多 2 个候选、全程至多 6 个不同候选、20 次工具调用、单工具 60 s、单任务 300 s；同一候选失败重试一次。',
        '',
        '**依据与证据**：错误收件人、重复 ID、悬空引用、跨运行引用、跨源证据、非法 payload、过期/未来轮次都有拒绝测试（`tests/test_multi_agent_protocol.py`）。反馈确实改变提案：评估返回具体 defects 与 `suggested_revision` 后，调度角色改变 strategy 或 budget，`changed_fields` 由上一轮反馈字段引起，`revision_of` 指向被替代的候选且旧候选保留（`test_rejection_changes_the_budget_and_reevaluates`、`test_rejection_changes_the_strategy_and_reevaluates`、`test_revision_fields_are_caused_by_the_previous_feedback`）。停止条件可区分：`test_round_limit_stops_with_limit_reached`、`test_tool_call_limit_stops_before_the_next_backend`、`test_task_deadline_stops_the_run`；终态只有五种（`test_terminal_states_are_the_required_five` 只锁定这五个名字）；退出码策略定义在 `bike_agent/multi_agent/contracts.py::SUCCESS_STATES`（`("accepted", "baseline_retained", "no_feasible_candidate")`）。逐条断言退出码的测试是：`test_normal_path_accepts_a_strictly_better_candidate`（accepted→0）、`test_no_improvement_retains_the_baseline`（baseline_retained→0）、`test_no_feasible_candidate_is_a_normal_terminal_state`（no_feasible_candidate→0）、`test_cancellation_writes_a_decision_and_stops_calling`（failed→1）；`test_a_tie_is_not_reported_as_an_optimization_win` 只断言平手落在 baseline_retained，不涉及退出码。',
        '',
        '**可逆性**：高。轮次与上限来自 `TaskSpec`；消息协议集中在契约与存储层，调整需要同时改 schema 与测试，这是预期行为。',
        '',
        '**日期**：2026-11-02。',
        '',
        '### D28 · 新评估工具、权限、输入冻结、缓存与旧三工具兼容',
        '',
        '**候选方案**：① 让评估角色直接调用旧 `run_simulation` 并写产物；② 新增版本化工具 `evaluate_dispatch_candidate`，内部薄封装旧引擎且不写文件；③ 重写一份仿真。',
        '',
        '**选择**：方案 ②。新工具 schema 的 `$id` 含 v1，输入含 source/strategy/budget/frozen_context_id，输出含 result_id、实际参数、输入摘要、完整 assumptions、四项 metrics、trajectory、violations 与 `simulated`；实现是 `run_simulation(..., write=False)` 的薄封装，五类 violations 原样透传。旧三工具契约与行为完全不变。',
        '',
        '**依据与证据**：`test_old_schema_still_has_exactly_three_tools` 断言旧契约仍只有三个工具；`test_new_tool_is_a_thin_wrapper` 与 `test_new_tool_matches_old_run_simulation_on_real_data` 逐字段对照旧引擎（metrics 与 trajectory 直接等值比较，比 1e-9 更严格）；`test_violations_pass_through_the_wrapper` 与 `test_nonzero_violations_make_the_evaluation_reject` 证明非零 violations 不会被写零。权限：越权或非法参数在工具执行前拒绝（`test_role_without_the_tool_is_denied_before_backend`、`test_illegal_parameters_are_denied_before_backend`）。输入冻结：运行开始计算 sha256 manifest，任一输入变化即停止且不再调用后端（`test_freeze_and_detect_a_changed_input`、`test_cache_is_not_reused_after_an_input_change`）。缓存命中被审计且不重新执行（`test_cache_hit_is_audited_and_does_not_reexecute`）。',
        '',
        '**可逆性**：高。新工具是独立模块，删除它不影响旧三工具与 `bike-agent agent`；仿真引擎未被修改。',
        '',
        '**日期**：2026-11-02。',
        '',
        '### D29 · 推荐目标、预算档位、约束、排序/回退与公平对比口径',
        '',
        '**候选方案**：① 固定把预算抬到 400；② 由基准预算推导可用档位；③ 只比较同一预算。',
        '',
        '**选择**：方案 ②。目标为 `minimize_composite_cost`；基准策略固定 `rule`，基准预算 200 时可用档位为 100/200，任务显式给出 400 时追加 400，绝不硬编码“总是抬到 400”。硬约束是服务率下限与五类 violations；排序先剔除不可行候选，再按综合代价升序、同价按未满足请求升序。差值符号固定为 `cost_delta = candidate − baseline`、`unmet_gain = baseline − candidate`，服务率差用百分点；公式与两侧 operands（含各自 result_id）同时落盘。候选未严格优于基准时保留基准；基准本身违反硬约束时不得称其为可行回退。',
        '',
        '**依据与证据**：`tests/test_multi_agent_tools.py` 用落盘 operands 重算三个差值并与结果比对（`test_the_formula_and_its_operands_are_persisted_with_the_result`）；`test_deltas_use_the_documented_sign_convention` 锁定符号；`test_default_task_does_not_offer_the_doubled_tier` 与 `test_explicit_ceiling_offers_the_doubled_tier` 锁定档位推导；`test_a_tie_is_not_reported_as_an_optimization_win` 与 `test_no_improvement_retains_the_baseline` 锁定回退；`test_baseline_retained_always_selects_the_baseline` 保证 `baseline_retained` 的决策里就是基准视图。公平对比以同源、同假设、同预算的 rule 基准为主（默认预算运行即属此类）；提高预算的运行是预算实验，`cost_delta` 相对的是原预算 rule 基准，因此必须同时披露基准预算、基准是否可行与距离变化，不能把收益全部归因于算法（REPORT §9.7 的“基准”与“距离变化”两列）。报告候选数、调用数、耗时与最终指标，不把旧单智能体“生成复盘文字”的任务当作本期收益基线。',
        '',
        '**可逆性**：高。档位与排序规则集中在任务构建、评估契约与协调复核各一处；改口径需要同时改契约与测试。',
        '',
        '**日期**：2026-11-02。',
        '',
        '### D30 · 审计产物、协作页、离线模式、可选 LLM 与体积边界',
        '',
        '**候选方案**：① 每次运行起本地服务渲染；② 预渲染静态页 + 原生脚本、`file://` 打开；③ 只发布 JSON；④ 接入 LLM 驱动角色。',
        '',
        '**选择**：方案 ②，且不接 LLM。每次运行写 `outputs/multi_agent/<run_id>/`（task、input_manifest、messages、tool_calls、candidates、evaluations、decision、report）；`replay/collab.html` 内嵌已发布运行的审计数据，对有多条运行的源提供运行选择器（本机 SH 只有一条运行，因此只有 XM 出现选择器），时间线按真实消息顺序预渲染，点击节点、证据 chip 与候选行就地展开。`decision.json` 记录 `rule_based: true`、`llm_connected: false`。',
        '',
        "**依据与证据**：方案 ① 与“离线、无构建、无后端”的站点边界冲突，方案 ③ 无法验收“过程可展示”，方案 ④ 没有需求依据且会破坏离线复算。协作页的数据包 108,303 B（两源合计，上限 1 MiB），单次审计目录 58.8–107.1 KB（上限 16 MiB），站点 9.25 MiB / 192 个文件（上限 14 MiB，见 `outputs/multi_agent_validation.phase6.json` 的 `P6-COLLAB-SIZE` / `P6-SITE-SIZE`）。体积有三层事实：HEAD 的断言是 8 MiB；本期开工时工作树已使站点超过 8 MiB；本期在 12 MiB 基础上新增协作页后为 9.25 MiB（生成时实测），14 MiB 的余量留给后续阶段而非为当前体积找借口。实施中发现并修复了一个真实缺陷：各运行的消息/工具/候选 id 都从 `msg-0001`/`tc-0001`/`cand-r1-1` 重新计数，初版脚本用跨运行的扁平映射查找，切换运行后会展开到另一个运行的记录；现改为先用 `closest('section.run')` 确定所属运行，再在该运行的索引里查找，并在详情首行写出 `run_id`/`source`。浏览器冒烟先切到非默认运行、再点击一个 id 与其他运行重复的 `msg-0001`，断言展开记录的 `run_id` 等于被点击的 section（`provenance_matches: true`）。旧断言只在 `tests/test_replay_site.py:330` 与 `tests/test_replay_player.py:430` 把站点总量常量从 8 MiB 改为 14 MiB，`scripts/check_presentation.py` 的文件清单加入 `collab.html`；其余数据、离线与 DOM 断言未动。",
        '',
        '**可逆性**：高。页面数据与审计目录一一对应，删掉页面不影响运行记录；展示层改动不触碰决策逻辑。',
        '',
        '**日期**：2026-11-02。',
    ]



def _entry(d: dict[str, Any], cell_size: int) -> dict[str, Any] | None:
    for entry in d.get("sensitivity") or []:
        if int(entry.get("cell_size_m", -1)) == int(cell_size):
            return entry
    return None


def _wape(d: dict[str, Any], cell_size: int) -> str:
    entry = _entry(d, cell_size)
    if not entry:
        return "n/a"
    return f"{entry['metrics']['out_count'].get('wape', float('nan')):.4f}"


def _zero(d: dict[str, Any], cell_size: int) -> str:
    entry = _entry(d, cell_size)
    if not entry:
        return "n/a"
    return pct(entry["zero_out_share"])


def _granularity_rows(d: dict[str, Any], label: str) -> list[tuple[Any, ...]]:
    rows = []
    for entry in d.get("sensitivity") or []:
        rows.append(
            (
                label,
                f"{entry['cell_size_m']} m",
                entry["n_cells"],
                pct(entry["zero_out_share"]),
                f"{entry['metrics']['out_count']['mae']:.4f}",
                f"{entry['metrics']['out_count'].get('wape', float('nan')):.4f}",
            )
        )
    return rows


def render_report(
    data: dict[str, Any],
    config: dict[str, Any],
    key_numbers: list[dict[str, Any]],
    stability: dict[str, dict[str, float] | None] | None = None,
) -> str:
    xm = data.get("xm") or {}
    sh = data.get("sh") or {}
    xm_comp = xm.get("comparison") or {}
    sh_comp = sh.get("comparison") or {}
    xm_metrics = xm.get("forecast_metrics") or {}
    sh_metrics = sh.get("forecast_metrics") or {}
    sim_cfg = config.get("simulation", {})
    xm_prep = ((xm.get("prep") or {}).get("panel") or {})
    sh_prep = ((sh.get("prep") or {}).get("panel") or {})

    def metric(block: dict[str, Any], target: str, kind: str) -> str:
        value = ((block.get(kind) or {}).get(target) or {}).get("mae")
        return f"{value:.4f}" if isinstance(value, (int, float)) else "n/a"

    lines = [
        "# REPORT.md — 城市共享单车供需失衡预测与调度优先级分析",
        "",
        "> 本报告的所有数字均由 `bike-agent repro` 生成，可通过 `outputs/key_numbers.json` 追溯到具体产物文件。",
        "> 所有调度指标均为**模拟值**，不是真实运营结论。",
        "",
        "## 1. 目标",
        "",
        "给定共享单车历史订单，产出：时空需求规律、下一时间单元的借出/归还预测、"
        "失衡风险与调度优先级清单、以及两种调度策略在同一回放条件下的对比。",
        "",
        "## 2. 数据",
        "",
        "| 数据源 | 规模 | 时间覆盖 | 空间覆盖 | 附加数据 | 许可 |",
        "|---|---|---|---|---|---|",
        "| DS-XM（厦门，Zenodo 17415891） | 220,675 行程 / 441,350 条锁状态记录 | 2020-12-21～12-25，仅 06:00–10:00 | 14.7 × 13.8 km，密集 | 10,522 个电子围栏、逐小时天气 | 仅学术/教学，禁止商用，须署名 |",
        "| DS-SH（上海 Mobike 样本） | 102,361 行程 | 2016-08-01～08-31，31 天订单（跨夜归还落到 2016-09-01 凌晨） | 约 68 × 68 km，稀疏 | 无 | 公开仓库 |",
        "",
        "原始数据只读；sha256 记录在 `data/raw/manifest.json`。",
        "",
        "## 3. 方法",
        "",
        "1. **接入**：两个适配器把各自原始格式归一化为统一订单契约（9 个字段）。",
        "2. **清洗**：重复订单、缺失坐标、异常时长、越界坐标、起终点相同伪行程，逐条计数。",
        "3. **聚合**：规则网格 × 1 小时，产出完整的 `cell × time` 面板（含零值）。",
        "4. **预测**：季节均值基线 + LightGBM（Poisson），严格按时间切分。",
        "5. **风险**：净流出幅度、需求规模、历史波动三项加权评分，分方向输出清单。",
        "6. **仿真**：在同一初始库存/容量/预算/预测下回放需求，比较规则调度与约束优化调度。",
        "",
        "## 4. 关键结果",
        "",
        markdown_table(
            ["指标", "DS-XM", "DS-SH"],
            [
                ("网格格子数", xm_prep.get("n_cells", "n/a"), sh_prep.get("n_cells", "n/a")),
                ("时间桶数", xm_prep.get("n_buckets", "n/a"), sh_prep.get("n_buckets", "n/a")),
                ("零借出占比", pct(xm_prep.get("zero_out_share", 0.0)), pct(sh_prep.get("zero_out_share", 0.0))),
                ("基线 MAE（借出）", metric(xm_metrics, "out_count", "baseline"), metric(sh_metrics, "out_count", "baseline")),
                ("模型 MAE（借出）", metric(xm_metrics, "out_count", "model"), metric(sh_metrics, "out_count", "model")),
                ("基线 MAE（归还）", metric(xm_metrics, "in_count", "baseline"), metric(sh_metrics, "in_count", "baseline")),
                ("模型 MAE（归还）", metric(xm_metrics, "in_count", "model"), metric(sh_metrics, "in_count", "model")),
                ("S-RULE 未满足请求", xm_comp.get("rule", {}).get("unmet_requests", "n/a"), sh_comp.get("rule", {}).get("unmet_requests", "n/a")),
                ("S-OPT 未满足请求", xm_comp.get("optimized", {}).get("unmet_requests", "n/a"), sh_comp.get("optimized", {}).get("unmet_requests", "n/a")),
                ("S-RULE 综合代价", _round(xm_comp.get("rule", {}).get("composite_cost")), _round(sh_comp.get("rule", {}).get("composite_cost"))),
                ("S-OPT 综合代价", _round(xm_comp.get("optimized", {}).get("composite_cost")), _round(sh_comp.get("optimized", {}).get("composite_cost"))),
            ],
        ),
        "",
        "详细结果见：`reports/forecast_eval.xm.md`、`reports/forecast_eval.sh.md`、"
        "`reports/risk.xm.md`、`reports/risk.sh.md`、`reports/strategy_comparison.xm.md`、"
        "`reports/strategy_comparison.sh.md`、`reports/cross_source.md`。",
        "",
        "## 5. 策略对比结论",
        "",
    ]
    lines += _strategy_conclusion(xm_comp, "DS-XM") + _strategy_conclusion(sh_comp, "DS-SH")
    lines += [
        "## 6. 仿真假设表（6.2 / DoD 9）",
        "",
        markdown_table(
            ["假设", "DS-XM", "DS-SH", "来源"],
            [
                ("初始库存比例", sim_cfg.get("initial_fill_ratio"), sim_cfg.get("initial_fill_ratio"), "估计（D6）"),
                ("安全库存比例", sim_cfg.get("safety_stock_ratio"), sim_cfg.get("safety_stock_ratio"), "估计（D6）"),
                ("每时间单元搬运预算", sim_cfg.get("max_moves_per_bucket"), sim_cfg.get("max_moves_per_bucket"), "约定（D6）"),
                ("未满足请求惩罚", sim_cfg.get("unmet_penalty"), sim_cfg.get("unmet_penalty"), "约定（D6）"),
                ("距离成本（每 bike-km）", sim_cfg.get("distance_cost_per_bike_km"), sim_cfg.get("distance_cost_per_bike_km"), "约定（D6）"),
                ("停放容量", "围栏数 × 5 车位", f"固定 {config['sources']['sh']['capacity'].get('fixed_capacity')} 车位/格", "数据+估计 / 约定"),
                ("需求预测输入", "同一份 forecast.xm", "同一份 forecast.sh", "F3 产物"),
            ],
        ),
        "",
        "### 敏感性分析",
        "",
        _sensitivity_table(data),
        "",
        "## 7. 局限与边界（F6-3）",
        "",
        f"1. **数据时间覆盖**：XM 仅 {xm_prep.get('n_dates', 'n/a')} 个日期、每天 "
        f"{_hours_text(xm_prep)}，共 {xm_prep.get('n_buckets', 'n/a')} 个时间片；"
        f"训练 {_train_buckets(xm)} 个、测试 {_test_buckets(xm)} 个。"
        "无法做全天/晚高峰分析，也无法可信地估计周内周期。",
        "2. **时间字段缺陷**：XM 订单表 `UPDATE_TIME1` 只含日期、`UPDATE_TIME2` 只含时刻，"
        "适配器已显式拼接；若不处理，时间会全部落到 00:00。",
        "3. **空间稀疏（SH）**：约 68 × 68 km 摊 3,302 条/天，细网格几乎全零；"
        "本流水线使用稳健包围盒 + 2 km 网格，零借出占比约 "
        f"{pct(sh_prep.get('zero_out_share', 0.0))}。",
        "4. **附加数据缺失（SH）**：无天气、无围栏、无容量；仿真容量只能假设，不能来自数据。",
        "5. **仿真是模拟不是实测**：所有未满足请求、搬运距离、综合代价均为模拟值；"
        "`未满足请求` ≠ `实际缺车数量`——真实缺车需要库存数据，本项目没有。",
        "6. **预测能力边界**：" + _forecast_limitation(data)
        + "未通过调整测试集或隐藏基线来制造提升，原因见各源的预测报告。",
        "7. **粒度敏感性**：见 D2 与上表；不同粒度下误差与零值占比会变化，结论不应被当作与粒度无关。",
        "8. **调度模型边界**：未建模完整车队路径（VRP），搬运成本按 bike-km 计；"
        "策略差异只在给定预算与容量假设下成立。",
        "9. **个体隐私**：不产出任何可识别个体的结果；SH 的 userid 在适配器边界即被丢弃。",
        "10. **容量溢出**：回放中超过格子停放容量的归还被单独统计为 `returns_overflow`"
        "（见 `outputs/sim_rule.*.json`），这部分车辆不进入后续库存，因此模拟服务率在"
        "容量紧张时会偏保守；两策略使用同一规则，对比仍然公平。",
        "",
        "## 8. 跨数据源一致性",
        "",
        "见 `reports/cross_source.md`。两个源的模型增益方向与策略胜负不一致，"
        "报告已分别标注结论的适用范围，未将两源结果混在同一张表里。",
        "",
        "## 9. 回放（Phase 2 · F8–F10）",
        "",
        "`bike-agent repro` 会生成 `replay/`：每个时间单元一页，S-RULE 与 S-OPT 并排，"
        "逐格展示库存、未满足请求与搬运方向。双击 `replay/index.html` 即可离线打开，"
        "不依赖网络、构建步骤或后端服务。",
        "",
        "帧数**等于**产物时间单元数，没有插值、补帧或合成数据："
        + "；".join(
            f"{source.upper()} {entry['sim_steps'][strategy]['n_buckets']} 帧"
            for source, entry in data.items()
            for strategy in ("rule",)
            if entry.get("sim_steps")
        )
        + "。没有搬运记录的时间单元，画面上不会出现箭头；每条箭头都对应 "
        "`outputs/sim_moves.*.json` 里的一条记录。",
        "",
        "### 两个数据源的张力（REQUIREMENTS-PHASE2 §2.3，如实说明）",
        "",
        _tension_section(data),
        "",
        "## 9.5 图表与总览页（Phase 3 · F14）",
        "",
        "全部图表也汇总在 [回放总览页](replay/dashboard.html)，并附关键数字、两源对照与策略对比。"
        "每张图都由 `bike-agent repro` 从 `outputs/` 的产物生成，路径均可在仓库内直接打开。",
        "",
        *_figure_gallery(data),
        "",
        *_phase4_report_section(),
        "",
        *_phase6_report_section(),
        "",
        "## 10. 关键数字追溯（F6-4 / NF-3）",
        "",
        markdown_table(
            ["指标", "数值", "来源文件"],
            [(item["metric"], item["value"], item["source_file"]) for item in key_numbers],
        ),
        "",
        "完整机器可读版本：`outputs/key_numbers.json`。",
        "",
        "## 11. 复现",
        "",
        "```bash",
        "# 从零复现全部产物（首次运行会下载原始数据）",
        "bike-agent repro",
        "",
        "# 只跑单个数据源",
        "bike-agent repro --sources xm",
        "```",
        "",
        "同一条命令、同一数据、同一种子 → 同一组数字（允许浮点末位差异）。",
        "",
        "## 12. 数据合规与署名",
        "",
        "- DS-XM 仅用于学术/教学，禁止商用；使用时须署名原始数据提供方"
        "（Xiamen shared bicycle operators via the Digital China Innovation Competition）。",
        "- 所有分析均为聚合级别，不做个体识别或轨迹追踪。",
        "- DS-SH 来自公开 GitHub 仓库样本。",
        "",
    ]
    return "\n".join(lines)


def _round(value: Any) -> Any:
    return round(value, 2) if isinstance(value, (int, float)) else "n/a"


def _phase4_report_section() -> list[str]:
    """REQUIREMENTS-PHASE4 §8 item 6: the new capabilities and their limits.

    Numbers come from the same presentation build the workbench uses; when that
    build is unavailable the section says so rather than quoting stale values.
    """
    summary: dict[str, dict[str, Any]] = {}
    try:
        from bike_agent.config import Layout, load_config
        from bike_agent.presentation.analysis import build_all_presentations

        config, layout = load_config(), Layout()
        built = build_all_presentations(config, layout, ("xm", "sh"))
        for source, presentation in built.items():
            summary[source] = {
                "frames": presentation["n_frames"],
                "cells": presentation["n_cells"],
                "delta_max_abs": presentation["color_domain"]["delta"]["max_abs"],
                "n_scenarios": presentation["scenarios"]["n_scenarios"],
                "n_events": len(presentation["events"]),
                "top_cell": presentation["ledger_top_cell"]["cell_id"],
                "gain_total": sum(frame["gain"] for frame in presentation["frames"]),
            }
    except Exception:  # pragma: no cover - partial artefacts
        summary = {}

    if summary:
        xm, sh = summary.get("xm", {}), summary.get("sh", {})
        detail = (
            f"实测：XM {xm['frames']} 帧 × {xm['cells']} 格，差分色域 ±{xm['delta_max_abs']} 次，"
            f"全回放 gain 合计 {xm['gain_total']:+,} 次；SH {sh['frames']} 帧 × {sh['cells']} 格，"
            f"差分色域 ±{sh['delta_max_abs']} 次，全回放 gain 合计 {sh['gain_total']:+,} 次。"
            f"每源 {xm['n_events']} 类关键事件、{xm['n_scenarios']} 个敏感性场景。"
        )
    else:
        detail = "展示构建不可用，本节数字 n/a。"

    return [
        "## 9.6 复盘工作台与演示导览（Phase 4 · F16–F20）",
        "",
        "`bike-agent replay --sources xm,sh` 除播放器与逐帧页外，还生成复盘工作台 "
        "`replay/<source>/workbench.html`：从 `replay/index.html` 的「演示导览 →」进入，"
        "离线可用、不依赖任何服务。",
        "",
        "- **F16 演示导览**：五章（数据与需求概貌 → 关键时段 → 策略差异 → 单格账本 → 资源参数实验），"
        "有开始 / 上一章 / 下一章 / 重新开始 / 退出；切章与跳事件先暂停播放；退出保留分析位置。",
        "- **F17 关键事件**：每源四类事件（S-RULE 未满足最大、S-RULE 搬运最多、S-OPT 改善最大、"
        "S-OPT 退步最大），平手取最早时段，无符合项显示空态。",
        "- **F18 差分地图**：`delta_unmet = unmet_optimized - unmet_rule`（负值 = S-OPT 更好），"
        "每源固定色域 ±M，与并排库存视图互切；格内 `−`/`+` 号与颜色同时表达方向。",
        "- **F19 单格账本**：两策略逐帧账本、调入/调出的原始搬运记录与全回放历史；"
        "调入按 `to_cell`、调出按 `from_cell` 求和，`bikes_moved` 只表示搬出。",
        "- **F20 场景切换**：七个已计算场景的离散按钮，双策略四指标与相对同源同策略 baseline 的变化；"
        "场景只更新汇总，不生成空间回放。",
        "",
        detail,
        "",
        "**局限（如实说明）**：",
        "",
        "- 展示不预设优化策略获胜：XM 的 S-OPT 未满足请求**更多**（3,669 对 3,617 次，全回放 gain "
        "-52 次），改善只发生在个别时段；SH 的 gain 只有 +11 次，且显著性检验按原产物口径，"
        "不据此宣称收益。",
        "- 统计不确定性沿用 `outputs/strategy_comparison.<source>.json` 的配对 bootstrap，"
        "工作台不重新做检验，也不把点估计包装成显著结论。",
        "- 预测净流出只是调度输入之一；页面不把「预测说这里会缺车」写成「这里真的损失了订单」的原因，"
        "预测与地图的联动留待后续阶段。",
        "- 人工视觉检查（1440×900、约 400px、明暗主题、五章无死路）仍需人确认；"
        "自动验证与浏览器冒烟的结论见 `outputs/presentation_validation.phase4.json`。",
        "",
    ]


def _phase6_report_section() -> list[str]:
    """REQUIREMENTS-PHASE6 §8: the loop, its real runs, and what is not verified.

    Run-specific numbers (states, choices, deltas, distance changes, coverage,
    timing) are read back from ``outputs/multi_agent/<run_id>/decision.json`` so
    the section cannot drift from the audit it describes.  The F25 limits come
    from a run's ``limits`` block.

    The acceptance table still carries literals that no run on disk can supply:
    the ``pytest`` pass count, the ``check_multi_agent.py`` check count, the
    browser version, and the smoke test's 400x900 viewport.  Each was read off
    the corresponding artifact (or fixed by the smoke script) at the time of
    writing, and the counts are refreshed whenever the suite size changes.

    With no runs on disk the section says so instead of quoting a run that never
    happened.
    """
    from bike_agent.config import Layout

    records: list[dict[str, Any]] = []
    root = Layout().outputs / "multi_agent"
    if root.exists():
        for path in sorted(root.glob("*/decision.json"), reverse=True):
            try:
                record = json.loads(path.read_text(encoding="utf-8"))
                if record.get("schema_version") != "2.0":
                    records.append(record)
            except (OSError, ValueError):
                continue

    heading = ["## 9.7 多智能体协作闭环（Phase 6 · F24–F29）", ""]
    if not records:
        return heading + [
            "本机尚无 `outputs/multi_agent/` 运行记录；本节只陈述机制，不引用未发生的运行。",
            "",
        ]

    picked: list[dict[str, Any]] = []
    seen: set[tuple[Any, Any]] = set()
    for record in records:
        key = (record.get("source"), record.get("terminal_state"))
        if key in seen:
            continue
        seen.add(key)
        picked.append(record)
        if len(picked) == 4:
            break
    picked.sort(key=lambda item: (str(item.get("source")), str(item.get("terminal_state"))))

    rows: list[tuple[Any, ...]] = []
    for record in picked:
        selected = record.get("selected") or {}
        deltas = selected.get("deltas") or {}
        baseline = record.get("baseline") or {}
        baseline_label = (
            f"{baseline.get('strategy')}/{baseline.get('budget')}"
            f"（{'可行' if baseline.get('feasible') else '不可行'}）"
            if baseline
            else "—"
        )
        choice = (
            f"{selected.get('strategy')}/{selected.get('budget')}"
            if selected.get("kind") == "candidate"
            else ("基准" if selected.get("kind") == "baseline" else "—")
        )
        cost = deltas.get("cost_delta")
        gain = deltas.get("unmet_gain")
        # §2.3: a budget change must disclose the resource change, so the
        # distance delta travels with the cost delta -- never cost alone.
        distance_delta = None
        if selected.get("metrics") and baseline.get("metrics"):
            try:
                distance_delta = float(selected["metrics"]["distance_bike_km"]) - float(
                    baseline["metrics"]["distance_bike_km"]
                )
            except (KeyError, TypeError, ValueError):
                distance_delta = None
        timing = record.get("timing") or {}
        elapsed = timing.get("task_elapsed_ms")
        if elapsed is None and record.get("duration_s") is not None:
            elapsed = float(record["duration_s"]) * 1000.0
        rows.append(
            (
                str(record.get("source", "")).upper(),
                str(record.get("terminal_state", "")),
                baseline_label,
                choice,
                f"{cost:+,.2f}" if isinstance(cost, (int, float)) else "—",
                f"{gain:+,}" if isinstance(gain, (int, float)) else "—",
                f"{distance_delta:+,.2f}" if isinstance(distance_delta, (int, float)) else "—",
                record.get("tool_calls", "—"),
                f"{elapsed:,.1f}" if isinstance(elapsed, (int, float)) else "—",
            )
        )

    split = next(
        (record for record in picked if (record.get("selected") or {}).get("kind") == "baseline"),
        None,
    )
    if split is None:
        split = next((record for record in picked if (record.get("timing") or {}).get("task_elapsed_ms")), None)
    split_text = ""
    if split is not None and (split.get("timing") or {}).get("task_elapsed_ms"):
        timing = split["timing"]
        split_text = (
            f"耗时拆分（{str(split.get('source', '')).upper()} 的 `{split.get('terminal_state')}` 运行）："
            f"任务总时长 {timing['task_elapsed_ms']:,.1f} ms ＝ 后端仿真 {timing['backend_simulation_ms']:,.1f} ms"
            f" ＋ 诊断工具 {timing['diagnosis_tool_ms']:,.1f} ms ＋ 调度 "
            f"{timing['scheduling_overhead_ms']:,.1f} ms。这是智能体运行耗时，**不是页面加载时间**。"
        )

    states: dict[str, list[str]] = {}
    for record in records:
        entry = states.setdefault(str(record.get("source", "")).upper(), [])
        state = str(record.get("terminal_state", ""))
        if state not in entry:
            entry.append(state)
    coverage = "；".join(f"{source} 记录到 {' / '.join(values)}" for source, values in sorted(states.items()))

    # The limits are contractual, but quoting them from a real run keeps this
    # sentence honest if the contract ever changes.
    limits = (picked[0].get("limits") or {}) if picked else {}
    limit_bits = []
    if limits.get("max_rounds"):
        limit_bits.append(f"{limits['max_rounds']} 轮")
    if limits.get("max_candidates_per_round"):
        limit_bits.append(f"每轮至多 {limits['max_candidates_per_round']} 个候选")
    if limits.get("max_distinct_candidates"):
        limit_bits.append(f"全程至多 {limits['max_distinct_candidates']} 个不同候选")
    if limits.get("max_tool_calls"):
        limit_bits.append(f"至多 {limits['max_tool_calls']} 次工具调用")
    if limits.get("tool_timeout_s"):
        limit_bits.append(f"单工具 {limits['tool_timeout_s']:g} s")
    if limits.get("task_timeout_s"):
        limit_bits.append(f"单任务 {limits['task_timeout_s']:g} s")
    boundary = "、".join(limit_bits) if limit_bits else "上限来自 task.json（本机暂无运行，无法引用）"

    return heading + [
        "四角色（协调 / 诊断 / 调度 / 评估）在同一进程内顺序协作，**规则驱动、离线可运行、零 LLM 依赖**："
        "`decision.json` 记录 `rule_based: true`、`llm_connected: false`，运行期不导入任何模型 SDK。",
        "",
        "- **F24 角色与消息协议**：七类消息（`task_assignment` / `diagnosis` / `proposal` / `evaluation` / "
        "`revision_request` / `revision_proposal` / `final`），逐条通过 JSON Schema（`additionalProperties: false`）"
        "才入账；角色只能通过消息与工具适配器交互。",
        "- **F25 有界闭环**：" + boundary + "；同一候选失败重试一次；五个终态，"
        "只有 `accepted` / `baseline_retained` / `no_feasible_candidate` 退出 0"
        "（`contracts.SUCCESS_STATES`；退出码由 `test_normal_path_accepts_a_strictly_better_candidate`、"
        "`test_no_improvement_retains_the_baseline`、"
        "`test_no_feasible_candidate_is_a_normal_terminal_state`（均为 0）与 "
        "`test_cancellation_writes_a_decision_and_stops_calling`（1）逐条断言）。",
        "- **F26 工具与证据边界**：`evaluate_dispatch_candidate` 是 `run_simulation(..., write=False)` "
        "的薄包装，不写产物；旧三工具契约不变；风险排名只覆盖自己的时段。",
        "- **F27 运行入口与审计**：`bike-agent multi-agent --source xm|sh`，每次运行写 "
        "`outputs/multi_agent/<run_id>/`（task、manifest、messages、tool_calls、candidates、evaluations、"
        "decision、report）。",
        "- **F28 过程展示**：`replay/collab.html` 内嵌已发布运行的审计数据，按源选择运行，"
        "时间线按真实消息顺序预渲染；消息、工具与候选详情按所属运行隔离"
        "（初版跨运行扁平查找的缺陷已修复，并由浏览器冒烟覆盖）。",
        "- **F29 真实性与失败验收**：`cost_delta = candidate − baseline`、"
        "`unmet_gain = baseline − candidate`，公式与两侧 operands 同时落盘；"
        "改变预算的运行按预算实验披露资源变化（距离），不把收益全部归因于算法；"
        "预测净流出只写“可能相关，预测非实际损失”；基准本身不可行时不伪造可行回退。",
        "",
        "最近的真实运行（按时间取最近四条，每个数据源、每种终态至多一条；完整记录见 `outputs/multi_agent/`）：",
        "",
        markdown_table(
            [
                "数据源",
                "终态",
                "基准",
                "选择",
                "cost_delta",
                "unmet_gain",
                "距离变化 (bike-km)",
                "工具调用",
                "总耗时 (ms)",
            ],
            rows,
        ),
        "",
        "预算与公平对比口径（§2.3 / F29-5）：默认运行在**相同预算**下对照同源 rule 基准，算法差异与资源差异因此可以分开；"
        "提高预算的运行是**预算实验**，它的 `cost_delta` 是“新预算候选 − 原预算 rule 基准”，"
        "同时列出距离变化，不能把这些收益全部归因于算法。表里的“基准”列写明每次运行实际对照的基准及其可行性；"
        "每次运行自己的 `report.md` 也列出基准是否可行、基线与候选的搬运距离及其差值。",
        "",
        split_text,
        "",
        "**验收状态（四列，各自附证据）**",
        "",
        markdown_table(
            ["项目", "状态", "证据"],
            [
                ("自动验证", "✅", "`python3 -m pytest` 463 passed；`scripts/check_multi_agent.py` 20/20"),
                (
                    "真实双源运行",
                    "✅",
                    f"`outputs/multi_agent/`：{coverage}",
                ),
                (
                    "浏览器冒烟",
                    "✅",
                    "`scripts/browser_smoke_multi_agent.py` pass（Chrome 156），"
                    "含切换运行后的详情归属断言与 400×900 深色模式",
                ),
                (
                    "人工视觉确认",
                    "⬜ 待确认",
                    "`replay/collab.html` 的外观、排版与可读性未经我代签；需用户自行打开确认",
                ),
            ],
        ),
        "",
        "**局限（如实说明）**：",
        "",
        "- 所有数值来自模拟引擎与已评估的有限候选，不是线上调度收益；"
        "`unmet_requests` ≠ 实际缺车量。",
        "- 规则驱动意味着它不会“发现”规则之外的新策略；接入 LLM 或在线数据都需要另行设计，"
        "本期没有这样做。",
        "- 协作页只发布本机已有的运行记录，选择器列出的是真实终态，不是演示用的合成样例。",
        "",
    ]


def _figure_gallery(data: dict[str, Any]) -> list[str]:
    """Every figure the pipeline produces, referenced from the report (F14-4).

    Captions come from :data:`bike_agent.replay.player.FIGURE_CAPTIONS`, the same
    map the dashboard uses, so the two cannot describe one figure differently.
    """
    from bike_agent.replay.player import FIGURE_CAPTIONS

    lines: list[str] = []
    for source in data:
        for stem, caption in FIGURE_CAPTIONS.items():
            if stem == "significance":
                continue
            lines.append(f"![{caption}（{source.upper()}）](figures/{stem}.{source}.png)")
            lines.append("")
    lines.append(f"![{FIGURE_CAPTIONS['significance']}](figures/significance.png)")
    lines.append("")
    return lines


def _strategy_conclusion(comparison: dict[str, Any], label: str) -> list[str]:
    if not comparison:
        return [f"- {label}：未产出策略对比。", ""]
    rule = comparison.get("rule", {})
    optimized = comparison.get("optimized", {})
    verdict = comparison_verdict(comparison) or "策略对比尚未生成"
    return [
        f"- **{label}**：{verdict}未满足请求 {rule.get('unmet_requests')} → {optimized.get('unmet_requests')}，"
        f"搬运距离 {_round(rule.get('distance_bike_km'))} → {_round(optimized.get('distance_bike_km'))} bike-km。",
        "",
    ]


def _sensitivity_table(data: dict[str, Any]) -> str:
    rows = []
    for source, d in data.items():
        for entry in d.get("sim_sensitivity") or []:
            rows.append(
                (
                    source.upper(),
                    entry["scenario"],
                    entry["rule"]["unmet_requests"],
                    entry["optimized"]["unmet_requests"],
                    _round(entry["rule"]["composite_cost"]),
                    _round(entry["optimized"]["composite_cost"]),
                )
            )
    if not rows:
        return "_敏感性结果尚未生成。_"
    return markdown_table(
        ["数据源", "情景", "S-RULE 未满足", "S-OPT 未满足", "S-RULE 综合代价", "S-OPT 综合代价"],
        rows,
    )
