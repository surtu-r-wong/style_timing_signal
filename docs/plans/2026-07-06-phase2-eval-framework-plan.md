# Phase 2「修秤」评价框架 — 实施计划

> **For Claude:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task.

**Goal:** 建一个正确口径的回测/评价模块 `backtest/`，把现有三条信号线（hybrid20 / citic40d / equal_weight）作为对中证500/1000/50-50 的市场择时信号，跑出**真实 OOS 基线**（全日历收益 + 期货成本 + 贴水 carry + 多空分段 + 显著性），修掉 `optimize_signal.py` 的四个评价问题。

**Architecture:** 纯读 PG（`stock_selector.index_daily` 标的收益 + `public.futures_daily` 主力基差算 carry）+ `output/` 三条信号线输出；不碰信号生成。信号→离散仓位 `{−1,0,+1}`（对齐双引擎 §1.3）→ 全日历策略收益（T 收盘信号 T+1 收盘成交）→ 指标（多头段/空头段分列）→ 同换手随机 bootstrap 显著性。50/50 blend 由 500+1000 合成。

**Tech Stack:** Python 3.13（系统 python3，pandas/numpy/psycopg2/yaml 已装）、PostgreSQL 只读、pytest。复用 `signals/common/config.load_db_config`。

**设计依据:** `docs/plans/2026-07-03-optimization-roadmap-design.md` §0（交易口径/carry）、§1.3（双引擎离散仓位/多空分段评价）、§3（方向二四问 + 四步框架）。2026-07-06 用户确认。

**关键背景（执行者须知）:**
- 三条线信号列：hybrid20=`output/hybrid20/confirmed_signal.csv` 的 **`hybrid_20`**（已是 {−1,0,+1}）；citic40d=`output/citic40d/citic_style_signal_40d.csv` 的 **`factor_20`**（连续 [−1,1]）；equal_weight=`output/equal_weight/equal_weight_signal_20d40z.csv` 的 **`factor_value`**（连续 [−1,1]）。
- 标的：PG `stock_selector.index_daily` 的 `000905.SH`(中证500)/`000852.SH`(中证1000)，均 2013-03-06→2026-07-03；blend=两者日收益等权。
- carry 数据：`public.futures_daily`（schema `public`），列 `symbol,trade_date,close,settle,oi`；IC/IM 合约 `IC{YYMM}.CFE`/`IM{YYMM}.CFE`；主力=每日 `oi` 最大合约；IC 2015-04+，IM 2022-07+，**止于 2026-04-29**。无期货的日期（上市前/2026-04-29 后）carry=0。
- carry-aware 窗口：500 口径 2015+、1000 口径 2022+（IM 上市日锁死，改不了）；spot-only 全窗 2013+。
- 纪律：TDD，每 task 先失败测试。纯函数任务用合成数据，不连 PG。连库任务单独标注。

---

## Task 1：模块骨架 + 指标纯函数

**Files:**
- Create: `backtest/__init__.py`（空）
- Create: `backtest/metrics.py`
- Create: `tests/test_bt_metrics.py`

**Step 1: 写失败测试** `tests/test_bt_metrics.py`：合成一条已知日收益序列，断言各指标。

```python
import sys; from pathlib import Path
import numpy as np, pandas as pd
ROOT = Path(__file__).resolve().parents[1]; sys.path.insert(0, str(ROOT))
from backtest.metrics import ann_return, sharpe, max_drawdown, calmar, turnover, hit_rate  # noqa

def _ret():  # 250 天，每天 +0.001（无波动上不去 sharpe，故加噪声）
    rng = np.random.default_rng(0)
    return pd.Series(0.0005 + rng.normal(0, 0.01, 500),
                     index=pd.bdate_range("2020-01-01", periods=500))

def test_ann_return_annualizes_daily_mean():
    r = pd.Series([0.001]*245, index=pd.bdate_range("2020-01-01", periods=245))
    assert abs(ann_return(r) - 0.001*245) < 1e-9      # 全日历口径：mean×245，不剔零

def test_max_drawdown_on_known_path():
    r = pd.Series([0.1, -0.5, 0.1])  # 累计 1.1→0.55→0.605；峰 1.1 谷 0.55 → dd≈-0.5
    assert abs(max_drawdown(r) - (-0.5)) < 1e-6

def test_sharpe_and_calmar_signs():
    r = _ret()
    assert sharpe(r) > 0
    assert calmar(r) == ann_return(r) / abs(max_drawdown(r))

def test_turnover_counts_position_changes():
    pos = pd.Series([0, 1, 1, -1, 0])   # |Δ|=1+0+2+1=4，跨 4 步 → 年化按 245/步数
    assert turnover(pos) == 4 / len(pos) * 245  # 简单口径：单边换手次数年化

def test_hit_rate_fraction_positive_among_nonzero():
    r = pd.Series([0.01, -0.01, 0.0, 0.02])
    assert hit_rate(r) == 2/3   # 非零 3 个，正 2 个
```

