import numpy as np
import pandas as pd
import pytest
from backtest.research_statistics import batch_rank_ic, generated_null, holm_adjust
from backtest.rotation_probe import nonoverlap_ic


def test_batched_stat_is_actual_nonoverlap_rank_ic():
    rng=np.random.default_rng(3); n=161
    s=rng.normal(size=n); r=rng.normal(size=n)
    idx=np.stack([np.arange(n),np.roll(np.arange(n),23)])
    actual=batch_rank_ic(s,r,20,idx)
    dates=pd.date_range('2000-01-01',periods=n)
    expected=[abs(nonoverlap_ic(pd.Series(s[ii],index=dates),pd.Series(r,index=dates),20)[0]) for ii in idx]
    assert actual==pytest.approx(expected)


def test_holm_controls_family_and_preserves_input_order():
    assert holm_adjust([.04,.001,.02])==pytest.approx([.04,.003,.04])


def test_generated_null_is_deterministic_with_independent_target_noise():
    x,y=generated_null('shared_volatility',512,31)
    xx,yy=generated_null('shared_volatility',512,31)
    assert np.array_equal(x,xx) and np.array_equal(y,yy)
    assert abs(np.corrcoef(x,y)[0,1])<.2
    assert x[:128].std()>2*x[-128:].std()
