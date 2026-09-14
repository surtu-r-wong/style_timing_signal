"""Read-only readiness inventory; never reads account positions/transactions."""
from __future__ import annotations
import argparse
import json
from backtest.data import _connect
from backtest.execution_audit import begin, finish
from signals.common.config import load_db_config


def inventory():
    c=_connect(load_db_config()); out={}
    try:
        c.set_session(readonly=True,isolation_level='REPEATABLE READ')
        with c.cursor() as q:
            q.execute("SET LOCAL statement_timeout='30s'")
            q.execute('SELECT transaction_timestamp()::text');out['observed_at']=q.fetchone()[0]
            q.execute("SELECT table_name FROM information_schema.tables WHERE table_schema='stock_selector' AND (table_name ILIKE '%fund%' OR table_name ILIKE '%etf%' OR table_name ILIKE '%share%' OR table_name ILIKE '%member%' OR table_name ILIKE '%rank%' OR table_name ILIKE '%money%') ORDER BY 1")
            out['research_tables']=[r[0] for r in q.fetchall()]
            q.execute("SELECT index_code,min(trade_date),max(trade_date),count(*),count(open_main_inflow_money),count(end_main_inflow_money),count(main_inflow_count),count(main_outflow_count),count(extra_bill_inflow_money),count(large_bill_inflow_money),count(DISTINCT src),min(fetched_at),max(fetched_at) FROM stock_selector.index_money_flow GROUP BY 1 ORDER BY 1")
            names=['index_code','first','last','rows','open_nonnull','end_nonnull','in_count_nonnull','out_count_nonnull','extra_nonnull','large_nonnull','n_sources','first_fetch','last_fetch']
            out['money_flow']=[dict(zip(names,r)) for r in q.fetchall()]
            q.execute("SELECT min(basket_date),max(basket_date),count(*),count(DISTINCT fund_code) FROM stock_selector.etf_constituent")
            out['etf_baskets']=dict(zip(['first','last','rows','n_funds'],q.fetchone()))
            q.execute("SELECT table_schema,table_name,column_name FROM information_schema.columns WHERE (table_schema='stock_selector' AND table_name IN ('index_money_flow','etf_constituent')) OR (table_schema='public' AND table_name ILIKE '%member%') ORDER BY 1,2,ordinal_position")
            out['field_catalog']=[dict(zip(['schema','table','column'],r)) for r in q.fetchall()]
            q.execute("SELECT ts_code,min(trade_date),max(trade_date),count(*),count(open) FILTER (WHERE open>0) FROM stock_selector.stock_daily_price WHERE ts_code IN ('510500.SH','512100.SH','159845.SZ') GROUP BY 1 ORDER BY 1")
            out['illustrative_etf_prices']=[dict(zip(['code','first','last','rows','valid_open'],r)) for r in q.fetchall()]
        c.rollback()
    finally:c.close()
    out['interpretation']={
        'member_positions':'No exchange member-ranking table identified in research schema/public member names. Account position tables intentionally excluded.',
        'etf_creation_redemption':'Basket constituents and basket cash/value are not outstanding fund units or daily net creations. Need dated total units and distribution/split treatment.',
        'money_flow_intraday':'Nonnull daily vendor open/end fields are available; confirm field interval/units and historical revision policy before defining any signal.',
        'pit':'fetched_at is collection time, not proof of contemporaneous historical release; no future-return test performed.',
        'scope':'Local research-data inventory only, no external historical pull or production service change.'}
    return out


def main():
    ap=argparse.ArgumentParser();ap.add_argument('--run-id',required=True);a=ap.parse_args()
    run=begin(a.run_id);out=inventory()
    (run/'outputs/data_inventory.json').write_text(json.dumps(out,ensure_ascii=False,indent=2,default=str))
    print(json.dumps({k:v for k,v in out.items() if k not in ('field_catalog','interpretation')},ensure_ascii=False,indent=2,default=str))
    finish(run,'new_input_inventory')

if __name__=='__main__':main()
