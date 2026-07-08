"""杠杆轴空头信号探针：三族因子 × 三关闸门（rotation probe 模板双侧化复用）。

设计：docs/plans/2026-07-08-leverage-probe-design.md。数据：
- stock_selector.edb_daily 两融 4 条（融资余额/买入额 沪+深，万元，2010-04 起）；
- stock_daily_price 全市场成交额（多源单位断层 → 逐行归一 + 锚点验证 + CSV 缓存；
  含北交所/1 只 ETF，体量 <2% 且做 z 后不敏感）。

族：L1 余额增速（series_signal 复用）/ L2 融资买入占成交比 / L3' 去杠杆天数
（余额/20 日平均成交额）。先验方向为负（干柴），但双侧检验、方向由数据裁决。
两融 T 日数据 T+1 早公布 → 所有信号 pit_lag 后移一格。任一族过闸 → 进
dual_legs_external_short 装配；全负 → 杠杆轴第四轴归档。

CLI: python3 -m backtest.leverage_probe [--families L1,L2,L3p] [--n-perm 1000]
产出: backtest/output/leverage_probe.csv（面板）+ leverage_probe_verdicts.csv。
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from backtest.rotation_probe import (  # noqa: E402
    HALVES,
    _load_ew_signal,
    _win,
    hold_position,
    nonoverlap_ic,
    partial_ic_with_pvalue,
    series_signal,
    shift_permutation_pvalue,
)
from signals.equal_weight.generate_signal import STD_FLOOR  # noqa: E402

_BAL = ("M0061606", "M0061610")  # 融资余额 沪/深（万元）
_BUY = ("M0061604", "M0061609")  # 融资买入额 沪/深（万元）

GRID_L1_LB = (5, 10, 20, 60)
GRID_L1_SM = (0, 3)
GRID_LEVEL = ((5, 60), (5, 250), (20, 60), (20, 250))
GRID_K = (5, 10, 20, 40)

# 全市场成交额锚点（元）：2015-06-18 杠杆牛顶部 / 2026-06-30 单源干净日，均已实测
ANCHORS = {"2015-06-18": 1.46e12, "2026-06-30": 3.25e12}
CACHE_PATH = ROOT / "backtest" / "output" / "market_turnover.csv"


# ---------------------------------------------------------------- 纯函数
def level_signal(level: pd.Series, lookback: int, z_window: int) -> pd.Series:
    """水平型序列信号化：rolling mean(lb) → z(zw) → tanh(z/2)。

    与生产管线 _compute_pair_signal 的 z→tanh 段同口径（min_periods=zw、
    STD_FLOOR 守卫、burn-in fillna(0)）；差异仅在输入是水平量的滚动均值
    而非收益累计——信息在水平极值（参与度/存量脆弱性），不在变化率。
    """
    sm = level.rolling(lookback, min_periods=1).mean()
    mu = sm.rolling(z_window, min_periods=z_window).mean()
    sd = sm.rolling(z_window, min_periods=z_window).std()
    sd = pd.Series(np.where(sd < STD_FLOOR, STD_FLOOR, sd), index=sd.index)
    z = ((sm - mu) / sd).replace([np.inf, -np.inf], np.nan).fillna(0.0)
    return pd.Series(np.tanh(z / 2.0), index=level.index)


def unit_scale(amount: pd.Series, volume: pd.Series, close: pd.Series) -> pd.Series:
    """逐行成交额单位判别：amount/(volume×close)≈0.1 →（千元/手）段 ×1000，≈1 → ×1。

    stock_daily_price 多源混单位且同日可混（设计 §3.2）；VWAP≈close 容差下
    阈值 0.3。volume/close 缺失或 0 → NaN（聚合剔除）。SQL CASE 的镜像。
    """
    ratio = (amount / (volume * close)).replace([np.inf, -np.inf], np.nan)
    out = pd.Series(np.where(ratio < 0.3, 1000.0, 1.0), index=amount.index)
    out[ratio.isna()] = np.nan
    return out


def pit_lag(sig: pd.Series) -> pd.Series:
    """两融 T 日数据 T+1 早公布 → 信号后移一格（t 值 = 原 t−1），首日剔除。"""
    return sig.shift(1).dropna()


def pick_representative(panel: pd.DataFrame,
                        halves: tuple[str, ...] = tuple(HALVES)) -> tuple[pd.Series, bool]:
    """族内代表选择（双侧化）：全窗与两半窗 IC 三者同号的候选中取 worst-half |IC| 最大。

    无同号候选 → (|IC| 最大行, False)——该族分窗稳健性由构造判负，行仅存档。
    """
    cols = [f"ic_{h}" for h in halves]
    p = panel.dropna(subset=["ic"] + cols)
    sgn = np.sign(p["ic"])
    consistent = sgn != 0
    for c in cols:
        consistent &= np.sign(p[c]) == sgn
    cand = p[consistent]
    if len(cand):
        worst = cand[cols].abs().min(axis=1)
        return cand.loc[worst.idxmax()], True
    return p.loc[p["ic"].abs().idxmax()], False


def validate_anchors(series: pd.Series, anchors: dict[str, float],
                     rel_tol: float = 0.15) -> None:
    """锚点断言：单位归一出错（千倍错位）在此爆炸，不让脏分母流入信号。"""
    for d, expected in anchors.items():
        ts = pd.Timestamp(d)
        if ts not in series.index:
            raise ValueError(f"锚点日 {d} 不在成交额序列中")
        got = float(series.loc[ts])
        if abs(got - expected) / expected > rel_tol:
            raise ValueError(f"锚点 {d}: 实得 {got:.3e}, 预期 ≈{expected:.3e}（超容差 {rel_tol}）")


def build_signals(bal: pd.Series, buy: pd.Series, amt: pd.Series
                  ) -> dict[str, dict[str, pd.Series]]:
    """三族因子装配（全部 PIT 后移）：L1 增速 / L2 占成交比 / L3' 去杠杆天数。"""
    out: dict[str, dict[str, pd.Series]] = {"L1": {}, "L2": {}, "L3p": {}}
    ret = bal.pct_change().dropna()
    for lb in GRID_L1_LB:
        for sm in GRID_L1_SM:
            out["L1"][f"L1_lb{lb}sm{sm}"] = pit_lag(series_signal(ret, lb, 2 * lb, sm))
    r2 = (buy * 1e4 / amt).dropna()          # 万元 → 元，同分母
    r3 = (bal * 1e4 / amt.rolling(20).mean()).dropna()
    for lb, zw in GRID_LEVEL:
        out["L2"][f"L2_lb{lb}zw{zw}"] = pit_lag(level_signal(r2, lb, zw))
        out["L3p"][f"L3p_lb{lb}zw{zw}"] = pit_lag(level_signal(r3, lb, zw))
    return out


