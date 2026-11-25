"""Regression checks for source-grounded repairs of unmatched basic foods."""
from copy import deepcopy
from unittest.mock import Mock

import pytest

from recipe_wrangler.matching.occurrence_facts import stated_oil_example
from recipe_wrangler.matching.reviewed_aliases import reviewed_record_compatible
from recipe_wrangler.matching.runtime import MatchingEngine
from recipe_wrangler.tools.nutrition_match import same_food_name


@pytest.mark.parametrize('name,expected',[
    ('lettice','lettuce'),('parmesan shavings','parmesan'),
    ('low-fat parmesan shavings','low-fat parmesan'),
])
def test_normalization_preserves_nutrition_qualifiers(name,expected):
    assert same_food_name(name)==expected


def test_oil_example_requires_an_explicit_source_species():
    line='1 tsp unsaturated oil like rapeseed or sunflower oil'
    example=stated_oil_example('unsaturated oil',line)
    assert example['name']=='rapeseed oil'
    assert line[example['start']:example['end']]==example['quote']
    assert stated_oil_example('unsaturated oil','1 tsp unsaturated oil') is None
    assert stated_oil_example('olive oil',line) is None
    assert stated_oil_example('unsaturated oil','unsaturated oil like engine oil') is None


def test_oil_example_keeps_original_facts_and_discloses_choice(monkeypatch):
    from recipe_wrangler.matching import runtime
    engine=object.__new__(MatchingEngine)
    selected={'decision':{'reason':'alias'},'source_facts':{'label':'Oil, rapeseed'},
        'calculation':{'kind':'retained_legacy_estimate','assumptions':[]}}
    engine.resolve=Mock(return_value=selected)
    engine.annotator=Mock()
    original={'input_text':'unsaturated oil','main_ingredient':None}
    monkeypatch.setattr(runtime,'recover_occurrence_facts',lambda *a:deepcopy(original))
    result=engine._resolve('unsaturated oil','irish','unsaturated oil like rapeseed or sunflower oil')
    assert result['observed_facts']==original
    assert result['retrieval_name']=='rapeseed oil'
    assert result['calculation']['assumptions'][-1]['policy']=='explicit_source_oil_example'
    assert selected['calculation']['assumptions']==[]


@pytest.mark.parametrize('name,original,label,accepted',[
    ('dairy-free margarine','','MARGARINE, LACTOSE-FREE',False),
    ('dairy-free margarine','','Margarine, vegan',True),
    ('evaporated fat-free milk','','Milk, evaporated',False),
    ('evaporated fat-free milk','','Milk, evaporated, nonfat',True),
    ('cheese spread triangle','1 low-fat cheese spread triangle','Cheese, processed, triangles',False),
    ('cheese spread triangle','1 low-fat cheese spread triangle','Cheese spread, plain, reduced fat',True),
    ('chickpeas','200g chickpeas','Chickpeas (dry)',False),
    ('chickpeas','200g dry chickpeas','Chickpeas (dry)',True),
])
def test_reviewed_proxy_cannot_erase_defining_requirements(name,original,label,accepted):
    assert reviewed_record_compatible(name,original,{'label':label}) is accepted
