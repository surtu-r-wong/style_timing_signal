"""Gate 0 执行器（r3 预登记 §4）。运行器进 repo —— v4/v5 的运行脚本散在 scratchpad
已丢失、0R 只能按产物重构，此教训不再犯。

子命令：
  0r   回归护栏 preflight（§4.2b）：
         A) 锚复现阶段 —— 以 08-19 口径（含 .HK 的旧样本空间）重跑 v5a 2000 带模拟全窗，
            期望 ρ ≈ 0.8046（±0.01），证明代码基线未漂移；
         B) 护栏阶段 —— 现口径重跑 2000 带模拟全窗 + 500 带真值 T9，
            判据 ρ ≥ 0.7946 / ≥ 0.9536。A→B 之差即 .HK 护栏修复的量化影响（修复台账）。
  0a   主闸（§4.1）：1000 带官方化模拟（判定版：prev 自举、无 000852 真值注入）ρ ≥ 0.85；
         同跑 000852 真值直通诊断版（不判定）。首跑值永久登记。
  0b   副闸（§4.2）：2000 带真值直通、运营期（2023-12 起）ρ ≥ 0.85。

一律剔除 2026-06-16（§4.3，index_daily 批次缝数据事件）。
输出 `backtest/output/gate0{r,a,b}_result.json` + 序列 CSV。
"""
from __future__ import annotations

import json
import sys
import time
from pathlib import Path

import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import backtest.pure_style_builder as psb  # noqa: E402
from backtest.pure_style_builder import (  # noqa: E402
    BAND_1000,
    PairResult,
    _conn,
    _linked_members,
    build_pair,
    rebalance_dates,
    review_cutoff,
)

OUTDIR = ROOT / "backtest" / "output"
EXCLUDE_DAYS = {pd.Timestamp("2026-06-16")}       # §4.3 数据事件条款
CSMAR_END = pd.Timestamp("2025-03-31")            # 数据源分段界
WINDOW = ("2015-01-01", "2026-08-18")             # v5a 同窗
TERMINAL = pd.Timestamp("2026-08-18")


def official_spread(code_g: str, code_v: str) -> pd.Series:
    """官方纯风格价差日收益 = 成长腿 close pct − 价值腿 close pct（index_daily）。"""
    c, s = _conn()
    try:
        with c.cursor() as cur:
            cur.execute(f"""SELECT index_code, trade_date, close::float8 FROM {s}.index_daily
                            WHERE index_code IN (%s, %s) AND close IS NOT NULL
                            ORDER BY trade_date""", (code_g, code_v))
            df = pd.DataFrame(cur.fetchall(), columns=["code", "trade_date", "close"])
    finally:
        c.close()
    df["trade_date"] = pd.to_datetime(df["trade_date"])
    w = df.pivot(index="trade_date", columns="code", values="close").dropna()
    r = w.pct_change().dropna()
    return (r[code_g] - r[code_v]).rename("official")


def rho_report(mine: pd.Series, official: pd.Series) -> dict:
    """公共日（剔 §4.3 事件日）的全窗与分段 ρ。"""
    j = pd.concat([mine.rename("mine"), official.rename("official")], axis=1).dropna()
    j = j[~j.index.isin(EXCLUDE_DAYS)]
    seg_c = j[j.index <= CSMAR_END]
    seg_w = j[j.index > CSMAR_END]

    def _c(x):
        return round(float(x["mine"].corr(x["official"])), 4) if len(x) > 30 else None

    return {"n_obs": int(len(j)), "rho": _c(j),
            "rho_csmar": [_c(seg_c), int(len(seg_c))],
            "rho_wind": [_c(seg_w), int(len(seg_w))],
            "window": [str(j.index.min().date()), str(j.index.max().date())]}


def truth_codes_by_date(index_code: str, dates: list[pd.Timestamp],
                        require_all: bool = False) -> dict:
    """{生效日 → 官方名单}（联动新一期优先，缺则回退并出声；全无则该期缺席）。"""
    out = {}
    for d in dates[:-1]:
        leg = _linked_members(index_code, d, review_cutoff(d), verbose=True)
        if leg:
            out[str(d.date())] = sorted(leg)
        elif require_all:
            raise RuntimeError(f"{index_code} @{d.date()} 无真值名单，真值直通版无法构建")
    return out


