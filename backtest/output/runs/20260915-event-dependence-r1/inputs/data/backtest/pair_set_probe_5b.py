"""⑤b 探针：红利腿**换伙伴**补测（沪深300 vs 中证红利）—— 单候选 E = 现役四对 + 红利腿。

预登记（**冻结，本模块只对表执行、无权改动**）：
`docs/plans/2026-08-13-probe-5b-dividend-partner-prereg.md` §0-§5。

## 为什么有这一批次

Batch 11（`docs/plans/2026-08-13-probe-5-pair-set.md` §2）实测发现：预登记里那对
`000922 / H00922` 是**同一条中证红利指数的价格版 / 全收益版**（日收益 corr 0.9983、
净值比年化漂移 +4.43% = 股息）→ 按"成长/价值式对照"构造出来的所谓"红利腿"，
相对收益的经济含义是**分红计提**，不是风格对照。⑤(a) 子命题 (ii)
（红利腿提供四对未表达的风格维度）**在那个构造下不可检验**。
本批次按用户拍板放行的补测，把腿换成**真风格对照**：`沪深300`（宽基/进攻腿）
vs `中证红利`（防御腿），两条都是**价格指数**（不用全收益版 —— Batch 11 的直接教训）。

## 构造（预登记 §1，逐条硬点）

| 项 | 取值 |
|---|---|
| 候选集合 | **恰 1 个**：`E_four_plus_dividend_leg` = 现役四对 + 红利腿，组内**算术平均、无权重参数** |
| 红利腿 | `PairConfig(group=5, left_column="沪深300", right_column="中证红利", direction="forward")`。left=进攻腿、right=防御腿；**+z = 宽基跑赢红利 = 风险偏好上行**，与成长腿同号进入等权。先验经济符号，非拟合 |
| 数据码 | `000300.SH`（价格指数）+ `000922.CSI`（价格版）；`signals/common/index_codes.csv` **不加映射**，走 Batch 11 的只读包装器 `pair_set_probe.load_dividend_closes(codes=...)`（冻结根按字节钉死，加能力只走包装器） |
| 参数 | lookback=20 / z_window=40 / smoothing=5 —— **从 `pair_set_probe` import 常量**，本模块不写字面量 |
| 日历 | 八风格指数日历；伙伴两码 reindex 对齐后**零缺值断言**（见下） |
| 引擎/同秤 | `backtest.data` + `backtest.engine.run_strategy`（3bp/边、全日历、blend carry）、`positions.production_position`（long-flat θ=0）、`fusion_probe` 非重叠 IC 机器 —— 全部 import 复用，**无平行实现** |

## 闸门（预登记 §3；判据顺序硬编码：前置诊断闸 → ⓪ → 收益层）

1. **前置诊断闸**：红利腿因子 z 序列 vs 现役合成因子 A 及四对分量的
   max|corr| **> 0.9 → 同源、不测、出局**（判定入档含覆盖天数）；
2. **闸门 ⓪**：E vs A 的非重叠 20 日非偏 rank IC，须 **train_2014_2020 与 val_2021_2023
   双窗均高于 A**，**且**配对 IC 差（同组抽样行同时作用两侧）在 **train 或 val** 窗
   CI 排除 0。**单候选 → α=0.05（95% CI），无 Bonferroni**；n_boot=10000, seed=0；
3. **收益层三判据**（blend · long-flat · 3bp+carry · 全日历）：worst(train,val) 净 Sharpe
   ≥ A **+0.15**、换手不增、剔最强年不劣；
4. **holdout 2024-2026 只报告**，禁止出现在任何闸门行；
5. 前一道不过即 `OVERALL=STOP`，**但收益层读数仍全表入档**（供关帐引用）。

**不跑 `selection_permutation`**：单候选无选择集，没有"选赢家"这回事（预登记 §3.5）。

## 数据完备性 preflight（正式跑的硬闸）

伙伴两码 reindex 到八风格日历后**零缺值**，否则抛 `DataIncompleteError`
（DATA_INCOMPLETE 语义，错误带缺失日期清单）。**没有绕过开关** —— 库里有洞就不许出结果。
（2026-08-13 现状：`000300.SH` 在 **2026-05-18 ~ 2026-06-15** 缺 21 个交易日，
成因见预登记 §6；洞全部落在 holdout，但 §1 冻结了全日历零缺值断言 → 补数到货前不开跑。）

CLI: python3 -m backtest.pair_set_probe_5b [--n-boot 10000] [--seed 0] [--db-host ...]
产出: backtest/output/probe_5b_dividend_partner_{panel,ic,diagnostics,verdict}.csv
      + probe_5b_dividend_partner_summary.json（sidecar）

本模块**不改动** `signals/`（含 `index_codes.csv` / `config_4pairs.csv`）/ `positions.py` /
`engine.py` / `pair_set_probe.py` / 任何既有探针 / 任何生产文件，只读库与只读 committed CSV。
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from backtest.fusion_probe import (  # noqa: E402
    forward_return, nonoverlap_grid, paired_ic_bootstrap, rank_ic, spearman_rows,
)
from backtest.pair_set_probe import (  # noqa: E402  —— 参数/口径/同秤机器一律复用，不另写
    ALPHA, DIV_CORR_GATE, GATE_WORST_TV_LIFT, IC_OFFSET, K_FORWARD, KOU_JING_REPORT,
    LOOKBACK, MAPPINGS, PRIMARY_KOU_JING, PRODUCTION_COL, PRODUCTION_CSV, SELECTION,
    SMOOTHING, STAT_WINDOWS, STYLE_NAMES, WINDOWS_REPORT, Z_WINDOW, _slice,
    evaluate_windows, load_dividend_closes, load_production_signal, map_position,
)
from backtest.pair_set_probe import PAIR_LEGS as _INCUMBENT_PAIR_LEGS  # noqa: E402
from signals.equal_weight.generate_signal import PairConfig  # noqa: E402

#: 同秤清单（预登记 §1）的 re-export：QA 独立复算时从本模块取到的必须是**同一支实现**。
#: `spearman_rows` 本模块不直接调用（它是 `paired_ic_bootstrap` 的内核），列在这里是为了
#: 让"没有平行 harness"这件事可被判例检验，而不是靠读 import 段自证。
SAME_SCALE_FUNCTIONS = (forward_return, nonoverlap_grid, rank_ic, paired_ic_bootstrap,
                        spearman_rows, map_position, evaluate_windows)

# ---------------------------------------------------------------- 预登记常量（不得扩）
DIVIDEND_LEG_KEY = "dividend_vs_broad"
#: 红利腿 = (左=进攻腿 沪深300, 右=防御腿 中证红利)。**两条都是价格指数**。
DIVIDEND_LEG = ("沪深300", "中证红利")
#: 只读直查用的数据码 → 中文列名。`index_codes.csv` 里**不加**这两行（冻结根）。
PARTNER_CODES = {"000300.SH": "沪深300", "000922.CSI": "中证红利"}

# 中文列名 → (进攻腿, 防御腿)。前四行 = Batch 11 的现役四对（同一份定义，不重抄）。
PAIR_LEGS = {**{k: _INCUMBENT_PAIR_LEGS[k] for k in ("300", "500", "1000", "2000")},
             DIVIDEND_LEG_KEY: DIVIDEND_LEG}

INCUMBENT = "A_four_pairs_incumbent"
CANDIDATE = "E_four_plus_dividend_leg"
SETS = {
    INCUMBENT: ("300", "500", "1000", "2000"),
    CANDIDATE: ("300", "500", "1000", "2000", DIVIDEND_LEG_KEY),
}
CHALLENGERS = (CANDIDATE,)

# 判据阈值：LOOKBACK / Z_WINDOW / SMOOTHING / K_FORWARD / IC_OFFSET /
# GATE_WORST_TV_LIFT / DIV_CORR_GATE / ALPHA 全部从 `pair_set_probe` import —— 见上。
BONFERRONI_M = 1               # **单候选 → 不做 Bonferroni**（预登记 §3.2）
GATE0_ALPHA = ALPHA / BONFERRONI_M          # = 0.05 → 95% CI
N_BOOT = 10000
SEED = 0
PRODUCTION_TRACE_TOL = 5e-05   # 生产 CSV 只存 4 位小数 → round(4) 的理论上界
OUTPUT_PREFIX = "probe_5b_dividend_partner"
_MAX_LISTED_DATES = 60         # 报错里逐日列举的上限（超出只列前 N 天 + 区间摘要）
#: 判据顺序（预登记 §3，**硬编码**）：前一道不过即 STOP。
GATE_ORDER = ("PRE_partner_corr", "0_rank_ic", "1_worst_tv_sharpe",
              "2_turnover_not_up", "3_concentration")


class DataIncompleteError(ValueError):
    """DATA_INCOMPLETE：价格面板未覆盖八风格日历 —— 正式跑的硬闸，**无绕过开关**。"""


# ---------------------------------------------------------------- 纯函数：集合 → 配对配置
def pair_configs_for(set_name: str) -> list[PairConfig]:
    """预登记集合 → `PairConfig` 列表（组号从 1 连续重编，冻结根的校验要求）。

    `direction="forward"` 逐行照抄 `config_4pairs.csv`；红利腿按预登记 §1 的
    **先验经济符号**取 left=沪深300 / right=中证红利（不是自由裁量点）。
    """
    if set_name not in SETS:
        raise KeyError(f"未预登记的集合: {set_name!r}（预登记恰 2 个：{list(SETS)}）")
    out = []
    for i, key in enumerate(SETS[set_name], start=1):
        left, right = PAIR_LEGS[key]
        out.append(PairConfig(group=i, left_column=left, right_column=right,
                              direction="forward"))
    return out


def build_factor(prices: pd.DataFrame, set_name: str) -> pd.Series:
    """某集合的因子序列（**冻结根逐字复用**，只换 `pair_configs`）。"""
    from signals.equal_weight.generate_signal import calculate_contrast_equal_weight_signal

    out = calculate_contrast_equal_weight_signal(
        prices, lookback=LOOKBACK, z_window=Z_WINDOW, smoothing_window=SMOOTHING,
        pair_configs=pair_configs_for(set_name))
    return out[PRODUCTION_COL].astype(float)


def build_pair_factors(prices: pd.DataFrame) -> dict[str, pd.Series]:
    """逐对的 tanh(z) 分量（诊断用：红利腿 vs 四对**各自**的相关性）。"""
    from signals.equal_weight.generate_signal import calculate_contrast_equal_weight_signal

    out = {}
    for key, (left, right) in PAIR_LEGS.items():
        frame = calculate_contrast_equal_weight_signal(
            prices, lookback=LOOKBACK, z_window=Z_WINDOW, smoothing_window=SMOOTHING,
            pair_configs=[PairConfig(group=1, left_column=left, right_column=right,
                                     direction="forward")])
        out[key] = frame[PRODUCTION_COL].astype(float)
    return out


# ---------------------------------------------------------------- 数据装载（只读）+ preflight
def load_partner_closes(db: dict | None = None, codes: dict | None = None) -> pd.DataFrame:
    """伙伴两码（000300.SH / 000922.CSI）收盘价 —— **复用 Batch 11 的只读包装器**。

    存在理由同 `pair_set_probe.load_dividend_closes` 的 docstring：
    `signals/common/index_codes.csv` 是**冻结根**（parity oracle 按字节钉死），
    加能力走包装器而不是就地改表。本函数只 SELECT，无任何写入。
    """
    return load_dividend_closes(db, codes=codes or PARTNER_CODES)


def _contiguous_ranges(dates: pd.DatetimeIndex,
                       calendar: pd.DatetimeIndex | None = None) -> list[str]:
    """把缺失日期压成连续区间串（缺口成因一眼可读，如 `2026-05-18~2026-06-15`）。

    "连续"按**日历上的相邻**判定（日历 = 面板索引，本身已是交易日序列）→ 长假不会把
    一个洞劈成两段。缺省无日历时退回"日历日相差 ≤ 4 天"的近似（周末间隔）。
    """
    if len(dates) == 0:
        return []
    if calendar is not None:
        pos = pd.Index(calendar).get_indexer(dates)
        adjacent = [bool(pos[i] == pos[i - 1] + 1) for i in range(1, len(pos))]
    else:
        adjacent = [bool((dates[i] - dates[i - 1]).days <= 4)
                    for i in range(1, len(dates))]
    out, start, prev = [], dates[0], dates[0]
    for i, d in enumerate(dates[1:]):
        if adjacent[i]:
            prev = d
            continue
        out.append((start, prev))
        start = prev = d
    out.append((start, prev))
    return [f"{a.date()}" if a == b else f"{a.date()}~{b.date()}" for a, b in out]


def check_calendar_coverage(panel: pd.DataFrame,
                            columns: list[str] | None = None) -> dict:
    """**数据完备性 preflight**：面板逐列在给定日历上零缺值，否则 `DataIncompleteError`。

    预登记 §1 冻结了"红利/300 reindex 对齐后零缺值断言"这一条，故这是**正式跑的闸**：
    有洞就不许出结果，错误里带**缺失日期清单**（DATA_INCOMPLETE 语义）供补数用。

    ⚠️ 比房规 `data_source._check_nan_policy` **更严**：那边"起始缺失（指数未发布）放行"，
    这里不放行——伙伴腿若晚于 2014-01-02 起才有数，覆盖就是不全，同属 DATA_INCOMPLETE。
    """
    cols = list(columns or panel.columns)
    detail, dates_by_col = {}, {}
    for col in cols:
        miss = panel.index[panel[col].isna()]
        if len(miss):
            detail[col] = len(miss)
            dates_by_col[col] = pd.DatetimeIndex(miss)
    n_missing = int(sum(detail.values()))
    diag = {
        "complete": n_missing == 0,
        "n_days": int(len(panel)),
        "first_date": str(panel.index.min().date()) if len(panel) else None,
        "last_date": str(panel.index.max().date()) if len(panel) else None,
        "columns_checked": cols,
        "n_missing_total": n_missing,
        "n_missing_by_column": detail,
    }
    if n_missing:
        parts = []
        for col, dates in dates_by_col.items():
            listed = [str(d.date()) for d in dates[:_MAX_LISTED_DATES]]
            more = "" if len(dates) <= _MAX_LISTED_DATES else \
                f" …（共 {len(dates)} 天，仅列前 {_MAX_LISTED_DATES} 天）"
            parts.append(f"{col} 缺 {len(dates)} 天，区间 "
                         f"[{'; '.join(_contiguous_ranges(dates, panel.index))}]，"
                         f"缺失日期: {', '.join(listed)}{more}")
        raise DataIncompleteError(
            f"DATA_INCOMPLETE: 价格面板未覆盖八风格日历 "
            f"（{diag['n_days']} 天，{diag['first_date']} ~ {diag['last_date']}），"
            f"合计缺 {n_missing} 个（列 × 天）。预登记 §1 冻结了零缺值断言 → 补数到货前不得开跑。"
            f" 明细：" + "；".join(parts))
    return diag


def load_prices(db: dict | None = None) -> pd.DataFrame:
    """八条风格 + 伙伴两码 → 同一张日历的宽表（日历 = 八条风格指数的交易日）。

    风格八条走房规 `load_pg_closes`（含 NaN 三级策略）；伙伴两码 reindex 到同一日历，
    随后过 `check_calendar_coverage` 的零缺值 preflight（缺一天即 DATA_INCOMPLETE）。
    """
    from signals.common.config import load_db_config
    from signals.common.data_source import load_pg_closes

    db = db or load_db_config()
    style = load_pg_closes(STYLE_NAMES, db=db)
    partner = load_partner_closes(db)
    joined = style.join(partner.reindex(style.index), how="left")
    check_calendar_coverage(joined)
    return joined


def production_trace_diff(incumbent_factor: pd.Series) -> dict:
    """溯源锚点：重建的现役 A vs committed 生产 CSV 的逐日 max|diff|。

    生产 CSV 只存 4 位小数（`round(4)`）→ 理论上界 `PRODUCTION_TRACE_TOL = 5e-05`。
    """
    ref = load_production_signal()
    idx = incumbent_factor.dropna().index.intersection(ref.index)
    d = float((incumbent_factor.reindex(idx) - ref.reindex(idx)).abs().max()) if len(idx) \
        else float("nan")
    return {"max_abs_diff": d, "n_overlap": int(len(idx)),
            "tolerance": PRODUCTION_TRACE_TOL,
            "within_tolerance": bool(np.isfinite(d) and d <= PRODUCTION_TRACE_TOL),
            "production_csv": PRODUCTION_CSV}


# ---------------------------------------------------------------- 前置诊断闸
def partner_gate(div_factor: pd.Series, incumbent_factor: pd.Series,
                 pair_factors: dict[str, pd.Series], prices: pd.DataFrame,
                 legs: tuple[str, str] = DIVIDEND_LEG,
                 threshold: float = DIV_CORR_GATE) -> dict:
    """预登记 §3.1 的前置诊断闸：红利腿 vs 现役四对族 corr > 阈值 → 同源、不测、出局。

    "现役四对因子"取**最保守读法** = max(|corr| vs 合成因子 A, |corr| vs 四个分量因子)，
    与 Batch 11 `pair_set_probe.dividend_gate` **逐字段同口径**（判例
    `test_partner_gate_reproduces_batch11_dividend_gate_on_identical_inputs` 钉死），
    差别只在腿的取法可传参 —— 那边把腿硬编码成 000922/H00922，这里是 300/红利。
    字段名刻意保持一致，便于与 Batch 11 的读数头对头 diff。
    """
    idx = div_factor.dropna().index.intersection(incumbent_factor.dropna().index)
    corr_agg = float(div_factor.reindex(idx).corr(incumbent_factor.reindex(idx)))
    corr_pairs = {k: float(div_factor.reindex(idx).corr(v.reindex(idx)))
                  for k, v in pair_factors.items() if k != DIVIDEND_LEG_KEY}
    worst = max([abs(corr_agg)] + [abs(v) for v in corr_pairs.values()])

    left, right = legs
    lv, rv = prices[left], prices[right]
    dret = pd.DataFrame({"l": lv.pct_change(), "r": rv.pct_change()}).dropna()
    ratio = (rv / lv).dropna()
    years = (ratio.index[-1] - ratio.index[0]).days / 365.25
    rel = dret["r"] - dret["l"]
    return {
        # ⚠️ 对齐后**面板**的长度（= 八条风格指数的日历），不是伙伴腿自身的行数。
        "n_days_aligned_panel": int(len(prices)),
        # 相关性实际用到的**重叠天数**（预登记要求"判定入档含覆盖天数"）。
        "n_days_factor_overlap": int(len(idx)),
        "first_date": str(prices.index.min().date()),
        "last_date": str(prices.index.max().date()),
        "corr_vs_incumbent_aggregate": corr_agg,
        **{f"corr_vs_pair_{k}": v for k, v in corr_pairs.items()},
        "max_abs_corr_vs_incumbent_family": worst,
        "threshold": threshold,
        "same_source_verdict": bool(worst > threshold),
        # —— 两腿的真实关系（Batch 11 的 PR/TR 退化判别证据，这次预期读数完全不同）——
        "daily_return_corr_between_two_legs": float(dret["l"].corr(dret["r"])),
        "level_ratio_first": float(ratio.iloc[0]),
        "level_ratio_last": float(ratio.iloc[-1]),
        "implied_annual_drift_of_ratio": float((ratio.iloc[-1] / ratio.iloc[0])
                                               ** (1.0 / years) - 1.0),
        "relative_return_mean_daily": float(rel.mean()),
        "relative_return_annualized": float(rel.mean() * 245),
        "relative_return_std_daily": float(rel.std(ddof=1)),
        "relative_return_share_negative_days": float((rel < 0).mean()),
        "relative_return_share_abs_gt_50bp": float((rel.abs() > 0.005).mean()),
    }


# ---------------------------------------------------------------- 面板（房规口径）
def build_panel(factors: dict[str, pd.Series], und: dict[str, pd.Series],
                carry: dict[str, pd.Series], common: pd.DatetimeIndex,
                cost_bps: float) -> pd.DataFrame:
    """集合 × 映射 × 三口径 × 五窗（blend + longflat 是主判据格，其余为报告列）。

    逐窗指标走 Batch 11 的 `evaluate_windows`（**同一支函数**）；carry 按口径各取各的
    （500→IC、1000→IM、blend→50/50）——混用会与 ①a/①b 不同秤。
    """
    rows = []
    for name, fac in factors.items():
        f = fac.reindex(common)
        for mapping in MAPPINGS:
            pos = map_position(f, mapping)
            for kj in KOU_JING_REPORT:
                met = evaluate_windows(pos, und[kj], carry[kj], cost_bps)
                row = {"pair_set": name, "mapping": mapping, "kou_jing": kj,
                       "carry_source": kj, "n_pairs": len(pair_configs_for(name)),
                       "is_incumbent": name == INCUMBENT}
                for wname, met_w in met.items():
                    for key, val in met_w.items():
                        row[f"{key}_{wname}"] = val
                row["worst_tv_sharpe"] = min(met[STAT_WINDOWS[0]]["net_sharpe"],
                                             met[STAT_WINDOWS[1]]["net_sharpe"])
                rows.append(row)
    return pd.DataFrame(rows)


# ---------------------------------------------------------------- 闸门 ⓪：非重叠 rank IC
def build_ic_report(factors: dict[str, pd.Series], und: pd.Series, k: int = K_FORWARD,
                    offset: int = IC_OFFSET, n_boot: int = N_BOOT, seed: int = SEED,
                    alpha: float = GATE0_ALPHA) -> tuple[pd.DataFrame, pd.DataFrame]:
    """闸门 ⓪：两个集合在**同一张非重叠 k 日网格**上的非偏 rank IC + 配对差检验。

    `alpha` 默认 **0.05（95% CI）** —— 单候选，预登记 §3.2 明写"无 Bonferroni"。
    配对差走 `fusion_probe.paired_ic_bootstrap`：**同一组抽样行同时作用于两侧**。
    """
    fwd = forward_return(und, k).dropna()
    common = fwd.index
    for f in factors.values():
        common = common.intersection(f.dropna().index)
    common = common.sort_values()

    ic_rows, diff_rows = [], []
    for win, bounds in WINDOWS_REPORT.items():
        idx_w = _slice(pd.Series(0.0, index=common), bounds).index
        grid = nonoverlap_grid(idx_w, k, offset)
        if len(grid) < 10:
            continue
        y = fwd.reindex(grid)
        cols = {name: f.reindex(grid) for name, f in factors.items()}
        for name, x in cols.items():
            ic_rows.append({"window": win, "pair_set": name, "k": k, "offset": offset,
                            **rank_ic(x, y)})
        for challenger in CHALLENGERS:
            boot = paired_ic_bootstrap(cols[challenger], cols[INCUMBENT], y,
                                       n=n_boot, seed=seed, alpha=alpha)
            diff_rows.append({"window": win, "challenger": challenger,
                              "reference": INCUMBENT, "k": k, "offset": offset,
                              "alpha": alpha, "bonferroni_m": BONFERRONI_M, **boot})
    return pd.DataFrame(ic_rows), pd.DataFrame(diff_rows)


# ---------------------------------------------------------------- 闸门判定
def evaluate_gates(panel_blend_lf: pd.DataFrame, ic_rep: pd.DataFrame,
                   ic_diff: pd.DataFrame, partner_diag: dict) -> pd.DataFrame:
    """预登记 §3 逐条判定，**行序 = 判据顺序**：前置诊断闸 → ⓪ → 收益层 → OVERALL。

    - **前置诊断闸**：`max|corr|` ≤ 0.9（同源判定为真 → 不过）；
    - **闸门 ⓪**：train 与 val **双窗 IC 均 > 现役**，**且**配对 IC 差的 95% CI
      在 **train 或 val** 窗排除 0（"明确优于而非平手"，读法逐字沿用 ③ / Batch 11）；
    - **收益层 ①**：`worst(train,val)` 净 Sharpe ≥ 现役 +0.15（blend / long-flat）；
    - **收益层 ②**：**选择窗**（2014-2023）年化换手 ≤ 现役同窗值；
    - **收益层 ③**：**选择窗**剔最强年 Sharpe（`yearly.concentration_summary`）≥ 现役同窗值。

    与 Batch 11 的**唯一行为差异**（预登记 §3.5 明写）：前置诊断闸不过时**不短路**，
    收益层读数仍全表入档供关帐引用 —— `OVERALL` 仍然按"前一道不过即 STOP"判 False，
    并另出一行 `first_failed_gate` 诊断（`pass=None`，不进 OVERALL）。
    `pass=None` 的行一律是诊断 / 对照量。
    """
    blend = panel_blend_lf.set_index("pair_set")
    inc = blend.loc[INCUMBENT]
    cand = blend.loc[CANDIDATE]

    def ic_of(name, win):
        hit = ic_rep[(ic_rep["pair_set"] == name) & (ic_rep["window"] == win)]["ic"]
        return float(hit.iloc[0]) if len(hit) else float("nan")

    def diff_of(name, win, col):
        hit = ic_diff[(ic_diff["challenger"] == name) & (ic_diff["window"] == win)][col]
        return hit.iloc[0] if len(hit) else float("nan")

    def row(gate, metric, value, threshold, passed):
        return {"pair_set": CANDIDATE, "gate": gate, "metric": metric,
                "value": value, "threshold": threshold, "pass": passed}

    rows = []
    # ---- 前置诊断闸（最先）
    pre = not bool(partner_diag["same_source_verdict"])
    rows.append(row("PRE_partner_corr", "max|corr| vs 现役四对族",
                    float(partner_diag["max_abs_corr_vs_incumbent_family"]),
                    DIV_CORR_GATE, pre))
    rows.append(row("PRE_partner_corr", "相关性覆盖天数（诊断）",
                    float(partner_diag.get("n_days_factor_overlap", float("nan"))),
                    float("nan"), None))

    # ---- 闸门 ⓪（次）
    higher = {w: bool(ic_of(CANDIDATE, w) > ic_of(INCUMBENT, w)) for w in STAT_WINDOWS}
    for w in STAT_WINDOWS:
        rows.append(row("0_rank_ic", f"rank_ic_{w}", ic_of(CANDIDATE, w),
                        ic_of(INCUMBENT, w), higher[w]))
    sig = any(bool(diff_of(CANDIDATE, w, "ci_excludes_zero")) for w in STAT_WINDOWS)
    rows.append(row("0_rank_ic",
                    f"paired_ic_diff_CI_excl_0(train或val窗, α={GATE0_ALPHA}, 无 Bonferroni)",
                    float(sig), 1.0, sig))
    gate0 = bool(all(higher.values()) and sig)
    rows.append(row("0_rank_ic", "闸门⓪汇总", float(gate0), 1.0, gate0))

    # ---- 收益层（最后；即便前两道已失手也全表入档，预登记 §3.5）
    g1 = bool(cand["worst_tv_sharpe"] >= inc["worst_tv_sharpe"] + GATE_WORST_TV_LIFT)
    rows.append(row("1_worst_tv_sharpe", "worst(train,val) 净 Sharpe",
                    float(cand["worst_tv_sharpe"]),
                    float(inc["worst_tv_sharpe"]) + GATE_WORST_TV_LIFT, g1))
    rows.append(row("1_worst_tv_sharpe", "margin_vs_incumbent",
                    float(cand["worst_tv_sharpe"] - inc["worst_tv_sharpe"]),
                    GATE_WORST_TV_LIFT, g1))
    g2 = bool(cand["turnover_selection_2014_2023"] <= inc["turnover_selection_2014_2023"])
    rows.append(row("2_turnover_not_up", "选择窗年化换手",
                    float(cand["turnover_selection_2014_2023"]),
                    float(inc["turnover_selection_2014_2023"]), g2))
    g3 = bool(cand["sharpe_ex_top1_selection_2014_2023"]
              >= inc["sharpe_ex_top1_selection_2014_2023"])
    rows.append(row("3_concentration", "选择窗剔最强年 Sharpe",
                    float(cand["sharpe_ex_top1_selection_2014_2023"]),
                    float(inc["sharpe_ex_top1_selection_2014_2023"]), g3))
    rows.append(row("3_concentration", "剔最强两年 Sharpe（诊断）",
                    float(cand["sharpe_ex_top2_selection_2014_2023"]),
                    float(inc["sharpe_ex_top2_selection_2014_2023"]), None))

    outcome = dict(zip(GATE_ORDER, (pre, gate0, g1, g2, g3)))
    first_failed = next((name for name in GATE_ORDER if not outcome[name]), None)
    rows.append(row("ORDER", f"first_failed_gate={first_failed}",
                    float("nan"), float("nan"), None))
    overall = first_failed is None
    rows.append(row("OVERALL",
                    "GO（前置+⓪+收益层三条全中）" if overall
                    else f"STOP（判据顺序在 {first_failed} 终止；其后读数仅入档不改裁决）",
                    float(overall), float("nan"), overall))
    return pd.DataFrame(rows)


def first_failed_gate(verdict: pd.DataFrame) -> str | None:
    """按**判据顺序**读出第一个未过的闸（全过 → None）。诊断行（`pass=None`）不参与。

    从判定表反推而不是另存状态：`evaluate` / 报告引用的"首个未过"与 `verdict.csv`
    里那张表**必然同源**，不会出现两处措辞打架。
    """
    for gate in GATE_ORDER:
        hit = verdict[(verdict["gate"] == gate) & verdict["pass"].notna()]
        if len(hit) and not bool(hit["pass"].all()):
            return gate
    return None


def build_metadata(summary: dict) -> pd.DataFrame:
    run = summary["run"]
    items = {
        "verdict": summary["verdict"],
        "first_failed_gate": summary["first_failed_gate"],
        "partner_same_source_verdict": summary["partner_gate"]["same_source_verdict"],
        "partner_max_abs_corr": summary["partner_gate"]["max_abs_corr_vs_incumbent_family"],
        "partner_corr_overlap_days": summary["partner_gate"]["n_days_factor_overlap"],
        **{f"worst_tv_sharpe_{k}": v for k, v in summary["worst_tv_sharpe"].items()},
        **{k: run[k] for k in (
            "n_boot", "seed", "alpha", "bonferroni_m", "cost_bps", "k_forward",
            "n_obs_selection", "sample_first_date", "sample_last_date",
            "calendar_n_days", "incumbent_vs_production_csv_max_abs_diff",
            "db_host", "carry_policy", "holdout_policy", "deploy_policy",
            "permutation_policy")},
    }
    return pd.DataFrame([{"pair_set": "-", "gate": "META", "metric": k, "value": v,
                          "threshold": float("nan"), "pass": None}
                         for k, v in items.items()])


# ---------------------------------------------------------------- 编排（纯计算，可离线冒烟）
def evaluate(prices: pd.DataFrame, und: dict[str, pd.Series],
             carry: dict[str, pd.Series], n_boot: int = N_BOOT, seed: int = SEED,
             cost_bps: float = 3.0, coverage: dict | None = None,
             db_host: str = "settings.yaml default") -> dict:
    """价格 + 收益/carry → 全部读数与判定（**不连库**，便于离线冒烟与独立复算）。"""
    factors = {name: build_factor(prices, name) for name in SETS}
    pair_factors = build_pair_factors(prices)
    trace = production_trace_diff(factors[INCUMBENT])
    if not trace["within_tolerance"]:
        print(f"警告: 现役 A 重建 vs 生产 CSV max|diff|={trace['max_abs_diff']:.3e} "
              f"> {PRODUCTION_TRACE_TOL:.1e}（溯源锚点异常，读数请人工核）", file=sys.stderr)

    diag = partner_gate(pair_factors[DIVIDEND_LEG_KEY], factors[INCUMBENT],
                        pair_factors, prices)

    common = factors[INCUMBENT].dropna().index
    for f in factors.values():
        common = common.intersection(f.dropna().index)
    common = common.intersection(und[PRIMARY_KOU_JING].index).sort_values()
    common_sel = common[(common >= pd.Timestamp(SELECTION[0]))
                        & (common <= pd.Timestamp(SELECTION[1]))]

    panel = build_panel(factors, und, carry, common, cost_bps)
    ic_rep, ic_diff = build_ic_report(factors, und[PRIMARY_KOU_JING], k=K_FORWARD,
                                      offset=IC_OFFSET, n_boot=n_boot, seed=seed)
    blend_lf = panel[(panel["kou_jing"] == PRIMARY_KOU_JING)
                     & (panel["mapping"] == "longflat")]
    verdict = evaluate_gates(blend_lf, ic_rep, ic_diff, diag)

    overall = verdict[verdict["gate"] == "OVERALL"]
    go = bool(overall["pass"].iloc[0]) if len(overall) else False
    first_failed = first_failed_gate(verdict)
    assert go == (first_failed is None), "OVERALL 与判据顺序读数不一致"

    def _by_window(name, mapping="longflat", kj=PRIMARY_KOU_JING) -> dict:
        r = panel[(panel["pair_set"] == name) & (panel["mapping"] == mapping)
                  & (panel["kou_jing"] == kj)].iloc[0]
        return {w: {k: (float(r[f"{k}_{w}"]) if pd.notna(r.get(f"{k}_{w}")) else None)
                    for k in ("net_sharpe", "net_ann", "maxdd", "calmar", "turnover",
                              "hit", "long_frac", "sharpe_ex_top1", "sharpe_ex_top2")}
                for w in WINDOWS_REPORT if f"net_sharpe_{w}" in r}

    summary = {
        "PROBE": "候选 ⑤b 红利腿换伙伴补测（单候选 E = 现役四对 + 沪深300/中证红利腿）",
        "prereg": "docs/plans/2026-08-13-probe-5b-dividend-partner-prereg.md（§0-§5 冻结）",
        "verdict": "GO" if go else "STOP",
        "first_failed_gate": first_failed,
        "sets": {k: list(v) for k, v in SETS.items()},
        "dividend_leg": {"left_column": DIVIDEND_LEG[0], "right_column": DIVIDEND_LEG[1],
                         "codes": PARTNER_CODES,
                         "sign_prior": "+z = 宽基跑赢红利 = 风险偏好上行（与成长腿同号）",
                         "note": "两条都是价格指数；全收益版（H00922）是 Batch 11 的教训，本次不用"},
        "not_tested_registered": {
            "其他伙伴构造": "000905 / 合成代理 / 885000.WI —— 预登记 §1 明示不测",
            "权重优化": "预登记明写不做（永久）",
            "参数网格": "lookback/z_window/smoothing 锁死在现役生产值，不进任何网格",
            "5 对 / 6 对配置": "不在本次预登记内",
        },
        "coverage_preflight": coverage,
        "partner_gate": diag,
        "production_trace": trace,
        "worst_tv_sharpe": {v: float(blend_lf.set_index("pair_set").loc[v, "worst_tv_sharpe"])
                            for v in blend_lf["pair_set"]},
        "by_window": {v: _by_window(v) for v in factors},
        "gate_readings": {
            "order": "前置诊断闸 → ⓪ → 收益层；前一道不过即 STOP，收益层读数仍全表入档",
            "pre": "红利腿 z 序列 vs 现役合成因子 A 及四对分量 max|corr| > 0.9 → 同源出局",
            "gate0": "非重叠 20 日 rank IC：E 在 train 与 val 双窗均 > A，且配对 IC 差的 "
                     "95% CI 在 train 或 val 窗排除 0。单候选 → α=0.05，**无 Bonferroni**",
            "gate1": "worst(train,val) 净 Sharpe ≥ 现役 +0.15（blend / long-flat）",
            "gate2": "选择窗（2014-2023）年化换手 ≤ 现役同窗值",
            "gate3": "选择窗剔最强年 Sharpe（yearly.concentration_summary）≥ 现役同窗值",
            "family_tax": "E 是 ⑤ 家族第 4 个挑战者集合（B/C/D 已 STOP）——任何 GO 的引用"
                          "必须带'家族第 4 次尝试'的上下文，且 GO 只触发另立部署批次+用户拍板",
        },
        "run": {
            "n_boot": n_boot, "seed": seed, "cost_bps": cost_bps,
            "k_forward": K_FORWARD, "ic_offset": IC_OFFSET,
            "alpha": GATE0_ALPHA, "bonferroni_m": BONFERRONI_M,
            "lookback": LOOKBACK, "z_window": Z_WINDOW, "smoothing": SMOOTHING,
            "n_obs_selection": len(common_sel), "selection_window": list(SELECTION),
            "sample_first_date": str(common[0].date()) if len(common) else None,
            "sample_last_date": str(common[-1].date()) if len(common) else None,
            "n_obs_full": len(common),
            "calendar_n_days": int(len(prices)),
            "incumbent_vs_production_csv_max_abs_diff": trace["max_abs_diff"],
            "db_host": db_host,
            "carry_policy": "每口径各用各的 carry（500→IC、1000→IM、blend→50/50）；"
                            "闸门只用 blend",
            "holdout_policy": "2024-2026 只报告不选择、不进任何闸门（预登记 §3.4）",
            "permutation_policy": "单候选无选择集 → 不跑 selection_permutation（预登记 §3.5）",
            "deploy_policy": "不改 deploy/、signals/、production.py；GO 只出立项草案，用户拍板",
        },
    }
    diagnostics = pd.DataFrame([{"metric": k, "value": v} for k, v in diag.items()])
    return {"panel": panel, "ic": pd.concat([ic_rep.assign(row="level"),
                                             ic_diff.assign(row="diff")],
                                            ignore_index=True),
            "diagnostics": diagnostics, "verdict": verdict, "summary": summary,
            "factors": factors, "pair_factors": pair_factors}


def run_probe(n_boot: int = N_BOOT, seed: int = SEED, cost_bps: float = 3.0,
              db=None) -> dict:
    """正式跑：只读库 → preflight → `evaluate`。**preflight 有洞即抛，无绕过开关。**"""
    from backtest.data import load_carry, load_underlying_returns

    prices = load_prices(db)
    coverage = check_calendar_coverage(prices)
    und = {kj: load_underlying_returns(kj, db=db) for kj in KOU_JING_REPORT}
    carry = {kj: load_carry(kj, db=db) for kj in KOU_JING_REPORT}
    return evaluate(prices, und, carry, n_boot=n_boot, seed=seed, cost_bps=cost_bps,
                    coverage=coverage,
                    db_host=(db or {}).get("host", "settings.yaml default"))


# ---------------------------------------------------------------- CLI
def main() -> int:
    ap = argparse.ArgumentParser(
        description="⑤b 红利腿换伙伴补测（单候选 E = 现役四对 + 沪深300/中证红利腿）")
    ap.add_argument("--n-boot", type=int, default=N_BOOT, help="配对 IC 差 bootstrap 次数")
    ap.add_argument("--seed", type=int, default=SEED)
    ap.add_argument("--cost-bps", type=float, default=3.0)
    ap.add_argument("--db-host", default=None, help="覆盖库主机（主库拒连时走只读备端）")
    args = ap.parse_args()

    db = None
    if args.db_host:
        from signals.common.config import load_db_config
        db = dict(load_db_config())
        db["host"] = args.db_host

    out = run_probe(n_boot=args.n_boot, seed=args.seed, cost_bps=args.cost_bps, db=db)
    out_dir = ROOT / "backtest" / "output"
    out_dir.mkdir(parents=True, exist_ok=True)
    out["panel"].round(6).to_csv(out_dir / f"{OUTPUT_PREFIX}_panel.csv", index=False)
    out["ic"].round(6).to_csv(out_dir / f"{OUTPUT_PREFIX}_ic.csv", index=False)
    out["diagnostics"].to_csv(out_dir / f"{OUTPUT_PREFIX}_diagnostics.csv", index=False)
    pd.concat([out["verdict"], build_metadata(out["summary"])], ignore_index=True).to_csv(
        out_dir / f"{OUTPUT_PREFIX}_verdict.csv", index=False)
    side = out_dir / f"{OUTPUT_PREFIX}_summary.json"
    side.write_text(json.dumps(out["summary"], ensure_ascii=False, indent=2, default=str)
                    + "\n", encoding="utf-8")

    d = out["summary"]["partner_gate"]
    print("=== 前置诊断闸（沪深300 vs 中证红利，两条均为价格指数） ===")
    print(f"  对齐后面板 {d['n_days_aligned_panel']} 天（{d['first_date']} ~ {d['last_date']}）；"
          f"相关性覆盖 {d['n_days_factor_overlap']} 天；"
          f"两腿日收益 corr={d['daily_return_corr_between_two_legs']:.4f}；"
          f"净值比 {d['level_ratio_first']:.3f}→{d['level_ratio_last']:.3f}"
          f"（年化漂移 {100 * d['implied_annual_drift_of_ratio']:.2f}%）")
    print(f"  红利腿因子 vs 现役族 max|corr|={d['max_abs_corr_vs_incumbent_family']:.4f} "
          f"（阈值 {DIV_CORR_GATE}）→ 同源判定 = {d['same_source_verdict']}")

    print("\n=== 闸门 ⓪：非重叠 20 日非偏 rank IC（blend·同网格同秤，α=0.05 无 Bonferroni） ===")
    lvl = out["ic"][out["ic"]["row"] == "level"]
    print(lvl.pivot_table(index="pair_set", columns="window", values="ic")
          .round(4).to_string())
    dif = out["ic"][out["ic"]["row"] == "diff"]
    print("\n配对 IC 差（E − A，95% CI）")
    print(dif[["window", "challenger", "diff_ic", "ci_lo", "ci_hi", "p_value",
               "ci_excludes_zero", "n_obs"]].round(4).to_string(index=False))

    blend = out["panel"][(out["panel"]["kou_jing"] == PRIMARY_KOU_JING)
                         & (out["panel"]["mapping"] == "longflat")]
    print("\n=== 收益层（blend · long-flat · 3bps + carry） ===")
    cols = ["pair_set", "n_pairs", "worst_tv_sharpe", "net_sharpe_train_2014_2020",
            "net_sharpe_val_2021_2023", "net_sharpe_holdout_2024_2026",
            "turnover_selection_2014_2023", "maxdd_selection_2014_2023",
            "sharpe_ex_top1_selection_2014_2023"]
    print(blend[[c for c in cols if c in blend.columns]].round(4).to_string(index=False))

    print("\n=== 判据逐条（顺序：前置诊断闸 → ⓪ → 收益层） ===")
    print(out["verdict"].round(6).to_string(index=False))
    v = out["summary"]["verdict"]
    print(f"\n{'★ GO' if v == 'GO' else '✗ STOP'}"
          f"（首个未过：{out['summary']['first_failed_gate']}）")
    print("⚠️ E 是 ⑤ 家族第 4 个挑战者集合（B/C/D 已 STOP）——序贯家族税须随读数一起引用。")
    print("⚠️ holdout 2024-2026 只报告不进闸门；本探针不动任何生产文件。")
    print(f"→ {out_dir}/{OUTPUT_PREFIX}_{{panel,ic,diagnostics,verdict}}.csv\n→ {side}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
