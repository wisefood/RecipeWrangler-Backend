"""Export successful HTTP nutrition drafts without changing production recipes."""
import argparse
import hashlib
import json
from pathlib import Path


def export_receipt(receipt_path: Path, output_directory: Path) -> Path | None:
    receipt = json.loads(receipt_path.read_text())
    response = receipt['response']
    card = response.get('adapted_recipe')
    if receipt.get('http_status') != 200 or response.get('status') != 'draft_requires_review' or not card:
        return None
    original_id = receipt['recipe_id']
    assert receipt['request']['mode'] == 'nutrition'
    assert card['instructions'] and card['ingredients'] and card['nutrition']
    assert receipt['source_and_active_profile_unchanged']
    assert response['final_objective_approval']['status'] == 'approved_input_objective'
    exported = {
        'original_recipe_id': original_id,
        'scope': 'nutrition',
        'status': response['status'],
        'baseline_run_id': receipt['baseline_run_id'],
        'request': receipt['request'],
        'adapted_recipe': card,
        'comparison': response['comparison'],
        'objective_approval': response['final_objective_approval'],
        'uncertainties': response['uncertainties'],
        'source_receipt': str(receipt_path),
        'source_receipt_sha256': hashlib.sha256(receipt_path.read_bytes()).hexdigest(),
    }
    # Hash the original ID instead of treating external identifiers as file paths.
    key = hashlib.sha256(original_id.encode()).hexdigest()[:16]
    target = output_directory / f'nutrition-{key}.json'
    output_directory.mkdir(parents=True, exist_ok=True)
    if target.exists():
        assert json.loads(target.read_text()) == exported, f'Conflicting existing export: {target}'
    else:
        with target.open('x') as stream:
            json.dump(exported, stream, indent=2, ensure_ascii=False, allow_nan=False)
    return target


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('receipts', nargs='+', type=Path)
    parser.add_argument('--output-directory', type=Path,
                        default=Path('analysis/evaluation-archive-2026-11-12/adaptation/nutrition-2026-11-10/adapted-recipes'))
    args = parser.parse_args()
    for receipt_path in args.receipts:
        result = export_receipt(receipt_path, args.output_directory)
        print(json.dumps({'receipt': str(receipt_path), 'export': str(result) if result else None}))
