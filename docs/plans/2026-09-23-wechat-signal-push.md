# 日更信号企业微信推送 Implementation Plan

> **For Claude:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task.

**Goal:** 给现有 18:30 日更链补上企业微信推送：护栏通过后推当日持仓，链路失败时告警器推失败通知。

**Architecture:** 新增 `deploy/daily_signals/notify_wechat.py`（纯标准库 + 成功路径才 import `backtest.*`），
作为 `run_daily_signals.sh` 的步骤 8；`alert_on_failure.sh` 以 `--alert` 模式调用它。池子映射进
`backtest/production.py::POOLS`，推送对象必须是护栏担保过的文件（运行期校验）。设计见
`docs/plans/2026-09-23-wechat-signal-push-design.md`。

**Tech Stack:** Python 3（miniconda，stdlib `urllib`/`csv`/`json`）、bash、systemd user units、pytest。

**约定（所有任务通用）：**
- 工作目录：`/home/elfbob/claude-code/style_timing_signal/.worktrees/wechat-signal-push`（分支 `feat/wechat-signal-push`），**不要 cd 到主仓**。
- 解释器：`PY=/home/elfbob/miniconda3/bin/python3`；测试命令一律 `$PY -m pytest -q -p no:cacheprovider <文件>`。
- **禁止**：`git add -A` / `git add .`（主仓与本 worktree 都有大量无关改动，逐个列文件）；打印 webhook URL
  或 `~/.config/market-monitor/alert.env` 的内容；真实发送企业微信（测试一律用本地 HTTP 桩）；
  运行 `tools/topup_index_daily.sh`；改主仓 `/home/elfbob/claude-code/style_timing_signal` 下任何文件。
- 提交信息沿用仓库中文惯例，结尾加：`Co-Authored-By: Claude Opus 5.5 (1M context) <noreply@anthropic.com>`

---

### Task 1: `backtest/production.py` —— 两池映射单一入口

**Files:**
- Modify: `backtest/production.py:25-35`
- Test: `tests/test_bt_production.py`（末尾追加）

**Step 1: Write the failing test**（追加到 `tests/test_bt_production.py` 末尾）

```python
def test_pools_map_to_files_actually_written():
    """2026-09-10 两池裁决：期货池=equal_weight 对称、现货池=slope20 long-flat。
    推送/展示从 POOLS/POOL_FILES 取；每个池文件必须是 write_recommended_positions 真写出来的那份。"""
    import tempfile
    from backtest.production import (POOL_FILES, POOLS, PRODUCTION_MAPPING, RECOMMENDED_FILES,
                                     REFERENCE_OUTPUTS, recommended_file, write_recommended_positions)
    assert POOLS == {"期货池": ("equal_weight", "symmetric"), "现货池": ("slope20", "longflat")}
    assert POOL_FILES == {"期货池": "output/recommended/equal_weight_symmetric.csv",
                          "现货池": "output/recommended/slope20_longflat.csv"}
    for name, m in POOLS.values():
        assert PRODUCTION_MAPPING.get(name) == m or REFERENCE_OUTPUTS.get(name) == m
    assert all(RECOMMENDED_FILES[n] == recommended_file(n, m) for n, m in PRODUCTION_MAPPING.items())
    with tempfile.TemporaryDirectory() as d:
        written = {p.name for p in write_recommended_positions(Path(d)).values()}
    assert {Path(p).name for p in POOL_FILES.values()} <= written
```

**Step 2: Run test to verify it fails**

Run: `$PY -m pytest -q -p no:cacheprovider tests/test_bt_production.py::test_pools_map_to_files_actually_written`
Expected: FAIL（`ImportError: cannot import name 'POOL_FILES'`）

**Step 3: Write minimal implementation** —— 把 `backtest/production.py` 第 25~35 行替换为：

```python
PRODUCTION_MAPPING = {"hybrid20": "longflat", "citic40d": "longflat", "equal_weight": "symmetric", "slope20": "symmetric"}   # slope20 2026-09-09 上线，空头腿 0.62 同形态
MAPPERS = {"longflat": production_position, "symmetric": symmetric_position}


def recommended_file(name: str, mapping: str) -> str:
    """推荐持仓文件的仓库相对路径（生产口径与参照口径同一命名规则）。"""
    return f"output/recommended/{name}_{mapping}.csv"


# 下游（仪表盘 / 新鲜度护栏 / 合并导出 / 推送）一律从这里取推荐持仓文件，不要自己拼文件名。
RECOMMENDED_FILES = {name: recommended_file(name, m) for name, m in PRODUCTION_MAPPING.items()}
# 参照产出：非现役口径也照常写，便于对照与回滚（equal_weight 的 long-flat）。
REFERENCE_OUTPUTS = {
    "equal_weight": "longflat",
    # 2026-09-10 用户裁决「两池分信号」：现货池（只多）跟 slope20 long-flat，期货池跟 equal_weight 对称。
    # 现货池文件 = 本参照产出；决策记录 docs/plans/2026-09-10-two-pool-signal-assignment-decision.md。
    "slope20": "longflat",
}
# 实盘两池 → (信号线, 持仓口径)，2026-09-10 用户裁决（063c322）。企业微信推送按这里置顶两池。
POOLS = {"期货池": ("equal_weight", "symmetric"), "现货池": ("slope20", "longflat")}
POOL_FILES = {pool: recommended_file(name, m) for pool, (name, m) in POOLS.items()}
```

（`write_recommended_positions` 不动。）

**Step 4: Run tests** —— `$PY -m pytest -q -p no:cacheprovider tests/test_bt_production.py` → 全 PASS

**Step 5: Commit**

```bash
git add backtest/production.py tests/test_bt_production.py
git commit -m "feat(production): POOLS/POOL_FILES —— 两池映射单一入口（推送/展示从这里取）"
```

---

### Task 2: 现货池文件升为护栏对象

**Files:**
- Modify: `deploy/daily_signals/check_freshness.py:43-60`
- Test: `tests/test_deploy_freshness_guard.py`（`test_gated_set_matches_production_signals` 末尾追加断言）

**Step 1: Write the failing test** —— 在 `test_gated_set_matches_production_signals` 函数体末尾追加：

```python
    # 2026-09-23 企业微信推送上线：推送把两池文件当可行动持仓发出去，两池文件必须是护栏对象。
    from backtest.production import POOL_FILES
    for pool, path in POOL_FILES.items():
        assert path in gated_paths, f"{pool} 文件 {path} 不在护栏清单里"
```

