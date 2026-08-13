"""⑤b 红利腿换伙伴补测探针（`backtest/pair_set_probe_5b.py`）的单元判例。

预登记：`docs/plans/2026-08-13-probe-5b-dividend-partner-prereg.md`（§0-§5 冻结）。
判例风格与覆盖面照抄 Batch 11 的 `tests/test_bt_pair_set_probe.py`，加两类 5b 特有的钉子：

1. **单候选**：集合恰 2 个（现役 A + 候选 E），E 恰 5 对，无权重自由度；
   红利腿 = `沪深300`(左) vs `中证红利`(右)，**价格版**（H00922 全收益版是 Batch 11 的教训，
   本次任何地方都不许出现）；α=0.05 **不做 Bonferroni**（m=1）。
2. **数据完备性 preflight**：伙伴腿对齐八风格日历零缺值，缺一天即 `DataIncompleteError`
   （DATA_INCOMPLETE 语义，错误里带缺失日期清单）——这是正式跑的硬闸。
   ⚠️ 本判例集**不连库**（仓规矩：测试只用合成数据 / committed CSV）。
"""
import inspect
import re
import sys
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import backtest.pair_set_probe as psp  # noqa: E402
import backtest.pair_set_probe_5b as m5b  # noqa: E402
from backtest import fusion_probe  # noqa: E402
from signals.equal_weight.generate_signal import (  # noqa: E402
    INPUT_FILE, calculate_contrast_equal_weight_signal, load_pair_configs, load_price_data,
)

ALL_COLUMNS = list(m5b.STYLE_NAMES) + list(m5b.DIVIDEND_LEG)


@pytest.fixture
def prices() -> pd.DataFrame:
    """合成价格宽表（10 列 = 八条风格 + 沪深300 + 中证红利），随机游走、恒正。"""
    rng = np.random.default_rng(11)
    idx = pd.bdate_range("2015-01-01", periods=520)
    out = {}
    for i, col in enumerate(ALL_COLUMNS):
        steps = rng.normal(0.0002, 0.011, len(idx))
        out[col] = 1000.0 * np.exp(np.cumsum(steps)) * (1 + 0.01 * i)
    return pd.DataFrame(out, index=idx)


@pytest.fixture
def long_prices() -> pd.DataFrame:
    """跨 train/val 两窗的合成价格（2014-01-02 起 2600 个工作日 ≈ 到 2023 年底）。"""
    rng = np.random.default_rng(7)
    idx = pd.bdate_range("2014-01-02", periods=2600)
    out = {}
    for i, col in enumerate(ALL_COLUMNS):
        steps = rng.normal(0.0002, 0.011, len(idx))
        out[col] = 1000.0 * np.exp(np.cumsum(steps)) * (1 + 0.01 * i)
    return pd.DataFrame(out, index=idx)


def _series(idx, rng, mu=0.0003, sd=0.012) -> pd.Series:
    return pd.Series(rng.normal(mu, sd, len(idx)), index=idx)


# ---------------------------------------------------------------- 1. 预登记完整性
def test_registered_sets_are_exactly_incumbent_plus_one_candidate():
    assert list(m5b.SETS) == [m5b.INCUMBENT, m5b.CANDIDATE]
    assert m5b.INCUMBENT == "A_four_pairs_incumbent"
    assert m5b.SETS[m5b.INCUMBENT] == ("300", "500", "1000", "2000")
    assert m5b.CHALLENGERS == (m5b.CANDIDATE,) and len(m5b.CHALLENGERS) == 1


def test_candidate_set_membership_is_exactly_four_plus_dividend_leg():
    assert m5b.SETS[m5b.CANDIDATE] == ("300", "500", "1000", "2000", m5b.DIVIDEND_LEG_KEY)
    cfgs = m5b.pair_configs_for(m5b.CANDIDATE)
    assert len(cfgs) == 5
    assert [p.group for p in cfgs] == [1, 2, 3, 4, 5]


def test_dividend_leg_is_broad_vs_dividend_with_hs300_on_the_left():
    """方向语义（预登记 §1）：left = 沪深300（进攻腿）、right = 中证红利（防御腿）。"""
    assert m5b.DIVIDEND_LEG == ("沪深300", "中证红利")
    assert m5b.PAIR_LEGS[m5b.DIVIDEND_LEG_KEY] == m5b.DIVIDEND_LEG
    leg = m5b.pair_configs_for(m5b.CANDIDATE)[-1]
    assert (leg.group, leg.left_column, leg.right_column, leg.direction) == \
           (5, "沪深300", "中证红利", "forward")


