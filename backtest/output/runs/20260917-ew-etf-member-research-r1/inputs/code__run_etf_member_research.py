"""Run the frozen ETF/member independent-leg research on office-derived inputs."""
from __future__ import annotations
import argparse,json,shutil
from pathlib import Path
from datetime import datetime,timezone
import numpy as np
import pandas as pd
from backtest.data import _expiry_from_symbol
from backtest.execution_ledger import contract_ledger,futures_weights
from backtest.intraday_flow_pilot import prepare_close_market,batch_close_ledgers,attribute_ledger
from backtest.run_intraday_flow_pilot import describe
from backtest.run_manifest import artifact_record,create_run_dir,write_manifest,git_state
from backtest.run_oi_research import side_episodes
from backtest.etf_member_delivery_contract import accept_delivery

ROOT=Path(__file__).resolve().parents[1]
RUN_ID="20260917-ew-etf-member-research-r1"
OFFICE=Path("/home/elfbob/claude-code/data_manager/requests/2026-09-17-style-timing-signal-etf-member-derived-inputs")
PRIOR=ROOT/"backtest/output/runs/20260917-ew-oi-research-r2"
SCENARIOS=[("close_3bps",3.,0),("close_10bps",10.,0),("second_close_3bps",3.,1)]

def build_targets(ew, features):
    if not ew.index.is_unique or not np.isfinite(ew).all():
        raise ValueError("invalid EW")
    base=np.sign(ew); long=base.clip(lower=0); short=base.clip(upper=0)
    targets=pd.DataFrame({"C__base":base,"L__base":long,"S__base":short,"C__half":long+.5*short})
    definitions=[]
    for source,f in features.items():
        if not f.index.equals(ew.index):
            raise ValueError("calendar mismatch")
        if f.valid.dtype!=bool or not np.isfinite(f.loc[f.valid,["value","r5"]]).all().all():
            raise ValueError("invalid valid inputs")
        for side,leg,other,direction in [("L",long,short,1),("S",short,long,-1)]:
            price=leg.where(~f.valid | (f.r5*direction>0),0.)
            selected=leg.where(~f.valid | ((f.r5*direction>0)&(f.value*direction>0)),0.)
            stem=source+"_"+side
            name=stem+"_signal"
            q=float(selected.abs().sum()/leg.abs().sum()) if leg.abs().sum() else 0.
            r=float(selected.abs().sum()/price.abs().sum()) if price.abs().sum() else 0.
            assert 0<=q<=1 and 0<=r<=1
            targets["C__"+stem+"_price"]=other+price
            targets["LEG__"+stem+"_price"]=price
            for key,value in [("C",other+selected),("LEG",selected),("Q",other+q*leg),
                              ("QLEG",q*leg),("D",other+r*price),("DLEG",r*price)]:
                targets[key+"__"+name]=value
            definitions.append({"candidate":name,"source":source,"side":side,"field":"value","q":q,"r":r,
                "base_days":int(leg.ne(0).sum()),"price_days":int(price.ne(0).sum()),"kept_days":int(selected.ne(0).sum()),
                "valid_days":int(f.valid.sum()),"fallback_days":int((~f.valid).sum()),
                "fallback_active_days":int((~f.valid&leg.ne(0)).sum())})
    return targets,definitions

