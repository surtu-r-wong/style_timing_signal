"""Dash 薄壳：布局 + 回调（数据/图形逻辑全在 data.py / figures.py）。

启动：python3 -m dashboard.app → http://127.0.0.1:8060
刷新即重读：回调每次触发（含页面加载）重新 data_bundle()——上游批式更新，
无后台轮询。PG 不可达时杠杆面板降级为占位，其余照常。
"""
from __future__ import annotations

import sys
from pathlib import Path

from dash import Dash, Input, Output, dcc, html

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from dashboard import figures as F  # noqa: E402
from dashboard.data import data_bundle, rebase_indices, slice_range  # noqa: E402

PAGE = "#f9f9f7"
CARD = {"background": F.SURFACE, "borderRadius": "8px", "padding": "12px 16px",
        "marginBottom": "14px", "border": "1px solid rgba(11,11,11,0.10)"}
_SIG_LABEL = {"equal_weight": "equal_weight（生产主信号）",
              "hybrid20": "hybrid20", "citic40d": "citic40d"}


def status_bar(signals: list[dict], fresh: dict) -> list:
    """① 状态条：三线 chip（因子值 + long-flat 持仓 + 截止日）+ 数据新鲜度行。"""
    chips = []
    for sig in signals:
        long = sig["position"] == 1
        stale = sig["pos_date"] != sig["date"]
        lines = [
            html.Div(_SIG_LABEL[sig["name"]],
                     style={"color": F.INK2, "fontSize": "12px"}),
            html.Div([
                html.Span(f"{sig['factor']:+.2f}",
                          style={"color": F.INK, "fontSize": "22px",
                                 "fontWeight": "600"}),
                html.Span([
                    html.Span("●", style={"color": F.UP if long else F.MUTED,
                                          "marginRight": "4px"}),
                    "持多" if long else "空仓",
                ], style={"color": F.INK2, "fontSize": "13px",
                          "marginLeft": "10px"}),
            ]),
            html.Div(f"信号截至 {sig['date']:%Y-%m-%d}",
                     style={"color": F.MUTED, "fontSize": "11px"}),
        ]
        if stale:
            lines.append(html.Div(
                f"⚠ 持仓截至 {sig['pos_date']:%Y-%m-%d}，落后信号——"
                "重跑 python3 -m backtest.production",
                style={"color": F.UP, "fontSize": "11px", "fontWeight": "600"}))
        chips.append(html.Div(lines, style={"flex": "1", "minWidth": "180px"}))
    fresh_line = html.Div(
        "数据截至 — " + " · ".join(f"{k} {v:%m-%d}" for k, v in fresh.items()),
        style={"color": F.MUTED, "fontSize": "11px", "marginTop": "8px"})
    return [html.Div(chips, style={"display": "flex", "gap": "12px",
                                   "flexWrap": "wrap"}), fresh_line]


def build_layout() -> html.Div:
    return html.Div([
        html.Div([
            html.H2("风格仪表盘",
                    style={"color": F.INK, "margin": "0 0 2px 0", "fontSize": "20px"}),
            html.Div("今天市场在哪：生产信号 · 风格钟摆 · 情绪温度 · 杠杆水位（展示层，零新信号）",
                     style={"color": F.MUTED, "fontSize": "12px"}),
        ], style={"marginBottom": "12px"}),
        dcc.RadioItems(
            id="range", value="1y", inline=True,
            options=[{"label": t, "value": v} for t, v in
                     (("近 1 年", "1y"), ("近 3 年", "3y"),
                      ("近 5 年", "5y"), ("全部", "all"))],
            style={"marginBottom": "12px", "color": F.INK2, "fontSize": "13px"},
            inputStyle={"marginRight": "4px"}, labelStyle={"marginRight": "16px"}),
        html.Div(id="status", style=CARD),
        html.Div(dcc.Graph(id="g-style"), style=CARD),
        html.Div(dcc.Graph(id="g-thermo"), style=CARD),
        html.Div(dcc.Graph(id="g-margin"), style=CARD),
        html.Div(dcc.Graph(id="g-energy"), style=CARD),
    ], style={"background": PAGE, "minHeight": "100vh", "padding": "20px 24px",
              "fontFamily": F.FONT, "maxWidth": "1200px", "margin": "0 auto"})


app = Dash(__name__, title="风格仪表盘")
app.layout = build_layout


@app.callback(
    Output("status", "children"),
    Output("g-style", "figure"),
    Output("g-thermo", "figure"),
    Output("g-margin", "figure"),
    Output("g-energy", "figure"),
    Input("range", "value"),
)
def refresh(range_key: str):
    b = data_bundle()
    f_margin = (F.fig_placeholder("PG 不可达 — 杠杆面板暂不可用（其余面板不受影响）")
                if b["margin"] is None
                else F.fig_margin(slice_range(b["margin"], range_key)))
    style = rebase_indices(slice_range(b["style"], range_key),
                           ["growth_index", "value_index"])
    return (
        status_bar(b["signals"], b["freshness"]),
        F.fig_style_meter(style),
        F.fig_thermometer(slice_range(b["thermo"], range_key)),
        f_margin,
        F.fig_energy_breadth(slice_range(b["turnover"], range_key),
                             b["turnover_pct"], slice_range(b["breadth"], range_key)),
    )


def main() -> None:
    app.run(host="127.0.0.1", port=8060, debug=False)


if __name__ == "__main__":
    main()
