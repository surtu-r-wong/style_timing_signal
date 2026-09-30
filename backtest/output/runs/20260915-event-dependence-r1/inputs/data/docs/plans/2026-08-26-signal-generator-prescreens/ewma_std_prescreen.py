"""EWMA-std 零成本预筛（2026-08-26）—— 只归档不批准。

命题（事前固定，无扫描）：把三条生产信号 z-score 的**分母** rolling std 换成
ewm(span=现役 z 窗口, min_periods=同现役).std()（adjust=True, bias=False 默认），
分子基线 rolling mean 不动，其余逐字节同生产语义。
机制假设：平坦窗 std 在波动率 regime 跳变后被旧波动率污染一个窗长，EWMA 衰减更快。

两把秤（feedback-timing-prescreen-two-dims）：
  功效 = 仓位分歧天数（production long-flat 口径，T+1 生效日计）
  容量 = 分歧生效日 |r| 量级与聚集（500/1000/blend 三口径）
不看带方向的收益差（那是未预登记的回测）。

前置自检：平坦版必须复现 committed CSV（同日期逐位、round(4) 后），否则 ABORT。
"""
import sys
from pathlib import Path

import numpy as np
import pandas as pd

ROOT = Path("/home/elfbob/claude-code/style_timing_signal")
sys.path.insert(0, str(ROOT))

from signals.common.data_source import load_pg_closes  # noqa: E402
from signals.equal_weight.generate_signal import (  # noqa: E402
    CONFIG_FILE, STD_FLOOR, load_pair_configs,
)
from signals.citic40d.generate_signal import build_basket  # noqa: E402
from backtest.data import load_underlying_returns  # noqa: E402

ANN = 245


def make_std(s: pd.Series, w: int, kind: str) -> pd.Series:
    if kind == "flat":
        return s.rolling(w, min_periods=w).std()
    if kind == "ewm":
        return s.ewm(span=w, min_periods=w).std()
    raise ValueError(kind)


# ── equal_weight（lookback 20 / z 40 / smoothing 5）────────────────────────────
def equal_weight_factor(prices: pd.DataFrame, pairs, kind: str,
                        lookback=20, z_window=40, smoothing=5) -> pd.Series:
    pair_signals = []
    for pair in pairs:
        l, r = pair.effective_columns()
        aligned = pd.DataFrame({"left": prices[l], "right": prices[r]}).dropna()
        rel = aligned["left"].pct_change().fillna(0) - aligned["right"].pct_change().fillna(0)
        cum = (1.0 + rel).rolling(lookback, min_periods=1).apply(lambda x: x.prod() - 1, raw=False)
        m = cum.rolling(z_window, min_periods=z_window).mean()
        sd = make_std(cum, z_window, kind)
        sd = pd.Series(np.where(sd < STD_FLOOR, STD_FLOOR, sd), index=sd.index)
        z = ((cum - m) / sd).replace([np.inf, -np.inf], np.nan).fillna(0.0)
        pair_signals.append(pd.Series(np.tanh(z / 2.0), index=aligned.index))
    raw = pd.Series(0.0, index=prices.index)
    for s in pair_signals:
        raw += s.reindex(prices.index).fillna(0.0)
    raw /= len(pair_signals)
    return raw.rolling(smoothing, min_periods=1).mean()


# ── citic40d（n 20 / z 40，5 因子等权）─────────────────────────────────────────
def citic_factor(style: pd.DataFrame, kind: str, n=20, m=40) -> pd.Series:
    offensive = build_basket(style, ["growth", "cycle"])
    defensive = build_basket(style, ["stability", "consumption"])
    wide_off = build_basket(style, ["growth", "cycle", "finance"])
    defs = {
        "growth_stability": (style["growth"], style["stability"]),
        "cycle_consumption": (style["cycle"], style["consumption"]),
        "finance_stability": (style["finance"], style["stability"]),
        "offensive_defensive": (offensive, defensive),
        "wide_off_def": (wide_off, defensive),
    }
    cols = {}
    for name, (long_leg, short_leg) in defs.items():
        spread = np.log(long_leg / long_leg.shift(n)) - np.log(short_leg / short_leg.shift(n))
        rm = spread.rolling(m, min_periods=m).mean()
        sd = make_std(spread, m, kind).replace(0, np.nan)
        cols[name] = np.tanh((spread - rm) / sd)
    return pd.DataFrame(cols, index=style.index).mean(axis=1)


