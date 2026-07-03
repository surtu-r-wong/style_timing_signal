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
