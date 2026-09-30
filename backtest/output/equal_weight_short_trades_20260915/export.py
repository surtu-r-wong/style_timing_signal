"""Export all incumbent equal_weight short episodes from frozen inputs."""
from pathlib import Path
import hashlib
import importlib.util
import json
import numpy as np
import pandas as pd

HERE = Path(__file__).resolve().parent
SOURCE = HERE.parent / "short_entry_day_direction_20260915"
END = pd.Timestamp("2026-09-11")


def main():
    meta = json.loads((SOURCE / "metadata.json").read_text())
    for name in ["equal_weight_symmetric.csv", "spot_close.csv"]:
        assert hashlib.sha256((SOURCE / name).read_bytes()).hexdigest() == meta["input_sha256"][name]
    pos = pd.read_csv(SOURCE / "equal_weight_symmetric.csv", parse_dates=["date"]).set_index("date").position
    close = pd.read_csv(SOURCE / "spot_close.csv", parse_dates=["date"]).set_index("date").loc[:END]
    daily = close.pct_change(fill_method=None).dropna()
    daily["blend"] = (daily["500"] + daily["1000"]) / 2
    nav = (1 + daily["blend"]).cumprod()
    entries = pos.index[(pos < 0) & (pos.shift(1) >= 0)]
    entries = entries[(entries >= "2014-01-02") & (entries <= END)]
    baseline = pd.read_csv(HERE.parent / "incumbent_winrate_baseline_20260915/trades.csv", parse_dates=["date", "end"])
    baseline = baseline[(baseline.signal == "equal_weight") & (baseline.side == "short") & (baseline.underlying == "blend")].set_index("date")
    assert list(entries) == list(baseline.index)
    rows = []
    for number, entry in enumerate(entries, 1):
        # Exit includes either flat or long; a direct flip to +1 closes the short.
        exits = pos.index[(pos.index > entry) & (pos >= 0)]
        exit_date = exits[0] if len(exits) and exits[0] <= END else pd.NaT
        asof = exit_date if pd.notna(exit_date) else END
        path = nav.loc[entry:asof] / nav.at[entry]
        pnl = 1 - path
        duration = len(path) - 1
        assert (pos.loc[(pos.index >= entry) & (pos.index < asof)] < 0).all()
        if pd.notna(exit_date):
            old = baseline.loc[entry]
            assert old.end == exit_date and duration == old.days
            assert np.allclose([pnl.iloc[-1], pnl.max(), pnl.min()], [old.gross, old.mfe, old.mae], atol=1e-12)
        else:
            assert pos.at[asof] < 0
        # Independent direct daily-return products for all old/new path fields.
        d = daily.loc[(daily.index > entry) & (daily.index <= asof), "blend"]
        direct = np.r_[1., (1 + d).cumprod().to_numpy()]
        assert np.allclose(path.to_numpy(), direct, atol=1e-12)
        equity = 1 + pnl
        assert (equity > 0).all()
        drawdown = equity / equity.cummax() - 1
        assert np.allclose(pnl.to_numpy(), 1 - direct, atol=1e-12)
        rows.append({"序号": number, "信号入场日": entry.strftime("%Y-%m-%d"),
                     "信号退出日": exit_date.strftime("%Y-%m-%d") if pd.notna(exit_date) else "",
                     "统计截至日": asof.strftime("%Y-%m-%d"), "状态": "已结束" if pd.notna(exit_date) else "持有中",
                     "持续交易日": duration, "持续自然日": (asof - entry).days,
                     "最终收益率": pnl.iloc[-1] if pd.notna(exit_date) else np.nan,
                     "截至日收益率": pnl.iloc[-1], "最大浮盈": pnl.max(), "最大浮盈日": pnl.idxmax().strftime("%Y-%m-%d"),
                     "最大浮亏": pnl.min(), "最大浮亏日": pnl.idxmin().strftime("%Y-%m-%d"),
                     "段内最大回撤": drawdown.min(), "最大回撤日": drawdown.idxmin().strftime("%Y-%m-%d"),
                     "进出每边3bps净收益率": pnl.iloc[-1] - .0003 * (1 + path.iloc[-1]) if pd.notna(exit_date) else np.nan})
    df = pd.DataFrame(rows)
    df.to_csv(HERE / "equal_weight_全部做空明细.csv", index=False, encoding="utf-8-sig")
    closed = df[df["状态"] == "已结束"]
    assert len(df) == 67 and len(closed) == 67 and (closed["最终收益率"] > 0).sum() == 42
    notes = [
        "现役equal_weight 20d40z/5日平滑，按推荐对称持仓的全部非空头→空头事件统计。",
        "历史窗2014-01-02至2026-09-11；共67次做空，全部结束，42盈25亏，毛胜率62.7%。",
        "标的是中证500/中证1000日收益50/50、每日恢复等权的现货代理，并非真实期货合约或账户收益。",
        "信号入场日T收盘为起点，首次退出空头的信号日收盘为终点；T日涨幅不算入本笔收益。信号依赖收盘数据，该价为研究基准价。",
        "持续交易日=入场收盘之后至退出/截至收盘的交易日数量；自然日=两个日期相减。",
        "最终收益率、最大浮盈、最大浮亏均相对入场收盘；路径只观察每日收盘，不是盘中最高/最低。包括入场零收益点，故最大浮盈不小于0、最大浮亏不大于0。",
        "最大浮盈/浮亏日期并列时取首次；0%的极值取入场日，不代表盘中没有波动。",
        "做空收益=1-标的截至收盘净值/入场收盘净值；段内最大回撤按初始本金1加做空损益形成的权益，从此前高点计算，与相对入场的最大浮亏不同。",
        "主表为毛收益，不含费用、期货贴水、换月和滑点；另列已结束交易的每边3bps进出金额费用示例。",
        "持有中的最终收益留空，仅列截至2026-09-11的浮动收益和路径极值。",
    ]
    lines = ["# equal_weight 全部做空明细", "", *["- " + x for x in notes], "",
             "| 序号 | 入场信号日 | 退出信号日 | 交易日数 | 最终/当前收益 | 最大浮盈 | 最大浮亏 |",
             "|---:|---|---|---:|---:|---:|---:|"]
    for r in rows:
        end = r["信号退出日"] or "持有中*"
        lines.append(f"| {r['序号']} | {r['信号入场日']} | {end} | {r['持续交易日']} | {r['截至日收益率']:+.2%} | {r['最大浮盈']:+.2%} | {r['最大浮亏']:+.2%} |")
    (HERE / "REPORT.md").write_text("\n".join(lines) + "\n")
    if importlib.util.find_spec("openpyxl"):
        from openpyxl.styles import Font, PatternFill, Alignment
        from openpyxl.utils import get_column_letter
        with pd.ExcelWriter(HERE / "equal_weight_全部做空明细.xlsx", engine="openpyxl") as writer:
            df.to_excel(writer, sheet_name="全部做空明细", index=False)
            pd.DataFrame({"口径说明": notes}).to_excel(writer, sheet_name="口径说明", index=False)
            ws = writer.sheets["全部做空明细"]
            ws.freeze_panes = "F2"
            ws.auto_filter.ref = ws.dimensions
            pct = {"最终收益率", "截至日收益率", "最大浮盈", "最大浮亏", "段内最大回撤", "进出每边3bps净收益率"}
            for col, name in enumerate(df.columns, 1):
                ws.column_dimensions[get_column_letter(col)].width = 27 if name == "进出每边3bps净收益率" else 18
                cell = ws.cell(1, col)
                cell.font = Font(bold=True, color="FFFFFF")
                cell.fill = PatternFill("solid", fgColor="24476A")
                for j in range(2, ws.max_row + 1):
                    ws.cell(j, col).alignment = Alignment(horizontal="center")
                    if name in pct:
                        ws.cell(j, col).number_format = "+0.00%;-0.00%;0.00%"
            writer.sheets["口径说明"].column_dimensions["A"].width = 145
        # Check the exported workbook round-trips with all 67 rows and blank final P&L for open trade.
        exported = pd.read_excel(HERE / "equal_weight_全部做空明细.xlsx", sheet_name="全部做空明细")
        assert len(exported) == len(df)
        assert np.allclose(exported["截至日收益率"], df["截至日收益率"])
        assert exported.loc[exported["状态"] == "持有中", "最终收益率"].isna().all()
    (HERE / "metadata.json").write_text(json.dumps({"asof": str(END.date()), "input_sha256": {
        str(SOURCE / name): meta["input_sha256"][name] for name in ["spot_close.csv", "equal_weight_symmetric.csv"]},
        "total_entries": len(df), "closed": len(closed), "wins": 42, "losses": 25}, indent=2) + "\n")
    (HERE / "verification.txt").write_text("PASS: all 67 entry dates match prior study; all 67 closed P&L/durations/extrema match verified baseline; all paths independently checked via daily return products; short profit direction and equity drawdown checked; exported workbook checked when available.\n")
    print("\n".join(lines))
    print("Excel created:", (HERE / "equal_weight_全部做空明细.xlsx").exists())


if __name__ == "__main__":
    main()
