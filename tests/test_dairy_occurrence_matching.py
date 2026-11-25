"""Joined runtime regressions for saved low-fat dairy matching failures."""
from unittest.mock import Mock

import pytest

from recipe_wrangler.matching.runtime import MatchingEngine
from recipe_wrangler.matching.evaluation.production_catalogue import COLLECTIONS
from recipe_wrangler.matching.contextual_parser import ContextualParser
from recipe_wrangler.matching.hybrid import HybridAnnotator
from recipe_wrangler.matching.ingredient_dictionary import IngredientDictionary
from recipe_wrangler.tools.nutrition_completeness import EU


@pytest.mark.parametrize('name,line,labels,expected',[
    ('cheddar cheese','30g of low-fat cheddar cheese, grated',
     ['Cheese Cheddar','Cheese, Cheddar type, half fat'],'1'),
    ('low fat Greek yogurt','2 tbsp low fat Greek yogurt',
     ['Yogurt, low fat, toffee','Yogurt, low fat, plain'],'1'),
    ('low fat toffee yogurt','2 tbsp low fat toffee yogurt',
     ['Yogurt, low fat, toffee','Yogurt, low fat, plain'],'0'),
])
def test_actual_runtime_preserves_source_fat_and_yogurt_flavour(name,line,labels,expected):
    specs=['cheese','cheddar cheese','yogurt']
    terms=[{'term_id':word,'preferred_label':word,'definition':'','curation_status':'',
            'labels':[{'text':word,'type':'preferred'}]} for word in specs]
    annotator=HybridAnnotator(ContextualParser(IngredientDictionary({'schema_version':1,'terms':terms}),{}))
    engine=MatchingEngine('offline-fixture',rows=[],manifest={'aliases':[]},annotator=annotator,foodex=Mock())
    candidates=[]
    for i,label in enumerate(labels):
        key=str(i)
        candidate={'id':key,'document':label,'distance':0.05+i*0.01,'metadata':{'eu_id':key,'food_name':label}}
        candidates.append(candidate)
        # Complete synthetic nutrients exercise linkage; these are not measured profiles.
        row={'id':key,'nutrients':{field:{'value':10,'unit':'kcal' if metric=='energy_kcal'
            else 'mg' if metric=='sodium_mg' else 'g'} for metric,field in EU.items()}}
        engine.ids[COLLECTIONS['eu'],key]={'annotation':{},'candidate':candidate,'nutrient_row':row}
    def search(collection,query,legacy=False):
        if collection!=COLLECTIONS['eu']:return []
        # The source-only qualifier must reach retrieval, not just the final gate.
        if name=='cheddar cheese' and not query.startswith('low fat '):return candidates[:1]
        return candidates
    engine.search=Mock(side_effect=search)
    result=engine.resolve(name,'eu',line)
    assert result['decision']['match']['id']==expected
    assert result['calculation']['kind']!='unresolved'
    assert result['observed_facts']['input_text']==name
    assert result['observed_facts']['facets']['fat_level']=='low'
    assert result['calculation']['observed_facts']==result['observed_facts']
