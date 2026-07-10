"""动量变换器扫描:成长/价值配对判强尺子替换评估。

分组不动(四对成长/价值),把配对判强弱的度量从"区间收益差"换成
"相对动量"(经典跳月/趋势斜率/风险调整三族),下游 z→tanh→等权→平滑
与生产 equal_weight 逐步对齐。裁决 = walk-forward 三窗扫描 + 同秤头对头
vs 现任 20d40z+sm5。设计:docs/plans/2026-07-10-momentum-transform-scan-design.md
"""
from __future__ import annotations

from pathlib import Path

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parents[1]


# ---------- 动量纯函数(每腿) ----------

def momentum_classic(price: pd.Series, length: int, skip: int = 0) -> pd.Series:
    """经典跳月动量:P(t−skip)/P(t−L−skip) − 1。skip=0 即区间收益。"""
    shifted = price.shift(skip)
    return shifted / shifted.shift(length) - 1.0


def momentum_slope(price: pd.Series, length: int) -> pd.Series:
    """对数价格对时间的滚动 OLS 斜率(日对数增速),向量化。

    窗口含 NaN 时结果为 NaN(点积自然传播),前 L−1 个值无定义。
    """
    y = np.log(price.to_numpy(dtype=float))
    x = np.arange(length, dtype=float)
    xc = x - x.mean()
    denom = float((xc ** 2).sum())

    out = np.full(len(y), np.nan)
    if len(y) >= length:
        windows = np.lib.stride_tricks.sliding_window_view(y, length)
        out[length - 1:] = windows @ xc / denom
    return pd.Series(out, index=price.index)


def momentum_voladj(price: pd.Series, length: int) -> pd.Series:
    """风险调整动量:区间收益 / 同窗日收益波动(ddof=1)。"""
    ret = price.pct_change()
    vol = ret.rolling(length, min_periods=length).std()
    mom = price / price.shift(length) - 1.0
    out = mom / vol
    return out.replace([np.inf, -np.inf], np.nan)


_MOMENTUM_FNS = {
    "classic": lambda price, length, skip: momentum_classic(price, length, skip),
    "slope": lambda price, length, skip: momentum_slope(price, length),
    "voladj": lambda price, length, skip: momentum_voladj(price, length),
}


def _zscore_tanh(series: pd.Series, z_window: int) -> pd.Series:
    """z→tanh 链,逐行镜像生产 _compute_pair_signal 的下游语义
    (min_periods=z_window、STD_FLOOR、inf→NaN→0、tanh(z/2))。"""
    from signals.equal_weight.generate_signal import STD_FLOOR

    rolling_mean = series.rolling(z_window, min_periods=z_window).mean()
    rolling_std = series.rolling(z_window, min_periods=z_window).std()
    rolling_std = pd.Series(
        np.where(rolling_std < STD_FLOOR, STD_FLOOR, rolling_std),
        index=rolling_std.index,
    )
    zscore = (series - rolling_mean) / rolling_std
    zscore = zscore.replace([np.inf, -np.inf], np.nan).fillna(0.0)
    return pd.Series(np.tanh(zscore / 2.0), index=series.index)


def momentum_pair_factor(
    prices: pd.DataFrame,
    pairs: list[tuple[str, str]],
    family: str,
    length: int,
    skip: int,
    z_window: int,
    smoothing: int,
) -> pd.Series:
    """每腿动量 → 配对差 → z→tanh → 等权 → 平滑(下游与生产逐步对齐)。"""
    fn = _MOMENTUM_FNS[family]
    pair_signals = []
    for left_col, right_col in pairs:
        aligned = prices[[left_col, right_col]].dropna()
        raw = fn(aligned[left_col], length, skip) - fn(aligned[right_col], length, skip)
        pair_signals.append(_zscore_tanh(raw, z_window))

    combined = pd.Series(0.0, index=prices.index)
    for sig in pair_signals:
        combined += sig.reindex(prices.index).fillna(0.0)
    combined /= len(pair_signals)

    if smoothing == 0:
        return combined
    return combined.rolling(smoothing, min_periods=1).mean()


# ---------- 网格与 PG 接线 ----------

def momentum_grid() -> list[dict]:
    """三族 20 形态 × z_window{40,120,250} × smoothing{0,5} = 120 组。

    设计 §2 原 14 形态 + 短窗补测(07-10 用户追加 L∈{5,10}):短窗只 skip=0
    (skip 已全灭且 skip≥L 无语义);classic 不含 L=20(step-2 已扫,
    L20/skip0 只作恒等锚不进裁决),但含 5/10(zw 网格与 step-2 不同)。
    """
    forms = [("classic", length, 0) for length in (5, 10)]
    forms += [("classic", length, skip) for length in (60, 120, 250) for skip in (0, 20)]
    forms += [(fam, length, 0) for fam in ("slope", "voladj")
              for length in (5, 10, 20, 60, 120, 250)]
    return [
        {"family": fam, "length": length, "skip": skip,
         "z_window": zw, "smoothing": sm}
        for fam, length, skip in forms
        for zw in (40, 120, 250)
        for sm in (0, 5)
    ]


PAIR_NAMES = ["沪深300成长", "沪深300价值", "中证500成长", "中证500价值",
              "中证1000成长", "中证1000价值", "中证2000成长", "中证2000价值"]


