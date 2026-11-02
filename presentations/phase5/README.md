# Phase 5 演示素材清单

本目录的截图与视频由 `scripts/capture_presentation.py` 从真实已生成页面捕获。
机器可读清单见 `manifest.json`；下面的状态键可用于重新定位到同一页面状态。

- 捕获命令：`python3 scripts/capture_presentation.py --all`
- 输入摘要：`b3f79cb34acdfabf…`（任一输入变化即视为素材过期）
- 工具：Google Chrome 156.0.8078.79；ffmpeg version 7.1.1 Copyright (c) 2000-2025 the FFmpeg developers

## 截图

### 导览概貌 · XM

- 文件：`presentations/phase5/guide-overview.png`（165,361 B，sha256 `d28fe2a5270525b8…`）
- 页面：`replay/xm/workbench.html`；视口 1600x900；主题 light
- 状态：源 xm，帧 0（2020-12-25 06:00:00），模式 side，投影 grid，格子 C0007_0010，场景 baseline
- 预测：指标 out，范围 cell，时段 2020-12-25 06:00:00，局部 MAE 30.673471486744695（n=4）
- 图注：XM 复盘工作台的演示导览首章：4 个时间单元、169 个格子，S-RULE 与 S-OPT 的未满足请求与搬运量。

### XM 地理底图上的差分地图

- 文件：`presentations/phase5/xm-geo-delta.png`（222,286 B，sha256 `256499cc2c4ec661…`）
- 页面：`replay/xm/workbench.html`；视口 1600x900；主题 light
- 状态：源 xm，帧 1（2020-12-25 07:00:00），模式 delta，投影 geo，格子 C0007_0010，场景 baseline
- 预测：指标 out，范围 cell，时段 2020-12-25 07:00:00，局部 MAE 30.673471486744695（n=4）
- 图注：XM 第 2 个时间单元、差分模式下切到地理底图：两个退步格（S-OPT 未满足更多）落在厦门岛内，色域固定 ±102 次。

### XM 单格库存账本

- 文件：`presentations/phase5/xm-ledger.png`（191,216 B，sha256 `1f88c53428282177…`）
- 页面：`replay/xm/workbench.html`；视口 1600x900；主题 light
- 状态：源 xm，帧 0（2020-12-25 06:00:00），模式 side，投影 grid，格子 C0007_0010，场景 baseline
- 预测：指标 out，范围 cell，时段 2020-12-25 06:00:00，局部 MAE 30.673471486744695（n=4）
- 图注：XM 默认格 C0007_0010（累计 S-RULE 未满足最高）在两策略下的期初/调入/调出/归还/期末与全回放历史，四个派生值都能由自身操作数复算。

### SH 预测联动

- 文件：`presentations/phase5/sh-forecast.png`（233,796 B，sha256 `0280d1a4c9a9e384…`）
- 页面：`replay/sh/workbench.html`；视口 1600x900；主题 light
- 状态：源 sh，帧 39（2016-08-27 11:00:00），模式 side，投影 grid，格子 C0007_0006，场景 baseline
- 预测：指标 out，范围 cell，时段 2016-08-27 11:00:00，局部 MAE 1.403775242776319（n=149）
- 图注：SH 当前格的实际、季节均值基线与模型预测三条序列（149 个测试时段），当前点误差与局部 MAE（含样本数）同屏显示。

### 敏感性场景切换

- 文件：`presentations/phase5/scenario.png`（236,118 B，sha256 `c5883d781a4b8d27…`）
- 页面：`replay/xm/workbench.html`；视口 1600x900；主题 light
- 状态：源 xm，帧 0（2020-12-25 06:00:00），模式 side，投影 grid，格子 C0007_0010，场景 initial_fill=0.3
- 预测：指标 out，范围 cell，时段 2020-12-25 06:00:00，局部 MAE 30.673471486744695（n=4）
- 图注：XM 切换到 initial_fill=0.3 场景：两策略四项汇总指标相对同源同策略 baseline 的绝对与百分比变化；地图仍是 baseline 回放。

### SH 地理底图

- 文件：`presentations/phase5/sh-geo.png`（279,762 B，sha256 `6d164bf17048b504…`）
- 页面：`replay/sh/workbench.html`；视口 1600x900；主题 light
- 状态：源 sh，帧 0（2016-08-25 20:00:00），模式 side，投影 geo，格子 C0007_0006，场景 baseline
- 预测：指标 out，范围 cell，时段 2016-08-25 20:00:00，局部 MAE 1.403775242776319（n=149）
- 图注：SH 工作台切到地理底图：黄浦江与区界为定位语境，255 个格子的期末库存叠加其上，比例尺与出处可复算。

## 视频

### 演示录屏 · 两源复盘

- 文件：`presentations/phase5/demo.webm`（503,332 B，sha256 `575babe8c91d6585…`）
- 视口 1280x720，4 fps，时长 116.0 秒
- 演示选段、模拟值：按既定章节停留并显式切章，画面全部来自真实已生成页面；视频播放时长与数据时钟分开，数据只有小时级时间单元，视频不暗示更细的观测频率。

| 章节 | 起 | 止 | 数据源 | 数据时段 |
|---|---|---|---|---|
| XM · 导览概貌 | 0.0s | 16.0s | XM | 2020-12-25 06:00:00 |
| XM · 关键事件跳转 | 16.0s | 32.0s | XM | 2020-12-25 08:00:00 |
| XM · 地理底图上的策略差分 | 32.0s | 50.0s | XM | 2020-12-25 07:00:00 |
| XM · 单格账本 | 50.0s | 66.0s | XM | 2020-12-25 06:00:00 |
| SH · 预测联动 | 66.0s | 84.0s | SH | 2016-08-27 11:00:00 |
| XM · 敏感性场景 | 84.0s | 100.0s | XM | 2020-12-25 06:00:00 |
| SH · 地理底图 | 100.0s | 116.0s | SH | 2016-08-25 20:00:00 |

## 状态键复核

`--verify` 会重新打开每张截图对应的页面、重放 `recipe` 里的步骤，再把读回的状态键与捕获时记录的逐项比较。这是「状态键能重新定位到当前页面」的实测证据，不是说明文字。

- 最近一次复核：通过（6 张，2026-11-02T03:22:34+00:00）
  - `source`
  - `frame`
  - `time_bucket`
  - `mode`
  - `projection`
  - `cell`
  - `scenario`
  - `forecast_metric`
  - `forecast_range`
  - `forecast_bucket`
  - `forecast_mae`
  - `forecast_n`
  - `delta_net`
