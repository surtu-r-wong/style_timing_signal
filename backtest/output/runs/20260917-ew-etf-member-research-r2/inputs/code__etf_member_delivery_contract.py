"""Independent consumer checks; reference features stay in memory, never repaired on disk."""
from pathlib import Path
import json
import numpy as np
import pandas as pd

def compare(actual, expected, label, errors):
    assert actual.index.equals(expected.index), label+" calendar"
    np.testing.assert_allclose(actual, expected, atol=1e-10, rtol=1e-9, equal_nan=True, err_msg=label)
    errors.append({"field":label, "max_abs_error":float((actual-expected).abs().max()) if expected.notna().any() else None})

def same_contract_delta(balance, selected):
    valid = balance.notna().rolling(6, min_periods=6).sum().eq(6)
    for lag in range(1, 6):
        valid &= selected.eq(selected.shift(lag))
    return (balance-balance.shift(5)).where(valid)

def select_previous_oi(current, previous):
    candidates = sorted(set(current) & set(previous.index))
    if not candidates:
        return None
    values = previous.reindex(candidates)
    if not np.isfinite(values).all() or (values<0).any() or values.max()<=0:
        return None
    return sorted(values.index[values.eq(values.max())])[0]

def accept_delivery(inputs):
    directory=inputs/"office"
    src=inputs/"sources"
    errors=[]
    calendar=pd.read_csv(directory/"calendar.csv", parse_dates=["date"])
    sessions=pd.DatetimeIndex(calendar.loc[calendar.sfe.eq(1),"date"])
    assert sessions.is_unique and sessions.is_monotonic_increasing
    assert str(sessions[0].date())=="2015-01-05" and str(sessions[-1].date())=="2026-09-16"
    original=pd.read_csv(src/"calendar.csv",parse_dates=["date"])
    expected=pd.DatetimeIndex(original.loc[original.sfe.eq(1)&original.date.between(sessions[0],sessions[-1]),"date"])
    assert sessions.equals(expected)
    frames={}
    for name,key,value in [("etf_fund_features","fund_id","e5"),("etf_features","source","value"),
                           ("member_product_features","product","m5"),("member_features","source","value"),
                           ("price_features","source","r5")]:
        f=pd.read_csv(directory/(name+".csv"),parse_dates=["date"])
        assert not f.duplicated([key,"date"]).any()
        assert f.feature_valid.dtype==bool
        assert f.feature_valid.equals(f[value].notna())
        assert f.loc[~f.feature_valid,"invalid_reason"].notna().all()
        frames[name]=f
    flow=pd.read_csv(src/"etf_net_flow.csv",parse_dates=["trade_date"])
    refs={}
    for fund,g in flow.groupby("ts_code"):
        first=max(g.trade_date.min(),sessions[0])
        fund_sessions=sessions[sessions>=first]
        g=g.set_index("trade_date").reindex(sessions)
        a=g.net_new_shares/(g.total_shares.shift(1)*g.event_factor)
        a=a.where(g.total_shares.shift(1).gt(0)&g.event_factor.gt(0)&~g.event_source.eq("detected"))
        e=a.rolling(5,min_periods=5).mean()
        f=frames["etf_fund_features"].query("fund_id==@fund").set_index("date")
        assert f.index.equals(fund_sessions)
        compare(f.a,a.reindex(f.index),fund+" a",errors)
        compare(f.e5,e.reindex(f.index),fund+" e5",errors)
        compare(f.previous_shares,g.total_shares.shift(1).reindex(f.index),fund+" previous_shares",errors)
        refs[fund]=e
    er={"ETF500":refs["510500.SH"],"ETFPOOL":(refs["510500.SH"]+(refs["512100.SH"]+refs["159845.SZ"])/2)/2}
    for name,e in er.items():
        f=frames["etf_features"].query("source==@name").set_index("date")
        compare(f.value,e,name,errors)
    detail=pd.read_csv(src/"contract_detail.csv",parse_dates=["date"])
    detail["contract"]=detail.symbol.str.replace(".CFE","",regex=False)
    life=pd.read_csv(src/"lifecycle_audit.csv",parse_dates=["date"])
    mr={}
    quality=[]
    for product in ["IC","IM"]:
        d=detail.loc[detail["product"].eq(product)]
        assert np.isfinite(d.oi).all() and d.oi.ge(0).all()
        dates=pd.DatetimeIndex(sorted(d.date.unique()))
        assert dates.equals(sessions[(sessions>=dates[0])&(sessions<=dates[-1])])
        rank=pd.read_csv(src/("rank_"+product+".csv"),dtype={"member_id":str},
                         usecols=["trade_date","contract","rank_type","rank","member_id","quantity"])
        rank=rank.loc[rank.rank_type.isin(["long_oi","short_oi"])]
        assert rank.member_id.str.fullmatch(r"\d{4}").all()
        assert rank.quantity.ge(0).all()
        assert not rank.duplicated(["trade_date","contract","rank_type","member_id"]).any()
        board=rank.groupby(["trade_date","contract","rank_type"]).agg(
            total=("quantity","sum"),count=("rank","size"),unique=("rank","nunique"),low=("rank","min"),high=("rank","max"))
        assert ((board["count"]==20)&(board.unique==20)&(board.low==1)&(board.high==20)).all()
        oi=d.pivot(index="date",columns="contract",values="oi")
        live=life.loc[life["product"].eq(product)].set_index("date")
        selected=[]; longs=[]; shorts=[]
        for i,date in enumerate(dates):
            actual=set(d.loc[d.date.eq(date),"contract"])
            listed={x.replace(".CFE","") for x in json.loads(live.loc[date,"expected_by_rule"])}
            assert actual==listed
            previous=oi.iloc[i-1].dropna() if i else pd.Series(dtype=float)
            c=select_previous_oi(listed,previous)
            selected.append(c)
            totals=[]
            for kind in ["long_oi","short_oi"]:
                key=(str(date.date()),c,kind)
                totals.append(board.loc[key,"total"] if key in board.index else np.nan)
            longs.append(totals[0]);shorts.append(totals[1])
        selected=pd.Series(selected,index=dates,dtype=object)
        L,S=pd.Series(longs,index=dates),pd.Series(shorts,index=dates)
        b=((L-S)/(L+S)).where((L+S)>0)
        m=same_contract_delta(b,selected)
        f=frames["member_product_features"].query("product==@product").set_index("date")
        assert f.index.equals(dates)
        assert f.selected_contract.fillna("").equals(selected.fillna("").rename("selected_contract"))
        assert f.selection_valid.equals(selected.notna().rename("selection_valid"))
        compare(f.long20,L,product+" long20",errors)
        compare(f.short20,S,product+" short20",errors)
        compare(f.balance,b,product+" balance",errors)
        compare(f.m5,m,product+" m5",errors)
        assert f.board_valid.equals(b.notna().rename("board_valid"))
        for i,date in enumerate(dates):
            if selected.loc[date] is not None:
                assert pd.Timestamp(f.loc[date,"selection_date"])==dates[i-1]
                assert f.loc[date,"selection_oi"]==oi.loc[dates[i-1],selected.loc[date]]
        mr[product]=m.reindex(sessions)
        quality.append({"product":product,"days":len(dates),"valid_days":int(m.notna().sum()),
                        "fallback_days":int(m.isna().sum()),"selected_contract_changes":int((selected.ne(selected.shift())&selected.notna()&selected.shift().notna()).sum())})
    expected_member={"MEMBER_IC":mr["IC"],"MEMBER_POOL":(mr["IC"]+mr["IM"])/2}
    for name,e in expected_member.items():
        f=frames["member_features"].query("source==@name").set_index("date")
        expected_dates=sessions if name=="MEMBER_IC" else sessions[sessions>=detail.loc[detail["product"].eq("IM"),"date"].min()]
        assert f.index.equals(expected_dates)
        compare(f.value,e.reindex(f.index),name,errors)
    products=pd.read_csv(src/"product_daily.csv",parse_dates=["date"])
    prices={}
    for product in ["IC","IM"]:
        px=products.loc[products["product"].eq(product)].set_index("date").index_close.reindex(sessions)
        prices[product]=px/px.shift(5)-1
    pr={"IC":prices["IC"],"POOL":(prices["IC"]+prices["IM"])/2}
    for name,e in pr.items():
        f=frames["price_features"].query("source==@name").set_index("date")
        compare(f.r5,e,"r5 "+name,errors)
    features={}
    for kind in ["etf_features","member_features"]:
        for name,f in frames[kind].groupby("source"):
            f=f.set_index("date")
            # Consume office columns after numerical validation, not reconstructed reference values.
            price_source="POOL" if "POOL" in name else "IC"
            p=frames["price_features"].loc[frames["price_features"].source.eq(price_source)].set_index("date")
            features[name]=align_consumed_features(f,p,sessions)
    return features,sessions,{"status":"pass","numeric_checks":errors,"member_coverage":quality,
        "calendar_and_keys":True,"prior_oi_selection_and_same_contract_windows":True,
        "reference_values_persisted_or_traded":False,"historical_pit_proven":False}


def align_consumed_features(feature, price, sessions):
    """Represent prelaunch absence as unavailable; never synthesize a source value."""
    f,p=feature.reindex(sessions),price.reindex(sessions)
    return pd.DataFrame({"value":f.value,"r5":p.r5,
                         "valid":f.feature_valid.eq(True)&p.feature_valid.eq(True)},index=sessions)
