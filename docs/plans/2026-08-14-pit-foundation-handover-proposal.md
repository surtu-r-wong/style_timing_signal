# PIT 底座移交 stock_selector —— 立项材料（2026-08-14）

> 由 B3 处置批（2026-08-14）起草的**成品转发文本**：用户可整段复制转发给 stock_selector 线。
> 起草依据：B3 正式跑裁决分析 + 项目盘点 §5；跨线分工由用户转发（纪律）。

### 立项请求：真实首披日 PIT 底座（Wind 逐 fact 首披日回填）

**提出方**：style_timing_signal（B3 研究轴）　**日期**：2026-08-14
**性质**：跨项目移交立项请求。价值主体在 stock_selector，**不占 style_timing_signal 预算**。

#### 一、背景

`stock_selector.stock_financial` 的 `ann_date` 当前取自 CSMAR，而**CSMAR 的 `ann_date` 是数据集
批次/导出日，不是真实首次披露日**。所有依赖该表的下游研究因此共享同一个 PIT 星号：
无法给出"真实可得信息"口径的结论，只能退回"法定披露上限"这类保守近似
（现行 reader 取 `min(库存 ann_date, 法定截止日)`）。

这是**全库级缺陷，不是 B3 的局部问题**。B3 只是第一个撞上它的消费方，不是唯一理由。

已完成的相邻工作（说明这不是从零开始）：
- CSMAR 脏行（`ann_date < end_date`）已清零：144 + 43 行，回滚凭据在
  `data_fixes/2026-07-24-stock-financial-ann-date/`（`backup_144_rows.csv`、`backup_43_rows.csv`）。
- Wind 132 行经 gateway 现场重取 `stm_issuingdate` 逐字相同，判定为 HK 财年网格口径、**有意不修**
  （另见本请求 §六的搭车项）。
- 这两件都只是**清理了不可能值**，没有解决"批次日 ≠ 首披日"这个口径本体。

#### 二、交付物

| # | 交付物 | 说明 |
|---|---|---|
| D1 | fact 级真实首披日字段 | 在 `stock_financial` 上新增/回填可验证的真实首披日（来源 Wind `stm_issuingdate` 或等价权威源），与现有 `ann_date` 并存而非覆盖 |
| D2 | 覆盖率与来源标注 | 每条 fact 标明首披日来源与是否 verified；未覆盖部分保留保守近似并显式标注 |
| D3 | gateway 取数与入库脚本 + 回滚凭据 | 沿用 `data_fixes/<date>-<topic>/` 惯例：README（做了什么/为什么/怎么回滚）+ backup CSV + apply receipt |
| D4 | 跨端同步确认 | Debian primary `100.65.111.79` 写入后 Pi5 backup 同步链确认（DDL 前须过 `market-monitor/migration/SCHEMA_CHANGES.md` 的 DDL 安全快查卡） |
| D5 | 消费方口径公告 | 通知所有 `stock_financial` 下游：新字段语义、切换时点、旧口径何时退役 |

#### 三、量级（来自 B3 正式跑实测，非估算）

- 底层财务事实底座：`stock_selector.stock_financial` 本次消费 **3,721,765 行**
  （出处：`run-windows-formal/backtest/run_manifest.json` 的 `database_source_evidence`，
  窗口 2003-01-01 ~ 2023-12-31）。
- 对应的模型行分母：**626,732 行**（2014-10 ~ 2023-12 每个冻结 PIT 策略 × formation 月）。
- 当前覆盖率：**0 / 626,732 = 0.0**。
  ⚠️ **这个 0 是保守标注规则的结果，不是"PIT 污染率 100%"的测量值**——不要拿它当污染程度读。
- 工程量参照：同类工程（share-capital par 标定，`data_fixes/2026-07-25-share-capital-par/`）
  为**周级**投入。

#### 四、验收标准

沿用 B3 设计稿 `docs/superpowers/specs/2026-07-13-b3-continuous-style-state-design.md:151-155`
预登记的三条（原文口径，未改写）：

