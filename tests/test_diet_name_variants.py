"""Whole-name evidence for ordinary preparations; mixtures stay uncertain."""
import re
from unittest.mock import Mock, patch

import pytest

from recipe_wrangler.utils import dietary_classification as dc
from recipe_wrangler.utils.consumer_suitability import PLANT_STAPLE_PATTERNS
from recipe_wrangler.utils.dietary_ingredient_evidence import canonical_ingredient_name, ingredient_diet_evidence


@pytest.mark.parametrize('name', [
    'roasted unsalted peanuts', 'popped popcorn', 'sweet mini peppers',
    'vanilla-flavored soymilk', 'unsweetened vanilla soy milk', 'gluten free baking flour',
    'earth balance vegan butter',
])
def test_plain_plant_name_variants_have_whole_name_evidence(name):
    key = canonical_ingredient_name(name)
    assert any(re.fullmatch(p, key) for p in PLANT_STAPLE_PATTERNS)


def test_roasted_peanuts_and_popped_popcorn_keep_attributed_source_evidence():
    for name in ['roasted unsalted peanuts', 'popped popcorn']:
        evidence = ingredient_diet_evidence(name, 'vegan')
        assert evidence['status'] == 'suitable'
        assert evidence['property_sources']


@pytest.mark.parametrize('name', [
    'honey roasted peanuts', 'roasted peanuts with butter', 'popcorn with bacon',
    'sweet mini peppers stuffed with beef', 'vanilla soymilk and cream',
    'soymilk with honey', 'gluten free pancake mix', 'earth balance buttery spread',
    'earth balance vegan butter with cream',
    'mayonnaise',
])
def test_mixtures_products_and_ambiguous_foods_do_not_gain_plain_plant_evidence(name):
    assert not any(re.fullmatch(p, canonical_ingredient_name(name)) for p in PLANT_STAPLE_PATTERNS)


def test_graph_classifier_receives_the_same_new_whole_name_candidate():
    batch = [{'element_id': 'one', 'name': 'roasted unsalted peanuts', 'class_ids': []}]
    with patch.object(dc, 'ingredient_link_batches', return_value=iter([batch])), patch.object(dc, '_scalar', return_value=1) as scalar:
        dc._classify_positive(Mock(), 'vegan', ['one'])
    assert scalar.call_args.kwargs['candidates'][0]['external_positive'] == ['open_food_facts:en:peanut']
