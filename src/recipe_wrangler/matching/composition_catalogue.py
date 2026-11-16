"""Offline, source-preserving composition annotations and deterministic lookup."""

from __future__ import annotations

import hashlib
import json
import re
import unicodedata
from collections import defaultdict
from dataclasses import asdict
from pathlib import Path
from typing import Iterable

from recipe_wrangler.matching.facets import EXTRACTOR_ID, extract_candidate_facets
from recipe_wrangler.matching.foodex2 import FoodEx2Catalogue

CATALOGUE_VERSION = 'composition_catalogue_v1'
# Local semantic mappings, not expert gold. No generic ancestry expansion.
FOODEX2_BASE_MAPPINGS = {
    'A031G': ('Hen eggs', {'base_identity': 'egg', 'biological_source': 'chicken'}),
    'A032B': ('Boiled eggs', {'base_identity': 'egg', 'processes': ['boiled'], 'preparation_state': 'cooked'}),
    'A032C': ('Fried eggs', {'base_identity': 'egg', 'processes': ['fried'], 'preparation_state': 'cooked'}),
    'A032D': ('Poached eggs', {'base_identity': 'egg', 'processes': ['poached'], 'preparation_state': 'cooked'}),
}
PHYSICAL_STATE_MAPPINGS = {'Powder': 'powder', 'Paste': 'paste', 'Liquid': 'liquid'}
COOKING = frozenset({'boiled', 'fried', 'poached', 'scrambled', 'baked', 'roasted', 'steamed',
                     'grilled', 'braised', 'blanched', 'sauteed', 'toasted'})
MISSING = frozenset({'', 'n_a', 'n/a', 'na', 'none', 'not available'})


def clean_value(value: object) -> str | None:
    text = str(value).strip() if value is not None else ''
    return None if text.casefold() in MISSING else text


def exact_description_key(text: str) -> str:
    """Ignore case, spacing and comma separators; retain order and every word.

    No quantity, qualifier, hyphen, plural or unknown word is discarded.
    """
    text = unicodedata.normalize('NFC', text).casefold()
    return ' '.join(re.sub(r'(?<!\d),|,(?!\d)', ' ', text).split())


def source_record_key(source: dict) -> str:
    return json.dumps([source['dataset'], source['release'], source['record_id']], ensure_ascii=False, separators=(',', ':'))


def validate_mappings(catalogue: FoodEx2Catalogue) -> None:
    for code, (label, _) in FOODEX2_BASE_MAPPINGS.items():
        term = catalogue.terms.get(code)
        if not term or term.label != label or term.status != 'APPROVED':
            raise ValueError(f'FoodEx2 mapping target changed: {code}')


def _has_value(value: object) -> bool:
    return value is not None and value != []


