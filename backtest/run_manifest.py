from __future__ import annotations

import hashlib
import json
import os
import subprocess
from pathlib import Path


def create_run_dir(root: Path, run_id: str) -> Path:
    candidate = Path(run_id)
    if (
        not candidate.parts
        or candidate == Path(".")
        or candidate.is_absolute()
        or len(candidate.parts) != 1
        or candidate.parts[0] == ".."
    ):
        raise ValueError("run_id must be a single relative path component")
    target = Path(root) / candidate
    target.mkdir(parents=True, exist_ok=False)
    (target / "inputs").mkdir()
    (target / "outputs").mkdir()
    (target / "logs").mkdir()
    return target


def artifact_record(path: Path, base: Path) -> dict:
    path = Path(path)
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return {
        "path": path.relative_to(base).as_posix(),
        "size": path.stat().st_size,
        "sha256": digest.hexdigest(),
    }


def write_manifest(run_dir: Path, payload: dict) -> Path:
    target = Path(run_dir) / "manifest.json"
    temporary = Path(run_dir) / ".manifest.json.tmp"
    rendered = json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
    temporary.write_text(rendered, encoding="utf-8")
    os.replace(temporary, target)
    return target


def _git_output(root: Path, *args: str) -> str:
    return subprocess.run(
        ["git", *args], cwd=root, check=True, capture_output=True, text=True
    ).stdout


def git_state(root: Path) -> dict[str, object]:
    return {
        "commit": _git_output(root, "rev-parse", "HEAD").strip(),
        "dirty": bool(_git_output(root, "status", "--porcelain").strip()),
    }


#: 输入表 → 其「数据日期」列。cutoffs / 漂移检测共用。
DEFAULT_INPUT_CONTRACT = {
    "index_daily": "trade_date",
    "stock_daily_price": "trade_date",
    "stock_indicator": "trade_date",
    "stock_financial": "end_date",
    "index_constituent": "effective_date",
}


def query_table_write_marks(connection, schema: str, contract: dict[str, str]) -> dict[str, str]:
    """每张输入表的 `max(updated_at)` —— 「**最后一次写入**是什么时候」。

    ## 为什么 `query_table_cutoffs` 不够（本函数存在的唯一理由）

    cutoffs 记的是 `max(数据日期)` = 「数据**延伸到**哪一天」，对**原地改写历史行**
    完全失明。2026-08 两次实测都属这一类：

    - 08-24：`index_daily` 的 932408/932409 自 08-03 起风格腿滞后一天，topup 重取
      改的是**已存在行的值**；
    - 08-25：另一会话补 `stock_indicator` 2025-09~2026-04 的历史缺口，补的是
      **窗口内本来缺失的行**。

    两者都不动 `max(trade_date)` —— 两次 run 的 `database_cutoffs` 一模一样，
    而读数差了 0.012。⇒ 想判断「这次 run 读到的输入是否被动过」，必须看写入时刻。
    """
    out = {}
    with connection.cursor() as cursor:
        for table in contract:
            cursor.execute(f"SELECT max(updated_at)::text FROM {schema}.{table}")
            out[table] = cursor.fetchone()[0]
    return out


def rows_touched_in_window(connection, schema: str, contract: dict[str, str],
                           since: str, terminal: str) -> dict[str, int]:
    """自 `since` 起被写入、且**数据日期 ≤ `terminal`** 的行数（按表）。

    `terminal` = 分析窗口终点。窗口**外**的写入（例如日更 timer 追加当天行）不影响
    历史读数，不该拦；窗口**内**的写入才会改变结果。这条区分是本机制可用的前提 ——
    否则每天 18:30 的日更都会把 run 标脏。
    """
    out = {}
    with connection.cursor() as cursor:
        for table, date_col in contract.items():
            cursor.execute(
                f"SELECT count(*) FROM {schema}.{table} "
                f"WHERE updated_at > %s AND {date_col} <= %s",
                (since, terminal))
            out[table] = int(cursor.fetchone()[0])
    return out


def input_drift_report(connection, schema: str, contract: dict[str, str],
                       marks_before: dict, since: str, terminal: str) -> dict:
    """run 期间输入是否被动过 —— 决定该 run 的读数**能否登记为首跑值**。

    `registrable_as_first_run` 为 False 时，该次读数**不得**用来重登锚：
    首跑值一旦登记就是永久参照物，标准应比普通运行更严。
    （不阻断运行本身 —— 拦住合法运行的代价比标脏一次高得多。）
    """
    after = query_table_write_marks(connection, schema, contract)
    moved = {t: {"before": marks_before.get(t), "after": after.get(t)}
             for t in contract if marks_before.get(t) != after.get(t)}
    touched = (rows_touched_in_window(connection, schema, contract, since, terminal)
               if moved else {})
    in_window = {t: n for t, n in touched.items() if n > 0}
    return {
        "write_marks_before": marks_before,
        "write_marks_after": after,
        "moved_tables": moved,
        "rows_touched_in_window": in_window,
        "inputs_moved": bool(moved),
        "inputs_moved_in_window": bool(in_window),
        "registrable_as_first_run": not in_window,
    }


def query_table_cutoffs(connection, schema: str, contract: dict[str, str]) -> dict[str, str]:
    out = {}
    with connection.cursor() as cursor:
        for table, column in contract.items():
            cursor.execute(f"SELECT max({column})::text FROM {schema}.{table}")
            value = cursor.fetchone()[0]
            if value is None:
                raise RuntimeError(f"{schema}.{table}.{column} has no cutoff")
            out[table] = value
    return out
