# bike-agent

城市共享单车供需失衡预测与调度优先级分析。Phase 1 实现 `REQUIREMENTS.md` 的 F1–F7，
Phase 2 实现 `REQUIREMENTS-PHASE2.md` 的 F8–F11（回放可视化、前后端与智能体层），
Phase 3 实现 `REQUIREMENTS-PHASE3.md` 的 F12–F15（播放器、可视化扩展、总览页与服务化），
Phase 4 实现 `REQUIREMENTS-PHASE4.md` 的 F16–F20（演示导览、关键事件、差分地图、单格账本、场景切换）：

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
- **F12** 自动播放器：自动播放、控制条、全帧覆盖、离线可用
- **F13** 可视化扩展：固定色标、跨帧趋势图、帧内明细、两策略对照
- **F14** 总览页 `replay/dashboard.html`，并在 `REPORT.md` 中嵌入图表
- **F15** `serve` 静态托管站点 + `GET /api/replay/timeline` 时间轴接口
- **F16** 演示导览：首页入口 + 五章推进（数据概貌 → 关键时段 → 策略差异 → 单格账本 → 参数实验）
- **F17** 关键事件跳转：两源四类事件的清单与一键定位，跳转即暂停
- **F18** 策略差分地图：逐格 `delta_unmet`，每源固定色域 ±M，与并排视图互切
- **F19** 单格库存账本：两策略逐帧账本、调入/调出原始记录与全回放历史
- **F20** 敏感性场景切换：七个已计算场景、双策略四指标、相对基准的变化

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

## 回放与播放器（Phase 2–3）

`bike-agent repro` 会顺带生成 `replay/`：**直接双击 `replay/index.html` 即可打开**，
不依赖任何网络、构建步骤或后端服务。

- **播放器**（Phase 3）：`replay/<source>/player.html` 打开后自动逐帧播放，
  可暂停、上一帧 / 下一帧、拖动进度、0.5× / 1× / 2× / 4× 变速、循环；
  默认 1×，动画只改变停顿，不改变数据
- **总览页**（Phase 3）：[`replay/dashboard.html`](replay/dashboard.html) 一页汇总关键数字、
  两源对照、策略对比与 `figures/` 全部图表
- **逐帧页**（Phase 2，保留）：每个时间单元一页，S-RULE 与 S-OPT 并排同步展示
  （`replay/<source>/frame-NNNN.html`），承担深链、自动验收与无 JavaScript 回退
- 帧数**等于**产物时间单元数：XM = 4，SH = 149；没有插值、补帧或合成数据
- 没有搬运记录的时间单元，画面上不会出现箭头（SH 每策略各有 40 帧）
- 页面上的每个指标都带 `data-source`，指向 `outputs/` 里的来源文件
- 播放器数据只打包一次：`replay-data.js`、`player.html` 首帧与
  `/api/replay/timeline` 共用同一个构建函数

两个数据源各有短板，界面上如实说明：**XM 有戏没帧**（服务率约 91%，但只有 4 个时间单元）、
**SH 有帧没戏**（149 帧拖得动，但服务率约 99%，多数帧搬运量只有个位数）。

## 复盘工作台与演示导览（Phase 4）

入口：`replay/index.html` 的 **「演示导览 →」**，或直接打开
`replay/xm/workbench.html` / `replay/sh/workbench.html`（离线可用，无需服务）。

- **五章导览**：数据与需求概貌 → 关键时段 → 策略差异 → 单格账本 → 资源参数实验。
  有开始 / 上一章 / 下一章 / 重新开始 / 退出导览；不自动翻章，切章与跳事件都会**先暂停播放**。
  退出导览保留当前分析位置；重新开始回到首章。
- **关键事件**：每源四类事件（S-RULE 未满足最大、S-RULE 搬运最多、S-OPT 改善最大、S-OPT 退步最大），
  平手取最早时段，无符合项显示"无改善事件 / 无退步事件"。点击后联动时段、地图、KPI、账本。
- **差分地图**：「并排对照 / 未满足请求差分」互切。逐格 `delta_unmet = unmet_optimized − unmet_rule`，
  **负值 = S-OPT 更少未满足**；每源色域固定为 ±M（M = 全回放全部格子的最大绝对差，XM 102 / SH 3）。
  `gain = unmet_rule − unmet_optimized` 与本指标互为相反数，两者标签不混用。
