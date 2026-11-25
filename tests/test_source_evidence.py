import pytest
from recipe_wrangler.matching.source_evidence import stated_weight, unique_source_line, unique_source_lines


def test_source_occurrences_cannot_borrow_the_same_line():
    assert unique_source_lines(['milk','milk powder'],['10g milk powder'])==['','']
    assert unique_source_lines(['milk','milk'],['100g milk'])==['','']


def test_repeated_source_lines_do_not_collapse_into_one_occurrence_binding():
    assert unique_source_lines(['milk'],['100g milk','100g milk'])==['']


def test_unique_full_labels_and_order_survive_source_binding():
    names=['Dried egg pasta, cooked, unsalted','Salt']
    lines=['6g Salt','600g Dried egg pasta, cooked, unsalted']
    assert unique_source_lines(names,lines)==[lines[1],lines[0]]
    assert unique_source_lines([],lines)==[]


def test_parser_node_does_not_attach_one_source_line_twice(monkeypatch):
    import importlib
    from types import SimpleNamespace
    module=importlib.import_module('recipe_wrangler.tools.parse_recipe_tool')
    monkeypatch.setattr(module,'parse_recipe_tool',SimpleNamespace(invoke=lambda payload:{
        'title':'Test','ingredient_names':['milk','milk powder'],'measurements':['',''],
        'directions':[],'total_time':1,'serves':1}))
    state=module.RecipeState(raw_recipe='10g milk powder')
    result=module.Recipe_Parser_Node(state)
    assert result.ingredient_original_texts==['','']


def test_profiler_node_does_not_reattach_one_source_line_twice(monkeypatch):
    import importlib
    module=importlib.import_module('recipe_wrangler.tools.recipe_profiling_tool')
    received={}
    class Captured(Exception):pass
    def capture(payload):
        received.update(payload)
        raise Captured
    monkeypatch.setattr(module,'Recipe_Profiling_Tool',capture)
    state=module.RecipeState(title='Test',region='EU',ingredient_names=['milk','milk powder'],
        measurements=['100g','10g'],weights=[100,10],serves=1,
        ingredient_original_texts=['10g milk powder'])
    with pytest.raises(Captured):module.Recipe_Profiling_Node(state)
    assert received['original_texts']==['','']


@pytest.mark.parametrize('grams',[600,1300])
def test_retained_pasta_source_mass_replaces_stale_input_identically_in_each_region(grams):
    from unittest.mock import Mock
    from recipe_wrangler.matching.runtime import MatchingEngine
    engine=object.__new__(MatchingEngine);engine.release_id='test'
    engine.resolve=Mock(return_value={'decision':{'confidence':'none'},'calculation':{
        'kind':'unresolved','values_per_100g':{},'assumptions':[],'blockers':['test_unknown']},
        'source_facts':None,'observed_facts':{'facets':{}},'leading_alternatives':[]})
    name='Dried egg pasta, cooked, unsalted'
    originals=unique_source_lines([name],[f'{grams}g {name}'])
    results=[engine.calculate(title='Test',ingredient_names=[name],weights=[5],
        original_texts=originals,source=region) for region in ['eu','irish','hungarian','slovenian']]
    assert [r['details'][0]['weight_g'] for r in results]==[grams]*4
    assert all(r['details'][0]['original_weight_g']==5 for r in results)
    assert all(r['details'][0]['quantity_status']=='explicit_source_mass' for r in results)
    assert len({r['quantity_input_hash'] for r in results})==1


@pytest.mark.parametrize('kind',['beef','chicken','vegetable'])
def test_reconstituted_stock_catalogue_label_is_one_liquid_with_estimated_density(kind):
    from recipe_wrangler.matching.quantities import resolve_quantity
    name=f'Broth or stock, {kind}, dehydrated and reconstituted'
    result=resolve_quantity(name,5,f'100ml {name}')
    assert result['weight_g']==100
    assert result['quantity_status']=='source_volume_estimate'
    assert result['quantity_evidence']['density_g_per_ml']==1
    assert result['quantity_evidence']['source_line']==f'100ml {name}'


@pytest.mark.parametrize('name,line',[
    ('broth','100ml broth or milk'),
    ('stock powder','100ml stock powder or water'),
    ('Broth or stock, beef, dehydrated and reconstituted',
     '100ml Broth or stock, beef, dehydrated and reconstituted or 100ml milk'),
    ('Broth or stock, beef, dehydrated and reconstituted',
     '100ml Broth or stock, beef, dehydrated'),
])
def test_stock_exception_cannot_hide_alternatives_or_unreconstituted_powder(name,line):
    assert stated_weight(name,line) is None


