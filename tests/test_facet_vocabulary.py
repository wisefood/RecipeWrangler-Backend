from copy import deepcopy

from recipe_wrangler.matching.facet_vocabulary import FacetVocabulary
from recipe_wrangler.matching.hybrid import HybridAnnotator
from recipe_wrangler.matching.contextual_parser import ContextualParser
from recipe_wrangler.matching.ingredient_dictionary import IngredientDictionary


def vocabulary():
    def term(code,label,parents=()):
        return {'code':code,'label':label,'status':'APPROVED','parent_codes':list(parents)}
    return FacetVocabulary({'catalogue_version':'test','facets':[
        {'code':'source','terms':[term('chicken','Gallus gallus (chicken) (as animal)'),
                                 term('duck','Duck (as animal)'),
                                 term('fish','Bombay duck (as animal)')]},
        {'code':'part','terms':[term('liver','Liver (as part-nature)',['A069L']),
                               term('spleen','Spleen (as part-nature)',['A069L']),
                               term('sauce','White sauces (as part-nature)',['A06HE'])]},
        {'code':'process','terms':[term('simmer','Simmering')]},
    ]})


def parser():
    return ContextualParser(IngredientDictionary({'schema_version':1,'provenance':{},'terms':[
        {'term_id':'test:'+word,'preferred_label':word,'definition':'','curation_status':'',
         'labels':[{'text':word,'type':'preferred'}]} for word in ['liver','spleen','white sauces']]}),{})


def test_export_drives_recognition_and_preserves_codes_and_offsets():
    v=vocabulary()
    text='chicken spleen, simmering'
    hits=v.recognize(text)
    assert {h['code'] for h in hits}=={'chicken','spleen','simmer'}
    assert all(text[h['start']:h['end']]==h['quote'] for h in hits)
    assert all(h['status']=='lexical_candidate_not_assertion' for h in hits)


def test_source_part_attachment_runs_before_generic_family_gate():
    a=HybridAnnotator(parser(),vocabulary()).annotate('chicken liver, raw')
    assert a['main_ingredient']=='chicken'
    assert a['facets']['biological_source']=='chicken'
    assert a['facets']['food_part']=='liver'
    assert a['facets']['preparation_state']=='raw'
    assert a['unresolved']==[]
    assert any(e.get('foodex2_code')=='liver' for e in a['evidence'])
    assert any(e.get('premise_codes')==['chicken','liver'] and e['quote']=='chicken liver' for e in a['evidence'])
    # Spleen is intentionally absent from the old _PARTS list.
    b=HybridAnnotator(parser(),vocabulary()).annotate('duck spleen, raw')
    assert b['facets']['food_part']=='spleen'
    assert b['facets']['biological_source']=='duck'


def test_long_names_and_nonanatomical_descriptors_are_not_flattened():
    v=vocabulary()
    assert [h['code'] for h in v.recognize('Bombay duck') if h['facet']=='source']==['fish']
    a=HybridAnnotator(parser(),v).annotate('chicken white sauces')
    assert a['facets']['biological_source'] is None
    assert a['facets']['food_part'] is None


def test_connectors_and_negation_do_not_become_producer_relations():
    for text in ['liver with chicken','liver for chicken','not chicken liver','liver and chicken']:
        a=HybridAnnotator(parser(),vocabulary()).annotate(text)
        assert a['facets']['biological_source'] is None,text


def test_ambiguous_source_alias_is_not_silently_selected():
    v=vocabulary()
    v.index['chicken'].append((('chicken',),'source','ambiguous'))
    v.terms[('source','ambiguous')]=deepcopy(v.terms[('source','chicken')])
    a=HybridAnnotator(parser(),v).annotate('chicken liver, raw')
    assert a['facets']['biological_source'] is None
    assert 'ambiguous_vocabulary_source_part' in a['review_reasons']


def test_generic_parenthesis_is_not_a_source_alias():
    v=FacetVocabulary({'catalogue_version':'test','facets':[{'code':'source','terms':[
        {'code':'fish','label':'Patagonotothen (generic) (as animal)','status':'APPROVED','parent_codes':[]}]}]})
    assert v.recognize('generic liver')==[]
    assert v.recognize('Patagonotothen')[0]['value']=='patagonotothen'