- **单格账本**：期初、调入、调出、归还、接纳归还、溢出归还、借车请求、满足、未满足、期末，
  加该格全回放历史；调入按 `to_cell`、调出按 `from_cell` 从搬运明细求和。
  默认格子取累计 S-RULE 未满足最高者（平手按 `cell_id` 升序）。
- **场景切换**：七个已计算场景（`baseline`、`initial_fill=0.3/0.7`、`budget=100/400`、
  `capacity=0.5x/2x`），双策略四指标与相对同源同策略 baseline 的绝对变化。
  **场景只更新汇总表，不生成空间回放**，地图始终是 baseline 回放。
- 导览与事件是**选段**，允许显式跳转并标注"关键事件/选段"；完整播放器仍覆盖全部帧。
- 数据源切换：每源一页，各自只加载自己的数据包；页首的 `XM` / `SH` 是**链接**，跳到另一源的工作台（两包同页会把站点推过 8 MiB 上限）。
- 键盘：Tab 到控件即可操作；`←/→` 切帧、`Esc` 退出导览、格子用「选择格子」下拉框选择（格子 rect 不带 `tabindex`，169/255 个格子不做成 tab 停靠点）。

验证命令：

```bash
python3 scripts/check_presentation.py --phase 4   # 确定性验收，写 outputs/presentation_validation.phase4.json
python3 scripts/browser_smoke.py --source xm      # 可选：开发期真实浏览器联动冒烟（需要 Chrome）
```

`browser_smoke.py` 除了点完所有控件，还量测渲染结果：差分格必须有正的 `getBoundingClientRect()`
宽高（只数元素个数会放过 `x="NaN"` 的零尺寸格子），账本的 `inventory_end` 必须能由它自己
`data-operands` 里的操作数算回来、`data-join-key` 必须指向屏幕上那个时段。任一不过即判失败。

两者都不参与站点运行；`browser_smoke.py` 找不到浏览器时返回 3 并如实报告"未验证"。

回放服务把同一批产物暴露成 HTTP 接口，三工具接口直接复用 F7 契约：

| 接口 | 能力 |
|---|---|
| `GET /` | 只读托管 `replay/` 静态站点（播放器、逐帧页、总览页） |
| `GET /api/sources` | 数据源、网格规模、时间范围 |
| `GET /api/replay/index?source=&strategy=` | 时间轴 |
| `GET /api/replay/frame?source=&strategy=&time_bucket=` | 单帧逐格状态 + 搬运记录 + 汇总 |
| `GET /api/replay/timeline?source=` | 播放器用的全帧紧凑数据（与 `replay-data.js` 同源） |
| `GET /api/tools/forecast` | 与 `get_forecast` 契约一致 |
| `GET /api/tools/risk` | 与 `get_risk_ranking` 契约一致 |
| `POST /api/tools/simulate` | 与 `run_simulation` 契约一致 |

静态托管只读、只允许 html/css/js/png/svg/json/txt/ico；路径穿越请求返回 4xx，
不会返回站点外的文件内容。

## 产物

- `reports/`：`data_quality.<source>.md`、`prep.<source>.md`、`forecast_eval.<source>.md`、
  `risk.<source>.md`、`strategy_comparison.<source>.md`、`cross_source.md`
- `figures/`：需求热力图、高峰时段分布、预测 vs 实际、优先级清单、策略对比、
  显著性森林图（`significance.png`，六条结论的变化幅度与 95% 置信区间）
- `outputs/`：`forecast.parquet`、`priority_replenish.csv`、`priority_clear.csv`、
  `sim_rule.*`、`sim_optimized.*`、`replay.<strategy>.<source>.parquet`（逐格逐时间单元状态）、
  `key_numbers.json`、`summary.<source>.json`、`agent_run.<source>.{json,md}`
- `outputs/`（Phase 4 新增）：`presentation_validation.phase4.json`（`check_presentation.py` 的验收报告）
- `replay/`：离线回放站点（`index.html` + `dashboard.html` + 播放器 + 每帧一页 +
  复盘工作台 `workbench.html` + `style.css` / `app.js` / `player.css` / `player.js` /
  `workbench.css` / `workbench.js` / `workbench-core.js` / `replay-data.js` / `presentation.js`）
- 根目录：`REPORT.md`、`DECISIONS.md`

## 测试

```bash
pytest                                                   # 含 Phase 4 的 F16–F20 自动项
python3 scripts/check_presentation.py --phase 4          # 确定性验收（失败返回非零）
python3 scripts/browser_smoke.py --source xm --json      # 可选：真实浏览器冒烟
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
