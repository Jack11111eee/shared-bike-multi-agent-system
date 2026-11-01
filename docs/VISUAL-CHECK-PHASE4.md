# Phase 4 视觉检查记录（REQUIREMENTS-PHASE4 §6.2）

**状态：自动验证完成 / 浏览器冒烟完成 / 人工视觉待确认。**

本文件不代替用户签署人工认可。下面三栏必须分开读：只有第一栏与第二栏有机器证据；
第三栏是需要人看的项目，目前**尚未由人确认**，因此本期不得写成"全部验收完成"。

## 一、自动验证完成（机器可判定，可复跑）

| 项目 | 命令 | 结果 |
|---|---|---|
| F16–F20 与 NF-22~25 的机器可判定部分 | `python3 scripts/check_presentation.py --phase 4` | 38/38 通过，报告见 `outputs/presentation_validation.phase4.json` |
| 全量数据/账本/事件/场景核对 | `python3 -m pytest` | 296 passed，0 failed，0 skipped |
| 业务数值不回归 | `python3 scripts/check_regression.py` | 退出码 0，既有值全部保留 |
| 站点体积 | `check_presentation.py` 的 `NF-24` | `replay/` 7.05 MiB（上限 8 MiB，7,387,653 字节；不计 gitignored 的 `.DS_Store` 与冒烟临时页）；`presentation.js` XM 90,747 / SH 1,405,651 字节（上限 2 MiB/源）；`workbench.html` XM 167,130 / SH 225,968 字节（上限 256 KiB） |
| 离线 | `NF-23` | `replay/` 下无任何外部 URL 引用 |

## 二、浏览器冒烟完成（真实 Chrome，headless）

`python3 scripts/browser_smoke.py --source xm|sh --json`，两源各 14 步，`ok: true`、`errors: []`：

- 导览：开始 → 4 次下一章 → 上一章 → 退出 → 重新开始，每步的 `data-chapter`、
  `#guide-title`、`#frame-label`、`#state-mode`、`#state-cell`、`#state-scenario` 与章节目标一致；
  同时活动的章节数恒为 1。
- 事件跳转：播放中点事件后 `#play-state` 由 `true` 变为 `false`（跳转即暂停）。
- 模式切换：`#state-mode` 变为 `delta`，差分格渲染 169 / 255 个；点格后 `#ledger-cell` 跟随选择。
- 场景：7 个按钮逐个点击，`#state-scenario` 与按钮一致，再点「回到 baseline」回到 `baseline`。
- 账本：浏览器渲染出的 `data-value` 与 parquet 逐字段相等（起始/请求/满足/归还/接纳归还/
  未满足/溢出/调出/期末），历史表有 `n_frames` 行，且每策略恰有 1 行标为当前帧。

除上述步骤外，另有三类断言（`geometryOk` / `provenanceOk`，任一不过则 `ok: false`）。
第二轮评审发现的三个阻塞缺陷恰好都落在"步骤都点过了、但画面是空的"这一层，所以它们单独计：

| 断言 | 量测方式 | XM 实测 | SH 实测 |
|---|---|---|---|
| 差分格真的有尺寸 | 切到差分模式后逐个 `getBoundingClientRect()`，宽高须 > 0 | 169 格，`zeroSize=0`，宽 39.76 px | 255 格，`zeroSize=0` |
| 派生值能由自己的操作数复算 | 读 `data-operands` 重算 `inventory_end`，与 `data-value` 比较 | 163 = 163 | 21 = 21 |
| 关联键指向屏幕上的时段 | `data-join-key` 须含 `#bucket-label` 的当前值 | 初始 06:00、跳转后 08:00 均一致 | 初始 20:00 一致 |

三项在**初始 / 切换模式后 / 事件跳转后**各测一次。量测只在差分视图可见时进行——
`hidden` 子树没有盒子，不区分"未显示"和"零尺寸"会让护栏自己造假失败。

复跑：`python3 scripts/browser_smoke.py --source xm`。找不到 Chrome 时脚本返回 3 并打印"未验证"，
`check_presentation.py` 的 `NF-27` 会记为 fail —— 未执行不等于通过。

## 三、人工视觉待确认（需要人看，机器不代签）

截图存于 `docs/visual-evidence/`，均来自 `replay/xm/workbench.html` 与 `replay/sh/workbench.html`：

| 检查项 | 截图 | 观察 | 状态 |
|---|---|---|---|
| 1440×900：主图、当前结论与关键操作可读；正文/关键图例 ≥ 14px | `workbench-xm-light-1440.png`、`workbench-xm-dark-1440.png` | 章节标题、正文与图例在 1440 宽下可读；证据行字号 11px 属辅助信息（来源/关联键/公式），正文与图例 13–14px | 待确认 |
| 窄屏：重排为纵向、主页面无横向溢出、触摸可完成核心操作 | `workbench-sh-narrow-500.png` | 实测视口 500 CSS px：`document.documentElement.scrollWidth == innerWidth == 500`，无横向溢出；布局已单列。**未能在本机复现 400 px 视口**：headless Chrome 在 macOS 上把窗口夹到最小 500 px，`--force-device-scale-factor` 也不改变 CSS 视口，所以 §6.2 的"约 400px"这一档只测到 500px，400px 需要人在真机/DevTools 上确认 | 待确认（400px 未实测） |
| 明暗两主题：零值/改善/退步/当前选中格可区分 | `workbench-xm-dark-1440.png` 与 light 版 | 两套配色均已截图；差分格同时用颜色与 `−`/`+` 号，零值格为中性灰 | 待确认 |
| 差分地图本身看得见、看得出方向 | `workbench-xm-delta-1440.png`（XM 第 3 帧）、`workbench-sh-delta-1440.png`（SH 第 123 帧，全回放 |delta| 最大） | 两源截图确认 169 / 255 格全部有实体方块：XM 本帧净差 −85、改善 2 格、无差异 167 格；SH 本帧净差 −3、改善 1 格、无差异 254 格。改善格带 `−` 号，与页面数字一致 | 待确认（仅证明可见，配色是否易读需人看） |
| 走完五章无死路；跳事件后不会被自动播放立即带走 | 见浏览器冒烟 | 冒烟断言了暂停与章节状态；视觉上是否有误导仍需人看 | 待确认 |
| 场景切换不会让人误认地图已换场景 | `workbench-xm-full-light.png` 的场景段 | 场景区上方固定写「baseline 回放地图」，并有「回到 baseline」；是否足够醒目需人判断 | 待确认 |
| 所有业务数值可展开证据；缺失/无变化/退步均有正常可读呈现 | 全页截图 | 每个数字带来源/关联键/公式；XM 的 S-OPT 退步与 SH 的"几乎无差异"（gain 合计 +11 次）都按原样呈现 | 待确认 |

### 已知的视觉限制（不是待确认项，是设计取舍）

- 两个网格图是 13×13 / 16×16 的方块，格子密度高，单格 12px；在窄屏上格子会变小，
  精确读单格请用「选择格子」下拉框或账本。
- 证据行（`data-source` / 关联键 / 公式）默认展开而非折叠，页面因此偏长；
  这是为了让"每个数字都能查到出处"不需要额外点击。
- 深浅主题由系统 `prefers-color-scheme` 决定，页面不提供手动切换。