def test_total_return_version_is_never_used_as_a_data_code():
    """Batch 11 的直接教训：全收益版 H00922 **从不作为数据码被使用**。

    ⚠️ **名实对表**（沿用 Batch 11 QA M-1 的读法）：模块源码里**确实出现** "H00922" /
    "全收益" 字样——模块 docstring 讲这批次为什么存在、`summary.json` 的 `dividend_leg.note`。
    那些是**文档串**。本判例断言的是"从不作为数据码/列名被使用"，不是"字符串从不出现"。
    """
    assert m5b.PARTNER_CODES == {"000300.SH": "沪深300", "000922.CSI": "中证红利"}
    blob = repr(m5b.SETS) + repr(m5b.PAIR_LEGS) + repr(m5b.PARTNER_CODES) \
        + repr(m5b.DIVIDEND_LEG)
    assert "H00922" not in blob and "全收益" not in blob
    for name in m5b.SETS:
        cols = [c for p in m5b.pair_configs_for(name)
                for c in (p.left_column, p.right_column)]
        assert not any(("H00922" in c) or ("全收益" in c) for c in cols)


def test_parameters_are_locked_to_production_values_by_import():
    """参数不是本模块另写的字面量，而是 Batch 11 常量的**同一对象**。"""
    assert (m5b.LOOKBACK, m5b.Z_WINDOW, m5b.SMOOTHING) == (20, 40, 5)
    assert (m5b.LOOKBACK, m5b.Z_WINDOW, m5b.SMOOTHING) == \
           (psp.LOOKBACK, psp.Z_WINDOW, psp.SMOOTHING)
    assert (m5b.K_FORWARD, m5b.IC_OFFSET) == (psp.K_FORWARD, psp.IC_OFFSET) == (20, 0)
    assert m5b.GATE_WORST_TV_LIFT == psp.GATE_WORST_TV_LIFT == 0.15
    assert m5b.DIV_CORR_GATE == psp.DIV_CORR_GATE == 0.9
    src = (ROOT / "backtest/pair_set_probe_5b.py").read_text(encoding="utf-8")
    for name in ("LOOKBACK", "Z_WINDOW", "SMOOTHING", "K_FORWARD",
                 "GATE_WORST_TV_LIFT", "DIV_CORR_GATE", "ALPHA"):
        assert not re.search(rf"(?m)^{name}\s*=", src), f"{name} 不得在 5b 里另写字面量"


def test_single_candidate_means_no_bonferroni():
    """单候选 → α=0.05（95% CI），m=1；n_boot/seed 按预登记 §3。"""
    assert m5b.ALPHA == psp.ALPHA == 0.05
    assert m5b.BONFERRONI_M == 1
    assert m5b.GATE0_ALPHA == 0.05
    assert (m5b.N_BOOT, m5b.SEED) == (10000, 0)


def test_incumbent_configs_delegate_to_batch11_module():
    """现役 A 不是重写的，是 Batch 11 `pair_configs_for` 的逐字段同值。"""
    mine = m5b.pair_configs_for(m5b.INCUMBENT)
    theirs = psp.pair_configs_for(psp.INCUMBENT)
    assert [(p.group, p.left_column, p.right_column, p.direction) for p in mine] == \
           [(p.group, p.left_column, p.right_column, p.direction) for p in theirs]


def test_incumbent_set_matches_frozen_config_4pairs_file():
    frozen = load_pair_configs(ROOT / "signals/equal_weight/config_4pairs.csv")
    mine = m5b.pair_configs_for(m5b.INCUMBENT)
    assert [(p.group, p.left_column, p.right_column, p.direction) for p in mine] == \
           [(p.group, p.left_column, p.right_column, p.direction) for p in frozen]


def test_incumbent_factor_identical_to_frozen_root_call(prices):
    frozen = calculate_contrast_equal_weight_signal(
        prices, lookback=m5b.LOOKBACK, z_window=m5b.Z_WINDOW,
        smoothing_window=m5b.SMOOTHING,
        pair_configs=load_pair_configs(ROOT / "signals/equal_weight/config_4pairs.csv"),
    )["factor_value"].astype(float)
    pd.testing.assert_series_equal(m5b.build_factor(prices, m5b.INCUMBENT), frozen,
                                   check_names=False)


def test_frozen_index_codes_csv_has_no_dividend_mapping_and_is_not_touched():
    """冻结根 `index_codes.csv` 按字节钉死：红利码走只读包装器，不进映射表。"""
    codes = (ROOT / "signals/common/index_codes.csv").read_text(encoding="utf-8-sig")
    assert "000922" not in codes and "中证红利" not in codes
    # 只读直查是 Batch 11 的同一支包装器（不另写一支）
    assert m5b.load_dividend_closes is psp.load_dividend_closes


def test_unknown_set_is_rejected():
    with pytest.raises(KeyError):
        m5b.pair_configs_for("F_six_pairs")


