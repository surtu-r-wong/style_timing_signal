"""全日历回测引擎。

口径（对应设计稿 §0 / §3.2 Step 0）：
- T 收盘出信号 → T+1 生效成交：`pos_eff = position.shift(1)`。
- 全日历：空仓日收益 = 0，计入序列（不剔除），指标分母是完整日历。
- 换手成本：`cost_bps`/边 × |有效仓位变化|，落在变化生效那天。
- carry：`carry` 为**年化贴水率**（正=贴水）。持多在贴水中赚 carry、持空付 carry：
  `carry_ret = pos_eff × carry / 245`（carry 对齐持仓当日）。
"""
import pandas as pd

ANN = 245


def run_strategy(position: pd.Series, underlying: pd.Series,
                 cost_bps: float = 3.0, carry: pd.Series | None = None) -> pd.DataFrame:
    position = position.astype(float)
    underlying = underlying.reindex(position.index).astype(float)

    pos_eff = position.shift(1).fillna(0.0)                 # T+1 生效
    gross = pos_eff * underlying

    trade = pos_eff.diff().abs()
    trade.iloc[0] = abs(pos_eff.iloc[0])                    # 首日建仓
    cost = cost_bps / 1e4 * trade

    if carry is not None:
        carry = carry.reindex(position.index).fillna(0.0)
        carry_ret = pos_eff * carry / ANN                  # 持仓当日的 carry
    else:
        carry_ret = pd.Series(0.0, index=position.index)

    ret = gross - cost + carry_ret
    return pd.DataFrame({"ret": ret, "pos_eff": pos_eff,
                         "gross": gross, "cost": cost, "carry": carry_ret})


def segment_returns(position: pd.Series, underlying: pd.Series,
                    cost_bps: float = 3.0, carry: pd.Series | None = None):
    """拆多头段/空头段：各自只保留一个方向的仓位，跑同一引擎。

    返回 (long_only_ret, short_only_ret) —— 用于多空分段评价（设计稿 §1.3/§3.2）。
    """
    long_only = position.clip(lower=0)
    short_only = position.clip(upper=0)
    long_ret = run_strategy(long_only, underlying, cost_bps, carry)["ret"]
    short_ret = run_strategy(short_only, underlying, cost_bps, carry)["ret"]
    return long_ret, short_ret
