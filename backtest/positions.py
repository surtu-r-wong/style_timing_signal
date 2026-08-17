"""信号 → 目标仓位映射。

默认 discrete：|signal| > threshold 取符号得 {−1, 0, +1}（对齐双引擎 §1.3 离散仓位）；
threshold=0 即取符号（恰为 0 → 0）。proportional：原值直通（敏感性对照）。

hybrid20 的 hybrid_20 本就是 {−1,0,+1}，discrete(θ=0) 对它是恒等；citic40d/equal_weight
的连续因子经此离散化。
"""
import numpy as np
import pandas as pd


def to_position(signal: pd.Series, mode: str = "discrete", threshold: float = 0.0) -> pd.Series:
    if mode == "proportional":
        return signal.astype(float)
    if mode == "discrete":
        gated = signal.where(signal.abs() > threshold, 0.0)
        return pd.Series(np.sign(gated), index=signal.index).astype(int)
    raise ValueError(f"unknown mode: {mode!r} (expected 'discrete' or 'proportional')")


def to_position_asym(signal: pd.Series, long_theta: float, short_theta: float) -> pd.Series:
    """非对称离散映射（双引擎 §1.3）：signal>long_theta→+1；signal<−short_theta→−1；否则 0。

    long_theta / short_theta 均为非负绝对阈值。short_theta>long_theta 即"空头门槛更高"。
    """
    if long_theta < 0 or short_theta < 0:
        raise ValueError("long_theta / short_theta must be non-negative")
    pos = pd.Series(0, index=signal.index, dtype=int)
    pos[signal > long_theta] = 1
    pos[signal < -short_theta] = -1
    return pos


def crossings(signal: pd.Series, threshold: float = 0.0) -> tuple[pd.Series, pd.Series]:
    """(上穿, 下穿) 布尔序列。上穿 = 前一日 ≤θ 且当日 >θ；下穿反之。

    首日没有前值 → 既不算上穿也不算下穿（要求真实穿越事件，不把"开局就在线上"
    当成一次入场信号）。
    """
    above = signal > threshold
    prev = above.shift(1)
    up = above & (prev == False)      # noqa: E712 —— 首日 prev 是 NaN，需要它判 False
    down = (~above) & (prev == True)  # noqa: E712
    return up, down


def staged_position(raw: pd.Series, smooth: pd.Series,
                    w1: float = 0.4, w2: float = 0.6,
                    threshold: float = 0.0, leg2_refill: bool = False) -> pd.Series:
    """两笔分批 long-flat：快信号试探进场 + 慢信号确认加仓，反向时快信号先减仓。

    规则（用户 2026-08-17 提出）：
      * 笔1（权重 `w1`）：**raw 上穿**开 → **smooth 下穿**平
      * 笔2（权重 `w2`）：**smooth 上穿**开 → **raw 下穿**平

    交易直觉：raw（未平滑）领先 smooth（5 日平滑）约 3 个交易日，于是 raw 负责
    "先探一脚"与"先撤一半"，smooth 负责"确认补满"与"最后离场"。

    ## 三个必须登记的自由度（都不是数据告诉我们的）

    1. **"出现信号" = 穿越事件，不是水平状态。** 只能这么解读：若按水平判定，
       状态 (raw>θ, smooth≤θ) 同时满足"笔1 该开"（raw 在线上）与"笔1 该平"
       （smooth 在线下），自相矛盾。改用事件后仓位**带路径依赖**——同一组
       (raw, smooth) 水平可对应不同仓位，取决于怎么走到这里的。
    2. **同日开平冲突 → 先平后开。** 若某笔的开仓与平仓事件同日触发（raw 上穿
       且 smooth 下穿），先执行平仓再执行开仓，净效果是持有。实测该冲突在
       equal_weight 全历史发生次数见 `staged_entry_probe` 的诊断表。
    3. **事件驱动会"卡死"。** 笔2 被 raw 下穿平掉后，若 smooth 全程不下穿，就再也
       没有 smooth 上穿事件 → 笔2 空置到下一次 smooth 真正走完一轮。这是规则的
       结构性后果而非 bug，诊断里单列"条件成立却空仓"的天数（equal_weight 全历史
       实测 **439 天 / 14.3%**）。

    `leg2_refill=True` 是针对第 3 条的诊断性变体：**raw 重新上穿且 smooth 仍在线上
    时，把笔2 一起加回来**（交易直觉上就是"快信号砍掉的大头，快信号回来时补回"）。
    它消除卡死，用来分清"读数差是思路问题还是这条实现细节的问题"。默认关闭 ——
    默认口径必须忠实于用户原始描述。

    只做多（long-flat），负信号一律不建仓——沿用 `production_position` 的既有裁决。
    """
    if not raw.index.equals(smooth.index):
        raise ValueError("raw 与 smooth 的索引必须逐位一致（同一份信号文件的两列）")
    for name, w in (("w1", w1), ("w2", w2)):
        if w < 0:
            raise ValueError(f"{name} 必须非负，收到 {w}")

    raw_up, raw_down = crossings(raw, threshold)
    sm_up, sm_down = crossings(smooth, threshold)

    hold1 = hold2 = False
    out = []
    for t in raw.index:
        if sm_down[t]:                      # 先平
            hold1 = False
        if raw_down[t]:
            hold2 = False
        if raw_up[t]:                       # 后开
            hold1 = True
            if leg2_refill and smooth[t] > threshold:
                hold2 = True                # 补回被快信号砍掉的大头
        if sm_up[t]:
            hold2 = True
        out.append((w1 if hold1 else 0.0) + (w2 if hold2 else 0.0))
    return pd.Series(out, index=raw.index, dtype=float)


def production_position(signal: pd.Series, threshold: float = 0.0) -> pd.Series:
    """推荐 production 持仓口径 = **long-flat**：signal>threshold→+1，否则 0（砍空头）。

    Phase 3 双引擎 v1 实证：复用风格信号的空头段【无独立盈利 + 无避险价值】——
    equal_weight long-flat Sharpe 1.42 vs 对称 1.39、MaxDD −13.9% vs −30.2%；
    CITIC 轴 T6 阈值扫描 short_sharpe≈0（全 16 组 −0.07~+0.04）独立佐证。
    → 交易这些信号时砍掉空头优于对称多空。连续因子与已离散带空信号皆适用。
    """
    return (signal > threshold).astype(int)
