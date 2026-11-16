"""Field mapping to TASTEset with evaluation-only measurement value projection.

Recipe measurements combine quantity/unit. Facet values are normalized claims,
whereas TASTEset annotates source spans. Candidate labels preserve these gaps.
"""
from __future__ import annotations

from copy import deepcopy

from recipe_wrangler.matching.evaluation.measurement_split import split_measurement

VERSION = 'tasteset_field_mapping_v2_measurement_values'
LABELS = ('FOOD', 'UNIT', 'QUANTITY', 'PHYSICAL_QUALITY', 'PROCESS', 'COLOR',
          'TASTE', 'PURPOSE', 'PART', 'TRADE_NAME', 'DIET', 'EXAMPLE',
          'EXCLUDED', 'EXCLUSIVE', 'POSSIBLE_SUBSTITUTE')
RECIPE_FIELDS = {'ingredient_names': ('FOOD',), 'ingredient_match_names': ('FOOD',),
                 'measurements': ('QUANTITY', 'UNIT')}
FIELD_LABELS = {
    'colour': ('COLOR',), 'food_part': ('PART',), 'processes': ('PROCESS',),
    'physical_form': ('PHYSICAL_QUALITY',), 'size': ('PHYSICAL_QUALITY',),
    'thickness': ('PHYSICAL_QUALITY',), 'ripeness': ('PHYSICAL_QUALITY',),
    'texture': ('PHYSICAL_QUALITY',), 'particle_size': ('PHYSICAL_QUALITY',),
    'preparation_state': ('PROCESS', 'PHYSICAL_QUALITY'),
    'preservation_methods': ('PROCESS', 'PHYSICAL_QUALITY'),
    'cooking_extent': ('PROCESS', 'PHYSICAL_QUALITY'),
    'drainage_status': ('PROCESS', 'PHYSICAL_QUALITY'),
    'reconstitution_state': ('PROCESS', 'PHYSICAL_QUALITY'),
    'grain_refinement': ('DIET', 'PHYSICAL_QUALITY'),
    'fat_level': ('DIET', 'PHYSICAL_QUALITY'),
    'fat_content_description': ('DIET', 'PHYSICAL_QUALITY'),
    'sugar_level': ('DIET', 'TASTE'), 'sodium_level': ('DIET', 'TASTE'),
    'gluten_free': ('DIET',), 'purpose': ('PURPOSE',),
    'context.ingredient_role': ('PURPOSE',),
    'qualifiers.portion_unit': ('UNIT',),
    'qualifiers.flavours': ('TASTE', 'FOOD'),
    'qualifiers.excluded_ingredients': ('EXCLUDED',),
}
NOTES = {
    'FOOD': 'Core/matching names and food heads differ in extent and normalization; preserve both.',
    'QUANTITY': 'measurement_splits[].quantity derives simple numeric values; complex forms require review. No recipe-source span is inferred.',
    'UNIT': 'measurement_splits[].unit normalizes explicit unit aliases. Bare counts retain unit=None; no unit or gram weight is inferred.',
    'PHYSICAL_QUALITY': 'Several facets correspond partially; boundaries and label meaning need review.',
    'PROCESS': 'Process events are closest; state/preservation claims can overlap physical quality.',
    'COLOR': 'colour is the spelling counterpart; extraction coverage and source extent still differ.',
    'TASTE': 'Sugar/sodium qualifiers and flavour identities are not general sensory-taste annotations.',
    'PURPOSE': 'Ingredient role and intended-use events cover only part of this label.',
    'PART': 'food_part is the closest counterpart; body-part/head boundaries can differ.',
    'TRADE_NAME': 'No dedicated exported field; the recipe prompt deliberately removes brand names.',
    'DIET': 'Explicit qualifier claims are partial counterparts, not whole-recipe suitability labels.',
    'EXAMPLE': 'No dedicated exported example relation.',
    'EXCLUDED': 'Excluded food heads/ingredients are partial counterparts; negated modifiers stay distinct.',
    'EXCLUSIVE': 'No dedicated exported exclusive-ingredient relation; optional does not mean exclusive.',
    'POSSIBLE_SUBSTITUTE': 'No dedicated field in these exports; alternative process events are not ingredient substitutes.',
}
UNSUPPORTED = {'TRADE_NAME', 'EXAMPLE', 'EXCLUSIVE', 'POSSIBLE_SUBSTITUTE'}


def normalize_label(label: str) -> str:
    """Accept the published space/underscore label spellings, rejecting unknowns."""
    normalized = '_'.join(label.strip().upper().split())
    if normalized not in LABELS:
        raise ValueError('Unknown TASTEset label: ' + label)
    return normalized


