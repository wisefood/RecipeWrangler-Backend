"""Source inheritance and runtime safeguards for external dietary knowledge."""
from unittest.mock import Mock, patch

import pytest

from scripts.preprocessing.build_dietary_ingredient_evidence import build
from recipe_wrangler.utils.dietary_ingredient_evidence import ingredient_diet_evidence
from recipe_wrangler.utils.consumer_suitability import dietary_keyword_evidence
from recipe_wrangler.utils.consumer_suitability import PLANT_STAPLE_PATTERNS, GROUP_RULES, positive_keyword_regex
from recipe_wrangler.utils.dietary_ingredient_evidence import canonical_ingredient_name
import re
from recipe_wrangler.utils import dietary_classification as dc
from recipe_wrangler.utils.non_food_ingredients import is_unambiguous_non_food_ingredient


def test_properties_inherit_but_explicit_maybe_and_conflicts_do_not_become_yes():
    result = build('''en: vegetable
vegan:en: yes
vegetarian:en: yes

< en: vegetable
en: lettuce, salad leaf

< en: vegetable
en: prepared salad
vegan:en: maybe

en: animal
vegan:en: no

< en: animal
< en: vegetable
en: uncertain food
''')
    entries = {e['id']: e for e in result['entries']}
    assert entries['en:lettuce']['vegan'] == {'value': 'yes', 'sources': ['en:vegetable']}
    assert entries['en:prepared salad']['vegan']['value'] == 'maybe'
    assert entries['en:uncertain food']['vegan']['value'] == 'maybe'


def test_direct_milk_constituent_resolves_ambiguous_parent_but_not_conflicting_yes():
    entries = build('''en: chocolate
vegan:en: maybe

< en: chocolate
en: white chocolate
allergens:en: en:milk

en: contradictory food
vegan:en: yes
allergens:en: en:milk
''')['entries']
    assert entries[1]['vegan']['value'] == 'no'
    assert 'allergen:en:milk' in entries[1]['vegan']['sources']
    assert entries[2]['vegan']['value'] == 'maybe'


@pytest.mark.parametrize('name', [
    'romaine lettuce', 'grapeseed oil', 'garbanzo beans', 'organic romaine lettuce',
    'mixed vegetables', 'agave nectar', 'mint leaf',
])
def test_plain_plants_gain_attributed_evidence(name):
    for group in ['vegan', 'vegetarian']:
        result = ingredient_diet_evidence(name, group)
        assert result['status'] == 'suitable'
        assert result['property_sources']


@pytest.mark.parametrize('name', [
    'romaine lettuce with beef', 'grapeseed oil and cream', 'unknown sauce',
    'mayonnaise', 'pasta', 'margarine', 'chicken flavored romaine lettuce',
    'romaine lettuce (may contain milk)', 'not vegan cheese', 'vegan cheese with milk',
])
def test_unknown_mixtures_and_claims_cannot_borrow_plain_food_evidence(name):
    assert (ingredient_diet_evidence(name, 'vegan') or {}).get('status') != 'suitable'


@pytest.mark.parametrize('name', ['flank steak', 'blade roast', 'hamburger', 'chuck roast'])
def test_unqualified_meat_names_block_all_three_groups(name):
    for group in ['vegan', 'vegetarian', 'pescatarian']:
        hits, presence = dietary_keyword_evidence(name, group)
        assert hits and presence == 'contains'


@pytest.mark.parametrize('name', [
    'hamburger buns', 'vegan hamburger', 'bean burger', 'cauliflower steak', 'vegan flank steak',
    'blade roast or tofu', 'without flank steak', 'blade roast (optional)',
])
def test_meat_shapes_choices_and_negations_do_not_become_definite_meat(name):
    hits, presence = dietary_keyword_evidence(name, 'vegetarian')
    assert not hits or presence != 'contains'


def test_external_animal_evidence_obeys_optional_and_choice_scope():
    assert dietary_keyword_evidence('lactose', 'vegan')[1] == 'contains'
    for name in ['lactose or tofu', 'lactose (optional)', 'without lactose']:
        hits, state = dietary_keyword_evidence(name, 'vegan')
        assert not hits or state != 'contains'


def test_real_graph_classifier_receives_attributed_positive_candidate():
    session = Mock()
    batch = [{'element_id': 'one', 'name': 'romaine lettuce', 'class_ids': []}]
    with patch.object(dc, 'ingredient_link_batches', return_value=iter([batch])), \
         patch.object(dc, '_scalar', return_value=1) as scalar:
        dc._classify_positive(session, 'vegan', ['one'])
    assert scalar.call_args.kwargs['candidates'][0]['external_positive'] == ['open_food_facts:en:romaine lettuce']
    query = scalar.call_args.args[1]
    assert 'rel.status <> "not_suitable"' in query
    assert 'size(possible_allergens) = 0' in query
    assert scalar.call_args.kwargs['candidates'][0]['allow_origin']


def test_prepared_mix_cannot_be_confirmed_from_plant_ancestor_alone():
    batch = [{'element_id': 'one', 'name': 'pancake mix', 'class_ids': ['plant']}]
    with patch.object(dc, 'ingredient_link_batches', return_value=iter([batch])), \
         patch.object(dc, '_scalar', return_value=1):
        dc._classify_positive(Mock(), 'vegan', ['one'])
    assert not batch[0]['allow_origin']


def test_white_chocolate_cannot_inherit_vegan_from_cocoa():
    evidence = ingredient_diet_evidence('white chocolate', 'vegan')
    assert evidence['status'] == 'not_suitable'
    assert 'allergen:en:milk' in evidence['property_sources']
    hits, presence = dietary_keyword_evidence('white chocolate', 'vegan')
    assert hits and presence == 'contains'
    assert not dietary_keyword_evidence('vegan white chocolate', 'vegan')[0]
    assert dietary_keyword_evidence('vegan white chocolate with milk', 'vegan')[1] == 'contains'


@pytest.mark.parametrize('name,group', [
    ('soymilk', 'vegan'), ("confectioners’ sugar", 'vegan'),
    ('organic cane sugar', 'vegan'), ('jicama', 'vegan'),
    ('Colby Jack cheese', 'vegetarian'), ('Monterey Jack cheese', 'vegetarian'),
    ('heavy whipping cream', 'vegetarian'), ('vanilla yogurt', 'vegetarian'),
    ('parmesan cheese', 'vegetarian'), ('cream cheese', 'vegetarian'),
])
def test_reviewed_named_foods_use_whole_names(name, group):
    patterns = PLANT_STAPLE_PATTERNS + [positive_keyword_regex(k) for k in GROUP_RULES[group]['positive_keywords']]
    assert any(re.fullmatch(p, canonical_ingredient_name(name)) for p in patterns)
    assert not any(re.fullmatch(p, canonical_ingredient_name(name + ' and mystery sauce')) for p in patterns)


def test_new_dairy_names_do_not_override_explicit_animal_rennet():
    assert dietary_keyword_evidence('Monterey Jack cheese with animal rennet', 'vegetarian')[1] == 'contains'


def test_equipment_is_filtered_but_pipette_contents_are_not():
    assert is_unambiguous_non_food_ingredient('miniature culinary pipettes')
    assert not is_unambiguous_non_food_ingredient('champagne')
    assert not is_unambiguous_non_food_ingredient('milk in culinary pipettes')
