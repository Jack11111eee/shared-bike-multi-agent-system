# bike-agent

城市共享单车供需失衡预测与调度优先级分析。Phase 1 实现 `REQUIREMENTS.md` 的 F1–F7，
Phase 2 实现 `REQUIREMENTS-PHASE2.md` 的 F8–F11（回放可视化、前后端与智能体层），
Phase 3 实现 `REQUIREMENTS-PHASE3.md` 的 F12–F15（播放器、可视化扩展、总览页与服务化），
Phase 4 实现 `REQUIREMENTS-PHASE4.md` 的 F16–F20（演示导览、关键事件、差分地图、单格账本、场景切换），
Phase 5 实现 `REQUIREMENTS-PHASE5.md` 的 F21–F23（地理底图、预测联动、演示素材），
Phase 6 实现 `REQUIREMENTS-PHASE6.md` 的 F24–F29（四角色消息协议、有界协作闭环、工具与证据边界、
运行入口与审计、协作过程展示、真实性与失败验收）：

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
- **F24** 四角色（协调 / 诊断 / 方案 / 评估）+ 版本化消息协议，职责与工具权限互斥
- **F25** 有界协作闭环：基准 → 诊断 → 提案 → 评估 → 拒绝 → 修订 → 重评估 → 终态
- **F26** 独立版本化评估工具（薄封装旧引擎）、输入冻结、缓存与证据引用
- **F27** `bike-agent multi-agent` 运行入口 + 每次运行独立审计目录
- **F28** 离线协作页 `replay/collab.html`，节点可追溯到真实消息与工具调用
- **F29** 真实性与失败验收：终态可区分、数字可追溯、不伪造 LLM/收益

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
bike-agent multi-agent --source xm       # 多智能体调度决策（规则驱动，可审计）
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

## 地理底图、预测联动与演示素材（Phase 5）

三项能力全部离线可用，站点仍是**双击 `replay/index.html` 就能打开**。

### 地理底图（F21）

工作台地图上有两组投影开关：**「规则网格」/「地理底图」**，与原有的
「并排对照 / 未满足请求差分」互相独立。切投影不改变数据源、时段或已选格子。

