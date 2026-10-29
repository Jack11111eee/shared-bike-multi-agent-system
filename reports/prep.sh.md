# 预处理与时空聚合报告 · SH

本报告由 `bike-agent prep` 生成，对应 F2。空间单元为规则网格（D2），时间单元为 1 小时（D3）。

## 清洗规则（逐条）

| 规则 | 移除行数 | 剩余行数 | 原因 |
|---|---|---|---|
| duplicate_trip_id | 0 | 102361 | same trip_id appears more than once; first occurrence kept |
| missing_coordinates | 0 | 102361 | at least one of start/end latitude or longitude is missing |
| abnormal_duration | 54 | 102307 | duration outside [60s, 21600s] |
| out_of_bounds_coordinates | 2387 | 99920 | start or end outside the 0.5%-99.5% coordinate envelope |
| pseudo_trip_same_origin_destination | 0 | 99920 | start and end within 50 m (or identical coordinates) |

输入 102,361 行，保留 99,920 行，共移除 2,441 行（2.38%）。

## 空间单元（D2）

| 指标 | 数值 |
|---|---|
| 网格边长 | 2000 m |
| 网格尺寸 | 15 × 17 |
| 有效格子数 | 255 |
| 经度范围 | 121.297000 → 121.603000 |
| 纬度范围 | 31.098000 → 31.389000 |

## 时间单元（D3）

| 指标 | 数值 |
|---|---|
| 时间桶数 | 768 |
| 覆盖日期数 | 32 |
| 出现的小时 | 0, 1, 2, 3, 4, 5, 6, 7, 8, 9, 10, 11, 12, 13, 14, 15, 16, 17, 18, 19, 20, 21, 22, 23 |
| 首桶 | 2016-08-01 00:00:00 |
| 末桶 | 2016-09-01 23:00:00 |

## 聚合与守恒（F2-2 / F2-5）

| 指标 | 数值 |
|---|---|
| 借出总量 out_count | 99,920 |
| 归还总量 in_count | 99,920 |
| 清洗后行程数 | 99,920 |
| 借出守恒 | 是 |
| 归还守恒 | 是 |
| 每格每桶平均借出 | 0.510 |

## 零值报告（F2-4）

| 指标 | 占比 |
|---|---|
| 零借出格子-时间片 | 77.15% |
| 零归还格子-时间片 | 76.84% |
| 借出与归还同时为零 | 70.46% |

> 零借出占比低于 80%，该粒度可直接用于预测与仿真。
