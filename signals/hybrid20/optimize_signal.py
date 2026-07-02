"""
五因子风格强弱信号系统 —— 基于中信风格指数 + 沪深300优化

因子池:
  F1: 成长 vs 稳定  (原始因子)
  F2: 周期 vs 消费  (经济周期敏感度)
  F3: 金融 vs 稳定  (流动性/利率敏感)
  F4: (成长+周期) vs (稳定+消费)  (进攻 vs 防御篮子)
  F5: (成长+周期+金融) vs (稳定+消费)  (宽基进攻 vs 防御)

每个因子构造:
  spread_N = ln_ret(A, N) - ln_ret(B, N)
  z = (spread - rolling_mean(250)) / rolling_std(250)
  factor = tanh(z)

综合信号:
  composite = 加权平均(各因子)，权重由HS300收益IC优化
  状态机离散化
"""

from pathlib import Path

import pandas as pd
import numpy as np
from itertools import product

# ════════════════════════════════════════
# 参数
# ════════════════════════════════════════
ROOT = Path(__file__).resolve().parents[2]
N_LIST = [20, 60]
M = 250  # z-score 滚动窗口

# 状态机阈值 (待优化)
DEFAULT_THRESHOLDS = {
    "open_long": 0.35,
    "close_long": 0.1,
    "open_short": -0.15,
    "close_short": -0.1,
}

# ════════════════════════════════════════
# 读取数据
# ════════════════════════════════════════
def load_data():
    # 五大风格指数 (合并文件)
    style = pd.read_csv(ROOT / "data" / "中信风格合并.csv", skiprows=5, usecols=[0, 1, 2, 3, 4, 5],
                         names=["date", "stability", "growth", "finance", "cycle", "consumption"],
                         parse_dates=["date"])
    style = style.dropna(subset=["date"]).set_index("date").sort_index().astype(float)

    # 沪深300
    hs = pd.read_csv(ROOT / "data" / "沪深300.csv", skiprows=6, usecols=[0, 1],
                      names=["date", "hs300"], parse_dates=["date"])
    hs = hs.dropna(subset=["date"]).set_index("date").sort_index().astype(float)

    df = style.join(hs, how="inner")
    return df


# ════════════════════════════════════════
# 因子构造
# ════════════════════════════════════════
def compute_spread_factor(long_leg: pd.Series, short_leg: pd.Series, n: int, m: int = M):
    """
    spread = ln_ret(long_leg, n) - ln_ret(short_leg, n)
    z-score标准化 → tanh
    """
    spread = np.log(long_leg / long_leg.shift(n)) - np.log(short_leg / short_leg.shift(n))
    roll_mean = spread.rolling(m, min_periods=m).mean()
    roll_std = spread.rolling(m, min_periods=m).std()
    z = (spread - roll_mean) / roll_std
    return np.tanh(z)


def build_basket(df, cols):
    """等权篮子: 各指数归一化后等权"""
    normed = df[cols].div(df[cols].iloc[0])
    return normed.mean(axis=1)


def build_all_factors(df):
    """构建所有因子"""
    # 篮子指数
    offensive = build_basket(df, ["growth", "cycle"])
    defensive = build_basket(df, ["stability", "consumption"])
    wide_offensive = build_basket(df, ["growth", "cycle", "finance"])

    factor_defs = {
        "growth_stability":   (df["growth"], df["stability"]),
        "cycle_consumption":  (df["cycle"], df["consumption"]),
        "finance_stability":  (df["finance"], df["stability"]),
        "offensive_defensive": (offensive, defensive),
        "wide_off_def":       (wide_offensive, defensive),
    }

    factors = {}
    for name, (long_leg, short_leg) in factor_defs.items():
        for n in N_LIST:
            key = f"{name}_{n}"
            factors[key] = compute_spread_factor(long_leg, short_leg, n)

    return pd.DataFrame(factors, index=df.index)


# ════════════════════════════════════════
# 状态机信号
# ════════════════════════════════════════
def make_signal(factor: pd.Series, thresholds: dict = None) -> pd.Series:
    """非对称阈值状态机"""
    t = thresholds or DEFAULT_THRESHOLDS
    ol, cl, os_, cs = t["open_long"], t["close_long"], t["open_short"], t["close_short"]

    signal = pd.Series(np.nan, index=factor.index)
    state = 0
    for i, v in enumerate(factor.values):
        if np.isnan(v):
            continue
        if state == 0:
            if v > ol:
                state = 1
            elif v < os_:
                state = -1
        elif state == 1:
            if v < cl:
                state = 0
                if v < os_:
                    state = -1
        elif state == -1:
            if v > cs:
                state = 0
                if v > ol:
                    state = 1
        signal.iloc[i] = state
    return signal


