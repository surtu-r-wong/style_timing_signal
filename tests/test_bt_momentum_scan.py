import sys
from pathlib import Path

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from backtest.momentum_scan import (  # noqa: E402
    momentum_classic, momentum_slope, momentum_voladj,
)


def _gbm(n, seed, mu=0.0003, sigma=0.012, start="2015-01-01"):
    rng = np.random.default_rng(seed)
    idx = pd.bdate_range(start, periods=n)
    return pd.Series(100.0 * np.exp(np.cumsum(rng.normal(mu, sigma, n))), index=idx)


# ---------- M1 经典跳月 ----------

def test_momentum_classic_hand_anchor_and_skip_alignment():
    """定义钉死:classic(L, skip)[t] = P(t−skip)/P(t−L−skip) − 1(off-by-one 手算锚)。"""
    idx = pd.bdate_range("2020-01-01", periods=6)
    p = pd.Series([100.0, 110.0, 121.0, 133.1, 146.41, 161.051], index=idx)

    m = momentum_classic(p, length=2, skip=0)
    assert np.isclose(m.iloc[2], 121.0 / 100.0 - 1)          # P(t)/P(t−2)−1
    assert np.isclose(m.iloc[5], 161.051 / 133.1 - 1)

    m1 = momentum_classic(p, length=2, skip=1)
    assert np.isclose(m1.iloc[3], 121.0 / 100.0 - 1)         # P(t−1)/P(t−3)−1
    assert np.isclose(m1.iloc[5], 146.41 / 121.0 - 1)


def test_momentum_classic_nan_before_full_window():
    p = _gbm(30, seed=0)
    m = momentum_classic(p, length=10, skip=5)
    assert m.iloc[: 15].isna().all()      # 前 L+skip 个值无定义
    assert m.iloc[15:].notna().all()


# ---------- M2 趋势斜率 ----------

def test_momentum_slope_matches_naive_ols():
    """向量化滚动 OLS 斜率 ≡ 逐窗 np.polyfit(对数价格 ~ 时间)。"""
    p = _gbm(80, seed=1)
    L = 20
    got = momentum_slope(p, length=L)

    y = np.log(p.to_numpy())
    x = np.arange(L, dtype=float)
    for t in [L - 1, 30, 55, 79]:
        expected = np.polyfit(x, y[t - L + 1: t + 1], 1)[0]
        assert np.isclose(got.iloc[t], expected), t
    assert got.iloc[: L - 1].isna().all()


def test_momentum_slope_exact_exponential_growth():
    """纯指数增长:斜率恒等于日对数增速。"""
    idx = pd.bdate_range("2020-01-01", periods=60)
    p = pd.Series(100.0 * np.exp(0.002 * np.arange(60)), index=idx)
    got = momentum_slope(p, length=20).dropna()
    assert np.allclose(got, 0.002)


# ---------- M3 风险调整 ----------

def test_momentum_voladj_hand_anchor():
    """voladj(L)[t] = (P(t)/P(t−L)−1) / std(近 L 个日收益, ddof=1)。"""
    p = _gbm(50, seed=2)
    L = 10
    got = momentum_voladj(p, length=L)

    ret = p.pct_change()
    t = 37
    expected = (p.iloc[t] / p.iloc[t - L] - 1) / ret.iloc[t - L + 1: t + 1].std()
    assert np.isclose(got.iloc[t], expected)
    assert got.iloc[: L].isna().all()


# ---------- 因果性(三族共用) ----------

def test_momentum_functions_are_causal():
    """前缀不变性:追加未来数据不得改变历史值。"""
    p = _gbm(120, seed=3)
    for fn, kw in [
        (momentum_classic, {"length": 20, "skip": 20}),
        (momentum_slope, {"length": 20}),
        (momentum_voladj, {"length": 20}),
    ]:
        full = fn(p, **kw)
        prefix = fn(p.iloc[:80], **kw)
        pd.testing.assert_series_equal(full.iloc[:80], prefix, check_names=False)


# ---------- 配对级新家族(国信研报三候选,2026-07-10 用户确认) ----------

