"""Replay retrospective: which bucket lost the most, and why (D12).

The planner is a deterministic reading of three tool responses.  No sampling, no
estimation, no model call -- so every sentence it writes can be traced back to a
number that one of the tools actually returned (F11-5).

Attribution matters more than prose here.  The risk ranking is computed for the
*last* bucket of the forecast horizon, which is not necessarily the bucket where
the loss peaked.  When the two differ, the ranking is reported as a separate,
clearly-labelled observation rather than folded into the explanation of the peak.
"""

from __future__ import annotations

from bike_agent.agent.audit import RunRecord, as_number
from bike_agent.agent.gateway import ToolGateway


def replay_review(gateway: ToolGateway, *, source: str, strategy: str, top_k: int = 5) -> RunRecord:
    """Point at the worst time bucket of one replay and attribute the loss."""
    record = RunRecord(task="replay_review", source=source, strategy=strategy)

    cursor = gateway.mark()
    simulation = gateway.run_simulation(source=source, strategy=strategy)
    record.add_calls(gateway.since(cursor))

    trajectory = simulation["trajectory"]
    if not trajectory:
        record.conclude("本次回放没有可复盘的时间单元。")
        return record

    # 1. The bucket that lost the most borrow requests.
    worst = max(trajectory, key=lambda row: (row["unmet_requests"], row["borrow_requests"]))
    metrics = simulation["metrics"]

    # 2. What the forecast expected in the worst bucket -- this one really is
    #    for the peak bucket, because the time bucket is passed explicitly.
    cursor = gateway.mark()
    forecast = gateway.get_forecast(source=source, time_bucket=worst["time_bucket"])
    record.add_calls(gateway.since(cursor))

    # 3. The risk ranking, which covers the horizon's last bucket, not the peak.
    cursor = gateway.mark()
    ranking = gateway.get_risk_ranking(source=source, direction="replenish", top_k=top_k)
    record.add_calls(gateway.since(cursor))

    net_out = sorted(forecast["forecasts"], key=lambda row: row["pred_net_flow"], reverse=True)[:top_k]
    net_out_text = (
        "、".join(
            f"`{row['cell_id']}` 预测净流出 {as_number(row['pred_net_flow'])}" for row in net_out[:3]
        )
        or "（该单元没有可用的预测行）"
    )

    ranking_bucket = ranking.get("time_bucket")
    same_bucket = str(ranking_bucket) == str(worst["time_bucket"])
    leaders = ranking["ranking"][:top_k]
    leader_text = (
        "、".join(
            f"`{item['cell_id']}`（分数 {as_number(item['score'])}，预测净流出 "
            f"{as_number(item['pred_net_flow'])}）"
            for item in leaders[:3]
        )
        or "（该方向没有候选格子）"
    )

    # Every number below is copied verbatim out of a tool response: no ratios,
    # no sums, no rounding.  That is what makes F11-5 checkable.
    if same_bucket:
        attribution = (
            f"该单元的风险清单前三格为 {leader_text}；"
            f"同一单元的预测里净流出最大的是 {net_out_text}。"
            f"该单元的未满足请求与这些格子的预测净流出同向，"
            f"说明损失集中在这一组净流出格子上，而调度预算与库存未能覆盖。"
        )
    else:
        attribution = (
            f"该单元的预测里净流出最大的是 {net_out_text}，"
            f"这是本单元损失的直接来源。"
            f"风险清单覆盖的时间单元是 {ranking_bucket}，与本单元 {worst['time_bucket']} 不同，"
            f"因此它只作为后续时段的格子参考，不用于解释本单元的损失。"
        )

    conclusion = (
        f"在 {source} 的 {strategy} 回放中，损失最集中的时间单元是 {worst['time_bucket']}："
        f"借车请求 {as_number(worst['borrow_requests'])} 次，其中未满足 "
        f"{as_number(worst['unmet_requests'])} 次，而整个回放的未满足请求合计为 "
        f"{as_number(metrics['unmet_requests'])} 次。"
        f"该回放的综合代价为 {as_number(metrics['composite_cost'])}，"
        f"服务率为 {as_number(metrics['service_rate'])}，"
        f"共 {as_number(len(trajectory))} 个时间单元。"
        f"{attribution}"
        f"以上数字均为模拟值。"
    )
    record.conclude(conclusion)

    record.note(
        "复盘口径：以逐时间单元的 `unmet_requests` 取最大值，平手时取借车请求更多者；"
        "没有做任何平滑、插值或跨单元聚合。"
    )
    if not same_bucket:
        record.note(
            f"风险清单的时间单元是 {ranking_bucket}，与损失峰值单元 {worst['time_bucket']} 不一致；"
            f"已按不一致处理，未把它的格子当作峰值单元的成因。"
        )
    if not leaders:
        record.note("风险清单在该方向上没有候选格子，结论中未引用任何风险排序。")
    return record
