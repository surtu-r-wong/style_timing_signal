"""Isolated short-only research. --prepare reads PG; default is fully offline.

Keep OPENBLAS_NUM_THREADS=1 and a 2GB virtual-memory limit. No database writes,
production signal edits, vendor calls, or deployments.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import sys
from pathlib import Path

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
OUT = ROOT / "backtest/output/runs/20261019-equity-bond-short-r1"
PREVIOUS = ROOT / "backtest/output/runs/20261019-equity-bond-crossing-r1"
START, END = "2015-04-16", "2026-10-16"
ANN = 245


def prepare():
    import psycopg2
    from signals.common.config import load_db_config

    OUT.mkdir(exist_ok=False)
    (OUT / "inputs").mkdir()
    (OUT / "outputs").mkdir()
    protocol = {
        "created_on": "2026-10-19", "research_only": True,
        "input_codes": ["H00300.CSI", "885007.WI"],
        "primary": "R=TR300/bond. position=-1 iff R>1.05 AND R<MA60(R) AND R/R.shift(20)-1<0, else 0",
        "execution": "day t information; t+1 close fill; t+2 close sensitivity; prior-day OI picks main; expiry excluded; explicit rolls; daily 1x equity target; fractional contracts",
        "market": "primary IC then IC/IM 50:50 after IM listing; IF diagnostic on same dates",
        "cost_bps_per_side": [3, 10], "range": [START, END],
        "sensitivity_grid": {"floor": [1.0, 1.05, 1.10], "ma": [40, 60, 120], "momentum": [10, 20, 40]},
        "controls": ["cash", "always_short", "ew_short", "level120", "ratio_trend_no_floor", "equity_trend", "equity_trend_floor105", "high120_turn_exit103", "ew_floor100", "ew_floor105", "ew_and_primary", "ew_or_primary", "half_ew_primary"],
        "fixed_splits": {"development": [START, "2020-12-31"], "validation1": ["2021-01-01", "2023-12-31"], "validation2": ["2024-01-01", END]},
        "selection_diagnostic": "choose best grid Sharpe in 2015-20 on primary market only, freeze rule for 2021+; all dates previously seen in other research, not virgin OOS",
        "bootstrap": {"blocks": [60, 245], "draws": 2000, "seed": 20261019},
        "limits": "No claims of live tradability, initial release timing, integer-lot execution, margin, limit-lock fill or market impact. All historical data reused. No thresholds optimized after primary results.",
    }
    (OUT / "inputs/protocol.json").write_text(json.dumps(protocol, ensure_ascii=False, indent=2))
    for name in ("daily_ratio.csv", "equal_weight_symmetric.csv", "slope20_longflat.csv", "pg_benchmarks.csv"):
        (OUT / "inputs" / name).write_bytes((PREVIOUS / name).read_bytes())
    cfg = load_db_config()
    try:
        conn = psycopg2.connect(host=cfg["host"], port=cfg["port"], dbname=cfg["name"], user=cfg["user"], password=cfg["password"], connect_timeout=10,
                               options="-c default_transaction_read_only=on -c statement_timeout=30000")
        conn.set_session(readonly=True, isolation_level="REPEATABLE READ")
        with conn.cursor() as q:
            q.execute("SELECT transaction_timestamp()::text")
            stamp = q.fetchone()[0]
            q.execute("SELECT trade_date,symbol,open,close,oi,settle,volume FROM public.futures_daily WHERE (symbol LIKE %s OR symbol LIKE %s OR symbol LIKE %s) AND trade_date BETWEEN %s AND %s ORDER BY 1,2", ("IF%", "IC%", "IM%", START, END))
            fut = pd.DataFrame(q.fetchall(), columns=["date", "symbol", "open", "close", "oi", "settle", "volume"])
        conn.rollback()
        conn.close()
    except Exception as exc:
        raise RuntimeError("PG readonly snapshot failed: " + type(exc).__name__) from None
    fut.to_csv(OUT / "inputs/futures.csv", index=False)
    summary = fut.assign(group=fut.symbol.str[:2]).groupby("group").date.agg(["count", "min", "max"])
    (OUT / "inputs/snapshot.json").write_text(json.dumps({"transaction_timestamp": stamp, "source": "PG public.futures_daily readonly", "rows": len(fut), "index_source": "frozen earlier Wind snapshot; no new Wind requests"}, indent=2))
    print("PREPARED\n" + summary.to_string(), flush=True)


def trend_short(ratio, floor=1.05, ma=60, momentum=20):
    down = ratio.lt(ratio.rolling(ma, min_periods=ma).mean()) & ratio.lt(ratio.shift(momentum))
    if floor is not None:
        down &= ratio.gt(floor)
    return -down.astype(float)


def high_turn_short(ratio):
    down = trend_short(ratio, floor=None).lt(0)
    state = False
    values = []
    for r, d in zip(ratio, down):
        if state and (r <= 1.03 or not d):
            state = False
        elif not state and r >= 1.20 and d:
            state = True
        values.append(-float(state))
    return pd.Series(values, index=ratio.index)


def cycle_short(ratio, entry=1.20, floor=1.03, stop=0.10):
    """Exploratory extension: retain short until convergence or adverse ratio move.

    The stop is on the indicator ratio, not a guaranteed PnL stop on IC/IM.
    Exits execute next close; gap losses can exceed the stated indicator move.
    """
    down=trend_short(ratio,floor=None).lt(0)
    active=False;entry_ratio=None;values=[]
    for r,d in zip(ratio,down):
        if active:
            if r<=floor or r>=entry_ratio*(1+stop):
                active=False
        elif r>=entry and d:
            active=True;entry_ratio=r
        values.append(-float(active))
    return pd.Series(values,index=ratio.index)


def signals(frame, ew):
    ratio, eq = frame.ratio, frame.equity
    primary = trend_short(ratio)
    ew = ew.reindex(frame.index).fillna(0.).clip(upper=0.)
    eq_trend = trend_short(eq, floor=None)
    result = {"cash": pd.Series(0., index=frame.index), "always_short": pd.Series(-1., index=frame.index),
              "ew_short": ew, "primary": primary, "level120": -ratio.ge(1.20).astype(float),
              "ratio_trend": trend_short(ratio, floor=None), "equity_trend": eq_trend,
              "equity_trend_floor105": eq_trend.where(ratio.gt(1.05), 0.),
              "high120_turn_exit103": high_turn_short(ratio),
              "ew_floor100": ew.where(ratio.gt(1.0), 0.), "ew_floor105": ew.where(ratio.gt(1.05), 0.),
              "ew_and_primary": -((ew < 0) & (primary < 0)).astype(float),
              "ew_or_primary": -((ew < 0) | (primary < 0)).astype(float),
              "half_ew_primary": (ew + primary) / 2}
    grid_names = []
    for floor in (1.0, 1.05, 1.10):
        for ma in (40, 60, 120):
            for mom in (10, 20, 40):
                name = f"grid_f{floor:.2f}_ma{ma}_mom{mom}"
                result[name] = trend_short(ratio, floor, ma, mom)
                grid_names.append(name)
    out = pd.DataFrame(result)
    assert out.notna().all().all() and out.le(0).all().all() and out.ge(-1).all().all()
    return out, grid_names


def schedule(futures, calendar, groups, weights):
    """Prior-day OI contract choice. All prices remain same-contract prices.

    Avoid all expiry-day positions, using third Friday advanced to next observed
    equity trading day. No historical metadata assertion is made for special
    exchange exceptions beyond this rule.
    """
    from backtest.data import _expiry_from_symbol
    p = futures.copy()
    if p.duplicated(["date", "symbol"]).any():
        raise ValueError("duplicate futures quote")
    close = p.pivot(index="date", columns="symbol", values="close").reindex(calendar)
    oi = p.pivot(index="date", columns="symbol", values="oi").reindex(calendar)
    symbols = list(close.columns)
    expiries = {}
    for sym in symbols:
        expiry = pd.Timestamp(_expiry_from_symbol(sym))
        possible = calendar[calendar >= expiry]
        expiries[sym] = possible[0] if len(possible) else expiry
    n, k = len(calendar), len(groups)
    choice = np.full((n, k), -1, dtype=int)
    oi_values=oi.to_numpy(dtype=float)
    expiry_values=np.array([expiries[sym].value for sym in symbols],dtype=np.int64)
    group_masks={g:np.array([sym.startswith(g) for sym in symbols]) for g in groups}
    for i in range(1, n):
        for g, group in enumerate(groups):
            if weights[i, g] == 0:
                continue
            eligible=group_masks[group] & (expiry_values>calendar[i].value) & np.isfinite(oi_values[i-1])
            candidates=np.flatnonzero(eligible)
            if len(candidates)==0:
                raise ValueError(f"no prior OI {group} {calendar[i].date()}")
            # pivot sorts symbols; argmax's first match gives lexical tie-break.
            choice[i,g]=candidates[np.argmax(oi_values[i-1,candidates])]
    price = close.to_numpy(dtype=float)
    chosen = np.ones((n,k)); old_now = np.ones((n,k)); old_previous = np.ones((n,k))
    rolled = np.zeros((n,k), dtype=bool)
    for i in range(1,n):
        for g in range(k):
            if choice[i,g] >= 0:
                chosen[i,g] = price[i, choice[i,g]]
            if choice[i-1,g] >= 0:
                old_now[i,g] = price[i, choice[i-1,g]]
                old_previous[i,g] = price[i-1, choice[i-1,g]]
            rolled[i,g] = choice[i,g] != choice[i-1,g]
    for values in (chosen, old_now, old_previous):
        if not np.isfinite(values).all() or (values <= 0).any():
            raise ValueError("required scheduled quote missing or invalid")
    desc = pd.DataFrame(index=calendar)
    for g,group in enumerate(groups):
        desc[group] = [symbols[j] if j >= 0 else "" for j in choice[:,g]]
    return {"new": chosen, "old_now": old_now, "old_previous": old_previous,
            "rolled": rolled, "weights": weights, "symbols": desc, "expiries": expiries}


def batched_close_ledger(signal, sched, cost_bps=3., delay=1):
    """Vectorized across strategies; exact cash-equity/retained-quantity ledger."""
    s = np.asarray(signal, dtype=float)
    if s.ndim == 1:
        s = s[:, None]
    n,m = s.shape
    k = sched["weights"].shape[1]
    equity = np.ones(m); qty = np.zeros((m,k))
    rets, costs, turns = np.zeros((n,m)), np.zeros((n,m)), np.zeros((n,m))
    for i in range(1,n):
        at_fill = equity + (qty * (sched["old_now"][i] - sched["old_previous"][i])).sum(axis=1)
        if (at_fill <= 0).any():
            raise ValueError("cash pool insolvent")
        decision = s[i-delay] if i >= delay else np.zeros(m)
        desired = decision[:,None] * sched["weights"][i] * at_fill[:,None] / sched["new"][i]
        trade_same = abs(desired-qty) * sched["new"][i]
        trade_roll = abs(desired) * sched["new"][i] + abs(qty) * sched["old_now"][i]
        turnover = np.where(sched["rolled"][i], trade_roll, trade_same).sum(axis=1)
        cost = turnover * cost_bps / 1e4
        next_equity = at_fill-cost
        if (next_equity <= 0).any():
            raise ValueError("cash pool insolvent after cost")
        rets[i] = next_equity/equity-1
        costs[i] = cost/equity
        turns[i] = turnover/equity
        equity, qty = next_equity, desired
    return rets,costs,turns


def stats(ret):
    a = np.asarray(ret,dtype=float)
    sd = np.std(a,ddof=1)
    nav = np.cumprod(1+a)
    return {"n": len(a), "ann_mean": np.mean(a)*ANN, "cagr": nav[-1]**(ANN/len(a))-1,
            "sharpe": np.mean(a)/sd*np.sqrt(ANN) if sd > 0 else 0.,
            "maxdd": np.min(nav/np.maximum.accumulate(np.r_[1.,nav])[1:]-1),
            "total_return": nav[-1]-1, "worst_day": np.min(a)}


def analyze():
    frame = pd.read_csv(OUT/"inputs/daily_ratio.csv", index_col="date", parse_dates=True)
    ew = pd.read_csv(OUT/"inputs/equal_weight_symmetric.csv", index_col="date", parse_dates=True).position
    fut = pd.read_csv(OUT/"inputs/futures.csv", parse_dates=["date"])
    pos_all,grid_names = signals(frame, ew)
    calendar = frame.loc[START:END].index
    pos = pos_all.loc[calendar]
    assert ew.reindex(calendar).notna().all()
    pos.to_csv(OUT/"outputs/signals.csv",index_label="date")
    first_im = fut.loc[fut.symbol.str.startswith("IM"),"date"].min()
    w = np.array([[.5,.5] if d>first_im else [1.,0.] for d in calendar])
    schedules = {"IC_IM": schedule(fut, calendar, ["IC","IM"], w),
                 "IF": schedule(fut, calendar, ["IF"], np.ones((len(calendar),1)))}
    for market,sc in schedules.items():
        sc["symbols"].to_csv(OUT/f"outputs/schedule_{market}.csv",index_label="date")
    panels=[];yearly=[];returns={};costs={};turnovers={}
    periods={"full":(START,END),"2015_2020":(START,"2020-12-31"),"2021_2023":("2021-01-01","2023-12-31"),
             "2024_2026":("2024-01-01",END),"2021_onward":("2021-01-01",END),"dual_listed":("2022-07-25",END),"ex_2015_2016":("2017-01-01",END)}
    for market,sc in schedules.items():
        for delay in (1,2):
            for cost in (3.,10.):
                key=f"{market}_delay{delay}_{cost:g}bps"
                r,c,t=batched_close_ledger(pos,sc,cost_bps=cost,delay=delay)
                ret=pd.DataFrame(r,index=calendar,columns=pos.columns)
                returns[key]=ret;costs[key]=pd.DataFrame(c,index=calendar,columns=pos.columns);turnovers[key]=pd.DataFrame(t,index=calendar,columns=pos.columns)
                ret.to_csv(OUT/f"outputs/returns_{key}.csv",index_label="date")
                effective=pos.shift(delay).fillna(0.)
                for name in pos:
                    for period,(lo,hi) in periods.items():
                        rr=ret.loc[lo:hi,name]
                        pp=effective.loc[lo:hi,name]
                        panels.append({"market":market,"delay":delay,"cost_bps":cost,"signal":name,"period":period,
                                       **stats(rr),"short_fraction":float(pp.lt(0).mean()),"mean_abs_exposure":float(pp.abs().mean()),
                                       "cost_ann":costs[key].loc[lo:hi,name].mean()*ANN,"turnover_ann":turnovers[key].loc[lo:hi,name].mean()*ANN})
                    if delay==1 and cost==3:
                        for year,rr in ret[name].groupby(ret.index.year):
                            yearly.append({"market":market,"signal":name,"year":year,**stats(rr)})
                print("EXECUTED",key,len(pos.columns),"strategies",flush=True)
    panel=pd.DataFrame(panels);panel.to_csv(OUT/"outputs/metrics.csv",index=False)
    pd.DataFrame(yearly).to_csv(OUT/"outputs/yearly.csv",index=False)
    primary=panel[(panel.delay==1)&(panel.cost_bps==3)&(panel.period=="full")&~panel.signal.str.startswith("grid_")]
    print("PRIMARY\n"+primary[["market","signal","cagr","sharpe","maxdd","short_fraction"]].round(4).to_string(index=False),flush=True)
    # Verify the new fast implementation against the established independent ledger.
    from backtest.execution_ledger import contract_ledger
    verification=[]
    for market in ("IC_IM","IF"):
        weights=pd.DataFrame(schedules[market]["weights"],index=calendar,columns=["IC","IM"] if market=="IC_IM" else ["IF"])
        for name in ("primary","ew_short"):
            ref=contract_ledger(fut,pos[name],weights,fill="close",cost_bps=3.,expiries=schedules[market]["expiries"])
            actual=returns[f"{market}_delay1_3bps"][name]
            error=float(np.max(abs(actual-ref.ret)))
            assert error<1e-12,(market,name,error)
            verification.append({"market":market,"signal":name,"max_daily_return_error":error})
            print("CROSSCHECK",market,name,error,flush=True)
    (OUT/"outputs/ledger_crosscheck.json").write_text(json.dumps(verification,indent=2))
    diagnostics(frame,pos,grid_names,returns,panel,costs,turnovers)


def diagnostics(frame,pos,grid_names,returns,panel,costs,turnovers):
    calendar=pos.index
    out=OUT/"outputs"
    core=["primary","ew_short","ew_floor100","ew_floor105","equity_trend_floor105"]
    comparisons=[("primary","ew_short"),("primary","equity_trend_floor105"),
                 ("ew_floor100","ew_short"),("ew_floor105","ew_short")]
    boot=[]
    for market in ("IC_IM","IF"):
        for delay in (1,2):
            ret=returns[f"{market}_delay{delay}_3bps"][core]
            values=ret.to_numpy()
            n=len(values)
            obs=values.mean(axis=0)/values.std(axis=0,ddof=1)*np.sqrt(ANN)
            for block in (60,245):
                rng=np.random.default_rng(20261019)
                sr=np.empty((2000,len(core)));means=np.empty_like(sr)
                for b in range(2000):
                    starts=rng.integers(0,n-block+1,size=int(np.ceil(n/block)))
                    indices=(starts[:,None]+np.arange(block)[None,:]).ravel()[:n]
                    sample=values[indices]
                    sd=sample.std(axis=0,ddof=1);mu=sample.mean(axis=0)
                    sr[b]=np.divide(mu,sd,out=np.zeros_like(mu),where=sd>0)*np.sqrt(ANN)
                    means[b]=mu*ANN
                for a,b in comparisons:
                    ia,ib=core.index(a),core.index(b)
                    delta=sr[:,ia]-sr[:,ib]
                    mean_delta=means[:,ia]-means[:,ib]
                    lo,hi=np.quantile(delta,[.025,.975])
                    ml,mh=np.quantile(mean_delta,[.025,.975])
                    estimate=obs[ia]-obs[ib]
                    boot.append({"market":market,"delay":delay,"block":block,"a":a,"b":b,
                                 "delta_sharpe":estimate,"lo":lo,"hi":hi,
                                 "mean_ann_delta":(values[:,ia]-values[:,ib]).mean()*ANN,
                                 "mean_ann_delta_lo":ml,"mean_ann_delta_hi":mh,
                                 "p_two_sided_uncorrected":(1+np.sum(abs(delta-estimate)>=abs(estimate)))/2001})
                lo,hi=np.quantile(means[:,0],[.025,.975])
                boot.append({"market":market,"delay":delay,"block":block,"a":"primary","b":"cash",
                             "mean_ann_delta":values[:,0].mean()*ANN,"mean_ann_delta_lo":lo,"mean_ann_delta_hi":hi})
    pd.DataFrame(boot).to_csv(out/"bootstrap.csv",index=False)
    # Fixed selection on old window, then evaluate same selected rule on later windows.
    g=panel[(panel.market=="IC_IM")&(panel.delay==1)&(panel.cost_bps==3)&panel.signal.isin(grid_names)]
    training=g[g.period=="2015_2020"].sort_values(["sharpe","signal"],ascending=[False,True])
    selected=training.signal.iloc[0]
    panel[panel.signal.eq(selected)].to_csv(out/"training_selected_evaluation.csv",index=False)
    grid=[]
    for (market,delay,cost,period),sub in panel[panel.signal.isin(grid_names)].groupby(["market","delay","cost_bps","period"]):
        grid.append({"market":market,"delay":delay,"cost_bps":cost,"period":period,"n":len(sub),
                     "positive_cagr":int(sub.cagr.gt(0).sum()),"cagr_min":sub.cagr.min(),"cagr_median":sub.cagr.median(),"cagr_max":sub.cagr.max(),
                     "sharpe_min":sub.sharpe.min(),"sharpe_median":sub.sharpe.median(),"sharpe_max":sub.sharpe.max()})
    pd.DataFrame(grid).to_csv(out/"grid_summary.csv",index=False)
    episodes=[]
    for market in ("IC_IM","IF"):
        ret=returns[f"{market}_delay1_3bps"]
        for name in core:
            decision=pos[name].shift(1).fillna(0.)
            start=None
            for i in range(len(calendar)):
                if start is None and decision.iloc[i]<0:
                    start=i
                if start is not None and (decision.iloc[i]==0 or i==len(calendar)-1):
                    r=ret[name].iloc[start:i+1]
                    episodes.append({"market":market,"signal":name,"entry_date":calendar[start],"end_date":calendar[i],
                                     "closed":bool(decision.iloc[i]==0),"holding_intervals":i-start,
                                     "return":float((1+r).prod()-1),"worst_daily_return":float(r.min())})
                    start=None
    ep=pd.DataFrame(episodes);ep.to_csv(out/"episodes.csv",index=False)
    episode_summary=[]
    for (market,name),sub in ep.groupby(["market","signal"]):
        closed=sub[sub.closed]
        episode_summary.append({"market":market,"signal":name,"closed_trades":len(closed),"open_trades":int((~sub.closed).sum()),
                                "win_rate":closed["return"].gt(0).mean(),"median_return":closed["return"].median(),
                                "mean_return":closed["return"].mean(),"worst_trade":closed["return"].min(),
                                "median_holding_intervals":closed.holding_intervals.median()})
    pd.DataFrame(episode_summary).to_csv(out/"episode_summary.csv",index=False)
    # Audit path dependence and where the two gates add or lose return.
    attribution=[]
    for market in ("IC_IM","IF"):
        for delay in (1,2):
            rr=returns[f"{market}_delay{delay}_3bps"]
            for name in ("ew_floor100","ew_floor105","ew_and_primary","ew_or_primary","half_ew_primary"):
                delta=rr[name]-rr.ew_short
                for year,z in delta.groupby(delta.index.year):
                    attribution.append({"market":market,"delay":delay,"signal":name,"year":year,"sum_daily_delta":z.sum()})
    pd.DataFrame(attribution).to_csv(out/"increment_attribution.csv",index=False)
    state={"days":len(pos),"primary_short_days":int(pos.primary.lt(0).sum()),
           "ew_short_days":int(pos.ew_short.lt(0).sum()),
           "joint_short_days":int((pos.primary.lt(0)&pos.ew_short.lt(0)).sum()),
           "primary_only_days":int((pos.primary.lt(0)&pos.ew_short.eq(0)).sum()),
           "primary_vs_gated_equity_signal_corr":pos.primary.corr(pos.equity_trend_floor105),
           "latest_date":str(calendar[-1].date()),"latest_ratio":float(frame.ratio.loc[calendar[-1]]),
           "latest_signals":pos.loc[calendar[-1],core].to_dict(),"training_selected":selected}
    (out/"summary.json").write_text(json.dumps(state,ensure_ascii=False,indent=2))
    print("DIAGNOSTICS",json.dumps(state,ensure_ascii=False),flush=True)
    print("GATE CI\n"+pd.DataFrame(boot).query("market == 'IC_IM' and a in ['ew_floor100','ew_floor105']")[["delay","block","a","delta_sharpe","lo","hi"]].round(4).to_string(index=False),flush=True)
    write_report()
    freeze_manifest()


def freeze_manifest():
    files={str(p.relative_to(OUT)):hashlib.sha256(p.read_bytes()).hexdigest() for p in sorted(OUT.rglob("*")) if p.is_file() and p.name!="manifest.json"}
    files["research_script"]=hashlib.sha256(Path(__file__).read_bytes()).hexdigest()
    files["reference_ledger"]=hashlib.sha256((ROOT/"backtest/execution_ledger.py").read_bytes()).hexdigest()
    (OUT/"manifest.json").write_text(json.dumps(files,indent=2))


def write_report():
    import base64
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt

    out=OUT/"outputs"
    if not (out/"cycle_metrics.csv").exists():
        print("First-stage files complete; run --cycle-extension before generating the combined report.",flush=True)
        return
    frame=pd.read_csv(OUT/"inputs/daily_ratio.csv",index_col="date",parse_dates=True)
    pos=pd.read_csv(out/"signals.csv",index_col="date",parse_dates=True)
    panel=pd.read_csv(out/"metrics.csv")
    yearly=pd.read_csv(out/"yearly.csv")
    bootstrap=pd.read_csv(out/"bootstrap.csv")
    cycle=pd.read_csv(out/"cycle_metrics.csv")
    ep=pd.read_csv(out/"episode_summary.csv")
    summary=json.loads((out/"summary.json").read_text())
    selected=pd.read_csv(out/"training_selected_evaluation.csv")
    grid=pd.read_csv(out/"grid_summary.csv")
    rr={market:pd.read_csv(out/f"returns_{market}_delay1_3bps.csv",index_col="date",parse_dates=True) for market in ("IC_IM","IF")}
    cycle_if=pd.read_csv(out/"cycle_returns_IF_delay1_3bps.csv",index_col="date",parse_dates=True)
    candidate=pd.DataFrame({"ratio":frame.ratio.reindex(pos.index),"original_ew_short":pos.ew_short,
                            "short_position":pos.ew_floor100,"sensitivity_floor105":pos.ew_floor105})
    candidate["flat_reason"]=np.select([candidate.original_ew_short.eq(0),candidate.ratio.le(1)],
                                       ["equal_weight_not_short","ratio_at_or_below_1"],default="short_allowed")
    candidate.to_csv(out/"ew_short_bond_guard.csv",index_label="date")
    candidate_spec={"status":"research_candidate_only_not_deployed","signal":"-1 iff original_equal_weight_position<0 and H00300.CSI_close/885007.WI_close>1.00; otherwise 0",
                    "timing":"signal date close information; next trading day close execution; vendor publication time must be verified before live use",
                    "source_files":["inputs/equal_weight_symmetric.csv","inputs/daily_ratio.csv"],
                    "sensitivity":"1.05 is a sensitivity threshold, not a selected optimum",
                    "constraints":"no positive position, no automatic production changes"}
    (out/"candidate_spec.json").write_text(json.dumps(candidate_spec,ensure_ascii=False,indent=2))

    plt.rcParams["font.family"]="Noto Sans CJK JP";plt.rcParams["axes.unicode_minus"]=False
    fig,ax=plt.subplots(2,2,figsize=(14,10),constrained_layout=True)
    for name,label,color in (("ew_short","现役EW纯空头","#557080"),("ew_floor100","EW + 比值≤1停止做空","#17856d"),("primary","两指数独立趋势空头","#b4474c")):
        ax[0,0].plot(rr["IC_IM"].index,(1+rr["IC_IM"][name]).cumprod(),label=label,color=color,lw=1.3)
    ax[0,0].set_title("IC/IM实际合约：独立开空规则弱，止空过滤有改善")
    ax[0,0].set_ylabel("净值：1倍名义敞口，空仓计0收益")
    for data,name,label,color in ((rr["IF"],"ew_short","现役EW纯空头","#557080"),(rr["IF"],"primary","两指数独立趋势空头","#b4474c"),(cycle_if,"cycle_entry130","波段补测最好参数（事后）","#b58227")):
        ax[0,1].plot(data.index,(1+data[name]).cumprod(),label=label,color=color,lw=1.3)
    ax[0,1].set_title("IF诊断：波段版本有小幅收益，近期未延续")
    yy=yearly[(yearly.market=="IC_IM")&yearly.signal.isin(["primary","ew_short","ew_floor100"])].pivot(index="year",columns="signal",values="total_return")
    xx=np.arange(len(yy));width=.26
    for offset,name,label,color in ((-1,"primary","独立趋势空头","#b4474c"),(0,"ew_short","现役EW纯空头","#557080"),(1,"ew_floor100","EW + 债基止空","#17856d")):
        ax[1,0].bar(xx+offset*width,yy[name]*100,width,label=label,color=color)
    ax[1,0].set_xticks(xx,yy.index,rotation=45);ax[1,0].set_ylabel("年度净收益 %（2026为年内）")
    ax[1,0].set_title("IC/IM逐年：独立信号在2020、2021、2025亏损")
    conditions=[(1,3),(1,10),(2,3),(2,10)]
    for name,label,color in (("primary","独立趋势空头","#b4474c"),("ew_floor100","比值≤1停止做空","#17856d"),("ew_floor105","比值≤1.05停止做空","#b58227")):
        diffs=[]
        for delay,cost in conditions:
            sub=panel[(panel.market=="IC_IM")&(panel.period=="full")&(panel.delay==delay)&(panel.cost_bps==cost)].set_index("signal")
            diffs.append(sub.at[name,"sharpe"]-sub.at["ew_short","sharpe"])
        ax[1,1].plot(range(4),diffs,marker="o",label=label,color=color)
    ax[1,1].axhline(0,color="black",lw=.7);ax[1,1].set_xticks(range(4),["次日/3bp","次日/10bp","隔日/3bp","隔日/10bp"])
    ax[1,1].set_ylabel("相对现役EW纯空头的夏普差")
    ax[1,1].set_title("执行延迟与成本敏感性")
    for axis in ax.flat:
        axis.grid(alpha=.2);axis.legend(fontsize=9)
    fig.suptitle("股债比值纯空头研究 · 2015-04-16—2026-10-16",fontsize=17)
    fig.savefig(out/"short_audit.png",dpi=150);plt.close(fig)

    labels={"cash":"空仓","always_short":"持续做空","ew_short":"现役EW纯空头","primary":"两指数独立趋势空头",
            "level120":"比值≥1.20即做空","ratio_trend":"比值转弱、不设止空底线","equity_trend":"普通股指趋势空头",
            "equity_trend_floor105":"普通趋势+比值1.05底线","high120_turn_exit103":"高位转弱、趋势恢复就退出",
            "ew_floor100":"EW空头+比值>1.00","ew_floor105":"EW空头+比值>1.05","ew_and_primary":"EW与独立规则同时空",
            "ew_or_primary":"任一规则空即空","half_ew_primary":"EW与独立规则各半",
            "cycle_primary":"波段：1.20入场/1.03收敛/反向10%退出","cycle_entry130":"波段：入场改为1.30（事后最好）"}

    def table(df,percent=(),translate=False):
        df=df.copy()
        if translate and "signal" in df:
            df["signal"]=df["signal"].map(lambda x:labels.get(x,x))
        for c in percent:
            df[c]=df[c].map(lambda x:f"{x:.2%}" if pd.notna(x) else "—")
        return df.to_html(index=False,border=0,float_format=lambda x:f"{x:.4f}",na_rep="—")

    main=panel[(panel.market=="IC_IM")&(panel.delay==1)&(panel.cost_bps==3)&(panel.period=="full")&~panel.signal.str.startswith("grid_")]
    display=["signal","cagr","sharpe","maxdd","short_fraction"]
    windows=panel[(panel.market=="IC_IM")&(panel.delay==1)&(panel.cost_bps==3)&panel.signal.isin(["primary","ew_short","ew_floor100","ew_floor105"])&panel.period.isin(["2015_2020","2021_2023","2024_2026","ex_2015_2016"])]
    robust=panel[(panel.market=="IC_IM")&(panel.period=="full")&panel.signal.isin(["primary","ew_short","ew_floor100","ew_floor105"])]
    gate_ci=bootstrap[(bootstrap.market=="IC_IM")&bootstrap.a.isin(["ew_floor100","ew_floor105"])]
    png=base64.b64encode((out/"short_audit.png").read_bytes()).decode()
    report=f"""<!doctype html><html lang="zh-CN"><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>股债比值纯空头研究 · 2026-10-19</title><style>body{{font:16px/1.75 system-ui,sans-serif;color:#172435;max-width:1200px;margin:35px auto;padding:0 22px}}h1{{font-size:29px}}h2{{font-size:22px;margin-top:32px}}table{{border-collapse:collapse;font-size:13px;display:block;overflow:auto;white-space:nowrap}}td,th{{padding:7px 10px;border-bottom:1px solid #dde3ea;text-align:right}}th{{background:#eef3f8}}img{{max-width:100%}}code{{background:#eef3f8;padding:2px 4px}}.verdict{{background:#eef5fb;border-left:4px solid #365c8d;padding:15px 20px}}a{{color:#265c98}}</style>
<h1>股债比值能否构造纯空头信号？</h1><p>研究日2026-10-19；数据截至2026-10-16。所有规则只允许做空/空仓；组合诊断可用半仓空头。未部署生产。</p>
<div class="verdict"><b>本轮证据支持把股债比值用于限制现役空头，尚不支持以两条指数独立产生一个稳定的新空头信号。</b>
<p>IC/IM上，独立趋势主规则年化复合收益−1.07%、最大回撤50.66%；27组趋势参数全期复合收益均为负。持续持空到收敛的补充主规则也亏损。现役EW空头附加R>1限制，则从年化8.90%提高至12.23%，但改善约75%来自2015—2016，执行延迟和阈值会影响结果。保留研究候选，不宣布已验证有效。</p></div>
<h2>1. 固定规则和对照</h2>
<p><code>R = H00300.CSI收盘 / 885007.WI收盘</code>，沿用原始点位。独立主规则：<b>R>1.05、R低于自身60日均线、R的20日变化为负，三项同时成立才持空−1，否则0</b>。三项均只用截至信号日的数据。其含义是同时要求距离债基有回落空间、相对趋势已经转弱，不能仅凭点位较高就做空。</p>
<p>首轮冻结规格：<a href="../inputs/protocol.json">protocol.json</a>。27组邻域参数为底线1.00/1.05/1.10 × 均线40/60/120日 × 变化10/20/40日；全部报告，不挑最好者作结论。普通股指趋势、无底线的比值趋势、高位转弱、现役EW及交集/并集/各半都为预设对照。</p>
<p>保留的纯空头研究候选：<code>short_position = -1 if equal_weight_position &lt; 0 and R &gt; 1.00 else 0</code>。当比值重新回到1以下或EW不再看空时平空；恢复条件后可再次开空。它沿用EW判断开空方向，债基负责限制低位空头。1.05作为敏感性阈值保留，未宣布为最优。</p>
<h2>2. 数据与成交</h2>
<p>共2795个交易日，2015-04-16—2026-10-16。两指数复用前轮Wind冻结数据，现役信号冻结CSV；IF/IC/IM来自本轮只读PostgreSQL快照。未调用新的Wind采集或写共享库。</p>
<p>主口径为IC；IM有首个报价的下一交易日起改IC/IM各半。IF在同窗单独诊断原始沪深300适配性。<b>使用实际合约价格，前一交易日持仓量选主力，旧仓按旧合约计盈亏，换月计平旧和开新双边成本，不能把两合约点位跳差算成收益，也不再额外叠加估计贴水。</b>到期按第三个周五、遇休市顺延到下一已观察交易日，并在到期日排除该合约；采用既有执行研究的规则，未重建所有特殊到期公告。参见<a href="https://www.cffex.com.cn/u/cms/www/202003/27165505w3j5.pdf">中金所交易细则</a>。</p>
<p>信号T日出、T+1收盘执行；再检验T+2收盘。初始现金1，最多约1倍名义敞口，空仓收益0，按池内净值日调仓，单边3bp/10bp。这里年化是净值CAGR，按245交易日；<b>不是保证金收益率，也不是账户实绩</b>。首日初始化空仓，收益/成本包含所有空仓日。整数手数、资金利息、保证金追缴、涨跌停无法成交、冲击及历史交易限制未建模；两指数的实际完整公布时点也待核实。</p>
<h2>3. IC/IM主结果</h2>
{table(main[display],["cagr","maxdd","short_fraction"],True)}
<p>独立主规则102笔已平仓交易，胜率44.12%，持有期中位数3个交易区间；现役EW为62笔、59.68%、22.5个区间。独立主规则与“普通股指趋势+1.05底线”的仓位相关约0.893，其夏普差约0.005，分块区间跨0：债基日常变动并未提供清晰的新开空信息。与EW合并后，无论并集还是各半，完整样本夏普也低于EW原空头。</p>
{table(ep[ep.market.eq("IC_IM")],["win_rate","median_return","mean_return","worst_trade"],True)}
<h2>4. 参数与时间分期</h2>
{table(grid[(grid.market=="IC_IM")&(grid.delay==1)&(grid.cost_bps==3)][["period","n","positive_cagr","cagr_min","cagr_median","cagr_max","sharpe_max"]],["cagr_min","cagr_median","cagr_max"])}
<p>27组趋势参数全期年化中位数约−4.35%；只用2015—2020挑选，选中的恰是已固定的1.05/60/20主规则，但其2021—2023及2024—2026均为负复合收益。这个选择练习只说明没有靠训练窗排名解决问题，<b>并非真正未看过的样本外</b>；该仓库此前多次使用全部这些年份。</p>
{table(windows[["signal","period","cagr","sharpe","maxdd"]],["cagr","maxdd"],True)}
<h2>5. 波段补测：持有到收敛</h2>
<p>这是看到首轮“中位仅3日”之后明确登记的补充检验，规格单列为<a href="../inputs/cycle_extension_protocol.json">cycle_extension_protocol.json</a>。主规则：R≥1.20且相对趋势转弱时开空；开空后持续持有，到R≤1.03或R比入场值反向上涨10%时退出。退出阈值针对<b>比值</b>，不保证期货亏损止于10%；仍在次日收盘执行。另检验7个含主规则的相邻规格，不改首轮定义。</p>
{table(cycle[(cycle.delay==1)&(cycle.cost_bps==3)&(cycle.period=="full")][["market","signal","cagr","sharpe","maxdd"]],["cagr","maxdd"],True)}
<p>IC/IM波段主规则年化−1.68%、回撤63.44%；最好补测参数为入场1.30，年化约1.61%、回撤43.32%，2021—2023和2024—2026各亏约5%/年。IF的入场1.30版本有正面迹象：年化5.06%、夏普0.405、回撤23.06%；但2024年以来年化−0.76%，再延迟一天执行年化降至2.28%、回撤32.36%。这是事后参数中最好的一格，不能按已验证独立策略推荐。</p>
<h2>6. 债基止空能补充现役多少</h2>
<p>EW+R>1：年化8.90%→12.23%，夏普0.493→0.650，全期最大回撤仍36.39%。2024—2026子窗回撤28.53%→13.24%；但全期日收益增量之和约75%来自2015—2016，2021—2023略有拖累。EW+R>1.05也有改善；其下一日/多延迟一日的改善幅度不同，不能仅按全期最优夏普挑阈值。</p>
{table(robust[["delay","cost_bps","signal","cagr","sharpe","maxdd"]],["cagr","maxdd"],True)}
<p>下表是配对移动分块bootstrap（60/245日块，各2000次）夏普增量的95%描述性区间。R>1在次日执行的两种区间均为正；再延迟一天、60日块则跨0。R>1.05在次日执行、60日块也跨0。没有对本轮及历史候选筛选做多重检验校正；不能把其中某个正区间称为正式统计通过。</p>
{table(gate_ci[["delay","block","a","delta_sharpe","lo","hi"]])}
<h2>7. 交付与结论边界</h2>
<p>可以形成一个只空/空仓的研究候选，最值得保留的是<b>EW负责方向、股债比值负责低位止空</b>。两个指数独立开空的版本，在本轮代表性规则和参数下尚无足够稳健性；IF波段版本有少量正面结果，值得知道，但还不能用事后最好参数替换现役。</p>
<p>研究候选CSV：<a href="ew_short_bond_guard.csv">ew_short_bond_guard.csv</a>；规则：<a href="candidate_spec.json">candidate_spec.json</a>；完整首轮<a href="signals.csv">signals.csv</a>与<a href="metrics.csv">metrics.csv</a>；补测<a href="cycle_metrics.csv">cycle_metrics.csv</a>；逐笔<a href="episodes.csv">episodes.csv</a>；统计<a href="bootstrap.csv">bootstrap.csv</a>。</p>
<p>本次未修改生产信号、调度或数据库。入库日更请求仍独立交由data_manager办理。原始点位基准日任意性、基金指数历史回溯和实时发布时点的限制继续成立；新增数据日更本身不能补成真正历史点时数据。</p>
<img src="data:image/png;base64,{png}" alt="独立空头、现役空头及债基止空过滤的收益、年度分布和执行敏感性">
<h2>复核</h2><p>新批量账本与仓库既有独立实际合约账本，在IC/IM和IF的主规则及EW基准上共4组、全部2795日收益逐日完全一致（最大差0）：<a href="ledger_crosscheck.json">ledger_crosscheck.json</a>。6项数值测试覆盖因果性、预热、波段退出、信号滞后、前日持仓量选合约、换月成本。完整文件校验见<a href="../manifest.json">manifest.json</a>。</p>
<p>离线复现：<code>bash -c 'ulimit -v 2000000; OPENBLAS_NUM_THREADS=1 OMP_NUM_THREADS=1 python3 tools/research_equity_bond_short.py'</code>；波段补测加<code>--cycle-extension</code>。首轮执行无需重新取数；<code>--prepare</code>只用于首次冻结，已存在目录会拒绝覆盖。报告可单独调用write_report重建，无需重复回测。</p>
</html>"""
    (out/"report.html").write_text(report,encoding="utf-8")


def offline_diagnostics():
    frame=pd.read_csv(OUT/"inputs/daily_ratio.csv",index_col="date",parse_dates=True)
    pos=pd.read_csv(OUT/"outputs/signals.csv",index_col="date",parse_dates=True)
    panel=pd.read_csv(OUT/"outputs/metrics.csv")
    returns={p.stem.replace("returns_",""):pd.read_csv(p,index_col="date",parse_dates=True) for p in (OUT/"outputs").glob("returns_*.csv")}
    diagnostics(frame,pos,[c for c in pos if c.startswith("grid_")],returns,panel,None,None)


def cycle_extension():
    """Freeze the added family before computing its returns; preserve first run."""
    specification={"declared_after_initial_results":True,
                   "reason":"Daily trend exits produced median 3-day trades. Check the distinct hold-until-convergence hypothesis without replacing original results.",
                   "primary":"enter R>=1.20 and below MA60 with negative 20d change; retain until R<=1.03 or R>=entry_R*1.10; next close execution",
                   "variants":{"cycle_primary":[1.20,1.03,.10],"cycle_entry110":[1.10,1.03,.10],"cycle_entry130":[1.30,1.03,.10],
                               "cycle_stop05":[1.20,1.03,.05],"cycle_stop15":[1.20,1.03,.15],"cycle_floor100":[1.20,1.00,.10],"cycle_floor105":[1.20,1.05,.10]},
                   "same_execution_costs_and_windows_as_primary":True,
                   "limits":"post-hoc extension; no new OOS; ratio stop is not a price-PnL stop"}
    path=OUT/"inputs/cycle_extension_protocol.json"
    if path.exists():
        assert json.loads(path.read_text())==specification,"frozen extension changed"
    else:
        path.write_text(json.dumps(specification,indent=2))
    frame=pd.read_csv(OUT/"inputs/daily_ratio.csv",index_col="date",parse_dates=True)
    calendar=frame.loc[START:END].index
    pos=pd.DataFrame({name:cycle_short(frame.ratio,*params) for name,params in specification["variants"].items()}).loc[calendar]
    pos.to_csv(OUT/"outputs/cycle_signals.csv",index_label="date")
    fut=pd.read_csv(OUT/"inputs/futures.csv",parse_dates=["date"])
    first_im=fut.loc[fut.symbol.str.startswith("IM"),"date"].min()
    weights=np.array([[.5,.5] if d>first_im else [1.,0.] for d in calendar])
    schedules={"IC_IM":schedule(fut,calendar,["IC","IM"],weights),"IF":schedule(fut,calendar,["IF"],np.ones((len(calendar),1)))}
    periods={"full":(START,END),"2015_2020":(START,"2020-12-31"),"2021_2023":("2021-01-01","2023-12-31"),
             "2024_2026":("2024-01-01",END),"ex_2015_2016":("2017-01-01",END)}
    rows=[]
    for market,sc in schedules.items():
        for delay in (1,2):
            for cost in (3.,10.):
                r,_,_=batched_close_ledger(pos,sc,cost,delay)
                ret=pd.DataFrame(r,index=calendar,columns=pos.columns)
                ret.to_csv(OUT/f"outputs/cycle_returns_{market}_delay{delay}_{cost:g}bps.csv",index_label="date")
                for name in pos:
                    for period,(lo,hi) in periods.items():
                        rows.append({"market":market,"delay":delay,"cost_bps":cost,"signal":name,"period":period,
                                     **stats(ret.loc[lo:hi,name]),"short_fraction":pos[name].shift(delay).fillna(0.).loc[lo:hi].lt(0).mean()})
    metrics=pd.DataFrame(rows);metrics.to_csv(OUT/"outputs/cycle_metrics.csv",index=False)
    print("CYCLE EXTENSION\n"+metrics.query("delay == 1 and cost_bps == 3 and period == 'full'")[["market","signal","cagr","sharpe","maxdd","short_fraction"]].round(4).to_string(index=False),flush=True)
    if (OUT/"outputs/bootstrap.csv").exists():
        write_report()
    freeze_manifest()


if __name__ == "__main__":
    ap=argparse.ArgumentParser();ap.add_argument("--prepare",action="store_true");ap.add_argument("--diagnostics",action="store_true");ap.add_argument("--cycle-extension",action="store_true")
    args=ap.parse_args()
    if args.prepare:
        prepare()
    elif args.diagnostics:
        offline_diagnostics()
    elif args.cycle_extension:
        cycle_extension()
    else:
        analyze()
