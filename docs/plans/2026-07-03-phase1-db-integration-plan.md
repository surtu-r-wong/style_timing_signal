# Phase 1：指数数据入库 + 三条信号线读库改造 — 实施计划

> **For Claude:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task.

> **执行后记（2026-07-06 落地）**：Task 1–4 / 7–9 按计划完成提交。Task 5（wsd 回填）因 Wind 会话层报 **-103（终端会话挂掉，非额度）** 受阻 → 改用**磁盘现成 Wind 导出 CSV 直接 seed PG**（13 条已确认指数，走 `stock_selector.write_index_daily` 幂等 upsert；wsd 恢复后同值覆盖，再补数据源级校验 + 日更 topup）。Task 10 默认源翻转已落地：**三线全默认 `pg`**，逐字节复现通过、27 测试过。**两处偏离原计划**：① equal_weight 未按原 5/6pairs 复现，而是按用户 7-06 决定**去掉创业板/科创两对、收敛为四对**（新 `config_4pairs`，输出值改变、变体A 起点回到 2014）；② 15 条中 `932000`/沪深300 历史段未 seed（信号线不用，延后）。详见 commit `6d32f76` / `dcb291f`。

**Goal:** 把三条信号线的输入数据从人工维护的 CSV 切到 market_monitor PG（`stock_selector.index_daily`），信号输出数值不变。

**Architecture:** 数据入库复用 stock_selector 现成基础设施（Wind gateway + `backfill.cli date-range --table index_daily`，断点续跑、upsert 幂等）；style_timing_signal 侧新增薄读取层 `signals/common/data_source.py`（psycopg2 只读 + 中文名↔代码映射），四个信号脚本加 `--source {csv,pg}`，先默认 csv 验证零差异后翻转默认为 pg。CSV 降级为备份/复现口径。

**Tech Stack:** Python 3.13（系统 python3，psycopg2 2.9.11 / pandas 3.0.1 / yaml 已装）、PostgreSQL（Debian `100.65.111.79`，只读）、pytest。

**设计依据：** `docs/plans/2026-07-03-optimization-roadmap-design.md` 第 4 节（方向三）。

**关键背景（执行者须知）：**

- 本仓库四个信号脚本以 `python3 signals/<dir>/<script>.py` 从**仓库根**运行；脚本用 `ROOT = Path(__file__).resolve().parents[2]` 锚定仓库根。直接按路径运行脚本时 `sys.path[0]` 是脚本目录而非仓库根，所以 PG 分支导入公共模块前要 `sys.path.insert(0, str(ROOT))`。
- 上次重组（2026-07-02）的纪律是**字节级输出一致**验证，本计划延续：PG 模式传 `--start <CSV首日>` 时输出必须与 CSV 模式逐字节相同。
- rolling 窗口是有限窗口（最长 250d），只要两种数据源在窗口内数值相同，起点之后 `lookback+z_window` 天起信号与数据起点无关——所以严格复现要求 PG 取数起点 = CSV 首日。
- `stock_selector.index_daily` PK `(index_code, trade_date)`，upsert 幂等，重复回填安全。
- Wind gateway（Windows `100.120.152.1:8080`）必须在线才能执行 Task 5；style_timing_signal 本身**永不**直连 gateway，只读 PG。

---

## Task 0（用户侧，与 Task 1–7 并行，阻塞 Task 9 的变体A全列验证）：核对创业板/科创 4 条指数代码

现有 `data/成长价值指数_2019.csv` 含 12 列，其中创业板成长/价值、科创成长/价值的 Wind 代码未确认（不在用户 7-02 `映射.xlsx` 的 15 对里）。

**用户操作**：Wind 终端查"创业板成长/创业板价值/科创成长(或科创板成长)/科创价值"候选代码，拉 2026-06 整月收盘价与 CSV 对应列逐日比对，数值完全一致即锁定。把 4 条代码告知执行者（或直接追加到 `signals/common/index_codes.csv`，格式见 Task 3）。

**若暂缓**：Task 9 的验证降级为"变体B 5 对中已确认的 4 对 + 变体A 6 对中已确认的 4 对"子集口径，全列验证遗留待办。

---

## Task 1：提交设计文档与本计划

**Files:** 无新建（提交已有两份文档）

**Step 1: 确认工作区状态**

Run: `cd /home/elfbob/claude-code/style_timing_signal && git status --short`
Expected: 仅 `docs/plans/2026-07-03-optimization-roadmap-design.md` 与 `docs/plans/2026-07-03-phase1-db-integration-plan.md` 两个未跟踪文件（如有 `.pytest_cache` 等噪声不提交）。

**Step 2: Commit**

```bash
git add docs/plans/2026-07-03-optimization-roadmap-design.md docs/plans/2026-07-03-phase1-db-integration-plan.md
git commit -m "docs: 三方向优化设计稿(v4) + Phase1 数据库直连实施计划"
```

