# B3 连续市值×风格交互与市场状态分解设计

- 日期：2026-07-13
- 状态：已按首轮书面审阅修订，等待复核
- 范围：方向 C 的 B3 研究与历史影子准入裁决
- 当前生产对照：equal_weight long-flat

## 1. 决策背景

原路线把 B3 定义为“市值层 × 风格层”双排序，用类似 Fama–French 的四角组合观察不同市值层中的成长—价值关系。B2 的行业中性自建篮子没有战胜 equal_weight long-flat，随后复盘据此写成“B2 切主失败，B3 自然终止”。

这个终止理由不成立。B2 检验的是“行业中性的纯风格读数能否直接替换生产信号”，B3 检验的是独立命题：

1. 风格效应是否随市值连续变化；
2. size × style 交互是否包含现有指数对没有完整表达的信息；
3. 同样的成长强于价值，在成长和价值同时上涨、同时下跌、方向分化时，是否具有不同的宽基择时含义。

B1 已经提供了支持继续检验而不是终止 B3 的证据：U2 与 equal_weight 的信号相关约 0.91，而 U4 约 0.63，说明市值异质性不可忽略。该差异不是 B3 成立的证明，但足以推翻“B2 失败即可终止 B3”的推理。

生产基线仍保持不动。经 2026-07-11 carry 修正，equal_weight long-flat 的全样本 headline 为 Sharpe 1.62、MaxDD -16.7%；对称多空为 Sharpe 1.41、MaxDD -29.3%。B3 必须在同一执行口径下挑战这个基线。

## 2. 目标与非目标

### 2.1 目标

本轮同时完成四个目标：

1. **结构测量**：识别 style 主效应、size 主效应和 size × style 交互，判断交互方向是否跨阶段稳定，并用硬排序曲面检查连续线性近似是否失真。
2. **生产挑战**：构造一个统一信号和一套 500/1000 分目标信号，在冻结参数、同成本、同 carry、同时间对齐下挑战 equal_weight long-flat。
3. **市场状态分解**：把成长强于价值拆成双方同涨、双方同跌和方向分化三种互斥状态，检验状态信息是否在现有 equal_weight 之外仍有增量。
4. **留下可复现裁决**：无论结果为 DATA_BLOCKED、COVERAGE_BLOCKED、STOP、MEASURE_ONLY 或 PASS_SHADOW，都输出完整、机器可读的证据链。

### 2.2 非目标

本轮不做以下事项：

- 不重新优化 B1/B2 的因子定义或分桶比例；
- 不把硬分桶中表现最好的格子变成新策略；
- 不加入 IPCA、树模型、神经网络、多项式或事后发现的新状态；
- 不扫描 20 日、40 日、5 日平滑等现有信号参数；
- 不用 2024—2026 的结果选择模型、改系数、改状态或改阈值；
- 不改 backtest/production.py，也不替换当前生产输出；
- 不在历史检验通过后直接上线，历史通过最多取得影子运行资格。

## 3. 预注册决策

下列决策在实现前冻结：

| 项目 | 固定口径 |
|---|---|
| 形成频率 | 每月最后一个交易日形成，下一交易日生效 |
| 风格输入 | 沿用 B1 的成长减价值综合分，不增加新基本面因子 |
| 市值输入 | 总市值对数，取负后标准化；数值越大表示越小盘 |
| 行业 | CITIC 一级；2021 年起 PIT 月度快照，之前用最早快照静态外推 |
| 截面缩尾 | 5% / 95%，沿用 B1 约定 |
| 组合权重 | 暴露绝对值比例加权，单票每腿上限 1%，每腿至少 100 只 |
| 月内持有 | 初始权重随个股收益自然漂移，不做日度等权再平衡 |
| 动量处理 | 20 日累计 → 40 日过去窗口 z-score → tanh(z/2) → 5 日平滑 |
| 发现期 | 2014-01-01 至 2020-12-31 |
| 确认期 | 2021-01-01 至 2023-12-31 |
| 第二验证窗 | 2024-01-01 至运行时可用末日；只报告，不参与选择 |
| 未来 OOS | unified 至少 120 个交易日；dual-target 至少 252 个交易日 |
| 生产候选 | B3_unified、B3_dual_target，仅两个 |
| 执行 | T 日收盘信号，T+1 收盘执行；单边 3bp；IC/IM 分腿 carry |
| 仓位 | long-flat，信号大于 0 做多，否则空仓 |

所有固定值写入 signals/style_basket/b3_config.yaml。CLI 只允许指定运行阶段、数据截止日和输出目录，不允许覆盖研究参数或裁决阈值。

## 4. 总体架构

    PIT 基本面、行业、市值和价格面板
                    |
                    v
       B3-A 连续正交暴露与条件风格组合
          |              |             |
          |              |             +--> 2×3 / 5×5 硬排序审计
          |              +----------------> 截面收益曲面
          +-------------------------------> style / size / interaction 轴
                    |
                    v
       B3-B 成长腿、价值腿与市场状态分解
          |         |          |
         UU        DD         DIV
                    |
                    v
       B3-C M0/M1、partial IC 与结构闸门
                    |
                    v
       B3-D unified / dual-target 同秤回测
                    |
                    v
       DATA_BLOCKED / COVERAGE_BLOCKED / STOP / MEASURE_ONLY / PASS_SHADOW

