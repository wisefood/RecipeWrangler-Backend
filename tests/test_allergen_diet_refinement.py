"""Source-derived regressions; deliberately separate possible from present."""
import pytest

from recipe_wrangler.repositories.neo4j_recipes import (
    detect_allergen_evidence_from_names, detect_allergens_from_names,
    _classify_recipe_suitability,
)
from recipe_wrangler.utils.es_recipe_evidence import (
    assessed_diet_tags, recipe_allergen_fields, recipe_consumer_fields,
)
from recipe_wrangler.utils.consumer_suitability import classification_version


@pytest.mark.parametrize('line,expected', [
    ('6 slices wholegrain sourdough, toasted, to serve', {'wheat', 'gluten'}),
    ('1 pitta pocket', {'wheat', 'gluten'}),
    ('1 wholemeal bagel', {'wheat', 'gluten'}),
    ('50 g Crispbread', {'wheat', 'gluten'}),
    ('1 slice of whole meal toast', {'wheat', 'gluten'}),
    ('1500 g Brownshrimp', {'crustacean_shellfish'}),
    ('gluten-free wheat starch', {'wheat'}),
    ('preservative sodium metabisulphite', {'sulphites'}),
    ('egg noodles or store-bought pasta', {'wheat', 'gluten'}),
    ('1/2 cup sour cream (low fat is optional)', {'milk'}),
    ('829.4 g Fried anchovies', {'fish'}),
    ('4 tablespoons reduced fat crème fraiche', {'milk'}),
])
def test_reviewed_named_sources(line, expected):
    assert expected <= set(detect_allergens_from_names([line]))


@pytest.mark.parametrize('line,allergen', [
    ('50g dried apricots', 'sulphites'),
    ('1/2 cup raisins', 'sulphites'), ('20 g Wine vinegar', 'sulphites'),
    ('1 teaspoon low-sodium soy sauce', 'wheat'),
    ('1 teaspoon low-sodium soy sauce', 'gluten'),
    ('chopped nuts (optional)', 'tree_nut'), ('mixed nuts', 'tree_nut'),
    ('250g ricotta ravioli or tortellini', 'milk'),
    ('milk or almond milk', 'milk'), ('may contain milk', 'milk'),
    ('vegetable stock cube', 'celery'), ('vegetable stock cube', 'milk'),
    ('hummus', 'sesame'), ('basil pesto', 'milk'),
    ('Caesar dressing', 'fish'), ('rye breadcrumbs', 'wheat'),
    ('mixed nuts', 'peanut'), ('no added sulphites wine', 'sulphites'),
    ('oat pastry', 'wheat'),
])
def test_uncertain_formulations_and_choices_are_possible(line, allergen):
    evidence = detect_allergen_evidence_from_names([line])
    assert any(r['allergen'] == allergen and r['presence'] == 'possible' for r in evidence)
    assert allergen not in detect_allergens_from_names([line])
    assessment = recipe_allergen_fields(None, evidence)['allergen_assessment']
    assert allergen in assessment['possible_allergens']


@pytest.mark.parametrize('line,allergen', [
    ('pine nuts', 'tree_nut'), ('chestnuts', 'tree_nut'),
    ('cream of tartar', 'milk'), ('cashew cheese sauce', 'milk'),
    ('pumpkin seed flour', 'wheat'), ('pumpkin seed flour', 'gluten'),
    ('edamame & mung bean fettuccine', 'wheat'),
    ('edamame & mung bean fettuccine', 'gluten'),
    ('sulphite-free wine', 'sulphites'), ('egg-free mayonnaise', 'egg'),
    ('gluten-free soy sauce', 'wheat'), ('gluten-free wheat starch', 'gluten'),
    ('cashew nuts', 'peanut'), ('pine nuts', 'peanut'),
    ('thin rice vermicelli noodles', 'wheat'), ('rice vermicelli noodles', 'gluten'),
    ('sauce without anchovies', 'fish'),
])
def test_reviewed_exceptions_do_not_add_positive_or_possible_evidence(line, allergen):
    assert not any(r['allergen'] == allergen for r in detect_allergen_evidence_from_names([line]))


def test_an_independent_positive_wins_over_possible_evidence():
    rows = detect_allergen_evidence_from_names(['milk or oat milk', 'whey powder'])
    fields = recipe_allergen_fields(None, rows)['allergen_assessment']
    assert fields['detected_allergens'] == ['milk']
    assert 'milk' not in fields['possible_allergens']


def test_possible_evidence_suppresses_free_from_but_does_not_downgrade_positive_diet():
    rows = detect_allergen_evidence_from_names(['vegetable stock'])
    consumer = recipe_consumer_fields([
        {'group': 'vegan', 'status': 'suitable', 'classification_version': classification_version('vegan')},
    ], evidence=rows)
    # owner decision 2026-11-18: "may contain" never gates suitability
    assert consumer['consumer_suitability'][0]['status'] == 'suitable'
    assert 'vegan' in consumer['suitable_for']
    assert assessed_diet_tags(['dairy_free', 'gluten_free'], consumer['consumer_suitability'], evidence=rows) == ['vegan']


@pytest.mark.parametrize('name', ['hake', 'hoki', 'calamari', 'brownshrimp', 'gelatine', 'anchovies'])
@pytest.mark.parametrize('group', ['vegan', 'vegetarian'])
def test_unlinked_animal_names_block_diet(name, group):
    assert _classify_recipe_suitability('Meal', [name], set(), group)[0] == 'not_suitable'


@pytest.mark.parametrize('name', ['goat cheese', 'oyster mushrooms', 'vegan gelatine', 'vegan salmon', 'cashew cheese'])
def test_named_alternatives_and_dairy_do_not_block_vegetarian(name):
    assert _classify_recipe_suitability('Meal', [name], set(), 'vegetarian')[0] == 'unknown'


def test_animal_ingredients_in_a_mixture_still_block():
    assert _classify_recipe_suitability('Meal', ['vegan gelatine with pork'], set(), 'vegetarian')[0] == 'not_suitable'


def test_possible_risk_is_not_support_for_confirmed_reconciliation():
    from recipe_wrangler.repositories.neo4j_recipes import _allergen_keyword_hits
    assert _allergen_keyword_hits('vegetable stock', 'milk') == []
    assert _allergen_keyword_hits('milk', 'milk') == ['milk']


def test_offline_evaluation_retains_strongest_evidence_and_missing_states():
    from scripts.eval.evaluate_allergen_diets import evidence_states, comparison
    rows = [{'allergen': 'milk', 'presence': p} for p in ['possible', 'contains', 'possible']]
    assert evidence_states(rows) == {'milk': 'present'}
    assert comparison({'milk': 'present', 'egg': 'possible'}, {'milk': 'present'}) == {
        'present->present': 1, 'possible->not_flagged': 1,
    }


