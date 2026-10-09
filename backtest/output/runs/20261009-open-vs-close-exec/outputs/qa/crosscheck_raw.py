# brute-force spot checks straight from the CSV text (no reuse of qa.py functions)
import csv, json
from collections import defaultdict
I='/tmp/claude-1000/-home-elfbob-claude-code-style-timing-signal/8ebe6247-4dcd-4083-a029-e12de7a53103/scratchpad/openexec/inputs/'
rows=list(csv.DictReader(open(I+'futures.csv')))
by=defaultdict(dict)
for r in rows: by[r['date']][r['symbol']]=r
dates=sorted(by)
pos={r['date']:float(r['position']) for r in csv.DictReader(open(I+'equal_weight_symmetric.csv'))}
pdates=sorted(pos)
def main(d,g):
    c=[(-int(r['oi']),s) for s,r in by[d].items() if s.startswith(g)]
    return min(c)[1] if c else None
# all-days limit rule on held contracts
cnt=0; ex=[]
for i in range(1,len(dates)):
    t,tp=dates[i],dates[i-1]
    for g in ('IC','IM'):
        h=main(tp,g)
        if not h: continue
        r=by[t][h]; O,H,L=float(r['open']),float(r['high']),float(r['low'])
        ps=float(r['pre_settle']) if r['pre_settle'] else None
        if (ps and abs(O/ps-1)>=0.095) or (O==H==L):
            cnt+=1; ex.append((t,h,O,H,L,ps))
print('all held-contract days flagged by spec rule:',cnt, ex[:12])
# first long->short flip after 2015-04-17
fl=[]
for i in range(2,len(pdates)):
    t=pdates[i]
    if t<'2015-04-17': continue
    po,pn=pos[pdates[i-2]],pos[pdates[i-1]]
    if po==1 and pn==-1: fl.append(t)
t=fl[0]; tp=dates[dates.index(t)-1]
h=main(tp,'IC'); r=by[t][h]
intra=float(r['close'])/float(r['open'])-1; gap=float(r['open'])/float(by[tp][h]['close'])-1
print('first 多翻空 day',t,'held',h,'O',r['open'],'C',r['close'],'C_prev',by[tp][h]['close'],'intra',intra,'gap',gap,'e per leg',-intra,'o per leg',-gap)
# 2017-01-04 gap
t='2017-01-04'; tp=dates[dates.index(t)-1]; h=main(tp,'IC'); print('2017-01-04 held',h,'O',by[t][h]['open'],'C_prev',by[tp][h]['close'])
# count flips
nf={'LS':0,'SL':0}
for i in range(2,len(pdates)):
    t=pdates[i]
    if t<'2015-04-17': continue
    po,pn=pos[pdates[i-2]],pos[pdates[i-1]]
    if po==1 and pn==-1: nf['LS']+=1
    if po==-1 and pn==1: nf['SL']+=1
print('flip counts',nf)
