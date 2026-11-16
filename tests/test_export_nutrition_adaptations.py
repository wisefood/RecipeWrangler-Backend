"""Evaluation exports preserve draft provenance and reject unsuccessful cases."""
import json

import pytest

from scripts.eval.export_nutrition_adaptations import export_receipt


def receipt():
    return {'recipe_id': '../original-id', 'http_status': 200, 'baseline_run_id': 'run-1',
            'request': {'mode': 'nutrition'}, 'source_and_active_profile_unchanged': True,
            'response': {'status': 'draft_requires_review',
                         'adapted_recipe': {'ingredients': ['ingredient'], 'instructions': ['Cook.'],
                                            'nutrition': {'per_serving': {'energy_kcal': 100}}},
                         'final_objective_approval': {'status': 'approved_input_objective'},
                         'comparison': {'baseline': 'run-1'}, 'uncertainties': ['Draft.']}}


def test_export_preserves_card_and_original_id_and_is_idempotent(tmp_path):
    source = tmp_path / 'receipt.json'
    data = receipt()
    source.write_text(json.dumps(data))
    directory = tmp_path / 'exports'
    target = export_receipt(source, directory)
    saved = json.loads(target.read_text())
    assert target.parent == directory
    assert saved['original_recipe_id'] == '../original-id'
    assert saved['scope'] == 'nutrition'
    assert saved['adapted_recipe'] == data['response']['adapted_recipe']
    assert export_receipt(source, directory) == target
    data['response']['uncertainties'].append('Changed receipt.')
    source.write_text(json.dumps(data))
    with pytest.raises(AssertionError, match='Conflicting existing export'):
        export_receipt(source, directory)


@pytest.mark.parametrize('status', ['unavailable', 'blocked', 'uncertain'])
def test_unsuccessful_receipts_are_not_exported(tmp_path, status):
    data = receipt()
    data['response']['status'] = status
    source = tmp_path / 'receipt.json'
    source.write_text(json.dumps(data))
    assert export_receipt(source, tmp_path / 'exports') is None
    assert not (tmp_path / 'exports').exists()
