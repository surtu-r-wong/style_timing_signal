"""deploy/daily_signals 的接线判例：runner 步骤 8 推送、告警器失败通知、service 锁冲突（2026-09-23 立）。

静态部分只读文本 + `bash -n`；运行期部分只把 runner 的**片段**（步骤 8 到文件末尾、fail()）抽出来，
配桩解释器用 bash 跑。**不执行整个脚本、不调 systemctl**：runner 会写共享库表 index_daily，告警器会
真发企业微信。推送脚本本身的行为归 tests/test_deploy_notify_wechat.py；这里只钉「接在哪、带什么参数、
失败怎么传播」——这几件一接错就**静默失效**，推送脚本自己的单测看不出来：

* 推送必须在护栏**通过之后**：「推送对象 ⊆ 护栏对象」的前提是护栏先跑完、通过、写好状态文件；
* 推送失败 runner 必须非零退出：否则 OnFailure 不触发，没送达也没人知道；
* 推送调用必须限时：socket 超时只管单次阻塞操作、总时长不封顶（DNS 根本不受它管），卡住会一直
  占锁到 TimeoutStartSec；
* 告警器调推送必须 best-effort（限时、后跟 ||、末尾 exit 0）：告警器自己绝不能成为新的失败源；
* 75 只属于 flock 跳过：service 的 SuccessExitStatus=75 把它记成功，步骤自己退出 75 若原样透传，
  失败就被吞成了成功；
* 步骤 0 办公室模式（2026-09-23 起，标志文件 SKIP_TOPUP 在）：调等数脚本 wait_for_inputs.py（限时、-u、
  输出经 tee 实时进日志），按它末尾的 INPUTS_* 映射 OFFICE_OK / OFFICE_LATE / OFFICE_CHECK_ERROR，没结果也记
  OFFICE_CHECK_ERROR，**三种都不中止链路**；同族哨兵 CRITICAL → OFFICE_SUSPECT，**在信号重算之前中止**。
  只有环境变量 STYLE_SIGNALS_SKIP_TOPUP=1 时仍是旧的 TOPUP_SKIPPED（不等）；标志文件不在时走原 topup 路径
  （回退用）。时间预算前后自洽：定时器窗口 ⊂ 等数截止、等数 max-wait + 最后一轮 ≤ 兜底、兜底 + 推送 ≤ service。
"""
import importlib.util
import json
import os
import re
import shlex
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
DEPLOY = ROOT / "deploy" / "daily_signals"
RUNNER = DEPLOY / "run_daily_signals.sh"
ALERTER = DEPLOY / "alert_on_failure.sh"
SERVICE = DEPLOY / "style-signals-daily.service"
ALERT_SERVICE = DEPLOY / "style-signals-daily-alert.service"
NOTIFY = "notify_wechat.py"
MAIN_UNIT = "style-signals-daily.service"
# 不加引号才会按词拆开（如 --dry-run）；加了引号，变量为空时会多传一个空串参数，argparse 直接 exit 2。
# ${VAR:-} 与 ${VAR-} 在 set -u 下都安全，都认。
NOTIFY_ARGS = re.compile(r'(?<!")\$\{STYLE_SIGNALS_NOTIFY_ARGS:?-\}(?!")')
NOTIFY_ARGS_WORD = re.compile(r"\$\{STYLE_SIGNALS_NOTIFY_ARGS:?-\}")
ALERT_BUDGET_MARGIN = 10   # 秒：告警单元 TimeoutStartSec 里留给告警文件摘要与 systemctl 查询的余量
WAIT = "wait_for_inputs.py"
INPUTS_ARGS = re.compile(r'(?<!")\$\{STYLE_SIGNALS_INPUTS_ARGS:?-\}(?!")')
INPUTS_ARGS_WORD = re.compile(r"\$\{STYLE_SIGNALS_INPUTS_ARGS:?-\}")
SERVICE_BUDGET_MARGIN = 600   # 秒：主 service TimeoutStartSec 里留给链路本身（约 20 秒）与抖动的余量
TIMER = DEPLOY / "style-signals-daily.timer"
WAIT_BUDGET_SLACK = 50       # 秒：等数兜底里「max-wait + 截止那一轮最坏查询」之外再留的余量（进程启动、导入等）


def _load_wait():
    """等数脚本本体（取它的默认值与连接参数来核时间预算；不连库）。"""
    sys.path.insert(0, str(ROOT))
    spec = importlib.util.spec_from_file_location("wait_for_inputs_for_wiring", DEPLOY / WAIT)
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


WFI = _load_wait()


# ── 文本解析 ───────────────────────────────────────────────────────────────────

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


def _find(lines: list[str], pattern: str, start: int = 0) -> int:
    """start 起第一条完全匹配 pattern 的逻辑行。"""
    hits = [i for i in range(start, len(lines)) if re.fullmatch(pattern, lines[i])]
    assert hits, f"找不到完全匹配 {pattern!r} 的行"
    return hits[0]


def _matching_fi(lines: list[str], start: int) -> int:
    """lines[start] 是多行 `if …; then`：返回与它配对的 fi（跳过嵌套 if；单行 `if …; fi` 不计层）。"""
    depth = 0
    for i in range(start, len(lines)):
        if re.match(r"if\s", lines[i]) and not re.search(r";\s*fi$", lines[i]):
            depth += 1
        elif lines[i] == "fi":
            depth -= 1
            if depth == 0:
                return i
    raise AssertionError(f"逻辑行 {start} 的 if 没有配对的 fi")


def _rc_branch(lines: list[str], call: int) -> tuple[str, int, int]:
    """调用行以 `|| <rc>=$?` 收住退出码 → (变量名, 其后失败分支的 if 行, 配对的 fi 行)。"""
    m = re.search(r"\|\|\s*(\w+)=\$\?$", lines[call])
    assert m, f"调用须以 `|| <rc>=$?` 接住退出码（set -e 下不接就直接崩出去）：{lines[call]}"
    start = _find(lines, rf"if \[\[\s*\$\{{{m.group(1)}\}}\s+-ne\s+0\s*\]\];\s*then", call + 1)
    return m.group(1), start, _matching_fi(lines, start)


def _guard_call(lines: list[str]) -> int:
    """步骤 7 的护栏调用（带 --max-lag 的那次；fail() 里那次只记账、不做检查）。"""
    return _only([i for i in _calls(lines, '"${GUARD}"') if "--max-lag" in lines[i]], "步骤 7 护栏调用")


def _duration(text: str) -> float:
    """coreutils timeout 的时长：数字 + 可选 s/m/h/d 后缀。"""
    m = re.fullmatch(r"(\d+(?:\.\d+)?)([smhd]?)", text)
    assert m, f"timeout 时长写法不认识：{text!r}"
    return float(m.group(1)) * {"": 1, "s": 1, "m": 60, "h": 3600, "d": 86400}[m.group(2)]


