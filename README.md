# bike-agent

城市共享单车供需失衡预测与调度优先级分析。Phase 1 实现 `REQUIREMENTS.md` 的 F1–F7，
Phase 2 实现 `REQUIREMENTS-PHASE2.md` 的 F8–F11（回放可视化、前后端与智能体层）：

- **F1** 两个数据源适配器（厦门 DS-XM、上海 DS-SH）→ 统一订单契约 + 完整性报告
- **F2** 五条清洗规则 → 规则网格 × 1 小时聚合（含零值面板）
- **F3** 季节均值基线 + LightGBM，严格按时间切分评估
- **F4** 可解释风险评分 → 优先补车 / 优先清运清单
- **F5** 同一回放条件下的规则调度 vs 约束优化调度
- **F6** 一条命令从零复现全部报告、图表与产物
- **F7** 三个稳定的 agent 工具契约（零 LLM 依赖）
- **F8** 逐格逐时间单元状态落盘，与既有汇总逐单元自洽
- **F9** 回放 HTTP 服务（薄封装，三工具契约复用）
- **F10** 离线回放界面，两策略并排，帧数等于产物帧数
- **F11** 可审计的智能体层（可选，独立模块，零 LLM）

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
bike-agent replay --sources xm,sh        # 回放站点 replay/
bike-agent serve --check                 # 校验回放服务，不占端口
bike-agent serve --port 8000             # 启动回放 HTTP 服务
bike-agent agent --source xm             # 智能体复盘（可选模块）
```

`--source` 可选 `xm` / `sh`；默认主数据源为 `xm`。

## 回放（Phase 2）

`bike-agent repro` 会顺带生成 `replay/`：**直接双击 `replay/index.html` 即可打开**，
不依赖任何网络、构建步骤或后端服务。

- 每个时间单元一页，S-RULE 与 S-OPT 并排同步展示（`replay/<source>/frame-NNNN.html`）
- 帧数**等于**产物时间单元数：XM = 4，SH = 149；没有插值、补帧或合成数据
- 没有搬运记录的时间单元，画面上不会出现箭头
- 页面上的每个指标都带 `data-source`，指向 `outputs/` 里的来源文件

两个数据源各有短板，界面上如实说明：**XM 有戏没帧**（服务率约 91%，但只有 4 个时间单元）、
**SH 有帧没戏**（149 帧拖得动，但服务率约 99%，多数帧搬运量只有个位数）。

回放服务把同一批产物暴露成 HTTP 接口，三工具接口直接复用 F7 契约：

| 接口 | 能力 |
|---|---|
| `GET /api/sources` | 数据源、网格规模、时间范围 |
| `GET /api/replay/index?source=&strategy=` | 时间轴 |
| `GET /api/replay/frame?source=&strategy=&time_bucket=` | 单帧逐格状态 + 搬运记录 + 汇总 |
| `GET /api/tools/forecast` | 与 `get_forecast` 契约一致 |
| `GET /api/tools/risk` | 与 `get_risk_ranking` 契约一致 |
| `POST /api/tools/simulate` | 与 `run_simulation` 契约一致 |

## 产物

- `reports/`：`data_quality.<source>.md`、`prep.<source>.md`、`forecast_eval.<source>.md`、
  `risk.<source>.md`、`strategy_comparison.<source>.md`、`cross_source.md`
- `figures/`：需求热力图、高峰时段分布、预测 vs 实际、优先级清单、策略对比、
  显著性森林图（`significance.png`，六条结论的变化幅度与 95% 置信区间）
- `outputs/`：`forecast.parquet`、`priority_replenish.csv`、`priority_clear.csv`、
  `sim_rule.*`、`sim_optimized.*`、`replay.<strategy>.<source>.parquet`（逐格逐时间单元状态）、
  `key_numbers.json`、`summary.<source>.json`、`agent_run.<source>.{json,md}`
- `replay/`：离线回放站点（`index.html` + 每帧一页 + `style.css` / `app.js`）
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
