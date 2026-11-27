"""Source mass annotations are quantities, not extra food-identity words."""
import pytest

from recipe_wrangler.matching.quantities import resolve_quantity


@pytest.mark.parametrize('name,line,weight,expected', [
    ('reduced-fat ricotta cheese', '⅔ cup (150g) reduced-fat ricotta cheese', 88.44, 150),
    ('flour', '1 cup (125g) flour', 140, 125),
    ('milk', '½ cup (120g) milk', 124, 120),
    ('potatoes', '4 new potatoes (160g / 5 1/2oz.)', 692, 160),
    ('new potatoes', '4 medium new potatoes (160g / 5 1/2oz.), quartered', 100, 160),
    ('baking potato', '1 medium baking potato (about 240 g)', 173, 240),
    ('cod fillet', '1 large fresh cod fillet (about 175 g)', 120, 175),
    ('onion', '1 small onion (about 40 g) chopped.', 70, 40),
    ('leeks', '4 medium leeks (about 650 g) washed and chopped.', 356, 650),
    ('potatoes', '2 medium potatoes (about 300 g) peeled and diced.', 176, 300),
    ('red chilli', '1/4 red chilli (about 10g), deseeded and thinly sliced', 3.5, 10),
    ('spring onion', '1 spring onion (about 10 g), finely sliced', 15, 10),
    ('chicken fillets', '2 medium chicken fillets (260g / 9 oz.), diced', 300, 260),
])
def test_original_total_mass_survives_identity_validation(name, line, weight, expected):
    result = resolve_quantity(name, weight, line)
    assert result['weight_g'] == expected
    assert result['quantity_status'] == 'explicit_source_mass'
    assert result['quantity_evidence']['source_line'] == line


@pytest.mark.parametrize('name,line', [
    ('flour', '1 cup flour and 2 tablespoons butter (30g)'),
    ('flour', '1 cup (125g) flour or 1 cup (100g) oats'),
    ('flour', '1 cup (125g each) flour'),
    ('spaghetti', '200g spaghetti bolognese, homemade'),
    ('potatoes', '4 potatoes (160g each)'),
    ('potatoes', '2-4 potatoes (160g)'),
    ('tomatoes', '2 cans tomatoes (400g)'),
    ('chicken', '2 chicken pies (about 500g)'),
    ('potatoes', '2 potatoes with cheese (about 300g)'),
    ('potatoes', '2 potatoes (about 300g with cheese)'),
    ('potatoes', '2 potatoes (about 300g), diced with butter'),
    ('onion', '1 onion or 40g shallots'),
])
def test_scope_or_other_food_is_not_removed_to_force_mass_recovery(name, line):
    result = resolve_quantity(name, 17, line)
    assert result['weight_g'] == 17
    assert result['quantity_evidence'] is None


def test_deliberate_zero_is_preserved_even_with_source_mass():
    assert resolve_quantity('potatoes', 0, '4 new potatoes (160g / 5 1/2oz.)')['weight_g'] == 0
