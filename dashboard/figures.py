"""仪表盘图形层：DataFrame → plotly Figure（dataviz 规范）。

色板 = dataviz 参考色板（validate_palette.js 已验证 PASS）：类别双系列
slot1 蓝 + slot2 青；单系列子图统一蓝（身份由子图标题承担，无图例）。
量纲不同的指标一律小倍数堆叠子图，不做双轴。文本用文本色，不用系列色。
"""
from __future__ import annotations

import pandas as pd
import plotly.graph_objects as go
from plotly.subplots import make_subplots

# ---- dataviz 参考色板（light mode，已过验证脚本）----
S1 = "#2a78d6"        # 类别 slot1 蓝
S2 = "#1baf7a"        # 类别 slot2 青（对比度 WARN → 图例+悬浮值缓解）
UP = "#e34948"        # A 股涨色（仅状态 chip 圆点，恒配文字标签）
INK = "#0b0b0b"
INK2 = "#52514e"
MUTED = "#898781"
GRID = "#e1e0d9"
BASELINE = "#c3c2b7"
SURFACE = "#fcfcfb"
FONT = 'system-ui, -apple-system, "Segoe UI", sans-serif'


def _chrome(fig: go.Figure, title: str, height: int) -> go.Figure:
    """统一 chrome：面色/隐性网格/墨色字/横向图例/x-unified 悬浮。"""
    fig.update_layout(
        paper_bgcolor=SURFACE, plot_bgcolor=SURFACE, height=height,
        font=dict(family=FONT, color=INK2, size=12),
        title=dict(text=title, font=dict(color=INK, size=15), x=0.01),
        margin=dict(l=56, r=20, t=48, b=36),
        hovermode="x unified",
        legend=dict(orientation="h", yanchor="bottom", y=1.0, xanchor="right", x=1.0,
                    font=dict(color=INK2)),
    )
    fig.update_xaxes(gridcolor=GRID, linecolor=BASELINE, zeroline=False,
                     tickfont=dict(color=MUTED))
    fig.update_yaxes(gridcolor=GRID, linecolor=BASELINE, zeroline=False,
                     tickfont=dict(color=MUTED))
    for ann in fig.layout.annotations:  # 子图标题
        ann.font = dict(color=INK2, size=12, family=FONT)
    return fig


def _line(x, y, name, color, pct=None, unit="", showlegend=True):
    """2px 线 trace；pct 给出时悬浮附 250d 分位；单系列子图 showlegend=False
    （子图标题即身份，图例只留双系列子图）。"""
    hover = f"%{{y:{unit}}}" if unit else "%{y:.4f}"
    kw = dict(x=x, y=y, name=name, mode="lines", showlegend=showlegend,
              line=dict(color=color, width=2))
    if pct is not None:
        return go.Scatter(customdata=pct,
                          hovertemplate=hover + "（分位 %{customdata:.0%}）", **kw)
    return go.Scatter(hovertemplate=hover, **kw)


def fig_style_meter(df: pd.DataFrame) -> go.Figure:
    """② 风格测量仪：成长/价值累计净值双线 + 信号化位置副图。"""
    fig = make_subplots(rows=2, cols=1, shared_xaxes=True, row_heights=[0.62, 0.38],
                        vertical_spacing=0.08,
                        subplot_titles=("累计净值（窗口起点=1，U2 行业中性纯风格）",
                                        "信号化位置（20d40z+sm5）"))
    fig.add_trace(_line(df.index, df["growth_index"], "成长腿", S1), row=1, col=1)
    fig.add_trace(_line(df.index, df["value_index"], "价值腿", S2), row=1, col=1)
    fig.add_trace(_line(df.index, df["signal"], "纯风格信号", S1, showlegend=False),
                  row=2, col=1)
    fig.add_hline(y=0.0, line=dict(color=BASELINE, width=1), row=2, col=1)
    fig.update_yaxes(range=[-1.05, 1.05], row=2, col=1)
    return _chrome(fig, "风格测量仪 · 成长 vs 价值（纯风格轴）", 460)


