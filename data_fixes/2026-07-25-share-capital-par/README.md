# 2026-07-25 stock_share_capital 面值推断同期锚修复（决策 B）

**状态：2026-07-27 已完成代码修复、沙箱/生产重跑、双回归验收和 B3 复验。**

## 背景：这是什么问题

B3 preflight 的 `DATA_MISSING_SHARES` 阻断（2026-07-24 三件套分析确认：46,004 票·月、
2014-01..2023-12 全窗覆盖、2023-12 单月缺 680 只）来自 **696 只 A 股（SH/SZ）缺口票**。

这 696 只**不是没数据**。它们的 CSMAR 股本历史（1992–2023）在 `stock_share_capital` 里
全是 `par_unknown` 空节点（`total_shares` NULL），唯一有值节点是 2025-04 的
`indicator_implied`——在所有 formation 日之后，as-of 取不到 → 全窗判缺股本。

根因是 **par（面值）推断跨期错配**：面值靠 `A003101000 / (total_mv/close 隐含股数)` 反推、
吸附标准档 {1.0, 0.1} ± 5% 容差。`_read_overlap` 的标定窗口锚在 `MAX(trade_date)`（≈2026），
而 CSMAR `A003101000` 止于 `CSMAR_END = 2025-03-31`。两者差一年多，期间送转/增发/回购让股本
稀释 >5% → 隐含面值掉出容差 → 整段历史 `par_unknown`。

**实证（2026-07-25 对 prod 探针）**：
- 同期锚（CSMAR_END 之后的窗口）命中标准面值档 **639 / 696**；
- 当前 2026 口径只命中 **1 / 696**。
- 样本机制：`000060.SZ` 股本 2025-Q1→2026 涨 18.8% → 隐含面值从真值 1.0 掉到 0.842 → miss；
  `000061.SZ` 涨 17.0% → 0.855 → miss。

## 代码修复（已完成）

根因修复为 stock_selector `c31104e`：
`fix(share-capital): anchor par-calibration overlap at CSMAR_END (contemporaneous)`。

`_read_overlap`（`stock_selector/backfill/modes.py`）的标定窗口从
`[MAX(trade_date)-180d, MAX(trade_date)]` 改锚到 **`[CSMAR_END, CSMAR_END+180d)`**，与最新
CSMAR 财报期同期，消除跨期漂移；仍 OOM 有界。`indicator_implied` 路径、`_STANDARD_PARS`、
`_PAR_TOLERANCE` 均未动。

生产复跑前又在其上落定两阶段标定，stock_selector master `e11d73e`
（原审查提交 `c284ef7`）：
`fix(share-capital): use tight par window with safe fallback`。

- 第一阶段取 **`[CSMAR_END, CSMAR_END+30d)`** 的隐含面值中位数并吸附；
- 若不能吸附，再退回原有完整 180 日 supplied-overlap 中位数；
- 因有旧口径 fallback，原本能成功标定的票不会因紧窗口丢失；
- 标准面值、±5% 容差、180 日有界读取及 `indicator_implied` 路径均未改；
- 合入态全套：**1819 passed, 1 skipped, 347 deselected**；
- 两阶段 spec/质量审查及最终集成审查均 **Approve**，无 Critical/Important。

设计与计划：

- `docs/superpowers/specs/2026-07-27-share-capital-two-stage-par-calibration-design.md`
- `docs/superpowers/plans/2026-07-27-share-capital-two-stage-par-calibration.md`
- `stock_selector/docs/plans/2026-07-25-share-capital-par-contemporaneous-anchor.md`

## 2026-07-27 执行与验收实录

### 前置与基线

- 四连测全部 0 丢包，PostgreSQL `SELECT 1` 返回
  `(1, 'market_monitor', 'stock_selector')`。
- `gap_before.csv`：696 行，SHA-256
  `ea5bc078e4bacbe68832d106b5f60dfe0fba4ca015975b72929af175cfd223ef`。
- `valued_tickers_before.csv`：5,200 行，SHA-256
  `52c2d90217dc306f1b6a0f13c7f37603dc3c1abc357aaa6e90464fdf908e4631`。
- **不得再运行 `--phase before`，否则会覆盖这份原始生产基线。**

### 数据重跑

- `stock_selector_test`：写入 11,621 行，`par_unknown=2`，当前 A 股池 `5198/5198`。
- prod（仅执行一次）：写入 89,579 行，`par_unknown=464`，当前 A 股池 `5200/5200`。
- `verify_par_recovery.py --phase after`：recovered **639**、residual tail **57**、
  `REGRESSION (valued->unvalued)` **0**、`NEW HISTORICAL GAP REGRESSION` **0**。
- `gap_after.csv` SHA-256：
  `c161e6a62cc391b6f1e24fc194784043457c59243600310798829c6b1691db1e`。
- `tail.csv` SHA-256：
  `93653f5ad7cade2d03872bd7796966e60e94074d7445eaa8192e4885b0995223`。

### B3 复验（`data_end=2023-12-31`，8G MemoryMax）

- standalone preflight 正常以数据闸门退出码 2 结束；内存峰值约 3.60GB，无 OOM。
- `DATA_MISSING_SHARES`：**46,004 → 5,781 票·月（-87.4%）**。
- 2023 年：653 票·月、每月 53–55 只，2023-12 为 55（修复前为 680）。
- `DATA_MISSING_CLOSE`：202 票·月（未变）；`close_carry_forward`：13,475 票·月（未变）。
- 120 个 required formation 月仍因剩余 SHARES/CLOSE 尾巴 `DATA_BLOCKED`。
- `b3_eval` 正常以退出码 2 结束，最终 verdict：
  `DATA_BLOCKED / DATA_CONTRACT`，尚未进入候选统计。
