from pathlib import Path
import csv, json, shutil, hashlib, subprocess
from datetime import datetime, timezone
import yaml
from backtest.run_manifest import create_run_dir, artifact_record, git_state, write_manifest

ROOT=Path.cwd()
RUN=create_run_dir(ROOT/'backtest/output/runs','20260914-statistical-dependency-audit-r1')
write_manifest(RUN,{'status':'running','stage':'statistical-dependency-audit','created_utc':datetime.now(timezone.utc).isoformat()})
shutil.copyfile(__file__,RUN/'inputs/build_audit.py')
registry=yaml.safe_load((ROOT/'docs/plans/research_registry.yaml').read_text())
studies=registry['studies']
assert len(studies)==56

def dumpcsv(name,rows):
    with (RUN/'outputs'/name).open('w',newline='') as f:
        w=csv.DictWriter(f,fieldnames=list(rows[0]));w.writeheader();w.writerows(rows)

def save_source(path):
    src=ROOT/path; dest=RUN/'inputs/source_before'/path
    dest.parent.mkdir(parents=True,exist_ok=True);shutil.copyfile(src,dest)
    return {'path':path,'sha256':hashlib.sha256(src.read_bytes()).hexdigest(),'size':src.stat().st_size}

sources=['docs/plans/research_registry.yaml','docs/plans/2026-08-12-selection-permutation-machine.md',
'docs/plans/2026-09-03-gate0-incumbent-audit.md','docs/plans/2026-09-03-gate0-criterion-argument.md',
'docs/plans/2026-09-09-gate0-reanalysis-results.md','docs/plans/2026-09-03-money-flow-axis-results.md',
'docs/plans/2026-09-03-index-option-iv-axis-results.md','docs/plans/2026-07-08-b2-industry-neutral-decomposition.md',
'docs/plans/2026-07-08-b1-style-basket-replication.md','docs/plans/2026-07-10-momentum-transform-scan-design.md',
'docs/plans/2026-07-11-external-review-fixes.md','docs/plans/2026-09-09-deploy-symmetric-equal-weight-decision.md',
'docs/plans/2026-09-09-deploy-slope20-fourth-line-decision.md','docs/plans/2026-08-12-probe-3-fusion-slope20.md',
'backtest/significance.py','backtest/baseline.py','backtest/selection_permutation.py','backtest/gate0_reanalysis.py',
'backtest/gate0_incumbent_audit.py','backtest/gate0_criterion_study.py','backtest/pure_style_eval.py',
'backtest/money_flow_axis_probe.py','backtest/basis_term_probe.py','backtest/consensus_axis_probe.py','backtest/new_high_axis_probe.py',
'signals/style_basket/decompose.py','backtest/output/gate0_reanalysis_summary.csv','backtest/output/gate0_incumbent_audit.json',
'backtest/output/money_flow_axis_selection.json','backtest/output/option_axis_selection.json',
'backtest/output/money_flow_axis_probe_verdicts.csv','backtest/output/option_axis_probe_verdicts.csv',
'backtest/output/pure_style_eval.csv','output/style_basket/validation_all.csv','output/style_basket/decomposition_U2.csv',
'backtest/output/runs/20260826T183948-p0-revalidation-ac11b3c/outputs/gate0r_result.json',
'backtest/output/runs/20260914-statistics-r1/outputs/calibration_summary.csv']
source_records=[save_source(p) for p in sources]
# Demo sidecar contains infrastructure metadata: freeze only the research summary.
demo_path=ROOT/'backtest/output/selection_permutation_demo_summary.json'
demo=json.loads(demo_path.read_text())
(RUN/'inputs/demo_research_summary.json').write_text(json.dumps(demo['summary'],indent=2))
source_records.append({'path':str(demo_path.relative_to(ROOT)),'sha256':hashlib.sha256(demo_path.read_bytes()).hexdigest(),'size':demo_path.stat().st_size,'snapshot':'research summary only; infrastructure metadata omitted'})
(RUN/'inputs/source_hashes.json').write_text(json.dumps(source_records,ensure_ascii=False,indent=2))
positives=[s for s in studies if s['outcome'] in {'pass','selected'} or s['status']=='adopted']
assert len(positives)==5
rows=[]
for s in studies:
    category=('risk_preference_adoption' if s['outcome']=='selected' else 'replication_acceptance' if s['outcome']=='pass' else 'no_positive_final_verdict')
    rows.append({'id':s['id'],'status_before':s['status'],'outcome_before':s['outcome'],'triage':category,
                 'review_scope':'deployment or foundation source checked' if s in positives else 'registry screened; no numerical reanalysis',
                 'claim_before':s['claim'],'evidence':' | '.join(s['documents']['evidence'])})
