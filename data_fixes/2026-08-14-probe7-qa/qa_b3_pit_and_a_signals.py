"""QA B3/E：PIT 攻击 + ⑦A 日频信号与 6 点 IC / 关3 独立重算。

- 独立 as-of 阶梯：逐日取"生效日 <= t 的期中 report_date 最大者"（直接语义实现，
  与实现的后缀最小值过滤完全不同的算法路径）；逐日断言 active 期 eff <= t、
  任何期在自身 eff 前从不 active；2019Q4 从不 active；
- 与实现 step_to_daily 输出逐位对比（两实现互证）；
- 70.7% 披露率复核（2020-04-22 时 2019Q4 主动样本已披露占比）；
- ⑦B 截断复算：3 个抽查日，β_t 只用 <= t 的收盘、分位只用 <= t 的 β；
- ⑦A 6 点 signed IC（选择窗）+ 赢家 A2 k40 逐窗 + 关3 对账。
"""
import sys
from pathlib import Path

import numpy as np
import pandas as pd
from scipy.stats import spearmanr

ROOT = Path("/home/elfbob/claude-code/style_timing_signal")
sys.path.insert(0, str(ROOT))
from backtest.data import _connect, load_carry, load_db_config  # noqa: E402

QA = Path(__file__).resolve().parent
sys.path.insert(0, str(QA))
from qa_b1_b_family import (  # noqa: E402  自己的独立实现（qa_b1 已验证）
    fetch_closes, my_net_stats, my_nonoverlap_ic, my_rolling_beta, my_rolling_pct,
)


def my_asof_ladder(values, eff_dates, calendar):
    """直接语义：day t 的值 = {期 p: eff_p <= t} 中 report_date 最大者的值。
    返回 (日频值, 日频 active 期 report_date)。O(天数×期数) 的朴素实现。"""
    per = pd.DataFrame({"v": values.to_numpy(dtype=float),
                        "eff": pd.to_datetime(eff_dates.to_numpy())},
                       index=values.index)  # index = report_date 升序
    per = per[per["v"].notna()]
    vals = np.full(len(calendar), np.nan)
    active = np.full(len(calendar), np.datetime64("NaT"), dtype="datetime64[ns]")
    for i, t in enumerate(calendar):
        elig = per[per["eff"] <= t]
        if len(elig):
            p = elig.index.max()          # report_date 最大者
            vals[i] = elig.loc[p, "v"]
            active[i] = np.datetime64(p)
    return (pd.Series(vals, index=calendar),
            pd.Series(active, index=calendar))