_TIMEOUT_LONG_OPTS = ("--foreground", "--kill-after", "--preserve-status", "--signal", "--verbose")


def _split_timeout(tokens: list[str]) -> tuple[float | None, float, list[str]]:
    """shlex 切好的命令若以 timeout 开头 → (时长秒, kill-after 秒, 被限时的命令)；否则 (None, 0, 原样)。

    长选项按 getopt 规则认无歧义前缀（`--kill 10` = `--kill-after 10`）。--preserve-status / --foreground
    直接判失败：前者让超时的退出码变成被杀命令自己的（如 143），后者不限时命令的子进程——
    runner 的 124/137 超时分支就走不到了。"""
    if not tokens or tokens[0] != "timeout":
        return None, 0.0, tokens
    i, kill = 1, 0.0
    while tokens[i].startswith("-"):
        opt = tokens[i]
        if opt == "--":
            i += 1
            break
        if opt.startswith("--"):
            name, eq, value = opt.partition("=")
            full = [n for n in _TIMEOUT_LONG_OPTS if n.startswith(name)]
            assert len(full) == 1, f"timeout 选项不认识或有歧义：{opt}"
            assert full[0] not in ("--preserve-status", "--foreground"), (
                f"timeout 不许带 {opt}：它改变超时语义（--preserve-status 让退出码变成被杀命令自己的，"
                f"124/137 分支走不到；--foreground 不限时命令的子进程）")
            if full[0] in ("--kill-after", "--signal") and not eq:
                i += 1
                value = tokens[i]
            if full[0] == "--kill-after":
                kill = _duration(value)
        elif opt in ("-k", "-s"):
            i += 1
            if opt == "-k":
                kill = _duration(tokens[i])
        elif opt.startswith("-k"):
            kill = _duration(opt[2:])
        i += 1   # -v / -sX 以及上面各分支吃掉的选项本身
    return _duration(tokens[i]), kill, tokens[i + 1:]


def _notify_call(lines: list[str], what: str) -> tuple[int, float | None, float, list[str]]:
    """唯一一处推送调用 → (逻辑行, 限时秒, kill-after 秒, 被限时的命令 tokens)。"""
    i = _only(_calls(lines, NOTIFY), what)
    return (i, *_split_timeout(shlex.split(lines[i])))


def _wait_call(lines: list[str]) -> tuple[int, float | None, float, list[str]]:
    """唯一一处等数调用 → (逻辑行, 限时秒, kill-after 秒, 被限时的命令 tokens)。"""
    i = _only(_calls(lines, WAIT), "runner 的等数调用")
    return (i, *_split_timeout(shlex.split(lines[i])))


def _check_push_argv(cmd: list[str], fixed: list[str]) -> list[str]:
    """被限时的推送命令须是「解释器 -u 脚本 透传参数 固定参数…」，返回脚本之后的参数。
    -u：不缓冲，报错行与消息正文在日志里按真实顺序交错；透传参数排在固定参数之前：argparse 对
    同名参数取最后一个，固定参数（--status-file 等）永远生效。"""
    assert len(cmd) > 3 and re.fullmatch(r"-[A-Za-z]*u[A-Za-z]*", cmd[1]), f"解释器后须紧跟 -u：{cmd}"
    assert cmd[2].endswith(f"/{NOTIFY}"), cmd
    args = cmd[3:cmd.index("||")] if "||" in cmd else cmd[3:]
    assert args and NOTIFY_ARGS_WORD.fullmatch(args[0]), f"透传参数须紧跟脚本、排在固定参数之前：{args}"
    missing = [flag for flag in fixed if flag not in args[1:]]
    assert not missing, f"缺固定参数 {missing}：{args}"
    assert "--dry-run" not in args
    return args


def _arg_source(lines: list[str], call: str, flag: str) -> str:
    """调用行里 flag 的取值表达式；取值是 "${VAR}" 就追到 VAR= 的赋值行。"""
    m = re.search(rf'{re.escape(flag)}\s+"([^"]*)"', call)
    assert m, f"缺 {flag}（且取值要加引号）：{call}"
    var = re.fullmatch(r"\$\{(\w+)\}", m.group(1))
    if not var:
        return m.group(1)
    return lines[_only([i for i, line in enumerate(lines) if line.startswith(f"{var.group(1)}=")],
                       f"{var.group(1)} 的赋值")]


def _sets_errexit(line: str) -> bool:
    """`set …` 是否打开 errexit：-e 可与别的短选项合写（-eu / -u -e / -uo pipefail -e），也可 -o errexit。"""
    if not re.match(r"set\s", line):
        return False
    tokens = shlex.split(line)
    for prev, tok in zip(tokens, tokens[1:]):
        if re.fullmatch(r"-[A-Za-z]*o", prev) and tok == "errexit":
            return True
        if re.fullmatch(r"-[A-Za-z]+", tok) and "e" in tok[1:]:
            return True
    return False


def _unit(path: Path) -> dict[str, dict[str, list[str]]]:
    """systemd 单元文件 → {节: {键: [值…]}}，按 systemd 语义：同一键多次赋值累加，空赋值（`Key=`）
    清空此前的值——`SuccessExitStatus=` 写在 75 之后，75 就作废了。标量键取最后一个值。"""
    sections: dict[str, dict[str, list[str]]] = {}
    current: dict[str, list[str]] = {}
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith(("#", ";")):
            continue
        if line.startswith("[") and line.endswith("]"):
            current = sections.setdefault(line[1:-1], {})
            continue
        key, _, value = line.partition("=")
        key, value = key.strip(), value.strip()
        if value:
            current.setdefault(key, []).append(value)
        else:
            current[key] = []
    return sections


def _timespan(text: str) -> float:
    """systemd 时间跨度（TimeoutStartSec 等）：纯数字按秒；认 s/sec/m/min/h/hr 组合，如 `1min 30s`。"""
    units = {"": 1, "s": 1, "sec": 1, "m": 60, "min": 60, "h": 3600, "hr": 3600}
    parts = re.findall(r"(\d+)\s*([a-z]*)", text)
    assert parts and "".join(n + u for n, u in parts) == re.sub(r"\s+", "", text), f"时间跨度写法不认识：{text!r}"
    return sum(int(n) * units[u] for n, u in parts)


def _timeout_start_sec(path: Path) -> float:
    return _timespan(_unit(path)["Service"]["TimeoutStartSec"][-1])


def _function(text: str, name: str) -> str:
    """bash 函数定义原文：从 `name() {` 到顶格的 `}`。"""
    start = text.index(f"\n{name}() {{") + 1
    return text[start:text.index("\n}\n", start) + 2]


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


# ── runner：步骤 7/8（静态）──────────────────────────────────────────────────────

