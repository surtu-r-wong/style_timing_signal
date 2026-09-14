"""Finish fixed 08-26 prescreens without searching parameters or deploying.

Input definitions imported from the archived scripts; all comparison p-values
are descriptive on reused history. Positive adoption needs independent evidence.
"""
from __future__ import annotations
import argparse
import importlib.util
from pathlib import Path
import shutil
import sys

import numpy as np
import pandas as pd
from backtest.execution_audit import ROOT, END, WINDOWS, begin, finish, copy_snapshot, inputs, stats
from backtest.engine import run_strategy
from backtest.paired_bootstrap import paired_block_bootstrap_sharpe_diff
from backtest.research_statistics import holm_adjust
from signals.common.data_source import load_pg_closes
from signals.equal_weight.generate_signal import load_pair_configs, CONFIG_FILE

ARCHIVE=ROOT/'docs/plans/2026-08-26-signal-generator-prescreens'


def archived_module():
    sys.path.insert(0,str(ARCHIVE))
    spec=importlib.util.spec_from_file_location('frozen_cusum_hmm',ARCHIVE/'cusum_hmm_prescreen.py')
    module=importlib.util.module_from_spec(spec);spec.loader.exec_module(module)
    return module


def dema(x,n):
    e=x.ewm(span=n,adjust=False).mean()
    return 2*e-e.ewm(span=n,adjust=False).mean()


def fixed_candidates(prices,production,pairs):
    m=archived_module()
    rebuilt=m.equal_weight_factor(prices,pairs,'flat').round(4)
    aligned=rebuilt.reindex(production.index)
    if aligned.isna().any() or not np.allclose(aligned,production.factor_value,atol=1e-10,rtol=0):
        mismatch=int((aligned!=production.factor_value).sum())
        raise ValueError(f'incumbent reconstruction mismatch {mismatch}')
    spreads=[m.standardize(d,40) for d in m.ew_daily_spreads(prices,pairs)]
    signals={'incumbent':production.factor_value,'raw':production.factor_value_raw,
             'DEMA5':dema(production.factor_value_raw,5).round(4),
             'DEMA7':dema(production.factor_value_raw,7).round(4),
             'CUSUM':m.aggregate([m.cusum_state(z) for z in spreads],prices.index),
             'Hamilton':m.aggregate([m.hamilton_score(z,1-133/3073) for z in spreads],prices.index)}
    return {name:s.reindex(production.index) for name,s in signals.items()}


def evaluate(run):
    _,_,_,_,_,_,und,carry=inputs(run)
    production=pd.read_csv(run/'inputs/equal_weight.csv',index_col='date',parse_dates=True)
    prices=pd.read_csv(run/'inputs/style_prices.csv',index_col='date',parse_dates=True)
    pairs=load_pair_configs(run/'inputs/config_4pairs.csv')
    sigs=fixed_candidates(prices,production,pairs)
    pd.DataFrame(sigs).to_csv(run/'outputs/candidate_factors.csv',index_label='date')
    checks=[]; ref=(sigs['incumbent']>0).astype(float)
    expected={'raw':285,'DEMA5':225,'DEMA7':162,'CUSUM':1374,'Hamilton':843}
    for name,s in sigs.items():
        if name=='incumbent':continue
        pos=(s>0).astype(float)
        history=pd.concat([ref.shift(1),pos.shift(1)],axis=1).dropna().loc[:'2026-08-24']
        actual=int((history.iloc[:,0]!=history.iloc[:,1]).sum())
        checks.append({'name':name,'expected_disagreement':expected[name],'actual_disagreement':actual,'match':actual==expected[name]})
    pd.DataFrame(checks).to_csv(run/'outputs/archive_reproduction.csv',index=False)
    if not all(r['match'] for r in checks):
        # Do not silently recharacterize updated data as an exact replay.
        raise ValueError('archived disagreement counts differ; investigate input drift before testing returns')
    rows=[]; comparisons=[]
    for mapping in ('longflat','symmetric'):
        rets={}
        for name,s in sigs.items():
            idx=s.index.intersection(und.dropna().index)
            pos=((s>0).astype(float) if mapping=='longflat' else np.sign(s)).reindex(idx)
            d=run_strategy(pos,und,3,carry)
            rets[name]=d.ret
            d.to_csv(run/f'outputs/{mapping}_{name}_ledger.csv',index_label='date')
            for win,(a,b) in WINDOWS.items():
                r=d.ret.loc[a:b]
                rows.append({'mapping':mapping,'name':name,'window':win,**stats(r)})
        for block in (20,60):
            blockrows=[]
            for name in sigs:
                if name=='incumbent':continue
                a,b=WINDOWS['full']
                cmp=paired_block_bootstrap_sharpe_diff(rets[name].loc[a:b],rets['incumbent'].loc[a:b],block=block,n=2000,seed=20260914)
                blockrows.append({'mapping':mapping,'name':name,'window':'full',**cmp})
            adjusted=holm_adjust([r['p_value'] for r in blockrows])
            for r,p in zip(blockrows,adjusted): r['p_holm_five']=float(p)
            comparisons.extend(blockrows)
    panel=pd.DataFrame(rows);panel.to_csv(run/'outputs/candidate_panel.csv',index=False)
    pd.DataFrame(comparisons).to_csv(run/'outputs/candidate_paired.csv',index=False)
    print(panel[(panel.mapping=='longflat') & panel.window.isin(['full','2021-2023','2024-2026'])][['name','window','ann','sharpe','maxdd']].round(4).to_string(index=False),flush=True)


def main():
    ap=argparse.ArgumentParser();ap.add_argument('--run-id',required=True);ap.add_argument('--snapshot-from',type=Path,required=True)
    a=ap.parse_args();run=begin(a.run_id);copy_snapshot(a.snapshot_from,run)
    shutil.copyfile(CONFIG_FILE,run/'inputs/config_4pairs.csv')
    pairs=load_pair_configs(CONFIG_FILE)
    names=list(dict.fromkeys(c for p in pairs for c in (p.left_column,p.right_column)))
    prices=load_pg_closes(names,end=END,trim_ragged_tail=True)
    prices.to_csv(run/'inputs/style_prices.csv',index_label='date')
    code=run/'inputs/code';code.mkdir()
    for f in ('cusum_hmm_prescreen.py','ewma_std_prescreen.py','dema_smoothing_probe.py'):
        shutil.copyfile(ARCHIVE/f,code/f)
    evaluate(run);finish(run,'candidate_completion',{'historical_sample_reused':True,'production_change':False})

if __name__=='__main__':main()