def annotate_record(record: dict, catalogue: FoodEx2Catalogue) -> dict:
    """Retain code-derived and label-derived proposals separately with conflicts."""
    label = record['label']
    extracted = extract_candidate_facets(label)
    rule_facets = json.loads(json.dumps(asdict(extracted.facets), default=sorted))
    issues = list(record.get('issues', []))
    if re.search(r'\b(?:not|never|or|for\s+(?:frying|boiling|baking|cooking))\b', label, re.I):
        issues.append('unsupported_label_context')
    if rule_facets['compound']:
        issues.append('possible_compound_or_property_conjunction')
    # Additional record-label rules are proposals, never substitutes for source codes.
    extra = {}
    if re.search(r'\beggs?\b', label, re.I):
        extents = re.findall(r'\b(hard|soft)[ -]boiled\b', label, re.I)
        if len(set(value.casefold() for value in extents)) == 1:
            extent = extents[0].casefold()
            extra['cooking_extent'] = extent + '_boiled'
            # Extent is represented separately, so it is not part of egg identity.
            rule_facets['base_identity'] = re.sub(r'\b' + extent + r'\b', '', rule_facets['base_identity']).strip()
    drained = bool(re.search(r'\bdrained\b', label, re.I))
    rinsed = bool(re.search(r'\brinsed\b', label, re.I))
    if drained:
        extra['drainage_status'] = 'drained_and_rinsed' if rinsed else 'drained'
    if re.search(r'\b(?:dissolved|reconstituted)\b', label, re.I):
        extra['reconstitution_state'] = 'reconstituted'
    for field, pattern in [('fat_content_description', r'\b\d+(?:\.\d+)?(?:\s*[-–]\s*\d+(?:\.\d+)?)?\s*%\s*fat\b')]:
        match = re.search(pattern, label, re.I)
        if match:
            extra[field] = match.group()
    rule_facets.update(extra)
    evidence = [
        {'facet': field, 'value': value, 'method': 'label_rule', 'source_field': 'label',
         'source_text': label, 'extractor_version': EXTRACTOR_ID,
         'review_status': 'unreviewed_proposal'}
        for field, value in rule_facets.items() if _has_value(value)
    ]
    source_facets = {}
    raw = record['metadata']
    code = clean_value(raw.get('FoodEx2 code'))
    code_evidence = None
    if code:
        resolved = catalogue.resolve(code)
        code_evidence = {
            'expression': code, 'catalogue_version': catalogue.version,
            'catalogue_sha256': catalogue.sha256, 'issues': list(resolved.issues),
            'terms': [{'code': term.code, 'label': term.label, 'status': term.status,
                       'implicit_expressions': list(term.implicit_facets)} for term in resolved.terms],
        }
        issues.extend(resolved.issues)
        base = code[:5]
        if not resolved.issues and base in FOODEX2_BASE_MAPPINGS:
            _, assertions = FOODEX2_BASE_MAPPINGS[base]
            source_facets.update(assertions)
            for field, value in assertions.items():
                evidence.append({'facet': field, 'value': value, 'method': 'source_code_mapping',
                                 'source_field': 'FoodEx2 code', 'source_term_id': base,
                                 'source_term_label': catalogue.terms[base].label,
                                 'catalogue_version': catalogue.version,
                                 'review_status': 'local_semantic_mapping_not_external_expert_gold'})
    physical_state = clean_value(raw.get('Physical state FoodEx2'))
    if physical_state in PHYSICAL_STATE_MAPPINGS:
        value = PHYSICAL_STATE_MAPPINGS[physical_state]
        source_facets['physical_form'] = value
        evidence.append({'facet': 'physical_form', 'value': value, 'method': 'source_field_mapping',
                         'source_field': 'Physical state FoodEx2', 'source_text': physical_state,
                         'review_status': 'local_semantic_mapping_not_external_expert_gold'})
    # Retain useful source descriptions without folding sample counts into identity.
    description_facets = {}
    description = clean_value(raw.get('Description'))
    if description:
        described = asdict(extract_candidate_facets(description).facets)
        for field in ('skin_status', 'bone_status', 'packing_medium', 'sodium_level',
                      'fat_level', 'sugar_level', 'grain_refinement', 'gluten_free'):
            if described[field] is not None:
                description_facets[field] = described[field]
                evidence.append({'facet': field, 'value': described[field],
                                 'method': 'description_rule', 'source_field': 'Description',
                                 'source_text': description, 'extractor_version': EXTRACTOR_ID,
                                 'review_status': 'unreviewed_proposal'})
    # Disagreements are visible; the detailed record wording remains the proposal.
    facets = dict(rule_facets)
    for field, value in description_facets.items():
        if not _has_value(facets.get(field)):
            facets[field] = value
        elif facets[field] != value:
            issues.append(f'label_description_disagreement:{field}')
    for field, value in source_facets.items():
        existing = facets.get(field)
        if not _has_value(existing):
            facets[field] = value
        elif existing != value:
            if field == 'processes':
                existing_cooking = set(existing) & COOKING
                source_cooking = set(value) & COOKING
                if existing_cooking and source_cooking and existing_cooking != source_cooking:
                    issues.append('source_label_disagreement:processes')
                elif not existing_cooking:
                    facets[field] = sorted(set(existing) | set(value))
            else:
                issues.append(f'source_label_disagreement:{field}')
    if 'unsupported_label_context' in issues:
        # Keep proposals for review, but do not expose context-blind assertions as usable.
        facets = {field: ([] if isinstance(value, list) else None) for field, value in facets.items()}
    return {**record, 'annotation_version': CATALOGUE_VERSION, 'facets': facets,
            'source_facets': source_facets, 'rule_facets': rule_facets,
            'description_rule_facets': description_facets,
            'annotation_evidence': evidence, 'foodex2_evidence': code_evidence,
            'issues': sorted(set(issues)), 'review_required': bool(issues),
            'annotation_status': 'offline_proposals_not_expert_gold',
            'nutrition_eligibility': 'not_assessed'}


