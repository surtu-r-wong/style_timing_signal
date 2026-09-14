"""Append-only same-evening market receipts; research prices, never executions."""
from __future__ import annotations
import argparse
from datetime import datetime, timedelta
import io
import json
from pathlib import Path
from zoneinfo import ZoneInfo

import numpy as np
import pandas as pd
from backtest.data import _connect, _expiry_from_symbol
from backtest.forward_observation import ROOT, _bytes, _sha, _clock, _lock, _repo_file, verify_campaign
from signals.common.config import load_db_config


def _implementation():
    return {name:_sha((ROOT/'backtest'/name).read_bytes())
            for name in ('forward_market.py','forward_scoring.py','execution_audit.py','metrics.py','data.py')}


def init_market(market,campaign,*,repo=ROOT,now=None):
    now=_clock(now); campaign=Path(campaign).resolve()
    verify_campaign(campaign)
    protocol=json.loads((campaign/'protocol.json').read_text())
    if now.astimezone(ZoneInfo('Asia/Shanghai')).date().isoformat()>=protocol['first_signal_date']:
        raise ValueError('market capture must be initialized before first signal date')
    meta={'campaign':str(campaign),'campaign_sha256':_sha((campaign/'campaign.json').read_bytes()),
          'created_at_utc':now.isoformat(),'first_signal_date':protocol['first_signal_date'],
          'implementation_sha256':_implementation(),'mode':'price_based_research_no_actual_fills',
          'interim_quality_only':True,'observation_sessions':504}
    market=Path(market); market.mkdir(parents=True,exist_ok=False)
    (market/'records').mkdir(); (market/'journal.jsonl').write_text('')
    raw=_bytes(meta); (market/'market.json').write_bytes(raw); (market/'market.sha256').write_text(_sha(raw)+'\n')
    return market


def metadata(market):
    market=Path(market); raw=(market/'market.json').read_bytes()
    if _sha(raw)!=(market/'market.sha256').read_text().strip(): raise ValueError('market metadata hash mismatch')
    meta=json.loads(raw); campaign=Path(meta['campaign'])
    if _sha((campaign/'campaign.json').read_bytes())!=meta['campaign_sha256']: raise ValueError('bound campaign changed')
    verify_campaign(campaign)
    return meta


def validate_snapshot(day,spot,futures,calendar):
    day=pd.Timestamp(day).normalize()
    for data in (spot,futures):
        if not {'date','symbol','open','close','oi'}<=set(data): raise ValueError('required quote fields missing')
        dates=pd.to_datetime(data.date)
        if len(data)==0 or not dates.eq(day).all() or data.symbol.duplicated().any(): raise ValueError('quotes stale, duplicate or empty')
        close=pd.to_numeric(data.close,errors='coerce')
        if not np.isfinite(close).all() or not close.gt(0).all(): raise ValueError('invalid close quote')
    if set(spot.symbol)!={'000905.SH','000852.SH'}: raise ValueError('both spot proxies required')
    if not futures.symbol.str.match(r'^(IC|IM)\d{4}\.CFE$').all(): raise ValueError('unexpected futures symbol')
    if not {'date','open'}<=set(calendar): raise ValueError('calendar fields missing')
    cal=calendar.copy(); cal['date']=pd.to_datetime(cal.date)
    if cal.date.duplicated().any() or cal.open.isna().any() or not cal.open.map(lambda x:isinstance(x,(bool,np.bool_))).all():
        raise ValueError('calendar duplicate or nonboolean flag')
    cal=cal.sort_values('date')
    if not pd.DatetimeIndex(cal.date).equals(pd.date_range(cal.date.min(),cal.date.max())):
        raise ValueError('calendar must include closed days without gaps')
    sessions=pd.DatetimeIndex(cal.loc[cal.open,'date'])
    if day not in sessions or not (sessions>day).any(): raise ValueError('calendar lacks today or next session')
    next_day=sessions[sessions>day][0]
    selected={}
    for group in ('IC','IM'):
        g=futures[futures.symbol.str.startswith(group)].copy()
        g['oi']=pd.to_numeric(g.oi,errors='coerce')
        g=g[np.isfinite(g.oi)&g.oi.ge(0)]
        g=g[g.symbol.map(lambda s:pd.Timestamp(_expiry_from_symbol(s))>next_day)].sort_values('symbol')
        if g.empty: raise ValueError('no eligible next-session contract '+group)
        selected[group]=g.loc[g.oi.idxmax(),'symbol']
    return next_day.date().isoformat(),selected


