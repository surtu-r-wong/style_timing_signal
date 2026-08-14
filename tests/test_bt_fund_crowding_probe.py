"""⑦ 公募持仓拥挤面探针的判例：预登记不变量 + 机器接入规矩 + ⑦ 自己的六类。

覆盖（预登记 `2026-08-14-probe-7-preregistration.md` §B 逐条）：

1. **网格与机器规矩**——⑦B 16 点 / ⑦A 6 点、顺序固定、k 恒在 variant[3]；
   `min_shift = 2·max(k)`（B=80 / A=120）；
2. **滚动 β**——闭式滚动矩解对逐窗 `lstsq` 逐位一致（1e-8）；无噪声线性数据精确回收
   系数；det 退化 → NaN 不静默；size/gv 维的 spread/mkt 定义（§B.2 原文）；
3. **rolling / expanding 分位**——房规平均秩口径 `(less+0.5·(equal+1))/n`；
   expanding burn-in 8 期（§B.9）、NaN 输入即炸；
4. **PIT**——P95 生效日 = `percentile_disc(0.95)` 语义（实际观测日，≥95% 已披露）；
   阶梯日频**生效日前不可见**；生效日乱序/重复 → 抛出（不静默修）；
5. **板块归类边界（§B.8 冻结字面口径）**——300/301+.SZ 与 688+.SH 进分子；
   689（科创非冻结前缀）只进分母；`.HK`/`.BJ` 两侧全剔；A2 镜像聚合对手算样本相符；
6. **A1 口径（§B.7）**——主动权益过滤（含"基金"后缀变体）、>100 保留原值、
   A/C 不去重（行即样本）、每期中位数、P95 生效日。

全部判例固定 seed、**纯离线**（不连库、不读缓存 CSV）。
"""
import sys
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from backtest.fund_crowding_probe import (  # noqa: E402
    A_BURN_IN, A_GRID_K, A_SERIES, ACTIVE_FUND_TYPES, B_DIMS, B_GRID_K, B_GRID_ZW,
    FAMILY_ALPHA, IDX_300, IDX_1000, IDX_FUND, IDX_GROWTH, IDX_VALUE, W_MAIN, W_SENS,
    a1_median_from_positions, a2_ratio_from_rows, a_variant_grid, b_variant_grid,
    board_flags, centered, dimension_returns, expanding_percentile, family_halves,
    family_min_shift, p95_eff_date, rolling_beta_spread, step_to_daily,
)
from dashboard.data import rolling_percentile  # noqa: E402

RNG = np.random.default_rng(7)


def _bdays(n: int, start: str = "2018-01-02") -> pd.DatetimeIndex:
    return pd.bdate_range(start, periods=n)


# ---------------------------------------------------------------- 1. 网格与机器规矩
class TestGrids:
    def test_b_grid_is_16_points_order_fixed(self):
        g = b_variant_grid()
        assert len(g) == 16 == len(set(g))
        assert g[0] == ("size", 240, 250, 5)
        assert g[-1] == ("gv", 240, 500, 40)
        # dim 外层 → zw → k 内层（产物对表约定）
        assert g[:4] == [("size", 240, 250, k) for k in (5, 10, 20, 40)]

    def test_a_grid_is_6_points_order_fixed(self):
        g = a_variant_grid()
        assert len(g) == 6 == len(set(g))
        assert g[0] == ("A1", "expanding", "annP95", 20)
        assert g[-1] == ("A2", "expanding", "annP95", 60)

    def test_k_always_at_position_3(self):
        """机器约定：`make_batch_scorer` 按 variant[3] 取 ScoreFrame。"""
        for v in b_variant_grid():
            assert v[3] in B_GRID_K
        for v in a_variant_grid():
            assert v[3] in A_GRID_K

    def test_min_shift_rule(self):
        assert family_min_shift(B_GRID_K) == 80
        assert family_min_shift(A_GRID_K) == 120

    def test_family_alpha_is_bonferroni_half(self):
        assert FAMILY_ALPHA == 0.025

    def test_family_halves_split_own_sample(self):
        idx = _bdays(11)
        h = family_halves(idx)
        assert h["half1"] == (str(idx[0].date()), str(idx[4].date()))
        assert h["half2"] == (str(idx[5].date()), str(idx[-1].date()))


