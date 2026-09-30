"""双引擎 v1 装配 + 分开评价 + CLI（Phase 3，见 2026-07-06-phase3-dual-engine-v1-plan.md）。

多头引擎 = 基信号多头段（门槛低）；空头引擎 = 基信号空头段（门槛高）经 carry 保护层门控；
执行层合成 net = long + short（多空同触发→0）。评价分开：多头 vs 满仓、空头避险价值。
"""
import sys
from pathlib import Path

import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from backtest.gates import carry_protection  # noqa: E402
from backtest.metrics import max_drawdown  # noqa: E402
from backtest.positions import to_position_asym  # noqa: E402


def synthesize(long_leg: pd.Series, short_leg: pd.Series) -> pd.Series:
    """合成净仓位：long+short，多空同触发(long>0 & short<0)→0，clip 到 [−1,1]。"""
    both = (long_leg > 0) & (short_leg < 0)
    net = (long_leg.astype(float) + short_leg.astype(float)).clip(-1.0, 1.0)
    return net.where(~both, 0.0)


def _monthly_compound(r: pd.Series) -> pd.Series:
    """日收益 → 月复利收益（按自然月分组，版本无关）。"""
    return (1.0 + r).groupby(r.index.to_period("M")).prod() - 1.0


def hedge_value(dual_ret: pd.Series, long_only_ret: pd.Series,
                underlying: pd.Series, down_threshold: float = -0.05) -> dict:
    """空头引擎避险价值（§1.3）。

    - down_month_hit：标的月收益 < down_threshold 的月份里，dual 月收益 > long_only 月收益的占比
      （无跌月 → nan）
    - maxdd_improve：|maxdd(long_only)| − |maxdd(dual)|（正=加空头腿后回撤改善）
    """
    u_m = _monthly_compound(underlying)
    d_m = _monthly_compound(dual_ret)
    l_m = _monthly_compound(long_only_ret)

    down = u_m[u_m < down_threshold]
    if len(down) == 0:
        hit = float("nan")
    else:
        beat = d_m.reindex(down.index) > l_m.reindex(down.index)
        hit = float(beat.mean())

    maxdd_improve = abs(max_drawdown(long_only_ret)) - abs(max_drawdown(dual_ret))
    return {"down_month_hit": hit, "n_down_months": int(len(down)),
            "maxdd_improve": float(maxdd_improve)}


def dual_legs(factor: pd.Series, carry: pd.Series, long_theta: float,
              short_theta: float, carry_theta: float):
    """连续因子 → 非对称离散 → 拆多空腿（空头腿经 carry 保护层门控）→ (long_leg, short_leg, net)。"""
    asym = to_position_asym(factor, long_theta, short_theta)
    long_leg = asym.clip(lower=0)
    short_leg = carry_protection(asym.clip(upper=0), carry, carry_theta)
    net = synthesize(long_leg, short_leg)
    return long_leg, short_leg, net


def assemble_dual(factor: pd.Series, carry: pd.Series, long_theta: float,
                  short_theta: float, carry_theta: float) -> pd.Series:
    """双引擎合成净仓位（多头段 + carry 门控空头段）。"""
    return dual_legs(factor, carry, long_theta, short_theta, carry_theta)[2]


def dual_legs_external_short(long_factor: pd.Series, short_signal: pd.Series, carry: pd.Series,
                            long_theta: float, carry_theta: float):
    """专属空头信号装配（Phase 4）：多头来自基因子(long_theta 门槛, long-only)，
    空头来自【外部信号】(carry 保护层门控)，执行层 synthesize 合成。

    与 dual_legs 的区别：空头腿不再从基因子空头段派生，而是喂外部脆弱性信号（如广度背离），
    检验专属信号能否补上共享风格短腿缺的避险价值（v1 已证共享短腿无用）。
    """
    long_leg = to_position_asym(long_factor, long_theta, long_theta).clip(lower=0)
    short_leg = carry_protection(short_signal.astype(float), carry, carry_theta)
    net = synthesize(long_leg, short_leg)
    return long_leg, short_leg, net


# ---------------- 编排 + CLI ----------------
DEFAULTS = {"long_theta": 0.10, "short_theta": 0.30, "carry_theta": 0.06}