**Step 2:** `python3 -m pytest tests/test_bt_metrics.py -q` → FAIL（no module）

**Step 3: 实现** `backtest/metrics.py`（`ANN=245`）：
- `ann_return(r) = r.mean()*ANN`（**全日历**：传入的是每日策略收益含空仓 0 日，不剔零）
- `sharpe(r) = r.mean()/r.std(ddof=1)*sqrt(ANN)`（std=0 → 0）
- `max_drawdown(r)`：`cum=(1+r).cumprod(); dd=(cum/cum.cummax()-1); return dd.min()`
- `calmar(r) = ann_return(r)/abs(max_drawdown(r))`（dd=0 → nan）
- `turnover(pos) = pos.diff().abs().sum()/len(pos)*ANN`
- `hit_rate(r)`：非零收益里正的占比（全零 → nan）

**Step 4:** pytest 通过。
**Step 5: Commit** `feat(backtest): 指标纯函数（全日历口径 ann/sharpe/maxdd/calmar/turnover/hit）`

---

## Task 2：信号 → 仓位映射

**Files:** Create `backtest/positions.py`、`tests/test_bt_positions.py`

**Step 1: 失败测试**：
```python
from backtest.positions import to_position
def test_discrete_sign_default():
    s = pd.Series([-0.5, 0.0, 0.3])
    assert list(to_position(s)) == [-1, 0, 1]            # 默认 discrete，θ=0 取符号，恰 0→0
def test_discrete_deadband():
    s = pd.Series([-0.5, 0.1, 0.3])
    assert list(to_position(s, threshold=0.2)) == [-1, 0, 1]  # |x|<=θ → 0
def test_proportional_passthrough():
    s = pd.Series([-0.5, 0.0, 0.3])
    assert list(to_position(s, mode="proportional")) == [-0.5, 0.0, 0.3]
def test_already_discrete_hybrid_untouched():
    s = pd.Series([-1, 0, 1])
    assert list(to_position(s)) == [-1, 0, 1]
```
**Step 3: 实现** `to_position(signal, mode="discrete", threshold=0.0)`：discrete → `np.sign(where(|s|>θ, s, 0))`；proportional → 原值。
**Step 5: Commit** `feat(backtest): 信号→仓位映射（离散阈值/比例）`

---

## Task 3：回测引擎（全日历策略收益 + 成本 + carry + 多空分段）

**Files:** Create `backtest/engine.py`、`tests/test_bt_engine.py`

