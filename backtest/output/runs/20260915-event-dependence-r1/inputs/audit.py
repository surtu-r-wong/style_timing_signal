"""Event attribution of previously rejected signal variants; descriptive only."""
from pathlib import Path
import sys, json, hashlib, shutil
import numpy as np
import pandas as pd
import yaml
ROOT = Path.cwd()
sys.path.insert(0, str(ROOT))
RUN = Path(__file__).resolve().parents[1]
OUT = RUN / 'outputs'
BT = ROOT / 'backtest/output'
SOURCE = BT / 'runs/20260914-grid-reassessment-r1'
from backtest.run_manifest import artifact_record, write_manifest

SOURCES = {}
def freeze(path):
    path = Path(path)
    if not path.is_absolute(): path = ROOT / path
    key = str(path.relative_to(ROOT))
    dst = RUN / 'inputs/data' / key
    if key not in SOURCES:
        dst.parent.mkdir(parents=True, exist_ok=True)
        if dst.exists():
            assert dst.read_bytes() == path.read_bytes(), f'input drift: {key}'
        else: shutil.copyfile(path, dst)
        SOURCES[key] = artifact_record(dst, RUN)
    return dst

def csv(path):
    d = pd.read_csv(freeze(path))
    dt = next((c for c in ['date','trade_date','report_date'] if c in d), d.columns[0])
    d[dt] = pd.to_datetime(d[dt])
    return d.set_index(dt).sort_index()

def js(path): return json.loads(freeze(path).read_text())

EVENTS = [
 ('tariff_20180323','2018-03-23','tariff','301行动','https://ustr.gov/about-us/policy-offices/press-office/press-releases/2018/june/ustr-issues-tariffs-chinese-products'),
 ('tariff_20180619','2018-06-19','tariff','6月15日关税清单，端午后首个交易日','https://ustr.gov/about-us/policy-offices/press-office/press-releases/2018/june/ustr-issues-tariffs-chinese-products'),
 ('tariff_20190506','2019-05-06','tariff','5月5日升级威胁','https://www.axios.com/2019/05/05/trump-ratchets-up-us-china-trade-war-with-new-tariff-threat'),
 ('tariff_20190802','2019-08-02','tariff','8月1日升级威胁','https://www.axios.com/2019/08/01/donald-trump-china-tariffs-300-billion-10-percent'),
 ('policy_20240924','2024-09-24','policy','金融政策组合','https://english.www.gov.cn/news/202409/25/content_WS66f3602ec6d0868f4e8eb3c0.html'),
 ('tariff_20250403','2025-04-03','tariff','美国4月2日关税行动','https://www.govinfo.gov/app/details/DCPD-202500425'),
 ('geneva_20250512','2025-05-12','tariff','日内瓦周末进展，开盘前已公开；正式声明15:00另作敏感性','https://www.mofcom.gov.cn/syxwfb/art/2025/art_1079483db82e4b5591ffb65900ef4eac.html')]

