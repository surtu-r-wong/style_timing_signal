"""日内情景估算：给定今天的成长/价值涨跌幅，估算 equal_weight 线（期货池现役）的当天 raw 与平滑值。

**干什么用**：盘中收盘前想提前知道"今晚日更会不会翻仓/翻负"，不用等 20:30。
拉 PG `stock_selector.index_daily` 里 8 个风格指数的最新收盘（数据基期 = 最后一个已收盘交易日），
在末尾追加一个模拟交易日，按生产口径（`signals/equal_weight/generate_signal.py`，20/40/5，
tanh(z/2)，四对等权）重跑整条管线，报出当天 raw、SMA5 平滑值、对称持仓、翻负阈值与反解。

**用法**（本机已验证的解释器；任何带 psycopg2+pandas 的 python 均可）：

    # 统一冲击：四对的成长腿都 -6.5%、价值腿都 +1%（单位：百分数）
    /home/elfbob/claude-code/stock_selector/.venv/bin/python tools/intraday_scenario.py -g -6.5 -v 1.0

    # 分对冲击：-g/-v 是默认值，--pair 按市值段覆盖（格式 段:成长%:价值%，段 ∈ 300/500/1000/2000）
    ... tools/intraday_scenario.py -g -6.5 -v 0 --pair 300:-6.5:1.0 --pair 500:-6.5:1.0 --pair 1000:0:0 --pair 2000:0:0

    # 不带参数 = 今天四对平走（相对收益 0）的基准估算，顺带看阈值和反解
    ... tools/intraday_scenario.py

**口径要点**（详见模块 generate_signal.py 与 tools/export_combined_signals.py 头注）：
  * 每对：相对收益 = 成长简单收益 − 价值简单收益 → 20 日滚动复合 −1 → z=(cum−mean40)/std40 → tanh(z/2)
  * raw = 四对等权平均；平滑值 = raw 的 5 日简单移动平均；对称持仓按平滑值过零翻转（阈值 0，无死区）
  * 翻负条件：raw_today < −(窗口内前四天 raw 合计)。前四天 raw 由复算得出，与 committed CSV 对账。

**护栏**：复算的最后一个真实交易日若与 committed CSV 偏差 > 5e-4 直接报错退出（防管线漂移）；
只读 PG（SELECT），不写任何库表、不改任何输出文件。
"""
from __future__ import annotations

import argparse
import sys
from datetime import timedelta
from pathlib import Path

import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from signals.common.data_source import load_pg_closes  # noqa: E402
from signals.equal_weight.generate_signal import (  # noqa: E402
    calculate_contrast_equal_weight_signal,
    load_pair_configs,
)

PAIRS = [
    "沪深300成长", "沪深300价值",
    "中证500成长", "中证500价值",
    "中证1000成长", "中证1000价值",
    "中证2000成长", "中证2000价值",
]
SEGMENT = {"300": 0, "500": 2, "1000": 4, "2000": 6}  # 段名 -> PAIRS 下标（成长, 价值）
COMMITTED = ROOT / "output" / "equal_weight" / "equal_weight_signal_20d40z.csv"
PULL_DAYS = 180  # 日历天，约 120 个交易日，远够 20+40 窗口热身


def run_pipeline(prices: pd.DataFrame) -> pd.DataFrame:
    cfgs = load_pair_configs(price_columns=list(prices.columns))
    return calculate_contrast_equal_weight_signal(
        prices, lookback=20, z_window=40, smoothing_window=5, pair_configs=cfgs,
    )


def next_trading_day(d: pd.Timestamp) -> pd.Timestamp:
    nxt = d + timedelta(days=1)
    while nxt.weekday() >= 5:  # 只跳周末；法定节假日不在盘中估算的关心范围内
        nxt += timedelta(days=1)
    return nxt


