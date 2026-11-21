"""Observed FoodOn failure regressions and identity-boundary controls."""
from unittest.mock import Mock
import pytest
from recipe_wrangler.utils import foodon_matching as fm


def hit(label, record='FOODON_test', distance=.1):
    return {'id': record, 'document': label, 'metadata': {'label': label},
            'distance': distance, 'lexical_rank': 1}


@pytest.mark.parametrize('name,label', [
    ('vanilla paste', 'almond paste'), ('beef mince', 'mince pie'),
    ('pork fillet', 'fish fillet'), ('tomatoes, standard, raw', 'oyster (standard, raw)'),
    ('low-fat milk', 'chocolate milk (low fat)'), ('milk (coconut)', 'milk'),
])
def test_shared_word_and_distance_do_not_prove_identity(monkeypatch, name, label):
    monkeypatch.setattr(fm, 'query_vector_collection', lambda *a: [hit(label)])
    assert fm.match_embedding(name, max_distance=.3) is None


def test_rejected_top_candidate_does_not_hide_supported_candidate(monkeypatch):
    monkeypatch.setattr(fm, 'query_vector_collection', lambda *a: [
        hit('almond paste', 'FOODON_bad', .01), hit('vanilla paste', 'FOODON_good', .12)])
    assert fm.match_embedding('vanilla paste', max_distance=.15) == ('FOODON_good', .12)


@pytest.mark.parametrize('name,label', [('courgettes','zucchini, raw'), ('greek yoghurt','greek yogurt'),
                                        ('olive oil','oil, olive, extra virgin'), ('chicken egg','chicken egg (fried)'),
                                        ('courgettes','zucchini food product')])
def test_existing_identity_synonyms_and_word_order_remain_supported(monkeypatch,name,label):
    monkeypatch.setattr(fm, 'query_vector_collection', lambda *a: [hit(label)])
    assert fm.match_embedding(name,max_distance=.15) == ('FOODON_test',.1)


def test_head_stripping_never_discards_biological_source(monkeypatch):
    query = Mock(return_value=[hit('fish fillet')])
    monkeypatch.setattr(fm, 'query_vector_collection', query)
    assert fm.match_head_noun('pork fillet', {'fillet':'FOODON_fish'}, max_distance=.15) is None
    query.assert_not_called()
    assert fm.match_head_noun('2 large tomatoes', {'tomato':'FOODON_tomato'}, max_distance=.15) == (
        'FOODON_tomato', 'head_noun_exact', 1.0)


def test_parenthetical_identity_cannot_create_false_exact_match():
    assert fm.match_exact('milk (coconut)', {'milk':'FOODON_dairy'}) is None
    assert fm.match_reviewed_alias('firm tofu (egg)') is None
    assert fm.match_exact('tomato (raw)', {'tomato':'FOODON_tomato'}) == 'FOODON_tomato'


def test_label_index_rejects_lossy_and_ambiguous_exact_keys():
    session = Mock()
    session.run.return_value = [
        {'id':'FOODON_coconut','label':'milk (coconut)'},
        {'id':'FOODON_first','label':'tomato'}, {'id':'FOODON_second','label':'tomato'},
        {'id':'FOODON_milk','label':'milk'}, {'id':'FOODON_milk','label':'milk'},
    ]
    assert fm.fetch_label_index(session) == {'milk':'FOODON_milk'}


@pytest.mark.parametrize('name', ['soy yoghurt','cashew milk','pea milk','coconut cream',
                                  'milk (coconut)','hazelnut yogurt'])
def test_plant_dairy_names_cannot_use_reviewed_dairy_family_alias(name):
    assert fm.match_reviewed_alias(name) is None


@pytest.mark.parametrize('name', ['milk or almond milk','sauce without milk','milk-free butter',
                                  'batter with no added milk','cream substitute'])
def test_uncertain_formulation_cannot_become_plain_dairy_family(monkeypatch,name):
    query = Mock(side_effect=AssertionError('No vector lookup for ambiguous formulations'))
    monkeypatch.setattr(fm,'query_vector_collection',query)
    # Explicit free-from wording must at least avoid the broad dairy alias.
    assert fm.match_reviewed_alias(name) is None
    assert fm.match_ingredient_to_foodon(name,{}) is None
    query.assert_not_called()


def test_explicitly_reviewed_gluten_free_alias_remains_available():
    assert fm.match_ingredient_to_foodon('gluten free pad thai noodles', {}) == (
        'FOODON_00005437', 'reviewed_exact_alias', 1.0, False)


def test_head_stripping_can_remove_identity_neutral_preparation_word(monkeypatch):
    monkeypatch.setattr(fm, 'query_vector_collection', lambda *a: [])
    assert fm.match_head_noun('chopped spinach', {'spinach':'FOODON_spinach'}, max_distance=.15) == (
        'FOODON_spinach', 'head_noun_exact', 1.0)


@pytest.mark.parametrize('reverse', [False, True])
def test_unqualified_label_precedes_qualified_collision_in_any_order(reverse):
    rows = [
        {'id':'FOODON_chopped','label':'apple (chopped)'},
        {'id':'FOODON_whole','label':'apple (whole)'},
        {'id':'FOODON_plain','label':'apple'},
    ]
    session = Mock(); session.run.return_value = rows[::-1] if reverse else rows
    assert fm.fetch_label_index(session) == {'apple':'FOODON_plain'}


def test_generic_food_concept_avoids_inventing_a_preparation():
    session = Mock(); session.run.return_value = [
        {'id':'FOODON_raw','label':'garlic (raw)'},
        {'id':'FOODON_cooked','label':'garlic (cooked)'},
        {'id':'FOODON_generic','label':'garlic food product'},
    ]
    assert fm.fetch_label_index(session)['garlic'] == 'FOODON_generic'


@pytest.mark.parametrize('name', ['couscous', 'hummus', 'celery flakes', 'berries'])
@pytest.mark.parametrize('reverse', [False, True])
def test_literal_plural_or_s_ending_label_beats_qualified_collision(name, reverse):
    rows = [
        {'id':'FOODON_prepared', 'label':name + ' (cooked)'},
        {'id':'FOODON_plain', 'label':name},
    ]
    session = Mock(); session.run.return_value = rows[::-1] if reverse else rows
    assert fm.match_exact(name, fm.fetch_label_index(session)) == 'FOODON_plain'


def test_singularized_literal_tie_stays_unresolved_in_every_order():
    from itertools import permutations
    rows = [{'id':'FOODON_a', 'label':'berry'}, {'id':'FOODON_b', 'label':'berries'},
            {'id':'FOODON_b', 'label':'berries'}]
    for order in permutations(rows):
        session = Mock(); session.run.return_value = order
        assert fm.match_exact('berries', fm.fetch_label_index(session)) is None


def test_new_link_writer_records_identity_policy_version_without_deleting():
    session = Mock()
    fm.write_link(session, 'ingredient', 'FOODON_concept', method='embedding', confidence=.9)
    assert session.run.call_args.kwargs['version'] == 'foodon-link-v4'
    assert 'DELETE' not in session.run.call_args.args[0]