def build():
    market = csv(BT/'entry_day_direction_20260915/spot_close.csv')
    ret = market.pct_change(fill_method=None).mean(axis=1,skipna=False).dropna().loc['2014-01-02':'2026-09-11']
    cal = ret.index
    carry = csv(SOURCE/'outputs/carry_comparison.csv')['fixed'].reindex(cal).fillna(0.)
    ew = csv(SOURCE/'inputs/equal_weight.csv')
    slope = csv(SOURCE/'inputs/slope20.csv').factor_value
    refs = {'ew_sym': np.sign(ew.factor_value).reindex(cal), 'ew_lf': (ew.factor_value>0).astype(float).reindex(cal),
            'slope_lf':(slope>0).astype(float).reindex(cal), 'ew_short':np.sign(ew.factor_value).clip(upper=0).reindex(cal)}
    records, positions, issues = [], {}, []
    def add(name,study,pos,mapping='sym',role='rejected_variant',detail=''):
        # Preserve the original native timeline; missing dates stay missing and are audited.
        pos = pos.sort_index().astype(float)
        assert pos.index.is_unique
        aligned = pos.reindex(cal)
        support = aligned.dropna()
        if len(support)<100:
            issues.append(dict(study=study,name=name,reason='less than 100 aligned observations')); return
        native = aligned.loc[support.index.min():support.index.max()]
        if native.isna().any():
            issues.append(dict(study=study,name=name,reason=f'internal calendar gaps: {int(native.isna().sum())}; longest contiguous segment used'))
            segments = native.notna().astype(int).groupby(native.isna().cumsum()).sum()
            seg = segments.idxmax(); native = native[native.isna().cumsum()==seg].dropna()
        if len(native)<100:return
        configs = [('carry','ew_sym')] if mapping=='sym' else [('carry','ew_short')] if mapping=='short' else [('carry','ew_lf'),('spot','slope_lf')]
        positions[name] = native
        for model, reference in configs:
            cid = f'{name}__{model}__{reference}'
            records.append(dict(candidate=cid,name=name,study=study,mapping=mapping,model=model,reference=reference,
                                role=role,detail=detail,start=str(native.index.min().date()),end=str(native.index.max().date()),
                                n=len(native),position_hash=hashlib.sha256(native.to_csv().encode()).hexdigest()))
    prices=csv(SOURCE/'inputs/style_prices.csv')
    from backtest.grid_reassessment import build_factors,INCUMBENTS
    from signals.equal_weight.generate_signal import load_pair_configs
    fac,params=build_factors(prices,load_pair_configs(freeze(SOURCE/'inputs/config_4pairs.csv')))
    for family,col in [('ew','factor_value'),('slope','factor_value')]:
        expected=ew[col] if family=='ew' else slope
        assert np.allclose(fac[INCUMBENTS[family]].round(4).reindex(expected.index),expected,atol=1e-10,rtol=0)
    for p in params:
        name=p['name']
        if name in INCUMBENTS.values():continue
        study='equal-weight-original-grid' if p['grid']=='ew40' else 'momentum-transform'
        add('grid_'+name,study,np.sign(fac[name]),role='unselected_grid',detail=json.dumps(p))
        add('grid_lf_'+name,study,(fac[name]>0).astype(float),'lf','unselected_grid',json.dumps(p))
    print('grid built',len(records),flush=True)
    from backtest.mapping_probe import build_variant_positions,INCUMBENT
    for v,pos in build_variant_positions(ew.factor_value).items():
        if v==INCUMBENT:continue
        add('mapping_'+str(v),'mapping-grid-32',pos,'lf',detail='original mixed-direction mapping; legacy LF benchmark; current spot comparison is hypothetical when short exposure exists')
    from backtest.staged_entry_probe import build_candidates,build_fast_sweep,build_threshold_sweep
    staged={**build_candidates(ew.factor_value_raw,ew.factor_value,.4,.6),
            **build_fast_sweep(ew.factor_value_raw,ew.factor_value,.4,.6),
            **build_threshold_sweep(ew.factor_value_raw,ew.factor_value,.4,.6)}
    for name,pos in staged.items():
        if 'incumbent' not in name:add('staged_'+name,'staged-entry',pos,'lf')
    from backtest.pair_weighting_probe import weight_grid,combine
    pairs=ew[[f'pair_0{i}_factor_20' for i in range(1,5)]].dropna()
    for w in weight_grid():
        if w==(.25,.25,.25,.25):continue
        add('weight_'+str(w),'pair-weighting-grid',np.sign(combine(pairs,w)))
    from backtest.pair_set_probe import build_factor
    for name in ['B_500_1000_2000','C_1000_only']:
        f=build_factor(prices,name)
        add('pairset_'+name,'pair-set',(f>0).astype(float),'lf',detail='2 of 3 original challengers; dividend variant requires additional source')
    from backtest.fusion_probe import fuse_equal
    f=fuse_equal(fac['ew_L20_zw40_sm5'],fac['slope_L20s0_zw120_sm0'])
    add('fusion_equal','fusion-slope20',(f>0).astype(float),'lf')
    add('fusion_equal_sym','fusion-slope20',np.sign(f),detail='original symmetric reference')
    f=csv(BT/'runs/20260914-fixed-candidates-r1/outputs/candidate_factors.csv')
    for name in f:
        if name=='incumbent':continue
        study='signal-generator-smoothing' if name.startswith('DEMA') or name=='raw' else 'signal-generator-cusum-hamilton'
        add('fixed_'+name,study,np.sign(f[name]),role='not_adopted_fixed')
        add('fixed_lf_'+name,study,(f[name]>0).astype(float),'lf','not_adopted_fixed')
    # Exact original 5d20z production variant, already designated non-production.
    f=csv(ROOT/'output/equal_weight/equal_weight_signal_5d20z.csv').factor_value
    add('ew_5d20z','equal-weight-5d20z',np.sign(f))
    add('ew_5d20z_lf','equal-weight-5d20z',(f>0).astype(float),'lf')
    from backtest.reversal_probe import GRID_A,GRID_B1,GRID_B2,b1_position,b2_position
    from backtest.momentum_scan import momentum_pair_factor
    paircols=[p.effective_columns() for p in load_pair_configs(freeze(SOURCE/'inputs/config_4pairs.csv'))]
    for j,p in enumerate(GRID_A):
        add(f'reversal_A_{j}','spread-short-reversal',-np.sign(momentum_pair_factor(prices,paircols,**p)),detail=json.dumps(p))
    for v in GRID_B1+GRID_B2:
        pos=b1_position(ret,v[1],v[2],v[3]) if v[0]=='B1' else b2_position(ret,v[1],v[3])
        add('trigger_'+str(v),'index-short-trigger',pos,'short',detail='warmup begins 2014 instead of original 2013; early history descriptive only')
    print('price candidates built',len(records),flush=True)
    from backtest.rotation_probe import hold_position
    def reps(study,signals,verdict):
        v=pd.read_csv(freeze(BT/verdict))
        for r in v.itertuples():
            if r.family not in signals:continue
            s=signals[r.family][r.best_form]
            pos=hold_position(s*float(r.direction),int(r.best_k))
            add(f'{study}_{r.family}',study,pos,role='frozen_representative',detail=f'{r.best_form}; k={r.best_k}; direction={r.direction}; original offset0')
    from backtest.basis_term_probe import build_signals as basis
    from backtest.consensus_axis_probe import build_signals as consensus
    from backtest.money_flow_axis_probe import build_signals as money
    from backtest.new_high_axis_probe import build_signals as breadth
    from backtest.option_axis_probe import build_option_signals
    from backtest.thermo_probe import build_thermo_signals
    from backtest.long_axes_probe import build_long_signals
    reps('futures-basis-term-structure',basis(csv(BT/'basis_term_series.csv')),'basis_term_probe_verdicts.csv')
    reps('analyst-revision',consensus(csv(BT/'consensus_revision_series.csv')),'consensus_axis_probe_verdicts.csv')
    reps('money-flow-residual-axis',money(csv(BT/'money_flow_series.csv')),'money_flow_axis_probe_verdicts.csv')
    reps('new-high-participation',breadth(csv(BT/'new_high_breadth_series.csv')),'new_high_axis_probe_verdicts.csv')
    reps('index-option-implied-vol',build_option_signals(csv(BT/'option_iv_IO.csv'),csv(BT/'option_iv_MO.csv')),'option_axis_probe_verdicts.csv')
    reps('thermo-probe',build_thermo_signals(csv(BT/'thermometer.csv'),csv(BT/'market_turnover.csv').amt_yuan),'thermo_probe_verdicts.csv')
    reps('long-axes-probes',build_long_signals(carry, csv(BT/'breadth.csv')),'long_axes_probe_verdicts.csv')
    print('representatives built',len(records),flush=True)
    catalogue=pd.DataFrame(records)
    catalogue.to_csv(OUT/'candidate_catalog.csv',index=False)
    pd.DataFrame(positions).to_csv(OUT/'candidate_positions.csv',index_label='date')
    pd.DataFrame(refs).to_csv(OUT/'reference_positions.csv',index_label='date')
    pd.DataFrame({'underlying':ret,'carry':carry}).to_csv(OUT/'market.csv',index_label='date')
    pd.DataFrame(issues).to_csv(OUT/'build_issues.csv',index=False)
    return catalogue,positions,refs,ret,carry


