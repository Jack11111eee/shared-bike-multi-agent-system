# 底图来源与离线分发许可（Phase 5 · F21）

> 本文件记录每一份进入 `replay/` 的底图素材：谁发布的、从哪下载、什么时候取的、
> 什么许可、许可能不能随包分发、原始与发布文件的摘要。机器可读版本在
> `replay/basemap/manifest.json`，本文件是它的说明与决策记录。

## 1. 选型结论

| 图层 | 数据集 | 许可 | 可否离线随包 | 用途 |
|---|---|---|---|---|
| 陆域/海岸线/岛屿 | **GSHHG 2.3.7** | LGPL-3.0-or-later | 可以 | 两源的陆地轮廓、厦门岛与九龙江口、长江口 |
| 内陆水系（河流/运河） | **OpenStreetMap / Geofabrik** | ODbL-1.0 | 可以（署名 + 保持 ODbL 条款） | 上海黄浦江/苏州河、厦门内河 |
| 行政界 | **OpenStreetMap / Geofabrik** | ODbL-1.0 | 可以（同上） | XM 镇街（admin_level 8）、SH 区（admin_level 6） |

两源合计发布 **134 KB**（`replay/xm/basemap.js` 35 KB + `replay/sh/basemap.js` 99 KB），
加 `manifest.json` 与许可文本共 **168 KB**，远低于 2 MiB 预算。

## 2. 候选与否决记录

| 候选 | 结果 | 理由 |
|---|---|---|
| **Natural Earth 10m**（coastline / land / admin_1 / rivers） | **否决** | 厦门岛在 10m 数据里只有 30 个顶点、轮廓明显失真（岛内轮廓被画成近似菱形），作为"足以辨认空间位置"的底图不合格；上海市界同样只有 213 点。分辨率不够，不是许可问题。 |
| **Overpass API 实时拉取海岸线** | **否决** | 公共实例持续返回 502（队列满），且依赖在线服务与运行时刻，与"离线随包、可复现"冲突。 |
| **OSM coastline-split 全量（osmdata.openstreetmap.de，96 MB）** | **否决** | 体量过大且需要额外切分工具；同一信息 GSHHG 以 149 MB 归档、2.6 MB 单层给出，且自带 `.prj` 与许可文本。 |
| **GSHHG 2.3.7 `h`（high）级** | **采用** | 厦门岛 30 点、上海长江口 39 点，海岸线在 20 m/px 的显示尺度上足够；LGPL 明确允许分发，且归档内自带 `LICENSE.TXT`。 |
| **Geofabrik 省级 free shapefile** | **采用** | 上海 47 MB / 福建 93 MB 归档，裁剪后分别只剩 482 KB / 62 KB；ODbL 允许发布派生地图，署名即可。 |
| **在线瓦片（高德/腾讯/OSM tiles）** | **否决** | 需要账号或运行时请求，明确超出本期范围（§1.4）。 |

## 3. 获取记录

| 项目 | 值 |
|---|---|
| 获取日期 | 2026-11-01 |
| GSHHG 归档 | `gshhg-shp-2.3.7.zip`，sha256 `8dbbe7e071e77e9e75f2d639239099ebca8d5c16d6a07df8169729d49f15cf41`，149,157,845 B |
| GSHHG 来源页 | https://www.ngdc.noaa.gov/mgg/shorelines/gshhg.html |
| GSHHG 下载地址 | https://github.com/GenericMappingTools/gshhg-gmt/releases/download/2.3.7/gshhg-shp-2.3.7.zip |
| OSM 上海归档 | `shanghai-261031-free.shp.zip`，sha256 `3027496ec551fd45e39f9040e2d21624d67c235b008f708ec4e6bcc38163d008`，47,509,398 B |
| OSM 福建归档 | `fujian-261031-free.shp.zip`，sha256 `4d8774eaff1fa7319814b3276a0ca76b7a10047fab5415b8cc2b1f5d6071c1f6`，93,535,421 B |
| OSM 来源页 | https://download.geofabrik.de/asia/china.html |

裁剪后的原始提取与摘要见 `data/basemap/raw/manifest.json`；发布文件的摘要见
`replay/basemap/manifest.json`。

## 4. 裁剪与简化

- **窗口**：XM `(118.02, 24.39, 118.24, 24.60)`，SH `(121.24, 31.04, 121.66, 31.46)`，
  比网格外缘再放宽，保证加 10% 上下文后仍有底图。