def _verify(market):
    meta=metadata(market)
    journal=[json.loads(x) for x in (market/'journal.jsonl').read_text().splitlines()]
    head=None; previous=meta['created_at_utc']; previous_day=None; calendar_flags={}
    for entry in journal:
        folder=market/'records'/entry['date']; raw=(folder/'record.json').read_bytes()
        if _sha(raw)!=entry['sha256']: raise ValueError('market record hash mismatch')
        r=json.loads(raw)
        if r['previous_sha256']!=head or r['market_sha256']!=_sha((market/'market.json').read_bytes()): raise ValueError('market record chain mismatch')
        stamp=datetime.fromisoformat(r['recorded_at_utc']); local=stamp.astimezone(ZoneInfo('Asia/Shanghai'))
        if (r['date']!=entry['date'] or local.date().isoformat()!=r['date'] or local.hour<16 or
            stamp<=datetime.fromisoformat(previous) or r['date']<meta['first_signal_date'] or
            (previous_day is not None and r['date']<=previous_day)):
            raise ValueError('invalid market receipt time')
        data={}
        for name in ('spot','futures','calendar'):
            raw=(folder/f'{name}.csv').read_bytes()
            if _sha(raw)!=r['files_sha256'][name]: raise ValueError('market snapshot hash mismatch')
            data[name]=pd.read_csv(io.BytesIO(raw))
        next_day,selected=validate_snapshot(r['date'],data['spot'],data['futures'],data['calendar'])
        if next_day!=r['next_session'] or selected!=r['next_contracts']: raise ValueError('stored contract target differs from snapshot')
        for c in data['calendar'].itertuples():
            d=str(c.date)[:10]
            if d in calendar_flags and calendar_flags[d]!=c.open: raise ValueError('calendar changed between receipts')
            calendar_flags[d]=bool(c.open)
        head=entry['sha256']; previous=r['recorded_at_utc']; previous_day=r['date']
    if {p.name for p in (market/'records').iterdir()}!={r['date'] for r in journal}: raise ValueError('unjournaled or missing market record')
    expected=sorted(d for d,flag in calendar_flags.items() if flag and meta['first_signal_date']<=d<=(previous_day or ''))
    recorded={r['date'] for r in journal}
    return {'records':len(journal),'head_sha256':head,'last_recorded_at_utc':previous,'last_date':previous_day,
            'sessions':expected,'missing_market_sessions':sorted(set(expected)-recorded),
            'completed_fill_sessions':max(len(expected)-1,0),'mode':meta['mode']}


def verify_market(market):
    market=Path(market)
    with _lock(market): return _verify(market)


