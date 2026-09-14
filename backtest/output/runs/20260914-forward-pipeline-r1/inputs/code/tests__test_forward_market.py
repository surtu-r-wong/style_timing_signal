import json
from datetime import datetime, timezone
import pandas as pd
import pytest
from backtest.forward_market import init_market, append_market, verify_market
from backtest.forward_scoring import score_frames
from backtest.forward_observation import init_campaign


def stamp(day): return datetime(2026,9,day,10,tzinfo=timezone.utc)


def frames(day):
    spot=pd.DataFrame([{'date':f'2026-09-{day:02d}','symbol':s,'open':100.,'close':100.,'oi':1.} for s in ('000905.SH','000852.SH')])
    fut=pd.DataFrame([{'date':f'2026-09-{day:02d}','symbol':s,'open':100.,'close':100.,'oi':oi} for s,oi in [('IC2609.CFE',30),('IC2610.CFE',20),('IM2609.CFE',30),('IM2610.CFE',20)]])
    cal=pd.DataFrame({'date':pd.date_range('2026-09-01','2026-10-31')})
    cal['open']=cal.date.dt.dayofweek.lt(5)
    return spot,fut,cal


@pytest.fixture
def ready(tmp_path):
    repo=tmp_path/'repo'; repo.mkdir()
    (repo/'code.py').write_text('v=1')
    (repo/'ew.csv').write_text('date,factor_value\n2026-09-15,1\n')
    spec=tmp_path/'p.json'
    spec.write_text(json.dumps({'first_signal_date':'2026-09-15','timezone':'Asia/Shanghai','earliest_record_hour':16,
      'fill':'next_actual_trading_day_close','sources':{'equal_weight':'ew.csv','slope20':'ew.csv'},'code_files':['code.py']}))
    campaign=tmp_path/'campaign'; init_campaign(campaign,spec,repo=repo,now=stamp(14))
    market=tmp_path/'market'; init_market(market,campaign,repo=repo,now=stamp(14))
    return repo,campaign,market


def test_append_hashes_market_and_chooses_prior_oi_contracts(ready):
    repo,campaign,market=ready
    path=append_market(market,*frames(17),{'kind':'fixture'},repo=repo,now=stamp(17))
    record=json.loads((path/'record.json').read_text())
    assert record['next_session']=='2026-09-18'
    assert record['next_contracts']=={'IC':'IC2610.CFE','IM':'IM2610.CFE'}
    assert verify_market(market)['records']==1
    with pytest.raises(FileExistsError): append_market(market,*frames(17),{},repo=repo,now=stamp(17))
    (path/'futures.csv').write_text('tamper')
    with pytest.raises(ValueError): verify_market(market)


def test_stale_data_and_intraday_rejected(ready):
    repo,_,market=ready
    with pytest.raises(ValueError): append_market(market,*frames(15),{},repo=repo,now=stamp(16))
    with pytest.raises(ValueError): append_market(market,*frames(15),{},repo=repo,now=datetime(2026,9,15,2,tzinfo=timezone.utc))
    assert verify_market(market)['records']==0


def test_missing_market_date_not_hidden_by_calendar(ready):
    repo,_,market=ready
    for day in (15,17): append_market(market,*frames(day),{},repo=repo,now=stamp(day))
    assert verify_market(market)['missing_market_sessions']==['2026-09-16']


def test_incomplete_write_and_code_changes_rejected(ready):
    repo,_,market=ready
    (repo/'code.py').write_text('v=2')
    with pytest.raises(ValueError): append_market(market,*frames(15),{},repo=repo,now=stamp(15))
    (market/'records'/'2026-09-15').mkdir()
    with pytest.raises(ValueError): verify_market(market)


def test_scoring_carries_missing_signals_and_respects_next_close():
    dates=pd.to_datetime(['2026-09-15','2026-09-16','2026-09-17'])
    spot=pd.concat([frames(d)[0] for d in (15,16,17)],ignore_index=True)
    fut=pd.concat([frames(d)[1] for d in (15,16,17)],ignore_index=True)
    fut.loc[fut.date.eq('2026-09-17'),'close']=110.
    receipts={'2026-09-15':{'signals':{'equal_weight':1.,'slope20':0.}}}
    ledgers,quality=score_frames(spot,fut,dates,receipts,cost=0.)
    assert quality['missing_signal_sessions']==['2026-09-16','2026-09-17']
    assert ledgers['futures_ew'].ret.tolist()==pytest.approx([0.,0.,.1])
    assert ledgers['futures_always_long'].ret.tolist()==pytest.approx([0.,0.,.1])
    assert ledgers['current_two_pool'].equity.iloc[-1]==pytest.approx(1.05)


def test_scoring_rejects_missing_quote_day():
    dates=pd.to_datetime(['2026-09-15','2026-09-16','2026-09-17'])
    spot=pd.concat([frames(d)[0] for d in (15,17)],ignore_index=True)
    fut=pd.concat([frames(d)[1] for d in (15,17)],ignore_index=True)
    with pytest.raises(ValueError,match='calendar'):
        score_frames(spot,fut,dates,{},cost=3.)


def test_interim_quality_blocks_return_output(ready,tmp_path):
    from backtest.forward_scoring import quality_report,score_campaign
    _,_,market=ready
    q=quality_report(market)
    assert not q['returns_released'] and not q['score_ready'] and not q['go_enabled']
    output=tmp_path/'returns'
    with pytest.raises(ValueError,match='interim quality only'): score_campaign(market,output)
    assert not output.exists()


def test_market_clock_cannot_cross_midnight(ready,monkeypatch):
    from backtest import forward_market as m
    repo,_,market=ready
    times=iter([stamp(15),stamp(16)])
    monkeypatch.setattr(m,'_clock',lambda unused:next(times))
    with pytest.raises(ValueError,match='clock crossed'): append_market(market,*frames(15),{},repo=repo)
    assert verify_market(market)['records']==0


def test_calendar_revision_is_not_silently_accepted(ready):
    repo,_,market=ready
    append_market(market,*frames(15),{},repo=repo,now=stamp(15))
    s,f,c=frames(16)
    c.loc[c.date.eq('2026-09-15'),'open']=False
    with pytest.raises(ValueError,match='calendar changed'): append_market(market,s,f,c,{},repo=repo,now=stamp(16))
    assert verify_market(market)['records']==1