# ── hybrid20（N 20/60，M 250，状态机 + 金融确认）──────────────────────────────
OPEN_LONG, CLOSE_LONG, OPEN_SHORT, CLOSE_SHORT = 0.35, 0.1, -0.15, -0.1


def make_signal(factor: pd.Series) -> pd.Series:
    signal = pd.Series(np.nan, index=factor.index)
    state = 0
    for i, v in enumerate(factor.values):
        if np.isnan(v):
            continue
        if state == 0:
            if v > OPEN_LONG:
                state = 1
            elif v < OPEN_SHORT:
                state = -1
        elif state == 1:
            if v < CLOSE_LONG:
                state = 0
                if v < OPEN_SHORT:
                    state = -1
        elif state == -1:
            if v > CLOSE_SHORT:
                state = 0
                if v > OPEN_LONG:
                    state = 1
        signal.iloc[i] = state
    return signal


def hybrid20_series(df: pd.DataFrame, kind: str, M=250) -> pd.Series:
    """复刻 update_growth_stability + update_confirmed_signal 的 hybrid_20 全链。"""
    out = pd.DataFrame(index=df.index)
    for n in [20, 60]:
        spread = np.log(df["growth"] / df["growth"].shift(n)) - \
                 np.log(df["stability"] / df["stability"].shift(n))
        rm = spread.rolling(M, min_periods=M).mean()
        sd = make_std(spread, M, kind)
        factor = np.tanh((spread - rm) / sd)
        out[f"factor_{n}"] = factor.round(4)
        out[f"signal_{n}"] = make_signal(factor)
    out = out.dropna()

    n = 20
    fsp = np.log(df["finance"] / df["finance"].shift(n)) - \
          np.log(df["stability"] / df["stability"].shift(n))
    rm = fsp.rolling(M, min_periods=M).mean()
    sd = make_std(fsp, M, kind)
    conf_sig = make_signal(np.tanh((fsp - rm) / sd)).reindex(out.index)

    main_sig = out["signal_20"]
    hybrid = pd.Series(0, index=main_sig.index, dtype=int)
    hybrid[main_sig == 1] = 1
    hybrid[(main_sig == -1) & (conf_sig != 1)] = -1
    return hybrid


# ── 预筛度量 ──────────────────────────────────────────────────────────────────
def reproduce_check(name: str, mine: pd.Series, committed: pd.Series) -> None:
    common = mine.index.intersection(committed.index)
    diff = (mine.reindex(common).round(4) - committed.reindex(common)).abs()
    bad = int((diff > 1e-9).sum())
    print(f"[{name}] 复现自检: 共同 {len(common)} 天, 不一致 {bad} 天, max|Δ|={diff.max():.2e}")
    if bad:
        raise SystemExit(f"ABORT: {name} 平坦版未能复现 committed CSV")


