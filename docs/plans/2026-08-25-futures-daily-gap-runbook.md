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

## 根治：方案 B 已实现（用户 2026-08-25 裁「都按建议跑」）

`tools/topup_futures_daily.py` —— gateway 取数 → market-monitor writer API
→ `public.futures_daily`（后端按 `(symbol, trade_date)` upsert，幂等）。
可无人值守，不需要 Windows 桌面会话。

**写入路径遵 market-monitor 客户端硬规**：不直连 SQL，POST `/api/data/daily`，
primary(Debian)→fallback(Pi5)，host 从 `config/settings.yaml` 的
`market_monitor_writer` 段读、**不硬编码**。

### ⛔ 前置条件：网关缺 `oi` 字段（推荐方案 B 时我不知道这一条）

gateway 是**哑管道**：每个端点向 Wind 要哪些字段，由 Windows 机上
`wind_gateway/config.yaml` 的 `fetchers.<name>.wsd_fields` 决定
（`endpoints.py:157-163` 读它）。实测现有端点：

| 端点 | 字段口径 | 有 `oi`？ |
|---|---|---|
| `/fetch/price` | 股票：open/high/low/close/volume/**amt/turn/adjfactor** | **无** |
| 其余 35 个端点 | 基金/财务/成分/EDB… | 无期货端点 |

而 carry 的定义是「**持仓量最大**的主力合约的年化基差」（`backtest/data.py:43`
`day_df.loc[day_df["oi"].idxmax()]`）—— **没有 `oi` 就选不出主力合约，carry 算不出来**。

**⇒ 需要你在 Windows 机上做一次网关侧改动**（我不碰那台机器）：

1. `wind_gateway/config.yaml` 加一个 fetcher：

   ```yaml
   fetchers:
     futures:
       wsd_fields: "open,high,low,close,volume,oi,amt,settle"
       wsd_options: ""
   ```

2. `wind_gateway/endpoints.py` 加 `/fetch/futures`（与 `/fetch/price` 同形，
   `endpoints.py:150-163` 抄一份改 fetcher 名即可）——这是**代码改动**，
   在 `stock_selector` 仓，需重新部署到 Windows 机。
3. 重启网关或 `POST /admin/reload`。

⚠️ **不要图省事往 `fetchers.price.wsd_fields` 里加 `oi,settle`** —— `/fetch/price`
是 `stock_daily_price` 写入链的共用端点，加列会波及所有消费方。

**在网关改好之前**可以 `--endpoint /fetch/price` 跑通全链路，但 `oi`/`settle` 会是空；
脚本对此有显式体检（`carry_readiness`），会打印 `⛔ 全空 —— carry 算不出`
而**不是**静默写入一批 NULL。

### 另一条路（未采纳，登记备查）

给 `backfill_optimized.py` 配 Windows 计划任务做增量日更 —— 它是交互式脚本
（跑完 `input("按回车键退出...")`），且 360 拒 WMI（见 `ops-b3-windows-execution-box`），
要先改成非交互。仍然依赖 Windows 桌面会话。

### 用法

```bash
python3 tools/topup_futures_daily.py \
    --codes "IC2609.CFE,IM2609.CFE" --start 2026-04-30 --end 2026-08-25 [--dry-run]
```

合约名单**不内置**（哑管道原则的客户端侧延伸）；补 118 天缺口用的 16 行清单见
`docs/plans/2026-08-25-futures-daily-gap-tasks.csv`。

⚠️ **2026-08-25 当日 Wind 日配额已耗尽**（`-40522017`，另一会话的 indicator 洞补撞的墙），
真跑要等配额恢复。当日已冒烟验证：鉴权通过、绕代理成功、请求到达网关、
配额错误被完整暴露（`gateway 429: {"error":"quota_exceeded",...}`）而非静默吞掉。

### 2026-09-02 补充：网关补丁已备好，可直接套用

`docs/plans/2026-09-02-gateway-futures-fetcher.patch`（对 `stock_selector` 仓根 `patch -p1`）：

- `wind_gateway/endpoints.py`：新增 `/fetch/futures`（与 `/fetch/price` 同形、独立 fetcher `futures`）。
- `wind_gateway/config.yaml.example`：新增 `fetchers.futures`（`open,high,low,close,volume,oi,amt,settle`）。
- `wind_gateway/tests/test_endpoint_futures.py`：3 个测试（oi/settle 列穿透、不污染 `/fetch/price`、缺配置 → `fetcher_not_configured`）。

验证：在网关代码的隔离副本上 `pytest wind_gateway/tests` **171 passed**（168 旧 + 3 新）；
删掉端点后新测试 2 红（变异验证）。补丁未落入 `stock_selector` 工作树（当时另一会话在动该仓）。

部署步骤（Windows 机，需 Wind 终端在线）：
1. `stock_selector` 仓根 `patch -p1 < .../2026-09-02-gateway-futures-fetcher.patch`，跑 `pytest wind_gateway/tests`。
2. 生产 `wind_gateway/config.yaml`（不在 git 内）加 `fetchers.futures` 段，值同 example。
3. 重启网关或 `POST /admin/reload`；`GET /fetch/futures?codes=IC2609.CFE&start=...&end=...` 应返回含 `oi`、`settle` 的列。
4. 回到本仓跑 `tools/topup_futures_daily.py`（合约名单见上文 16 行清单），`carry_readiness` 体检必须不再报 `⛔ 全空`。

### 2026-09-03 部署记录与现状更正

- 网关补丁已部署：部署版 `D:\wind_gateway\endpoints.py`（8 月 24 日独立改过，与仓里版本不同）套补丁后重启，
  `/fetch/futures` 实测返回 open/high/low/close/volume/oi/amt/settle 全有值（IC2609/IM2609 两日），`/fetch/price` 列不变。
  部署套件在 `D:\wind_gateway_futures_patch\`。**事故**：套件里 apply.ps1 用 PowerShell `Get-Content/Set-Content` 追加
  config.yaml，把 UTF-8 中文注释写成 ANSI，网关启动 `UnicodeDecodeError`；已从脚本的字节级备份恢复并以 UTF-8 重插
  （坏文件留作 `config.yaml.broken_ansi_20260903`）。教训入记忆 `ops-powershell-utf8-config-corruption`。
- **缺口已由另一条通路闭合**：`public.futures_daily` 自 2026-04-30 至 09-02 共 85 个交易日，IC/IM 每日各 4 合约、全部带 oi，
  `source='exchange'`，由 market-monitor `data-collecter/exchange_daily/loader.py` 按交易所日数据写入（08-31 起、每日更新到 09-03）。
  本文「补缺口操作单」与 `tools/topup_futures_daily.py` 降级为**备用通路**，未再写入。
- C1 复检：新增样本 84 个交易日（k=10 约 +8 观测），功效与 07-09 几乎相同，**不重跑**；重开条件已在登记表量化
  （k=10 非重叠观测 ≥ 340，约 2029-02）。
