"""Wording regressions: mandatory foods, choices and warnings have separate scope."""
import pytest
from recipe_wrangler.repositories.neo4j_recipes import _allergen_keyword_evidence, _classify_recipe_suitability
from recipe_wrangler.utils.consumer_suitability import dietary_keyword_evidence
from recipe_wrangler.utils.foodon_matching import supported_existing_link, ingredient_link_batches


@pytest.mark.parametrize('name,allergen,presence', [
    ('milk, may contain soy', 'milk', 'contains'),
    ('milk, may contain soy', 'soy', 'possible'),
    ('milk (may contain soy)', 'milk', 'contains'),
    ('milk (may contain soy)', 'soy', 'possible'),
    ('milk with optional almonds', 'milk', 'contains'),
    ('milk with optional almonds', 'tree_nut', 'possible'),
    ('coconut milk, cream', 'milk', 'contains'),
    ('gluten-free pasta, wheat flour', 'gluten', 'contains'),
    ('milk or almond milk', 'milk', 'possible'),
    ('milk (or almond milk)', 'milk', 'possible'),
    ('egg noodles or store-bought pasta', 'wheat', 'contains'),
    ('nuts (optional)', 'tree_nut', 'possible'),
    ('sour cream (low fat is optional)', 'milk', 'contains'),
])
def test_scoped_allergen_evidence(name, allergen, presence):
    hits, actual = _allergen_keyword_evidence(name, allergen)
    assert hits
    assert actual == presence


@pytest.mark.parametrize('name,status', [
    ('sauce without milk', 'unknown'), ('chicken or tofu', 'unknown'),
    ('optional chicken', 'unknown'), ('may contain milk', 'unknown'),
    ('coconut milk, cream', 'not_suitable'), ('chicken or beef', 'not_suitable'),
    ('kidney beans and beef', 'not_suitable'), ('coconut milk and cream', 'not_suitable'),
    ('milk with optional almonds', 'not_suitable'),
    ('without milk and cream', 'unknown'), ('without milk and with cream', 'not_suitable'),
    ('do not cook without milk', 'not_suitable'),
])
def test_scoped_diet_blocking(name, status):
    assert _classify_recipe_suitability('Dinner', [name], set(), 'vegan')[0] == status
    hits, presence = dietary_keyword_evidence(name, 'vegan')
    assert bool(hits and presence == 'contains') == (status == 'not_suitable')


@pytest.mark.parametrize('name,label,expected', [
    ('pork fillet', 'fish fillet', False), ('peanut butter', 'butter', False),
    ('milk (coconut)', 'milk', False), ('chicken or tofu', 'chicken', False),
    ('optional milk', 'milk', False), ('courgettes', 'zucchini, raw', True),
    ('milk', 'milk food product', True),
])
def test_old_ontology_links_need_identity(name, label, expected):
    assert supported_existing_link(name, 'FOODON_test', label) is expected


def test_ontology_reader_pages_and_rejects_bad_link():
    class Session:
        def __init__(self): self.calls = []
        def run(self, query, params):
            assert query.timeout == 60
            self.calls.append(params)
            return [] if params['after'] else [
                {'element_id':'1', 'name':'pork fillet', 'classes':[{'foodon_id':'bad','label':'fish fillet'}]},
                {'element_id':'2', 'name':'milk', 'classes':[{'foodon_id':'good','label':'milk'}]},
            ]
    session = Session()
    assert list(ingredient_link_batches(session)) == [[
        {'element_id':'1','name':'pork fillet','class_ids':[]},
        {'element_id':'2','name':'milk','class_ids':['good']},
    ]]
    assert [p['after'] for p in session.calls] == ['', '2']


def test_alternative_conjunction_does_not_confirm_one_choice_ingredient():
    assert _allergen_keyword_evidence('chicken or tofu with milk', 'milk')[1] == 'possible'


def test_graph_negative_classifier_receives_contextual_hits_and_vetted_links(monkeypatch):
    from recipe_wrangler.utils import dietary_classification as dc
    from types import SimpleNamespace
    batch = [
        {'element_id':'1','name':'sauce without milk','class_ids':[]},
        {'element_id':'2','name':'chicken or tofu','class_ids':[]},
        {'element_id':'3','name':'coconut milk, cream','class_ids':[]},
    ]
    monkeypatch.setattr(dc, 'ingredient_link_batches', lambda *a, **k: iter([batch]))
    calls = []
    class Session:
        def run(self, query, **params):
            calls.append((str(query), params))
            return SimpleNamespace(single=lambda: {'count':1})
    assert dc._classify_negative(Session(), 'vegan', ['1','2','3']) == 1
    candidates = calls[0][1]['candidates']
    assert candidates[0]['keyword_hits'] == candidates[1]['keyword_hits'] == []
    assert 'cream' in candidates[2]['keyword_hits']
    assert 'mapped.foodon_id IN item.class_ids' in calls[0][0]