**Step 2: Run** `$PY -m pytest -q -p no:cacheprovider tests/test_deploy_freshness_guard.py::test_gated_set_matches_production_signals`
Expected: FAIL（`现货池 文件 output/recommended/slope20_longflat.csv 不在护栏清单里`）

**Step 3: Implement** —— `check_freshness.py`：
- `GATED` 上方注释改为：`# 硬护栏对象：四条生产信号（backtest.baseline.SIGNALS）+ 四份推荐持仓 + 两池文件（backtest.production.POOL_FILES）。`
- `GATED` 末尾加一行：
  `    "recommended_slope20_longflat": "output/recommended/slope20_longflat.csv",   # 现货池；2026-09-23 推送上线由「参考」升护栏`
- 从 `INFORMATIONAL` 删掉 `"recommended_slope20_longflat_ref": ...` 那一行。

**Step 4: Run** `$PY -m pytest -q -p no:cacheprovider tests/test_deploy_freshness_guard.py` → 全 PASS

**Step 5: Commit**

```bash
git add deploy/daily_signals/check_freshness.py tests/test_deploy_freshness_guard.py
git commit -m "fix(guard): 现货池 slope20_longflat 由参考升护栏 —— 推送对象必须被护栏担保"
```

---

### Task 3: `notify_wechat.py` —— 消息组装（纯函数）

**Files:**
- Create: `deploy/daily_signals/notify_wechat.py`
- Create: `tests/test_deploy_notify_wechat.py`

**Step 1: Write the failing tests** —— 新建 `tests/test_deploy_notify_wechat.py`：

```python
"""deploy/daily_signals/notify_wechat.py 单测（不连库、不连外网：发送一律打本地 HTTP 桩）。"""
import importlib.util
import json
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
NOTIFY_PATH = ROOT / "deploy" / "daily_signals" / "notify_wechat.py"


def _load():
    sys.path.insert(0, str(ROOT))
    spec = importlib.util.spec_from_file_location("notify_wechat", NOTIFY_PATH)
    module = importlib.util.module_from_spec(spec)
    # 必须先登记：模块里有 dataclass + `from __future__ import annotations`，dataclasses 解析
    # 字符串注解时会查 sys.modules[cls.__module__]，不登记就 AttributeError。
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


nw = _load()

DATES = ["2026-09-16", "2026-09-17", "2026-09-18", "2026-09-21", "2026-09-22"]
# 推荐持仓（文件 → 5 天持仓）与信号值（信号线 → 末日值）；路径一律从映射取，不在测试里拼。
POSITIONS = {
    ("equal_weight", "symmetric"): [-1, 1, 1, 1, 1],   # 期货池：09-17 起持多第 4 日
    ("slope20", "longflat"): [0, 0, 0, 0, 1],          # 现货池：09-22 翻仓
    ("slope20", "symmetric"): [-1, -1, -1, -1, 1],     # 参考：翻仓 持空→持多
    ("hybrid20", "longflat"): [0, 0, 0, 0, 0],         # 参考：首行起就空仓
    ("citic40d", "longflat"): [1, 1, 1, 1, 1],
}
SIGNAL_LAST = {"equal_weight": "0.2784", "slope20": "0.0461", "hybrid20": "0", "citic40d": "0.4318"}


def _write_csv(path: Path, header: list[str], rows: list[list]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    lines = [",".join(header)] + [",".join(str(v) for v in r) for r in rows]
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def make_tree(root: Path, *, drop_last: tuple[str, str] | None = None) -> dict:
    """造出所有推送对象 + 信号文件，返回与之匹配的 OK 状态字典。drop_last：该 (线, 口径) 少最后一天。"""
    from backtest.baseline import SIGNALS
    from backtest.production import recommended_file
    files = {}
    for (name, m), pos in POSITIONS.items():
        rel = recommended_file(name, m)
        rows = [[d, p] for d, p in zip(DATES, pos)]
        if drop_last == (name, m):
            rows = rows[:-1]
        _write_csv(root / rel, ["date", "position"], rows)
        files[f"{name}_{m}"] = {"path": rel, "last_date": rows[-1][0], "gated": True}
    for name, last in SIGNAL_LAST.items():
        rel, col = SIGNALS[name]
        _write_csv(root / rel, ["date", col], [[d, "0.1"] for d in DATES[:-1]] + [[DATES[-1], last]])
    return {
        "result": "OK", "failed_step": None, "finished_at": "2026-09-22T18:31:05+08:00",
        "topup": "OK", "topup_reason": None,
        "upstream": {"max_trade_date": DATES[-1], "gaps": []},
        "files": files, "max_lag_trading_days": 0, "max_lag_allowed": 1,
        "output_gap_total": 0, "breaches": [], "upstream_breach": None,
    }


def compose(tmp_path, status=None, today="2026-09-22", **kw):
    st = make_tree(tmp_path, **kw) if status is None else status
    text, as_of = nw.compose_message(tmp_path, st, today=today)
    return text, as_of, text.split("\n")


def test_two_pools_come_first_with_run_and_value(tmp_path):
    text, as_of, lines = compose(tmp_path)
    assert as_of == "2026-09-22"
    assert lines[0] == "风格择时 信号日 2026-09-22｜链路 OK"
    assert lines[1] == "【期货池】equal_weight 对称：持多 +1（09-17 起第 4 日）信号值 0.2784"
    assert lines[2] == "【现货池】slope20 long-flat：⚡翻仓 空仓 0 → 持多 +1 信号值 0.0461"


def test_other_production_lines_listed_once_as_reference(tmp_path):
    text, _, lines = compose(tmp_path)
    ref = lines[lines.index("【其余生产线·参考】") + 1:]
    assert "  slope20 对称：⚡翻仓 持空 -1 → 持多 +1 信号值 0.0461" in ref
    assert "  hybrid20 long-flat：空仓 0（09-16 起第 5 日）信号值 0" in ref
    assert "  citic40d long-flat：持多 +1（09-16 起第 5 日）信号值 0.4318" in ref
    assert text.count("equal_weight") == 1          # 期货池已置顶，参考区不重复
    assert "equal_weight long-flat" not in text     # 参照口径不推


def test_header_says_today_when_signal_day_differs(tmp_path):
    _, _, lines = compose(tmp_path, today="2026-10-01")
    assert lines[0] == "风格择时 信号日 2026-09-22（今天 10-01）｜链路 OK"


def test_line_behind_signal_day_is_marked(tmp_path):
    _, _, lines = compose(tmp_path, drop_last=("citic40d", "longflat"))
    assert "  citic40d long-flat：持多 +1（09-16 起第 4 日）（末行 09-21）信号值 0.1" in lines


def test_health_line_when_everything_ok(tmp_path):
    _, _, lines = compose(tmp_path)
    assert lines[-1] == "topup OK · 护栏 OK（最大落后 0 交易日 · 缺口 0）"


def test_degraded_topup_and_upstream_gaps_are_flagged(tmp_path):
    st = make_tree(tmp_path)
    st["topup"], st["topup_reason"] = "TOPUP_SKIPPED", "前置闸门：额度不足"
    st["upstream"]["gaps"] = ["2026-09-18"]
    _, _, lines = compose(tmp_path, status=st)
    assert lines[-3] == "护栏 OK（最大落后 0 交易日 · 缺口 0）"
    assert lines[-2] == "⚠ topup TOPUP_SKIPPED：前置闸门：额度不足"
    assert lines[-1] == "⚠ 上游缺 1 天：2026-09-18"


def test_refuses_when_guard_result_not_ok(tmp_path):
    st = make_tree(tmp_path)
    st["result"] = "STALE"
    with pytest.raises(nw.Refused, match="STALE"):
        nw.compose_message(tmp_path, st, today="2026-09-22")


def test_refuses_file_not_vouched_by_guard(tmp_path):
    st = make_tree(tmp_path)
    st["files"]["slope20_longflat"]["gated"] = False
    with pytest.raises(nw.Refused, match="slope20_longflat.csv"):
        nw.compose_message(tmp_path, st, today="2026-09-22")


def test_refuses_file_changed_after_guard(tmp_path):
    st = make_tree(tmp_path)
    st["files"]["equal_weight_symmetric"]["last_date"] = "2026-09-21"
    with pytest.raises(nw.Refused, match="2026-09-21"):
        nw.compose_message(tmp_path, st, today="2026-09-22")


def test_truncate_keeps_head_and_says_how_many_dropped():
    text = "\n".join(["头一行"] + [f"第 {i} 行 " + "长" * 40 for i in range(60)])
    out = nw.truncate_text(text, limit=2048, note="见日志")
    assert len(out.encode("utf-8")) <= 2048
    assert out.startswith("头一行\n第 0 行")
    assert out.endswith("行，见日志）") and "还有" in out


def test_real_message_fits_wechat_limit(tmp_path):
    text, _, _ = compose(tmp_path)
    assert len(text.encode("utf-8")) <= nw.WECHAT_TEXT_LIMIT
```

