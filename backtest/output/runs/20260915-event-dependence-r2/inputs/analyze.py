"""Second-round attribution and random-calendar diagnostic on frozen r1 inputs."""
from pathlib import Path
import sys,json,hashlib,shutil
import numpy as np,pandas as pd
ROOT=Path.cwd();sys.path.insert(0,str(ROOT))
from backtest.engine import run_strategy
from backtest.run_manifest import artifact_record,write_manifest
RUN=Path(__file__).resolve().parents[1];OUT=RUN/'outputs';OUT.mkdir(exist_ok=True)
SOURCE=RUN.parent/'20260915-event-dependence-r1';IN=RUN/'inputs/data';IN.mkdir(exist_ok=True)
source_manifest=json.loads((SOURCE/'manifest.json').read_text())
source_hashes={r['path']:r for r in source_manifest['artifacts']}
used=[]
def read(name,index=False):
    rel='outputs/'+name;src=SOURCE/rel;rec=artifact_record(src,SOURCE)
    assert rec==source_hashes[rel],name
    dst=IN/name
    if dst.exists():assert dst.read_bytes()==src.read_bytes()
    else:shutil.copyfile(src,dst)
    used.append(artifact_record(dst,RUN))
    return pd.read_csv(dst,index_col=0 if index else None,parse_dates=True if index else None)
cat=read('candidate_catalog.csv');pos=read('candidate_positions.csv',True)
ref=read('reference_positions.csv',True);market=read('market.csv',True)
mask=read('event_masks.csv',True);windows=read('event_windows.csv')
full=read('full_sensitivity.csv');worst=read('worst_tv_comparisons.csv')
oldreview=read('review_candidates.csv');coverage=read('registry_coverage.csv')
write_manifest(RUN,{'status':'running','design':'inputs/design.md','source':SOURCE.name})
shutil.copyfile(ROOT/'backtest/engine.py',RUN/'inputs/engine.py')
shutil.copyfile(ROOT/'backtest/run_manifest.py',RUN/'inputs/run_manifest.py')
focus=set(oldreview.candidate)
wide=full[(full.lag==1)&full['mask'].eq('all_20')&full.both_metrics_flip]
focus.update(wide.candidate)
focuscat=cat[cat.candidate.isin(focus)].copy()
assert len(focuscat)==26
focuscat.to_csv(OUT/'focus_catalog.csv',index=False)

def metrics(s):
    nav=np.r_[1.,np.cumprod(1+s.to_numpy())]
    return dict(sharpe=float(s.mean()/s.std(ddof=1)*np.sqrt(245)),total_return=float(nav[-1]-1),
                maxdd=float((nav/np.maximum.accumulate(nav)-1).min()))
def ledger(r,lag):
    p=pos[r['name']].dropna();b=ref[r['reference']].reindex(p.index)
    if lag==2:p=p.shift(1).fillna(0);b=b.shift(1).fillna(0)
    ca=market['carry'] if r['model']=='carry' else None
    return run_strategy(p,market.underlying,carry=ca),run_strategy(b,market.underlying,carry=ca)

# Put each shortlisted LF candidate against its historical and current same-direction references.
same=full[full.name.isin(focuscat.name)&full['mask'].isin(['all_1','all_5','all_10','all_20'])].copy()
same.to_csv(OUT/'same_mapping_comparisons.csv',index=False)
original_reasons={
 'equal-weight-original-grid':'原EW网格未选参数；早期参数选择轨迹不完整，不能给每行虚构单独否决原因。',
 'momentum-transform':'原族代表需跨期稳定并达到切主收益门槛；本行不一定是当年的族代表；旧三窗选择后来改为train/val。',
 'staged-entry':'原为long-flat探索，除全窗收益外，关注验证期优势集中于少数日期、回撤及提前进/出机制；非仅2024后总收益排序。',
 'pair-set':'原long-flat；配对IC增量闸未过，worst(train,val)要求至少+0.15且换手/集中度不恶化；2024后只展示。',
 'signal-generator-smoothing':'原预筛描述性；09-14固定生成器回放未形成替换证据，不是原正式STOP规格。',
 'signal-generator-cusum-hamilton':'原预筛描述性；09-14固定生成器回放未形成替换证据，不是原正式STOP规格。',
 'threshold-and-microcap-grids':'原阈值比较在train/val，blend增益小且选择校正未过；本轮仅blend子网格。',
 'b2-industry-neutral':'原行业中性拆分关注独立信息与收益层；纯风格IC较高不等于收益增量已证实。',
 'pair-weighting-grid':'原静态权重族选择校正未过，收益层代表worst(train,val)亦低于等权；本行是20日窗口事后敏感候选。'}
