"""风格仪表盘数据层（dashboard/data）：纯函数 + committed 产物契约 TDD。

loader 直接读仓库 committed CSV（output/ 与 backtest/output/），列契约即测试；
PG 两融装载不进单测（复用已测的 leverage_probe._load_margin，集成路径）。
"""
import sys
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from dashboard.data import (  # noqa: E402
    data_bundle,
    freshness,
    load_breadth,
    load_signals_status,
    load_style_meter,
    load_thermometer,
    load_turnover,
    rebase_indices,
    rolling_percentile,
    slice_range,
    trim_incomplete_tail,
    trim_zero_return_tail,
)


# ---------------------------------------------------------------- rolling_percentile
def test_rolling_percentile_last_of_increasing_window_is_one():
    """严格递增 → 每日都是窗口内最高 → 分位=1；窗口未满 → NaN。"""
    s = pd.Series(np.arange(10.0), index=pd.bdate_range("2024-01-01", periods=10))
    got = rolling_percentile(s, window=5)
    assert got.iloc[:4].isna().all()
    assert (got.iloc[4:] == 1.0).all()


def test_rolling_percentile_middle_value():
    """窗口 [1..5] 末值 3 → rank 3/5 = 0.6。"""
    s = pd.Series([1.0, 2.0, 4.0, 5.0, 3.0])
    got = rolling_percentile(s, window=5)
    assert got.iloc[-1] == pytest.approx(0.6)


# ---------------------------------------------------------------- freshness
def test_freshness_reports_last_valid_date_per_source():
    a = pd.Series([1.0, 2.0], index=pd.to_datetime(["2026-07-01", "2026-07-07"]))
    b = pd.Series([1.0, np.nan], index=pd.to_datetime(["2026-06-17", "2026-06-18"]))
    got = freshness({"两融": a, "信号": b})
    assert got["两融"] == pd.Timestamp("2026-07-07")
    assert got["信号"] == pd.Timestamp("2026-06-17")  # 尾部 NaN 不算新鲜


# ---------------------------------------------------------------- committed 产物契约
def test_load_style_meter_contract():
    df = load_style_meter()
    assert {"growth_index", "value_index", "spread", "signal"} <= set(df.columns)
    assert df.index.is_monotonic_increasing and isinstance(df.index, pd.DatetimeIndex)
    assert len(df) > 3000  # 2013-06 起日度


def test_load_thermometer_contract_with_percentiles():
    df = load_thermometer()
    assert {"lu_ratio", "burst_rate", "lu_premium",
            "lu_ratio_pct", "burst_rate_pct", "lu_premium_pct"} <= set(df.columns)
    assert df[["lu_ratio_pct"]].dropna().iloc[-1, 0] <= 1.0


def test_load_turnover_and_breadth_contract():
    amt = load_turnover()
    assert isinstance(amt, pd.Series) and len(amt) > 8000
    br = load_breadth()
    assert {"pct_above_ma20", "pct_above_ma60", "hi_lo_diff20"} <= set(br.columns)


def test_load_signals_status_three_lines():
    """状态条：三线各出 name/factor/date/position，position∈{0,1}。"""
    rows = load_signals_status()
    assert [r["name"] for r in rows] == ["equal_weight", "hybrid20", "citic40d"]
    for r in rows:
        assert np.isfinite(r["factor"])
        assert r["position"] in (0, 1)
        assert isinstance(r["date"], pd.Timestamp)


def test_committed_signal_and_position_dates_in_sync():
    """守卫：committed 信号 CSV 与 recommended 持仓 CSV 末日必须一致——
    更新信号后忘跑 python3 -m backtest.production 时在这里挂掉，
    别让仪表盘把旧仓位当现状展示（2026-07-11 审查修正）。"""
    for r in load_signals_status():
        assert r["pos_date"] == r["date"], (
            f"{r['name']}: 持仓截至 {r['pos_date']:%Y-%m-%d} != "
            f"信号截至 {r['date']:%Y-%m-%d}，重跑 python3 -m backtest.production")


