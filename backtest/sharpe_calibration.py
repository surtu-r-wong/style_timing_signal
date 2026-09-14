"""Independent size/power study of two fixed paired Sharpe comparisons."""
from __future__ import annotations
import argparse
from datetime import datetime, timezone
import json
from pathlib import Path
import shutil

import numpy as np
import pandas as pd
from scipy.stats import beta
from backtest.paired_bootstrap import moving_block_indices
from backtest.research_statistics import holm_adjust
from backtest.run_manifest import create_run_dir, artifact_record, git_state, write_manifest

ROOT=Path(__file__).resolve().parents[1]
SEED=2026091402
KINDS=('iid','cluster_t5','ar03','slow_regime','vol_break')
PLAN=ROOT/'docs/superpowers/plans/2026-09-14-forward-completion-plan.md'


def generate(kind,n,seed,delta=0.):
    """Four exchangeable marginal processes: joint equal-Sharpe null.

    Population marginal variance is .01**2 (time-average for vol_break).
    Components share .6 variance globally and .2 within each comparison.
    No sample demeaning/rescaling: independent null outcomes can be unequal.
    """
    rng=np.random.default_rng(seed)
    size=n+512
    z=rng.normal(size=(size,7))
    if kind=='cluster_t5':
        z=rng.standard_t(5,size=(size,7))*np.sqrt(3/5)
        v=np.zeros(size)
        variance=.15**2/(1-.95**2)
        v[0]=rng.normal(scale=np.sqrt(variance))
        for t in range(1,size): v[t]=.95*v[t-1]+rng.normal(scale=.15)
        z*=np.exp(v-variance)[:,None]
    elif kind=='ar03':
        for t in range(1,size): z[t]=.3*z[t-1]+np.sqrt(1-.3**2)*z[t]
    elif kind=='slow_regime':
        state=rng.choice([-1.,1.],size=7)
        for t in range(size):
            state*=np.where(rng.random(7)<.02,-1.,1.)
            z[t]=.6*z[t]+.8*state
    elif kind not in ('iid','vol_break'):
        raise ValueError(kind)
    z=z[-n:]
    x=np.column_stack([np.sqrt(.6)*z[:,0]+np.sqrt(.2)*z[:,1+j//2]+np.sqrt(.2)*z[:,3+j] for j in range(4)])
    if kind=='vol_break':
        vol=np.array_split(np.arange(n),4)
        scale=np.ones(n)
        for indices,value in zip(vol,(.5,2.,1.,.7)): scale[indices]=value
        scale/=np.sqrt(np.mean(scale**2))
        x*=scale[:,None]
    out=.01*x+.3*.01/np.sqrt(245)
    out[:,0]+=delta*.01/np.sqrt(245)
    return out


def paired_joint_p(x,block,draws,seed):
    x=np.asarray(x,float)
    if x.ndim!=2 or x.shape[1]!=4 or not np.isfinite(x).all():
        raise ValueError('four finite joint net-return series required')
    def sharpes(a,axis):
        return np.divide(a.mean(axis=axis),a.std(axis=axis,ddof=1),
                         out=np.zeros_like(a.mean(axis=axis)),where=a.std(axis=axis,ddof=1)>0)*np.sqrt(245)
    original=sharpes(x,0).reshape(2,2)
    diff=original[:,0]-original[:,1]
    idx=moving_block_indices(len(x),block,draws,np.random.default_rng(seed))
    bs=sharpes(x[idx],1).reshape(draws,2,2)
    delta=bs[:,:,0]-bs[:,:,1]
    return (np.sum(np.abs(delta-diff)>=np.abs(diff),axis=0)+1)/(draws+1)


def combine_p(p20,p60):
    return holm_adjust(np.maximum(p20,p60))


def study(reps=400,draws=499):
    rows=[]
    cases=[(k,0.) for k in KINDS]+[(k,d) for k in ('iid','cluster_t5') for d in (.2,.4,.6)]
    for case,(kind,delta) in enumerate(cases):
        for rep in range(reps):
            data_seed=SEED+case*10000+rep
            x=generate(kind,504,data_seed,delta)
            p20=paired_joint_p(x,20,draws,data_seed+1000000)
            p60=paired_joint_p(x,60,draws,data_seed+2000000)
            differences=(x.mean(axis=0)/x.std(axis=0,ddof=1)*np.sqrt(245)).reshape(2,2)
            differences=differences[:,0]-differences[:,1]
            for rule,adjusted in [('block20',holm_adjust(p20)),('block60',holm_adjust(p60)),('both_blocks',combine_p(p20,p60))]:
                rows.append({'kind':kind,'delta':delta,'rep':rep,'data_seed':data_seed,'rule':rule,
                             'p_C1':adjusted[0],'p_C2':adjusted[1],
                             'reject_any':bool((adjusted<=.05).any()),
                             'reject_C2':bool(adjusted[1]<=.05),
                             'positive_C1':bool(adjusted[0]<=.05 and differences[0]>0)})
        print(f'completed {kind}, delta={delta:g}, datasets={reps}',flush=True)
    data=pd.DataFrame(rows)
    summary=[]
    for (kind,delta,rule),g in data.groupby(['kind','delta','rule'],sort=False):
        metric='reject_any' if delta==0 else 'positive_C1'
        count=int(g[metric].sum()); n=len(g)
        upper=float(beta.ppf(.95,count+1,n-count)) if count<n else 1.
        lo=float(beta.ppf(.025,count,n-count+1)) if count else 0.
        hi=float(beta.ppf(.975,count+1,n-count)) if count<n else 1.
        summary.append({'kind':kind,'delta':delta,'rule':rule,'metric':metric,'n':n,'count':count,
                        'rate':count/n,'ci_lo':lo,'ci_hi':hi,'upper_onesided95':upper,
                        'null_C2_reject_rate':float(g.reject_C2.mean())})
    return data,pd.DataFrame(summary)


def main():
    ap=argparse.ArgumentParser(); ap.add_argument('--run-id',required=True); ap.add_argument('--smoke',action='store_true'); a=ap.parse_args()
    run=create_run_dir(ROOT/'backtest/output/runs',a.run_id)
    shutil.copyfile(PLAN,run/'inputs/prereg.md')
    for path in ('backtest/sharpe_calibration.py','backtest/paired_bootstrap.py','backtest/research_statistics.py','tests/test_sharpe_calibration.py'):
        shutil.copyfile(ROOT/path,run/'inputs'/path.replace('/','__'))
    write_manifest(run,{'status':'running','git':git_state(ROOT),'created_utc':datetime.now(timezone.utc).isoformat()})
    data,summary=study(5 if a.smoke else 400,49 if a.smoke else 499)
    data.to_csv(run/'outputs/replications.csv',index=False); summary.to_csv(run/'outputs/summary.csv',index=False)
    main_rows=summary[summary.rule.eq('both_blocks')]
    verdict={'development_only':a.smoke,'go_enabled':False,
             'size_scenario_criterion_met':bool((main_rows[main_rows.delta.eq(0)].upper_onesided95<=.075).all()),
             'power_target_met':bool((main_rows[main_rows.delta.eq(.4)].rate>=.8).all()),
             'formal_forward_method_frozen':False,'scope':'listed synthetic net-return DGPs only'}
    (run/'outputs/verdict.json').write_text(json.dumps(verdict,indent=2))
    files=[artifact_record(p,run) for sub in ('inputs','outputs','logs') for p in sorted((run/sub).rglob('*')) if p.is_file()]
    write_manifest(run,{'status':'complete','git':git_state(ROOT),'completed_utc':datetime.now(timezone.utc).isoformat(),
                        'artifacts':files,'seed':SEED,'n_sessions':504,'reps':5 if a.smoke else 400,'draws':49 if a.smoke else 499,**verdict})
    print(main_rows.round(4).to_string(index=False),flush=True)
    print(json.dumps(verdict),flush=True)


if __name__=='__main__': main()
