from unittest.mock import Mock, MagicMock, patch
import pytest

from recipe_wrangler.matching.quality import finalize_profile
from recipe_wrangler.repositories.matching_store import persist_profile_record


def test_source_saturated_fat_above_total_fat_requires_review_without_rewriting_values():
    p=profile()
    p['details'][0]['values_per_100g']={'fat_g':2.1,'saturated_fat_g':3.0}
    result=finalize_profile(p)
    assert result['nutrition_quality']['status']=='needs_validation'
    assert 'source:saturated_fat_exceeds_total_fat:position=0' in result['nutrition_quality']['diagnostic_flags']
    assert result['details'][0]['values_per_100g']=={'fat_g':2.1,'saturated_fat_g':3.0}
    assert result['nutri_score'] is None


def profile(energy=200):
    return {'serves':2,'details':[{'ingredient':'chicken','weight_g':200,'position':0,
        'quantity_status':'retained_input_quantity_unverified'}],
        'clean_totals':{'energy_kcal':energy,'protein_g':40,'sugar_g':1,
            'saturated_fat_g':2,'sodium_mg':100,'fibre_g':0},
        'clean_totals_per_serving':{'energy_kcal':energy/2},
        'supported_partial_totals_per_serving':{'energy_kcal':energy/2}}


def test_complete_score_uses_corrected_mass_and_is_explicitly_estimated():
    result=finalize_profile(profile())
    assert result['nutri_score_breakdown']['inputs']['total_weight_g']==200
    assert result['nutrition_quality']['unverified_quantity_positions']==[0]
    assert result['nutrition_quality']['independently_validated'] is False
    assert result['profiling_quality']['matching_effective_total_weight_g']==200


def test_incomplete_totals_are_not_zero_filled_or_given_a_score():
    p=profile();p['clean_totals']['protein_g']=None
    result=finalize_profile(p)
    assert result['clean_totals']['protein_g'] is None
    assert result['nutrition_quality']['status']=='incomplete'
    assert result['nutri_score'] is None


def test_high_calorie_warning_does_not_prevent_an_input_score():
    result=finalize_profile(profile(2400))
    assert result['nutri_score_breakdown'] is not None
    assert result['nutrition_quality']['status']=='needs_validation'
    assert 'complete:energy_at_least_1000_kcal_per_serving' in result['nutrition_quality']['diagnostic_flags']
    assert result['nutrition_quality']['score_blockers']==[]
    assert result['nutrition_quality']['independently_validated'] is False


def test_extreme_partial_lower_bound_is_flagged_without_clipping():
    p=profile(7000);p['clean_totals']['energy_kcal']=None
    p['clean_totals_per_serving']['energy_kcal']=None
    result=finalize_profile(p)
    assert result['supported_partial_totals_per_serving']['energy_kcal']==3500
    assert result['nutri_score'] is None
    assert result['nutrition_quality']['status']=='needs_validation'
    assert result['nutrition_quality']['diagnostic_flags']==[
        'partial_lower_bound:energy_at_least_3000_kcal_per_serving']


def test_saved_production_profile_activates_and_enqueues_in_same_transaction():
    c=Mock()
    c.execute.return_value.scalar.side_effect=[None,True]
    persist_profile_record(c,{'recipe_id':'r','nutrition_source':'eu',
        'nutrition_profiling_details':[{'ingredient':'milk','weight_g':100,
            'catalogue_release':'release','mechanism_version':'v'}]})
    sql=[str(call.args[0]) for call in c.execute.call_args_list]
    assert any('INSERT INTO public.matching_active_profiles' in q for q in sql)
    assert any('INSERT INTO matching_projection_jobs' in q for q in sql)


def test_unavailable_per_serving_never_falls_back_to_whole_batch():
    from recipe_wrangler.catalog.nutrition import per_serving
    assert per_serving({'total_nutrients_per_serving':None,
        'total_nutrients':{'energy_kcal':5000}}) is None


def test_flagged_active_profile_clears_previous_nutrition_claims():
    from recipe_wrangler.matching import claims
    engine=MagicMock();engine.connect.return_value.execution_options.return_value=engine.connect.return_value
    c=engine.connect.return_value.__enter__.return_value
    c.execute.return_value.mappings.return_value.partitions.return_value=[[
        {'recipe_id':'r','profile':finalize_profile(profile(7000))}]]
    with patch.object(claims,'get_engine',return_value=engine),patch.object(claims,'run_query') as write:
        assert claims.refresh_claims(['r'])==1
        assert write.call_args.args[1]['rows']==[{'id':'r','claims':[]}]


