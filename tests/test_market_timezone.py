"""中国市场日期独立于主机时区；固定时刻，数据库/通知/重计算均用离线替身。"""
import json
import sys
from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

import pandas as pd
import pytest

from deploy.daily_signals import check_freshness as freshness
from deploy.daily_signals import notify_wechat as notify
from deploy.daily_signals import topup_guard as topup
from deploy.daily_signals import wait_for_inputs as inputs


SHANGHAI = ZoneInfo("Asia/Shanghai")
NEW_YORK = ZoneInfo("America/New_York")


@pytest.fixture
def freeze(monkeypatch):
    def at(shanghai_time):
        instant = datetime.fromisoformat(shanghai_time).replace(tzinfo=SHANGHAI)

        class FixedDatetime(datetime):
            @classmethod
            def now(cls, tz=None):
                result = instant.astimezone(tz or NEW_YORK)
                return result if tz is not None else result.replace(tzinfo=None)

        class FixedDate(date):
            @classmethod
            def today(cls):
                return instant.astimezone(NEW_YORK).date()

        for module in (topup, freshness, notify, inputs):
            monkeypatch.setattr(module, "datetime", FixedDatetime)
            if hasattr(module, "date"):
                monkeypatch.setattr(module, "date", FixedDate)
        for name in ("now", "today"):
            monkeypatch.setattr(pd.Timestamp, name, classmethod(
                lambda cls, tz=None: cls(FixedDatetime.now(ZoneInfo(tz) if tz else None))))
        return instant

    return at


@pytest.mark.parametrize("local,expected", [
    ("2026-11-04 15:29:59", "2026-11-03"),
    ("2026-11-04 15:30:00", "2026-11-04"),
    ("2026-07-06 15:30:00", "2026-07-06"),  # EDT，周一
    ("2026-11-02 09:00:00", "2026-10-30"),  # 月界、DST 切换后周一
    ("2026-11-07 20:30:00", "2026-11-06"),  # 周六
])
def test_topup_cli_uses_shanghai_close(freeze, monkeypatch, capsys, local, expected):
    freeze(local)
    monkeypatch.setattr(sys, "argv", ["topup_guard", "--mode", "last-trading-day"])
    assert topup.main() == 0
    assert capsys.readouterr().out.strip() == expected


def test_topup_accepts_same_instant_in_another_timezone():
    instant = datetime(2026, 11, 4, 15, 30, tzinfo=SHANGHAI)
    assert topup.expected_last_trading_day(instant.astimezone(NEW_YORK)) == date(2026, 11, 4)


def test_audit_uses_shanghai_day_for_future_date_check(tmp_path, monkeypatch):
    path = tmp_path / "before.json"
    before = {"window_start": "2026-11-03", "max_trade_date": "2026-11-03",
              "closes": {"A": {"2026-11-03": 100.0}}}
    after = {**before, "max_trade_date": "2026-11-04",
             "closes": {"A": {"2026-11-03": 100.0, "2026-11-04": 101.0}}}
    path.write_text(json.dumps(before))
    monkeypatch.setattr(topup, "take_snapshot", lambda **kw: after)
    instant = datetime(2026, 11, 4, 9, 30, tzinfo=SHANGHAI)
    assert topup.run_audit(path, instant.astimezone(NEW_YORK)) == topup.EXIT_GO


@pytest.mark.parametrize("local", ["2026-11-04 09:30:00", "2026-07-01 00:05:00"])
def test_freshness_detects_eighth_day_even_if_new_york_is_yesterday(freeze, monkeypatch, local):
    instant = freeze(local)
    latest = (instant.date() - timedelta(days=8)).isoformat()
    monkeypatch.setattr(freshness, "load_calendar", lambda: ([latest], latest))
    monkeypatch.setattr(freshness, "evaluate", lambda *a: ({}, [], 0))
    report, ok = freshness.build_report(1)
    assert report["upstream"]["checked_on"] == instant.date().isoformat()
    assert report["upstream"]["calendar_days_behind_today"] == 8
    assert report["upstream"]["frozen"] and not ok