def test_pair_winrate_hand_anchor():
    """胜率动量 = 窗口内 sign(左腿日收益−右腿日收益) 的均值 ∈ [−1,1]。"""
    from backtest.momentum_scan import pair_winrate

    idx = pd.bdate_range("2020-01-01", periods=7)
    # 左腿日收益恒 +1%,右腿:前 3 天 +2%(左输),后 3 天 0%(左赢)
    left = pd.Series(100.0 * np.cumprod([1.0] + [1.01] * 6), index=idx)
    right_rets = [1.02, 1.02, 1.02, 1.0, 1.0, 1.0]
    right = pd.Series(100.0 * np.cumprod([1.0] + right_rets), index=idx)

    got = pair_winrate(left, right, length=6)
    assert np.isclose(got.iloc[6], 0.0)          # 3 赢 3 输
    got4 = pair_winrate(left, right, length=4)
    assert np.isclose(got4.iloc[6], 0.5)         # 近 4 天:3 赢 1 输 → (3−1)/4
    assert got.iloc[:6].isna().all()


def test_pair_smooth_hand_anchor():
    """平滑动量 = 窗口内相对收益之和 / |相对收益| 之和(位移/路程,∈[−1,1])。"""
    from backtest.momentum_scan import pair_smooth

    idx = pd.bdate_range("2020-01-01", periods=6)
    left = pd.Series(100.0 * np.cumprod([1.0, 1.01, 1.01, 1.01, 1.01, 1.01]), index=idx)
    right = pd.Series(100.0, index=idx)
    got = pair_smooth(left, right, length=5)
    assert np.isclose(got.iloc[5], 1.0)          # 单边上行:位移=路程

    # 交替 ±1%:位移≈0
    alt = pd.Series(100.0 * np.cumprod([1.0, 1.01, 0.99, 1.01, 0.99, 1.01]), index=idx)
    got_alt = pair_smooth(alt, right, length=4)
    assert abs(got_alt.iloc[5]) < 0.2


def test_pair_highdist_excludes_today_anchor():
    """高点距离 = 相对价格 / 前 L 日(不含当日)最高 − 1;创新高日为正。"""
    from backtest.momentum_scan import pair_highdist

    idx = pd.bdate_range("2020-01-01", periods=6)
    ratio_path = [1.0, 1.2, 1.1, 1.0, 0.9, 1.3]   # 用 right=const → ratio=left/100
    left = pd.Series([100.0 * r for r in ratio_path], index=idx)
    right = pd.Series(100.0, index=idx)

    got = pair_highdist(left, right, length=3)
    assert np.isclose(got.iloc[4], 0.9 / 1.2 - 1)   # 前3日 max(1.2,1.1,1.0)
    assert np.isclose(got.iloc[5], 1.3 / 1.1 - 1)   # 前3日 max(1.1,1.0,0.9),创新高为正
    assert got.iloc[:3].isna().all()


def test_pair_level_functions_are_causal():
    from backtest.momentum_scan import pair_winrate, pair_smooth, pair_highdist

    left, right = _gbm(120, seed=30), _gbm(120, seed=31)
    for fn in (pair_winrate, pair_smooth, pair_highdist):
        full = fn(left, right, length=20)
        prefix = fn(left.iloc[:80], right.iloc[:80], length=20)
        pd.testing.assert_series_equal(full.iloc[:80], prefix, check_names=False)


# ---------- 配对装配层 ----------

def test_pair_factor_downstream_matches_production_exactly():
    """恒等对拍:右腿常数时 classic(L, skip=0) 的原始序列与生产 rolling 复利
    逐点相等,故 z→tanh 下游(min_periods=z_window、STD_FLOOR、fillna 链)
    必须与生产 calculate_contrast_equal_weight_signal 在成熟区精确一致。"""
    from backtest.momentum_scan import momentum_pair_factor
    from signals.equal_weight.generate_signal import (
        calculate_contrast_equal_weight_signal,
    )

    L, zw = 20, 40
    panel = pd.DataFrame({"G": _gbm(300, seed=4), "V": 100.0})

    got = momentum_pair_factor(
        panel, pairs=[("G", "V")], family="classic",
        length=L, skip=0, z_window=zw, smoothing=0,
    )
    expected = calculate_contrast_equal_weight_signal(
        panel, lookback=L, z_window=zw, smoothing_window=0,
    )["factor_value"]

    mature = got.index[L + zw + 5:]
    assert len(mature) > 200
    assert np.allclose(got.loc[mature], expected.loc[mature])


