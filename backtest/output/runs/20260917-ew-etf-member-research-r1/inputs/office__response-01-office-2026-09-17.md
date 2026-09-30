# 办公室回复 01 · ew-etf-member-features-v1 已交付

> 日期：2026-09-17 ｜ 办公室 → style_timing_signal ｜ `delivery/ew-etf-member-features-v1/`（六件按 `feature_contract.json` 列顺序；manifest 逐文件 SHA-256；源三份交付的 SHA-256 记在 manifest.sources）

| 文件 | sha256 前 16 |
|---|---|
| `etf_fund_features.csv` | `904845db8b408321` |
| `etf_features.csv` | `7998ad749e744bd7` |
| `member_product_features.csv` | `86c478438fae5750` |
| `member_features.csv` | `5c867b7b8c68ab18` |
| `price_features.csv` | `d16270dce984f6e2` |
| `calendar.csv` | `a2dad611a0c028b6` |

**范围 / 算法版本**：端点 2026-09-16，日历 2015-01-05 起；`ew-etf-member-features-v1`，脚本 `data_manager/scripts/ew_etf_member_v1.py`（12 条手算测试、4 变异体全杀，见处置 §1）。

**首次有效日**：ETF500 2015-01-12 · ETFPOOL 2021-04-07 · MEMBER_IC 2015-04-24 · MEMBER_POOL 2022-08-01 · r5 IC 2015-04-23 · r5 POOL 2022-07-29。
分年与前后半期覆盖、无效原因计数在 `quality_report.json`；产品层每日的候选集、前日 OI、并列、换约与缺榜原因在 `member_product_features.csv`。

**按 spec 的三个硬点**：选约只用 t−1 OI 且候选限「t 与 t−1 都上市」；榜单任一不足 20 名 → 无效、不换次主力、不补零；m5 要求 6 日同一合约——IC 因此约 19% 日无效（125 次换约），**按你方要求报告覆盖、不放宽**。

**时点假设**：ETF T+1 早间（未证明）；会员榜单公布时点 ≠ 办公室采集时间；价格沿 ew-oi-v1。

**一处需你方知悉**：r5_IC 用 ew-oi-v1 的 `index_close`，IC 产品行自 2015-04-16 起，故 2015-01-05..04-15 的 r5 无效；spec 说「可复用办公室 OI 交付」，我按此办；若要那 67 天需另取 `index_daily`（不在本函）。

验收有出入追 `response-02-style-timing-signal-<日期>.md`。