**Step 1: 失败测试**（合成，不连 PG）：
```python
from backtest.engine import run_strategy
def test_tplus1_fill_and_full_calendar():
    # 标的日收益，仓位 T 定 T+1 生效
    idx = pd.bdate_range("2020-01-01", periods=4)
    underlying = pd.Series([0.0, 0.10, -0.05, 0.02], index=idx)
    pos = pd.Series([1, 1, 0, -1], index=idx)          # T 收盘信号
    out = run_strategy(pos, underlying, cost_bps=0, carry=None)
    # T+1 生效：day1 收益=pos[day0]*u[day1]=1*0.10；day2=1*-0.05；day3=0*0.02=0
    assert abs(out["ret"].iloc[1] - 0.10) < 1e-9
    assert abs(out["ret"].iloc[3] - 0.0) < 1e-9        # 空仓日=0（全日历）
def test_turnover_cost_charged_on_change():
    idx = pd.bdate_range("2020-01-01", periods=3)
    u = pd.Series([0.0,0.0,0.0], index=idx); pos=pd.Series([0,1,1],index=idx)
    out = run_strategy(pos, u, cost_bps=3, carry=None)  # day1 建仓 |Δ|=1 → -3bp
    assert abs(out["ret"].iloc[1] - (-0.0003)) < 1e-9
def test_carry_long_earns_short_pays_in_discount():
    idx = pd.bdate_range("2020-01-01", periods=2)
    u = pd.Series([0.0,0.0], index=idx); pos=pd.Series([1,-1],index=idx)
    carry = pd.Series([0.08,0.08], index=idx)  # 年化贴水率 8%（正=贴水）
    out = run_strategy(pos, u, cost_bps=0, carry=carry)
    # 多头 T+1 earns +0.08/245；此处 day1 pos=pos[day0]=1 → +0.08/245
    assert abs(out["ret"].iloc[1] - 0.08/245) < 1e-9
def test_long_short_segments_split():
    # 提供 segment_returns(pos, underlying) → (long_only_ret, short_only_ret)
    ...
```
**Step 3: 实现** `run_strategy(position, underlying, cost_bps=3, carry=None) -> DataFrame[ret, pos_eff]`：
- `pos_eff = position.shift(1).fillna(0)`（T+1 生效）
- `gross = pos_eff * underlying`
- `cost = cost_bps/1e4 * position.diff().abs().shift(1).fillna(0)`（换手在建仓日计）
- `carry_ret = pos_eff * (carry.shift(1).fillna(0))/245`（carry 为年化贴水率，正=贴水；多头+、空头−）
- `ret = gross - cost + carry_ret`
- 另 `segment_returns`：long=`clip(pos_eff,0,None)*underlying`，short=`clip(pos_eff,None,0)*underlying`（各自含各自 carry/cost），供多空分段指标。
**Step 5: Commit** `feat(backtest): 全日历回测引擎（T+1成交/换手成本/贴水carry/多空分段）`

---

## Task 4：数据层（标的收益 + 主力基差 carry）

**Files:** Create `backtest/data.py`、`tests/test_bt_data.py`（`main_contract_basis` 用合成 DataFrame 测纯逻辑；PG 读取函数标 `@pytest.mark.skipif` 无库时跳过或单独手验）

**Step 1: 失败测试（纯逻辑）**：
```python
from backtest.data import pick_main_contract, annualized_basis, blend_returns
def test_pick_main_contract_by_oi():
    df = pd.DataFrame({"symbol":["IC2606.CFE","IC2609.CFE"],"close":[8270,8128],"oi":[152310,85759]})
    assert pick_main_contract(df) == "IC2606.CFE"        # oi 最大
def test_expiry_month_parsed_and_basis_sign():
    # 贴水：futures<spot → 年化基差为正（贴水率）
    b = annualized_basis(futures=8270, spot=8400, trade_date=date(2026,4,29), symbol="IC2606.CFE")
    assert b > 0
def test_blend_equal_weight_daily_returns():
    a=pd.Series([0.01,0.02]); c=pd.Series([0.03,0.00])
    assert list(blend_returns(a,c)) == [0.02,0.01]
```
**Step 3: 实现**：
- `pick_main_contract(day_df)`：`oi` 最大的 symbol。
- `annualized_basis(futures, spot, trade_date, symbol)`：解析 `IC2606`→到期月，到期日≈该月第三个周五；`days=max((expiry-trade_date).days,1)`；`return (spot-futures)/spot * 365/days`（正=贴水；多头 earns）。
- `load_underlying_returns(kou_jing, start=None)`：读 `index_daily`（`签signals.common.data_source` 风格 psycopg2），500=`000905.SH`、1000=`000852.SH` 日收益，blend=`blend_returns`。返回 date 索引的日收益 Series。
- `load_carry(kou_jing, start=None)`：读 `public.futures_daily`（IC 对 500、IM 对 1000；blend=两者平均），逐日取主力→`annualized_basis` 对齐 spot（同库 index_daily）；无期货日 → 0。
**Step 4:** 纯逻辑测试通过；PG 读取手验（打印 500 口径近日 carry 与收益样例）。
**Step 5: Commit** `feat(backtest): 数据层（标的日收益 + 主力合约年化基差carry + blend）`

---

## Task 5：显著性（同换手随机 bootstrap + 基准）

**Files:** Create `backtest/significance.py`、`tests/test_bt_significance.py`

