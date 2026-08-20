"""2021-2023 反超线深挖（2026-08-20 用户指令「按这个再继续挖」）。

背景：7 月切主评估的分窗结构（repo 文档 §7.8 前后）——自建混合篮子(U2)信号
在现役最弱窗 2021-2023 三口径×两段一致反超 +0.29~+0.44，其余窗落后。
本探针四问：
  ① 逐年拆解：反超是三年均匀还是单年侥幸；
  ② 窗内配对显著性：2021-2023 段 mixed vs ew 的 paired block bootstrap；
  ③ 归因：反超属混合版不属提纯版 → 行业分量；rotation 信号窗内单独表现佐证；
  ④ 探索性集成（⚠️ 选择偏置声明：假设来自看过 2021-2023 分窗后形成，
     全窗读数含 in-sample 选择成分，正式采用须另行预登记）：
     E50 = mean(现役 ew 因子, mixed 因子)；E5 = mixed 作第 5 输入等权
     （四对因子 + mixed，各 1/5）。同秤全套 + 配对 vs 现役。

CLI: python3 -W ignore -m backtest.mixed_ensemble_probe
产出: backtest/output/mixed_ensemble_probe_{report,paired,yearly}.csv + 控制台。
"""
from __future__ import annotations

import sys
from pathlib import Path

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from backtest.baseline import build_report  # noqa: E402
from backtest.data import load_carry, load_underlying_returns  # noqa: E402
from backtest.engine import run_strategy  # noqa: E402
from backtest.metrics import sharpe  # noqa: E402
from backtest.paired_bootstrap import paired_block_bootstrap_sharpe_diff  # noqa: E402
from backtest.positions import to_position  # noqa: E402
from signals.common.config import load_db_config  # noqa: E402
from signals.equal_weight.generate_signal import _compute_pair_signal  # noqa: E402
from signals.style_basket.decompose import _load_spread, _signal  # noqa: E402
from signals.style_basket.validate import INDEX_PAIRS, _fetch_index_closes  # noqa: E402

EW_FILE = ROOT / "output" / "equal_weight" / "equal_weight_signal_20d40z.csv"
MIXED_FILE = ROOT / "output" / "style_basket" / "signal_self_mixed_U2.csv"


def _load_factor(path: Path) -> pd.Series:
    df = pd.read_csv(path, parse_dates=["date"]).set_index("date").sort_index()
    return df["factor_value"]


def _rotation_signal() -> pd.Series:
    v1, v2 = _load_spread("U2"), _load_spread("U2", "_neutral")
    joint = pd.concat([v1["spread"].rename("a"), v2["spread"].rename("b")],
                      axis=1, join="inner").dropna()
    rot_nav = (1.0 + (joint["a"] - joint["b"])).cumprod()
    return _signal(rot_nav, pd.Series(1.0, index=joint.index))


def main() -> int:
    db = load_db_config()
    ew, mixed = _load_factor(EW_FILE), _load_factor(MIXED_FILE)
    closes = _fetch_index_closes(db, [c for p in INDEX_PAIRS.values() for c in p])
    pair_sig = pd.DataFrame(
        {n: _compute_pair_signal(closes[g], closes[v], lookback=20, z_window=40)
         .rolling(5, min_periods=1).mean() for n, (g, v) in INDEX_PAIRS.items()})
    rot = _rotation_signal()

    idx = ew.index.intersection(mixed.index).intersection(pair_sig.dropna().index)
    factors = {
        "ew": ew.reindex(idx),
        "mixed": mixed.reindex(idx),
        "rotation": rot.reindex(idx),
        "E50": (ew.reindex(idx) + mixed.reindex(idx)) / 2,
        "E5": (pair_sig.reindex(idx).sum(axis=1) + mixed.reindex(idx)) / 5,
    }
    positions = {k: to_position(v, "discrete") for k, v in factors.items()}

    und = load_underlying_returns("blend", db=db).reindex(idx).dropna()
    car = load_carry("blend", db=db)

    def _ret(name: str, seg: str) -> pd.Series:
        p = positions[name].reindex(und.index).astype(float)
        if seg == "long":
            p = p.clip(lower=0)
        return run_strategy(p, und, 3.0, car.reindex(und.index))["ret"]

    # ① 逐年拆解（blend long-flat）
    rows_y = []
    for name in ["ew", "mixed", "rotation", "E50", "E5"]:
        r = _ret(name, "long")
        for y, grp in r.groupby(r.index.year):
            rows_y.append({"signal": name, "year": int(y), "sharpe": sharpe(grp)})
    yearly = pd.DataFrame(rows_y).pivot(index="year", columns="signal", values="sharpe")
    yearly["mixed-ew"] = yearly["mixed"] - yearly["ew"]

    # ② 窗内配对 + 全窗配对（候选 vs ew）
    rows_p = []
    for name in ["mixed", "rotation", "E50", "E5"]:
        for seg in ["long", "full"]:
            for wname, (s, e) in {"2021-2023": ("2021-01-01", "2023-12-31"),
                                  "full": (None, None)}.items():
                ra, rb = _ret(name, seg), _ret("ew", seg)
                if s:
                    ra, rb = ra[s:e], rb[s:e]
                d = paired_block_bootstrap_sharpe_diff(ra, rb, block=20, n=10000, seed=0)
                rows_p.append({"signal": name, "seg": seg, "window": wname, **d})
    paired = pd.DataFrame(rows_p)

    # ④ 同秤全套（四窗三口径三段，供档案）
    sigs = {k: ("<inline>", "factor_value") for k in positions}
    rep = build_report(mode="discrete", bootstrap_n=0, cost_bps=3.0, db=db,
                       signals=sigs, positions=positions)

    out = ROOT / "backtest" / "output"
    yearly.to_csv(out / "mixed_ensemble_probe_yearly.csv")
    paired.to_csv(out / "mixed_ensemble_probe_paired.csv", index=False)
    rep.to_csv(out / "mixed_ensemble_probe_report.csv", index=False)

    print("① 逐年 Sharpe（blend long-flat）:")
    print(yearly.round(2).to_string())
    print("\n② 配对检验（候选 − ew 现役, blend）:")
    cols = ["signal", "seg", "window", "diff_sharpe", "ci_lo", "ci_hi", "p_value", "n_obs"]
    print(paired[cols].round(4).to_string(index=False))
    show = rep[(rep.kou_jing == "blend") & (rep.seg == "long")]
    print("\n④ blend long-flat 四窗 Sharpe:")
    print(show.pivot_table(index="window", columns="signal", values="sharpe",
                           sort=False).round(3).to_string())
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