def test_pair_factor_multi_pair_mean_and_smoothing():
    """四对等权 + 平滑:多对输出 = 各单对(smoothing=0)均值再 rolling(sm, min_periods=1) 均值。"""
    from backtest.momentum_scan import momentum_pair_factor

    panel = pd.DataFrame({
        "A": _gbm(200, seed=5), "B": _gbm(200, seed=6),
        "C": _gbm(200, seed=7), "D": _gbm(200, seed=8),
    })
    kw = dict(family="slope", length=20, skip=0, z_window=40)

    combined = momentum_pair_factor(panel, pairs=[("A", "B"), ("C", "D")], smoothing=5, **kw)
    p1 = momentum_pair_factor(panel, pairs=[("A", "B")], smoothing=0, **kw)
    p2 = momentum_pair_factor(panel, pairs=[("C", "D")], smoothing=0, **kw)
    expected = ((p1 + p2) / 2.0).rolling(5, min_periods=1).mean()

    pd.testing.assert_series_equal(combined, expected, check_names=False)


def test_classic20_skip0_correlates_with_production_above_099():
    """恒等锚(设计 §5-1):两腿皆动时 classic(20,0) 与现任仅差复利交叉项,corr>0.99。"""
    from backtest.momentum_scan import momentum_pair_factor
    from signals.equal_weight.generate_signal import (
        calculate_contrast_equal_weight_signal,
    )

    cols = {}
    for i, name in enumerate(["g1", "v1", "g2", "v2", "g3", "v3", "g4", "v4"]):
        cols[name] = _gbm(400, seed=10 + i)
    panel = pd.DataFrame(cols)
    pairs = [("g1", "v1"), ("g2", "v2"), ("g3", "v3"), ("g4", "v4")]

    got = momentum_pair_factor(panel, pairs=pairs, family="classic",
                               length=20, skip=0, z_window=40, smoothing=5)
    expected = calculate_contrast_equal_weight_signal(
        panel, lookback=20, z_window=40, smoothing_window=5,
    )["factor_value"]

    mature = got.index[80:]
    corr = got.loc[mature].corr(expected.loc[mature])
    assert corr > 0.99, corr


# ---------- 网格与 PG 接线 ----------

def test_momentum_grid_174_combos_families_and_ranges():
    """设计 §2 + 短窗补测 + 国信三候选:29 形态 × zw{40,120,250} × sm{0,5} = 174。"""
    from backtest.momentum_scan import momentum_grid

    combos = momentum_grid()
    assert len(combos) == 174

    forms = {(c["family"], c["length"], c["skip"]) for c in combos}
    assert len(forms) == 29
    assert {f for f, _, _ in forms} == {
        "classic", "slope", "voladj", "winrate", "smooth", "highdist"}
    assert {(l, s) for f, l, s in forms if f == "classic"} == {
        (5, 0), (10, 0),
        (60, 0), (60, 20), (120, 0), (120, 20), (250, 0), (250, 20)}
    for fam in ("slope", "voladj"):
        assert {(l, s) for f, l, s in forms if f == fam} == {
            (5, 0), (10, 0), (20, 0), (60, 0), (120, 0), (250, 0)}
    for fam in ("winrate", "smooth"):
        assert {(l, s) for f, l, s in forms if f == fam} == {(10, 0), (20, 0), (60, 0)}
    assert {(l, s) for f, l, s in forms if f == "highdist"} == {
        (60, 0), (120, 0), (250, 0)}
    assert {c["z_window"] for c in combos} == {40, 120, 250}
    assert {c["smoothing"] for c in combos} == {0, 5}


