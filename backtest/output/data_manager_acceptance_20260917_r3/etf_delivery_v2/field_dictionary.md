# etf-share-flow-v2 字段字典（2026-09-17 修正版；与 v1 差异见文末）

## etf_share_daily.csv（原始层，一基金一交易日一行，上市后）
| 列 | 来源字段 | 含义 / 单位 |
|---|---|---|
| fund_id | ts_code | 交易所代码（`510500.SH / 512100.SH / 159845.SZ`；`.OF` 形式返回逐位相同） |
| trade_date | — | 交易日 |
| total_shares | unit_tradable | **场内份额 = 合计份额**（ETF 无场外份额类；`unit_nontradable` 仅封闭期非空），单位：份 |
| unit_nontradable | unit_nontradable | 上市后恒空，原样保留 |
| unit_nav | nav | 单位净值，元/份 |
| nav_date | NAV_date2 | 净值对应日期（绝大多数 = trade_date；节假日前后有 1–10 天滞后） |
| close / volume / amount | close / volume / amt | 二级市场收盘价（元）/ 成交量（份）/ 成交额（元） |
| purchase_status / redemption_status | fund_pchmstatus / fund_redmstatus | 开放申购 / 暂停申购 / 封闭期 / 未成立 … |
| source / fetched_at | — | `wind:wsd`，经网关 `/fetch/fund_nav_daily` 借道，2026-09-17 14:48–14:50 CST 一次取得 |

## share_translation_events.csv（份额折算事件）
`factor` = 折算后份额 / 折算前份额。`event_source=official` 来自 Wind `fund_fundsharetranslationdate/ratio`（**静态、只给最近一次**）；
`detected` 来自数据：份额环比偏离 1 超过 8% 且 份额环比 × 净值环比 与 1 的偏差 < 3%。同日两路以 official 为准。

## net_flow_daily.csv（派生层）
| 列 | 定义 |
|---|---|
| net_new_shares | shares_t − shares_{t−1} × event_factor_t（非事件日因子 1） |
| est_net_flow_cny | net_new_shares × (unit_nav_{t−1} / event_factor_t)：新单位份额 × 新单位前日净值；**一级市场份额变动的估算代理，不是实际申赎金额** |
| event_adjusted / event_source / event_factor | 当日是否按折算换算过前日份额 |
| large_move | 净值连续但份额单日变动 > 20% —— 是真申赎（或未识别的事件），**请当条件看**，不是错 |
| flow_note | 首日 / 日历内缺观测的原因；差分不跨缺日 |

## pre_listing_rows.csv
上市前（未成立 / 封闭期）的行，含认购期 `unit_nontradable`；不进派生。

## v2 相对 v1
1. 折算日金额乘 `unit_nav_{t−1}/f`（v1 乘 `unit_nav_{t−1}`，单位不相容）。
2. `event_source=detected` 的折算日 `net_new_shares` / `est_net_flow_cny` 置空，`flow_note` 说明不可独立辨识（v1 给的是机械 0）。
3. 其余行逐位相同。
