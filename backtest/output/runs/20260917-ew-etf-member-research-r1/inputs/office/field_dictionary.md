# ew-etf-member-features-v1 字段字典

## etf_fund_features.csv（基金层，日历行从各基金首观测起）
| 列 | 含义 |
|---|---|
| previous_date / previous_shares | 日历上紧邻的前一交易日及其份额（缺则本日 a 无效） |
| event_factor | 折算因子 f（普通日 1；官方折算日为 Wind 比例；detected 日为份额比） |
| net_new_shares | v2 交付的 N |
| a | N / (previous_shares × f)：**折算后单位**的标准化份额变动 |
| e5 / window_start / window_end | 最近 5 个连续交易日 a 的均值及窗口；5 个全有效才有值 |
| daily_valid / feature_valid / invalid_reason | a 有效 / e5 有效 / 原因（no previous session · detected translation day · net_new_shares missing · non-positive denominator · fewer than 5 consecutive valid daily values） |
| event_source / large_move | 沿 v2 |

## etf_features.csv（来源层）
`ETF500 = e5(510500.SH)`；`ETFPOOL = 均值(ETF500, ETF1000)`，`ETF1000 = 均值(e5 512100.SH, e5 159845.SZ)` 两只都须有效。`fixed_members` 固定成分；`valid_member_count` 当日有效成员数（ETFPOOL 满分 3）。

## member_product_features.csv（产品层，IC 自 2015-04-16、IM 自 2022-07-22）
| 列 | 含义 |
|---|---|
| selected_contract / selection_date / selection_oi | 在「t 日上市 ∩ t−1 日上市」集合里按 **t−1 日 OI** 最大选出的合约、所用 OI 日与值 |
| eligible_contracts / selection_tie / selection_valid | 候选集（JSON）；并列（按代码升序取首）；选择是否有效（首日无 t−1、已上市合约缺前日 OI、全零 → 无效） |
| long20 / short20 / balance | 所选合约持买 / 持卖榜前 20 名数量和；b = (L−S)/(L+S)；任一榜不足 20 名、ID 重复、缺榜 → 无效，不换约不补零 |
| m5 / window_start / window_end / contract_changed_in_window | b_t − b_{t−5}，要求 t−5..t 六日同一合约且 b 全有效 |
| board_valid / feature_valid / invalid_reason | 榜有效 / m5 有效 / 原因 |

## member_features.csv
`MEMBER_IC = m5(IC)`；`MEMBER_POOL = 均值(m5 IC, m5 IM)`，两者有效，自 IM 上市起。

## price_features.csv
`r5 = P_t / P_{t−5} − 1`，P 为 ew-oi-v1 交付的 `index_close`（IC→000905.SH，IM→000852.SH）；`POOL` = 均值(IC, IM)，自 IM 上市起。IC 2015-01-05..04-15 无 IC 产品行 → r5 无效（价格取自已交付的产品层，不另取指数）。

## calendar.csv
2015-01-05..2026-09-16 全部自然日 + `sfe`（上期所日历，与中金所交易日逐日重合，见 ew-oi-v1）。

## 单位与时点
份额：份；持仓：手（单边）；b、m5、a、e5、r5 无量纲。ETF 时点保守假设 T+1 早间；会员榜单中金所收盘后公布、办公室采集时间 2026-09-17；价格沿 ew-oi-v1。