- **裁剪是几何裁剪，不是坐标过滤**：多边形用 Sutherland–Hodgman（贴着窗口边闭合，
  不会用一条从未存在的弦把海岸线接起来），折线用 Liang–Barsky（保留窗口内的子段）。
- **线要素按可见长度筛选**：水系 ≥1.5 km、行政界 ≥2 km。上海有上千条亚公里级运河，
  全画出来会把黄浦江埋掉。这是**选择规则**，不是简化，记录在
  `data/basemap/raw/manifest.json` 的 `selection` 字段里。
- **简化**：Douglas–Peucker，容差 **0.35 px**（约为验收阈值 0.5 px 的一半），
  在投影后坐标上做。原始/发布顶点数记在 `replay/basemap/manifest.json`。
- **不做**的事：不画道路。搬运箭头是格心到格心的调度关系，画街道会诱导读者把它
  当成路线规划。

## 5. 许可与署名

- GSHHG：LGPL-3.0-or-later。随包三份文本：`replay/basemap/licenses/GSHHG-LICENSE.txt`
  （归档自带的许可声明）、`LGPL-3.0.txt`（LGPL v3 全文，来自同一归档的
  `COPYING.LESSERv3`）、`GPL-3.0.txt`（LGPL v3 引用到的 GPL v3 全文）。
  三者缺一，构建直接失败——"指向一份许可"不等于"随包提供该许可"。
- OSM/Geofabrik：ODbL-1.0。完整许可文本随包：`replay/basemap/licenses/ODbL-1.0.txt`。
  构建同时校验每份文本的长度与标识行，占位文件或截断文件不会被当成许可。
- **重建时谁复制什么**：`python3 -m bike_agent.basemap.prepare` 会从 GSHHG 归档里复制
  它确实拥有的两份（`LICENSE.TXT`、`COPYING.LESSERv3`）；归档里**没有** GPL v3 全文，
  两份归档里都**没有** ODbL 全文，所以这两份要人工放进 `replay/basemap/licenses/`
  （来源分别是 <https://www.gnu.org/licenses/gpl-3.0.txt> 与
  <https://opendatacommons.org/licenses/odbl/1-0/>，写成普通文本）。
  缺任何一份，`prepare` 在写提取文件**之前**就失败并逐份给出获取位置，不会留下一个
  "提取好了但构建拒绝发布"的半成品。仓库里已随包的四份可以直接沿用。
- **归档成员与随包文件逐字节相同（可离线复核）**。下表两行是 `gshhg-shp-2.3.7.zip`
  内成员的 sha256，与仓库里同名文件的 sha256 一致；有归档的人可以自己 `shasum -a 256`
  对一遍，不必相信本文档的叙述：

  | 归档成员 | 随包文件 | sha256 |
  |---|---|---|
  | `LICENSE.TXT` | `replay/basemap/licenses/GSHHG-LICENSE.txt` | `dc53a4833bf9121360a5286917683f041439327353ffe0231206c6b746005b81` |
  | `COPYING.LESSERv3` | `replay/basemap/licenses/LGPL-3.0.txt` | `ea7d049c7705dc13afc202dd18e1827f3484f8212fd3fa7b82fc4a0c363432c9` |

  `GPL-3.0.txt` 与 `ODbL-1.0.txt` 不在任何归档里，它们的内容由各自官方发布页决定，
  所以不列摘要——列了也只能证明"当时下载的是这一份"，证明不了"这就是官方文本"。
- 页面上的署名是**普通文本**（`© OpenStreetMap contributors（ODbL 1.0）`、
  `GSHHG 2.3.7（NOAA / University of Hawaii，LGPL-3.0）`），来源 URL 只写在
  manifest 与图注里，**不写成外部 `href`/`src`/CSS 引用**——离线扫描（Phase 2 NF-13、
  Phase 4 NF-23）仍然原样生效。

## 6. 运行期约束

- 站点双击可用，**没有任何运行时外网请求**：底图以 `replay/<source>/basemap.js`
  的经典脚本随包，页面用 `file://` 即可加载。
- 底图只提供位置语境，**不携带任何业务数值**。页面上每个数字仍带
  `data-source` / `data-join-key` / `data-formula`，指向 `outputs/` 的产物。
- 重做底图（`python3 -m bike_agent.basemap.prepare`）是**开发期**动作，
  不在 `bike-agent replay` 或 `bike-agent repro` 的路径里。
