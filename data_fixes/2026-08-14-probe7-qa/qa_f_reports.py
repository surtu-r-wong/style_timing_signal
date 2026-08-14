"""QA F：报告列抽查 —— 集中度摘要（两族赢家）独立重算 + ⑦B×⑦A 相关独立重算。"""
import sys
from pathlib import Path

import numpy as np
import pandas as pd
from scipy.stats import spearmanr

ROOT = Path("/home/elfbob/claude-code/style_timing_signal")
sys.path.insert(0, str(ROOT))
from backtest.data import load_carry, load_db_config  # noqa: E402

QA = Path(__file__).resolve().parent
sys.path.insert(0, str(QA))
from qa_b1_b_family import fetch_closes, my_rolling_beta, my_rolling_pct  # noqa: E402
from qa_b3_pit_and_a_signals import my_asof_ladder  # noqa: E402


def my_strategy_ret(sig_centered, direction, k, und, carry, cost_bps=3.0, ann=245):
    idx = sig_centered.index
    pos = pd.Series(np.nan, index=idx)
    pts = np.arange(0, len(idx), k)
    sv = (sig_centered * direction).to_numpy(dtype=float)
    pos.iloc[pts] = np.sign(sv[pts])
    pos = pos.ffill().fillna(0.0)
    pos_eff = pos.shift(1).fillna(0.0)
    trade = pos_eff.diff().abs()
    trade.iloc[0] = abs(pos_eff.iloc[0])
    ret = (pos_eff * und.reindex(idx) - cost_bps / 1e4 * trade
           + pos_eff * carry.reindex(idx).fillna(0.0) / ann)
    return ret.dropna()


def my_conc(ret, ann=245):
    def sh(r):
        sd = r.std(ddof=1)
        return float(r.mean() / sd * np.sqrt(ann)) if sd > 0 else 0.0
    logc = np.log1p(ret).groupby(ret.index.year).sum()
    order = logc.sort_values(ascending=False).index.tolist()
    years = pd.Series(ret.index.year, index=ret.index)
    roll = (ret.rolling(735).mean() / ret.rolling(735).std(ddof=1) * np.sqrt(ann)).dropna()
    return {"sharpe_full": sh(ret), "sharpe_ex_top1": sh(ret[~years.isin(order[:1])]),
            "sharpe_ex_top2": sh(ret[~years.isin(order[:2])]),
            "ex_top1_year": order[0], "ex_top2_years": sorted(order[:2]),
            "roll3y_min": float(roll.min()), "roll3y_median": float(roll.median()),
            "roll3y_neg_share": float((roll < 0).mean())}