def consolidate_sources(records: Iterable[dict]) -> list[dict]:
    """Collapse identical source metadata; preserve variants under the same native ID."""
    groups = defaultdict(dict)
    for record in records:
        source = record['source']
        if not all(clean_value(source.get(key)) for key in ('dataset', 'release', 'record_id')) or not clean_value(record.get('label')):
            raise ValueError('Source dataset/release/record ID and label are required')
        key = source_record_key(source)
        signature = hashlib.sha256(json.dumps({'label': record['label'], 'metadata': record['metadata']}, sort_keys=True, ensure_ascii=False, default=str).encode()).hexdigest()
        if signature not in groups[key]:
            groups[key][signature] = {**record, 'source_rows': []}
        groups[key][signature]['source_rows'].append(record['source_row'])
    result = []
    for key, variants in sorted(groups.items()):
        for signature, record in sorted(variants.items()):
            multiple = len(variants) > 1
            result.append({**record, 'source_record_key': key,
                           'catalogue_key': key + ('#variant:' + signature if multiple else ''),
                           'issues': ['source_id_multiple_metadata_variants'] if multiple else []})
    return result


class CompositionCatalogue:
    """Exact lookup and explicit facet filtering; never selects nutrient values."""

    def __init__(self, records: Iterable[dict], aliases: Iterable[dict] = ()) -> None:
        self.records = {}
        self.exact_index = defaultdict(set)
        for record in records:
            key = record['catalogue_key']
            if key in self.records:
                raise ValueError(f'Duplicate catalogue key: {key}')
            self.records[key] = record
            self.exact_index[exact_description_key(record['label'])].add(key)
        self.alias_index = defaultdict(set)
        for alias in aliases:
            if alias.get('status') != 'reviewed' or not alias.get('rationale'):
                raise ValueError('Aliases must be reviewed and have a rationale')
            if alias['catalogue_key'] not in self.records:
                raise ValueError('Alias references an unknown composition record')
            key = exact_description_key(alias['phrase'])
            if not key:
                raise ValueError('Alias phrase must not be empty')
            self.alias_index[key].add(alias['catalogue_key'])

    @classmethod
    def load(cls, directory: Path) -> CompositionCatalogue:
        with (directory / 'records.jsonl').open(encoding='utf-8') as stream:
            records = [json.loads(line) for line in stream if line.strip()]
        aliases = json.loads((directory / 'reviewed_aliases.json').read_text())['aliases']
        return cls(records, aliases)

    def lookup(self, text: str, dataset: str | None = None) -> dict:
        key = exact_description_key(text)
        # Literal labels take precedence; every eligible record at that tier is retained.
        in_scope = lambda item: dataset is None or self.records[item]['source']['dataset'] == dataset
        exact = {item for item in self.exact_index.get(key, set()) if in_scope(item)}
        keys = exact or {item for item in self.alias_index.get(key, set()) if in_scope(item)}
        candidates = [self.records[item] for item in sorted(keys)
                      if dataset is None or self.records[item]['source']['dataset'] == dataset]
        return {'query': text, 'lookup_key': key,
                'status': 'no_exact_description' if not candidates else 'unique_exact_description' if len(candidates) == 1 else 'ambiguous_exact_description',
                'method': 'exact_label' if exact else 'reviewed_alias' if keys else None,
                'candidates': candidates,
                'selection_policy': 'Record lookup only; source preference, annotation review and nutrient eligibility are not resolved.'}

    def find_facets(self, criteria: dict, dataset: str | None = None) -> tuple[dict, ...]:
        """Return proposed candidates only; missing facets do not satisfy a criterion."""
        allowed = set(next(iter(self.records.values()))['facets']) if self.records else set()
        if not criteria or not set(criteria) <= allowed or any(value is None or value == [] for value in criteria.values()):
            raise ValueError('Provide nonempty criteria using known facet fields')
        result = []
        for record in self.records.values():
            if record['review_required'] or (dataset and record['source']['dataset'] != dataset):
                continue
            def matches(field, value):
                actual = record['facets'].get(field)
                if isinstance(value, list):
                    return isinstance(actual, list) and set(value) <= set(actual)
                return actual == value
            if all(matches(field, value) for field, value in criteria.items()):
                result.append(record)
        return tuple(result)
