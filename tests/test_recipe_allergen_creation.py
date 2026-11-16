"""Creation preserves recipe claims without promoting them to shared food facts."""
from contextlib import nullcontext
from types import SimpleNamespace
from unittest.mock import Mock
import importlib.util
from pathlib import Path
import pytest
from recipe_wrangler.repositories import neo4j_recipes as repo


class CaptureSession:
    def __init__(self, existing=()):
        self.calls = []
        self.claims = list(existing)

    def run(self, query, params):
        query = str(query)
        self.calls.append((query, params))
        if 'AS classes' in query:
            return []
        if 'MERGE (r:Recipe' in query and params.get('declared_allergens') is not None:
            self.claims = params['declared_allergens']
        if 'RETURN DISTINCT i.canonical_id' in query:
            return []
        return SimpleNamespace(single=lambda: {'names': self.claims}, consume=lambda: None)


def run_creation(monkeypatch, *, names=('sauce',), declared=None, hints=(), existing=(), source='user'):
    session = CaptureSession(existing)
    monkeypatch.setattr(repo, 'driver', SimpleNamespace(session=lambda: nullcontext(session)))
    result = repo.upsert_recipe_to_neo4j(
        'r', 'Sauce', list(names), list(names), [''] * len(names), ['Mix'], 5, 1,
        None, list(hints), [], source=source, declared_allergens=declared,
    )
    return session, result


@pytest.mark.parametrize('declared,existing,expected', [
    ([' Sesame ', 'sesame'], [], ['sesame']),
    (None, ['milk'], ['milk']),
    ([], ['milk'], []),
])
def test_creation_claim_ownership_and_preservation(monkeypatch, declared, existing, expected):
    session, (labels, _) = run_creation(monkeypatch, declared=declared, existing=existing)
    assert labels == expected
    assert session.claims == expected
    assert 'FOREACH' in session.calls[0][0]
    assert session.calls[0][1]['declaration_source'] == 'recipe_create'
    # No keyword-based ingredient fact can be seeded by a recipe's sesame claim.
    assert not [q for q, _ in session.calls if 'UNWIND $ingredient_hits' in q]


def test_creation_context_matches_analysis_even_when_caller_hints_missing(monkeypatch):
    names = ['coconut milk', 'oyster mushrooms', 'milk with almond milk', 'sauce without sesame']
    session, _ = run_creation(monkeypatch, names=names, declared=['sesame'])
    writes = {p['allergen']: p['ingredient_hits'] for q, p in session.calls if 'UNWIND $ingredient_hits' in q}
    assert set(writes) == set(repo.detect_allergens_from_names(names))
    assert 'sesame' not in writes
    assert 'molluscs' not in writes
    assert writes['milk'] == [{'name': 'milk with almond milk', 'hits': ['milk'], 'presence': 'contains'}]


def test_preserved_manual_claim_informs_creation_suitability(monkeypatch):
    classify = Mock(return_value=('unknown', []))
    monkeypatch.setattr(repo, '_classify_recipe_suitability', classify)
    _, (labels, _) = run_creation(monkeypatch, existing=['milk'])
    assert labels == ['milk']
    assert all(call.args[2] == {'milk'} for call in classify.call_args_list)


def test_creation_passes_possible_evidence_to_its_declaration_writer(monkeypatch):
    session, (labels, _) = run_creation(monkeypatch, names=['dried apricots'])
    writes = {p['allergen']: p['ingredient_hits'] for q, p in session.calls if 'UNWIND $ingredient_hits' in q}
    assert labels == []
    assert writes == {'sulphites': [{'name': 'dried apricots', 'hits': ['dried apricot'], 'presence': 'possible'}]}


