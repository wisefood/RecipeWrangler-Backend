from copy import deepcopy
from unittest.mock import Mock

import pytest

from recipe_wrangler.matching.candidate_gate import CompatibilityGatedCatalogue
from recipe_wrangler.matching.contextual_parser import ContextualParser
from recipe_wrangler.matching.hybrid import HybridAnnotator
from recipe_wrangler.matching.ingredient_dictionary import IngredientDictionary


@pytest.fixture
def gate():
    specs=[('pasta','pasta'),('water food product','generic'),('yogurt','generic'),('fruit','generic'),('milk','milk'),('bun','cereal'),('ginger','generic'),('oats','cereal'),
           ('porridge','generic'),('chili pepper','generic'),('chili con carne (dish)','generic')]
    terms=[{'term_id':str(i),'preferred_label':name,'definition':'','curation_status':'',
            'labels':[{'text':name,'type':'preferred'}]+(
                [{'text':'chilli','type':'exact_synonym'}] if name.startswith('chili ') else [])}
           for i,(name,_) in enumerate(specs)]
    parser=ContextualParser(IngredientDictionary({'schema_version':1,'terms':terms}),
        {str(i):{family} for i,(_,family) in enumerate(specs)})
    provider=Mock(errors=[])
    return CompatibilityGatedCatalogue(provider,HybridAnnotator(parser))


@pytest.mark.parametrize('query,bad,good',[
    ('low-fat milk','BUN, LOW-FAT, LOW-FAT MILK','Milk, low fat'),
    ('low-fat milk','Milk, cultured, low fat','Milk, low fat'),
    ('low-fat milk','Milk whole','Milk, low fat'),
    ('low-fat milk','Milk, whole, raw','Milk, low fat'),
    ('low-fat milk','Milk lowfat fat dried','Milk, low fat'),
    ('semi-skimmed milk','Milk whole','Milk, semi-skimmed'),
    ('crushed chillies','Chilli con carne, homemade','Chili pepper, crushed'),
    ('porridge oats','Oat porridge, overnight oats','Oats'),
    ('milk, boiled','Milk, fried','Milk, boiled'),
])
def test_contradictions_are_removed_before_ranking_and_next_candidate_remains(gate,query,bad,good):
    gate.begin_input(query)
    candidates=[{'id':'bad','document':bad},{'id':'good','document':good}]
    gate.catalogue.query.return_value=candidates
    assert [c['id'] for c in gate.query('test',query)]==['good']
    assert gate.audit[0]['blocked']
    assert gate.audit[0]['blocking_rules']
    assert gate.input_facts()['input_text']==query


def test_alias_lookup_cannot_bypass_identity_guard(gate):
    gate.begin_input('low-fat milk')
    gate.catalogue.by_id.return_value={'id':'bad','document':'Bun, low-fat milk'}
    assert gate.by_id('test','bad') is None


def test_curated_alias_cannot_overwrite_a_known_food_head(gate):
    gate.aliases={'milk':'Bun, low-fat milk'}
    gate.begin_input('milk')
    gate.catalogue.by_id.return_value={'id':'bun','document':'Bun, low-fat milk'}
    assert gate.by_id('test','bun') is None
    assert gate.input_facts()['main_ingredient']=='milk'


def test_word_order_and_unstated_properties_do_not_trigger_rejection(gate):
    gate.begin_input('ground ginger')
    gate.catalogue.query.return_value=[{'id':'ginger','document':'Ginger, ground'}]
    assert len(gate.query('test','ground ginger'))==1
    assert gate.input_facts()['facets']['processes']==['ground']
    gate.begin_input('low-fat milk')
    gate.catalogue.query.return_value=[{'id':'milk','document':'Milk, 1% fat, pasteurised'}]
    assert len(gate.query('test','low-fat milk'))==1
    assert not gate.audit[-1]['blocked']
    assert gate.audit[-1]['compatibility']['relation']=='needs_review'


def test_bare_chilli_is_not_inferred_to_be_a_prepared_dish(gate):
    assert gate.annotate('chilli')['main_ingredient']=='chilli'
    assert gate.annotate('Chilli con carne, homemade')['main_ingredient']=='chilli con carne'


def test_inverted_catalogue_name_and_dish_scope(gate):
    gate.begin_input('milk')
    gate.catalogue.query.return_value=[{'id':'mixed','document':'Bun with milk'}]
    assert gate.query('test','milk')==[]
    assert gate.audit[-1]['blocked']


