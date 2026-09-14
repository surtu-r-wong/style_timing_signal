"""Re-run the corrected short-leg audit from frozen market inputs, preserving old evidence."""
from __future__ import annotations
import argparse
from pathlib import Path
import shutil

import pandas as pd
from backtest.execution_audit import ROOT,begin,finish,copy_snapshot,inputs
from backtest.data import annualized_basis
from backtest import short_leg_audit as audit


def main():
    ap=argparse.ArgumentParser();ap.add_argument('--run-id',required=True);ap.add_argument('--snapshot-from',type=Path,required=True)
    a=ap.parse_args();run=begin(a.run_id);copy_snapshot(a.snapshot_from,run)
    spot,fut,_,_,_,_,und,blend=inputs(run)
    close=spot.pivot(index='date',columns='symbol',values='close')
    carry={'blend':blend}
    for name,prefix,code in [('500','IC','000905.SH'),('1000','IM','000852.SH')]:
        f=fut[fut.symbol.str.startswith(prefix)].sort_values(['date','symbol'])
        main=f.loc[f.groupby('date').oi.idxmax()]
        carry[name]=pd.Series({r.date:annualized_basis(r.close,close.at[r.date,code],r.date.date(),r.symbol)
                               for r in main.itertuples()})
    cache=run/'inputs/cache/backtest/output';cache.mkdir(parents=True)
    for name in ('money_flow_series.csv','basis_term_series.csv','consensus_revision_series.csv','option_iv_IO.csv'):
        shutil.copyfile(ROOT/'backtest/output'/name,cache/name)
    ew=pd.read_csv(run/'inputs/equal_weight.csv',index_col='date',parse_dates=True).factor_value
    audit.ROOT=run/'inputs/cache';audit.OUT=run/'outputs/short_leg_audit_corrected.json'
    audit.load_underlying_returns=lambda kj:und
    audit.load_carry=lambda kj:carry[kj]
    audit._load_ew_signal=lambda:ew
    audit.main()
    shutil.copyfile(Path(__file__),run/'inputs/audit_corrections.py')
    finish(run,'audit_corrections')

if __name__=='__main__':main()
