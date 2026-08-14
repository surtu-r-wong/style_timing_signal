"""QA B2：⑦A 期级序列独立重算（从原始 SQL / 原始 CSV 行起算，自写归类与聚合）。

- A2：抽 3 期（2018-06-30 / 2020-12-31 / 2025-09-30）行级 fetch，pandas 独立归类
  （300/301+.SZ、688+.SH 进分子；.SH/.SZ 进分母；.HK/.BJ 剔除），对账缓存；
- A1：同 3 期从 fund_stock_position.csv 原始行 + DB fund_meta 主动过滤重算中位数；
- 生效日：两侧 P95（排序后 0-based ceil(0.95 n)-1）独立重算；
- 边界清点：B 股（900+.SH / 200+.SZ）在分母中的占比、689 前缀、.BJ/.HK 行数。
"""
import sys
from pathlib import Path

import numpy as np
import pandas as pd

ROOT = Path("/home/elfbob/claude-code/style_timing_signal")
sys.path.insert(0, str(ROOT))
from backtest.data import _connect, load_db_config  # noqa: E402

PERIODS = ["2018-06-30", "2020-12-31", "2025-09-30"]
ACTIVE = ["偏股混合型", "偏股混合型基金", "普通股票型", "普通股票型基金"]


def my_p95(dates):
    a = np.sort(pd.to_datetime(pd.Series(dates)).dropna().to_numpy())
    return pd.Timestamp(a[int(np.ceil(0.95 * len(a))) - 1])


