"""Frozen comparative inference study with a separate confirmation simulation."""
from __future__ import annotations
import argparse
from datetime import datetime,timezone
import json
from pathlib import Path
import shutil

import numpy as np
import pandas as pd
from scipy.signal import lfilter
from scipy.stats import beta,norm,t
from scipy.optimize import brentq
from backtest.research_statistics import holm_adjust
from backtest.run_manifest import create_run_dir,artifact_record,git_state,write_manifest

ROOT=Path(__file__).resolve().parents[1]
PLAN=ROOT/'docs/superpowers/plans/2026-09-14-robust-inference-plan.md'
KINDS=('iid','cluster_t5','ar03','slow_regime','vol_break','slower_regime')
HORIZONS=(504,2016,8064)
METHODS=('group_t4','group_t8','sn_if')


def generate(kind,n,seed,delta=0.):
    """Same marginal DGP specifications as earlier study, new vectorized streams."""
    rng=np.random.default_rng(seed);size=n+512
    z=rng.normal(size=(size,7))
    if kind=='cluster_t5':
        z=rng.standard_t(5,size=(size,7))*np.sqrt(3/5)
        variance=.15**2/(1-.95**2)
        shocks=rng.normal(scale=.15,size=size);shocks[0]=rng.normal(scale=np.sqrt(variance))
        v=lfilter([1.],[1.,-.95],shocks)
        z*=np.exp(v-variance)[:,None]
    elif kind=='ar03':
        z[0]/=np.sqrt(1-.3**2)
        z=lfilter([np.sqrt(1-.3**2)],[1.,-.3],z,axis=0)
    elif kind in ('slow_regime','slower_regime'):
        flip=.02 if kind=='slow_regime' else .005
        state=rng.choice([-1.,1.],size=7)*np.cumprod(np.where(rng.random((size,7))<flip,-1.,1.),axis=0)
        z=.6*z+.8*state
    elif kind not in ('iid','vol_break'):raise ValueError(kind)
    z=z[-n:]
    x=np.column_stack([np.sqrt(.6)*z[:,0]+np.sqrt(.2)*z[:,1+j//2]+np.sqrt(.2)*z[:,3+j] for j in range(4)])
    if kind=='vol_break':
        scale=np.ones(n)
        for rows,v in zip(np.array_split(np.arange(n),4),(.5,2.,1.,.7)):scale[rows]=v
        x*=(scale/np.sqrt(np.mean(scale**2)))[:,None]
    out=.01*x+.3*.01/np.sqrt(245)
    out[:,0]+=delta*.01/np.sqrt(245)
    return out


def _check(x):
    x=np.asarray(x,float)
    if x.ndim!=2 or x.shape[1]!=4 or len(x)<16 or not np.isfinite(x).all():raise ValueError('four finite paired returns required')
    return x


def group_test(x,q):
    x=_check(x)
    groups=[]
    for g in np.array_split(x,q):
        sd=g.std(axis=0,ddof=1)
        if np.any(sd<=0):raise ValueError('degenerate group variance')
        s=g.mean(axis=0)/sd*np.sqrt(245)
        groups.append(s[::2]-s[1::2])
    d=np.asarray(groups);effect=d.mean(axis=0);se=d.std(axis=0,ddof=1)/np.sqrt(q)
    stat=np.divide(effect,se,out=np.zeros(2),where=se>0)
    p=2*t.sf(np.abs(stat),q-1)
    p=np.where((se==0)&(effect!=0),0.,p)
    return p,effect


def influence(x):
    x=_check(x);mu=x.mean(axis=0);sd=x.std(axis=0,ddof=0)
    if np.any(sd<=0):raise ValueError('degenerate variance')
    z=(x-mu)/sd
    psi=(z-.5*(mu/sd)*(z*z-1))*np.sqrt(245)
    theta=mu/sd*np.sqrt(245)
    return theta[::2]-theta[1::2],psi[:,::2]-psi[:,1::2]


def brownian_reference(paths=50000,steps=2048,seed=91700401):
    rng=np.random.default_rng(seed);values=[];grid=np.arange(1,steps+1)/steps
    for start in range(0,paths,256):
        b=rng.normal(size=(min(256,paths-start),steps)).cumsum(axis=1)/np.sqrt(steps)
        bridge=b-b[:,-1,None]*grid
        values.extend(np.abs(b[:,-1])/np.sqrt(np.mean(bridge*bridge,axis=1)))
    return np.sort(values)


def self_normalized_test(x,reference):
    theta,psi=influence(x);n=len(psi)
    bridge=(psi-psi.mean(axis=0)).cumsum(axis=0)
    variance=np.sum(bridge*bridge,axis=0)/(n*n)
    stat=np.divide(np.sqrt(n)*np.abs(theta),np.sqrt(variance),out=np.zeros(2),where=variance>0)
    stat=np.where((variance==0)&(theta!=0),np.inf,stat)
    p=(len(reference)-np.searchsorted(reference,stat,side='left')+1)/(len(reference)+1)
    return p,theta


def evaluate(x,reference,methods):
    result={}
    for method in methods:
        if method=='sn_if':p,e=self_normalized_test(x,reference)
        else:p,e=group_test(x,int(method[-1]))
        result[method]=(holm_adjust(p),e)
    return result


def summarize(rows):
    d=pd.DataFrame(rows);summary=[]
    for (phase,n,kind,delta,method),g in d.groupby(['phase','n_sessions','kind','delta','method'],sort=False):
        metric='reject_any' if delta==0 else 'positive_C1'
        hits=int(g[metric].sum());total=len(g)
        summary.append({'phase':phase,'n_sessions':n,'kind':kind,'delta':delta,'method':method,
                        'metric':metric,'replications':total,'count':hits,'rate':hits/total,
                        'upper95':float(beta.ppf(.95,hits+1,total-hits)) if hits<total else 1.,
                        'ci_lo':float(beta.ppf(.025,hits,total-hits+1)) if hits else 0.,
                        'ci_hi':float(beta.ppf(.975,hits+1,total-hits)) if hits<total else 1.,
                        'null_C2_rate':float(g.reject_C2.mean())})
    return d,pd.DataFrame(summary)


def simulate(phase,horizons,reps,reference):
    rows=[];base=91710000 if phase=='A' else 91810000
    methods=METHODS if phase=='A' else ('group_t4',)
    case=0
    for n in horizons:
        for kind,delta in [(k,0.) for k in KINDS]+[(k,.4) for k in ('iid','cluster_t5','slow_regime')]:
            for rep in range(reps):
                seed=base+case*1000+rep
                x=generate(kind,n,seed,delta)
                for method,(p,e) in evaluate(x,reference,methods).items():
                    rows.append({'phase':phase,'n_sessions':n,'kind':kind,'delta':delta,'method':method,
                                 'rep':rep,'data_seed':seed,'p_C1':p[0],'p_C2':p[1],
                                 'estimate_C1':e[0],'estimate_C2':e[1],
                                 'reject_any':bool((p<=.05).any()),'reject_C2':bool(p[1]<=.05),
                                 'positive_C1':bool(p[0]<=.05 and e[0]>0)})
            case+=1
            print(f'{phase}: n={n}, {kind}, delta={delta:g}, reps={reps}',flush=True)
    return summarize(rows)


def choose_horizon(summary):
    null=summary[(summary.method=='group_t4')&(summary.delta==0)]
    kinds=set(summary[summary.delta==0].kind)
    for n,g in null.groupby('n_sessions',sort=True):
        if set(g.kind)==kinds and (g.upper95<=.075).all():
            return {'n_sessions':int(n),'screen_size_passed':True,'method':'group_t4'}
    return {'n_sessions':8064,'screen_size_passed':False,'method':'group_t4'}


def gaussian_requirement(rho,delta,base=.3):
    """Delta-method iid paired-Gaussian planning approximation, not a guarantee."""
    v0=2*245*(1-rho)+base**2*(1-rho*rho)
    s1=base+delta
    v1=2*245*(1-rho)+.5*(s1*s1+base*base-2*s1*base*rho*rho)
    return (norm.ppf(1-.05/4)*np.sqrt(v0)+norm.ppf(.8)*np.sqrt(v1))**2/delta**2


def diagnostic_outputs(run):
    rows=[]
    for rho in (.5,.8,.95,.99):
        n=int(np.ceil(gaussian_requirement(rho,.4)))
        rows.append({'rho':rho,'delta':.4,'power':.8,'approx_n':n,'years_245':n/245,
                     'approx_mde_504':brentq(lambda delta:gaussian_requirement(rho,delta)-504,.0001,10.)})
    pd.DataFrame(rows).to_csv(run/'outputs/iid_gaussian_planning.csv',index=False)
    source=ROOT/'backtest/output/runs/20260914-forward-pipeline-r1'
    meta=json.loads((source/'manifest.json').read_text());series={}
    for name in ('futures_ew','futures_always_long','current_two_pool','ew_two_pool'):
        path=source/f'outputs/rehearsal_{name}_3bps.csv'
        a=artifact_record(path,source)
        if a!=next(v for v in meta['artifacts'] if v['path']==a['path']):raise ValueError('historical input hash changed')
        shutil.copyfile(path,run/'inputs'/path.name)
        series[name]=pd.read_csv(path,index_col='date').ret.iloc[1:]
    rows=[]
    for label,a,b in [('C1','futures_ew','futures_always_long'),('C2','current_two_pool','ew_two_pool')]:
        pair=pd.concat([series[a],series[b]],axis=1)
        if pair.isna().any().any():raise ValueError('misaligned historical returns')
        x=np.tile(pair.to_numpy(),(1,2));theta,psi=influence(x);s=pd.Series(psi[:,0])
        rows.append({'comparison':label,'n':len(x),'return_correlation':pair.iloc[:,0].corr(pair.iloc[:,1]),
                     'sharpe_difference_ddof0':theta[0],'if_acf1':s.autocorr(1),'if_acf20':s.autocorr(20),
                     'if_acf60':s.autocorr(60),'used_for_method_selection':False})
    pd.DataFrame(rows).to_csv(run/'outputs/historical_dependence_descriptive.csv',index=False)


def main():
    ap=argparse.ArgumentParser();ap.add_argument('--run-id',required=True);ap.add_argument('--smoke',action='store_true');a=ap.parse_args()
    run=create_run_dir(ROOT/'backtest/output/runs',a.run_id)
    shutil.copyfile(PLAN,run/'inputs/prereg.md')
    for path in ('backtest/robust_sharpe_inference.py','backtest/research_statistics.py','tests/test_robust_sharpe_inference.py'):
        shutil.copyfile(ROOT/path,run/'inputs'/path.replace('/','__'))
    write_manifest(run,{'status':'running','created_utc':datetime.now(timezone.utc).isoformat(),'git':git_state(ROOT)})
    reference=brownian_reference(paths=1000 if a.smoke else 50000)
    np.save(run/'inputs/brownian_absolute_reference.npy',reference)
    d,s=simulate('A',HORIZONS,5 if a.smoke else 400,reference)
    d.to_csv(run/'outputs/A_replications.csv',index=False);s.to_csv(run/'outputs/A_summary.csv',index=False)
    choice=choose_horizon(s)
    (run/'outputs/B_frozen_choice.json').write_text(json.dumps({**choice,'frozen_utc':datetime.now(timezone.utc).isoformat()},indent=2))
    print('B frozen choice: '+json.dumps(choice),flush=True)
    d,b=simulate('B',(choice['n_sessions'],),5 if a.smoke else 800,reference)
    d.to_csv(run/'outputs/B_replications.csv',index=False);b.to_csv(run/'outputs/B_summary.csv',index=False)
    diagnostic_outputs(run)
    verdict={**choice,'development_only':a.smoke,'confirmation_size_passed':bool((b[b.delta.eq(0)].upper95<=.075).all()),
             'confirmation_power_target_met':bool((b[b.delta.eq(.4)&b.kind.isin(['iid','cluster_t5'])].rate>=.8).all()),
             'go_enabled':False,'forward_protocol_changed':False}
    (run/'outputs/verdict.json').write_text(json.dumps(verdict,indent=2))
    files=[artifact_record(p,run) for sub in ('inputs','outputs','logs') for p in sorted((run/sub).rglob('*')) if p.is_file()]
    write_manifest(run,{'status':'complete','git':git_state(ROOT),'completed_utc':datetime.now(timezone.utc).isoformat(),'artifacts':files,**verdict})
    print(b.round(4).to_string(index=False),flush=True);print(json.dumps(verdict),flush=True)


if __name__=='__main__':main()