def main():
    db = load_db_config()
    q = pd.read_csv(ROOT / "backtest/output/fund_crowding_quarterly.csv",
                    parse_dates=["report_date", "a1_eff_date", "a2_eff_date",
                                 "eff_plus30"]).set_index("report_date")

    closes = fetch_closes(db)
    r500 = closes["000905.SH"].dropna().pct_change().dropna()
    r1000 = closes["000852.SH"].dropna().pct_change().dropna()
    both = pd.concat([r500, r1000], axis=1).dropna()
    und = (both.iloc[:, 0] + both.iloc[:, 1]) / 2.0
    cal = und.index  # 探针用 und[blend].index 作日历

    # ---------- 独立 as-of 阶梯 + PIT 结构断言 ----------
    print("=== PIT 攻击（⑦A annP95 主口径）===")
    ladders = {}
    for ser, effcol in (("a1", "a1_eff_date"), ("a2", "a2_eff_date")):
        vals, active = my_asof_ladder(q[f"{ser}_pct"], q[effcol], cal)
        ladders[ser] = vals
        eff_map = q[effcol].to_dict()
        ok_days = 0
        for t, p in active.dropna().items():
            p = pd.Timestamp(p)
            assert eff_map[p] <= t, f"{ser} {t.date()} active 期 {p.date()} eff 在未来!"
            ok_days += 1
        first_valid = vals.dropna().index[0]
        never = set(q.index[q[f"{ser}_pct"].notna()]) - set(pd.to_datetime(active.dropna().unique()))
        print(f"  [{ser}] 逐日 {ok_days} 天 active 期 eff<=t 全过；首生效日 {first_valid.date()}"
              f"；从不显示的期: {[str(p.date()) for p in sorted(never)]}")
        assert str(first_valid.date()) == "2018-04-23"
    assert {str(p.date()) for p in
            (set(q.index[q['a2_pct'].notna()]) -
             set())} >= {"2019-12-31"}  # sanity
    # 2019Q4 从不 active（两序列同 eff 结构）
    for ser in ("a1", "a2"):
        vals, active = my_asof_ladder(q[f"{ser}_pct"], q[f"{ser}_eff_date"], cal)
        shown = set(pd.to_datetime(pd.Series(active.dropna().unique())))
        assert pd.Timestamp("2019-12-31") not in shown, "2019Q4 出现在日频序列!"
    print("  [偏离C] 2019Q4 台阶从不出现在日频序列 —— 证实")

    # 与实现 step_to_daily 互证（两条独立算法路径）
    from backtest.fund_crowding_probe import step_to_daily
    for ser, effcol in (("a1", "a1_eff_date"), ("a2", "a2_eff_date")):
        theirs = step_to_daily(q[f"{ser}_pct"], q[effcol], cal)
        mine = ladders[ser]
        same_nan = bool(np.array_equal(mine.isna().to_numpy(), theirs.isna().to_numpy()))
        d = float((mine - theirs).abs().max())
        print(f"  [互证] {ser}: NaN 掩码相同={same_nan}, max|diff|={d:.2e}")
        assert same_nan and d == 0.0

    # 70.7% 披露率（2020-04-22 时 2019Q4 主动样本）
    conn = _connect(db)
    with conn.cursor() as cur:
        cur.execute("SET statement_timeout='120s'")
        cur.execute(f"SELECT fund_code FROM {db['schema']}.fund_meta "
                    f"WHERE fund_type = ANY(%s)",
                    (["偏股混合型", "偏股混合型基金", "普通股票型", "普通股票型基金"],))
        active_codes = {r[0] for r in cur.fetchall()}
    conn.close()
    pos = pd.read_csv(ROOT / "backtest/output/fund_stock_position.csv",
                      parse_dates=["report_date", "ann_date"])
    s19q4 = pos[(pos["report_date"] == "2019-12-31") & pos["fund_code"].isin(active_codes)]
    frac = float((s19q4["ann_date"] <= "2020-04-22").mean())
    print(f"  [偏离C] 2019Q4 主动样本 n={len(s19q4)}，2020-04-22 已披露 {frac*100:.1f}%"
          f"（文档 70.7%）；至 2020-04-30 {float((s19q4['ann_date'] <= '2020-04-30').mean())*100:.1f}%")

    # 逐期 P95 覆盖率结构断言：eff 日当天 >= 95% 已披露（主口径设计保证）
    cover = []
    for p, g in pos[pos["fund_code"].isin(active_codes)].groupby("report_date"):
        eff = q.loc[p, "a1_eff_date"]
        cover.append(float((g["ann_date"] <= eff).mean()))
    print(f"  [P95 覆盖] 41 期 eff 日披露率 min={min(cover)*100:.2f}% "
          f"(>=95% 全过: {all(c >= 0.95 for c in cover)})")

    # ---------- ⑦B 截断复算（右端无未来数据）----------
    print("\n=== ⑦B 截断复算（3 抽查日）===")
    for t in ("2018-01-11", "2020-07-15", "2023-12-29"):
        t = pd.Timestamp(t)
        trunc = closes[closes.index <= t]           # 只给 <= t 的收盘
        j = trunc[["885000.WI", "000300.SH", "000852.SH"]].dropna()
        rets = j.pct_change().dropna()
        spread = rets["000852.SH"] - rets["000300.SH"]
        mkt = (rets["000300.SH"] + rets["000852.SH"]) / 2.0
        w = 240
        X = np.column_stack([np.ones(w), mkt.to_numpy()[-w:], spread.to_numpy()[-w:]])
        beta_t = np.linalg.lstsq(X, rets["885000.WI"].to_numpy()[-w:], rcond=None)[0][2]
        cache = pd.read_csv(ROOT / "backtest/output/fund_crowding_beta_daily.csv",
                            parse_dates=["date"]).set_index("date")
        d = abs(beta_t - float(cache.loc[t, "beta_s_size_w240"]))
        # 分位：只用 <= t 的 β
        btr = cache.loc[cache.index <= t, "beta_s_size_w240"].dropna().to_numpy()
        wz = btr[-500:]
        pct_t = (np.sum(wz < btr[-1]) + 0.5 * (np.sum(wz == btr[-1]) + 1)) / 500
        print(f"  {t.date()}: 截断 β diff={d:.2e}; 截断 zw500 分位={pct_t:.6f}")

    # ---------- ⑦A 6 点 IC + 赢家逐窗 + 关3 ----------
    print("\n=== ⑦A 6 点 signed IC 对账 ===")
    ew = pd.read_csv(ROOT / "output/equal_weight/equal_weight_signal_20d40z.csv",
                     parse_dates=["date"]).set_index("date")["factor_value"]
    common = ladders["a1"].dropna().index.intersection(ladders["a2"].dropna().index)
    common = common.intersection(und.index).intersection(ew.dropna().index).sort_values()
    sel = common[(common >= "2014-01-01") & (common <= "2023-12-31")]
    print(f"  选择样本 {sel[0].date()} ~ {sel[-1].date()}, n={len(sel)}")
    assert (str(sel[0].date()), str(sel[-1].date()), len(sel)) == \
        ("2018-04-23", "2023-12-29", 1385)
    panel = pd.read_csv(ROOT / "backtest/output/probe_7_fund_crowding_panel.csv")
    pa = panel[(panel.family_group == "A") & (panel.kou_jing == "blend")]
    und_sel = und.reindex(sel)
    worst = 0.0
    ics = {}
    for ser in ("A1", "A2"):
        for k in (20, 40, 60):
            ic, n = my_nonoverlap_ic(ladders[ser.lower()].reindex(sel), und_sel, k)
            row = pa[(pa.family == ser) & (pa.k == k)].iloc[0]
            diff = abs(ic - row.ic_selection_2014_2023)
            worst = max(worst, diff)
            ics[(ser, k)] = ic
            print(f"  {ser} k{k}: mine={ic:+.6f} theirs={row.ic_selection_2014_2023:+.6f} "
                  f"diff={diff:.2e} n={n}/{row.n_selection_2014_2023}")
    print(f"  max|diff| = {worst:.2e}")
    win = max(ics, key=lambda kk: abs(ics[kk]))
    print(f"  [赢家] {win} |IC|={abs(ics[win]):.10f}（产物 0.23326440234912882）")
    assert win == ("A2", 40)

    n2 = len(sel) // 2
    ref = {"half1": -0.18551735511819167, "half2": -0.35233942448257105,
           "holdout": 0.2816179718697737, "val": -0.4069792156591293,
           "train": -0.13129595307736341, "full": -0.0962613631422811}
    wsig = ladders["a2"]
    segs = {"half1": sel[:n2], "half2": sel[n2:],
            "train": common[(common >= "2014-01-01") & (common <= "2020-12-31")],
            "val": common[(common >= "2021-01-01") & (common <= "2023-12-31")],
            "holdout": common[(common >= "2024-01-01") & (common <= "2026-12-31")],
            "full": common}
    for wname, m in segs.items():
        ic, n = my_nonoverlap_ic(wsig.reindex(m), und.reindex(m), 40)
        print(f"  [{wname:8s}] mine={ic:+.10f} theirs={ref[wname]:+.10f} "
              f"diff={abs(ic - ref[wname]):.2e} n={n}")
    print(f"  half 切点: {sel[n2-1].date()} | {sel[n2].date()}（产物 2021-02-25 | 2021-02-26）")

    carry = load_carry("blend", db=db)
    csig = (wsig - 0.5).reindex(common).dropna()
    ssel = csig[(csig.index >= "2014-01-01") & (csig.index <= "2023-12-31")]
    stats = my_net_stats(ssel, -1.0, 40, und, carry)
    ref3 = {"net_sharpe": 0.15381327031879494, "net_ann": 0.032560772013261095,
            "maxdd": -0.36567389248817994, "turnover": 1.4151624548736461, "n_obs": 1385}
    print("\n=== ⑦A 关3 selection 窗对账 ===")
    for k2, v in stats.items():
        d = abs(v - ref3[k2]) if isinstance(v, float) else (v - ref3[k2])
        print(f"  {k2}: mine={v} theirs={ref3[k2]} diff={d:.2e}" if isinstance(v, float)
              else f"  {k2}: {v} vs {ref3[k2]}")
    # holdout 段仓位恒 -1 复核
    hsig = csig[(csig.index >= "2024-01-01")]
    pos_h = np.sign((hsig * -1.0).to_numpy())
    print(f"  holdout 段仓位取值集合: {sorted(set(pos_h.tolist()))}（文档：恒 -1）")
    print("\n[DONE] qa_b3")


if __name__ == "__main__":
    main()