def build_scenario_prices(
    prices: pd.DataFrame, base_g: float, base_v: float, overrides: dict[int, tuple[float, float]]
) -> tuple[pd.DataFrame, pd.Timestamp]:
    """在 prices 末尾追加一个模拟交易日：每对成长 ×(1+g)、价值 ×(1+v)，返回 (新价宽表, 模拟日期)。"""
    last = prices.index[-1]
    sim_dt = next_trading_day(last)
    p = prices.copy()
    row = p.loc[last].copy()
    shocks: list[tuple[str, float, float]] = []
    for i in range(0, len(PAIRS), 2):
        g, v = overrides.get(i, (base_g, base_v))
        row[PAIRS[i]] = row[PAIRS[i]] * (1 + g)
        row[PAIRS[i + 1]] = row[PAIRS[i + 1]] * (1 + v)
        shocks.append((PAIRS[i].replace("成长", ""), g, v))
    p.loc[sim_dt] = row
    p.attrs["shocks"] = shocks
    return p, sim_dt


def eval_scenario(prices: pd.DataFrame, base_g: float, base_v: float,
                  overrides: dict[int, tuple[float, float]]) -> dict:
    sp, sim_dt = build_scenario_prices(prices, base_g, base_v, overrides)
    out = run_pipeline(sp)
    row = out.loc[sim_dt]
    window = out["factor_value_raw"].iloc[-5:]
    prior4 = window.iloc[:-1].sum()
    return {
        "sim_dt": sim_dt, "row": row,
        "shocks": sp.attrs["shocks"],
        "threshold": -prior4,  # raw < threshold 则平滑值转负
    }


def solve_growth(prices: pd.DataFrame, v: float, thresh: float) -> float | None:
    """价值涨幅固定为 v%，找统一成长跌幅使 raw 恰好 = thresh。粗扫 + 二分（raw 随 g 递减）。"""
    grid = [0.0, -0.03, -0.06, -0.10, -0.15, -0.25, -0.40, -0.70]
    lo = hi = None
    for a, b in zip(grid, grid[1:]):  # a 不太负 -> raw 较高
        ra = eval_scenario(prices, a, v, {})["row"]["factor_value_raw"]
        rb = eval_scenario(prices, b, v, {})["row"]["factor_value_raw"]
        if ra > thresh >= rb:
            lo, hi = a, b  # lo 不太负端, hi 更负端
            break
    if lo is None:
        return None
    for _ in range(30):
        mid = (lo + hi) / 2
        r = eval_scenario(prices, mid, v, {})["row"]["factor_value_raw"]
        lo, hi = (mid, hi) if r > thresh else (lo, mid)
    return (lo + hi) / 2


def solve_value(prices: pd.DataFrame, g: float, thresh: float) -> float | None:
    """成长涨幅固定为 g%，找统一价值涨幅使 raw 恰好 = thresh。raw 随 v 递减。"""
    grid = [0.0, 0.02, 0.04, 0.07, 0.10, 0.15, 0.25, 0.50]
    lo = hi = None
    for a, b in zip(grid, grid[1:]):  # a 小 -> raw 较高
        ra = eval_scenario(prices, g, a, {})["row"]["factor_value_raw"]
        rb = eval_scenario(prices, g, b, {})["row"]["factor_value_raw"]
        if ra > thresh >= rb:
            lo, hi = a, b
            break
    if lo is None:
        return None
    for _ in range(30):
        mid = (lo + hi) / 2
        r = eval_scenario(prices, g, mid, {})["row"]["factor_value_raw"]
        lo, hi = (mid, hi) if r > thresh else (lo, mid)
    return (lo + hi) / 2


def parse_pair(spec: str) -> tuple[int, float, float]:
    """'300:-6.5:1.0' -> (PAIRS 下标, g, v)，单位百分数。"""
    parts = spec.split(":")
    if len(parts) != 3 or parts[0] not in SEGMENT:
        raise argparse.ArgumentTypeError(
            f"--pair 格式应为 段:成长%:价值%（段 ∈ {sorted(SEGMENT)}），收到: {spec!r}")
    seg, g, v = parts[0], float(parts[1]), float(parts[2])
    return SEGMENT[seg], g / 100.0, v / 100.0