def fig_thermometer(df: pd.DataFrame) -> go.Figure:
    """③ 涨停温度计小倍数：占比/炸板率/溢价（量纲不同，各行独立轴）。"""
    fig = make_subplots(rows=3, cols=1, shared_xaxes=True, vertical_spacing=0.07,
                        subplot_titles=("涨停占比", "炸板率", "涨停次日溢价"))
    fig.add_trace(_line(df.index, df["lu_ratio"], "涨停占比", S1,
                        pct=df["lu_ratio_pct"], unit=".2%"), row=1, col=1)
    fig.add_trace(_line(df.index, df["burst_rate"], "炸板率", S1,
                        pct=df["burst_rate_pct"], unit=".1%"), row=2, col=1)
    fig.add_trace(_line(df.index, df["lu_premium"], "涨停溢价", S1,
                        pct=df["lu_premium_pct"], unit=".2%"), row=3, col=1)
    fig.add_hline(y=0.0, line=dict(color=BASELINE, width=1), row=3, col=1)
    fig.update_layout(showlegend=False)  # 单系列子图，标题即身份
    return _chrome(fig, "涨停温度计 · A 股短线情绪（悬浮见 250d 分位）", 520)


def fig_margin(df: pd.DataFrame) -> go.Figure:
    """④ 杠杆小倍数：两融余额 / 融资买入占成交比 / 20d 增速。"""
    fig = make_subplots(rows=3, cols=1, shared_xaxes=True, vertical_spacing=0.07,
                        subplot_titles=("融资余额（沪+深，万亿元）", "融资买入占成交比",
                                        "余额 20 日增速"))
    fig.add_trace(_line(df.index, df["balance"] * 1e4 / 1e12, "融资余额", S1,
                        unit=".2f"), row=1, col=1)
    fig.add_trace(_line(df.index, df["buy_ratio"], "占成交比", S1,
                        pct=df["buy_ratio_pct"], unit=".1%"), row=2, col=1)
    fig.add_trace(_line(df.index, df["bal_growth20"], "20d 增速", S1,
                        unit=".1%"), row=3, col=1)
    fig.add_hline(y=0.0, line=dict(color=BASELINE, width=1), row=3, col=1)
    fig.update_layout(showlegend=False)
    return _chrome(fig, "杠杆 · 两融（T+1 公布口径）", 520)


def fig_energy_breadth(amt: pd.Series, amt_pct: pd.Series, br: pd.DataFrame) -> go.Figure:
    """⑤ 能量+广度：成交额（分位悬浮）/ %>MA 双线 / 新高新低差。"""
    fig = make_subplots(rows=3, cols=1, shared_xaxes=True, vertical_spacing=0.07,
                        subplot_titles=("全市场成交额（万亿元）", "个股 > 均线占比",
                                        "20 日新高 − 新低（占比差）"))
    fig.add_trace(_line(amt.index, amt / 1e12, "成交额", S1,
                        pct=amt_pct.reindex(amt.index), unit=".2f",
                        showlegend=False), row=1, col=1)
    fig.add_trace(_line(br.index, br["pct_above_ma20"], "> MA20", S1, unit=".0%"),
                  row=2, col=1)
    fig.add_trace(_line(br.index, br["pct_above_ma60"], "> MA60", S2, unit=".0%"),
                  row=2, col=1)
    fig.add_trace(_line(br.index, br["hi_lo_diff20"], "新高−新低", S1, unit=".1%",
                        showlegend=False), row=3, col=1)
    fig.add_hline(y=0.0, line=dict(color=BASELINE, width=1), row=3, col=1)
    return _chrome(fig, "能量与广度 · 成交额水位 + 参与面", 520)


def fig_placeholder(text: str) -> go.Figure:
    """降级占位（如 PG 不可达时的杠杆面板）。"""
    fig = go.Figure()
    fig.add_annotation(text=text, showarrow=False, font=dict(color=MUTED, size=14))
    fig.update_xaxes(visible=False)
    fig.update_yaxes(visible=False)
    return _chrome(fig, "杠杆 · 两融", 200)