def test_curated_identity_alias_retains_original_property_constraints(gate):
    gate.aliases={'frosty milk':'Milk, low fat'}
    gate.begin_input('frosty milk')
    gate.catalogue.by_id.return_value={'id':'bun','document':'Bun, low-fat milk'}
    assert gate.by_id('test','bun') is None
    gate.aliases={'ice':'Milk'}
    gate.begin_input('ice')
    gate.catalogue.by_id.return_value={'id':'milk','document':'Milk'}
    assert gate.by_id('test','milk')['id']=='milk'
    assert gate.input_facts()['main_ingredient'] is None
    assert gate.audit[-1]['curated_alias_identity_evidence']=='Milk'


def test_existing_query_normalization_reaches_curated_alias_without_losing_input(gate):
    gate.aliases={'bicarbonate of soda':'Bicarbonate of soda'}
    gate.alias_normalizer=lambda name:'bicarbonate of soda' if name=='bread soda' else name
    gate.begin_input('bread soda')
    gate.catalogue.by_id.return_value={'id':'soda','document':'Bicarbonate of soda'}
    assert gate.by_id('test','soda') is not None
    assert gate.input_facts()['input_text']=='bread soda'


def test_multiword_ingredient_does_not_hide_a_following_product_head(gate):
    from recipe_wrangler.matching.ingredient_dictionary import DictionaryHit
    parser=gate.annotator.parser
    for name in ['red wine','wine','vinegar']:
        parser.dictionary.index[name].append(DictionaryHit(name,name,name,'preferred','',''))
    assert gate.annotate('red wine vinegar')['main_ingredient']=='vinegar'
    assert gate.annotate('Vinegar, red wine')['main_ingredient']=='vinegar'


def test_sweet_modifier_is_not_the_confectionery_head_when_a_food_follows(gate):
    from recipe_wrangler.matching.ingredient_dictionary import DictionaryHit
    parser=gate.annotator.parser
    for name in ['sweet','gherkin']:
        parser.dictionary.index[name].append(DictionaryHit(name,name,name,'preferred','',''))
    assert gate.annotate('sweet green gherkins')['main_ingredient']=='gherkin'
    assert gate.annotate('sweet')['main_ingredient']=='sweet'


def test_gate_failure_cannot_be_swallowed_as_an_empty_candidate_pool(gate):
    gate.begin_input('milk')
    gate.catalogue.query.return_value=[{'document':'Milk'}]
    with pytest.raises(KeyError):
        gate.query('test','milk')
    assert gate.errors[-1]['reason']=='gate_failure'


@pytest.mark.parametrize('name',['baking potato','frying potato','roasting potato'])
def test_nominal_cooking_use_does_not_assert_a_cooked_ingredient(gate,name):
    from recipe_wrangler.matching.ingredient_dictionary import DictionaryHit
    gate.annotator.parser.dictionary.index['potato'].append(DictionaryHit('potato','potato','potato','preferred','',''))
    query=gate.annotate(name)
    assert query['facets']['preparation_state'] is None
    assert not query['facets']['processes']
    assert any(m['polarity']=='intended' for m in query['modifiers'])
    gate.begin_input(name)
    gate.catalogue.query.return_value=[{'id':'potato','document':'Potato, raw'}]
    assert len(gate.query('test',name))==1
    assert gate.annotate('potato, baked')['facets']['preparation_state']=='cooked'


@pytest.mark.parametrize('query,label',[
    ('garlic cloves','Garlic, raw'),('spinach leaves','Spinach, raw'),
    ('asparagus spears','Asparagus, raw'),('greek yoghurt','Yoghurt, Greek, full fat'),
])
def test_partial_head_overlap_does_not_reject_supported_ingredient_subtypes(gate,query,label):
    from recipe_wrangler.matching.ingredient_dictionary import DictionaryHit, normalize_label
    import re
    # Isolate the gate: both complete dictionary heads are recognized but differ.
    base=re.split(r',',label)[0].casefold()
    for name in [normalize_label(query),normalize_label(base)]:
        gate.annotator.parser.dictionary.index[name].append(DictionaryHit(name,name,name,'preferred','',''))
    gate.begin_input(query)
    gate.catalogue.query.return_value=[{'id':'supported','document':label}]
    assert len(gate.query('test',query))==1


def test_exact_dictionary_synonym_resolves_identity_without_overriding_cooking(gate):
    from recipe_wrangler.matching.ingredient_dictionary import DictionaryHit
    for name in ['cornflour','corn starch']:
        gate.annotator.parser.dictionary.index[name].append(DictionaryHit('starch','corn starch',name,'exact_synonym','',''))
    gate.begin_input('cornflour')
    gate.catalogue.query.return_value=[{'id':'starch','document':'Corn starch'}]
    assert len(gate.query('test','cornflour'))==1
    gate.begin_input('cornflour, boiled')
    gate.catalogue.query.return_value=[{'id':'starch','document':'Corn starch, raw'}]
    assert gate.query('test','cornflour, boiled')==[]