# ---------------------------------------------------------------- 2. 滚动 β
class TestRollingBeta:
    def test_recovers_exact_linear_coefficients(self):
        n, w = 400, 240
        idx = _bdays(n)
        m = pd.Series(RNG.normal(0, 0.01, n), index=idx)
        s = pd.Series(RNG.normal(0, 0.008, n), index=idx)
        y = 0.0002 + 1.15 * m - 0.65 * s
        out = rolling_beta_spread(y, m, s, w)
        got = out["beta_s"].dropna()
        assert len(got) == n - w + 1
        assert np.allclose(got.to_numpy(), -0.65, atol=1e-10)
        assert np.allclose(out["beta_m"].dropna().to_numpy(), 1.15, atol=1e-10)

    def test_matches_windowed_lstsq_with_noise(self):
        n, w = 320, 240
        idx = _bdays(n)
        m = pd.Series(RNG.normal(0, 0.01, n), index=idx)
        s = pd.Series(RNG.normal(0, 0.008, n), index=idx)
        y = 0.5 * m - 0.3 * s + pd.Series(RNG.normal(0, 0.004, n), index=idx)
        out = rolling_beta_spread(y, m, s, w)
        for end in (w - 1, w + 30, n - 1):
            block = slice(end - w + 1, end + 1)
            x_mat = np.column_stack([np.ones(w), m.iloc[block], s.iloc[block]])
            ref = np.linalg.lstsq(x_mat, y.iloc[block].to_numpy(), rcond=None)[0]
            assert abs(float(out["beta_s"].iloc[end]) - ref[2]) < 1e-8
            assert abs(float(out["beta_m"].iloc[end]) - ref[1]) < 1e-8

    def test_warmup_is_nan_and_degenerate_det_is_nan(self):
        n, w = 300, 240
        idx = _bdays(n)
        m = pd.Series(RNG.normal(0, 0.01, n), index=idx)
        out = rolling_beta_spread(0.7 * m, m, m * 2.0, w)   # spread 与 mkt 完全共线
        assert out["beta_s"].isna().all()                    # det=0 → NaN 不静默
        good = rolling_beta_spread(
            0.7 * m, m, pd.Series(RNG.normal(0, 0.01, n), index=idx), w)
        assert good["beta_s"].iloc[: w - 1].isna().all()     # 满窗前 NaN
        assert good["beta_s"].iloc[w - 1:].notna().all()

    def test_dimension_returns_definitions(self):
        """§B.2 原文：size spread = r852−r300；gv spread = r370−r371；mkt = 两腿均值。"""
        idx = _bdays(6)
        closes = pd.DataFrame(
            {IDX_FUND: np.linspace(100, 105, 6), IDX_300: np.linspace(50, 55, 6),
             IDX_1000: np.linspace(80, 78, 6), IDX_GROWTH: np.linspace(30, 33, 6),
             IDX_VALUE: np.linspace(40, 39, 6)}, index=idx)
        y, mkt, spr = dimension_returns(closes, "size")
        r = closes.pct_change().dropna()
        assert np.allclose(spr, r[IDX_1000] - r[IDX_300])
        assert np.allclose(mkt, (r[IDX_300] + r[IDX_1000]) / 2)
        assert np.allclose(y, r[IDX_FUND])
        _, mkt_gv, spr_gv = dimension_returns(closes, "gv")
        assert np.allclose(spr_gv, r[IDX_GROWTH] - r[IDX_VALUE])
        assert np.allclose(mkt_gv, (r[IDX_GROWTH] + r[IDX_VALUE]) / 2)

    def test_w_constants_frozen(self):
        assert (W_MAIN, W_SENS) == (240, 120)
        assert B_GRID_ZW == (250, 500) and B_GRID_K == (5, 10, 20, 40)
        assert B_DIMS == ("size", "gv")


# ---------------------------------------------------------------- 3. 分位口径
class TestPercentiles:
    def test_rolling_percentile_house_convention(self):
        s = pd.Series([3.0, 1.0, 2.0, 5.0, 4.0], index=_bdays(5))
        got = rolling_percentile(s, window=3)
        assert got.iloc[:2].isna().all()
        # 窗 [3,1,2] 末值 2：less=1, equal=1 → (1+0.5·2)/3 = 2/3
        assert got.iloc[2] == pytest.approx(2 / 3)
        assert got.iloc[3] == pytest.approx(1.0)          # 窗 [1,2,5] 末值最大
        assert got.iloc[4] == pytest.approx(2 / 3)        # 窗 [2,5,4]

    def test_expanding_percentile_burn_in_8(self):
        vals = pd.Series(np.arange(12, dtype=float),
                         index=pd.period_range("2016Q1", periods=12, freq="Q")
                         .to_timestamp(how="end"))
        got = expanding_percentile(vals)
        assert got.iloc[:A_BURN_IN].isna().all()          # 前 8 期 NaN（§B.9）
        assert got.iloc[A_BURN_IN:].notna().all()
        # 第 9 期是 9 个值里的最大：less=8, equal=1 → (8+1)/9 = 1.0
        assert got.iloc[8] == pytest.approx(1.0)

    def test_expanding_percentile_ties_average_rank(self):
        vals = pd.Series([1.0] * 9, index=range(9))
        got = expanding_percentile(vals)
        # 全并列：less=0, equal=9 → (0 + 0.5·10)/9 = 5/9（平均秩口径）
        assert got.iloc[8] == pytest.approx(5 / 9)

    def test_expanding_percentile_rejects_nan(self):
        with pytest.raises(ValueError, match="NaN"):
            expanding_percentile(pd.Series([1.0, np.nan, 2.0] + [3.0] * 7))

    def test_centered_shifts_by_half_only(self):
        s = pd.Series([0.1, 0.5, 0.9])
        assert np.allclose(centered(s), [-0.4, 0.0, 0.4])


