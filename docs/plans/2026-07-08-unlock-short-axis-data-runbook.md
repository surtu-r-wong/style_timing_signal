# 空头换轴数据解锁 runbook（2026-07-08）

> 用户选定：先解锁用户侧数据，再开工高价值束杆轴（两融/ETF 申赎）。
> 本文 = 你的待办清单（按依赖排序）+ 我方已就绪件清单。
> 数据侦察更正（本轮实查）：**涨停温度计不卡 Wind**（stock_daily_price high/pre_close
> 完备 >99.5%，单日验证封板 31/炸板 15 量级合理）——它随时可作零等待备选轴。

## 我方已就绪（本轮交付，零等待）

| 件 | 位置 | 状态 |
|---|---|---|
| gateway `/fetch/edb` 通用端点 | stock_selector 仓库 commit（wind_client.call_edb + endpoint + 5 测试，全套 148 过） | ✅ 待部署 |
| `edb_daily` DDL 稿 | `tools/ddl_edb_daily.sql`（通用 EDB 表；**不动同步链上的 bond_daily**；不纳入同步） | ✅ 待执行 |
| 回填脚本 | `tools/backfill_edb.py`（gateway → edb_daily 幂等 upsert，EDB 码全由参数传入） | ✅ 就绪 |
| settings 模板 | `config/settings.example.yaml` 加 `wind_gateway.url/token` 段 | ✅（你的 settings.yaml 补两行） |

## 你的待办（按依赖排序）

### A. 部署 gateway 更新（Windows `desktop-p7mgeir`，~2 分钟）
1. 拷贝 stock_selector 仓库最新 `wind_gateway/wind_client.py` + `wind_gateway/endpoints.py`
   到 `D:\wind_gateway\`（或整目录 git pull）；
2. 重启 wind_gateway 进程（`$env:PYTHONPATH="D:\"; python -m wind_gateway`）；
3. **config.yaml 无需任何改动**（EDB 无字段概念，码由客户端传参——哑管道）。
4. 验收：`curl -H "Authorization: Bearer <tok>" "http://100.120.152.1:8080/fetch/edb?codes=<任一EDB码>&start=2026-06-01&end=2026-07-01"` 出数即通。

### B. Wind 终端核对 EDB 码（EDB 浏览器，~10 分钟）
> 我不臆造具体码——请在 Wind 终端 EDB 数据浏览器按关键词检索并记下序列 ID（M 开头）。

| 用途 | 检索关键词 | 期望序列 | 备注 |
|---|---|---|---|
| 杠杆轴·两融 | 「融资余额」 | 沪市 + 深市（或两市合计） | 日频；若有「融资融券余额」合计也记下 |
| 杠杆轴·两融 | 「融资买入额」 | 沪市 + 深市 | 日频 |
| ERP 债腿 | 「中债国债到期收益率:10年」 | 10Y YTM | 日频；design 附录 B2 即此 |

### C. 执行 edb_daily DDL（Debian 主端，~1 分钟）
```bash
# 执行前过一遍 SCHEMA_CHANGES.md 安全卡 A 节（backup/同步健康/回滚/同步判定）
psql -h 100.65.111.79 -U admin -d market_monitor -v ON_ERROR_STOP=1 \
     -f tools/ddl_edb_daily.sql
```
（或让我代跑——SQL 已按安全卡设计：新表不纳入同步=单端安全。）

### D. 跑回填（Linux，B/C 完成后）
```bash
# settings.yaml 补 wind_gateway 段后：
python3 tools/backfill_edb.py \
    --codes "<码1>,<码2>,<码3>,<码4>" \
    --names "融资余额_沪,融资余额_深,融资买入额_沪,融资买入额_深" \
    --start 2014-01-01 --end $(date +%F)
python3 tools/backfill_edb.py --codes "<10Y码>" --names "中债10Y_YTM" \
    --start 2014-01-01 --end $(date +%F)
```
两融历史 2014 起 ≈ 3,000 日 × 4 序列 = 1.2 万 cells，EDB 额度占用可忽略。

### E. （独立，可延后）ETF 份额字段
`fund_nav_daily` 在库 447 万行日频（2020-05 起）但**只有净值列**——申赎信号需要
**份额**。两融解锁后若要做资金行为轴：在 Wind 核对基金份额 wsd 字段（参考
`docs/operations/wind-api-cheatsheet.md`），加进 `wind_gateway/config.yaml` 的
fund_nav_daily wsd 字段表（此表你独占维护），Linux 侧 writer 加列另议。

### F. （独立，运维）futures_daily 日更断点
`public.futures_daily` 止 **2026-04-29**（断 ~2 个月）。写入方=spread_analyzer
`fetch_prices` 管道——排查其 scheduler 是否停跑。恢复后基差轴（carry-deepening）
的日更信号才有生产意义（历史段回测不受影响）。

## 数据到位后的开工顺序（下一轮）

1. **杠杆轴信号研究**（两融增速/占成交比——design §2 面4，P1.5 首位）：
   brainstorm 定信号形态 → rotation probe 三关闸门模板直接复用（含 partial_rank_ic
   独立增量检验）→ 过闸才进空头引擎装配（dual_legs_external_short 编排器现成）；
2. 备选并行：涨停温度计轴（零数据等待，随时可启）。
