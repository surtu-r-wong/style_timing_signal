"""Read-only acceptance of ETF/member deliveries; quality facts only, no strategy features."""
from pathlib import Path
import json, shutil
import numpy as np
import pandas as pd
from backtest.run_manifest import artifact_record

ROOT=Path.cwd()
OFFICE=Path("/home/elfbob/claude-code/data_manager/requests")
OUT=ROOT/"backtest/output/data_manager_acceptance_20260917_r2"
OUT.mkdir(exist_ok=False)
calendar=pd.read_csv(ROOT/"backtest/output/runs/20260917-ew-oi-research-r2/inputs/office/calendar.csv",parse_dates=["date"])
sessions=pd.DatetimeIndex(calendar.loc[calendar.sfe.eq(1),"date"])
receipts=[]
hash_mismatches=[]
def check_files(directory):
    m=json.loads((directory/"manifest.json").read_text())
    for item in m.get("artifacts",m.get("files",[])):
        actual=artifact_record(directory/item["path"],directory)
        if actual["sha256"]!=item["sha256"]:
            hash_mismatches.append({"source":str(directory/item["path"]),"expected":item["sha256"],"actual":actual["sha256"]})
            assert item["path"]=="collect.stdout.log", "payload hash mismatch blocks consumption"
        receipts.append({"source":str(directory/item["path"]),**actual})
    return m

etf=OFFICE/"2026-09-17-style-timing-signal-etf-share-flow/delivery/etf-share-flow-v1"
m=check_files(etf)
shutil.copytree(etf,OUT/"etf_delivery")
d=pd.read_csv(OUT/"etf_delivery/etf_share_daily.csv",parse_dates=["trade_date","nav_date"])
flow=pd.read_csv(OUT/"etf_delivery/net_flow_daily.csv",parse_dates=["trade_date"])
events=pd.read_csv(OUT/"etf_delivery/share_translation_events.csv",parse_dates=["trade_date"])
assert len(d)==len(flow)==7013
assert not d.duplicated(["fund_id","trade_date"]).any()
assert not flow.duplicated(["ts_code","trade_date"]).any()
etf_quality=[]; issues=[]
for fund,g in d.groupby("fund_id"):
    g=g.sort_values("trade_date").set_index("trade_date")
    f=flow.loc[flow.ts_code.eq(fund)].set_index("trade_date").reindex(g.index)
    expected=sessions[(sessions>=g.index.min()) & (sessions<=g.index.max())]
    assert expected.difference(g.index).empty
    assert g.loc[g.index>=sessions.min()].index.difference(expected).empty
    assert np.isfinite(g[["total_shares","unit_nav"]]).all().all()
    assert g.total_shares.ge(0).all() and g.unit_nav.gt(0).all()
    assert not g.nav_date.gt(g.index).any()
    net=g.total_shares-g.total_shares.shift(1)*f.event_factor
    np.testing.assert_allclose(net,f.net_new_shares,atol=1e-5,rtol=1e-10,equal_nan=True)
    implemented=net*g.unit_nav.shift(1)
    np.testing.assert_allclose(implemented,f.est_net_flow_cny,atol=.1,rtol=1e-10,equal_nan=True)
    # Unit reconciliation: office net_new_shares is in post-translation units.
    consistent=net*(g.unit_nav.shift(1)/f.event_factor)
    for date in f.index[f.event_adjusted]:
        issues.append({"fund":fund,"date":str(date.date()),"source":f.loc[date,"event_source"],
                       "factor":float(f.loc[date,"event_factor"]),
                       "net_new_shares_post_translation_units":float(net.loc[date]),
                       "delivered_amount_cny":float(f.loc[date,"est_net_flow_cny"]),
                       "unit_consistent_check_amount_cny":float(consistent.loc[date]),
                       "difference_cny":float(f.loc[date,"est_net_flow_cny"]-consistent.loc[date])})
    nullclose=g.index[g.close.isna()]
    etf_quality.append({"fund":fund,"rows":len(g),"first":str(g.index.min().date()),"last":str(g.index.max().date()),
                        "missing_close_dates":[str(x.date()) for x in nullclose],
                        "nav_date_earlier_than_trade_date":int(g.nav_date.lt(g.index).sum()),
                        "event_adjusted_days":int(f.event_adjusted.sum()),"large_move_days":int(f.large_move.sum()),
                        "independent_calendar_verified_from":str(max(g.index.min(),sessions.min()).date())})
