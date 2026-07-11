"""薄财务读取层（方向 C · B1）：读 stock_financial → friendly 归一化 + 源切换 + PIT。

镜像 stock_selector.pg_reader.fetch_financial 的读取口径，但用本项目连接、不 import
stock_selector 包（守设计稿 §4.1）。纯逻辑（translate_data / PIT deadline）在
financial_field_map（拷贝，有校验测试）；本模块是薄 IO 胶水，跑真库验证。
"""
import sys
from pathlib import Path

import pandas as pd

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from signals.common.config import load_db_config  # noqa: E402
from signals.common.financial_field_map import (  # noqa: E402
    CSMAR_END, legal_disclosure_deadline, translate_data,
)


def _connect(db):
    import psycopg2
    return psycopg2.connect(host=db["host"], port=db["port"], dbname=db["name"],
                            user=db["user"], password=db["password"], connect_timeout=15)


def fetch_financial_facts(tickers, start, end, db=None, statement_types=None) -> pd.DataFrame:
    """归一化财务事实：ts_code, end_date, statement_type, ann_date(PIT-capped), data(friendly dict)。

    源切换：end_date ≤ CSMAR_END 取 csmar、之后取 wind。ann_date 取 min(库存, 法定披露上限)：
    CSMAR 库存 ann_date 多为数据集批次日（73% 晚于法定截止），cap 后才可用；代价是真晚披的
    少数个股（~1%/年，多 ST）被提前到法定日，非严格 PIT——详见
    financial_field_map.legal_disclosure_deadline docstring（2026-07-11 审查修正）。
    statement_types 非 None 时只取列出的报表类型（批量管线省流量）。
    """
    db = db or load_db_config()
    type_clause = "AND statement_type = ANY(%s)" if statement_types else ""
    sql = f"""
        SELECT ts_code, end_date, statement_type, ann_date, report_type, data, data_source
        FROM {db['schema']}.stock_financial
        WHERE ts_code = ANY(%s) AND end_date BETWEEN %s AND %s
          AND ((data_source='csmar' AND end_date <= %s)
            OR (data_source='wind'  AND end_date >  %s))
          {type_clause}
        ORDER BY ts_code, statement_type, end_date
    """
    params = [list(tickers), start, end, CSMAR_END, CSMAR_END]
    if statement_types:
        params.append(list(statement_types))
    conn = _connect(db)
    try:
        with conn.cursor() as cur:
            cur.execute(sql, params)
            rows, cols = cur.fetchall(), [d[0] for d in cur.description]
    finally:
        conn.close()
    df = pd.DataFrame(rows, columns=cols)
    if df.empty:
        return df
    df["data"] = [translate_data(d, src, st)
                  for d, src, st in zip(df["data"], df["data_source"], df["statement_type"])]
    df["ann_date"] = [min(ann, legal_disclosure_deadline(ed))
                      for ann, ed in zip(df["ann_date"], df["end_date"])]
    return df.drop(columns=["data_source"]).reset_index(drop=True)
