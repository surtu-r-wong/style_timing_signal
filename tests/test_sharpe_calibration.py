import numpy as np
import pandas as pd
import pytest
from backtest.sharpe_calibration import paired_joint_p, combine_p, generate
from backtest.paired_bootstrap import paired_block_bootstrap_sharpe_diff


def test_joint_bootstrap_matches_existing_single_comparison():
    x=generate('iid',80,12)
    actual=paired_joint_p(x,block=20,draws=99,seed=34)
    for j in range(2):
        ref=paired_block_bootstrap_sharpe_diff(pd.Series(x[:,2*j]),pd.Series(x[:,2*j+1]),block=20,n=99,seed=34)
        assert actual[j]==pytest.approx(ref['p_value'])


def test_conservative_rule_takes_max_before_holm():
    adjusted=combine_p(np.array([.001,.02]),np.array([.04,.03]))
    np.testing.assert_allclose(adjusted,[.06,.06])


def test_identical_pairs_cannot_be_significant():
    rng=np.random.default_rng(3)
    a=rng.normal(size=100)
    x=np.column_stack([a,a,a,a])
    np.testing.assert_equal(paired_joint_p(x,20,49,5),[1.,1.])


def test_alternative_changes_only_first_strategy_mean():
    for kind in ('iid','cluster_t5','ar03','slow_regime','vol_break'):
        a=generate(kind,504,17)
        b=generate(kind,504,17,delta=.4)
        np.testing.assert_allclose(b[:,0]-a[:,0],.4*.01/np.sqrt(245))
        np.testing.assert_equal(a[:,1:],b[:,1:])
        assert np.isfinite(a).all()