def test_graph_recipe_title_uses_same_context_rules():
    from recipe_wrangler.utils import dietary_classification as dc
    from types import SimpleNamespace
    calls = []
    class Session:
        def run(self, query, **params):
            calls.append((str(query), params))
            if 'RETURN rid, r.title AS title' in str(query):
                return [{'rid':'negated','title':'Sauce without milk'},
                        {'rid':'meat','title':'Beef and potatoes'}]
            return SimpleNamespace(single=lambda: {'count':2})
    assert dc._aggregate_recipe_batch(Session(), ['negated','meat'], 'vegan') == 2
    assert calls[-1][1]['blocked_titles'] == ['meat']


@pytest.mark.parametrize('name', ['cream, dairy-free', 'cheese, vegan', 'beans, butter',
    'Milk, soya, non-dairy alternative to milk, unsweetened, fortified'])
def test_diet_retains_inverted_food_and_suffix_alternatives(name):
    assert _classify_recipe_suitability('Dinner', [name], set(), 'vegan')[0] == 'unknown'


def test_comma_optional_qualifies_the_preceding_food():
    assert _allergen_keyword_evidence('almonds, optional', 'tree_nut')[1] == 'possible'


def test_egg_species_qualifier_is_not_chicken_meat():
    assert _classify_recipe_suitability('Dinner', ['Eggs, chicken, whole, scrambled, without milk'],
                                       set(), 'vegetarian')[0] == 'unknown'


def test_batch_foodon_writes_only_supported_links(monkeypatch):
    from scripts.neo4j import tag_allergens as tag
    from types import SimpleNamespace
    from contextlib import nullcontext
    items = [{'element_id':'bad','name':'pork fillet','class_ids':[]},
             {'element_id':'good','name':'milk','class_ids':['FOODON_00001256']}]
    monkeypatch.setattr(tag,'ingredient_link_batches',lambda *a,**k:iter([items]))
    calls = []
    class Session:
        def run(self, query, **params):
            assert query.timeout == 60
            calls.append((str(query),params))
            return SimpleNamespace(single=lambda:{'tagged':1})
    assert tag._tag_by_foodon(SimpleNamespace(session=lambda:nullcontext(Session())),
                              'milk',['FOODON_00001256']) == 1
    assert calls[0][1]['supported_links'] == items[1:]
    assert 'f.foodon_id IN item.class_ids' in calls[0][0]


def test_creation_foodon_writer_has_the_same_identity_guard(monkeypatch):
    import runpy
    from recipe_wrangler.repositories import neo4j_recipes as repo
    helpers = runpy.run_path('tests/test_recipe_allergen_creation.py')
    links = [{'element_id':'1','name':'pork fillet','class_ids':[]}]
    monkeypatch.setattr(repo,'ingredient_link_batches',lambda *a,**k:iter([links]))
    session, _ = helpers['run_creation'](monkeypatch,names=['pork fillet'])
    writes = [(q,p) for q,p in session.calls if 'any(item IN $supported_links' in q]
    assert writes
    assert all(p['supported_links'] == links for q,p in writes)
    assert all('f.foodon_id IN item.class_ids' in q for q,p in writes)


@pytest.mark.parametrize('name', ['milk, optional almonds', 'milk with optional almonds'])
def test_optional_named_food_does_not_qualify_previous_mandatory_food(name):
    assert _allergen_keyword_evidence(name, 'milk')[1] == 'contains'
    assert _allergen_keyword_evidence(name, 'tree_nut')[1] == 'possible'


def test_optional_serving_instruction_keeps_food_optional():
    assert _allergen_keyword_evidence('4 tbsp sour cream, dollop (optional)', 'milk')[1] == 'possible'


@pytest.mark.parametrize('name,foodon_id,label', [
    ('beef tomato', 'FOODON_00001006', 'mammalian meat food product'),
    ('cream of tartar', 'FOODON_00001256', 'dairy food product'),
    ('cocoa butter', 'FOODON_00001256', 'dairy food product'),
])
def test_broad_economic_alias_is_not_composition_evidence(name, foodon_id, label):
    assert not supported_existing_link(name, foodon_id, label)


@pytest.mark.parametrize('name,allergen', [
    ('walnuts (1/4 cup, chopped, optional)', 'tree_nut'),
    ('milk (skimmed, optional)', 'milk'),
    ('walnuts (chopped and optional)', 'tree_nut'),
])
def test_parenthetical_preparation_does_not_separate_optional_from_food(name, allergen):
    assert _allergen_keyword_evidence(name, allergen)[1] == 'possible'
