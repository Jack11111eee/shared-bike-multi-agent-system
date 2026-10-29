# 预处理与时空聚合报告 · XM

本报告由 `bike-agent prep` 生成，对应 F2。空间单元为规则网格（D2），时间单元为 1 小时（D3）。

## 清洗规则（逐条）

| 规则 | 移除行数 | 剩余行数 | 原因 |
|---|---|---|---|
| duplicate_trip_id | 0 | 220675 | same trip_id appears more than once; first occurrence kept |
| missing_coordinates | 0 | 220675 | at least one of start/end latitude or longitude is missing |
| abnormal_duration | 6310 | 214365 | duration outside [60s, 21600s] |
| out_of_bounds_coordinates | 6765 | 207600 | start or end outside the 0.5%-99.5% coordinate envelope |
| pseudo_trip_same_origin_destination | 11918 | 195682 | start and end within 50 m (or identical coordinates) |

输入 220,675 行，保留 195,682 行，共移除 24,993 行（11.33%）。

## 空间单元（D2）

| 指标 | 数值 |
|---|---|
| 网格边长 | 1000 m |
| 网格尺寸 | 13 × 13 |
| 有效格子数 | 169 |
| 经度范围 | 118.068263 → 118.189247 |
| 纬度范围 | 24.435954 → 24.548138 |

## 时间单元（D3）

| 指标 | 数值 |
|---|---|
| 时间桶数 | 20 |
| 覆盖日期数 | 5 |
| 出现的小时 | 6, 7, 8, 9 |
| 首桶 | 2020-12-21 06:00:00 |
| 末桶 | 2020-12-25 09:00:00 |

## 聚合与守恒（F2-2 / F2-5）

| 指标 | 数值 |
|---|---|
| 借出总量 out_count | 195,682 |
| 归还总量 in_count | 195,682 |
| 清洗后行程数 | 195,682 |
| 借出守恒 | 是 |
| 归还守恒 | 是 |
| 每格每桶平均借出 | 57.894 |

## 零值报告（F2-4）

| 指标 | 占比 |
|---|---|
| 零借出格子-时间片 | 29.76% |
| 零归还格子-时间片 | 27.13% |
| 借出与归还同时为零 | 25.18% |

> 零借出占比低于 80%，该粒度可直接用于预测与仿真。
