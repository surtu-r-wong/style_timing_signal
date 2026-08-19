"""纯风格编制管线的纯函数层（`backtest/pure_style_builder`）—— 不连库。

覆盖 2026-08-18 三处修复里可离线验证的两处：
① PIT 可知日的法定披露截止日映射；② `avg_mv_1y` 的日频口径（股本前向填充）。
"""
import sys
from pathlib import Path

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from backtest.pure_style_builder import (  # noqa: E402
    BAND_1000,
    BAND_2000,
    OfficialBand,
    _apply_buffer,
    _avg_mv_1y_daily,
    _cap_weights,
    _select_band,
    _select_tail,
    _statutory_deadline,
    review_cutoff,
)


# ---------------------------------------------------------------- 法定披露截止日
def test_statutory_deadline_maps_all_four_report_periods():
    """Q1→当年4/30、半年报→当年8/31、Q3→当年10/31、年报→**次年**4/30。"""
    got = _statutory_deadline(pd.Series(pd.to_datetime(
        ["2018-03-31", "2018-06-30", "2018-09-30", "2018-12-31"])))
    assert list(got) == list(pd.to_datetime(
        ["2018-04-30", "2018-08-31", "2018-10-31", "2019-04-30"]))


def test_statutory_deadline_returns_nat_for_pseudo_rows():
    """CSMAR 的 `01-01` 伪行不是自然季末 → NaT（下游按 is_quarter_end 剔除）。"""
    got = _statutory_deadline(pd.Series(pd.to_datetime(["2016-01-01", "2016-03-31"])))
    assert pd.isna(got.iloc[0]) and got.iloc[1] == pd.Timestamp("2016-04-30")


def test_statutory_deadline_is_an_upper_bound_on_true_disclosure():
    """截止日 ≥ 报告期末，且年报跨年 —— 这是"保守而非前视"论证的算术前提。"""
    ends = pd.Series(pd.to_datetime(["2020-03-31", "2020-06-30", "2020-09-30", "2020-12-31"]))
    dl = _statutory_deadline(ends)
    assert (dl > ends).all()


# ---------------------------------------------------------------- 日频日均总市值
def _px(rows):
    return [(c, pd.Timestamp(d), v) for c, d, v in rows]


def test_avg_mv_1y_daily_forward_fills_share_capital():
    """股本按 `effective_date` 前向填充：换股本前后各按当时的在外股本计市值。"""
    px = _px([("A.SH", "2024-01-02", 10.0), ("A.SH", "2024-06-03", 10.0)])
    sh = [("A.SH", pd.Timestamp("2023-01-01"), 100.0),
          ("A.SH", pd.Timestamp("2024-03-01"), 200.0)]
    got = _avg_mv_1y_daily(px, sh)
    assert got["A.SH"] == (10.0 * 100 + 10.0 * 200) / 2      # 1000 与 2000 的均值


def test_avg_mv_1y_daily_drops_days_before_first_share_record():
    """首条股本记录之前的交易日无法定价 → 不计入均值（而非当 0 处理）。"""
    px = _px([("A.SH", "2023-01-02", 10.0), ("A.SH", "2024-01-02", 20.0)])
    sh = [("A.SH", pd.Timestamp("2023-06-01"), 50.0)]
    got = _avg_mv_1y_daily(px, sh)
    assert got["A.SH"] == 20.0 * 50                          # 只剩 2024-01-02 那天


def test_avg_mv_1y_daily_keeps_codes_independent():
    """`merge_asof(by=ts_code)` 不得让 A 的股本泄漏到 B。"""
    px = _px([("A.SH", "2024-01-02", 10.0), ("B.SZ", "2024-01-02", 10.0)])
    sh = [("A.SH", pd.Timestamp("2023-01-01"), 100.0),
          ("B.SZ", pd.Timestamp("2023-01-01"), 300.0)]
    got = _avg_mv_1y_daily(px, sh)
    assert got["A.SH"] == 1000.0 and got["B.SZ"] == 3000.0


def test_avg_mv_1y_daily_empty_inputs_are_safe():
    """无价格或无股本 → 空序列，由调用方回退到月末口径（不得抛）。"""
    assert _avg_mv_1y_daily([], []).empty
    assert _avg_mv_1y_daily(_px([("A.SH", "2024-01-02", 10.0)]), []).empty


# ---------------------------------------------------------------- 双帽权重
def test_cap_weights_enforces_single_and_top5_caps():
    """单样本 ≤15%、前五合计 ≤60%，且归一化到 1。"""
    w = _cap_weights(pd.Series([100.0, 1.0, 1.0, 1.0, 1.0, 1.0, 1.0, 1.0, 1.0, 1.0]))
    assert np.isclose(w.sum(), 1.0)
    assert w.max() <= 0.15 + 1e-9
    assert w.nlargest(5).sum() <= 0.60 + 1e-9


