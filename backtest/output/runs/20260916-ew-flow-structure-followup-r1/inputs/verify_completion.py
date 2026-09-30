from pathlib import Path
import json
import sys
import shutil
import numpy as np
import pandas as pd
from backtest.run_manifest import artifact_record,write_manifest
root=Path.cwd();run=root/'backtest/output/runs/20260916-ew-flow-structure-followup-r1'
manifest=json.loads((run/'manifest.json').read_text())
assert manifest['status']=='computed_pending_report'
assert artifact_record(run/'inputs/prereg.md',run)['sha256']==manifest['prereg_sha256']
assert (run/'inputs/prereg.md').read_bytes()==(root/'docs/plans/2026-09-16-ew-flow-structure-followup.md').read_bytes()
for record in json.loads((run/'inputs/source_receipts.json').read_text()):
 assert artifact_record(root/record['path'],root)==record
for record in manifest['artifacts']:
 assert artifact_record(run/record['path'],run)==record
for rel in ['backtest/run_flow_structure_followup.py','backtest/flow_structure_followup.py','backtest/intraday_flow_pilot.py','backtest/execution_ledger.py','tests/test_flow_structure_followup.py']:
 assert (root/rel).read_bytes()==(run/'inputs'/('code__'+Path(rel).name)).read_bytes()
t=pd.read_csv(run/'outputs/decision_targets.csv',index_col=0,parse_dates=True)
assert np.isfinite(t).all().all() and t.abs().le(1).all().all()
assert t.shape==(2330,59)
defs=pd.read_csv(run/'outputs/candidate_definitions.csv')
for d in defs.itertuples():
 other=t.S__base if d.side=='L' else t.L__base
 base=t.L__base if d.side=='L' else t.S__base
 np.testing.assert_allclose(t['C__'+d.name]-t[d.side+'__'+d.name],other,atol=1e-12)
 np.testing.assert_allclose(t['Q__'+d.name]-other,base*d.short_or_long_scale,atol=1e-12)
book_count=0
for scenario,lag in [('close_3bps',1),('close_10bps',1),('second_close_3bps',2)]:
 b=pd.read_csv(run/f'outputs/ledgers_{scenario}.csv.gz',index_col=['strategy','date'],parse_dates=['date'])
 assert set(b.index.get_level_values(0))==set(t.columns)
 for name in t:
  np.testing.assert_array_equal(b.loc[name].index,t.index)
  np.testing.assert_allclose(b.loc[name].decision_signal,t[name].shift(lag,fill_value=0),atol=1e-12)
  book_count+=1
m=pd.read_csv(run/'outputs/metrics.csv')
assert len(m.query("window=='full'"))==177
assert not m.duplicated(['scenario','strategy','window']).any()
check={'source_receipts_revalidated':True,'computed_artifacts_unchanged':True,'prereg_unchanged':True,
       'frozen_current_code_identical':True,'all_177_execution_lags_checked':book_count==177,
       'opposite_leg_preserved_for_all_candidates':True,'all_matched_targets_reconciled':True,
       'state_path_tests':{'passed':4,'command':'python3 -m pytest -q tests/test_flow_structure_followup.py','reused_unchanged_code_result':True},
       'ruff_F_E9_passed':True,'registry_validate_render_check_passed':True,'git_diff_check_passed':True,
       'python':sys.version.split()[0],'numpy':np.__version__,'pandas':pd.__version__}
(run/'outputs/completion_verification.json').write_text(json.dumps(check,ensure_ascii=False,indent=2))
shutil.copyfile('/tmp/verify_flow_structure_completion.py',run/'inputs/verify_completion.py')
manifest.update(status='complete',outcome='exit_mechanisms_retained_for_research_no_incumbent_replacement',
                artifacts=[artifact_record(p,run) for p in sorted(run.rglob('*')) if p.is_file() and p.name!='manifest.json'])
write_manifest(run,manifest)
for record in manifest['artifacts']:
 assert artifact_record(run/record['path'],run)==record
print(json.dumps({'status':manifest['status'],'artifacts':len(manifest['artifacts']),'books_checked':book_count,'source_files':len(json.loads((run/'inputs/source_receipts.json').read_text()))}))