1. **覆盖**：2014—2023 实际进入 style_score 的每一条 CSMAR 财务事实都有**可验证的**真实首披日；
2. **重跑**：用真实首披日重跑完整暴露、状态、结构和生产裁决；
3. **一致性**：真实首披日结果与法定日近似的 `beta_h` 方向、M1 相对 M0 增量、候选准入方向一致。

外加数据工程侧三条（本请求补充）：

4. 回滚可执行：按主键 UPDATE 回旧值，rowcount 与 apply receipt 逐字相符；
5. 未覆盖部分显式可查：不得用近似值静默填充而不留标注；
6. 双端一致：Debian 与 Pi5 的该字段抽样比对通过。

#### 五、与 B3 重开条件的关系（**最关键的一段**）

**B3 已于 2026-08-12 按选项 A 关轴归档：STOP + provisional，不进"勿重开"清单。**

> **B3 的唯一重开条件 = 本 PIT 底座建成后，用真实 PIT 数据把 B3 的四路统计重跑一次验证。**
> 除此之外不得以任何其他理由重开。届时若闸门仍不过，B3 升级为终态 STOP。
> （出处：`docs/plans/2026-08-12-project-review-and-priorities.md` §9.3；
> provisional 的强制性来自设计稿 `:142` 与 `:513` 的预登记，收尾写法不是自由选择。）

这句话对 stock_selector 的实际含义有两层，请勿混淆：

- **不要为了给 B3 翻案而立这个项。** B3 正式跑的失败缺口与已观测的口径敏感度
  相差 **10.05 ~ 57.39 倍**（`stability` q500 需 +0.534490 而口径扰动只给出 0.053182；
  qblend 需 +0.565171 而扰动只给 0.009848）。以翻案为目的的期望回报**很低**，
  B3 自己的 verdict 分析已预测重跑后仍 STOP。
- **要为了整个库的 PIT 能力而立这个项。** 惠及所有依赖 `stock_financial` 的下游研究
  （因子回测、选股回溯）；B3 只是它的第一个受益者而非唯一理由。
  原文（verdict 分析 §6.2 选项 C）：「这个权衡属于用户」。

因此：**B3 侧不催、不跟踪、不为此排期。** 若 stock_selector 决定不立项，
B3 就永久停在 provisional STOP + `DATA_BLOCKED` 星号，这是可接受的终态；
星号的解释成本已被 verdict 分析 §4 一次性付清。

#### 六、可搭车的三条小项（低优先，非本立项必需）

| # | 内容 | 触发条件 |
|---|---|---|
| A1 | SalG 5 票营收 TTM 回填或明示豁免：`000820.SZ`、`300431.SZ`、`600145.SH`、`600421.SH`、`600610.SH`（最后 formation 实际只需 `000820.SZ` 1 只） | 顺手则做；受影响模型行 ≤0.13%，**不改任何统计结论**，只洗溯源星号 |
| A3 | SHARES 57 尾巴（`data_fixes/2026-07-25-share-capital-par/tail.csv`，实际 56 只）+ CLOSE 202 行 / 14 只票（`000670.SZ` 28、`000155.SZ` 19、`000995.SZ` 19…，全部 `UNEXPLAINED_EXACT_DATE_GAP`） | style_timing_signal 生产线不消费这两张表，但 stock_selector 选股管线消费；按活跃度优先 |
| A4 | Wind 132 行 HK 财年网格口径定义：这些槽位行的**数值**属于哪个财报期（财年 H1 还是日历半年） | 仅需确认；若 stock_selector 管线未来加 `ann_date ≥ end_date` 约束会直接撞墙。留言在 `data_fixes/2026-07-24-stock-financial-ann-date/README.md:33` |

#### 七、不做的后果

- 任何 PIT 敏感研究都无法给出"真实可得信息"口径的结论，只能继续用保守近似；
- B3 的 `DATA_BLOCKED` 无法解除，其负结论永久带星号；
- 每一次新的 coverage 闸门报 `DATA_MISSING_*` 都要重新解释一遍同一件事。

---

