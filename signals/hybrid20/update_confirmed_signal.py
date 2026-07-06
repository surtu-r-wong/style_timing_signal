"""
成长/稳健 + 金融确认 信号 —— 日常更新脚本

在原始 growth_stability_signal 基础上，加入金融/稳定因子作为确认过滤，输出三种信号:
  - confirmed: 双向确认 (主信号与金融因子矛盾时降为中性)
  - hybrid:    原始多 + 确认空 (多头保持原始信号, 仅空头需金融确认)

数据源: 中信风格合并.csv (稳定/成长/金融/周期/消费)
输出:   confirmed_signal.csv
依赖:   需先运行 update_growth_stability.py 生成 growth_stability_signal.csv
"""

import argparse
import sys
from pathlib import Path

import pandas as pd
import numpy as np

# ════════════════════════════════════════
# 参数 (与原始脚本一致)
# ════════════════════════════════════════
ROOT = Path(__file__).resolve().parents[2]
STYLE_FILE = ROOT / "data" / "中信风格合并.csv"
ORIG_SIGNAL_FILE = ROOT / "output" / "hybrid20" / "growth_stability_signal.csv"
OUTPUT_FILE = ROOT / "output" / "hybrid20" / "confirmed_signal.csv"

N_LIST = [20, 60]
M = 250

# 金融/稳定因子使用与主因子相同的阈值
OPEN_LONG = 0.35
CLOSE_LONG = 0.1
OPEN_SHORT = -0.15
CLOSE_SHORT = -0.1


# ════════════════════════════════════════
# 参数与数据源
# ════════════════════════════════════════
parser = argparse.ArgumentParser(description="成长/稳健 + 金融确认信号")
parser.add_argument("--source", choices=["csv", "pg"], default="pg",
                    help="数据源: pg=stock_selector.index_daily（默认）, csv=data/中信风格合并.csv（备份/审计）")
parser.add_argument("--start", default=None, help="pg 模式起始日 YYYY-MM-DD（复现验证时传 CSV 首日）")
parser.add_argument("--end", default=None, help="pg 模式截止日 YYYY-MM-DD（复现验证时对齐 CSV 尾日）")
parser.add_argument("--orig-signal", default=str(ORIG_SIGNAL_FILE),
                    help=f"原始信号路径, 默认 {ORIG_SIGNAL_FILE}")
parser.add_argument("--output", default=str(OUTPUT_FILE), help=f"输出路径, 默认 {OUTPUT_FILE}")
args = parser.parse_args()

if args.source == "csv" and (args.start is not None or args.end is not None):
    parser.error("--start/--end 仅在 --source pg 模式下有效")

# 稳定 & 金融指数
if args.source == "pg":
    sys.path.insert(0, str(ROOT))
    from signals.common.data_source import load_pg_closes

    df = load_pg_closes(["稳定", "金融"], start=args.start, end=args.end, trim_ragged_tail=True).rename(
        columns={"稳定": "stability", "金融": "finance"}
    )
else:
    df = pd.read_csv(
        STYLE_FILE, skiprows=5, usecols=[0, 1, 3],
        names=["date", "stability", "finance"], parse_dates=["date"],
    )
    df = df.dropna(subset=["date"]).set_index("date").sort_index()
    df["stability"] = df["stability"].astype(float)
    df["finance"] = df["finance"].astype(float)

# 原始信号
orig = pd.read_csv(args.orig_signal, parse_dates=["date"], index_col="date")


# ════════════════════════════════════════
# 构建金融/稳定确认因子
# ════════════════════════════════════════
def make_signal(factor: pd.Series) -> pd.Series:
    """状态机: 非对称阈值 + 状态保持"""
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


