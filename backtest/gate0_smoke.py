"""Gate 0 跑前单期人工核对（r3 预登记 §8-④）—— 不算 ρ、不碰 932407/932406 官方序列。

四件事：
  1. `_space_frame` 后缀构成 + `.HK` 护栏影响量化（若不剔会挤进前 1500/3500 的席位数
     —— 决定 2000 带锚值是否受修复影响，独立于 ρ 的证据）；
  2. 1000 带官方化模拟单期 vs 000852 真值重合率（prev=None 与 prev=真值两版）；
  3. 尾部桶单期组成（只数 / .BJ / 无 .HK / 无巨头 / 市值形状）；
  4. 因子面板非空率 + 腿只数（1000 带与尾部各一期，不看收益）。

输出：stdout + `backtest/output/gate0_smoke.json`。
"""
import json
import sys
import time
from pathlib import Path

import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from backtest.pure_style_builder import (  # noqa: E402
    BAND_1000,
    _conn,
    _linked_members,
    _space_frame,
    factor_panel,
    official_sample_space,
    review_cutoff,
    sample_space,
    select_legs,
    style_probabilities,
    style_scores,
    tail_sample_space,
)

OUT = ROOT / "backtest" / "output" / "gate0_smoke.json"
report: dict = {}
t0 = time.time()


def hk_pressure(eff: pd.Timestamp, g: pd.DataFrame) -> dict:
    """若不剔 .HK，窗内可定价的 .HK 有多少只、会挤进前 1500/3500 几席。"""
    cutoff = review_cutoff(eff)
    d = cutoff.date().isoformat()
    c, s = _conn()
    try:
        with c.cursor() as cur:
            cur.execute(f"""SELECT ts_code, trade_date, close::float8, amount::float8
                            FROM {s}.stock_daily_price
                            WHERE ts_code LIKE '%%.HK'
                              AND trade_date > DATE '{d}' - INTERVAL '365 days'
                              AND trade_date <= DATE '{d}'
                              AND close IS NOT NULL AND pre_close>0 AND volume>0""")
            px = pd.DataFrame(cur.fetchall(), columns=["ts_code", "trade_date", "close", "amount"])
            cur.execute(f"""SELECT ts_code, effective_date, total_shares::float8
                            FROM {s}.stock_share_capital
                            WHERE ts_code LIKE '%%.HK' AND effective_date <= DATE '{d}'
                              AND total_shares IS NOT NULL""")
            sh = pd.DataFrame(cur.fetchall(), columns=["ts_code", "effective_date", "total_shares"])
    finally:
        c.close()
    if px.empty or sh.empty:
        return {"n_hk_priced": 0, "into_top1500": 0, "into_top3500": 0}
    px["trade_date"] = pd.to_datetime(px["trade_date"])
    sh["effective_date"] = pd.to_datetime(sh["effective_date"])
    m = pd.merge_asof(px.sort_values("trade_date"), sh.sort_values("effective_date"),
                      left_on="trade_date", right_on="effective_date",
                      by="ts_code", direction="backward").dropna(subset=["total_shares"])
    m["mv"] = m["close"] * m["total_shares"]
    hk_mv = m.groupby("ts_code")["mv"].mean().sort_values(ascending=False)
    ranked = g.sort_values("avg_mv", ascending=False)["avg_mv"]
    thr1500 = ranked.iloc[1499] if len(ranked) >= 1500 else ranked.iloc[-1]
    thr3500 = ranked.iloc[3499] if len(ranked) >= 3500 else ranked.iloc[-1]
    return {"n_hk_priced": int(len(hk_mv)),
            "into_top1500": int((hk_mv > thr1500).sum()),
            "into_top3500": int((hk_mv > thr3500).sum()),
            "hk_top3": [(k, round(v / 1e8, 1)) for k, v in hk_mv.head(3).items()]}


def truth_after(index_code: str, eff: pd.Timestamp) -> set[str] | None:
    return _linked_members(index_code, eff, review_cutoff(eff), verbose=True)


