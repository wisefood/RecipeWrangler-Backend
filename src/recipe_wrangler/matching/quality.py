"""Production nutrition diagnostics; diagnostic limits are not accuracy guarantees."""
from __future__ import annotations

import math

from recipe_wrangler.utils.nutri_score import compute_nutri_score_with_breakdown

QUALITY_VERSION = 'nutrition_quality_v2_source_consistency'
# Tables round fat and saturated fat separately: milk at 0.021 g fat and 0.15 g saturated is noise, not a bad record.
SATURATED_FAT_ROUNDING_G = 0.5


def finalize_profile(profile: dict) -> dict:
    """Expose uncertainty and calculate scores only from complete nutrient inputs."""
    if profile.get('source_context_rematch_positions') or any(
        d.get('source_context_status')=='requires_rematching' for d in profile.get('details',[])
    ):
        raise ValueError('Original ingredient context changed; matching must be rerun before finalization')
    totals = profile.get('clean_totals') or {}
    per_serving = profile.get('clean_totals_per_serving') or {}
    partial = profile.get('supported_partial_totals_per_serving') or {}
    details = profile.get('details') or []
    flags = []
    for position,detail in enumerate(details):
        values=detail.get('values_per_100g') or {}
        fat=values.get('fat_g');saturated=values.get('saturated_fat_g')
        if (isinstance(fat,(int,float)) and isinstance(saturated,(int,float))
                and math.isfinite(fat) and math.isfinite(saturated) and saturated>fat+SATURATED_FAT_ROUNDING_G):
            flags.append('source:saturated_fat_exceeds_total_fat:position='+str(detail.get('position',position)))
    for basis, values in [('complete', per_serving), ('partial_lower_bound', partial)]:
        energy = values.get('energy_kcal')
        if energy is None:
            continue
        if not math.isfinite(energy) or energy < 0:
            flags.append(basis + ':invalid_energy')
        elif energy >= 3000:
            flags.append(basis + ':energy_at_least_3000_kcal_per_serving')
        elif energy >= 1000:
            flags.append(basis + ':energy_at_least_1000_kcal_per_serving')
    unknown_quantities = [d.get('position', i) for i, d in enumerate(details)
                          if d.get('weight_g', 0) > 0 and
                          d.get('quantity_status') in {'retained_input_quantity_unverified', 'seasoning_policy_estimate'}]
    incomplete = any(value is None for value in totals.values()) or not totals
    quality = {'version': QUALITY_VERSION,
               'status': 'needs_validation' if flags else 'incomplete' if incomplete else 'estimated',
               'diagnostic_flags': flags,
               'unverified_quantity_positions': unknown_quantities,
               'missing_servings': not profile.get('serves'),
               'independently_validated': False,
               'threshold_purpose': 'diagnostic_only',
               'nutrition_status': profile.get('nutrition_status')}
    profile['nutrition_quality'] = quality
    profile['profiling_quality'] = {**(profile.get('profiling_quality') or {}),
        'nutrition_quality': quality,
        'matching_metadata': {k: profile.get(k) for k in
                             ('mechanism_version', 'catalogue_release', 'quantity_version', 'quantity_input_hash')},
        'nutrition_completeness': {k: profile.get(k) for k in
            ('nutrition_status', 'nutrient_status', 'supported_partial_totals',
             'supported_partial_totals_per_serving', 'missing_contributions')}}
    total_weight = sum(d.get('weight_g') or 0 for d in details)
    matched_weight = sum(d.get('weight_g') or 0 for d in details
                         if d.get('matched_nutritional_ingredient') or d.get('nutrition_intentionally_ignored'))
    coverage = round(matched_weight / (total_weight or 1), 4)
    profile['profiling_quality'].update(nutrition_coverage=coverage,
        nutrition_low_coverage=coverage < .8, matching_effective_total_weight_g=total_weight)
    mapping = {'energy_kcal': ('Energy', 4.184), 'sugar_g': ('Sugars, total', 1),
        'saturated_fat_g': ('Fatty acids, total saturated', 1),
        'sodium_mg': ('Sodium, Na', 1), 'fibre_g': ('Fiber, total dietary', 1),
        'protein_g': ('Protein', 1)}
    score = None
    score_blockers = [flag for flag in flags if 'invalid_energy' in flag or flag.startswith('source:')]
    quality['score_blockers'] = score_blockers
    # A large serving deserves review, but its calorie warning is not invalid nutrition.
    if not score_blockers and all(totals.get(key) is not None for key in mapping):
        score = compute_nutri_score_with_breakdown(
            {'nutrients': {label: {'value': totals[key] * factor}
                           for key, (label, factor) in mapping.items()}},
            [{'name': d.get('ingredient') or d.get('name'), 'weight_grams': d['weight_g']}
             for d in details])
    profile['nutri_score_breakdown'] = None
    profile['nutri_score'] = None
    if score and 'error' not in score:
        profile['nutri_score_breakdown'] = score.pop('breakdown', None)
        profile['nutri_score'] = score
        quality['score_status'] = 'estimated'
    else:
        quality['score_status'] = 'unavailable_quality_flags' if score_blockers else 'unavailable_incomplete_inputs'
    return profile
