"""Also compare every historical LF candidate with the CURRENT symmetric EW incumbent."""
import audit as a
import pandas as pd
MAIN=a.OUT;EXTRA=MAIN/'current_reference';EXTRA.mkdir(exist_ok=True)
c=pd.read_csv(MAIN/'candidate_catalog.csv');c=c[(c.model=='carry')&(c.reference=='ew_lf')].copy()
c['reference']='ew_sym';c['candidate']=c['name']+'__carry__ew_sym'
assert not c.candidate.isin(pd.read_csv(MAIN/'candidate_catalog.csv').candidate).any(), 'already merged'
c['detail']=c.detail.fillna('')+'; supplemental current symmetric EW comparator'
p=pd.read_csv(MAIN/'candidate_positions.csv',index_col='date',parse_dates=True)
positions={name:p[name].dropna() for name in c.name}
refs=dict(pd.read_csv(MAIN/'reference_positions.csv',index_col='date',parse_dates=True).items())
m=pd.read_csv(MAIN/'market.csv',index_col='date',parse_dates=True)
c.to_csv(EXTRA/'candidate_catalog.csv',index=False)
a.OUT=EXTRA;a.analyze(c,positions,refs,m.underlying,m['carry'])
for f in ['candidate_catalog.csv','event_attribution.csv','event_position_response.csv','primary_comparisons.csv','rank_flips.csv']:
 pd.concat([pd.read_csv(MAIN/f),pd.read_csv(EXTRA/f)],ignore_index=True).to_csv(MAIN/f,index=False)
print('CURRENT REFERENCE',len(c),flush=True)