---

## Task 2：配置层（settings.yaml 模板 + 加载器）

**Files:**
- Create: `config/settings.example.yaml`
- Create: `signals/common/config.py`
- Create: `tests/test_common_config.py`
- Modify: `.gitignore`（追加一行）

**Step 1: 写失败测试**

`tests/test_common_config.py`：

```python
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from signals.common.config import load_db_config  # noqa: E402


def test_load_db_config_reads_yaml(tmp_path):
    f = tmp_path / "settings.yaml"
    f.write_text(
        "database:\n  host: 1.2.3.4\n  port: 5432\n  name: market_monitor\n"
        "  user: admin\n  password: x\n  schema: stock_selector\n",
        encoding="utf-8",
    )
    db = load_db_config(f)
    assert db["host"] == "1.2.3.4"
    assert db["schema"] == "stock_selector"


def test_load_db_config_missing_file_raises(tmp_path):
    with pytest.raises(FileNotFoundError, match="settings.example.yaml"):
        load_db_config(tmp_path / "nope.yaml")


def test_load_db_config_missing_key_raises(tmp_path):
    f = tmp_path / "settings.yaml"
    f.write_text("database:\n  host: 1.2.3.4\n", encoding="utf-8")
    with pytest.raises(ValueError, match="port"):
        load_db_config(f)
```

**Step 2: 跑测试确认失败**

Run: `cd /home/elfbob/claude-code/style_timing_signal && python3 -m pytest tests/test_common_config.py -q`
Expected: FAIL（`ModuleNotFoundError: signals.common`）

**Step 3: 最小实现**

`signals/common/config.py`：

```python
"""读取 config/settings.yaml 的数据库连接配置（gitignored，模板见 settings.example.yaml）。"""
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[2]
CONFIG_FILE = ROOT / "config" / "settings.yaml"
_REQUIRED = ("host", "port", "name", "user", "password", "schema")


def load_db_config(config_file: str | Path = CONFIG_FILE) -> dict:
    config_file = Path(config_file)
    if not config_file.exists():
        raise FileNotFoundError(
            f"{config_file} 不存在：复制 config/settings.example.yaml 为 "
            f"config/settings.yaml 并填入数据库连接信息"
        )
    cfg = yaml.safe_load(config_file.read_text(encoding="utf-8")) or {}
    db = cfg.get("database") or {}
    missing = [k for k in _REQUIRED if k not in db]
    if missing:
        raise ValueError(f"settings.yaml database 段缺少字段: {missing}")
    return db
```

`config/settings.example.yaml`：

```yaml
# 复制为 config/settings.yaml（gitignored）并填入真实值。
# 与 stock_selector/config/settings.yaml 的 database 段同库同凭据，schema 固定 stock_selector。
database:
  host: 100.65.111.79
  port: 5432
  name: market_monitor
  user: admin
  password: replace_me
  schema: stock_selector
```

`.gitignore` 追加：

```
config/settings.yaml
```

**Step 4: 跑测试确认通过**

Run: `python3 -m pytest tests/test_common_config.py -q`
Expected: 3 passed

**Step 5: 手工创建真实配置（不入库）**

从 `/home/elfbob/claude-code/stock_selector/config/settings.yaml` 的 database 段抄 host/port/name/user/password，写入 `config/settings.yaml`，schema 填 `stock_selector`。

Run: `python3 -c "import sys; sys.path.insert(0,'.'); from signals.common.config import load_db_config; print(load_db_config()['host'])"`
Expected: `100.65.111.79`

**Step 6: Commit**

```bash
git add config/settings.example.yaml signals/common/config.py tests/test_common_config.py .gitignore
git commit -m "feat(common): 数据库配置层（settings.example + 加载器 + 测试）"
```

---

## Task 3：中文名 ↔ Wind 代码映射表

**Files:**
- Create: `signals/common/index_codes.csv`
- Create: `tests/test_index_codes.py`
- Modify: `signals/common/data_source.py`（本 task 只建映射加载函数，文件在此新建）

**Step 1: 写失败测试**

`tests/test_index_codes.py`：

```python
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from signals.common.data_source import load_code_map  # noqa: E402


def test_code_map_contains_citic_and_gv_pairs():
    m = load_code_map()
    assert m["稳定"] == "CI005921.WI"
    assert m["成长"] == "CI005920.WI"
    assert m["中证500成长"] == "H30351.CSI"
    assert m["中证1000价值"] == "932406.CSI"
    assert m["沪深300"] == "000300.SH"


def test_code_map_rejects_duplicate_names(tmp_path):
    f = tmp_path / "codes.csv"
    f.write_text("name,code\nA,X1\nA,X2\n", encoding="utf-8")
    with pytest.raises(ValueError, match="重复"):
        load_code_map(f)
```

