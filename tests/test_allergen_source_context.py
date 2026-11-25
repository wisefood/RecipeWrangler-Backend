"""Source-derived regressions and internal boundary controls, not accuracy gold."""
import pytest
from recipe_wrangler.repositories.neo4j_recipes import (
    detect_allergen_evidence_from_names, detect_allergens_from_names,
)
from recipe_wrangler.utils.es_recipe_evidence import recipe_allergen_fields


@pytest.mark.parametrize('text,allergen', [
    ('Eggs, chicken, whole, scrambled, without milk', 'milk'),
    ('fresh oyster mushrooms', 'molluscs'),
    ('oyster-mushroom powder', 'molluscs'),
    ('sauce without any milk', 'milk'),
    ('batter with no added milk', 'milk'),
])
def test_explicit_context_is_not_positive_keyword_evidence(text, allergen):
    assert allergen not in detect_allergens_from_names([text])
    evidence = detect_allergen_evidence_from_names([text])
    assert not any(row['allergen'] == allergen for row in evidence)
    assessment = recipe_allergen_fields(None, evidence)['allergen_assessment']
    assert allergen in assessment['unresolved_allergens']


@pytest.mark.parametrize('text,allergen', [
    ('scrambled eggs without milk', 'egg'),
    ('without milk and with cream', 'milk'),
    ('without milk, milk powder added separately', 'milk'),
    ('without milk, contains whey', 'milk'),
    ('do not cook without milk', 'milk'),
    ('oyster mushrooms and oysters', 'molluscs'),
    ('oyster sauce', 'molluscs'),
])
def test_other_positive_mentions_remain_detected(text, allergen):
    assert allergen in detect_allergens_from_names([text])


def test_preparation_milk_lost_from_normalized_oats_is_a_source_candidate():
    text = 'Porridge oats, unfortified, cooked, made up with semi-skimmed milk'
    assert 'milk' in detect_allergens_from_names([text])
    assert 'milk' not in detect_allergens_from_names(['Porridge oats'])