# ---------------------------------------------------------------- 2. 等权、无权重自由度
@pytest.mark.parametrize("name", ["A_four_pairs_incumbent", "E_four_plus_dividend_leg"])
def test_set_factor_is_plain_mean_of_its_pair_factors(prices, name):
    legs = m5b.build_pair_factors(prices)
    want = sum(legs[k] for k in m5b.SETS[name]) / len(m5b.SETS[name])
    pd.testing.assert_series_equal(m5b.build_factor(prices, name), want, check_names=False)


def test_candidate_is_five_pair_mean_and_differs_from_incumbent(prices):
    a = m5b.build_factor(prices, m5b.INCUMBENT)
    e = m5b.build_factor(prices, m5b.CANDIDATE)
    assert float((a - e).abs().max()) > 1e-6
    legs = m5b.build_pair_factors(prices)
    want = (legs["300"] + legs["500"] + legs["1000"] + legs["2000"]
            + legs[m5b.DIVIDEND_LEG_KEY]) / 5.0
    pd.testing.assert_series_equal(e, want, check_names=False)


def test_no_weight_parameter_anywhere():
    """构造函数只有 (prices, set_name)——没有可扫的权重自由度（预登记明写不做权重优化）。"""
    assert list(inspect.signature(m5b.build_factor).parameters) == ["prices", "set_name"]
    assert list(inspect.signature(m5b.pair_configs_for).parameters) == ["set_name"]
    src = (ROOT / "backtest/pair_set_probe_5b.py").read_text(encoding="utf-8")
    assert "weights" not in src


def test_dividend_leg_sign_is_positive_when_broad_beats_dividend():
    """+z = 宽基跑赢红利 = 风险偏好上行（预登记 §1 的先验经济符号）。"""
    rng = np.random.default_rng(21)
    idx = pd.bdate_range("2015-01-01", periods=300)
    rel = np.concatenate([rng.normal(0.0, 0.004, 280), np.full(20, 0.012)])
    div = 1000.0 * np.exp(np.cumsum(rng.normal(0.0, 0.008, len(idx))))
    frame = pd.DataFrame({c: 1000.0 + np.arange(len(idx)) * 0.1 for c in m5b.STYLE_NAMES},
                         index=idx)
    frame["中证红利"] = div
    frame["沪深300"] = div * np.exp(np.cumsum(rel))
    leg = m5b.build_pair_factors(frame)[m5b.DIVIDEND_LEG_KEY]
    assert float(leg.iloc[-1]) > 0.5
    # 方向对调 → 符号翻负（left/right 不是可随手互换的自由度）
    flipped = frame.rename(columns={"沪深300": "中证红利", "中证红利": "沪深300"})
    assert float(m5b.build_pair_factors(flipped)[m5b.DIVIDEND_LEG_KEY].iloc[-1]) < -0.5


# ---------------------------------------------------------------- 3. 溯源锚点（离线）
def test_production_trace_tolerance_is_the_round4_bound():
    assert m5b.PRODUCTION_TRACE_TOL == 5e-05


def test_production_trace_diff_reads_committed_csv():
    ref = psp.load_production_signal()
    same = m5b.production_trace_diff(ref)
    assert same["max_abs_diff"] == pytest.approx(0.0, abs=1e-12)
    assert same["n_overlap"] == len(ref) and same["within_tolerance"] is True
    off = m5b.production_trace_diff(ref + 1e-3)
    assert off["max_abs_diff"] > m5b.PRODUCTION_TRACE_TOL
    assert off["within_tolerance"] is False


def test_incumbent_rebuild_matches_committed_production_csv_within_round4_bound():
    """溯源锚：从**冻结根的 committed 价格 CSV** 重建的 A == 线上那条信号（round(4) 界内）。

    离线口径（仓规矩：判例不连库）。已知偏离：`data/成长价值指数_2014.csv` 的**末尾几行**
    与库内已修订的中证1000/2000 值不一致（CSV 未同步），故偏离只允许落在价格 CSV 末 5 行。
    """
    px = load_price_data(INPUT_FILE)
    a = m5b.build_factor(px, m5b.INCUMBENT)
    ref = psp.load_production_signal()
    idx = a.index.intersection(ref.index)
    assert len(idx) > 3000
    d = (a.reindex(idx) - ref.reindex(idx)).abs()
    stale_tail = set(a.index[-5:])
    assert set(d.index[d > m5b.PRODUCTION_TRACE_TOL]) <= stale_tail
    assert float(d.drop(index=[t for t in stale_tail if t in d.index]).max()) \
        <= m5b.PRODUCTION_TRACE_TOL