@pytest.mark.parametrize('name,line,grams',[
    ('mushrooms','5 (100g) mushrooms, sliced',100),
    ('lamb cutlets','12 lean french-trimmed lamb cutlets (around 400g)',400),
    ('vegetarian dumplings','4 frozen vegetarian dumplings (about 70g)',70),
    ('coconut milk','½ cup reduced-fat coconut milk',123.6),
    ('mushrooms','100g mushrooms',100),
])
def test_explicit_source_quantities(name,line,grams):
    assert stated_weight(name,line)['weight_g']==pytest.approx(grams)


@pytest.mark.parametrize('line',['4 dumplings (70g each)','2 cans tomatoes (400g)',
    '100g tuna, drained','1 potato or 100g rice','2-4 mushrooms (100g)',
    '3 canned pear halves','3 semi-dried tomatoes'])
def test_ambiguous_quantities_are_not_invented(line):
    assert stated_weight('food',line) is None


def test_unique_source_join_rejects_multiple_distinct_lines_and_collapses_scrape_duplicates():
    assert unique_source_line('tomatoes',['3 tomatoes','3 tomatoes'])=='3 tomatoes'
    assert unique_source_line('tomatoes',['3 tomatoes','100g tomatoes']) is None


@pytest.mark.parametrize('line,grams',[
    ('1½ cups milk',370.8),('2½ cups milk',618),('3½ cups water',840)])
def test_mixed_unicode_fraction_is_not_concatenated_into_a_large_numerator(line,grams):
    name='water' if 'water' in line else 'milk'
    assert stated_weight(name,line)['weight_g']==pytest.approx(grams)


def test_concatenated_multi_ingredient_quantities_are_not_borrowed():
    assert stated_weight('rolled oats','150g crushed biscuits 1 cup rolled oats')['weight_g']==81
    assert stated_weight('biscuits','150g crushed biscuits 1 cup rolled oats')['weight_g']==150
    assert stated_weight('spaghetti','150g spaghetti bolognese 1 cup milk') is None


def test_section_scrape_does_not_hide_unique_individual_ingredient_line():
    line='¼ cup low-fat plain yoghurt'
    assert unique_source_line('low-fat plain yoghurt',['300g potatoes '+line,line])==line


def test_rolled_oats_source_synonyms_do_not_hide_measured_cup():
    result=stated_weight('rolled oats','150g crushed arrowroot biscuits 1 cup rolled oats oatmeal uncooked')
    assert result['weight_g']==81
    assert result['source_line']=='1 cup rolled oats oatmeal uncooked'


def test_solid_volume_and_powder_basis_are_not_assigned_liquid_density():
    assert stated_weight('vegetable stock','4 teaspoons vegetable stock powder') is None
    assert stated_weight('tomato passata','½ cups) tomato passata') is None
    assert stated_weight('mixed vegetables','8 cups mixed vegetables') is None


def test_size_qualified_item_mass_is_not_assumed_to_be_group_mass():
    assert stated_weight('tortillas','8 mini (25g) wholegrain tortillas') is None
    assert stated_weight('potatoes','4 x large (300g) Sebago potatoes, scrubbed and dried') is None
    assert stated_weight('pita breads','4 x large (75g) wholemeal pita breads') is None
    assert stated_weight('potatoes','4 × large (300g) potatoes') is None
    assert stated_weight('potatoes','4x large (300g) potatoes') is None


def test_structured_dish_mass_is_not_assigned_to_a_component():
    assert stated_weight('spaghetti','200 g Spaghetti bolognese, homemade',
        source_name='Spaghetti bolognese, homemade') is None
    assert stated_weight('spaghetti','200g spaghetti',source_name='spaghetti')['weight_g']==200


def test_bound_lines_are_eliminated_before_retrying_ambiguous_names():
    lines=['150 g Cabbage, red, raw','200 g Cabbage, red, cooked with apple','100 g Duck, raw']
    assert unique_source_lines(['Cabbage','Cabbage, red, raw','Duck'],lines)==[
        '200 g Cabbage, red, cooked with apple','150 g Cabbage, red, raw','100 g Duck, raw']
    lines=['80 g Tofu, soya bean, steamed','30 g Beans, edamame, frozen']
    assert unique_source_lines(['Beans','Tofu'],lines)==['30 g Beans, edamame, frozen','80 g Tofu, soya bean, steamed']
    # A true tie stays unknown: nothing is bound first, so nothing is eliminated.
    assert unique_source_lines(['Dips'],['10 g Dips, sour','20 g Dips, sour'])==['']