study_rows=[]
for r in focuscat.itertuples():
    row=dict(candidate=r.candidate,name=r.name,study=r.study,original_reason=original_reasons[r.study])
    for reference,label in [('ew_sym','current_sym'),('ew_lf','same_lf'),('slope_lf','current_spot')]:
        s=same[same.name.eq(r.name)&same.reference.eq(reference)&same.lag.eq(1)&same['mask'].eq('all_10')]
        if len(s):
            t=s.iloc[0];row[label+'_original_delta_sharpe']=t.delta_sharpe;row[label+'_neutral_delta_sharpe']=t.neutral_delta_sharpe
    row['review_reading']='事件集中但执行时点敏感' if r.name.startswith('grid_ew_') else '仅20日小幅双指标反转，执行不稳' if r.study=='pair-weighting-grid' else '跨映射反转；同多头基准未反转'
    study_rows.append(row)
pd.DataFrame(study_rows).to_csv(OUT/'rejection_review.csv',index=False)

# Freeze exact documents supporting rejection-mechanism descriptions.
docs=set()
for r in coverage[coverage.study.isin(focuscat.study)].itertuples():
    d=json.loads(r.documents)
    docs.update(d.get('spec',[]));docs.update(d.get('report',[]))
docs.add('docs/plans/2026-09-14-incumbent-parameter-lineage.md')
for doc in sorted(docs):
    src=ROOT/doc
    if src.exists():
        dst=RUN/'inputs/evidence'/doc;dst.parent.mkdir(parents=True,exist_ok=True);shutil.copyfile(src,dst)

events=windows[windows.window.eq(10)]
event_rows=[];daily_rows=[];year_rows=[];max_identity_error=0.
for r in focuscat.to_dict('records'):
    for lag in [1,2]:
        x,y=ledger(r,lag);p=x.pos_eff;q=y.pos_eff;idx=x.index
        for year in sorted(set(idx.year)):
            sel=idx.year==year
            for tag in ['none','all_10']:
                xx=x.ret.loc[sel].copy();yy=y.ret.loc[sel].copy()
                if tag!='none':
                    ev=mask[tag].reindex(xx.index);xx=xx.mask(ev,0);yy=yy.mask(ev,0)
                mx,my=metrics(xx),metrics(yy)
                year_rows.append(dict(candidate=r['candidate'],lag=lag,year=year,mask=tag,n=len(xx),
                  **{'candidate_'+k:v for k,v in mx.items()},**{'reference_'+k:v for k,v in my.items()},
                  relative_log=float(np.log1p(xx).sum()-np.log1p(yy).sum())))
        for e in events.itertuples():
            dates=mask.index[mask[e.event]];dates=dates.intersection(idx)
            if len(dates)!=10:continue
            anchor=dates[0];pe=p.loc[anchor];qe=q.loc[anchor];u=market.underlying.loc[dates]
            initial=(pe-qe)*u
            response=((p.loc[dates]-pe)-(q.loc[dates]-qe))*u
            carry_gap=x['carry'].loc[dates]-y['carry'].loc[dates]
            cost_gap=-x.cost.loc[dates]+y.cost.loc[dates]
            netgap=x.ret.loc[dates]-y.ret.loc[dates]
            err=float((netgap-initial-response-carry_gap-cost_gap).abs().max())
            max_identity_error=max(max_identity_error,err);assert err<1e-12
            ld=np.log1p(x.ret.loc[dates])-np.log1p(y.ret.loc[dates])
            pchange=p.index[(p.diff().fillna(p.iloc[0])!=0)&(p.index<=anchor)]
            qchange=q.index[(q.diff().fillna(q.iloc[0])!=0)&(q.index<=anchor)]
            later=idx[(idx>=anchor)][:20]
            pf=later[p.reindex(later).to_numpy()!=pe];qf=later[q.reindex(later).to_numpy()!=qe]
            event_rows.append(dict(candidate=r['candidate'],name=r['name'],lag=lag,event=e.event,
               candidate_initial=pe,reference_initial=qe,first_day_log_gap=ld.iloc[0],later_days_log_gap=ld.iloc[1:].sum(),event_log_gap=ld.sum(),
               initial_direction_simple_gap=initial.sum(),response_simple_gap=response.sum(),carry_simple_gap=carry_gap.sum(),cost_simple_gap=cost_gap.sum(),net_simple_gap=netgap.sum(),
               candidate_event_return=(1+x.ret.loc[dates]).prod()-1,reference_event_return=(1+y.ret.loc[dates]).prod()-1,
               candidate_last_change=str(pchange[-1].date()) if len(pchange) else '',reference_last_change=str(qchange[-1].date()) if len(qchange) else '',
               candidate_next_change=str(pf[0].date()) if len(pf) else '',reference_next_change=str(qf[0].date()) if len(qf) else ''))
            for date in dates:
                daily_rows.append(dict(candidate=r['candidate'],lag=lag,event=e.event,date=str(date.date()),underlying=market.underlying.loc[date],
                    position=p.loc[date],reference_position=q.loc[date],candidate_return=x.ret.loc[date],reference_return=y.ret.loc[date],
                    initial_direction_gap=initial.loc[date],response_gap=response.loc[date],carry_gap=carry_gap.loc[date],cost_gap=cost_gap.loc[date],relative_log_gap=ld.loc[date]))
