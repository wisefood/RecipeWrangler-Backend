"""Build an offline English dietary lookup from a pinned OFF ingredients file.

Usage: python scripts/preprocessing/build_dietary_ingredient_evidence.py SOURCE MANIFEST OUTPUT
The manifest must include the downloaded source SHA-256, URL and commit.
"""
from __future__ import annotations

from collections import defaultdict
import hashlib
import json
from pathlib import Path
import re
import sys


def build(text: str) -> dict:
    nodes = []
    aliases = defaultdict(set)
    for block in re.split(r"\n\s*\n", text):
        names = {}
        parents = []
        properties = {}
        allergens = []
        for raw in block.splitlines():
            line = raw.split('#', 1)[0].strip()
            if match := re.fullmatch(r"(vegan|vegetarian):en:\s*(yes|no|maybe)", line):
                properties[match[1]] = match[2]
            elif match := re.fullmatch(r"allergens:en:\s*(.+)", line):
                allergens.extend(v.strip() for v in match[1].split(','))
            elif match := re.fullmatch(r"<\s*([a-z]{2}):\s*(.+)", line):
                parents.append((match[1], match[2].casefold().strip()))
            elif match := re.fullmatch(r"([a-z]{2}):\s*(.+)", line):
                names.setdefault(match[1], []).extend(v.strip() for v in match[2].split(',') if v.strip())
        if not names:
            continue
        language = 'en' if 'en' in names else next(iter(names))
        node = {'id': language + ':' + names[language][0], 'names': names,
                'parents': parents, 'properties': properties, 'allergens': allergens}
        index = len(nodes)
        nodes.append(node)
        for lang, values in names.items():
            for value in values:
                aliases[(lang, value.casefold())].add(index)

    cache = {}

    def resolve(index, diet, visiting=frozenset()):
        key = (index, diet)
        if key in cache:
            return cache[key]
        if index in visiting:
            return {'value': 'maybe', 'sources': ['taxonomy_cycle']}
        node = nodes[index]
        blocking = {'en:fish', 'en:crustaceans', 'en:molluscs'}
        if diet == 'vegan':
            blocking |= {'en:milk', 'en:eggs'}
        declared = blocking.intersection(node['allergens'])
        if declared:
            # Direct constituent evidence can resolve a parent's "maybe".
            # A contradictory explicit yes remains uncertain for review.
            return {'value': 'maybe' if node['properties'].get(diet) == 'yes' else 'no',
                    'sources': [node['id'], *('allergen:' + a for a in sorted(declared))]}
        if diet in node['properties']:
            return {'value': node['properties'][diet], 'sources': [node['id']]}
        inherited = []
        for parent in node['parents']:
            candidates = aliases.get(parent, set())
            if len(candidates) == 1:
                inherited.append(resolve(next(iter(candidates)), diet, visiting | {index}))
            elif len(candidates) > 1:
                inherited.append({'value': 'maybe', 'sources': ['ambiguous_parent']})
        evidence = [value for value in inherited if value['value'] is not None]
        values = {value['value'] for value in evidence}
        result = {'value': next(iter(values)) if len(values) == 1 else 'maybe' if values else None,
                  'sources': sorted({source for value in evidence for source in value['sources']})}
        cache[key] = result
        return result

    entries = []
    for index, node in enumerate(nodes):
        if 'en' not in node['names']:
            continue
        values = {diet: resolve(index, diet) for diet in ('vegan', 'vegetarian')}
        entries.append({'id': node['id'], 'names': node['names']['en'], **values})
    return {'entries': entries}


def main():
    source, manifest_path, output = map(Path, sys.argv[1:])
    manifest = json.loads(manifest_path.read_text())
    assert hashlib.sha256(source.read_bytes()).hexdigest() == manifest['sha256']
    result = build(source.read_text())
    result['source'] = {'name': 'Open Food Facts contributors', **manifest}
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(result, ensure_ascii=False, separators=(',', ':')) + '\n')
    print(json.dumps({'entries': len(result['entries']), 'bytes': output.stat().st_size}))


if __name__ == '__main__':
    main()
