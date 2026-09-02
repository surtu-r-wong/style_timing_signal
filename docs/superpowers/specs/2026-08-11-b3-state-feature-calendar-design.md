# B3 State Feature Calendar and Zero-Variance Design

## 背景

2026-08-11，B3 正式 campaign 第一次走到 structure 阶段。equal_weight
输入溯源已经部署并通过远端目标测试，但 structure 在消费父级
`state_components.csv` 时 fail closed：

```text
DATA_BLOCKED: model state components.F_X must be finite and nonmissing
```

只读诊断确认这是一个独立于 equal_weight 的既有设计缺口：

- structural formation calendar 有 111 个形成日，从 `2014-10-31` 到
  `2023-12-29`；
- states 日频从 `2014-11-03` 才开始；
- 冻结的 20 日 raw、40 日 z-score、5 日平滑需要完整因果暖启动，六个
  policy/q 组合首个全部有限的形成日是 `2015-01-30`；
- `2014-11-28` 和 `2014-12-31` 的四个模型特征对全部六个组合都不完整；
- `F_X` 还在 `2015-03-31`、`2015-09-30`、`2016-05-31` 出现缺失，原因是
  前五个平滑日内曾出现完整的 40 日常量窗口。现实现把零标准差转换成 NaN，
  NaN 随后污染 5 日平滑；
- 2021--2023 确认期不存在这些缺失。

中证 1000 在 2014-10-17 才发布，因此不能用真实 q1000 数据构造更早的模型
预热历史。合成填充或非因果回填都会改变研究对象，不可接受。

## 目标

1. 明确区分 structural calendar 与 model calendar。
2. 保留 2014-10 起的 exposure、hard-sort 和结构完整性审计。
3. 将 M0/M1 的有效 discovery 起点冻结为 2015-01。
4. 将完整窗口内的零方差状态分量定义为中性 z-score 0，同时保留真正暖启动
   或数据缺口的 NaN。
5. 保持 fail-closed、确定性输出、父子 manifest 哈希绑定和失败原子性。
6. 复用已经验证的 preflight、exposures、portfolios 父产物，只重建 states 及其
   下游。

## 非目标

- 不修改 equal_weight 的 `factor_value` 定义、文件内容或已部署的输入溯源契约。
- 不为 q1000 制造 2014-10 之前的代理历史。
- 不缩短 20/40/5 因果窗口，不使用未来数据，不前填充或后填充特征。
- 不根据当前文件里的首个非缺失日期动态选择模型起点。
- 不改变确认期、报告期、生产阈值或候选选择规则。
- 不把任意残余 NaN 静默删月后继续拟合。

## 决策一：双日历

### Structural calendar

`signals/style_basket/b3_config.yaml` 中现有的
`windows.discovery = [2014-10-01, 2020-12-31]` 保持不变。它继续定义可观察的
结构研究起点。完整的 111 个形成日用于：

- exposure 与真实指数目标坐标；
- hard-sort surface 的月份和 cell 完整性；
- 结构形成日连续性和父产物覆盖审计。

structure coefficients 继续遵循已冻结的 `2015-2017`、`2018-2020`、
`2021-2023` 和 report-only 分期；2014-10--12 不产生系数行。

### Model calendar

structure 代码新增显式、冻结的 `MODEL_DISCOVERY_START = 2015-01-01`。model
calendar 是 structural formation dates 中不早于该常量的严格子集，正式数据的
首日是 `2015-01-30`。

model calendar 用于：

- 在形成日抽取 `F_U`、`F_D`、`F_X`、`F_T`；
- 构造下一形成期目标收益；
- M0/M1 discovery、early、late、confirmation 与 report-only 样本；
- 模型斜率稳定性和 state coverage 门禁；
- 模型所需的 state/axis 日频网格完整性检查。

模型起点只能来自冻结常量，不能由 `first_valid_index()`、丢弃缺失行或当前数据
快照推导。

### 统一首期窗口

所有影响模型或模型门禁的首期窗口统一为 `2015-2017`。当前残留的
`2014-2017` 重索引逻辑必须改为同一来源，避免对本来不属于 model calendar 的
2014 月份制造假缺失或假 gate failure。

非门禁的全 discovery state coverage 行从 `2014-2020` 改名为
`2015-2020`，使标签与实际样本一致。2014-10--12 只保留在 structural calendar
的 exposure、hard-sort 和结构审计中。

实现应集中定义模型窗口，供模型拟合、beta 稳定性和 coverage 共同消费，避免
多份硬编码再次漂移。

## 决策二：零方差语义

`signals.style_basket.b3_states._causal_transform` 保留现有三层窗口：

1. `raw = component.rolling(20, min_periods=20).sum()`；
2. raw 上的 40 日均值与样本标准差，`min_periods=40`；
3. `tanh(z / 2)` 上的 5 日简单移动平均，`min_periods=5`。

z-score 分支改为：

- 40 日统计未就绪，或窗口内存在真实缺失：z-score 为 NaN；
- 40 日统计完整且标准差 `>= 1e-8`：按 `(raw - mean) / std` 计算；
- 40 日统计完整且标准差 `< 1e-8`：z-score 明确定义为 `0.0`。

