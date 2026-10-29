# REPORT.md — 城市共享单车供需失衡预测与调度优先级分析

> 本报告的所有数字均由 `bike-agent repro` 生成，可通过 `outputs/key_numbers.json` 追溯到具体产物文件。
> 所有调度指标均为**模拟值**，不是真实运营结论。

## 1. 目标

给定共享单车历史订单，产出：时空需求规律、下一时间单元的借出/归还预测、失衡风险与调度优先级清单、以及两种调度策略在同一回放条件下的对比。

## 2. 数据

| 数据源 | 规模 | 时间覆盖 | 空间覆盖 | 附加数据 | 许可 |
|---|---|---|---|---|---|
| DS-XM（厦门，Zenodo 17415891） | 220,675 行程 / 441,350 条锁状态记录 | 2020-12-21～12-25，仅 06:00–10:00 | 14.7 × 13.8 km，密集 | 10,522 个电子围栏、逐小时天气 | 仅学术/教学，禁止商用，须署名 |
| DS-SH（上海 Mobike 样本） | 102,361 行程 | 2016-08-01～08-31，31 天订单（跨夜归还落到 2016-09-01 凌晨） | 约 68 × 68 km，稀疏 | 无 | 公开仓库 |

原始数据只读；sha256 记录在 `data/raw/manifest.json`。

## 3. 方法

1. **接入**：两个适配器把各自原始格式归一化为统一订单契约（9 个字段）。
2. **清洗**：重复订单、缺失坐标、异常时长、越界坐标、起终点相同伪行程，逐条计数。
3. **聚合**：规则网格 × 1 小时，产出完整的 `cell × time` 面板（含零值）。
4. **预测**：季节均值基线 + LightGBM（Poisson），严格按时间切分。
5. **风险**：净流出幅度、需求规模、历史波动三项加权评分，分方向输出清单。
6. **仿真**：在同一初始库存/容量/预算/预测下回放需求，比较规则调度与约束优化调度。

## 4. 关键结果

| 指标 | DS-XM | DS-SH |
|---|---|---|
| 网格格子数 | 169 | 255 |
| 时间桶数 | 20 | 743 |
| 零借出占比 | 29.76% | 76.39% |
| 基线 MAE（借出） | 7.0754 | 0.5336 |
| 模型 MAE（借出） | 7.3755 | 0.5103 |
| 基线 MAE（归还） | 6.9712 | 0.5396 |
| 模型 MAE（归还） | 6.7322 | 0.5129 |
| S-RULE 未满足请求 | 3617 | 181 |
| S-OPT 未满足请求 | 3669 | 170 |
| S-RULE 综合代价 | 3,788.15 | 892.88 |
| S-OPT 综合代价 | 3,829.88 | 879.42 |

详细结果见：`reports/forecast_eval.xm.md`、`reports/forecast_eval.sh.md`、`reports/risk.xm.md`、`reports/risk.sh.md`、`reports/strategy_comparison.xm.md`、`reports/strategy_comparison.sh.md`、`reports/cross_source.md`。

## 5. 策略对比结论

- **DS-XM**：S-OPT 未优于 S-RULE（F5-5 允许，已如实报告）。未满足请求 3617 → 3669，搬运距离 855.77 → 804.38 bike-km，综合代价 3788.15 → 3829.88。

- **DS-SH**：S-OPT 的综合代价不高于 S-RULE。未满足请求 181 → 170，搬运距离 3559.42 → 3547.09 bike-km，综合代价 892.88 → 879.42。

## 6. 仿真假设表（6.2 / DoD 9）

| 假设 | DS-XM | DS-SH | 来源 |
|---|---|---|---|
| 初始库存比例 | 0.5 | 0.5 | 估计（D6） |
| 安全库存比例 | 0.2 | 0.2 | 估计（D6） |
| 每时间单元搬运预算 | 200 | 200 | 约定（D6） |
| 未满足请求惩罚 | 1 | 1 | 约定（D6） |
| 距离成本（每 bike-km） | 0.2 | 0.2 | 约定（D6） |
| 停放容量 | 围栏数 × 5 车位 | 固定 40 车位/格 | 数据+估计 / 约定 |
| 需求预测输入 | 同一份 forecast.xm | 同一份 forecast.sh | F3 产物 |

