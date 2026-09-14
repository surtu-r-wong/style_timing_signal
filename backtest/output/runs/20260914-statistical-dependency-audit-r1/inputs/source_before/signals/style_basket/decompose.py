"""B2 分解分析：指数对信号 = 纯风格 × α + 行业轮动 × β（设计稿 §2.4 v2）。

输入：v1 混合篮子（spread_<U>.csv）+ v2 行业中性篮子（spread_<U>_neutral.csv，
同一 scores、仅选样差异）。行业轮动分量 := v1.spread − v2.spread（纯行业配置贡献）。

三个层面：
  ① 方差分解：各指数对日收益差 OLS ~ [纯风格, 行业轮动]，报 β/R²（含单变量）；
  ② 信号化：三分量各走 equal_weight 生产管线（20d40z + sm5）→ 三条候选信号；
  ③ 预测 IC：信号(t) vs 未来收益——月频非重叠（主判据，月末信号 vs 次月价差收益）
     + 日频 k∈{5,10,20}（参考）；Pearson/Spearman 双报。
判据（设计稿）：纯风格分量 IC 显著优于混合体 → 考虑切主；行业轮动分量有独立
预测力 → 第二个新信号。
产出 output/style_basket/decomposition_<U>.csv + 控制台摘要。
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from signals.common.config import load_db_config  # noqa: E402
from signals.equal_weight.generate_signal import _compute_pair_signal  # noqa: E402
from signals.style_basket.build import OUT_DIR  # noqa: E402
from signals.style_basket.validate import INDEX_PAIRS, _fetch_index_closes  # noqa: E402


def _load_spread(uni: str, suffix: str = "") -> pd.DataFrame:
    return pd.read_csv(
        OUT_DIR / f"spread_{uni}{suffix}.csv", parse_dates=["date"]
    ).set_index("date")


def _signal(left: pd.Series, right: pd.Series) -> pd.Series:
    """equal_weight 生产管线口径：lookback20/z40 tanh + rolling5 平滑。"""
    return _compute_pair_signal(left, right, lookback=20, z_window=40).rolling(
        5, min_periods=1
    ).mean()


def _ols_r2(y: np.ndarray, x_cols: list[np.ndarray]) -> tuple[list[float], float]:
    """OLS（含截距）→ (各列系数, R²)。"""
    x = np.column_stack([np.ones(len(y))] + x_cols)
    beta, *_ = np.linalg.lstsq(x, y, rcond=None)
    resid = y - x @ beta
    r2 = 1.0 - resid.var() / y.var()
    return list(beta[1:]), float(r2)


def decompose_universe(uni: str, db=None) -> pd.DataFrame:
    db = db or load_db_config()
    v1 = _load_spread(uni)
    v2 = _load_spread(uni, "_neutral")
    joint = pd.concat(
        [v1["spread"].rename("v1"), v2["spread"].rename("v2")], axis=1, join="inner"
    ).dropna()
    joint["rotation"] = joint["v1"] - joint["v2"]

    codes = [c for pair in INDEX_PAIRS.values() for c in pair]
    idx_close = _fetch_index_closes(db, codes)
    pair_rets = {}
    for name, (g, v) in INDEX_PAIRS.items():
        if g in idx_close.columns and v in idx_close.columns:
            pair_rets[name] = (
                idx_close[g].pct_change(fill_method=None)
                - idx_close[v].pct_change(fill_method=None)
            )
    pair_rets["blend"] = pd.concat(pair_rets.values(), axis=1).mean(axis=1)

    rows = []
    # ① 方差分解（日收益层）
    for name, pr in pair_rets.items():
        m = pd.concat([pr.rename("y"), joint[["v2", "rotation"]]], axis=1).dropna()
        if len(m) < 250:
            continue
        betas, r2_full = _ols_r2(m["y"].to_numpy(), [m["v2"].to_numpy(), m["rotation"].to_numpy()])
        _, r2_style = _ols_r2(m["y"].to_numpy(), [m["v2"].to_numpy()])
        _, r2_rot = _ols_r2(m["y"].to_numpy(), [m["rotation"].to_numpy()])
        rows.append(
            {"universe": uni, "block": "variance", "target": name,
             "beta_style": betas[0], "beta_rotation": betas[1],
             "r2_full": r2_full, "r2_style_only": r2_style, "r2_rotation_only": r2_rot,
             "n": len(m)}
        )

    # ② 三分量信号化
    ones = pd.Series(1.0, index=joint.index)
    rot_nav = (1.0 + joint["rotation"]).cumprod()
    signals = {
        "v1_mixed": _signal(v1["growth_index"], v1["value_index"]),
        "v2_pure_style": _signal(v2["growth_index"], v2["value_index"]),
        "rotation": _signal(rot_nav, ones),
    }

    # ③ 预测 IC：月频非重叠（主）+ 日频 k（参考），目标=blend 指数对价差
    for sig_name, sig in signals.items():
        for target in ["blend", "500pair", "1000pair"]:
            pr = pair_rets[target]
            base = pd.concat([sig.rename("sig"), pr.rename("ret")], axis=1).dropna()
            # 月频：月末信号 vs 次月收益和
            month_sig = base["sig"].resample("ME").last()
            month_ret = base["ret"].resample("ME").sum()
            fwd = month_ret.shift(-1)
            mj = pd.concat([month_sig, fwd], axis=1).dropna()
            ic_m_p = mj.iloc[:, 0].corr(mj.iloc[:, 1])
            ic_m_s = mj.iloc[:, 0].corr(mj.iloc[:, 1], method="spearman")
            row = {"universe": uni, "block": "ic", "signal": sig_name, "target": target,
                   "ic_monthly_pearson": ic_m_p, "ic_monthly_spearman": ic_m_s,
                   "n_months": len(mj)}
            # 日频 k 参考
            for k in (5, 10, 20):
                fwd_k = base["ret"].rolling(k).sum().shift(-k)
                dj = pd.concat([base["sig"], fwd_k], axis=1).dropna()
                row[f"ic_d{k}_spearman"] = dj.iloc[:, 0].corr(dj.iloc[:, 1], method="spearman")
            rows.append(row)
    return pd.DataFrame(rows)


def main() -> int:
    parser = argparse.ArgumentParser(description="B2 纯风格/行业轮动分解")
    parser.add_argument("--universes", default="U2,U0")
    args = parser.parse_args()
    db = load_db_config()
    for uni in args.universes.split(","):
        uni = uni.strip()
        if not (OUT_DIR / f"spread_{uni}_neutral.csv").exists():
            print(f"[decompose] {uni}: 缺 neutral 产出，跳过")
            continue
        got = decompose_universe(uni, db=db)
        out = OUT_DIR / f"decomposition_{uni}.csv"
        got.to_csv(out, index=False)
        var_block = got[got["block"] == "variance"]
        ic_block = got[got["block"] == "ic"]
        print(f"\n=== {uni} · 方差分解（指数对日收益 ~ 纯风格 + 行业轮动） ===")
        print(var_block.drop(columns=["block", "signal"] if "signal" in var_block else ["block"],
                             errors="ignore").to_string(index=False, float_format=lambda x: f"{x:.3f}"))
        print(f"\n=== {uni} · 预测 IC（月频非重叠为主判据） ===")
        cols = ["signal", "target", "ic_monthly_pearson", "ic_monthly_spearman",
                "ic_d5_spearman", "ic_d10_spearman", "ic_d20_spearman", "n_months"]
        print(ic_block[cols].to_string(index=False, float_format=lambda x: f"{x:.3f}"))
        print(f"[decompose] -> {out.name}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