def momentum_factor_fn(start=None):
    """返回 fn(family, length, skip, z_window, smoothing) → factor_value。

    PG 8 列一次加载,配对沿用生产 config_4pairs(含 direction),
    equal_weight_factor_fn 同款接线。
    """
    from signals.common.data_source import load_pg_closes
    from signals.equal_weight.generate_signal import load_pair_configs

    prices = load_pg_closes(PAIR_NAMES, start=start)
    configs = load_pair_configs(ROOT / "signals/equal_weight/config_4pairs.csv")
    pairs = [cfg.effective_columns() for cfg in configs]

    def fn(family, length, skip, z_window, smoothing):
        return momentum_pair_factor(prices, pairs=pairs, family=family,
                                    length=length, skip=skip,
                                    z_window=z_window, smoothing=smoothing)

    return fn


# ---------- 编排:扫描 + 同秤头对头 ----------

_SHARPE_COLS = ["sharpe_train_14_20", "sharpe_val_21_23", "sharpe_holdout_24_26"]


def pick_plateau_representatives(rep: pd.DataFrame) -> pd.DataFrame:
    """每族高原代表 = 三窗最差 Sharpe 最大者(拒绝尖峰,设计 §4)。"""
    rep = rep.copy()
    rep["worst_window"] = rep[_SHARPE_COLS].min(axis=1)
    idx = rep.groupby("family")["worst_window"].idxmax()
    return rep.loc[sorted(idx)].reset_index(drop=True)


def _candidate_name(row) -> str:
    return (f"{row['family']}_L{int(row['length'])}s{int(row['skip'])}"
            f"_zw{int(row['z_window'])}_sm{int(row['smoothing'])}")


def run_head2head(scan_csv: Path, bootstrap_n: int = 500) -> pd.DataFrame:
    """每族高原代表 + 现任 20d40z+sm5,对称/long-flat 双口径上 baseline 同秤。"""
    from backtest.baseline import build_report
    from backtest.positions import production_position, to_position
    from backtest.scan import equal_weight_factor_fn

    reps = pick_plateau_representatives(pd.read_csv(scan_csv))
    mom_fn = momentum_factor_fn()
    factors = {"incumbent_ret20": equal_weight_factor_fn()(
        lookback=20, z_window=40, smoothing=5)}
    for _, row in reps.iterrows():
        factors[_candidate_name(row)] = mom_fn(
            family=row["family"], length=int(row["length"]), skip=int(row["skip"]),
            z_window=int(row["z_window"]), smoothing=int(row["smoothing"]))

    positions = {}
    for name, fac in factors.items():
        positions[f"{name}_sym"] = to_position(fac, mode="discrete")
        positions[f"{name}_lf"] = production_position(fac)
    report = build_report(bootstrap_n=bootstrap_n, positions=positions)

    incumbent = factors["incumbent_ret20"]
    diag = pd.DataFrame([
        {"candidate": name, "corr_vs_incumbent": fac.corr(incumbent)}
        for name, fac in factors.items() if name != "incumbent_ret20"
    ])
    return report, diag, reps


def main() -> int:
    import argparse

    from backtest.data import load_carry, load_underlying_returns
    from backtest.scan import scan_grid

    parser = argparse.ArgumentParser(description="动量变换器扫描 + 同秤头对头(设计 2026-07-10)")
    parser.add_argument("--kj", choices=["500", "1000", "blend"], default="blend")
    parser.add_argument("--head2head", action="store_true",
                        help="从 scan_momentum.csv 选每族高原代表,与现任同秤对比")
    parser.add_argument("--bootstrap", type=int, default=500)
    args = parser.parse_args()

    out_dir = ROOT / "backtest" / "output"
    out_dir.mkdir(parents=True, exist_ok=True)
    scan_path = out_dir / "scan_momentum.csv"

    if args.head2head:
        report, diag, reps = run_head2head(scan_path, bootstrap_n=args.bootstrap)
        h2h_path = out_dir / "momentum_head2head.csv"
        report.to_csv(h2h_path, index=False)
        diag_path = out_dir / "momentum_head2head_diag.csv"
        diag.to_csv(diag_path, index=False)
        print("每族高原代表(worst-window 最大):")
        print(reps.round(2).to_string(index=False))
        print("\n诊断:与现任因子相关")
        print(diag.round(3).to_string(index=False))
        print(f"\n→ {h2h_path}\n→ {diag_path}")
        return 0

    windows = {
        "train_14_20": ("2014-01-01", "2020-12-31"),
        "val_21_23": ("2021-01-01", "2023-12-31"),
        "holdout_24_26": ("2024-01-01", "2026-12-31"),
    }
    fn = momentum_factor_fn()
    und = load_underlying_returns(args.kj)
    car = load_carry(args.kj)
    rep = scan_grid(fn, momentum_grid(), und, car, windows)
    rep.to_csv(scan_path, index=False)
    show = rep.copy()
    show["worst"] = show[_SHARPE_COLS].min(axis=1)
    print(show.round(2).sort_values("worst", ascending=False).head(20).to_string(index=False))
    print(f"\n[momentum · {args.kj}] 现任=收益差 20d40z+sm5(holdout 1.69)。→ {scan_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
