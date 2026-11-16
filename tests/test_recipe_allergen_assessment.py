from recipe_wrangler.utils.es_recipe_evidence import recipe_allergen_fields, known_allergen_exclusions
from recipe_wrangler.utils.food_ontology import ALLERGEN_ONTOLOGY_MAPPINGS


def test_empty_labels_leave_every_group_unresolved():
    fields = recipe_allergen_fields([], [])
    assert fields['allergen_uncertainty'] is True
    assert set(fields['allergen_assessment']['unresolved_allergens']) == set(ALLERGEN_ONTOLOGY_MAPPINGS)
    assert fields['allergen_assessment']['reason_codes'] == ['allergen_absence_not_assessed']
    assert fields['allergen_assessment']['cross_contact_assessed'] is False


def test_positive_presence_resolves_only_that_group():
    result = recipe_allergen_fields([], [{'allergen':'Peanut','presence':'contains'}])
    assert result['allergen_assessment']['detected_allergens'] == ['peanut']
    assert 'peanut' not in result['allergen_assessment']['unresolved_allergens']
    assert 'milk' in result['allergen_assessment']['unresolved_allergens']
    assert result['allergen_uncertainty'] is True


def test_possible_and_ingredient_absence_do_not_certify_recipe_absence():
    result = recipe_allergen_fields([], [
        {'allergen':'milk','presence':'may_contain'},
        {'allergen':'egg','presence':'absent'},
    ])
    assert result['allergen_assessment']['possible_allergens'] == ['milk']
    assert {'milk','egg'} <= set(result['allergen_assessment']['unresolved_allergens'])
    assert result['allergen_assessment']['detected_allergens'] == []


def test_recorded_positive_label_wins_over_conflicting_absence():
    result = recipe_allergen_fields(['milk'], [{'allergen':'milk','presence':'absent'}])
    assert result['allergen_assessment']['detected_allergens'] == ['milk']


def test_known_conflict_filter_targets_only_positive_evidence():
    clauses = known_allergen_exclusions(['peanut'])
    assert 'peanut' in clauses[0]['terms']['allergens']
    positive = clauses[1]['nested']['query']['bool']['filter']
    assert {'terms':{'allergen_evidence.presence':['contains','present']}} in positive[1]['bool']['should']
    assert 'unknown' not in str(positive)
    assert 'may_contain' not in str(positive)


def test_response_models_preserve_the_shared_warning():
    from recipe_wrangler.schemas.models import RecipeCreateResponse, RecipeDetailResponse, FoodChatRecipeItem
    fields = recipe_allergen_fields([], [])
    created = RecipeCreateResponse(recipe_id='r', **fields).model_dump()
    detail = RecipeDetailResponse(recipe_id='r', title='Recipe', ingredients=[], instructions=[], duration=None, serves=None, **fields).model_dump()
    meal = FoodChatRecipeItem(recipe_id='r', title='Recipe', ingredients='', directions='', **fields).model_dump()
    for response in [created, detail, meal]:
        assert response['allergen_uncertainty'] is True
        assert response['allergen_assessment']['reason_codes'] == ['allergen_absence_not_assessed']