def spread_of(pair: PairResult) -> pd.Series:
    return (pair.growth - pair.value).dropna()


def dump(name: str, payload: dict, mine: pd.Series | None = None,
         official: pd.Series | None = None) -> None:
    p = OUTDIR / f"{name}.json"
    p.write_text(json.dumps(payload, ensure_ascii=False, indent=1))
    if mine is not None and official is not None:
        pd.concat([mine.rename("mine"), official.rename("official")], axis=1) \
            .to_csv(OUTDIR / f"{name}_series.csv")
    print(f"落盘 {p}", flush=True)


def run_0r() -> None:
    t0 = time.time()
    dates = rebalance_dates(*WINDOW) + [TERMINAL]
    off2000 = official_spread("932409.CSI", "932408.CSI")
    res: dict = {"gate": "0R", "anchors": {"v5a": 0.8046, "band500": 0.9636},
                 "thresholds": {"sim2000": 0.7946, "band500": 0.9536}}

    print("== 0R-A 锚复现（旧口径：样本空间含 .HK）", flush=True)
    psb._EXCHANGE_SUFFIXES = (".SH", ".SZ", ".BJ", ".HK")
    try:
        pair = build_pair(None, None, dates, take_top_half=True, official_space=True)
        res["repro_hk"] = rho_report(spread_of(pair), off2000)
        res["repro_hk"]["n_by_date"] = pair.n_growth
    finally:
        psb._EXCHANGE_SUFFIXES = (".SH", ".SZ", ".BJ")
    print(f"  锚复现 ρ = {res['repro_hk']['rho']}（锚 0.8046）", flush=True)

    print("== 0R-B1 现口径 2000 带模拟全窗（.HK 护栏生效）", flush=True)
    pair = build_pair(None, None, dates, take_top_half=True, official_space=True)
    res["sim2000_guarded"] = rho_report(spread_of(pair), off2000)
    print(f"  ρ = {res['sim2000_guarded']['rho']}（判据 ≥0.7946）", flush=True)

    print("== 0R-B2 500 带真值 + T9", flush=True)
    cbd = truth_codes_by_date("000905.SH", dates, require_all=True)
    pair = build_pair(None, None, dates, take_top_half=False, official_space=True,
                      codes_by_date=cbd)
    off500 = official_spread("932403.CSI", "932402.CSI")
    res["band500_true"] = rho_report(spread_of(pair), off500)
    print(f"  ρ = {res['band500_true']['rho']}（判据 ≥0.9536）", flush=True)

    res["hk_guard_delta"] = (None if res["sim2000_guarded"]["rho"] is None
                             else round(res["sim2000_guarded"]["rho"]
                                        - res["repro_hk"]["rho"], 4))
    res["pass"] = bool(res["repro_hk"]["rho"] is not None
                       and abs(res["repro_hk"]["rho"] - 0.8046) <= 0.01
                       and res["sim2000_guarded"]["rho"] >= 0.7946
                       and res["band500_true"]["rho"] >= 0.9536)
    res["elapsed_s"] = round(time.time() - t0, 1)
    dump("gate0r_result", res)
    print(f"0R {'PASS' if res['pass'] else 'FAIL'}（{res['elapsed_s']}s）", flush=True)