# ---------------------------------------------------------------- 4. PIT
class TestPIT:
    def test_p95_eff_date_percentile_disc_semantics(self):
        # n=20 → ceil(0.95·20)=19 → 第 19 小（0-based 18）
        days = pd.date_range("2020-04-01", periods=20)
        shuffled = days.to_numpy().copy()
        RNG.shuffle(shuffled)
        assert p95_eff_date(pd.Series(shuffled)) == days[18]
        # n=100 → 第 95 小
        days2 = pd.date_range("2021-04-01", periods=100)
        assert p95_eff_date(pd.Series(days2)) == days2[94]
        # 覆盖率保证：≥95% 样本 ann_date ≤ 生效日
        eff = p95_eff_date(pd.Series(days))
        assert (days <= eff).mean() >= 0.95

    def test_step_to_daily_not_visible_before_eff_date(self):
        cal = pd.bdate_range("2020-01-01", "2020-03-31")
        values = pd.Series([np.nan, 0.4, 0.7],
                           index=pd.to_datetime(["2019-09-30", "2019-12-31", "2020-03-31"]))
        eff = pd.Series(pd.to_datetime(["2019-10-25", "2020-01-22", "2020-04-24"]),
                        index=values.index)
        daily = step_to_daily(values, eff, cal)
        assert daily.loc[: "2020-01-21"].isna().all()      # burn-in 期不生效 + 生效日前不可见
        assert (daily.loc["2020-01-22":"2020-03-31"] == 0.4).all()   # 0.7 在 4/24 才生效

    def test_step_to_daily_ffills_between_effective_dates(self):
        cal = pd.bdate_range("2020-01-01", "2020-12-31")
        values = pd.Series([0.2, 0.8], index=pd.to_datetime(["2019-12-31", "2020-06-30"]))
        eff = pd.Series(pd.to_datetime(["2020-01-20", "2020-07-20"]), index=values.index)
        daily = step_to_daily(values, eff, cal)
        assert (daily.loc["2020-01-20":"2020-07-17"] == 0.2).all()
        assert (daily.loc["2020-07-20":] == 0.8).all()

    def test_step_to_daily_newer_report_supersedes_on_eff_collision(self):
        """偏离记录 C 的判例：2019Q4 年报 P95（2020-04-30）晚于 2020Q1 季报 P95
        （2020-04-22）→ 旧期永不显示、新期一经生效即为当前值（as-of by report_date）。"""
        cal = pd.bdate_range("2020-01-01", "2020-06-30")
        values = pd.Series([0.2, 0.8], index=pd.to_datetime(["2019-12-31", "2020-03-31"]))
        eff = pd.Series(pd.to_datetime(["2020-04-30", "2020-04-22"]), index=values.index)
        daily = step_to_daily(values, eff, cal)
        assert daily.loc[: "2020-04-21"].isna().all()
        assert (daily.loc["2020-04-22":] == 0.8).all()     # 0.2 从不出现（含 4/30 之后）

    def test_step_to_daily_equal_eff_dates_later_report_wins(self):
        cal = pd.bdate_range("2020-01-01", "2020-06-30")
        values = pd.Series([0.2, 0.8], index=pd.to_datetime(["2019-12-31", "2020-03-31"]))
        eff = pd.Series(pd.to_datetime(["2020-04-22", "2020-04-22"]), index=values.index)
        daily = step_to_daily(values, eff, cal)
        assert (daily.loc["2020-04-22":] == 0.8).all()