for eff_s in ("2024-06-17", "2026-06-15"):
    eff = pd.Timestamp(eff_s)
    print(f"\n{'=' * 62}\n单期核对 @生效日 {eff_s}（cutoff={review_cutoff(eff).date()}）", flush=True)
    g = _space_frame(eff)
    sfx = pd.Series(g.index.str[-3:]).value_counts().to_dict()
    hk = hk_pressure(eff, g)
    print(f"  帧 {len(g)} 只，后缀 {sfx}；.HK 护栏影响：可定价 {hk['n_hk_priced']} 只，"
          f"若不剔挤进前1500 {hk['into_top1500']} 席 / 前3500 {hk['into_top3500']} 席", flush=True)

    # ---- 1000 带官方化模拟 vs 000852 真值
    m852 = truth_after("000852.SH", eff)
    sim_a = official_sample_space(eff, prev=None, verbose=True, band=BAND_1000, frame=g)
    sim_b = official_sample_space(eff, prev=m852, verbose=True, band=BAND_1000, frame=g)
    row = {"frame_n": len(g), "suffix": sfx, "hk_pressure": hk}
    if m852:
        ov_a = len(set(sim_a) & m852) / len(m852)
        ov_b = len(set(sim_b) & m852) / len(m852)
        print(f"  1000带模拟 vs 000852 真值({len(m852)}只)："
              f"prev=None 重合 {ov_a:.1%} / prev=真值 重合 {ov_b:.1%}", flush=True)
        row["ov_1000_prev_none"] = round(ov_a, 4)
        row["ov_1000_prev_truth"] = round(ov_b, 4)

    # ---- 尾部桶组成
    tail, m2000 = tail_sample_space(eff, verbose=True, frame=g)
    tail_g = g.reindex(tail)
    n_bj = sum(c.endswith(".BJ") for c in tail)
    n_hk = sum(c.endswith(".HK") for c in tail)
    top8 = tail_g.sort_values("avg_mv", ascending=False).head(8)
    print(f"  尾部 {len(tail)} 只（.BJ {n_bj} / .HK {n_hk}），"
          f"日均市值中位 {tail_g['avg_mv'].median() / 1e8:.1f} 亿、"
          f"最大 {tail_g['avg_mv'].max() / 1e8:.1f} 亿", flush=True)
    print(f"  尾部市值前8：{[(c, round(v / 1e8, 1)) for c, v in top8['avg_mv'].items()]}", flush=True)
    row.update({"tail_n": len(tail), "tail_bj": n_bj, "tail_hk": n_hk,
                "tail_median_avg_mv_yi": round(float(tail_g["avg_mv"].median()) / 1e8, 2),
                "tail_max_avg_mv_yi": round(float(tail_g["avg_mv"].max()) / 1e8, 2)})
    report[eff_s] = row

# ---- 因子面板非空率 + 腿只数（各一期，取 2026-06-15）
eff = pd.Timestamp("2026-06-15")
cutoff = review_cutoff(eff)
for label, picked in (
    ("band1000", official_sample_space(eff, prev=truth_after("000852.SH", eff), band=BAND_1000)),
    ("tail", tail_sample_space(eff)[0]),
):
    sp = sample_space(cutoff, None, None, codes=picked, apply_filters=False)
    panel = factor_panel(sp, cutoff)
    nn = (panel.notna().mean() * 100).round(1).to_dict()
    prob = style_probabilities(style_scores(panel))
    wg, wv = select_legs(prob, take_top_half=False)
    print(f"\n  [{label}] 名单 {len(picked)} → 快照对齐 {len(sp)}；因子非空率% {nn}", flush=True)
    print(f"  [{label}] 腿只数：成长 {len(wg)} / 价值 {len(wv)}", flush=True)
    report[f"panel_{label}"] = {"picked": len(picked), "aligned": len(sp),
                                "nonnull_pct": nn, "n_growth": len(wg), "n_value": len(wv)}

report["elapsed_s"] = round(time.time() - t0, 1)
OUT.write_text(json.dumps(report, ensure_ascii=False, indent=1))
print(f"\n落盘 {OUT}（{report['elapsed_s']}s）", flush=True)
