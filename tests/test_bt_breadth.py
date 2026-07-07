import sys
from pathlib import Path

import pandas as pd
import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))


def _panel():
    """长表复权 close：A 单调升、B 走平后跌、C 单调跌。"""
    data = {"A": [10, 11, 12, 13], "B": [10, 10, 10, 9], "C": [10, 9, 8, 7]}
    dates = pd.to_datetime(["2020-01-01", "2020-01-02", "2020-01-03", "2020-01-06"])
    rows = [{"ts_code": code, "trade_date": d, "close": c}
            for code, closes in data.items() for d, c in zip(dates, closes)]
    return pd.DataFrame(rows), dates


def test_pct_above_ma_strict_greater():
    """%>MA：某日 close 严格大于自身 L 日均线的股票占比（denom=有满窗的股票）。"""
    from backtest.breadth import compute_breadth
    prices, dates = _panel()
    b = compute_breadth(prices, ma_windows=(3,), hilo_windows=())
    # 前两日无满 3 窗 → 无值；d3/d4 只有 A 在均线上 → 1/3
    assert b.loc[dates[2], "pct_above_ma3"] == pytest.approx(1 / 3)
    assert b.loc[dates[3], "pct_above_ma3"] == pytest.approx(1 / 3)


def test_hi_lo_diff_new_high_minus_new_low():
    """新高−新低：(N 日新高家数 − N 日新低家数)/活跃家数。d4: A 新高、B/C 新低 → (1−2)/3。"""
    from backtest.breadth import compute_breadth
    prices, dates = _panel()
    b = compute_breadth(prices, ma_windows=(), hilo_windows=(3,))
    assert b.loc[dates[3], "hi_lo_diff3"] == pytest.approx(-1 / 3)


def test_min_active_drops_sparse_dates():
    """活跃股票数 < min_active 的交易日（如未灌全的当日）被剔除，避免伪广度。"""
    from backtest.breadth import compute_breadth
    prices, dates = _panel()
    d5 = pd.Timestamp("2020-01-07")  # 稀疏日：仅 A 一只成交
    sparse = pd.concat(
        [prices, pd.DataFrame([{"ts_code": "A", "trade_date": d5, "close": 14}])],
        ignore_index=True)
    b = compute_breadth(sparse, ma_windows=(3,), hilo_windows=(), min_active=2)
    assert d5 not in b.index          # 稀疏日剔除
    assert dates[3] in b.index        # 正常日（3 只）保留


def test_breadth_divergence_f1_deteriorating():
    """F1 走弱：价 P 日新高 且 breadth_t < breadth_{t−Q} → 空触发 −1；仅价新高不够。"""
    from backtest.breadth import breadth_divergence
    idx = pd.RangeIndex(5)
    underlying = pd.Series([10, 11, 12, 11, 13], index=idx, dtype=float)
    breadth = pd.Series([0.5, 0.6, 0.55, 0.4, 0.45], index=idx)
    sig = breadth_divergence(underlying, breadth, new_high_window=3,
                             div_lookback=2, form="deteriorating")
    assert sig.iloc[4] == -1      # 新高(13) + 走弱(0.45<0.55)
    assert sig.iloc[2] == 0       # 新高(12)但未走弱(0.55≥0.50)
    assert int(sig.sum()) == -1   # 全程仅一次触发


def test_breadth_divergence_hold_extends_short():
    """持有期：触发后持有 short H 日（事件型对冲需持有穿越下跌，非 1 日 blip）。"""
    from backtest.breadth import breadth_divergence
    idx = pd.RangeIndex(7)
    underlying = pd.Series([10, 11, 12, 11, 10, 9, 8], index=idx, dtype=float)  # 仅 d2 新高
    breadth = pd.Series([0.5, 0.6, 0.55, 0.7, 0.7, 0.7, 0.7], index=idx)        # 仅 d2 走弱
    kw = dict(new_high_window=3, div_lookback=1, form="deteriorating")
    assert list(breadth_divergence(underlying, breadth, hold=1, **kw)) == [0, 0, -1, 0, 0, 0, 0]
    assert list(breadth_divergence(underlying, breadth, hold=3, **kw)) == [0, 0, -1, -1, -1, 0, 0]


def test_breadth_divergence_f2_nonconfirm():
    """F2 未同步新高：价 P 日新高 且 breadth < 自身 P 日滚动高 → −1；breadth 处自身高则确认(0)。"""
    from backtest.breadth import breadth_divergence
    idx = pd.RangeIndex(5)
    underlying = pd.Series([10, 11, 12, 11, 13], index=idx, dtype=float)
    breadth = pd.Series([0.50, 0.52, 0.55, 0.40, 0.45], index=idx)
    sig = breadth_divergence(underlying, breadth, new_high_window=3, form="nonconfirm")
    assert sig.iloc[4] == -1      # 新高 + breadth(0.45)<P日高(0.55)
    assert sig.iloc[2] == 0       # 新高但 breadth(0.55)=自身P日高 → 确认
    assert int(sig.sum()) == -1


def test_breadth_divergence_f3_low_pct():
    """F3 低分位：价 P 日新高 且 breadth < 其 Q 窗 threshold 分位 → −1。"""
    from backtest.breadth import breadth_divergence
    idx = pd.RangeIndex(5)
    underlying = pd.Series([10, 11, 12, 11, 13], index=idx, dtype=float)
    breadth = pd.Series([0.50, 0.60, 0.55, 0.60, 0.40], index=idx)
    sig = breadth_divergence(underlying, breadth, new_high_window=3,
                             div_lookback=3, form="low_pct", threshold=0.5)
    assert sig.iloc[4] == -1      # 新高 + breadth(0.40)<中位数(0.55)
    assert sig.iloc[2] == 0       # 新高但 breadth(0.55)=中位数 → 不触发
    assert int(sig.sum()) == -1


def test_build_breadth_caches_and_returns(tmp_path):
    """build_breadth 组合 fetch→compute→缓存：注入假 fetch 避开 PG，验证列 + 缓存可复读。"""
    from backtest.breadth import build_breadth
    prices, dates = _panel()
    cache = tmp_path / "breadth.csv"
    df = build_breadth(db=None, start=None, cache_path=cache,
                       ma_windows=(3,), hilo_windows=(3,), min_active=1,
                       fetch=lambda db, start: prices)
    assert "pct_above_ma3" in df.columns and "hi_lo_diff3" in df.columns
    assert cache.exists()
    reread = pd.read_csv(cache, index_col=0)
    assert reread.loc[str(dates[3].date()), "hi_lo_diff3"] == pytest.approx(-1 / 3)