dumpcsv('registry_inventory.csv',rows)

cases=[]
def case(id,subject,method,evidence,finding,action):
    cases.append(dict(id=id,subject=subject,method=method,evidence=evidence,finding=finding,action=action))
case('P1','equal_weight symmetric deployment','risk preference; paired bootstrap descriptive','2026-09-09-deploy-symmetric-equal-weight-decision.md','Deployment explicitly not a statistical superiority verdict.','Retain adoption record; no claim of calibrated alpha or superiority.')
case('P2','slope20 deployment','risk preference; historical return comparison','2026-09-09-deploy-slope20-fourth-line-decision.md','Deployed L20/zw120/sm0; earlier L20/zw40/sm5 p-values concern another variant.','Retain adoption record; history reused, superiority unconfirmed.')
case('P3','equal_weight long-flat','risk preference','research_registry.yaml: production-equal-weight-long-flat','Superseded mapping; not selected on corrected p<0.05.','Retain historical adoption record.')
case('P4','B1 replication','correlation acceptance, no prediction null','output/style_basket/validation_all.csv','Committed validation file exists; U0-U3 daily signal correlations 0.878..0.906. Previous missing-artifact caveat false.','Retain limited replication PASS; repair evidence link. Does not prove all data PIT-correct.')
case('P5','Gate 0R foundation','frozen anchor and drift acceptance','20260826T183948-p0-revalidation-ac11b3c/outputs/gate0r_result.json','pass=true; rho 0.8022/0.7966/0.9698. No min-P/max-T discovery decision.','Retain PASS for this frozen data/specification only.')
case('D1','08-12 equal_weight demo','selection rotation; worst(train,val) Sharpe','selection_permutation_demo_summary.json','p_selected=0.006993; winner lb20/zw100/sm0, not deployed lb20/zw40/sm5.','Withdraw deployed-point selection-proof wording; diagnostic only pending relevant calibration.')
case('D2','09-03 incumbent grid','selection rotation; absolute rank IC','gate0_incumbent_audit.json; gate0_reanalysis_summary.csv','min-P=0.012987 belongs k10 winner; deployed k20 has min-P=0.154845/V1=0.130869.','No calibrated positive inference; deployment or repeated old windows not independent proof.')
case('D3','F1 representative and V3','single representative rotation; later partial-IC grid','money_flow_axis_probe_verdicts.csv; gate0_reanalysis_summary.csv','k20 IC p=.001998/partial p=.002997; V1 min-P=.070929; V3=.039960.','Demote confirmed increment to sample association; retain STOP and full-phase/replication caveats.')
case('D4','F1 grid winner','selection rotation; absolute rank IC','money_flow_axis_selection.json','k40 winner IC=.418836; max-T=.004995/min-P=.019980, distinct from k20 representative.','Diagnostic only; do not transfer winner p to frozen representative.')
case('D5','O2 representative and grid','probe rotation + selection rotation','option_axis_probe_verdicts.csv; option_axis_selection.json','Probe PASS True is partial; representative p=.254745; different winner p=.043956/min-P=.233766.','Retain STOP; no confirmed independent information and no proof of no information.')
case('D6','B2 pure versus mixed IC','point estimates; no paired IC-difference test','signals/style_basket/decompose.py; output/style_basket/decomposition_U2.csv','IC .179 versus .126 are levels; code computes correlations without difference CI or p.','Withdraw significant improvement claim; keep descriptive ranking and no-switch decision.')
case('D7','early slope and baseline/B2 p-values','bootstrap_pvalue is circular position shift, not paired difference','backtest/significance.py; 2026-07-10-momentum-transform-scan-design.md','p<=.002/.004 test each strategy versus shifted itself; no superiority test, no full search calibration.','Historical diagnostics only; current slope uses a different exact specification.')
case('M1','08-12 synthetic calibration','independent stationary/circular null','2026-08-12-selection-permutation-machine.md section 4','Actual independent generated calibration; differs from 09-03 pool rank exercise.','Retain within tested mechanisms; not a guarantee on nonstationary finance/Sharpe grids.')
case('M2','09-03 calibration guarantee','same-pool rank exercise','2026-09-03-gate0-criterion-argument.md','Does not validate source-series exchangeability; independently refuted 09-14.','Withdraw universal min-P default and guarantee; historical algorithm frozen.')
dumpcsv('positive_claims.csv',cases)

