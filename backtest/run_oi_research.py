"""Consume office-derived OI features under the frozen EW independent-leg plan."""
from __future__ import annotations

import argparse
from datetime import datetime, timezone
import json
from pathlib import Path
import shutil

import numpy as np
import pandas as pd

from backtest.data import _expiry_from_symbol
from backtest.execution_ledger import contract_ledger, futures_weights
from backtest.intraday_flow_pilot import prepare_close_market, batch_close_ledgers, attribute_ledger, residual_past
from backtest.oi_delivery_contract import validate_delivery
from backtest.run_intraday_flow_pilot import describe
from backtest.run_manifest import artifact_record, create_run_dir, write_manifest, git_state

ROOT = Path(__file__).resolve().parents[1]
RUN_ID = '20260917-ew-oi-research-r2'
OFFICE = Path('/home/elfbob/claude-code/data_manager/requests/2026-09-17-style-timing-signal-futures-oi-derived-inputs')
READY = ROOT/'backtest/output/runs/20260917-ew-oi-input-readiness-r1'
SCENARIOS = [('close_3bps', 3., 0), ('close_10bps', 10., 0), ('second_close_3bps', 3., 1)]
SPECS = ['2026-09-17-ew-oi-and-new-input-plan.md', '2026-09-17-ew-oi-execution-freeze.md']


def build_targets(ew, features):
    if not ew.index.is_unique or not np.isfinite(ew).all():
        raise ValueError('invalid EW calendar or nonfinite factor')
    base = np.sign(ew)
    long, short = base.clip(lower=0), base.clip(upper=0)
    targets = pd.DataFrame({'C__base': base, 'L__base': long, 'S__base': short,
                            'C__half': long+.5*short})
    definitions = []
    for source, f in features.items():
        if not f.index.equals(ew.index):
            raise ValueError('feature calendar mismatch')
        if not np.isfinite(f[['r5', 'g5', 'u5']]).all().all():
            raise ValueError('features must be finite')
        for side, leg, other, direction in [('L', long, short, 1), ('S', short, long, -1)]:
            price = leg.where(f.r5*direction>0, 0.)
            stem = source+'_'+side
            targets['C__'+stem+'_price'] = other+price
            targets['LEG__'+stem+'_price'] = price
            if not leg.abs().sum() or not price.abs().sum():
                raise ValueError('zero reference exposure')
            for field in ['g5', 'u5']:
                name = stem+'_'+field
                selected = price.where(f[field]>0, 0.)
                q = float(selected.abs().sum()/leg.abs().sum())
                r = float(selected.abs().sum()/price.abs().sum())
                assert 0 <= q <= 1 and 0 <= r <= 1
                for prefix, target in [('C', other+selected), ('LEG', selected),
                                       ('Q', other+q*leg), ('QLEG', q*leg),
                                       ('D', other+r*price), ('DLEG', r*price)]:
                    targets[prefix+'__'+name] = target
                definitions.append({'candidate': name, 'source': source, 'side': side, 'field': field,
                                    'q': q, 'r': r, 'base_days': int(leg.ne(0).sum()),
                                    'price_days': int(price.ne(0).sum()), 'kept_days': int(selected.ne(0).sum())})
    return targets, definitions


def pool_acceptance_reference(ic, im):
    """Independent check of the contractual requirement that both product rows be valid."""
    left = ic.reindex(im.index)
    result = (left+im)/2
    return result.where(left.notna().all(axis=1) & im.notna().all(axis=1), np.nan)