**Step 2: Run** `$PY -m pytest -q -p no:cacheprovider tests/test_deploy_notify_wechat.py`
Expected: FAIL（`FileNotFoundError`/`No such file`：`notify_wechat.py` 还不存在）

**Step 3: Write minimal implementation** —— 新建 `deploy/daily_signals/notify_wechat.py`（本任务只写到组装为止，发送与 CLI 在 Task 4~6 补）：

```python
"""日更信号链的企业微信推送（run_daily_signals.sh 步骤 8；--alert 由 alert_on_failure.sh 调用）。

成功路径：护栏通过后推当日持仓——两池置顶（backtest.production.POOLS），其余生产线作参考，
末尾一行链路体检。**只推护栏担保过的文件**：状态文件 result 必须是 OK，且每份要推的持仓文件都以
gated=true 出现在状态文件 files 里、last_date 与文件当前末行一致，否则拒推（exit 1）。

失败路径（--alert）：只读状态文件拼一条失败通知，不 import pandas——科学栈坏了也要能报警。

webhook：环境变量 ALERT_WEBHOOK_URL 优先，否则读 ~/.config/market-monitor/alert.env
（bs-toolkit、数据管理办公室同一个机器人）。URL 里带 key，任何输出都不打印它。
qyapi.weixin.qq.com 是国内端点，本机 Clash 代理会让它失败 → 显式绕代理。

用法：
    python3 deploy/daily_signals/notify_wechat.py --dry-run     # 只打印，不发、不写状态文件
    python3 deploy/daily_signals/notify_wechat.py               # 发送，并把 notify 段写回状态文件
    python3 deploy/daily_signals/notify_wechat.py --alert --systemd-result exit-code
设计：docs/plans/2026-09-23-wechat-signal-push-design.md
"""
from __future__ import annotations

import csv
import sys
from dataclasses import dataclass
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

#: 企业微信 text 消息超过这么多 UTF-8 字节会被整条拒收，不是截断。
WECHAT_TEXT_LIMIT = 2048
POSITION_LABELS = {1: "持多 +1", 0: "空仓 0", -1: "持空 -1"}
MAPPING_LABELS = {"symmetric": "对称", "longflat": "long-flat"}
LOG_NOTE = "全文见当日运行日志"


class Refused(RuntimeError):
    """不该推（护栏未过 / 推送对象未经护栏担保 / 没配 webhook）。"""


@dataclass
class Line:
    name: str          # 信号线
    mapping: str       # symmetric / longflat
    rel_path: str      # 推荐持仓文件（仓库相对路径）
    last_date: str
    position: int
    run_start: str     # 当前这段持仓的起始日
    run_days: int
    prev: int | None   # 上一段持仓；序列从头就是这一段时为 None
    value: str | None  # 信号值（SIGNALS 的列，信号日当天，原样字符串）


def _read_column(path: Path, col: str) -> list[tuple[str, str]]:
    with path.open(encoding="utf-8", newline="") as fh:
        return [(row["date"], row[col]) for row in csv.DictReader(fh)]


def _load_line(root: Path, name: str, mapping: str, rel_path: str, signals: dict) -> Line:
    rows = [(d, int(float(v))) for d, v in _read_column(root / rel_path, "position")]
    if not rows:
        raise Refused(f"{rel_path} 是空文件")
    last_date, pos = rows[-1]
    i = len(rows) - 1
    while i > 0 and rows[i - 1][1] == pos:
        i -= 1
    sig_path, col = signals[name]
    value = dict(_read_column(root / sig_path, col)).get(last_date)
    return Line(name, mapping, rel_path, last_date, pos, rows[i][0], len(rows) - i,
                rows[i - 1][1] if i > 0 else None, value)


def collect_lines(root: Path) -> tuple[list[tuple[str, Line]], list[Line]]:
    """-> ([(池名, 行)], [其余生产线的行])。映射全部取自 backtest.production，不在这里拼文件名。"""
    from backtest.baseline import SIGNALS
    from backtest.production import POOLS, PRODUCTION_MAPPING, recommended_file
    pools = [(pool, _load_line(root, name, m, recommended_file(name, m), SIGNALS))
             for pool, (name, m) in POOLS.items()]
    pooled = set(POOLS.values())
    others = [_load_line(root, name, m, recommended_file(name, m), SIGNALS)
              for name, m in PRODUCTION_MAPPING.items() if (name, m) not in pooled]
    return pools, others


def check_vouched(status: dict, lines: list[Line]) -> None:
    """只推护栏担保过的东西：result=OK，且每份文件 gated 且末行与护栏核验时一致。"""
    if status.get("result") != "OK":
        raise Refused(f"护栏结果是 {status.get('result')}，不推持仓")
    vouched = {f.get("path"): f.get("last_date")
               for f in (status.get("files") or {}).values() if f.get("gated")}
    for line in lines:
        if line.rel_path not in vouched:
            raise Refused(f"{line.rel_path} 不是护栏对象，不推")
        if vouched[line.rel_path] != line.last_date:
            raise Refused(f"{line.rel_path} 末行 {line.last_date} 与护栏核验时的 "
                          f"{vouched[line.rel_path]} 不一致（护栏之后文件又被改过），不推")


def _short(day: str, ref: str) -> str:
    return day[5:] if day[:4] == ref[:4] else day


def describe(line: Line, as_of: str) -> str:
    pos = POSITION_LABELS.get(line.position, str(line.position))
    if line.run_days == 1 and line.prev is not None:
        body = f"⚡翻仓 {POSITION_LABELS.get(line.prev, str(line.prev))} → {pos}"
    else:
        body = f"{pos}（{_short(line.run_start, as_of)} 起第 {line.run_days} 日）"
    lag = f"（末行 {_short(line.last_date, as_of)}）" if line.last_date != as_of else ""
    value = line.value if line.value not in (None, "") else "—"
    return f"{line.name} {MAPPING_LABELS.get(line.mapping, line.mapping)}：{body}{lag} 信号值 {value}"


def health_lines(status: dict) -> list[str]:
    guard = (f"护栏 OK（最大落后 {status.get('max_lag_trading_days')} 交易日 · "
             f"缺口 {status.get('output_gap_total')}）")
    topup = status.get("topup")
    out = [f"topup OK · {guard}" if topup == "OK" else guard]
    if topup != "OK":
        out.append(f"⚠ topup {topup}：{status.get('topup_reason') or '未记原因'}")
    gaps = (status.get("upstream") or {}).get("gaps") or []
    if gaps:
        more = " 等" if len(gaps) > 5 else ""
        out.append(f"⚠ 上游缺 {len(gaps)} 天：{'、'.join(gaps[:5])}{more}")
    return out


def truncate_text(text: str, *, limit: int, note: str = "") -> str:
    """从末尾整行删到 UTF-8 字节数装得下，并注明删了几行（照搬 bs-toolkit）。"""
    if len(text.encode("utf-8")) <= limit:
        return text
    lines = text.split("\n")
    dropped = 0
    while lines:
        dropped += 1
        lines.pop()
        tail = f"…（超长，还有 {dropped} 行{'，' + note if note else ''}）"
        candidate = "\n".join([*lines, tail])
        if len(candidate.encode("utf-8")) <= limit:
            return candidate
    return f"…（超长，还有 {dropped} 行{'，' + note if note else ''}）"


def compose_message(root: Path, status: dict, *, today: str) -> tuple[str, str]:
    """-> (消息正文, 信号日)。推送对象未经护栏担保则 raise Refused。"""
    pools, others = collect_lines(root)
    check_vouched(status, [line for _, line in pools] + others)
    as_of = max(line.last_date for line in [line for _, line in pools] + others)
    head = f"风格择时 信号日 {as_of}"
    if as_of != today:
        head += f"（今天 {_short(today, as_of)}）"
    lines = [head + "｜链路 OK"]
    lines += [f"【{pool}】{describe(line, as_of)}" for pool, line in pools]
    lines.append("【其余生产线·参考】")
    lines += [f"  {describe(line, as_of)}" for line in others]
    lines += health_lines(status)
    return truncate_text("\n".join(lines), limit=WECHAT_TEXT_LIMIT, note=LOG_NOTE), as_of
```