硬排序、连续组合和截面回归是三种相互校验的观察方式，不是三个可自由择优的策略。连续正交组合是唯一主表示；硬排序只负责显示非线性和边界效应；截面回归只负责验证收益曲面的方向与稳定性。

## 5. 数据合同与前置闸门

### 5.1 月末截面

每个形成日需要以下字段：

- ticker、formation_date；
- B1 style_score 及其底层因子可用性；
- total_market_value；
- CITIC 一级行业；
- 财务信息可知日、可知日来源和真实首披日覆盖标志；
- 后续复权收盘收益、交易状态；
- 500、1000 现金指数收益以及 IC、IM carry。

有两个截面集合：

1. **size universe**：形成日仍在样本中的 A 股，具备有效总市值和行业标签。m、m_perp 与目标市值坐标在该集合上计算。
2. **model universe**：size universe 中同时具备合法 style_score 的股票。style_score 的缩尾标准化、s_perp、h_perp 及所有风格组合在该集合上计算。

这一区分保证早期财务覆盖不足时，500/1000 的市值坐标仍由完整市场位置决定，而不是被“可打风格分的股票”改变。

### 5.2 法定日滞后敏感性与真实首披日缺口

已核实的 CSMAR ann_date 大量是数据集批次/导出日而非真实首披日；当前 reader 对其取 min(库存 ann_date, 法定截止日)。因此以下两条都建立在同一个“法定日近似”上：

- 主口径：CSMAR 历史段按法定披露截止日近似，Wind 段使用可用的真实公告日期；
- 滞后一月口径：CSMAR 报告只有到“法定截止日之后的下一个月末”才可进入组合，Wind 仍使用真实公告日期；
- 不允许逐股或逐期选择两个口径中更好的一条。

这两条只检验“在同一近似上额外滞后一个月”的敏感性，不能修复或排除真正晚披股票在两个口径里共享的前视。无翻转只能说明结论对这一月滞后不敏感，不能称为严格 PIT 验证。

以下任一核心结论在两个口径间翻转，判为 DATA_BLOCKED：

- size × style 平均交互系数变号；
- M1 相对 M0 的确认期增量由正变非正；
- 任一生产候选的历史准入结论由通过变不通过或反之。

真正取得 PASS_SHADOW 前，必须从 Wind 或等价权威来源补齐真实首披日，并满足：

- 2014—2023 实际进入 style_score 的每一条 CSMAR 财务事实都有可验证的真实首披日；
- 用真实首披日重跑完整暴露、状态、结构和生产裁决；
- 真实首披日结果与法定日近似的 beta_h 方向、M1 相对 M0 增量和候选准入方向一致。

在真实首披日覆盖完成前，可以生成明确标为 approximate-PIT 的 STOP 或 MEASURE_ONLY 研究结果；任何本来会得到 PASS_SHADOW 的候选必须降为 DATA_BLOCKED。approximate-PIT 的负结果不能写入“勿重开”清单。

### 5.3 行业口径

- 2021-01 起按 effective_date 使用 CITIC 一级月度快照；
- 2021 年前使用每只股票最早可用标签静态外推；
- 缺失标签进入明确的 UNKNOWN 类，不按当前行业回填；
- 金融行业 CF/P 屏蔽必须按每个形成日的 PIT 行业执行，不能继续使用当前快照贯穿历史。

交互方向必须在 2021 年前行业静态近似段与 2021 年后行业 PIT 段同号。2014—2020 的状态 partial IC 因参与 M1 拟合，只作样本内诊断；状态增量的独立证据只取 2021—2023。

### 5.4 影子运行的数据前置

当前 SalG 在 2025Q2 之后存在冻结问题。它不影响 2014—2023 的发现与确认裁决，但：

- 2024—2026 的展示必须标明冻结区间；
- 在 SalG 恢复更新且完成拼接连续性检查前，任何历史通过都只能停留在 DATA_BLOCKED。

真实首披日全覆盖也是同级前置。SalG、真实首披日或 carry 任一未就绪时，均不得启动影子运行。

### 5.5 市值坐标校准

目标市值坐标不依赖指数成份选股，但必须确认排名带确实代表目标指数的市值位置。2021 年起，将排名代理与当期真实成份比较：

- q500 代理与真实中证 500 成份的月度 m_perp 中位数，绝对差的时间均值不得超过 0.25 个截面标准差；
- q1000 同理；
- q1000 > q500 至少在 90% 的月份成立，因为 m_perp 越大代表越小盘。

未满足时判 DATA_BLOCKED，先修正“目标坐标”定义，不进入策略比较。

### 5.6 数据新鲜度