def test_runner_pushes_only_after_guard_passed(runner):
    """推送排在护栏调用与「✔ freshness_guard 通过」之后，且全文只此一处——
    fail() 等失败分支里不许推持仓（失败归告警器报）。"""
    assert 'GUARD="${SCRIPT_DIR}/check_freshness.py"' in runner
    guard = _guard_call(runner)
    passed = _only([i for i, line in enumerate(runner) if "✔ freshness_guard 通过" in line],
                   "「✔ freshness_guard 通过」日志")
    push = _only(_calls(runner, NOTIFY), "runner 的推送调用")
    assert guard < passed < push


def test_runner_guard_failure_branch_ends_with_exit(runner):
    """护栏未过的分支必须以 exit 收尾，否则落进步骤 8 把没过护栏的持仓推出去——推送脚本虽会因
    result≠OK 拒推，但那是第二道闸，第一道不能形同虚设。"""
    _, start, end = _rc_branch(runner, _guard_call(runner))
    assert re.match(r"exit\b", runner[end - 1]), runner[start:end + 1]


def test_runner_push_args(runner):
    """推送读护栏刚写的那份状态文件；附加参数只经 STYLE_SIGNALS_NOTIFY_ARGS 透传——写死 --dry-run
    （演练完忘删）= 从此只打印不发，而链路照样报成功。"""
    i, _, _, cmd = _notify_call(runner, "runner 的推送调用")
    assert NOTIFY_ARGS.search(runner[i]), f"须不加引号透传 STYLE_SIGNALS_NOTIFY_ARGS：{runner[i]}"
    args = _check_push_argv(cmd, ["--status-file"])
    assert args[args.index("--status-file") + 1] == "${STATUS_FILE}" and "--alert" not in args
    assert re.search(r'--status-file\s+"\$\{STATUS_FILE\}"', runner[_guard_call(runner)])


def test_runner_push_failure_exits_nonzero(runner):
    """推送失败 → 日志 NOTIFY_FAILED + exit 1 → OnFailure 告警器（超时与否都是 exit 1）；
    「成功」收尾只在失败分支之后。"""
    _, start, end = _rc_branch(runner, _only(_calls(runner, NOTIFY), "runner 的推送调用"))
    branch = runner[start + 1:end]
    assert any(line.startswith("log ") and "NOTIFY_FAILED" in line for line in branch), branch
    exits = [line for line in branch if re.match(r"exit\b", line)]
    assert exits and set(exits) == {"exit 1"} and branch[-1] == "exit 1", exits
    success = [i for i, line in enumerate(runner) if "日更信号链结束：成功" in line]
    assert success and min(success) > end


def test_runner_push_is_time_limited(runner):
    """推送调用外包 timeout 且带 -k：socket 超时只管单次阻塞操作、总时长不封顶（DNS 根本不受它管，
    慢回包 / TLS 也能一段段拖），真正封顶的是这层限时；SIGTERM 杀不掉时 -k 兜底强杀。
    限时（含宽限）不超过主 service TimeoutStartSec 的一半——否则还是 systemd 先动手。"""
    i, duration, kill, _ = _notify_call(runner, "runner 的推送调用")
    assert duration is not None, f"推送调用须外包 timeout：{runner[i]}"
    assert kill > 0, f"timeout 须带 -k（SIGTERM 杀不掉时强杀）：{runner[i]}"
    limit = _timeout_start_sec(SERVICE)
    assert duration + kill <= limit / 2, (duration, kill, limit)


# ── runner：运行期判例（只跑抽出来的片段 + 桩解释器）─────────────────────────────

_STUB = r'''#!/usr/bin/env bash
# 冒充解释器：记下 argv，按 STUB_RC 立即退出（124/137 冒充 timeout 的退出码，不真等）
printf '%s\n' "$@" > "${STUB_ARGV}"
echo "stub：推送脚本的输出"
exit "${STUB_RC}"
'''


@pytest.mark.parametrize("notify_args", [None, "--dry-run --status-file /elsewhere/status.json"],
                         ids=["args-unset", "args-override"])
@pytest.mark.parametrize("stub_rc", [0, 1, 124, 137])
def test_runner_step8_runtime(tmp_path, runner, stub_rc, notify_args):
    """runner 从「# ── 步骤 8」到文件末尾的原文 + 桩解释器：0 → 成功收尾 rc 0；1 → 通用 NOTIFY_FAILED
    rc 1；124 / 137（超时 / 宽限后强杀）→ 超时文案 rc 1。能抓住静态断言看不见的东西，比如删掉
    notify_rc=0（set -u 下推送成功反而崩出去）。

    两种透传：**未设**（从子进程 env 里删掉，不是设空串）= 生产默认路径，systemd 单元里就没有这个
    变量，set -u 下任何一处写成 ${STYLE_SIGNALS_NOTIFY_ARGS}（不带 :-）都会当场崩；带同名
    --status-file 的覆盖尝试 = 固定参数必须排在最后才生效。"""
    text = RUNNER.read_text(encoding="utf-8")
    stub = tmp_path / "python_stub"
    stub.write_text(_STUB, encoding="utf-8")
    stub.chmod(0o755)
    status, script_dir = tmp_path / "status.json", tmp_path / "deploy"
    script = tmp_path / "step8.sh"
    script.write_text("\n".join([
        "set -euo pipefail",
        'log() { echo "$*"; }',
        "START_TS=$SECONDS",
        f"STATUS_FILE={shlex.quote(str(status))}",
        f"SCRIPT_DIR={shlex.quote(str(script_dir))}",
        f"PYTHON={shlex.quote(str(stub))}",
    ]) + "\n" + text[text.index("# ── 步骤 8"):], encoding="utf-8")
    argv_file = tmp_path / "argv"
    env = {**os.environ, "STUB_RC": str(stub_rc), "STUB_ARGV": str(argv_file)}
    env.pop("STYLE_SIGNALS_NOTIFY_ARGS", None)
    if notify_args is not None:
        env["STYLE_SIGNALS_NOTIFY_ARGS"] = notify_args
    out = subprocess.run(["bash", str(script)], env=env, capture_output=True, text=True, timeout=60)
    log = out.stdout + out.stderr
    failed = [line for line in log.splitlines() if line.startswith("NOTIFY_FAILED")]
    _, duration, _, _ = _notify_call(runner, "runner 的推送调用")
    timed_out = f"推送超时（{duration:g}s"
    if stub_rc == 0:
        assert (out.returncode, failed) == (0, []), log
        assert "日更信号链结束：成功" in log, log
    else:
        assert out.returncode == 1 and len(failed) == 1, log
        assert "日更信号链结束：推送失败" in log, log
        if stub_rc in (124, 137):
            assert timed_out in failed[0], failed
        else:
            assert timed_out not in failed[0] and f"exit {stub_rc}" in failed[0], failed
            assert "见上方输出" in failed[0], failed
    argv = argv_file.read_text(encoding="utf-8").splitlines()
    head, fixed = ["-u", str(script_dir / NOTIFY)], ["--status-file", str(status)]
    if notify_args is None:
        assert argv == head + fixed, argv
    else:
        assert argv == head + notify_args.split() + fixed, argv


