# bike-agent · 城市共享单车供需失衡预测与多智能体调度工作台

用两个真实公开数据集做共享单车的需求预测、失衡风险排序与调度仿真，再由四个大模型角色
（协调 / 诊断 / 方案 / 评估）在受约束的工具集里协作，选出一个可被仿真验证的调度方案，
并保留完整的决策证据。

- 代码仓库：<https://github.com/Jack11111eee/shared-bike-multi-agent-system>
- 在线演示：**需本机启动**，见下方「启动在线演示」。启动后浏览器访问 `http://127.0.0.1:8000/console/`。
- 详细方案：[`docs/project-detail.md`](docs/project-detail.md)（Word 版：[`docs/project-detail.docx`](docs/project-detail.docx)）

---

## 1. 启动在线演示

演示站是**本地静态站点 + 本地 HTTP 服务**，不需要部署、不需要联网、不需要 API Key。
真实模型调用是离线的一次性运行，已经完成并保存在 `outputs/multi_agent/`，回放页面不会再次调用模型。

### 方式 A：一键启动（推荐，macOS）

双击仓库根目录的 `start-demo.command`，或在终端执行：

```bash
./start-demo.command
```

脚本会自动挑选一个装好依赖的 Python，从 8000 开始找空闲端口，并打印访问地址：

```text
工作台地址：http://127.0.0.1:8000/console/
```

保持窗口运行，按 `Ctrl-C` 停止服务。

### 方式 B：命令行启动

```bash
python3 -m pip install -e .            # 基础依赖（CPU 即可，无需 GPU）
python3 -m bike_agent.cli serve --port 8000
# 浏览器打开 http://127.0.0.1:8000/console/
```

### 方式 C：不启动服务，直接双击打开

| 页面 | 文件路径 | 启动服务后的地址 |
|---|---|---|
| 四角色决策工作台 | `console/index.html` | `/console/` |
| 回放站点首页 | `replay/index.html` | `/` |
| 复盘工作台 | `replay/xm/workbench.html`、`replay/sh/workbench.html` | `/xm/workbench.html`、`/sh/workbench.html` |
| 智能播放器 | `replay/<source>/player.html` | `/<source>/player.html` |
| 总览页 | `replay/dashboard.html` | `/dashboard.html` |
| 多智能体协作页 | `replay/collab.html` | `/collab.html` |
| 决策报告 | `outputs/multi_agent/<run_id>/presentation/decision-report.html` | `/api/v2/runs/<run_id>/report` |

`serve` 把 `replay/` 作为站点根目录，所以播放器地址是 `http://127.0.0.1:8000/xm/player.html`，
不是 `/replay/xm/player.html`。文件路径一列用于 `file://` 直接打开，两者内容相同。

### 五分钟演示路径

1. **任务总览**：历史时段、满足率目标（≥ 92%）、每小时搬运上限（400 辆）与四个角色分工。
2. **协作过程**：点播放或逐步推进；每个节点可展开「收到了什么 / 做了什么 / 交付了什么 / 证据」。
3. **候选方案**：比较同预算换策略与提高预算的差别；点击候选切换到它的空间结果。
4. **空间结果**：切时段、切地图指标、切在线/离线底图，查看未满足请求与区域库存账本。
5. **决策结果**：最终选择、代价、报告与调拨明细下载。

> 主演示运行：`20261102T152230044850Z-xm-llm-a4feea`（accepted，rule / 400）。
> 工作台同时保留「保留基准」「无可行候选」「执行失败」等真实终态，失败记录不会被改写为成功。

---

## 2. 目录结构树

