# bike-agent

城市共享单车供需失衡预测与调度优先级分析。实现 `REQUIREMENTS.md` 的 F1–F7：

- **F1** 两个数据源适配器（厦门 DS-XM、上海 DS-SH）→ 统一订单契约 + 完整性报告
- **F2** 五条清洗规则 → 规则网格 × 1 小时聚合（含零值面板）
- **F3** 季节均值基线 + LightGBM，严格按时间切分评估
- **F4** 可解释风险评分 → 优先补车 / 优先清运清单
- **F5** 同一回放条件下的规则调度 vs 约束优化调度
- **F6** 一条命令从零复现全部报告、图表与产物
- **F7** 三个稳定的 agent 工具契约（零 LLM 依赖）

## 安装

```bash
python3 -m pip install -e .
```

要求 Python ≥ 3.10，CPU 即可，无需 GPU。

## 一键复现

```bash
bike-agent repro --fresh
```

首次运行会下载原始数据（约 110 MB），随后依次完成：下载 → 校验 → 清洗聚合 →
预测 → 风险清单 → 两种策略仿真 → 报告与图表。退出码 0 表示全部产物生成成功。

## 分步命令

```bash
bike-agent data fetch --source xm        # 下载 + 归一化 + 契约校验
bike-agent data validate --source xm     # 完整性报告 reports/data_quality.xm.md
bike-agent prep --source xm              # 清洗 + 聚合
bike-agent forecast --source xm          # 基线 + LightGBM
bike-agent risk --source xm              # 风险评分 + 两张清单
bike-agent simulate --strategy rule      # 或 --strategy optimized
bike-agent report --sources xm,sh        # 汇总报告与图表
```

`--source` 可选 `xm` / `sh`；默认主数据源为 `xm`。

## 产物

- `reports/`：`data_quality.<source>.md`、`prep.<source>.md`、`forecast_eval.<source>.md`、
  `risk.<source>.md`、`strategy_comparison.<source>.md`、`cross_source.md`
- `figures/`：需求热力图、高峰时段分布、预测 vs 实际、优先级清单、策略对比
- `outputs/`：`forecast.parquet`、`priority_replenish.csv`、`priority_clear.csv`、
  `sim_rule.*`、`sim_optimized.*`、`key_numbers.json`、`summary.<source>.json`
- 根目录：`REPORT.md`、`DECISIONS.md`

## 测试

```bash
pytest
```

## 工具契约

```bash
bike-agent tools --schema
bike-agent tools --call get_risk_ranking --input '{"direction":"replenish","top_k":5}'
```

## 配置

所有假设（网格大小、时间粒度、切分比例、模型超参、仿真库存/容量/预算）都在
`bike_agent/config/default.yaml`，可用 `--config override.yaml` 覆盖。
决策与证据记录在 `DECISIONS.md`。

## 数据许可

DS-XM 仅用于学术/教学，禁止商用，须署名原始数据提供方。
所有分析均为聚合级别，不产出可识别个体的结果。
