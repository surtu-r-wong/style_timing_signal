# 2026-09-02：000820.SZ 两季营业收入回填（清除 B3 `SALG_FRESHNESS` blocker）

用户裁决：「回填做一下」（2026-09-02 下午，在 r4 正式重跑裁决之后）。

## 问题

B3 真首披正式重跑 r4（`data_fixes/2026-09-02-b3-true-disclosure-formal/`）最终 verdict
`DATA_BLOCKED`，唯一 run blocker 是 `SALG_FRESHNESS`：`salg_valid_through = 2020-04-30`。
足迹只有 000820.SZ 一只票：CSMAR 利润表 2020-03-31 与 2021-03-31 两行**没有**
`B001100000`（营业总收入）/ `B001101000`（营业收入）两个键，其余科目齐全。
一季度收入缺失使 TTM 断 4 季、12 季斜率窗再断 11 季，SalG 从 2020-04 一直卡到 2024 年底。

## 取数（Wind，只读）

`/fetch/financial_snapshot`（wss，`rptType=1;unit=1`），字段 `tot_oper_rev, oper_rev, stm_issuingdate`，
原始返回存 `wind_fetch.json`：

| end_date | Wind tot_oper_rev / oper_rev | CSMAR B001100000 | 角色 |
|---|---|---|---|
| 2020-03-31 | 0.0 / 0.0 | 缺键 | 目标 |
| 2020-06-30 | 3,233,442.48 | 3,233,442.48 | 口径对照，逐分一致 |
| 2021-03-31 | 0.0 / 0.0 | 缺键 | 目标 |
| 2021-06-30 | 1,277,610.61 | 1,277,610.61 | 口径对照，逐分一致 |

结论：**这两季营业收入真实为 0，CSMAR 对零值省略了键**。Wind `stm_issuingdate`
（2020-04-30 / 2021-04-29）与库内 `stock_first_disclosure` 的首披日逐日一致。

## 修法与理由

- B3 的 `_fetch_raw_financial` 与共享 `financial_reader` 都按 `end_date <= CSMAR_END`
  只读 `csmar` 行；`stock_financial` 主键是 `(ts_code, end_date, statement_type)`，
  `scripts.load_wind_quarterly` 也明确拒写 CSMAR_END 之前的 wind 行。所以唯一可行的修法是
  **原地给这两条 csmar 行的 JSON 补键**：`B001100000 = 0.0`、`B001101000 = 0.0`。
- 行内加溯源标记 `_backfill_2026_09_02`（来源、Wind 首披日、原因、本记录路径）。两边的
  CSMAR 字段映射都是白名单提取（`{m[k]: v ... if k in m}`），未知键对所有读取端不可见。
- `ann_date` 不动（读取端本来就 cap 到法定截止日；B3 走真首披表）。`updated_at` 置 now()。
- 只写 Debian 主库 `100.65.111.79`（B3 与 WSL 工作树读的就是它）。幂等：已有键的行跳过。

## 执行记录

`apply_fix.py`：`--dry-run` 先跑一遍确认补丁内容 → 正式跑 UPDATE 2 行（16:47:38 CST）→
读取端验证 `translate_data(data, "csmar", "income")["revenue"] == 0.0` 两行均通过。
`db_snapshot_*.json`：`after` 为修后原样，`before_reconstructed` 由补丁键反推（补丁是纯键合并，反推精确）。
首次正式跑因验证函数参数写错在 UPDATE 提交后抛异常，快照由随后的幂等重跑补写；库内内容与本记录一致。

## 下游

按冻结规格重跑 B3 全流程 r5（preflight→exposures→portfolios→states→structure→eval），
见 `data_fixes/2026-09-02-b3-true-disclosure-formal-r5/`。注意：SalG 在 exposures 阶段计算，
所以**不能**只重跑 structure→eval（r4 裁决记录里那句「只需重跑 structure→eval」是错的，已在 r5 记录中更正）。

## 同类发现（→ 已于同日晚按用户裁决处理：`data_fixes/2026-09-02-csmar-zero-revenue-batch/`，Wind 明确为 0 的 11 行已回填，Wind 亦空的 32 行未动待裁）

全市场 csmar 利润表季行中「有净利润键、无 `B001100000`」的还有 43 行 / 13 只票（修复前含 000820.SZ 为 45 行 / 14 只；2018-03-31 ~ 2025-03-31，
修复前按月份 3 月 22 / 6 月 8 / 9 月 9 / 12 月 6 行），机制大概率相同（零收入省键）。这些票不触发 run blocker
（blocker 只看最后一个 formation 的 SalG 时效），但各自的 SalG 在缺口后 11 个季度内为 NaN。
是否统一回填由用户决定；取数成本约 45 × 2 个 wss 格。

| ts_code | 缺键季度 |
|---|---|
| 000996.SZ | 2024-03-31 |
| 002473.SZ | 2021-03-31 |
| 600145.SH | 2018-03-31 |
| 600421.SH | 2018-03-31 |
| 600610.SH | 2018-03-31, 2018-09-30, 2018-12-31, 2019-03-31, 2019-06-30, 2019-09-30 |
| 688091.SH | 2021-09-30 |
| 688192.SH | 2022-03-31, 2022-06-30, 2022-09-30, 2022-12-31, 2023-03-31, 2023-06-30 |
| 688197.SH | 2023-03-31, 2024-03-31 |
| 688266.SH | 2019-12-31, 2020-03-31, 2020-06-30, 2021-03-31 |
| 688302.SH | 2022-03-31, 2022-06-30, 2022-09-30, 2023-03-31, 2023-06-30, 2023-09-30, 2023-12-31, 2024-03-31, 2025-03-31 |
| 688382.SH | 2022-09-30, 2022-12-31, 2023-03-31 |
| 688520.SH | 2021-03-31, 2021-06-30 |
| 900906.SH | 2018-03-31, 2018-09-30, 2018-12-31, 2019-03-31, 2019-06-30, 2019-09-30 |

## 回滚

```sql
UPDATE stock_selector.stock_financial
SET data = data - 'B001100000' - 'B001101000' - '_backfill_2026_09_02', updated_at = now()
WHERE ts_code = '000820.SZ' AND statement_type = 'income' AND data_source = 'csmar'
  AND end_date IN ('2020-03-31', '2021-03-31') AND data ? '_backfill_2026_09_02';
```
