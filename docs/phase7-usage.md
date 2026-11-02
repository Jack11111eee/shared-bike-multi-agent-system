# 四角色大模型调度工作台：运行与展示

本阶段采用四个具有独立上下文的 DeepSeek 角色，复用已有预测、风险和仿真工具。程序校验动作、约束和结果，不替模型补齐决策。主展示是已完成真实运行的交互回放。

## 1. 打开演示

直接打开项目内的 console/index.html，或者某次运行的 outputs/multi_agent/<run_id>/presentation/index.html。后者是独立演示包，不依赖后端、模型和网络。

需要 HTTP 预览时：

```bash
python -m bike_agent.cli serve --port 8000
# 浏览器访问 http://127.0.0.1:8000/console/
```

当前机器已有依赖的解释器为 /Users/huaxinzhang/miniconda3/bin/python；默认 python3 若缺依赖，可使用该解释器，或自行建立项目环境。

macOS 可双击根目录 start-demo.command，它会选择具备项目依赖的 Python，启动本地服务并提示访问地址。默认使用8000端口，遇到占用会自动选择空闲端口，请以终端显示的网址为准。

## 2. 五分钟展示路径

1. **任务总览**：说明分析历史时段、满足率目标、每小时搬运上限和角色分工。
2. **协作过程**：点击播放或逐步推进。角色节点展示上游任务、动作、交付与证据。
3. **候选方案**：说明同预算换策略与提高预算的区别；点击候选查看该候选的空间结果。
4. **空间结果**：切换历史数据时段、地图指标和区域；解释未满足请求、搬运关系及库存账本。
5. **决策结果**：说明最终选择和代价，打开报告、下载调拨明细。

协作步骤和历史数据时段有独立控制器。演示速度只影响阅读停留，不改变真实运行耗时或业务数据粒度。

## 3. 配置 DeepSeek

将 .env.example 复制为 .env.local，填写：

```dotenv
DEEPSEEK_API_KEY=你的本地密钥
DEEPSEEK_BASE_URL=https://api.deepseek.com
DEEPSEEK_MODEL=账号可用的模型名称
```

.env.local 已加入忽略列表。不要把密钥放到前端、命令参数、报告或聊天里。

```bash
python -m pip install -e '.[llm]'
python -m bike_agent.cli llm check
```

llm check 仅查询账号可用模型，不进行付费的完整协作。未配置模型名称时，显式 LLM 运行会从账号可用列表优先选择 deepseek-flash，并将实际模型写入运行配置。

## 4. 执行真实任务

```bash
python -m bike_agent.cli multi-agent \
  --mode llm --source xm --max-budget 400 --min-service-rate 0.92
```

此命令会向已配置 DeepSeek 服务发送聚合预测、风险、模拟指标和角色交付。不会发送原始订单、个体标识和本地密钥。新运行写入独立目录，不覆盖历史结果。

完成或失败后均保存审计并发布工作台。默认 deterministic 模式仍离线可用，repro 不自动调用模型。

预算是每小时最多搬运的车辆数，不是人民币金额。候选为既有策略与合法预算档位的组合；逐格搬运由引擎计算，不是模型自由生成道路路线。

## 5. 查看和重新发布

```bash
python -m bike_agent.cli console publish --run-id <真实运行ID>
python scripts/check_phase7.py --run-id <真实运行ID>
```

每次发布生成：

- presentation/index.html：该运行的独立离线工作台。
- presentation/decision-report.html：可打印、可保存 PDF 的报告。
- presentation/decision-report.md：Markdown 报告。
- presentation/dispatch-moves.csv：有有效选择时的模拟调拨明细。
- presentation/manifest.json：文件摘要与运行身份。
- console/：已发布运行的统一展示入口。

仅发布经过脱敏的模型角色交付与工具事实；完整提示输入和模型原始响应保留在本地 model_io/。

工作台默认选择已正常完成的真实模型记录。测试夹具默认不发布、不列入真实运行列表；规则历史记录不冒充模型记录。失败运行也保留并可查看。

## 6. 证据和结果文件

- task.json、input_manifest.json：冻结任务与数据摘要。
- execution_config.json：模型身份、调用限制与提示词摘要，无密钥。
- model_calls.jsonl、model_io/：真实模型请求及响应证据。
- messages.jsonl、tool_calls.jsonl、checks.jsonl：角色交付、工具事实和校验。
- events.v2.jsonl：按实际发生顺序保存的事件。
- artifacts/<artifact_set_id>/：候选独立的汇总、逐时、逐格与搬运结果。
- decision.json、run.json：终态、选择和实际耗时。

同一个候选编号在其他运行中可能重复，因此查询必须同时携带 run_id。缓存身份包含输入和仿真代码摘要。

## 7. 已提供的只读 HTTP 接口

- GET /api/v2/runs
- GET /api/v2/runs/<run_id>
- GET /api/v2/runs/<run_id>/events?after_seq=N
- GET /api/v2/runs/<run_id>/candidates/<candidate_id>
- GET /api/v2/runs/<run_id>/results/<result_id>/spatial
- GET /api/v2/runs/<run_id>/report
- GET /api/v2/runs/<run_id>/exports/dispatch-moves.csv

浏览器实时创建任务与实时事件流属于后续增强项。未提供的功能不放置看似可执行的按钮。真实计算通过 CLI 触发，页面回放不会再次调用模型或仿真。

## 8. 检查与局限

```bash
python -m pytest
python scripts/check_phase7.py
```

普通自动测试使用标明为 test_fixture 的 Provider，验证协议、事实校验、修订和错误处理；真实模型证据来自显式 API 运行，不把测试夹具的通过算作模型完成。

地图提供高德 → OpenStreetMap → 离线三层切换，也可以手工选择来源。已验证当前环境能使用 OpenStreetMap 在线街道底图；高德缺少安全配置时可自动回退。

高德通过本地 HTTP 服务读取 .env.local 中的 AMAP_KEY / AMAP_SECURITY_CODE，仅将地图 Web Key 用于浏览器 SDK，不把 DeepSeek 密钥暴露给页面。坐标通过官方接口转换后叠加；转换失败时不在高德底图上直接叠加 WGS-84。

独立演示包不包含地图凭证；联网时可使用无需 Key 的 OpenStreetMap，断网时始终可用离线地理视图。OpenStreetMap 仅请求当前可见瓦片，保留署名，不做批量预下载。Leaflet 1.9.4 和许可已随包提供。

本地仿真目前为同步执行：超时结果会被拒绝，取消会停止后续动作，但不承诺抢占正在执行的引擎计算。现场主展示使用已完成运行的回放。

所有业务指标均为模拟值，未满足请求不等于实测缺车或真实损失订单。提高预算带来的收益包含资源效应；单次运行不证明多智能体优于其他架构。
