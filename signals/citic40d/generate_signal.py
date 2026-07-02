"""
五因子中信风格强弱信号系统（40日z-score窗口）

输入：
- 中信风格合并.csv

因子池：
- F1: 成长 vs 稳定
- F2: 周期 vs 消费
- F3: 金融 vs 稳定
- F4: (成长+周期) vs (稳定+消费)
- F5: (成长+周期+金融) vs (稳定+消费)

每个因子：
- spread_N = ln_ret(A, N) - ln_ret(B, N)
- z = (spread - rolling_mean(40)) / rolling_std(40)
- factor = tanh(z)

输出：
- factor_20: 20 日窗口下 5 个因子的等权连续信号
"""

import argparse
from pathlib import Path

import numpy as np
import pandas as pd


ROOT = Path(__file__).resolve().parents[2]
INPUT_FILE = ROOT / "data" / "中信风格合并.csv"
OUTPUT_FILE = ROOT / "output" / "citic40d" / "citic_style_signal_40d.csv"
N_LIST = [20]
Z_WINDOW = 40
def load_style_data(input_file: str | Path = INPUT_FILE) -> pd.DataFrame:
    style = pd.read_csv(
        input_file,
        skiprows=5,
        usecols=[0, 1, 2, 3, 4, 5],
        names=["date", "stability", "growth", "finance", "cycle", "consumption"],
        parse_dates=["date"],
    )
    style = style.dropna(subset=["date"]).set_index("date").sort_index()
    return style.astype(float)


def compute_spread_factor(long_leg: pd.Series, short_leg: pd.Series, n: int, m: int = Z_WINDOW):
    spread = np.log(long_leg / long_leg.shift(n)) - np.log(short_leg / short_leg.shift(n))
    roll_mean = spread.rolling(m, min_periods=m).mean()
    roll_std = spread.rolling(m, min_periods=m).std().replace(0, np.nan)
    z = (spread - roll_mean) / roll_std
    return np.tanh(z)


def build_basket(df: pd.DataFrame, cols: list[str]) -> pd.Series:
    normed = df[cols].div(df[cols].iloc[0])
    return normed.mean(axis=1)


def build_all_factors(df: pd.DataFrame) -> pd.DataFrame:
    offensive = build_basket(df, ["growth", "cycle"])
    defensive = build_basket(df, ["stability", "consumption"])
    wide_offensive = build_basket(df, ["growth", "cycle", "finance"])

    factor_defs = {
        "growth_stability": (df["growth"], df["stability"]),
        "cycle_consumption": (df["cycle"], df["consumption"]),
        "finance_stability": (df["finance"], df["stability"]),
        "offensive_defensive": (offensive, defensive),
        "wide_off_def": (wide_offensive, defensive),
    }

    factors = {}
    for name, (long_leg, short_leg) in factor_defs.items():
        for n in N_LIST:
            factors[f"{name}_{n}"] = compute_spread_factor(long_leg, short_leg, n)

    return pd.DataFrame(factors, index=df.index)


def build_output(style: pd.DataFrame) -> pd.DataFrame:
    factors = build_all_factors(style)
    output = pd.DataFrame(index=style.index)

    for n in N_LIST:
        n_cols = [col for col in factors.columns if col.endswith(f"_{n}")]
        output[f"factor_{n}"] = factors[n_cols].mean(axis=1).round(4)

    return output.dropna()


def main() -> int:
    parser = argparse.ArgumentParser(description="Generate CITIC style signal with 40-day z-score")
    parser.add_argument("--input", default=INPUT_FILE, help="input CSV path")
    parser.add_argument("--output", default=OUTPUT_FILE, help="output CSV path")
    args = parser.parse_args()

    style = load_style_data(args.input)
    output = build_output(style)
    output.to_csv(args.output, index_label="date")

    print(f"Input: {args.input}")
    print(f"Output: {args.output}")
    print(f"Style data range: {style.index.min().date()} ~ {style.index.max().date()}, {len(style)} rows")
    print(f"Output range: {output.index.min().date()} ~ {output.index.max().date()}, {len(output)} rows")
    latest = output.iloc[-1]
    print(f"Latest ({output.index[-1].date()}):")
    for n in N_LIST:
        print(f"  N={n}: factor={latest[f'factor_{n}']:.4f}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
