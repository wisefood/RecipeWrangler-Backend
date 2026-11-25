"""Free-from requests exclude positives without claiming verified absence."""
from types import SimpleNamespace
import importlib.util
from pathlib import Path

import pytest

from recipe_wrangler.catalog import foodchat
from recipe_wrangler.tools import es_recipe_search as search
from recipe_wrangler.utils.es_recipe_evidence import recipe_allergen_fields

_spec = importlib.util.spec_from_file_location('query_subset', Path(__file__).with_name('allergen_query_subset.py'))
_module = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_module)


@pytest.mark.parametrize('tag,allergen', [('nut_free', 'peanut'), ('nut_free', 'tree_nut'), ('dairy_free', 'milk'), ('gluten_free', 'wheat'), ('gluten_free', 'gluten')])
def test_both_search_paths_keep_unknowns_and_exclude_positive_evidence(tag, allergen):
    request = SimpleNamespace(constraints=SimpleNamespace(), user_profile=SimpleNamespace(diet=[tag]))
    paths = [
        search.build_es_query(search.RecipeSearchConstraints(diet_tags=[tag]))['query']['bool']['filter'],
        foodchat._hard_filters(request, []),
    ]
    base = {'has_profile': True, 'ingredients': [], **recipe_allergen_fields()}
    for filters in paths:
        assert all(_module.matches(clause, base) for clause in filters)
        for evidence in [
            {'allergens': [allergen]},
            {'allergen_evidence': [{'allergen': allergen, 'presence': 'contains'}]},
        ]:
            assert not all(_module.matches(clause, {**base, **evidence}) for clause in filters)
        uncertain = {**base, 'allergen_evidence': [{'allergen': allergen, 'presence': 'possible'}]}
        assert all(_module.matches(clause, uncertain) for clause in filters)
        assert base['allergen_uncertainty'] is True


@pytest.mark.parametrize('tag',['dairyfree','dairy-free','dairy free'])
def test_free_from_aliases_use_the_same_request_contract(tag):
    query=search.build_es_query(search.RecipeSearchConstraints(diet_tags=[tag]))
    filters=query['query']['bool']['filter']
    assert all(_module.matches(clause,{'has_profile':True}) for clause in filters)
    assert not all(_module.matches(clause,{'has_profile':True,'allergens':['milk']}) for clause in filters)


def test_batch_generators_abstain_without_querying_or_writing():
    spec = importlib.util.spec_from_file_location('tag_diet_contract', 'scripts/facets/tag_diet.py')
    batch = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(batch)
    for apply in [False, True]:
        assert batch._tag_allergen_absence(None, 'nut_free', ['peanut'], apply=apply) == 0


def test_batch_replace_cannot_erase_source_labels(monkeypatch):
    from scripts.facets import tag_diet
    from unittest.mock import patch
    monkeypatch.setattr('sys.argv',['tag_diet','--apply','--replace'])
    with patch.object(tag_diet,'_driver') as driver,pytest.raises(SystemExit):
        tag_diet.main()
    driver.assert_not_called()


def test_graph_free_from_request_uses_presence_checks_and_preserves_other_diets():
    from recipe_wrangler.schemas import RecipeSearchFilters
    from recipe_wrangler.tools.param_search import build_param_search_cypher
    query, _, params = build_param_search_cypher(
        RecipeSearchFilters(diet_tags=['nut_free', 'vegetarian'], exclude_allergens=['milk'])
    )
    assert params['diet_tags'] == ['vegetarian']
    assert {'peanut', 'tree_nut', 'milk'} <= set(params['exclude_allergens'])
    assert '(r)-[:HAS_DECLARATION]' in query
    assert '(:Ingredient)-[:HAS_DECLARATION]' in query
    assert "['contains', 'present', '']" in query
