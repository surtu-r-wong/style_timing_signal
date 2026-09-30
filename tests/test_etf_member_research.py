import numpy as np
import pandas as pd
import pytest
from backtest.run_etf_member_research import build_targets
from backtest.etf_member_delivery_contract import select_previous_oi, same_contract_delta

def fixture():
    idx=pd.bdate_range("2020-01-01",periods=10)
    ew=pd.Series([1,1,1,1,-1,-1,-1,-1,0,-1],index=idx,dtype=float)
    f=pd.DataFrame({"value":[1,-1,0,np.nan,-1,1,0,np.nan,1,-1],
                    "r5":[1,1,1,np.nan,-1,-1,-1,np.nan,1,1],
                    "valid":[True,True,True,False,True,True,True,False,True,True]},index=idx)
    return ew,f

def test_missing_reverts_to_incumbent_and_price_uses_same_mask():
    ew,f=fixture()
    t,d=build_targets(ew,{"X":f})
    assert t.C__X_L_signal.tolist()==[1,0,0,1,-1,-1,-1,-1,0,-1]
    assert t.C__X_S_signal.tolist()==[1,1,1,1,-1,0,0,-1,0,0]
    assert t.LEG__X_L_price.tolist()==[1,1,1,1,0,0,0,0,0,0]
    assert t.LEG__X_S_price.tolist()==[0,0,0,0,-1,-1,-1,-1,0,0]
    assert len(d)==2 and len(t.columns)==20
    for definition in d:
        n=definition["candidate"]
        assert definition["fallback_days"]==2
        np.testing.assert_allclose(t["LEG__"+n].abs().sum(),t["QLEG__"+n].abs().sum())
        np.testing.assert_allclose(t["LEG__"+n].abs().sum(),t["DLEG__"+n].abs().sum())

def test_future_input_changes_do_not_change_prior_candidate_or_price_targets():
    ew,f=fixture();before,_=build_targets(ew,{"X":f})
    g=f.copy();g.iloc[-2:]=[99,-99,True]
    after,_=build_targets(ew,{"X":g})
    keys=[c for c in before if c.startswith(("C__","LEG__"))]
    pd.testing.assert_frame_equal(before[keys].iloc[:-2],after[keys].iloc[:-2])

def test_valid_nan_and_mismatched_calendar_rejected():
    ew,f=fixture();f.loc[f.index[0],"value"]=np.nan
    with pytest.raises(ValueError,match="valid inputs"):
        build_targets(ew,{"X":f})
    with pytest.raises(ValueError,match="calendar"):
        build_targets(ew,{"X":f.iloc[:-1]})

def test_same_contract_six_sessions_and_gaps():
    idx=pd.bdate_range("2020-01-01",periods=15)
    b=pd.Series(np.arange(15)/10,index=idx)
    c=pd.Series(["IC1"]*7+["IC2"]*8,index=idx)
    actual=same_contract_delta(b,c)
    assert actual.iloc[:5].isna().all()
    np.testing.assert_allclose(actual.iloc[5:7],.5)
    assert actual.iloc[7:12].isna().all()
    np.testing.assert_allclose(actual.iloc[12:],.5)
    b.iloc[9]=np.nan
    assert same_contract_delta(b,c).iloc[9:15].isna().all()

def test_selection_uses_prior_oi_current_eligibility_and_deterministic_tie():
    previous=pd.Series({"IC1":20.,"IC2":30.,"IC3":30.})
    assert select_previous_oi({"IC1","IC2","IC3","NEW"},previous)=="IC2"
    assert select_previous_oi({"IC1","IC3"},previous)=="IC3"
    assert select_previous_oi({"IC1"},pd.Series({"IC1":0.})) is None
    assert select_previous_oi({"IC1","IC2"},pd.Series({"IC1":20.,"IC2":np.nan})) is None


def test_prelaunch_absence_is_unavailable_not_replaced_by_existing_source():
    from backtest.etf_member_delivery_contract import align_consumed_features
    dates=pd.bdate_range("2022-07-20",periods=4)
    f=pd.DataFrame({"value":[0.1,0.2],"feature_valid":[True,True]},index=dates[2:])
    p=pd.DataFrame({"r5":[.1]*4,"feature_valid":[True]*4},index=dates)
    out=align_consumed_features(f,p,dates)
    assert out.value.iloc[:2].isna().all()
    assert out.valid.tolist()==[False,False,True,True]
