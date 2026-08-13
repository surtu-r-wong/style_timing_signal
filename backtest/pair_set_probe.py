"""⑤ 探针：**配对集合**的 OOS 裁决（A/B/C 三对照）+ 中证红利腿（D）的诊断闸。

预登记出处：`docs/plans/2026-08-12-signal-research-agenda.md` §3 ⑤(f)——
> 子集集合**预登记为 4 个**（四对等权[现役] / 500+1000+2000 / 单 1000 对 / 四对+红利腿），
> **不做权重优化**（扫权重必过拟合）。
> **⓪ 先过同秤头对头**：候选集合 vs 现役四对的**非偏 rank IC**（同 harness、同窗、非重叠 k 日），
> **先于收益层判据**（§4 C5 指定给"替换类命题"的那杆秤）。
> **GO（换配对集合）**：worst(train,val) Sharpe ≥ 现役 **+0.15** **且**换手不增**且**集中度不恶化
> （剔最强年后仍不劣）。否则 → 维持四对等权并归档。
> 红利腿单独先过一道诊断闸：与现役四对因子的 corr **> 0.9 即直接判"同源、不测"**（省成本）。
> **预算上限**：2 个工作日。

**双前置**：
1. **C1 机器债** = 候选 ⓪ `backtest/selection_permutation.py`（commit `2d716db`）**已付清**；
2. **用户放行** = 议程 §7.1 问 4「⑤'集合裁决≠回头调参'……**都认**；⑤ **排队**」。
   放行不免闸：选择端只用 train/val，**2024-2026 只报告不选择、不进任何闸门**（§7.3 第 3 条）。

## 预登记的 4 个集合（**不得扩、不得改**）

| 键 | 配对 | 角色 |
|---|---|---|
| `A_four_pairs_incumbent` | 300 / 500 / 1000 / 2000 | **现役**（`config_4pairs.csv` 逐行同构），对照点 |
| `B_500_1000_2000` | 500 / 1000 / 2000 | 设计稿 §2.2「300 对不入等权」那条从未裁决的建议 |
| `C_1000_only` | 1000 | 设计稿 L4 登记的"单 1000 对" |
| `D_four_plus_dividend` | 300 / 500 / 1000 / 2000 + 红利腿 | 库内已有、从未消费的风格腿 |

**不做权重优化**：四个集合一律**组内等权**（`calculate_contrast_equal_weight_signal` 的
`raw_signal /= len(pair_signals)`），参数一律锁死在现役生产值 lookback=20 / z_window=40 /
smoothing=5 —— 参数维度**不进网格**，本探针只动"配对集合"这一个维度。

**885000.WI 偏股基金腿不在预登记集合内 → 不测**（登记留档，见探针文档"未测项登记"）。

## 机器接入三规矩（`2026-08-12-selection-permutation-machine.md` §6 + 上三批 QA）

1. **多候选合成单次调用**：4 个集合（含现役对照点）进**同一次** `selection_permutation_test`；
   两个统计量族（收益层 worst-tv Sharpe / 闸门 ⓪ 的 worst-tv rank IC）各**一次**调用，
   族内合成、族间是**串联 AND 闸**（两族都要过才 GO）→ 串联不放大假阳率。
   闸门 ⓪ 的配对 IC 差检验**另按预登记 Bonferroni α=0.05/3**（3 个候选集合）；
2. `-inf` **只给无法评分的行**（样本不足 / 统计量非有限）。换手 / 集中度 / IC 三条判据
   都是**独立后置闸门**，**不编进统计量**（机器文档 §9 第 11 条边界）；
3. **移位区间 `min_shift = 2·max(k) = 40`、`max_shift = n_obs − 40`**（房规 [2k, n−2k]，
   k = 前瞻天数 20）。收益层统计量本身无前瞻窗，此处沿用同一区间是**更保守**的取法
   （下界更大 → 残留配对更少），并让两族共用同一份索引矩阵、可逐行对表。

## 口径（§4 C4 同秤）

- 因子：冻结根 `signals/equal_weight/generate_signal.py` 的
  `calculate_contrast_equal_weight_signal`（**逐字复用**，只换 `pair_configs`）；
- 价格：只读 PG `stock_selector.index_daily`（八条风格指数走 `signals/common/data_source.py`
  的房规读取；红利两码 `signals/common/index_codes.csv` 里**没有映射**，
  故本模块自带一支**只读**直查，**不改冻结根的映射表**）；
- 收益 / carry / 引擎：`backtest.data` + `backtest.engine.run_strategy`（3 bps/边 + 逐口径 carry，
  T+1 生效、全日历），与 ①a / ①b / ② / ③ **同秤**；
- 部署映射 = `positions.production_position`（long-flat, θ=0）——**闸门只读 long-flat**；
  对称口径同表报告，**不进闸门**（①a：long-flat 是风险偏好选择，**无收益优势的统计证据**）；
- 向量化引擎 `net_return_matrix` / `sharpe_rows` 与非重叠 IC 的四支纯函数
  （`forward_return` / `nonoverlap_grid` / `rank_ic` / `paired_ic_bootstrap` / `spearman_rows`）
  **直接 import 既有探针**（①b / ③），不建平行 harness。

CLI: python3 -m backtest.pair_set_probe [--n-perm 1000] [--seed 0] [--db-host 100.75.102.44]
产出: backtest/output/probe_5_pairset_{panel,ic,permutation,diagnostics,verdict}.csv
      + probe_5_pairset_summary.json（sidecar）

本模块**不改动** `signals/`（含 `index_codes.csv` / `config_4pairs.csv`）/ `positions.py` /
`engine.py` / `selection_permutation.py` / 任何既有探针 / 任何生产文件，只读库与只读 committed CSV。
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
from backtest.mapping_probe import net_return_matrix, sharpe_rows  # noqa: E402
from backtest.selection_permutation import selection_permutation_test  # noqa: E402
from signals.equal_weight.generate_signal import PairConfig  # noqa: E402

# ---------------------------------------------------------------- 预登记常量（不得扩）
# 中文列名 → (成长腿, 价值腿)。前四行与 `signals/equal_weight/config_4pairs.csv` 逐行同构。
PAIR_LEGS = {
    "300": ("沪深300成长", "沪深300价值"),
    "500": ("中证500成长", "中证500价值"),
    "1000": ("中证1000成长", "中证1000价值"),
    "2000": ("中证2000成长", "中证2000价值"),
    "dividend": ("中证红利", "中证红利全收益"),      # 预登记原文的 000922 / H00922 顺序
}
STYLE_NAMES = [n for k in ("300", "500", "1000", "2000") for n in PAIR_LEGS[k]]
DIVIDEND_CODES = {"000922.CSI": "中证红利", "H00922.CSI": "中证红利全收益"}

SETS = {
    "A_four_pairs_incumbent": ("300", "500", "1000", "2000"),
    "B_500_1000_2000": ("500", "1000", "2000"),
    "C_1000_only": ("1000",),
    "D_four_plus_dividend": ("300", "500", "1000", "2000", "dividend"),
}
INCUMBENT = "A_four_pairs_incumbent"
CHALLENGERS = ("B_500_1000_2000", "C_1000_only", "D_four_plus_dividend")
DIVIDEND_SET = "D_four_plus_dividend"
# 诊断用（**不进闸门、不进机器**）：红利腿方向反转（H00922 在左），见文档"自由裁量点"。
DIAG_SET = "D_rev_dividend_reversed"

# 参数锁死在现役生产值 —— 参数维度不进本探针的任何网格
LOOKBACK, Z_WINDOW, SMOOTHING = 20, 40, 5
PRODUCTION_CSV = "output/equal_weight/equal_weight_signal_20d40z.csv"
PRODUCTION_COL = "factor_value"

PRIMARY_KOU_JING = "blend"
KOU_JING_REPORT = ("500", "1000", "blend")
MAPPINGS = ("longflat", "symmetric")          # 闸门只读 longflat；symmetric 仅报告

SELECTION = ("2014-01-01", "2023-12-31")
WINDOWS_REPORT = {
    "selection_2014_2023": SELECTION,
    "train_2014_2020": ("2014-01-01", "2020-12-31"),
    "val_2021_2023": ("2021-01-01", "2023-12-31"),
    "holdout_2024_2026": ("2024-01-01", "2026-12-31"),
    "full_2014_2026": (None, None),
}
STAT_WINDOWS = ("train_2014_2020", "val_2021_2023")

# 判据阈值（⑤(f) 原文，**不得改**）
K_FORWARD = 20                 # 非重叠前瞻天数（③ 同值，同秤复用）
IC_OFFSET = 0                  # 非重叠抽样起点
GATE_WORST_TV_LIFT = 0.15      # 收益层判据 ①：worst(train,val) Sharpe ≥ 现役 +0.15
DIV_CORR_GATE = 0.9            # D 前置诊断闸：corr > 0.9 → 同源、不测
BONFERRONI_M = 3               # 预登记 3 个候选集合 → α = 0.05/3
ALPHA = 0.05
_MIN_WINDOW_OBS = 60


# ---------------------------------------------------------------- 纯函数：集合 → 配对配置
def pair_configs_for(set_name: str) -> list[PairConfig]:
    """预登记集合 → `PairConfig` 列表（**组号从 1 连续重编**，冻结根的校验要求）。

    `direction="forward"` 逐行照抄 `config_4pairs.csv`；红利腿按预登记原文
    "000922/H00922" 的**书写顺序**取 left/right（自由裁量点，见探针文档）。
    """
    if set_name == DIAG_SET:
        keys = SETS[DIVIDEND_SET]
    elif set_name in SETS:
        keys = SETS[set_name]
    else:
        raise KeyError(f"未预登记的集合: {set_name!r}（预登记恰 4 个：{list(SETS)}）")
    out = []
    for i, key in enumerate(keys, start=1):
        left, right = PAIR_LEGS[key]
        if set_name == DIAG_SET and key == "dividend":
            left, right = right, left       # 诊断变体：红利腿方向反转
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
    for key in PAIR_LEGS:
        left, right = PAIR_LEGS[key]
        frame = calculate_contrast_equal_weight_signal(
            prices, lookback=LOOKBACK, z_window=Z_WINDOW, smoothing_window=SMOOTHING,
            pair_configs=[PairConfig(group=1, left_column=left, right_column=right,
                                     direction="forward")])
        out[key] = frame[PRODUCTION_COL].astype(float)
    return out


# ---------------------------------------------------------------- 数据装载（只读）
def load_dividend_closes(db: dict, codes: dict[str, str] | None = None) -> pd.DataFrame:
    """红利两码收盘价（只读单查询，带连接/语句超时）。

    存在理由：`signals/common/index_codes.csv` 里**没有** 000922 / H00922 的映射，
    而该文件是冻结根 —— 加能力走**包装器**而不是就地改表
    （`constraint-stock-selector-legacy-roots`）。本函数只 SELECT，无任何写入。
    """
    import psycopg2

    codes = codes or DIVIDEND_CODES
    conn = psycopg2.connect(
        host=db["host"], port=db["port"], dbname=db["name"],
        user=db["user"], password=db["password"], connect_timeout=10)
    try:
        with conn.cursor() as cur:
            cur.execute("SET statement_timeout='30s'")
            cur.execute(
                f"""SELECT index_code, trade_date, close FROM {db['schema']}.index_daily
                    WHERE index_code = ANY(%s) ORDER BY trade_date""",
                (list(codes),))
            rows = cur.fetchall()
    finally:
        conn.close()
    df = pd.DataFrame(rows, columns=["index_code", "date", "close"])
    missing = [c for c in codes if c not in set(df["index_code"])]
    if missing:
        raise ValueError(f"index_daily 中以下代码无数据: {missing}")
    wide = df.pivot(index="date", columns="index_code", values="close").astype(float)
    wide.index = pd.to_datetime(wide.index)
    return wide.sort_index()[list(codes)].rename(columns=codes)


def load_prices(db: dict | None = None) -> pd.DataFrame:
    """八条风格 + 红利两码 → **同一张日历**的宽表（日历 = 八条风格指数的交易日）。

    风格八条走房规 `load_pg_closes`（含 NaN 三级策略）；红利两码 reindex 到同一日历。
    实测红利日历是风格日历的**超集**（多一天 2013-12-31）→ 对齐只丢红利那一天，
    **A/B/C 三个集合的因子与"红利在不在表里"完全无关**（列不被引用）。
    """
    from signals.common.config import load_db_config
    from signals.common.data_source import load_pg_closes

    db = db or load_db_config()
    style = load_pg_closes(STYLE_NAMES, db=db)
    div = load_dividend_closes(db)
    joined = style.join(div.reindex(style.index), how="left")
    bad = joined.columns[joined.isna().any()].tolist()
    if bad:
        raise ValueError(f"对齐后仍有缺值列（红利日历未覆盖风格日历）: {bad}")
    return joined


def load_production_signal() -> pd.Series:
    df = pd.read_csv(ROOT / PRODUCTION_CSV, parse_dates=["date"]).set_index("date")
    return df.sort_index()[PRODUCTION_COL].astype(float)


# ---------------------------------------------------------------- 红利腿前置诊断闸
def dividend_gate(div_factor: pd.Series, incumbent_factor: pd.Series,
                  pair_factors: dict[str, pd.Series], prices: pd.DataFrame,
                  threshold: float = DIV_CORR_GATE) -> dict:
    """⑤(f) 的红利腿前置闸：与现役四对因子 corr > 阈值 → 判"同源、不测"，D 出局。

    "现役四对因子"取**最保守读法** = max(|corr| vs 合成因子 A, |corr| vs 四个分量因子)
    —— 任一 > 0.9 即判同源（自由裁量点，见探针文档）。
    同时落盘覆盖天数与"两码是不是同一指数的价格/全收益版"的判别证据。
    """
    idx = div_factor.dropna().index.intersection(incumbent_factor.dropna().index)
    corr_agg = float(div_factor.reindex(idx).corr(incumbent_factor.reindex(idx)))
    corr_pairs = {k: float(div_factor.reindex(idx).corr(v.reindex(idx)))
                  for k, v in pair_factors.items() if k != "dividend"}
    worst = max([abs(corr_agg)] + [abs(v) for v in corr_pairs.values()])

    left, right = PAIR_LEGS["dividend"]
    lv, rv = prices[left], prices[right]
    dret = pd.DataFrame({"l": lv.pct_change(), "r": rv.pct_change()}).dropna()
    ratio = (rv / lv).dropna()
    years = (ratio.index[-1] - ratio.index[0]).days / 365.25
    rel = dret["r"] - dret["l"]
    return {
        "n_days_dividend_leg": int(len(prices)),
        "first_date": str(prices.index.min().date()),
        "last_date": str(prices.index.max().date()),
        "corr_vs_incumbent_aggregate": corr_agg,
        **{f"corr_vs_pair_{k}": v for k, v in corr_pairs.items()},
        "max_abs_corr_vs_incumbent_family": worst,
        "threshold": threshold,
        "same_source_verdict": bool(worst > threshold),
        # —— 两码的真实关系（PR vs TR 判别证据）——
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
def _slice(s: pd.Series, win: tuple) -> pd.Series:
    a, b = win
    out = s
    if a is not None:
        out = out[out.index >= pd.Timestamp(a)]
    if b is not None:
        out = out[out.index <= pd.Timestamp(b)]
    return out


def window_masks(index: pd.DatetimeIndex) -> dict[str, np.ndarray]:
    out = {}
    for name, (a, b) in WINDOWS_REPORT.items():
        m = np.ones(len(index), dtype=bool)
        if a is not None:
            m &= index >= pd.Timestamp(a)
        if b is not None:
            m &= index <= pd.Timestamp(b)
        out[name] = m
    return out


def map_position(factor: pd.Series, mapping: str) -> pd.Series:
    from backtest.positions import production_position, to_position

    if mapping == "longflat":
        return production_position(factor).astype(float)
    if mapping == "symmetric":
        return to_position(factor, mode="discrete").astype(float)
    raise ValueError(f"未知 mapping={mapping!r}，可选 {MAPPINGS}")


def evaluate_windows(pos: pd.Series, und: pd.Series, carry: pd.Series,
                     cost_bps: float) -> dict[str, dict]:
    """逐窗房规指标（切窗后再跑 `run_strategy`，与 ①b / `scan.py` 同做法）。"""
    from backtest.engine import run_strategy
    from backtest.metrics import (
        ann_return, calmar, hit_rate, max_drawdown, sharpe, turnover,
    )
    from backtest.yearly import concentration_summary

    out = {}
    for name, win in WINDOWS_REPORT.items():
        p = _slice(pos, win)
        u = _slice(und, win)
        idx = p.index.intersection(u.index)
        if len(idx) < _MIN_WINDOW_OBS:
            continue
        p, u = p.reindex(idx), u.reindex(idx)
        ret = run_strategy(p, u, cost_bps, carry.reindex(idx))["ret"]
        conc = concentration_summary(ret)
        out[name] = {
            "net_sharpe": sharpe(ret), "net_ann": ann_return(ret),
            "maxdd": max_drawdown(ret), "calmar": calmar(ret),
            "turnover": turnover(p), "hit": hit_rate(ret),
            "long_frac": float((p > 0).mean()), "short_frac": float((p < 0).mean()),
            "sharpe_ex_top1": conc["sharpe_ex_top1"],
            "sharpe_ex_top2": conc["sharpe_ex_top2"],
            "ex_top1_year": conc["ex_top1_year"],
            "n_obs": int(len(ret)),
        }
    return out


def build_panel(factors: dict[str, pd.Series], und: dict[str, pd.Series],
                carry: dict[str, pd.Series], common: pd.DatetimeIndex,
                cost_bps: float) -> pd.DataFrame:
    """集合 × 映射 × 三口径 × 五窗（blend + longflat 是主判据格，其余为报告列）。

    ⚠️ carry **按口径各取各的**（500→IC、1000→IM、blend→50/50）——混用会让次级口径
    与 ①a/①b 不同秤（①b 独立 QA I-1 的修）。
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
                       "is_incumbent": name == INCUMBENT,
                       "is_gated": name in SETS}
                for wname, m in met.items():
                    for key, val in m.items():
                        row[f"{key}_{wname}"] = val
                row["worst_tv_sharpe"] = min(met["train_2014_2020"]["net_sharpe"],
                                             met["val_2021_2023"]["net_sharpe"])
                rows.append(row)
    return pd.DataFrame(rows)


