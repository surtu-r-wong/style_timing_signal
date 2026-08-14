# 2026-07-24 stock_financial ann_date 修复记录

## 做了什么

Debian 主端 `market_monitor.stock_selector.stock_financial`，单事务 UPDATE **144 行**：
`ann_date := legal_disclosure_deadline(end_date)`（Q1→4-30 / H1→8-31 / Q3→10-31 / 年报→次年 4-30）。

- 范围：B3 preflight 窗口内（end_date 2003-01-01..2023-12-31，csmar ≤ CSMAR_END）且
  `ann_date < end_date` 的全部行 —— income 92 / balance 48 / cashflow_indirect 4，共 101 只票
- 为什么这个值：CSMAR 存的 ann_date 是数据集批次日，这 144 行批次日早于期末（物理不可能）。
  两条消费管线（b3 校验器、stock_selector `fetch_financial_facts` 的 `MIN(stored, legal)`）
  都以法定披露上限为可用性口径；填上限 = b3 校验通过 + 顺带消掉 stock_selector 侧这些行的
  lookahead（原先 MIN 取到了不可能的早日期）
- 同步：`stock_selector` 在同步链上，sync-worker 传 UPDATE（触发器已刷 updated_at），
  Pi5 无需手动操作

## 回滚

`backup_144_rows.csv` 含全部旧值。回滚 = 按 (ts_code, end_date, statement_type) 把
`old_ann_date` UPDATE 回去（同样单事务、rowcount 必须 =144）。

## 第二批（同日晚些）：csmar 43 行已修，wind 132 行查清后**有意不修**

- **csmar 43 行**（2024-06/09/12 各季，同法修复）：`backup_43_rows.csv`。
  至此**全表 CSMAR 脏行清零**。
- **wind 132 行 = 不是脏数据，是 Wind 网格口径**：全部是非自然年财年（6 月年结为主）的
  港股半年报公司。经 gateway 现场重取 `stm_issuingdate`（`backup_wind_refetch.csv`，132 行
  全 SKIPPED），Wind 今天返回的披露日与库里逐字相同——即 Wind 把财年中报（期末 12-31、
  次年 2 月披露）挂在财年年中的日历季槽位（如 0016.HK 槽位 2025-06-30 ↔ 披露 2025-02-27）。
  处置：**保持原样**。理由：① B3 已将 .HK 整体排除出样本池，这些行不再进 B3 校验器；
  ② 改写会与 Wind 真值矛盾，污染 stock_selector 的 HK 数据域。
  ⚠️ 留给 stock_selector 的口径问题：这些槽位行的**数值**属于哪个财报期（财年 H1 还是日历
  半年）需要在其 HK 财务消费方里定义清楚；若其管线未来加 ann≥end 约束，要先解决网格映射。
