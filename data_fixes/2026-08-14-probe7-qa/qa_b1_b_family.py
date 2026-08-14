"""QA B1/B4/E：⑦B 族独立重算（不 import 探针模块的任何函数）。

自写：收盘价 SQL、收益率、逐窗 lstsq 滚动 β、平均秩滚动分位、非重叠 signed IC、
持仓/净值算术（镜像 engine 文档语义）。只在数据装载层复用 backtest.data 的
load_db_config/_connect/load_carry（QA 任务书明示允许）。

对账目标（产物值）：
- 共用选择样本 2018-01-11 ~ 2023-12-29, n=1450
- 赢家 size W240 zw500 k40 signed IC = -0.2027036537011014
- half1 = -0.17401960784313728 / half2 = -0.32658127818231547
- holdout = +0.002200221353402789 / full = -0.16716444995607624
- 关3 net_sharpe selection = 0.43544902360754806
"""
import json
import sys
from pathlib import Path

import numpy as np
import pandas as pd
from scipy.stats import spearmanr

ROOT = Path("/home/elfbob/claude-code/style_timing_signal")
sys.path.insert(0, str(ROOT))
from backtest.data import _connect, load_carry, load_db_config  # noqa: E402

OUT = Path(__file__).resolve().parent
LEGS = ["885000.WI", "000300.SH", "000852.SH", "399370.SZ", "399371.SZ", "000905.SH"]
FIRST_EXPECT = {"885000.WI": "2015-01-05", "000300.SH": "2014-01-02",
                "000852.SH": "2013-03-06", "399370.SZ": "2013-01-04",
                "399371.SZ": "2013-01-04"}


def fetch_closes(db):
    conn = _connect(db)
    try:
        with conn.cursor() as cur:
            cur.execute("SET statement_timeout='120s'")
            cur.execute(
                f"""SELECT index_code, trade_date, close FROM {db['schema']}.index_daily
                    WHERE index_code = ANY(%s) ORDER BY trade_date""", (LEGS,))
            rows = cur.fetchall()
    finally:
        conn.close()
    wide = (pd.DataFrame(rows, columns=["code", "date", "close"])
            .pivot(index="date", columns="code", values="close").astype(float))
    wide.index = pd.to_datetime(wide.index)
    return wide.sort_index()


def my_rolling_beta(y, m, s, w):
    """逐窗 numpy lstsq（与实现的滚动矩闭式解完全独立）。"""
    n = len(y)
    bs = np.full(n, np.nan)
    bm = np.full(n, np.nan)
    ya, ma, sa = y.to_numpy(), m.to_numpy(), s.to_numpy()
    for end in range(w - 1, n):
        sl = slice(end - w + 1, end + 1)
        X = np.column_stack([np.ones(w), ma[sl], sa[sl]])
        coef, _, rank, _ = np.linalg.lstsq(X, ya[sl], rcond=None)
        if rank == 3:
            bm[end], bs[end] = coef[1], coef[2]
    return pd.Series(bs, index=y.index), pd.Series(bm, index=y.index)


def my_rolling_pct(s, zw):
    """末值平均秩分位 (less + 0.5*(equal+1))/zw，窗口未满 NaN。逐窗循环独立实现。"""
    a = s.to_numpy(dtype=float)
    out = np.full(len(a), np.nan)
    for i in range(zw - 1, len(a)):
        w = a[i - zw + 1: i + 1]
        out[i] = (np.sum(w < a[i]) + 0.5 * (np.sum(w == a[i]) + 1)) / zw
    return pd.Series(out, index=s.index)


def my_nonoverlap_ic(sig, ret, k):
    """块末 t=k-1,2k-1,... 的信号 vs 其后 k 日收益和（位置 t+1..t+k），spearmanr。"""
    idx = sig.index.intersection(ret.index)
    s = sig.reindex(idx).to_numpy(dtype=float)
    r = ret.reindex(idx).to_numpy(dtype=float)
    cs = np.concatenate([[0.0], np.cumsum(r)])
    pts = np.arange(k - 1, len(idx), k)
    pts = pts[pts + k <= len(idx) - 1]
    fwd = cs[pts + 1 + k] - cs[pts + 1]          # sum r[t+1 .. t+k]
    sv = s[pts]
    ok = np.isfinite(sv) & np.isfinite(fwd)
    if ok.sum() < 3:
        return np.nan, int(ok.sum())
    ic, _ = spearmanr(sv[ok], fwd[ok])
    return float(ic), int(ok.sum())