- `coverage_audit.csv` SHA-256：
  `13c8af70650a24ba00c1b0890e979c487a0133589e6127967340e622426e9358`。
- `manifests/preflight.json` SHA-256：
  `cb4f8360804c16d7c58d2285d1d9e82fb0a3ad9a96fccc36ab4235269e49b035`。

style_timing_signal verifier 加固提交 `25e9c4b` 会同时阻断 valued→unvalued 和
`gap_after - gap_before` 新历史缺口；合入后全套 **813 passed**。

## 待用户决定

- **57 尾巴票**（`tail.csv`：面值不落标准档，散在 ~0.2–0.9 / ~1.2–1.6 / 个别 ~20）：
  Wind 定向回填 `total_shares` 历史 vs 接受为 B3 豁免。**不自动处理。**
- style_timing_signal 尚未 push；stock_selector `origin/master` 已于 2026-07-27 14:44
  由外部 push 更新到 `e11d73e`（本代理未执行 `git push`）。

## 回滚

两阶段代码回滚 = `git revert e11d73e`（会回到 `c31104e` 的 180 日同期锚口径）。
根因锚点回滚另需评估 `c31104e`。生产 UPSERT 已执行；回滚代码不会自动回滚数据。
`gap_before.csv` / `valued_tickers_before.csv` 是重跑前的完整基线快照，必须保留用于核对。

## 2026-07-27 B3 逐票影响审计（只读）

本轮没有执行 prod backfill、B3 preflight/eval 或 `verify_par_recovery.py --phase before`。
审计脚本使用只读事务，以最终 `tail.csv` 和 `coverage_audit.csv` 为不可变锚，复刻 B3
`size_exclusion` 原因优先级并逐月对账。

执行命令：

```bash
/home/elfbob/miniconda3/bin/python \
  data_fixes/2026-07-25-share-capital-par/build_b3_impact_audit.py \
  --tail data_fixes/2026-07-25-share-capital-par/tail.csv \
  --coverage-audit \
    /home/elfbob/claude-code/style_timing_signal/output/style_basket/b3/coverage_audit.csv \
  --settings \
    /home/elfbob/claude-code/style_timing_signal/config/settings.yaml \
  --output-dir data_fixes/2026-07-25-share-capital-par
```

输入锚：

- `tail.csv`：`93653f5ad7cade2d03872bd7796966e60e94074d7445eaa8192e4885b0995223`；
- `coverage_audit.csv`：`13c8af70650a24ba00c1b0890e979c487a0133589e6127967340e622426e9358`。

### SHARES 结果

- 逐月明细与 coverage 完全一致：**5,781 all / 5,445 required**；实际涉及 **56 只**。
- 汇总仍固定保留 tail 全集 **57 行**；`688347.SH` 于 2023-08-07 上市，截至
  2023-12-29 尚不足 180 天，因此实际 `DATA_MISSING_SHARES` 影响为 0、`in_pool_2023_12=False`。
- 最高优先级为 2023-12 仍在池且全 128 月受影响的活跃票；前十为
  `000035.SZ, 000156.SZ, 000301.SZ, 000498.SZ, 000547.SZ, 000603.SZ,`
  `000620.SZ, 000681.SZ, 000813.SZ, 000820.SZ`。

### CLOSE 结果

- 逐月明细与 coverage 完全一致：**202 all / 190 required / 14 tickers**。
- 14 只及 all 影响数：`000670.SZ(28), 000155.SZ(19), 000995.SZ(19),`
  `000520.SZ(15), 000629.SZ(15), 000792.SZ(15), 000950.SZ(15),`
  `002506.SZ(15), 600710.SH(15), 600732.SH(14), 600610.SH(13),`
  `000751.SZ(15), 000545.SZ(2), 600698.SH(2)`。
- 202 行全部是 formation date 原始价格行缺失；同日 `stock_suspension` 证据为 0，
  退市边界为 0，且 202 行全部存在后续非空 close，因此当前证据桶均为
  `UNEXPLAINED_EXACT_DATE_GAP`。这只说明需要继续核查停牌证据遗漏/价格源缺口，
  **不构成自动回填、豁免或 universe 剔除结论**。

### 产物哈希

- `shares_tail_impact_by_ticker.csv`（57 行）：
  `ff8a9123504d430225c0ea0618c6a07c06373b07f2826af8bff450d6a55d7a40`；
- `shares_tail_impact_detail.csv`（5,781 行）：
  `a2fd07c9cdf5ba6b1defb89850eaf5bea629c7e76cf372c95931c745eedead13`；
- `close_gap_impact_by_ticker.csv`（14 行）：
  `473d412f5de45546ceff30649f23711848199def754a609b60684d1a8206a8aa`；
- `close_gap_impact_detail.csv`（202 行）：
  `53496d6b57ec599d0839b4f8df8f72ea7e9fa31099f1baf65255e3b6e286a2f5`；
- `impact_audit_manifest.json`：
  `777818dd48a0fc4513f74391ce9c57b8bca0f5750173590ba894c93be22b1f74`。

下一步按汇总优先级对活跃 SHARES 票做 Wind 历史股本事实取证；CLOSE 202 则逐票核查
停牌记录与价格源，不与 SHARES 回填混写。