def load_factor(name: str = "equal_weight") -> pd.Series:
    """读连续因子（不离散化）—— 双引擎从连续因子做非对称映射。"""
    from backtest.baseline import SIGNALS
    path, col = SIGNALS[name]
    df = pd.read_csv(ROOT / path, parse_dates=["date"]).set_index("date").sort_index()
    return df[col]


def build_dual_report(name="equal_weight", long_theta=0.10, short_theta=0.30,
                      carry_theta=0.06, cost_bps=3.0, bootstrap_n=500, seed=0,
                      db=None) -> pd.DataFrame:
    from backtest.baseline import KOU_JING, WINDOWS, _MIN_OBS, _row, _slice
    from backtest.data import load_carry, load_underlying_returns
    from backtest.engine import run_strategy
    from backtest.positions import to_position
    from backtest.significance import bootstrap_pvalue

    factor = load_factor(name)
    raw_sym = to_position(factor, mode="discrete", threshold=0.0)
    und_all = {kj: load_underlying_returns(kj, db=db) for kj in KOU_JING}
    car_all = {kj: load_carry(kj, db=db) for kj in KOU_JING}

    rows = []
    for kj in KOU_JING:
        for win, (s, e) in WINDOWS.items():
            und = _slice(und_all[kj], s, e)
            car = _slice(car_all[kj], s, e)
            f = _slice(factor, s, e)
            idx = f.index.intersection(und.index)
            if len(idx) < _MIN_OBS:
                continue
            f, u, c = f.reindex(idx), und.reindex(idx), car.reindex(idx)
            long_leg, short_leg, net = dual_legs(f, c, long_theta, short_theta, carry_theta)
            raw = _slice(raw_sym, s, e).reindex(idx).fillna(0)

            variants = {"long_engine": long_leg, "short_engine": short_leg,
                        "dual": net, "raw_symmetric": raw}
            rets = {}
            for vname, p in variants.items():
                ret = run_strategy(p, u, cost_bps, c)["ret"]
                rets[vname] = ret
                row = {"variant": vname, "kou_jing": kj, "window": win, **_row(ret, p)}
                if bootstrap_n and vname in ("dual", "short_engine", "raw_symmetric"):
                    row["pvalue"] = bootstrap_pvalue(p, u, "sharpe", bootstrap_n, seed, c, cost_bps)
                if vname == "dual":
                    row.update(hedge_value(rets["dual"], rets["long_engine"], u))
                rows.append(row)

            # buy_hold：满仓持有标的指数（spot，无 carry/无换手），与 baseline 口径一致
            rows.append({"variant": "buy_hold", "kou_jing": kj, "window": win,
                         **_row(u, pd.Series(0.0, index=idx))})
    return pd.DataFrame(rows)


def main() -> int:
    import argparse
    ap = argparse.ArgumentParser(description="Phase3 双引擎 v1：非对称阈值 + carry 保护层")
    ap.add_argument("--signal", default="equal_weight",
                    choices=["equal_weight", "citic40d", "hybrid20"])
    ap.add_argument("--long-theta", type=float, default=DEFAULTS["long_theta"])
    ap.add_argument("--short-theta", type=float, default=DEFAULTS["short_theta"])
    ap.add_argument("--carry-theta", type=float, default=DEFAULTS["carry_theta"])
    ap.add_argument("--cost-bps", type=float, default=3.0)
    ap.add_argument("--bootstrap", type=int, default=500)
    args = ap.parse_args()

    rep = build_dual_report(
        args.signal, args.long_theta, args.short_theta, args.carry_theta,
        args.cost_bps, args.bootstrap,
    )
    out_dir = ROOT / "backtest" / "output"
    out_dir.mkdir(parents=True, exist_ok=True)
    out_path = out_dir / "dual_engine_metrics.csv"
    rep.to_csv(out_path, index=False)

    show = rep.copy()
    for col in ("ann", "maxdd", "hit", "down_month_hit", "maxdd_improve"):
        if col in show:
            show[col] = (show[col] * 100).round(1)
    for col in ("sharpe", "calmar", "turnover"):
        show[col] = show[col].round(2)
    if "pvalue" in show:
        show["pvalue"] = show["pvalue"].round(3)
    print(f"[{args.signal}] long_theta={args.long_theta} short_theta={args.short_theta} "
          f"carry_theta={args.carry_theta}")
    print(show.to_string(index=False))
    print(f"\n→ {out_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
