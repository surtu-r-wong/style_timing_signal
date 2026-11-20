"""Offline reconstruction from complete anchors; never writes official weight columns."""
import argparse,csv,hashlib,json
from collections import defaultdict
from decimal import Decimal as D
from pathlib import Path

def read(p):return list(csv.DictReader(p.open()))
def sha(p):return hashlib.sha256(p.read_bytes()).hexdigest()
def save(out,name,rows,cols=None):
    with (out/name).open('w',newline='') as f:
        w=csv.DictWriter(f,fieldnames=cols or list(rows[0]));w.writeheader();w.writerows(rows)

def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--inputs',type=Path,required=True)
    parser.add_argument('--growth-inputs',type=Path,required=True)
    parser.add_argument('--output-dir',type=Path,required=True)
    parser.add_argument('--manifest',type=Path,required=True)
    parser.add_argument('--manifest-sha256',required=True)
    args=parser.parse_args();root=args.inputs.resolve();growthroot=args.growth_inputs.resolve();out=args.output_dir.resolve()
    manifest=args.manifest.resolve();assert sha(manifest)==args.manifest_sha256,'Manifest identity mismatch'
    package=manifest.parent
    for entry in json.loads(manifest.read_text())['files']:
        path=(package/entry['path']).resolve();assert path.is_relative_to(package)
        assert path.stat().st_size==entry['bytes'] and sha(path)==entry['sha256'],entry['path']
    assert root.is_relative_to(package) and growthroot.is_relative_to(package)
    out.mkdir(exist_ok=False)
    anchors=defaultdict(list)
    for r in read(root/'anchors.csv'):anchors[r['anchor_id']].append(r)
    levels={(r['series_code'],r['trade_date']):D(r['close']) for r in read(root/'official-levels.csv')}
    calendar=sorted({d for _,d in levels})
    factors={(r['ts_code'],r['trade_date']):r for r in read(growthroot/'stock-growth-factors.csv')}
    daily=[];target_weights=[];summary=[];states={}
    for case in read(root/'cases.csv'):
        aid=case['anchor_id'];rows=anchors[aid];members=[r['ts_code'] for r in rows];assert len(set(members))==len(members)
        dates=[d for d in calendar if case['validation_start']<=d<=case['validation_end']]
        anchor=rows[0]['anchor_date'];assert anchor in dates
        missing=[(c,d) for c in members for d in dates[1:] if (c,d) not in factors]
        unresolved=[(c,d) for c in members for d in dates[1:] if (c,d) in factors and factors[c,d]['reference_agrees']!='True']
        if missing or unresolved:
            summary.append(dict(case_id=case['case_id'],status='BLOCKED_ACTION_OR_PRICE_EVIDENCE',missing_keys=missing,unresolved_keys=unresolved));continue
        observed={r['ts_code']:D(r['weight_percent'])/100 for r in rows}
        total=sum(observed.values());assert abs(total-1)<D('.001')
        weights={anchor:{c:v/total for c,v in observed.items()}}
        for i in range(dates.index(anchor),0,-1):
            day,prev=dates[i],dates[i-1]
            raw={c:weights[day][c]/D(factors[c,day]['price_growth']) for c in members}
            denominator=sum(raw.values());weights[prev]={c:v/denominator for c,v in raw.items()}
        for i in range(dates.index(anchor)+1,len(dates)):
            day,prev=dates[i],dates[i-1]
            raw={c:weights[prev][c]*D(factors[c,day]['price_growth']) for c in members}
            denominator=sum(raw.values());weights[day]={c:v/denominator for c,v in raw.items()}
        opening=calendar[calendar.index(case['effective_date'])-1]
        assert opening in weights
        for c in members:
            target_weights.append(dict(case_id=case['case_id'],index_code=case['index_code'],effective_date=case['effective_date'],basket_role=case['basket_role'],ts_code=c,
                weight_at_prior_close=weights[opening][c],prior_close_date=opening,anchor_id=aid,anchor_date=anchor,anchor_weight_percent=observed[c]*100,
                source_type='derived_price_and_announced_action_drift',official_target_weight=False,source_date_semantics=rows[0]['weight_reference_date_semantics']))
        price_errors=[];tr_errors=[]
        for prev,day in zip(dates,dates[1:]):
            for kind,column,suffix in [('price','price_growth',''),('total_return','total_return_growth','CNY010')]:
                computed=sum(weights[prev][c]*D(factors[c,day][column]) for c in members)-1
                official=levels[case['index_code'][:6]+suffix,day]/levels[case['index_code'][:6]+suffix,prev]-1
                comparable=not case['stop_official_comparison_before'] or day<case['stop_official_comparison_before']
                error=(computed-official)*10000 if comparable else None
                if comparable:(price_errors if kind=='price' else tr_errors).append(error)
                daily.append(dict(case_id=case['case_id'],index_code=case['index_code'],effective_date=case['effective_date'],basket_role=case['basket_role'],trade_date=day,series_kind=kind,
                    reconstructed_return=computed,official_return=official,official_comparison_valid=comparable,error_bp=error,
                    relative_to_anchor='after' if day>anchor else 'at_or_before',
                    carried_quotes=sum(factors[c,day]['current_quote_basis']=='suspension_carry' for c in members)))
                states[case['case_id'],day,kind]=computed
        summary.append(dict(case_id=case['case_id'],status='COMPUTED',members=len(members),anchor_date=anchor,
            anchor_source=rows[0]['source_kind'],anchor_date_semantics=rows[0]['weight_reference_date_semantics'],
            validated_price_days=len(price_errors),validated_total_return_days=len(tr_errors),
            price_max_abs_error_bp=str(max(map(abs,price_errors))),tr_max_abs_error_bp=str(max(map(abs,tr_errors))),
            official_target_weights_certified=False))
    effects=[]
    for idx in ['932402.CSI','932403.CSI','932408.CSI','932409.CSI']:
        for eff in ['2025-12-15','2026-06-15']:
            for kind in ['price','total_return']:
                old=states.get((idx+'-'+eff+'-old',eff,kind));new=states.get((idx+'-'+eff+'-new',eff,kind))
                if old is not None and new is not None:
                    effects.append(dict(index_code=idx,effective_date=eff,series_kind=kind,old_basket_return=old,new_basket_return=new,
                        new_minus_old_bp=(new-old)*10000,weight_source='derived_from_complete_observed_anchors',official_exact_attribution=False))
    save(out,'derived-opening-weights.csv',target_weights)
    save(out,'daily-validation.csv',daily)
    save(out,'effective-day-effects.csv',effects,['index_code','effective_date','series_kind','old_basket_return','new_basket_return','new_minus_old_bp','weight_source','official_exact_attribution'])
    (out/'summary.json').write_text(json.dumps(summary,ensure_ascii=False,indent=2)+'\n')
    paths=[root/n for n in ['anchors.csv','cases.csv','official-levels.csv']]+[growthroot/'stock-growth-factors.csv']
    (out/'input-hashes.json').write_text(json.dumps([dict(path=str(p),sha256=sha(p)) for p in paths],indent=2)+'\n')
    (out/'audit_script.py').write_bytes(Path(__file__).read_bytes())
    files=[dict(path=p.name,bytes=p.stat().st_size,sha256=sha(p)) for p in sorted(out.iterdir())]
    (out/'run-manifest.json').write_text(json.dumps(dict(source_manifest=str(manifest),source_manifest_sha256=args.manifest_sha256,files=files),indent=2)+'\n')
    for p in out.iterdir():p.chmod(0o444)
    print(json.dumps(dict(cases=len(summary),computed=sum(r['status']=='COMPUTED' for r in summary),weights=len(target_weights),daily_rows=len(daily),effective_day_comparisons=len(effects))),flush=True)

if __name__=='__main__':main()