# ---------------------------------------------------------------- 数据装载
def _load_margin(db=None) -> tuple[pd.Series, pd.Series]:
    from signals.common.config import load_db_config
    from backtest.data import _connect
    db = db or load_db_config()
    conn = _connect(db)
    try:
        with conn.cursor() as cur:
            cur.execute(
                f"""SELECT edb_code, trade_date, value FROM {db['schema']}.edb_daily
                    WHERE edb_code = ANY(%s)""",
                (list(_BAL + _BUY),),
            )
            rows = cur.fetchall()
    finally:
        conn.close()
    wide = (pd.DataFrame(rows, columns=["code", "date", "value"])
            .pivot(index="date", columns="code", values="value").astype(float))
    wide.index = pd.to_datetime(wide.index)
    wide = wide.sort_index()
    return wide[list(_BAL)].sum(axis=1), wide[list(_BUY)].sum(axis=1)


def build_market_turnover(db=None, force: bool = False) -> pd.Series:
    """全市场日成交额（元）：单扫 SQL 逐行 CASE 归一（unit_scale 镜像）+ 锚点验证 + 缓存。"""
    if CACHE_PATH.exists() and not force:
        s = pd.read_csv(CACHE_PATH, parse_dates=["date"]).set_index("date")["amt_yuan"]
        validate_anchors(s, ANCHORS)
        return s
    from signals.common.config import load_db_config
    from backtest.data import _connect
    db = db or load_db_config()
    conn = _connect(db)
    try:
        with conn.cursor() as cur:
            cur.execute(
                f"""SELECT trade_date,
                           SUM(CASE WHEN amount / (volume * close) < 0.3
                                    THEN amount * 1000 ELSE amount END) AS amt_yuan
                    FROM {db['schema']}.stock_daily_price
                    WHERE amount > 0 AND volume > 0 AND close > 0
                    GROUP BY trade_date ORDER BY trade_date"""
            )
            rows = cur.fetchall()
    finally:
        conn.close()
    s = pd.Series({pd.Timestamp(d): float(a) for d, a in rows},
                  name="amt_yuan").sort_index()
    validate_anchors(s, ANCHORS)
    CACHE_PATH.parent.mkdir(parents=True, exist_ok=True)
    s.rename_axis("date").reset_index().to_csv(CACHE_PATH, index=False)
    return s