- 股票、现金指数、信号和 carry 的运行清单必须记录各自最大交易日；
- 历史同秤比较只运行到共同有效末日，不把 carry 缺失日静默填零并继续延长；
- 当前 futures carry 停在 2026-04-29，影子运行前必须恢复；
- 任何 required formation month 无法合法形成时，必须先按第 5.7 节区分数据缺口与设计覆盖不足，不能统一归为 DATA_BLOCKED，也不能删除该月后继续称为同窗比较。

### 5.7 预飞覆盖审计

在读取任何前瞻收益、目标收益或既有回测产物之前，先对 2014—2023 的每个 required formation month 做预飞审计。主口径和滞后一月口径都要检查：

- size universe 与 model universe 股票数；
- s_perp、m_perp、h_perp、x(qblend)、x(q500)、x(q1000) 每个正负腿的股票数；
- 1% 封顶后能否归一；
- 行业、总市值、style_score 的缺失原因分布；
- q 所需排名与设计矩阵秩。

分类固定如下：

1. **DATA_BLOCKED**：源表、日期、字段或已知 ingestion 缺口使合法截面无法被计算或完整性无法判断。
2. **COVERAGE_BLOCKED**：输入合同完整，但有效市场截面在既定 1% 上限和每腿 100 只约束下不可行，或合法截面的设计矩阵结构性秩亏。

COVERAGE_BLOCKED 在任何收益被读取前终止本轮，不自动降低每腿数量、不放宽权重上限，也不自动移动发现期起点。若要改变起点或约束，必须先修改并重新审阅本规格，避免看过收益后调整样本。

## 6. 连续正交暴露

### 6.1 标准化

形成日 t：

1. 在 model universe 对 style_score、在 size universe 对 log(total_market_value) 分别做 5% / 95% 截面缩尾；
2. 对 size universe 中缩尾后的 log 市值取负并标准化，得到 m；m 越大代表越小盘；
3. 行业回归使用截距加 K-1 个行业虚拟变量，截面股票等权；
4. 每个残差暴露最终除以自身截面标准差，均值为 0、方差为 1。

定义：

    m_perp = resid(m | 1, industry)                         [size universe]
    s_perp = resid(style_score | 1, industry, m)            [model universe]
    h_raw  = winsorize(s_perp * m_perp, 5%, 95%)
    h_perp = resid(h_raw | 1, industry, s_perp, m_perp)     [model universe]

其中 h_perp 是纯 size × style 交互。使用等权 OLS，不加 Ridge，不按回归拟合优度改变模型。

数值闸门：

- s_perp 与行业、m_perp 在 model universe 的绝对样本相关不超过 1e-8；
- m_perp 与行业在 size universe 的绝对样本相关不超过 1e-8；
- h_perp 与行业、s_perp、m_perp 在 model universe 的绝对样本相关不超过 1e-8；
- 设计矩阵秩亏、残差标准差为 0 或出现非有限值时，该形成月无效。

这里的“与行业相关”指残差与每个实际进入回归的行业虚拟变量的归一化内积。

### 6.2 单轴组合

对任一暴露 x，先构造未封顶两腿：

    w_plus_i  = max(x_i, 0)  / sum(max(x, 0))
    w_minus_i = max(-x_i, 0) / sum(max(-x, 0))

然后对每条腿迭代执行 1% 单票封顶，把剩余权重按未封顶的绝对暴露比例重新分配给尚未封顶的股票。若无法同时满足“权重和为 1、单票不超过 1%、至少 100 只”，该月无效。

分别对 s_perp、m_perp、h_perp 生成 style、size、interaction 三条独立轴收益。它们用于结构诊断，不自动进入生产候选。

### 6.3 目标条件风格组合

按形成日总市值降序排名。市值相同时按 ticker 升序打破并列，使排名与输入行顺序无关：

    q500   = median(m_perp | market-cap rank 301..800)
    q1000  = median(m_perp | market-cap rank 801..1800)
    qblend = (q500 + q1000) / 2

对 q 属于 qblend、q500、q1000，定义：

    x_i(q) = s_perp_i + q * h_perp_i

x(q) 表示在目标市值坐标处的条件风格暴露。正腿命名为成长腿 G_q，负腿命名为价值腿 V_q，并使用与单轴组合相同的权重与约束。

排名带只用于定位 q。组合仍在整个 model universe 上连续加权；测试必须证明排名带以外的股票在暴露非零时能够进入组合。禁止把 301—800 或 801—1800 偷换成选股 universe。

### 6.4 月内收益

组合在形成日收盘确定初始权重，从下一交易日收盘收益开始生效，并持有至下一形成日。月内不恢复初始等权，按资产价值自然漂移：

    V_t = sum_i w_i,f * product_(u=f+1..t)(1 + r_i,u)
    R_t = V_t / V_(t-1) - 1

正式停牌且有交易状态记录时，当日价格按不变处理、权重保留；无法由交易状态解释的缺价属于数据错误，不在当日把缺价股票权重重新分给其他股票。单只股票出现无法处理的终止缺价时，相关形成月无效。

## 7. 硬排序与截面收益曲面

### 7.1 硬排序仅作审计

使用同一 model universe 和同一月内持有方法，对 m_perp 与 s_perp 做彼此独立的排序，固定输出：