@pytest.mark.parametrize("code, expected", [(75, 1), (1, 1), (2, 2), (3, 3)])
def test_runner_fail_never_exits_75(tmp_path, code, expected):
    """75 只属于 flock 跳过：service 的 SuccessExitStatus=75 会把它记成功、不触发告警。步骤自己退出
    75 经 fail() 原样透传就会被当成功吞掉——fail() 须把 75 改成 1，日志照记原始退出码；其余原样。"""
    script = tmp_path / "fail.sh"
    script.write_text("\n".join([
        "set -euo pipefail",
        'log() { echo "$*"; }',
        "steps_json() { echo '[]'; }",
        "PYTHON=true GUARD=guard STATUS_FILE=s LOG_FILE=l STARTED_AT=t TOPUP_STATUS=OK TOPUP_REASON=",
        "START_TS=$SECONDS",
        _function(RUNNER.read_text(encoding="utf-8"), "fail"),
        f"fail some_step {code}",
        'echo "fail() 没有退出"',
    ]) + "\n", encoding="utf-8")
    out = subprocess.run(["bash", str(script)], capture_output=True, text=True, timeout=30)
    assert out.returncode == expected, out.stdout + out.stderr
    assert f"（exit {code}）" in out.stdout, out.stdout
    assert "fail() 没有退出" not in out.stdout


def test_runner_exit_75_only_for_lock_conflict(runner):
    """全文字面 exit 75 只有 flock 跳过那一处。"""
    start = _find(runner, r"if ! flock -n\b.*")
    end = _matching_fi(runner, start)
    hits = [i for i in _calls(runner, "exit") if re.search(r'\bexit\s+"?75\b', runner[i])]
    assert len(hits) == 1 and start < hits[0] < end, [runner[i] for i in hits]


# ── runner：步骤 0 办公室模式（等数）───────────────────────────────────────────────

def test_runner_wait_call_shape(runner):
    """等数调用：外包 timeout 且带 -k（一路等到截止也有硬上限，SIGTERM 杀不掉时强杀）；解释器后紧跟 -u
    （进度行实时进日志）；透传参数不加引号、紧跟脚本（排在任何固定参数之前）；输出经 tee 落一份副本
    供解析（边等边看，而不是等完才一次性吐出来）；退出码用 || 接住（set -e 下不接就崩出去）。"""
    i, duration, kill, cmd = _wait_call(runner)
    assert duration is not None and kill > 0, f"等数调用须外包 timeout -k：{runner[i]}"
    assert re.fullmatch(r"-[A-Za-z]*u[A-Za-z]*", cmd[1]), f"解释器后须紧跟 -u：{cmd}"
    assert cmd[2].endswith(f"/{WAIT}"), cmd
    assert INPUTS_ARGS_WORD.fullmatch(cmd[3]), f"透传参数须紧跟脚本：{cmd}"
    assert INPUTS_ARGS.search(runner[i]), f"须不加引号透传 STYLE_SIGNALS_INPUTS_ARGS：{runner[i]}"
    assert "|" in cmd and cmd[cmd.index("|") + 1] == "tee", f"输出须经 tee 实时进日志：{runner[i]}"
    assert re.search(r"\|\|\s*\w+=\$\?$", runner[i]), f"须以 || <rc>=$? 接住退出码：{runner[i]}"
    assert "--once" not in cmd, "写死 --once = 从此不等，办公室晚到几分钟就照旧数据算"


_STEP0_STUB = r"""#!/usr/bin/env bash
# 冒充解释器：记下 argv，照 STUB_OUT 原样打印（冒充等数脚本 / 前置闸门的输出），按 STUB_RC 退出。
# 给了 STUB_LS_DIR 就先把那个目录的文件名记进 STUB_LS（看等数期间结果副本叫什么）。
printf '%s\n' "$@" > "${STUB_ARGV}"
if [[ -n "${STUB_LS_DIR:-}" ]]; then ls -A "${STUB_LS_DIR}" > "${STUB_LS}"; fi
if [[ " $* " == *" audit "* && -n "${STUB_AUDIT_RC:-}" ]]; then exit "${STUB_AUDIT_RC}"; fi
printf '%s' "${STUB_OUT:-}"
exit "${STUB_RC:-0}"
"""


def _run_step0(tmp_path, *, flag: bool, stub_out: str = "", stub_rc: int = 0, env_extra: dict | None = None,
               before: str = ""):
    """runner 从「# ── 步骤 0」到「# ── 步骤 1-6」之前的原文 + 桩解释器，跑完打印 TOPUP_STATUS / TOPUP_REASON /
    steps JSON。record_step 用 runner 原文；fail 换成先打印同样三项、再退出 1（哨兵 CRITICAL 走的就是它）。
    -> (bash 结果, 结果字典, 桩 argv 或 None)。"""
    text = RUNNER.read_text(encoding="utf-8")
    stub = tmp_path / "python_stub"
    stub.write_text(_STEP0_STUB, encoding="utf-8")
    stub.chmod(0o755)
    script_dir, log_dir = tmp_path / "deploy", tmp_path / "logs"
    script_dir.mkdir()
    log_dir.mkdir()
    if flag:
        (script_dir / "SKIP_TOPUP").write_text("2026-09-23 起输入由办公室写入\n", encoding="utf-8")
    report = ['echo "RESULT_STATUS=${TOPUP_STATUS}"', 'echo "RESULT_REASON=${TOPUP_REASON}"',
              'echo "RESULT_STEPS=$(steps_json)"']
    script = tmp_path / "step0.sh"
    script.write_text("\n".join([
        "set -euo pipefail",
        'log() { echo "LOG $*"; }',
        _function(text, "record_step"),
        'steps_json() { echo "[${STEPS_JSON}]"; }',
        'fail() { echo "FAIL_CALLED $*"; ' + "; ".join(report) + "; exit 1; }",
        f"PYTHON={shlex.quote(str(stub))}",
        f"SCRIPT_DIR={shlex.quote(str(script_dir))}",
        f"LOG_DIR={shlex.quote(str(log_dir))}",
        f"REPO={shlex.quote(str(tmp_path / 'repo'))}",
        f"TOPUP_GUARD={shlex.quote(str(script_dir / 'topup_guard.py'))}",
        f"TOPUP_SNAPSHOT={shlex.quote(str(log_dir / '.topup_pre_snapshot.json'))}",
        "TOPUP_TIMEOUT=900 TOPUP_STATUS=UNKNOWN TOPUP_REASON= STEPS_JSON=",
        before,
    ]) + "\n" + text[text.index("# ── 步骤 0"):text.index("# ── 步骤 1-6")] + "\n".join(report) + "\n",
        encoding="utf-8")
    argv_file = tmp_path / "argv"
    env = {**os.environ, "STUB_OUT": stub_out, "STUB_RC": str(stub_rc), "STUB_ARGV": str(argv_file)}
    for var in ("STYLE_SIGNALS_INPUTS_ARGS", "STYLE_SIGNALS_SKIP_TOPUP", "STUB_LS_DIR", "STUB_LS", "STUB_AUDIT_RC"):
        env.pop(var, None)
    env.update(env_extra or {})
    out = subprocess.run(["bash", str(script)], env=env, capture_output=True, text=True, timeout=60)
    got = dict(line.split("=", 1) for line in out.stdout.splitlines() if line.startswith("RESULT_"))
    argv = argv_file.read_text(encoding="utf-8").splitlines() if argv_file.exists() else None
    return out, got, argv