def sharp(x):
    sd=x.std(ddof=1)
    return x.mean()/sd*np.sqrt(245) if len(x)>1 and sd>0 else np.nan

def daily_engine(pos,u,c,lag):
    a=np.asarray(pos,float); eff=np.r_[np.zeros(lag),a[:-lag]]
    cost=.0003*np.abs(np.diff(np.r_[0.,eff]))
    return eff*(u+c/245)-cost,eff,cost

def masks(cal,u):
    out={'none':np.zeros(len(cal),bool)}; er=[]
    evm={}
    for eid,date,kind,label,src in EVENTS:
        i=cal.searchsorted(pd.Timestamp(date))
        if i>=len(cal):continue
        for days in [1,5,10,20]:
            m=np.zeros(len(cal),bool);m[i:min(i+days,len(cal))]=True
            evm[(eid,days)]=m
            er.append(dict(event=eid,anchor=date,kind=kind,label=label,source=src,window=days,
                           start=str(cal[i].date()),end=str(cal[min(i+days,len(cal))-1].date()),n=int(m.sum())))
            if days==10:out[eid]=m
    for days in [1,5,10,20]:
        out[f'all_{days}']=np.logical_or.reduce([v for (k,d),v in evm.items() if d==days])
        out[f'tariffs_{days}']=np.logical_or.reduce([v for (k,d),v in evm.items() if d==days and k!='policy_20240924'])
        out[f'policy_{days}']=evm[('policy_20240924',days)]
    # Geneva formal statement at Shanghai close; sensitivity to the next-day anchor.
    shifted=out['all_10'].copy() & ~evm[('geneva_20250512',10)]
    i=cal.searchsorted(pd.Timestamp('2025-05-13'));shifted[i:i+10]=True
    out['all_10_geneva_nextday']=shifted
    for n in [5,10,20]:
        m=np.zeros(len(cal),bool);m[np.argsort(np.abs(u))[-n:]]=True;out[f'abs_market_top{n}']=m
    pd.DataFrame(er).to_csv(OUT/'event_windows.csv',index=False)
    pd.DataFrame(out,index=cal).to_csv(OUT/'event_masks.csv',index_label='date')
    return out