这里的 0 表示该状态分量在完整历史窗口内没有可辨识偏离。它正常进入 5 日平滑。
该规则不会把暖启动 NaN 或真实数据缺口伪装成有效观测。

## 数据流与校验

### States

states 阶段使用已验证的 portfolios 父产物重新计算 `state_components.csv`。输出
schema 不变，内容哈希会改变；`states.json` 必须原子更新并绑定新 SHA-256。

states 阶段不得修改 preflight、exposures 或 portfolios 的文件和 manifest。

### Structure

structure 继续先验证四个父 manifest 和文件哈希。随后：

1. 从 exposures 取得完整 structural formation calendar 并验证月度连续性；
2. 用冻结日期派生 model calendar；
3. hard-sort 和结构形成日完整性消费 structural calendar；
4. structure coefficients、state/axis 网格、模型特征、目标和拟合消费从
   `2015-01` 开始的冻结分期；
5. model calendar 内任何非有限模型特征、缺失形成日或 state/axis 网格不一致都
   `DATA_BLOCKED`；
6. 不允许通过 `dropna()`、自动后移起点或候选间不同样本规避阻断。

structure 成功后照常原子写入：

- `structure_coefficients.csv`；
- `model_comparison.csv`；
- `structure_manifest.json`。

manifest 的 `inputs.equal_weight_control` 必须保留已部署的路径、原始文件 SHA-256、
`date` 列和 `factor_value` 列绑定。

### Eval

eval 不新增宽松分支。它必须：

1. 验证 structure manifest 与两个 structure 输出的 SHA-256；
2. 重新加载 equal_weight 文件；
3. 比较 source kind、路径、文件 SHA-256、日期列和数值列；
4. 任一不一致即在构建 evaluation 前 `DATA_BLOCKED`。

最终研究结论可以因为其他已登记门禁成为 `COVERAGE_BLOCKED`，但不得再因为
state 暖启动、完整零方差窗口或 equal_weight provenance 阻断。

## 失败处理与可恢复性

- states 与 structure 继续使用临时文件加原子替换。
- 重跑失败时不能留下指向旧文件或部分新文件的有效 manifest。
- 正式重跑 states 前，记录旧 `state_components.csv` 与 `states.json` 的 SHA-256，
  并在 campaign 外的部署暂存目录保存可恢复副本。
- 不删除旧父产物、正式 run 目录或部署 bundle。
- 若新 states 未通过 schema、哈希或目标测试，停止在 states，不运行 structure。

## 测试策略

### 单元测试

1. 暖启动期仍全部为 NaN，不能被零方差分支提前变成 0。
2. 完整 20/40 窗口内的常量分量得到 z-score 0，完成 5 日平滑后特征为 0。
3. 常量窗口后重新出现非零分量时，形成日不再被此前零方差 NaN 污染。
4. 真实输入缺口继续传播为 NaN 并触发下游阻断。

### Structure 契约测试

1. structural calendar 从 2014-10 开始且保持连续。
2. model calendar 固定从 2015-01 开始。
3. 2014-10--12 缺少模型特征不会进入 M0/M1，也不会被静默算作 2015 样本。
4. 2015 年起任一模型形成日缺失或非有限必须阻断。
5. hard-sort 仍要求 2014-10--12 的完整结构数据。
6. early、beta 稳定性和 state coverage 首期都使用 `2015-2017`。
7. 非门禁全 discovery coverage 行标为 `2015-2020`。
8. 输出在成功重跑间保持字节确定性，写入失败时全部失效。

### 回归与正式核验

1. 本地运行 states/structure/eval 目标测试。
2. 本地运行全仓测试。
3. 部署后通过 `ssh -p 2222` 与 `wsl -e` 在 Windows 工作树运行目标测试。
4. 正式顺序运行 states、structure、eval。
5. 检查 states 与 structure manifest 的输出哈希。
6. 对照 structure manifest 的 equal_weight 路径、列名和 SHA-256 与磁盘文件。
7. 确认 eval 已通过 provenance 验证并进入正常研究门禁。

## 部署边界

本设计不修改全局 config，因此现有 config hash 保持不变。已完成的 preflight、
exposures、portfolios manifest 可以继续作为可信父产物。需要重新生成的最小链路为：

```text
portfolios（复用）
    -> states（重建）
    -> structure（首次成功生成）
    -> eval（下游核验）
```

正式 Windows 仓库继续使用 Windows Git 处理 CRLF 和索引；运行 Python 时使用用户
确认的 `ssh -p 2222 ... wsl -e` 路径及已部署 WSL 虚拟环境。

## 验收标准

- 所有新增与既有目标测试通过；全仓测试无失败。
- 正式 states manifest 为 `OK`，且绑定新 `state_components.csv` 哈希。
- 正式 structure 不再因 2014 暖启动或完整零方差窗口阻断。
- structure manifest 精确记录实际 equal_weight 输入路径、`date`、
  `factor_value` 与文件 SHA-256。
- eval 对同一输入通过 provenance 核验；受控测试中任何文件刷新或列绑定变化都会
  fail closed。
- 2014-10--12 仍在 structural 审计中，且没有进入模型拟合或模型门禁样本。