# ---------------------------------------------------------------- 4. 数据完备性 preflight
def _panel_with_hole(hole: list[str] | None) -> pd.DataFrame:
    idx = pd.bdate_range("2014-01-02", periods=200)
    frame = pd.DataFrame({c: 1000.0 + np.arange(len(idx)) * 0.1 for c in ALL_COLUMNS},
                         index=idx)
    if hole:
        frame.loc[pd.to_datetime(hole), "沪深300"] = np.nan
    return frame


def test_coverage_preflight_passes_on_complete_panel():
    got = m5b.check_calendar_coverage(_panel_with_hole(None))
    assert got["n_missing_total"] == 0
    assert got["n_days"] == 200
    assert got["complete"] is True
    assert got["columns_checked"] == ALL_COLUMNS


def test_coverage_preflight_raises_data_incomplete_with_the_missing_dates():
    hole = ["2014-05-19", "2014-05-20", "2014-05-21"]
    with pytest.raises(m5b.DataIncompleteError) as err:
        m5b.check_calendar_coverage(_panel_with_hole(hole))
    msg = str(err.value)
    assert "DATA_INCOMPLETE" in msg
    assert "沪深300" in msg
    for d in hole:
        assert d in msg
    assert "3" in msg          # 缺失天数入档


def test_data_incomplete_error_is_a_value_error():
    assert issubclass(m5b.DataIncompleteError, ValueError)


def test_coverage_preflight_lists_contiguous_ranges():
    hole = ["2014-05-19", "2014-05-20", "2014-05-21", "2014-09-01"]
    with pytest.raises(m5b.DataIncompleteError) as err:
        m5b.check_calendar_coverage(_panel_with_hole(hole))
    msg = str(err.value)
    assert "2014-05-19~2014-05-21" in msg and "2014-09-01" in msg


def test_contiguous_ranges_use_calendar_adjacency_not_calendar_days():
    """长假不该把一个洞劈成两段：相邻性按**面板日历上的位置**判，不按日历日相差。"""
    cal = pd.DatetimeIndex(list(pd.bdate_range("2026-01-05", periods=5))
                           + list(pd.bdate_range("2026-02-23", periods=5)))
    hole = cal[3:7]                      # 跨越 7 周的日历空档，但在日历上是连续 4 个交易日
    assert m5b._contiguous_ranges(hole, cal) == [f"{hole[0].date()}~{hole[-1].date()}"]
    assert len(m5b._contiguous_ranges(hole)) == 2      # 无日历时退回近似 → 劈成两段


def test_preflight_message_truncates_a_very_long_missing_list():
    idx = pd.bdate_range("2014-01-02", periods=400)
    frame = pd.DataFrame({c: 1000.0 + np.arange(len(idx)) * 0.1 for c in ALL_COLUMNS},
                         index=idx)
    frame.loc[idx[100:300], "中证红利"] = np.nan
    with pytest.raises(m5b.DataIncompleteError) as err:
        m5b.check_calendar_coverage(frame)
    msg = str(err.value)
    assert "共 200 天" in msg and "中证红利 缺 200 天" in msg
    assert msg.count("2014-") + msg.count("2015-") <= m5b._MAX_LISTED_DATES + 6


def test_load_prices_runs_the_preflight(monkeypatch):
    """`load_prices` 必须过 preflight —— 库里有洞时开跑即报错，不许静默 dropna。"""
    import signals.common.data_source as ds

    idx = pd.bdate_range("2014-01-02", periods=200)
    style = pd.DataFrame({c: 1000.0 + np.arange(len(idx)) * 0.1 for c in m5b.STYLE_NAMES},
                         index=idx)
    partner = pd.DataFrame({c: 1000.0 + np.arange(len(idx)) * 0.1
                            for c in m5b.DIVIDEND_LEG}, index=idx)
    monkeypatch.setattr(ds, "load_pg_closes", lambda names, db=None, **kw: style[names])

    holed = partner.copy()
    holed.loc[holed.index[100:103], "沪深300"] = np.nan
    monkeypatch.setattr(m5b, "load_partner_closes", lambda db=None, codes=None: holed)
    with pytest.raises(m5b.DataIncompleteError):
        m5b.load_prices(db={"host": "x"})

    monkeypatch.setattr(m5b, "load_partner_closes", lambda db=None, codes=None: partner)
    got = m5b.load_prices(db={"host": "x"})
    assert list(got.columns) == ALL_COLUMNS and len(got) == 200