**Step 2: 跑测试确认失败**

Run: `python3 -m pytest tests/test_index_codes.py -q`
Expected: FAIL（no module `signals.common.data_source`）

**Step 3: 建映射表与加载函数**

`signals/common/index_codes.csv`（代码来源：中信五风格 = `data/README.md`+成分文件后缀；成长/价值对 = 用户 2026-07-02 `映射.xlsx`；终验以 Task 6 数值比对为准）：

```csv
name,code
稳定,CI005921.WI
成长,CI005920.WI
金融,CI005917.WI
周期,CI005918.WI
消费,CI005919.WI
沪深300,000300.SH
中证2000,932000.CSI
沪深300成长,000918.CSI
沪深300价值,000919.CSI
中证500成长,H30351.CSI
中证500价值,H30352.CSI
中证1000成长,932407.CSI
中证1000价值,932406.CSI
中证2000成长,932409.CSI
中证2000价值,932408.CSI
```

（Task 0 完成后由用户/执行者追加 `创业板成长/创业板价值/科创成长/科创价值` 4 行。）

`signals/common/data_source.py`（第一部分）：

```python
"""PG 读取层：从 stock_selector.index_daily 读指数收盘价，输出中文列名宽表。

中文名 ↔ Wind 代码映射在同目录 index_codes.csv；连接配置在 config/settings.yaml。
"""
from pathlib import Path

import pandas as pd

from signals.common.config import load_db_config

CODES_FILE = Path(__file__).resolve().parent / "index_codes.csv"


def load_code_map(codes_file: str | Path = CODES_FILE) -> dict[str, str]:
    df = pd.read_csv(codes_file, encoding="utf-8-sig")
    dup = sorted(df.loc[df["name"].duplicated(), "name"].unique())
    if dup:
        raise ValueError(f"index_codes.csv 存在重复 name: {dup}")
    return dict(zip(df["name"].str.strip(), df["code"].str.strip()))
```

**Step 4: 跑测试确认通过**

Run: `python3 -m pytest tests/test_index_codes.py -q`
Expected: 2 passed

**Step 5: Commit**

```bash
git add signals/common/index_codes.csv signals/common/data_source.py tests/test_index_codes.py
git commit -m "feat(common): 指数中文名↔Wind代码映射表与加载函数"
```

---

## Task 4：data_source 整形函数与 PG 读取

**Files:**
- Modify: `signals/common/data_source.py`（追加两个函数）
- Create: `tests/test_data_source.py`

**Step 1: 写失败测试（纯函数，不碰 PG）**

`tests/test_data_source.py`：

```python
import sys
from datetime import date
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from signals.common.data_source import rows_to_frame  # noqa: E402


def _rows():
    return [
        ("C1", date(2026, 1, 6), 110.0),
        ("C1", date(2026, 1, 5), 100.0),
        ("C2", date(2026, 1, 5), 200.0),
        ("C2", date(2026, 1, 6), 220.0),
    ]


def test_rows_to_frame_shapes_wide_chinese_columns():
    df = rows_to_frame(_rows(), {"C1": "稳定", "C2": "成长"})
    assert list(df.columns) == ["稳定", "成长"]
    assert df.index.is_monotonic_increasing
    assert df.loc["2026-01-05", "稳定"] == 100.0
    assert df["成长"].dtype == float


def test_rows_to_frame_missing_code_raises():
    with pytest.raises(ValueError, match="C9"):
        rows_to_frame(_rows(), {"C1": "稳定", "C2": "成长", "C9": "金融"})
```

**Step 2: 跑测试确认失败**

Run: `python3 -m pytest tests/test_data_source.py -q`
Expected: FAIL（`rows_to_frame` 不存在）

**Step 3: 实现 `rows_to_frame` 与 `load_pg_closes`**

在 `signals/common/data_source.py` 追加：