def main():
    db = load_db_config()
    closes = fetch_closes(db)
    r500 = closes["000905.SH"].dropna().pct_change().dropna()
    r1000 = closes["000852.SH"].dropna().pct_change().dropna()
    both = pd.concat([r500, r1000], axis=1).dropna()
    und = (both.iloc[:, 0] + both.iloc[:, 1]) / 2.0
    carry = load_carry("blend", db=db)
    ew = pd.read_csv(ROOT / "output/equal_weight/equal_weight_signal_20d40z.csv",
                     parse_dates=["date"]).set_index("date")["factor_value"]
    q = pd.read_csv(ROOT / "backtest/output/fund_crowding_quarterly.csv",
                    parse_dates=["report_date", "a1_eff_date", "a2_eff_date"]
                    ).set_index("report_date")

    # --- B 赢家（size zw500 k40）与 4 条 B 基础信号 ---
    j = closes[["885000.WI", "000300.SH", "000852.SH"]].dropna()
    rets = j.pct_change().dropna()
    bs_size, _ = my_rolling_beta(rets["885000.WI"],
                                 (rets["000300.SH"] + rets["000852.SH"]) / 2,
                                 rets["000852.SH"] - rets["000300.SH"], 240)
    jg = closes[["885000.WI", "399370.SZ", "399371.SZ"]].dropna()
    rg = jg.pct_change().dropna()
    bs_gv, _ = my_rolling_beta(rg["885000.WI"], (rg["399370.SZ"] + rg["399371.SZ"]) / 2,
                               rg["399370.SZ"] - rg["399371.SZ"], 240)
    sigs = {("size", 250): my_rolling_pct(bs_size.dropna(), 250),
            ("size", 500): my_rolling_pct(bs_size.dropna(), 500),
            ("gv", 250): my_rolling_pct(bs_gv.dropna(), 250),
            ("gv", 500): my_rolling_pct(bs_gv.dropna(), 500)}
    common = None
    for s in sigs.values():
        idx = s.dropna().index
        common = idx if common is None else common.intersection(idx)
    common = common.intersection(und.index).intersection(ew.dropna().index).sort_values()
    sel_b = common[(common >= "2014-01-01") & (common <= "2023-12-31")]

    csig = (sigs[("size", 500)] - 0.5).reindex(sel_b)
    ret_b = my_strategy_ret(csig, -1.0, 40, und, carry)
    ref_b = {"sharpe_full": 0.43544902360754806, "sharpe_ex_top1": 0.07481588265274557,
             "sharpe_ex_top2": -0.1303998788375456, "ex_top1_year": 2019,
             "roll3y_min": 0.18917774615373312, "roll3y_median": 0.6524223966013778,
             "roll3y_neg_share": 0.0}
    mine = my_conc(ret_b)
    print("=== ⑦B 赢家 concentration（独立重算 vs 产物）===")
    for k2, rv in ref_b.items():
        mv = mine[k2]
        d = abs(mv - rv) if isinstance(rv, float) else int(mv != rv)
        print(f"  {k2}: mine={mv} theirs={rv} diff={d:.2e}" if isinstance(rv, float)
              else f"  {k2}: {mv} vs {rv}")

    # --- A 赢家（A2 k40）---
    a2, _ = my_asof_ladder(q["a2_pct"], q["a2_eff_date"], und.index)
    a1, _ = my_asof_ladder(q["a1_pct"], q["a1_eff_date"], und.index)
    common_a = (a1.dropna().index.intersection(a2.dropna().index)
                .intersection(und.index).intersection(ew.dropna().index).sort_values())
    sel_a = common_a[(common_a >= "2014-01-01") & (common_a <= "2023-12-31")]
    ret_a = my_strategy_ret((a2 - 0.5).reindex(sel_a), -1.0, 40, und, carry)
    ref_a = {"sharpe_full": 0.15381327031879494, "sharpe_ex_top1": -0.4764853011002383,
             "sharpe_ex_top2": -0.8527666289670975, "ex_top1_year": 2019,
             "roll3y_min": -0.5420759359155835, "roll3y_median": -0.065653260566222,
             "roll3y_neg_share": 0.5683563748079877}
    mine = my_conc(ret_a)
    print("\n=== ⑦A 赢家 concentration（独立重算 vs 产物）===")
    for k2, rv in ref_a.items():
        mv = mine[k2]
        print(f"  {k2}: mine={mv} theirs={rv} diff={abs(mv-rv):.2e}"
              if isinstance(rv, float) else f"  {k2}: {mv} vs {rv}")
    print(f"  ex_top2_years mine={mine['ex_top2_years']}（产物 2019,2022）")

    # --- corr(⑦B size, ⑦A)（限 ⑦A 选择样本 n=1385，独立重算）---
    print("\n=== corr 抽查（独立信号，⑦A 选择样本）===")
    ref_corr = {("zw250", "a2"): (0.512151, 0.531518), ("zw500", "a2"): (0.598182, 0.53157),
                ("zw250", "a1"): (-0.097336, -0.1725), ("zw500", "a1"): (0.076882, -0.072694)}
    for zw in (250, 500):
        for ser, lad in (("a1", a1), ("a2", a2)):
            x = sigs[("size", zw)].reindex(sel_a)
            y = lad.reindex(sel_a)
            m = x.notna() & y.notna()
            pe = float(np.corrcoef(x[m], y[m])[0, 1])
            sp = float(spearmanr(x[m], y[m])[0])
            rp, rs = ref_corr[(f"zw{zw}", ser)]
            print(f"  size_zw{zw} vs {ser}: pearson mine={pe:+.6f}/theirs={rp:+.6f} "
                  f"spearman mine={sp:+.6f}/theirs={rs:+.6f} n={int(m.sum())}")
    print("\n[DONE] qa_f")


if __name__ == "__main__":
    main()