```text
bike-agents/
├── README.md                     本文：启动方式、结构树、模块对应表
├── start-demo.command            双击启动本地演示服务（自动选 Python 与端口）
├── pyproject.toml                包定义、依赖、可选 extras（llm / capture / media-check / basemap）
├── REPORT.md                     数据、方法、关键结果与追溯表（由 repro 生成）
├── DECISIONS.md                  D1–D30 决策记录：为什么这样算、放弃了哪些方案
├── REQUIREMENTS*.md              Phase 1–6 的需求与验收条目（F1–F29）
│
├── bike_agent/                   全部 Python 代码（一个包，按流水线分层）
│   ├── cli.py                    唯一命令行入口：data / prep / forecast / risk / simulate /
│   │                             report / replay / serve / agent / multi-agent / console / tools
│   ├── config.py, config/default.yaml   配置加载与全部可调假设
│   │
│   ├── data/                     【接入层】原始数据 → 统一订单契约
│   │   ├── xm.py                 DS-XM 适配器（Zenodo 17415891，厦门）
│   │   ├── sh.py                 DS-SH 适配器（Mobike 上海样本）
│   │   ├── contract.py           9 字段统一契约与校验
│   │   └── quality.py            完整性报告
│   │
│   ├── prep/                     【清洗与聚合层】
│   │   ├── clean.py              五条清洗规则，逐条计数
│   │   ├── grid.py               以米为单位的规则网格（XM 169 格 / SH 255 格）
│   │   └── aggregate.py          格 × 1 小时完整面板（含零值）
│   │
│   ├── forecast/                 【预测层】
│   │   ├── baseline.py           季节均值基线
│   │   ├── features.py / model.py   LightGBM（Poisson）与特征
│   │   ├── split.py              严格按时间切分（前 80% / 后 20%）
│   │   └── pipeline.py           训练、评估、敏感性
│   │
│   ├── risk/                     【风险层】净流出 × 需求规模 × 波动 → 补车 / 清运清单
│   ├── sim/                      【仿真层】规则调度 vs 约束优化调度的同一回放对比
│   │   ├── engine.py             逐时间单元的事件顺序与守恒检查
│   │   ├── strategies.py         rule / optimized 两个调度器
│   │   └── pipeline.py           对比、敏感性场景、逐格轨迹落盘
│   │
│   ├── report/                   【报告层】指标、图表、REPORT/DECISIONS 文本
│   ├── stats.py                  显著性检验（配对差、bootstrap 置信区间）
│   │
│   ├── replay/                   【回放生成层】把产物渲染成离线静态站点
│   │   ├── site.py               逐帧页、工作台、总览页
│   │   └── player.py             播放器数据包（replay-data.js）
│   │
│   ├── presentation/             【展示层】页面构建与只读视图模型
│   │   ├── analysis.py / render.py / forecast.py / geo_js.py   工作台与地理图层
│   │   ├── collab.py             Phase 6 离线协作页
│   │   ├── run_view.py           运行记录的只读投影（不调用模型）
│   │   ├── console.py            发布工作台：console/ 与 presentation/
│   │   └── assets/               页面资源与随包 Leaflet 1.9.4
│   │
│   ├── serve/app.py              【服务层】标准库 HTTP：静态托管 + /api/* + /api/v2/*
│   │
│   ├── multi_agent/              【多智能体层】
│   │   ├── task.py               任务定义与冻结输入
│   │   ├── orchestrator.py       规则驱动的编排（deterministic 模式）
│   │   ├── runtime.py            大模型模式的合法动作集、状态机、限额
│   │   ├── llm_roles.py          四个角色的模型调用、工具往返与交付校验
│   │   ├── contracts_v2.py       版本化动作 / 消息 / 交付 Schema
│   │   ├── gateway.py            角色可用的工具网关（权限互斥）
│   │   ├── artifacts.py          每个候选独立的仿真产物
│   │   ├── journal.py            追加式审计（失败也保留已发生事件）
│   │   ├── messages.py / roles.py / audit.py / contracts.py
│   │   └── prompts/              四角色版本化提示词
│   │
│   ├── llm/                      【模型接入层】Provider 接口 + DeepSeek 适配
│   ├── basemap/                  离线底图提取与构建（GSHHG + OSM）
│   ├── tools/                    F7 三工具契约与薄包装 handler
│   └── agent/                    Phase 2 可审计规则智能体（可选模块）
│
├── console/                      发布出来的决策工作台（静态，双击可开）
│   ├── index.html                入口
│   ├── console.js / console.css  三个视图：任务总览 / 协作过程 / 决策结果
│   ├── maps.js                   高德 → OpenStreetMap → 离线 三层地图
│   ├── replay-data.js            空间回放数据包
│   ├── config.js / map-config.js 本地配置（不含密钥）
│   ├── vendor/leaflet/           随包 Leaflet
│   └── exports/<run_id>/         每次发布导出的报告、CSV、manifest
│
├── replay/                       离线回放站点
│   ├── index.html                站点首页
│   ├── dashboard.html            关键数字总览
│   ├── collab.html               多智能体协作页
│   ├── player.html/js/css        自动播放器
│   ├── workbench.*               复盘工作台（导览 / 差分 / 账本 / 场景）
│   ├── xm/  sh/                  每源一包：frame-NNNN.html、player.html、
│   │                             workbench.html、replay-data.js、basemap.js
│   └── basemap/                  底图 manifest 与许可文本
│
├── outputs/                      全部落盘产物（可追溯的数字来源）
│   ├── forecast.<source>.parquet 预测结果
│   ├── priority_replenish/clear.*.csv   风险清单
│   ├── sim_*.json / replay.*.parquet    仿真汇总与逐格逐时状态
│   ├── key_numbers.json          关键数字 → 来源文件对应表
│   ├── phase7_*.json/xml         验收、浏览器检查、pytest 报告
│   └── multi_agent/<run_id>/     每次运行一个不可变审计目录
│
├── data/                         原始数据（raw/ 与 processed/ 不入库，由 repro 重建）
├── reports/  figures/            各阶段报告与图表
├── docs/                         设计与证据文档、项目详细方案
├── presentations/                汇报 PPT、截图、备份视频、演示包
├── scripts/                      验收与冒烟脚本（check_*.py、browser_smoke*.py、capture_*）
└── tests/                        470 项自动测试
```

