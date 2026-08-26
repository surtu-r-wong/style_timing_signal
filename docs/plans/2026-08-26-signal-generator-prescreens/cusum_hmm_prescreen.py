"""变点族(CUSUM) + regime 族(Hamilton) 生成式信号零成本预筛(2026-08-26)——只归档不批准。

命题:把三条生产信号的**生成器**(动量+z-score+tanh)整段替换为非线性生成器,
聚合结构(多对/多因子平均、hybrid 金融确认状态机)与 long-flat 映射不变。

═══ 事前固定规格(跑一次,不看结果调参,无扫描)═══
共用:
  输入 = 各线自己的价差日增量(equal_weight: 简单收益差,同现役;citic/hybrid: 日对数收益差)
  标准化 z_t = d_t / rolling_std(d, w),w = 各线现役 z 窗(ew 40 / citic 40 / hybrid 250),
  min_periods=w;不减均值(μ0=0,漂移正是检测对象)。暖机期 NaN → 生成器跳过该日更新。

CUSUM(双侧反射,检测日均值漂移):
  S+_t = max(0, S+_{t-1} + z_t − k), S−_t = max(0, S−_{t-1} − z_t − k)
  k = 0.25(δ/2 规则,检测 δ=0.5σ 漂移), h = 5.0(教科书 ARL 标准值)
  S+ > h → state=+1 并重置 S±=0;S− > h → state=−1 并重置。初始 state=0。

Hamilton(两状态高斯滤波,参数固定不估计):
  状态均值 ±μ,μ = 0.25(= CUSUM 的 δ/2,同一来源);观测方差 1(已标准化)。
  对称持续概率 p 由**现役切换时间尺度**映射(事前规则 p = 1 − 切换次数/生效日数):
    equal_weight p=1−133/3073, citic p=1−367/4040, hybrid p=1−178/3731
  滤波概率 P_t = P(s=high|y_1..t),P_0=0.5;y NaN 日仅按转移阵传播。score = 2P−1。

映射:
  equal_weight/citic: 各对/因子的 state 或 score 求平均,production long-flat (>0)。
  hybrid: 主价差与金融价差各出一个 main/conf——CUSUM 直接用 state;
    Hamilton 用 score 过现役同一状态机(0.35/0.1/−0.15/−0.1)。
    hybrid 规则同现役:main==1→1;main==−1 且 conf≠1→−1;>0 → 多。

两把秤同 EWMA 预筛(分歧天数 + 分歧生效日 |r| 量级与聚集);另报持多占比作结构 sanity。
"""
import sys
from pathlib import Path

import numpy as np
import pandas as pd

ROOT = Path("/home/elfbob/claude-code/style_timing_signal")
SCRATCH = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(SCRATCH))

from ewma_std_prescreen import (  # noqa: E402
    equal_weight_factor, citic_factor, hybrid20_series, make_signal, prescreen,
)
from signals.common.data_source import load_pg_closes  # noqa: E402
from signals.equal_weight.generate_signal import CONFIG_FILE, load_pair_configs  # noqa: E402
from signals.citic40d.generate_signal import build_basket  # noqa: E402
from backtest.data import load_underlying_returns  # noqa: E402

K_CUSUM, H_CUSUM, MU = 0.25, 5.0, 0.25


def standardize(d: pd.Series, w: int) -> pd.Series:
    return d / d.rolling(w, min_periods=w).std()


def cusum_state(z: pd.Series) -> pd.Series:
    sp = sn = 0.0
    state = 0
    out = np.zeros(len(z), dtype=float)
    for i, v in enumerate(z.values):
        if not np.isnan(v):
            sp = max(0.0, sp + v - K_CUSUM)
            sn = max(0.0, sn - v - K_CUSUM)
            if sp > H_CUSUM:
                state, sp, sn = 1, 0.0, 0.0
            elif sn > H_CUSUM:
                state, sp, sn = -1, 0.0, 0.0
        out[i] = state
    return pd.Series(out, index=z.index)


def hamilton_score(z: pd.Series, p_stay: float) -> pd.Series:
    """两状态(±MU, σ=1)对称链滤波概率 → 2P−1。"""
    P = 0.5  # P(s=high)
    out = np.zeros(len(z), dtype=float)
    for i, v in enumerate(z.values):
        P_pred = P * p_stay + (1.0 - P) * (1.0 - p_stay)
        if not np.isnan(v):
            lh = np.exp(-0.5 * (v - MU) ** 2)
            ll = np.exp(-0.5 * (v + MU) ** 2)
            num = P_pred * lh
            den = num + (1.0 - P_pred) * ll
            P = num / den if den > 0 else 0.5
        else:
            P = P_pred
        out[i] = 2.0 * P - 1.0
    return pd.Series(out, index=z.index)


# ── 三条线的价差日增量(与现役腿定义一致)────────────────────────────────────
def ew_daily_spreads(prices: pd.DataFrame, pairs) -> list[pd.Series]:
    out = []
    for pair in pairs:
        l, r = pair.effective_columns()
        aligned = pd.DataFrame({"left": prices[l], "right": prices[r]}).dropna()
        out.append(aligned["left"].pct_change().fillna(0)
                   - aligned["right"].pct_change().fillna(0))
    return out