- 2×3：size 按中位数分两组，style 按 30% / 40% / 30% 分三组；
- 5×5：size 与 style 各按五分位分组；
- 每个格子的等权收益、股票数、行业分布和形成日覆盖；
- 2×3 四角交互差：

      (small-growth - small-value)
      - (big-growth - big-value)

- 5×5 每个市值行的 growth-value 差、相邻市值行之差与连续模型拟合残差。

必须输出所有格子和所有月份。不得根据结果改分位点，也不得把最强格子加入生产候选。
硬排序格子固定等权，不施加连续组合的 1% 单票上限；其收益只作形状审计，不能与连续候选混用。

### 7.2 Fama–MacBeth 风格验证

对每个形成日，使用本形成日的下一交易日至下一形成日的非重叠个股收益做截面 OLS：

    r_i,t+1 = alpha_t
              + beta_s,t * s_perp_i,t
              + beta_m,t * m_perp_i,t
              + beta_h,t * h_perp_i,t
              + error_i,t+1

输出每月系数、时间序列均值、普通 t 值与 Newey–West lag 3 t 值。显著性只作描述；主结构闸门看 beta_h 的方向稳定性，不根据某个 t 值决定增加模型复杂度。

如果 2×3/5×5 显示明显弯曲，而连续交互无法解释，结论写为“线性交互模型失配”。本轮仍不追加二次项、样条或机器学习；这些只能进入新一轮预注册。

## 8. 成长—价值市场状态分解

### 8.1 内部主状态

对每个 q 和交易日 t，保留条件组合两条腿的简单收益 G_q,t 与 V_q,t，转换为对数收益：

    g_q,t = log(1 + G_q,t)
    v_q,t = log(1 + V_q,t)
    d_q,t = g_q,t - v_q,t

定义三个互斥、完备状态：

    d_UU  = d * I(g >= 0 and v >= 0)
    d_DD  = d * I(g <  0 and v <  0)
    d_DIV = d - d_UU - d_DD

含义：

- UU：成长与价值都涨，成长相对更强或更弱；
- DD：成长与价值都跌，成长相对抗跌或跌得更多；
- DIV：一涨一跌，以及一个为零、另一个为负的边界情况。

逐日必须满足：

    d = d_UU + d_DD + d_DIV

绝对误差不得超过 1e-12。因为 d_DIV 按残差定义，这一恒等式只是防实现错误的浮点工程守卫，不构成经济结构证据。状态出现频率只用于诊断，不作为权重。

### 8.2 信号化

每个状态分量先做过去 20 个交易日之和：

    style_up_raw(q,t)   = sum_20(d_UU)
    style_down_raw(q,t) = sum_20(d_DD)
    style_div_raw(q,t)  = sum_20(d_DIV)
    style_total_raw(q,t)= sum_20(d)

四条 raw 序列分别使用只含过去数据的 40 日 z-score、tanh(z/2) 和 5 日简单移动平均，得到 F_U、F_D、F_X、F_T。raw 层保持精确可加；分别非线性标准化后不要求继续可加。

### 8.3 外部市场方向只作稳健性

另按对应现金宽基指数当日收益分成：

- market up：R_target > 0；
- market non-positive：R_target <= 0。

报告内部 UU/DD 结论在外部市场上行和非上行日是否同方向。该切分不产生候选、不参与模型选择，也不替代内部两腿联合状态。

## 9. M0、M1 与生产信号

### 9.1 非重叠月频拟合

在每个形成日取 F_U、F_D、F_X、F_T 的月末值，目标为下一形成期对应现金指数的收益：

- qblend 对应 50/50 的 500 与 1000 现金指数日收益组合；
- q500 对应中证 500；
- q1000 对应中证 1000。

只在 2014—2020 估计：

    M0: R_q,t+1 = alpha + beta_T * F_T,q,t + error

    M1: R_q,t+1 = alpha
                    + beta_U * F_U,q,t
                    + beta_D * F_D,q,t
                    + beta_X * F_X,q,t
                    + error

M0 与 M1 是固定消融对照，不是严格嵌套模型：F_U、F_D、F_X 分别经过非线性标准化后，其和不等于 F_T。因此不使用 likelihood-ratio、F-test 或“多三个自由度后的样本内拟合提升”作为证据。确认期 OOS R-squared 与 IC 只是一道必要筛子；独立增量主要由确认期 partial IC 和同秤生产闸门承载。

2020 年末后系数永久冻结。2021—2023 用冻结系数确认，2024—2026 只报告。模型截距用于拟合诊断，但不进入择时分数，避免把长期正权益风险溢价变成“永久做多”。

为了固定检查系数稳定性，另在 2014—2017 和 2018—2020 各拟合一次同规格 M1。两个子样本模型只用于以下稳定性闸门，不替代 2014—2020 全发现期模型，也不允许择优：

- 两个非零斜率向量 (beta_U, beta_D, beta_X) 的 cosine similarity 必须大于 0；
- 把两个子样本斜率分别应用到 2021—2023 日频特征后，两条分数的 Spearman 相关必须至少为 0.50。