pass_rows=[]
for p in sorted((ROOT/'backtest/output').glob('*verdict*.csv')):
    for i,r in enumerate(csv.DictReader(p.open()),1):
        hits=[k for k,v in r.items() if k.lower() in {'pass','overall','verdict','pass_probe'} and str(v).lower() in {'true','pass','go'}]
        if hits:
            gate=r.get('gate',''); subject=r.get('family',r.get('family_group',r.get('pair_set','')))
            kind='partial_probe' if not gate else 'component_gate'
            assert gate.upper()!='OVERALL', (p,r)
            pass_rows.append({'source':str(p.relative_to(ROOT)),'data_row':i,'positive_columns':','.join(hits),'subject':subject,'gate':gate,'metric':r.get('metric',''),'classification':kind})
for path in sorted({r['source'] for r in pass_rows}):
    if path not in sources: save_source(path)
dumpcsv('artifact_pass_rows.csv',pass_rows)
reanalysis=list(csv.DictReader((ROOT/'backtest/output/gate0_reanalysis_summary.csv').open()))
assert len(reanalysis)==17 and not any(float(r['V1_minP'])<.05 for r in reanalysis)
summary={'registry_studies_screened':56,'positive_final_records':5,'risk_preference_adoption_records':3,'replication_acceptance_records':2,
         'reviewed_claim_cases':len(cases),'positive_component_rows':len(pass_rows),'reanalysis_representatives':17,'V1_minP_below_005':0,
         'V3_minP_below_005':[{'line':r['line'],'family':r['family'],'p':float(r['V3_minP'])} for r in reanalysis if float(r['V3_minP'])<.05],
         'scope':'Registry and top-level verdict/selection artifacts plus cited positive prose; no rerun of historical p-values, no estimate of actual false discoveries.',
         'limits':['No complete historical search log reconstructed.','No independently verified live trading track record audited.','Hybrid20/citic40d original adoption evidence not separately represented among these five positive registry records.']}
(RUN/'outputs/summary.json').write_text(json.dumps(summary,ensure_ascii=False,indent=2))
guard=['backtest/production.py','signals/equal_weight/generate_signal.py','signals/slope20/generate_signal.py','dashboard/data.py']
guard+= [str(p.relative_to(ROOT)) for p in sorted((ROOT/'output/recommended').glob('*.csv'))]
(RUN/'inputs/production_guard.json').write_text(json.dumps({p:hashlib.sha256((ROOT/p).read_bytes()).hexdigest() for p in guard},indent=2))
print(json.dumps(summary,ensure_ascii=False,indent=2))
print(RUN)
