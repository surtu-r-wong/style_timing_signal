"""官方指数停发时的自建腿灾备（failover）—— 构建 + 演练验收。

## 命题与既有证据

若 `index_daily` 的官方风格腿停发/长期不可用，生产信号（四对价差斜率等权平均）
就断料。**自建腿顶上在系统层面几乎无损**——这不是推测，是实测（复刻误差阶梯，
`docs/plans/2026-08-20-classifier-swap-argument.md` §7.10）：

| 层级 | 读数 |
|---|---|
| 单带价差 ρ | 1000 带 0.8957 / 2000 带 0.8710 / 300·500 带 0.957·0.964 |
| 单带信号仓位分歧 | 13.1% ~ 15.9% 天 |
| **装进四对系统后** | 最终信号 corr **0.984~0.985**、仓位分歧 **~5% 天** |
| **系统级绩效差** | **0 ± 0.05**（p 0.69~0.93，两方向都出现过） |

机制 = 多重平均把复刻误差也钝化掉了（这次是朋友）。对照：换方法论（7 月 B1）
绩效 −0.27~−0.36 —— **「换测量」代价≈0、「换定义」代价显著**，故灾备必须走
**官方级复刻管线**（`backtest.pure_style_builder`），不得临时换自有因子层。

## 本模块提供什么

1. `build_legs()` —— 用官方级复刻管线造指定带的成长/价值腿日收益（可指定窗口）。
2. `signal_from_legs()` —— 腿日收益 → 生产口径信号（价差 NAV 适配器 + 20d40z + sm5）。
3. `drill()` —— **演练验收**：自建腿信号 vs 官方腿信号的 corr / 仓位分歧 / 末日仓位，
   对照 §7.10 已登记容差判定 READY / DEGRADED。

CLI:
    python3 -m deploy.failover.failover_legs --bands 1000 --start 2024-01-01
        → deploy/failover/output/failover_drill_<bands>.json + _legs.csv

⚠️ 构建是**重活**（单带全窗 ~1 小时级，与 Gate 0 同量级）。真出事故时的正确姿势见
`deploy/failover/README.md`：先用本模块补齐断料期，再按周期增量续，不要每天全量重建。
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

import pandas as pd

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

OUTDIR = Path(__file__).resolve().parent / "output"

#: 生产四对（官方码）与其自建复刻的市值排名带。
#: 300/500 走官方成分真值路径；1000/2000 走官方化模拟选样（库内无全历史成分）。
BAND_SPEC = {
    "300": {"official": ("000918.CSI", "000919.CSI"), "mother": "000300.SH"},
    "500": {"official": ("H30351.CSI", "H30352.CSI"), "mother": "000905.SH"},
    "1000": {"official": ("932407.CSI", "932406.CSI"), "mother": "000852.SH"},
    "2000": {"official": ("932409.CSI", "932408.CSI"), "mother": "932000.CSI"},
}

#: §7.10 已登记的系统级容差（四对齐备时）。单带演练不套用系统级判据 —— 单带
#: 分歧 13~16% 天是**已知且可接受**的，因为四对平均会把它压到 ~5%。
READY_SIGNAL_CORR = 0.85        # 单带信号 corr 下限（§7.10 实测 0.886~0.892）
READY_POSITION_DIFF = 0.20      # 单带仓位分歧上限（§7.10 实测 13.1%~15.9%）


# ─────────────────────────── 纯函数（可单测，不连库） ───────────────────────────
def nav_from_returns(returns: pd.Series) -> pd.Series:
    """日收益 → 净值（起点 1.0）。空序列安全。"""
    r = returns.dropna()
    return (1.0 + r).cumprod() if len(r) else pd.Series(dtype=float)


def signal_from_legs(growth: pd.Series, value: pd.Series) -> pd.Series:
    """腿日收益 → 生产口径信号（与 `decompose._signal` 同一函数，不另造轮子）。"""
    from signals.style_basket.decompose import _signal
    return _signal(nav_from_returns(growth), nav_from_returns(value))


def position_of(signal: pd.Series, theta: float = 0.0) -> pd.Series:
    """生产部署口径 long-flat：signal > θ → 1，否则 0。"""
    return (signal > theta).astype(int)


def compare_signals(mine: pd.Series, official: pd.Series) -> dict:
    """自建 vs 官方：公共窗上的信号 corr、仓位分歧比例、末日仓位。"""
    j = pd.concat([mine.rename("mine"), official.rename("official")], axis=1).dropna()
    if len(j) < 30:
        return {"n_obs": len(j), "signal_corr": None, "position_diff_ratio": None,
                "note": "公共窗不足 30 日，不判"}
    pm, po = position_of(j["mine"]), position_of(j["official"])
    return {
        "n_obs": int(len(j)),
        "window": [str(j.index.min().date()), str(j.index.max().date())],
        "signal_corr": round(float(j["mine"].corr(j["official"])), 4),
        "position_diff_ratio": round(float((pm != po).mean()), 4),
        "position_last_mine": int(pm.iloc[-1]),
        "position_last_official": int(po.iloc[-1]),
    }


def verdict_of(comparison: dict,
               min_corr: float = READY_SIGNAL_CORR,
               max_diff: float = READY_POSITION_DIFF) -> dict:
    """演练判定（单带口径，容差取自 §7.10 单带实测区间）。"""
    corr, diff = comparison.get("signal_corr"), comparison.get("position_diff_ratio")
    if corr is None or diff is None:
        return {"status": "INCONCLUSIVE", "reason": comparison.get("note", "读数缺失")}
    ok = corr >= min_corr and diff <= max_diff
    return {
        "status": "READY" if ok else "DEGRADED",
        "signal_corr": corr, "min_corr": min_corr,
        "position_diff_ratio": diff, "max_position_diff": max_diff,
        "reason": ("单带保真度落在 §7.10 已登记区间内，四对齐备时系统级差 0±0.05"
                   if ok else "单带保真度劣于 §7.10 登记区间，切换前须先查复刻管线"),
    }


# ─────────────────────────────── 连库构建 ───────────────────────────────
def build_legs(band: str, start: str, end: str,
               verbose: bool = True) -> tuple[pd.Series, pd.Series, dict]:
    """官方级复刻管线造该带的成长/价值腿日收益。

    300/500 用官方成分真值（`truth_codes_by_date`）；1000/2000 用官方化模拟选样
    （`official_sample_space`，库内无全历史成分）。参数与 Gate 0 逐字相同——
    灾备腿必须与已验收的复刻管线同源，不得另调。
    """
    from backtest.pure_style_builder import build_pair, rebalance_dates

    if band not in BAND_SPEC:
        raise ValueError(f"未知带 {band}（可选 {sorted(BAND_SPEC)}）")
    dates = rebalance_dates(start, end) + [pd.Timestamp(end)]
    if len(dates) < 2:
        raise ValueError(f"窗口 {start}~{end} 内不足一个完整调样期")

    kwargs = {"take_top_half": False, "official_space": True, "verbose": verbose}
    if band in ("300", "500"):
        from backtest.gate0_runner import truth_codes_by_date
        mother = BAND_SPEC[band]["mother"]
        kwargs["codes_by_date"] = truth_codes_by_date(mother, dates, require_all=True)
    else:
        from backtest.pure_style_builder import BAND_1000, BAND_2000
        kwargs["band"] = BAND_1000 if band == "1000" else BAND_2000

    pair = build_pair(None, None, dates, **kwargs)
    meta = {"band": band, "rebalances": len(dates) - 1,
            "start": start, "end": end,
            "skipped": list(pair.skipped or []),
            "n_growth": pair.n_growth, "n_value": pair.n_value}
    return pair.growth, pair.value, meta


def official_legs(band: str) -> tuple[pd.Series, pd.Series]:
    """官方腿日收益（对照组）。"""
    from backtest.gate0_runner import _conn
    code_g, code_v = BAND_SPEC[band]["official"]
    c, s = _conn()
    try:
        with c.cursor() as cur:
            cur.execute(f"""SELECT index_code, trade_date, close::float8
                            FROM {s}.index_daily WHERE index_code IN (%s,%s)
                              AND close IS NOT NULL ORDER BY trade_date""",
                        (code_g, code_v))
            df = pd.DataFrame(cur.fetchall(), columns=["code", "date", "close"])
    finally:
        c.close()
    df["date"] = pd.to_datetime(df["date"])
    w = df.pivot(index="date", columns="code", values="close").sort_index()
    return (w[code_g].pct_change(fill_method=None).dropna(),
            w[code_v].pct_change(fill_method=None).dropna())


def drill(band: str, start: str, end: str, verbose: bool = True) -> dict:
    """演练：造自建腿 → 两侧同法出信号 → 对比 → 判定。"""
    g, v, meta = build_legs(band, start, end, verbose)
    og, ov = official_legs(band)
    mine = signal_from_legs(g, v)
    official = signal_from_legs(og, ov)
    comparison = compare_signals(mine, official)
    return {"band": band, "build": meta, "comparison": comparison,
            "verdict": verdict_of(comparison),
            "legs": pd.DataFrame({"growth": g, "value": v})}


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="自建腿灾备构建与演练验收")
    ap.add_argument("--bands", default="1000", help="逗号分隔，可选 300,500,1000,2000")
    ap.add_argument("--start", default="2024-01-01")
    ap.add_argument("--end", default=None, help="缺省=今天")
    ap.add_argument("--output-dir", default=str(OUTDIR))
    args = ap.parse_args(argv)

    end = args.end or str(pd.Timestamp.today().date())
    out_dir = Path(args.output_dir)
    out_dir.mkdir(parents=True, exist_ok=True)

    results = {}
    for band in [b.strip() for b in args.bands.split(",") if b.strip()]:
        print(f"══ {band} 带灾备演练 {args.start} → {end}", flush=True)
        res = drill(band, args.start, end)
        res.pop("legs").to_csv(out_dir / f"failover_legs_{band}.csv")
        results[band] = res
        print(f"  {res['verdict']['status']}："
              f"信号 corr={res['comparison'].get('signal_corr')}，"
              f"仓位分歧={res['comparison'].get('position_diff_ratio')}", flush=True)

    path = out_dir / "failover_drill.json"
    path.write_text(json.dumps(results, ensure_ascii=False, indent=1))
    print(f"\n落盘 {path}")
    return 0 if all(r["verdict"]["status"] == "READY" for r in results.values()) else 1


if __name__ == "__main__":
    raise SystemExit(main())
