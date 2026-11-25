"""Formatting equivalence never replaces food, amount or preparation evidence."""
import pytest

from recipe_wrangler.matching.source_context import source_line_key


@pytest.mark.parametrize('left,right', [
    ('850ml / 1½ pint chicken stock', '850ml chicken stock'),
    ('450g / 1lb beef', '450g beef'),
    ('3 sticks of celery, chopped', '3 sticks celery, chopped'),
    ('1 bunch of parsley, enough for 1 tablespoon of chopped parsley',
     '1 bunch parsley, enough for 1 tablespoon chopped parsley'),
    ('1½ tablespoons oil', '1 1/2 tablespoons oil'),
    ('30g / 1oz mozzarella', '30g mozzarella'),
    ('80g / 3oz raspberries', '80g raspberries'),
    ('1.5kg / 3 1/2lb pumpkin', '1.5kg pumpkin'),
    ('600ml / 1pint stock', '600ml stock'),
    ('1 litre / 1¾ pints stock', '1 litres stock'),
    ('1 pinch of ground cumin', '1 pinch ground cumin'),
    ('Pinch of paprika', 'Pinch paprika'),
    ('Juice of ½ a lemon', 'Juice 0.5 a lemon'),
    ('small amount of yoghurt', 'small amount yoghurt'),
    ('125g pot of plain yoghurt', '125g pot plain yoghurt'),
    ('50ml / 2 fl oz water', '50ml water'),
    ('Large handful of rocket', 'Large handful rocket'),
    ('2 half slices of kiwi fruit', '2 half slices kiwi fruit'),
    ('1 serving of trail mix', '1 serving trail mix'),
    ('4 new potatoes (160g / 5 1/2oz.)', '4 new potatoes (160g)'),
    ('1 cod fillet (120g / 4 1/2oz.)', '1 cod fillet (120g)'),
    ('2 x 400g cans of chickpeas', '2 x 400g cans chickpeas'),
    ('0.25 of a ripe avocado', '0.25 a ripe avocado'),
    ('1-inch of fresh ginger', '1-inch fresh ginger'),
    ('850mls vegetable stock', '850ml vegetable stock'),
    ('25g / 1oz. plain flour', '25g plain flour'),
    ('1 heaped tsp of paprika', '1 heaped tsp paprika'),
    ('1.5 litress stock', '1.5l stock'),
])
def test_verified_display_variants_have_the_same_key(left, right):
    assert source_line_key(left) == source_line_key(right)


@pytest.mark.parametrize('left,right', [
    ('850ml / 2 pint chicken stock', '850ml chicken stock'),
    ('850ml / 1½ pint chicken stock', '900ml chicken stock'),
    ('850ml / 1½ pint chicken stock', '850ml vegetable stock'),
    ('1 bunch of raw parsley', '1 bunch dried parsley'),
    ('Chicken of the Sea tuna', 'Chicken the Sea tuna'),
    ('450g / 1 pint beef', '450g beef'),
    ('450g / 1lb beef with salt', '450g beef'),
    ('160g / 2oz pasta', '160g pasta'),
    ('100g / 4oz pasta', '100g pasta'),
    ('1 litre / 2 pints stock', '1 litre stock'),
    ('1 pinch of salt', '1 pinch sugar'),
    ('Cream of mushroom soup', 'Cream mushroom soup'),
])
def test_different_or_unverified_text_cannot_bind(left, right):
    assert source_line_key(left) != source_line_key(right)


@pytest.mark.parametrize('left,right',[
    ('½ tsp coriander powder','0.5 teaspoon coriander powder'),
    ('1/4 tablespoon vegetable oil','0.25 tbsp vegetable oil'),
    ('2 slices of wholegrain bread','2 slices wholegrain bread'),
    ('2 leaves of lettuce','2 leaves lettuce'),
    ('450g of potatoes','450 g potatoes'),
    ('1 salmon fillet (100g / 3½oz.)','1 salmon fillet (100g)'),
    ('low-fat yogurt','low fat yogurt'),
    ('salt & pepper to taste','salt and pepper to taste'),
])
def test_grounded_fraction_unit_and_filler_equivalence(left,right):
    assert source_line_key(left)==source_line_key(right)