event_df=pd.DataFrame(event_rows);event_df.to_csv(OUT/'event_decomposition.csv',index=False)
pd.DataFrame(daily_rows).to_csv(OUT/'event_daily_paths.csv',index=False)
pd.DataFrame(year_rows).to_csv(OUT/'yearly_results.csv',index=False)
cols=['first_day_log_gap','later_days_log_gap','event_log_gap','initial_direction_simple_gap','response_simple_gap','carry_simple_gap','cost_simple_gap','net_simple_gap']
event_df.groupby(['candidate','name','lag'])[cols].sum().reset_index().to_csv(OUT/'event_decomposition_summary.csv',index=False)
print('DECOMPOSED',len(event_df),'candidate/event/lag rows',flush=True)

# Random windows are fixed across candidates; same counts by year and 70 days per draw.
cal=market.index;rng=np.random.default_rng(20260915);B=2000
groups={2018:2,2019:2,2024:1,2025:2};draws=[];window_records=[]
for b in range(B):
    all_dates=[]
    for year,nseg in groups.items():
        inds=np.flatnonzero(cal.year==year);chosen=[]
        while len(chosen)<nseg:
            start=int(rng.integers(0,len(inds)-9));s=set(inds[start:start+10])
            if not any(s.intersection(t) for t in chosen):
                chosen.append(s);window_records.append(dict(draw=b,year=year,start=str(cal[inds[start]].date()),end=str(cal[inds[start+9]].date())))
        for s in chosen:all_dates.extend(s)
    draw=np.array(sorted(all_dates));assert len(draw)==len(set(draw))==70;draws.append(draw)