def capture(run):
    delivery=OFFICE/"delivery/ew-etf-member-features-v1"
    m=json.loads((delivery/"manifest.json").read_text())
    assert m["contract_version"]=="ew-etf-member-features-v1" and m["end"]=="2026-09-16"
    for x in m["artifacts"]+m["supporting"]:
        assert artifact_record(delivery/x["path"],delivery)["sha256"]==x["sha256"]
    shutil.copytree(delivery,run/"inputs/office")
    source_dir=run/"inputs/sources";source_dir.mkdir()
    receipts=[]
    for name,x in m["sources"].items():
        p=Path("/home/elfbob/claude-code")/x["path"]
        assert artifact_record(p,p.parent)["sha256"]==x["sha256"]
        shutil.copyfile(p,source_dir/(name+p.suffix))
        receipts.append(x)
    oi=Path("/home/elfbob/claude-code/data_manager/requests/2026-09-17-style-timing-signal-futures-oi-derived-inputs/delivery/ew-oi-v1")
    m0=json.loads((oi/"manifest.json").read_text())
    rec=next(x for x in m0["artifacts"]+m0["supporting_files"] if x["path"]=="lifecycle_audit.csv")
    assert artifact_record(oi/"lifecycle_audit.csv",oi)["sha256"]==rec["sha256"]
    shutil.copyfile(oi/"lifecycle_audit.csv",source_dir/"lifecycle_audit.csv")
    for name in ["request.md","spec.md","feature_contract.json","response-01-office-2026-09-17.md","disposition.md"]:
        shutil.copyfile(OFFICE/name,run/"inputs"/("office__"+name))
    pm=json.loads((PRIOR/"manifest.json").read_text())
    for name in ["futures.csv","equal_weight.csv"]:
        p=PRIOR/"inputs"/name
        item=next(x for x in pm["artifacts"] if x["path"]=="inputs/"+name)
        assert artifact_record(p,PRIOR)==item
        shutil.copyfile(p,run/"inputs"/name)
    for rel in ["backtest/run_etf_member_research.py","backtest/etf_member_delivery_contract.py",
                "backtest/run_oi_research.py","backtest/intraday_flow_pilot.py","backtest/execution_ledger.py",
                "backtest/data.py","backtest/metrics.py","backtest/run_intraday_flow_pilot.py",
                "backtest/run_manifest.py","tests/test_etf_member_research.py"]:
        shutil.copyfile(ROOT/rel,run/"inputs"/("code__"+Path(rel).name))
    (run/"inputs/source_receipts.json").write_text(json.dumps(receipts,indent=2))

def accept(run):
    features,calendar,report=accept_delivery(run/"inputs")
    futures=pd.read_csv(run/"inputs/futures.csv",parse_dates=["date"])
    raw=pd.read_csv(run/"inputs/sources/contract_detail.csv",parse_dates=["date"])
    a=raw.set_index(["date","symbol"]).sort_index()
    b=futures.set_index(["date","symbol"]).sort_index()
    assert a.index.equals(b.index)
    np.testing.assert_array_equal(a[["oi","volume"]],b[["oi","volume"]])
    ew=pd.read_csv(run/"inputs/equal_weight.csv",index_col="date",parse_dates=True).factor_value
    report["execution_raw_oi_volume_exact"]=True
    (run/"outputs/delivery_acceptance.json").write_text(json.dumps(report,indent=2))
    first={k:str(f.index[f.valid][0].date()) for k,f in features.items()}
    primary=max(pd.Timestamp(first[k]) for k in ["ETF500","MEMBER_IC"])
    common=max(pd.Timestamp(v) for v in first.values())
    assert str(primary.date())=="2015-04-24" and str(common.date())=="2022-08-01"
    freeze={"IC_long":str(primary.date()),"common":str(common.date()),"endpoint":"2026-09-16",
            "first_valid_joint_price":first,"T_plus_1_position_shift":1,"T_plus_2_position_shift":2,
            "last_target_with_execution":"2026-09-15","last_target_with_next_close_pnl":"2026-09-14",
            "later_targets_retained_for_audit_only":True,"fallback":"incumbent; price control same mask",
            "frozen_before_pnl":True}
    freeze_path=run/"inputs/execution_freeze.json"
    if freeze_path.exists():
        assert json.loads(freeze_path.read_text())==freeze
    else:
        freeze_path.write_text(json.dumps(freeze,indent=2))
    print("Delivery accepted and dates frozen:",json.dumps(freeze),flush=True)
    return features,calendar,futures,ew,primary,common

