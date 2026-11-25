"""Live sustainability adaptation: the user's allergens and diet, the stored-row shape, and the GET routing."""
from types import SimpleNamespace as NS

import pytest

pytest.importorskip('recipe_wrangler.services.adaptation.nutrition_adaptation', reason='needs the nutrition adaptation modules')
from recipe_wrangler.services.adaptation import sustainability_adaptation as sa
from recipe_wrangler.services.adaptation import sustainability_live as live

@pytest.fixture(autouse=True)
def no_graph(monkeypatch):
    from recipe_wrangler.services.adaptation import nutrition_adaptation
    monkeypatch.setattr(nutrition_adaptation, 'USE_TAGS', False)


CF = {'Prawns, raw': 20.0, 'Mussels, raw': 2.0, 'Beef, steak, raw': 28.0, 'Rice, white, raw': 3.0, 'Onion, raw': 0.3, 'Chicken, meat, raw': 5.0, 'Turkey, meat, raw': 5.5,
      'Cheese, cheddar': 9.0, 'Cheese, mozzarella': 6.0}
RECORDS = [{'collection': 'nutritional_ingredients_irish', 'label': label, 'candidate_id': 'c' + str(n), 'dataset': 'irish', 'f': {'base_identity': True}}
           for n, label in enumerate(['Chicken, meat, raw', 'Turkey, meat, raw', 'Cheese, mozzarella', 'Mussels, raw'])]
item = lambda name, w, label, cid: (CF.get(label), 'agribalyse')  # noqa: E731
label_cf = lambda label, cid=None: CF.get(label)  # noqa: E731


def ing(pos, text, w, label):
    return NS(position=pos, name=text, original_text=text, quantity=NS(weight_g=w),
              composition=NS(provenance={'candidate': {'document': label}, 'candidate_id': 'x'}, source_dataset='irish'))


def beef_recipe(extra=()):
    snap = NS(serves=4, ingredients=[ing(1, '500g beef steak, cubed', 500, 'Beef, steak, raw'), ing(2, '200g rice', 200, 'Rice, white, raw'),
                                     ing(3, '1 onion', 100, 'Onion, raw'), *extra])
    return dict(recipe_id='r1', title='Beef stew', region='irish'), snap, {'title': 'Beef stew', 'instructions': ['Brown the beef.', 'Add the rice.']}


def cheese_recipe():
    snap = NS(serves=4, ingredients=[ing(1, '400g cheddar cheese, grated', 400, 'Cheese, cheddar'), ing(2, '200g rice', 200, 'Rice, white, raw')])
    return dict(recipe_id='r2', title='Cheesy rice', region='irish'), snap, {'title': 'Cheesy rice', 'instructions': ['Mix.']}


def prawn_recipe():
    snap = NS(serves=4, ingredients=[ing(1, '400g prawns, peeled', 400, 'Prawns, raw'), ing(2, '200g rice', 200, 'Rice, white, raw')])
    return dict(recipe_id='r3', title='Prawn rice', region='irish'), snap, {'title': 'Prawn rice', 'instructions': ['Cook.']}


def run(recipe, **kw):
    case, snap, rec = recipe
    return live.adapt_recipe(case, snap, rec, RECORDS, 'IE', item_factor=item, label_factor=label_cf, **kw)


def test_without_restrictions_beef_goes_to_poultry():
    row = run(beef_recipe())
    assert row['status'] == 'adapted' and row['is_adapted'] and row['swap']['candidate_label'] in ('chicken', 'turkey')
    assert row['benefit']['facet'] == 'sustainability' and 'CO2e' in row['benefit']['tip'] and row['benefit']['tip_source'] == 'code'
    assert row['adapted']['changed_positions'] == [1] and row['adapted']['lines'][0] != row['original']['lines'][0] and row['adapted']['lines'][1:] == row['original']['lines'][1:]
    assert row['source'] == 'live' and row['mode'] == 'sustainability' and row['region'] == 'IE'


def test_no_safe_swap_returns_a_reason_not_a_swap():
    # mussels would add an allergen the prawn recipe does not have, with or without user restrictions
    for kw in ({}, {'allergens': ['milk']}):
        row = run(prawn_recipe(), **kw)
        assert row['status'] == 'no_suggestion' and row['reason_code'] == 'alternatives_add_allergen' and not row['is_adapted'] and row['swap'] is None


@pytest.mark.parametrize('diet', ['vegetarian', 'vegan', 'pescatarian'])
def test_diet_blocks_poultry_targets_in_the_search(diet):
    case, snap, rec = beef_recipe()
    assert sa.adapt(case, snap, rec, RECORDS, item, label_cf)['status'] == 'adapted'
    out = sa.adapt(case, snap, rec, RECORDS, item, label_cf, diet=diet)
    assert out['status'] == 'no_candidate' and out['rejected']['allergen_or_diet'] >= 1


def test_recipe_that_already_breaks_the_diet_is_not_suitable():
    row = run(beef_recipe(), diet='pescatarian')
    assert row['status'] == 'not_suitable_for_user' and row['reason_code'] == 'recipe_breaks_user_restriction' and row['offending']
    assert row['swap'] is None and not row['is_adapted']


def test_milk_allergy_on_a_milk_recipe_is_not_suitable():
    row = run(cheese_recipe(), allergens=['milk'])
    assert row['status'] == 'not_suitable_for_user' and row['offending']


def test_an_unrelated_allergy_keeps_the_swap():
    row = run(beef_recipe(), allergens=['milk'])
    assert row['status'] == 'adapted' and row['swap']['candidate_label'] in ('chicken', 'turkey')


def test_empty_restrictions_equal_no_restrictions():
    assert run(beef_recipe(), allergens=[], diet=None)['swap'] == run(beef_recipe())['swap']


def test_adapt_blocks_a_target_with_the_users_allergen():
    case, snap, rec = cheese_recipe()
    plain = sa.adapt(case, snap, rec, RECORDS, item, label_cf)
    assert any('mozzarella' in c['to_label'].lower() for c in plain.get('candidates', []))
    blocked = sa.adapt(case, snap, rec, RECORDS, item, label_cf, allergens=['milk'])
    assert not any('mozzarella' in c['to_label'].lower() for c in blocked.get('candidates', [])) and 'allergen_or_diet' in blocked['rejected']


def test_why_sentences_exist_for_the_new_codes():
    from recipe_wrangler.services.adaptation.sustainability_reasons import WHY
    assert 'recipe_breaks_user_restriction' in set(WHY)


def test_ciqual_code_join_uses_the_shipped_table():
    assert live.ciqual_code('ciqual:20001') == '20001' and live.ciqual_code('nope') is None
    assert len(live._codes()) > 1000


def test_get_route_serves_the_stored_row_unless_restrictions_are_sent(monkeypatch):
    from recipe_wrangler.repositories import recipe_adaptations
    from recipe_wrangler.services.adaptation import router
    monkeypatch.setattr(recipe_adaptations, 'get_adaptation', lambda rid, mode, region: {'status': 'adapted', 'stored': True})
    seen = {}
    monkeypatch.setattr(live, 'adapt_active_recipe', lambda rid, region, allergens=(), diet=None: seen.update(a=list(allergens), d=diet) or {'status': 'adapted', 'source': 'live'})
    assert router.stored_adaptation('r1', 'sustainability', 'IE', allergens=[], diet=None)['stored'] is True and not seen
    assert router.stored_adaptation('r1', 'sustainability', 'IE', allergens=['milk'], diet='vegan')['source'] == 'live'
    assert seen == {'a': ['milk'], 'd': 'vegan'}