UU、DD、DIV 在发现期和确认期各自至少覆盖 10% 的有效交易日。频率只检查状态是否有足够识别样本，不进入回归权重、信号权重或候选排序。

每日择时分数为：

    score_M0(q,t) = beta_T * F_T,q,t
    score_M1(q,t) = beta_U * F_U,q,t
                    + beta_D * F_D,q,t
                    + beta_X * F_X,q,t

不再对线性组合增加新 z-score、阈值或波动率缩放。M0 是固定消融基线，不是第三个生产候选。

### 9.2 两个固定生产候选

1. **B3_unified**：使用 qblend 的 score_M1，同时控制 IC 与 IM；两腿各占组合 50%。
2. **B3_dual_target**：q500 的 score_M1 只控制 IC，q1000 的 score_M1 只控制 IM；两个净收益腿各占组合 50%。

B3_500 和 B3_1000 的独立表现必须报告，但只是 B3_dual_target 的组成诊断，不能作为额外候选单独挑选。

每日 score 大于 0 时下一交易日持有对应期货多头，否则空仓。两个候选均不与 equal_weight 混合，不做仓位分档或波动率目标。

IM 于 2022-07-22 才上市。q1000 对现金中证 1000 的预测研究在此之前仍有合法目标，但 B3_dual_target 的 1000 执行腿在 2022-07-22 前只能表示为“中证 1000 现货收益 + 0 carry”的反事实历史腿，不能冒充真实可执行的 IM 记录。第 10.2 节因此同时要求完整确认窗的公平 head-to-head 和 IM 上市后的可执行一致性检查。

## 10. 裁决闸门

### 10.1 结构闸门

结构闸门分为公共项和候选项。公共项必须同时满足：

1. beta_h 的时间序列均值在 2014—2017、2018—2020、2021—2023 三段同号；
2. 2021—2023 的 interaction 轴与 style 轴月频收益绝对 Pearson 相关小于 0.80；
3. 交互方向在 2021 年前行业近似段、2021 年后 PIT 段同号；
4. 2×3/5×5 的全格结果已完整生成，没有通过挑格子改变模型。

每个候选还必须单独满足：

1. 冻结系数的 M1 在 2021—2023 相对 M0 的 OOS R-squared 增量大于 0，且月频 Spearman IC 不低于 M0；
2. 控制 equal_weight 月末信号后的 partial Spearman IC 在 2021—2023 合并样本为正，并在三个自然年中至少两个同号；
3. 2014—2017 与 2018—2020 的 M1 斜率向量和确认期分数通过第 9.1 节的稳定性闸门；
4. 对应 q 的 UU、DD、DIV 通过第 9.1 节的最低状态覆盖闸门。

2014—2020 的 partial IC 必须输出并标为 in-sample diagnostic，但不进入任何 AND 闸门，也不被描述为 regime 外独立证据。

候选映射固定如下：

- B3_unified 只按 qblend 对 blend 目标检查候选项；
- B3_dual_target 要求 q500 对 500 目标、q1000 对 1000 目标分别通过全部候选项，不能用一边的强结果抵消另一边失败。

OOS R-squared 定义为 1 - SSE_model / SST_train_mean，其中 SST_train_mean 使用确认期目标收益相对发现期目标收益均值的离差平方和。结构闸门不允许用第二验证窗补救。

### 10.2 同秤生产闸门

同秤事实固定如下：

- load_underlying_returns("blend") = 0.5 × 中证 500 现货日收益 + 0.5 × 中证 1000 现货日收益；
- load_carry("blend") = 0.5 × IC carry + 0.5 × IM carry，缺失腿按 0；
- 引擎把生效仓位作用在现货收益 + carry 上。

因此 equal_weight long-flat、B3_unified 和 B3_dual_target 都交易同一个 50/50 宽基 blend 与同一 IC/IM carry；差别只在持仓信号。

当前 committed backtest/output/baseline_metrics.csv 的确认窗参照行为：

| 基线 | 窗口 | n | 年化收益 | Sharpe | MaxDD | 年化换手 |
|---|---:|---:|---:|---:|---:|---:|
| equal_weight blend long-flat | 2021—2023 | 727 | 11.72% | 1.001 | -13.12% | 8.425 |

全样本同口径换手为 10.635。生产闸门的换手基数必须使用同一确认窗的 long-flat 8.425，而不是全样本 10.635，也不是价差评价的约 21.1。上述数字是可读上下文，不硬编码为裁决输入；正式运行必须在相同日期和最新 manifest 下重算基线并同时报告差异。按当前上下文，+0.10 Sharpe、2pp MaxDD、1.5 倍换手分别约等于 Sharpe 1.101、MaxDD 不差于 -15.12%、换手不高于 12.638。

仅在结构闸门全部通过后，对 B3_unified 和 B3_dual_target 分别检验完整的 2021—2023：

