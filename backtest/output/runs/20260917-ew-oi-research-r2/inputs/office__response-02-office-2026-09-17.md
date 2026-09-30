# 办公室回复 02 · ew-oi-v1 正式交付

> 日期：2026-09-17 ｜ 办公室 → style_timing_signal ｜ 交付目录：`delivery/ew-oi-v1/`

用户批准后已取到 `000905.SH / 000852.SH` 的 2026-09-16 日线（wsd 12 单元格，写入 `stock_selector.index_daily`），按契约终点 **2026-09-16** 重跑并交付：

| 文件 | SHA-256 | 说明 |
|---|---|---|
| `product_daily.csv` | `4cbe348adce71206e391709920c66d11edde9e4a8524110902aef1a53a4616e8` | IC 2,779 + IM 1,009 行，`complete` 全 True |
| `features.csv` | `4000330904f75e4cea19a015521f0efafe110d2ccd4216bff1390f66b91793e6` | IC / POOL 各同行数；`u5` 有效 IC 2,524（2016-04-29 起）、POOL 754（2023-08-09 起） |
| `calendar.csv` | `e71f1333cb557e02da23a592d3e0cd2e8993cffde4c1bb30bffa9db4d0a87983` | 与你方 09-17 只读快照的 `calendar.csv` 指纹**逐位相同** |
| `manifest.json` | — | 含上表、`generated_at_utc`、生成脚本与办公室仓 git HEAD |
| `contract_detail.csv` / `lifecycle_audit.csv` / `raw_inputs/` / `quality_report.json` | 见 manifest `supporting_files` | 合约日明细（含采集时点、来源）、规则集合 vs 主表逐日、四张源表原始字节、质量报告 |

你方 `oi_delivery_contract.py --delivery delivery/ew-oi-v1` 在办公室这边 **PASS**（`last_date 2026-09-16`）。请在你方环境重跑一次。

**口径提醒**（细节在回函 01 与处置 §2）：OI / 成交量单边手数；`index_close` 为 `stock_selector.index_daily.close`；采集时点逐行在 `contract_detail.csv.collected_at_cst`，历史发布时点不冒充；`sfe` 是与 IC 交易日逐日重合的上期所日历。

**接口对齐**（不改契约）：`lifecycle_change_in_previous_five_sessions` = t−5..t−1 内有过集合变化；无效原因除 warmup 外另有「训练窗起点落在原始特征预热期」各 5 天，行保留、`u5` 为空。

**后续**：用户已裁两条指数进 WSL2 日更，之后 `index_daily` 不再需要单独批价；本交付按契约固定在 09-16，续期另函。

验收有出入请追 `response-03-style-timing-signal-<日期>.md`。
