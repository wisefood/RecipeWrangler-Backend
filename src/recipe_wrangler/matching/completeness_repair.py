"""Prefer better-covered same-food records without inventing missing values."""
import re
import math
from recipe_wrangler.matching.nutrition_policy import calculation_decision
from recipe_wrangler.tools.nutrition_completeness import METRICS


def missing(values):
    return {metric for metric in METRICS if not isinstance(values.get(metric),(int,float))
            or isinstance(values.get(metric),bool) or not math.isfinite(values[metric]) or values[metric]<0}


def eligible_record(facts):
    """Salt cannot be a usable nutrition match without its defining nutrient."""
    annotation = (facts or {}).get('text_annotation') or {}
    identity = (annotation.get('facets') or {}).get('base_identity') or annotation.get('main_ingredient')
    label = (facts or {}).get('label') or ''
    values = (facts or {}).get('values_per_100g') or {}
    fat, saturated = values.get('fat_g'), values.get('saturated_fat_g')
    if isinstance(fat,(int,float)) and isinstance(saturated,(int,float)) and saturated > fat + .5:
        return False
    salt = identity == 'salt' or bool(re.match(r'^(?:sea salt|salt)(?:$|\s*,|\s+w/)', label, re.I))
    return not (salt and 'sodium_mg' in missing((facts or {}).get('values_per_100g') or {}))


def label_key(label):
    key = ' '.join(re.findall(r'[a-z0-9]+', label.casefold()))
    return 'sea salt' if key in {'sea salt', 'salt sea'} else key


def choose_complete_record(observed, current, rows):
    """Require a strict coverage gain and preserve known source characteristics."""
    old_values = (current or {}).get('values_per_100g') or {}
    old_missing = missing(old_values)
    if not old_missing:
        return None
    old_annotation = (current or {}).get('text_annotation') or observed
    old_facets = old_annotation.get('facets') or {}
    exact_label = label_key((current or {}).get('label') or observed.get('input_text',''))
    choices = []
    for row in rows:
        facts = row['annotation']
        annotation = facts.get('text_annotation') or {}
        facets = annotation.get('facets') or {}
        values = facts.get('values_per_100g') or {}
        new_missing = missing(values)
        same_label = bool(exact_label and exact_label == label_key(facts['label']))
        # Ontology heads can be too broad (black/sweet pepper, dairy/plant cheese).
        # This repair changes nutrition records, never the ingredient itself.
        if (not same_label or not new_missing < old_missing or not eligible_record(facts)):
            continue
        # A more complete table is not permission to change raw/cooked basis,
        # species, anatomical part, fat/salt class or a known product qualifier.
        if any(value not in (None, '', [], {}) and facets.get(key) != value
               for key, value in old_facets.items()
               if key not in {'identity', 'base_identity'}):
            continue
        old_qualifiers = old_annotation.get('qualifiers') or {}
        qualifiers = annotation.get('qualifiers') or {}
        if any(value not in (None, '', [], {}) and qualifiers.get(key) != value
               for key, value in old_qualifiers.items()):
            continue
        calculation = calculation_decision(observed, row['candidate'], facts, selected=True,
                                           alias_identity=same_label)
        if calculation['kind'] == 'unresolved':
            continue
        choices.append((len(new_missing), row['collection'], row['candidate_id'], row, calculation))
    if not choices:
        return None
    _, _, _, row, calculation = min(choices, key=lambda item:item[:3])
    return row, calculation