def test_alternatives_are_checked_against_selected_branch_and_preserved(gate):
    gate.begin_input('milk or ginger')
    gate.catalogue.query.return_value=[{'id':'ginger','document':'Ginger'},{'id':'milk','document':'Milk'}]
    assert [r['id'] for r in gate.query('test','milk')]==['milk']
    assert gate.semantic_input()['main_ingredient']=='milk'
    original=gate.input_facts()
    assert original['input_text']=='milk or ginger'
    assert original['main_ingredient'] is None
    assert original['facets']['base_identity'] is None
    assert [a['text'] for a in original['alternatives']]==['milk','ginger']
    assert original['alternatives'][0]['annotation']['main_ingredient']=='milk'


def test_alternative_does_not_discard_explicit_nutrition_qualifiers(gate):
    gate.begin_input('low-fat milk or milk')
    assert gate.semantic_input()['facets']['fat_level']=='low'
    assert gate.semantic_input()['inherited_qualifiers']==[]


def test_unselected_alternative_cannot_supply_nutrition_or_state_qualifiers(gate):
    gate.begin_input('milk or low-fat milk, boiled')
    first=gate.annotate('milk')
    before=deepcopy(first)
    selected=gate.semantic_input()
    assert selected['facets']==first['facets']
    assert selected['facets']['fat_level'] is None
    assert selected['facets']['processes']==[]
    assert first==before


def test_fruit_suffix_does_not_steal_the_kiwi_head(gate):
    from recipe_wrangler.matching.ingredient_dictionary import DictionaryHit
    for name in ['kiwi','fruit']:
        gate.annotator.parser.dictionary.index[name].append(DictionaryHit(name,name,name,'preferred','',''))
    assert gate.annotate('Kiwi fruit yellow')['main_ingredient']=='kiwi'


def test_prepared_sauce_head_is_not_replaced_by_a_named_component(gate):
    from recipe_wrangler.matching.ingredient_dictionary import DictionaryHit
    for name,family in [('salsa','sauce'),('tomato','generic')]:
        gate.annotator.parser.dictionary.index[name].append(DictionaryHit(name,name,name,'preferred','',''))
        gate.annotator.parser.families[name]={family}
    assert gate.annotate('Salsa tomato dip')['main_ingredient']=='salsa'
    assert gate.annotate('tomato salsa')['main_ingredient']=='salsa'


def test_boiled_water_is_water_and_cannot_match_a_boiled_vegetable(gate):
    from recipe_wrangler.matching.ingredient_dictionary import DictionaryHit
    gate.annotator.parser.dictionary.index['parsnip'].append(DictionaryHit('parsnip','parsnip','parsnip','preferred','',''))
    query=gate.annotate('boiled water')
    assert query['main_ingredient']=='water'
    assert query['facets']['processes']==['boiled']
    gate.begin_input('boiled water')
    gate.catalogue.query.return_value=[{'id':'wrong','document':'Parsnip, boiled'},
                                     {'id':'unlisted','document':'Unlisted marrow, boiled in unsalted water'},
                                     {'id':'water','document':'Water, municipal'}]
    assert [r['id'] for r in gate.query('test','boiled water')]==['water']
    assert gate.annotate('watercress')['main_ingredient'] is None


def test_unspecified_berry_mixture_cannot_be_replaced_by_arbitrary_species(gate):
    gate.begin_input('summer berries')
    gate.catalogue.query.return_value=[{'id':'species','document':'Rowan-berries, raw'},
                                     {'id':'mix','document':'Mixed berries'}]
    assert [r['id'] for r in gate.query('test','berries')]==['mix']


def test_explicitly_requested_fruit_component_is_not_an_unrelated_mixture(gate):
    gate.begin_input('fruit flavoured yoghurt')
    gate.catalogue.query.return_value=[{'id':'fruit','document':'Yogurt with fruit'}]
    assert len(gate.query('test','yogurt'))==1
    gate.begin_input('yogurt')
    assert gate.query('test','yogurt')==[]


def test_based_phrase_preserves_the_final_product_head(gate):
    from recipe_wrangler.matching.ingredient_dictionary import DictionaryHit
    for name in ['tomato','sauce']:
        gate.annotator.parser.dictionary.index[name].append(DictionaryHit(name,name,name,'preferred','',''))
    assert gate.annotate('tomato based pasta sauce')['main_ingredient']=='sauce'


