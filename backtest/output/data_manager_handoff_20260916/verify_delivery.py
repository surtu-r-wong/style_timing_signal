from pathlib import Path
from datetime import datetime, timezone
from decimal import Decimal
import csv,hashlib,json,shutil,sys
import pandas as pd
ROOT=Path('/home/elfbob/claude-code/style_timing_signal')
sys.path.insert(0,str(ROOT))
from backtest.data import _connect
from backtest.flow_field_readiness import FIELDS
from signals.common.config import load_db_config
RUN=ROOT/'backtest/output/data_manager_handoff_20260916'
OFFICE=Path('/home/elfbob/claude-code/data_manager/requests/2026-09-16-style-timing-signal-index-money-flow-backfill')
def main():
 RUN.mkdir(exist_ok=False)
 shutil.copyfile(__file__,RUN/'verify_delivery.py')
 with (ROOT/'backtest/output/runs/20260916-ew-leg-flow-readiness-r1/inputs/money_flow.csv').open() as h:old=list(csv.DictReader(h))
 cols=['index_code','trade_date','wind_sector',*FIELDS,'src','fetched_at','updated_at']
 codes=['000300.SH','399102.SZ','000680.SH']
 conn=_connect(load_db_config())
 try:
  conn.set_session(readonly=True,isolation_level='REPEATABLE READ')
  with conn.cursor() as q:
   q.execute("SET LOCAL statement_timeout='30s'")
   q.execute('SELECT transaction_timestamp()::text');stamp=q.fetchone()[0]
   q.execute('SELECT '+','.join(cols)+" FROM stock_selector.index_money_flow WHERE index_code=ANY(%s) AND trade_date<='2026-09-15' ORDER BY index_code,trade_date",(codes,))
   current=[dict(zip(cols,row)) for row in q.fetchall()]
   q.execute("SELECT calendar_date FROM public.trading_calendar WHERE sfe=true AND calendar_date BETWEEN '2026-09-04' AND '2026-09-15' ORDER BY calendar_date")
   calendar=[str(row[0]) for row in q.fetchall()]
   q.execute("SELECT index_code,min(trade_date),max(trade_date),count(*),count(*) FILTER (WHERE close IS NULL) FROM stock_selector.index_daily WHERE index_code=ANY(%s) GROUP BY 1 ORDER BY 1",(codes+['000905.SH','000852.SH'],))
   prices=[dict(zip(['index_code','start','end','rows','null_close'],row)) for row in q.fetchall()]
   q.execute("SELECT index_code,trade_date FROM stock_selector.index_daily WHERE index_code=ANY(%s) AND trade_date BETWEEN '2026-09-04' AND '2026-09-15' AND close IS NOT NULL",(codes+['000905.SH','000852.SH'],))
   pxkeys={(c,str(d)) for c,d in q.fetchall()}
  conn.rollback()
 finally:conn.close()
 idx={(r['index_code'],str(r['trade_date'])):r for r in current}
 assert len(idx)==len(current),'duplicate keys'
 historical=[r for r in current if str(r['trade_date'])<'2026-09-04']
 assert len(historical)==len(old)==7403,'frozen history row count changed'
 for r in old:
  actual=idx[(r['index_code'],r['trade_date'])]
  for field in FIELDS:assert Decimal(r[field])==actual[field],(r['index_code'],r['trade_date'],field)
  for field in ['wind_sector','src']:assert r[field]==actual[field]
 added=[r for r in current if str(r['trade_date'])>='2026-09-04']
 assert len(calendar)==8 and len(added)==24,'unexpected P0 coverage'
 for code in codes:
  ds=[r for r in added if r['index_code']==code]
  assert sorted(str(r['trade_date']) for r in ds)==calendar
  assert all(r['src']=='wind:wset:marketmoneyflows' for r in ds)
  for r in ds:
   assert all(r[k] is not None and Decimal(str(r[k])).is_finite() for k in FIELDS)
   assert r['main_in_money']>=0 and r['main_out_money']>=0
   assert r['main_in_money']+r['main_out_money']>0
 pd.DataFrame(added).to_csv(RUN/'p0_added_rows.csv',index=False)
 refs=[]
 for name in ['disposition.md','response-01-office-2026-09-16.md','response-02-office-2026-09-16.md']:
  shutil.copyfile(OFFICE/name,RUN/name)
  refs.append({'path':str(OFFICE/name),'sha256':hashlib.sha256((OFFICE/name).read_bytes()).hexdigest()})
 receipt={'observed_at':stamp,'completed_utc':datetime.now(timezone.utc).isoformat(),'transaction_read_only':True,'p0_passed':True,'total_requested_codes_through_20260915':len(current),'new_rows':len(added),'expected_business_dates':calendar,'old_7403_rows_all_11_numeric_fields_decimal_exact_match':True,'numeric_values_compared':len(old)*len(FIELDS),'old_sector_and_source_match':True,'new_rows_nonnull_finite_and_positive_denominator':True,'price_coverage':prices,'missing_price_dates_in_p0_window':{r['index_code']:[d for d in calendar if (r['index_code'],d) not in pxkeys] for r in prices},'p1_status':'pending_exact_I_U_vendor_field_codes_and_office_new_table_governance','source_replies':refs,'no_vendor_calls_or_database_writes':True}
 (RUN/'verification.json').write_text(json.dumps(receipt,ensure_ascii=False,indent=2,default=str)+'\n')
 print(json.dumps(receipt,ensure_ascii=False,default=str))
if __name__=='__main__':
 try:main()
 except Exception as exc:raise SystemExit('Delivery verification failed: '+type(exc).__name__) from None
