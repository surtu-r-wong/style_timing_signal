# 年度集中度分解常规化（2026-07-12）

## 1. 命题

用户追问"现有信号全量胜出，是否只在一个特定时期表现特别突出"。ad-hoc 分解已给出答案
（13 个自然年 lf Sharpe 全正、剔最强两年 2015+2025 后仍 1.21、滚动 3 年从未转负），
用户拍板把它变成常规产出。本设计把该分解固化为 `backtest/yearly.py`，
成为与 `baseline.py` 并列的观察面：**baseline 回答"三窗/三口径/分段谁强"，
yearly 回答"强是不是靠个别大年撑的"**。

## 2. 口径（与生产完全同秤）

- 数据：committed 信号 CSV（复用 `baseline.SIGNALS` / `load_signal`）+ PG
  `index_daily` 标的 + 修正后 blend carry（固定 50/50）。
- 引擎：`run_strategy`（全日历 / 3bp / T+1），long-flat = `production_position(factor)`
  （与 `production.py` 逐位一致），对称 = `to_position(factor)`。
- 默认 blend，`--kou-jing` 可切 500/1000；三条线全跑。

## 3. 接口（纯函数可离线测）

```
yearly_table(ret_lf, ret_sym, bh, pos_lf) -> DataFrame
  # 每自然年一行: days, lf_ann, lf_sharpe, sym_ann, sym_sharpe, bh_ann,
  #               excess(=lf_ann-bh_ann), pct_long(shift(1)后持多日占比),
  #               log_contrib_pct(该年对 lf 累计对数收益贡献占比; 总和≤0 时 NaN)
concentration_summary(ret) -> dict
  # sharpe_full / sharpe_ex_top1 / sharpe_ex_top2(按年度对数贡献剔除)
  # ex_top1_year / ex_top2_years
  # roll3y_min / roll3y_min_date / roll3y_median / roll3y_neg_share(735d 满窗; 样本不足→NaN)
build_yearly_report(cost_bps, db, kou_jing) -> (yearly_df, conc_df)   # 编排, 读 PG
```

集中度摘要只作用于 **long-flat（生产口径）** 收益序列——命题就是"生产信号是否
靠个别时期"；对称口径逐年列在 yearly 表里供对照，不进摘要。

## 4. 输出

- `backtest/output/yearly_decomposition.csv`（signal × year 行）
- `backtest/output/yearly_concentration.csv`（signal 一行摘要）
- CLI：`python3 -m backtest.yearly [--kou-jing blend] [--cost-bps 3]`，console 两表。

## 5. 测试锚（TDD，全离线合成）

1. 两年合成序列 → 逐年 ann/sharpe/days/excess/pct_long 精确断言；log_contrib 总和 100。
2. 单一大年合成 → ex_top1 恰好等于剔除该年后手算 Sharpe；ex_top1_year 正确。
3. 样本 < 735 日 → roll3y 字段 NaN 不崩；全零收益年 → sharpe 0.0（沿 `metrics.sharpe`）。
4. schema 合同守卫：两张表列名精确匹配（下游/仪表盘若消费，改列必炸测试）。

## 6. 边界外（YAGNI 登记）

- **不动挑战者裁决机器**（momentum head2head / 五探针）：那套机器有预登记重开条件
  （出 PASS 前先改置换选优）。若未来重开挑战者评估，`concentration_summary`
  对 candidate 收益序列一行可接入 head2head 表——本次不接。
- 不做月度/季度粒度（年度已回答命题）；不做窗口加权（分窗 maximin 纪律已覆盖选择端）。
