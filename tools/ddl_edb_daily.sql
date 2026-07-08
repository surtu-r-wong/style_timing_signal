-- 通用 EDB 序列落库表（方向 B 换轴数据解锁，2026-07-08）
--
-- ⚠️ 执行前必读：/home/elfbob/market-monitor/migration/SCHEMA_CHANGES.md 顶部
--    "🛡️ DDL 安全快查卡"（已按其设计本表；执行时仍过一遍 A 节 4 件必做）。
--
-- 设计决策：
--   * 不纳入双端同步（信号研究序列，Debian 单端够用）——按安全卡 B"新表（不纳入
--     同步）"：单端建即可，无需 updated_at 触发器/sync_state baseline/config.yaml；
--     判定命令：SELECT * FROM sync_state WHERE schema_name='stock_selector'
--               AND table_name='edb_daily';  -- 应无行
--   * 通用表：一张吃所有 EDB 序列（两融余额/买入额、10Y 国债 YTM、未来 ERP 组件
--     等），margin 语义放 series_name，未来新序列零 DDL；
--   * 不动 public.bond_daily（其在同步链上，写它有跨端影响面；留给真正债券行情）。
--
-- 执行（Debian 主端，admin 用户）：
--   psql -h 100.65.111.79 -U admin -d market_monitor -v ON_ERROR_STOP=1 \
--        -f tools/ddl_edb_daily.sql

CREATE TABLE IF NOT EXISTS stock_selector.edb_daily (
    edb_code    text        NOT NULL,   -- Wind EDB 序列 id（用户在终端核对后使用）
    trade_date  date        NOT NULL,
    value       numeric     NOT NULL,
    series_name text,                   -- 人读标签，如 融资余额_沪 / 中债10Y_YTM
    updated_at  timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (edb_code, trade_date)
);

COMMENT ON TABLE stock_selector.edb_daily IS
    'Wind EDB 宏观/市场日频序列（两融、国债收益率等）；不纳入双端同步；'
    '写入方 style_timing_signal/tools/backfill_edb.py';
