"""容量自适应桶数 绩效对比（2026-08-20 用户直接指令跑，未走预登记流程——结果按
描述性对比报告，若正面须另行正式预登记确认）。

命题（用户提出）：较早年份只用 3 对、市场容量到位后逐步加到 4 对/5 对，
是否优于现役「四对等权全窗」。

变体（切换时点 = 当年可观测规则，非事后挑日）：
  A  现役：300/500/1000/2000 四对等权全窗（基线）
  D  三对全窗：永不加 2000 对（孤立 2000 对总贡献的参照）
  V1 早切：2022-06-13 起加 2000 对（规则=资格筛后全市场首次 ≥3800，
     即 2000 带名义空间放得下；取其后年中调样生效日）
  V2 晚切：2023-06-12 起加 2000 对（规则=2000 带待选首次满编 2000 只）
  V3 全递进：三对 → 2022-06-13 起加 2000 对 + 尾部对（官方链余集，
     tail_pair_daily.csv，2022-06-14 起物理存在）成五对等权

秤：backtest.baseline 同秤（离散仓位、3bp、carry；blend 主口径 + 500/1000；
四窗×三段）+ 配对 block bootstrap（paired_block_bootstrap_sharpe_diff，
n=10000, block=20）各候选 vs A，blend 的 long-flat（生产姿势）与对称 full 两段。
信号构造与生产一致：每对 _compute_pair_signal(20d/40z) + rolling5，在役对等权平均。

CLI: python3 -W ignore -m backtest.adaptive_bucket_compare
产出: backtest/output/adaptive_bucket_compare_{report,paired}.csv + 控制台。
"""
from __future__ import annotations

import sys
from pathlib import Path

import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from backtest.baseline import KOU_JING, build_report  # noqa: E402
from backtest.data import load_carry, load_underlying_returns  # noqa: E402
from backtest.engine import run_strategy  # noqa: E402
from backtest.paired_bootstrap import paired_block_bootstrap_sharpe_diff  # noqa: E402
from backtest.positions import to_position  # noqa: E402
from signals.common.config import load_db_config  # noqa: E402
from signals.equal_weight.generate_signal import _compute_pair_signal  # noqa: E402
from signals.style_basket.validate import INDEX_PAIRS, _fetch_index_closes  # noqa: E402

TAIL_FILE = ROOT / "backtest" / "output" / "tail_pair_daily.csv"
SWITCH_4 = {"V1": "2022-06-13", "V2": "2023-06-12", "V3": "2022-06-13"}
SWITCH_5 = {"V3": "2022-06-13"}
CORE3 = ["300pair", "500pair", "1000pair"]


def _pair_signal(g_nav: pd.Series, v_nav: pd.Series) -> pd.Series:
    return _compute_pair_signal(g_nav, v_nav, lookback=20, z_window=40).rolling(
        5, min_periods=1
    ).mean()


def build_factor_panel(db) -> pd.DataFrame:
    closes = _fetch_index_closes(db, [c for p in INDEX_PAIRS.values() for c in p])
    sig = {n: _pair_signal(closes[g], closes[v]) for n, (g, v) in INDEX_PAIRS.items()}
    tail = pd.read_csv(TAIL_FILE, index_col=0, parse_dates=True)
    sig["tailpair"] = _pair_signal(
        (1 + tail["growth"]).cumprod(), (1 + tail["value"]).cumprod()
    )
    return pd.DataFrame(sig)


def variant_factor(panel: pd.DataFrame, name: str) -> pd.Series:
    core = panel[CORE3].mean(axis=1)
    if name == "D3":
        out = core
    else:
        four = panel[CORE3 + ["2000pair"]].mean(axis=1)
        if name == "A4":
            out = four
        else:
            cut4 = pd.Timestamp(SWITCH_4[name])
            out = core.where(panel.index < cut4, four)
            if name in SWITCH_5:
                five = panel[CORE3 + ["2000pair", "tailpair"]].mean(axis=1)
                cut5 = pd.Timestamp(SWITCH_5[name])
                has5 = panel.index >= cut5
                out = out.where(~(has5 & panel["tailpair"].notna()),
                                five.fillna(out))
    return out.dropna()


def main() -> int:
    db = load_db_config()
    panel = build_factor_panel(db).dropna(subset=CORE3 + ["2000pair"])
    variants = ["A4", "D3", "V1", "V2", "V3"]
    positions = {v: to_position(variant_factor(panel, v), "discrete") for v in variants}
    sigs = {v: ("<inline>", "factor_value") for v in variants}

    rep = build_report(mode="discrete", bootstrap_n=0, cost_bps=3.0, db=db,
                       signals=sigs, positions=positions)
    out1 = ROOT / "backtest" / "output" / "adaptive_bucket_compare_report.csv"
    rep.to_csv(out1, index=False)

    # 配对检验：候选 vs A4，blend 口径，long-flat 与对称两段
    und = load_underlying_returns("blend", db=db)
    car = load_carry("blend", db=db)
    rows = []
    idx0 = positions["A4"].index
    for v in variants:
        idx0 = idx0.intersection(positions[v].index)

    def _ret(pos: pd.Series, seg: str) -> pd.Series:
        p = pos.reindex(idx0).astype(float)
        if seg == "long":
            p = p.clip(lower=0)
        i = p.index.intersection(und.index)
        return run_strategy(p.reindex(i), und.reindex(i), 3.0, car.reindex(i))["ret"]

    for seg in ["long", "full"]:
        base = _ret(positions["A4"], seg)
        for v in ["D3", "V1", "V2", "V3"]:
            r = paired_block_bootstrap_sharpe_diff(_ret(positions[v], seg), base,
                                                   block=20, n=10000, seed=0)
            rows.append({"variant": v, "seg": seg, **r})
    paired = pd.DataFrame(rows)
    out2 = ROOT / "backtest" / "output" / "adaptive_bucket_compare_paired.csv"
    paired.to_csv(out2, index=False)

    show = rep[(rep.kou_jing == "blend") & (rep.seg.isin(["long", "full"]))].copy()
    for c in ["ann", "maxdd"]:
        show[c] = (show[c] * 100).round(1)
    for c in ["sharpe", "turnover"]:
        show[c] = show[c].round(3)
    print(show.pivot_table(index=["seg", "window"], columns="signal",
                           values="sharpe", sort=False).round(3).to_string())
    print("\n配对检验（候选 − A4 现役，blend）:")
    cols = ["variant", "seg", "diff_sharpe", "ci_lo", "ci_hi", "p_value", "n_obs"]
    print(paired[cols].round(4).to_string(index=False))
    print(f"\n→ {out1}\n→ {out2}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
