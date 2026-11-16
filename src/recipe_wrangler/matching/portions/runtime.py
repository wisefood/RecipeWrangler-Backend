"""ID-validated portion matching with ranked approximations or strict audit mode."""
import re

from .core import PortionInput, PortionFacets, infer_facets, merge_facets, select_portion, rank_portions
from .sources import identity_key, reviewed_name_key, records_for_food, default_european_peel
from .sources import COUNT_UNITS, normalize_portion_name
from recipe_wrangler.utils import weigh_calculation_usda_ as usda


def match_request(name: str, unit: str | None, request: PortionInput,
                  resolved_usda_id: str | None = None, validated_food_id: str | None = None) -> dict:
    name = normalize_portion_name(name)
    observed, blockers = infer_facets(name)
    facets, conflicts = merge_facets(observed, request.facets)
    blockers += conflicts
    if unit is None:
        unit = 'piece'  # a bare number counts items ("2 apples")
    if unit in {'small', 'medium', 'large'}:
        facets, conflicts = merge_facets(facets, PortionFacets(size=unit))
        blockers += conflicts
        unit = 'piece'
    unit = {'pieces': 'piece', 'whole': 'piece', 'fruit': 'piece', 'potato': 'piece',
            'cups': 'cup', 'tbsp': 'tablespoon', 'tsp': 'teaspoon', 'cloves': 'clove',
            'rasher': 'slice', 'rashers': 'slice', 'cob': 'ear', 'cobs': 'ear'}.get(unit, unit)
    food_id = request.food_id or (identity_key(resolved_usda_id) if resolved_usda_id else validated_food_id or reviewed_name_key(name))
    if food_id and food_id.startswith('usda_weights:'):
        if food_id.split(':', 1)[1] != resolved_usda_id:
            blockers.append('unvalidated_food_id')
        food_id = identity_key(food_id.split(':', 1)[1])
    elif food_id and food_id.startswith('fndds_weights:'):
        if food_id != validated_food_id:
            blockers.append('unvalidated_food_id')
    elif food_id and food_id.startswith(('ausnut2023:', 'nz_concise2021:', 'fdc_foundation:')):
        if food_id != validated_food_id: blockers.append('unvalidated_food_id')
        from .external import external_identity
        food_id = external_identity(food_id) or food_id
    elif food_id and not food_id.startswith('ingredient:'):
        blockers.append('unsupported_id_namespace')
    if food_id and food_id.startswith('ingredient:') and reviewed_name_key(name) != food_id:
        blockers.append('food_name_id_mismatch')
    if food_id is None and resolved_usda_id:
        food_id = identity_key(resolved_usda_id)
    if food_id is None: blockers.append('unresolved_food_identity')
    if unit is None: blockers.append('missing_unit')
    records = records_for_food(food_id) if food_id and not blockers else []
    # "2 breasts chicken breast", "1 bulb fennel": a unit word naming the item counts whole items,
    # unless the sources measure in that unit themselves (garlic "clove").
    item_words = {usda._normalize_token(w) for text in [name, *(r.food_name for r in records)]
                  for w in re.findall(r'[a-z]+', text.lower())}
    if unit and unit != 'piece' and unit not in {r.unit for r in records} and usda._normalize_token(unit) in item_words:
        unit = 'piece'
    # "6 cherry tomatoes": the source lists the variety as its own count unit on a generic food.
    if unit == 'piece' and records:
        generic = {w for r in records for w in re.findall(r'[a-z]+', r.food_name.lower())}
        variety = ({r.unit for r in records} & set(re.findall(r'[a-z]+', name))) - generic - {'piece'}
        if len(variety) == 1:
            unit = variety.pop()
    assumptions = []
    # Recipes write "1 onion" for a medium one; the assumption is recorded, never silent.
    if unit == 'piece' and facets.size is None and request.match_mode != 'strict':
        facets = facets.model_copy(update={'size': 'medium'})
        assumptions.append({'facet': 'size', 'value': 'medium', 'policy': 'unsized_count_defaults_to_medium'})
    if food_id and facets.peel is None and (basis := default_european_peel(food_id)):
        facets = facets.model_copy(update={'peel': basis})
        assumptions.append({'facet': 'peel', 'value': basis, 'policy': 'reviewed_edible_basis_default'})
    if blockers:
        return {'status': 'unresolved', 'record': None, 'blockers': sorted(set(blockers)),
                'match_quality': 'unresolved', 'match_score': 0.0,
                'score_kind': 'heuristic_compatibility', 'approximations': [],
                'food_id': food_id, 'requested_facets': facets.model_dump(), 'assumptions': assumptions}
    matcher = select_portion if request.match_mode == 'strict' else rank_portions
    if unit == 'piece':
        named_units = {r.unit for r in records if r.unit in {'head', 'egg', 'nut'}}
        if len(named_units) == 1:
            unit = next(iter(named_units))
    unit = COUNT_UNITS.get(unit, unit)
    volumes = usda._unit_volumes_ml(str(usda.DEFAULT_UNIT_VOLUMES))
    result = matcher(records, food_id, unit, facets, request.region, volumes)
    result.update(food_id=food_id, requested_facets=facets.model_dump(), assumptions=assumptions)
    return result