# ---------------------------------------------------------------- 闸门 ⓪：非重叠 rank IC
def build_ic_report(factors: dict[str, pd.Series], und: pd.Series, k: int = K_FORWARD,
                    offset: int = IC_OFFSET, n_boot: int = 10000, seed: int = 0,
                    alpha: float = ALPHA / BONFERRONI_M) -> tuple[pd.DataFrame, pd.DataFrame]:
    """闸门 ⓪：各集合在**同一张非重叠 k 日网格**上的非偏 rank IC + 配对差检验。

    `alpha` 默认已按预登记 Bonferroni 压到 0.05/3（3 个候选集合），
    故 `ci_lo/ci_hi` 是 **98.33%** 区间而不是 95%。
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
                            "is_gated": name in SETS, **rank_ic(x, y)})
        for challenger in factors:
            if challenger == INCUMBENT:
                continue
            boot = paired_ic_bootstrap(cols[challenger], cols[INCUMBENT], y,
                                       n=n_boot, seed=seed, alpha=alpha)
            diff_rows.append({"window": win, "challenger": challenger,
                              "reference": INCUMBENT, "k": k, "offset": offset,
                              "alpha": alpha, "bonferroni_m": BONFERRONI_M,
                              "is_gated": challenger in SETS, **boot})
    return pd.DataFrame(ic_rows), pd.DataFrame(diff_rows)


# ---------------------------------------------------------------- 机器接口（batch_stat_fn）
def make_sharpe_scorer(pos_arrays: dict[str, np.ndarray], und: np.ndarray,
                       carry: np.ndarray, masks: dict[str, np.ndarray],
                       cost_bps: float):
    """收益层族：一次算完某集合在全部重抽样行下的 `worst(train,val)` 净 Sharpe。

    **重排的是仓位序列**（收益 / carry 按日历不动）：循环移位对仓位是整体搬家 →
    换手（`Σ|Δpos|`）除绕接点外逐日保持 → 成本后净 Sharpe 不失真（①b 已验证）。
    """
    def _fn(variant: str, idx_matrix: np.ndarray) -> np.ndarray:
        pos = pos_arrays[variant][np.asarray(idx_matrix)]
        vals = [sharpe_rows(net_return_matrix(pos[:, masks[w]], und[masks[w]],
                                              carry[masks[w]], cost_bps))
                for w in STAT_WINDOWS]
        return np.minimum(vals[0], vals[1])
    return _fn


def make_ic_scorer(fac_arrays: dict[str, np.ndarray], fwd: np.ndarray,
                   grid_pos: dict[str, np.ndarray]):
    """闸门 ⓪ 族：一次算完某集合在全部重抽样行下的 `worst(train,val)` **有符号** rank IC。

    取**有符号**而非 |IC|（② 用的是 |IC|）：本命题是**替换类**、方向由部署固定
    （因子 >0 → 做多），负 IC 不是"另一种有效"而是失效 → 统计量必须是有符号的。
    """
    def _fn(variant: str, idx_matrix: np.ndarray) -> np.ndarray:
        fac = fac_arrays[variant][np.asarray(idx_matrix)]
        vals = []
        for w in STAT_WINDOWS:
            g = grid_pos[w]
            x = fac[:, g]
            vals.append(spearman_rows(x, np.tile(fwd[g], (x.shape[0], 1))))
        return np.minimum(vals[0], vals[1])
    return _fn


# ---------------------------------------------------------------- 闸门判定
def evaluate_gates(panel_blend_lf: pd.DataFrame, ic_rep: pd.DataFrame,
                   ic_diff: pd.DataFrame, gated_sets: tuple[str, ...],
                   div_diag: dict) -> pd.DataFrame:
    """⑤(f) 判据逐条判定：**先 ⓪ 后收益层**，任一不满足 → 该候选 STOP。

    读法（开跑前钉死，见探针文档 §"判据读法"）：
    - **闸门 ⓪**（先）：候选的非重叠 rank IC 在 **train 与 val 双窗均 > 现役**，
      **且**配对 IC 差的 **Bonferroni 98.33% CI** 在至少一窗排除 0
      （"明确优于而非平手"，读法逐字沿用 ③ 的闸门 ⓪ → 同秤）；
    - **收益层 ①**：`worst(train,val)` 净 Sharpe ≥ 现役 **+0.15**（blend / long-flat）；
    - **收益层 ②**：**选择窗**（2014-2023）年化换手 ≤ 现役同窗值（原文"换手不增"是比较式）；
    - **收益层 ③**：**选择窗**剔最强年后的 Sharpe（`yearly.concentration_summary`
      的 `sharpe_ex_top1`，按年度对数贡献排序）≥ 现役同窗值（"剔最强年后仍不劣"）。
    - 红利腿另有**前置诊断闸**：同源判定为真 → D 直接出局，后续判据不再评。
    `pass=None` 的行是诊断 / 对照量，不参与该候选的 OVERALL。
    """
    blend = panel_blend_lf.set_index("pair_set")
    inc = blend.loc[INCUMBENT]

    def ic_of(name, win):
        hit = ic_rep[(ic_rep["pair_set"] == name) & (ic_rep["window"] == win)]["ic"]
        return float(hit.iloc[0]) if len(hit) else float("nan")

    def diff_of(name, win, col):
        hit = ic_diff[(ic_diff["challenger"] == name) & (ic_diff["window"] == win)][col]
        return hit.iloc[0] if len(hit) else float("nan")

    rows = []
    for name in gated_sets:
        if name == INCUMBENT:
            continue
        if name == DIVIDEND_SET and div_diag["same_source_verdict"]:
            rows.append({"pair_set": name, "gate": "PRE_dividend_corr",
                         "metric": "max|corr| vs 现役四对族", "value":
                         div_diag["max_abs_corr_vs_incumbent_family"],
                         "threshold": DIV_CORR_GATE, "pass": False})
            rows.append({"pair_set": name, "gate": "OVERALL",
                         "metric": "STOP（同源、不测）", "value": 0.0,
                         "threshold": float("nan"), "pass": False})
            continue
        if name == DIVIDEND_SET:
            rows.append({"pair_set": name, "gate": "PRE_dividend_corr",
                         "metric": "max|corr| vs 现役四对族", "value":
                         div_diag["max_abs_corr_vs_incumbent_family"],
                         "threshold": DIV_CORR_GATE, "pass": True})

        cand = blend.loc[name]
        # ---- 闸门 ⓪（先）
        higher = {w: bool(ic_of(name, w) > ic_of(INCUMBENT, w)) for w in STAT_WINDOWS}
        for w in STAT_WINDOWS:
            rows.append({"pair_set": name, "gate": "0_rank_ic",
                         "metric": f"rank_ic_{w}", "value": ic_of(name, w),
                         "threshold": ic_of(INCUMBENT, w), "pass": higher[w]})
        sig = any(bool(diff_of(name, w, "ci_excludes_zero")) for w in STAT_WINDOWS)
        rows.append({"pair_set": name, "gate": "0_rank_ic",
                     "metric": f"paired_ic_diff_CI_excl_0(任一选择窗, α={ALPHA}/{BONFERRONI_M})",
                     "value": float(sig), "threshold": 1.0, "pass": sig})
        gate0 = bool(all(higher.values()) and sig)
        rows.append({"pair_set": name, "gate": "0_rank_ic", "metric": "闸门⓪汇总",
                     "value": float(gate0), "threshold": 1.0, "pass": gate0})

        # ---- 收益层（后）
        g1 = bool(cand["worst_tv_sharpe"] >= inc["worst_tv_sharpe"] + GATE_WORST_TV_LIFT)
        rows.append({"pair_set": name, "gate": "1_worst_tv_sharpe",
                     "metric": "worst(train,val) 净 Sharpe",
                     "value": float(cand["worst_tv_sharpe"]),
                     "threshold": float(inc["worst_tv_sharpe"]) + GATE_WORST_TV_LIFT,
                     "pass": g1})
        rows.append({"pair_set": name, "gate": "1_worst_tv_sharpe",
                     "metric": "margin_vs_incumbent",
                     "value": float(cand["worst_tv_sharpe"] - inc["worst_tv_sharpe"]),
                     "threshold": GATE_WORST_TV_LIFT, "pass": g1})
        g2 = bool(cand["turnover_selection_2014_2023"] <= inc["turnover_selection_2014_2023"])
        rows.append({"pair_set": name, "gate": "2_turnover_not_up",
                     "metric": "选择窗年化换手",
                     "value": float(cand["turnover_selection_2014_2023"]),
                     "threshold": float(inc["turnover_selection_2014_2023"]), "pass": g2})
        g3 = bool(cand["sharpe_ex_top1_selection_2014_2023"]
                  >= inc["sharpe_ex_top1_selection_2014_2023"])
        rows.append({"pair_set": name, "gate": "3_concentration",
                     "metric": "选择窗剔最强年 Sharpe",
                     "value": float(cand["sharpe_ex_top1_selection_2014_2023"]),
                     "threshold": float(inc["sharpe_ex_top1_selection_2014_2023"]),
                     "pass": g3})
        rows.append({"pair_set": name, "gate": "3_concentration",
                     "metric": "剔最强两年 Sharpe（诊断）",
                     "value": float(cand["sharpe_ex_top2_selection_2014_2023"]),
                     "threshold": float(inc["sharpe_ex_top2_selection_2014_2023"]),
                     "pass": None})
        overall = bool(gate0 and g1 and g2 and g3)
        rows.append({"pair_set": name, "gate": "OVERALL",
                     "metric": "GO（⓪+三条全中）" if overall else "STOP（任一条不中）",
                     "value": float(overall), "threshold": float("nan"), "pass": overall})
    return pd.DataFrame(rows)


def build_metadata(summary: dict) -> pd.DataFrame:
    run, mach = summary["run"], summary["machine"]
    items = {
        "verdict": summary["verdict"],
        "n_candidates_GO": summary["n_candidates_go"],
        **{f"p_selected_{fam}": v["p_selected"] for fam, v in mach.items()},
        **{f"p_naive_UNCORRECTED_{fam}": v["p_naive_UNCORRECTED"] for fam, v in mach.items()},
        **{f"p_min_p_{fam}": v["p_min_p"] for fam, v in mach.items()},
        **{f"winner_{fam}": v["best_variant"] for fam, v in mach.items()},
        **{f"observed_best_{fam}": v["observed_best"] for fam, v in mach.items()},
        **{f"worst_tv_sharpe_{k}": v for k, v in summary["worst_tv_sharpe"].items()},
        "dividend_same_source_verdict": summary["dividend_gate"]["same_source_verdict"],
        "dividend_max_abs_corr": summary["dividend_gate"]["max_abs_corr_vs_incumbent_family"],
        **{k: run[k] for k in (
            "n_perm", "seed", "cost_bps", "k_forward", "n_obs_selection",
            "sample_first_date", "sample_last_date", "min_shift", "max_shift",
            "n_variants", "vectorized_vs_house_sharpe_max_abs_diff",
            "vectorized_vs_house_ic_max_abs_diff",
            "incumbent_vs_production_csv_max_abs_diff",
            "db_host", "carry_policy", "holdout_policy", "deploy_policy")},
    }
    return pd.DataFrame([{"pair_set": "-", "gate": "META", "metric": k, "value": v,
                          "threshold": float("nan"), "pass": None}
                         for k, v in items.items()])


# ---------------------------------------------------------------- 编排
def run_probe(n_perm: int = 1000, seed: int = 0, cost_bps: float = 3.0,
              n_boot: int = 10000, db=None) -> dict:
    from backtest.data import load_carry, load_underlying_returns

    prices = load_prices(db)
    factors = {name: build_factor(prices, name) for name in SETS}
    factors[DIAG_SET] = build_factor(prices, DIAG_SET)      # 诊断变体（不进闸门/机器）
    pair_factors = build_pair_factors(prices)

    # —— 溯源锚点：现役集合重建 vs committed 生产 CSV（后者只存 4 位小数）
    prod = load_production_signal()
    idx_p = factors[INCUMBENT].index.intersection(prod.index)
    prod_diff = float((factors[INCUMBENT].reindex(idx_p) - prod.reindex(idx_p)).abs().max())

    div_diag = dividend_gate(pair_factors["dividend"], factors[INCUMBENT],
                             pair_factors, prices)

    und = {kj: load_underlying_returns(kj, db=db) for kj in KOU_JING_REPORT}
    carry = {kj: load_carry(kj, db=db) for kj in KOU_JING_REPORT}

    common = factors[INCUMBENT].dropna().index
    for f in factors.values():
        common = common.intersection(f.dropna().index)
    common = common.intersection(und[PRIMARY_KOU_JING].index).sort_values()
    common_sel = common[(common >= pd.Timestamp(SELECTION[0]))
                        & (common <= pd.Timestamp(SELECTION[1]))]

    panel = build_panel(factors, und, carry, common, cost_bps)
    ic_rep, ic_diff = build_ic_report(factors, und[PRIMARY_KOU_JING], k=K_FORWARD,
                                      offset=IC_OFFSET, n_boot=n_boot, seed=seed)

    # ---- 机器：4 个集合（含现役对照点）合成，每族一次调用
    gated = tuple(SETS)
    min_shift = 2 * K_FORWARD
    max_shift = len(common_sel) - 2 * K_FORWARD
    masks = window_masks(common_sel)
    und_sel = und[PRIMARY_KOU_JING].reindex(common_sel).to_numpy(dtype=float)
    carry_sel = carry[PRIMARY_KOU_JING].reindex(common_sel).fillna(0.0).to_numpy(dtype=float)
    pos_sel = {v: map_position(factors[v].reindex(common_sel), "longflat").to_numpy(dtype=float)
               for v in gated}
    fac_sel = {v: factors[v].reindex(common_sel).to_numpy(dtype=float) for v in gated}

    fwd_full = forward_return(und[PRIMARY_KOU_JING], K_FORWARD)
    fwd_sel = fwd_full.reindex(common_sel).to_numpy(dtype=float)
    if not np.all(np.isfinite(fwd_sel)):
        raise AssertionError("选择窗内存在缺失的前瞻收益——机器的 IC 族无法评分")
    grid_pos = {}
    for w in STAT_WINDOWS:
        pos_idx = np.flatnonzero(masks[w])
        grid_pos[w] = pos_idx[IC_OFFSET::K_FORWARD]

    meta = {"n_obs": len(common_sel), "min_shift": min_shift, "max_shift": max_shift,
            "first_date": str(common_sel[0].date()), "last_date": str(common_sel[-1].date())}
    res_sharpe = selection_permutation_test(
        gated, n_obs=len(common_sel),
        batch_stat_fn=make_sharpe_scorer(pos_sel, und_sel, carry_sel, masks, cost_bps),
        n_perm=n_perm, seed=seed, scheme="rotation",
        min_shift=min_shift, max_shift=max_shift,
        statistic_name="worst_tv_net_sharpe(train,val)", meta=meta)
    res_ic = selection_permutation_test(
        gated, n_obs=len(common_sel),
        batch_stat_fn=make_ic_scorer(fac_sel, fwd_sel, grid_pos),
        n_perm=n_perm, seed=seed, scheme="rotation",
        min_shift=min_shift, max_shift=max_shift,
        statistic_name="worst_tv_signed_rank_ic(train,val)", meta=meta)

    # ---- 运行期口径校验：向量化统计量 == 房规引擎 / 房规 rank_ic
    blend_lf = panel[(panel["kou_jing"] == PRIMARY_KOU_JING)
                     & (panel["mapping"] == "longflat")]
    house_sharpe = np.array([float(blend_lf.set_index("pair_set").loc[v, "worst_tv_sharpe"])
                             for v in gated])
    diff_sharpe = float(np.nanmax(np.abs(house_sharpe - res_sharpe.observed)))
    if not diff_sharpe < 1e-9:
        raise AssertionError(f"向量化净 Sharpe 与房规引擎不一致：max|diff|={diff_sharpe:.3e}")
    house_ic = np.array([min(float(ic_rep[(ic_rep["pair_set"] == v)
                                          & (ic_rep["window"] == w)]["ic"].iloc[0])
                             for w in STAT_WINDOWS) for v in gated])
    diff_ic = float(np.nanmax(np.abs(house_ic - res_ic.observed)))
    if not diff_ic < 1e-9:
        raise AssertionError(f"向量化 rank IC 与房规 rank_ic 不一致：max|diff|={diff_ic:.3e}")

    verdict = evaluate_gates(blend_lf, ic_rep, ic_diff, gated, div_diag)
    go = verdict[verdict["gate"] == "OVERALL"]
    n_go = int(sum(1 for v in go["pass"] if v is True))     # pass 列含 None → 不用 fillna

    def _mach(res):
        s = res.summary()
        null = np.asarray(res.null_selected, dtype=float)
        finite = null[np.isfinite(null)]
        s["null_selected_quantiles"] = ({f"p{q}": float(np.percentile(finite, q))
                                         for q in (5, 25, 50, 75, 95, 99)}
                                        if finite.size else {})
        return s

    def _by_window(name, mapping="longflat", kj=PRIMARY_KOU_JING) -> dict:
        row = panel[(panel["pair_set"] == name) & (panel["mapping"] == mapping)
                    & (panel["kou_jing"] == kj)].iloc[0]
        return {w: {k: (float(row[f"{k}_{w}"]) if pd.notna(row.get(f"{k}_{w}")) else None)
                    for k in ("net_sharpe", "net_ann", "maxdd", "calmar", "turnover",
                              "hit", "long_frac", "sharpe_ex_top1", "sharpe_ex_top2")}
                for w in WINDOWS_REPORT if f"net_sharpe_{w}" in row}

    summary = {
        "PROBE": "候选 ⑤ 配对集合的 OOS 裁决 + 红利腿（4 个预登记集合，合成单次调用 × 两族）",
        "verdict": "GO" if n_go else "STOP",
        "n_candidates_go": n_go,
        "prerequisites": {
            "C1_machine_debt": "已付清（候选 ⓪ backtest/selection_permutation.py, commit 2d716db）",
            "user_release": "已放行（议程 §7.1 问 4：⑤ 集合裁决≠回头调参，都认；⑤ 排队）",
            "deploy_constraint": "本探针不动任何生产文件；GO 亦只出建议草案，部署由用户另行拍板",
        },
        "sets": {k: list(v) for k, v in SETS.items()},
        "not_tested_registered": {
            "885000.WI_偏股基金腿": "不在预登记集合内 → 本探针不测（登记留档）",
            "权重优化": "预登记明写不做（扫权重必过拟合）",
            "参数网格": "lookback/z_window/smoothing 锁死在现役生产值，不进任何网格",
        },
        "dividend_gate": div_diag,
        "machine": {"sharpe": _mach(res_sharpe), "rank_ic": _mach(res_ic)},
        "worst_tv_sharpe": {v: float(blend_lf.set_index("pair_set").loc[v, "worst_tv_sharpe"])
                            for v in panel["pair_set"].unique()
                            if v in set(blend_lf["pair_set"])},
        "by_window": {v: _by_window(v) for v in factors},
        "gate_readings": {
            "gate0": "非重叠 20 日 rank IC：候选双窗均 > 现役，且配对 IC 差 Bonferroni "
                     "98.33% CI 至少一窗排除 0（读法沿用 ③ 的闸门 ⓪）",
            "gate1": "worst(train,val) 净 Sharpe ≥ 现役 +0.15（blend / long-flat）",
            "gate2": "选择窗（2014-2023）年化换手 ≤ 现役同窗值",
            "gate3": "选择窗剔最强年 Sharpe（yearly.concentration_summary）≥ 现役同窗值",
            "order": "先 ⓪ 后收益层；任一不满足 → 该候选 STOP，维持四对等权",
        },
        "run": {
            "n_perm": n_perm, "seed": seed, "cost_bps": cost_bps,
            "k_forward": K_FORWARD, "ic_offset": IC_OFFSET, "n_boot_paired_ic": n_boot,
            "alpha_bonferroni": ALPHA / BONFERRONI_M, "bonferroni_m": BONFERRONI_M,
            "lookback": LOOKBACK, "z_window": Z_WINDOW, "smoothing": SMOOTHING,
            "n_obs_selection": len(common_sel), "selection_window": list(SELECTION),
            "sample_first_date": str(common[0].date()),
            "sample_last_date": str(common[-1].date()),
            "n_obs_full": len(common),
            "min_shift": min_shift, "max_shift": max_shift, "n_variants": len(gated),
            "grid_points_per_window": {w: int(len(g)) for w, g in grid_pos.items()},
            "vectorized_vs_house_sharpe_max_abs_diff": diff_sharpe,
            "vectorized_vs_house_ic_max_abs_diff": diff_ic,
            "incumbent_vs_production_csv_max_abs_diff": prod_diff,
            "db_host": (db or {}).get("host", "settings.yaml default"),
            "carry_policy": "每口径各用各的 carry（500→IC、1000→IM、blend→50/50）；"
                            "机器与四条判据只用 blend",
            "holdout_policy": "2024-2026 只报告不选择、不进任何闸门（勘误 §3 / 议程 §7.3）",
            "deploy_policy": "不改 deploy/、signals/、production.py；GO 只出草案，用户另行拍板",
        },
    }
    perm = pd.concat([res_sharpe.to_frame().assign(family="worst_tv_net_sharpe"),
                      res_ic.to_frame().assign(family="worst_tv_signed_rank_ic")],
                     ignore_index=True)
    diag = pd.DataFrame([{"metric": k, "value": v} for k, v in div_diag.items()])
    return {"panel": panel, "ic": pd.concat([ic_rep.assign(row="level"),
                                             ic_diff.assign(row="diff")], ignore_index=True),
            "permutation": perm, "diagnostics": diag, "verdict": verdict,
            "summary": summary, "res_sharpe": res_sharpe, "res_ic": res_ic}


# ---------------------------------------------------------------- CLI
def main() -> int:
    ap = argparse.ArgumentParser(
        description="⑤ 配对集合 OOS 裁决 + 红利腿探针（4 个预登记集合，不扩）")
    ap.add_argument("--n-perm", type=int, default=1000)
    ap.add_argument("--seed", type=int, default=0)
    ap.add_argument("--cost-bps", type=float, default=3.0)
    ap.add_argument("--n-boot", type=int, default=10000, help="配对 IC 差 bootstrap 次数")
    ap.add_argument("--db-host", default=None, help="覆盖库主机（主库拒连时走只读备端）")
    args = ap.parse_args()

    db = None
    if args.db_host:
        from signals.common.config import load_db_config
        db = dict(load_db_config())
        db["host"] = args.db_host

    out = run_probe(n_perm=args.n_perm, seed=args.seed, cost_bps=args.cost_bps,
                    n_boot=args.n_boot, db=db)
    out_dir = ROOT / "backtest" / "output"
    out_dir.mkdir(parents=True, exist_ok=True)
    out["panel"].round(6).to_csv(out_dir / "probe_5_pairset_panel.csv", index=False)
    out["ic"].round(6).to_csv(out_dir / "probe_5_pairset_ic.csv", index=False)
    out["permutation"].round(6).to_csv(out_dir / "probe_5_pairset_permutation.csv",
                                       index=False)
    out["diagnostics"].to_csv(out_dir / "probe_5_pairset_diagnostics.csv", index=False)
    pd.concat([out["verdict"], build_metadata(out["summary"])], ignore_index=True).to_csv(
        out_dir / "probe_5_pairset_verdict.csv", index=False)
    side = out_dir / "probe_5_pairset_summary.json"
    side.write_text(json.dumps(out["summary"], ensure_ascii=False, indent=2, default=str)
                    + "\n", encoding="utf-8")

    d = out["summary"]["dividend_gate"]
    print("=== 红利腿前置诊断闸（000922 / H00922） ===")
    print(f"  覆盖 {d['n_days_dividend_leg']} 天（{d['first_date']} ~ {d['last_date']}）；"
          f"两码日收益 corr={d['daily_return_corr_between_two_legs']:.4f}；"
          f"净值比 {d['level_ratio_first']:.3f}→{d['level_ratio_last']:.3f}"
          f"（年化漂移 {100 * d['implied_annual_drift_of_ratio']:.2f}%）")
    print(f"  红利腿因子 vs 现役族 max|corr|={d['max_abs_corr_vs_incumbent_family']:.4f} "
          f"（阈值 {DIV_CORR_GATE}）→ 同源判定 = {d['same_source_verdict']}")

    print("\n=== 闸门 ⓪：非重叠 20 日非偏 rank IC（blend·同网格同秤） ===")
    lvl = out["ic"][out["ic"]["row"] == "level"]
    print(lvl.pivot_table(index="pair_set", columns="window", values="ic")
          .round(4).to_string())
    dif = out["ic"][out["ic"]["row"] == "diff"]
    print("\n配对 IC 差（候选 − 现役，Bonferroni α=0.05/3 → 98.33% CI）")
    print(dif[["window", "challenger", "diff_ic", "ci_lo", "ci_hi", "p_value",
               "ci_excludes_zero", "n_obs"]].round(4).to_string(index=False))

    blend = out["panel"][(out["panel"]["kou_jing"] == PRIMARY_KOU_JING)
                         & (out["panel"]["mapping"] == "longflat")]
    print("\n=== 收益层（blend · long-flat · 3bps + carry） ===")
    cols = ["pair_set", "n_pairs", "worst_tv_sharpe", "net_sharpe_train_2014_2020",
            "net_sharpe_val_2021_2023", "net_sharpe_holdout_2024_2026",
            "turnover_selection_2014_2023", "maxdd_selection_2014_2023",
            "sharpe_ex_top1_selection_2014_2023"]
    print(blend.sort_values("worst_tv_sharpe", ascending=False)[cols]
          .round(4).to_string(index=False))

    print("\n=== 机器（4 集合合成，每族一次调用；p_selected 为主口径） ===")
    for fam, m in out["summary"]["machine"].items():
        print(f"  [{fam}] winner={m['best_variant']} observed_best={m['observed_best']:.4f} "
              f"p_selected={m['p_selected']:.4f} p_naive={m['p_naive_UNCORRECTED']:.4f} "
              f"p_min_p={m['p_min_p']:.4f} inflation={m['selection_inflation']:.2f}")

    print("\n=== 判据逐条 ===")
    print(out["verdict"].round(6).to_string(index=False))
    v = out["summary"]["verdict"]
    print(f"\n{'★ GO' if v == 'GO' else '✗ STOP：无候选集合过闸 → 维持四对等权并归档'}")
    print("⚠️ 885000.WI 偏股基金腿不在预登记集合内 → 未测（登记留档）。")
    print("⚠️ 本探针不动任何生产文件；部署变更须由用户另行拍板。")
    print(f"→ {out_dir}/probe_5_pairset_"
          f"{{panel,ic,permutation,diagnostics,verdict}}.csv\n→ {side}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