@pytest.mark.parametrize('left,right',[
    ('½ tsp salt','1 tsp salt'),('2 leaves lettuce','4 leaves lettuce'),
    ('450g potatoes','450g sweet potatoes'),('low-fat yogurt','full-fat yogurt'),
    ('1 salmon fillet (100g / 8oz.)','1 salmon fillet (100g)'),
])
def test_formatting_does_not_hide_material_differences(left,right):
    assert source_line_key(left)!=source_line_key(right)


def seasoning_items(names,lines):
    from types import SimpleNamespace
    return [SimpleNamespace(position=i,name=name,original_text=line,source_evidence=None)
            for i,(name,line) in enumerate(zip(names,lines,strict=True))]


def test_split_seasoning_binds_both_parts_to_one_literal_source():
    from recipe_wrangler.matching.source_context import split_seasoning_bindings
    items=seasoning_items(['rice','salt','black pepper'],['100g rice','Salt and pepper to taste',''])
    bindings=split_seasoning_bindings(items,['100g rice','Salt and pepper to taste'])
    assert [b['source_position'] for b in bindings]==[0,1,1]
    assert bindings[2]['source_text_recovered']
    assert bindings[1]['method']=='explicit_split_seasoning_source_line'
    assert items[2].original_text==''


@pytest.mark.parametrize('names,lines,source',[
    (['salt','sugar'],['salt and pepper',''],['salt and pepper']),
    (['salt','white pepper'],['salt and black pepper',''],['salt and black pepper']),
    (['salt','black pepper'],['salt and pepper',''],['salt and pepper','salt and pepper']),
    (['salt','black pepper'],['salt and pepper',''],['salt and pepper','100g rice']),
    (['salt','black pepper'],['1 tsp salt and pepper',''],['1 tsp salt and pepper']),
])
def test_split_binding_does_not_guess_foods_drop_lines_or_allocate_measured_mixtures(names,lines,source):
    from recipe_wrangler.matching.source_context import split_seasoning_bindings
    assert split_seasoning_bindings(seasoning_items(names,lines),source) is None


def test_missing_food_line_and_split_seasoning_can_be_recovered_together():
    from recipe_wrangler.matching.source_context import split_seasoning_bindings
    items=seasoning_items(['rice','creme fraiche','salt','black pepper'],
                         ['100g rice','','Salt and pepper to taste',''])
    bindings=split_seasoning_bindings(items,
        ['100g rice','2 tbsp crème fraiche','Salt and pepper to taste'])
    assert [b['source_position'] for b in bindings]==[0,1,2,2]
    assert bindings[1]['source_text_recovered']
    assert items[1].original_text==''


@pytest.mark.parametrize('source',[
    ['100g rice','1 tomato','2 tomatoes','Salt and pepper to taste'],
    ['150g rice','1 tomato','Salt and pepper to taste'],
    ['100g rice','1 tomato','2 tomato','Salt and pepper to taste'],
    ['100g rice','1 tomato','1 tsp salt and pepper'],
])
def test_combined_recovery_preserves_coverage_quantity_and_ambiguity_guards(source):
    from recipe_wrangler.matching.source_context import split_seasoning_bindings
    items=seasoning_items(['rice','tomato','salt','black pepper'],
                         ['100g rice','','Salt and pepper to taste',''])
    assert split_seasoning_bindings(items,source) is None


def test_damaged_mixed_spoon_fraction_matches_archive_wording():
    assert source_line_key('1 0.5 teaspoon mild curry powder') == source_line_key('1 ½ teaspoon mild curry powder')
    assert source_line_key('2 0.5 tablespoons olive oil') != source_line_key('2 tablespoons olive oil')
    assert source_line_key('1 0.5 kg flour') != source_line_key('1 ½ kg flour')


def test_optional_salt_and_pepper_is_an_unmeasured_split_but_pinch_is_not():
    from recipe_wrangler.utils.seasoning_quantities import salt_and_pepper_parts
    assert salt_and_pepper_parts('Salt and pepper (optional)') == ('salt', 'black pepper')
    assert salt_and_pepper_parts('Pinch of salt and pepper') is None


def test_single_typo_line_recovered_only_by_elimination():
    from recipe_wrangler.matching.source_context import unambiguous_missing_source_bindings as recover
    items = seasoning_items(['onion', 'parsley'], ['1 onion', ''])
    assert recover(items, ['1 onion', '1 tablespoon fresh parlsey'])[1]['source_position'] == 1
    assert recover(items, ['1 onion', '1 tablespoon fresh thyme']) is None