**Step 4: Run** `$PY -m pytest -q -p no:cacheprovider tests/test_deploy_notify_wechat.py` → 11 PASS

**Step 5: Commit**

```bash
git add deploy/daily_signals/notify_wechat.py tests/test_deploy_notify_wechat.py
git commit -m "feat(notify): 企业微信推送消息组装 —— 两池置顶、只推护栏担保过的文件"
```

---

### Task 4: 发送（绕代理、重试一次、URL 不外泄）+ webhook 解析

**Files:** Modify `deploy/daily_signals/notify_wechat.py`；Test 追加到 `tests/test_deploy_notify_wechat.py`

**Step 1: Write the failing tests**（追加；本地桩服务器也供 Task 5/6 用）：

```python
import threading
import urllib.error
from http.server import BaseHTTPRequestHandler, HTTPServer


@pytest.fixture
def stub():
    """本地 webhook 桩：记录收到的 JSON；reply 可改成非 0 errcode。"""
    state = {"bodies": [], "reply": {"errcode": 0, "errmsg": "ok"}}

    class Handler(BaseHTTPRequestHandler):
        def do_POST(self):
            n = int(self.headers["Content-Length"])
            state["bodies"].append(json.loads(self.rfile.read(n).decode("utf-8")))
            data = json.dumps(state["reply"]).encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def log_message(self, *args):
            pass

    server = HTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    state["url"] = f"http://127.0.0.1:{server.server_port}/cgi-bin/webhook/send?key=TESTKEY123"
    yield state
    server.shutdown()


def test_send_posts_text_message_and_bypasses_proxy(stub, monkeypatch):
    # 代理指向一个必死端口：若 send_text 走环境代理，这次请求必然失败。
    for var in ("NO_PROXY", "no_proxy"):
        monkeypatch.delenv(var, raising=False)
    monkeypatch.setenv("http_proxy", "http://127.0.0.1:9")
    monkeypatch.setenv("HTTP_PROXY", "http://127.0.0.1:9")
    nw.send_text(stub["url"], "你好\n第二行", wait=0)
    assert stub["bodies"] == [{"msgtype": "text", "text": {"content": "你好\n第二行"}}]


def test_send_raises_on_nonzero_errcode(stub):
    stub["reply"] = {"errcode": 93000, "errmsg": "invalid webhook url"}
    with pytest.raises(nw.SendError, match="93000"):
        nw.send_text(stub["url"], "x", wait=0)
    assert len(stub["bodies"]) == 1          # errcode 是配置问题，不重试


class _FlakyOpener:
    def __init__(self, fail_times, url):
        self.calls, self.fail_times, self.url = 0, fail_times, url

    def open(self, req, timeout):
        self.calls += 1
        if self.calls <= self.fail_times:
            raise urllib.error.URLError(f"boom while calling {self.url}")
        return _Resp(b'{"errcode": 0, "errmsg": "ok"}')


class _Resp:
    def __init__(self, data):
        self.data = data

    def read(self):
        return self.data

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


def test_send_retries_network_error_once():
    url = "https://qyapi.weixin.qq.com/cgi-bin/webhook/send?key=SECRETKEY42"
    opener = _FlakyOpener(1, url)
    nw.send_text(url, "x", wait=0, opener=opener)
    assert opener.calls == 2


def test_send_error_never_leaks_webhook_key():
    url = "https://qyapi.weixin.qq.com/cgi-bin/webhook/send?key=SECRETKEY42"
    opener = _FlakyOpener(5, url)
    with pytest.raises(nw.SendError) as exc:
        nw.send_text(url, "x", wait=0, opener=opener)
    assert opener.calls == 2
    assert "SECRETKEY42" not in str(exc.value) and exc.value.__cause__ is None


def test_webhook_from_env_file_and_env_var_precedence(tmp_path, monkeypatch):
    env = tmp_path / "alert.env"
    env.write_text("# 注释\n\nexport OTHER=1\nALERT_WEBHOOK_URL='https://example.invalid/x?key=abc'\n",
                   encoding="utf-8")
    monkeypatch.delenv("ALERT_WEBHOOK_URL", raising=False)
    assert nw.resolve_webhook(env) == "https://example.invalid/x?key=abc"
    assert nw.resolve_webhook(tmp_path / "missing.env") == ""
    monkeypatch.setenv("ALERT_WEBHOOK_URL", "https://example.invalid/y?key=def")
    assert nw.resolve_webhook(env) == "https://example.invalid/y?key=def"
```