def test_load_prices_catches_a_partner_leg_that_starts_late(monkeypatch):
    """伙伴腿起点晚于风格日历 = 覆盖不全，同样是 DATA_INCOMPLETE（不是"起始缺失放行"）。"""
    import signals.common.data_source as ds

    idx = pd.bdate_range("2014-01-02", periods=200)
    style = pd.DataFrame({c: 1000.0 + np.arange(len(idx)) * 0.1 for c in m5b.STYLE_NAMES},
                         index=idx)
    partner = pd.DataFrame({c: 1000.0 + np.arange(160) * 0.1 for c in m5b.DIVIDEND_LEG},
                           index=idx[40:])
    monkeypatch.setattr(ds, "load_pg_closes", lambda names, db=None, **kw: style[names])
    monkeypatch.setattr(m5b, "load_partner_closes", lambda db=None, codes=None: partner)
    with pytest.raises(m5b.DataIncompleteError):
        m5b.load_prices(db={"host": "x"})


# ---------------------------------------------------------------- 5. 前置诊断闸
def _diag(max_corr: float) -> dict:
    return {"same_source_verdict": bool(max_corr > m5b.DIV_CORR_GATE),
            "max_abs_corr_vs_incumbent_family": max_corr,
            "n_days_factor_overlap": 3065}


def test_partner_gate_fires_on_high_correlation(prices):
    idx = prices.index
    rng = np.random.default_rng(3)
    base = pd.Series(rng.normal(size=len(idx)), index=idx)
    twin = base * 0.99 + 0.01 * pd.Series(rng.normal(size=len(idx)), index=idx)
    got = m5b.partner_gate(twin, base, {"300": base, m5b.DIVIDEND_LEG_KEY: twin}, prices)
    assert got["same_source_verdict"] is True
    assert got["max_abs_corr_vs_incumbent_family"] > m5b.DIV_CORR_GATE


def test_partner_gate_passes_on_low_correlation(prices):
    idx = prices.index
    rng = np.random.default_rng(4)
    a = pd.Series(rng.normal(size=len(idx)), index=idx)
    b = pd.Series(rng.normal(size=len(idx)), index=idx)
    got = m5b.partner_gate(b, a, {"300": a, m5b.DIVIDEND_LEG_KEY: b}, prices)
    assert got["same_source_verdict"] is False


def test_partner_gate_records_coverage_days(prices):
    legs = m5b.build_pair_factors(prices)
    got = m5b.partner_gate(legs[m5b.DIVIDEND_LEG_KEY],
                           m5b.build_factor(prices, m5b.INCUMBENT), legs, prices)
    assert got["n_days_aligned_panel"] == len(prices)
    assert 0 < got["n_days_factor_overlap"] <= len(prices)
    for key in ("corr_vs_incumbent_aggregate", "corr_vs_pair_300", "corr_vs_pair_500",
                "corr_vs_pair_1000", "corr_vs_pair_2000", "threshold",
                "daily_return_corr_between_two_legs", "implied_annual_drift_of_ratio"):
        assert key in got and got[key] is not None
    assert got["max_abs_corr_vs_incumbent_family"] == pytest.approx(
        max(abs(got["corr_vs_incumbent_aggregate"]),
            *[abs(got[f"corr_vs_pair_{k}"]) for k in ("300", "500", "1000", "2000")]))


def test_partner_gate_reproduces_batch11_dividend_gate_on_identical_inputs(prices):
    """不是平行实现：喂 Batch 11 的入参，逐字段读数与 `psp.dividend_gate` 相同。"""
    frame = prices.rename(columns={"沪深300": "中证红利全收益"})
    frame = frame.rename(columns={"中证红利": "中证红利_price"})
    frame = frame.rename(columns={"中证红利_price": "中证红利"})
    rng = np.random.default_rng(9)
    div = pd.Series(rng.normal(size=len(prices)), index=prices.index)
    inc = pd.Series(rng.normal(size=len(prices)), index=prices.index)
    legs = {"300": inc, "500": inc * 0.5, "1000": inc * -0.3, "2000": inc * 0.1}
    theirs = psp.dividend_gate(div, inc, {**legs, "dividend": div}, frame)
    mine = m5b.partner_gate(div, inc, {**legs, m5b.DIVIDEND_LEG_KEY: div}, frame,
                            legs=("中证红利", "中证红利全收益"))
    shared = set(theirs) & set(mine)
    assert "corr_vs_incumbent_aggregate" in shared and len(shared) >= 12
    for key in shared:
        if isinstance(theirs[key], float):
            assert mine[key] == pytest.approx(theirs[key], rel=1e-12, abs=1e-12), key
        else:
            assert mine[key] == theirs[key], key


# ---------------------------------------------------------------- 6. 闸门判定
def _panel(worst=1.0, turn=10.0, ex1=1.0,
           inc_worst=1.0, inc_turn=10.0, inc_ex1=1.0) -> pd.DataFrame:
    return pd.DataFrame([
        {"pair_set": m5b.INCUMBENT, "worst_tv_sharpe": inc_worst,
         "turnover_selection_2014_2023": inc_turn,
         "sharpe_ex_top1_selection_2014_2023": inc_ex1,
         "sharpe_ex_top2_selection_2014_2023": inc_ex1 - 0.1},
        {"pair_set": m5b.CANDIDATE, "worst_tv_sharpe": worst,
         "turnover_selection_2014_2023": turn,
         "sharpe_ex_top1_selection_2014_2023": ex1,
         "sharpe_ex_top2_selection_2014_2023": ex1 - 0.1},
    ])