def execute(run):
    source_features, calendar, futures, ew, primary, common = accept(run)
    expiries = {}
    for symbol in futures.symbol.unique():
        expiry = pd.Timestamp(_expiry_from_symbol(symbol))
        after = calendar[calendar>=expiry]
        expiries[symbol] = after[0] if len(after) else expiry
    endpoint = pd.Timestamp('2026-09-16')
    availability, input_events = [], []
    funds = pd.read_csv(run/'inputs/office/etf_fund_features.csv',parse_dates=['date'])
    members = pd.read_csv(run/'inputs/office/member_product_features.csv',parse_dates=['date'])
    event_masks = {}
    for source in source_features:
        if source.startswith('ETF'):
            ff = funds if source=='ETFPOOL' else funds.loc[funds.fund_id.eq('510500.SH')]
            flag = ff.large_move | ff.event_source.fillna('none').ne('none')
            event_masks[source] = flag.groupby(ff.date).any().reindex(calendar,fill_value=False).rolling(5,min_periods=1).max().astype(bool)
        else:
            mm = members if source=='MEMBER_POOL' else members.loc[members['product'].eq('IC')]
            event_masks[source] = mm.contract_changed_in_window.groupby(mm.date).any().reindex(calendar,fill_value=False)

    metrics, comparisons, stresses, daily, attribution, lifecycle, trade_summaries, checks, exposure = [], [], [], [], [], [], [], [], []
    definitions_all, ledger_count = [], 0
    for window_name, start, sources in [('IC_long', primary, ['ETF500', 'MEMBER_IC']), ('common', common, ['ETF500', 'ETFPOOL', 'MEMBER_IC', 'MEMBER_POOL'])]:
        idx = calendar[(calendar>=start) & (calendar<=endpoint)]
        f = {source: source_features[source].loc[idx] for source in sources}
        assert ew.reindex(idx).notna().all()
        targets, definitions = build_targets(ew.reindex(idx), f)
        targets.to_csv(run/f'outputs/targets_{window_name}.csv', index_label='date')
        definitions_all += [dict(window=window_name, **d) for d in definitions]
        weights = futures_weights(idx, futures.loc[futures.symbol.str.startswith('IM'), 'date'].min())
        market = prepare_close_market(futures, idx, weights, expiries)
        subsets = {'full': idx, 'early_half': idx[:len(idx)//2], 'late_half': idx[len(idx)//2:]}
        subsets.update({f'year_{y}': idx[idx.year==y] for y in sorted(set(idx.year))})
        for scenario, cost, lag in SCENARIOS:
            print(window_name, scenario, len(targets.columns), 'ledgers', flush=True)
            signals = targets.shift(lag, fill_value=0.)
            book = batch_close_ledgers(market, signals, cost_bps=cost)
            ledger_count += len(book)
            for name in ['C__'+sources[-1]+'_L_signal', 'DLEG__'+sources[-1]+'_S_signal']:
                ref = contract_ledger(futures, signals[name], weights, expiries=expiries, cost_bps=cost)
                np.testing.assert_allclose(book[name].ret, ref.ret, atol=1e-12, rtol=1e-10)
                checks.append({'window': window_name, 'scenario': scenario, 'strategy': name,
                               'max_error': float((book[name].ret-ref.ret).abs().max())})
            trades = []
            for name, ledger in book.items():
                assert np.isfinite(ledger.ret).all()
                np.testing.assert_allclose(ledger.decision_signal, targets[name].shift(lag+1, fill_value=0.), atol=1e-12)
                np.testing.assert_allclose(ledger.gross_pnl-ledger.cost,
                                           ledger.equity.diff().fillna(ledger.equity.iloc[0]-1.), atol=1e-12, rtol=1e-10)
                _, t = attribute_ledger(ledger)
                trades.append(t.assign(strategy=name))
                closed = t.loc[t.closed]
                trade_summaries.append({'window': window_name, 'scenario': scenario, 'strategy': name,
                                        'closed_trades': len(closed), 'closed_win_rate': float(closed.net_pnl.gt(0).mean()),
                                        'open_trades': int((~t.closed).sum())})
                for period, dates in subsets.items():
                    metrics.append({'window': window_name, 'scenario': scenario, 'strategy': name, 'period': period,
                                    'start': str(dates[0].date()), 'end': str(dates[-1].date()), **describe(ledger.loc[dates])})
            pd.concat(book, names=['strategy', 'date']).to_csv(run/f'outputs/ledgers_{window_name}_{scenario}.csv.gz')
            pd.concat(trades, ignore_index=True).to_csv(run/f'outputs/trades_{window_name}_{scenario}.csv', index=False)
            labels = {side: side_episodes(book['C__base'], direction) for side, direction in [('L', 1), ('S', -1)]}
            for definition in definitions:
                name, source, side = definition['candidate'], definition['source'], definition['side']
                stem = source+'_'+side+'_price'
                marker = ~f[source].valid
                # A day may contain old-position PnL plus new-position transaction costs.
                event = marker.shift(lag+2, fill_value=False) | marker.shift(lag+1, fill_value=False)
                availability.append({'window':window_name,'scenario':scenario,'candidate':name,
                                     'valid_decision_days':int(f[source].valid.sum()),
                                     'fallback_decision_days':int((~f[source].valid).sum()),
                                     'fallback_pnl_or_cost_days':int(event.sum())})
                for account, left, rights in [
                    ('combined', 'C__'+name, [('matched_price', 'D__'+name), ('price', 'C__'+stem),
                                             ('matched_base', 'Q__'+name), ('base', 'C__base'), ('half', 'C__half')]),
                    ('leg', 'LEG__'+name, [('matched_price', 'DLEG__'+name), ('price', 'LEG__'+stem),
                                          ('matched_base', 'QLEG__'+name), ('base', side+'__base')])]:
                    a = book[left]
                    for comparison, right in rights:
                        b = book[right]
                        delta = np.log1p(a.ret)-np.log1p(b.ret)
                        for period, dates in subsets.items():
                            ma, mb = describe(a.loc[dates]), describe(b.loc[dates])
                            comparisons.append({'window': window_name, 'scenario': scenario, 'candidate': name,
                                                'account': account, 'comparison': comparison, 'period': period,
                                                'sharpe_difference': ma['sharpe']-mb['sharpe'],
                                                'candidate_cagr': ma['cagr'], 'reference_cagr': mb['cagr'],
                                                'delta_log': float(delta.loc[dates].sum())})
                        if comparison!='matched_price':
                            continue
                        daily.append(pd.DataFrame({'window': window_name, 'scenario': scenario, 'candidate': name,
                                                   'account': account, 'date': idx, 'delta_log': delta.to_numpy()}))
                        grouped = delta.groupby(labels[side]).sum()
                        np.testing.assert_allclose(grouped.sum(), delta.sum(), atol=1e-12, rtol=0)
                        np.testing.assert_allclose(delta.groupby(idx.year).sum().sum(), delta.sum(), atol=1e-12, rtol=0)
                        for episode, value in grouped.items():
                            attribution.append({'window': window_name, 'scenario': scenario, 'candidate': name,
                                                'account': account, 'episode': int(episode), 'delta_log': float(value)})
                        lifecycle.append({'window': window_name, 'scenario': scenario, 'candidate': name, 'account': account,
                                          'event_days': int(event.sum()), 'event_log': float(delta[event].sum()),
                                          'outside_log': float(delta[~event].sum()), 'total_log': float(delta.sum())})
                        np.testing.assert_allclose(delta[event].sum()+delta[~event].sum(), delta.sum(), atol=1e-12)
                        flags = event_masks[source].reindex(idx)
                        flagged = flags.shift(lag+2,fill_value=False) | flags.shift(lag+1,fill_value=False)
                        input_events.append({'window':window_name,'scenario':scenario,'candidate':name,'account':account,
                            'event_type':'ETF_translation_or_large_move_5day' if source.startswith('ETF') else 'member_contract_change_window',
                            'event_days':int(flagged.sum()),'event_log':float(delta[flagged].sum()),
                            'outside_log':float(delta[~flagged].sum()),'total_log':float(delta.sum())})
                        np.testing.assert_allclose(delta[flagged].sum()+delta[~flagged].sum(),delta.sum(),atol=1e-12)
                        top = delta.nlargest(5).index
                        for count in [0, 1, 5]:
                            aa, bb = a.copy(), b.copy()
                            aa.loc[top[:count], 'ret'] = 0.; bb.loc[top[:count], 'ret'] = 0.
                            stresses.append({'window': window_name, 'scenario': scenario, 'candidate': name,
                                             'account': account, 'best_days_zeroed': count,
                                             'sharpe_difference': describe(aa)['sharpe']-describe(bb)['sharpe']})
                for period, dates in subsets.items():
                    exposure.append({'window': window_name, 'scenario': scenario, 'candidate': name, 'period': period,
                                     'decision_exposure': float(targets.loc[dates, 'LEG__'+name].abs().mean()),
                                     'matched_price_exposure': float(targets.loc[dates, 'DLEG__'+name].abs().mean())})
    for filename, rows in [('metrics', metrics), ('comparisons', comparisons), ('stress', stresses),
                           ('episode_attribution', attribution), ('fallback_attribution', lifecycle), ('input_event_attribution', input_events),
                           ('trade_summary', trade_summaries), ('definitions', definitions_all), ('exposure', exposure), ('availability', availability)]:
        pd.DataFrame(rows).to_csv(run/f'outputs/{filename}.csv', index=False)
    pd.concat(daily, ignore_index=True).to_csv(run/'outputs/relative_daily.csv.gz', index=False)
    assert ledger_count==312
    report = {'new_ledgers': ledger_count, 'reference_checks': checks,
              'account_leg_trade_identities': True, 'fill_delays_verified': True,
              'episode_year_fallback_additivity': True, 'all_returns_finite': True,
              'completed_utc': datetime.now(timezone.utc).isoformat()}
    (run/'outputs/verification.json').write_text(json.dumps(report, indent=2))
    print(pd.DataFrame(comparisons).query("scenario=='close_3bps' and period=='full' and account=='combined' and comparison=='matched_price'").round(5).to_string(index=False), flush=True)
    return report


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--prepare-only', action='store_true')
    parser.add_argument('--execute-prepared', action='store_true')
    args = parser.parse_args()
    run = ROOT/'backtest/output/runs'/RUN_ID
    if args.execute_prepared:
        manifest = json.loads((run/'manifest.json').read_text())
        assert manifest['status']=='prepared'
        for item in manifest['artifacts']:
            assert artifact_record(run/item['path'], run)==item
        assert (run/'inputs/code__run_etf_member_research.py').read_bytes()==Path(__file__).read_bytes()
    else:
        run = create_run_dir(ROOT/'backtest/output/runs', RUN_ID)
        manifest = {'run_id': RUN_ID, 'status': 'running', 'git': git_state(ROOT)}
        write_manifest(run, manifest)
        capture(run)
    try:
        if args.prepare_only:
            accept(run)
            status = 'prepared'
        else:
            manifest['verification'] = execute(run)
            status = 'computed_pending_report'
    except Exception as exc:
        manifest.update(status='failed', error_type=type(exc).__name__)
        write_manifest(run, manifest)
        raise
    manifest.update(status=status, artifacts=[artifact_record(p, run) for p in sorted(run.rglob('*'))
                                             if p.is_file() and p != run/'manifest.json'])
    write_manifest(run, manifest)


if __name__ == '__main__':
    main()
