"""Gate 0 执行器（r3 预登记 §4）。运行器进 repo —— v4/v5 的运行脚本散在 scratchpad
已丢失、0R 只能按产物重构，此教训不再犯。

子命令：
  0r   回归护栏 preflight（§4.2b）：
         A) 锚复现阶段 —— 以含 .HK 的旧样本空间重跑 v5a 2000 带模拟全窗，
            期望 ρ ≈ 锚（±0.01），证明代码基线未漂移；
         B) 护栏阶段 —— 现口径重跑 2000 带模拟全窗 + 500 带真值 T9，
            判据 = 各判定量自己的锚 − 0.01。A→B 之差即 .HK 护栏的量化影响（修复台账）。
       锚值见下方登记常量（2026-08-21 重登，裁决记录 =
       docs/plans/2026-08-20-data-foundation-repair.md §7）。
  0a   主闸（§4.1）：1000 带官方化模拟（判定版：prev 自举、无 000852 真值注入）ρ ≥ 0.85；
         同跑 000852 真值直通诊断版（不判定）。首跑值永久登记。
  0b   副闸（§4.2）：2000 带真值直通、运营期（2023-12 起）ρ ≥ 0.85。

一律剔除 2026-06-16（§4.3，index_daily 批次缝数据事件）。
输出 `backtest/output/gate0{r,a,b}_result.json` + 序列 CSV。
"""
from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path

import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import backtest.pure_style_builder as psb  # noqa: E402
from backtest.run_manifest import (  # noqa: E402
    DEFAULT_INPUT_CONTRACT,
    input_drift_report,
    query_table_write_marks,
)
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

# 锚与地板登记（2026-08-25 **第二次**重登：`index_daily` 修复后、库静默期的干净跑）。
#
# ⚠️ 锚与地板**解耦**：地板是**绝对标准**，不随锚漂移。08-21 之前注释里写的
# 「地板 = 各判定量自己的锚 − 0.01」只是当时的标定由来，不是维护规则——若地板
# 每次重登都跟着锚下移，绝对标准会被逐轮棘轮稀释，Gate 0 就不再是关卡。
#
# 三个锚里只有第一个进 pass/fail（±0.01 复现带，用途 = 探测代码漂移）；
# 另两个只进 JSON 报表，判据是下面两条 FLOOR。
#
# ## 为什么当天重登两次（教训，勿删）
#
# 08-25 上午那次重登用的是 08-24 run 的读数，而**那次 run（15:46 起跑）早于同日
# 19:18 的 `index_daily` 风格腿滞后修复 3.5 小时** —— 932408/932409 有 12 个交易日
# 装着滞后一天的值，锚因此在**被污染的对照序列**上标定，偏低约 0.012。
# 取证与双向 A/B 归因（已排除代码改动、伪行、重述 tie-break 三种解释）见
# `docs/plans/2026-08-25-gate0-anchor-contamination.md`。
#
# 由此立了 `input_drift` 机制（`run_manifest.input_drift_report`）：四个 runner
# 起跑拍输入表 `max(updated_at)`、收尾比对，窗口内被改写则**拒绝登记为首跑值**。
# 本次登记的运行 `registrable_as_first_run=True`（五张表写入时刻逐个未动）。
#
# ⚠️ 现存余量：sim2000 距 FLOOR_SIM2000 为 0.0166。跌破即 0R 形式 FAIL ——
# 那是绝对地板的设计意图，不是事故，别当回归查。
#
# 历史锚永久在案（勿删）：
#   08-19 首跑 0.8046/0.8007/0.9636 + 旧地板 0.7946/0.9536（DP 恒 0 时代）
#   08-21 重登 0.7951/0.7900/0.9698（DP 修复后、首披日升级前）
#   08-25 上午 0.7900/0.7847/0.9698（**污染值，勿用** —— 见上文）
# 证据：git 历史 + data_fixes/2026-08-20-dp-factor-and-leg-lists/ +
# backtest/output/runs/20260825T163511-gate0r-anchor-recalibration-2e83a20/
ANCHOR_REPRO_HK = 0.8022   # 0R-A 锚复现（样本空间含 .HK 旧口径），复现带 ±0.01
ANCHOR_SIM2000 = 0.7966    # 0R-B1 现口径 2000 带模拟全窗（.HK 护栏生效）—— 仅报表用
ANCHOR_BAND500 = 0.9698    # 0R-B2 / 0R' 500 带真值 + T9（08-24 逐位不变）—— 仅报表用
FLOOR_SIM2000 = 0.7800
FLOOR_BAND500 = 0.9598
ANCHOR_0B = 0.8815         # 0B 参考锚（判据仍是预登记绝对阈值 0.85）
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
         official: pd.Series | None = None, *, outdir: Path = OUTDIR) -> None:
    outdir = Path(outdir)
    outdir.mkdir(parents=True, exist_ok=True)
    p = outdir / f"{name}.json"
    p.write_text(json.dumps(payload, ensure_ascii=False, indent=1))
    if mine is not None and official is not None:
        pd.concat([mine.rename("mine"), official.rename("official")], axis=1) \
            .to_csv(outdir / f"{name}_series.csv")
    print(f"落盘 {p}", flush=True)