```python
def rows_to_frame(rows, name_by_code: dict[str, str]) -> pd.DataFrame:
    """(index_code, trade_date, close) 行集 → date 索引 × 中文列名宽表。

    列顺序跟随 name_by_code 的插入顺序；任一代码零行则报错列出。
    """
    df = pd.DataFrame(rows, columns=["index_code", "trade_date", "close"])
    missing = [c for c in name_by_code if df.empty or c not in set(df["index_code"])]
    if missing:
        raise ValueError(f"index_daily 中以下代码无数据: {missing}")
    wide = df.pivot(index="trade_date", columns="index_code", values="close")
    wide.index = pd.to_datetime(wide.index)
    wide = wide.sort_index()
    wide = wide[[c for c in name_by_code]].rename(columns=name_by_code)
    wide.index.name = "date"
    return wide.astype(float)


def load_pg_closes(names: list[str], start=None, end=None, db: dict | None = None) -> pd.DataFrame:
    """按中文名列表读收盘价宽表。start/end 为 'YYYY-MM-DD' 或 None（全历史）。"""
    import psycopg2

    code_map = load_code_map()
    unknown = [n for n in names if n not in code_map]
    if unknown:
        raise KeyError(f"index_codes.csv 缺少映射: {unknown}")
    codes = [code_map[n] for n in names]

    db = db or load_db_config()
    conn = psycopg2.connect(
        host=db["host"], port=db["port"], dbname=db["name"],
        user=db["user"], password=db["password"],
        connect_timeout=10,
    )
    try:
        with conn.cursor() as cur:
            cur.execute(
                f"""SELECT index_code, trade_date, close
                    FROM {db["schema"]}.index_daily
                    WHERE index_code = ANY(%s)
                      AND (%s::date IS NULL OR trade_date >= %s::date)
                      AND (%s::date IS NULL OR trade_date <= %s::date)""",
                (codes, start, start, end, end),
            )
            rows = cur.fetchall()
    finally:
        conn.close()
    return rows_to_frame(rows, dict(zip(codes, names)))
```

**Step 4: 跑测试确认通过**

Run: `python3 -m pytest tests/test_data_source.py tests/ -q`
Expected: 新增 2 passed，原有测试全过

**Step 5: Commit**

```bash
git add signals/common/data_source.py tests/test_data_source.py
git commit -m "feat(common): PG 读取层 rows_to_frame + load_pg_closes"
```

---

## Task 5：回填 15 条已确认指数（执行任务，须 gateway 在线）

**Files:** 无代码变更（stock_selector 侧执行）

**Step 1: 前置检查**

Run: `curl -s --max-time 5 http://100.120.152.1:8080/health`
Expected: 返回 JSON 状态（gateway 在线）。离线则联系用户在 Windows 侧启动，本 task 挂起。

**Step 2: 回填中信风格 5 条（2010 起）**

```bash
cd /home/elfbob/claude-code/stock_selector
.venv/bin/python -m stock_selector.backfill.cli date-range \
  --table index_daily \
  --tickers "CI005917.WI,CI005918.WI,CI005919.WI,CI005920.WI,CI005921.WI" \
  --start 2010-01-01 --end $(date +%F) --source wind
```

Expected: 正常完成（15 天/chunk 断点续跑，中断重跑同命令自动续）。
风险预案：若 CI 代码 wsd 全返 null/报错，`wind_source.fetch_index_daily` 已内置 `/fetch/price` 回退；仍失败则在 Wind 终端确认 CI 后缀（.WI）并更新 index_codes.csv 与命令重试。

**Step 3: 回填成长/价值 8 条 + 中证2000（2013-12 起）**

```bash
.venv/bin/python -m stock_selector.backfill.cli date-range \
  --table index_daily \
  --tickers "000918.CSI,000919.CSI,H30351.CSI,H30352.CSI,932406.CSI,932407.CSI,932408.CSI,932409.CSI,932000.CSI" \
  --start 2013-12-01 --end $(date +%F) --source wind
```

**Step 4: 补沪深300 历史段（2010-01-01 ~ 2021-01-31，与库内存量重叠部分 upsert 幂等）**

```bash
.venv/bin/python -m stock_selector.backfill.cli date-range \
  --table index_daily --tickers "000300.SH" \
  --start 2010-01-01 --end 2021-01-31 --source wind
```

**Step 5: 验收查询**

```bash
.venv/bin/python - <<'EOF'
import yaml, psycopg2
db = yaml.safe_load(open('config/settings.yaml'))['database']
conn = psycopg2.connect(host=db['host'], port=db['port'], dbname=db['name'], user=db['user'], password=db['password'])
cur = conn.cursor()
codes = ['CI005917.WI','CI005918.WI','CI005919.WI','CI005920.WI','CI005921.WI',
         '000918.CSI','000919.CSI','H30351.CSI','H30352.CSI','932406.CSI','932407.CSI',
         '932408.CSI','932409.CSI','932000.CSI','000300.SH']
cur.execute("""SELECT index_code, count(*), min(trade_date), max(trade_date)
               FROM stock_selector.index_daily WHERE index_code = ANY(%s)
               GROUP BY 1 ORDER BY 1""", (codes,))
for r in cur.fetchall(): print(r)
conn.close()
EOF
```

Expected（验收标准）：
- CI 5 条：min ≈ 2010-01-04，行数 ~3,900+，max = 最近交易日
- 000918/000919/H30351/H30352：min ≈ 2013-12-02
- 932406–932409：min 不晚于 2014-01-02（932 系基日 2012/2013-12-31，历史回溯值应存在；若 min 晚于 2014-01-02，记录并在 Task 6 diff 时评估影响）
- 000300.SH：min ≈ 2010-01-04，与既有 2021+ 段无缝衔接
- 任一条不达标：查 gateway 日志与 Wind 返回码，解决前不进 Task 6