def _ic(cand_ic=0.3, inc_ic=0.2, excl=True) -> tuple:
    rows, diffs = [], []
    for w in m5b.STAT_WINDOWS:
        rows.append({"pair_set": m5b.INCUMBENT, "window": w, "ic": inc_ic})
        rows.append({"pair_set": m5b.CANDIDATE, "window": w, "ic": cand_ic})
        diffs.append({"challenger": m5b.CANDIDATE, "window": w, "ci_excludes_zero": excl})
    return pd.DataFrame(rows), pd.DataFrame(diffs)


def _judge(corr=0.1, **kw) -> pd.DataFrame:
    panel = _panel(**{k: v for k, v in kw.items()
                      if k in ("worst", "turn", "ex1", "inc_worst", "inc_turn", "inc_ex1")})
    ic_rep, ic_diff = _ic(**{k: v for k, v in kw.items()
                             if k in ("cand_ic", "inc_ic", "excl")})
    return m5b.evaluate_gates(panel, ic_rep, ic_diff, _diag(corr))


def _overall(rep: pd.DataFrame) -> bool:
    row = rep[(rep["pair_set"] == m5b.CANDIDATE) & (rep["gate"] == "OVERALL")]
    assert len(row) == 1
    return bool(row["pass"].iloc[0])


def test_all_gates_pass_at_exact_boundaries():
    assert _overall(_judge(worst=1.0 + m5b.GATE_WORST_TV_LIFT, turn=10.0, ex1=1.0)) is True


@pytest.mark.parametrize("kw", [
    {"worst": 1.0 + m5b.GATE_WORST_TV_LIFT - 1e-9},            # 收益层①：差一点点
    {"worst": 1.0 + m5b.GATE_WORST_TV_LIFT, "turn": 10.001},   # 收益层②：换手上升
    {"worst": 1.0 + m5b.GATE_WORST_TV_LIFT, "ex1": 0.999},     # 收益层③：集中度恶化
    {"worst": 1.0 + m5b.GATE_WORST_TV_LIFT, "cand_ic": 0.1},   # 闸门⓪：IC 不高于现役
    {"worst": 1.0 + m5b.GATE_WORST_TV_LIFT, "excl": False},    # 闸门⓪：与现役平手
    {"worst": 1.0 + m5b.GATE_WORST_TV_LIFT, "corr": 0.95},     # 前置诊断闸：同源
])
def test_each_single_failure_flips_overall_to_stop(kw):
    assert _overall(_judge(**kw)) is False


def test_gate0_requires_both_selection_windows_higher():
    panel = _panel(worst=1.0 + m5b.GATE_WORST_TV_LIFT)
    ic_rep, ic_diff = _ic()
    ic_rep.loc[(ic_rep["pair_set"] == m5b.CANDIDATE)
               & (ic_rep["window"] == m5b.STAT_WINDOWS[0]), "ic"] = 0.2
    assert _overall(m5b.evaluate_gates(panel, ic_rep, ic_diff, _diag(0.1))) is False


def test_gate_order_is_prefilter_then_gate0_then_returns():
    """判据顺序硬编码：行序 = 前置诊断闸 → ⓪ → 收益层 → OVERALL。"""
    rep = _judge(worst=1.0 + m5b.GATE_WORST_TV_LIFT)
    order = [g for g in rep["gate"].tolist()]
    assert order[0] == "PRE_partner_corr"
    assert order.index("0_rank_ic") < order.index("1_worst_tv_sharpe")
    assert order.index("1_worst_tv_sharpe") < order.index("2_turnover_not_up")
    assert order.index("2_turnover_not_up") < order.index("3_concentration")
    assert order[-1] == "OVERALL"


def test_first_failed_gate_is_recorded():
    rep = _judge(corr=0.95, worst=1.0 + m5b.GATE_WORST_TV_LIFT)
    row = rep[rep["metric"].astype(str).str.startswith("first_failed_gate")]
    assert len(row) == 1 and "PRE_partner_corr" in str(row["metric"].iloc[0])
    assert row["pass"].iloc[0] is None