CLEAN_DETAIL = "2026-09-23 同族共动性：15 码日收益无 CRITICAL"
OK_REASON = "2026-09-23 15 码到齐（等 0 秒）"
LATE_REASON = "2026-09-23 截至 21:30 仍缺 1 码：000300.SH，按库内已有数据照算"
ERR_REASON = "2026-09-23：OperationalError: timeout expired"
CRIT_DETAIL = "2026-09-23：CRITICAL 2000pair 对内价差 27.07pp （932409.CSI +16.02% vs 932408.CSI -11.05%，判据 ≥8pp）"
ACCEPTED_DETAIL = f"{CRIT_DETAIL}（已按 --accept-sentinel 2026-09-23 人工放行）"
WARN_DETAIL = "2026-09-23：WARN 300pair 对内价差 6.50pp （判据 ≥6pp，需人工复核）"
UNCHECKED_DETAIL = "RuntimeError: boom（15 码已到齐，本日数据未经同族共动性检查）"
NO_VERDICT = "等数结果没给出同族哨兵结论（INPUTS_SENTINEL={}）；2026-09-23 15 码到齐（等 0 秒），本日数据未经同族共动性检查"


def _contract(status: str, reason: str, sentinel: str | None = "CLEAN", detail: str | None = CLEAN_DETAIL) -> str:
    """冒充等数脚本的输出：一行进度 + 契约行（sentinel / detail 给 None 就不打那一行）。"""
    lines = ["[wait_for_inputs] 20:31:17 第 1 轮：进度行", f"INPUTS_STATUS={status}", "INPUTS_DAY=2026-09-23",
             f"INPUTS_REASON={reason}"]
    lines += [] if sentinel is None else [f"INPUTS_SENTINEL={sentinel}"]
    lines += [] if detail is None else [f"INPUTS_SENTINEL_DETAIL={detail}"]
    return "\n".join(lines) + "\n"


def _skipped(status: str, reason: str) -> str:
    return _contract(status, reason, "SKIPPED", "输入未到齐，未做同族哨兵")


@pytest.mark.parametrize("stub_out, stub_rc, status, reason", [
    (_contract("OK", OK_REASON), 0, "OFFICE_OK", OK_REASON),
    (_skipped("LATE", LATE_REASON), 0, "OFFICE_LATE", LATE_REASON),
    (_skipped("CHECK_ERROR", ERR_REASON), 0, "OFFICE_CHECK_ERROR", ERR_REASON),
    # 到齐了、哨兵没判成（出错 / 缺行 / 不认识）：OFFICE_UNCHECKED 照常往下走，不当 OFFICE_OK 也不当 CHECK_ERROR
    (_contract("OK", OK_REASON, "SKIPPED", UNCHECKED_DETAIL), 0, "OFFICE_UNCHECKED", UNCHECKED_DETAIL),
    (_contract("OK", OK_REASON, "SKIPPED", None), 0, "OFFICE_UNCHECKED", NO_VERDICT.format("SKIPPED")),
    (_contract("OK", OK_REASON, None, None), 0, "OFFICE_UNCHECKED", NO_VERDICT.format("空")),
    (_contract("OK", OK_REASON, "FOO", "x"), 0, "OFFICE_UNCHECKED", NO_VERDICT.format("FOO")),
    # 只有 WARN：OFFICE_OK_WARN（不阻断），原因 = WARN 明细
    (_contract("OK", OK_REASON, "WARN", WARN_DETAIL), 0, "OFFICE_OK_WARN", WARN_DETAIL),
    (_contract("OK", OK_REASON, "WARN", None), 0, "OFFICE_OK_WARN", "同族哨兵 WARN（没记明细）"),
    # 人工放行（--accept-sentinel T）：OFFICE_ACCEPTED，原因 = 带放行注记的哨兵明细，不中止
    (_contract("OK", OK_REASON, "ACCEPTED", ACCEPTED_DETAIL), 0, "OFFICE_ACCEPTED", ACCEPTED_DETAIL),
    (_contract("OK", OK_REASON, "ACCEPTED", None), 0,
     "OFFICE_ACCEPTED", "同族哨兵 CRITICAL 已人工放行（没记明细）"),
    ("", 124, "OFFICE_CHECK_ERROR", "wait_for_inputs 无结果（exit 124）"),          # 超时被杀，什么都没打
    ("Traceback …\n", 1, "OFFICE_CHECK_ERROR", "wait_for_inputs 无结果（exit 1）"),  # 崩了
    ("", 0, "OFFICE_CHECK_ERROR", "wait_for_inputs 无结果（exit 0）"),              # 退 0 但没给结果
    (_skipped("WAITING", "x"), 0,
     "OFFICE_CHECK_ERROR", "wait_for_inputs 结果不认识（INPUTS_STATUS=WAITING，exit 0）"),
    # m-4：多组结果取最后一组；原因里的 = 原样保留；只有 STATUS 行 → 未记原因
    (_contract("OK", OK_REASON) + _skipped("LATE", LATE_REASON), 0, "OFFICE_LATE", LATE_REASON),
    (_skipped("CHECK_ERROR", "2026-09-23：OperationalError: host=10.0.0.1 port=5432 failed"), 0,
     "OFFICE_CHECK_ERROR", "2026-09-23：OperationalError: host=10.0.0.1 port=5432 failed"),
    ("INPUTS_STATUS=LATE\n", 0, "OFFICE_LATE", "未记原因"),
], ids=["ok", "late", "check-error", "unchecked", "unchecked-no-detail", "unchecked-missing", "unchecked-unknown",
        "ok-warn", "ok-warn-no-detail", "accepted",
        "accepted-no-detail", "timeout-no-output",
        "crash", "exit0-no-output", "unknown-status", "last-group-wins", "reason-with-equals", "status-only"])
def test_runner_office_mode_maps_wait_result(tmp_path, stub_out, stub_rc, status, reason):
    """标志文件在 → 跑等数脚本，按末尾的 INPUTS_* 映射；没解析到结果一律 OFFICE_CHECK_ERROR。这几种都不中止
    链路，状态文件字段名仍叫 topup（告警器与推送按它读）。"""
    out, got, argv = _run_step0(tmp_path, flag=True, stub_out=stub_out, stub_rc=stub_rc)
    log = out.stdout + out.stderr
    assert out.returncode == 0 and "FAIL_CALLED" not in log, log
    assert (got["RESULT_STATUS"], got["RESULT_REASON"]) == (status, reason), log
    steps = json.loads(got["RESULT_STEPS"])
    assert [(s["step"], s["status"]) for s in steps] == [("topup", status)], steps
    assert argv is not None and argv[1].endswith(f"/{WAIT}"), argv   # 真的调了等数脚本
    if stub_out.startswith("[wait_for_inputs]"):
        assert "第 1 轮：进度行" in out.stdout, log                     # 经 tee 实时进日志