（Task 0 完成后对创业板/科创 4 条重复 Step 3 命令格式，start 2013-12-01。）

---

## Task 6：PG vs CSV 数据 diff 报告

**Files:**
- Create: `tools/diff_pg_vs_csv.py`

**Step 1: 写 diff 工具**

`tools/diff_pg_vs_csv.py`：

```python
"""逐日逐列比对 PG index_daily 与本地 CSV，输出差异报告。

用法（仓库根）: python3 tools/diff_pg_vs_csv.py
报告: output/phase1_diff/summary.csv + 明细 *_mismatch.csv（仅当有差异）
"""
import sys
from pathlib import Path

import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from signals.common.data_source import load_pg_closes  # noqa: E402

OUT_DIR = ROOT / "output" / "phase1_diff"
TOL = 1e-4  # CSV 导出精度容差；严格目标是 0


def load_citic_csv() -> pd.DataFrame:
    df = pd.read_csv(ROOT / "data" / "中信风格合并.csv", skiprows=5, usecols=[0, 1, 2, 3, 4, 5],
                     names=["date", "稳定", "成长", "金融", "周期", "消费"], parse_dates=["date"])
    return df.dropna(subset=["date"]).set_index("date").sort_index().astype(float)


def load_gv_csv(name: str) -> pd.DataFrame:
    df = pd.read_csv(ROOT / "data" / name, encoding="utf-8-sig")
    df = df.rename(columns={df.columns[0]: "date"})
    df["date"] = pd.to_datetime(df["date"], errors="coerce")
    return df.dropna(subset=["date"]).set_index("date").sort_index().apply(pd.to_numeric, errors="coerce")


def load_hs300_csv() -> pd.DataFrame:
    df = pd.read_csv(ROOT / "data" / "沪深300.csv", skiprows=6, usecols=[0, 1],
                     names=["date", "沪深300"], parse_dates=["date"])
    return df.dropna(subset=["date"]).set_index("date").sort_index().astype(float)


def compare(tag: str, csv_df: pd.DataFrame, rows: list[dict]) -> None:
    names = [c for c in csv_df.columns]
    try:
        pg_df = load_pg_closes(names)
    except (KeyError, ValueError) as e:  # 代码未映射/未回填（如创业板/科创）
        rows.append({"数据集": tag, "状态": f"跳过: {e}"})
        return
    common = csv_df.index.intersection(pg_df.index)
    pg_missing = csv_df.index.difference(pg_df.index)
    for col in names:
        both = pd.DataFrame({"csv": csv_df.loc[common, col], "pg": pg_df.loc[common, col]}).dropna()
        diff = (both["csv"] - both["pg"]).abs()
        n_bad = int((diff > TOL).sum())
        rows.append({
            "数据集": tag, "列": col, "共同日数": len(both),
            "最大绝对差": float(diff.max()) if len(both) else None,
            "超容差行数": n_bad, "PG缺日数": len(pg_missing),
            "状态": "OK" if n_bad == 0 and len(pg_missing) == 0 else "DIFF",
        })
        if n_bad:
            bad = both[diff > TOL]
            bad.to_csv(OUT_DIR / f"{tag}_{col}_mismatch.csv")


def main() -> int:
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    rows: list[dict] = []
    compare("中信风格", load_citic_csv(), rows)
    compare("成长价值2019", load_gv_csv("成长价值指数_2019.csv"), rows)
    compare("成长价值2014", load_gv_csv("成长价值指数_2014.csv"), rows)
    compare("沪深300", load_hs300_csv(), rows)
    report = pd.DataFrame(rows)
    report.to_csv(OUT_DIR / "summary.csv", index=False)
    print(report.to_string(index=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
```

**Step 2: 运行并判读**

Run: `cd /home/elfbob/claude-code/style_timing_signal && python3 tools/diff_pg_vs_csv.py`

判定阶梯：
1. 全列 `状态=OK` 且最大绝对差 = 0 → 理想，可做字节级复现（Task 7–9 用 `cmp` 验证）；
2. 最大绝对差 ≤ 1e-4 且无缺日 → CSV 导出精度差异：打开对应 `_mismatch.csv` 确认是纯舍入（如 CSV 2 位小数）。若是，Task 7–9 验证口径降为"因子列逐行差 < 1e-3"，并在 commit message 记录；
3. 有缺日或差异超容差 → **停**：逐条排查（Wind 导出口径 vs wsd 口径 / 代码映射错误 / 回填缺段），解决后重跑。成长价值 CSV 若与 932 系历史回溯值有系统性差异，说明原 CSV 用的是别的 1000/2000 风格指数（如国证 399630/631）——回到用户核对代码。

**Step 3: Commit（工具 + 判定结论）**