# ---------------------------------------------------------------- 编排（逐族三关裁决）
def run_families_probe(sigs_all: dict[str, dict[str, pd.Series]],
                       families: tuple[str, ...], grid_k: tuple[int, ...],
                       n_perm: int = 1000, cost_bps: float = 3.0, db=None
                       ) -> tuple[pd.DataFrame, pd.DataFrame]:
    """通用逐族三关裁决（杠杆/温度计等任意轴共用）：IC 面板 + 同号代表 + 三关。"""
    from backtest.data import load_carry, load_underlying_returns
    from backtest.engine import run_strategy
    from backtest.metrics import ann_return, sharpe, turnover

    und = {kj: load_underlying_returns(kj, db=db) for kj in ["500", "1000", "blend"]}
    carry_blend = load_carry("blend", db=db)
    ew = _load_ew_signal()

    # ① IC 面板（全窗 + 两半窗 × 三口径）
    rows = []
    for fam in families:
        for form, sig in sigs_all[fam].items():
            for k in grid_k:
                for kj, u in und.items():
                    row = {"family": fam, "form": form, "k": k, "kou_jing": kj}
                    row["ic"], row["n_windows"] = nonoverlap_ic(sig, u, k)
                    for half, (a, b) in HALVES.items():
                        row[f"ic_{half}"] = nonoverlap_ic(
                            _win(sig, a, b), _win(u, a, b), k)[0]
                    rows.append(row)
    panel = pd.DataFrame(rows)

    # ② 逐族：同号代表 → 三关
    verdicts = []
    for fam in families:
        blend = panel[(panel["family"] == fam) & (panel["kou_jing"] == "blend")]
        best, sign_ok = pick_representative(blend)
        form, k = str(best["form"]), int(best["k"])
        sig = sigs_all[fam][form]
        direction = float(np.sign(best["ic"]))

        p_ic = shift_permutation_pvalue(sig, und["blend"], k, n_perm=n_perm)
        pic, p_pic = partial_ic_with_pvalue(sig, und["blend"], ew, k, n_perm=n_perm)
        gate1 = bool(np.isfinite(p_ic) and p_ic < 0.05
                     and np.isfinite(pic) and pic * direction > 0
                     and np.isfinite(p_pic) and p_pic < 0.05)
        gate2 = sign_ok  # 选择规则已强制全窗+两半窗同号
        pos = hold_position(sig * direction, k)
        strat = run_strategy(pos, und["blend"].reindex(pos.index), cost_bps, carry_blend)
        net = strat["ret"].dropna()
        net_sharpe = sharpe(net)
        gate3 = bool(net_sharpe > 0)

        verdicts.append({
            "family": fam, "best_form": form, "best_k": k, "direction": direction,
            "ic": float(best["ic"]), "ic_pvalue": p_ic,
            "partial_ic_vs_ew": pic, "partial_ic_pvalue": p_pic,
            **{f"ic_{h}": float(best[f"ic_{h}"]) for h in HALVES},
            "net_sharpe": net_sharpe, "net_ann": ann_return(net),
            "turnover": turnover(pos),
            "gate1_significant_and_independent": gate1,
            "gate2_stable_halves": gate2,
            "gate3_net_positive": gate3,
            "PASS": gate1 and gate2 and gate3,
        })
    return panel, pd.DataFrame(verdicts)


def run_probe(families: tuple[str, ...] = ("L1", "L2", "L3p"), n_perm: int = 1000,
              cost_bps: float = 3.0, db=None) -> tuple[pd.DataFrame, pd.DataFrame]:
    bal, buy = _load_margin(db)
    amt = build_market_turnover(db)
    sigs_all = build_signals(bal, buy, amt)
    return run_families_probe(sigs_all, families, GRID_K, n_perm, cost_bps, db)


def main() -> int:
    ap = argparse.ArgumentParser(description="杠杆轴空头信号探针（三族×三关闸门）")
    ap.add_argument("--families", default="L1,L2,L3p")
    ap.add_argument("--n-perm", type=int, default=1000)
    ap.add_argument("--cost-bps", type=float, default=3.0)
    ap.add_argument("--rebuild-turnover", action="store_true",
                    help="强制重建全市场成交额缓存")
    args = ap.parse_args()

    if args.rebuild_turnover:
        build_market_turnover(force=True)
    families = tuple(f.strip() for f in args.families.split(",") if f.strip())
    panel, verdicts = run_probe(families, args.n_perm, args.cost_bps)

    out_dir = ROOT / "backtest" / "output"
    out_dir.mkdir(parents=True, exist_ok=True)
    panel.to_csv(out_dir / "leverage_probe.csv", index=False)
    verdicts.to_csv(out_dir / "leverage_probe_verdicts.csv", index=False)

    show = panel[panel["kou_jing"] == "blend"].copy()
    for c in ["ic"] + [f"ic_{h}" for h in HALVES]:
        show[c] = show[c].round(3)
    print("=== IC 面板（blend 口径） ===")
    print(show.drop(columns=["kou_jing"]).to_string(index=False))
    print("\n=== 逐族三关裁决 ===")
    for _, v in verdicts.iterrows():
        print(f"\n-- {v['family']} --")
        for key, val in v.items():
            if key == "family":
                continue
            print(f"  {key}: {val:.4f}" if isinstance(val, float) else f"  {key}: {val}")
    n_pass = int(verdicts["PASS"].sum())
    print(f"\n{'★ PASS：' + str(n_pass) + ' 族过闸 → 进 dual_legs_external_short 装配'
          if n_pass else '✗ STOP：全族停线 → 杠杆轴第四轴归档'}")
    print(f"→ {out_dir / 'leverage_probe.csv'}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
