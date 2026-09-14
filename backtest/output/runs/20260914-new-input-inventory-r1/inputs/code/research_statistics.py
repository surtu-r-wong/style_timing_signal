"""Independent null generation and phase sensitivity, fixed on 2026-09-14.

No retrospective changes to candidate GO/STOP. --smoke is development-only;
formal uses 400 independent datasets, 499 transformations and 1600 daily points.
"""
from __future__ import annotations
import argparse
import json
from pathlib import Path
import shutil

import numpy as np
import pandas as pd
from scipy.stats import rankdata, binomtest
from backtest.execution_audit import ROOT, begin, finish, copy_snapshot, inputs
from backtest.engine import run_strategy
from backtest.metrics import sharpe
from backtest.gate0_criterion_study import size_study
from backtest.leverage_probe import level_signal
from backtest.selection_permutation import selection_permutation_test

SEED, N, REPS, PERMS = 20260914,1600,400,499
KS=(5,10,20,40)


def holm_adjust(values):
    p=np.asarray(values,float)
    if not np.isfinite(p).all() or (p<0).any() or (p>1).any():
        raise ValueError('invalid p-values')
    order=np.argsort(p); adjusted=np.minimum(1.,np.maximum.accumulate(p[order]*np.arange(len(p),0,-1)))
    out=np.empty(len(p)); out[order]=adjusted
    return out


def batch_rank_ic(signal, returns, k, indices):
    """Exactly the existing |nonoverlap_ic|, evaluated for many rotations."""
    n=len(returns); pts=np.arange(k-1,n-k,k)
    fwd=pd.Series(returns).rolling(k).sum().shift(-k).to_numpy()[pts]
    sr=rankdata(np.asarray(signal)[indices[:,pts]],axis=1)
    yr=rankdata(fwd); yr=yr-yr.mean(); sr=sr-sr.mean(axis=1,keepdims=True)
    den=np.sqrt((sr*sr).sum(axis=1)*(yr*yr).sum())
    return np.divide(np.abs(sr@yr),den,out=np.zeros(len(indices)),where=den>0)


def generated_null(kind,n,seed):
    """y_t has zero conditional mean given all history in every case.
+
+    Drivers of x and y are independent; shared deterministic volatility and
+    time-varying x persistence intentionally violate circular stationarity.
+    """
    rng=np.random.default_rng(seed)
    x=rng.normal(size=n); y=rng.normal(size=n)
    if kind=='stationary': return x,y
    if kind=='shared_volatility':
        vol=np.ones(n); vol[:n//4]=6.
        return x*vol,y*vol
    if kind=='regime':
        vol=np.repeat([4.,.5,2.,1.],int(np.ceil(n/4)))[:n]
        phi=np.repeat([.9,.1,.6,.3],int(np.ceil(n/4)))[:n]
        z=np.zeros(n)
        for i in range(1,n): z[i]=phi[i]*z[i-1]+np.sqrt(1-phi[i]**2)*x[i]
        return z*vol,y*vol
    raise ValueError(kind)


def calibration(reps=REPS,perms=PERMS):
    rows=[]; variants=[(lb,k) for lb in KS for k in KS]
    for ki,kind in enumerate(('stationary','shared_volatility','regime')):
        for rep in range(reps):
            seed=SEED+ki*10000+rep
            x,y=generated_null(kind,N,seed)
            forms={lb:pd.Series(x).rolling(lb,min_periods=1).mean().to_numpy() for lb in KS}
            def batch(v,idx): return batch_rank_ic(forms[v[0]],y,v[1],idx)
            for scheme in ('restricted','full_group'):
                rng=np.random.default_rng(seed+100000)
                shifts=(rng.integers(80,N-80,size=perms) if scheme=='restricted'
                        else rng.integers(0,N,size=perms))
                indices=((np.arange(N)[None,:]-shifts[:,None])%N).astype(np.int32)
                result=selection_permutation_test(variants,n_obs=N,index_matrix=indices,batch_stat_fn=batch)
                rows.append({'kind':kind,'rep':rep,'scheme':scheme,'p_maxT':result.p_selected,'p_minP':result.p_min_p})
            if (rep+1)%50==0: print(f'calibration {kind} {rep+1}/{reps}',flush=True)
    d=pd.DataFrame(rows); summary=[]
    for (kind,scheme),g in d.groupby(['kind','scheme']):
        for criterion in ('p_maxT','p_minP'):
            hits=int((g[criterion]<=.05).sum()); ci=binomtest(hits,len(g)).proportion_ci()
            summary.append({'kind':kind,'scheme':scheme,'criterion':criterion,'n_datasets':len(g),
                            'reject_rate':hits/len(g),'ci_lo':ci.low,'ci_hi':ci.high,
                            'binomial_p_vs_005':binomtest(hits,len(g),p=.05).pvalue})
    return d,pd.DataFrame(summary)


def phase(run):
    source=ROOT/'backtest/output/money_flow_series.csv'
    shutil.copyfile(source,run/'inputs/money_flow_series.csv')
    _,_,_,_,_,_,u,c=inputs(run)
    f=pd.read_csv(run/'inputs/money_flow_series.csv',index_col='trade_date',parse_dates=True)
    s=level_signal(f.F1.dropna(),5,250)
    idx=s.index.intersection(u.dropna().index); s=s.reindex(idx); u=u.reindex(idx)
    forward=u.rolling(20).sum().shift(-20)
    rows=[]
    for offset in range(20):
        pts=np.arange(offset,len(idx),20)
        j=pd.concat([s.iloc[pts],forward.iloc[pts]],axis=1).dropna()
        p=pd.Series(np.nan,index=idx); p.iloc[pts]=np.sign(s.iloc[pts]); p=p.ffill().fillna(0)
        r=run_strategy(p,u,3,c)['ret']
        rows.append({'offset':offset,'n_windows':len(j),'ic':j.iloc[:,0].corr(j.iloc[:,1],method='spearman'),
                     'net_sharpe':sharpe(r),'original_IC_phase':offset==19,'original_trade_phase':offset==0})
    d=pd.DataFrame(rows); d.to_csv(run/'outputs/F1_phase_panel.csv',index=False)
    print('F1 phase:',d[['ic','net_sharpe']].agg(['min','median','max']).round(4).to_dict(),flush=True)


def main():
    ap=argparse.ArgumentParser();ap.add_argument('--run-id',required=True);ap.add_argument('--snapshot-from',type=Path,required=True)
    ap.add_argument('--smoke',action='store_true');a=ap.parse_args()
    run=begin(a.run_id);copy_snapshot(a.snapshot_from,run)
    x=np.arange(200.)[:,None]*np.arange(1.,5.)[None,:]+10000
    (run/'outputs/rank_identity_counterexample.json').write_text(json.dumps(size_study(x),indent=2))
    phase(run)
    d,summary=calibration(reps=5 if a.smoke else REPS,perms=49 if a.smoke else PERMS)
    d.to_csv(run/'outputs/null_datasets.csv',index=False);summary.to_csv(run/'outputs/calibration_summary.csv',index=False)
    print(summary.round(4).to_string(index=False),flush=True)
    finish(run,'statistics',{'development_only':a.smoke,'n_obs':N,'reps':5 if a.smoke else REPS,'permutations':49 if a.smoke else PERMS,'seed':SEED})

if __name__=='__main__':main()