def test_first_failed_gate_reads_the_earliest_failure_in_prereg_order():
    assert m5b.GATE_ORDER == ("PRE_partner_corr", "0_rank_ic", "1_worst_tv_sharpe",
                              "2_turnover_not_up", "3_concentration")
    assert m5b.first_failed_gate(_judge(worst=1.0 + m5b.GATE_WORST_TV_LIFT)) is None
    # 前置闸与收益层同时失手 → 报最早的那一道
    both = _judge(corr=0.95, worst=0.0)
    assert m5b.first_failed_gate(both) == "PRE_partner_corr"
    assert m5b.first_failed_gate(_judge(worst=0.0)) == "1_worst_tv_sharpe"
    assert m5b.first_failed_gate(
        _judge(worst=1.0 + m5b.GATE_WORST_TV_LIFT, excl=False)) == "0_rank_ic"


def test_returns_layer_is_still_archived_when_an_earlier_gate_fails():
    """预登记 §3.5：前一道不过即 STOP，**但收益层读数仍全表入档**（供关帐引用）。"""
    rep = _judge(corr=0.95, worst=1.0 + m5b.GATE_WORST_TV_LIFT)
    assert _overall(rep) is False
    gates = set(rep["gate"])
    assert {"PRE_partner_corr", "0_rank_ic", "1_worst_tv_sharpe",
            "2_turnover_not_up", "3_concentration", "OVERALL"} <= gates


def test_holdout_window_never_appears_in_any_gate_row():
    rep = _judge(worst=1.0 + m5b.GATE_WORST_TV_LIFT)
    gate_rows = rep[rep["gate"] != "OVERALL"]
    assert not gate_rows["metric"].astype(str).str.contains("holdout|2024|2025|2026").any()
    assert not rep["gate"].astype(str).str.contains("holdout").any()


def test_diagnostic_rows_never_enter_overall():
    rep = _judge(worst=1.0 + m5b.GATE_WORST_TV_LIFT)
    assert len(rep[rep["pass"].isna()]) >= 1
    assert _overall(rep) is True


# ---------------------------------------------------------------- 7. 同秤：只 import 不重写
def test_same_scale_functions_are_the_house_ones():
    assert m5b.forward_return is fusion_probe.forward_return
    assert m5b.nonoverlap_grid is fusion_probe.nonoverlap_grid
    assert m5b.rank_ic is fusion_probe.rank_ic
    assert m5b.paired_ic_bootstrap is fusion_probe.paired_ic_bootstrap
    assert m5b.spearman_rows is fusion_probe.spearman_rows
    assert m5b.map_position is psp.map_position
    assert m5b.evaluate_windows is psp.evaluate_windows
    assert m5b.WINDOWS_REPORT is psp.WINDOWS_REPORT
    assert m5b.STAT_WINDOWS is psp.STAT_WINDOWS
    assert m5b.SAME_SCALE_FUNCTIONS == (
        fusion_probe.forward_return, fusion_probe.nonoverlap_grid, fusion_probe.rank_ic,
        fusion_probe.paired_ic_bootstrap, fusion_probe.spearman_rows,
        psp.map_position, psp.evaluate_windows)


def test_no_parallel_harness_is_defined_in_this_module():
    """同秤纪律：引擎/映射/IC 四支只 import 不重写（源码里不得出现同名 def）。"""
    src = (ROOT / "backtest/pair_set_probe_5b.py").read_text(encoding="utf-8")
    for name in ("forward_return", "nonoverlap_grid", "rank_ic", "paired_ic_bootstrap",
                 "spearman_rows", "run_strategy", "production_position",
                 "map_position", "evaluate_windows", "sharpe", "turnover"):
        assert not re.search(rf"(?m)^def {name}\b", src), f"{name} 不得在 5b 里重写"


def test_ic_report_uses_alpha_005_without_bonferroni():
    rng = np.random.default_rng(2)
    idx = pd.bdate_range("2014-01-02", periods=1900)
    und = _series(idx, rng)
    factors = {m5b.INCUMBENT: _series(idx, rng, 0.0, 0.4).rolling(5).mean().fillna(0.0),
               m5b.CANDIDATE: _series(idx, rng, 0.0, 0.4).rolling(5).mean().fillna(0.0)}
    ic_rep, ic_diff = m5b.build_ic_report(factors, und, n_boot=200, seed=0)
    assert set(ic_diff["alpha"]) == {0.05}
    assert set(ic_diff["bonferroni_m"]) == {1}
    assert set(ic_diff["challenger"]) == {m5b.CANDIDATE}
    assert set(ic_diff["reference"]) == {m5b.INCUMBENT}
    assert set(ic_rep["pair_set"]) == {m5b.INCUMBENT, m5b.CANDIDATE}