def my_net_stats(sig_centered, direction, k, und, carry, cost_bps=3.0, ann=245):
    """镜像 hold_position + run_strategy 文档语义的独立算术。"""
    idx = sig_centered.index
    pos = pd.Series(np.nan, index=idx)
    pts = np.arange(0, len(idx), k)
    sv = (sig_centered * direction).to_numpy(dtype=float)
    pos.iloc[pts] = np.sign(sv[pts])
    pos = pos.ffill().fillna(0.0)
    u = und.reindex(idx).astype(float)
    pos_eff = pos.shift(1).fillna(0.0)
    gross = pos_eff * u
    trade = pos_eff.diff().abs()
    trade.iloc[0] = abs(pos_eff.iloc[0])
    cost = cost_bps / 1e4 * trade
    c = carry.reindex(idx).fillna(0.0)
    ret = gross - cost + pos_eff * c / ann
    ret = ret.dropna()
    sd = ret.std(ddof=1)
    sharpe = float(ret.mean() / sd * np.sqrt(ann)) if sd > 0 else 0.0
    cum = (1 + ret).cumprod()
    mdd = float((cum / cum.cummax() - 1).min())
    to = float(pos.diff().abs().sum() / len(pos) * ann)
    return {"net_sharpe": sharpe, "net_ann": float(ret.mean() * ann),
            "maxdd": mdd, "turnover": to, "n_obs": int(len(ret))}