def prescreen(name: str, pos_flat: pd.Series, pos_ewm: pd.Series, und: dict) -> None:
    eff_flat = pos_flat.shift(1).dropna()
    eff_ewm = pos_ewm.shift(1).dropna()
    common = eff_flat.index.intersection(eff_ewm.index)
    d = common[eff_flat.reindex(common) != eff_ewm.reindex(common)]
    yrs = len(common) / ANN
    print(f"\n━━ {name} ━━  评窗 {common.min().date()}~{common.max().date()} 共 {len(common)} 生效日")
    sw_f = int((pos_flat.diff().abs() > 0).sum())
    sw_e = int((pos_ewm.diff().abs() > 0).sum())
    print(f"  功效秤: 分歧 {len(d)} 天 ({len(d)/len(common)*100:.2f}%) | 切换次数 现役 {sw_f} → EWMA {sw_e}")
    if len(d) == 0:
        return
    by_year = pd.Series(1, index=d).groupby(d.year).sum()
    top = by_year.sort_values(ascending=False).head(3)
    print(f"  聚集: 按年 {dict(by_year)} | 前3年占 {top.sum()/len(d)*100:.0f}%")
    runs, cur = [], 1
    for a, b in zip(d[:-1], d[1:]):
        loc_a, loc_b = common.get_loc(a), common.get_loc(b)
        if loc_b == loc_a + 1:
            cur += 1
        else:
            runs.append(cur); cur = 1
    runs.append(cur)
    print(f"  连段: {len(runs)} 段, 最长 {max(runs)} 天, 中位 {int(np.median(runs))} 天")
    for kj, r in und.items():
        rr = r.reindex(common).dropna()
        rd = r.reindex(d).dropna()
        if rd.empty:
            continue
        print(f"  容量秤[{kj}]: 分歧日|r| 中位 {rd.abs().median()*100:.3f}% / 均值 {rd.abs().mean()*100:.3f}%"
              f" / P90 {rd.abs().quantile(.9)*100:.3f}% (全窗中位 {rr.abs().median()*100:.3f}%)"
              f" | Σ|r|={rd.abs().sum()*100:.2f}% → 年化上界 {rd.abs().sum()/yrs*100:.3f}%/年")


def main() -> int:
    print("=" * 78)
    print("EWMA-std 预筛 | 变体=std→ewm(span=现役z窗), 均值不动, 无扫描 | 只归档不批准")
    print("=" * 78)

    und = {kj: load_underlying_returns(kj) for kj in ["500", "1000", "blend"]}

    # equal_weight
    pairs = load_pair_configs(CONFIG_FILE)
    need = list(dict.fromkeys(c for p in pairs for c in (p.left_column, p.right_column)))
    prices = load_pg_closes(need, trim_ragged_tail=True)
    ew_flat = equal_weight_factor(prices, pairs, "flat")
    ew_ewm = equal_weight_factor(prices, pairs, "ewm")
    committed = pd.read_csv(ROOT / "output/equal_weight/equal_weight_signal_20d40z.csv",
                            parse_dates=["date"], index_col="date")["factor_value"]
    reproduce_check("equal_weight", ew_flat, committed)
    prescreen("equal_weight", (ew_flat.round(4) > 0).astype(int),
              (ew_ewm.round(4) > 0).astype(int), und)

    # citic40d
    style = load_pg_closes(["稳定", "成长", "金融", "周期", "消费"], trim_ragged_tail=True).rename(
        columns={"稳定": "stability", "成长": "growth", "金融": "finance",
                 "周期": "cycle", "消费": "consumption"})
    ct_flat = citic_factor(style, "flat")
    ct_ewm = citic_factor(style, "ewm")
    committed = pd.read_csv(ROOT / "output/citic40d/citic_style_signal_40d.csv",
                            parse_dates=["date"], index_col="date")["factor_20"]
    reproduce_check("citic40d", ct_flat.dropna(), committed)
    prescreen("citic40d", (ct_flat.round(4) > 0).astype(int).dropna(),
              (ct_ewm.round(4) > 0).astype(int).dropna(), und)

    # hybrid20
    hf = load_pg_closes(["稳定", "成长", "金融"], trim_ragged_tail=True).rename(
        columns={"稳定": "stability", "成长": "growth", "金融": "finance"})
    hy_flat = hybrid20_series(hf, "flat")
    hy_ewm = hybrid20_series(hf, "ewm")
    committed = pd.read_csv(ROOT / "output/hybrid20/confirmed_signal.csv",
                            parse_dates=["date"], index_col="date")["hybrid_20"]
    reproduce_check("hybrid20", hy_flat.astype(float), committed.astype(float))
    prescreen("hybrid20", (hy_flat > 0).astype(int), (hy_ewm > 0).astype(int), und)

    print("\n完（预筛结果只归档，不构成任何批准）")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
