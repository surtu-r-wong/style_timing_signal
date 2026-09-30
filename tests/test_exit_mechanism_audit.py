import pandas as pd

from backtest.flow_structure_followup import short_modes
from backtest.run_exit_mechanism_audit import decision_inventory


def test_decision_inventory_distinguishes_forced_entry_and_reentry_rejection():
    index = pd.date_range('2020-01-01', periods=8)
    base = pd.Series([-1., -1., -1., -1., 0., -1., -1., -1.], index=index)
    score = pd.Series([-1., 1., -1., 1., 1., 0., -1., 1.], index=index)
    labels, episodes = decision_inventory(base, score, short_modes(base, score), 'test')
    assert labels.exit_only.tolist() == ['forced_entry', 'same', 'same', 'suppressed_reentry',
                                       'same', 'same', 'same', 'suppressed_reentry']
    assert labels.entry_exit.tolist() == ['same', 'entry_rejected', 'same', 'entry_rejected',
                                        'same', 'same', 'same', 'suppressed_reentry']
    first = episodes.query("mode=='exit_only' and decision_episode==0").iloc[0]
    assert first.left_censored and not first.right_censored
    assert first.forced_entry_days == 1 and first.blocked_daily_entry_count == 1
    assert first.daily_reentries == 1
    assert first.daily_entries_after_baseline_start == 2
    assert first.first_flat_after_entry_or_rejection == '2020-01-03'
    last = episodes.query("mode=='entry_exit' and decision_episode==1").iloc[0]
    assert last.right_censored and last.entry_gate
