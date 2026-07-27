# 2026-07-25 stock_share_capital 面值推断同期锚修复（决策 B）

**状态：代码已修并合并；prod 重跑 + B3 复验待做（Debian 库 MTU 黑洞，等网络恢复）。**

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

## 代码修复（已完成，stock_selector master `c31104e`）

`fix(share-capital): anchor par-calibration overlap at CSMAR_END (contemporaneous)`

`_read_overlap`（`stock_selector/backfill/modes.py`）的标定窗口从
`[MAX(trade_date)-180d, MAX(trade_date)]` 改锚到 **`[CSMAR_END, CSMAR_END+180d)`**，与最新
CSMAR 财报期同期，消除跨期漂移；仍 OOM 有界。`indicator_implied` 路径、`_STANDARD_PARS`、
`_PAR_TOLERANCE` 均未动。

- 新增 red-green 测试 `test_read_overlap_anchored_at_csmar_end`；
- 改名 OOM 边界测试 `test_read_overlap_window_bounded_oom`；
- 挪 2 个 par fixture 种子日进新窗（断言值不变）；
- **全套 1816 passed**；spec 审查 ✅ + code-reviewer **Approve**（4 Minor）。

计划全文：`stock_selector/docs/plans/2026-07-25-share-capital-par-contemporaneous-anchor.md`。

## 🛑 待做：等 Debian 库（`100.65.111.79`）黑洞恢复后一把过

黑洞诊断（2026-07-25 四连测）：ICMP 50% 丢包 RTT 2309ms、TCP 握手过、小查询超时 = MTU 黑洞，
根因 Debian 侧。**用户在家网络无法改善，需回到能连库的环境再跑。**

恢复后按序执行（工具已预置）：

```bash
# 0) 先四连测确认黑洞已清（见 memory ops-tailscale-blackhole-diagnosis）

# 1) 存 prod 基线（写 gap_before.csv + valued_tickers_before.csv，应报 gap≈696）
cd /home/elfbob/claude-code/style_timing_signal/data_fixes/2026-07-25-share-capital-par
python verify_par_recovery.py --phase before

# 2) 沙箱先验（stock_selector_test，不动 prod）
cd /home/elfbob/claude-code/stock_selector
.venv/bin/python -m stock_selector.backfill.cli share-capital --use-test

# 3) prod 重跑（UPSERT stock_share_capital；Pi5 由同步链自动跟）
.venv/bin/python -m stock_selector.backfill.cli share-capital

# 4) 验恢复 + 出尾巴清单（写 gap_after.csv + tail.csv；应报 recovered≈639 / 残 tail≈57 /
#    REGRESSION 必须=0）
cd /home/elfbob/claude-code/style_timing_signal/data_fixes/2026-07-25-share-capital-par
python verify_par_recovery.py --phase after

# 5) 护栏下 B3 复验（看 DATA_MISSING_SHARES 从 46,004 塌到尾巴量、34 个纯 SHARES 月尤其
#    2023 全年是否解锁）
cd /home/elfbob/claude-code/style_timing_signal
systemd-run --user --scope -p MemoryMax=8G \
  .venv/bin/python -m backtest.b3_eval --data-end 2023-12-31
python /tmp/.../scratchpad/analyze_preflight.py   # 或重写三件套统计
```

**若第 4 步 recovered 明显少于 639，或 REGRESSION > 0**：审查员 M1 提示前向窗口有轻微下偏
（股本长期上行）。旋钮 = 把 `_read_overlap` 改居中窗口 `[CSMAR_END−90d, CSMAR_END+90d)`
或缩小 `recent_days`，再重跑第 2–4 步。

## 待用户决定

- **57 尾巴票**（`tail.csv`：面值不落标准档，散在 ~0.2–0.9 / ~1.2–1.6 / 个别 ~20）：
  Wind 定向回填 `total_shares` 历史 vs 接受为 B3 豁免。**不自动处理。**
- 何时 push origin（style_timing_signal main 领先 ~38、stock_selector master 领先 3）。

## 回滚

代码回滚 = `git revert c31104e`（stock_selector）。数据侧重跑是幂等 UPSERT，本次尚未执行；
一旦执行，`gap_before.csv` / `valued_tickers_before.csv` 是重跑前的完整基线快照，可据以
核对任何意外变更。
