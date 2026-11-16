"""Batch keyword writes must share runtime context without touching recipe claims."""
from contextlib import nullcontext
from types import SimpleNamespace
import pytest
from scripts.neo4j import tag_allergens as batch
from recipe_wrangler.repositories.neo4j_recipes import _allergen_keyword_hits


class RecordingSession:
    def __init__(self, rows):
        self.rows = rows
        self.calls = []
        self.writes = []

    def run(self, query, **params):
        assert query.timeout == 60
        text = str(query)
        self.calls.append((text, params))
        assert 'DELETE' not in text and 'Recipe' not in text
        if 'RETURN elementId(i)' in text:
            assert params['limit'] == 100
            return [r for r in self.rows if r['element_id'] > params['after']][:params['limit']]
        assert len(params['ingredient_hits']) <= 100
        self.writes.extend(params['ingredient_hits'])
        return SimpleNamespace(single=lambda: {'tagged': len(params['ingredient_hits'])})


def driver_for(session):
    return SimpleNamespace(session=lambda: nullcontext(session))


@pytest.mark.parametrize('allergen,names,expected', [
    ('milk', ['coconut milk', 'milk with almond milk', 'without milk, contains whey', 'sauce without milk'], [1, 2]),
    ('molluscs', ['oyster mushrooms', 'oyster mushrooms and oysters', 'oyster sauce'], [1, 2]),
    ('wheat', ['almond flour', 'wheat flour', 'gluten-free flour'], [1]),
    ('sesame', ['sauce without sesame', 'sesame seeds'], [1]),
])
def test_batch_hits_preserve_runtime_context(allergen, names, expected):
    rows = [{'element_id': str(n), 'name': name} for n, name in enumerate(names)]
    session = RecordingSession(rows)
    assert batch._tag_by_keyword(driver_for(session), allergen) == len(expected)
    assert session.writes == [
        {'element_id': str(n), 'name': names[n], 'hits': _allergen_keyword_hits(names[n], allergen), 'presence': 'contains'} for n in expected
    ]


def test_batch_uses_unique_node_identity_and_bounded_pages():
    # Distinct nodes with equal names cannot multiply one another's write matches.
    rows = [{'element_id': f'{n:04}', 'name': 'milk'} for n in range(205)]
    session = RecordingSession(rows)
    assert batch._tag_by_keyword(driver_for(session), 'milk') == 205
    assert len(session.writes) == 205
    assert len({w['element_id'] for w in session.writes}) == 205
    reads = [p for q, p in session.calls if 'RETURN elementId(i)' in q]
    assert [p['after'] for p in reads] == ['', '0099', '0199', '0204']
    writes = [p for q, p in session.calls if 'MERGE (al:Allergen' in q]
    assert [len(p['ingredient_hits']) for p in writes] == [100, 100, 5]


def test_empty_batch_never_writes():
    session = RecordingSession([])
    assert batch._tag_by_keyword(driver_for(session), 'milk') == 0
    assert session.writes == [] and len(session.calls) == 1


def test_possible_batch_evidence_has_no_unconditional_positive_edge():
    session = RecordingSession([{'element_id': 'a', 'name': 'dried apricots'}])
    assert batch._tag_by_keyword(driver_for(session), 'sulphites') == 1
    assert session.writes == [{'element_id': 'a', 'name': 'dried apricots', 'hits': ['dried apricot'], 'presence': 'possible'}]
    query = next(q for q, p in session.calls if 'MERGE (al:Allergen' in q)
    assert "FOREACH (_ IN CASE candidate.presence WHEN 'contains' THEN [1] ELSE [] END" in query
    assert 'ELSE candidate.presence END' in query


def test_nonadvancing_page_stops_before_duplicate_writes():
    class BrokenPage(RecordingSession):
        def run(self, query, **params):
            if 'RETURN elementId(i)' in str(query):
                return [{'element_id': 'a', 'name': 'milk'}]
            return super().run(query, **params)
    session = BrokenPage([])
    with pytest.raises(RuntimeError, match='pagination did not advance'):
        batch._tag_by_keyword(driver_for(session), 'milk')
    assert len(session.writes) == 1


def test_gluten_and_coeliac_roots_use_primary_foodon_cereal_ids():
    from recipe_wrangler.utils.food_ontology import ALLERGEN_DETECTION_RULES, CLASSIFICATION_VERSION
    from scripts.neo4j.enrich_fato_foodon import GROUP_RULES
    # Verified against the 2025-12-30 and 2026-09-20 primary OWL releases.
    cereals = {'FOODON_00001141', 'FOODON_00001191', 'FOODON_00001190', 'FOODON_00001189'}
    non_cereals = {'FOODON_00001275', 'FOODON_00001217', 'FOODON_00001272', 'FOODON_00001254'}
    roots = set(ALLERGEN_DETECTION_RULES['gluten']['roots'])
    assert cereals <= roots and not roots & non_cereals
    coeliac = set(GROUP_RULES['coeliac']['blocking_roots'])
    assert cereals - {'FOODON_00001189'} <= coeliac and not coeliac & non_cereals
    assert CLASSIFICATION_VERSION == 'fato-foodon-v8'
