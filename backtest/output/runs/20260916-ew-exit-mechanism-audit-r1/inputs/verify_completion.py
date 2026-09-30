from pathlib import Path
import json
import shutil
import difflib
import numpy as np
import pandas as pd
from backtest.run_manifest import artifact_record,write_manifest
root=Path.cwd();run=root/'backtest/output/runs/20260916-ew-exit-mechanism-audit-r1'
m=json.loads((run/'manifest.json').read_text());assert m['status']=='computed_pending_report'
assert artifact_record(run/'inputs/prereg.md',run)['sha256']==m['prereg_sha256']
assert (run/'inputs/prereg.md').read_bytes()==(root/'docs/plans/2026-09-16-ew-exit-mechanism-audit.md').read_bytes()
for rec in m['artifacts']:
 assert artifact_record(run/rec['path'],run)==rec
receipts=json.loads((run/'inputs/source_receipts.json').read_text())
for rec in receipts:
 assert artifact_record(root/rec['path'],root)==rec
for name in ['flow_structure_followup.py','intraday_flow_pilot.py','execution_ledger.py','run_intraday_flow_pilot.py','run_flow_robustness_diagnostic.py']:
 assert (root/'backtest'/name).read_bytes()==(run/'inputs'/('code__'+name)).read_bytes()
assert (root/'backtest/run_exit_mechanism_audit.py').read_bytes()==(run/'inputs/corrected__run_exit_mechanism_audit.py').read_bytes()
assert (root/'tests/test_exit_mechanism_audit.py').read_bytes()==(run/'inputs/corrected__test_exit_mechanism_audit.py').read_bytes()
old=(run/'inputs/code__run_exit_mechanism_audit.py').read_text()
expected=old.replace("'daily_reentries': int(daily_entries.iloc[1:].sum()),", "'daily_reentries': max(0, int(daily_entries.sum())-1),\n                            'daily_entries_after_baseline_start': int(daily_entries.iloc[1:].sum()),")
assert expected==(run/'inputs/corrected__run_exit_mechanism_audit.py').read_text()
(run/'outputs/counter_code_diff.patch').write_text(''.join(difflib.unified_diff(old.splitlines(keepends=True),expected.splitlines(keepends=True),fromfile='executed_snapshot',tofile='corrected_counter_only')))
t=pd.read_csv(run/'outputs/decision_targets.csv',index_col=0,parse_dates=True)
assert t.shape==(2330,32) and np.isfinite(t).all().all() and t.abs().le(1).all().all()
defs=pd.read_csv(run/'outputs/definitions.csv')
for d in defs.itertuples():
 np.testing.assert_allclose(t['C__'+d.candidate]-t['S__'+d.candidate],t.L__base,atol=1e-12)
 np.testing.assert_allclose(t['D__'+d.candidate]-t['DS__'+d.candidate],t.L__base,atol=1e-12)
 np.testing.assert_allclose(t['S__'+d.candidate].abs().sum(),t['DS__'+d.candidate].abs().sum(),atol=1e-10)
 np.testing.assert_allclose(t['Q__'+d.candidate]-t.L__base,t.S__base*d.q_baseline,atol=1e-12)
books=0
for scenario,lag in [('close_3bps',1),('close_10bps',1),('second_close_3bps',2)]:
 b=pd.read_csv(run/f'outputs/ledgers_{scenario}.csv.gz',index_col=['strategy','date'],parse_dates=['date'])
 assert set(b.index.get_level_values(0))==set(t.columns)
 for name in t:
  np.testing.assert_allclose(b.loc[name].decision_signal,t[name].shift(lag,fill_value=0),atol=1e-12)
  books+=1
 assert len(pd.read_csv(run/f'outputs/baseline_episodes_{scenario}.csv'))==48
metrics=pd.read_csv(run/'outputs/metrics.csv');assert len(metrics.query("window=='full'"))==96
assert not metrics.duplicated(['scenario','strategy','window']).any()
assert json.loads((run/'outputs/verification.json').read_text())['new_ledgers']==48
verification={'frozen_inputs_and_original_computed_artifacts_unchanged':True,'prereg_unchanged':True,
              'source_receipts_verified':len(receipts),'all_books_execution_lags_verified':books,
              'all_q_r_references_and_opposite_legs_verified':True,'initial_and_corrected_code_archived':True,
              'only_postcompute_change_is_diagnostic_counter':True,'new_inventory_regression_test_passed':True,
              'four_existing_state_rule_tests_reused_unchanged_code':True,'ruff_F_E9_passed':True,
              'registry_validate_and_render_check_passed':True,'git_diff_check_passed':True}
(run/'outputs/completion_verification.json').write_text(json.dumps(verification,indent=2))
shutil.copyfile('/tmp/verify_exit_audit_completion.py',run/'inputs/verify_completion.py')
m.update(status='complete',outcome='source_sensitivity_reduced_but_increment_over_scaled_daily_unconfirmed',
         artifacts=[artifact_record(p,run) for p in sorted(run.rglob('*')) if p.is_file() and p.name!='manifest.json'])
write_manifest(run,m)
for rec in m['artifacts']:
 assert artifact_record(run/rec['path'],run)==rec
print(json.dumps({'status':m['status'],'artifacts':len(m['artifacts']),'books_verified':books,'new_books':48,'reused_books':48}))