# ---------------------------------------------------------------- slice_range
def test_slice_range_windows_anchor_on_last_date():
    """1y/3y/5y 按末日回溯，all 原样返回；Series 与 DataFrame 通吃。"""
    idx = pd.bdate_range("2020-01-01", "2026-07-01")
    s = pd.Series(1.0, index=idx)
    assert slice_range(s, "all").equals(s)
    got = slice_range(s, "1y")
    assert got.index.min() >= pd.Timestamp("2025-07-01")
    assert got.index.max() == idx.max()
    df = pd.DataFrame({"a": s})
    assert slice_range(df, "3y").index.min() >= pd.Timestamp("2023-07-01")


# ---------------------------------------------------------------- trim_incomplete_tail
def test_trim_incomplete_tail_drops_collapsed_last_days():
    """上游只灌了部分股票的尾部日（activity 崩到中位的零头）→ 剔除。"""
    idx = pd.bdate_range("2026-06-01", periods=22)
    vals = [5100.0] * 20 + [5080.0, 11.0]   # 末日 = 2026-07-01 式垃圾日
    s = pd.Series(vals, index=idx)
    got = trim_incomplete_tail(s)
    assert got.index.max() == idx[-2]
    assert len(got) == 21


def test_trim_incomplete_tail_keeps_healthy_tail_and_mid_dips():
    """健康尾部原样；历史中段的真实低活跃日不误删（守卫只作用于尾部）。"""
    idx = pd.bdate_range("2026-06-01", periods=22)
    vals = [5100.0] * 10 + [2000.0] + [5100.0] * 11   # 中段低点是真实历史
    s = pd.Series(vals, index=idx)
    got = trim_incomplete_tail(s)
    assert got.equals(s)


# ---------------------------------------------------------------- trim_zero_return_tail
def test_trim_zero_return_tail_drops_placeholder_days():
    """build 时点上游未更 → CSV 尾部出现两腿收益≈0 的占位日 → 剔除；
    中段真实平静日（单腿小、双腿不同时≈0）不动。"""
    idx = pd.bdate_range("2026-06-24", periods=6)
    df = pd.DataFrame({
        "growth_ret": [0.005, 0.0, 0.02, 0.005, 0.0, 0.0],
        "value_ret": [0.004, 0.003, 0.006, 0.004, 0.00006, 0.0],
    }, index=idx)   # 末两日=占位；第 2 日 growth=0 但 value 正常 → 保留
    got = trim_zero_return_tail(df, ["growth_ret", "value_ret"])
    assert got.index.max() == idx[3]
    assert len(got) == 4


# ---------------------------------------------------------------- rebase_indices
def test_rebase_indices_normalizes_to_window_start():
    """切窗后两腿归一到窗口首日=1，窗口内相对走势可比。"""
    idx = pd.bdate_range("2026-01-01", periods=5)
    df = pd.DataFrame({"growth_index": [1.5, 1.65, 1.8, 1.65, 1.5],
                       "value_index": [5.0, 5.0, 5.5, 6.0, 5.0],
                       "signal": [0.1] * 5}, index=idx)
    got = rebase_indices(df, ["growth_index", "value_index"])
    assert got["growth_index"].iloc[0] == pytest.approx(1.0)
    assert got["value_index"].iloc[0] == pytest.approx(1.0)
    assert got["value_index"].iloc[3] == pytest.approx(1.2)
    assert (got["signal"] == 0.1).all()          # 非指数列不动
    assert df["value_index"].iloc[0] == 5.0      # 不改原 df


# ---------------------------------------------------------------- margin TTL 缓存
def test_load_margin_uses_ttl_cache(monkeypatch):
    """PG 查询 ~7s 是页面瓶颈 → TTL 内复用，range 切换秒回。"""
    import backtest.leverage_probe as lp
    from dashboard import data as D

    calls = {"n": 0}

    def fake(db=None):
        calls["n"] += 1
        idx = pd.bdate_range("2025-01-01", periods=300)
        return pd.Series(1e8, index=idx), pd.Series(1e6, index=idx)

    monkeypatch.setattr(lp, "_load_margin", fake)
    D._MARGIN_CACHE.clear()
    a = D.load_margin()
    b = D.load_margin()
    assert calls["n"] == 1
    assert a is b
    D._MARGIN_CACHE.clear()


# ---------------------------------------------------------------- bundle
def test_data_bundle_without_margin_has_all_panels():
    b = data_bundle(include_margin=False)
    assert {"signals", "style", "thermo", "turnover", "breadth", "freshness"} <= set(b)
    assert b["margin"] is None
    assert "风格测量仪" in b["freshness"]
