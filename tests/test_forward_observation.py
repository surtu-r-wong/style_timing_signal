import json
from datetime import datetime, timezone

import pytest

from backtest.forward_observation import init_campaign, record_observation, verify_campaign


def now(day=15, hour=9):
    return datetime(2026, 9, day, hour, tzinfo=timezone.utc)


@pytest.fixture
def setup(tmp_path):
    repo=tmp_path/'repo';repo.mkdir()
    (repo/'generator.py').write_text('VERSION = 1\n')
    sources={'equal_weight':'ew.csv','slope20':'slope.csv'}
    for p in sources.values():
        (repo/p).write_text('date,factor_value\n2026-09-14,0.1\n2026-09-15,-0.2\n')
    protocol={'campaign_id':'test-v1','first_signal_date':'2026-09-15','timezone':'Asia/Shanghai',
              'sources':sources,'code_files':['generator.py'],'earliest_record_hour':16,
              'fill':'next_actual_trading_day_close','status':'observation_only_no_statistical_go'}
    spec=tmp_path/'protocol.json';spec.write_text(json.dumps(protocol))
    campaign=tmp_path/'campaign'
    init_campaign(campaign,spec,repo=repo,now=now(14))
    return repo,campaign


def test_timely_observation_preserves_bytes_and_positions(setup):
    repo,campaign=setup
    p=record_observation(campaign,repo=repo,now=now())
    m=json.loads((p/'record.json').read_text())
    assert m['decision_date']=='2026-09-15'
    assert m['targets']=={'spot_slope_longflat':0,'futures_ew_symmetric':-1,'spot_ew_longflat_control':0}
    assert (p/'equal_weight.csv').read_bytes()==(repo/'ew.csv').read_bytes()
    assert verify_campaign(campaign)['records']==1


@pytest.mark.parametrize('kind',['stale','future','duplicate_date','nan'])
def test_rejects_bad_source_without_creating_record(setup,kind):
    repo,campaign=setup
    data={'stale':'2026-09-14,0.2','future':'2026-09-16,0.2',
          'duplicate_date':'2026-09-15,0.2\n2026-09-15,0.3','nan':'2026-09-15,nan'}[kind]
    (repo/'ew.csv').write_text('date,factor_value\n'+data+'\n')
    with pytest.raises(ValueError):record_observation(campaign,repo=repo,now=now())
    assert not list((campaign/'records').iterdir())


@pytest.mark.parametrize('time',[now(14),now(15,6)])
def test_rejects_pre_start_or_intraday_record(setup,time):
    repo,campaign=setup
    with pytest.raises(ValueError):record_observation(campaign,repo=repo,now=time)
    assert verify_campaign(campaign)['records']==0


def test_rejects_duplicate_and_source_tampering(setup):
    repo,campaign=setup
    p=record_observation(campaign,repo=repo,now=now())
    with pytest.raises(FileExistsError):record_observation(campaign,repo=repo,now=now())
    (p/'equal_weight.csv').write_text('changed')
    with pytest.raises(ValueError):verify_campaign(campaign)


@pytest.mark.parametrize('target',['protocol','code'])
def test_rejects_protocol_or_code_drift(setup,target):
    repo,campaign=setup
    p=campaign/'protocol.json' if target=='protocol' else repo/'generator.py'
    p.write_text(p.read_text()+'\n')
    with pytest.raises(ValueError):record_observation(campaign,repo=repo,now=now())


def test_chain_preserves_prior_forecast_when_history_changes(setup):
    repo,campaign=setup
    first=record_observation(campaign,repo=repo,now=now())
    for name in ['ew.csv','slope.csv']:
        (repo/name).write_text('date,factor_value\n2026-09-14,0.1\n2026-09-15,0.9\n2026-09-16,0.4\n')
    second=record_observation(campaign,repo=repo,now=now(16))
    assert json.loads((second/'record.json').read_text())['prior_signal_revisions']==['equal_weight','slope20']
    assert json.loads((first/'record.json').read_text())['targets']['futures_ew_symmetric']==-1
    assert verify_campaign(campaign)['records']==2


def test_missing_final_record_is_detected(setup):
    import shutil
    repo,campaign=setup
    p=record_observation(campaign,repo=repo,now=now())
    shutil.rmtree(p)
    with pytest.raises((ValueError,FileNotFoundError)):
        verify_campaign(campaign)


def test_interrupted_write_requires_review(setup):
    repo,campaign=setup
    (campaign/'records'/'2026-09-15').mkdir()
    with pytest.raises(ValueError,match='unjournaled'):
        verify_campaign(campaign)


def test_campaign_cannot_start_retroactively(tmp_path):
    (tmp_path/'generator.py').write_text('x=1')
    (tmp_path/'ew.csv').write_text('date,factor_value\n2026-09-14,0.2\n')
    spec=tmp_path/'p.json'
    spec.write_text(json.dumps({'first_signal_date':'2026-09-14','timezone':'Asia/Shanghai',
       'sources':{'equal_weight':'ew.csv','slope20':'ew.csv'},'code_files':['generator.py'],
       'earliest_record_hour':16,'fill':'next_actual_trading_day_close'}))
    with pytest.raises(ValueError,match='after campaign creation'):
        init_campaign(tmp_path/'campaign',spec,repo=tmp_path,now=now(14))


def test_real_clock_crossing_midnight_rejects_capture(setup,monkeypatch):
    import backtest.forward_observation as mod
    repo,campaign=setup
    ticks=iter([datetime(2026,9,15,15,59,tzinfo=timezone.utc),datetime(2026,9,15,16,0,tzinfo=timezone.utc)])
    monkeypatch.setattr(mod,'_clock',lambda _:next(ticks))
    with pytest.raises(ValueError,match='date or clock changed'):
        record_observation(campaign,repo=repo)
    assert not list((campaign/'records').iterdir())