confirm_factors = {}
confirm_signals = {}
for n in N_LIST:
    spread = np.log(df["finance"] / df["finance"].shift(n)) - \
             np.log(df["stability"] / df["stability"].shift(n))
    roll_mean = spread.rolling(M, min_periods=M).mean()
    roll_std = spread.rolling(M, min_periods=M).std()
    z = (spread - roll_mean) / roll_std
    factor = np.tanh(z)
    confirm_factors[n] = factor
    confirm_signals[n] = make_signal(factor)


# ════════════════════════════════════════
# 确认过滤: 主信号与金融因子矛盾时降为中性
# ════════════════════════════════════════
out = orig.copy()

for n in N_LIST:
    main_sig = orig[f"signal_{n}"]
    conf_sig = confirm_signals[n].reindex(orig.index)

    confirmed = main_sig.copy()
    # 主因子做多, 金融因子做空 → 中性
    confirmed[(main_sig == 1) & (conf_sig == -1)] = 0
    # 主因子做空, 金融因子做多 → 中性
    confirmed[(main_sig == -1) & (conf_sig == 1)] = 0

    out[f"confirmed_{n}"] = confirmed.astype(int)

    # hybrid: 多头用原始, 空头用确认
    hybrid = pd.Series(0, index=main_sig.index, dtype=int)
    hybrid[main_sig == 1] = 1                              # 多头保持原始
    hybrid[(main_sig == -1) & (conf_sig != 1)] = -1        # 空头需金融不反对
    out[f"hybrid_{n}"] = hybrid

    # 金融因子值和信号
    out[f"fin_factor_{n}"] = confirm_factors[n].reindex(orig.index).round(4)
    out[f"fin_signal_{n}"] = conf_sig.astype(int)


# ════════════════════════════════════════
# 输出
# ════════════════════════════════════════
# 列顺序: 20日因子和三个信号在前, 60日在后
col_order = []
for n in N_LIST:
    col_order.append(f"factor_{n}")
    col_order.append(f"signal_{n}")
    col_order.append(f"fin_factor_{n}")
    col_order.append(f"fin_signal_{n}")
    col_order.append(f"confirmed_{n}")
    col_order.append(f"hybrid_{n}")
out = out[col_order]
out.to_csv(args.output)

src_desc = "pg:stock_selector.index_daily" if args.source == "pg" else f"csv:{STYLE_FILE}"
print(f"输入: {args.orig_signal} + {src_desc}")
print(f"输出: {args.output}")
print(f"区间: {out.index.min().date()} ~ {out.index.max().date()}, 共 {len(out)} 行")
print()

# 最新信号
latest = out.iloc[-1]
orig_latest = orig.iloc[-1]
print(f"最新 ({out.index[-1].date()}):")
for n in N_LIST:
    o = int(orig_latest[f'signal_{n}'])
    c = int(latest[f'confirmed_{n}'])
    h = int(latest[f'hybrid_{n}'])
    ff = latest[f'fin_factor_{n}']
    fs = int(latest[f'fin_signal_{n}'])
    print(f"  N={n}: factor={latest[f'factor_{n}']:.4f}  原始={o}  "
          f"金融因子={ff:.4f}  金融信号={fs}  确认={c}  混合={h}")
print()

# 对比统计
def print_dist(label, col_series, total, months):
    counts = col_series.value_counts().sort_index()
    switches = (col_series.diff().abs() > 0).sum()
    print(f"  {label:<6s}: 多{counts.get(1,0):>5d}({counts.get(1,0)/total*100:4.0f}%) "
          f"中{counts.get(0,0):>5d}({counts.get(0,0)/total*100:4.0f}%) "
          f"空{counts.get(-1,0):>5d}({counts.get(-1,0)/total*100:4.0f}%) "
          f"| 切换{switches}次")

total = len(out)
months = (out.index.max() - out.index.min()).days / 30.44

for n in N_LIST:
    print(f"signal_{n}:")
    print_dist("原始", orig[f"signal_{n}"], total, months)
    print_dist("确认", out[f"confirmed_{n}"], total, months)
    print_dist("混合", out[f"hybrid_{n}"], total, months)
    print()
