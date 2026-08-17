"""把三条生产信号 + 三份 long-flat 推荐持仓合并导出成一份自包含的信号文件。

**为什么要这个工具**：部署口径的信息本来散在六个 CSV 里（三条信号值 + 三份仓位），
拿去用的时候要自己 join。这里按 `backtest.baseline.SIGNALS` 这份权威注册表把它们
outer join 成一张宽表，列顺序按 long 段 Sharpe 从高到低（equal_weight 1.616 →
hybrid20 1.227 → citic40d 1.121，依据 `backtest/output/baseline_metrics.csv` 的
`kou_jing=blend, window=full, seg=long` 行）。

**这是快照，不是日更产物**：日更链路（`deploy/daily_signals/run_daily_signals.sh`）
不生成它，所以信号更新后它会过时。刷新就是重跑本脚本——纯本地读 committed CSV，
不连库、不调 Wind、零额度。

    python3 tools/export_combined_signals.py
    python3 tools/export_combined_signals.py --output /path/to/somewhere.csv

导出列（`*_signal` = 部署口径信号、`*_position` = long-flat 仓位 1 持多 / 0 空仓，
另加两列部署口径之前的原始量，见 `UPSTREAM`）：

    date, equal_weight_signal, equal_weight_signal_raw, equal_weight_position,
          hybrid20_signal, hybrid20_factor, hybrid20_position,
          citic40d_signal, citic40d_position

  * `equal_weight_signal_raw` = 四对等权平均后**未做 5 日平滑**的当天值。平滑会带
    滞后：2026-08-14 raw +0.4419 vs 平滑后 +0.1970。**部署口径是平滑后那列**。
  * `hybrid20_factor` = 状态机离散化**之前**的连续 tanh(z)（z 窗口 250）。
  * citic40d 无此类列——它的 `factor_20` 既无平滑也无离散化，本身就是最原始一层。

**三条线的 signal 取值域不同，不要混着比大小**：

  * `equal_weight_signal` 连续 (−1,1)：四对「成长 vs 价值」（沪深300/中证500/1000/2000）
    各算 spread=ln_ret(成长,20)−ln_ret(价值,20) → z=(spread−mean40)/std40 → tanh(z)，
    四对等权平均后再取 **5 日简单移动平均**（变体 A = 生产口径 20/40/5）。
  * `citic40d_signal` 连续 (−1,1)：五因子中信风格（成长vs稳定、周期vs消费、金融vs稳定、
    (成长+周期)vs(稳定+消费)、(成长+周期+金融)vs(稳定+消费)）同法 tanh(z) 等权平均，
    z 窗口 40，**无平滑**。
  * `hybrid20_signal` **离散 {−1,0,+1}**：「成长 vs 稳定」（中信风格）z 窗口 **250** 的
    tanh(z) 先过非对称阈值状态机（开多 >0.35 / 平多 <0.1 / 开空 <−0.15 / 平空 >−0.1，
    带状态保持），再要求空头得到财务面不反对才成立。它不是连续因子值。

三条线起点不同（citic40d 2010-04-02 / hybrid20 2011-04-18 / equal_weight
2014-01-02，各自 warmup 长度不同），早于某条线起点的行该列留空——**留空是事实，
不是缺失**，不要 ffill。
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from backtest.baseline import SIGNALS  # noqa: E402  (需先设好 sys.path)

# 按 long 段 Sharpe 降序（baseline_metrics.csv 的 blend/full/long 行）：
#   equal_weight 1.6163 > hybrid20 1.2269 > citic40d 1.1213
# 注意别按 full 段排——那会把 citic40d(0.783) 排到最后而 hybrid20(1.270) 靠前，
# 名次虽同但判据不是部署口径；部署是 long-flat，故一律看 seg=long。
ORDER = ["equal_weight", "hybrid20", "citic40d"]
# 信号列本身就是整数的线（状态机离散化产物），见模块 docstring 的取值域说明。
DISCRETE_SIGNALS = {"hybrid20"}

# 每条线在「部署口径」之前的那一层原始量，一并导出方便看当天未加工的读数。
# ⚠️ 只作参考：回测、Sharpe、long-flat 仓位全部建立在部署口径列上，这些原始列
# 没有经过同一套验证，别拿它们直接做决策。
# citic40d 不在此列——它的 factor_20 既无平滑也无离散化，本身就是最原始的一层。
UPSTREAM = {
    "equal_weight": ("output/equal_weight/equal_weight_signal_20d40z.csv",
                     "factor_value_raw", "equal_weight_signal_raw"),
    "hybrid20": ("output/hybrid20/confirmed_signal.csv",
                 "factor_20", "hybrid20_factor"),
}
DEFAULT_OUTPUT = ROOT / "output" / "recommended" / "combined_signals.csv"


def load_series(rel_path: str, column: str, name: str) -> pd.Series:
    df = pd.read_csv(ROOT / rel_path, parse_dates=["date"], index_col="date")
    if column not in df.columns:
        raise KeyError(f"{rel_path} 里没有列 {column!r}（实际列：{list(df.columns)}）")
    return df[column].rename(name)


def build_combined() -> pd.DataFrame:
    missing = [n for n in SIGNALS if n not in ORDER]
    if missing:
        raise RuntimeError(
            f"SIGNALS 注册表新增了 {missing}，但本脚本的 ORDER 没跟上——"
            f"请补进 ORDER 并核对 long 段 Sharpe 排序")
    cols: list[pd.Series] = []
    for name in ORDER:
        rel, column = SIGNALS[name]
        cols.append(load_series(rel, column, f"{name}_signal"))
        if name in UPSTREAM:
            up_rel, up_col, up_name = UPSTREAM[name]
            cols.append(load_series(up_rel, up_col, up_name))
        cols.append(load_series(
            f"output/recommended/{name}_longflat.csv", "position",
            f"{name}_position"))
    combined = pd.concat(cols, axis=1).sort_index()
    # 仓位是整数语义，outer join 会把有空洞的列变 float；空值保留为空，非空转 int。
    # hybrid20 的 signal 同样是整数（状态机三值），不转的话会被 float_format 印成
    # "-1.0000"，看着像连续因子值——那是误导，故一并转回 Int64。
    for name in ORDER:
        combined[f"{name}_position"] = combined[f"{name}_position"].astype("Int64")
        if name in DISCRETE_SIGNALS:
            combined[f"{name}_signal"] = combined[f"{name}_signal"].astype("Int64")
    return combined


def main() -> int:
    ap = argparse.ArgumentParser(description="导出合并的信号文件（快照，非日更产物）")
    ap.add_argument("--output", default=str(DEFAULT_OUTPUT), help=f"默认 {DEFAULT_OUTPUT}")
    args = ap.parse_args()

    combined = build_combined()
    out = Path(args.output)
    out.parent.mkdir(parents=True, exist_ok=True)
    combined.to_csv(out, index_label="date", float_format="%.4f")

    print(f"已写出 {out}")
    print(f"  行数   {len(combined)}   区间 {combined.index[0]:%Y-%m-%d} .. "
          f"{combined.index[-1]:%Y-%m-%d}")
    print(f"  列     {', '.join(combined.columns)}")
    print(f"  末行   {combined.index[-1]:%Y-%m-%d}")
    for name in ORDER:
        # 按列取而不是 .iloc[-1] 取整行——取整行会把混合 dtype 塌成 float，
        # 把整数列印成 "1.0"。
        sig = combined[f"{name}_signal"].iloc[-1]
        pos = combined[f"{name}_position"].iloc[-1]
        sig_s = f"{sig:+d}" if name in DISCRETE_SIGNALS else f"{sig:+.4f}"
        extra = ""
        if name in UPSTREAM:
            up_name = UPSTREAM[name][2]
            label = up_name.removeprefix(f"{name}_")   # 别用 split('_',1)——线名自带下划线
            extra = f"  {label}={combined[up_name].iloc[-1]:+.4f}"
        print(f"    {name:14s} signal={sig_s}{extra}  position={pos}")
    nonnull = combined.notna().sum()
    print("  各列非空行数（起点不同属正常，留空是事实不是缺失）：")
    for col in combined.columns:
        print(f"    {col:26s} {nonnull[col]:5d}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
