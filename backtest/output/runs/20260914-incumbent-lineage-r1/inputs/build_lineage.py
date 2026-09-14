from pathlib import Path
import subprocess,csv,io,json,shutil,hashlib
from datetime import datetime,timezone
import pandas as pd
from backtest.run_manifest import create_run_dir,write_manifest,git_state
ROOT=Path.cwd();RUN=create_run_dir(ROOT/'backtest/output/runs','20260914-incumbent-lineage-r1')
write_manifest(RUN,{'status':'running','stage':'incumbent-lineage-and-forward-preparation','created_utc':datetime.now(timezone.utc).isoformat(),'git':git_state(ROOT)})
shutil.copyfile(__file__,RUN/'inputs/build_lineage.py')
def git(*args):return subprocess.check_output(['git',*args],cwd=ROOT)
def snapshot(commit,path):
 raw=git('show',commit+':'+path);p=RUN/'inputs/git'/commit/path;p.parent.mkdir(parents=True,exist_ok=True);p.write_bytes(raw);return raw
objects=[('6b07639','latest_equal_weight_signal/generate_equal_weight_signal_contrast.py'),('6b07639','latest_equal_weight_signal/style_factor_groups.csv'),
('6b07639','latest_equal_weight_signal/smooth_vs_raw_grid_optimization_0621_next_close_summary.csv'),
('a2e19f0','signals/equal_weight/generate_signal.py'),('a2e19f0','signals/equal_weight/config_6pairs.csv'),
('dcb291f','signals/equal_weight/config_4pairs.csv'),('3b05c55','backtest/scan.py'),('cfd9d70','backtest/output/scan_equal_weight.csv'),
('f7ad5b1','backtest/momentum_scan.py'),('f7ad5b1','backtest/output/momentum_head2head.csv'),
('9523365','backtest/production.py'),('fb96394','signals/slope20/generate_signal.py'),('063c322','backtest/production.py')]
for commit,path in objects:snapshot(commit,path)
rows=[]
for commit in ['4cfe03a','0a91aed','98f22f2','f7ad5b1']:
 raw=snapshot(commit,'backtest/output/scan_momentum.csv');df=pd.read_csv(io.BytesIO(raw));slope=df[df.family=='slope']
 for label,cols in [('old_three',['sharpe_train_14_20','sharpe_val_21_23','sharpe_holdout_24_26']),('new_two',['sharpe_train_14_20','sharpe_val_21_23'])]:
  r=slope.loc[slope[cols].min(axis=1).idxmax()]
  rows.append({'commit':commit,'rows':len(df),'slope_rows':len(slope),'selection_rule':label,
   'length':int(r.length),'skip':int(r.skip),'z_window':int(r.z_window),'smoothing':int(r.smoothing),'score':float(r[cols].min()),'scan_blob':git('rev-parse',commit+':backtest/output/scan_momentum.csv').decode().strip()})
with (RUN/'outputs/selection_replay.csv').open('w',newline='') as f:
 w=csv.DictWriter(f,fieldnames=list(rows[0]));w.writeheader();w.writerows(rows)
assert {(r['z_window'],r['smoothing']) for r in rows if r['selection_rule']=='old_three'}=={(40,5)}
assert {(r['z_window'],r['smoothing']) for r in rows if r['selection_rule']=='new_two'}=={(120,0)}
assert rows[-1]['scan_blob']==rows[-3]['scan_blob']
commits=['6b07639','a2e19f0','dcb291f','3b05c55','cfd9d70','213f807','4cfe03a','0a91aed','98f22f2','f7ad5b1','9523365','fb96394','063c322','f56c05c','2d4fa9c']
metadata=[]
for commit in commits:
 full,authored,committed,subject=git('show','-s','--format=%H%n%aI%n%cI%n%s',commit).decode().strip().split('\n',3)
 metadata.append({'commit':full,'authored_at':authored,'committed_at':committed,'subject':subject})
(RUN/'outputs/commit_timeline.json').write_text(json.dumps(metadata,ensure_ascii=False,indent=2))
old=pd.read_csv(io.BytesIO(snapshot('6b07639','latest_equal_weight_signal/smooth_vs_raw_grid_optimization_0621_next_close_summary.csv')))
summary={'first_git_snapshot':'6b07639','first_git_snapshot_date':'2026-07-02','pre_snapshot_search_present':True,
 'pre_snapshot_optimization_sample_start':str(old.start_date.min()),'pre_snapshot_optimization_sample_end':str(old.end_date.max()),
 'pre_snapshot_signal_variants':sorted(old.signal_column.unique().tolist()),'initial_snapshot_pairs':len(pd.read_csv(io.BytesIO(snapshot('6b07639','latest_equal_weight_signal/style_factor_groups.csv')))),
 'parameterization_pairs':len(pd.read_csv(io.BytesIO(snapshot('a2e19f0','signals/equal_weight/config_6pairs.csv')))),
 'current_pairs':len(pd.read_csv('signals/equal_weight/config_4pairs.csv')),
 'momentum_scan_rows':[84,120,174],'slope_current_spec_reproduced':True,'scan_blob_unchanged_at_carry_fix':True,
 'unknowns':['Before-first-snapshot selection chronology and number of tries','Whether uncommitted scans were run during July 11 repair','Independent real-money execution and point-in-time records'],
 'historical_boundary':'All dates through 2026-09-14 excluded from prospective observation; prior work has evaluated returns through 2026-09-11.'}
(RUN/'outputs/lineage_summary.json').write_text(json.dumps(summary,ensure_ascii=False,indent=2))
current=['signals/equal_weight/generate_signal.py','signals/equal_weight/config_4pairs.csv','signals/slope20/generate_signal.py','backtest/momentum_scan.py',
 'backtest/scan.py','backtest/positions.py','backtest/production.py','backtest/execution_ledger.py','backtest/forward_observation.py',
 'signals/common/data_source.py','signals/common/factors.py','tests/test_forward_observation.py',
 'docs/superpowers/plans/2026-09-14-incumbent-lineage-forward-plan.md']
for rel in current:
 dest=RUN/'inputs/current'/rel;dest.parent.mkdir(parents=True,exist_ok=True);shutil.copyfile(ROOT/rel,dest)
guards=['backtest/production.py','signals/equal_weight/generate_signal.py','signals/slope20/generate_signal.py']
guards += [str(p.relative_to(ROOT)) for p in sorted((ROOT/'output/recommended').glob('*.csv'))]
(RUN/'inputs/production_guard.json').write_text(json.dumps({p:hashlib.sha256((ROOT/p).read_bytes()).hexdigest() for p in guards},indent=2))
print(json.dumps(summary,ensure_ascii=False,indent=2))
print(RUN)
