"""hybrid20 两步夜更脚本的共享层：阈值、状态机与相对强弱因子。

为什么要这个模块（2026-10-12）：update_growth_stability.py（链路步骤 1，主信号）与
update_confirmed_signal.py（步骤 2，金融确认）此前各自逐字复制同一份 N_LIST/M、
四个状态机阈值与 make_signal 状态机。两层每晚同链运行，将来调阈值只改一处会
静默失同步——确认层会拿与主信号不同的阈值判同一个因子，committed CSV 无任何报错。
常量、状态机与 spread→z→tanh 因子收敛到这里，两个脚本只留各自的接线与输出。
"""
from __future__ import annotations

import numpy as np
import pandas as pd

N_LIST = [20, 60]       # 收益率回看窗口
M = 250                 # z-score标准化滚动窗口

OPEN_LONG = 0.35        # 开多阈值
CLOSE_LONG = 0.1        # 平多阈值
OPEN_SHORT = -0.15      # 开空阈值
CLOSE_SHORT = -0.1      # 平空阈值


def relative_factor(df: pd.DataFrame, long_col: str, short_col: str, n: int, m: int = M) -> pd.Series:
    """spread_N = ln(长(t)/长(t-N)) - ln(短(t)/短(t-N)) → 滚动 z（窗口 m）→ tanh(z)。"""
    spread = np.log(df[long_col] / df[long_col].shift(n)) - \
             np.log(df[short_col] / df[short_col].shift(n))
    roll_mean = spread.rolling(m, min_periods=m).mean()
    roll_std = spread.rolling(m, min_periods=m).std()
    z = (spread - roll_mean) / roll_std
    return np.tanh(z)


def make_signal(factor: pd.Series) -> pd.Series:
    """状态机: 非对称阈值 + 状态保持"""
    signal = pd.Series(np.nan, index=factor.index)
    state = 0
    for i, v in enumerate(factor.values):
        if np.isnan(v):
            continue
        if state == 0:
            if v > OPEN_LONG:
                state = 1
            elif v < OPEN_SHORT:
                state = -1
        elif state == 1:
            if v < CLOSE_LONG:
                state = 0
                if v < OPEN_SHORT:
                    state = -1
        elif state == -1:
            if v > CLOSE_SHORT:
                state = 0
                if v > OPEN_LONG:
                    state = 1
        signal.iloc[i] = state
    return signal
