# 指数板块级资金流向回填（Wind wset marketmoneyflows → stock_selector.index_money_flow）

立项：2026-09-03 用户裁「做」（资金流向面，预登记 `docs/plans/2026-09-03-money-flow-axis-prereg.md`）。
用户给定 Wind 调用：`w.wset("marketmoneyflows","startdate=…;enddate=…;frequency=day;sector=csi_300;securitytype=A股")`。

## 通路（三段）

| 段 | 位置 | 状态 |
|---|---|---|
| 网关端点 `/fetch/market_money_flow` | stock_selector 分支 `feat/index-money-flow` `2e31f2e`（worktree `.worktrees/index-money-flow`）；部署件已 scp 到 Windows `D:\deploy_stage\moneyflow\{endpoints.py,config.yaml}` | **待用户部署重启**（见下） |
| 表 `stock_selector.index_money_flow` | migration 055（同分支），**已应用 test schema + Debian prod**（schema_migrations 2026-09-03 15:48） | 建成、空表 |
| 取数/灌数 | 本目录 `fetch_money_flow.py` / `load_to_db.py` | 待网关上线后跑 |

## 为什么不能从 ssh 直接跑 WindPy

已实测：ssh 会话里 `w.start()` 返回 `-40520004`（登录失败）——Wind 只在交互桌面会话可用，网关正因此叫 `start_gateway_interactive`；
计划任务 / WMI 在该机已被 360 封（memory `ops-b3-windows-execution-box`）。所以只能走网关加端点 + 用户重启。

## 部署步骤（Windows 机，需 Wind 终端在线）

`D:\deploy_stage\moneyflow\endpoints.py` = 现网运行版（md5 `cfa355b5…`，含今晨 /fetch/futures）**+ 新端点**；
`config.yaml` = 现网 `D:\wind_gateway\config.yaml`（UTF-8 CRLF）**+ `fetchers.market_money_flow` 段**（插在 index_constituent 前）。
两文件 md5：endpoints `8af916c2988f6e7d44f47ba4fd11e522`，config `a576e908a1d2cffc6b5bdfd2a310b592`。

1. 备份：`copy D:\wind_gateway\endpoints.py D:\wind_gateway\endpoints.py.bak.20260903_premoneyflow`，config 同理。
2. 覆盖：`copy /Y D:\deploy_stage\moneyflow\endpoints.py D:\wind_gateway\` 与 `config.yaml` 同理（**用 copy，不要用编辑器另存**——避免再次写成 ANSI）。
3. 重启网关（`start_gateway_interactive.ps1`）。
4. 验证（开发机）：`python data_fixes/2026-09-03-index-money-flow/fetch_money_flow.py --probe` → 四指数 MATCH。

## 取数 / 灌数

```bash
python data_fixes/2026-09-03-index-money-flow/fetch_money_flow.py --probe          # 定 sector 代码
python data_fixes/2026-09-03-index-money-flow/fetch_money_flow.py --start 2014     # 逐指数逐年 → raw/
python data_fixes/2026-09-03-index-money-flow/load_to_db.py --dry-run              # 体检（恒等式/只数/对账）
python data_fixes/2026-09-03-index-money-flow/load_to_db.py                        # UPSERT + load_receipt.json
```

额度估计：4 指数 × ~3,080 日 × 12 列 ≈ **14.8 万格**（中证2000 若只有 2023 起则 ≈ 12 万格）。实花见 `fetch.log` 末行 `total_cells`。

## 回滚

- 表：stock_selector `data/schema/rollback/055_index_money_flow_rollback.sql`（DROP + 清登记行；单端表无同步链四步）。
- 网关：`copy /Y D:\wind_gateway\endpoints.py.bak.20260903_premoneyflow D:\wind_gateway\endpoints.py`，config 同理，重启。
