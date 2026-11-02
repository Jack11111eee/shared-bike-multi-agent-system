# Phase 7 汇报与演示材料

主演示运行：20261102T152230044850Z-xm-llm-a4feea。四个角色使用 DeepSeek 真实调用完成三轮候选搜索，包含一次实际发生的反馈修订。

## 打开方式

- [统一工作台](../../console/index.html)：包含已发布的正常与失败运行。
- [主演示独立入口](../../outputs/multi_agent/20261102T152230044850Z-xm-llm-a4feea/presentation/index.html)：该次运行的完整静态页面。
- [独立演示包 ZIP](standalone-demo.zip)：解压后打开 index.html。
- [决策报告](../../outputs/multi_agent/20261102T152230044850Z-xm-llm-a4feea/presentation/decision-report.html)。
- [模拟调拨 CSV](../../outputs/multi_agent/20261102T152230044850Z-xm-llm-a4feea/presentation/dispatch-moves.csv)。
- [五分钟讲稿与演示顺序](demo-script.md)。
- [五页汇报 PPT](bike-agents-report.pptx)：包含可编辑的四角色职责表和结果图表，讲者备注附有讲稿要点与来源。

本机也可双击根目录 start-demo.command 启动服务，访问终端显示的工作台网址。默认 http://127.0.0.1:8000/console/，端口占用时自动选择其他空闲端口。

## 截图和备份视频

| 文件 | 内容 |
|---|---|
| [overview.jpg](overview.jpg) | 任务、目标、角色分工、结果概貌 |
| [workflow-diagnosis.jpg](workflow-diagnosis.jpg) | 诊断角色的证据与交付 |
| [workflow-feedback.jpg](workflow-feedback.jpg) | 模型收到反馈后修改搬运预算 |
| [workflow-evaluation.jpg](workflow-evaluation.jpg) | 第三轮候选的独立仿真评审 |
| [result.jpg](result.jpg) | 最终选择、效果、代价与交付物 |
| [map-online.jpg](map-online.jpg) | OpenStreetMap 真实街道底图和候选空间结果 |
| [map-offline.jpg](map-offline.jpg) | 同一结果的离线底图和区域账本 |
| [demo-key-scenes.mp4](demo-key-scenes.mp4) | 真实页面关键状态截图串联的备份视频 |

视频是关键状态选段，**不是连续操作录屏或现场计算录像**。每个画面来自真实浏览器页面，停留时长用于阅读，不表示模型运行耗时。主要演示应使用交互工作台，视频用于页面无法临场操作时的备份。

## 主结果的准确说法

- 目标：模拟借车满足率至少 92%，每小时搬运上限不超过 400 辆。
- 基准：rule / 200，满足率 91.36%，未满足请求 3,617 次。
- 最终选择：rule / 400，满足率 92.10%，未满足请求 3,306 次。
- 变化：未满足减少 311 次，满足率增加约 0.74 个百分点。
- 代价：搬运工作量增加约 928.80 车·公里；预算上限从 200 提到 400。
- 综合代价从 3,788.15 到 3,662.91，减少约 125.24 加权分值。
- 这些是有限候选、冻结假设下的历史仿真结果，不能全部归因于算法，不能称为真实运营收益。

## 真实性与验证

原始运行包含 22 次模型请求和 9 次业务工具调用，总执行约 59.6 秒。页面回放不会重新调用模型。新旧失败运行都保留，开发阶段的失败次数不作为固定版本的模型可靠性估计。

- [完整测试报告](../../outputs/phase7_pytest.xml)：470 passed，0 failures，0 errors，0 skipped。
- [运行产物检查](../../outputs/phase7_validation.json)：25 项检查通过。
- [浏览器检查记录](../../outputs/phase7_browser_checks.json)：同名候选跨运行隔离、无未来指标提前显示、地图身份、三种宽度无横向溢出。

HTTP 工作台与地图切换已实测。当前内置浏览器禁止 file://，独立包只完成本地文件与资源完整性检查；请在本机普通浏览器中打开确认。在线地图可选高德、OpenStreetMap 和离线；高德在本环境未成功加载，OpenStreetMap 已实测可用。

manifest.json 记录媒体摘要、来源运行及页面资源清单摘要。修改 UI 或数据后应重新核对并生成相应素材。