1. 扣除 3bp 和对应 carry 后，Sharpe 至少比 equal_weight long-flat 高 0.10；
2. MaxDD 相对基线恶化不超过 2 个百分点；
3. 年化换手不超过同一确认窗 equal_weight long-flat 的 1.5 倍；
4. 月频 partial IC 合并样本为正，且至少两个自然年同号；
5. 对候选与基线的配对日收益做 20 日移动块 bootstrap：
   - 5,000 次；
   - 固定随机种子 20260713；
   - 从配对日收益的全部重叠 20 日块中有放回抽样，拼接后截断到原确认期长度；
   - 统计量为 Sharpe 差；
   - 单侧 p = (1 + count(delta_sharpe <= 0)) / 5001；
6. 对 unified 和 dual-target 两个 p 值做 Holm 校正，校正后 p < 0.10。

Holm 的假设族始终固定为两个候选。结构闸门失败的候选原始 p 记为 1，不能通过“先删掉失败候选”缩小校正数量。

所有指标复用 backtest 现有全日历、245 日年化、T+1、成本和 carry 实现。比较必须使用完全相同的交易日交集。

年度集中度不再是 2021—2023 的硬闸门。必须同时报告：

- 2021—2023 三个自然年的 signed P&L、absolute P&L share 和剔除最强年后的指标，不设 40% 截止线；
- 2014—2023 的同类全段诊断，但明确标注 2014—2020 使用了全发现期拟合系数，含样本内成分，不能为确认期失败提供补救。

#### 10.2.1 dual-target 的 IM 上市边界

B3_dual_target 除完整确认窗全部闸门外，还必须在 2022-07-22 至 2023-12-31 的 IM 可执行子窗满足：

1. 与基线的公共交易日至少 252 日；
2. 净 Sharpe 差大于 0；
3. MaxDD 相对同子窗基线恶化不超过 2 个百分点；
4. q500 对 500、q1000 对 1000 的月频 partial IC 分别不为负。

该子窗不另做 p 值或第三次模型选择；它是 dual-target 的必要可执行一致性检查。若完整确认窗通过但该子窗失败或少于 252 日，dual-target 最多为 MEASURE_ONLY，不能由 pre-IM 反事实段取得 PASS_SHADOW。产物必须分别列出 pre-IM、post-IM 和 full-confirmation 三段。

### 10.3 2024—2026 的地位

第二验证窗只追加以下报告：

- 结构系数方向；
- M0/M1 IC 和 partial IC；
- 两候选净值、回撤、换手和年度贡献；
- 两个法定日近似口径的滞后敏感性；
- dual-target 的 pre-IM / post-IM 边界分解。

无论表现更好还是更差，都不得改变历史裁决、系数、候选定义或阈值。

### 10.4 最终状态与优先级

1. **DATA_BLOCKED**：输入数据合同、法定日滞后敏感性或市值坐标校准使历史裁决无法完成；或者候选本来会 PASS_SHADOW，但真实首披日、SalG 或最新 carry 尚未满足影子前置。其优先级最高。
2. **COVERAGE_BLOCKED**：数据合同完整，但预飞审计证明固定的 1%/100 只/形成期约束不可行。它要求先改规格，不等同于数据缺口或经济性失败。
3. **STOP**：数据与覆盖充分，但核心结构闸门失败。approximate-PIT 下的 STOP 必须标为 provisional，不进入“勿重开”清单。
4. **MEASURE_ONLY**：结构成立，但生产闸门失败，或 dual-target 只在 pre-IM 反事实段成立。approximate-PIT 标志必须随行。
5. **PASS_SHADOW**：至少一个预注册候选通过全部结构、生产、Holm 及其适用的 IM 边界闸门，并且影子数据前置全部就绪。

verdicts.csv 同时保存 statistical_verdict 与 final_verdict：前者记录 approximate-PIT 下的结构/经济结果，后者应用真实首披日、SalG、carry 和覆盖前置。这样上游阻断不会抹去研究信息，也不会把“would pass”误报为可启动影子。

如果两个候选都通过，两者都进入影子期，不用历史指标再选一个。

## 11. 影子期

PASS_SHADOW 后才建立影子运行，不改生产仓位：

- B3_unified 至少连续 120 个交易日；
- B3_dual_target 因真实 IM 历史较短，至少连续 252 个交易日；
- M1 系数、信号窗口、状态边界、目标 q、阈值和成本假设全部冻结；
- 每日同时记录 equal_weight 与通过候选的输入日期、分数、拟议持仓、净收益和异常；
- 输入过期时 fail closed，不用旧信号冒充新信号；
- 真实首披日、SalG 与 IC/IM carry 必须在影子期每日通过新鲜度检查；
- 影子期结束后的生产迁移标准另立规格，本设计不预先承诺替换。

## 12. 模块边界

### 12.1 配置

signals/style_basket/b3_config.yaml

- 仅保存无秘密的研究参数、日期窗口、随机种子和闸门；
- 每次运行计算配置哈希并写入 manifest；
- 不读取数据库密码等运行环境秘密。

### 12.2 暴露与组合

signals/style_basket/b3_exposures.py

- 纯函数：缩尾、标准化、行业回归、正交残差、目标 q、权重封顶；
- 输入为单月截面 DataFrame；
- 输出为暴露、权重和约束诊断；
- 不访问数据库、不写文件。

signals/style_basket/b3_portfolios.py