def test_import_retains_metadata_with_qualified_origin(monkeypatch):
    session, (labels, _) = run_creation(monkeypatch, declared=['sesame'], source='HealthyFoods')
    assert labels == ['sesame']
    assert session.calls[0][1]['declaration_source'] == 'imported_recipe_metadata'
    path = Path('scripts/postgres/import_healthyfoods_to_neo4j.py')
    spec = importlib.util.spec_from_file_location('healthyfoods_creation_test', path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    base = {'healthyfoods_recipe': {'title': 'Sauce', 'ingredients': ['sauce']},
            'profile_result': {'ingredient_names': ['sauce'], 'measurements': [''], 'allergens': ['sesame']}}
    fields = module._extract_recipe_fields('r', base)
    assert fields['declared_allergens'] == ['sesame']
    base['profile_result']['allergens'] = []
    assert module._extract_recipe_fields('r', base)['declared_allergens'] is None


@pytest.mark.parametrize('projected', [{}, {'allergens': ['milk'], 'allergen_evidence': [
    {'allergen': 'milk', 'ingredient': 'milk', 'presence': 'contains'}]}])
def test_create_http_retains_manual_claim_during_projection_delay(monkeypatch, projected):
    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    from recipe_wrangler.api.routers import recipes
    from recipe_wrangler.utils import nutrition_postgres
    saved = []
    monkeypatch.setattr(recipes, '_generate_user_recipe_id', lambda *args: 'r')
    monkeypatch.setattr(recipes, 'split_ingredient_lines', lambda lines: (['sauce'], ['']))
    monkeypatch.setattr(recipes, 'upsert_recipe_to_neo4j', lambda **kw: (saved.append(kw) or (['sesame'], [])))
    monkeypatch.setattr(recipes, 'commit_recipe', lambda *args: SimpleNamespace(complete=True))
    monkeypatch.setattr(recipes, '_catalog_recipe_by_id', lambda *args: projected)
    monkeypatch.setattr(recipes, 'cache_delete', lambda *args: None)
    monkeypatch.setattr(nutrition_postgres, 'upsert_recipe_profiling_trace', lambda *args: None)
    app = FastAPI(); app.include_router(recipes.router)
    payload = dict(title='Sauce', ingredients=['sauce'], instructions=['Mix'], duration=5, serves=1,
                   allergens=[' Sesame '], **{name: 0 for name in [
                       'protein_g','carbohydrate_g','fat_g','energy_kcal','sugar_g',
                       'saturated_fat_g','sodium_mg','fibre_g']})
    with TestClient(app) as client:
        response = client.post('/recipes/', json=payload)
    assert response.status_code == 200, response.text
    assert saved[0]['allergens'] == []
    assert saved[0]['declared_allergens'] == [' Sesame ']
    body = response.json()
    assert 'sesame' in body['allergens']
    claim = next(e for e in body['allergen_evidence'] if e['allergen'] == 'sesame')
    assert claim['sources'] == ['recipe_create'] and claim['keyword_matches'] == []
    assert body['allergen_uncertainty'] is True


def test_patch_replaces_creation_provenance(monkeypatch):
    execute = Mock(return_value=[{'rid': 'r'}])
    monkeypatch.setattr(repo, 'run_query', execute)
    repo.update_recipe_in_neo4j('r', allergens=['egg'])
    assert "r.declared_allergens_source = 'recipe_edit'" in execute.call_args.args[0]


def test_bulk_and_incremental_preserve_claim_origin():
    import ast
    import re
    fragments = []
    for path in ['src/recipe_wrangler/catalog/projection.py', 'scripts/catalog/build_recipes.py']:
        tree = ast.parse(Path(path).read_text())
        query = next(ast.literal_eval(n.value) for n in tree.body if isinstance(n, ast.Assign)
                     and any(isinstance(t, ast.Name) and t.id == 'RECIPE_QUERY' for t in n.targets))
        fragment = re.search(r'RETURN detected \+ recipe_evidence \+ \[name IN coalesce\(r.declared_allergens, \[\]\).*?AS allergen_evidence', query, re.S).group(0)
        assert 'coalesce(r.declared_allergens_source, "recipe_edit")' in fragment
        fragments.append(fragment)
    assert fragments[0] == fragments[1]