def test_ic_grid_matches_fusion_probe_nonoverlap_grid():
    rng = np.random.default_rng(6)
    idx = pd.bdate_range("2014-01-02", periods=1900)
    und = _series(idx, rng)
    fac = _series(idx, rng, 0.0, 0.4)
    factors = {m5b.INCUMBENT: fac, m5b.CANDIDATE: fac * 0.5}
    ic_rep, _ = m5b.build_ic_report(factors, und, n_boot=50, seed=0)
    fwd = fusion_probe.forward_return(und, m5b.K_FORWARD).dropna()
    common = fwd.index.intersection(fac.dropna().index)
    win = psp.WINDOWS_REPORT["train_2014_2020"]
    idx_w = common[(common >= pd.Timestamp(win[0])) & (common <= pd.Timestamp(win[1]))]
    grid = fusion_probe.nonoverlap_grid(idx_w, m5b.K_FORWARD, m5b.IC_OFFSET)
    want = fusion_probe.rank_ic(fac.reindex(grid), fwd.reindex(grid))["ic"]
    got = ic_rep[(ic_rep["pair_set"] == m5b.INCUMBENT)
                 & (ic_rep["window"] == "train_2014_2020")]["ic"].iloc[0]
    assert float(got) == pytest.approx(want, abs=1e-12)


# ---------------------------------------------------------------- 8. 管线冒烟（合成数据）
def test_evaluate_pipeline_smoke_on_synthetic_data(long_prices):
    """通路冒烟：合成价格 + 合成收益/carry 走完 evaluate()，**不连库、不出正式产物**。"""
    rng = np.random.default_rng(13)
    idx = long_prices.index
    und = {kj: _series(idx, rng) for kj in psp.KOU_JING_REPORT}
    carry = {kj: _series(idx, rng, 0.05, 0.02) for kj in psp.KOU_JING_REPORT}
    out = m5b.evaluate(long_prices, und, carry, n_boot=200, seed=0, cost_bps=3.0)

    panel = out["panel"]
    assert set(panel["pair_set"]) == {m5b.INCUMBENT, m5b.CANDIDATE}
    assert len(panel) == 2 * len(psp.MAPPINGS) * len(psp.KOU_JING_REPORT)
    assert set(panel[panel["pair_set"] == m5b.CANDIDATE]["n_pairs"]) == {5}
    assert set(panel[panel["pair_set"] == m5b.INCUMBENT]["n_pairs"]) == {4}

    verdict = out["verdict"]
    assert len(verdict[verdict["gate"] == "OVERALL"]) == 1
    assert out["summary"]["verdict"] in {"GO", "STOP"}
    assert out["summary"]["run"]["bonferroni_m"] == 1
    assert out["summary"]["run"]["alpha"] == 0.05
    assert out["summary"]["sets"] == {k: list(v) for k, v in m5b.SETS.items()}
    assert "holdout_policy" in out["summary"]["run"]
    assert set(out["diagnostics"]["metric"]) >= {"same_source_verdict",
                                                 "max_abs_corr_vs_incumbent_family"}


def test_evaluate_gate_rows_never_read_holdout(long_prices):
    rng = np.random.default_rng(17)
    idx = long_prices.index
    und = {kj: _series(idx, rng) for kj in psp.KOU_JING_REPORT}
    carry = {kj: _series(idx, rng, 0.05, 0.02) for kj in psp.KOU_JING_REPORT}
    out = m5b.evaluate(long_prices, und, carry, n_boot=100, seed=0)
    gate_rows = out["verdict"][out["verdict"]["gate"] != "OVERALL"]
    assert not gate_rows["metric"].astype(str).str.contains("holdout").any()


def test_evaluate_writes_nothing_to_the_products_directory(long_prices):
    """产物**只能由 `main()`** 落盘：import 与 `evaluate()` 都不得写 `backtest/output/`。

    ⚠️ 初版这条判例断言的是"目录里不存在 5b 产物"——那是 Batch 13 **交付阶段**的相位约束，
    正式跑落盘后必然失效（且失效方式是误报）。这里换成**长期不变式**：纯计算路径不落盘，
    判例才不会随批次相位翻脸。

    **已知边界**：快照只取 `is_file()` → **新建子目录不捕捉**（目录本身的变化看不见）；
    且对**并发写 `backtest/output/`** 敏感——与别的探针同时跑会误报。
    取向是"宁可误报不可漏报"，与它要守的不变式同向。
    """
    out_dir = ROOT / "backtest" / "output"

    def snapshot():
        return {p.name: (p.stat().st_size, p.stat().st_mtime_ns)
                for p in out_dir.iterdir() if p.is_file()}

    before = snapshot()
    rng = np.random.default_rng(23)
    idx = long_prices.index
    und = {kj: _series(idx, rng) for kj in psp.KOU_JING_REPORT}
    carry = {kj: _series(idx, rng, 0.05, 0.02) for kj in psp.KOU_JING_REPORT}
    m5b.evaluate(long_prices, und, carry, n_boot=50, seed=0)
    assert snapshot() == before