### 敏感性分析

| 数据源 | 情景 | S-RULE 未满足 | S-OPT 未满足 | S-RULE 综合代价 | S-OPT 综合代价 |
|---|---|---|---|---|---|
| XM | baseline | 3617 | 3669 | 3,788.15 | 3,829.88 |
| XM | initial_fill=0.3 | 4804 | 4846 | 5,011.21 | 5,049.57 |
| XM | initial_fill=0.7 | 3035 | 3137 | 3,200.02 | 3,298.13 |
| XM | budget=100 | 3853 | 3853 | 3,932.92 | 3,936.19 |
| XM | budget=400 | 3306 | 3471 | 3,662.91 | 3,784.21 |
| XM | capacity=0.5x | 7431 | 7351 | 7,633.56 | 7,514.47 |
| XM | capacity=2x | 1315 | 1311 | 1,476.50 | 1,470.99 |
| SH | baseline | 181 | 170 | 892.88 | 879.42 |
| SH | initial_fill=0.3 | 401 | 409 | 1,141.71 | 1,143.82 |
| SH | initial_fill=0.7 | 119 | 116 | 816.66 | 814.15 |
| SH | budget=100 | 181 | 170 | 892.88 | 879.42 |
| SH | budget=400 | 181 | 170 | 892.88 | 879.42 |
| SH | capacity=0.5x | 797 | 801 | 1,530.96 | 1,526.25 |
| SH | capacity=2x | 37 | 35 | 737.78 | 735.96 |

## 7. 局限与边界（F6-3）

1. **数据时间覆盖**：XM 仅 5 个日期、每天 06:00–10:00，共 20 个时间片；训练 16 个、测试 4 个。无法做全天/晚高峰分析，也无法可信地估计周内周期。
2. **时间字段缺陷**：XM 订单表 `UPDATE_TIME1` 只含日期、`UPDATE_TIME2` 只含时刻，适配器已显式拼接；若不处理，时间会全部落到 00:00。
3. **空间稀疏（SH）**：约 68 × 68 km 摊 3,302 条/天，细网格几乎全零；本流水线使用稳健包围盒 + 2 km 网格，零借出占比约 76.39%。
4. **附加数据缺失（SH）**：无天气、无围栏、无容量；仿真容量只能假设，不能来自数据。
5. **仿真是模拟不是实测**：所有未满足请求、搬运距离、综合代价均为模拟值；`未满足请求` ≠ `实际缺车数量`——真实缺车需要库存数据，本项目没有。
6. **预测未优于基线（XM）**：见 `reports/forecast_eval.xm.md`；原因是训练时间片过少与基线过强，未通过调整测试集或隐藏基线来制造提升。
7. **粒度敏感性**：见 D2 与上表；不同粒度下误差与零值占比会变化，结论不应被当作与粒度无关。
8. **调度模型边界**：未建模完整车队路径（VRP），搬运成本按 bike-km 计；策略差异只在给定预算与容量假设下成立。
9. **个体隐私**：不产出任何可识别个体的结果；SH 的 userid 在适配器边界即被丢弃。
10. **容量溢出**：回放中超过格子停放容量的归还被单独统计为 `returns_overflow`（见 `outputs/sim_rule.*.json`），这部分车辆不进入后续库存，因此模拟服务率在容量紧张时会偏保守；两策略使用同一规则，对比仍然公平。

## 8. 跨数据源一致性

见 `reports/cross_source.md`。两个源的模型增益方向与策略胜负不一致，报告已分别标注结论的适用范围，未将两源结果混在同一张表里。

## 9. 关键数字追溯（F6-4 / NF-3）

