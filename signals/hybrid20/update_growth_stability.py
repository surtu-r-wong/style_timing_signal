"""
成长/稳健 相对强弱因子 & 离散信号 —— 日常更新脚本

数据源: 中信风格合并.csv (稳定/成长/金融/周期/消费 风格指数收盘价)
输出:   growth_stability_signal.csv

计算逻辑:
  1. spread_N = ln(成长(t)/成长(t-N)) - ln(稳健(t)/稳健(t-N))  (N=20, 60)
  2. z = (spread - rolling_mean(250)) / rolling_std(250)
  3. factor = tanh(z)  → 连续值 [-1, 1]
  4. 状态机离散化:
     - 开多: factor > 0.35
     - 平多: factor < 0.1
     - 开空: factor < -0.15
     - 平空: factor > -0.1

使用方式:
  1. 更新 中信风格合并.csv (追加最新数据)
  2. python update_growth_stability.py
"""

import argparse
import sys
from pathlib import Path

import pandas as pd
import numpy as np

# ════════════════════════════════════════
# 参数
# ════════════════════════════════════════
ROOT = Path(__file__).resolve().parents[2]
INPUT_FILE = ROOT / "data" / "中信风格合并.csv"
OUTPUT_FILE = ROOT / "output" / "hybrid20" / "growth_stability_signal.csv"

N_LIST = [20, 60]       # 收益率回看窗口
M = 250                 # z-score标准化滚动窗口

OPEN_LONG = 0.35        # 开多阈值
CLOSE_LONG = 0.1        # 平多阈值
OPEN_SHORT = -0.15      # 开空阈值
CLOSE_SHORT = -0.1      # 平空阈值


# ════════════════════════════════════════
# 参数与数据源
# ════════════════════════════════════════
parser = argparse.ArgumentParser(description="成长/稳健相对强弱信号")
parser.add_argument("--source", choices=["csv", "pg"], default="csv",
                    help="数据源: csv=data/中信风格合并.csv, pg=stock_selector.index_daily")
parser.add_argument("--start", default=None, help="pg 模式起始日 YYYY-MM-DD（复现验证时传 CSV 首日）")
parser.add_argument("--end", default=None, help="pg 模式截止日 YYYY-MM-DD（复现验证时对齐 CSV 尾日）")
parser.add_argument("--output", default=str(OUTPUT_FILE), help=f"输出路径, 默认 {OUTPUT_FILE}")
args = parser.parse_args()

if args.source == "csv" and (args.start is not None or args.end is not None):
    parser.error("--start/--end 仅在 --source pg 模式下有效")

if args.source == "pg":
    sys.path.insert(0, str(ROOT))
    from signals.common.data_source import load_pg_closes

    df = load_pg_closes(["稳定", "成长"], start=args.start, end=args.end, trim_ragged_tail=True).rename(
        columns={"稳定": "stability", "成长": "growth"}
    )
else:
    df = pd.read_csv(
        INPUT_FILE,
        skiprows=5,
        usecols=[0, 1, 2],
        names=["date", "stability", "growth"],
        parse_dates=["date"],
    )
    df = df.dropna(subset=["date"]).set_index("date").sort_index()
    df["stability"] = df["stability"].astype(float)
    df["growth"] = df["growth"].astype(float)


# ════════════════════════════════════════
# 信号生成
# ════════════════════════════════════════
def make_signal(factor: pd.Series) -> pd.Series:
    """状态机: 非对称阈值 + 状态保持"""
    signal = pd.Series(np.nan, index=factor.index)
    state = 0
    for i, v in enumerate(factor):
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


out = pd.DataFrame(index=df.index)

for n in N_LIST:
    spread = np.log(df["growth"] / df["growth"].shift(n)) - \
             np.log(df["stability"] / df["stability"].shift(n))

    roll_mean = spread.rolling(M, min_periods=M).mean()
    roll_std = spread.rolling(M, min_periods=M).std()
    z = (spread - roll_mean) / roll_std
    factor = np.tanh(z)

    out[f"factor_{n}"] = factor.round(4)
    out[f"signal_{n}"] = make_signal(factor)

out = out.dropna()
out["signal_20"] = out["signal_20"].astype(int)
out["signal_60"] = out["signal_60"].astype(int)


# ════════════════════════════════════════
# 输出
# ════════════════════════════════════════
out.to_csv(args.output)

# 打印摘要
src_desc = "pg:stock_selector.index_daily" if args.source == "pg" else f"csv:{INPUT_FILE}"
print(f"输入: {src_desc}")
print(f"输出: {args.output}")
print(f"区间: {out.index.min().date()} ~ {out.index.max().date()}, 共 {len(out)} 行")
print(f"阈值: 开多>{OPEN_LONG}, 平多<{CLOSE_LONG}, 开空<{OPEN_SHORT}, 平空>{CLOSE_SHORT}")
print()

# 最新信号
latest = out.iloc[-1]
print(f"最新 ({out.index[-1].date()}):")
for n in N_LIST:
    print(f"  N={n}: factor={latest[f'factor_{n}']:.4f}  signal={int(latest[f'signal_{n}'])}")
print()

# 统计
for n in N_LIST:
    col = f"signal_{n}"
    counts = out[col].value_counts().sort_index()
    total = len(out)
    switches = (out[col].diff().abs() > 0).sum()
    months = (out.index.max() - out.index.min()).days / 30.44
    print(f"signal_{n}: 多{counts.get(1,0)}({counts.get(1,0)/total*100:.0f}%) "
          f"中{counts.get(0,0)}({counts.get(0,0)/total*100:.0f}%) "
          f"空{counts.get(-1,0)}({counts.get(-1,0)/total*100:.0f}%) "
          f"| 切换{switches}次 ({switches/months:.1f}/月)")