- 纯函数：形成日权重生效、月内买入持有漂移、成长/价值腿和单轴收益；
- 明确处理停牌、缺价与无效月；
- 不负责 z-score、状态或生产回测。

signals/style_basket/b3_states.py

- 纯函数：对数腿收益、UU/DD/DIV 恒等分解、滚动状态特征和外部方向报告；
- 不拟合 M0/M1。

signals/style_basket/b3_build.py

- 先编排不读取前瞻收益的 preflight，再编排法定日主/滞后一月面板、形成日循环和研究缓存；
- 复用 B1 因子计算能力，但只有 provenance 与策略完全匹配时才复用旧缓存；
- 不改变现有 B1/B2 文件和 committed 输出。

### 12.3 结构与生产评价

backtest/b3_structure.py

- 2×3、5×5、Fama–MacBeth 风格回归、M0/M1、partial IC 和结构闸门；
- 输出结构证据，不执行期货持仓。

backtest/b3_eval.py

- 生成 unified、dual-target 的 long-flat 持仓；
- 复用现有 engine/data/metrics 基础设施，不复用 significance.bootstrap_pvalue；
- 新增配对 20 日 moving-block Sharpe 差 bootstrap 与两候选 Holm 校正；
- 完成成本、carry、IM 上市边界、report-only 年度集中度和最终 verdict。

现有 significance.py 做的是同换手仓位循环移位置换，零假设为“仓位与收益无预测对齐”；B3 新检验重采样候选与基线的配对日收益，回答“候选 Sharpe 是否高于同秤基线”。两者零假设不同，函数和产物不得共用名称。

不在本阶段创建 b3_shadow.py。只有历史 verdict 为 PASS_SHADOW 且用户审阅结果后，才为影子运行另写计划，避免提前搭建未必会使用的生产设施。

## 13. 产物合同

大体量中间文件放 output/style_basket/b3/，默认 gitignored：

| 文件 | 内容 |
|---|---|
| monthly_exposures.csv.gz | 每月股票级 s_perp、m_perp、h_perp、x(q) 与权重 |
| coverage_audit.csv | 在读取收益前生成的逐形成月、逐暴露正负腿数量与 DATA/COVERAGE 分类 |
| exposure_diagnostics.csv | 样本数、秩、正交误差、最大权重、有效股票数、q 与失败原因 |
| axis_returns.csv | style、size、interaction 三轴日收益 |
| conditional_leg_returns.csv | qblend/q500/q1000 的 G、V 两腿日收益 |
| state_components.csv | raw 状态分量、标准化特征、状态频率及外部方向标签 |
| hard_sort_surface.csv | 2×3 和 5×5 全格全月结果 |

紧凑裁决产物放 backtest/output/b3/：

| 文件 | 内容 |
|---|---|
| structure_coefficients.csv | 月度 beta_s、beta_m、beta_h 及分段汇总 |
| model_comparison.csv | M0/M1 的 discovery/confirmation/report 指标 |
| production_metrics.csv | 基线与两个候选的 full-confirmation、pre-IM、post-IM 同秤指标 |
| yearly_contribution.csv | 2021—2023 与 2014—2023 的 report-only 年度 P&L、集中度及 in-sample 标志 |
| bootstrap.csv | 原始 p、Holm 校正 p 与置信区间 |
| verdicts.csv | 每道数据、覆盖、结构、生产闸门，以及 statistical/final 两层状态 |
| run_manifest.json | 数据末日、配置哈希、代码 commit、输入文件哈希、真实首披日覆盖、IM 边界与无效月份 |

只有紧凑、可复核的最终研究产物在研究完成并经用户确认后考虑提交；股票级缓存不进入 Git。

## 14. 失败处理

统一 fail closed：

- 输入列缺失、主键重复、形成日不单调：立即失败；
- 预飞阶段发现源表、字段、日期或 ingestion 不完整：DATA_BLOCKED；
- 输入完整但单腿少于 100 只、1% 封顶无法归一、设计矩阵结构性秩亏：COVERAGE_BLOCKED；
- 通过预飞后再出现残差方差为零或正交误差越界：视为实现/数值错误，整次运行失败，不生成经济 verdict；
- q 所需排名不足、真实指数校准失败：DATA_BLOCKED；
- G 或 V 小于等于 -100%、对数收益非有限、状态恒等式失败：立即失败；
- 非停牌原因缺价、终止缺价无法处理：DATA_BLOCKED；
- z-score 过去窗口不足时输出 NaN；不得用未来数据补齐，正式评价从所有策略共同有效日开始；
- required formation month 无效时不得删除月份后计算“完整确认期”，必须保留 DATA_BLOCKED 或 COVERAGE_BLOCKED 的原始原因；
- carry 末日落后时历史比较截到共同末日，影子期禁止启动；
- M0/M1 回归失败时不退回简单总风格信号冒充候选；
- 任一失败必须在 coverage_audit.csv、exposure_diagnostics.csv、verdicts.csv 和 run_manifest.json 的适用文件中留痕。

## 15. 测试设计

### 15.1 暴露单元测试