def test_pair_factor_dispatches_pair_level_families():
    """配对级家族走 pair 分派:winrate 装配 = _zscore_tanh(pair_winrate(左,右), zw)。"""
    from backtest.momentum_scan import (
        _zscore_tanh, momentum_pair_factor, pair_winrate,
    )

    panel = pd.DataFrame({"G": _gbm(200, seed=40), "V": _gbm(200, seed=41)})
    got = momentum_pair_factor(panel, pairs=[("G", "V")], family="winrate",
                               length=20, skip=0, z_window=40, smoothing=0)
    expected = _zscore_tanh(pair_winrate(panel["G"], panel["V"], length=20), 40)
    expected = expected.reindex(panel.index).fillna(0.0)
    pd.testing.assert_series_equal(got, expected, check_names=False)


def test_momentum_factor_fn_wires_pg_panel_and_pair_configs(monkeypatch):
    """接线:PG 8 列一次加载,配对取 config_4pairs 的 effective_columns(含 direction)。"""
    import signals.common.data_source as ds
    from backtest.momentum_scan import momentum_factor_fn, momentum_pair_factor
    from signals.equal_weight.generate_signal import load_pair_configs

    names = ["沪深300成长", "沪深300价值", "中证500成长", "中证500价值",
             "中证1000成长", "中证1000价值", "中证2000成长", "中证2000价值"]
    rng = np.random.default_rng(20)
    idx = pd.bdate_range("2015-01-01", periods=250)
    panel = pd.DataFrame(
        {c: 100.0 * np.exp(np.cumsum(rng.normal(0.0003, 0.012, 250))) for c in names},
        index=idx,
    )

    def fake_load(got_names, start=None, end=None, trim_ragged_tail=False):
        assert got_names == names
        return panel[got_names].copy()

    monkeypatch.setattr(ds, "load_pg_closes", fake_load)

    fn = momentum_factor_fn()
    got = fn(family="voladj", length=60, skip=0, z_window=120, smoothing=5)

    configs = load_pair_configs(ROOT / "signals/equal_weight/config_4pairs.csv")
    pairs = [cfg.effective_columns() for cfg in configs]
    expected = momentum_pair_factor(panel, pairs=pairs, family="voladj",
                                    length=60, skip=0, z_window=120, smoothing=5)
    pd.testing.assert_series_equal(got, expected, check_names=False)


def test_pick_plateau_representatives_selects_on_train_val_only():
    """高原代表 = 每族 train/val 两窗最差值最大者；holdout 只报告不进选择
    (2026-07-11 审查修正:holdout 进选择会消耗 OOS)。"""
    from backtest.momentum_scan import pick_plateau_representatives

    rep = pd.DataFrame([
        # A:train/val 双高但 holdout 低——按旧三窗规则会落选,新规则必须选它
        {"family": "classic", "length": 120, "skip": 20, "z_window": 40, "smoothing": 0,
         "sharpe_train_14_20": 1.0, "sharpe_val_21_23": 0.9, "sharpe_holdout_24_26": 0.1},
        # B:两窗平庸但 holdout 不低——旧三窗规则的赢家(worst 0.5 > 0.1)
        {"family": "classic", "length": 60, "skip": 20, "z_window": 120, "smoothing": 5,
         "sharpe_train_14_20": 0.5, "sharpe_val_21_23": 0.6, "sharpe_holdout_24_26": 0.7},
        # slope:单行
        {"family": "slope", "length": 60, "skip": 0, "z_window": 40, "smoothing": 5,
         "sharpe_train_14_20": 0.5, "sharpe_val_21_23": 0.6, "sharpe_holdout_24_26": 0.4},
    ])
    got = pick_plateau_representatives(rep)

    assert list(got["family"]) == ["classic", "slope"]
    classic = got[got["family"] == "classic"].iloc[0]
    assert classic["length"] == 120 and classic["z_window"] == 40
    assert np.isclose(classic["worst_window"], 0.9)   # min(train,val)，holdout 不参与
    assert np.isclose(got[got["family"] == "slope"].iloc[0]["worst_window"], 0.5)
