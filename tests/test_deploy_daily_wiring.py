"""deploy/daily_signals 的接线静态判例：runner 步骤 8 推送、告警器失败通知、service 锁冲突（2026-09-23 立）。

只读文本 + `bash -n`，**不执行脚本、不调 systemctl**：runner 会写共享库表 index_daily，告警器会
真发企业微信。推送脚本本身的行为归 tests/test_deploy_notify_wechat.py；这里只钉「接在哪、带什么
参数、失败怎么传播」——这几件一接错就**静默失效**，推送脚本自己的单测看不出来：

* 推送必须在护栏**通过之后**：「推送对象 ⊆ 护栏对象」的前提是护栏先跑完、通过、写好状态文件；
* 推送失败 runner 必须非零退出：否则 OnFailure 不触发，没送达也没人知道；
* 告警器调推送必须 best-effort（外包 timeout、后跟 ||、末尾 exit 0）：告警器自己绝不能成为新的失败源；
* 锁冲突的 75 不算失败（SuccessExitStatus=75）：否则接上微信后每次撞锁都是一条假告警。
"""
import re
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
DEPLOY = ROOT / "deploy" / "daily_signals"
RUNNER = DEPLOY / "run_daily_signals.sh"
ALERTER = DEPLOY / "alert_on_failure.sh"
SERVICE = DEPLOY / "style-signals-daily.service"
NOTIFY = "notify_wechat.py"
MAIN_UNIT = "style-signals-daily.service"
# 不加引号才会按词拆开（如 --dry-run）；加了引号，变量为空时会多传一个空串参数，argparse 直接 exit 2
NOTIFY_ARGS = re.compile(r'(?<!")\$\{STYLE_SIGNALS_NOTIFY_ARGS:-\}(?!")')


def _logical_lines(path: Path) -> list[str]:
    """逻辑行：去掉整行注释，反斜杠续行拼成一行，首尾空白去掉。"""
    out, buf = [], []
    for raw in path.read_text(encoding="utf-8").splitlines():
        if not buf and raw.lstrip().startswith("#"):
            continue
        line = raw.strip()
        if line.endswith("\\"):
            buf.append(line[:-1].strip())
            continue
        out.append(" ".join([*buf, line]))
        buf = []
    return out


def _calls(lines: list[str], needle: str) -> list[int]:
    """提到 needle 的可执行行；log / echo 打印行会提到脚本名，但不是调用。"""
    return [i for i, line in enumerate(lines)
            if needle in line and not re.match(r"(log|echo)\b", line)]


def _only(indices: list[int], what: str) -> int:
    assert len(indices) == 1, f"{what}应恰有 1 处，实际 {len(indices)} 处"
    return indices[0]


def _guard_call(lines: list[str]) -> int:
    """步骤 7 的护栏调用（带 --max-lag 的那次；fail() 里那次只记账、不做检查）。"""
    return _only([i for i in _calls(lines, '"${GUARD}"') if "--max-lag" in lines[i]], "步骤 7 护栏调用")


def _arg_source(lines: list[str], call: str, flag: str) -> str:
    """调用行里 flag 的取值表达式；取值是 "${VAR}" 就追到 VAR= 的赋值行。"""
    m = re.search(rf'{re.escape(flag)}\s+"([^"]*)"', call)
    assert m, f"缺 {flag}（且取值要加引号）：{call}"
    var = re.fullmatch(r"\$\{(\w+)\}", m.group(1))
    if not var:
        return m.group(1)
    return lines[_only([i for i, line in enumerate(lines) if line.startswith(f"{var.group(1)}=")],
                       f"{var.group(1)} 的赋值")]


def _unit(path: Path) -> dict[str, list[tuple[str, str]]]:
    """systemd 单元文件 → {节: [(键, 值)]}；同一个键可重复出现，故不用 configparser。"""
    sections: dict[str, list[tuple[str, str]]] = {}
    current: list[tuple[str, str]] = []
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith(("#", ";")):
            continue
        if line.startswith("[") and line.endswith("]"):
            current = sections.setdefault(line[1:-1], [])
            continue
        key, _, value = line.partition("=")
        current.append((key.strip(), value.strip()))
    return sections


@pytest.fixture(scope="module")
def runner() -> list[str]:
    return _logical_lines(RUNNER)


@pytest.fixture(scope="module")
def alerter() -> list[str]:
    return _logical_lines(ALERTER)


@pytest.mark.parametrize("script", [RUNNER, ALERTER], ids=lambda p: p.name)
def test_bash_syntax(script):
    """bash -n 只做语法解析，不执行。"""
    out = subprocess.run(["bash", "-n", str(script)], capture_output=True, text=True, timeout=30)
    assert (out.returncode, out.stderr) == (0, "")


# ── runner：步骤 8 ─────────────────────────────────────────────────────────────

def test_runner_pushes_only_after_guard_passed(runner):
    """推送排在护栏调用与「✔ freshness_guard 通过」之后，且全文只此一处——
    fail() 等失败分支里不许推持仓（失败归告警器报）。"""
    assert 'GUARD="${SCRIPT_DIR}/check_freshness.py"' in runner
    guard = _guard_call(runner)
    passed = _only([i for i, line in enumerate(runner) if "✔ freshness_guard 通过" in line],
                   "「✔ freshness_guard 通过」日志")
    push = _only(_calls(runner, NOTIFY), "runner 的推送调用")
    assert guard < passed < push