def _drift_marks() -> tuple[dict, str]:
    """起跑前拍一次输入表写入时刻 + 库时钟（用库时钟而非本机时钟，避免时钟漂移）。"""
    c, s = _conn()
    try:
        with c.cursor() as cur:
            cur.execute("SELECT now()::text")
            now = cur.fetchone()[0]
        return query_table_write_marks(c, s, DEFAULT_INPUT_CONTRACT), now
    finally:
        c.close()


def _drift_check(marks: dict, since: str) -> dict:
    """收尾时比对：run 期间输入被动过吗？动的行落在分析窗口内吗？

    2026-08-25 立此机制的直接教训见 `run_manifest.query_table_write_marks` docstring
    与 `docs/plans/2026-08-25-gate0-anchor-contamination.md`。
    """
    c, s = _conn()
    try:
        rep = input_drift_report(c, s, DEFAULT_INPUT_CONTRACT, marks, since,
                                 TERMINAL.date().isoformat())
    finally:
        c.close()
    if rep["inputs_moved_in_window"]:
        print("  ⛔ 输入在 run 期间被改写，且改动落在分析窗口内："
              f"{rep['rows_touched_in_window']}", flush=True)
        print("  ⛔ 本次读数**不得**登记为首跑值 / 不得用于重登锚。", flush=True)
    elif rep["inputs_moved"]:
        print(f"  ⚠️ 输入有写入但全在分析窗口外（{TERMINAL.date()} 之后），不影响读数："
              f"{sorted(rep['moved_tables'])}", flush=True)
    else:
        print("  ✓ 输入在 run 期间未被改写 → 读数可登记为首跑值", flush=True)
    return rep


def run_0r(outdir: Path = OUTDIR) -> int:
    t0 = time.time()
    marks0, since = _drift_marks()
    dates = rebalance_dates(*WINDOW) + [TERMINAL]
    off2000 = official_spread("932409.CSI", "932408.CSI")
    res: dict = {"gate": "0R", "anchors_registered": "2026-08-25b",
                 "anchors": {"repro_hk": ANCHOR_REPRO_HK, "sim2000_guarded": ANCHOR_SIM2000,
                             "band500": ANCHOR_BAND500},
                 "thresholds": {"sim2000": FLOOR_SIM2000, "band500": FLOOR_BAND500}}

    print("== 0R-A 锚复现（旧口径：样本空间含 .HK）", flush=True)
    psb._EXCHANGE_SUFFIXES = (".SH", ".SZ", ".BJ", ".HK")
    try:
        pair = build_pair(None, None, dates, take_top_half=True, official_space=True)
        res["repro_hk"] = rho_report(spread_of(pair), off2000)
        res["repro_hk"]["n_by_date"] = pair.n_growth
    finally:
        psb._EXCHANGE_SUFFIXES = (".SH", ".SZ", ".BJ")
    print(f"  锚复现 ρ = {res['repro_hk']['rho']}（锚 {ANCHOR_REPRO_HK}）", flush=True)

    print("== 0R-B1 现口径 2000 带模拟全窗（.HK 护栏生效）", flush=True)
    pair = build_pair(None, None, dates, take_top_half=True, official_space=True)
    res["sim2000_guarded"] = rho_report(spread_of(pair), off2000)
    print(f"  ρ = {res['sim2000_guarded']['rho']}（判据 ≥{FLOOR_SIM2000}）", flush=True)

    print("== 0R-B2 500 带真值 + T9", flush=True)
    cbd = truth_codes_by_date("000905.SH", dates, require_all=True)
    pair = build_pair(None, None, dates, take_top_half=False, official_space=True,
                      codes_by_date=cbd)
    off500 = official_spread("932403.CSI", "932402.CSI")
    res["band500_true"] = rho_report(spread_of(pair), off500)
    print(f"  ρ = {res['band500_true']['rho']}（判据 ≥{FLOOR_BAND500}）", flush=True)

    res["hk_guard_delta"] = (None if res["sim2000_guarded"]["rho"] is None
                             else round(res["sim2000_guarded"]["rho"]
                                        - res["repro_hk"]["rho"], 4))
    res["pass"] = bool(res["repro_hk"]["rho"] is not None
                       and abs(res["repro_hk"]["rho"] - ANCHOR_REPRO_HK) <= 0.01
                       and res["sim2000_guarded"]["rho"] >= FLOOR_SIM2000
                       and res["band500_true"]["rho"] >= FLOOR_BAND500)
    res["elapsed_s"] = round(time.time() - t0, 1)
    res["input_drift"] = _drift_check(marks0, since)
    dump("gate0r_result", res, outdir=outdir)
    print(f"0R {'PASS' if res['pass'] else 'FAIL'}（{res['elapsed_s']}s）", flush=True)
    return 0