@pytest.mark.parametrize("status", ["OK", "LATE"])
def test_runner_office_sentinel_critical_aborts_before_signals(tmp_path, status):
    """同族哨兵 CRITICAL → OFFICE_SUSPECT，office_inputs_stage 返回 1 → fail "inputs_check(OFFICE_SUSPECT)"：
    在信号重算之前中止、交告警器（同 2026-08-24 起 topup 事后审计的口径）。只看哨兵行，不看 STATUS。"""
    out, got, argv = _run_step0(tmp_path, flag=True, stub_out=_contract(status, "x", "CRITICAL", CRIT_DETAIL))
    log = out.stdout + out.stderr
    assert out.returncode == 1 and "FAIL_CALLED inputs_check(OFFICE_SUSPECT) 1" in log, log
    assert (got["RESULT_STATUS"], got["RESULT_REASON"]) == ("OFFICE_SUSPECT", CRIT_DETAIL), log
    steps = json.loads(got["RESULT_STEPS"])
    assert [(s["step"], s["status"]) for s in steps] == [("topup", "OFFICE_SUSPECT")], steps
    suspect = [line for line in out.stdout.splitlines() if line.startswith("LOG OFFICE_SUSPECT:")]
    assert suspect, log
    # 处置里给出能照抄的放行命令，日期就是这次的信号日
    assert any('STYLE_SIGNALS_INPUTS_ARGS="--accept-sentinel 2026-09-23"' in line for line in suspect), suspect


def test_runner_office_sentinel_critical_without_detail(tmp_path):
    out, got, _ = _run_step0(tmp_path, flag=True, stub_out=_contract("OK", "x", "CRITICAL", None))
    assert out.returncode == 1 and got["RESULT_REASON"] == "同族哨兵 CRITICAL（没记明细）", out.stdout


def test_runner_office_mode_result_copy_is_temporary(tmp_path):
    """结果副本每次 mktemp 新建（logs/.inputs_wait.XXXXXX）、解析完即删：不可能读到上一次的结果。"""
    listing = tmp_path / "ls"
    out, got, _ = _run_step0(tmp_path, flag=True, stub_out=_contract("OK", OK_REASON),
                             env_extra={"STUB_LS_DIR": str(tmp_path / "logs"), "STUB_LS": str(listing)})
    assert got["RESULT_STATUS"] == "OFFICE_OK", out.stdout + out.stderr
    during = listing.read_text(encoding="utf-8").split()
    assert len(during) == 1 and re.fullmatch(r"\.inputs_wait\.\w{6}", during[0]), during
    assert sorted(p.name for p in (tmp_path / "logs").iterdir()) == []


def test_runner_office_mode_mktemp_failure(tmp_path):
    """logs/ 不可写、建不了结果副本：记 OFFICE_CHECK_ERROR 照常往下走，不调等数脚本。"""
    try:
        out, got, argv = _run_step0(tmp_path, flag=True, stub_out=_contract("OK", OK_REASON),
                                    before='chmod 555 "${LOG_DIR}"')
    finally:
        (tmp_path / "logs").chmod(0o755)
    assert out.returncode == 0 and argv is None, out.stdout + out.stderr
    assert got["RESULT_STATUS"] == "OFFICE_CHECK_ERROR"
    assert got["RESULT_REASON"].startswith("建不了等数结果副本"), got


@pytest.mark.parametrize("inputs_args", [None, "--once", "--once --interval 60", "--accept-sentinel 2026-09-24"],
                         ids=["args-unset", "once", "two-args", "accept-sentinel"])
def test_runner_office_mode_argv(tmp_path, inputs_args):
    """桩看到的 argv = -u 脚本 [透传参数…]。未设（从子进程 env 里删掉，不是设空串）= 生产默认路径，
    set -u 下写成 ${STYLE_SIGNALS_INPUTS_ARGS}（不带 :-）会当场崩。"""
    env = {} if inputs_args is None else {"STYLE_SIGNALS_INPUTS_ARGS": inputs_args}
    out, got, argv = _run_step0(tmp_path, flag=True, stub_out=_contract("OK", "到齐"), env_extra=env)
    assert got["RESULT_STATUS"] == "OFFICE_OK", out.stdout + out.stderr
    head = ["-u", str(tmp_path / "deploy" / WAIT)]
    assert argv == head + ([] if inputs_args is None else inputs_args.split()), argv


def test_runner_office_mode_does_not_parse_stale_result(tmp_path):
    """旧的固定名副本 logs/.inputs_wait.out 残留（哪怕只读）也读不到：结果只从本次 mktemp 的副本解析。"""
    stale = "INPUTS_STATUS=OK\nINPUTS_DAY=2026-09-22\nINPUTS_REASON=昨天的结果\n"
    before = ("mkdir -p \"${LOG_DIR}\" && printf '%s' " + shlex.quote(stale)
              + " > \"${LOG_DIR}/.inputs_wait.out\" && chmod 444 \"${LOG_DIR}/.inputs_wait.out\"")
    out, got, _ = _run_step0(tmp_path, flag=True, stub_out="", stub_rc=124, before=before)
    assert got["RESULT_STATUS"] == "OFFICE_CHECK_ERROR", out.stdout + out.stderr
    assert got["RESULT_REASON"] == "wait_for_inputs 无结果（exit 124）"


def test_runner_env_skip_without_flag_keeps_old_behaviour(tmp_path):
    """只有环境变量 STYLE_SIGNALS_SKIP_TOPUP=1、没有标志文件：旧行为 TOPUP_SKIPPED，不等数、不调任何脚本。"""
    out, got, argv = _run_step0(tmp_path, flag=False, env_extra={"STYLE_SIGNALS_SKIP_TOPUP": "1"})
    assert out.returncode == 0, out.stdout + out.stderr
    assert (got["RESULT_STATUS"], got["RESULT_REASON"]) == ("TOPUP_SKIPPED", "环境变量 STYLE_SIGNALS_SKIP_TOPUP=1")
    assert argv is None


def test_runner_flag_wins_over_env_skip(tmp_path):
    out, got, argv = _run_step0(tmp_path, flag=True, stub_out=_contract("OK", "到齐"),
                                env_extra={"STYLE_SIGNALS_SKIP_TOPUP": "1"})
    assert got["RESULT_STATUS"] == "OFFICE_OK" and argv[1].endswith(f"/{WAIT}"), out.stdout + out.stderr


