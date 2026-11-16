"""Build attributed physical-portion records from audited public source exports."""
import argparse
from collections import Counter
import hashlib
import json
from pathlib import Path
from recipe_wrangler.matching.portions.external import external_records
from recipe_wrangler.matching.portions import external, core, sources


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--limit', type=int)
    args = parser.parse_args()
    root = Path(__file__).resolve().parents[1]
    audit = root/'analysis/portion-dataset-audit-2026-11-03'
    paths = [audit/'ausnut-reference-candidates.json',audit/'nz-portions.json',audit/'foundation-portion-candidates.json',root/'data/processed/usda/usda-weights-v2.json']
    collections = [json.loads(path.read_text()) for path in paths]
    if args.limit:
        collections = [rows[:args.limit] for rows in collections[:3]] + collections[3:]
    counts = Counter(); mapped = 0; blocked = 0; seen = set()
    args.output.parent.mkdir(parents=True, exist_ok=True)
    with args.output.open('w') as stream:
        for record in external_records(*collections):
            if record.record_id in seen: continue
            seen.add(record.record_id)
            counts[record.source_food_id.split(':')[0]] += 1
            mapped += record.food_id != record.source_food_id
            blocked += bool(record.blockers)
            stream.write(record.model_dump_json()+'\n')
    manifest = {'records':sum(counts.values()),'by_source':dict(counts),'exact_full_label_crosswalk_records':mapped,'blocked':blocked,
                'source_sha256':{str(path.relative_to(root)):hashlib.sha256(path.read_bytes()).hexdigest() for path in paths},
                'output_sha256':hashlib.sha256(args.output.read_bytes()).hexdigest(),
                'transform_sha256':{str(Path(module.__file__).relative_to(root)):hashlib.sha256(Path(module.__file__).read_bytes()).hexdigest() for module in (external, core, sources)},
                'policy':'Native IDs distinct. Exact full-label crosswalks only. Density g/mL kept distinct from portion grams. Original fields, labels, attribution and terms retained. No Fineli.'}
    args.output.with_suffix('.manifest.json').write_text(json.dumps(manifest,indent=2)+'\n')
    print(json.dumps(manifest,indent=2))


if __name__=='__main__': main()