def capture(run):
    delivery = OFFICE/'delivery/ew-oi-v1'
    manifest = json.loads((delivery/'manifest.json').read_text())
    assert manifest['contract_version']=='ew-oi-v1' and manifest['window_end']=='2026-09-16'
    for item in manifest['artifacts']+manifest['supporting_files']:
        assert artifact_record(delivery/item['path'], delivery)['sha256']==item['sha256']
    shutil.copytree(delivery, run/'inputs/office')
    for name in ['disposition.md', 'response-01-office-2026-09-17.md', 'response-02-office-2026-09-17.md']:
        shutil.copyfile(OFFICE/name, run/'inputs'/('office__'+name))
    source_manifest = json.loads((READY/'manifest.json').read_text())
    record = next(r for r in source_manifest['artifacts'] if r['path']=='inputs/futures.csv')
    assert artifact_record(READY/'inputs/futures.csv', READY)==record
    shutil.copyfile(READY/'inputs/futures.csv', run/'inputs/futures.csv')
    shutil.copyfile(ROOT/'output/equal_weight/equal_weight_signal_20d40z.csv', run/'inputs/equal_weight.csv')
    prior = ROOT/'backtest/output/runs/20260916-ew-exit-mechanism-audit-r1/outputs/decision_targets.csv'
    shutil.copyfile(prior, run/'inputs/prior_targets.csv')
    sources = [artifact_record(p, ROOT) for p in [READY/'inputs/futures.csv', prior,
               ROOT/'output/equal_weight/equal_weight_signal_20d40z.csv']]
    (run/'inputs/source_receipts.json').write_text(json.dumps(sources, indent=2))
    for spec in SPECS:
        shutil.copyfile(ROOT/'docs/plans'/spec, run/'inputs'/spec)
    for rel in ['backtest/run_oi_research.py', 'backtest/oi_delivery_contract.py', 'tests/test_oi_research.py',
                'backtest/intraday_flow_pilot.py', 'backtest/execution_ledger.py', 'backtest/data.py',
                'backtest/metrics.py', 'backtest/run_intraday_flow_pilot.py', 'backtest/run_manifest.py']:
        shutil.copyfile(ROOT/rel, run/'inputs'/('code__'+Path(rel).name))


def accept(run):
    delivery = run/'inputs/office'
    products = pd.read_csv(delivery/'product_daily.csv', parse_dates=['date'])
    features = pd.read_csv(delivery/'features.csv', parse_dates=['date'])
    calendar = pd.read_csv(delivery/'calendar.csv', parse_dates=['date'])
    sessions = pd.DatetimeIndex(calendar.loc[calendar.sfe.eq(1), 'date'])
    report = validate_delivery(products, features, sessions)
    raw = pd.read_csv(delivery/'raw_inputs/futures.csv', parse_dates=['date'])
    execution = pd.read_csv(run/'inputs/futures.csv', parse_dates=['date'])
    keys = ['date', 'symbol']
    a, b = raw.set_index(keys).sort_index(), execution.set_index(keys).sort_index()
    assert a.index.equals(b.index)
    np.testing.assert_array_equal(a[['oi', 'volume']], b[['oi', 'volume']])
    price = pd.read_csv(delivery/'raw_inputs/index.csv', parse_dates=['date'])
    generated = {}
    errors = []
    # Independent numerical acceptance only; these reconstructions are never persisted or traded.
    for product, code in [('IC', '000905.SH'), ('IM', '000852.SH')]:
        p = products.loc[products['product'].eq(product)].set_index('date')
        sums = raw.loc[raw['product'].eq(product)].groupby('date')[['oi', 'volume']].sum().reindex(p.index)
        np.testing.assert_array_equal(p[['total_oi', 'total_volume']], sums)
        px = price.loc[price.index_code.eq(code)].set_index('date').close.reindex(sessions)
        np.testing.assert_allclose(p.index_close, px.reindex(p.index), atol=1e-10, rtol=0)
        x = pd.DataFrame({'g5': np.log(p.total_oi/p.total_oi.shift(5)),
                          'r5': (px/px.shift(5)-1).reindex(p.index),
                          'rv20': (px.pct_change(fill_method=None).rolling(20, min_periods=20).std(ddof=1)*np.sqrt(245)).reindex(p.index),
                          'v5': (p.total_volume/p.total_oi.shift(1)).rolling(5, min_periods=5).mean()}, index=p.index)
        generated[product] = x
    calculated = {'IC': generated['IC'], 'POOL': pool_acceptance_reference(generated['IC'], generated['IM'])}
    for source, x in calculated.items():
        f = features.loc[features.source.eq(source)].set_index('date')
        for field in ['g5', 'r5', 'rv20', 'v5']:
            np.testing.assert_allclose(f[field], x[field], atol=1e-10, rtol=1e-9, equal_nan=True)
            errors.append({'source': source, 'field': field, 'max_abs_error': float((f[field]-x[field]).abs().max())})
        controls = pd.DataFrame({'r5': x.r5, 'abs_r5': x.r5.abs(), 'rv20': x.rv20, 'v5': x.v5})
        residual = residual_past(x.g5, controls, window=250)
        np.testing.assert_allclose(f.u5, residual, atol=1e-9, rtol=1e-8, equal_nan=True)
        errors.append({'source': source, 'field': 'u5', 'max_abs_error': float((f.u5-residual).abs().max())})
        first = f.index[f.feature_valid][0]
        assert f.loc[first:].feature_valid.all()
    ew = pd.read_csv(run/'inputs/equal_weight.csv', index_col='date', parse_dates=True).factor_value
    prior = pd.read_csv(run/'inputs/prior_targets.csv', index_col='date', parse_dates=True)
    np.testing.assert_array_equal(np.sign(ew.reindex(prior.index)), prior.C__base)
    report.update(raw_execution_rows=len(raw), oi_volume_match_exactly=True,
                  numeric_checks=errors, previous_ew_direction_matches=True,
                  historical_pit_individually_proven=False)
    (run/'outputs/delivery_acceptance.json').write_text(json.dumps(report, indent=2))
    print('Delivery accepted:', json.dumps(report), flush=True)
    return products, features, sessions, execution, ew


