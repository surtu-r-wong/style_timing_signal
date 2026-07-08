"""方向 C · B1：截面打分装配（缩尾→截面z→成长/价值合成→风格轴）。

因子口径见设计稿 §2.5 自建 v1 草案：成长=[SalG, ProG]、价值=[EP, BP, CF/P, DP]，
5%/95% 缩尾 → 截面 Z → /√n 合成（缺因子按可用数归一，金融股 CF/P 已在上游剔为
NaN）。style_score = growth − value：正=市场偏成长、负=偏价值，作分桶排序轴。
"""
from __future__ import annotations

import sys
from pathlib import Path

import pandas as pd

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from signals.common.factors import (  # noqa: E402
    composite_score,
    cross_section_zscore,
    winsorize,
)

GROWTH_COLS = ["sal_g", "pro_g"]
VALUE_COLS = ["ep", "bp", "cfp", "dp"]

# universe 定义（设计稿 §2.4，市值排名带；1=最大）：扫描维度，回测末端裁决
UNIVERSE_BANDS: dict[str, tuple[int, int | None]] = {
    "U0": (1, None),  # 全市场（轻过滤在管线层）
    "U1": (301, None),  # 剔前 300
    "U2": (301, 1800),  # 标的段
    "U3": (301, 3800),  # 标的段 + 2000 段
    "U4": (1801, 3800),  # 纯小微盘（诊断）
}


def style_scores(
    factors: pd.DataFrame, lower: float = 0.05, upper: float = 0.95
) -> pd.DataFrame:
    """截面因子原始值（index=ts_code）→ growth_score / value_score / style_score。

    全市场截面统一缩尾+标准化（universe 只做末端过滤，不影响打分——设计定调
    「全市场打分、universe 仅过滤器」）。
    """
    z = pd.DataFrame(
        {c: cross_section_zscore(winsorize(factors[c], lower, upper)) for c in factors}
    )
    out = factors.copy()
    out["growth_score"] = composite_score(z[GROWTH_COLS])
    out["value_score"] = composite_score(z[VALUE_COLS])
    out["style_score"] = out["growth_score"] - out["value_score"]
    return out


def select_baskets(scores: pd.Series, pct: float = 0.3) -> tuple[list, list]:
    """style_score → (成长篮, 价值篮)：降序 Top pct 与 Bottom pct，NaN 剔除。

    桶大小 = floor(有效样本 × pct)；不足 1 只 → 两篮皆空（该期无信号）。
    """
    valid = scores.dropna().sort_values(ascending=False)
    k = int(len(valid) * pct)
    if k < 1:
        return [], []
    return list(valid.index[:k]), list(valid.index[-k:])


def extend_earliest_to_past(pool: pd.DataFrame) -> pd.DataFrame:
    """行业快照 2021 前静态近似：每股最早行复制一条 end/known=1900-01-01 的行。

    industry_classification 仅 2021-01 起有月度快照（设计稿定调：更早期间用最早
    快照近似）。外推后统一走 asof_latest——早于最早快照的评估日自然落到近似行。
    """
    earliest = (
        pool.sort_values(["ts_code", "end_date"]).groupby("ts_code", as_index=False).first()
    )
    ancient = pd.Timestamp("1900-01-01")
    earliest = earliest.assign(end_date=ancient, known_date=ancient)
    return pd.concat([earliest, pool], ignore_index=True)


def select_baskets_industry_neutral(
    scores: pd.Series,
    industries: pd.Series,
    pct: float = 0.3,
    min_industry: int = 5,
) -> tuple[list, list]:
    """B2 行业中性选样：**每行业内** Top/Bottom pct 再汇总（设计稿 §2.4 v2）。

    两篮从每个行业取相同只数（k_i = floor(n_i×pct)）且桶内等权 → 两腿行业分布
    恒等、行业暴露=0 由构造保证；打分复用 v1 全市场 style_score（同一分数、只改
    选样 → v1−v2 之差即纯行业配置分量）。
    小行业（有效样本 < min_industry）与无行业标签的票并入一个 pooled 桶同规则选样。
    """
    valid = scores.dropna()
    ind = industries.reindex(valid.index)
    counts = ind.value_counts()
    small = set(counts[counts < min_industry].index)
    ind = ind.where(~ind.isin(small)).fillna("_pooled")

    growth: list = []
    value: list = []
    for _, grp in valid.groupby(ind):
        k = int(len(grp) * pct)
        if k < 1:
            continue
        ranked = grp.sort_values(ascending=False)
        growth += list(ranked.index[:k])
        value += list(ranked.index[-k:])
    return growth, value


def universe_mask(rank: pd.Series, name: str) -> pd.Series:
    """市值排名（1=最大）→ universe 成员掩码（UNIVERSE_BANDS 定义的排名带）。"""
    lo, hi = UNIVERSE_BANDS[name]
    mask = rank >= lo
    if hi is not None:
        mask &= rank <= hi
    return mask


def basket_spread_returns(
    returns: pd.DataFrame,
    schedule: list[tuple[pd.Timestamp, list, list]],
) -> pd.DataFrame:
    """成员日收益矩阵 + 月度换仓表 → 两腿等权日收益与价差。

    持有约定：formation 日**收盘**建仓（当日分数 PIT 已知），次日起收益计入，
    持有至下一 formation 日（含当日收益，收盘才换仓）。桶内=日度等权（每日再平衡
    的等权均值）；成员当日停牌/无数据（NaN）自动跳过，全桶无数据 → NaN。
    输出 index 从首个 formation 次日到 returns 末尾，columns=[growth_ret,
    value_ret, spread]。
    """
    schedule = sorted(schedule, key=lambda item: item[0])
    out_index = returns.index[returns.index > schedule[0][0]]
    out = pd.DataFrame(
        index=out_index, columns=["growth_ret", "value_ret", "spread"], dtype=float
    )
    for i, (formation, growth_members, value_members) in enumerate(schedule):
        until = schedule[i + 1][0] if i + 1 < len(schedule) else None
        mask = out_index > formation
        if until is not None:
            mask &= out_index <= until
        days = out_index[mask]
        if len(days) == 0:
            continue
        g = returns.loc[days, [c for c in growth_members if c in returns.columns]]
        v = returns.loc[days, [c for c in value_members if c in returns.columns]]
        out.loc[days, "growth_ret"] = g.mean(axis=1, skipna=True)
        out.loc[days, "value_ret"] = v.mean(axis=1, skipna=True)
    out["spread"] = out["growth_ret"] - out["value_ret"]
    return out