pd.DataFrame(issues).to_csv(OUT/"etf_event_unit_reconciliation.csv",index=False)
members=[]
for phase,product in [("phase1-IM","IM"),("phase2-IC","IC")]:
    path=OFFICE/"2026-09-17-style-timing-signal-futures-member-feasibility/delivery"/phase
    m=check_files(path)
    dest=OUT/phase; dest.mkdir()
    for name in ["manifest.json","fetch_log.csv","member_alias.csv","not_disclosed.csv"]:
        shutil.copyfile(path/name,dest/name)
    log=pd.read_csv(path/"fetch_log.csv",parse_dates=["trade_date"])
    expected=sessions[(sessions>=pd.Timestamp(m["start"])) & (sessions<=pd.Timestamp(m["end"]))]
    assert pd.DatetimeIndex(log.trade_date.sort_values()).equals(expected)
    assert log.kind.eq("data").all() and log.http_status.eq(200).all()
    groups={}; quantity_rows=0; ids=set(); leading_zero_rows=0
    for chunk in pd.read_csv(path/"rank_long.csv",dtype={"member_id":str},chunksize=25000):
        assert chunk.disclosure_status.eq("disclosed").all()
        assert chunk.quantity.ge(0).all() and chunk["rank"].between(1,20).all()
        assert chunk.member_id.notna().all() and chunk.member_id.str.fullmatch(r"\d{4}").all()
        quantity_rows+=len(chunk); ids.update(chunk.member_id)
        leading_zero_rows+=int(chunk.member_id.str.startswith("0").sum())
        for key,g in chunk.groupby(["trade_date","contract","rank_type"],sort=False):
            ranks=groups.setdefault(key,set())
            incoming=set(g["rank"].astype(int))
            assert len(incoming)==len(g) and not ranks.intersection(incoming)
            ranks.update(incoming)
    assert quantity_rows==m["rank_rows"]
    assert all(ranks==set(range(1,21)) for ranks in groups.values())
    missing=pd.read_csv(path/"not_disclosed.csv",dtype={"member_id":str})
    assert len(missing)==m["not_disclosed_rows"] and missing.disclosure_status.eq("not_disclosed").all()
    assert missing[["quantity","change"]].isna().all().all()
    assert not missing.duplicated(["trade_date","contract","rank_type"]).any()
    assert not set(zip(missing.trade_date,missing.contract,missing.rank_type)).intersection(groups)
    aliases=pd.read_csv(path/"member_alias.csv",dtype={"member_id":str})
    assert ids <= set(aliases.member_id)
    products_per_day={}
    for date,contract,kind in groups:
        products_per_day.setdefault(date,set()).add(contract)
    members.append({"product":product,"days":len(expected),"rank_rows":quantity_rows,"boards":len(groups),
                    "not_disclosed_rows":len(missing),"member_ids":len(ids),
                    "leading_zero_rows":leading_zero_rows,
                    "days_with_only_one_disclosed_contract":sum(len(v)==1 for v in products_per_day.values()),
                    "rank_keys_unique_and_each_board_1_to_20":True,"ids_preserved_as_strings":True})
report={"etf_raw_arithmetic_and_hashed_files_checked":True,"etf_derivative_status":"event_unit_reconciliation_pending_office",
        "etf":etf_quality,"event_unit_issues":issues,"members":members,
        "member_acceptance_scope":"hashes, calendar, board ranks, declared nondisclosure, alias coverage; no member indicators derived",
        "hash_mismatches":hash_mismatches,"all_member_payload_hashes_match":True,"full_member_manifest_pass":not hash_mismatches,"no_market_dataset_repaired":True}
(OUT/"verification.json").write_text(json.dumps(report,ensure_ascii=False,indent=2))
(OUT/"source_receipts.json").write_text(json.dumps(receipts,ensure_ascii=False,indent=2))
print(json.dumps(report,ensure_ascii=False,indent=2))