# ════════════════════════════════════════
# 评估: 用沪深300未来收益率衡量因子质量
# ════════════════════════════════════════
def evaluate_factor(factor: pd.Series, hs300: pd.Series, fwd_days=20):
    """计算因子IC (与未来N日收益的rank相关系数)"""
    fwd_ret = np.log(hs300.shift(-fwd_days) / hs300)
    valid = pd.DataFrame({"factor": factor, "fwd_ret": fwd_ret}).dropna()
    if len(valid) < 100:
        return 0.0
    ic = valid["factor"].corr(valid["fwd_ret"], method="spearman")
    return ic


def evaluate_signal(signal: pd.Series, hs300: pd.Series):
    """评估离散信号的表现"""
    daily_ret = np.log(hs300 / hs300.shift(1))
    merged = pd.DataFrame({"signal": signal.shift(1), "ret": daily_ret}).dropna()
    merged = merged[merged["signal"] != 0]

    if len(merged) < 100:
        return {"annual_ret": 0, "sharpe": 0, "win_rate": 0, "n_trades": 0}

    strat_ret = merged["signal"] * merged["ret"]
    annual_ret = strat_ret.mean() * 245
    annual_vol = strat_ret.std() * np.sqrt(245)
    sharpe = annual_ret / annual_vol if annual_vol > 0 else 0

    # 按持仓段统计胜率
    merged["trade_id"] = (merged["signal"].diff().abs() > 0).cumsum()
    trade_pnl = merged.groupby("trade_id")["ret"].apply(lambda x: (x * merged.loc[x.index, "signal"]).sum())
    win_rate = (trade_pnl > 0).mean() if len(trade_pnl) > 0 else 0
    n_switches = (signal.diff().abs() > 0).sum()

    return {
        "annual_ret": annual_ret,
        "sharpe": sharpe,
        "win_rate": win_rate,
        "n_trades": n_switches,
    }


# ════════════════════════════════════════
# 阈值优化 (网格搜索)
# ════════════════════════════════════════
def optimize_thresholds(factor: pd.Series, hs300: pd.Series):
    """对单个因子做阈值网格搜索, 以Sharpe为目标"""
    best_sharpe = -999
    best_t = DEFAULT_THRESHOLDS.copy()

    # 粗搜索
    for ol in np.arange(0.15, 0.55, 0.1):
        for cl in np.arange(0.0, ol, 0.1):
            for os_ in np.arange(-0.50, -0.05, 0.1):
                cs = os_ + 0.1  # close_short 略高于 open_short
                t = {"open_long": ol, "close_long": cl, "open_short": os_, "close_short": cs}
                sig = make_signal(factor, t)
                result = evaluate_signal(sig, hs300)
                if result["sharpe"] > best_sharpe and result["n_trades"] > 20:
                    best_sharpe = result["sharpe"]
                    best_t = t

    return best_t, best_sharpe


# ════════════════════════════════════════
# 综合因子: IC加权
# ════════════════════════════════════════
def build_composite_factor(factors_df: pd.DataFrame, hs300: pd.Series, fwd_days=20):
    """用IC加权构建综合因子"""
    ics = {}
    for col in factors_df.columns:
        ic = evaluate_factor(factors_df[col], hs300, fwd_days)
        ics[col] = ic

    print("\n各因子IC (未来20日):")
    for name, ic in sorted(ics.items(), key=lambda x: -abs(x[1])):
        print(f"  {name:30s}: IC={ic:+.4f}")

    # 只选IC > 0的因子参与加权 (正IC意味着因子值大→未来涨)
    pos_ics = {k: v for k, v in ics.items() if v > 0.01}
    if not pos_ics:
        print("  警告: 无正IC因子, 使用所有因子等权")
        weights = {col: 1.0 / len(factors_df.columns) for col in factors_df.columns}
    else:
        total = sum(pos_ics.values())
        weights = {k: v / total for k, v in pos_ics.items()}

    print("\n综合因子权重:")
    for name, w in sorted(weights.items(), key=lambda x: -x[1]):
        print(f"  {name:30s}: {w:.3f}")

    composite = sum(factors_df[col] * w for col, w in weights.items())
    return composite, weights, ics