def test_projection_failure_remains_pending_for_retry():
    from recipe_wrangler.matching import projection_worker as worker
    engine=MagicMock();lock=engine.connect.return_value.__enter__.return_value
    lock.execute.return_value.scalar.return_value=True
    lock.execute.return_value.mappings.return_value=[{'recipe_id':'r','generation':7}]
    c=engine.begin.return_value.__enter__.return_value
    with patch.object(worker,'get_engine',return_value=engine),\
         patch.object(worker.store,'deployment',return_value={'mode':'new','release_id':'release'}),\
         patch.object(worker,'reconcile_new_recipes'),\
         patch.object(worker,'run_query',side_effect=RuntimeError('temporary graph outage')):
        assert worker.drain_once()==1
    sql=[str(call.args[0]) for call in c.execute.call_args_list]
    assert any('last_error=:error' in q for q in sql)
    assert not any('completed_generation=GREATEST' in q for q in sql)


def test_rollback_refuses_to_overwrite_profiles_saved_after_activation():
    from scripts.composition import promote_matching_production as promotion
    engine=MagicMock();c=engine.begin.return_value.__enter__.return_value
    c.execute.return_value.mappings.return_value.one.return_value={'status':'active'}
    c.execute.return_value.scalar.return_value=1
    with patch.object(promotion,'get_engine',return_value=engine),\
         pytest.raises(ValueError,match='Profiles changed after activation'):
        promotion.rollback('promotion')
    assert not any('UPDATE "nutrients-recipe-profiles"' in str(call.args[0])
                   for call in c.execute.call_args_list)


@pytest.mark.parametrize('fail_refresh', [False, True])
def test_worker_refreshes_suitability_before_projection_and_leaves_failures_pending(fail_refresh):
    from recipe_wrangler.matching import projection_worker as worker
    from recipe_wrangler.matching import graph_sync, claims
    from recipe_wrangler.catalog import projection
    from recipe_wrangler.utils import dietary_classification, recipe_cache
    engine = MagicMock()
    lock = engine.connect.return_value.__enter__.return_value
    lock.execute.return_value.scalar.return_value = True
    lock.execute.return_value.mappings.return_value = [{'recipe_id':'r', 'generation':7}]
    writes = engine.begin.return_value.__enter__.return_value
    order = []

    def refresh(ids):
        assert ids == ['r']
        order.append('suitability')
        if fail_refresh:
            raise RuntimeError('suitability unavailable')

    with patch.object(worker, 'get_engine', return_value=engine), \
         patch.object(worker.store, 'deployment', return_value={'mode':'new', 'release_id':'release'}), \
         patch.object(worker, 'run_query', return_value=[{'revision':'revision'}]), \
         patch.object(graph_sync, 'bridge', return_value={'projected_runs':1}), \
         patch.object(claims, 'refresh_claims'), \
         patch.object(dietary_classification, 'refresh_recipe_suitability', side_effect=refresh), \
         patch.object(projection, 'project', side_effect=lambda rid:order.append('project')), \
         patch.object(recipe_cache, 'cache_delete', side_effect=lambda rid, **kwargs:order.append('cache')):
        assert worker.drain_once(['r']) == 1
    sql = [str(call.args[0]) for call in writes.execute.call_args_list]
    if fail_refresh:
        assert order == ['suitability']
        assert any('last_error=:error' in q for q in sql)
        assert not any('completed_generation=GREATEST' in q for q in sql)
    else:
        assert order == ['suitability', 'project', 'cache']
        assert any('completed_generation=GREATEST' in q for q in sql)


@pytest.mark.parametrize('deployed,allowed', [({'mode':'new','release_id':'r'}, True),
                                              ({'mode':'new','release_id':'other'}, False)])
def test_repromotion_within_release_rehearses_without_commit(deployed, allowed):
    from scripts.composition import promote_matching_production as promotion
    engine=MagicMock();c=engine.begin.return_value.__enter__.return_value
    c.execute.return_value.mappings.return_value.one.return_value={'status':'prepared','release_id':'r'}
    c.execute.return_value.scalar.return_value=4;c.execute.return_value.rowcount=4
    with patch.object(promotion,'get_engine',return_value=engine),\
         patch.object(promotion.store,'deployment',return_value=deployed):
        if not allowed:
            with pytest.raises(ValueError,match='same release'):promotion.promote('p',rehearse=True)
            return
        result=promotion.promote('p',rehearse=True)
    assert result['rehearsal'] and not result['committed'] and result['activated_profiles']==4
    # The rehearsal exception leaves the transaction context, so the outer begin() block rolls back.
    exit_args=engine.begin.return_value.__exit__.call_args.args
    assert exit_args[0] is promotion._Rehearsal
