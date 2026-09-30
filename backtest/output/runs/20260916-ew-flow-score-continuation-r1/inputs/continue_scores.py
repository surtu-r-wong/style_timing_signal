from pathlib import Path
from datetime import datetime, timezone
import json,shutil,sys
import numpy as np
import pandas as pd
ROOT=Path('/home/elfbob/claude-code/style_timing_signal');sys.path.insert(0,str(ROOT))
from backtest.data import _connect
from backtest.run_flow_source_ablation import source_scores,CODES
from backtest.run_manifest import create_run_dir,artifact_record,write_manifest
from signals.common.config import load_db_config

def main():
 r=create_run_dir(ROOT/'backtest/output/runs','20260916-ew-flow-score-continuation-r1')
 scope='''沿用已固定的B_neg来源分数，250日历史OLS、20日平滑、不换参数/方向/来源权重。只延伸研究分数，不计算收益、不部署；未具备价格控制的日期保留缺失，不填充。此前至2026-09-03的输入与分数须复现。新增WSET资金流取已验收的P0快照；仅从库读取9月4至15日价格与独立交易日历。已向data_manager提供字段无需用户重复；新增WSS回填等待办公室落盘交付。'''
 (r/'inputs/scope.md').write_text(scope+'\n')
 shutil.copyfile(__file__,r/'inputs/continue_scores.py')
 prior=ROOT/'backtest/output/runs/20260916-ew-flow-source-ablation-r1'
 for filename in ['money_flow.csv','board_prices.csv','star_prices.csv']:
  shutil.copyfile(prior/'inputs'/filename,r/'inputs'/filename)
 shutil.copyfile(prior/'outputs/source_scores.csv',r/'inputs/prior_scores.csv')
 handoff=ROOT/'backtest/output/data_manager_handoff_20260916'
 for filename in ['p0_added_rows.csv','verification.json']:
  shutil.copyfile(handoff/filename,r/'inputs'/filename)
 for filename in ['backtest/run_flow_source_ablation.py','backtest/intraday_flow_pilot.py']:
  shutil.copyfile(ROOT/filename,r/'inputs'/('code__'+filename.replace('/','__')))
 conn=_connect(load_db_config())
 try:
  conn.set_session(readonly=True,isolation_level='REPEATABLE READ')
  with conn.cursor() as q:
   q.execute("SET LOCAL statement_timeout='30s'")
   q.execute('SELECT transaction_timestamp()::text');stamp=q.fetchone()[0]
   q.execute("SELECT trade_date,index_code,close FROM stock_selector.index_daily WHERE index_code=ANY(%s) AND trade_date BETWEEN '2026-09-04' AND '2026-09-15' ORDER BY 1,2",(list(CODES.values()),))
   prices=pd.DataFrame(q.fetchall(),columns=['date','index_code','close'])
   q.execute("SELECT calendar_date FROM public.trading_calendar WHERE sfe=true AND calendar_date BETWEEN '2026-09-04' AND '2026-09-15' ORDER BY 1")
   extension=pd.DatetimeIndex([row[0] for row in q.fetchall()],name='date')
  conn.rollback()
 finally:conn.close()
 prices.date=pd.to_datetime(prices.date);prices.close=pd.to_numeric(prices.close)
 prices.to_csv(r/'inputs/price_extension.csv',index=False)
 pd.DataFrame({'date':extension}).to_csv(r/'inputs/calendar_extension.csv',index=False)
 old=pd.read_csv(r/'inputs/prior_scores.csv',index_col=0,parse_dates=True)
 calendar=old.index.append(extension)
 assert not calendar.has_duplicates and calendar.is_monotonic_increasing
 flow=pd.concat([pd.read_csv(r/'inputs/money_flow.csv',parse_dates=['trade_date']),pd.read_csv(r/'inputs/p0_added_rows.csv',parse_dates=['trade_date'])],ignore_index=True)
 board=pd.concat([pd.read_csv(r/'inputs/board_prices.csv',parse_dates=['date']),pd.read_csv(r/'inputs/star_prices.csv',parse_dates=['date']),prices],ignore_index=True)
 assert not flow.duplicated(['index_code','trade_date']).any()
 assert not board.duplicated(['index_code','date']).any()
 scores,residuals=source_scores(flow,board,calendar)
 np.testing.assert_allclose(scores.loc[old.index,old.columns],old,rtol=0,atol=1e-12,equal_nan=True)
 added=scores.loc[extension];added.to_csv(r/'outputs/score_extension.csv',index_label='date')
 scores.to_csv(r/'outputs/source_scores.csv',index_label='date')
 residuals.loc[extension].to_csv(r/'outputs/residual_extension.csv',index_label='date')
 keep=added.ge(-1e-12).astype(float).where(added.notna())
 keep.to_csv(r/'outputs/conditional_keep_short.csv',index_label='date')
 availability=[]
 for dt in extension:
  available=added.loc[dt].notna().all()
  availability.append({'date':str(dt.date()),'score_ready':bool(available),'missing_price_codes':[c for c in CODES.values() if prices.loc[prices.date.eq(dt)&prices.index_code.eq(c),'close'].dropna().empty]})
 verification={'observed_at':stamp,'old_scores_reproduced_atol':1e-12,'unchanged_formula_and_direction':True,'extended_business_days':len(extension),'ready_days':int(added.notna().all(axis=1).sum()),'availability':availability,'conditional_gate_only_not_actual_positions':True,'no_returns_computed':True,'no_vendor_calls_no_database_writes':True}
 (r/'outputs/verification.json').write_text(json.dumps(verification,indent=2))
 latest=added.dropna().iloc[-1];date=added.dropna().index[-1].date()
 report=f'''# 原资金流研究分数续算\n\n沿固定B_neg规则，仅更新研究分数；截至{date}有{verification['ready_days']}个新增完整交易日。历史分数全部在1e-12内复现。\n\n````\n{added.to_string()}\n````\n\nconditional_keep_short.csv只表示现役指空时保留空头的条件（分数>0，绝对值<=1e-12按原规则保留），不是实际仓位或下单指令。价格缺失日期保留空值，不用旧价格或旧分数补齐。新增日期很少且属于回溯续算，不作独立收益确认。没有运行收益比较或修改生产。\n'''
 (r/'outputs/REPORT.md').write_text(report)
 write_manifest(r,{'status':'complete','completed_utc':datetime.now(timezone.utc).isoformat(),'stage':'fixed_score_continuation_no_returns','artifacts':[artifact_record(p,r) for p in sorted(r.rglob('*')) if p.is_file()]})
 print(json.dumps(verification,ensure_ascii=False));print('Latest:',str(date),latest.to_dict());print(added.to_string())
if __name__=='__main__':
 try:main()
 except Exception as exc:raise SystemExit('Score continuation failed: '+type(exc).__name__) from None