```bash
git add tools/diff_pg_vs_csv.py
git commit -m "feat(tools): PG vs CSV 逐日逐列 diff 工具；diff 结论: <OK全零 / 舍入级差异 / 见 summary>"
```

（`output/phase1_diff/` 为运行产物，不提交。）

---

## Task 7：hybrid20 两脚本读库改造 + 复现验证

**Files:**
- Modify: `signals/hybrid20/update_growth_stability.py`
- Modify: `signals/hybrid20/update_confirmed_signal.py`

**Step 1: 改造 update_growth_stability.py**

顶部 import 区追加 `import argparse`、`import sys`。把第 44–55 行（"读取数据"段）替换为：

```python
# ════════════════════════════════════════
# 参数与数据源
# ════════════════════════════════════════
parser = argparse.ArgumentParser(description="成长/稳健相对强弱信号")
parser.add_argument("--source", choices=["csv", "pg"], default="csv",
                    help="数据源: csv=data/中信风格合并.csv, pg=stock_selector.index_daily")
parser.add_argument("--start", default=None, help="pg 模式起始日 YYYY-MM-DD（复现验证时传 CSV 首日）")
parser.add_argument("--output", default=str(OUTPUT_FILE), help=f"输出路径, 默认 {OUTPUT_FILE}")
args = parser.parse_args()

if args.source == "pg":
    sys.path.insert(0, str(ROOT))
    from signals.common.data_source import load_pg_closes

    df = load_pg_closes(["稳定", "成长"], start=args.start).rename(
        columns={"稳定": "stability", "成长": "growth"}
    )
else:
    df = pd.read_csv(
        INPUT_FILE,
        skiprows=5,
        usecols=[0, 1, 2],
        names=["date", "stability", "growth"],
        parse_dates=["date"],
    )
    df = df.dropna(subset=["date"]).set_index("date").sort_index()
    df["stability"] = df["stability"].astype(float)
    df["growth"] = df["growth"].astype(float)
```

末尾输出段 `out.to_csv(OUTPUT_FILE)` 改为 `out.to_csv(args.output)`，两处打印 `{INPUT_FILE}` 改为 `{args.source}:{INPUT_FILE if args.source == 'csv' else 'stock_selector.index_daily'}`（保持原打印结构即可，措辞不苛求）。

**Step 2: 改造 update_confirmed_signal.py**

同样方式：argparse（`--source/--start/--output`，default 同上），"读取数据"段 PG 分支：

```python
if args.source == "pg":
    sys.path.insert(0, str(ROOT))
    from signals.common.data_source import load_pg_closes

    df = load_pg_closes(["稳定", "金融"], start=args.start).rename(
        columns={"稳定": "stability", "金融": "finance"}
    )
else:
    ...  # 原 CSV 读取逻辑原样保留
```

`ORIG_SIGNAL_FILE` 读取保持不变（读上一步输出）；新增 `--orig-signal` 参数默认 `ORIG_SIGNAL_FILE`（验证时指向 pg 模式的中间产物）；输出走 `args.output`。

**Step 3: CSV 模式回归（输出必须与改造前完全一致）**

```bash
cd /home/elfbob/claude-code/style_timing_signal
git stash -- output/ 2>/dev/null; true   # 确保工作区输出是提交态
python3 signals/hybrid20/update_growth_stability.py
python3 signals/hybrid20/update_confirmed_signal.py
git diff --stat -- output/hybrid20/
```

Expected: `git diff` 无变化（默认 csv 路径行为与改造前逐字节一致）。

**Step 4: PG 模式复现验证**

先取 CSV 首日：`head -7 data/中信风格合并.csv`（第 6 行数据行日期，应为 2010-01-04）。

```bash
mkdir -p /tmp/claude-1000/-home-elfbob-claude-code-style-timing-signal/1a888100-1c74-4551-9deb-00800878efb1/scratchpad/pgverify
SCRATCH=/tmp/claude-1000/-home-elfbob-claude-code-style-timing-signal/1a888100-1c74-4551-9deb-00800878efb1/scratchpad/pgverify
python3 signals/hybrid20/update_growth_stability.py --source pg --start 2010-01-04 --output $SCRATCH/gs.csv
python3 signals/hybrid20/update_confirmed_signal.py --source pg --start 2010-01-04 \
  --orig-signal $SCRATCH/gs.csv --output $SCRATCH/confirmed.csv
cmp $SCRATCH/gs.csv output/hybrid20/growth_stability_signal.csv && echo GS_IDENTICAL
cmp $SCRATCH/confirmed.csv output/hybrid20/confirmed_signal.csv && echo CONFIRMED_IDENTICAL
```

Expected: 两个 IDENTICAL（Task 6 结论为舍入级差异时，改用 pandas 读入比较因子列 `abs diff < 1e-3`、信号列完全一致，并记录）。
注意：PG 的 max(trade_date) 若新于 CSV 尾日，输出会多出尾部行 → `cmp` 前先按 CSV 尾日截断比较（`head -n $(wc -l < output/hybrid20/growth_stability_signal.csv)`）。差异只允许出现在"PG 比 CSV 多出的尾部新交易日"。

