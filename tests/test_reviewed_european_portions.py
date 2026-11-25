"""European defaults are source-faithful and limited to compatible food forms."""
import csv
from pathlib import Path
from unittest.mock import patch

import pytest

from recipe_wrangler.tools.reviewed_european_portions import european_size_reference, _PORTIONS
from recipe_wrangler.tools import ingredient_weight_tool as tool


def test_all_six_references_match_original_rivm_export():
    source = Path(__file__).resolve().parents[1] / 'analysis/portion-dataset-audit-2026-11-03/rivm-banana-export.csv'
    if not source.exists():
        pytest.skip('original RIVM export lives in gitignored analysis/')
    rows = {row['Maat']: row for row in csv.DictReader(source.open(encoding='utf-8-sig'), delimiter=';')}
    for size in ('small', 'medium', 'large'):
        for name in ('banana, peeled', 'banana with peel'):
            ref = european_size_reference(name, size)
            assert ref['grams_per_unit'] == float(rows[ref['portion_desc']]['Hoeveelheid'])
            assert ref['source_food_id'] == 'NEVO151'
            assert '2026/2.0, RIVM' in ref['source']
            assert not ref['basis_assumed']


def test_european_default_precedes_usda_and_scales_quantity():
    with patch.object(tool, 'match_portion', side_effect=AssertionError('European reference should win')):
        grams, ref, method = tool._reference_weight_fallback('banana', '2', 'medium', usda_id='09040')
    assert grams == 260
    assert method == 'reviewed_european_size_reference'
    assert ref['basis_assumed']
    assert ref['weight_basis'] == 'edible_without_peel'


@pytest.mark.parametrize('name,unit', [
    ('banana bread', 'medium'), ('banana flour', 'small'), ('banana chips', 'large'),
    ('sliced banana', 'medium'), ('cooked banana', 'medium'), ('plantain', 'medium'),
    ('banana', 'cup'), ('banana', 'piece'), ('papaya', 'small'),
    ('banana peeled with skin', 'small'), ('banana skin', 'small'),
    ('small banana', 'large'),
])
def test_incompatible_references_are_not_european_defaults(name, unit):
    assert european_size_reference(name, unit) is None


def test_all_reviewed_records_retain_original_source_values_and_labels():
    root = Path(__file__).resolve().parents[1]
    paths = list((root / 'analysis/weight-tool-refinement-2026-11-03/rivm-expansion').glob('*.csv'))
    if not paths:
        pytest.skip('original RIVM exports live in gitignored analysis/')
    paths.append(root / 'analysis/portion-dataset-audit-2026-11-03/rivm-banana-export.csv')
    originals = set()
    for path in paths:
        with path.open(encoding='utf-8-sig') as source:
            for row in csv.DictReader(source, delimiter=';'):
                originals.add((row['NEVO-code'], row['NEVO-naam'], row['Maat'], float(row['Hoeveelheid'].replace(',', '.'))))
    assert len(_PORTIONS) == 41
    assert len({key[0] for key in _PORTIONS}) == 8
    for (food, basis, size), row in _PORTIONS.items():
        assert row in originals
        name = food + (' with skin' if basis == 'with_peel' else ' peeled')
        ref = european_size_reference(name, size)
        assert ref['source_food_id'] == 'NEVO' + row[0]
        assert ref['source_food_name'] == row[1]
        assert ref['portion_desc'] == row[2]
        assert ref['grams_per_unit'] == row[3]


@pytest.mark.parametrize('name,size,grams,excluded', [
    ('apples', 'medium', 135, ['core']),
    ('apple peeled', 'medium', 120, ['core']),
    ('pear', 'medium', 214, ['core']),
    ('pear peeled', 'medium', 180, ['core']),
    ('orange', 'large', 170, []),
    ('orange with skin', 'large', 243, []),
    ('mandarins', 'small', 30, []),
    ('raw potatoes', 'medium', 88, []),
    ('potato peeled', 'medium', 70, []),
    ('cucumber', 'small', 304, ['waste']),
    ('cucumber peeled', 'large', 495, ['waste']),
    ('avocado', 'medium', 180, ['pit']),
])
def test_reviewed_defaults_preserve_food_and_edible_part(name, size, grams, excluded):
    ref = european_size_reference(name, size)
    assert ref['grams_per_unit'] == grams
    assert ref['excluded_parts'] == excluded


@pytest.mark.parametrize('name,size', [
    ('baked potato', 'medium'), ('sweet potato', 'medium'),
    ('new potato', 'small'), ('baby cucumber', 'small'),
    ('pickled cucumber', 'small'), ('chopped apple', 'medium'),
    ('apple with core', 'medium'), ('avocado with pit', 'medium'),
    ('avocado unpeeled', 'medium'), ('blood orange', 'medium'),
    ('clementine', 'small'), ('orange juice', 'medium'),
    ('pear', 'small'), ('pear', 'large'), ('zucchini', 'large'),
    ('carrot', 'medium'), ('white mushroom', 'small'),
    ('apple banana', 'medium'), ('apple skin', 'medium'),
    ('potato peeled with skin', 'medium'),
])
def test_unreviewed_sizes_varieties_parts_and_preparations_are_rejected(name, size):
    assert european_size_reference(name, size) is None