| 指标 | 数值 | 来源文件 |
|---|---|---|
| XM · trip rows | 220675 | outputs/summary.xm.json |
| XM · duplicate rate | 0 | outputs/summary.xm.json |
| XM · abnormal duration share | 0.0286 | outputs/summary.xm.json |
| XM · grid cells | 169 | outputs/summary.xm.json |
| XM · time buckets | 20 | outputs/summary.xm.json |
| XM · zero-departure share | 0.2976 | outputs/summary.xm.json |
| XM · total departures | 195682 | outputs/summary.xm.json |
| XM · out_count baseline MAE | 7.0754 | outputs/forecast_metrics.xm.json |
| XM · out_count model MAE | 7.3755 | outputs/forecast_metrics.xm.json |
| XM · in_count baseline MAE | 6.9712 | outputs/forecast_metrics.xm.json |
| XM · in_count model MAE | 6.7322 | outputs/forecast_metrics.xm.json |
| XM · replenish candidates | 35 | outputs/risk_summary.xm.json |
| XM · clear candidates | 134 | outputs/risk_summary.xm.json |
| XM · rule unmet requests | 3617 | outputs/strategy_comparison.xm.json |
| XM · optimized unmet requests | 3669 | outputs/strategy_comparison.xm.json |
| XM · rule distance bike-km | 855.7733 | outputs/strategy_comparison.xm.json |
| XM · optimized distance bike-km | 804.3799 | outputs/strategy_comparison.xm.json |
| XM · rule composite cost | 3,788.15 | outputs/strategy_comparison.xm.json |
| XM · optimized composite cost | 3,829.88 | outputs/strategy_comparison.xm.json |
| SH · trip rows | 102361 | outputs/summary.sh.json |
| SH · duplicate rate | 0 | outputs/summary.sh.json |
| SH · abnormal duration share | 0.0005 | outputs/summary.sh.json |
| SH · grid cells | 255 | outputs/summary.sh.json |
| SH · time buckets | 743 | outputs/summary.sh.json |
| SH · zero-departure share | 0.7639 | outputs/summary.sh.json |
| SH · total departures | 99920 | outputs/summary.sh.json |
| SH · out_count baseline MAE | 0.5336 | outputs/forecast_metrics.sh.json |
| SH · out_count model MAE | 0.5103 | outputs/forecast_metrics.sh.json |
| SH · in_count baseline MAE | 0.5396 | outputs/forecast_metrics.sh.json |
| SH · in_count model MAE | 0.5129 | outputs/forecast_metrics.sh.json |
| SH · replenish candidates | 124 | outputs/risk_summary.sh.json |
| SH · clear candidates | 131 | outputs/risk_summary.sh.json |
| SH · rule unmet requests | 181 | outputs/strategy_comparison.sh.json |
| SH · optimized unmet requests | 170 | outputs/strategy_comparison.sh.json |
| SH · rule distance bike-km | 3,559.42 | outputs/strategy_comparison.sh.json |
| SH · optimized distance bike-km | 3,547.09 | outputs/strategy_comparison.sh.json |
| SH · rule composite cost | 892.8842 | outputs/strategy_comparison.sh.json |
| SH · optimized composite cost | 879.4185 | outputs/strategy_comparison.sh.json |

完整机器可读版本：`outputs/key_numbers.json`。

## 10. 复现

```bash
# 从零复现全部产物（首次运行会下载原始数据）
bike-agent repro

# 只跑单个数据源
bike-agent repro --sources xm
```

同一条命令、同一数据、同一种子 → 同一组数字（允许浮点末位差异）。

## 11. 数据合规与署名

- DS-XM 仅用于学术/教学，禁止商用；使用时须署名原始数据提供方（Xiamen shared bicycle operators via the Digital China Innovation Competition）。
- 所有分析均为聚合级别，不做个体识别或轨迹追踪。
- DS-SH 来自公开 GitHub 仓库样本。