**Step 1: 失败测试**：
```python
from backtest.significance import bootstrap_pvalue
def test_pvalue_between_0_and_1_and_reproducible():
    idx=pd.bdate_range("2020-01-01",periods=300)
    u=pd.Series(np.random.default_rng(1).normal(0,0.01,300),index=idx)
    pos=pd.Series(np.random.default_rng(2).choice([-1,0,1],300),index=idx)
    p=bootstrap_pvalue(pos,u,metric="sharpe",n=200,seed=0)
    assert 0<=p<=1 and bootstrap_pvalue(pos,u,"sharpe",200,seed=0)==p
```
**Step 3: 实现** `bootstrap_pvalue(position, underlying, metric, n=1000, seed=0)`：实际策略 metric；生成 n 条**同换手**随机仓位（保持相同的换手次数/仓位边际分布——洗牌 position 的变化点或按边际重采样），各跑 `run_strategy`+metric，`p=(#random>=actual+1)/(n+1)`。基准 `buy_hold`/`long_only` 便捷函数。
**Step 5: Commit** `feat(backtest): 同换手随机 bootstrap 显著性 + buy-hold/only-long 基准`

---

## Task 6：基线编排器 + CLI

**Files:** Create `backtest/baseline.py`、`tests/test_bt_baseline.py`（编排用 1 个小合成信号+合成收益测“产出表结构正确”，不连 PG）

**Step 1: 失败测试**：断言 `build_report(signals, underlyings)` 返回的 DataFrame 含列 `[signal, 口径, 段, ann, sharpe, maxdd, calmar, turnover, hit, pvalue, n_obs, start, end]`，且每 signal×口径 有 full/long/short 三行。

**Step 3: 实现** `backtest/baseline.py`：
- 常量：三信号（名→文件→列）、三口径（500/1000/blend）、分段窗口（full / 2014-2020 / 2021-2023 / 2024-2026）。
- `run_baseline(source="pg")`：对每 signal×口径：读信号列→`to_position`（hybrid20 直接用，连续信号 sign）→对齐标的收益日期→`run_strategy`（cost 3bp + carry）→整段+多头段+空头段指标 + bootstrap p 值；空头段额外算避险价值（标的月跌>5% 命中率、加入组合 MaxDD 改善）。
- CLI `python3 -m backtest.baseline [--source pg] [--mode discrete|proportional]`：打印表 + 写 `backtest/output/baseline_metrics.csv`。
**Step 5: Commit** `feat(backtest): 基线编排器 + CLI（3信号×3口径×多空分段+显著性）`

---

## Task 7：跑基线 + 记录结论

**Step 1:** `python3 -m backtest.baseline --source pg`（先跑 500 口径全量；1000/blend 数据已在库）
**Step 2:** 人读 `backtest/output/baseline_metrics.csv`：三条线在 500/1000/blend 的真实 OOS 表现、多头段 vs 空头段、carry 前后差异、bootstrap 是否显著。
**Step 3:** 把关键结论写进 `backtest/README.md`（模块用途、口径、carry 窗口约束、如何跑）+ 更新设计稿路线图状态。
**Step 4: Commit** `docs(backtest): 三条线真实 OOS 基线结论 + README`

---

## Task 8（条件性）：build_basket 非-PIT 结构修复

**前置**：Task 7 若发现 citic40d 的 `build_basket`（`iloc[0]` 归一）导致追加数据改写历史信号（非 point-in-time），此 task 修；否则跳过并记录“活信号未受影响”。

**Files:** Modify 相关信号脚本（先 grep `iloc[0]` / `build_basket` 落在哪些**活**脚本，`optimize_signal.py` 是研究脚本可后置）。
**做法**：归一改等权日收益合成；**会改信号数值**——按 equal_weight reshape 先例：改测试锚 + 重生成 output + commit message 标注数值变化。

**验收总清单**（Phase 2「修秤」完成）：
- [ ] `backtest/` 六模块 + 测试全绿
- [ ] 三条线 × 三口径 基线表产出（多头段/空头段/避险分列 + bootstrap p 值 + carry 前后）
- [ ] carry 用 futures_daily 主力实际基差（500≥2015 / 1000≥2022，无期货日 carry=0）
- [ ] 结论写入 README + 路线图状态更新
- [ ] （如触发）build_basket PIT 修复并标注数值变化