def test_runner_topup_mode_keeps_topup_audit_step_name(tmp_path):
    """回退的 topup 模式：前置闸门放行、topup 调不起来（桩仓库里没有脚本 → DEGRADED）、事后审计判可疑 →
    fail 的步骤名仍是 topup_audit(SUSPECT)；inputs_check(...) 只属于办公室模式。"""
    out, got, _ = _run_step0(tmp_path, flag=False, stub_rc=0, env_extra={"STUB_AUDIT_RC": "1"})
    log = out.stdout + out.stderr
    assert out.returncode == 1 and "FAIL_CALLED topup_audit(SUSPECT) 1" in log, log
    assert got["RESULT_STATUS"] == "SUSPECT"


def test_runner_office_mode_sweeps_stale_result_copies(tmp_path):
    """办公室分支开头清理 logs/ 下超过 60 分钟的 .inputs_wait.* 残留（链路中途被杀留下的）；新的、子目录里的不碰。"""
    import time as _time
    now = int(_time.time())
    ages = {".inputs_wait.old123": 2 * 3600, ".inputs_wait.new456": 60, "other.log": 2 * 3600,
            "sub/.inputs_wait.deep99": 2 * 3600}
    # logs/ 由 _run_step0 建；残留文件在跑第 0 步之前（before 钩子里）建，时间戳用 touch -d 设
    before = " && ".join(['mkdir -p "${LOG_DIR}/sub"'] +
                         [f'touch -d @{now - age} "${{LOG_DIR}}/{name}"' for name, age in ages.items()])
    logs_dir = tmp_path / "logs"
    out, got, _ = _run_step0(tmp_path, flag=True, stub_out=_contract("OK", OK_REASON), before=before)
    assert got["RESULT_STATUS"] == "OFFICE_OK", out.stdout + out.stderr
    left = sorted(p.name for p in logs_dir.iterdir())
    assert left == [".inputs_wait.new456", "other.log", "sub"], left
    assert (logs_dir / "sub" / ".inputs_wait.deep99").exists()


def test_runner_without_flag_takes_topup_path(tmp_path):
    """标志文件不在：原 topup 路径（回退用）——先跑前置闸门；闸门说 SKIP 就 TOPUP_SKIPPED，不等数。"""
    out, got, argv = _run_step0(tmp_path, flag=False, stub_out="SKIP_REASON=测试：闸门不放行\n", stub_rc=10)
    assert out.returncode == 0, out.stdout + out.stderr
    assert (got["RESULT_STATUS"], got["RESULT_REASON"]) == ("TOPUP_SKIPPED", "测试：闸门不放行")
    assert argv[:3] == [str(tmp_path / "deploy" / "topup_guard.py"), "--mode", "preflight"], argv


# ── 告警器：失败通知 ──────────────────────────────────────────────────────────

def test_alerter_push_is_best_effort(alerter):
    """告警器调 notify_wechat.py --alert：限时（带 -k）、后跟 ||——推不出去只记进告警文件，不传播。"""
    i, duration, kill, cmd = _notify_call(alerter, "告警器的推送调用")
    assert duration is not None and kill > 0, f"须外包 timeout -k：{alerter[i]}"
    assert "||" in cmd, f"须后跟 ||（告警器自己绝不失败）：{alerter[i]}"
    assert NOTIFY_ARGS.search(alerter[i]), f"须不加引号透传 STYLE_SIGNALS_NOTIFY_ARGS：{alerter[i]}"
    args = _check_push_argv(cmd, ["--alert", "--status-file", "--systemd-result", "--run-started"])
    assert args[args.index("--status-file") + 1] == "${STATUS_FILE}"
    # 与 runner 写的是同一份状态文件
    status = [line for line in alerter if line.startswith("STATUS_FILE=")]
    assert status and status == [line for line in _logical_lines(RUNNER) if line.startswith("STATUS_FILE=")]


def test_alerter_passes_main_service_result_and_start(alerter):
    """--systemd-result / --run-started 取自**主** service：查成告警单元自己的启动时刻，每份状态文件都会
    被判成「上一次运行留下的」。属性名带边界（ExecMainStartTimestampMonotonic 是另一个量）；
    --timestamp=unix 给出 @秒，推送脚本才解析得了，少了它会静默退回按日期判断。"""
    call = alerter[_only(_calls(alerter, NOTIFY), "告警器的推送调用")]
    unit = rf"(?<![\w.-]){re.escape(MAIN_UNIT)}(?![\w.-])"
    result = _arg_source(alerter, call, "--systemd-result")
    for pattern in (unit, r"-p\s*Result\b", r"(?<!\S)--value(?!\S)"):
        assert re.search(pattern, result), f"{pattern}：{result}"
    started = _arg_source(alerter, call, "--run-started")
    for pattern in (unit, r"-p\s*ExecMainStartTimestamp\b", r"(?<!\S)--value(?!\S)", r"--timestamp=unix\b"):
        assert re.search(pattern, started), f"{pattern}：{started}"


def test_alerter_appends_push_output_to_alert_file(alerter):
    """推送输出（含 REFUSED / SEND_FAILED）追加进告警文件留底，且在告警文件写好之后——
    顺序反了会被后面的 > 冲掉。"""
    write = _only([i for i, line in enumerate(alerter) if re.match(r'\}\s*>\s*"\$\{ALERT_FILE\}"', line)],
                  "告警文件的首次写入")
    push = _only(_calls(alerter, NOTIFY), "告警器的推送调用")
    close = next(i for i in range(push, len(alerter)) if alerter[i].startswith("}"))
    assert write < push
    assert re.fullmatch(r'\}\s*>>\s*"\$\{ALERT_FILE\}"\s+2>&1', alerter[close]), alerter[close]


def test_alerter_summary_shows_notify():
    """告警文件的状态摘要打印 notify 段：推送失败的原因（拒推理由 / 企业微信回的 errcode）记在那里。"""
    assert re.search(r"notify\s*=\s*\{d\.get\(['\"]notify['\"]\)\}", ALERTER.read_text(encoding="utf-8"))


def test_alerter_text_runs_no_commands(alerter):
    """告警文本里的命令是写给人照抄的，不能在告警时被执行：echo / printf 行里的反引号是命令替换；
    【处置】段再禁 $(…)——写成 $(python3 …) 会在告警时当场补发。摘要段的 $(date …) 是有意取值，
    不在此列。"""
    shown = [i for i, line in enumerate(alerter) if re.match(r"(echo|printf)\b", line)]
    assert not [alerter[i] for i in shown if "`" in alerter[i]]
    start = _only([i for i, line in enumerate(alerter) if re.fullmatch(r'echo\s+"\[处置\]"', line)],
                  "【处置】段标题")
    end = next(i for i in range(start, len(alerter)) if alerter[i].startswith("}"))
    bad = [alerter[i] for i in shown if start < i < end and re.search(r"`|\$\(", alerter[i])]
    assert not bad, bad


