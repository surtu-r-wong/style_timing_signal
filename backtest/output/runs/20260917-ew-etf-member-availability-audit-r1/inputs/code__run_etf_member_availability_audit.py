"""Post-hoc availability-matched risk controls; no new market features or candidate rules."""
from pathlib import Path
import json,shutil
import numpy as np
import pandas as pd
from backtest.data import _expiry_from_symbol
from backtest.execution_ledger import contract_ledger,futures_weights
from backtest.intraday_flow_pilot import prepare_close_market,batch_close_ledgers,attribute_ledger
from backtest.run_intraday_flow_pilot import describe
from backtest.run_manifest import artifact_record,create_run_dir,write_manifest
from backtest.run_etf_member_research import SCENARIOS
ROOT=Path(__file__).resolve().parents[1]
PARENT=ROOT/"backtest/output/runs/20260917-ew-etf-member-research-r3"
RUN_ID="20260917-ew-etf-member-availability-audit-r1"

def match_available(selected,reference,valid):
    if not selected.index.equals(reference.index) or not selected.index.equals(valid.index):
        raise ValueError("calendar")
    scale=selected.loc[valid].abs().sum()/reference.loc[valid].abs().sum() if reference.loc[valid].abs().sum() else 0.
    assert 0<=scale<=1
    out=reference.copy()
    out.loc[valid]*=scale
    np.testing.assert_allclose(out.loc[~valid],selected.loc[~valid],atol=1e-12)
    for mask in [valid,~valid]:
        np.testing.assert_allclose(out.loc[mask].abs().sum(),selected.loc[mask].abs().sum(),atol=1e-10)
    return out,float(scale)

