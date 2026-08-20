# 2026-08-20：DP 死因子修复 + 换源拼接（R3）+ 2026-06 腿名单回补（R2）

> 方向二（数据底座修复）执行台账。立项诊断 = `docs/plans/2026-08-20-data-foundation-repair.md`。
> 用户批准：R2（~1,000 格）+ R3（零额度工程）「确认，开始」。

## R2：wset 回补 932409/932408 的 2026-06 期腿名单

- 工具：`stock_selector.backfill.cli index-constituent-history`（as_of=2026-06-15，
  每腿一次 wset 调用，含 i_weight）。
- 回执：**Job 10000001745 completed rows=207**（932409.CSI 成长腿）、
  **Job 10000001746 completed rows=307**（932408.CSI 价值腿）。合计 514 格，额度内。
- 已验证入库：`index_constituent` 两腿各 4 期（2025-01-24 / 2025-06-16 / 2025-12-15 /
  **2026-06-15**）。
- 回滚：`DELETE FROM stock_selector.index_constituent WHERE index_code IN
  ('932409.CSI','932408.CSI') AND effective_date='2026-06-15'`（预期 rowcount 514）。
- 注意：0B「真值直通」消费的是 **932000 母指数名单**（样本空间）而非腿名单；
  腿名单的消费方 = 腿级直通对照与命中率诊断。

## R3-a：⚠️ 缺陷修复——DP 因子在所有 Gate 0 运行中恒为 0（死因子）

- **病灶**：`factor_panel` 把红利**事件行**（年度/半年度，无季度 YTD 链）喂给按季
  差分的 `pit_ttm_with_known`（`_ttm_latest`）→ 全量返回空 → `fillna(0)` →
  DP 恒 0 → 截面 z 后恒 0 → **价值得分实际只有 BP/EP(/CFP)**，违背官方
  「价值 = D/P + B/P (+ CF/P) + E/P」规格（DP 权重 1/3 金融 / 1/4 非金融）。
- **实测证据（独立于 ρ）**：茅台/平安/格力在 2024-10-31 / 2024-12-31 / 2025-04-30
  三个 asof 下 `_ttm_latest` 全 EMPTY；原始红利行本身完好（年报+中期、ann 正常）。
- **修法**：新增 `dividend_ttm_events`——修正可知日落在 (asof−366d, asof] 的事件行
  求和（=「近 12 个月已宣告分红」市场惯用口径，与 Wind dividend_yield 分子同口径）。
- **测试**：`tests/test_bt_pure_style_builder.py` 新增 4 条（年报+中期求和窗口 /
  未宣告·可知日缺失·超窗剔除 / 空输入 / 换源边界），全套 32 过。

## R3-b：换源拼接——2026 年起 DP 走 stock_indicator.dividend_yield

- csmar dividend 停更（end_date ≤ 2025-03-31）→ 2026 年起调样拿不到 2025 年报分红。
- `DP_INDICATOR_START = 2026-01-01`：边界前 csmar 事件行路径（Gate 0 锚区间口径
  统一），边界起 `_fetch_dp_indicator`（日频股息率，30 天回看，NULL=未分红→0）。
  单一截面单一源；因子进截面 z，源间尺度差不影响排序。
- **交叉验证（2024-12-31 截面 × 932000 真值 2000 只，双源都新鲜）**：
  是否分红判定一致率 **99.0%**；每股股利 Spearman **0.9747**（双正子集 0.9806，
  中位比值 wind/csmar = **1.000**）；比率层 Spearman **0.9713**。
  （对照：修复前死路径下一致率仅 26.1%。）

## 三闸重跑（修复后重登锚）

- 首跑登记值的产物备份在本目录（gate0{r,a,b}_result*.json/csv、
  replication_dev_v5_series.csv，均为 2026-08-19 首跑）。**首跑值永久登记不变**；
  修复后新值另行登记于修复台账（docs 收官文档）。
- 跑法：`setsid nohup` 顺序 0B → 0R → 0A，日志 `backtest/output/r3_rerun.log`。
- 预期方向：DP 复活会改变全窗复刻序列；0R 锚复现按设计会**有意漂移**（这是
  修复不是漂移事故）。另 index_daily 官方序列今日已换修正批次（6-16 妖点剔除
  条款不变）。
