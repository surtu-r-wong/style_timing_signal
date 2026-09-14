from pathlib import Path
import json
import platform
import numpy as np
import pandas as pd
from backtest.grid_reassessment import INCUMBENTS, WINDOWS
from backtest.scan import scan_grid
from signals.equal_weight.generate_signal import calculate_contrast_equal_weight_signal, load_pair_configs
from backtest.momentum_scan import momentum_pair_factor
from backtest.forward_observation import verify_campaign

root=Path.cwd()
run=root/'backtest/output/runs/20260914-grid-reassessment-r1'
prices=pd.read_csv(run/'inputs/style_prices.csv', index_col='date',parse_dates=True)
configs=load_pair_configs(run/'inputs/config_4pairs.csv')
spot=pd.read_csv(run/'inputs/spot.csv',parse_dates=['date'])
und=spot.pivot(index='date',columns='symbol',values='close').pct_change().mean(axis=1).dropna()
carry=pd.read_csv(run/'outputs/carry_comparison.csv',index_col='date',parse_dates=True)
table=pd.read_csv(run/'outputs/full_grid.csv')
checks=[]
for family,name in INCUMBENTS.items():
    if family=='ew':
        fn=lambda: calculate_contrast_equal_weight_signal(prices,lookback=20,z_window=40,smoothing_window=5,pair_configs=configs).factor_value
    else:
        fn=lambda: momentum_pair_factor(prices,[c.effective_columns() for c in configs],family='slope',length=20,skip=0,z_window=120,smoothing=0)
    factor=fn()
    for regime,col in [('old_sym','old'),('fixed_sym','fixed')]:
        ref=scan_grid(lambda:factor,[{}],und,carry[col],WINDOWS).iloc[0]
        actual=table[table.name.eq(name)&table.regime.eq(regime)].iloc[0]
        error=max(abs(ref[f'sharpe_{w}']-actual[f'sharpe_{w}']) for w in WINDOWS)
        assert error<1e-12
        checks.append({'name':name,'regime':regime,'reference':'original scan_grid','max_abs_error':error})
for cost in (3,10):
    new=pd.read_csv(run/f'outputs/ledger_two_pool_ew_L20_zw40_sm5__slope_L20s0_zw120_sm0_{cost}bps.csv',index_col='date')
    old=pd.read_csv(root/f'backtest/output/runs/20260914-execution-audit-r2/outputs/ledger_two_pool_spotclose_futclose_{cost}bps_separate.csv',index_col='date')
    pd.testing.assert_frame_equal(new,old,rtol=1e-12,atol=1e-12)
    checks.append({'cost_bps':cost,'reference':'prior execution-audit-r2 full daily ledger','matched':True})
checks.append({'campaign':verify_campaign(root/'backtest/output/forward/20260914-incumbents-v1')})
payload={'checks':checks,'python':platform.python_version(),'numpy':np.__version__,'pandas':pd.__version__}
(run/'outputs/cross_validation.json').write_text(json.dumps(payload,ensure_ascii=False,indent=2,default=str))
print(json.dumps(payload,ensure_ascii=False,indent=2,default=str))