- **来源**：陆域与海岸线用 [GSHHG 2.3.7](https://www.ngdc.noaa.gov/mgg/shorelines/gshhg.html)
  （NOAA / University of Hawaii，LGPL-3.0）；水系与行政界用
  [OpenStreetMap 经 Geofabrik 的省级提取](https://download.geofabrik.de/asia/china.html)
  （© OpenStreetMap contributors，ODbL 1.0）。
- **离线**：底图以 `replay/<source>/basemap.js` 随包，没有运行时请求、没有 API key。
  来源、许可、CRS、覆盖范围、裁剪窗口与线要素阈值、简化参数与摘要都记在
  `replay/basemap/manifest.json`，许可文本在 `replay/basemap/licenses/`。
  出处、许可与坐标口径同时以**纯文本**写在地图下方，URL 不写成外链。
- **坐标口径**：两源业务坐标都按 WGS-84 直接叠加，**不做 GCJ-02 转换**。
  XM 的数据集 README 明写 WGS-84；SH 没有声明，改用道路控制点实测排除 GCJ-02。
  完整证据与残余风险见 [`docs/crs-evidence.md`](docs/crs-evidence.md)。
- **不是道路轨迹**：搬运连线是格心到格心的调度关系，图上不画街道，避免被读成路线规划。
- 比例尺的像素长度由 `米/像素` 反算，`<title>` 里带完整算式。

### 预测联动（F22）

工作台的「预测联动」区块显示当前格（或全源汇总）的
**实际 / 季节均值基线 / 模型预测**三条序列，支持 **借出 / 归还 / 净流量** 三种指标。

- 只显示 `outputs/forecast.<source>.parquet` 覆盖的**测试段**；不补训练期、不插值、
  不前向填充。缺失点画成断口，读数是 `—`。
- 净流量口径 `net = 借出 − 归还`（正 = 净流出，负 = 净流入），由 out/in 相减得到，不单独存储。
- 当前点显示三个原始值与 `pred − actual`、`|pred − actual|`；**局部 MAE** 只统计该格
  实际配对的测试点并同屏显示样本数 `n`；切到「全源汇总」时标签变成**全源 MAE**，
  与逐格数字不是同一口径。全源评估同时列出基线与模型的 MAE，模型没赢就写没赢。
- **MAE 不是预测区间**；页面上不画任何置信带。
- 点曲线上的时段会**先暂停**再跳到该时段，选中格保持不变；两条策略消费同一份需求预测，
  预测不随调度策略变化。

### 演示截图与录屏（F23）

`presentations/phase5/` 里有六张 1600×900 截图和一段 116 秒的 1280×720 WebM，
清单见 [`presentations/phase5/README.md`](presentations/phase5/README.md) 与 `manifest.json`。
媒体**不在** `replay/` 里，不扩大静态根，也不计入站点体积。

```bash
python3 scripts/capture_presentation.py --check    # 打印工具路径与版本；缺工具返回 2
python3 scripts/capture_presentation.py --all      # 重新生成全部截图与视频
python3 scripts/capture_presentation.py --shots    # 只生成六张截图
python3 scripts/capture_presentation.py --video    # 只生成视频
python3 scripts/capture_presentation.py --verify   # 重放记录的状态键并逐项比对，不产出媒体
```

可选依赖：Chrome/Chromium（截图与交互）、`websockets`（DevTools 协议）、
`ffmpeg`/`ffprobe`（编码与验证）、`Pillow`（PNG 解码校验）、`pyshp`（仅重做底图提取时需要）。
对应 extras：`pip install -e '.[capture]'` / `'.[media-check]'` / `'.[basemap]'`。
**主站、`bike-agent replay` 与不涉及媒体的测试都不需要它们**；
缺工具时导出命令明确失败并给出准备步骤，不会产出空文件或占位图。
媒体验收测试需要 `ffprobe`/`ffmpeg` 真正解码视频——缺了会**明确失败**（不是跳过），
因为"只看后缀"证明不了可播放；没有 Chrome 时浏览器冒烟如实报告"未验证"并返回 3。

截图状态（源、帧、时段、模式、投影、格子、场景、预测指标/范围、捕获状态）与步骤清单都写进
`manifest.json`，可以按记录重跑出同一语义状态。`--all` 在同一个浏览器里捕获完就立刻
重放每张截图的 `recipe`，把读回的状态键与记录逐项比较，结果写进 `manifest.json` 的
`replay_check`——「状态键能重新定位到当前页面」因此是实测结果，不是说明文字。
`--verify` 可以单独再跑一次（它不写媒体，也不改写上一次导出的 `last_run` 记录）。

输入摘要覆盖 `outputs/` 的产物**和**页面资源（`workbench.html`、`workbench.js`、
`basemap.js`、`forecast.js` 等），`input_files` 逐文件记 sha256，**每条素材另记捕获时的摘要**。
改一行页面代码再交付旧截图，`check_presentation.py --phase 5` 会直接报"素材已过期"，不会默默放行。
`--verify` 一张图都不拍，所以它不会改写摘要；`--shots` / `--video` 只重录自己那一半，
留下的旧素材会被点名并让命令返回非零——要刷新整套就运行 `--all`。

## 多智能体调度决策（Phase 6 · F24–F29）

四个角色在同一进程内顺序协作，**规则驱动、离线可运行、零 LLM 依赖**：

| 角色 | 职责 | 可用工具 |
|---|---|---|
| coordinator | 下发任务、审阅评估、决定修订或终结 | 无（不计算指标） |
| diagnosis | 从基准轨迹找最差时段与关注格 | `get_forecast`、`get_risk_ranking` |
| dispatch | 提出/修订候选（策略 × 预算） | 无 |
| evaluation | 运行候选、建立基准、计算差值 | `evaluate_dispatch_candidate` |

角色之间只传结构化消息（`task_assignment` / `diagnosis` / `proposal` / `evaluation` /
`revision_request` / `revision_proposal` / `final`），每条消息带 `run_id`、
`parent_id`、`evidence_refs`、轮次与 schema 版本；越权调用、非法参数、悬空引用、
过期轮次都在工具执行前被拒绝。诊断角色不直接读 `outputs/` 或 `data/processed/`，
业务数据只经网关获取。

```bash
bike-agent multi-agent --source xm
bike-agent multi-agent --source sh --max-budget 400 --min-service-rate 0.92
bike-agent multi-agent --source xm --max-rounds 3 --max-tool-calls 20
```

**预算档位不是写死的**：默认上限等于基准预算（默认 200），因此默认只评估 100/200；
显式传入 `--max-budget 400` 才会出现 400。硬约束（参数、输入冻结、五个 violations 全零、
可选服务率下限）先于综合代价排序；可行基准始终参与最终选择，候选更差或平手时保留基准。

**终态与退出码**：

| 终态 | 含义 | 退出码 |
|---|---|---|
| `accepted` | 可行候选严格优于可行基准 | 0 |
| `baseline_retained` | 基准可行且没有候选严格更优（含平手） | 0 |
| `no_feasible_candidate` | 基准与所有候选都不满足硬约束 | 0 |
| `failed` | 协议/工具/输入错误，或运行被取消（Ctrl-C 仍写审计），闭环未正常完成 | 1 |
| `limit_reached` | 轮次、候选数、工具调用或时限用尽 | 1 |

`baseline_retained` 不是“优化获胜”；基准违反硬约束时也不会被称作可行回退。
符号固定：`cost_delta = candidate − baseline`，`unmet_gain = baseline − candidate`，
服务率差用百分点；公式、操作数与 `result_id` 一起写入审计目录。

**每次运行一个目录**，只增不改，失败也保留已发生事件：
`outputs/multi_agent/<run_id>/` 下有 `task.json`、`input_manifest.json`、
`messages.jsonl`、`rejections.jsonl`、`tool_calls.jsonl`、`events.jsonl`、
`candidates.json`、`evaluations.json`、`decision.json`、`report.md`、`run.json`。
`report.md` 里同时写明基准是否可行、基线与候选的综合代价 / 未满足 / 服务率 / 搬运距离
及距离差值——提高预算买来的距离增加不会被藏在平均收益后面。
`input_manifest.json` 记录六个必需输入、三个可选输入（存在才纳入）与相关配置的 sha256；工具调用前后各校验一次，
输入变化即失败，不会拿旧结果冒充新运行。

**协作页** `replay/collab.html` 由真实运行记录生成：时间线节点带 `message_id` /
`tool_call_id`，证据 chip 点击后就地展开被引用的工具记录，候选表可展开原值，诊断时段与格子提供到
`<source>/player.html#frame=N`、`<source>/workbench.html#frame=N&cell=ID` 的深链。
XM 区带运行选择器：默认展示高上限 `accepted` 运行，可切到默认上限的 `baseline_retained` 与
高约束的 `no_feasible_candidate` 运行（“没有改善”和“没有可行候选”都是正常结果，不渲染成优化获胜）。
消息、工具与候选 id 在每个运行内重新计数，因此详情面板先确定所属运行再查找，展开内容的
首行写明 `run_id` / `source`，切换运行后不会串到另一个运行的记录。
页面无外部请求，`file://` 直接可读；打开页面不会触发智能体。

> 本页展示的是**规则驱动**的多智能体过程，**LLM 未接入**；所有数值来自原仿真引擎，
> 结论只代表本次已评估的有限候选，不声称全局最优或必然运营收益。


## 产物

- `reports/`：`data_quality.<source>.md`、`prep.<source>.md`、`forecast_eval.<source>.md`、
  `risk.<source>.md`、`strategy_comparison.<source>.md`、`cross_source.md`
- `figures/`：需求热力图、高峰时段分布、预测 vs 实际、优先级清单、策略对比、
  显著性森林图（`significance.png`，六条结论的变化幅度与 95% 置信区间）
- `outputs/`：`forecast.parquet`、`priority_replenish.csv`、`priority_clear.csv`、
  `sim_rule.*`、`sim_optimized.*`、`replay.<strategy>.<source>.parquet`（逐格逐时间单元状态）、
  `key_numbers.json`、`summary.<source>.json`、`agent_run.<source>.{json,md}`
- `outputs/`（Phase 4 新增）：`presentation_validation.phase4.json`（`check_presentation.py` 的验收报告）
- `outputs/`（Phase 5 新增）：`presentation_validation.phase5.json`（F21–F23 的验收报告）
- `outputs/multi_agent/`（Phase 6 新增）：每次运行一个独立审计目录（见上节）；
  `multi_agent_validation.phase6.json` 是 `check_multi_agent.py` 的验收报告，
  `multi_agent_browser_smoke.phase6.json` 是浏览器冒烟结果
- `replay/`：离线回放站点（`index.html` + `dashboard.html` + 播放器 + 每帧一页 +
  复盘工作台 `workbench.html` + `style.css` / `app.js` / `player.css` / `player.js` /
  `workbench.css` / `workbench.js` / `workbench-core.js` / `workbench-geo.js` /
  `replay-data.js` / `presentation.js` / `basemap.js` / `forecast.js`）
- `replay/collab.html`（Phase 6 新增）：离线多智能体协作页
- `replay/basemap/`（Phase 5 新增）：`manifest.json`（底图来源/许可/CRS/裁剪/简化/摘要）+
  `licenses/`（随包的许可文本）
- `data/basemap/raw/`（Phase 5 新增）：裁剪后的底图提取与 `manifest.json`，
  由 `python3 -m bike_agent.basemap.prepare` 生成；构建只读它，不联网
- `presentations/phase5/`（Phase 5 新增）：六张截图 + 一段 WebM + `manifest.json` + 说明
- 根目录：`REPORT.md`、`DECISIONS.md`
- `docs/`：`crs-evidence.md`（坐标证据）、`basemap-sources.md`（底图来源与许可）、
  `phase5-feasibility.md`（M25 可行性验证）、`phase5-baseline.json`（Phase 4 基线快照）

## 测试

```bash
pytest                                                   # 含 Phase 4 F16–F20、Phase 5 F21–F23 与 Phase 6 F24–F29 自动项
python3 scripts/check_presentation.py --phase 4          # Phase 4 确定性验收（失败返回非零）
python3 scripts/check_presentation.py --phase 5          # Phase 5 确定性验收（F21–F23）
python3 scripts/check_multi_agent.py                     # Phase 6 确定性验收（F24–F29，20 项）
python3 scripts/browser_smoke.py --source xm --json      # 可选：真实浏览器冒烟
python3 scripts/browser_smoke_multi_agent.py --json      # 可选：协作页深链浏览器冒烟
python3 scripts/capture_presentation.py --check          # 可选：导出工具可用性
```

## 工具契约

```bash
bike-agent tools --schema
bike-agent tools --call get_risk_ranking --input '{"direction":"replenish","top_k":5}'
```

旧 F7 三工具（`get_forecast` / `get_risk_ranking` / `run_simulation`）的 schema 与返回结构保持不变。
Phase 6 新增独立版本化契约 `evaluate_dispatch_candidate`（`schema_version: "1.0"`），
它是 `run_simulation(..., write=False)` 的薄封装：同参数下四项指标与轨迹逐值一致，
并把引擎产生的五个 `violations` 原样透出（旧 handler 会丢弃它们）。新契约不属于旧三工具集合。

## 配置

所有假设（网格大小、时间粒度、切分比例、模型超参、仿真库存/容量/预算）都在
`bike_agent/config/default.yaml`，可用 `--config override.yaml` 覆盖。
决策与证据记录在 `DECISIONS.md`。

## 数据许可

DS-XM 仅用于学术/教学，禁止商用，须署名原始数据提供方。
所有分析均为聚合级别，不产出可识别个体的结果。
