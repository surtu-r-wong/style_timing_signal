import numpy as np
import pandas as pd
from scipy.stats import ttest_1samp
from backtest.robust_sharpe_inference import group_test, influence, self_normalized_test, generate, choose_horizon


def test_group_t_matches_scipy_on_group_estimates():
    x=generate('iid',504,14)
    p,effects=group_test(x,4)
    groups=np.stack([g.mean(axis=0)/g.std(axis=0,ddof=1)*np.sqrt(245) for g in np.array_split(x,4)])
    differences=groups[:,::2]-groups[:,1::2]
    np.testing.assert_allclose(p,ttest_1samp(differences,0,axis=0).pvalue)
    np.testing.assert_allclose(effects,differences.mean(axis=0))


def test_influence_matches_weighted_empirical_contamination_derivative():
    x=generate('iid',504,15)
    _,psi=influence(x)
    j=33; eps=1e-7
    w=np.full(len(x),(1-eps)/len(x));w[j]+=eps
    mu=x.mean(axis=0); v=((x-mu)**2).mean(axis=0)
    mu1=(w[:,None]*x).sum(axis=0);v1=(w[:,None]*(x-mu1)**2).sum(axis=0)
    derivative=((mu1/np.sqrt(v1)-mu/np.sqrt(v))/eps*np.sqrt(245)).reshape(2,2)
    np.testing.assert_allclose(psi[j],derivative[:,0]-derivative[:,1],rtol=2e-5,atol=1e-5)


def test_methods_invariant_under_positive_rescaling():
    x=generate('slow_regime',504,3)
    reference=np.linspace(.01,40,1000)
    np.testing.assert_allclose(group_test(x,4)[0],group_test(x*np.array([2,3,4,5]),4)[0])
    np.testing.assert_equal(self_normalized_test(x,reference)[0],self_normalized_test(x*9,reference)[0])


def test_identical_pairs_have_self_normalized_p_one():
    x=generate('iid',504,1);x[:,1]=x[:,0];x[:,3]=x[:,2]
    p,effect=self_normalized_test(x,np.linspace(.01,30,1000))
    np.testing.assert_equal(p,[1.,1.]);np.testing.assert_equal(effect,[0.,0.])


def test_confirmation_choice_requires_all_scenarios_and_primary_method():
    rows=[]
    for n in (504,2016,8064):
        for kind in ('iid','slow_regime'):
            rows.append({'method':'group_t4','n_sessions':n,'kind':kind,'delta':0.,'upper95':.08 if n==504 and kind=='slow_regime' else .07})
    selected=choose_horizon(pd.DataFrame(rows))
    assert selected=={'n_sessions':2016,'screen_size_passed':True,'method':'group_t4'}
    d=pd.DataFrame(rows);d['upper95']=.2
    assert choose_horizon(d)=={'n_sessions':8064,'screen_size_passed':False,'method':'group_t4'}


def test_alternative_preserves_controls_and_new_slow_state_supported():
    for kind in ('iid','cluster_t5','ar03','slow_regime','slower_regime','vol_break'):
        a=generate(kind,504,91);b=generate(kind,504,91,.4)
        np.testing.assert_array_equal(a[:,1:],b[:,1:])
        np.testing.assert_allclose(b[:,0]-a[:,0],.004/np.sqrt(245))
