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

### B. Wind 终端核对 EDB 码（EDB 数据浏览器，~10-15 分钟）
> 入口：终端「数据」→「EDB 经济数据库」（或顶部搜索框输 EDB）。检索到序列后，
> 序列详情里有 **指标 ID（M 开头）**、频率、起始日期——三样都记。
> 铁律：以终端所见为准，下面只是检索词与预期，不是答案。

| # | 用途 | 检索关键词 | 要哪条 | 预期 | 记录 |
|---|---|---|---|---|---|
| 1 | 杠杆·存量 | 融资余额 | **沪市**融资余额 | 日频，2010-03 起 | M 码+起始日 |
| 2 | 杠杆·存量 | 融资余额 | **深市**融资余额 | 日频，2010-03 起 | M 码+起始日 |
| 3 | 杠杆·流量 | 融资买入额 | 沪市融资买入额 | 日频 | M 码 |
| 4 | 杠杆·流量 | 融资买入额 | 深市融资买入额 | 日频 | M 码 |
| 5 | 可选·省事 | 融资融券余额 | **两市合计**（若有单一序列） | 日频 | M 码（有则 1/2 可只作校验） |
| 6 | 可选·做空侧 | 融券余额 | 两市或分市场 | 日频（量小，完整性用） | M 码 |
| 7 | ERP 债腿 | 中债国债到期收益率:10年 | 10Y YTM | 日频，2002 起 | M 码 |

栏目路径参考（版本可能不同）：1-6 大致在「中国 → 金融市场/股票 → 融资融券」；
7 在「中国 → 利率 → 中债收益率曲线」。检索框直接搜关键词最快。

找到任一码后即可验收 gateway（待办 A 完成后）：
```bash
curl -H "Authorization: Bearer <tok>" \
  "http://100.120.152.1:8080/fetch/edb?codes=<M码>&start=2026-06-01&end=2026-07-01"
```

**✅ B 完成（2026-07-08 用户核对回传，顺序=沪,深）**：

| 序列 | 沪 | 深 |
|---|---|---|
| 融资余额 | M0061606 | M0061610 |
| 融资买入额 | M0061604 | M0061609 |
| 融资融券余额 | M0061608 | M0061613 |
| 融券余额 | M0061607 | M0061612 |
| 中债国债到期收益率:10年 | M1000166 | — |

ETF 份额 wsd 字段（E 用）：`unit_fundshare_total`（份额总额）+ `unit_floortrading`
（场内流通份额），options `unit=1`；示例标的 510330.SH。

**✅ C 完成（2026-07-08 AI 代跑）**：edb_daily 已建（安全卡判定=非同步对象，
sync_state 0 行；回滚 SQL：`DROP TABLE IF EXISTS stock_selector.edb_daily;`）。

**D 一键回填（gateway 通后执行）**：
```bash
python3 tools/backfill_edb.py \
  --codes "M0061606,M0061610,M0061604,M0061609,M0061608,M0061613,M0061607,M0061612" \
  --names "融资余额_沪,融资余额_深,融资买入额_沪,融资买入额_深,融资融券余额_沪,融资融券余额_深,融券余额_沪,融券余额_深" \
  --start 2010-01-01 --end $(date +%F)
python3 tools/backfill_edb.py --codes "M1000166" --names "中债10Y_YTM" \
  --start 2010-01-01 --end $(date +%F)
```
回填后自洽校验：融资融券余额(沪) ≈ 融资余额(沪)+融券余额(沪)——验证沪/深分组
标签是否正确（若恒等式跨组才成立则标签互换）。

**✅ D 完成（2026-07-08 晚，39,718 行入库）**：两融 8 条各 3,949 行
（2010-04-01..2026-07-07）+ 10Y 两条 4,117/4,009 行（2010-01-04 起，
货币网 M1002626 为主、中证 M1001654 备份；上清所条 2017 起太短弃用）。
自洽校验：沪/深「融资融券余额=融资余额+融券余额」恒等式误差 ~1e-16 ✓。

**⚠️ 排障结论（重要运维知识，2026-07-08 一整晚的教训）**：
1. **502 的真相 = 360 按连接来源对 8080 HTTP 深度检查**：本机交互进程
   （curl.exe）放行；sshd 隧道转发、tailscaled 外来流量被检查——小响应
   （ping）放行、大响应（edb 全历史 JSON）被吃回 502 空 body，且有触发后
   数分钟的间歇拦截窗。gateway 进程本身基本从未崩溃（本机始终 200）。
2. **可靠取数路径**：`ssh → Windows 本机 curl.exe localhost:8080 落 JSON
   文件 → scp 拉回 → 本地解析入库`。一次性 10 码全历史无一失败。
   走隧道/直连仅适合小请求（ping/短窗验证）。
3. WindPy 会话有 **Windows 登录会话隔离**：ssh 会话里 `w.start()` 报
   -40520004 不代表终端没登录——gateway 必须从桌面会话启动。
4. M1000166（中债估值那条 10Y）API 取出全 null（授权差异），换来源即可。
5. 若要根治网络路径：360 白名单放行 python/sshd 的 8080，或 gateway 换
   非常见端口（未验证）。当前本机-curl 路径够用，未动。

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

### E. （独立，可延后）ETF 份额字段（代码生成器 CG，~10 分钟）
`fund_nav_daily` 在库 447 万行日频（2020-05 起）但**只有净值列**——申赎信号需要
**基金份额**（日频，场内 ETF 每日申赎才有意义）。

> 入口：终端「量化」→「代码生成器 CG」→ 函数选 **wsd** → 品种类型选基金。
> 铁律同上：字段名以 CG 输出为准（cheatsheet 第一条），不要用别处的拼写。

1. 选一只 ETF 试（如 510300.SH 沪深300ETF），字段树/检索框搜「**份额**」——
   目标是「基金份额」或「场内流通份额」类**日频**字段（不是净值、不是规模元值）；
   记下 CG 输出的**确切字段名拼写**；
2. 顺手确认该字段日频取数有值（CG 生成代码跑一下 2026-06 一个月）；
3. 标的选定（头部各 2 只即可，你定）：300（如 510300/510330）、500（如
   510500/512500）、1000（如 512100/159845）——记下选定的 6 只代码；
4. 把字段加进 `D:\wind_gateway\config.yaml` 的 `fund_nav_daily` fetcher
   `wsd_fields`（此文件你独占维护）→ `/admin/reload` 或重启生效。
   Linux 侧 writer 加列 + 表加列（DDL）到时我来。

### F. （独立，运维）futures_daily 日更断点
`public.futures_daily` 止 **2026-04-29**（断 ~2 个月）。写入方=spread_analyzer
`fetch_prices` 管道——排查其 scheduler 是否停跑。恢复后基差轴（carry-deepening）
的日更信号才有生产意义（历史段回测不受影响）。

## 数据到位后的开工顺序（下一轮）

1. **杠杆轴信号研究**（两融增速/占成交比——design §2 面4，P1.5 首位）：
   brainstorm 定信号形态 → rotation probe 三关闸门模板直接复用（含 partial_rank_ic
   独立增量检验）→ 过闸才进空头引擎装配（dual_legs_external_short 编排器现成）；
2. 备选并行：涨停温度计轴（零数据等待，随时可启）。
