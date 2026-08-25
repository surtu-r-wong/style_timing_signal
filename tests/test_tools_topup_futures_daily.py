"""`tools/topup_futures_daily.py` 的纯函数判例（2026-08-25 立）。

取数与写库要网关/writer，不在单测里跑；这里钉住三件会静默出错的事：
列名映射、后端必需列补齐、以及「oi 全空 = carry 算不出」的体检。
"""
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tools"))

import topup_futures_daily as t  # noqa: E402


def test_to_records_renames_wind_columns_to_table_columns():
    """Wind 的 `amt` 是 `public.futures_daily` 的 `turnover`；`ts_code` 是 `symbol`。"""
    cols = ["ts_code", "trade_date", "OPEN", "close", "amt", "oi"]
    rows = [["IC2609.CFE", "2026-08-03", 5800.0, 5820.2, 1.23e9, 41234]]
    (rec,) = t.to_records(cols, rows)
    assert rec["symbol"] == "IC2609.CFE"
    assert rec["turnover"] == 1.23e9
    assert rec["open"] == 5800.0          # 大写列名要小写化
    assert rec["oi"] == 41234


def test_to_records_fills_backend_required_columns_with_none():
    """后端 build_insert_sql 强取这 8 列 —— 缺列会 KeyError，必须补 None 而非省略。"""
    (rec,) = t.to_records(["ts_code", "trade_date", "close"],
                          [["IM2609.CFE", "2026-08-03", 6100.0]])
    for col in ("open", "high", "low", "volume", "oi", "turnover", "settle"):
        assert col in rec and rec[col] is None
    assert rec["close"] == 6100.0


def test_to_records_does_not_overwrite_present_values_with_none():
    """`setdefault` 语义：已有值不能被补 None 覆盖（写反了会把整批数据清空）。"""
    (rec,) = t.to_records(["ts_code", "trade_date", "oi", "settle"],
                          [["IC2609.CFE", "2026-08-03", 41234, 5815.0]])
    assert rec["oi"] == 41234 and rec["settle"] == 5815.0


def test_carry_readiness_flags_all_null_oi():
    """oi 全空 = 网关 fetchers 字段没配对（/fetch/price 是股票口径，不含 oi）。

    这条体检存在的理由：不做的话脚本会「成功」写入一批 oi 全 NULL 的行，
    carry 依旧算不出，而且看起来像数据本身没有持仓量。
    """
    recs = t.to_records(["ts_code", "trade_date", "close"],
                        [["IC2609.CFE", "2026-08-03", 5820.2],
                         ["IC2609.CFE", "2026-08-04", 5830.0]])
    assert t.carry_readiness(recs) == {"close": 2, "oi": 0, "rows": 2}


def test_carry_readiness_counts_partial_coverage():
    recs = [{"close": 1.0, "oi": 10}, {"close": 2.0, "oi": None}, {"close": None, "oi": 30}]
    assert t.carry_readiness(recs) == {"close": 2, "oi": 2, "rows": 3}


def test_no_proxy_session_disables_env_proxies():
    """`trust_env=False` —— 带 Clash 代理时 gateway 与两个 writer 全部假 502。"""
    assert t._no_proxy_session().trust_env is False


def test_writer_conf_rejects_missing_keys():
    with pytest.raises(ValueError, match="market_monitor_writer"):
        t.writer_conf({"market_monitor_writer": {"primary_url": "http://x/api"}})


def test_gateway_conf_rejects_missing_keys():
    with pytest.raises(ValueError, match="wind_gateway"):
        t.gateway_conf({"wind_gateway": {"url": "http://x"}})
