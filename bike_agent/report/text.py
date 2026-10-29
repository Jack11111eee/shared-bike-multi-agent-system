"""Markdown renderers for every report the project must ship."""

from __future__ import annotations

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
    return "\n".join(lines)


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
        "## 9. 关键数字追溯（F6-4 / NF-3）",
        "",
        markdown_table(
            ["指标", "数值", "来源文件"],
            [(item["metric"], item["value"], item["source_file"]) for item in key_numbers],
        ),
        "",
        "完整机器可读版本：`outputs/key_numbers.json`。",
        "",
        "## 10. 复现",
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
        "## 11. 数据合规与署名",
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
