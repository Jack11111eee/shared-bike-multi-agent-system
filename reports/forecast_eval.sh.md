# 短期需求预测评估 · SH

对应 F3。评估严格按时间切分（训练在前、测试在后），未使用随机切分。

## 切分与可复现参数

| 项目 | 取值 |
|---|---|
| 训练时间桶 | 594（2016-08-01 00:00:00 → 2016-08-25 19:00:00） |
| 测试时间桶 | 149（2016-08-25 20:00:00 → 2016-09-01 01:00:00） |
| 训练/测试比例 | 0.8 |
| 随机种子 | 42 |
| 模型 | LightGBM（Poisson） |
| 超参 | n_estimators=300, learning_rate=0.05, num_leaves=31, min_child_samples=20, feature_fraction=0.9, l2_regularization=1.0, objective=poisson, n_jobs=1 |
| 训练样本数 | 151470 |
| 测试样本数 | 37995 |

## 基线 vs 模型（F3-2）

| 目标 | 基线 MAE | 模型 MAE | MAE 变化 | 基线 RMSE | 模型 RMSE |
|---|---|---|---|---|---|
| 借出量 | 0.5336 | 0.5103 | -4.37% | 1.1416 | 1.0761 |
| 归还量 | 0.5396 | 0.5129 | -4.94% | 1.1605 | 1.0850 |

基线定义：每个格子在该小时的历史均值（仅用训练集计算），无历史时回退到格子均值、再回退到全局均值。

## 结论（F3-3）

在本数据源与本次时间切分上，LightGBM 在两个目标上的 MAE 均优于季节均值基线。提升幅度见上表；未调整测试集、未隐藏基线结果、未改用随机切分。

## 特征

`cell_id`、`hour`、`dow`、`is_weekend`、`gap_hours`、`lag1_out_count`、`lag1_in_count`、`roll3_out_count`、`roll3_in_count`、`cell_mean_out_count`、`cell_mean_in_count`、`cell_hour_mean_out_count`、`cell_hour_mean_in_count`

## 复现

```bash
bike-agent forecast --source sh
```

同一命令、同一数据、同一种子应得到相同误差（NF-1）。