def test_runner_push_args(runner):
    """推送读护栏刚写的那份状态文件；附加参数只经 STYLE_SIGNALS_NOTIFY_ARGS 透传——
    写死 --dry-run（演练完忘删）= 从此只打印不发，而链路照样报成功。"""
    push = runner[_only(_calls(runner, NOTIFY), "runner 的推送调用")]
    assert '--status-file "${STATUS_FILE}"' in push
    assert '--status-file "${STATUS_FILE}"' in runner[_guard_call(runner)]
    assert NOTIFY_ARGS.search(push), f"须不加引号透传 ${{STYLE_SIGNALS_NOTIFY_ARGS:-}}：{push}"
    assert "--dry-run" not in push and "--alert" not in push


def test_runner_push_failure_exits_nonzero(runner):
    """推送失败 → 日志 NOTIFY_FAILED + exit 1 → OnFailure 告警器；「成功」收尾只在失败分支之后。"""
    push = _only(_calls(runner, NOTIFY), "runner 的推送调用")
    m = re.search(r"\|\|\s*(\w+)=\$\?$", runner[push])
    assert m, f"推送调用须以 `|| <rc>=$?` 接住退出码（set -e 下不接就直接崩出去，没有 NOTIFY_FAILED）：{runner[push]}"
    start = runner.index(f"if [[ ${{{m.group(1)}}} -ne 0 ]]; then", push)
    end = runner.index("fi", start)
    branch = runner[start + 1:end]
    assert any(line.startswith("log ") and "NOTIFY_FAILED" in line for line in branch), branch
    assert "exit 1" in branch, branch
    success = [i for i, line in enumerate(runner) if "日更信号链结束：成功" in line]
    assert success and min(success) > end


# ── 告警器：失败通知 ──────────────────────────────────────────────────────────

def test_alerter_push_is_best_effort(alerter):
    """告警器调 notify_wechat.py --alert：外包 timeout、后跟 ||——推不出去只记进告警文件，不传播。"""
    call = alerter[_only(_calls(alerter, NOTIFY), "告警器的推送调用")]
    assert re.match(r"timeout \d+ ", call), f"须外包 timeout：{call}"
    assert "||" in call.split(NOTIFY, 1)[1], f"须后跟 ||（告警器自己绝不失败）：{call}"
    assert re.search(r"(^|\s)--alert(\s|$)", call), call
    assert '--status-file "${STATUS_FILE}"' in call
    assert NOTIFY_ARGS.search(call), f"须不加引号透传 ${{STYLE_SIGNALS_NOTIFY_ARGS:-}}：{call}"
    assert "--dry-run" not in call
    # 与 runner 写的是同一份状态文件
    status = [line for line in alerter if line.startswith("STATUS_FILE=")]
    assert status == [line for line in _logical_lines(RUNNER) if line.startswith("STATUS_FILE=")]


def test_alerter_passes_main_service_result_and_start(alerter):
    """--systemd-result / --run-started 取自**主** service：查成告警单元自己的启动时刻，
    每份状态文件都会被判成「上一次运行留下的」。--timestamp=unix 给出 @秒，推送脚本才解析得了；
    少了它退回人类可读时间，解析失败会静默退回按日期判断。"""
    call = alerter[_only(_calls(alerter, NOTIFY), "告警器的推送调用")]
    result = _arg_source(alerter, call, "--systemd-result")
    assert MAIN_UNIT in result and "-p Result" in result and "--value" in result, result
    started = _arg_source(alerter, call, "--run-started")
    for part in (MAIN_UNIT, "-p ExecMainStartTimestamp", "--value", "--timestamp=unix"):
        assert part in started, f"缺 {part}：{started}"


def test_alerter_appends_push_output_to_alert_file(alerter):
    """推送输出（含 REFUSED / SEND_FAILED）追加进告警文件留底，且在告警文件写好之后——
    顺序反了会被后面的 > 冲掉。"""
    write = _only([i for i, line in enumerate(alerter) if line.startswith('} > "${ALERT_FILE}"')],
                  "告警文件的首次写入")
    push = _only(_calls(alerter, NOTIFY), "告警器的推送调用")
    close = next(i for i in range(push, len(alerter)) if alerter[i].startswith("}"))
    assert write < push
    assert alerter[close] == '} >> "${ALERT_FILE}" 2>&1'


def test_alerter_never_fails(alerter):
    """不开 errexit、末尾 exit 0：告警器自己绝不能成为新的失败源。"""
    assert not any(re.match(r"set\s+(-\w*e|.*-o\s+errexit)", line) for line in alerter)
    assert [line for line in alerter if line][-1] == "exit 0"


# ── service ───────────────────────────────────────────────────────────────────

def test_service_lock_conflict_is_success(runner):
    """runner 撞锁退出 75（另一实例在跑，由它推送）：service 须把 75 记成功，否则 OnFailure 发假告警。
    两边的 75 要对得上，这条配置才不是空的。"""
    codes = [value for key, value in _unit(SERVICE).get("Service", []) if key == "SuccessExitStatus"]
    assert any("75" in value.split() for value in codes), codes
    start = next(i for i, line in enumerate(runner) if re.match(r"if ! flock -n\b", line))
    assert "exit 75" in runner[start + 1:runner.index("fi", start)]


def test_service_keeps_onfailure_and_no_install():
    """OnFailure 仍接告警单元；无 [Install]（由 timer 拉起，enable 了会每次登录双跑）。"""
    unit = _unit(SERVICE)
    assert ("OnFailure", "style-signals-daily-alert.service") in unit["Unit"]
    assert "Install" not in unit