**Step 5: Commit**

```bash
git add signals/hybrid20/update_growth_stability.py signals/hybrid20/update_confirmed_signal.py
git commit -m "feat(hybrid20): --source pg 读库支持，CSV 默认行为与 PG 复现均验证一致"
```

---

## Task 8：citic40d 读库改造 + 复现验证

**Files:**
- Modify: `signals/citic40d/generate_signal.py`

**Step 1: 改造**

`load_style_data` 保持为 CSV 加载器；新增 PG 分支。main() 中：

```python
parser.add_argument("--source", choices=["csv", "pg"], default="csv")
parser.add_argument("--start", default=None, help="pg 模式起始日（复现验证传 2010-01-04）")
```

加载逻辑：

```python
if args.source == "pg":
    import sys
    sys.path.insert(0, str(ROOT))
    from signals.common.data_source import load_pg_closes

    style = load_pg_closes(["稳定", "成长", "金融", "周期", "消费"], start=args.start).rename(
        columns={"稳定": "stability", "成长": "growth", "金融": "finance",
                 "周期": "cycle", "消费": "consumption"}
    )
else:
    style = load_style_data(args.input)
```

**Step 2: CSV 回归 + PG 复现（同 Task 7 模式）**

```bash
python3 signals/citic40d/generate_signal.py
git diff --stat -- output/citic40d/            # 应无变化
python3 signals/citic40d/generate_signal.py --source pg --start 2010-01-04 --output $SCRATCH/citic.csv
cmp <(head -n $(wc -l < output/citic40d/citic_style_signal_40d.csv) $SCRATCH/citic.csv) \
    output/citic40d/citic_style_signal_40d.csv && echo CITIC_IDENTICAL
```

Expected: CITIC_IDENTICAL

**Step 3: Commit**

```bash
git add signals/citic40d/generate_signal.py
git commit -m "feat(citic40d): --source pg 读库支持，输出与 CSV 模式一致"
```

---

## Task 9：equal_weight 读库改造 + 复现验证

**Files:**
- Modify: `signals/equal_weight/generate_signal.py`
- Create（如 Task 0 已完成）: `signals/common/index_codes.csv` 追加 4 行

**Step 1: 改造**

main() 加 `--source {csv,pg}`（default csv）与 `--start`。加载顺序反转为**先配置后价格**：

```python
pair_configs = load_pair_configs(args.config)          # 不再传 price_columns，先取列清单
needed = [c for p in pair_configs for c in (p.left_column, p.right_column)]

if args.source == "pg":
    import sys
    sys.path.insert(0, str(ROOT))
    from signals.common.data_source import load_pg_closes

    prices = load_pg_closes(list(dict.fromkeys(needed)), start=args.start)
else:
    prices = load_price_data(args.input)

pair_configs = _validate_pair_configs(pair_configs, list(prices.columns))
```

（`load_pair_configs(config_file)` 已支持 `price_columns=None`；二次校验复用现有 `_validate_pair_configs`。）

**Step 2: CSV 回归**

```bash
python3 signals/equal_weight/generate_signal.py
python3 signals/equal_weight/generate_signal.py \
  --input data/成长价值指数_2014.csv --config signals/equal_weight/config_5pairs.csv \
  --lookback 5 --z-window 20 --smoothing 0 \
  --output output/equal_weight/equal_weight_signal_5d20z.csv
git diff --stat -- output/equal_weight/       # 应无变化
python3 -m pytest tests/ -q                    # 全套回归
```

**Step 3: PG 复现验证**

前置：Task 0 的 4 条代码已入 `index_codes.csv` 并按 Task 5 Step 3 格式回填。
- 变体A：`--source pg --start 2019-12-31 --config config_6pairs.csv` → 与 `equal_weight_signal_20d40z.csv` 截尾 cmp；
- 变体B：`--source pg --start 2014-01-02 --config config_5pairs.csv --lookback 5 --z-window 20 --smoothing 0` → 与 `equal_weight_signal_5d20z.csv` 截尾 cmp。

若 Task 0 未完成：仅用已确认列临时建 `config_4pairs_pgtest.csv`（300/500/1000/2000 四对）跑 PG 模式做**管线冒烟**（不做数值对照），在 commit message 标注"变体A/B 全列复现验证 gated on 创业板/科创代码核对"。

**Step 4: Commit**

```bash
git add signals/equal_weight/generate_signal.py signals/common/index_codes.csv
git commit -m "feat(equal_weight): --source pg 读库支持（配置先行、按需取列）"
```

---

## Task 10：默认源翻转 + 文档更新