def main():
    db = load_db_config()
    closes = fetch_closes(db)
    for code, first in FIRST_EXPECT.items():
        got = str(closes[code].dropna().index[0].date())
        assert got == first, f"{code} 首日 {got} != {first}"
    print("[OK] 五腿首日锚点与预登记 §A/E 一致")

    # --- 独立 blend und（000905 + 000852 等权）---
    r500 = closes["000905.SH"].dropna().pct_change().dropna()
    r1000 = closes["000852.SH"].dropna().pct_change().dropna()
    both = pd.concat([r500, r1000], axis=1).dropna()
    und = (both.iloc[:, 0] + both.iloc[:, 1]) / 2.0

    # --- 独立 β（size / gv，W=240）---
    betas = {}
    for dim, (lega, legb) in {"size": ("000300.SH", "000852.SH"),
                              "gv": ("399370.SZ", "399371.SZ")}.items():
        j = closes[["885000.WI", lega, legb]].dropna()
        rets = j.pct_change().dropna()
        spread = (rets[legb] - rets[lega]) if dim == "size" else (rets[lega] - rets[legb])
        mkt = (rets[lega] + rets[legb]) / 2.0
        bs, bm = my_rolling_beta(rets["885000.WI"], mkt, spread, 240)
        betas[dim] = bs
        print(f"[beta] {dim}: 首个非 NaN {bs.dropna().index[0].date()}, n={bs.notna().sum()}")

    # 与产物缓存对账
    cache = pd.read_csv(ROOT / "backtest/output/fund_crowding_beta_daily.csv",
                        parse_dates=["date"]).set_index("date")
    for dim in ("size", "gv"):
        mine = betas[dim].dropna()
        theirs = cache[f"beta_s_{dim}_w{240}"].reindex(mine.index)
        d = float((mine - theirs).abs().max())
        print(f"[对账] beta_s_{dim}_w240 vs 缓存 max|diff| = {d:.3e}")
        assert d < 1e-7, d

    # --- 独立信号（4 条基础序列）---
    sigs = {}
    for dim in ("size", "gv"):
        for zw in (250, 500):
            sigs[(dim, zw)] = my_rolling_pct(betas[dim].dropna(), zw)

    # --- 共用索引与选择样本 ---
    ew = pd.read_csv(ROOT / "output/equal_weight/equal_weight_signal_20d40z.csv",
                     parse_dates=["date"]).set_index("date")["factor_value"]
    common = None
    for s in sigs.values():
        idx = s.dropna().index
        common = idx if common is None else common.intersection(idx)
    common = common.intersection(und.index).intersection(ew.dropna().index).sort_values()
    sel = common[(common >= "2014-01-01") & (common <= "2023-12-31")]
    print(f"[样本] 选择样本 {sel[0].date()} ~ {sel[-1].date()}, n={len(sel)}")
    assert (str(sel[0].date()), str(sel[-1].date()), len(sel)) == \
        ("2018-01-11", "2023-12-29", 1450)

    # --- 16 点 signed IC（选择窗）---
    und_sel = und.reindex(sel)
    results = {}
    for (dim, zw), s in sigs.items():
        for k in (5, 10, 20, 40):
            ic, n = my_nonoverlap_ic(s.reindex(sel), und_sel, k)
            results[(dim, zw, k)] = (ic, n)
    panel = pd.read_csv(ROOT / "backtest/output/probe_7_fund_crowding_panel.csv")
    pb = panel[(panel.family_group == "B") & (panel.kou_jing == "blend")]
    print("\n=== 16 点 signed IC 对账（我 vs 产物 panel）===")
    worst = 0.0
    for (dim, zw, k), (ic, n) in sorted(results.items()):
        row = pb[(pb.family == dim) & (pb.zw == str(zw)) & (pb.k == k)].iloc[0]
        diff = abs(ic - row.ic_selection_2014_2023)
        worst = max(worst, diff)
        flag = "OK" if diff < 2e-6 else "!!"
        print(f"  {dim:4s} zw{zw} k{k:2d}: mine={ic:+.6f} theirs={row.ic_selection_2014_2023:+.6f} "
              f"diff={diff:.2e} n={n}/{row.n_selection_2014_2023} {flag}")
    print(f"max |diff| (panel 落盘 1e-6 精度) = {worst:.2e}")

    # 赢家判定（|IC| argmax）
    win = max(results, key=lambda kk: abs(results[kk][0]))
    print(f"\n[赢家] {win} |IC|={abs(results[win][0]):.10f}（产物 0.2027036537011014）")
    assert win == ("size", 500, 40)

    # --- 赢家逐窗（half/holdout/train/val/full）---
    wsig = sigs[("size", 500)]
    n2 = len(sel) // 2
    halves = {"half1": (sel[0], sel[n2 - 1]), "half2": (sel[n2], sel[-1])}
    print(f"[half 切点] half1 {halves['half1'][0].date()}~{halves['half1'][1].date()} / "
          f"half2 {halves['half2'][0].date()}~{halves['half2'][1].date()}")
    wins = {"selection": (None, None, sel), "train": ("2014-01-01", "2020-12-31", common),
            "val": ("2021-01-01", "2023-12-31", common),
            "holdout": ("2024-01-01", "2026-12-31", common), "full": (None, None, common)}
    ref = {"selection": -0.2027036537011014, "train": -0.17401960784313728,
           "val": -0.41421568627450983, "holdout": 0.002200221353402789,
           "full": -0.16716444995607624,
           "half1": -0.17401960784313728, "half2": -0.32658127818231547}
    got = {}
    for wname, (a, b, base) in wins.items():
        m = base
        if a is not None:
            m = m[(m >= a) & (m <= b)]
        ic, n = my_nonoverlap_ic(wsig.reindex(m), und.reindex(m), 40)
        got[wname] = ic
        print(f"  [{wname:9s}] mine={ic:+.10f} theirs={ref[wname]:+.10f} "
              f"diff={abs(ic-ref[wname]):.2e} n={n}")
    for hname, (a, b) in halves.items():
        m = sel[(sel >= a) & (sel <= b)]
        ic, n = my_nonoverlap_ic(wsig.reindex(m), und.reindex(m), 40)
        got[hname] = ic
        print(f"  [{hname:9s}] mine={ic:+.10f} theirs={ref[hname]:+.10f} "
              f"diff={abs(ic-ref[hname]):.2e} n={n}")

    # --- E: fwd 构造零泄漏断言（rolling.sum().shift(-k) ≡ 严格 t+1..t+k）---
    r = und_sel.to_numpy()
    fwd_impl = pd.Series(r, index=sel).rolling(40).sum().shift(-40).to_numpy()
    cs = np.concatenate([[0.0], np.cumsum(r)])
    pts = np.arange(39, len(sel), 40)
    ok_pts = pts[pts + 40 <= len(sel) - 1]
    fwd_mine = cs[ok_pts + 1 + 40] - cs[ok_pts + 1]
    d = float(np.nanmax(np.abs(fwd_impl[ok_pts] - fwd_mine)))
    print(f"\n[E] fwd 窗 = 严格 (t+1..t+k] 收益和：max|diff|={d:.3e}（0 泄漏当日/未来错位）")
    assert d < 1e-12
    tail = pts[pts + 40 > len(sel) - 1]
    assert np.all(~np.isfinite(fwd_impl[tail])), "越界块末应为 NaN 被丢弃"
    print(f"[E] 越界块末 {len(tail)} 个全 NaN（被 dropna 丢弃，无 holdout 泄漏进选择窗）")

    # --- 关3 净 Sharpe（独立算术 + 房规 carry 装载）---
    carry = load_carry("blend", db=db)
    csig = (wsig - 0.5).reindex(common).dropna()
    ssel = csig[(csig.index >= "2014-01-01") & (csig.index <= "2023-12-31")]
    stats = my_net_stats(ssel, -1.0, 40, und, carry)
    ref3 = {"net_sharpe": 0.43544902360754806, "net_ann": 0.09465643809988097,
            "maxdd": -0.37271514770791725, "turnover": 1.6896551724137931, "n_obs": 1450}
    print("\n=== 关3 selection 窗净表现对账 ===")
    for k2, v in stats.items():
        print(f"  {k2}: mine={v} theirs={ref3[k2]} diff={abs(v - ref3[k2]):.2e}"
              if isinstance(v, float) else f"  {k2}: {v} vs {ref3[k2]}")

    json.dump({"winner_ic_by_window_mine": got, "gate3_mine": stats},
              open(OUT / "qa_b1_out.json", "w"), indent=2, default=str)
    print("\n[DONE] qa_b1")


if __name__ == "__main__":
    main()