@pytest.mark.parametrize("explicit", [None, "2020-01-02"])
def test_push_default_date_and_explicit_override(freeze, monkeypatch, tmp_path, capsys, explicit):
    freeze("2026-11-04 09:30:00")
    status = tmp_path / "status.json"
    status.write_text("{}")
    monkeypatch.setattr(notify, "compose_message", lambda root, status, *, today: (today, today))
    args = notify.build_parser().parse_args(["--dry-run", "--status-file", str(status)])
    args.today = explicit
    assert notify.run_push(args, "") == 0
    assert capsys.readouterr().out.splitlines()[0] == (explicit or "2026-11-04")
    assert status.read_text() == "{}"


@pytest.mark.parametrize("finished,now,stale", [
    ("2026-11-03T20:30:00-05:00", "2026-11-04T09:31:00+08:00", False),
    ("2026-11-04T09:30:00+08:00", "2026-11-03T20:31:00-05:00", False),
    ("2026-11-03T10:30:00-05:00", "2026-11-03T11:31:00-05:00", True),
    ("2026-11-04 09:30:00", "2026-11-04 09:31:00", False),
])
def test_alert_compares_business_dates_after_timezone_conversion(finished, now, stale):
    assert notify._status_is_stale(finished, now, None) is stale


def test_alert_displays_market_time_with_offset(freeze, tmp_path, capsys):
    freeze("2026-11-04 09:30:00")
    status = tmp_path / "status.json"
    status.write_text(json.dumps({"finished_at": "2026-11-03T20:29:00-05:00",
                                  "failed_step": "citic40d", "result": "FAILED"}))
    args = notify.build_parser().parse_args(["--dry-run", "--status-file", str(status)])
    assert notify.run_alert(args, "") == 0
    text = capsys.readouterr().out
    assert "2026-11-04 09:30:00+08:00" in text
    assert "citic40d" in text


@pytest.mark.parametrize("local,expected", [
    ("2026-11-04 19:59:59", "2026-11-03"),
    ("2026-11-04 20:00:00", "2026-11-04"),
    ("2026-07-06 20:00:00", "2026-07-06"),
])
def test_existing_wait_clock_keeps_shanghai_ready_boundary(freeze, local, expected):
    freeze(local)
    got = inputs.expected_signal_day(inputs.shanghai_now(), inputs.parse_hhmm("20:00"), {})
    assert got.day.isoformat() == expected


@pytest.mark.parametrize("explicit", [None, "2020-01-02"])
def test_failover_default_end_and_override(freeze, monkeypatch, tmp_path, explicit):
    from deploy.failover import failover_legs
    freeze("2026-07-01 00:05:00")
    seen = []

    def drill(band, start, end):
        seen.append(end)
        return {"legs": pd.DataFrame(), "verdict": {"status": "READY"}, "comparison": {}}

    monkeypatch.setattr(failover_legs, "drill", drill)
    args = ["--output-dir", str(tmp_path)] + (["--end", explicit] if explicit else [])
    assert failover_legs.main(args) == 0
    assert seen == [explicit or "2026-07-01"]


class DateBoundaryReached(Exception):
    """在取数边界停止，避免启动财务池或腿构建。"""


@pytest.mark.parametrize("entry", ["pool", "scores", "axis"])
@pytest.mark.parametrize("explicit", [None, "2020-01-02"])
def test_research_end_dates(freeze, monkeypatch, tmp_path, entry, explicit):
    from signals.style_basket import build as basket
    from backtest import axis_rotation_builder as axis
    freeze("2026-07-01 00:05:00")
    expected = explicit or "2026-07-01"
    seen = []

    def capture(*args, **kwargs):
        seen.append(args[-1])
        raise DateBoundaryReached

    monkeypatch.setattr(basket, "CACHE_DIR", tmp_path)
    monkeypatch.setattr(basket, "_all_tickers", lambda db: ["A"])
    monkeypatch.setattr(basket, "fetch_financial_facts", capture)
    for name in ("_read_pool", "_fetch_shares_pool", "_fetch_financial_set", "_fetch_list_dates"):
        monkeypatch.setattr(basket, name, lambda *a: None)
    monkeypatch.setattr(basket, "_fetch_trading_days", lambda *a: pd.DatetimeIndex([]))
    monkeypatch.setattr(basket, "_month_end_trading_days", capture)
    monkeypatch.setattr(axis, "rebalance_dates", capture)
    with pytest.raises(DateBoundaryReached):
        if entry == "axis":
            axis.build(end=explicit)
        else:
            getattr(basket, f"build_{entry}")(db={"offline": True}, end=explicit)
    assert seen == [expected]