def append_market(market,spot,futures,calendar,provenance,*,repo=ROOT,now=None):
    actual=now is None; now=_clock(now); market=Path(market)
    with _lock(market):
        status=_verify(market); meta=metadata(market)
        local=now.astimezone(ZoneInfo('Asia/Shanghai')); day=local.date().isoformat()
        if local.hour<16 or day<meta['first_signal_date']: raise ValueError('same-day evening capture not open')
        if status['last_date'] and day<=status['last_date']:
            if day==status['last_date']: raise FileExistsError('market date already recorded')
            raise ValueError('retroactive market date')
        if now<=datetime.fromisoformat(status['last_recorded_at_utc']): raise ValueError('retroactive clock')
        if _implementation()!=meta['implementation_sha256']: raise ValueError('market implementation changed')
        campaign=json.loads((Path(meta['campaign'])/'campaign.json').read_text())
        for path,sha in campaign['code_sha256'].items():
            if _sha(_repo_file(repo,path).read_bytes())!=sha: raise ValueError('frozen signal code changed')
        next_day,selected=validate_snapshot(day,spot,futures,calendar)
        prior_calendar={}
        for folder in sorted((market/'records').iterdir()):
            for row in pd.read_csv(folder/'calendar.csv').itertuples(): prior_calendar[str(row.date)[:10]]=bool(row.open)
        for row in calendar.itertuples():
            date=str(row.date)[:10]
            if date in prior_calendar and prior_calendar[date]!=row.open: raise ValueError('calendar changed between receipts')
        blobs={}
        for name,data in [('spot',spot),('futures',futures),('calendar',calendar)]:
            data=data.copy();data['date']=pd.to_datetime(data.date).dt.strftime('%Y-%m-%d')
            blobs[name]=data.to_csv(index=False).encode()
        if actual:
            end=_clock(None)
            if end<now or end.astimezone(ZoneInfo('Asia/Shanghai')).date()!=local.date(): raise ValueError('clock crossed capture boundary')
            now=end
        record={'date':day,'recorded_at_utc':now.isoformat(),'previous_sha256':status['head_sha256'],
                'market_sha256':_sha((market/'market.json').read_bytes()),'files_sha256':{n:_sha(b) for n,b in blobs.items()},
                'next_session':next_day,'next_contracts':selected,'provenance':provenance,
                'claim':'observed prices and next-session contract targets; no actual fill receipt'}
        folder=market/'records'/day;folder.mkdir(exist_ok=False)
        for name,blob in blobs.items(): (folder/f'{name}.csv').write_bytes(blob)
        raw=_bytes(record);(folder/'record.json').write_bytes(raw)
        with (market/'journal.jsonl').open('a') as f:
            f.write(json.dumps({'date':day,'sha256':_sha(raw)})+'\n');f.flush()
        return folder


def read_pg(day,first_date):
    """Read-only repeatable-read snapshot. No provider PIT guarantee."""
    conn=_connect(load_db_config())
    try:
        conn.set_session(readonly=True,isolation_level='REPEATABLE READ')
        with conn.cursor() as q:
            q.execute("SET LOCAL statement_timeout='60s'")
            q.execute('SELECT transaction_timestamp()::text');stamp=q.fetchone()[0]
            q.execute('SELECT trade_date,index_code,open,close FROM stock_selector.index_daily WHERE index_code IN (%s,%s) AND trade_date=%s ORDER BY index_code',('000905.SH','000852.SH',day))
            spot=pd.DataFrame(q.fetchall(),columns=['date','symbol','open','close']);spot['oi']=1.
            q.execute('SELECT trade_date,symbol,open,close,oi FROM public.futures_daily WHERE (symbol LIKE %s OR symbol LIKE %s) AND trade_date=%s ORDER BY symbol',('IC%','IM%',day))
            futures=pd.DataFrame(q.fetchall(),columns=['date','symbol','open','close','oi'])
            q.execute('SELECT calendar_date,sfe FROM public.trading_calendar WHERE calendar_date BETWEEN %s AND %s AND deleted_at IS NULL ORDER BY calendar_date',
                      (first_date,pd.Timestamp(day).date()+timedelta(days=60)))
            calendar=pd.DataFrame(q.fetchall(),columns=['date','open'])
        conn.rollback()
    finally: conn.close()
    return spot,futures,calendar,{'kind':'readonly_database_snapshot','transaction_timestamp':stamp,
        'calendar_source':'public.trading_calendar.sfe; require both spot proxies and IC/IM quotes each recorded day',
        'actual_executions':False}


def main():
    ap=argparse.ArgumentParser();ap.add_argument('action',choices=['init','record','verify']);ap.add_argument('--market',type=Path,required=True);ap.add_argument('--campaign',type=Path)
    a=ap.parse_args()
    if a.action=='init':
        if a.campaign is None: ap.error('--campaign required')
        init_market(a.market,a.campaign)
    elif a.action=='record':
        meta=metadata(a.market);day=_clock(None).astimezone(ZoneInfo('Asia/Shanghai')).date()
        append_market(a.market,*read_pg(day,meta['first_signal_date']))
    print(json.dumps(verify_market(a.market),ensure_ascii=False,indent=2))


if __name__=='__main__': main()