draws=np.array(draws);pd.DataFrame(window_records).to_csv(OUT/'random_windows.csv',index=False)
eligible=cat[cat.reference.eq('ew_sym')&cat.start.le('2018-01-01')&cat.end.ge('2025-12-31')].copy().reset_index(drop=True)
assert focus.issubset(set(eligible.candidate))
random_summaries=[];count_rows=[];trial_rows=[];direct_errors=[]
for lag in [1,2]:
    X=[];Y=[];Ns=[]
    for r in eligible.to_dict('records'):
        x,y=ledger(r,lag);Ns.append(len(x));X.append(x.ret.reindex(cal).fillna(0).to_numpy());Y.append(y.ret.reindex(cal).fillna(0).to_numpy())
    X=np.array(X);Y=np.array(Y);N=np.array(Ns)
    lx=np.log1p(X)-np.log1p(Y);Sx=X.sum(axis=1);Sy=Y.sum(axis=1);Qx=(X*X).sum(axis=1);Qy=(Y*Y).sum(axis=1)
    def sharp_sums(s,q):return (s/N)/np.sqrt((q-s*s/N)/(N-1))*np.sqrt(245)
    base_delta=sharp_sums(Sx,Qx)-sharp_sums(Sy,Qy);base_log=lx.sum(axis=1)
    actual=full[(full.lag==lag)&full['mask'].eq('all_10')].set_index('candidate').loc[eligible.candidate]
    assert np.allclose(base_delta,actual.delta_sharpe)
    orig_lag=base_delta<0;deltas=[];logs=[]
    scope=eligible.comparison_scope.to_numpy();scope_names=['current_EW_symmetric','current_EW_cross_mapping']
    for b,inds in enumerate(draws):
        dx=sharp_sums(Sx-X[:,inds].sum(axis=1),Qx-(X[:,inds]**2).sum(axis=1))
        dy=sharp_sums(Sy-Y[:,inds].sum(axis=1),Qy-(Y[:,inds]**2).sum(axis=1))
        delta=dx-dy;log=base_log-lx[:,inds].sum(axis=1);deltas.append(delta);logs.append(log)
        flips=orig_lag&(delta>0);both=flips&(base_log<0)&(log>0)
        for name in scope_names:
            sel=scope==name;count_rows.append(dict(draw=b,lag=lag,comparison_scope=name,n=int(sel.sum()),originally_lagging=int((orig_lag&sel).sum()),sharpe_flips=int((flips&sel).sum()),both_flips=int((both&sel).sum())))
        # Direct recomputation checks first and last candidate and a focused candidate on fixed draws.
        if b in [0,1,1999]:
            for j in [0,len(eligible)-1,int(eligible.index[eligible.candidate.eq(next(iter(sorted(focus))))][0])]:
                r=eligible.iloc[j];xx,yy=ledger(r,lag)
                xx=xx.ret.mask(xx.index.isin(cal[inds]),0);yy=yy.ret.mask(yy.index.isin(cal[inds]),0)
                direct=metrics(xx)['sharpe']-metrics(yy)['sharpe'];directlog=np.log1p(xx).sum()-np.log1p(yy).sum()
                err=max(abs(delta[j]-direct),abs(log[j]-directlog));direct_errors.append(err);assert err<1e-9
    deltas=np.array(deltas);logs=np.array(logs)
    for j,r in enumerate(eligible.itertuples()):
        row=dict(candidate=r.candidate,name=r.name,study=r.study,lag=lag,comparison_scope=r.comparison_scope,n=r.n,draws=B,
           originally_lagging=bool(orig_lag[j]),original_delta_sharpe=base_delta[j],actual_neutral_delta_sharpe=actual.iloc[j].neutral_delta_sharpe,
           actual_event_log_gap=actual.iloc[j].event_relative_log,actual_nonevent_log_gap=actual.iloc[j].nonevent_relative_log,
           random_sharpe_flip_fraction=float(np.mean(orig_lag[j]&(deltas[:,j]>0))),
           random_both_flip_fraction=float(np.mean(orig_lag[j]&(deltas[:,j]>0)&(base_log[j]<0)&(logs[:,j]>0))),
           random_event_gap_at_least_as_adverse_fraction=float(np.mean((base_log[j]-logs[:,j])<=actual.iloc[j].event_relative_log)),
           random_neutral_delta_q05=float(np.quantile(deltas[:,j],.05)),random_neutral_delta_median=float(np.median(deltas[:,j])),random_neutral_delta_q95=float(np.quantile(deltas[:,j],.95)))
        random_summaries.append(row)
        if r.candidate in focus:
            for b in range(B):trial_rows.append(dict(candidate=r.candidate,lag=lag,draw=b,neutral_delta_sharpe=deltas[b,j],nonevent_relative_log=logs[b,j]))
    print('RANDOM CHECKED',lag,len(eligible),B,flush=True)
random_df=pd.DataFrame(random_summaries);random_df.to_csv(OUT/'random_calendar_summary.csv',index=False)
random_df[random_df.candidate.isin(focus)].to_csv(OUT/'focus_random_summary.csv',index=False)
counts=pd.DataFrame(count_rows);counts.to_csv(OUT/'random_flip_counts.csv',index=False)
pd.DataFrame(trial_rows).to_csv(OUT/'focus_random_trials.csv',index=False)

# Data-independent window/identity checks in addition to direct recomputation above.
wr=pd.DataFrame(window_records)
assert wr.groupby('draw').size().eq(7).all()
for year,n in groups.items():assert wr[wr.year.eq(year)].groupby('draw').size().eq(n).all()
assert np.allclose(event_df.first_day_log_gap+event_df.later_days_log_gap,event_df.event_log_gap)
assert np.allclose(event_df.initial_direction_simple_gap+event_df.response_simple_gap+event_df.carry_simple_gap+event_df.cost_simple_gap,event_df.net_simple_gap)
verification=dict(status='passed',source_run=SOURCE.name,input_hashes_checked=len(used),focus_specifications=len(focuscat),
 event_decompositions=len(event_df),daily_decomposition_error=max_identity_error,random_calendar_draws=B,random_eligible_comparisons=len(eligible),
 random_direct_metric_checks=len(direct_errors),random_max_direct_error=max(direct_errors),random_windows_per_draw=7,random_days_per_draw=70,
 note='Descriptive random-calendar diagnostic, not a calibrated p-value or a fresh out-of-sample test.')
(OUT/'verification.json').write_text(json.dumps(verification,indent=2))
(OUT/'input_sources.json').write_text(json.dumps(used,indent=2))
print(json.dumps(verification,indent=2),flush=True)
print(random_df[random_df.candidate.isin(focus)&random_df.name.eq('grid_ew_L20_zw250_sm0')].to_string(index=False),flush=True)
