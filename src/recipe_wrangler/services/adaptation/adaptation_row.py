"""One answer shape for every adaptation mode: a `recipe_adaptations`-shaped row.
Stored row first; a recipe with no stored row, or a restricted request on a mode that is cheap to recompute, runs that mode's engine.
Only the engines differ per mode, the request and the row are the same."""
from __future__ import annotations

from .nutrition_reasons import WHY as NUTRITION_WHY
from .sustainability_reasons import WHY as SUSTAINABILITY_WHY
from .cost_reasons import WHY as COST_WHY
from .time_reasons import WHY as TIME_WHY


def _nutrition_live(recipe_id, region, allergens, diet):
    from .nutrition_adaptation import adapt_active_recipe, live_row
    from .service import _region_to_source
    return live_row(adapt_active_recipe(recipe_id, _region_to_source(region), allergens=allergens, diet=diet), region)


def _cost_live(recipe_id, region, allergens, diet):
    from .cost_live import adapt_active_recipe
    from .cost_llm import client
    return adapt_active_recipe(recipe_id, region, allergens=allergens, diet=diet, llm=client())


def _time_live(recipe_id, region, allergens, diet):
    from .cost_llm import client
    from .time_live import adapt_active_recipe
    return adapt_active_recipe(recipe_id, region, allergens=allergens, diet=diet, llm=client('TIME_ADAPT_LLM'))


def _sustainability_live(recipe_id, region, allergens, diet):
    from .sustainability_live import adapt_active_recipe
    return adapt_active_recipe(recipe_id, region, allergens=allergens, diet=diet)


# live engine, whether a restricted request recomputes instead of checking the stored swap, reason sentences
# ponytail: nutrition live takes minutes, so restricted nutrition checks the stored swap
MODES = {
    'nutrition': (_nutrition_live, False, NUTRITION_WHY),
    'sustainability': (_sustainability_live, True, SUSTAINABILITY_WHY),
    'cost': (_cost_live, True, COST_WHY),
    'time': (_time_live, True, TIME_WHY),
}


def adaptation_row(recipe_id, mode, region, allergens=(), diet=None):
    """The adaptation row for one recipe, mode and region, or None when nothing is stored and the mode has no engine."""
    from recipe_wrangler.repositories.recipe_adaptations import get_adaptation
    from .nutrition_adaptation import restrict_stored_row
    live, recompute_when_restricted, why = MODES[mode]
    restricted = bool(allergens or diet)
    row = get_adaptation(recipe_id, mode, region)
    if live and (row is None or (restricted and recompute_when_restricted)):
        row = live(recipe_id, region, allergens, diet)
        if row['status'] == 'failed':  # never answer a restricted request from a stored row the engine could not check
            row = dict(row, status='not_scored', is_adapted=False)
    elif row is not None and restricted:
        row = restrict_stored_row(row, allergens, diet)
    if row is not None and row['status'] != 'adapted':
        row['reason'] = why.get(row['reason_code']) or (row.get('provenance') or {}).get('reason')
    return row