def run_0a() -> None:
    t0 = time.time()
    dates = rebalance_dates(*WINDOW) + [TERMINAL]
    off = official_spread("932407.CSI", "932406.CSI")
    res: dict = {"gate": "0A", "threshold": 0.85,
                 "judged": "官方化模拟（prev 自举，无 000852 真值注入）"}

    print("== 0A 判定版：1000 带官方化模拟（首跑，永久登记）", flush=True)
    pair = build_pair(None, None, dates, take_top_half=False, official_space=True,
                      band=BAND_1000, truth_prev_index=None)
    mine = spread_of(pair)
    res["first_run"] = rho_report(mine, off)
    res["first_run"]["n_by_date"] = {k: [pair.n_growth[k], pair.n_value[k]]
                                     for k in pair.n_growth}
    res["first_run"]["skipped"] = pair.skipped
    print(f"  ⭐ 首跑 ρ = {res['first_run']['rho']}（判据 ≥0.85）", flush=True)

    print("== 0A 诊断版：000852 真值直通（不判定）", flush=True)
    cbd = truth_codes_by_date("000852.SH", dates)
    pair_d = build_pair(None, None, dates, take_top_half=False, official_space=True,
                        band=BAND_1000, truth_prev_index=None, codes_by_date=cbd)
    res["diagnostic_truth"] = rho_report(spread_of(pair_d), off)
    res["diagnostic_truth"]["n_truth_periods"] = len(cbd)
    res["sim_cost"] = (None if res["diagnostic_truth"]["rho"] is None
                       else round(res["diagnostic_truth"]["rho"] - res["first_run"]["rho"], 4))
    print(f"  诊断版 ρ = {res['diagnostic_truth']['rho']}（真值期 {len(cbd)}）", flush=True)

    res["pass"] = bool(res["first_run"]["rho"] is not None and res["first_run"]["rho"] >= 0.85)
    res["elapsed_s"] = round(time.time() - t0, 1)
    dump("gate0a_result", res, mine, off)
    print(f"0A 首跑 {'PASS' if res['pass'] else 'FAIL — 进入诊断-修复-重跑循环（§4.1）'}"
          f"（{res['elapsed_s']}s）", flush=True)


def run_0b() -> None:
    t0 = time.time()
    dates = rebalance_dates("2023-10-01", "2026-08-18") + [TERMINAL]
    off = official_spread("932409.CSI", "932408.CSI")
    print("== 0B 副闸：2000 带真值直通，运营期", flush=True)
    cbd = truth_codes_by_date("932000.CSI", dates, require_all=True)
    pair = build_pair(None, None, dates, take_top_half=True, official_space=True,
                      codes_by_date=cbd)
    mine = spread_of(pair)
    res = {"gate": "0B", "threshold": 0.85, "anchor_0819": 0.8727,
           "truth": rho_report(mine, off)}
    res["pass"] = bool(res["truth"]["rho"] is not None and res["truth"]["rho"] >= 0.85)
    res["elapsed_s"] = round(time.time() - t0, 1)
    dump("gate0b_result", res, mine, off)
    print(f"0B ρ = {res['truth']['rho']} → {'PASS' if res['pass'] else 'FAIL'}"
          f"（{res['elapsed_s']}s）", flush=True)



def run_preflight500() -> None:
    """0R'（等比5桶预登记 裁决点3）：只复算 500 带真值锚，确认代码未从 Gate 0 状态漂移。"""
    t0 = time.time()
    dates = rebalance_dates(*WINDOW) + [TERMINAL]
    cbd = truth_codes_by_date("000905.SH", dates, require_all=True)
    pair = build_pair(None, None, dates, take_top_half=False, official_space=True,
                      codes_by_date=cbd)
    off500 = official_spread("932403.CSI", "932402.CSI")
    rep = rho_report(spread_of(pair), off500)
    ok = rep["rho"] is not None and rep["rho"] >= 0.9536
    res = {"gate": "0R'", "anchor": 0.9636, "threshold": 0.9536, "band500_true": rep,
           "pass": bool(ok), "elapsed_s": round(time.time() - t0, 1)}
    dump("gate0rp_result", res)
    print(f"0R' rho = {rep['rho']} -> {'PASS' if ok else 'FAIL'}（{res['elapsed_s']}s）", flush=True)


if __name__ == "__main__":
    cmd = sys.argv[1] if len(sys.argv) > 1 else ""
    {"0r": run_0r, "0a": run_0a, "0b": run_0b, "0rp": run_preflight500}.get(
        cmd, lambda: sys.exit("用法: gate0_runner.py 0r|0a|0b|0rp"))()