def citic_daily_spreads(style: pd.DataFrame) -> list[pd.Series]:
    offensive = build_basket(style, ["growth", "cycle"])
    defensive = build_basket(style, ["stability", "consumption"])
    wide_off = build_basket(style, ["growth", "cycle", "finance"])
    legs = [
        (style["growth"], style["stability"]),
        (style["cycle"], style["consumption"]),
        (style["finance"], style["stability"]),
        (offensive, defensive),
        (wide_off, defensive),
    ]
    return [np.log(a / a.shift(1)) - np.log(b / b.shift(1)) for a, b in legs]


def aggregate(series_list: list[pd.Series], index: pd.Index) -> pd.Series:
    agg = pd.Series(0.0, index=index)
    for s in series_list:
        agg += s.reindex(index).fillna(0.0)
    return agg / len(series_list)


def hybrid_from(main: pd.Series, conf: pd.Series) -> pd.Series:
    hybrid = pd.Series(0, index=main.index, dtype=int)
    hybrid[main == 1] = 1
    hybrid[(main == -1) & (conf.reindex(main.index) != 1)] = -1
    return hybrid


def main() -> int:
    print("=" * 78)
    print(f"CUSUM(k={K_CUSUM},h={H_CUSUM}) + Hamilton(μ={MU},p=现役尺度映射) 生成式预筛")
    print("规格事前固定,无扫描 | 只归档不批准")
    print("=" * 78)

    und = {kj: load_underlying_returns(kj) for kj in ["500", "1000", "blend"]}

    # ── equal_weight ──
    pairs = load_pair_configs(CONFIG_FILE)
    need = list(dict.fromkeys(c for p in pairs for c in (p.left_column, p.right_column)))
    prices = load_pg_closes(need, trim_ragged_tail=True)
    incumbent = (equal_weight_factor(prices, pairs, "flat").round(4) > 0).astype(int)
    spreads = [standardize(d, 40) for d in ew_daily_spreads(prices, pairs)]
    p_stay = 1 - 133 / 3073
    pos_cusum = (aggregate([cusum_state(z) for z in spreads], prices.index) > 0).astype(int)
    pos_hmm = (aggregate([hamilton_score(z, p_stay) for z in spreads], prices.index) > 0).astype(int)
    print(f"\n[equal_weight] 持多占比: 现役 {incumbent.mean():.1%} | "
          f"CUSUM {pos_cusum.mean():.1%} | Hamilton {pos_hmm.mean():.1%} (p_stay={p_stay:.4f})")
    prescreen("equal_weight/CUSUM", incumbent, pos_cusum, und)
    prescreen("equal_weight/Hamilton", incumbent, pos_hmm, und)

    # ── citic40d ──
    style = load_pg_closes(["稳定", "成长", "金融", "周期", "消费"], trim_ragged_tail=True).rename(
        columns={"稳定": "stability", "成长": "growth", "金融": "finance",
                 "周期": "cycle", "消费": "consumption"})
    incumbent = (citic_factor(style, "flat").round(4) > 0).astype(int)
    spreads = [standardize(d, 40) for d in citic_daily_spreads(style)]
    p_stay = 1 - 367 / 4040
    pos_cusum = (aggregate([cusum_state(z) for z in spreads], style.index) > 0).astype(int)
    pos_hmm = (aggregate([hamilton_score(z, p_stay) for z in spreads], style.index) > 0).astype(int)
    print(f"\n[citic40d] 持多占比: 现役 {incumbent.mean():.1%} | "
          f"CUSUM {pos_cusum.mean():.1%} | Hamilton {pos_hmm.mean():.1%} (p_stay={p_stay:.4f})")
    prescreen("citic40d/CUSUM", incumbent, pos_cusum, und)
    prescreen("citic40d/Hamilton", incumbent, pos_hmm, und)

    # ── hybrid20 ──
    hf = load_pg_closes(["稳定", "成长", "金融"], trim_ragged_tail=True).rename(
        columns={"稳定": "stability", "成长": "growth", "金融": "finance"})
    incumbent = (hybrid20_series(hf, "flat") > 0).astype(int)
    d_main = np.log(hf["growth"] / hf["growth"].shift(1)) - \
             np.log(hf["stability"] / hf["stability"].shift(1))
    d_conf = np.log(hf["finance"] / hf["finance"].shift(1)) - \
             np.log(hf["stability"] / hf["stability"].shift(1))
    z_main, z_conf = standardize(d_main, 250), standardize(d_conf, 250)
    p_stay = 1 - 178 / 3731
    hy_cusum = hybrid_from(cusum_state(z_main).astype(int), cusum_state(z_conf).astype(int))
    hy_hmm = hybrid_from(make_signal(hamilton_score(z_main, p_stay)),
                         make_signal(hamilton_score(z_conf, p_stay)))
    pos_cusum = (hy_cusum > 0).astype(int)
    pos_hmm = (hy_hmm > 0).astype(int)
    print(f"\n[hybrid20] 持多占比: 现役 {incumbent.mean():.1%} | "
          f"CUSUM {pos_cusum.mean():.1%} | Hamilton {pos_hmm.mean():.1%} (p_stay={p_stay:.4f})")
    prescreen("hybrid20/CUSUM", incumbent, pos_cusum.reindex(incumbent.index).fillna(0).astype(int), und)
    prescreen("hybrid20/Hamilton", incumbent, pos_hmm.reindex(incumbent.index).fillna(0).astype(int), und)

    print("\n完(预筛结果只归档,不构成任何批准)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
