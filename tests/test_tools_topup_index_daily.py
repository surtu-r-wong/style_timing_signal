"""`tools/topup_index_daily.sh` 的判例（2026-09-11 立）。

钉住一件 2026-09-10 真实发生过的事：**闸门与执行体对「补到哪天」的口径必须一致**。

那天 09:07（开盘后 7 分钟）日更链路自己跑起来，前置闸门按 15:30 正确判出
「应有的最后交易日 = 09-09」并放行补跑（库内当时停在 09-03），而本脚本自己写死
`END=$(date +%F)` 取到 **09-10**——闸门根本没打算让它取的日子。结果 15 个指数的
09-10 收盘价全是前一日复制的占位值，进了库。

那次被事后审计的「前值复制」规则接住了，但接住它靠运气：开盘才 7 分钟，Wind 还在
返回前一日收盘。同样的补跑发生在 10:30，Wind 返回的是**实时价**——既不等于前值、
也是个有限的正常数字——事后审计的每一条规则、以及同族共动性哨兵，全都抓不到。

取数与写库要 Wind 网关和 stock_selector 的 venv，不在单测里跑；用 `TOPUP_DRY_RUN=1`
让脚本打印将要执行的区间后立即退出（在 `cd` 到别的仓之前），只测区间是怎么算出来的。
"""
import importlib.util
import os
import re
import subprocess
import sys
from datetime import date, datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "tools" / "topup_index_daily.sh"
GUARD_PATH = ROOT / "deploy" / "daily_signals" / "topup_guard.py"


def _load_guard():
    sys.path.insert(0, str(ROOT))
    spec = importlib.util.spec_from_file_location("topup_guard_for_script_test", GUARD_PATH)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


guard = _load_guard()


def _run(*args, env_extra=None, expect_rc=0):
    env = {**os.environ, "TOPUP_DRY_RUN": "1", **(env_extra or {})}
    out = subprocess.run(["bash", str(SCRIPT), *args], env=env,
                         capture_output=True, text=True, timeout=120)
    assert out.returncode == expect_rc, \
        f"rc={out.returncode}（期望 {expect_rc}）stderr={out.stderr!r}"
    return out


def _dry_run(*args, env_extra=None):
    return _run(*args, env_extra=env_extra).stdout


def _fake_gate(tmp_path, body):
    """冒充闸门的假解释器。脚本用 `${STYLE_SIGNALS_PYTHON:-python3}` 调闸门，
    这里把它换掉，就能把「闸门说了什么」和「今天是哪天」彻底解耦。"""
    fake = tmp_path / "fake_gate"
    fake.write_text(f"#!/usr/bin/env bash\n{body}\n")
    fake.chmod(0o755)
    return {"STYLE_SIGNALS_PYTHON": str(fake)}


def _parse(stream, flag):
    m = re.search(rf"--{flag} (\d{{4}}-\d{{2}}-\d{{2}})", stream)
    assert m, f"dry-run 输出里找不到 --{flag}：{stream!r}"
    return m.group(1)


def test_end_comes_from_the_gate_not_calendar_today():
    """END 必须是闸门的「应有的最后交易日」，不是 `date +%F`。

    收盘前跑 → 闸门给昨天 → 脚本就只补到昨天，当天的行根本不去取。
    时刻在断言两侧各取一次，跨 15:30 的那一秒也不会 flaky。
    """
    before = guard.expected_last_trading_day(datetime.now()).isoformat()
    end = _parse(_dry_run(), "end")
    after = guard.expected_last_trading_day(datetime.now()).isoformat()

    assert end in {before, after}, f"END={end} 不是闸门口径（{before} / {after}）"


def test_end_is_not_today_when_run_before_market_close():
    """本判例的命题本身：盘中跑不得取到当天。

    只在收盘前运行时才有判别力；收盘后闸门给的就是今天，那时这条恒真。
    """
    now = datetime.now()
    end = _parse(_dry_run(), "end")

    if now.weekday() < 5 and (now.hour * 60 + now.minute) < 15 * 60 + 30:
        assert end != date.today().isoformat(), "收盘前仍取到当天 → 会取到盘中价/占位行"


def test_start_defaults_to_fourteen_days_before_and_is_overridable():
    """START 仍是「14 天前」的自愈窗口，且首个位置参数可覆盖（既有契约不变）。"""
    start = _parse(_dry_run(), "start")
    assert start < _parse(_dry_run(), "end")

    assert _parse(_dry_run("2026-01-05"), "start") == "2026-01-05"


def test_end_is_whatever_the_gate_says(tmp_path):
    """END 完全由闸门决定，与「今天是哪天」无关。

    这是本文件里唯一**任何时刻都有判别力**的判例：让假闸门吐一个绝不可能等于今天的
    哨兵日期，若脚本退回 `date +%F`，END 就会是今天而不是哨兵 → 红。上面两条依赖
    真实闸门的判例在收盘后恒真（那时闸门给的本来就是今天），挡不住回退。
    """
    out = _dry_run(env_extra=_fake_gate(tmp_path, "echo 1999-12-31"))

    assert _parse(out, "end") == "1999-12-31"


def test_fails_closed_when_gate_errors(tmp_path):
    """闸门自身出错 → 脚本一并失败 = 零写入。

    `set -e` + 命令替换的既有语义，但这是护栏的命题（不可信响应零写入），钉死它。
    """
    out = _run(env_extra=_fake_gate(tmp_path, "exit 3"), expect_rc=3)

    assert "--end" not in out.stdout        # 没走到打印区间就已经中止
