"""§7 平滑插点测量(2026-08-26 同日追加)——raw 天花板 / DEMA(5) / DEMA(7) 对现役 MA5。

用户追问「5 日平滑换 DEMA 是否优化」。此插点只存在于 equal_weight 20d40z
(citic40d/hybrid20 无平滑步)。输入=committed 信号 CSV(不碰库表,underlying 除外);
秤同主登记两把;只归档不批准。DEMA(n) = 2·EMA(n) − EMA(n)∘EMA(n),adjust=False。
本脚本为当日两次交互测量的合并复现,数字与首测逐位核对(raw 285/9.27%/245,
DEMA5 225/7.32%/179/corr .9686, DEMA7 162/5.27%/151/corr .9832)。
"""
import sys
from pathlib import Path

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT))

from backtest.data import load_underlying_returns  # noqa: E402

df = pd.read_csv(ROOT / "output/equal_weight/equal_weight_signal_20d40z.csv",
                 parse_dates=["date"], index_col="date")
raw, inc = df["factor_value_raw"], df["factor_value"]


def dema(x: pd.Series, n: int) -> pd.Series:
    e = x.ewm(span=n, adjust=False).mean()
    return 2 * e - e.ewm(span=n, adjust=False).mean()


pos_inc = (inc > 0).astype(int)
und = load_underlying_returns("blend")
base = pos_inc.shift(1).dropna()
n_days = len(base)
yrs = n_days / 245
print(f"评窗 {base.index.min().date()}~{base.index.max().date()} 共 {n_days} 生效日, "
      f"现役切换 {int((pos_inc.diff().abs() > 0).sum())} 次")

variants = [("raw(sm0,天花板)", raw, (raw > 0).astype(int)),
            ("DEMA5", dema(raw, 5), (dema(raw, 5).round(4) > 0).astype(int)),
            ("DEMA7", dema(raw, 7), (dema(raw, 7).round(4) > 0).astype(int))]
for label, series, p in variants:
    v = p.shift(1).dropna().reindex(base.index)
    d = base.index[base != v]
    sw = int((p.diff().abs() > 0).sum())
    rd = und.reindex(d).dropna()
    rr = und.reindex(base.index).dropna()
    by = pd.Series(1, index=d).groupby(d.year).sum()
    top3 = by.sort_values(ascending=False).head(3).sum() / max(len(d), 1)
    print(f"\n[{label}] 分歧 {len(d)} 天 ({len(d)/n_days*100:.2f}%) | 切换 {sw} 次 | "
          f"因子相关 {series.corr(inc):.4f}")
    if len(d):
        print(f"  分歧日|r| 中位 {rd.abs().median()*100:.3f}% (全窗 {rr.abs().median()*100:.3f}%)"
              f" | Σ|r|={rd.abs().sum()*100:.2f}% → 年化上界 {rd.abs().sum()/yrs*100:.3f}%/年"
              f" | 前3年占 {top3*100:.0f}%")

print("\n完(预筛结果只归档,不构成任何批准)")