---

## 3. 分块对应关系

| 分块 | 回答什么问题 | 主要入口 | 典型产物 |
|---|---|---|---|
| 接入 + 清洗聚合 | 数据能不能用、干净不干净 | `bike-agent data fetch/validate`、`prep` | `reports/data_quality.*.md`、`prep.*.md`、`data/processed/` |
| 预测 | 下一个时间单元会有多少借出/归还 | `bike-agent forecast` | `outputs/forecast.*.parquet`、`reports/forecast_eval.*.md` |
| 风险 | 哪些格子会缺车、哪些会堆车 | `bike-agent risk` | `priority_replenish.*.csv`、`priority_clear.*.csv` |
| 仿真 | 同样的需求和预算下，两种调度谁更好 | `bike-agent simulate`、`repro` | `sim_*.json`、`replay.<strategy>.<source>.parquet` |
| 回放站点 | 逐帧看空间过程，离线可运行 | `bike-agent replay` | `replay/<source>/`、`replay/dashboard.html` |
| 多智能体（规则） | 四角色协议与协作闭环是否成立 | `bike-agent multi-agent` | `outputs/multi_agent/<run_id>/` |
| 多智能体（大模型） | 真实模型能否在约束内选出可验证方案 | `bike-agent multi-agent --mode llm` | 同上 + `model_calls.jsonl`、`model_io/` |
| 发布与工作台 | 把一次真实运行变成可点击的回放 | `bike-agent console publish --run-id …` | `console/`、`presentation/index.html` |
| 服务 | 把产物和工具契约暴露成 HTTP | `bike-agent serve` | `/api/*`、`/api/v2/*` |
| 验收 | 结论是否真的被证据支持 | `pytest`、`scripts/check_*.py` | `outputs/phase7_validation.json` 等 |

数据流：

```text
原始数据 → 统一订单契约 → 清洗 → 网格×小时面板
        → 预测 / 风险清单 ─┐
                          ├→ 调度仿真（rule vs optimized）→ 逐格轨迹
                          └→ 多智能体决策（工具调用 + 约束校验）
                                                        ↓
                                        审计目录 → 工作台 / 报告 / 回放站点
```

---

## 4. 常用命令