**前置**：Task 7–9 复现验证全部通过（equal_weight 至少变体B 通过）。

**Files:**
- Modify: 四个信号脚本的 `--source` default `"csv"` → `"pg"`
- Modify: `README.md`（运行段 + 数据流段）、`data/README.md`（口径说明）

**Step 1: 翻转默认值**（4 处一行改动）

**Step 2: 文档更新**

- 根 `README.md`：数据流图与"日常更新流程"改为"PG 自动日更（见 tools/topup_index_daily.sh）→ 跑信号命令 → 看 output"；CSV 模式说明为 `--source csv` 备份/审计口径；
- `data/README.md`：顶部加一段"2026-07 Phase1 起默认数据源为 PG（stock_selector.index_daily），本目录 CSV 转为备份与复现对照口径，不再要求逐日人工维护"。

**Step 3: 终验（默认即 PG）**

```bash
python3 signals/hybrid20/update_growth_stability.py && python3 signals/hybrid20/update_confirmed_signal.py
python3 signals/citic40d/generate_signal.py
python3 signals/equal_weight/generate_signal.py   # 变体A（Task 0 完成后）
python3 -m pytest tests/ -q
```

Expected: 全部成功、pytest 全过、output 最新行日期 = PG 最新交易日。

**Step 4: Commit**

```bash
git add signals/ README.md data/README.md
git commit -m "feat: 三条信号线默认数据源切 PG，CSV 降级为备份口径"
```

---

## Task 11：日更 topup 脚本

**Files:**
- Create: `tools/topup_index_daily.sh`

**Step 1: 写脚本**

```bash
#!/usr/bin/env bash
# 增量补齐 stock_selector.index_daily 的本项目指数到今天。
# 前置：Wind gateway 在线。用法：tools/topup_index_daily.sh [START_DATE]
# START 缺省取 14 天前（upsert 幂等，重叠无害，顺带自愈短缺口）。
set -euo pipefail
CODES="CI005917.WI,CI005918.WI,CI005919.WI,CI005920.WI,CI005921.WI,000918.CSI,000919.CSI,H30351.CSI,H30352.CSI,932406.CSI,932407.CSI,932408.CSI,932409.CSI,932000.CSI,000300.SH"
START="${1:-$(date -d '14 days ago' +%F)}"
END="$(date +%F)"
cd /home/elfbob/claude-code/stock_selector
exec .venv/bin/python -m stock_selector.backfill.cli date-range \
  --table index_daily --tickers "$CODES" --start "$START" --end "$END" --source wind
```

（Task 0 代码确认后把 4 条追加进 CODES。）

**Step 2: 执行验证**

Run: `chmod +x tools/topup_index_daily.sh && tools/topup_index_daily.sh`
Expected: 正常完成；重复运行第二遍也正常（幂等）。

**Step 3: Commit**

```bash
git add tools/topup_index_daily.sh
git commit -m "feat(tools): index_daily 日更 topup 脚本（幂等，默认回看14天）"
```

---

## Task 12（OPTIONAL，stock_selector 仓库）：scheduler 日更接管

`stock_selector/stock_selector/scheduler/jobs.py` 目前只有 `_MONTHLY_INDICES`（月频、单日拉取）。若用户希望 scheduler 自动接管（替代手动 topup）：在 `daily_update_job` 内对本项目 15+4 条代码追加 `write_index_daily(conn, source.fetch_index_daily(codes, as_of, as_of))`，代码清单提为模块常量 `_DAILY_INDICES`，并补对应单测。**改动在 stock_selector 仓库、须其 356 项测试全过**；scheduler 当前未必常驻运行（stock_indicator 增量停在 2026-04-30），故本 task 默认跳过，topup 脚本为主路径。

---

## Task 13：收尾

**Step 1: 全套测试**

Run: `python3 -m pytest tests/ -q`
Expected: 全过（原有 + 新增 7 项）

**Step 2: 设计稿状态更新**

`docs/plans/2026-07-03-optimization-roadmap-design.md` 头部状态行改为：`**状态**: Phase 1 已落地（见 2026-07-03-phase1-db-integration-plan.md）；Phase 2 待启动`。

**Step 3: Commit**

```bash
git add docs/plans/2026-07-03-optimization-roadmap-design.md
git commit -m "docs: Phase 1 完成标记，设计稿状态更新"
```

**验收总清单**（全部满足才算 Phase 1 完成）：
- [ ] 15 条指数在 `index_daily` 覆盖至最近交易日，起点达标
- [ ] diff 报告全 OK（或舍入级差异已归因并记录）
- [ ] hybrid20 / citic40d PG 复现逐字节一致；equal_weight 至少变体B 一致（变体A gated on Task 0）
- [ ] 默认源 = pg，`--source csv` 回退可用，pytest 全过
- [ ] topup 脚本幂等可用，README/data README 已改口径