def test_leftover_is_not_a_separate_food_identity(gate):
    from recipe_wrangler.matching.ingredient_dictionary import DictionaryHit
    for name in ['leftover','lamb']:
        gate.annotator.parser.dictionary.index[name].append(DictionaryHit(name,name,name,'preferred','',''))
    q=gate.annotate('leftover roasted lamb')
    assert q['main_ingredient']=='lamb'
    assert q['facets']['processes']==['roasted']


def test_specialist_meat_part_is_blocked_before_ranking(gate):
    gate.begin_input('pork')
    gate.catalogue.query.return_value=[{'id':'head','document':'Pork, head'},
                                     {'id':'meat','document':'Pork, raw'}]
    assert 'head' not in [c['id'] for c in gate.query('test','pork')]
    assert any(r['rule_id']=='identity.unsupported_specialist_part' for r in gate.audit[0]['blocking_rules'])


def test_alternative_retains_shared_meat_fat_evidence(gate):
    gate.begin_input('lean minced beef or lamb')
    original=gate.annotate('lean minced beef or lamb')
    # Use the actual parser evidence; this test also catches dropped lean extraction.
    assert original['facets']['meat_fat_description'] is not None
    selected=gate.semantic_input()
    assert selected['facets']['meat_fat_description']==original['facets']['meat_fat_description']
    assert selected['inherited_qualifiers']==[]


@pytest.mark.parametrize('text,expected',[
    ('lean beef','lean'),('extra-lean minced beef','extra lean'),
    ('beef lean and fat','lean and fat'),('not lean beef',None),
])
def test_explicit_meat_fat_preserves_wording_and_negation(gate,text,expected):
    assert gate.annotate(text)['facets']['meat_fat_description']==expected


@pytest.mark.parametrize('query,bad,good',[
    ('white fish fillets','Surimi white fish','White fish fillets, raw'),
    ('bananas','Bananas, red','Bananas'),
    ('pears, canned','Pear, canned in syrup','Pear, canned in water'),
    ('vegetarian dumplings','Spoon dumplings','Vegetarian dumplings'),
    ('wholemeal pasta','Pasta, white, dried, raw','Wholemeal pasta, dried'),
])
def test_material_product_refinement_needs_input_support(gate,query,bad,good):
    gate.begin_input(query)
    gate.catalogue.query.return_value=[{'id':'bad','document':bad},{'id':'good','document':good}]
    assert [c['id'] for c in gate.query('test',query)]==['good']


@pytest.mark.parametrize('name',['Surimi white fish','Bananas, red','Pear, canned in syrup','Spoon dumplings'])
def test_explicitly_requested_product_refinements_remain_available(gate,name):
    gate.begin_input(name)
    gate.catalogue.by_id.return_value={'id':'exact','document':name}
    assert gate.by_id('test','exact') is not None


def test_dried_occurrence_blocks_raw_alias_without_changing_search_name(gate):
    from recipe_wrangler.matching.occurrence_facts import recover_occurrence_facts
    gate.aliases={'ginger':'Ginger, raw'}
    observed=recover_occurrence_facts('ginger','ginger, dried',gate.annotate)
    gate.begin_input('ginger',observed)
    gate.catalogue.by_id.return_value={'id':'raw','document':'Ginger, raw'}
    assert gate.by_id('test','raw') is None
    assert gate.input_facts()['input_text']=='ginger'


def test_explicit_wholegrain_takes_precedence_over_white_colour(gate):
    gate.begin_input('wholemeal pasta')
    gate.catalogue.by_id.return_value={'id':'wholegrain','document':'Wholemeal pasta, white'}
    assert gate.by_id('test','wholegrain') is not None


def test_facet_retrieval_finds_supported_wholegrain_without_quantity_text(gate):
    from recipe_wrangler.matching.occurrence_facts import recover_occurrence_facts
    gate.facet_retrieval=True
    gate.begin_input('pasta',recover_occurrence_facts('pasta','400g wholemeal pasta',gate.annotate))
    gate.catalogue.query.side_effect=[
        [{'id':'white','document':'Pasta, white, raw'}],
        [{'id':'whole','document':'Wholemeal pasta, raw'}]]
    assert [r['id'] for r in gate.query('test','pasta')]==['whole']
    assert gate.catalogue.query.call_args_list[1].args==('test','pasta wholemeal')
    assert gate.input_facts()['input_text']=='pasta'