```bash
python3 -m pip install -e .            # 基础依赖
python3 -m pip install -e '.[llm]'     # 需要真实调用 DeepSeek 时

bike-agent repro --fresh               # 从零复现全部产物（首次会下载约 110 MB 数据）
bike-agent serve --port 8000           # 启动本地演示服务
bike-agent replay --sources xm,sh      # 只重建回放站点
bike-agent multi-agent --source xm     # 规则驱动的四角色协作（离线、零 LLM）
bike-agent console publish --run-id <run_id>   # 重新发布某次运行到工作台

python3 -m pytest                      # 全量自动测试
python3 scripts/check_phase7.py        # Phase 7 确定性验收
```

真实大模型运行（会发送聚合指标与角色交付，不发送原始订单与密钥）：

```bash
cp .env.example .env.local             # 填入 DEEPSEEK_API_KEY 等，.env.local 已在忽略列表
python3 -m bike_agent.cli llm check
python3 -m bike_agent.cli multi-agent --mode llm --source xm --max-budget 400 --min-service-rate 0.92
```

---

## 5. 关键结果（全部为模拟值）

| 指标 | DS-XM | DS-SH |
|---|---|---|
| 行程数 | 220,675 | 102,361 |
| 网格 × 时间桶 | 169 × 20 | 255 × 743 |
| 基线 MAE（借出） | 7.0754 | 0.5336 |
| 模型 MAE（借出） | 7.3755 | 0.5103 |
| S-RULE 未满足请求 | 3,617 | 181 |
| S-OPT 未满足请求 | 3,669 | 170 |

- **DS-SH**：S-OPT 综合代价显著更低（892.88 → 879.42，95% CI [-25.31, -1.08]，n=149）。
- **DS-XM**：S-OPT 点估计更高但差异不显著（3788.15 → 3829.88，95% CI [-268.47, +371.98]，n=4）。
- **大模型协作运行**：6 个候选、3 轮、22 次模型请求、9 次工具调用、约 59.6 秒；
  选中 rule / 400，满足率 91.36% → 92.10%，未满足减少 311 次，
  代价是搬运工作量增加约 928.80 车·公里（预算上限同时从 200 提到 400，属于资源效应）。

两个数据源的真实短板也写在界面上：**XM 有戏没帧**（服务率约 91%，但只有 4 个时间单元）、
**SH 有帧没戏**（149 帧拖得动，但服务率约 99%，多数帧搬运量只有个位数）。

---

## 6. 文档索引

| 文档 | 内容 |
|---|---|
| [`docs/project-detail.md`](docs/project-detail.md) | 项目详细方案：目标、方法、架构、验证、AI 工具使用与改进方向 |
| [`docs/phase7-usage.md`](docs/phase7-usage.md) | 大模型工作台运行与展示说明 |
| [`docs/phase7-redesign-design.md`](docs/phase7-redesign-design.md) | Phase 7 架构设计与取舍 |
| [`docs/phase7-implementation.md`](docs/phase7-implementation.md) | Phase 7 实施与验收记录 |
| [`docs/crs-evidence.md`](docs/crs-evidence.md) | 坐标口径证据 |
| [`docs/basemap-sources.md`](docs/basemap-sources.md) | 离线底图来源与许可 |
| [`REPORT.md`](REPORT.md) | 数据、方法、结果与关键数字追溯 |
| [`DECISIONS.md`](DECISIONS.md) | D1–D30 决策记录 |
| [`presentations/phase7/README.md`](presentations/phase7/README.md) | 五页 PPT、讲稿、截图与备份视频 |

---

## 7. 边界与数据许可

- 所有调度指标均为**模拟值**；未满足借车请求不等于实测缺车或真实损失订单。
- 历史订单驱动的复盘，不是无未来信息的在线预测实验；提高预算带来的改善包含资源效应。
- DS-XM 数据仅用于学术/教学，禁止商用，须署名原始数据提供方；分析均为聚合级别。
- 页面不需要联网；底图为随包的 GSHHG 与 OpenStreetMap 提取，许可文本见 `replay/basemap/licenses/`。