**Step 2: Run** → FAIL（`AttributeError: module 'notify_wechat' has no attribute 'send_text'`）

**Step 3: Implement** —— 在 `notify_wechat.py` 顶部 import 区补 `json, os, time, urllib.error, urllib.parse, urllib.request`，
常量区补下列常量，并在 `compose_message` 之后追加函数：

```python
WEBHOOK_ENV = "ALERT_WEBHOOK_URL"
DEFAULT_ENV_FILE = Path.home() / ".config" / "market-monitor" / "alert.env"
SEND_TIMEOUT = 10.0
RETRY_WAIT = 5.0


class SendError(RuntimeError):
    """企业微信没收下（网络错误重试后仍失败 / errcode≠0 / 回包不是 JSON）。"""


def load_env_file(path: Path) -> dict[str, str]:
    """极简 dotenv：KEY=VALUE、export 前缀、成对引号、# 注释；读不到就当空。"""
    try:
        text = path.read_text(encoding="utf-8")
    except OSError:
        return {}
    out = {}
    for raw in text.splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("export "):
            line = line[len("export "):].lstrip()
        key, sep, value = line.partition("=")
        if not sep:
            continue
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "'\"":
            value = value[1:-1]
        out[key.strip()] = value
    return out


def resolve_webhook(env_file: Path) -> str:
    return os.environ.get(WEBHOOK_ENV) or load_env_file(env_file).get(WEBHOOK_ENV, "")


def _scrub(text: str, url: str) -> str:
    """报错文本里抹掉 URL 与 key——它们会进日志、告警文件和状态文件。"""
    out = text.replace(url, "<webhook>")
    key = urllib.parse.parse_qs(urllib.parse.urlsplit(url).query).get("key", [""])[0]
    return out.replace(key, "<key>") if key else out


def send_text(url: str, text: str, *, timeout: float = SEND_TIMEOUT, retries: int = 1,
              wait: float = RETRY_WAIT, opener=None) -> None:
    """发一条 text 消息。显式绕代理；网络层错误重试 retries 次；errcode≠0 不重试。"""
    body = json.dumps({"msgtype": "text", "text": {"content": text}},
                      ensure_ascii=False).encode("utf-8")
    opener = opener or urllib.request.build_opener(urllib.request.ProxyHandler({}))
    for attempt in range(retries + 1):
        req = urllib.request.Request(url, data=body, headers={"Content-Type": "application/json"})
        try:
            with opener.open(req, timeout=timeout) as resp:
                raw = resp.read()
            break
        except (urllib.error.URLError, TimeoutError, ConnectionError) as exc:
            if attempt >= retries:
                raise SendError(f"网络错误（共试 {retries + 1} 次）：{_scrub(str(exc), url)}") from None
            time.sleep(wait)
    try:
        reply = json.loads(raw.decode("utf-8"))
    except ValueError:
        raise SendError(f"回包不是 JSON：{_scrub(raw[:200].decode('utf-8', 'replace'), url)}") from None
    if reply.get("errcode", 0) != 0:
        raise SendError(_scrub(f"企业微信拒收：errcode={reply.get('errcode')} "
                               f"{reply.get('errmsg')}", url))
```

**Step 4: Run** `$PY -m pytest -q -p no:cacheprovider tests/test_deploy_notify_wechat.py` → 16 PASS

**Step 5: Commit** `git add deploy/daily_signals/notify_wechat.py tests/test_deploy_notify_wechat.py` +
`git commit -m "feat(notify): 发送 —— 绕代理、网络错误重试一次、报错不外泄 webhook key"`

---

### Task 5: CLI 推送模式（状态文件 notify 段、`--dry-run` 不写状态）

**Files:** Modify `deploy/daily_signals/notify_wechat.py`；Test 追加

**Step 1: Write the failing tests**：