def main():
    db = load_db_config()
    q = pd.read_csv(ROOT / "backtest/output/fund_crowding_quarterly.csv",
                    parse_dates=["report_date", "a1_eff_date", "a2_eff_date"]
                    ).set_index("report_date")

    conn = _connect(db)
    with conn.cursor() as cur:
        cur.execute("SET statement_timeout='120s'")
        cur.execute(f"SELECT fund_code FROM {db['schema']}.fund_meta "
                    f"WHERE fund_type = ANY(%s)", (ACTIVE,))
        active = {r[0] for r in cur.fetchall()}
        cur.execute(f"SELECT fund_type, COUNT(*) FROM {db['schema']}.fund_meta "
                    f"GROUP BY fund_type ORDER BY 2 DESC")
        print("[fund_meta 全库]", cur.fetchall())
        print(f"[active codes] {len(active)}")

        for per in PERIODS:
            cur.execute(
                f"""SELECT h.fund_code, h.ts_code, h.hold_value, h.ann_date
                    FROM {db['schema']}.fund_stock_holdings h
                    JOIN {db['schema']}.fund_meta m USING (fund_code)
                    WHERE m.fund_type = ANY(%s) AND h.report_date = DATE %s""",
                (ACTIVE, per))
            rows = pd.DataFrame(cur.fetchall(),
                                columns=["fund_code", "ts_code", "hold_value", "ann_date"])
            code = rows["ts_code"].astype(str)
            hv = pd.to_numeric(rows["hold_value"], errors="coerce").astype(float)
            is_sh = code.str.endswith(".SH")
            is_sz = code.str.endswith(".SZ")
            in_den = is_sh | is_sz
            in_num = ((code.str.startswith(("300", "301")) & is_sz)
                      | (code.str.startswith("688") & is_sh))
            num, den = float(hv[in_num].sum()), float(hv[in_den].sum())
            ratio = num / den
            nf = rows["fund_code"].nunique()
            eff = my_p95(rows.drop_duplicates("fund_code")["ann_date"])
            ref = q.loc[pd.Timestamp(per)]
            print(f"\n=== A2 {per}（行数 {len(rows)}）===")
            print(f"  num  : mine={num:.4f} theirs={ref.a2_num_value:.4f} "
                  f"diff={abs(num - ref.a2_num_value):.2e}")
            print(f"  den  : mine={den:.4f} theirs={ref.a2_den_value:.4f} "
                  f"diff={abs(den - ref.a2_den_value):.2e}")
            print(f"  ratio: mine={ratio:.12f} theirs={ref.a2_ratio:.12f} "
                  f"diff={abs(ratio - ref.a2_ratio):.2e}")
            print(f"  n_funds: mine={nf} theirs={ref.a2_n_funds}")
            print(f"  a2_eff(P95): mine={eff.date()} theirs={ref.a2_eff_date.date()}")
            # 边界清点
            hk = int(code.str.endswith(".HK").sum())
            bj = int(code.str.endswith(".BJ").sum())
            b_share = hv[(code.str.startswith("900") & is_sh)
                         | (code.str.startswith("200") & is_sz)].sum()
            p689 = int((code.str.startswith("689") & is_sh).sum())
            other_suffix = int((~in_den & ~code.str.endswith(".HK")
                                & ~code.str.endswith(".BJ")).sum())
            print(f"  [边界] .HK 行 {hk} / .BJ 行 {bj} / 其他后缀 {other_suffix} / "
                  f"689 前缀 {p689} / B股市值占分母 {b_share / den * 100:.4f}%")
    conn.close()

    # ---- A1（CSV 原始行 + 主动过滤）----
    pos = pd.read_csv(ROOT / "backtest/output/fund_stock_position.csv",
                      parse_dates=["report_date", "ann_date"])
    print(f"\n[position CSV] {len(pos)} 行, 期数 {pos['report_date'].nunique()}")
    for per in PERIODS:
        sub = pos[(pos["report_date"] == per) & (pos["fund_code"].isin(active))]
        med = float(sub["stock_to_nav_pct"].median())
        eff = my_p95(sub["ann_date"])
        ref = q.loc[pd.Timestamp(per)]
        print(f"=== A1 {per}: n={len(sub)}(theirs {ref.a1_n_funds}) "
              f"median mine={med:.6f} theirs={ref.a1_median:.6f} "
              f"diff={abs(med - ref.a1_median):.2e}; "
              f"eff mine={eff.date()} theirs={ref.a1_eff_date.date()}")
        n_gt100 = int((sub["stock_to_nav_pct"] > 100).sum())
        print(f"    >100 行数 {n_gt100}（保留原值口径）")

    # 全期对账（A1 中位数全 41 期，一次性）
    sub = pos[pos["fund_code"].isin(active)]
    med_all = sub.groupby("report_date")["stock_to_nav_pct"].median()
    d = (med_all - q["a1_median"]).abs().max()
    print(f"\n[全期] A1 中位数 41 期 max|diff| = {d:.2e}")
    n_all = sub.groupby("report_date")["fund_code"].size()
    print(f"[全期] a1_n_funds 全等: {bool((n_all == q['a1_n_funds']).all())}")
    eff_all = sub.groupby("report_date")["ann_date"].apply(my_p95)
    print(f"[全期] a1_eff_date 全等: {bool((eff_all == q['a1_eff_date']).all())}")
    # >100 与 <0 清点（§C 冻结数：>100 14 行 / <0 0 行）
    print(f"[全库] >100 共 {int((pos['stock_to_nav_pct'] > 100).sum())} 行 "
          f"(max {pos['stock_to_nav_pct'].max()}), <0 共 "
          f"{int((pos['stock_to_nav_pct'] < 0).sum())} 行")
    # 独立 expanding 分位（平均秩，burn-in 8）
    def exp_pct(v):
        out = np.full(len(v), np.nan)
        for i in range(8, len(v)):
            w = v[: i + 1]
            out[i] = (np.sum(w < v[i]) + 0.5 * (np.sum(w == v[i]) + 1)) / len(w)
        return out
    for col, pcol in (("a1_median", "a1_pct"), ("a2_ratio", "a2_pct")):
        mine = exp_pct(q[col].to_numpy(dtype=float))
        theirs = q[pcol].to_numpy(dtype=float)
        d = np.nanmax(np.abs(mine - theirs))
        assert np.array_equal(np.isnan(mine), np.isnan(theirs))
        print(f"[分位] {pcol} 独立重算 max|diff| = {d:.2e}（前 8 期 NaN 一致）")
    print("\n[DONE] qa_b2")


if __name__ == "__main__":
    main()
