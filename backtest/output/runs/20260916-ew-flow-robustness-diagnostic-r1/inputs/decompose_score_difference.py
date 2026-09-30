from pathlib import Path
import shutil
import numpy as np
import pandas as pd
R=Path('backtest/output/runs/20260916-ew-flow-robustness-diagnostic-r1')
P=Path('backtest/output/runs/20260916-ew-flow-source-ablation-r1')
(R/'inputs/score_decomposition_scope.md').write_text('在查看18个分歧日和收益归因后追加：对分数差做OLS残差的精确代数分解，区分20日内原始B差、当日控制差、历史系数差；不生成混合输入信号或收益候选，不为任何单个原始差异日作因果认定。\n')
shutil.copyfile(__file__,R/'inputs/decompose_score_difference.py')
shutil.copyfile(P/'inputs/board_prices.csv',R/'inputs/wset_board_prices.csv')
scores=pd.read_csv(R/'inputs/scores.csv',index_col=0,parse_dates=True);oldscore=pd.read_csv(R/'inputs/wset_continued_scores.csv',index_col=0,parse_dates=True)
idx=scores.index
a=pd.read_csv(R/'inputs/flow.csv',parse_dates=['trade_date']).query("index_code=='000300.SH'").set_index('trade_date').reindex(idx)
b=pd.read_csv(R/'inputs/wset_money_flow.csv',parse_dates=['trade_date']).query("index_code=='000300.SH'").set_index('trade_date').reindex(idx)
np_=pd.read_csv(R/'inputs/prices.csv',parse_dates=['date']).query("index_code=='000300.SH'").set_index('date').close.reindex(idx)
op=pd.read_csv(R/'inputs/wset_board_prices.csv',parse_dates=['date']).query("index_code=='000300.SH'").set_index('date').close.reindex(idx)
ni=a.xlarge_buy_money+a.large_buy_money;nu=a.xlarge_sell_money+a.large_sell_money
ng=ni+nu;og=b.main_in_money+b.main_out_money
ny=(a.end_main_inflow_money-a.open_main_inflow_money)/ng
o_y=(b.end_main_inflow_money-b.open_main_inflow_money)/og
nx=np.column_stack([np.ones(len(idx)),(ni-nu)/ng,np_.pct_change(fill_method=None)])
ox=np.column_stack([np.ones(len(idx)),(b.main_in_money-b.main_out_money)/og,op.pct_change(fill_method=None)])
rows=[]
for date in pd.read_csv(R/'outputs/source_decision_disagreements.csv',parse_dates=['decision_date']).decision_date:
 end=idx.get_loc(date);parts=[];old_res=[];new_res=[]
 for t in range(end-19,end+1):
  ob,_,rank1,_=np.linalg.lstsq(ox[t-250:t],o_y.iloc[t-250:t],rcond=None)
  nb,_,rank2,_=np.linalg.lstsq(nx[t-250:t],ny.iloc[t-250:t],rcond=None)
  assert rank1==rank2==3
  direct=ny.iloc[t]-o_y.iloc[t]
  control=-(nx[t]-ox[t])@ob
  history=-nx[t]@(nb-ob)
  parts.append((direct,control,history))
  old_res.append(o_y.iloc[t]-ox[t]@ob);new_res.append(ny.iloc[t]-nx[t]@nb)
 p=np.array(parts).mean(axis=0);gap=float(scores.loc[date,'CSI300']-oldscore.loc[date,'CSI300'])
 np.testing.assert_allclose(np.mean(old_res),oldscore.loc[date,'CSI300'],atol=1e-12,rtol=0)
 np.testing.assert_allclose(np.mean(new_res),scores.loc[date,'CSI300'],atol=1e-12,rtol=0)
 np.testing.assert_allclose(p.sum(),gap,atol=1e-12,rtol=0)
 rows.append({'decision_date':str(date.date()),'new_minus_old_score':gap,'raw_B_difference_20d':p[0],'current_controls_difference_20d':p[1],'historical_coefficients_difference_20d':p[2], 'max_control_price_difference_270d':float((np_-op).iloc[end-270:end+1].abs().max())})
d=pd.DataFrame(rows);d.to_csv(R/'outputs/score_difference_decomposition.csv',index=False)
print(d.to_string(index=False))