# ---------------------------------------------------------------- 官方选样时间线（2026-08-19）
def test_review_cutoff_june_effective_maps_to_april_30():
    """6 月生效 → 当年 4-30（沪深300方案 §6.2：「上一年度5月1日至审核年度4月30日」）。"""
    assert review_cutoff(pd.Timestamp("2024-06-17")) == pd.Timestamp("2024-04-30")


def test_review_cutoff_december_effective_maps_to_october_31():
    assert review_cutoff(pd.Timestamp("2024-12-16")) == pd.Timestamp("2024-10-31")


def test_review_cutoff_rejects_off_cycle_months():
    import pytest
    with pytest.raises(ValueError):
        review_cutoff(pd.Timestamp("2024-03-15"))


# ---------------------------------------------------------------- 定调缓冲区（1600进/2400保）
def test_buffer_none_prev_is_pure_ranking():
    assert _apply_buffer(list("abcdef"), None, target=4) == list("abcd")


def test_buffer_keeps_old_member_inside_keep_rank_over_better_ranked_new():
    """老样本 e（排名5，keep=6 内）挤掉排名更好的新票 d —— 缓冲区的本义。"""
    got = _apply_buffer(list("abcdef"), prev={"e"}, target=4, in_rank=3, keep_rank=6)
    assert got == ["a", "b", "c", "e"]


def test_buffer_old_keep_outranks_new_entry_on_capacity_conflict():
    """容量冲突时老样本优先保留：老 e/f（排名5/6，keep 内）锁 2 席，
    新样本只剩 2 席按排名取 a、b —— 排名更好的新票 c、d 反而落选。
    （官方 2026-06 实测行为：保留排名 2100–2400 段老样本、只换 232/2000。）"""
    got = _apply_buffer(list("abcdef"), prev={"e", "f"}, target=4, in_rank=3, keep_rank=6)
    assert got == ["a", "b", "e", "f"]


def test_buffer_drops_old_member_beyond_keep_rank():
    """老样本 g 排名 7 > keep_rank=6 → 失去优先保留资格，按普通候选竞争后落选。"""
    got = _apply_buffer(list("abcdefg"), prev={"g"}, target=4, in_rank=3, keep_rank=6)
    assert got == ["a", "b", "c", "d"]


def test_buffer_old_beyond_keep_rank_still_competes_by_rank():
    """>keep_rank 的老样本按普通候选竞争：老 c 排名 3（keep=2 之外）仍以排名进入。"""
    got = _apply_buffer(list("abcde"), prev={"c"}, target=3, in_rank=2, keep_rank=2)
    assert got == ["a", "b", "c"]


def test_buffer_backfills_by_rank_when_short():
    """old_keep 不足 target 时按排名补齐（不分新老）。"""
    got = _apply_buffer(list("abcde"), prev={"a"}, target=4, in_rank=2, keep_rank=3)
    assert got == ["a", "b", "c", "d"]


def test_buffer_old_members_overflow_trims_worst_ranked_old():
    """老样本多于 target 时保留排名最好的 target 只。"""
    got = _apply_buffer(list("abcde"), prev={"b", "c", "d", "e"}, target=3, in_rank=2, keep_rank=5)
    assert got == ["b", "c", "d"]


# ---------------------------------------------------------------- 官方带选样层（r3）
def _frame(rows):
    """rows = [(ts_code, avg_mv, adv), ...] → `_space_frame` 输出形状的帧。"""
    df = pd.DataFrame(rows, columns=["ts_code", "avg_mv", "adv"]).set_index("ts_code")
    df["is_bj"] = df.index.str.endswith(".BJ")
    return df


def test_band_constants_match_methodology_originals():
    # 《中证2000》V1.1：前90%成交额（剔后10%）/剔前1500/取2000/缓冲1600-2400/含北交所
    assert (BAND_2000.adv_trim, BAND_2000.mv_prescreen, BAND_2000.target,
            BAND_2000.buf_in, BAND_2000.buf_keep, BAND_2000.include_bj) == \
        (0.10, 1500, 2000, 1600, 2400, True)
    # 《中证1000》V1.1：剔成交额后20%/剔前300/取前1000/缓冲800-1200/仅沪深
    assert (BAND_1000.adv_trim, BAND_1000.mv_prescreen, BAND_1000.target,
            BAND_1000.buf_in, BAND_1000.buf_keep, BAND_1000.include_bj) == \
        (0.20, 300, 1000, 800, 1200, False)
    assert "000905.SH" in BAND_1000.excl_indices        # 剔中证800 的 500 半（真值全历史）