def side_episodes(ledger, direction):
    held = ledger.decision_signal.mul(direction).gt(0).to_numpy()
    labels = pd.Series(-1, index=ledger.index, dtype=int)
    starts = np.flatnonzero(held & ~np.r_[False, held[:-1]])
    for number, start in enumerate(starts):
        endings = np.flatnonzero(~held[start:])
        stop = start+endings[0] if len(endings) else len(held)-1
        assert labels.iloc[start:stop+1].eq(-1).all()
        labels.iloc[start:stop+1] = number
    return labels


def execute(run):
    products, features, calendar, futures, ew = accept(run)
    expiries = {}
    for symbol in futures.symbol.unique():
        expiry = pd.Timestamp(_expiry_from_symbol(symbol))
        after = calendar[calendar>=expiry]
        expiries[symbol] = after[0] if len(after) else expiry
    source_features = {source: f.set_index('date') for source, f in features.groupby('source')}
    primary = source_features['IC'].index[source_features['IC'].feature_valid][0]
    common = source_features['POOL'].index[source_features['POOL'].feature_valid][0]
    assert str(primary.date())=='2016-04-29' and str(common.date())=='2023-08-09'
    endpoint = pd.Timestamp('2026-09-16')
    masks = {}
    for product, p in products.groupby('product'):
        p = p.set_index('date')
        masks[product] = p.listing_or_expiry_change | p.lifecycle_change_in_previous_five_sessions
    metrics, comparisons, stresses, daily, attribution, lifecycle, trade_summaries, checks, exposure = [], [], [], [], [], [], [], [], []
    definitions_all, ledger_count = [], 0
    for window_name, start, sources in [('IC_long', primary, ['IC']), ('common', common, ['IC', 'POOL'])]:
        idx = calendar[(calendar>=start) & (calendar<=endpoint)]
        f = {source: source_features[source].loc[idx] for source in sources}
        for frame in f.values():
            assert frame.feature_valid.all()
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
            for name in ['C__'+sources[-1]+'_L_u5', 'DLEG__'+sources[-1]+'_S_g5']:
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
                marker = masks['IC'].reindex(idx)
                if source=='POOL':
                    marker = marker | masks['IM'].reindex(idx)
                # A day may contain old-position PnL plus new-position transaction costs.
                event = marker.shift(lag+2, fill_value=False) | marker.shift(lag+1, fill_value=False)
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
                           ('episode_attribution', attribution), ('lifecycle_attribution', lifecycle),
                           ('trade_summary', trade_summaries), ('definitions', definitions_all), ('exposure', exposure)]:
        pd.DataFrame(rows).to_csv(run/f'outputs/{filename}.csv', index=False)
    pd.concat(daily, ignore_index=True).to_csv(run/'outputs/relative_daily.csv.gz', index=False)
    assert ledger_count==276
    report = {'new_ledgers': ledger_count, 'reference_checks': checks,
              'account_leg_trade_identities': True, 'fill_delays_verified': True,
              'episode_year_lifecycle_additivity': True, 'all_returns_finite': True,
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
        assert (run/'inputs/code__run_oi_research.py').read_bytes()==Path(__file__).read_bytes()
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