def analyze(catalogue,positions,refs,u,carry):
    cal=u.index; em=masks(cal,u.to_numpy())
    rows,event_rows,checks=[],[],[]
    periods={'full':np.ones(len(cal),bool),'train':cal<='2020-12-31','val':(cal>='2021-01-01')&(cal<='2023-12-31'),'reused':cal>='2024-01-01'}
    # Compute on the full native calendar BEFORE period/event masks; never reset at event edges.
    for n,r in enumerate(catalogue.itertuples()):
        p=positions[r.name]; idx=p.index;ix=cal.get_indexer(idx)
        bench=refs[r.reference].reindex(idx).to_numpy()
        car=carry.loc[idx].to_numpy() if r.model=='carry' else np.zeros(len(idx))
        underlying=u.loc[idx].to_numpy()
        for lag in [1,2]:
            a,pe,cost=daily_engine(p.to_numpy(),underlying,car,lag)
            b,be,bcost=daily_engine(bench,underlying,car,lag)
            assert np.all(a>-1) and np.all(b>-1)
            da=np.log1p(a)-np.log1p(b)
            for period,pm in periods.items():
                sel=np.asarray(pm)[ix]
                if sel.sum()<60:continue
                aa,bb=a[sel],b[sel];delta=da[sel]
                sf,sb=sharp(aa),sharp(bb)
                for tag,mask in em.items():
                    m=mask[ix][sel]
                    ac=aa.copy();bc=bb.copy();ac[m]=0;bc[m]=0
                    off=delta[~m].sum();on=delta[m].sum();total=delta.sum()
                    assert np.isclose(total,on+off,atol=1e-10)
                    sa,sbb=sharp(ac),sharp(bc)
                    rows.append(dict(candidate=r.candidate,name=r.name,study=r.study,model=r.model,reference=r.reference,role=r.role,
                                     lag=lag,period=period,mask=tag,n=int(sel.sum()),event_days=int(m.sum()),
                                     sharpe_candidate=sf,sharpe_reference=sb,delta_sharpe=sf-sb,
                                     neutral_sharpe_candidate=sa,neutral_sharpe_reference=sbb,neutral_delta_sharpe=sa-sbb,
                                     total_relative_log=total,event_relative_log=on,nonevent_relative_log=off,
                                     event_share_of_gap=on/total if abs(total)>1e-10 else np.nan,
                                     rank_flip=bool(sf<sb and sa>sbb),
                                     both_metrics_flip=bool(sf<sb and total<0 and sa>sbb and off>0)))
            for eid,date,kind,label,src in EVENTS:
                g=cal.searchsorted(pd.Timestamp(date));loc=idx.searchsorted(pd.Timestamp(date))
                if loc>=len(idx) or idx[loc]!=cal[g]:continue
                m=em.get(eid,np.zeros(len(cal),bool))[ix]
                initial=pe[loc]
                future=pe[loc:min(loc+20,len(pe))]
                changes=np.flatnonzero(future!=initial)
                event_rows.append(dict(candidate=r.candidate,event=eid,lag=lag,pre_event_effective_position=initial,
                    reference_pre_event_position=be[loc],first_effective_change_date=str(idx[loc+changes[0]].date()) if len(changes) else '',
                    event_return=np.prod(1+a[m])-1,reference_event_return=np.prod(1+b[m])-1,
                    event_relative_log=da[m].sum(),event_gross_sum=np.sum(pe[m]*underlying[m]),
                    frozen_position_gross_sum=initial*np.sum(underlying[m]),
                    response_gross_sum=np.sum((pe[m]-initial)*underlying[m])))
        if n%150==0:print('scored',n,'/',len(catalogue),flush=True)
    result=pd.DataFrame(rows);result.to_csv(OUT/'event_attribution.csv',index=False)
    pd.DataFrame(event_rows).to_csv(OUT/'event_position_response.csv',index=False)
    chosen=result[(result.period=='full')&(result['mask']=='all_10')&(result.lag==1)]
    chosen.to_csv(OUT/'primary_comparisons.csv',index=False)
    chosen[chosen.rank_flip].to_csv(OUT/'rank_flips.csv',index=False)
    print('PRIMARY',chosen.groupby(['role','model']).agg(comparisons=('candidate','size'),originally_lagging=('delta_sharpe',lambda x:int((x<0).sum())),flips=('rank_flip','sum'),both=('both_metrics_flip','sum')).to_string(),flush=True)
    print(chosen[chosen.rank_flip][['name','study','model','reference','delta_sharpe','neutral_delta_sharpe','total_relative_log','nonevent_relative_log']].to_string(index=False),flush=True)
    return result


def main():
    write_manifest(RUN,{'status':'running','analysis':'descriptive event attribution','plan':'inputs/prereg.md'})
    catalogue,positions,refs,u,carry=build()
    result=analyze(catalogue,positions,refs,u,carry)
    (OUT/'input_sources.json').write_text(json.dumps(SOURCES,indent=2))
    # Snapshot imported local code for reproducibility; includes original frozen candidate definitions.
    for module in list(sys.modules.values()):
        filename=getattr(module,'__file__',None)
        if filename:
            path=Path(filename).resolve()
            if path.is_relative_to(ROOT) and path.suffix=='.py' and not path.is_relative_to(RUN):
                freeze(path)
    (OUT/'input_sources.json').write_text(json.dumps(SOURCES,indent=2))
    print('DONE CORE; awaiting independent verification and coverage report',flush=True)

if __name__=='__main__':main()