# ---------------------------------------------------------------- 5. 板块归类（§B.8）
class TestBoardClassification:
    CODES = ["300059.SZ", "301111.SZ", "688001.SH", "689009.SH", "600000.SH",
             "000001.SZ", "002594.SZ", "0700.HK", "836000.BJ", "920001.BJ",
             "300001.SH", "688001.SZ"]

    def test_boundary_codes(self):
        f = board_flags(pd.Series(self.CODES))
        num = dict(zip(self.CODES, f["in_num"]))
        den = dict(zip(self.CODES, f["in_den"]))
        assert num["300059.SZ"] and num["301111.SZ"] and num["688001.SH"]
        assert not num["689009.SH"] and den["689009.SH"]   # 689 非冻结前缀：只进分母
        assert not num["600000.SH"] and den["600000.SH"]
        assert not num["000001.SZ"] and den["000001.SZ"]
        assert not num["0700.HK"] and not den["0700.HK"]   # 港股两侧全剔
        assert not num["836000.BJ"] and not den["836000.BJ"]   # 北交所两侧全剔
        assert not num["920001.BJ"] and not den["920001.BJ"]
        # 前缀×后缀交叉错位：300+.SH / 688+.SZ 均不进分子（字面口径）
        assert not num["300001.SH"] and den["300001.SH"]
        assert not num["688001.SZ"] and den["688001.SZ"]

    def test_a2_ratio_mirror_matches_hand_calc(self):
        rows = pd.DataFrame({
            "ts_code": ["300059.SZ", "688001.SH", "600000.SH", "0700.HK", "836000.BJ"],
            "hold_value": [10.0, 20.0, 70.0, 999.0, 999.0],
        })
        num, den, ratio = a2_ratio_from_rows(rows)
        assert num == pytest.approx(30.0)
        assert den == pytest.approx(100.0)      # 港股/北交所不进分母
        assert ratio == pytest.approx(0.30)

    def test_a2_ratio_empty_denominator_is_nan(self):
        rows = pd.DataFrame({"ts_code": ["0700.HK"], "hold_value": [5.0]})
        assert np.isnan(a2_ratio_from_rows(rows)[2])


# ---------------------------------------------------------------- 6. A1 口径（§B.7）
class TestA1:
    def _positions(self) -> pd.DataFrame:
        d1, d2 = pd.Timestamp("2016-03-31"), pd.Timestamp("2016-06-30")
        return pd.DataFrame({
            "fund_code": ["A.OF", "B.OF", "C.OF", "D.OF", "E.OF",
                          "A.OF", "B.OF", "C.OF"],
            "report_date": [d1] * 5 + [d2] * 3,
            "stock_to_nav_pct": [90.0, 88.0, 125.03, 60.0, 40.0, 91.0, 89.0, 91.0],
            "ann_date": pd.to_datetime(
                ["2016-04-20", "2016-04-21", "2016-04-22", "2016-04-19", "2016-04-25",
                 "2016-07-20", "2016-07-21", "2016-07-19"]),
        })

    def test_active_filter_median_and_over_100_kept(self):
        pos = self._positions()
        active = {"A.OF", "B.OF", "C.OF", "D.OF"}     # E 被过滤（非主动权益）
        out = a1_median_from_positions(pos, active)
        # 期1：[90, 88, 125.03, 60] → 中位 89；>100 保留原值参与
        assert out.loc["2016-03-31", "a1_median"] == pytest.approx(89.0)
        assert out.loc["2016-03-31", "a1_n_funds"] == 4
        # 期2：[91, 89, 91] → 91（A/C 类同值重复行不去重，行即样本）
        assert out.loc["2016-06-30", "a1_median"] == pytest.approx(91.0)

    def test_eff_date_is_p95_of_period_sample(self):
        pos = self._positions()
        out = a1_median_from_positions(pos, {"A.OF", "B.OF", "C.OF", "D.OF"})
        # 期1 样本 4 个 ann_date → ceil(0.95·4)=4 → 最大值 2016-04-22
        assert out.loc["2016-03-31", "a1_eff_date"] == pd.Timestamp("2016-04-22")

    def test_empty_after_filter_raises(self):
        with pytest.raises(ValueError, match="主动权益"):
            a1_median_from_positions(self._positions(), {"NOPE.OF"})

    def test_active_fund_types_cover_suffix_variants(self):
        """§B.7：ACTIVE_FUND_TYPES 含"基金"后缀变体（picker.py:48 只读参考）。"""
        assert {"普通股票型", "偏股混合型", "普通股票型基金", "偏股混合型基金"} \
            == set(ACTIVE_FUND_TYPES)
        assert A_SERIES == ("A1", "A2") and A_GRID_K == (20, 40, 60)