- 已知行业与市值结构的截面能恢复正确残差；
- s_perp、m_perp、h_perp 满足均值、方差和正交误差；
- 股票顺序打乱不改变按 ticker 对齐的输出；
- 1% 封顶权重和为 1，最少 100 只，封顶迭代确定性；
- 不可行截面明确失败，不静默放宽上限；
- q1000、q500 的方向和校准规则正确；
- 排名带外股票能进入 x(q) 组合，证明排名带不是选股 universe；
- 法定日主/滞后一月政策只改变其合法可知日之后的形成月；
- 两个近似口径无翻转时，真实首披日缺失仍阻止 PASS_SHADOW；
- preflight 在任何目标收益或既有 backtest 产物被读取前完成；
- 相同薄截面分别因缺源数据和合法股票不足触发 DATA_BLOCKED、COVERAGE_BLOCKED。

### 15.2 组合与状态单元测试

- 次日才计收益，形成日收益不进入；
- 一个两股票例子精确验证买入持有权重漂移，不退回日度等权；
- 停牌零收益保留权重，无法解释的缺价触发失败；
- UU、DD、DIV 覆盖双方同涨、同跌、相反、零边界全部情况；
- d = d_UU + d_DD + d_DIV 在浮点容差内逐日成立；
- 20 日、40 日和 5 日处理只访问当前及过去数据；
- 修改未来数据不能改变历史任何信号。

### 15.3 结构测试

- 合成“无交互”数据时 beta_h 接近零且不能凭空通过结构闸门；
- 合成“正交互”数据时连续回归和硬排序四角恢复同方向；
- 合成 UU 有效、DD 无效的数据时 M1 在冻结确认窗优于 M0；
- M0/M1 不走严格嵌套模型检验，样本内拟合提升不能单独过闸；
- 早/晚发现期斜率向量反向或确认期分数相关低于 0.50 时，稳定性闸门失败；
- 任一状态在发现期或确认期覆盖不足 10% 时，候选失败且不得给稀疏状态加权补救；
- 2021 年后的目标收益变化不能反向修改 2014—2020 系数；
- 2014—2020 partial IC 被标为 in-sample diagnostic 且不影响 verdict；
- 2024—2026 数据变化不影响任何历史选择字段；
- partial_rank_ic 控制 equal_weight 的计算与现有 helper 一致；
- 全格输出测试阻止只保存最佳格子。

### 15.4 回测与裁决测试

- B3 与 equal_weight 使用相同 T+1、3bp、carry 和共同交易日；
- unified 对 IC/IM 使用同一个分数，dual-target 使用两个目标分数；
- 2022-07-22 前 dual-target 1000 腿被标为 counterfactual，之后才标 executable；
- dual-target post-IM 少于 252 日或方向闸门失败时不能 PASS_SHADOW；
- unified 影子期阈值为 120 日，dual-target 为 252 日；
- 单独 500/1000 指标不增加 Holm 假设数；
- 20 日移动块 bootstrap 固定种子可复现；
- 配对 moving-block 检验与 significance.py 的仓位循环移位置换在合成零假设下产生不同、各自正确的结果；
- Holm 两假设校正使用原始 p 值，边界 p=0.10 的通过/失败行为固定；
- MaxDD 恶化 2pp、同窗 long-flat 换手 1.5 倍的边界行为固定；
- 年度集中度字段不参与 PASS/FAIL，修改其值不能改变 verdict；
- DATA_BLOCKED > COVERAGE_BLOCKED > STOP > MEASURE_ONLY > PASS_SHADOW 的判定优先级固定。

### 15.5 回归保护

- 现有 tests/test_style_basket.py、B1/B2 committed 产物和 equal_weight 生产输出保持不变；
- 新 B3 测试拆为 exposures、portfolios/states、structure、eval 四组；
- 固定小型合成夹具覆盖两个行业、大小盘、成长价值和三种市场状态；
- 全测试套通过后仍需用字节或哈希回归确认现任生产 CSV 未漂移。

## 16. 推荐运行顺序

    python -m signals.style_basket.b3_build --stage preflight
    python -m signals.style_basket.b3_build --stage exposures
    python -m signals.style_basket.b3_build --stage portfolios
    python -m signals.style_basket.b3_build --stage states
    python -m backtest.b3_structure
    python -m backtest.b3_eval

preflight 不得读取前瞻收益；只有其通过后才允许后续阶段读取收益。每一步读取上一步 manifest 并验证配置哈希和数据末日。任一阶段不匹配就拒绝复用缓存。完整运行可以提供 --stage all，但内部仍按上述顺序执行和落盘。

## 17. 设计完成标准

本设计只有在以下条件均满足后才可以进入实施计划：

1. 用户确认本文准确表达了已讨论的五部分设计；
2. 文中不存在未决事项或可由实现者自行选择的核心统计口径；
3. 生产候选始终只有 unified 和 dual-target 两个；
4. 2024—2026 明确保持只报告；
5. 生产代码与影子设施明确不在首轮实现范围。

实施必须另行使用 writing-plans 形成按测试驱动拆分的执行计划。本规格的提交不授权直接开始编码。