def test_alerter_explains_office_suspect(alerter):
    """【处置】段说清 OFFICE_SUSPECT（办公室模式下同族哨兵拦下的中止）怎么办：它不是 topup 模式的 SUSPECT，
    不能照「置 SKIP_TOPUP」处置——标志文件本来就在。"""
    start = _only([i for i, line in enumerate(alerter) if re.fullmatch(r'echo\s+"\[处置\]"', line)], "【处置】段标题")
    end = next(i for i in range(start, len(alerter)) if alerter[i].startswith("}"))
    assert any("OFFICE_SUSPECT" in line for line in alerter[start:end]), alerter[start:end]
    assert any("--accept-sentinel" in line for line in alerter[start:end]), alerter[start:end]
    assert any("inputs_check(OFFICE_SUSPECT)" in line for line in alerter[start:end]), alerter[start:end]


def test_alerter_time_budget(alerter):
    """告警器里会卡住的外部调用（推送、notify-send）都限时，合计（含宽限）给告警单元 TimeoutStartSec
    留出余量——超了 systemd 会把告警器整个杀掉，排在后面的通知全丢。"""
    sends = [i for i, line in enumerate(alerter) if re.match(r"(timeout(\s+\S+)+?\s+)?notify-send\b", line)]
    i = _only(sends, "notify-send 调用")
    d_send, k_send, cmd_send = _split_timeout(shlex.split(alerter[i]))
    assert d_send is not None, f"notify-send 须限时：{alerter[i]}"
    assert cmd_send[-2:] == ["||", "true"], f"notify-send 须后跟 || true：{alerter[i]}"
    _, d_push, k_push, _ = _notify_call(alerter, "告警器的推送调用")
    limit = _timeout_start_sec(ALERT_SERVICE)
    assert d_push + k_push + d_send + k_send <= limit - ALERT_BUDGET_MARGIN, (d_push, k_push, d_send, k_send, limit)


def test_alerter_never_fails(alerter):
    """不开 errexit、末尾 exit 0：告警器自己绝不能成为新的失败源。"""
    assert not [line for line in alerter if _sets_errexit(line)]
    assert [line for line in alerter if line][-1] == "exit 0"


# ── service ───────────────────────────────────────────────────────────────────

def test_service_lock_conflict_is_success(runner):
    """runner 撞锁退出 75（另一实例在跑，由它推送）：service 须把 75 记成功，否则 OnFailure 发假告警。
    按 systemd 语义取生效值（空赋值清空）；两边的 75 要对得上，这条配置才不是空的。"""
    codes = " ".join(_unit(SERVICE).get("Service", {}).get("SuccessExitStatus", [])).split()
    assert "75" in codes, codes
    start = _find(runner, r"if ! flock -n\b.*")
    assert any(re.fullmatch(r'exit\s+"?75"?', line) for line in runner[start + 1:_matching_fi(runner, start)])


def test_service_timeout_covers_wait_and_push(runner):
    """主 service 的 TimeoutStartSec 要装得下「等数兜底 + 推送限时」（都含宽限）再留 600 秒：否则办公室迟到
    那晚还在等数，systemd 就先把整条链杀了——信号不算、不推，只剩一条 timeout 告警。"""
    _, d_wait, k_wait, _ = _wait_call(runner)
    _, d_push, k_push, _ = _notify_call(runner, "runner 的推送调用")
    limit = _timeout_start_sec(SERVICE)
    assert d_wait + k_wait + d_push + k_push + SERVICE_BUDGET_MARGIN <= limit, (d_wait, k_wait, d_push, k_push, limit)


def test_runner_wait_timeout_covers_max_wait_and_last_round(runner):
    """runner 的等数兜底 ≥ 等数脚本 max-wait 默认值 + 截止那一轮最坏查询耗时 + 余量：等数脚本到点自己报 LATE，
    不被兜底杀掉（被杀 = 没结果，只能记 OFFICE_CHECK_ERROR，哨兵也没跑成）。一轮最多三次查询（重读日历 +
    到齐 + 哨兵），单次最坏 = 连库超时 + 单句超时，取自 connect_kwargs 实际交给 psycopg2 的参数。"""
    _, duration, _, _ = _wait_call(runner)
    kw = WFI.connect_kwargs({"host": "h", "port": 1, "name": "n", "user": "u", "password": "p", "schema": "s"})
    per_query = kw["connect_timeout"] + int(re.search(r"statement_timeout=(\d+)", kw["options"]).group(1)) / 1000
    need = WFI.DEFAULT_MAX_WAIT + WFI.QUERIES_PER_ROUND_MAX * per_query + WAIT_BUDGET_SLACK
    assert duration >= need, (duration, WFI.DEFAULT_MAX_WAIT, WFI.QUERIES_PER_ROUND_MAX, per_query)


def _clock_seconds(text: str) -> int:
    hh, mm = text.split(":")
    return int(hh) * 3600 + int(mm) * 60


def test_timer_window_fits_wait_defaults():
    """定时器（OnCalendar + RandomizedDelaySec + AccuracySec）与等数默认值自洽：
    ① 按 Asia/Shanghai 触发（等数脚本的时钟也是北京时间）；
    ② 最早起跑 ≥ ready-from：定时器那次的信号日是「今天」，才会等今天的数；
    ③ 最晚起跑 < 截止：定时器那次一定有得等；
    ④ 从起跑等到截止 ≤ max-wait：最早起跑时等得最久，这一条对它成立，对 [最早, 最晚] 里任何起跑时刻都成立——
       max-wait 只截短手工早跑，截不到定时器。"""
    timer = _unit(TIMER)["Timer"]
    on_calendar = timer["OnCalendar"][-1]
    assert "Asia/Shanghai" in on_calendar.split(), on_calendar
    m = re.search(r"(?<!\d)(\d{1,2}):(\d{2})(?!\d)", on_calendar)
    assert m, on_calendar
    earliest = int(m.group(1)) * 3600 + int(m.group(2)) * 60
    latest = earliest + _timespan(timer["RandomizedDelaySec"][-1]) + _timespan(timer.get("AccuracySec", ["1min"])[-1])
    ready, deadline = _clock_seconds(WFI.DEFAULT_READY_FROM), _clock_seconds(WFI.DEFAULT_DEADLINE)
    assert ready <= earliest, "定时器早于 ready-from 起跑：信号日会取成前一天，永远不等当天的数"
    assert latest < deadline, "定时器最晚起跑已过截止：等数形同虚设"
    assert deadline - earliest <= WFI.DEFAULT_MAX_WAIT, "定时器起跑的自然等待被 max-wait 截短"


def test_service_keeps_onfailure_and_no_install():
    """OnFailure 仍接告警单元；无 [Install]（由 timer 拉起，enable 了会每次登录双跑）。"""
    unit = _unit(SERVICE)
    assert "style-signals-daily-alert.service" in " ".join(unit["Unit"].get("OnFailure", [])).split()
    assert "Install" not in unit
