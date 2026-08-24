"""新轮动轴·批次一构建：低波/动量/流动性/股息 四轴 × 四母带腿对日收益。

设计与判读规则冻结：docs/plans/2026-08-24-new-rotation-axes-entry-ticket.md §1
（先于运行提交）。样本空间/调样日程/漂移语义/DP 机器全部复用
`backtest.pure_style_builder`（腿工厂），本模块只新增价格因子与三分位选腿。

CLI: python3 -m backtest.axis_rotation_builder --output-dir <dir>
产出: axis_legs_daily.csv（date, axis, band, long_ret, short_ret）+ axis_build.json。
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from backtest.pure_style_builder import (  # noqa: E402
    _daily_returns,
    _dividend_field,
    _fetch_dp_indicator,
    _fetch_series,
    dividend_ttm_events,
    dp_source_for,
    rebalance_dates,
    review_cutoff,
    sample_space,
)

AXES = ("lowvol", "momentum", "liquidity", "dividend")
BANDS = {"300": (1, 300), "500": (301, 800), "1000": (801, 1800), "2000": (1801, 3800)}
START_EFF = "2015-06-01"           # 首个 eff = 2015-06-15（与 geo5 同窗）
FETCH_CAL_DAYS = 420               # 因子窗取数：cutoff 前自然日
VOL_WINDOW = 244                   # 低波：最后 N 个交易日
MOM_WINDOW, MOM_SKIP = 252, 21     # 动量：跳过最后 21 日、再往前至多 252 日
MIN_OBS = 120                      # 两因子的最少有效日
MIN_LEG = 10                       # 任一腿低于此只数 → 该带该期跳过


# ---------------------------------------------------------------- 纯函数
def price_factors(rets: pd.DataFrame) -> pd.DataFrame:
    """日收益宽表（date × ts_code，窗末=cutoff）→ 低波/动量因子。

    低波 = 最后 VOL_WINDOW 行的 std；动量 = 跳过最后 MOM_SKIP 行、再往前至多
    MOM_WINDOW 行的累计收益。有效日 < MIN_OBS 置 NaN（调用侧按轴剔除）。
    """
    if rets.empty:
        return pd.DataFrame(columns=["vol", "mom"])
    tail = rets.iloc[-VOL_WINDOW:]
    vol = tail.std()
    vol[tail.notna().sum() < MIN_OBS] = np.nan

    mom_slice = rets.iloc[-(MOM_WINDOW + MOM_SKIP):-MOM_SKIP] if len(rets) > MOM_SKIP \
        else rets.iloc[0:0]
    if len(mom_slice):
        mom = (1.0 + mom_slice.fillna(0.0)).prod() - 1.0
        mom[mom_slice.notna().sum() < MIN_OBS] = np.nan
    else:
        mom = pd.Series(np.nan, index=rets.columns)
    return pd.DataFrame({"vol": vol, "mom": mom})


def tercile_split(factor: pd.Series) -> tuple[list[str], list[str]]:
    """按因子升序，两端各取 n//3 只 →（低端名单, 高端名单）。"""
    f = factor.dropna().sort_values(kind="mergesort")
    n = len(f) // 3
    if n == 0:
        return [], []
    return list(f.index[:n]), list(f.index[-n:])


def axis_legs(factors: pd.DataFrame, axis: str) -> tuple[list[str], list[str]]:
    """canonical 方向（设计稿 §1）：返回（多腿, 空腿）。

    factors 列：vol / mom / liq / dp（index=ts_code）。
    """
    col, long_is_high = {
        "lowvol": ("vol", False),        # 低波腿 − 高波腿
        "momentum": ("mom", True),       # 高动量 − 低动量
        "liquidity": ("liq", True),      # 高换手 − 低换手
        "dividend": ("dp", True),        # 高股息 − 低股息
    }[axis]
    lo, hi = tercile_split(factors[col])
    return (hi, lo) if long_is_high else (lo, hi)


def drift_leg(codes: list[str], wide: pd.DataFrame) -> pd.Series:
    """等权腿在 wide（date × ts_code 日收益）上的漂移日收益（_leg_returns 同款语义）。"""
    if not codes or wide.empty:
        return pd.Series(dtype=float)
    r = wide.reindex(columns=codes).fillna(0.0)          # 停牌/缺行 → 当日 0 收益
    w = np.ones(len(codes), dtype=float)
    out = {}
    for dt, row in zip(r.index, r.to_numpy(dtype=float)):
        tot = w.sum()
        out[dt] = float((w * row).sum() / tot) if tot > 0 else 0.0
        w = w * (1.0 + row)
    return pd.Series(out).sort_index()


