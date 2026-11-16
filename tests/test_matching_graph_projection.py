from scripts.composition.project_matching_graph import variants


def test_variant_projection_keeps_cooking_separate_from_handling_and_recipe_state():
    row={'release_id':'r','collection':'eu','candidate_id':'1',
        'candidate':{'document':'Chicken breast, fried'},'annotation':{'native_annotation':None,
        'text_annotation':{'facets':{'base_identity':'chicken','biological_source':'chicken',
            'food_part':'breast','preparation_state':'cooked','processes':['fried','chopped']}},'blockers':[]}}
    variant=next(variants([row]))
    assert variant['identity']=='chicken'
    assert any(p['kind']=='cooking_method' and p['value']=='fried' for p in variant['properties'])
    assert any(p['kind']=='processes' and p['value']=='chopped' for p in variant['properties'])


def test_chicken_breast_frame_retains_part_without_inventing_cooking():
    from recipe_wrangler.matching.graph_projection import ingredient_frame
    a={'foods':[{'text':'chicken'}],'main_ingredient':'chicken',
        'review_reasons':['ambiguous_ontology_candidates'],
        'facets':{'base_identity':'chicken','food_part':'breast','biological_source':'chicken'}}
    frame=ingredient_frame('chicken breast',a,'r')
    assert frame['identity']=='chicken'
    assert any(p['kind']=='food_part' and p['value']=='breast' for p in frame['properties'])
    assert not any(p['kind']=='cooking_method' for p in frame['properties'])
    a['review_reasons'].append('unexplained_text')
    assert ingredient_frame('chicken breast with sauce',a,'r') is None


def test_occurrence_does_not_inherit_fried_state_from_selected_source():
    from recipe_wrangler.matching.graph_projection import occurrence_projection
    run={'run_id':'run','recipe_id':'recipe','nutrition_source':'eu','release_id':'r'}
    detail={'position':0,'ingredient':'chicken','observed_facts':{'facets':{'biological_source':'chicken'}},
        'quantity_input_hash':'shared-hash','quantity_version':'shared-v1','weight_g':100,
        'selected_composition_facts':{'facets':{'processes':['fried']}}}
    row=occurrence_projection(run,detail,{'collection':'eu','candidate_id':'1'})
    assert row['original_id']=='recipe:0'
    assert row['quantity_input_hash']=='shared-hash'
    assert row['weight_g']==100
    assert not any(p['kind']=='cooking_method' for p in row['properties'])


def test_composition_without_established_identity_still_has_an_addressable_variant():
    row={'release_id':'r','collection':'eu','candidate_id':'unknown',
        'candidate':{'document':'Unclassified product'},'annotation':{'native_annotation':None,
        'text_annotation':{'facets':{},'main_ingredient':None},'blockers':[]}}
    variant=next(variants([row]))
    assert variant['composition_id']=='unknown'
    assert variant['identity_id'] is None