```python
def _status_file(tmp_path, status):
    p = tmp_path / "logs" / "daily_signals_status.json"
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(status, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return p


def _argv(tmp_path, status_path, *extra):
    return ["--root", str(tmp_path), "--status-file", str(status_path),
            "--env-file", str(tmp_path / "no-such.env"), "--today", "2026-09-22", *extra]


def test_dry_run_prints_sends_nothing_and_leaves_status_alone(tmp_path, monkeypatch, capsys):
    monkeypatch.delenv("ALERT_WEBHOOK_URL", raising=False)
    p = _status_file(tmp_path, make_tree(tmp_path))
    before = p.read_bytes()
    assert nw.main(_argv(tmp_path, p, "--dry-run")) == 0
    assert "风格择时 信号日 2026-09-22｜链路 OK" in capsys.readouterr().out
    assert p.read_bytes() == before


def test_push_sends_and_records_notify_block(tmp_path, monkeypatch, stub):
    monkeypatch.setenv("ALERT_WEBHOOK_URL", stub["url"])
    p = _status_file(tmp_path, make_tree(tmp_path))
    assert nw.main(_argv(tmp_path, p)) == 0
    assert stub["bodies"][0]["text"]["content"].startswith("风格择时 信号日 2026-09-22")
    st = json.loads(p.read_text(encoding="utf-8"))
    assert st["notify"]["sent"] is True and st["notify"]["as_of"] == "2026-09-22"
    assert st["notify"]["error"] is None and st["result"] == "OK" and "files" in st


def test_push_without_webhook_fails_and_records(tmp_path, monkeypatch):
    monkeypatch.delenv("ALERT_WEBHOOK_URL", raising=False)
    p = _status_file(tmp_path, make_tree(tmp_path))
    assert nw.main(_argv(tmp_path, p)) == 1
    notify = json.loads(p.read_text(encoding="utf-8"))["notify"]
    assert notify["sent"] is False and "ALERT_WEBHOOK_URL" in notify["error"]


def test_push_rejected_by_wechat_records_error(tmp_path, monkeypatch, stub):
    stub["reply"] = {"errcode": 45009, "errmsg": "api freq out of limit"}
    monkeypatch.setenv("ALERT_WEBHOOK_URL", stub["url"])
    p = _status_file(tmp_path, make_tree(tmp_path))
    assert nw.main(_argv(tmp_path, p)) == 1
    notify = json.loads(p.read_text(encoding="utf-8"))["notify"]
    assert notify["sent"] is False and "45009" in notify["error"]
    assert "TESTKEY123" not in json.dumps(notify)


def test_push_refused_when_guard_not_ok(tmp_path, monkeypatch, stub, capsys):
    monkeypatch.setenv("ALERT_WEBHOOK_URL", stub["url"])
    st = make_tree(tmp_path)
    st["result"] = "STALE"
    p = _status_file(tmp_path, st)
    assert nw.main(_argv(tmp_path, p)) == 1
    assert stub["bodies"] == [] and "REFUSED" in capsys.readouterr().err
```

**Step 2: Run** → FAIL（`AttributeError: ... 'main'`）

**Step 3: Implement** —— import 区补 `argparse` 与 `from datetime import date, datetime`，常量区补
`DEFAULT_STATUS = ROOT / "logs" / "daily_signals_status.json"`，文件末尾追加：

```python
def load_status(path: Path) -> dict:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise Refused(f"读不了状态文件 {path}：{type(exc).__name__}") from None


def record_notify(status_path: Path, notify: dict) -> None:
    """把推送结果写回状态文件的 notify 段：重读再写 + 原子替换，其他字段原样保留。"""
    status = load_status(status_path)
    status["notify"] = notify
    tmp = status_path.with_name(status_path.name + ".tmp")
    tmp.write_text(json.dumps(status, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    os.replace(tmp, status_path)


def run_push(args) -> int:
    status_path = Path(args.status_file)
    status = load_status(status_path)
    text, as_of = compose_message(Path(args.root), status,
                                  today=args.today or date.today().isoformat())
    print(text)
    if args.dry_run:
        print("（--dry-run：未发送，未写状态文件）")
        return 0
    notify = {"sent": False, "at": datetime.now().astimezone().isoformat(timespec="seconds"),
              "as_of": as_of, "bytes": len(text.encode("utf-8")), "error": None}
    url = resolve_webhook(Path(args.env_file))
    try:
        if not url:
            raise Refused(f"没有 {WEBHOOK_ENV}（环境变量或 {args.env_file}），拒绝静默；"
                          f"预览用 --dry-run")
        send_text(url, text)
        notify["sent"] = True
    except (Refused, SendError) as exc:
        notify["error"] = str(exc)
        raise
    finally:
        record_notify(status_path, notify)
    print(f"已推送企业微信（信号日 {as_of}，{notify['bytes']} 字节）")
    return 0


def build_parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(description="日更信号链企业微信推送")
    ap.add_argument("--status-file", default=str(DEFAULT_STATUS), help="状态 JSON（护栏写的那份）")
    ap.add_argument("--root", default=str(ROOT), help="数据根目录，默认仓库根（测试/演示可指向副本）")
    ap.add_argument("--env-file", default=str(DEFAULT_ENV_FILE),
                    help=f"{WEBHOOK_ENV} 所在 env 文件（环境变量优先）")
    ap.add_argument("--dry-run", action="store_true", help="只打印消息：不发送、不写状态文件")
    ap.add_argument("--alert", action="store_true", help="失败通知模式（由 alert_on_failure.sh 调用）")
    ap.add_argument("--systemd-result", default="", help="--alert 用：主 service 的 systemd Result")
    ap.add_argument("--today", default=None, help="YYYY-MM-DD，默认今天（测试用）")
    return ap


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        return run_push(args)
    except (Refused, SendError) as exc:
        print(f"REFUSED: {exc}" if isinstance(exc, Refused) else f"SEND_FAILED: {exc}",
              file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
```

**Step 4: Run** → 21 PASS

**Step 5: Commit** `git add ...` 两个文件 + `git commit -m "feat(notify): CLI 推送模式 —— notify 段回写状态文件，--dry-run 不写"`

---

### Task 6: `--alert` 失败通知模式（不 import pandas）

**Files:** Modify `deploy/daily_signals/notify_wechat.py`；Test 追加

**Step 1: Write the failing tests**：

