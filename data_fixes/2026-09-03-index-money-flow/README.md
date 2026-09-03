# 板块级资金流向回填（Wind wset `marketmoneyflows` → `stock_selector.index_money_flow`）

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
schtasks/WMI 又被 360 拒。故新数据源一律「加端点 + 用户重启网关」。

## 三条只有实测才知道的事实

**① `sector` 是白名单枚举，只有三个板块。** 约 50 个候选（`csi_500`/`csi500`/`zz500`/`中证500`/`000905.SH`/
`csi_100`/`csi_800`/`sse_50`/`全部A股`/`sse`/`szse`/`gem`/`sme`…）全部回 `-40521008`；
通过的只有 `csi_300`、`chinext`、`star`，且中文 `沪深300`/`创业板`/`科创板` 同样通过——
**中英文两套写法结论一致，排除拼写问题**。中证500/1000/2000 这张表根本不给。

**② Wind 静默截断到窗口末尾约 62~66 行，不报错。** 整年请求只回最后一个季度（2015 全年请求回的是
2015-09-30..12-31 共 62 行），用户那次 3 个月导出恰好 65 行正是踩在上限上。
故取数按 **2 个月分块**，并逐窗用 `index_daily` 的交易日历硬校验覆盖；窗口交易日数一旦 ≥ `TRUNC_CAP=60` 直接中止
（低于该上限时前段缺失只能是真实无数据，如科创板 2019-07-22 才开板，不能与截断混为一谈）。

**③ 字段间只有一条恒等式成立。** 全样本 7,403 行：
- `main_in − main_out ≡ extra_bill + large_bill`：**零违反**，max 误差 2.8e-07 ✓
- `maininflowmoney == main_in − main_out`：1,882 行（25%）不成立，p99 6.5 万元、max 7.9 万
- 四档净流入之和 == 0：3,007 行（41%）不成立，p99 2.9 万元

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

数据单位：与用户导出的 CSV 65 天逐列对账，比值 [1−2e−14, 1+1e−14] → **倍率 1，无需换算**（万元）。

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
