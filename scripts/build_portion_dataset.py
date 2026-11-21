"""Export source-preserving USDA/RIVM portion records; Fineli is never an input."""
import argparse
from collections import Counter
import hashlib
import json
from pathlib import Path

from recipe_wrangler.matching.portions.sources import usda_records, european_records
from recipe_wrangler.matching.portions import core, sources
from recipe_wrangler.matching.portions.external import external_index, DEFAULT_EXTERNAL
from recipe_wrangler.tools import reviewed_european_portions
from recipe_wrangler.utils.weigh_calculation_usda_ import _unit_volumes_ml


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--usda', type=Path, required=True)
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--limit-foods', type=int)
    parser.add_argument('--unit-volumes', type=Path, default=Path('data/nutrition/matching/aliases/unit_volume_ml_ground_truth.json'))
    args = parser.parse_args()
    rows = json.loads(args.usda.read_text())
    unit_volumes = _unit_volumes_ml(str(args.unit_volumes))
    if args.limit_foods is not None:
        if args.limit_foods <= 0: parser.error('--limit-foods must be positive')
        rows = rows[:args.limit_foods]
    args.output.parent.mkdir(parents=True, exist_ok=True)
    counts = Counter(); blockers = Counter(); identities = set(); seen = set()
    source_ids = Counter(); duplicates = 0; blocked_records = 0
    with args.output.open('w') as stream:
        external = (r for group in external_index()[1].values() for r in group)
        for records in (usda_records(rows, unit_volumes), european_records(), external):
            for record in records:
                if record.record_id in seen:
                    duplicates += 1
                    continue
                seen.add(record.record_id); identities.add(record.food_id)
                counts[record.region] += 1; blockers.update(record.blockers)
                source_ids[record.source_food_id.split(':', 1)[0]] += 1
                blocked_records += bool(record.blockers)
                stream.write(record.model_dump_json() + '\n')
    manifest = {'source_sha256': hashlib.sha256(args.usda.read_bytes()).hexdigest(),
                'external_sha256': hashlib.sha256(DEFAULT_EXTERNAL.read_bytes()).hexdigest() if DEFAULT_EXTERNAL.exists() else None,
                'unit_volume_profile_sha256': hashlib.sha256(args.unit_volumes.read_bytes()).hexdigest(),
                'transform_sha256': {p: hashlib.sha256(Path(p).read_bytes()).hexdigest()
                                     for p in (core.__file__, sources.__file__, sources.usda.__file__, __file__)},
                'runtime_european_module_sha256': hashlib.sha256(Path(reviewed_european_portions.__file__).read_bytes()).hexdigest(),
                'records': sum(counts.values()), 'records_by_region': dict(counts),
                'records_by_native_namespace': dict(source_ids),
                'source_portion_rows': sum(len(row.get('portions') or []) for row in rows),
                'duplicate_records_removed': duplicates, 'records_with_blockers': blocked_records,
                'food_identities': len(identities), 'blocker_counts': dict(blockers),
                'output_sha256': hashlib.sha256(args.output.read_bytes()).hexdigest(),
                'limit_foods': args.limit_foods,
                'policy': 'Native IDs namespaced; only explicit reviewed crosswalks. Unknown facets remain null. Blocked records retained for review. Fineli excluded. USDA release lineage remains unverified.'}
    args.output.with_suffix('.manifest.json').write_text(json.dumps(manifest, indent=2)+'\n')
    print(json.dumps(manifest, indent=2))


if __name__ == '__main__':
    main()
