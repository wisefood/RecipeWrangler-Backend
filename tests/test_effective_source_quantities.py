"""Gross labelled mass and an estimated edible mass remain distinct."""
from copy import deepcopy

from recipe_wrangler.matching.quantities import effective_quantity, resolve_quantity, valid_quantity_adjustment


def test_drained_source_pack_mass_is_not_reported_as_measured_edible_mass():
    line = '2 x 400g cans no-added salt cannellini beans, rinsed, drained'
    original = resolve_quantity('cannellini beans', 5.0, line)
    before = deepcopy(original)
    result = effective_quantity(original, line)
    assert original == before
    assert result['weight_g'] == 480.0
    assert result['quantity_evidence'] is None
    assert result['quantity_status'] == 'adjusted_input_quantity_estimate'
    assert result['source_quantity_evidence']['weight_g'] == 800.0
    detail = {**result, 'ingredient': 'cannellini beans', 'original_ingredient_text': line}
    assert valid_quantity_adjustment(detail)
    for field, value in [('weight_g', 800.0), ('quantity_evidence', original['quantity_evidence']),
                         ('source_quantity_evidence', None), ('quantity_adjustment', None)]:
        changed = deepcopy(detail)
        changed[field] = value
        assert not valid_quantity_adjustment(changed)


def test_direct_source_mass_is_preserved_without_an_estimated_adjustment():
    line = '200g chickpeas'
    quantity = resolve_quantity('chickpeas', 5.0, line)
    result = effective_quantity(quantity, line)
    assert result == quantity
    assert result['quantity_evidence']['weight_g'] == 200.0
    assert 'quantity_adjustment' not in result
def test_saved_v4_drainage_evidence_remains_verifiable_after_per_serving_fix():
    from recipe_wrangler.matching.quantities import effective_quantity, resolve_quantity, valid_quantity_adjustment
    version = 'shared_source_quantities_v4_adjusted_evidence'
    line = '400g can lentils, rinsed and drained'
    detail = effective_quantity(resolve_quantity('lentils', 400, line), line, version=version)
    detail.update(ingredient='lentils', original_ingredient_text=line, quantity_version=version)
    assert detail['weight_g'] == 240
    assert valid_quantity_adjustment(detail)