def main():
    run=create_run_dir(ROOT/"backtest/output/runs",RUN_ID)
    manifest={"status":"running","run_id":RUN_ID,"parent":str(PARENT.relative_to(ROOT)),
              "post_hoc":True,"purpose":"Match incumbent fallback exactly and valid-state exposure; explanatory controls only"}
    write_manifest(run,manifest)
    try:
        m=json.loads((PARENT/"manifest.json").read_text())
        assert m["status"]=="computed_pending_report"
        needed=["inputs/futures.csv","inputs/office/calendar.csv","inputs/office/etf_features.csv",
                "inputs/office/member_features.csv","inputs/office/price_features.csv",
                "outputs/targets_IC_long.csv","outputs/targets_common.csv","outputs/definitions.csv"]
        needed += ["outputs/ledgers_"+w+"_"+s+".csv.gz" for w in ["IC_long","common"] for s,_,_ in SCENARIOS]
        receipts=[]
        for name in needed:
            src=PARENT/name
            expected=next(x for x in m["artifacts"] if x["path"]==name)
            assert artifact_record(src,PARENT)==expected
            dst=run/"inputs"/src.name
            shutil.copyfile(src,dst)
            receipts.append({"parent_path":name,**expected})
        (run/"inputs/parent_receipts.json").write_text(json.dumps(receipts,indent=2))
        shutil.copyfile(__file__,run/"inputs/code__run_etf_member_availability_audit.py")
        (run/"inputs/diagnostic_freeze.json").write_text(json.dumps({
            "post_hoc_reason":"Approximately 48% of MEMBER_POOL short matched-price net log difference occurs on fallback-affected dates.",
            "scope":"All 12 original window/candidate cases, all 3 original scenarios; original targets and windows unchanged.",
            "controls":"Fallback states equal original leg; valid states scaled original leg or price-only leg to selected valid-state exposure.",
            "new_candidate_rules":0,"new_control_ledgers":72,"parameters_fitted_on_full_history":"valid-state exposure ratios; not deployable",
            "metrics":"combined account full/early/late, 0/1/5 best relative days zeroed",
            "frozen_before_diagnostic_pnl":True},indent=2))
        futures=pd.read_csv(run/"inputs/futures.csv",parse_dates=["date"])
        calendar=pd.read_csv(run/"inputs/calendar.csv",parse_dates=["date"])
        sessions=pd.DatetimeIndex(calendar.loc[calendar.sfe.eq(1),"date"])
        defs=pd.read_csv(run/"inputs/definitions.csv")
        f=pd.concat([pd.read_csv(run/"inputs/etf_features.csv",parse_dates=["date"]),
                     pd.read_csv(run/"inputs/member_features.csv",parse_dates=["date"])])
        p=pd.read_csv(run/"inputs/price_features.csv",parse_dates=["date"])
        expires={}
        for symbol in futures.symbol.unique():
            dt=pd.Timestamp(_expiry_from_symbol(symbol))
            after=sessions[sessions>=dt]
            expires[symbol]=after[0] if len(after) else dt
        metrics=[];stresses=[];ratios=[];checks=[];count=0
        for window in ["IC_long","common"]:
            t=pd.read_csv(run/"inputs"/("targets_"+window+".csv"),index_col="date",parse_dates=True)
            idx=t.index
            controls={}
            wdefs=defs.loc[defs.window.eq(window)]
            for _,d in wdefs.iterrows():
                source,name,side=d.source,d.candidate,d.side
                ff=f.loc[f.source.eq(source)].set_index("date").reindex(idx)
                ps="POOL" if "POOL" in source else "IC"
                pp=p.loc[p.source.eq(ps)].set_index("date").reindex(idx)
                valid=ff.feature_valid.eq(True)&pp.feature_valid.eq(True)
                selected=t["LEG__"+name]
                other=t["C__base"]-t[side+"__base"]
                for ref,column in [("matched_available_base",side+"__base"),
                                   ("matched_available_price","LEG__"+source+"_"+side+"_price")]:
                    leg,scale=match_available(selected,t[column],valid)
                    key=ref+"__"+name
                    controls[key]=other+leg
                    ratios.append({"window":window,"candidate":name,"reference":ref,"valid_scale":scale,
                                   "valid_days":int(valid.sum()),"fallback_days":int((~valid).sum())})
            targets=pd.DataFrame(controls,index=idx)
            targets.to_csv(run/"outputs"/("control_targets_"+window+".csv"),index_label="date")
            weights=futures_weights(idx,futures.loc[futures.symbol.str.startswith("IM"),"date"].min())
            market=prepare_close_market(futures,idx,weights,expires)
            periods={"full":idx,"early_half":idx[:len(idx)//2],"late_half":idx[len(idx)//2:]}
            for scenario,cost,lag in SCENARIOS:
                print(window,scenario,len(targets.columns),"availability-control ledgers",flush=True)
                signals=targets.shift(lag,fill_value=0.)
                book=batch_close_ledgers(market,signals,cost_bps=cost)
                count+=len(book)
                original=pd.read_csv(run/"inputs"/("ledgers_"+window+"_"+scenario+".csv.gz"),parse_dates=["date"]).set_index(["strategy","date"])
                for key in list(book)[-2:]:
                    ref=contract_ledger(futures,signals[key],weights,expiries=expires,cost_bps=cost)
                    np.testing.assert_allclose(book[key].ret,ref.ret,atol=1e-12)
                    checks.append({"window":window,"scenario":scenario,"control":key,"max_error":float((book[key].ret-ref.ret).abs().max())})
                for key,b in book.items():
                    reference,name=key.split("__")
                    a=original.loc["C__"+name]
                    assert a.index.equals(idx) and np.isfinite(b.ret).all()
                    np.testing.assert_allclose(b.decision_signal,targets[key].shift(lag+1,fill_value=0.),atol=1e-12)
                    np.testing.assert_allclose(b.gross_pnl-b.cost,b.equity.diff().fillna(b.equity.iloc[0]-1.),atol=1e-12)
                    attribute_ledger(b)
                    for period,dates in periods.items():
                        aa,bb=describe(a.loc[dates]),describe(b.loc[dates])
                        metrics.append({"window":window,"scenario":scenario,"candidate":name,"reference":reference,"period":period,
                            "candidate_cagr":aa["cagr"],"reference_cagr":bb["cagr"],"reference_sharpe":bb["sharpe"],
                            "sharpe_difference":aa["sharpe"]-bb["sharpe"],
                            "delta_log":float((np.log1p(a.loc[dates].ret)-np.log1p(b.loc[dates].ret)).sum())})
                    top=(np.log1p(a.ret)-np.log1p(b.ret)).nlargest(5).index
                    for n in [0,1,5]:
                        aa,bb=a.copy(),b.copy()
                        aa.loc[top[:n],"ret"]=0.;bb.loc[top[:n],"ret"]=0.
                        stresses.append({"window":window,"scenario":scenario,"candidate":name,"reference":reference,
                            "best_days_zeroed":n,"sharpe_difference":describe(aa)["sharpe"]-describe(bb)["sharpe"]})
                pd.concat(book,names=["strategy","date"]).to_csv(run/"outputs"/("ledgers_"+window+"_"+scenario+".csv.gz"))
        assert count==72
        pd.DataFrame(metrics).to_csv(run/"outputs/comparisons.csv",index=False)
        pd.DataFrame(stresses).to_csv(run/"outputs/stress.csv",index=False)
        pd.DataFrame(ratios).to_csv(run/"outputs/ratios.csv",index=False)
        verification={"new_control_ledgers":count,"reference_checks":checks,"state_exposures_match":True,
                      "fallback_positions_identical":True,"account_trade_and_lag_checks":True,"post_hoc_not_deployable":True}
        (run/"outputs/verification.json").write_text(json.dumps(verification,indent=2))
        manifest.update(status="computed_pending_report",verification=verification)
        print(pd.DataFrame(metrics).query("scenario=='close_3bps' and period=='full'").round(5).to_string(index=False),flush=True)
    except Exception as e:
        manifest.update(status="failed",error_type=type(e).__name__)
        raise
    finally:
        manifest["artifacts"]=[artifact_record(p,run) for p in sorted(run.rglob("*")) if p.is_file() and p!=run/"manifest.json"]
        write_manifest(run,manifest)

if __name__=="__main__":
    main()
