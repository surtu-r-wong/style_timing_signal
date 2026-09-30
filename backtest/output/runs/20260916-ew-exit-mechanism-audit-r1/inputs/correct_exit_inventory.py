from pathlib import Path
import json
import shutil
import numpy as np
import pandas as pd
from backtest.flow_structure_followup import short_modes
from backtest.run_exit_mechanism_audit import decision_inventory
from backtest.run_manifest import artifact_record
root=Path.cwd();run=root/'backtest/output/runs/20260916-ew-exit-mechanism-audit-r1'
assert json.loads((run/'manifest.json').read_text())['status']=='computed_pending_report'
t=pd.read_csv(run/'inputs/decision_targets.csv',index_col=0,parse_dates=True)
rows=[]
for source,filename in [('new','scores.csv'),('old','wset_continued_scores.csv')]:
 score=pd.read_csv(run/'inputs'/filename,index_col=0,parse_dates=True).CSI300.reindex(t.index)
 labels,inventory=decision_inventory(t.S__base,score,short_modes(t.S__base,score),source)
 rows.append(inventory)
new=pd.concat(rows,ignore_index=True)
old=pd.read_csv(run/'outputs/decision_episode_inventory.csv')
keep=[c for c in old.columns if c!='daily_reentries']
pd.testing.assert_frame_equal(old[keep],new[keep],check_dtype=False)
np.testing.assert_array_equal(old.daily_reentries,new.daily_entries_after_baseline_start)
new.to_csv(run/'outputs/decision_episode_inventory_corrected.csv',index=False)
for rel in ['backtest/run_exit_mechanism_audit.py','tests/test_exit_mechanism_audit.py']:
 shutil.copyfile(root/rel,run/'inputs'/('corrected__'+Path(rel).name))
note={'scope':'Diagnostic inventory counter only; targets, ledgers, returns and all other attribution fields unchanged.',
      'original_daily_reentries_meaning':'entries after baseline episode start, including a delayed first entry',
      'corrected_daily_reentries_meaning':'max(total actual daily entries minus one, zero)',
      'added_column':'daily_entries_after_baseline_start preserves original count',
      'original_artifact':artifact_record(run/'outputs/decision_episode_inventory.csv',run),
      'corrected_artifact':artifact_record(run/'outputs/decision_episode_inventory_corrected.csv',run),
      'all_other_inventory_fields_identical':True,'regression_test_passed':True}
(run/'outputs/inventory_counter_correction.json').write_text(json.dumps(note,ensure_ascii=False,indent=2))
shutil.copyfile('/tmp/correct_exit_inventory.py',run/'inputs/correct_exit_inventory.py')
print('counter semantics corrected, all other fields exact')