def test_select_band_drops_bj_only_when_band_excludes_it():
    g = _frame([("A.SH", 100, 10), ("B.SZ", 90, 10), ("C.BJ", 80, 10)])
    band_sh = OfficialBand("t", 0.0, 0, (), 3, 2, 4, include_bj=False)
    band_all = OfficialBand("t", 0.0, 0, (), 3, 2, 4, include_bj=True)
    assert _select_band(g, band_sh, set(), None)[0] == ["A.SH", "B.SZ"]
    assert _select_band(g, band_all, set(), None)[0] == ["A.SH", "B.SZ", "C.BJ"]


def test_select_band_adv_trim_removes_bottom_quantile_before_ranking():
    # 4 只，adv_trim=0.5 → 成交额后 50%（C、D）在排名之前被剔
    g = _frame([("A.SH", 10, 100), ("B.SH", 90, 90), ("C.SH", 100, 10), ("D.SH", 95, 5)])
    band = OfficialBand("t", 0.5, 0, (), 4, 2, 4, True)
    picked, n_all, _ = _select_band(g, band, set(), None)
    assert picked == ["B.SH", "A.SH"] and n_all == 2    # C/D 市值再大也进不来


def test_select_band_prescreen_and_membership_exclusion():
    g = _frame([("BIG.SH", 100, 10), ("MID.SH", 50, 10), ("MEM.SH", 40, 10), ("SM.SH", 30, 10)])
    band = OfficialBand("t", 0.0, 1, (), 3, 2, 4, True)   # 剔市值排名第 1（BIG）
    picked, _, n_cand = _select_band(g, band, {"MEM.SH"}, None)
    assert picked == ["MID.SH", "SM.SH"] and n_cand == 2  # BIG 被排名筛剔、MEM 被成分剔


def test_select_band_wires_buffer_params():
    # target=2：prev 里排名 3 的老样本落在 keep_rank=3 内 → 压过排名 2 的新票
    g = _frame([(f"S{i}.SH", 100 - i, 10) for i in range(4)])
    band = OfficialBand("t", 0.0, 0, (), 2, 1, 3, True)
    assert _select_band(g, band, set(), {"S2.SH"})[0] == ["S0.SH", "S2.SH"]


def test_select_tail_is_remainder_after_mothers_prescreen_and_inner_trim():
    g = _frame([("TOP.SH", 100, 50), ("MOM.SH", 90, 50), ("T1.SH", 80, 40),
                ("T2.BJ", 70, 30), ("T3.SH", 60, 1)])
    picked, n_all, n_cand = _select_tail(g, {"MOM.SH"}, mv_prescreen=1,
                                         adv_trim=0.0, inner_adv_trim=0.4)
    # TOP 被前1剔、MOM 被四母剔、T3 被带内流动性尾剔；.BJ 保留；按市值降序
    assert picked == ["T1.SH", "T2.BJ"]
    assert n_all == 5 and n_cand == 3


def test_select_tail_has_no_buffer_semantics():
    # 余集 = 全部保留（除筛），与 prev 无关 —— 接口上根本不收 prev
    g = _frame([("A.SH", 10, 10), ("B.SH", 9, 10)])
    picked, _, _ = _select_tail(g, set(), mv_prescreen=0, adv_trim=0.0, inner_adv_trim=0.0)
    assert picked == ["A.SH", "B.SH"]


# ---------------------------------------------------------------- 等比 5 桶切段（2026-08-19）
def test_split_geometric_exact_on_31():
    from backtest.pure_style_builder import _split_geometric
    codes = [f"S{i:02d}" for i in range(31)]
    parts = _split_geometric(codes)
    assert [len(p) for p in parts] == [1, 2, 4, 8, 16]
    assert sum(parts, []) == codes                    # 无缝、无重叠、保序


def test_split_geometric_scales_to_small_market():
    from backtest.pure_style_builder import _split_geometric
    parts = _split_geometric([f"S{i}" for i in range(2349)])   # 2015 年规模
    sizes = [len(p) for p in parts]
    assert sum(sizes) == 2349 and all(s > 0 for s in sizes)    # 全非空（替换式的立身之本）
    assert sizes[0] in (75, 76) and 1210 <= sizes[4] <= 1214   # ≈ 1:2:4:8:16


def test_split_geometric_boundaries_are_cumulative():
    from backtest.pure_style_builder import _split_geometric
    # 累计边界取整：n=100 → 边界 round(100*[1,3,7,15]/31) = 3,10,23,48
    parts = _split_geometric([str(i) for i in range(100)])
    assert [len(p) for p in parts] == [3, 7, 13, 25, 52]
