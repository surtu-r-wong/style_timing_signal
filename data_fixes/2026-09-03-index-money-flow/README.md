# 板块级资金流向回填（Wind wset `marketmoneyflows` → `stock_selector.index_money_flow`）

> 2026-09-16管理交接：历史回填统一由data_manager执行，本目录脚本与下述复跑命令保留作历史审计，不再由本项目执行。办公室已续补原三板块至09-15，共24行，消费侧核验见[续接记录](../../docs/plans/2026-09-16-equal-weight-independent-legs.md)。旧fetch脚本以index_daily作为日历，在价格数据落后时会漏掉真实交易日；办公室及本次验收使用public.trading_calendar。以下09-03行数/覆盖描述保留其历史时点。


2026-09-03 收官。表 = migration `055_index_money_flow.sql`（单端，不入跨端同步链）。

## 通路

| 段 | 状态 |
|---|---|
| 表 | Debian 正式库 + test 库均已建，`schema_migrations` 已登记 |
| 网关端点 `/fetch/market_money_flow` | 已部署上线（stock_selector 分支 `feat/index-money-flow`；用户 09-03 18:0x 重启网关生效） |
| 取数 `fetch_money_flow.py` | 已跑完，7,403 行 |
| 灌库 `load_to_db.py` | 已跑完，五道硬闸全过，`load_receipt.json` |
| 板块指数日线 `fetch_board_index.py` | 已跑完，399102.SZ + 000680.SH 共 4,701 行入 `index_daily` |

**为什么必须走网关**：ssh 会话里 `w.start()` 返回 `-40520004`、所有 wset 回 `-103`——WindPy 只在交互桌面会话可用；
schtasks/WMI 又被 360 拒。当时为WSET新增端点并由用户重启。2026-09-16已通过现有通用WSS端点查询其他指数，新源不必一律新增端点。

## 三条只有实测才知道的事实

**① 当时WSET仅验证通过三个板块，不能推断所有Wind通路只有三个。** 约50个候选（包括`csi_500`、`中证500`、`000905.SH`等）返回`-40521008`；通过的是`csi_300`、`chinext`、`star`及对应中文写法。这是本次参数探测范围内的结论，不是完整供应商白名单证明。2026-09-16经已有WSS端点，已取得500、1000、2000、50、800的开盘/尾盘净流入；详见[新接口核查与分来源结果](../../docs/plans/2026-09-16-ew-flow-source-results.md)。

**② Wind 静默截断到窗口末尾约 62~66 行，不报错。** 整年请求只回最后一个季度（2015 全年请求回的是
2015-09-30..12-31 共 62 行），用户那次 3 个月导出恰好 65 行正是踩在上限上。
故取数按 **2 个月分块**，并逐窗用 `index_daily` 的交易日历硬校验覆盖；窗口交易日数一旦 ≥ `TRUNC_CAP=60` 直接中止
（低于该上限时前段缺失只能是真实无数据，如科创板 2019-07-22 才开板，不能与截断混为一谈）。

**③ 字段间只有一条恒等式成立。** 全样本 7,403 行：
- `main_in − main_out ≡ extra_bill + large_bill`：**零违反**，max 误差 2.8e-07 ✓
- `maininflowmoney == main_in − main_out`：1,882 行（25%）不成立，p99原始数值约65,000、max约79,000
- 四档净流入之和 == 0：3,007 行（41%）不成立，p99原始数值约29,000

初稿据用户那 65 行样本归纳的「四档零和 / main = in − out」不是全局真的。
研究口径因此取机械自洽的 `(in − out)/(in + out)` 作分子，Wind 的 `maininflowmoney` 列**照原样入库但不用于构造**。

## 覆盖与口径

| index_code | Wind sector | 含义 | 资金流 | 收益率指数 |
|---|---|---|---|---|
| 000300.SH | `csi_300` | 沪深300 | 2015-01-05..2026-09-03，2,837 行 | 库内已有 |
| 399102.SZ | `chinext` | 创业板（全体，1,400 余只） | 2015-01-05..2026-09-03，2,837 行 | 补入创业板综，2014-01 起 |
| 000680.SH | `star` | 科创板（615 只） | 2019-07-22..2026-09-03，1,729 行 | 补入科创综指，**基期 2019-12-31** |

`index_code` 取该板块**自己的**收益率指数：创业板用**创业板综** 399102.SZ 而非创业板指 399006.SZ（后者只 100 只，
与板块口径不符）；科创板同理用科创综指。两条新腿不进日更 topup——`topup_guard` / `check_freshness` 的代码列表来自
固定的 `load_code_map()`、不扫表，故不会误报，但也意味着**这两条腿不会自动更新**，要用得手动重跑本目录脚本。

数据倍率：与用户导出的 CSV 65 天逐列对账，比值 [1−2e−14, 1+1e−14] → **原始文件与终端导出倍率 1**。

2026-09-16接口单位复核：用户说明所指金额为元；此前据此把WSET文档也改为元不够严谨。新增WSS（`unit=1`）与WSET的沪深300开尾盘值，在6日期12项抽查中均为10,000倍，支持对应WSET开尾盘原值以万元表达。原文件与终端导出倍率1不能证明两接口同单位。创业板/科创全板与对应指数WSS在换算后仍有差异，不能直接互换；重取WSET的16项样本则与旧文件一致。未修改库值；旧比值对所有相关金额统一换单位不敏感，但这不覆盖通路混用或统计范围差异。详见[字段说明](../../docs/plans/2026-09-16-money-flow-field-definitions.md)与[接口对账](../../docs/plans/2026-09-16-ew-flow-source-results.md)。

## 额度实花

`fetch.log` 累计 **78,444 格**（含第一次未分块跑浪费的 23,652 格）+ 板块指数日线 49,312 格 + 探针约 2,000 格。
初稿按四指数估的 14.8 万格作废（板块少了一个、但分块使请求数增加）。

## 复跑

```bash
python3 data_fixes/2026-09-03-index-money-flow/fetch_money_flow.py --probe        # 验通路
python3 data_fixes/2026-09-03-index-money-flow/fetch_money_flow.py --start 2015   # 已存在的窗自动跳过
python3 data_fixes/2026-09-03-index-money-flow/load_to_db.py --dry-run            # 看体检
python3 data_fixes/2026-09-03-index-money-flow/load_to_db.py                      # UPSERT
python3 data_fixes/2026-09-03-index-money-flow/fetch_board_index.py               # 板块指数日线
```

## 回滚

`data/schema/rollback/055_index_money_flow_rollback.sql`（DROP INDEX/TABLE + 删 `schema_migrations` 行）。
`index_daily` 里新加的两条腿：`DELETE FROM stock_selector.index_daily WHERE index_code IN ('399102.SZ','000680.SH')`。