def run_0a(outdir: Path = OUTDIR) -> int:
    t0 = time.time()
    marks0, since = _drift_marks()
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
    res["input_drift"] = _drift_check(marks0, since)
    dump("gate0a_result", res, mine, off, outdir=outdir)
    print(f"0A 首跑 {'PASS' if res['pass'] else 'FAIL — 进入诊断-修复-重跑循环（§4.1）'}"
          f"（{res['elapsed_s']}s）", flush=True)
    return 0


def run_0b(outdir: Path = OUTDIR) -> int:
    t0 = time.time()
    marks0, since = _drift_marks()
    dates = rebalance_dates("2023-10-01", "2026-08-18") + [TERMINAL]
    off = official_spread("932409.CSI", "932408.CSI")
    print("== 0B 副闸：2000 带真值直通，运营期", flush=True)
    cbd = truth_codes_by_date("932000.CSI", dates, require_all=True)
    pair = build_pair(None, None, dates, take_top_half=True, official_space=True,
                      codes_by_date=cbd)
    mine = spread_of(pair)
    res = {"gate": "0B", "threshold": 0.85, "anchor": ANCHOR_0B,
           "truth": rho_report(mine, off)}
    res["pass"] = bool(res["truth"]["rho"] is not None and res["truth"]["rho"] >= 0.85)
    res["elapsed_s"] = round(time.time() - t0, 1)
    res["input_drift"] = _drift_check(marks0, since)
    dump("gate0b_result", res, mine, off, outdir=outdir)
    print(f"0B ρ = {res['truth']['rho']} → {'PASS' if res['pass'] else 'FAIL'}"
          f"（{res['elapsed_s']}s）", flush=True)
    return 0



def run_preflight500(outdir: Path = OUTDIR) -> int:
    """0R'（等比5桶预登记 裁决点3）：只复算 500 带真值锚，确认代码未从 Gate 0 状态漂移。"""
    t0 = time.time()
    marks0, since = _drift_marks()
    dates = rebalance_dates(*WINDOW) + [TERMINAL]
    cbd = truth_codes_by_date("000905.SH", dates, require_all=True)
    pair = build_pair(None, None, dates, take_top_half=False, official_space=True,
                      codes_by_date=cbd)
    off500 = official_spread("932403.CSI", "932402.CSI")
    rep = rho_report(spread_of(pair), off500)
    ok = rep["rho"] is not None and rep["rho"] >= FLOOR_BAND500
    res = {"gate": "0R'", "anchor": ANCHOR_BAND500, "threshold": FLOOR_BAND500,
           "band500_true": rep, "pass": bool(ok), "elapsed_s": round(time.time() - t0, 1)}
    res["input_drift"] = _drift_check(marks0, since)
    dump("gate0rp_result", res, outdir=outdir)
    print(f"0R' rho = {rep['rho']} -> {'PASS' if ok else 'FAIL'}（{res['elapsed_s']}s）", flush=True)
    return 0


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="Gate 0 执行器")
    ap.add_argument("gate", choices=("0r", "0a", "0b", "0rp"))
    ap.add_argument("--output-dir", type=Path, default=OUTDIR)
    args = ap.parse_args(argv)
    return {"0r": run_0r, "0a": run_0a, "0b": run_0b, "0rp": run_preflight500}[args.gate](
        outdir=args.output_dir)


if __name__ == "__main__":
    raise SystemExit(main())
