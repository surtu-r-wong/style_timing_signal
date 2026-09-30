# 办公室回复 05 · 折算日金额单位已修（v2），v1 原件保留

> 日期：2026-09-17 ｜ 对应你方回函 04 ｜ 交付 `delivery/etf-share-flow-v2/`（v1 目录原样保留）

## 1. 你方指出的两点都对

1. **单位不相容**：`net_new_shares` 是折算后单位，v1 却乘了折算前的 `unit_nav_(t−1)`。v2 改为 `net_new_shares × (unit_nav_(t−1) / f)`，即新单位份额 × 新单位前日净值。三条官方折算重算后**与你方同单位检查式逐位相同**：

| 基金 | 日期 | v1 | v2 |
|---|---|---:|---:|
| 159845.SZ | 2023-02-28 | 332,949,518.83 | **445,331,540.49** |
| 510500.SH | 2022-08-26 | 41,327,958.40 | **36,081,996.87** |
| 512100.SH | 2022-09-02 | 14,896.97 | **40,752.22** |

其余 7,009 个非事件日 v1/v2 逐位相同（差异 0）。新增用例「只折算不申赎 → 净新增 0、金额 0；折算叠 100 份申购 → 金额 = 100 × 旧净值 / f」，把 v1 旧算法放回去该用例变红。

2. **510500 的 2015-04-14（detected）**：因子取自当日份额比，净新增份额机械为 0，不是「当日无申赎」的证据。v2 把该日 `net_new_shares` / `est_net_flow_cny` 置空，`flow_note` 写「detected translation … flow not independently identifiable」，`event_adjusted=True`、`event_source=detected` 保留推断身份。

## 2. 更正回函 03 的一句叙述

159845.SZ 2023-02-28：调整后净新增份额 **+161,484,876.72 份，是净申购**，不是我写的「叠着大额赎回」。当日原始份额环比 0.8265 高于折算因子 0.7476，份额降得比折算少，差额就是净申购。原句作废。

## 3. v2 文件指纹

| `etf_share_daily.csv` | `999285055f470b9d…` |
| `net_flow_daily.csv` | `db329d9f53d91b05…` |
| `pre_listing_rows.csv` | `ab55b602ec1f5249…` |
| `share_translation_events.csv` | `679d755968b4865a…` |

`field_dictionary.md` 已同步（金额公式、detected 日置空）；`manifest.json` 记 `contract_version=etf-share-flow-v2`。原始层 `etf_share_daily.csv` 内容不变（指纹与 v1 相同）。
