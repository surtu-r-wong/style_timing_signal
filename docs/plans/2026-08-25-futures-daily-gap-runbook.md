# `futures_daily` 断更 118 天：更正的诊断 + 补缺口操作单（2026-08-25）

## ⚠️ 先更正一条登记错误

历史记忆里有两处互相矛盾的登记，今天核准了：

| 登记 | 出处 | 判定 |
|---|---|---|
| 「写入方 = `data-collecter/collector.py`（WindPy 直连，须桌面会话）」 | 2026-07 优化 roadmap | ❌ **错**。`collector.py` 里 `futures_daily` grep **零命中**；它是**实时行情监控**器，采的是 `config.yaml` 里手挑的螺纹/焦煤/铜/镍/铝/金/银 + 3 只股票，**根本不含 IC/IM** |
| 「写入方 = `backfill/backfill_optimized.py` + `backfill_multiasset.py`」 | 2026-08-12 负债台账 | ✅ **对**。`backfill_optimized.py` 里 4 处 `futures_daily`，`from WindPy import w` + `w.wsd`，经 `http_client.post_with_fallback` 走 `/api/data/daily` 落库 |

**这条更正改变了处置方式**：不存在「重启 collector 就能恢复」的路径。

## 真实诊断

`futures_daily` **从来没有日更自动化**。它是一张**手动跑批次**填出来的表：
`backfill_optimized.py` 是带 CSV 任务表、断点续传、多线程的**回填**脚本，交互式
（跑完 `input("按回车键退出...")`），只能在桌面控制台跑。

证据闭环：

- 最近一次 backfill 日志 = `data-collecter/backfill/logs/backfill_20260424_100448.log`（**2026-04-24**）
- `futures_daily` 最大 `trade_date` = **2026-04-29**
- 之后没有任何 backfill 日志

⇒ **断更 = 04-24 之后没人再跑过批次**，不是进程挂了。

**推论（比补缺口更重要）**：即便这次补上，只要没有自动化，**缺口会再次打开**。
C1 基差率复检要的是「样本拉长后重测」，那是个持续性需求，一次性回填答不了。

## 补缺口操作单（在 Windows 机的桌面会话里跑）

前置：Wind 终端已登录（WindPy 必须在桌面会话，ssh 起会 `-40520004`）。

1. 把任务表拷到 Windows 机的 `data-collecter/backfill/` 下，命名 `tasks_futures_gap.csv`。
   内容已生成在本仓 **`docs/plans/2026-08-25-futures-daily-gap-tasks.csv`**（16 行）：

   ```
   symbol,start_date,end_date
   IC2605.CFE,2026-04-30,2026-08-25     ← IC/IM × 2605/2606/2607/2608/2609/2610/2612/2703
   ...
   ```

   合约集合的依据：断更前实测每个品种同时有 4 个活跃合约（当月 + 次月 + 后两个季月），
   3~4 月实测为 2603/2604/2605/2606/2609/2612。按此推 5~8 月的并集 = 上述 8 个月份。
   不存在的合约/日期组合 Wind 返回空，不会污染。

2. 跑：

   ```
   cd data-collecter\backfill
   python backfill_optimized.py --csv tasks_futures_gap.csv
   ```

   交互提示 `是否立即执行回填? (y/n)` 回 `y`。

3. 跑完告诉我，我这侧验缺口（一条 SQL，零 Wind 额度）：
   `SELECT symbol, min/max(trade_date), count(*) FROM public.futures_daily WHERE symbol LIKE 'IC%' OR 'IM%'`，
   核对 2026-04-30 → 今日的交易日是否连续、每日是否至少有一个合约带 `oi`（carry 取 oi 最大的当主力）。

## 影响面（为什么这事不急）

- **日更部署链不消费 carry**（沿调用链核实，非只 grep 目录）：
  `run_daily_signals.sh:223` → `python -m backtest.production` → `production_position(raw[col])`
  = `(signal > threshold).astype(int)`，**纯阈值，输入只有信号 CSV**。
  `backtest/data.py`（含 `load_carry`）确实在 import 图里，但只是 `backtest.baseline`
  的顶层 import，生产路径**从不调用**它。
  ⇒ 118 天断更**没有影响过任何一天的部署仓位**。carry 只进回测的收益核算口径。
- 卡住的只有两件研究侧的事：
  1. **C1 基差率复检**（八轴关帐后唯一预登记的复检项，p=.090 / 净 Sharpe 0.51 / 语义=风险预警）；
  2. carry 承载方式分析里「**当前**贴水水平」那半个问题（2026-08-24 的描述性分析只能用
     2022-07~2026-04 共同窗作答）。

## 若要根治（需另行立项，本文不主张）

两条路，都不是「重启」能解决的：

- **A**：给 `backfill_optimized.py` 配一个 Windows 计划任务做增量日更 —— 但它是交互式脚本，
  且 360 拒 WMI（见 `ops-b3-windows-execution-box` 的实测），要先改成非交互。
- **B**：Linux 侧走 gateway 建轻量 topup（只取 IC/IM 的 close/oi），可无人值守，
  与现有 `tools/topup_index_daily.sh` 同姿势；代价是新管道 + 写 market-monitor
  `public` schema，须按该仓规矩过。