```python
import subprocess

NOW = "2026-09-23 18:31:07"


def test_alert_for_failed_step():
    st = {"result": "FAILED", "failed_step": "topup_audit(SUSPECT)",
          "finished_at": "2026-09-23T18:31:05+08:00", "topup": "SUSPECT",
          "topup_reason": "事后审计判定写入可疑（exit 1）"}
    lines = nw.build_alert(st, systemd_result="exit-code", now=NOW).split("\n")
    assert lines[0] == "⚠ 风格择时日更链失败｜2026-09-23 18:31:07"
    assert lines[1] == "结果 FAILED · 失败步骤 topup_audit(SUSPECT) · 状态写于 2026-09-23T18:31:05+08:00"
    assert "topup SUSPECT：事后审计判定写入可疑（exit 1）" in lines
    assert "systemd Result=exit-code" in lines
    assert lines[-1] == "持仓未更新/未送达，以上一次推送为准；处置见 logs/ALERT_daily_signals"


def test_alert_for_stale_lists_first_three_breaches():
    st = {"result": "STALE", "finished_at": "2026-09-23T18:31:05+08:00", "topup": "OK",
          "breaches": [f"b{i}" for i in range(5)]}
    lines = nw.build_alert(st, systemd_result="", now=NOW).split("\n")
    assert [l for l in lines if l.startswith("护栏：")] == ["护栏：b0", "护栏：b1", "护栏：b2"]
    assert not any(l.startswith("systemd") for l in lines)


def test_alert_when_status_missing():
    text = nw.build_alert(None, systemd_result="timeout", now=NOW)
    assert "状态文件缺失或无法解析" in text and "systemd Result=timeout" in text


def test_alert_when_status_ok_but_unit_failed():
    st = {"result": "OK", "finished_at": "2026-09-23T18:31:05+08:00", "topup": "OK"}
    text = nw.build_alert(st, systemd_result="timeout", now=NOW)
    assert "状态文件没记下失败原因" in text


def test_alert_reports_notify_error_instead():
    st = {"result": "OK", "finished_at": "2026-09-23T18:31:05+08:00", "topup": "OK",
          "notify": {"sent": False, "error": "网络错误（共试 2 次）：timed out"}}
    text = nw.build_alert(st, systemd_result="exit-code", now=NOW)
    assert "推送失败：网络错误（共试 2 次）：timed out" in text
    assert "状态文件没记下失败原因" not in text


def test_alert_cli_sends(tmp_path, monkeypatch, stub):
    monkeypatch.setenv("ALERT_WEBHOOK_URL", stub["url"])
    p = _status_file(tmp_path, {"result": "STALE", "finished_at": "x", "topup": "OK",
                                "breaches": ["recommended_slope20 落后 2 交易日"]})
    assert nw.main(["--alert", "--status-file", str(p), "--env-file", str(tmp_path / "x.env"),
                    "--systemd-result", "exit-code"]) == 0
    assert "护栏：recommended_slope20 落后 2 交易日" in stub["bodies"][0]["text"]["content"]


def test_alert_mode_never_imports_pandas(tmp_path):
    """科学栈坏了也得能报警：把 pandas 设成不可导入，--alert 仍须跑通。"""
    p = _status_file(tmp_path, {"result": "FAILED", "failed_step": "citic40d"})
    code = ("import sys, runpy; sys.modules['pandas'] = None; "
            f"sys.argv = ['notify_wechat.py', '--alert', '--dry-run', '--status-file', {str(p)!r}]; "
            f"runpy.run_path({str(NOTIFY_PATH)!r}, run_name='__main__')")
    proc = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True)
    assert proc.returncode == 0, proc.stderr
    assert "失败步骤 citic40d" in proc.stdout
```

**Step 2: Run** → FAIL（`AttributeError: ... 'build_alert'`）

**Step 3: Implement** —— 在 `run_push` 之前追加：

```python
ALERT_FILE_NOTE = "处置见 logs/ALERT_daily_signals"


def build_alert(status: dict | None, *, systemd_result: str, now: str) -> str:
    """失败通知：只用状态文件里的字段，不碰产出文件、不 import pandas。"""
    lines = [f"⚠ 风格择时日更链失败｜{now}"]
    if status is None:
        lines.append("状态文件缺失或无法解析——链路可能在写状态之前就死了")
    else:
        lines.append(f"结果 {status.get('result')} · 失败步骤 {status.get('failed_step') or '—'}"
                     f" · 状态写于 {status.get('finished_at')}")
        if status.get("topup") not in (None, "OK"):
            lines.append(f"topup {status.get('topup')}：{status.get('topup_reason') or '未记原因'}")
        lines += [f"护栏：{b}" for b in (status.get("breaches") or [])[:3]]
        if status.get("upstream_breach"):
            lines.append(f"上游：{status['upstream_breach']}")
        if status.get("error"):
            lines.append(f"检查出错：{status['error']}")
        notify_error = (status.get("notify") or {}).get("error")
        if notify_error:
            lines.append(f"推送失败：{notify_error}")
        elif status.get("result") == "OK":
            lines.append("状态文件没记下失败原因——可能在写状态前就被杀了（看 systemd Result）")
    if systemd_result:
        lines.append(f"systemd Result={systemd_result}")
    lines.append(f"持仓未更新/未送达，以上一次推送为准；{ALERT_FILE_NOTE}")
    return truncate_text("\n".join(lines), limit=WECHAT_TEXT_LIMIT, note=ALERT_FILE_NOTE)


def run_alert(args) -> int:
    try:
        status = json.loads(Path(args.status_file).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        status = None
    text = build_alert(status, systemd_result=args.systemd_result,
                       now=datetime.now().strftime("%Y-%m-%d %H:%M:%S"))
    print(text)
    if args.dry_run:
        return 0
    url = resolve_webhook(Path(args.env_file))
    if not url:
        raise Refused(f"没有 {WEBHOOK_ENV}，失败通知只进告警文件")
    send_text(url, text)
    print("已推送失败通知")
    return 0
```

并把 `main` 里的 `return run_push(args)` 改为 `return run_alert(args) if args.alert else run_push(args)`。
（`collect_lines` 里对 `backtest.*` 的 import 已在函数体内，`--alert` 路径不会触发。）

**Step 4: Run** → 28 PASS

**Step 5: Commit** `git add ...` 两个文件 + `git commit -m "feat(notify): --alert 失败通知模式 —— 只读状态文件、不依赖 pandas"`

---

### Task 7: 接入 runner、告警器、service

**Files:**
- Modify: `deploy/daily_signals/run_daily_signals.sh`（头注释 + 末尾）
- Modify: `deploy/daily_signals/alert_on_failure.sh`
- Modify: `deploy/daily_signals/style-signals-daily.service`

**Step 1: runner** —— 头注释「链路」列表在 `7. deploy/daily_signals/check_freshness.py` 行下加
`#   8. deploy/daily_signals/notify_wechat.py      —— 企业微信推送（护栏通过才推；失败 → 非零退出）`；
「步骤 1-7 任一失败」改「步骤 1-8」；环境变量段加
`#   STYLE_SIGNALS_NOTIFY_ARGS 透传给 notify_wechat.py（如 --dry-run：只打印不发）`。
把文件末尾从 `TOTAL=$((SECONDS - START_TS))` 起的内容替换为：

