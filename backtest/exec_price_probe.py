"""Batch 12 · 执行价口径审计探针（路线 B = shift(2) 敏感性；路线 A = 期货开盘价空头）。

**审计报告型探针**：不设 GO/STOP 闸门、不动部署、无选优环节（故不欠置换选优机器的债）。
设计与预登记读法见 `docs/plans/2026-08-13-exec-price-audit.md`。

背景（口径差正式入档）：
- 现役引擎 `backtest/engine.py:20` 是 `pos_eff = position.shift(1)`，标的收益取现货指数
  **收盘对收盘**（`backtest/data.py:100`）→ 数学上等价于「T 收盘出信号、T 收盘成交」；
- 设计稿 `docs/plans/2026-07-03-optimization-roadmap-design.md:259` 承诺的却是
  「T 收盘信号 → **T+1 收盘**成交（开盘价作敏感性对照）」。该差异从未被讨论/裁决。

两条路线：
- **B（shift(2)）**：`pos_eff = position.shift(2)`，成本照旧落在新生效日；其余一切不变。
  全样本同秤对比 shift(1) vs shift(2)，差异显著性用配对 moving-block bootstrap。
- **A（期货开盘价）**：空头段改记在**期货主力价格系**上（同一合约衔接，换月不产生虚假损益），
  并**去掉该段的引擎 carry 项**（期货价格已内含基差收敛，避免双算）；多头/空仓一切照旧。
  预登记读法（先写进报告再跑，防读法购物）：
    (i)   现行记账 = 现货收盘成交 + 空头付 carry（committed 口径，基准）
    (i.5) 现货价格系 + **去掉空头 carry**（把 (ii)−(i) 拆成两段的中间读法）
    (ii)  期货价格系 + T 收盘成交（隔离「换标的」效应）
    (iii) 期货价格系 + 空头**开仓** T+1 开盘成交（开仓日收益 = open→close），平仓与多头不变
    (iv)  期货价格系 + 空头开仓与平仓都在 T+1 开盘（对称性对照）
  **(iii)−(ii) 才是纯「开仓时点 / 隔夜缺口」效应**；
  (ii)−(i) 是换标的效应，且 = (i.5)−(i)「去 carry」+ (ii)−(i.5)「换价格系」两段之和。

数据边界（诚实边界，报告须复述）：
- 现货指数开盘价全库缺失（index_daily 3,265 行仅 30 行有 open）→ 路线 A 只能走期货价格系；
- `public.futures_daily` IC 自 2015-04-16、IM 自 2022-07-22，**断更于 2026-04-29**（已知欠账）
  → 500 口径样本 2015-04→2026-04、1000/blend 口径 2022-07→2026-04（blend 取交集）。

CLI: python3 -m backtest.exec_price_probe [--route a|b|all] [--seed 0] [--db-host HOST]
产出: backtest/output/probe_exec_price_*.{csv,json}
连库默认走 Pi5 备端只读（100.75.102.44），connect_timeout 由 data._connect 提供。
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from backtest.baseline import KOU_JING, SIGNALS, WINDOWS, _slice  # noqa: E402
from backtest.data import (  # noqa: E402
    _FUT, _connect, load_carry, load_underlying_returns, pick_main_contract,
)
from backtest.metrics import (  # noqa: E402
    ann_return, calmar, hit_rate, max_drawdown, sharpe, turnover,
)
from backtest.paired_bootstrap import paired_block_bootstrap_sharpe_diff  # noqa: E402
from backtest.positions import production_position, to_position  # noqa: E402
from signals.common.config import load_db_config  # noqa: E402

ANN = 245
MIN_OBS = 60                      # 与 baseline._MIN_OBS 同值（该常量为私有，此处显式复刻）
PRIMARY_SIGNAL = "equal_weight"   # 现役主信号（勿重开清单第 2 条）
PRIMARY_KOU_JING = "blend"        # headline 口径
DEFAULT_DB_HOST = "100.75.102.44"  # Pi5 备端（本任务硬护栏：只读、只走备端）

# 预登记读法（顺序即报告顺序；改动须先改这里再跑）
# (i.5) 为 QA 终审 I2 追加的**中间读法**：只去掉空头 carry、价格系仍是现货，
# 用来把 (ii)−(i) 这个合成效应拆成「去 carry」与「换价格系」两段。
READINGS: dict[str, dict] = {
    "i_spot_close": dict(short_source="spot", short_entry="close",
                         short_exit="close", short_carry=True),
    "i5_spot_close_nocarry": dict(short_source="spot", short_entry="close",
                                  short_exit="close", short_carry=False),
    "ii_fut_close": dict(short_source="fut", short_entry="close",
                         short_exit="close", short_carry=False),
    "iii_fut_open_entry": dict(short_source="fut", short_entry="open",
                               short_exit="close", short_carry=False),
    "iv_fut_open_both": dict(short_source="fut", short_entry="open",
                             short_exit="open", short_carry=False),
}
# 预登记对比对（分解口径：去 carry / 换价格系 / 开仓时点 / 平仓对称性 / 合成 / 总效应）
READING_PAIRS = [
    ("i5_spot_close_nocarry", "i_spot_close"),   # 去空头 carry 的贡献
    ("ii_fut_close", "i5_spot_close_nocarry"),   # 换到期货价格系的贡献
    ("ii_fut_close", "i_spot_close"),            # 上两者的合成（换标的效应）
    ("iii_fut_open_entry", "ii_fut_close"),      # 纯开仓时点（隔夜缺口）效应 ★
    ("iv_fut_open_both", "iii_fut_open_entry"),  # 平仓也挪到开盘的增量
    ("iii_fut_open_entry", "i_spot_close"),      # 目标读法 vs 现行记账（总效应）
]


# ---------------------------------------------------------------- 通用
def resolve_db(host: str | None = None) -> dict:
    """读 config/settings.yaml 后覆盖 host（默认 Pi5 备端）。"""
    db = dict(load_db_config())
    db["host"] = host or DEFAULT_DB_HOST
    return db


def _metrics(ret: pd.Series, pos: pd.Series) -> dict:
    return {"ann": ann_return(ret), "sharpe": sharpe(ret), "maxdd": max_drawdown(ret),
            "calmar": calmar(ret), "turnover": turnover(pos), "hit": hit_rate(ret),
            "n_obs": int(len(ret))}


def load_primary_signal(name: str = PRIMARY_SIGNAL) -> pd.Series:
    path, col = SIGNALS[name]
    df = pd.read_csv(ROOT / path, parse_dates=["date"]).set_index("date").sort_index()
    return df[col]


def build_legs(raw: pd.Series) -> dict[str, pd.Series]:
    """四个评估对象的目标仓位。

    注意 θ=0 时 `symmetric.clip(lower=0) ≡ longflat`（sign(x)>0 ⇔ x>0），
    两行读数必然逐位相等 —— 这是内建的自洽校验，不是重复口径。
    """
    symmetric = to_position(raw, mode="discrete", threshold=0.0).astype(float)
    return {
        "symmetric": symmetric,
        "longflat": production_position(raw, threshold=0.0).astype(float),
        "long_seg": symmetric.clip(lower=0),
        "short_seg": symmetric.clip(upper=0),
    }


# ---------------------------------------------------------------- 路线 B：shift 变体引擎
def run_strategy_shift(position: pd.Series, underlying: pd.Series, cost_bps: float = 3.0,
                       carry: pd.Series | None = None, shift: int = 1) -> pd.DataFrame:
    """`engine.run_strategy` 的 shift 变体（**不改引擎本体**）。

    shift=1 → 与现役引擎逐行等价（T 收盘成交）；shift=2 → T+1 收盘成交。
    成本与 carry 都跟着 `pos_eff` 走，即成本落在**新生效日**，口径与现行一致。
    首日建仓成本落第二天的既有怪癖（engine.py:24）原样保留，不"顺手修"。
    """
    if shift < 0:
        raise ValueError(f"shift must be >= 0, got {shift}")
    position = position.astype(float)
    underlying = underlying.reindex(position.index).astype(float)
    if len(position) == 0:
        empty = pd.Series(dtype=float)
        return pd.DataFrame({"ret": empty, "pos_eff": empty, "gross": empty,
                             "cost": empty, "carry": empty})

    pos_eff = position.shift(shift).fillna(0.0)
    gross = pos_eff * underlying

    trade = pos_eff.diff().abs()
    trade.iloc[0] = abs(pos_eff.iloc[0])
    cost = cost_bps / 1e4 * trade

    if carry is not None:
        carry = carry.reindex(position.index).fillna(0.0)
        carry_ret = pos_eff * carry / ANN
    else:
        carry_ret = pd.Series(0.0, index=position.index)

    ret = gross - cost + carry_ret
    return pd.DataFrame({"ret": ret, "pos_eff": pos_eff, "gross": gross,
                         "cost": cost, "carry": carry_ret})


def build_route_b(block: int = 20, n_boot: int = 10000, seed: int = 0,
                  cost_bps: float = 3.0, db=None,
                  signal_name: str = PRIMARY_SIGNAL) -> pd.DataFrame:
    """{对称/long-flat/多头段/空头段} × {500,1000,blend} × 四窗：shift1 vs shift2 并排。

    bootstrap 的差定义为 **Sharpe(shift2) − Sharpe(shift1)**（负 = 多等一天的损失）。
    """
    legs_full = build_legs(load_primary_signal(signal_name))
    und_all = {kj: load_underlying_returns(kj, db=db) for kj in KOU_JING}
    car_all = {kj: load_carry(kj, db=db) for kj in KOU_JING}

    rows = []
    for kj in KOU_JING:
        for win, (s, e) in WINDOWS.items():
            und = _slice(und_all[kj], s, e)
            car = _slice(car_all[kj], s, e)
            for leg, pos_full in legs_full.items():
                pos = _slice(pos_full, s, e)
                idx = pos.index.intersection(und.index)
                if len(idx) < MIN_OBS:
                    continue
                p, u, c = pos.reindex(idx), und.reindex(idx), car.reindex(idx)
                r1 = run_strategy_shift(p, u, cost_bps, c, shift=1)["ret"]
                r2 = run_strategy_shift(p, u, cost_bps, c, shift=2)["ret"]
                boot = paired_block_bootstrap_sharpe_diff(r2, r1, block=block,
                                                          n=n_boot, seed=seed)
                row = {"signal": signal_name, "kou_jing": kj, "window": win,
                       "leg": leg, "n_obs": int(len(idx))}
                for tag, ret in (("shift1", r1), ("shift2", r2)):
                    for k, v in _metrics(ret, p).items():
                        if k == "n_obs":
                            continue
                        row[f"{k}_{tag}"] = v
                for k in ("ann", "sharpe", "maxdd", "calmar", "hit"):
                    row[f"d_{k}"] = row[f"{k}_shift2"] - row[f"{k}_shift1"]
                row.update({k: boot[k] for k in
                            ("diff_sharpe", "ci_lo", "ci_hi", "p_value",
                             "boot_mean", "boot_sd", "block", "n_boot", "seed")})
                row["ci_excludes_zero"] = bool(row["ci_lo"] > 0 or row["ci_hi"] < 0)
                rows.append(row)
    return pd.DataFrame(rows)


# ---------------------------------------------------------------- 路线 A：期货价格系
def load_futures_raw(kou_jing: str, start=None, db=None) -> pd.DataFrame:
    """读 IC/IM 全合约日线（open/close/oi）。只读查询，走 `data._connect`。"""
    db = db or resolve_db()
    conn = _connect(db)
    try:
        with conn.cursor() as cur:
            cur.execute(
                """SELECT trade_date, symbol, open, close, oi FROM public.futures_daily
                   WHERE symbol LIKE %s AND (%s::date IS NULL OR trade_date>=%s::date)
                   ORDER BY trade_date, symbol""",
                (_FUT[kou_jing] + "%", start, start),
            )
            rows = cur.fetchall()
    finally:
        conn.close()
    df = pd.DataFrame(rows, columns=["trade_date", "symbol", "open", "close", "oi"])
    for c in ("open", "close", "oi"):
        df[c] = pd.to_numeric(df[c], errors="coerce")
    df["trade_date"] = pd.to_datetime(df["trade_date"])
    return df.dropna(subset=["open", "close", "oi"])


def main_contract_series(fdf: pd.DataFrame) -> pd.Series:
    """逐日主力合约（oi 最大），复用 `data.pick_main_contract` 的判定。"""
    out = {pd.Timestamp(td): pick_main_contract(g) for td, g in fdf.groupby("trade_date")}
    return pd.Series(out).sort_index()


def held_contract_frame(fdf: pd.DataFrame) -> pd.DataFrame:
    """同一合约衔接的期货价格系。

    第 t 日的收益一律用**同一张合约**算：取 `held(t) = main(t−1)`，
    即「昨收时点实际持有的那张主力」——换月日因此不产生虚假换月损益
    （换月的价差在下一日以新主力的日内收益体现，而非跨合约跳空）。
    held(t) 在 t 日无报价时退化为 main(t)（罕见，摘牌边界）。

    列：symbol_held / prev_close / open / close /
        ret_cc = close/prev_close − 1（收→收）
        ret_oc = close/open − 1（当日开→收）
        gap    = open/prev_close − 1（隔夜缺口）
    恒等式：(1+ret_cc) = (1+gap)(1+ret_oc)。
    """
    fdf = fdf.drop_duplicates(subset=["trade_date", "symbol"])
    main = main_contract_series(fdf)
    close_w = fdf.pivot(index="trade_date", columns="symbol", values="close")
    open_w = fdf.pivot(index="trade_date", columns="symbol", values="open")
    dates = list(close_w.index)

    rows = []
    for i in range(1, len(dates)):
        t, p = dates[i], dates[i - 1]
        for sym in (main.loc[p], main.loc[t]):
            pc, op, cl = close_w.at[p, sym], open_w.at[t, sym], close_w.at[t, sym]
            if pd.notna(pc) and pd.notna(op) and pd.notna(cl) and pc > 0 and op > 0:
                rows.append({"date": t, "symbol_held": sym, "prev_close": float(pc),
                             "open": float(op), "close": float(cl),
                             "ret_cc": float(cl) / float(pc) - 1.0,
                             "ret_oc": float(cl) / float(op) - 1.0,
                             "gap": float(op) / float(pc) - 1.0})
                break
    return pd.DataFrame(rows).set_index("date").sort_index()


def blend_futures_frame(a: pd.DataFrame, b: pd.DataFrame) -> pd.DataFrame:
    """blend 期货价格系 = 两腿收益等权（与 `data.blend_returns` 的现货口径一致）。

    只取两腿都有报价的交集日（IM 上市前不放大单腿），价格列不做混合（无意义）。
    """
    idx = a.index.intersection(b.index)
    out = pd.DataFrame(index=idx)
    for c in ("ret_cc", "ret_oc", "gap"):
        out[c] = (a.loc[idx, c].astype(float) + b.loc[idx, c].astype(float)) / 2.0
    out["symbol_held"] = (a.loc[idx, "symbol_held"].astype(str) + "|"
                          + b.loc[idx, "symbol_held"].astype(str))
    return out


def load_futures_frame(kou_jing: str, start=None, db=None) -> pd.DataFrame:
    if kou_jing == "blend":
        return blend_futures_frame(load_futures_frame("500", start, db),
                                   load_futures_frame("1000", start, db))
    return held_contract_frame(load_futures_raw(kou_jing, start, db))


def run_strategy_dual(position: pd.Series, spot_ret: pd.Series, fut: pd.DataFrame | None = None,
                      cost_bps: float = 3.0, carry: pd.Series | None = None,
                      short_source: str = "spot", short_entry: str = "close",
                      short_exit: str = "close", short_carry: bool = True,
                      shift: int = 1) -> pd.DataFrame:
    """双价格系引擎：多头/空仓走现货+carry，空头可改走期货价格系并按开盘价成交。

    - `short_source="spot"` + `short_carry=True` → 与 `engine.run_strategy` 逐行等价（读法 i）；
    - `short_source="fut"` → 空头日的标的收益换成期货 `ret_cc`，且该段 carry 归零
      （期货价格已内含基差收敛，再加 carry 会双算）；
    - `short_entry="open"` → 空头**开仓日**（pos_eff 由非空转空的那天）改用 `ret_oc`
      （= 当日 open→close），即晚到开盘价成交；
    - `short_exit="open"` → 空头**平仓日**（pos_eff 由空转非空的那天）补记一段
      `−gap`（前收→开盘的空头损益），即在该日开盘才平掉。
    换手成本口径不变（按 |Δpos_eff| 计，落在生效日）。
    """
    if short_source not in ("spot", "fut"):
        raise ValueError(f"short_source must be 'spot'|'fut', got {short_source!r}")
    if short_entry not in ("close", "open") or short_exit not in ("close", "open"):
        raise ValueError("short_entry / short_exit must be 'close'|'open'")
    if short_source == "spot" and (short_entry == "open" or short_exit == "open"):
        raise ValueError("现货指数开盘价全库缺失 → spot 价格系不支持开盘成交")

    position = position.astype(float)
    idx = position.index
    spot_ret = spot_ret.reindex(idx).astype(float)
    if len(position) == 0:
        empty = pd.Series(dtype=float)
        return pd.DataFrame({"ret": empty, "pos_eff": empty, "gross": empty,
                             "cost": empty, "carry": empty, "exit_pnl": empty})

    pos_eff = position.shift(shift).fillna(0.0)
    is_short = pos_eff < 0
    prev_short = is_short.shift(1, fill_value=False).astype(bool)

    und_eff = spot_ret.copy()
    exit_pnl = pd.Series(0.0, index=idx)
    if short_source == "fut":
        if fut is None:
            raise ValueError("short_source='fut' 需要提供期货价格系 fut")
        ret_cc = fut["ret_cc"].reindex(idx).astype(float)
        und_eff = und_eff.where(~is_short, ret_cc)
        if short_entry == "open":
            open_day = is_short & ~prev_short
            und_eff = und_eff.where(~open_day, fut["ret_oc"].reindex(idx).astype(float))
        if short_exit == "open":
            exit_day = prev_short & ~is_short
            exit_pnl = -fut["gap"].reindex(idx).astype(float).fillna(0.0) * exit_day.astype(float)

    gross = pos_eff * und_eff.fillna(0.0)

    trade = pos_eff.diff().abs()
    trade.iloc[0] = abs(pos_eff.iloc[0])
    cost = cost_bps / 1e4 * trade

    if carry is not None:
        c = carry.reindex(idx).fillna(0.0)
        pos_for_carry = pos_eff if short_carry else pos_eff.clip(lower=0)
        carry_ret = pos_for_carry * c / ANN
    else:
        carry_ret = pd.Series(0.0, index=idx)

    ret = gross - cost + carry_ret + exit_pnl
    return pd.DataFrame({"ret": ret, "pos_eff": pos_eff, "gross": gross,
                         "cost": cost, "carry": carry_ret, "exit_pnl": exit_pnl})


def effective_position(position: pd.Series, shift: int = 1,
                       position_full: pd.Series | None = None) -> pd.Series:
    """有效仓位 `pos_eff`。

    给了 `position_full`（**未切片**的整条仓位）就用它算 shift 再对齐到窗口 —— 否则
    窗口首日的 `shift().fillna(0)` 会把「跨窗界持有的空头」误判成一次**新开仓**
    （QA 终审 M1：500 × 2024-2026 的 2024-01-03 即一次伪开仓，导致分窗合计 61 ≠ full 的 60）。
    **只用于描述性诊断**；收益侧仍按 house harness 的切片后 shift 口径（见审计报告 §6.15）。
    """
    if position_full is None:
        return position.astype(float).shift(shift).fillna(0.0)
    return (position_full.astype(float).shift(shift).fillna(0.0)
            .reindex(position.index).fillna(0.0))


def overnight_gap_diagnostics(position: pd.Series, fut: pd.DataFrame, shift: int = 1,
                              position_full: pd.Series | None = None) -> dict:
    """空头开仓日的期货隔夜缺口（前收→开盘）分布 + 两个显著性检验。

    符号约定：gap>0 = 高开。空头晚到开盘成交时，
    **gap>0 → 躲过一段亏损**（少赔 gap），**gap<0 → 错过一段利润**（少赚 |gap|）。
    `missed_pnl_sum` = Σ(−gap) = 若在前收成交、空头本可多赚的隔夜合计（正 = 净错过）。

    显著性（QA 终审 I4：方向性描述不得被当作"稳定事实"）：
    - `binom_p_up`：高开/低开次数对 H0=50% 的双侧二项检验（剔除 gap 恰为 0 的日）；
    - `t_p_mean`：缺口均值对 H0=0 的单样本双侧 t 检验。
    """
    pos_eff = effective_position(position, shift, position_full)
    # 前一日的有效仓位同样从**未切片**序列取（多滞后一期），否则窗首 prev_short 恒为 False
    # → 跨窗界的持空会被重记成一次新开仓。position_full=None 时与朴素 shift 完全等价。
    is_short = pos_eff < 0
    prev_short = effective_position(position, shift + 1, position_full) < 0
    open_day = is_short & ~prev_short
    g = fut["gap"].reindex(pos_eff.index).astype(float)[open_day].dropna()
    flip_days = int(((pos_eff > 0) & prev_short).sum())  # −1→+1 直翻日（读法 iv 隔夜重叠）
    if len(g) == 0:
        return {"n_short_open": 0, "flip_short_to_long": flip_days}

    from scipy import stats
    n_up, n_dn = int((g > 0).sum()), int((g < 0).sum())
    binom_p = (float(stats.binomtest(n_up, n_up + n_dn, 0.5).pvalue)
               if n_up + n_dn > 0 else float("nan"))
    t_p = (float(stats.ttest_1samp(g, 0.0).pvalue)
           if len(g) > 1 and g.std(ddof=1) > 0 else float("nan"))
    return {
        "n_short_open": int(len(g)),
        "gap_mean": float(g.mean()), "gap_median": float(g.median()),
        "gap_std": float(g.std(ddof=1)) if len(g) > 1 else float("nan"),
        "gap_p10": float(np.percentile(g, 10)), "gap_p90": float(np.percentile(g, 90)),
        "n_gap_up": n_up, "n_gap_down": n_dn,
        "pct_gap_up_short_avoids_loss": float((g > 0).mean()),
        "pct_gap_down_short_misses_gain": float((g < 0).mean()),
        "binom_p_up": binom_p, "t_p_mean": t_p,
        "missed_pnl_sum": float((-g).sum()),
        "missed_pnl_mean": float((-g).mean()),
        "flip_short_to_long": flip_days,
    }


def roll_cost_estimate(position: pd.Series, fut: pd.DataFrame, cost_bps: float = 3.0,
                       shift: int = 1, position_full: pd.Series | None = None) -> dict:
    """期货腿**换月**的往返成本估计（QA 终审 I7：本审计未把它计入路线 A 收益，故单列）。

    换月日 = `symbol_held` 变化的那天（blend 的 `symbol_held` 是两腿拼接，按腿分别计、
    各按 1/腿数 的仓位权重）。单次换月 = 平旧 + 开新 = **2 × cost_bps**；只有落在
    **持空日**的换月才与路线 A 的空头段读数相关。
    (iii)−(ii) 两读法的换月完全相同、配对相消，**不受此污染**；受影响的是 (ii)−(i)。
    """
    pos_eff = effective_position(position, shift, position_full)
    syms = (fut["symbol_held"].reindex(pos_eff.index).astype(str)
            .str.split("|", expand=True))
    w = 1.0 / syms.shape[1]
    short = pos_eff < 0
    n_roll = n_roll_short = 0
    cost = 0.0
    for c in syms.columns:
        chg = syms[c].ne(syms[c].shift(1)) & syms[c].shift(1).notna()
        n_roll += int(chg.sum())
        n_roll_short += int((chg & short).sum())
        cost += int((chg & short).sum()) * 2.0 * cost_bps / 1e4 * w
    n_days = max(len(pos_eff), 1)
    return {"n_legs": int(syms.shape[1]), "n_roll_days": n_roll,
            "n_roll_days_short": n_roll_short,
            "roll_cost_total": float(cost),
            "roll_cost_ann": float(cost / n_days * ANN)}


def _route_a_legs(raw: pd.Series) -> dict[str, pd.Series]:
    """路线 A 的评估对象：只有含空头的口径才会随读法变化。

    `longflat` 一并跑，作为**不变性校验**（无空头日 → 四读法必须逐位相等）。
    """
    legs = build_legs(raw)
    return {k: legs[k] for k in ("symmetric", "short_seg", "longflat")}


def build_route_a(block: int = 20, n_boot: int = 10000, seed: int = 0,
                  cost_bps: float = 3.0, db=None, signal_name: str = PRIMARY_SIGNAL
                  ) -> tuple[pd.DataFrame, pd.DataFrame, pd.DataFrame]:
    """四读法 × {对称, 空头段, long-flat} × 三口径 × 窗口。

    返回 (metrics_long, pairs_boot, gap_diag)。所有读法都在**同一条截短索引**上评估
    （期货可得日 ∩ 现货日 ∩ 信号日），确保同秤。
    """
    legs_full = _route_a_legs(load_primary_signal(signal_name))
    und_all = {kj: load_underlying_returns(kj, db=db) for kj in KOU_JING}
    car_all = {kj: load_carry(kj, db=db) for kj in KOU_JING}
    fut_all = {kj: load_futures_frame(kj, db=db) for kj in KOU_JING}

    met_rows, pair_rows, gap_rows = [], [], []
    for kj in KOU_JING:
        fut_kj = fut_all[kj]
        for win, (s, e) in WINDOWS.items():
            und = _slice(und_all[kj], s, e)
            car = _slice(car_all[kj], s, e)
            fut = _slice(fut_kj, s, e)
            for leg, pos_full in legs_full.items():
                pos = _slice(pos_full, s, e)
                idx = pos.index.intersection(und.index).intersection(fut.index)
                if len(idx) < MIN_OBS:
                    continue
                p, u, c, f = pos.reindex(idx), und.reindex(idx), car.reindex(idx), fut.reindex(idx)
                rets = {}
                for name, kw in READINGS.items():
                    rets[name] = run_strategy_dual(p, u, f, cost_bps, c, shift=1, **kw)["ret"]
                    met_rows.append({"signal": signal_name, "kou_jing": kj, "window": win,
                                     "leg": leg, "reading": name,
                                     "start": str(idx[0].date()), "end": str(idx[-1].date()),
                                     **_metrics(rets[name], p)})
                for hi, lo in READING_PAIRS:
                    boot = paired_block_bootstrap_sharpe_diff(rets[hi], rets[lo], block=block,
                                                              n=n_boot, seed=seed)
                    pair_rows.append({
                        "signal": signal_name, "kou_jing": kj, "window": win, "leg": leg,
                        "pair": f"{hi}−{lo}",
                        "sharpe_hi": sharpe(rets[hi]), "sharpe_lo": sharpe(rets[lo]),
                        "d_ann": ann_return(rets[hi]) - ann_return(rets[lo]),
                        "d_maxdd": max_drawdown(rets[hi]) - max_drawdown(rets[lo]),
                        **{k: boot[k] for k in ("diff_sharpe", "ci_lo", "ci_hi", "p_value",
                                                "boot_mean", "boot_sd", "block", "n_boot",
                                                "seed", "n_obs")},
                        "ci_excludes_zero": bool(boot["ci_lo"] > 0 or boot["ci_hi"] < 0),
                    })
                if leg in ("symmetric", "short_seg"):
                    gap_rows.append({
                        "kou_jing": kj, "window": win, "leg": leg,
                        "start": str(idx[0].date()), "end": str(idx[-1].date()),
                        **overnight_gap_diagnostics(p, f, position_full=pos_full),
                        **roll_cost_estimate(p, f, cost_bps, position_full=pos_full)})
    return pd.DataFrame(met_rows), pd.DataFrame(pair_rows), pd.DataFrame(gap_rows)


# ---------------------------------------------------------------- CLI
def _round(df: pd.DataFrame, cols, nd=3) -> pd.DataFrame:
    out = df.copy()
    for c in cols:
        if c in out:
            out[c] = out[c].astype(float).round(nd)
    return out


def main() -> int:
    ap = argparse.ArgumentParser(description="Batch 12 执行价口径审计（B=shift2 / A=期货开盘）")
    ap.add_argument("--route", default="all", choices=["a", "b", "all"])
    ap.add_argument("--seed", type=int, default=0)
    ap.add_argument("--db-host", default=DEFAULT_DB_HOST, help="默认 Pi5 备端只读")
    ap.add_argument("--block", type=int, default=20, help="moving-block 块长（预登记 20 日）")
    ap.add_argument("--n-boot", type=int, default=10000, help="重抽样次数（预登记 ≥1000）")
    ap.add_argument("--cost-bps", type=float, default=3.0)
    args = ap.parse_args()
    if args.n_boot < 1000:
        raise SystemExit("预登记要求 n≥1000")

    db = resolve_db(args.db_host)
    out_dir = ROOT / "backtest" / "output"
    out_dir.mkdir(parents=True, exist_ok=True)
    summary: dict = {"seed": args.seed, "block": args.block, "n_boot": args.n_boot,
                     "cost_bps": args.cost_bps, "db_host": db["host"],
                     "signal": PRIMARY_SIGNAL, "readings": list(READINGS)}

    if args.route in ("b", "all"):
        rep = build_route_b(args.block, args.n_boot, args.seed, args.cost_bps, db)
        rep.to_csv(out_dir / "probe_exec_price_route_b.csv", index=False)
        show = _round(rep[["kou_jing", "window", "leg", "n_obs", "sharpe_shift1",
                           "sharpe_shift2", "diff_sharpe", "ci_lo", "ci_hi", "p_value",
                           "maxdd_shift1", "maxdd_shift2"]],
                      ["sharpe_shift1", "sharpe_shift2", "diff_sharpe", "ci_lo", "ci_hi",
                       "maxdd_shift1", "maxdd_shift2", "p_value"], 3)
        print("=== 路线 B：shift(1) vs shift(2)（diff = shift2 − shift1）===")
        print(show.to_string(index=False))
        head = rep[(rep.kou_jing == PRIMARY_KOU_JING) & (rep.window == "full")
                   & (rep.leg == "longflat")]
        if len(head):
            summary["route_b_headline"] = head.iloc[0].to_dict()

    if args.route in ("a", "all"):
        met, pairs, gap = build_route_a(args.block, args.n_boot, args.seed, args.cost_bps, db)
        met.to_csv(out_dir / "probe_exec_price_route_a.csv", index=False)
        pairs.to_csv(out_dir / "probe_exec_price_route_a_pairs.csv", index=False)
        gap.to_csv(out_dir / "probe_exec_price_gap_diag.csv", index=False)
        print("\n=== 路线 A：四读法指标（long 表）===")
        print(_round(met[["kou_jing", "window", "leg", "reading", "n_obs", "ann",
                          "sharpe", "maxdd", "hit"]],
                     ["ann", "sharpe", "maxdd", "hit"], 4).to_string(index=False))
        print("\n=== 路线 A：预登记分解（配对 bootstrap）===")
        print(_round(pairs[["kou_jing", "window", "leg", "pair", "n_obs", "sharpe_hi",
                            "sharpe_lo", "diff_sharpe", "ci_lo", "ci_hi", "p_value"]],
                     ["sharpe_hi", "sharpe_lo", "diff_sharpe", "ci_lo", "ci_hi",
                      "p_value"], 4).to_string(index=False))
        print("\n=== 路线 A：空头开仓日隔夜缺口诊断 ===")
        print(gap.to_string(index=False))
        summary["route_a_n_rows"] = int(len(met))

    (out_dir / "probe_exec_price_summary.json").write_text(
        json.dumps(summary, indent=2, ensure_ascii=False, default=str), encoding="utf-8")
    print(f"\n→ {out_dir}/probe_exec_price_*.csv|json")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