def main() -> int:
    ap = argparse.ArgumentParser(
        description="equal_weight 线日内情景估算（成长/价值涨跌幅 -> raw/SMA5/持仓/翻负阈值）",
        formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("-g", "--growth", type=float, default=0.0, help="四对成长腿统一涨跌幅%%（默认 0）")
    ap.add_argument("-v", "--value", type=float, default=0.0, help="四对价值腿统一涨跌幅%%（默认 0）")
    ap.add_argument("--pair", type=parse_pair, action="append", default=[], metavar="段:成长%%:价值%%",
                    help="按市值段覆盖统一值，可重复，段 ∈ 300/500/1000/2000")
    args = ap.parse_args()

    overrides = {idx: (g, v) for idx, g, v in args.pair}
    if overrides:
        segs = sorted({PAIRS[i].replace("成长", "").replace("中证", "").replace("沪深", "")
                       for i in overrides})
        print(f"分对覆盖: {', '.join(segs)}（未列出的段用统一值 g={args.growth:+.2f}% v={args.value:+.2f}%）")

    prices = load_pg_closes(PAIRS, start=(pd.Timestamp.today() - pd.Timedelta(days=PULL_DAYS)).strftime("%Y-%m-%d"),
                            trim_ragged_tail=True, trim_ragged_head=True)
    base_date = prices.index[-1]
    print(f"数据基期: {base_date.date()}（PG index_daily，8 指数，{len(prices)} 行）")

    # 口径对账：复算 vs committed CSV 的最后公共交易日
    base_out = run_pipeline(prices)
    committed = pd.read_csv(COMMITTED, index_col=0, parse_dates=True)
    check_date = min(base_date, committed.index[-1])
    diff = (base_out.loc[check_date, ["factor_value_raw", "factor_value"]]
            - committed.loc[check_date, ["factor_value_raw", "factor_value"]]).abs().max()
    if diff > 5e-4:
        print(f"口径校验失败: 复算与 committed {check_date.date()} 偏差 {diff:.6f} > 5e-4，管线可能漂移，退出")
        return 1
    print(f"口径校验: 对账 {check_date.date()} 最大偏差 {diff:.6f} ✓")

    res = eval_scenario(prices, args.growth / 100.0, args.value / 100.0, overrides)
    row, thresh, sim_dt = res["row"], res["threshold"], res["sim_dt"]
    pos = 1 if row["factor_value"] > 0 else (-1 if row["factor_value"] < 0 else 0)

    print(f"模拟日期: {sim_dt.date()}（下一交易日）")
    print(f"冲击明细: " + " | ".join(f"{n} 成长{g*100:+.2f}%/价值{v*100:+.2f}%" for n, g, v in res["shocks"]))
    print(f"\n翻负阈值: raw_today < {thresh:+.4f} 则平滑值转负（前四天 raw 合计 {-thresh:+.4f}）")
    pair_cells = " | ".join(
        f"{PAIRS[i*2].replace('成长','')} {row[f'pair_{i+1:02d}_factor_20']:+.3f}" for i in range(4))
    print(f"四对 factor: {pair_cells}")
    raw, sm = row["factor_value_raw"], row["factor_value"]
    print(f"raw 当天值   = {raw:+.4f}   距阈值 {raw - thresh:+.4f}")
    print(f"平滑值 SMA5  = {sm:+.4f}   -> 对称持仓 {pos:+d}")

    if not overrides:  # 统一冲击模式下给反解
        gs = solve_growth(prices, args.value / 100.0, thresh)
        vs = solve_value(prices, args.growth / 100.0, thresh)
        gtxt = f"成长跌至 {gs*100:.2f}%" if gs is not None else "扫描区间内单日成长冲击翻不了负"
        vtxt = f"价值涨至 +{vs*100:.2f}%" if vs is not None else "扫描区间内单日价值冲击翻不了负"
        print(f"反解(统一): {gtxt}（价值 {args.value:+.2f}% 固定）才翻负")
        print(f"          成长 {args.growth:+.2f}% 固定时，{vtxt} 才翻负")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
