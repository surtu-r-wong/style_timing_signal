"""机制关 preflight：三分量信号（v1 混合/v2 纯风格/rotation）对宽基交易标的的预测 IC。

检验 2026-07-08 切主评估的「失配」解释（宽基择时需要行业轮动成分）——该解释当年
从未对交易标的测过（decompose.py 的 IC 目标只有指数对价差）。设计与判读规则冻结在
docs/plans/2026-08-20-classifier-swap-argument.md §7.5（先于运行提交）。

方法 = decompose.py ③ 原法（月末信号 vs 次月收益和、非重叠、Spearman 主/Pearson 副、
日频 d5/d10/d20 参考），仅换目标为宽基现货日收益（backtest.data.load_underlying_returns，
与 baseline 秤同一标的层，不含 carry）。两个锚：
  ① 接线锚 = 复现已发表的指数对价差 IC（U2: v1/v2/rot = 0.126/0.179/0.050 Spearman）；
  ② 量级锚 = 生产 ew 信号对宽基的 IC。

CLI: python3 -W ignore -m backtest.rotation_target_probe [--universes U2,U0]
产出: backtest/output/rotation_target_probe.csv + 控制台摘要。
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from backtest.data import load_underlying_returns  # noqa: E402
from signals.common.config import load_db_config  # noqa: E402
from signals.style_basket.decompose import _load_spread, _signal  # noqa: E402
from signals.style_basket.validate import INDEX_PAIRS, _fetch_index_closes  # noqa: E402

EW_SIGNAL_FILE = ROOT / "output" / "equal_weight" / "equal_weight_signal_20d40z.csv"
SUBWINDOWS = {"2014-19": (None, "2019-12-31"), "2020-26": ("2020-01-01", None)}
BROAD_TARGETS = ["blend", "500", "1000"]


def _monthly_ic(sig: pd.Series, ret: pd.Series, start=None, end=None) -> dict:
    """decompose.py ③ 原法：月末信号 vs 次月收益和（非重叠）。"""
    base = pd.concat([sig.rename("sig"), ret.rename("ret")], axis=1).dropna()
    if start:
        base = base.loc[base.index >= pd.Timestamp(start)]
    if end:
        base = base.loc[base.index <= pd.Timestamp(end)]
    month_sig = base["sig"].resample("ME").last()
    fwd = base["ret"].resample("ME").sum().shift(-1)
    mj = pd.concat([month_sig, fwd], axis=1).dropna()
    out = {
        "ic_monthly_pearson": mj.iloc[:, 0].corr(mj.iloc[:, 1]),
        "ic_monthly_spearman": mj.iloc[:, 0].corr(mj.iloc[:, 1], method="spearman"),
        "n_months": len(mj),
    }
    for k in (5, 10, 20):
        fwd_k = base["ret"].rolling(k).sum().shift(-k)
        dj = pd.concat([base["sig"], fwd_k], axis=1).dropna()
        out[f"ic_d{k}_spearman"] = dj.iloc[:, 0].corr(dj.iloc[:, 1], method="spearman")
    return out


def build_signals(uni: str) -> dict[str, pd.Series]:
    v1 = _load_spread(uni)
    v2 = _load_spread(uni, "_neutral")
    joint = pd.concat(
        [v1["spread"].rename("v1"), v2["spread"].rename("v2")], axis=1, join="inner"
    ).dropna()
    rot_nav = (1.0 + (joint["v1"] - joint["v2"])).cumprod()
    ones = pd.Series(1.0, index=joint.index)
    return {
        "v1_mixed": _signal(v1["growth_index"], v1["value_index"]),
        "v2_pure_style": _signal(v2["growth_index"], v2["value_index"]),
        "rotation": _signal(rot_nav, ones),
    }


def run(universes: list[str], db=None) -> pd.DataFrame:
    db = db or load_db_config()

    # 目标序列：宽基现货收益（baseline 同层）+ 指数对价差 blend（接线锚用）
    broad = {kj: load_underlying_returns(kj, db=db) for kj in BROAD_TARGETS}
    idx_close = _fetch_index_closes(db, [c for p in INDEX_PAIRS.values() for c in p])
    pair_rets = {
        name: idx_close[g].pct_change(fill_method=None)
        - idx_close[v].pct_change(fill_method=None)
        for name, (g, v) in INDEX_PAIRS.items()
        if g in idx_close.columns and v in idx_close.columns
    }
    pair_blend = pd.concat(pair_rets.values(), axis=1).mean(axis=1)

    ew = (
        pd.read_csv(EW_SIGNAL_FILE, parse_dates=["date"])
        .set_index("date").sort_index()["factor_value"]
    )

    rows = []
    for uni in universes:
        sigs = build_signals(uni)
        for sig_name, sig in sigs.items():
            # 接线锚：指数对价差 blend（预期复现 decompose.py 已发表值）
            rows.append({"universe": uni, "signal": sig_name, "target": "pair_blend(锚)",
                         "window": "full", **_monthly_ic(sig, pair_blend)})
            for tgt in BROAD_TARGETS:
                rows.append({"universe": uni, "signal": sig_name, "target": f"宽基{tgt}",
                             "window": "full", **_monthly_ic(sig, broad[tgt])})
                for wname, (s, e) in SUBWINDOWS.items():
                    rows.append({"universe": uni, "signal": sig_name, "target": f"宽基{tgt}",
                                 "window": wname, **_monthly_ic(sig, broad[tgt], s, e)})
    # 量级锚：生产 ew 信号对宽基
    for tgt in BROAD_TARGETS:
        rows.append({"universe": "-", "signal": "ew_生产锚", "target": f"宽基{tgt}",
                     "window": "full", **_monthly_ic(ew, broad[tgt])})
        for wname, (s, e) in SUBWINDOWS.items():
            rows.append({"universe": "-", "signal": "ew_生产锚", "target": f"宽基{tgt}",
                         "window": wname, **_monthly_ic(ew, broad[tgt], s, e)})
    return pd.DataFrame(rows)


def main() -> int:
    ap = argparse.ArgumentParser(description="机制关 preflight：三分量对宽基标的的 IC")
    ap.add_argument("--universes", default="U2,U0")
    args = ap.parse_args()

    rep = run([u.strip() for u in args.universes.split(",") if u.strip()])
    out = ROOT / "backtest" / "output" / "rotation_target_probe.csv"
    out.parent.mkdir(parents=True, exist_ok=True)
    rep.to_csv(out, index=False)

    show = rep.copy()
    for c in [c for c in show.columns if c.startswith("ic_")]:
        show[c] = show[c].round(3)
    print(show.to_string(index=False))
    print(f"\n→ {out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