def mapping_contract() -> list[dict]:
    """Return all fifteen labels, including unsupported and ambiguous relations."""
    return [{'label': label,
             'recipe_fields': [key + '[]' for key, values in RECIPE_FIELDS.items() if label in values],
             'facet_fields': (['foods[].text', 'foods[].identity'] if label == 'FOOD' else
                              ['foods[polarity=excluded].text', 'foods[polarity=excluded].identity'] if label == 'EXCLUDED' else []) +
                             [key if key.startswith(('context.', 'qualifiers.')) else 'facets.' + key
                              for key, values in FIELD_LABELS.items() if label in values and key != 'purpose'],
             'modifier_field_values': [key for key, values in FIELD_LABELS.items() if label in values],
             'comparison_fields': ['measurement_splits[].' + ('quantity' if label == 'QUANTITY' else 'unit')] if label in {'QUANTITY', 'UNIT'} else [],
             'relationship': 'unsupported' if label in UNSUPPORTED else
                             'deterministic_value_projection' if label in {'QUANTITY', 'UNIT'} else 'partial',
             'notes': NOTES[label]} for label in LABELS]


def _record(path: str, value, labels: tuple[str, ...], **extra) -> dict:
    return {'source_field': path, 'value': deepcopy(value), 'candidate_labels': list(labels),
            'mapping_status': 'unmapped' if not labels else 'ambiguous' if len(labels) > 1 else 'candidate',
            **extra}


def project_recipe_output(parsed: dict) -> dict:
    """Preserve recipe exports and derive separate measurement values for evaluation."""
    present = [parsed[key] for key in RECIPE_FIELDS if key in parsed]
    if any(not isinstance(values, list) or any(not isinstance(v, str) for v in values) for values in present):
        raise ValueError('Recipe parser fields must be lists of strings')
    if present and len({len(values) for values in present}) != 1:
        raise ValueError('Recipe parser occurrence vectors are not aligned')
    records = [_record(f'{field}[{position}]', value, labels, position=position,
                       representation='combined_measurement' if field == 'measurements' else 'normalized_name', span=None)
               for field, labels in RECIPE_FIELDS.items() for position, value in enumerate(parsed.get(field, []))]
    return {'mapping_version': VERSION, 'stage': 'recipe_parser', 'records': records,
            'measurement_splits': [{'position': position, **split_measurement(value)}
                                   for position, value in enumerate(parsed.get('measurements', []))],
            'exact_span_predictions': False, 'scoring_policy_finalized': False}


def project_facet_output(annotation: dict) -> dict:
    """Map exported per-line annotations, retaining polarity and their coordinate frame."""
    coordinate_text = annotation.get('parsed_text', annotation.get('original_text'))
    if not isinstance(coordinate_text, str):
        raise ValueError('Facet annotation requires its coordinate text')
    raw_text = annotation.get('input_text', coordinate_text)
    records = []
    def event(path, value, labels, item):
        start, end = item.get('start'), item.get('end')
        quote = item.get('quote', item.get('text'))
        valid = (type(start) is int and type(end) is int and 0 <= start < end <= len(coordinate_text)
                 and isinstance(quote, str) and coordinate_text[start:end] == quote)
        records.append(_record(path, value, labels, quote=quote, span=[start, end] if valid else None,
            supplied_span_valid=valid, polarity=item.get('polarity'), target_food_id=item.get('target_food_id'),
            representation='source_event', span_scope='annotation_coordinate_text'))
    for i, food in enumerate(annotation.get('foods', [])):
        event(f'foods[{i}]', food.get('identity'), ('EXCLUDED',) if food.get('polarity') == 'excluded' else ('FOOD',), food)
    for i, item in enumerate(annotation.get('modifiers', [])):
        event(f'modifiers[{i}].{item["field"]}', item.get('value'), FIELD_LABELS.get(item['field'], ()), item)
    for group in ('facets', 'qualifiers', 'context'):
        for key, value in annotation.get(group, {}).items():
            if value is None or value == '' or value == [] or value == ():
                continue
            lookup = key if group == 'facets' else group + '.' + key
            records.append(_record(group + '.' + key, value, FIELD_LABELS.get(lookup, ()),
                                   representation='normalized_value', span=None))
    return {'mapping_version': VERSION, 'stage': 'contextual_facet_annotation',
            'parser_version': annotation.get('parser_version'), 'annotation_version': annotation.get('annotation_version'),
            'coordinate_text': coordinate_text, 'raw_input_text': raw_text,
            'coordinates_match_raw_input': coordinate_text == raw_text, 'records': records,
            'exact_span_predictions': False, 'scoring_policy_finalized': False}
