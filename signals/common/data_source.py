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