# ════════════════════════════════════════
# 主流程
# ════════════════════════════════════════
def main():
    print("=" * 60)
    print("五因子风格强弱信号系统")
    print("=" * 60)

    # 1. 加载数据
    df = load_data()
    print(f"\n数据区间: {df.index.min().date()} ~ {df.index.max().date()}, {len(df)} 行")

    # 2. 构建所有因子
    factors_df = build_all_factors(df)
    print(f"构建因子: {list(factors_df.columns)}")

    # 3. 评估各单因子
    print("\n" + "=" * 60)
    print("单因子评估 (默认阈值)")
    print("=" * 60)

    for col in factors_df.columns:
        sig = make_signal(factors_df[col])
        result = evaluate_signal(sig, df["hs300"])
        print(f"  {col:30s}: 年化={result['annual_ret']:+.2%}  "
              f"Sharpe={result['sharpe']:+.2f}  胜率={result['win_rate']:.1%}  "
              f"交易次数={result['n_trades']}")

    # 4. 构建综合因子 (分N=20和N=60两组)
    print("\n" + "=" * 60)
    print("综合因子构建")
    print("=" * 60)

    output = pd.DataFrame(index=df.index)

    for n in N_LIST:
        n_cols = [c for c in factors_df.columns if c.endswith(f"_{n}")]
        sub_factors = factors_df[n_cols]

        composite, weights, ics = build_composite_factor(sub_factors, df["hs300"])

        # 优化阈值
        print(f"\n优化N={n}综合因子阈值...")
        best_t, best_sharpe = optimize_thresholds(composite, df["hs300"])
        print(f"  最优阈值: 开多>{best_t['open_long']:.2f}  平多<{best_t['close_long']:.2f}  "
              f"开空<{best_t['open_short']:.2f}  平空>{best_t['close_short']:.2f}")
        print(f"  最优Sharpe: {best_sharpe:.2f}")

        signal = make_signal(composite, best_t)
        result = evaluate_signal(signal, df["hs300"])
        print(f"  优化后: 年化={result['annual_ret']:+.2%}  Sharpe={result['sharpe']:+.2f}  "
              f"胜率={result['win_rate']:.1%}  交易次数={result['n_trades']}")

        output[f"factor_{n}"] = composite.round(4)
        output[f"signal_{n}"] = signal

    # 5. 与原始信号对比
    print("\n" + "=" * 60)
    print("与原始信号对比")
    print("=" * 60)

    orig = pd.read_csv(ROOT / "output" / "hybrid20" / "growth_stability_signal.csv", parse_dates=["date"], index_col="date")
    for n in N_LIST:
        orig_sig = orig[f"signal_{n}"]
        orig_result = evaluate_signal(orig_sig, df["hs300"])
        new_result = evaluate_signal(output[f"signal_{n}"], df["hs300"])
        print(f"\n  N={n}:")
        print(f"    原始: 年化={orig_result['annual_ret']:+.2%}  Sharpe={orig_result['sharpe']:+.2f}  "
              f"胜率={orig_result['win_rate']:.1%}  交易={orig_result['n_trades']}")
        print(f"    优化: 年化={new_result['annual_ret']:+.2%}  Sharpe={new_result['sharpe']:+.2f}  "
              f"胜率={new_result['win_rate']:.1%}  交易={new_result['n_trades']}")

    # 6. 输出
    output = output.dropna()
    for n in N_LIST:
        output[f"signal_{n}"] = output[f"signal_{n}"].astype(int)

    output.to_csv(ROOT / "output" / "hybrid20" / "optimized_signal.csv")
    print(f"\n输出: optimized_signal.csv")
    print(f"区间: {output.index.min().date()} ~ {output.index.max().date()}, {len(output)} 行")

    # 最新信号
    latest = output.iloc[-1]
    print(f"\n最新 ({output.index[-1].date()}):")
    for n in N_LIST:
        print(f"  N={n}: factor={latest[f'factor_{n}']:.4f}  signal={int(latest[f'signal_{n}'])}")

    # 信号分布
    for n in N_LIST:
        col = f"signal_{n}"
        counts = output[col].value_counts().sort_index()
        total = len(output)
        switches = (output[col].diff().abs() > 0).sum()
        months = (output.index.max() - output.index.min()).days / 30.44
        print(f"\n  signal_{n}: 多{counts.get(1,0)}({counts.get(1,0)/total*100:.0f}%) "
              f"中{counts.get(0,0)}({counts.get(0,0)/total*100:.0f}%) "
              f"空{counts.get(-1,0)}({counts.get(-1,0)/total*100:.0f}%) "
              f"| 切换{switches}次 ({switches/months:.1f}/月)")


if __name__ == "__main__":
    main()