# ---------------------------------------------------------------- 取数装配
def dp_factor(sp: pd.DataFrame, cutoff: pd.Timestamp) -> pd.Series:
    """D/P：逐字复用腿工厂已冻结机器（factor_panel 的 DP 分支）。"""
    codes = list(sp.index)
    if dp_source_for(cutoff) == "indicator":
        return _fetch_dp_indicator(codes, cutoff).reindex(sp.index).fillna(0.0)
    dps = dividend_ttm_events(
        _fetch_series(codes, cutoff, "dividend", _dividend_field()), cutoff)
    return (dps / sp["avg_close_1y"]).reindex(sp.index).fillna(0.0)


def build_period_band(band: str, cutoff: pd.Timestamp, eff: pd.Timestamp,
                      nxt: pd.Timestamp, verbose: bool = True
                      ) -> tuple[pd.DataFrame, dict]:
    """一个（调样期 × 带）的四轴腿对日收益；返回 (长表, 台账条目)。"""
    lo, hi = BANDS[band]
    sp = sample_space(cutoff, lo, hi)
    log = {"band": band, "eff": str(eff.date()), "cutoff": str(cutoff.date()),
           "space": len(sp), "legs": {}, "skipped": []}
    if len(sp) < 3 * MIN_LEG:
        log["skipped"] = list(AXES)
        return pd.DataFrame(), log

    hist = _daily_returns(list(sp.index),
                          cutoff - pd.Timedelta(days=FETCH_CAL_DAYS), cutoff)
    pf = price_factors(hist).reindex(sp.index)
    factors = pd.DataFrame({
        "vol": pf["vol"],
        "mom": pf["mom"],
        "liq": (sp["adv_1y"] / sp["avg_mv_1y"]).where(
            sp["adv_1y"].notna() & sp["avg_mv_1y"].notna()),
        "dp": dp_factor(sp, cutoff),
    })

    legs = {}
    for axis in AXES:
        lg, sh = axis_legs(factors, axis)
        if len(lg) < MIN_LEG or len(sh) < MIN_LEG:
            log["skipped"].append(axis)
            continue
        legs[axis] = (lg, sh)
        log["legs"][axis] = {"long": len(lg), "short": len(sh)}
    if not legs:
        return pd.DataFrame(), log

    union = sorted({c for lg, sh in legs.values() for c in lg + sh})
    fwd = _daily_returns(union, eff, nxt)
    rows = []
    for axis, (lg, sh) in legs.items():
        long_r, short_r = drift_leg(lg, fwd), drift_leg(sh, fwd)
        pair = pd.concat([long_r.rename("long_ret"), short_r.rename("short_ret")],
                         axis=1).dropna()
        pair.insert(0, "band", band)
        pair.insert(0, "axis", axis)
        rows.append(pair.reset_index(names="date"))
    if verbose:
        legs_desc = " ".join(f"{a}:{v['long']}/{v['short']}" for a, v in log["legs"].items())
        print(f"  {band}带 @{cutoff.date()} 空间 {len(sp)} → {legs_desc}"
              + (f" 跳过 {log['skipped']}" if log["skipped"] else ""), flush=True)
    return (pd.concat(rows, ignore_index=True) if rows else pd.DataFrame()), log


def build(end: str | None = None, verbose: bool = True) -> tuple[pd.DataFrame, dict]:
    end = end or str(pd.Timestamp.today().date())
    effs = rebalance_dates(START_EFF, end)
    bounds = list(zip(effs, effs[1:] + [pd.Timestamp(end)]))
    if verbose:
        print(f"新轮动轴构建：{len(effs)} 期，{effs[0].date()} → {end}", flush=True)
    frames, logs = [], []
    for eff, nxt in bounds:
        cutoff = review_cutoff(eff)
        for band in BANDS:
            df, log = build_period_band(band, cutoff, eff, nxt, verbose)
            logs.append(log)
            if len(df):
                frames.append(df)
    out = pd.concat(frames, ignore_index=True).sort_values(
        ["axis", "band", "date"]).reset_index(drop=True)
    build_log = {"n_periods": len(effs), "start_eff": str(effs[0].date()), "end": end,
                 "periods": logs}
    return out, build_log


def main() -> int:
    ap = argparse.ArgumentParser(description="新轮动轴批次一构建（四轴×四带腿对）")
    ap.add_argument("--output-dir", required=True)
    ap.add_argument("--end", default=None)
    args = ap.parse_args()
    out_dir = Path(args.output_dir)
    out_dir.mkdir(parents=True, exist_ok=True)

    legs, log = build(end=args.end)
    legs.to_csv(out_dir / "axis_legs_daily.csv", index=False)
    (out_dir / "axis_build.json").write_text(
        json.dumps(log, ensure_ascii=False, indent=1))
    n_skip = sum(1 for p in log["periods"] if p["skipped"])
    print(f"落盘 axis_legs_daily.csv（{len(legs)} 行）+ axis_build.json"
          f"（{len(log['periods'])} 期带，含跳过 {n_skip} 条）", flush=True)
    print("AXIS BUILD DONE", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