```bash
if [[ ${guard_rc} -ne 0 ]]; then
  log "STALE/CHECK_ERROR: 新鲜度护栏未通过（exit ${guard_rc}）——见上方 STALE 行与 ${STATUS_FILE}"
  log "════════ 日更信号链结束：失败，总耗时 $((SECONDS - START_TS))s ════════"
  exit "${guard_rc}"
fi
log "✔ freshness_guard 通过，用时 ${guard_dt}s"

# ── 步骤 8：企业微信推送（护栏通过才推；推送失败 → 非零退出，交 OnFailure 告警器）──────
log "▶ notify_wechat: notify_wechat.py ${STYLE_SIGNALS_NOTIFY_ARGS:-}"
notify_rc=0
# shellcheck disable=SC2086
"${PYTHON}" "${SCRIPT_DIR}/notify_wechat.py" --status-file "${STATUS_FILE}" \
    ${STYLE_SIGNALS_NOTIFY_ARGS:-} || notify_rc=$?
if [[ ${notify_rc} -ne 0 ]]; then
  log "NOTIFY_FAILED: 企业微信推送失败（exit ${notify_rc}）——信号与护栏均已完成，只是没送达；见 ${STATUS_FILE} 的 notify 段"
  log "════════ 日更信号链结束：推送失败，总耗时 $((SECONDS - START_TS))s ════════"
  exit 1
fi
log "════════ 日更信号链结束：成功，总耗时 $((SECONDS - START_TS))s ════════"
```

**Step 2: 告警器** —— `alert_on_failure.sh`：头注释动作列表改为 1 写告警文件 / 2 best-effort 企业微信失败通知 /
3 best-effort 桌面通知；在 `# 桌面通知` 段之前插入：

```bash
# 企业微信失败通知：best-effort。拼消息与发送都在 notify_wechat.py --alert（只读状态文件、
# 不 import pandas）；输出追加进告警文件，留底推了什么。
PYTHON="${STYLE_SIGNALS_PYTHON:-}"
if [[ -z "${PYTHON}" ]]; then
  for cand in "${REPO}/.venv/bin/python3" /home/elfbob/miniconda3/bin/python3; do
    [[ -x "${cand}" ]] && { PYTHON="${cand}"; break; }
  done
fi
SYSTEMD_RESULT="$(systemctl --user show style-signals-daily.service -p Result --value 2>/dev/null || true)"
{
  echo
  echo "[企业微信失败通知]"
  timeout 30 "${PYTHON:-python3}" "${SCRIPT_DIR}/notify_wechat.py" --alert \
      --status-file "${STATUS_FILE}" --systemd-result "${SYSTEMD_RESULT}" \
      || echo "  (未送达，exit $?)"
} >> "${ALERT_FILE}" 2>&1
```

**Step 3: service** —— `style-signals-daily.service` 的 `Nice=5` 之后加：

```ini
# 75 = runner 的 flock 跳过（已有实例在跑，那个实例会推送），不是失败——别触发 OnFailure 假告警。
SuccessExitStatus=75
```

**Step 4: 验证**
- `bash -n deploy/daily_signals/run_daily_signals.sh && bash -n deploy/daily_signals/alert_on_failure.sh` → 无输出
- `systemd-analyze --user verify deploy/daily_signals/style-signals-daily.service` → 无 `SuccessExitStatus` 相关报错
- **沙箱端到端 —— 由控制器亲自执行，实现子代理不要跑 runner**（runner 的 topup 会写共享表
  `index_daily`；只在本 worktree、标志文件 + 环境变量双保险跳过 topup、推送只干跑）：
  1. `W=/home/elfbob/claude-code/style_timing_signal/.worktrees/wechat-signal-push`；
     `ln -s /home/elfbob/claude-code/style_timing_signal/config/settings.yaml $W/config/settings.yaml`（gitignored；跑完删）；
     `echo "沙箱端到端，禁止写库" > $W/deploy/daily_signals/SKIP_TOPUP`
  2. `STYLE_SIGNALS_SKIP_TOPUP=1 STYLE_SIGNALS_NOTIFY_ARGS=--dry-run $W/deploy/daily_signals/run_daily_signals.sh`
     → 日志含 `TOPUP_SKIPPED`、消息正文、`（--dry-run：未发送，未写状态文件）`、`日更信号链结束：成功`
  3. 告警器干跑：`env -u ALERT_WEBHOOK_URL $PY $W/deploy/daily_signals/notify_wechat.py --alert --dry-run --status-file $W/logs/daily_signals_status.json --systemd-result exit-code`
  4. 清理：`rm $W/config/settings.yaml $W/deploy/daily_signals/SKIP_TOPUP && git -C $W checkout -- output/ && rm -rf $W/logs`
     （`git -C $W status` 只剩本任务改动）

**Step 5: Commit**

```bash
git add deploy/daily_signals/run_daily_signals.sh deploy/daily_signals/alert_on_failure.sh deploy/daily_signals/style-signals-daily.service
git commit -m "feat(daily): 链路步骤 8 企业微信推送 + 告警器推失败通知 + 锁冲突 75 不算失败"
```

---

### Task 8: README

**Files:** Modify `deploy/daily_signals/README.md`

- 「文件」表加 `notify_wechat.py` 一行；「链路」加步骤 8；「产物」表 `daily_signals_status.json` 说明补「`notify` 段：推送结果」。
- 新增一节 `## 企业微信推送`（放在「产物」之前）：何时推（护栏通过后约 18:31）、推什么（两池置顶 / 其余生产线参考 /
  体检行；贴设计文档 §2.3 的样例）、webhook 从哪来（`~/.config/market-monitor/alert.env`，600，仓外，
  与 bs-toolkit/数据管理办公室同一机器人）、只推护栏担保过的文件、失败形态表（照设计文档 §4）、
  预览 `python3 deploy/daily_signals/notify_wechat.py --dry-run`、补发 `python3 deploy/daily_signals/notify_wechat.py`。
- 「手动操作」的环境变量表加 `STYLE_SIGNALS_NOTIFY_ARGS`；「安装」段注明改了 service 要重新 `cp` + `daemon-reload`。

Commit：`git add deploy/daily_signals/README.md && git commit -m "docs(daily): README 补企业微信推送一节"`

---

### Task 9: 总验证

1. `$PY -m pytest -q -p no:cacheprovider tests/test_deploy_notify_wechat.py tests/test_deploy_freshness_guard.py tests/test_bt_production.py tests/test_deploy_topup_guard.py tests/test_deploy_family_sentinel.py tests/test_deploy_failover_legs.py` → 全 PASS
2. `$PY -m pytest -q -p no:cacheprovider --collect-only tests/ 2>&1 | tail -3` → 无 collection error
3. 变异验证（每条改坏→跑→确认变红→还原）：`check_vouched` 去掉 gated 过滤；`send_text` 改用默认 opener；
   `_scrub` 直接返回原文；`describe` 翻仓判断改成 `run_days == 1`；`run_push` 的 dry-run 分支挪到 `record_notify` 之后。
